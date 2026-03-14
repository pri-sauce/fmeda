import json
import re
import pdfplumber
import pandas as pd
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE        = 'your_dataset.xlsx'
BLK_SHEET         = 'BLK'
PDF_MODES_FILE    = 'pdf_extracted.json'   # table extracted by pdf_extractor.py
FULL_PDF_FILE     = 'rulebook.pdf'         # the full 188pg rulebook PDF
OUTPUT_FILE       = 'llm_output.json'
DEBUG_FILE        = 'llm_debug.json'
OLLAMA_MODEL      = 'qwen3:30b'
OLLAMA_URL        = 'http://localhost:11434/api/generate'
MAX_PDF_CONTEXT_CHARS = 8000
# ─────────────────────────────────────────────────────────────────────────────


# ── DESCRIPTION PATCHES for broken PDF extractions ───────────────────────────
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

# ── KEYWORD MAP: maps block name keywords → PDF search terms ─────────────────
CIRCUIT_KEYWORDS = {
    "reference":    ["voltage regulator", "reference", "bandgap", "bias"],
    "bandgap":      ["voltage regulator", "reference", "bandgap", "bias"],
    "bias":         ["voltage regulator", "reference", "bias", "current"],
    "oscillator":   ["oscillator", "clock", "frequency", "PLL"],
    "osc":          ["oscillator", "clock", "frequency"],
    "dac":          ["digital to analogue", "DAC", "converter"],
    "adc":          ["analogue to digital", "ADC", "converter"],
    "amplifier":    ["operational amplifier", "buffer", "amplifier"],
    "comparator":   ["comparator", "threshold", "hysteresis"],
    "driver":       ["driver", "high-side", "low-side", "HS/LS"],
    "ldo":          ["voltage regulator", "linear", "LDO"],
    "watchdog":     ["watchdog", "timer", "digital"],
    "spi":          ["interface", "digital", "communication", "serial"],
    "register":     ["digital", "logic", "register"],
    "detector":     ["comparator", "threshold", "detector"],
    "thermal":      ["temperature", "thermal", "sensor"],
    "current sense":["amplifier", "current", "sense"],
    "overcurrent":  ["comparator", "threshold", "overcurrent"],
    "pll":          ["PLL", "phase locked", "oscillator", "clock"],
    "charge pump":  ["charge pump", "regulator boost", "switching"],
    "post":         ["digital", "logic", "test"],
    "fault":        ["digital", "logic", "driver"],
}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF CONTEXT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_full_pdf_text(pdf_path):
    """Extract all text from the full rulebook PDF page by page."""
    pages = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                pages[i + 1] = text
        print(f"  Extracted {len(pages)} pages from rulebook")
    except Exception as e:
        print(f"  WARNING: Could not read rulebook PDF: {e}")
    return pages


def get_relevant_pdf_context(block, pdf_pages, max_chars=MAX_PDF_CONTEXT_CHARS):
    """Score and retrieve the most relevant rulebook pages for a given block."""
    if not pdf_pages:
        return ""

    block_text = (block['block_name'] + " " + block['function']).lower()

    # Build keyword list from circuit type map + block name words
    search_keywords = []
    for key, keywords in CIRCUIT_KEYWORDS.items():
        if key in block_text:
            search_keywords.extend(keywords)
    for word in re.split(r'[\s/\-\(\)×,]+', block['block_name']):
        if len(word) > 3:
            search_keywords.append(word)
    search_keywords = list(set(k.lower() for k in search_keywords))

    # Score pages
    scored = []
    for page_num, text in pdf_pages.items():
        text_lower = text.lower()
        score = sum(text_lower.count(kw) for kw in search_keywords)
        if score > 0:
            scored.append((score, page_num, text))
    scored.sort(reverse=True)

    # Concatenate top pages up to max_chars
    parts = []
    total = 0
    for _, page_num, text in scored[:6]:
        if total >= max_chars:
            break
        chunk = text[:max_chars - total]
        parts.append(f"[Page {page_num}]\n{chunk}")
        total += len(chunk)

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def extract_excel(filepath):
    xl = pd.ExcelFile(filepath)
    print(f"Sheets: {xl.sheet_names}")
    sheets = {}
    for sheet in xl.sheet_names:
        df = pd.read_excel(filepath, sheet_name=sheet, dtype=str).fillna('')
        sheets[sheet] = df.to_dict(orient='records')
    return sheets


def extract_blocks(sheets, sheet_name):
    if sheet_name not in sheets:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {list(sheets.keys())}")
    rows = sheets[sheet_name]
    print(f"BLK columns: {list(rows[0].keys()) if rows else 'empty'}")
    for r in rows[:3]:
        print(f"  {r}")
    blocks = []
    for row in rows:
        vals = [str(v).strip() for v in row.values() if str(v).strip()]
        if len(vals) >= 2:
            blocks.append({"block_name": vals[0], "function": vals[1]})
        elif len(vals) == 1:
            blocks.append({"block_name": vals[0], "function": ""})
    return blocks


def clean_mode_string(mode):
    """Strip footnote markers (a/b/c/d) that bleed from PDF into mode strings."""
    import re

    # Footnotes typically appear after words ending in: s (spikes), n (oscillation), t (drift)
    # NOT after: e (prescribed, affected), r, l, d — these are real word endings

    # Case 1: trailing space + marker: "spikes b" -> "spikes"
    mode = re.sub(r'\s+[abcd]$', '', mode)

    # Case 2: marker directly appended after s/n/t: "spikesb", "driftc", "oscillationa"
    mode = re.sub(r'([snt])[abcd](\s|$)', lambda m: m.group(1) + m.group(2), mode)

    # Case 3: leading marker before capital: "a Output" -> "Output"
    mode = re.sub(r'^[abcd]\s+(?=[A-Z])', '', mode)

    # Fix "too low , including" -> "too low, including"
    mode = re.sub(r'\s+,', ',', mode)
    mode = re.sub(r'  +', ' ', mode)

    return mode.strip()


def load_and_prepare_pdf_modes(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        pdf_modes = json.load(f)

    # Patch broken descriptions
    for part in pdf_modes:
        name = part.get("part_name", "")
        if name in DESCRIPTION_PATCHES:
            for entry in part.get("entries", []):
                desc = entry.get("description", "")
                if not desc or desc[0].islower() or len(desc) < 30:
                    entry["description"] = DESCRIPTION_PATCHES[name]
                    print(f"  [PATCHED] {name}")

    # Build structured reference with cleaned mode strings
    ref = []
    for part in pdf_modes:
        entries = part.get("entries", [])
        all_modes, descs = [], []
        for entry in entries:
            for m in entry.get("modes", []):
                cleaned = clean_mode_string(m)
                if cleaned:
                    all_modes.append(cleaned)
            d = entry.get("description", "").strip()
            if d and d not in descs:
                descs.append(d)
        if all_modes:
            ref.append({
                "part_type": part["part_name"],
                "what_it_is": " ".join(descs),
                "standard_failure_modes": all_modes
            })

    # Build flat deduplicated cleaned mode list
    seen, all_valid = set(), []
    for part in ref:
        for m in part["standard_failure_modes"]:
            if m not in seen:
                seen.add(m)
                all_valid.append(m)

    print(f"  Cleaned {len(all_valid)} unique mode strings")
    return ref, all_valid


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CALLS
# ═══════════════════════════════════════════════════════════════════════════════

def query_ollama(prompt, model, temperature=0.1):
    r = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 16384, "top_p": 0.9}
    })
    r.raise_for_status()
    return r.json()["response"].strip()


def call_reasoning(block, pdf_reference, rulebook_context, model):
    """Call 1 — Pure engineering reasoning. No JSON constraints."""

    rulebook_section = f"""
═══════════════════════════════════════════════
RELEVANT RULEBOOK CONTEXT (excerpts from the 188pg IEC standard)
Use this for deeper understanding of how failure modes are defined and applied.
═══════════════════════════════════════════════
{rulebook_context}
""" if rulebook_context else ""

    prompt = f"""You are a senior functional safety engineer with 15+ years of hands-on experience in automotive mixed-signal IC design and FMEDA analysis (ISO 26262, IEC 60748-5).

You are reviewing a block-level FMEDA for an automotive IC. A junior engineer needs your guidance on which standard IEC failure modes apply to this specific block.

═══════════════════════════════════════════════
BLOCK UNDER ANALYSIS
═══════════════════════════════════════════════
Block Name : {block['block_name']}
Function   : {block['function']}

═══════════════════════════════════════════════
IEC STANDARD FAILURE MODE TABLE (organized by part type)
═══════════════════════════════════════════════
{json.dumps(pdf_reference, indent=2)}
{rulebook_section}
═══════════════════════════════════════════════
ANALYSIS INSTRUCTIONS
═══════════════════════════════════════════════

Work through each step carefully. This is technical engineering analysis — be precise.

STEP 1 — CIRCUIT TOPOLOGY
Describe this block at circuit level:
- What is its PRIMARY electrical output? (regulated voltage, bias current, clock, digital word, gate drive, etc.)
- What internal circuit elements does it likely contain?
- Is the output analog, digital, or mixed-signal?
- Does it DRIVE a load or only provide a reference/signal?
- Note: if block name has "×N" (e.g. ×10), analyze one single instance

STEP 2 — IEC PART TYPE MAPPING
Map this block to part type(s) from the IEC table:
- Primary match: which part type and WHY (based on function, not name)
- Secondary matches if any: which and WHY
- Explicit rejections: which part types do NOT apply and WHY
One block can map to multiple part types.

STEP 3 — MODE-BY-MODE EVALUATION
For EACH failure mode in your matched part types, evaluate:
✓ APPLICABLE — state the physical reason why this block can exhibit this failure
✗ NOT APPLICABLE — state why this is impossible or irrelevant for this topology

Key rules per circuit type:
- REFERENCE/BIAS blocks: stuck output, floating, out-of-range voltage/current, drift, quiescent current always apply
- DRIVER blocks (HS/LS, pre-driver): stuck ON/OFF, floating, resistance too high/low, turn-on/off timing always apply
- OSCILLATOR/CLOCK blocks: stuck, incorrect frequency, duty cycle, jitter, drift always apply
- ADC/DAC blocks: stuck outputs, accuracy/linearity errors, settling time always apply  
- COMPARATOR blocks: false trigger, no trigger, stuck output, oscillation always apply
- DIGITAL-ONLY blocks (SPI, register, watchdog): analog modes typically do NOT apply — only include if there is a genuine analog interface

STEP 4 — ENGINEERING CROSS-CHECK
Before concluding:
1. Would each listed mode actually cause a safety-relevant issue for this block?
2. Any duplicate modes listed under different names? Remove them.
3. Any obvious modes missing for this circuit type?

STEP 5 — FINAL LIST
Write your final applicable modes clearly, one per line.
Copy mode text EXACTLY from the IEC table — no paraphrasing."""

    return query_ollama(prompt, model, temperature=0.1)


def call_extraction(block, reasoning, all_valid_modes, model):
    """Call 2 — Extract clean JSON from reasoning. Temperature=0 for determinism."""

    prompt = f"""You are extracting a structured result from an engineering analysis document.

BLOCK: {block['block_name']}

ENGINEERING ANALYSIS:
{reasoning}

VALID FAILURE MODE STRINGS (you MUST copy from this list exactly — character for character):
{json.dumps(all_valid_modes, indent=2)}

EXTRACTION TASK:
1. Find every failure mode in the analysis that was concluded as APPLICABLE (marked ✓ or in the final list)
2. For each, find the EXACT matching string in the valid modes list
   - "stuck" in analysis → match "Output is stuck (i.e. high or low)" from list
   - "floating" in analysis → match "Output is floating (i.e. open circuit)" from list
   - Always prefer the FULL string from the valid list, not a partial match
3. Remove any duplicates
4. Do NOT include modes marked ✗ or explicitly rejected
5. Do NOT invent or paraphrase — only strings that exist in the valid list

Return ONLY a valid JSON array. No explanation, no markdown, no text before or after the array."""

    return query_ollama(prompt, model, temperature=0.0)


def call_verification(block, modes, all_valid_modes, model):
    """
    Call 3 — Python-first hard filter + lightweight LLM relevance check.
    Never adds modes, only removes irrelevant ones.
    """
    valid_set = set(all_valid_modes)

    # Step 1: Python hard filter — remove anything not in valid list
    pre_filtered = [m for m in modes if m in valid_set]
    removed = [m for m in modes if m not in valid_set]
    if removed:
        print(f"    [verify] Removed {len(removed)} invalid: {[r[:40] for r in removed[:2]]}")

    # Step 2: If empty after reasoning, trust the reasoning — don't add anything
    if not modes:  # reasoning explicitly returned []
        return []

    # Step 3: If nothing survived the hard filter, return empty
    if not pre_filtered:
        return []

    # Step 4: Quick LLM relevance pass — only remove, never add
    prompt = f"""Quality check: remove any failure modes that do NOT apply to this IC block.

BLOCK: {block['block_name']} — {block['function']}

MODES TO CHECK:
{json.dumps(pre_filtered, indent=2)}

Rules:
- REMOVE modes that are clearly wrong for this block type
- For digital-only blocks (registers, memory, SPI, watchdog, logic): keep ONLY "stuck" and "floating" type modes, remove all analog modes (voltage accuracy, drift, oscillation, frequency, etc.)
- For analog blocks: keep all modes that are physically possible
- Do NOT add any new modes
- Return ONLY the JSON array of kept modes, nothing else"""

    raw = query_ollama(prompt, model, temperature=0.0)

    try:
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        m = re.search(r'\[.*\]', clean, re.DOTALL)
        if m:
            verified = json.loads(m.group())
            if isinstance(verified, list):
                # Hard safety net — never allow anything outside valid list
                return [v for v in verified if v in valid_set]
    except Exception:
        pass

    return pre_filtered


def parse_json_array(raw):
    try:
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        m = re.search(r'\[.*\]', clean, re.DOTALL)
        if m:
            result = json.loads(m.group())
            if isinstance(result, list):
                return result
    except Exception:
        pass
    return re.findall(r'"([^"]{5,})"', raw)


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATION — 2 LLM calls + 1 Python filter for speed
# ═══════════════════════════════════════════════════════════════════════════════

def process_block(block, pdf_reference, all_valid_modes, pdf_pages, model):
    rulebook_context = get_relevant_pdf_context(block, pdf_pages)
    context_chars = len(rulebook_context)

    print(f"    [1/2] Reasoning (rulebook: {context_chars} chars)...")
    reasoning = call_reasoning(block, pdf_reference, rulebook_context, model)

    print(f"    [2/2] Extracting modes...")
    raw_extraction = call_extraction(block, reasoning, all_valid_modes, model)
    modes = parse_json_array(raw_extraction)

    # Python hard filter — remove anything not in valid list (no 3rd LLM call)
    valid_set = set(all_valid_modes)
    final_modes = [m for m in modes if m in valid_set]
    removed = [m for m in modes if m not in valid_set]
    if removed:
        print(f"    [filter] Removed {len(removed)} invalid strings")

    print(f"    ✓ {len(final_modes)} modes")

    return final_modes, {
        "reasoning":           reasoning,
        "extraction_raw":      raw_extraction,
        "after_extraction":    modes,
        "after_filter":        final_modes,
        "removed_invalid":     removed,
        "rulebook_context_chars": context_chars
    }


def build_output(blocks, pdf_reference, all_valid_modes, pdf_pages, model):
    result, debug_log = [], []

    for i, block in enumerate(blocks):
        print(f"\n[{i+1}/{len(blocks)}] {block['block_name']}")
        final_modes, debug_info = process_block(
            block, pdf_reference, all_valid_modes, pdf_pages, model
        )

        debug_log.append({
            "block_name": block["block_name"],
            "function":   block["function"],
            **debug_info
        })

        rows = [
            {
                "Failure Mode Number": str(seq),
                "Standard failure mode": mode,
                "Block Failure rate [FIT]": "",
                "Failure rate [FIT]": "",
                "Percentage of Safe Faults": "",
                "Safety mechanism(s) (IC) allowing to prevent the violation of the safety goal": "",
                "Failure mode coverage wrt. violation of safety goal": "",
                "Residual or Single Point Fault failure rate [FIT]": "",
                "Latent Multiple Point Fault failure rate [FIT]": ""
            }
            for seq, mode in enumerate(final_modes, start=1)
        ]

        result.append({
            "block_name": block["block_name"],
            "function":   block["function"],
            "rows":       rows
        })
        print(f"    ✓ {len(final_modes)} modes assigned")

    with open(DEBUG_FILE, 'w', encoding='utf-8') as f:
        json.dump(debug_log, f, indent=2, ensure_ascii=False)
    print(f"\nDebug log → {DEBUG_FILE}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=== Step 1: Excel ===")
    sheets = extract_excel(EXCEL_FILE)

    print("\n=== Step 2: BLK sheet ===")
    blocks = extract_blocks(sheets, BLK_SHEET)
    print(f"Found {len(blocks)} blocks")

    print("\n=== Step 3: PDF modes table ===")
    pdf_reference, all_valid_modes = load_and_prepare_pdf_modes(PDF_MODES_FILE)
    print(f"Loaded {len(pdf_reference)} part types, {len(all_valid_modes)} unique modes")

    print("\n=== Step 4: Rulebook PDF ===")
    pdf_pages = extract_full_pdf_text(FULL_PDF_FILE)

    print(f"\n=== Step 5: LLM pipeline ({OLLAMA_MODEL}) ===")
    output = build_output(blocks, pdf_reference, all_valid_modes, pdf_pages, OLLAMA_MODEL)

    print("\n=== Step 6: Save ===")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {OUTPUT_FILE}")
    for item in output:
        print(f"   {item['block_name']}: {len(item['rows'])} modes")
