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
    # Build a clean categorized reference from the PDF
    pdf_by_part = []
    for part in pdf_modes:
        entries = part.get("entries", [])
        all_modes = []
        for entry in entries:
            all_modes.extend(entry.get("modes", []))
        if all_modes:
            pdf_by_part.append({
                "part_type": part["part_name"],
                "description": entries[0].get("description", "") if entries else "",
                "standard_failure_modes": all_modes
            })

    prompt = f"""You are a senior functional safety engineer performing FMEDA (Failure Mode Effects and Diagnostic Analysis) for an automotive IC, following IEC 60748-5 standard.

## YOUR TASK
Determine the applicable standard failure modes for the following IC block.

## BLOCK TO ANALYZE
- **Block Name**: {block['block_name']}
- **Block Function**: {block['function']}

## STEP-BY-STEP REASONING INSTRUCTIONS
You must follow this exact reasoning process before giving your answer:

STEP 1 - UNDERSTAND THE BLOCK:
  Read the block's function carefully. What does it do electrically?
  What is its primary output? (voltage, current, digital signal, clock, etc.)
  What kind of circuit is it fundamentally? (reference, oscillator, amplifier, comparator, DAC, driver, etc.)

STEP 2 - IDENTIFY THE CIRCUIT CATEGORY:
  Match this block to the most appropriate part type(s) from the IEC standard list below.
  A block may match MORE THAN ONE part type (e.g. a "Bandgap Reference" is both a voltage reference AND produces a current, so consider both).
  Do not just match on name — match on FUNCTION.

STEP 3 - FILTER MODES BY RELEVANCE:
  For each candidate failure mode, ask:
  - Can this block physically exhibit this failure? (e.g. if it has no output clock, "incorrect duty cycle" doesn't apply)
  - Would this failure mode affect the block's PRIMARY function?
  - Is this mode already covered by a more specific mode in the list?
  Exclude modes that are clearly irrelevant to this block's function.
  Include modes that affect the block's output signal type (voltage/current/digital).

STEP 4 - OUTPUT ONLY THE FINAL LIST:
  Return a JSON array of applicable standard failure mode strings.
  No numbering, no explanation in the output — just the JSON array.

## IEC STANDARD FAILURE MODES REFERENCE (organized by part type):
{json.dumps(pdf_by_part, indent=2)}

## OUTPUT FORMAT
Return ONLY a valid JSON array of strings. No markdown, no explanation, no preamble.
Example: ["Output is stuck (i.e. high or low)", "Output is floating (i.e. open circuit)"]

Think through all 4 steps carefully, then output only the final JSON array."""

    raw = query_ollama(prompt, model)

    try:
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        import re
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            modes = json.loads(match.group())
            if isinstance(modes, list):
                return modes, raw
    except Exception:
        pass

    import re
    return re.findall(r'"([^"]+)"', raw), raw


# ── STEP 5: Build output JSON ─────────────────────────────────────────────────
def build_output(blocks, pdf_modes, model):
    result   = []
    debug_log = []
    total    = len(blocks)

    for i, block in enumerate(blocks):
        print(f"Processing [{i+1}/{total}]: {block['block_name']} ...")
        matched_modes, raw_reasoning = match_failure_modes(block, pdf_modes, model)

        debug_log.append({
            "block_name": block["block_name"],
            "llm_raw_response": raw_reasoning,
            "matched_modes": matched_modes
        })

        rows = []
        for seq, mode in enumerate(matched_modes, start=1):
            rows.append({
                "Failure Mode Number": str(seq),
                "Standard failure mode": mode,
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

    # Save debug log
    with open('llm_debug.json', 'w', encoding='utf-8') as f:
        json.dump(debug_log, f, indent=2, ensure_ascii=False)
    print("Debug reasoning saved to llm_debug.json")

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