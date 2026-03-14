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


# ═══════════════════════════════════════════════════════════════════════════════
# GROUND TRUTH KNOWLEDGE BASE
# Learned from comparing manual analysis against real FMEDA output.
# Rules are based on FUNCTION, not block name — so they work regardless of naming.
# ═══════════════════════════════════════════════════════════════════════════════

SM_MODES = [
    "Fail to detect",
    "False detection"
]

INTERFACE_MODES = [
    "TX: No message transferred as requested",
    "TX: Message transferred when not requested",
    "TX: Message transferred too early/late",
    "TX: Message transferred with incorrect value",
    "RX: No incoming message processed",
    "RX: Message transferred when not requested",
    "RX: Message transferred too early/late",
    "RX: Message transferred with incorrect value"
]

TRIM_MODES = [
    "Error of omission (i.e. not triggered when it should be)",
    "Error of comission (i.e. triggered when it shouldn't be)",
    "Incorrect settling time (i.e. outside the expected range)",
    "Incorrect output"
]

LOGIC_MODES = [
    "Output is stuck (i.e. high or low)",
    "Output is floating (i.e. open circuit)",
    "Incorrect output voltage value"
]

DRIVER_MODES = [
    "Driver is stuck in ON or OFF state",
    "Driver is floating (i.e. open circuit, tri-stated)",
    "Driver resistance too high when turned on",
    "Driver resistance too low when turned off",
    "Driver turn-on time too fast or too slow",
    "Driver turn-off time too fast or too slow"
]

# ─── FUNCTIONAL CATEGORY DEFINITIONS ─────────────────────────────────────────
# Each category is defined by what the block DOES, not what it's named.
# Used to build the reasoning prompt context — LLM picks the right one.

FUNCTIONAL_CATEGORIES = {

    "SAFETY_MECHANISM": {
        "description": "A safety mechanism that monitors something and detects faults",
        "indicators": ["safety mechanism", "monitors and detects", "fault detection", "diagnostic"],
        "fixed_modes": SM_MODES,
        "iec_type": None,
        "reasoning": "All safety mechanisms get exactly: Fail to detect + False detection"
    },

    "SERIAL_INTERFACE": {
        "description": "A serial communication interface (SPI, I2C, UART, etc.) that transfers data",
        "indicators": [
            "serial interface", "spi interface", "i2c interface", "uart", "serial protocol",
            "channel programming", "fault readback", "configuration register",
            "serial data", "mosi", "miso", "sclk"
        ],
        "fixed_modes": INTERFACE_MODES,
        "iec_type": None,
        "reasoning": "Serial interfaces use TX/RX communication failure modes"
    },

    "SELF_TEST": {
        "description": "A self-test, POST, or calibration block that validates other blocks at startup",
        "indicators": [
            "self-test", "self test", "power-on self", "post", "validates", "validates dac",
            "startup test", "calibrat", "trim circuit", "trimming"
        ],
        "fixed_modes": TRIM_MODES,
        "iec_type": None,
        "reasoning": "Self-test/calibration blocks use error of omission/commission pattern"
    },

    "SWITCH_DRIVER": {
        "description": "A power switch, driver bank, or output driver that switches current/voltage to a load",
        "indicators": [
            "sw_bank", "switch bank", "driver bank", "output switch", "power switch",
            "drives the open-drain", "open-drain", "nfault", "fault output pin",
            "current sink", "led driver", "channel driver"
        ],
        "fixed_modes": DRIVER_MODES,
        "iec_type": "High-side/Low-side",
        "reasoning": "Power switches and driver banks use full 6-mode HS/LS driver pattern"
    },

    "VOLTAGE_REFERENCE": {
        "description": "Produces a stable reference voltage or acts as a reference signal source. "
                       "Includes: bandgap refs, bias references, current sense outputs (treated as reference signals), "
                       "thermal sensor outputs (treated as reference signals), LDO outputs used as references",
        "indicators": [
            "reference voltage", "stable voltage", "bandgap", "1.2v", "reference for",
            "current sense", "senses channel", "via shunt", "sense amplifier",
            "temperature via", "thermal sensor", "monitors die temperature",
            "on-chip diode", "temperature sensor"
        ],
        "fixed_modes": None,
        "iec_type": "Voltage references",
        "reasoning": "Key ground truth insight: current sense amps and thermal sensors are treated "
                     "as voltage reference blocks in FMEDA practice, not as op-amps or ADCs"
    },

    "CURRENT_SOURCE": {
        "description": "Generates or mirrors a reference current for biasing internal analog circuits",
        "indicators": [
            "bias current", "current mirror", "reference current", "bias generator",
            "pull-up current source", "current source", "current reference"
        ],
        "fixed_modes": None,
        "iec_type": "Current source (including bias current generator)",
        "reasoning": "Current bias generators and pull-up current sources use current source modes"
    },

    "VOLTAGE_REGULATOR": {
        "description": "Regulates output voltage under varying load (LDO, SMPS, linear regulator)",
        "indicators": [
            "ldo", "low dropout", "linear regulator", "smps", "switching regulator",
            "regulates voltage", "maintains.*voltage.*load", "voltage regulator"
        ],
        "fixed_modes": None,
        "iec_type": "Voltage regulators (linear, SMPS, etc.)",
        "reasoning": "LDOs and regulators use the 8-mode voltage regulator pattern"
    },

    "CHARGE_PUMP": {
        "description": "Boosts or inverts voltage using switched capacitors",
        "indicators": ["charge pump", "boost converter", "switched capacitor", "voltage boost", "voltage inverter"],
        "fixed_modes": None,
        "iec_type": "Charge pump, regulator boost",
        "reasoning": "Charge pumps use 6-mode pattern (no accuracy/drift unlike linear regulators)"
    },

    "OSCILLATOR": {
        "description": "Generates a periodic clock signal",
        "indicators": [
            "generates.*clock", "internal clock", "oscillator", "4 mhz", "clock signal",
            "pwm clock", "clock generation", "ring oscillator", "rc oscillator"
        ],
        "fixed_modes": None,
        "iec_type": "Oscillator",
        "reasoning": "All 7 oscillator modes apply"
    },

    "WATCHDOG": {
        "description": "Monitors clock edges or timing continuity and asserts a fault on timeout",
        "indicators": [
            "watchdog", "monitors.*clock.*continuity", "clock loss", "monitors internal clock",
            "asserts fault on clock", "clock monitor", "clock integrity"
        ],
        "fixed_modes": None,
        "iec_type": "Oscillator",
        "reasoning": "Watchdog timer monitors oscillator behavior — uses oscillator modes for the monitored signal path. "
                     "The output fault signal itself: stuck + floating"
    },

    "DAC": {
        "description": "Converts a digital code to an analog output (voltage or current)",
        "indicators": [
            "digital to analog", "digital to analogue", "dac", "current programming",
            "channel current", "n-bit", "8-bit current", "pwm generation"
        ],
        "fixed_modes": None,
        "iec_type": "N bits digital to analogue converters (DAC)",
        "reasoning": "All 8 DAC modes apply"
    },

    "ADC": {
        "description": "Converts an analog signal to a digital word",
        "indicators": [
            "analog to digital", "analogue to digital", "adc", "digitizes",
            "converts.*analog.*digital", "n-bit adc", "successive approximation"
        ],
        "fixed_modes": None,
        "iec_type": "N bits analogue to digital converters (N-bit ADC)",
        "reasoning": "All 8 ADC modes apply"
    },

    "COMPARATOR": {
        "description": "Compares an input signal against a threshold and produces a binary output",
        "indicators": [
            "comparator", "compares.*against.*threshold", "compares sensed", "115% threshold",
            "triggers fault", "overcurrent", "detects.*using.*threshold", "threshold detection",
            "monitors drain voltage", "detects shorted", "short.*detector",
            "open-load detector", "detects disconnected", "open load", "load detector"
        ],
        "fixed_modes": None,
        "iec_type": "Voltage/Current comparator",
        "reasoning": "All 5 comparator modes apply: not-triggering, false-triggering, stuck, floating, oscillation"
    },

    "HS_LS_DRIVER": {
        "description": "Drives a gate or switches a load using high-side or low-side FET",
        "indicators": [
            "hs/ls", "high-side", "low-side", "gate driver", "half-bridge",
            "full-bridge", "h-bridge", "pre-driver", "fet driver"
        ],
        "fixed_modes": None,
        "iec_type": "High-side/Low-side",
        "reasoning": "All 6 HS/LS driver modes apply"
    },

    "OP_AMP": {
        "description": "Amplifies an analog signal with gain control (op-amp, instrumentation amp, buffer)",
        "indicators": [
            "operational amplifier", "op-amp", "opamp", "instrumentation amp",
            "gain amplifier", "buffer amplifier", "differential amplifier",
            "amplifies.*signal", "gain.*error", "signal conditioning"
        ],
        "fixed_modes": None,
        "iec_type": "Operational amplifier and buffer",
        "reasoning": "All 9 op-amp modes apply"
    },
}


def classify_block_fixed(block):
    """
    Returns (pattern_name, modes) if this block has a definitively fixed pattern,
    or (None, None) if it needs LLM analysis.
    Only SM blocks are truly fixed — everything else goes through LLM with hints.
    """
    name = block['block_name'].strip()
    func = block['function'].lower()

    # SM blocks — named SM + digits, always get fail/detect pattern
    if re.match(r'^sm\d', name.lower()):
        return 'SM', SM_MODES

    # Also catch blocks whose FUNCTION explicitly says "safety mechanism"
    if 'safety mechanism' in func and ('monitors' in func or 'detects' in func):
        return 'SM', SM_MODES

    return None, None


def get_functional_category(block):
    """
    Find the best matching functional category for this block.
    Returns the category dict or None.
    Checks indicators against block name + function combined.
    """
    combined = (block['block_name'] + ' ' + block['function']).lower()

    best_match = None
    best_score = 0

    for cat_name, cat in FUNCTIONAL_CATEGORIES.items():
        score = 0
        for indicator in cat['indicators']:
            # Use regex for multi-word indicators
            if re.search(indicator.replace('.*', '.*'), combined):
                score += 2 if len(indicator) > 10 else 1
        if score > best_score:
            best_score = score
            best_match = (cat_name, cat)

    return best_match if best_score > 0 else None


def get_circuit_hint(block):
    """Return IEC part type hint from functional category."""
    match = get_functional_category(block)
    if match:
        _, cat = match
        return cat.get('iec_type')
    return None


# ─── DESCRIPTION PATCHES for broken PDF extractions ─────────────────────────
DESCRIPTION_PATCHES = {
    "High-side/Low-side (HS/LS) driver": (
        "Hardware part/subpart that applies voltage to a load in a single direction. "
        "A high-side driver connects the load to the high voltage rail. "
        "A low-side driver connects the load to the ground rail."
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
        "Hardware part/subpart driving a gate of an external FET used as a HS or LS driver."
    ),
}

# ─── PDF KEYWORD MAP for rulebook context retrieval ──────────────────────────
CIRCUIT_KEYWORDS = {
    "reference":    ["voltage regulator", "reference", "bandgap"],
    "bandgap":      ["reference", "bandgap"],
    "oscillator":   ["oscillator", "clock", "frequency"],
    "osc":          ["oscillator", "clock"],
    "dac":          ["digital to analogue", "DAC"],
    "adc":          ["analogue to digital", "ADC"],
    "amplifier":    ["operational amplifier", "buffer"],
    "comparator":   ["comparator", "threshold"],
    "driver":       ["driver", "high-side", "low-side"],
    "ldo":          ["voltage regulator", "linear", "LDO"],
    "thermal":      ["temperature", "thermal"],
    "current sense":["amplifier", "current", "sense"],
    "overcurrent":  ["comparator", "threshold"],
    "charge pump":  ["charge pump", "regulator boost"],
}


# ═══════════════════════════════════════════════════════════════════════════════
# MODE STRING CLEANER
# Strips footnote markers (a/b/c/d) that bleed from PDF into mode strings.
# ═══════════════════════════════════════════════════════════════════════════════

def clean_mode_string(mode):
    import re
    mode = re.sub(r'\s+[abcd]$', '', mode)                      # "spikes b" -> "spikes"
    mode = re.sub(r'([snt])[abcd](\s|$)', lambda m: m.group(1) + m.group(2), mode)  # "driftc " -> "drift "
    mode = re.sub(r'^[abcd]\s+(?=[A-Z])', '', mode)             # "a Output..." -> "Output..."
    mode = re.sub(r'\s+,', ',', mode)                           # "too low , including" -> "too low, including"
    mode = re.sub(r'  +', ' ', mode)
    return mode.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def extract_full_pdf_text(pdf_path):
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
    if not pdf_pages:
        return ""
    combined = (block['block_name'] + ' ' + block['function']).lower()
    search_keywords = []
    for key, keywords in CIRCUIT_KEYWORDS.items():
        if key in combined:
            search_keywords.extend(keywords)
    for word in re.split(r'[\s/\-\(\)×,]+', block['block_name']):
        if len(word) > 3:
            search_keywords.append(word)
    search_keywords = list(set(k.lower() for k in search_keywords))

    scored = []
    for page_num, text in pdf_pages.items():
        text_lower = text.lower()
        score = sum(text_lower.count(kw) for kw in search_keywords)
        if score > 0:
            scored.append((score, page_num, text))
    scored.sort(reverse=True)

    parts, total = [], 0
    for _, page_num, text in scored[:5]:
        if total >= max_chars:
            break
        chunk = text[:max_chars - total]
        parts.append(f"[Page {page_num}]\n{chunk}")
        total += len(chunk)
    return "\n\n".join(parts)


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


def load_and_prepare_pdf_modes(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        pdf_modes = json.load(f)

    for part in pdf_modes:
        name = part.get("part_name", "")
        if name in DESCRIPTION_PATCHES:
            for entry in part.get("entries", []):
                desc = entry.get("description", "")
                if not desc or desc[0].islower() or len(desc) < 30:
                    entry["description"] = DESCRIPTION_PATCHES[name]
                    print(f"  [PATCHED] {name}")

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

    seen, all_valid = set(), []
    for part in ref:
        for m in part["standard_failure_modes"]:
            if m not in seen:
                seen.add(m)
                all_valid.append(m)

    print(f"  Loaded {len(ref)} part types, {len(all_valid)} unique cleaned modes")
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


def call_reasoning(block, pdf_reference, category, circuit_hint, rulebook_context, model):
    """Call 1 — Deep reasoning with functional category context."""

    # Build category guidance section
    if category:
        cat_section = f"""
═══════════════════════════════════════════
FUNCTIONAL CATEGORY MATCH (from empirical FMEDA analysis)
═══════════════════════════════════════════
This block matches the category: {category.get('_name', '')}
Definition: {category['description']}
Recommended IEC part type: {circuit_hint or 'see below'}
Engineering reasoning: {category['reasoning']}

CRITICAL GROUND TRUTH LESSONS:
- Current sense amplifiers → use "Voltage references" modes, NOT op-amp modes
  (their output is treated as a reference signal in FMEDA practice)
- Thermal sensors / on-chip temp monitors → use "Voltage references" modes, NOT ADC modes
- Watchdog timers → use Oscillator modes for the monitored clock path
- Open-load detectors → use Comparator modes (the threshold detection is primary)
- Short-to-GND detectors → use Comparator modes (drain voltage vs threshold)
- nFAULT / fault output drivers → use HS/LS Driver modes (open-drain FET output)

Start your analysis from the recommended IEC part type above.
Only deviate if you have a clear, specific technical reason.
"""
    else:
        cat_section = """
No strong functional category match — reason from circuit topology carefully.
Default to "Voltage references" for any analog block producing a stable signal.
"""

    rulebook_section = f"""
RELEVANT RULEBOOK CONTEXT:
{rulebook_context}
""" if rulebook_context else ""

    prompt = f"""You are a senior functional safety engineer performing FMEDA analysis for an automotive IC (ISO 26262, IEC 60748-5).

═══════════════════════════════════════════
BLOCK UNDER ANALYSIS
═══════════════════════════════════════════
Block Name : {block['block_name']}
Function   : {block['function']}
{cat_section}
═══════════════════════════════════════════
IEC STANDARD FAILURE MODES REFERENCE
═══════════════════════════════════════════
{json.dumps(pdf_reference, indent=2)}
{rulebook_section}
═══════════════════════════════════════════
ANALYSIS — follow these steps
═══════════════════════════════════════════

STEP 1 — WHAT DOES THIS BLOCK OUTPUT?
State the primary output in one line: stable voltage / bias current / clock signal /
digital word / switching drive / fault signal. If ×N in name, analyze one instance.

STEP 2 — CONFIRM IEC PART TYPE
Start from the recommended type above. Confirm or override with one-sentence justification.

STEP 3 — APPLY MODE RULES
Use these complete mode sets — do not partially apply:
- Voltage references    → 7 modes: stuck, floating, incorrect value, accuracy/drift, spikes, oscillation within range, incorrect start-up
- Current source        → 6 core modes: stuck, floating, incorrect ref current, accuracy/drift, spikes, oscillation; add branch modes ONLY if block has multiple outputs
- Oscillator            → 7 modes: stuck, floating, incorrect swing, incorrect frequency, incorrect duty cycle, drift, jitter
- DAC (N-bit)           → 8 modes: stuck, floating, offset error, linearity error, full-scale gain error, no monotonic curve, incorrect settling time, oscillation/drift
- ADC (N-bit)           → 8 modes: stuck, floating, accuracy error, offset error, no monotonic characteristic, full-scale error, linearity error, incorrect settling time
- Comparator            → 5 modes: not triggering, falsely triggering, stuck, floating, oscillation
- HS/LS Driver          → 6 modes: stuck ON/OFF, floating, resistance too high on, resistance too low off, turn-on time, turn-off time
- Voltage regulator     → 8 modes: OV, UV, spikes, incorrect start-up, accuracy/drift, oscillation within range, fast oscillation, quiescent current
- Charge pump           → 6 modes: OV, UV, spikes, incorrect start-up, oscillation within range, quiescent current

STEP 4 — COPY EXACT STRINGS
List each applicable mode word-for-word from the IEC table above. No shortening or paraphrasing."""

    return query_ollama(prompt, model, temperature=0.1)


def call_extraction(block, reasoning, all_valid_modes, model):
    """Call 2 — Extract clean JSON from reasoning. Temperature=0."""

    prompt = f"""Extract the final FMEDA failure mode list from this engineering analysis.

BLOCK: {block['block_name']}

ANALYSIS:
{reasoning}

VALID MODE STRINGS — copy EXACTLY from this list:
{json.dumps(all_valid_modes, indent=2)}

RULES:
1. Find every mode in the STEP 4 final list of the analysis
2. Match each to the EXACT string from the valid list (character for character)
3. Partial match examples: "stuck" → "Output is stuck (i.e. high or low)"; "drift" → find the full drift string
4. Do NOT include modes marked ✗ or rejected in the analysis
5. Do NOT paraphrase, shorten, or invent strings
6. Remove duplicates
7. If no modes apply, return []

Return ONLY the JSON array, nothing else:"""

    return query_ollama(prompt, model, temperature=0.0)


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
# PIPELINE ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def process_block(block, pdf_reference, all_valid_modes, pdf_pages, model):
    valid_set = set(all_valid_modes)

    # ── Fixed pattern: only truly universal fixed patterns (SM blocks) ────────
    pattern_name, fixed_modes = classify_block_fixed(block)
    if fixed_modes is not None:
        print(f"    [FIXED:{pattern_name}] {len(fixed_modes)} modes")
        return fixed_modes, {
            "method": f"fixed:{pattern_name}",
            "final_modes": fixed_modes
        }

    # ── Functional category matching ─────────────────────────────────────────
    cat_match = get_functional_category(block)
    category_name = cat_match[0] if cat_match else None
    category      = cat_match[1] if cat_match else {}
    circuit_hint  = category.get('iec_type') if category else None

    # Inject name into category dict for prompt use
    if category:
        category['_name'] = category_name

    if category_name:
        print(f"    [CATEGORY] {category_name} → {circuit_hint}")

        # High-confidence fixed pattern: category has fixed modes AND score ≥ 3
        combined = (block['block_name'] + ' ' + block['function']).lower()
        score = sum(
            2 if len(ind) > 10 else 1
            for ind in category.get('indicators', [])
            if re.search(ind, combined)
        )
        if category.get('fixed_modes') and score >= 3:
            print(f"    [FIXED:{category_name}] confidence={score} — skipping LLM")
            return category['fixed_modes'], {
                "method": f"fixed:{category_name}",
                "confidence": score,
                "final_modes": category['fixed_modes']
            }

    # ── Rulebook context ─────────────────────────────────────────────────────
    rulebook_context = get_relevant_pdf_context(block, pdf_pages)

    # ── Call 1: Reasoning ────────────────────────────────────────────────────
    print(f"    [1/2] Reasoning (cat={category_name}, rulebook={len(rulebook_context)}c)...")
    reasoning = call_reasoning(block, pdf_reference, category or {}, circuit_hint, rulebook_context, model)

    # ── Call 2: Extraction ───────────────────────────────────────────────────
    print(f"    [2/2] Extracting...")
    raw_extraction = call_extraction(block, reasoning, all_valid_modes, model)
    modes = parse_json_array(raw_extraction)

    # ── Hard filter ──────────────────────────────────────────────────────────
    final_modes = [m for m in modes if m in valid_set]
    removed = [m for m in modes if m not in valid_set]
    if removed:
        print(f"    [filter] Removed {len(removed)}: {[r[:30] for r in removed[:2]]}")

    print(f"    ✓ {len(final_modes)} modes")

    return final_modes, {
        "method": "llm_2call",
        "category": category_name,
        "circuit_hint": circuit_hint,
        "reasoning": reasoning,
        "extraction_raw": raw_extraction,
        "after_extraction": modes,
        "removed_invalid": removed,
        "final_modes": final_modes,
        "rulebook_context_chars": len(rulebook_context)
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
        print(f"    → {len(final_modes)} modes assigned")

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

    print("\n=== Step 4: Rulebook PDF ===")
    pdf_pages = extract_full_pdf_text(FULL_PDF_FILE)

    print(f"\n=== Step 5: Pipeline ({OLLAMA_MODEL}) ===")
    output = build_output(blocks, pdf_reference, all_valid_modes, pdf_pages, OLLAMA_MODEL)

    print("\n=== Step 6: Save ===")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {OUTPUT_FILE}")
    for item in output:
        print(f"   {item['block_name']}: {len(item['rows'])} modes")