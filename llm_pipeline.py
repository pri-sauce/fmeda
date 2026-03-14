import json
import pandas as pd
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE        = 'fusa_ai_agent_mock_data.xlsx'      # <-- your excel file
BLK_SHEET         = 'BLK'                    # sheet with block names & functions
PDF_MODES_FILE    = 'pdf_extracted.json'     # output from pdf_extractor.py
OUTPUT_FILE       = 'llm_output.json'
OLLAMA_MODEL      = 'qwen3.5:0.8b'                 # change to your ollama model name
OLLAMA_URL        = 'http://localhost:11434/api/generate'
# ─────────────────────────────────────────────────────────────────────────────


# ── STEP 1: Extract all sheets from Excel ────────────────────────────────────
def extract_excel(filepath):
    xl = pd.ExcelFile(filepath)
    print(f"Sheets found: {xl.sheet_names}")
    all_sheets = {}
    for sheet in xl.sheet_names:
        df = pd.read_excel(filepath, sheet_name=sheet, dtype=str).fillna('')
        all_sheets[sheet] = df.to_dict(orient='records')
    return all_sheets


# ── STEP 2: Extract block names + functions from BLK sheet ───────────────────
def extract_blocks(all_sheets, sheet_name):
    if sheet_name not in all_sheets:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {list(all_sheets.keys())}")

    rows = all_sheets[sheet_name]
    print(f"\nBLK sheet columns: {list(rows[0].keys()) if rows else 'empty'}")

    # Print first few rows so user can verify
    print("First 3 rows of BLK sheet:")
    for r in rows[:3]:
        print(f"  {r}")

    blocks = []
    for row in rows:
        vals = [str(v).strip() for v in row.values() if str(v).strip()]
        if len(vals) >= 2:
            blocks.append({
                "block_name": vals[0],
                "function": vals[1]
            })
        elif len(vals) == 1:
            blocks.append({
                "block_name": vals[0],
                "function": ""
            })

    return blocks


# ── STEP 3: Load PDF failure modes ───────────────────────────────────────────
def load_pdf_modes(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


# ── STEP 4: Ask LLM to match failure modes for each block ────────────────────
def query_ollama(prompt, model):
    response = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1}
    })
    response.raise_for_status()
    return response.json()["response"].strip()


def match_failure_modes(block, pdf_modes, model):
    # Flatten all part entries from pdf for context
    pdf_summary = []
    for part in pdf_modes:
        for entry in part.get("entries", []):
            modes = entry.get("modes", [])
            for mode in modes:
                pdf_summary.append({
                    "part_name": part["part_name"],
                    "description": entry.get("description", ""),
                    "mode": mode
                })

    prompt = f"""You are a functional safety expert analyzing IC failure modes.

BLOCK INFORMATION:
- Block Name: {block['block_name']}
- Function: {block['function']}

POSSIBLE FAILURE MODES (from IEC standard, part name + description + mode):
{json.dumps(pdf_summary, indent=2)}

TASK:
Based on the block's function, identify which failure modes from the list above are applicable to this block.
Return ONLY a JSON array of strings, each string being one applicable standard failure mode.
Return only modes that genuinely apply. Do not add explanation or extra text.

Example output format:
["Output is stuck (i.e. high or low)", "Output is floating (i.e. open circuit)"]

Return only the JSON array, nothing else."""

    raw = query_ollama(prompt, model)

    # Parse the JSON array from response
    try:
        # Strip any markdown fences
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        modes = json.loads(clean)
        if isinstance(modes, list):
            return modes
    except Exception:
        pass

    # Fallback: extract quoted strings
    import re
    return re.findall(r'"([^"]+)"', raw)


# ── STEP 5: Build output JSON ─────────────────────────────────────────────────
def build_output(blocks, pdf_modes, model):
    result = []
    total = len(blocks)

    for i, block in enumerate(blocks):
        print(f"Processing [{i+1}/{total}]: {block['block_name']} ...")
        matched_modes = match_failure_modes(block, pdf_modes, model)

        rows = []
        for seq, mode in enumerate(matched_modes, start=1):
            rows.append({
                "Failure Mode Number": str(seq),
                "Standard failure mode": mode,
                # All other fields intentionally empty
                "Block Failure rate [FIT]": "",
                "Failure rate [FIT]": "",
                "Percentage of Safe Faults": "",
                "Safety mechanism(s) (IC) allowing to prevent the violation of the safety goal": "",
                "Failure mode coverage wrt. violation of safety goal": "",
                "Residual or Single Point Fault failure rate [FIT]": "",
                "Latent Multiple Point Fault failure rate [FIT]": ""
            })

        result.append({
            "block_name": block["block_name"],
            "function": block["function"],
            "rows": rows
        })

    return result


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=== Step 1: Extracting Excel ===")
    all_sheets = extract_excel(EXCEL_FILE)

    print("\n=== Step 2: Extracting BLK sheet ===")
    blocks = extract_blocks(all_sheets, BLK_SHEET)
    print(f"Found {len(blocks)} blocks:")
    for b in blocks:
        print(f"  - {b['block_name']}: {b['function'][:60]}...")

    print("\n=== Step 3: Loading PDF failure modes ===")
    pdf_modes = load_pdf_modes(PDF_MODES_FILE)
    print(f"Loaded {len(pdf_modes)} parts from PDF")

    print(f"\n=== Step 4: Querying LLM ({OLLAMA_MODEL}) ===")
    output = build_output(blocks, pdf_modes, OLLAMA_MODEL)

    print("\n=== Step 5: Saving output ===")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Done! Output saved to {OUTPUT_FILE}")
    print(f"   {len(output)} blocks processed")
    for item in output:
        print(f"   {item['block_name']}: {len(item['rows'])} failure modes assigned")
