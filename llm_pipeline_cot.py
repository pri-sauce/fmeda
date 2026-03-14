import json
import pandas as pd
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE        = 'your_dataset.xlsx'      # <-- your excel file
BLK_SHEET         = 'BLK'                    # sheet with block names & functions
PDF_MODES_FILE    = 'pdf_extracted.json'     # output from pdf_extractor.py
OUTPUT_FILE       = 'llm_output.json'
OLLAMA_MODEL      = 'llama3'                 # change to your ollama model name
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


# ── DESCRIPTION PATCHES ──────────────────────────────────────────────────────
# Some PDF entries have broken/truncated descriptions due to PDF extraction overflow.
# Patch them here with correct descriptions from the IEC standard.
DESCRIPTION_PATCHES = {
    "High-side/Low-side (HS/LS) driver": (
        "Hardware part/subpart that applies voltage to a load in a single direction. "
        "A high-side driver connects the load to the high voltage rail. "
        "A low-side driver connects the load to the ground rail. "
        "Used to switch power to a load such as a motor, LED, solenoid, etc."
    ),
    "High-side/Low-side": (
        "Hardware part/subpart that applies voltage to a load in a single direction. "
        "A high-side driver connects the load to the high voltage rail. "
        "A low-side driver connects the load to the ground rail."
    ),
    "Charge pump, regulator boost": (
        "Hardware part/subpart that converts, and optionally regulates, voltages using "
        "switching technology and capacitive-energy storage elements, and maintains a "
        "constant output voltage with a varying voltage input."
    ),
    "High-side/Low-side pre-driver": (
        "Hardware part/subpart driving a gate of an external FET that is used as a "
        "high-side or low-side driver. Controls the switching of external power transistors."
    ),
}

def patch_pdf_modes(pdf_modes):
    """Fix truncated descriptions in PDF extracted data."""
    for part in pdf_modes:
        name = part.get("part_name", "")
        if name in DESCRIPTION_PATCHES:
            for entry in part.get("entries", []):
                desc = entry.get("description", "")
                # If description looks truncated (starts mid-sentence or is very short)
                if not desc or desc[0].islower() or len(desc) < 30:
                    entry["description"] = DESCRIPTION_PATCHES[name]
                    print(f"  [PATCHED] Description fixed for: {name}")
    return pdf_modes
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


def reasoning_call(block, pdf_by_part, model):
    """Call 1: Pure reasoning — no JSON constraints, let the model think freely."""
    prompt = f"""You are a senior functional safety engineer with 15+ years experience in automotive IC FMEDA analysis (ISO 26262, IEC 60748-5).

A junior engineer has asked you to analyze this IC block and explain your reasoning before they fill out the FMEDA table.

## BLOCK
- Name: {block['block_name']}
- Function: {block['function']}

## YOUR JOB
Think out loud about this block. Walk through:

1. WHAT IS THIS BLOCK ELECTRICALLY?
   Describe what this block does at a circuit level. What is its output — voltage, current, clock, digital signal, switching signal? What circuit topology does it use internally?

2. WHICH IEC PART TYPE(S) DOES IT MATCH?
   From the list below, which standard part type(s) best describe this block? Explain WHY based on function, not just name.
   Remember: one block can match multiple part types.

3. WHICH FAILURE MODES ARE PHYSICALLY POSSIBLE?
   Go through the candidate modes one by one. For each, say whether it CAN happen on this specific block and WHY or WHY NOT.
   Be specific — e.g. "stuck output applies because this block drives a voltage output directly" or "duty cycle does not apply because this block has no clock output."

4. FINAL SHORTLIST
   List only the modes you concluded are genuinely applicable. Explain your final reasoning.

## IEC STANDARD PART TYPES AND MODES FOR REFERENCE
{json.dumps(pdf_by_part, indent=2)}

Think carefully and explain your reasoning at each step. Be thorough."""

    return query_ollama(prompt, model)


def extraction_call(block, reasoning, pdf_by_part, model):
    """Call 2: Given the reasoning, extract the final clean JSON list."""
    # Build flat mode list for strict matching
    all_valid_modes = []
    for part in pdf_by_part:
        all_valid_modes.extend(part.get("standard_failure_modes", []))

    prompt = f"""You are a functional safety engineer finalizing an FMEDA entry.

A senior engineer has already analyzed this block and written their reasoning below.
Your job is to extract the final answer from that reasoning as a clean JSON array.

## BLOCK
- Name: {block['block_name']}
- Function: {block['function']}

## SENIOR ENGINEER'S REASONING
{reasoning}

## VALID MODES LIST (copy strings EXACTLY from this list — do not paraphrase)
{json.dumps(all_valid_modes, indent=2)}

## YOUR TASK
1. Read the reasoning above carefully
2. Extract the failure modes the senior engineer concluded ARE applicable
3. Match each one to the EXACT string from the valid modes list above
4. Return ONLY a valid JSON array of those exact strings

Rules:
- Only include modes explicitly concluded as applicable in the reasoning
- Copy strings EXACTLY as they appear in the valid modes list
- Do not add modes not mentioned in the reasoning
- Do not add explanation, markdown, or any text outside the JSON array
- If no modes apply, return []

Output only the JSON array:"""

    return query_ollama(prompt, model)


def match_failure_modes(block, pdf_modes, model):
    # Build enriched reference organized by part type
    pdf_by_part = []
    for part in pdf_modes:
        entries = part.get("entries", [])
        all_modes = []
        descriptions = []
        for entry in entries:
            all_modes.extend(entry.get("modes", []))
            d = entry.get("description", "").strip()
            if d and d not in descriptions:
                descriptions.append(d)
        if all_modes:
            pdf_by_part.append({
                "part_type": part["part_name"],
                "what_it_is": " ".join(descriptions),
                "standard_failure_modes": all_modes
            })

    # ── Call 1: Reasoning ────────────────────────────────────────────────────
    reasoning = reasoning_call(block, pdf_by_part, model)

    # ── Call 2: Extraction ───────────────────────────────────────────────────
    raw = extraction_call(block, reasoning, pdf_by_part, model)

    try:
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        import re
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            modes = json.loads(match.group())
            if isinstance(modes, list):
                return modes, {"reasoning": reasoning, "extraction_raw": raw}
    except Exception:
        pass

    import re
    return re.findall(r'"([^"]+)"', raw), {"reasoning": reasoning, "extraction_raw": raw}


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
            "function": block["function"],
            "call_1_reasoning": raw_reasoning.get("reasoning", ""),
            "call_2_extraction": raw_reasoning.get("extraction_raw", ""),
            "final_modes": matched_modes
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
    pdf_modes = patch_pdf_modes(pdf_modes)
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
