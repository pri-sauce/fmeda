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

    prompt = f"""You are a senior functional safety engineer with 15+ years experience in automotive IC design and FMEDA analysis (IEC 26262, IEC 60748-5).

## BLOCK TO ANALYZE
- Block Name: {block['block_name']}
- Block Function: {block['function']}

## IEC STANDARD PART TYPES AND THEIR FAILURE MODES
{json.dumps(pdf_by_part, indent=2)}

## HOW A HUMAN EXPERT DOES THIS ANALYSIS
Follow these exact steps, think carefully at each one:

### STEP 1 — Understand the block's electrical nature
Ask yourself:
- What does this block OUTPUT? (stable voltage, current, clock signal, digital bits, drive current to load, etc.)
- What is its core circuit topology? (bandgap = voltage reference + current source, oscillator = generates clock, DAC = digital to analog, driver = switches power to load, comparator = threshold detection, etc.)
- Does it have an analog output, digital output, or both?
- Can it drive a load directly, or does it just provide a signal/reference?

### STEP 2 — Map to IEC part type(s) by FUNCTION not by NAME
- Do NOT match by name similarity alone (e.g. "Current DAC" is a DAC, not just a "current source")
- Match by what the block DOES electrically
- A single block may map to MULTIPLE IEC part types (e.g. a block with both analog output and clock output needs modes from both)
- For driver blocks (HS/LS, pre-driver, H-bridge): focus on switching behavior, stuck states, resistance, timing
- For reference blocks (bandgap, bias): focus on output voltage/current accuracy, stuck, floating, drift
- For oscillator/PLL/clock blocks: focus on frequency, duty cycle, jitter, stuck, missing pulses
- For ADC/DAC blocks: focus on accuracy, linearity, stuck outputs, settling time
- For comparator blocks: focus on false triggering, no triggering, stuck output, oscillation
- For digital/logic blocks (SPI, watchdog, registers): these may NOT map to analog failure modes at all — be honest if no modes apply

### STEP 3 — For EACH candidate failure mode, ask these filter questions
Only include a mode if ALL of these are true:
1. Can this block physically exhibit this behavior given its circuit topology?
2. Does this mode affect the block's PRIMARY function or output?
3. Is the mode meaningful — not redundant with another already included mode?
4. Would a safety engineer actually list this in an FMEDA for this block type?

### STEP 4 — Special rules
- If the block is a DRIVER (switches current/voltage to a load): always consider stuck ON/OFF, floating, resistance too high/low, turn-on/off timing
- If the block is a REFERENCE or BIAS generator: always consider stuck output, floating, out-of-range voltage/current, drift, quiescent current
- If the block is an OSCILLATOR or CLOCK: always consider stuck, incorrect frequency, duty cycle, jitter, drift
- If the block is purely DIGITAL (SPI, register, logic): do not force-fit analog modes — return empty array [] if none apply
- If the block name contains "×N" (e.g. ×10), it means there are N identical instances — treat it as one instance for failure mode purposes

### STEP 5 — Output
Return ONLY a valid JSON array of the applicable standard failure mode strings from the IEC list above.
Copy the mode strings EXACTLY as written in the IEC list. Do not paraphrase or invent new modes.
No explanation, no markdown, no preamble — just the raw JSON array.

If no modes apply, return: []"""

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