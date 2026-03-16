"""
fmeda_agents.py  —  Multi-Agent FMEDA Pipeline
================================================

AGENT 1  (LLM)  — Block Analyst
  Reads BLK + SM sheets, reasons about each block's:
  - FMEDA functional category (REF, BIAS, LDO, OSC, TEMP, CSNS, ADC, CP,
    LOGIC, INTERFACE, TRIM, SW_BANK, SM)
  - IEC 62380 / AEC-Q100 standard failure modes for that category
  - Which other chip blocks this block's output feeds into (downstream graph)

AGENT 2  (LLM)  — IC Effects & Safety Analyst
  For every (block, failure_mode) pair:
  - Reasons about downstream impact on other IC blocks
  - Generates the exact bullet-format IC effect string
  - Determines system-level effect (LED ON/OFF, fail-safe, device damage)
  - Assigns memo X or O and safety mechanism columns

AGENT 3  (LLM)  — Critic / Consistency Checker
  Reviews the complete JSON:
  - Checks IC effects are consistent across blocks
  - Validates memo X/O matches the IC effect severity
  - Flags anything suspicious for human review

AGENT 4  (Hardcoded)  — Template Writer
  Takes the validated JSON and fills FMEDA_TEMPLATE.xlsx
  with 100% deterministic, correctly formatted output.

Usage:
  python fmeda_agents.py

Config: edit the CONFIG block below.
"""

import json, re, time, shutil, sys
import pandas as pd
import openpyxl
import requests
from openpyxl.styles import Alignment

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DATASET_FILE      = 'fusa_ai_agent_mock_data.xlsx'
BLK_SHEET         = 'BLK'
SM_SHEET          = 'SM'
TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
OUTPUT_FILE       = 'FMEDA_filled.xlsx'
INTERMEDIATE_JSON = 'fmeda_agents_output.json'

OLLAMA_URL        = 'http://localhost:11434/api/generate'
OLLAMA_MODEL      = 'qwen3:30b'
OLLAMA_TIMEOUT    = 300

# Controls
SKIP_CACHE        = False    # set True to re-run LLM even if cache exists
CACHE_FILE        = 'fmeda_agents_cache.json'
# ─────────────────────────────────────────────────────────────────────────────


# ═════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE — few-shot examples + IEC failure mode library
# LLMs work best with concrete examples of the EXACT output format expected.
# ═════════════════════════════════════════════════════════════════════════════

# The exact bullet format used in every real FMEDA file
IC_EFFECT_FORMAT = """
EXACT FORMAT for "effects on IC output":
  • BLOCK_NAME
      - specific effect line 1
      - specific effect line 2
  • ANOTHER_BLOCK
      - specific effect

Rules:
  - Use • (bullet) before block name, no indent
  - Use     - (4 spaces + dash) before each effect line
  - If no other block is affected: write exactly "No effect"
  - Be specific: not "BIAS is affected" but "Output reference voltage is stuck"
""".strip()

# IEC 62380 standard failure mode libraries per functional category
IEC_FAILURE_MODES = {
    "VOLTAGE_REFERENCE": [
        "Output is stuck (i.e. high or low)",
        "Output is floating (i.e. open circuit)",
        "Incorrect output voltage value (i.e. outside the expected range)",
        "Output voltage accuracy too low, including drift",
        "Output voltage affected by spikes",
        "Output voltage oscillation within the expected range",
        "Incorrect start-up time (i.e. outside the expected range)",
    ],
    "BIAS_CURRENT": [
        "One or more outputs are stuck (i.e. high or low)",
        "One or more outputs are floating (i.e. open circuit)",
        "Incorrect reference current (i.e. outside the expected range)",
        "Reference current accuracy too low , including drift",
        "Reference current affected by spikes",
        "Reference current oscillation within the expected range",
        "One or more branch currents outside the expected range \nwhile reference current is correct",
        "One or more branch currents accuracy too low , including \ndrift",
        "One or more branch currents affected by spikes",
        "One or more branch currents oscillation within the expected range",
    ],
    "LDO_REGULATOR": [
        "Output voltage higher than a high threshold of the prescribed range (i.e. over voltage — OV)",
        "Output voltage lower than a low threshold of the prescribed range (i.e. under voltage — UV)",
        "Output voltage affected by spikes",
        "Incorrect start-up time",
        "Output voltage accuracy too low, including drift",
        "Output voltage oscillation within the prescribed range",
        "Output voltage affected by a fast oscillation outside the prescribed range but with average value within the prescribed range",
        "Quiescent current (i.e. current drawn by the regulator in order to control its internal circuitry for proper operation) exceeding the maximum value",
    ],
    "OSCILLATOR": [
        "Output is stuck (i.e. high or low)",
        "Output is floating (i.e. open circuit)",
        "Incorrect output signal swing (i.e. outside the expected range)",
        "Incorrect frequency of the output signal",
        "Incorrect duty cycle of the output signal",
        "Drift of the output frequency",
        "Jitter too high in the output signal",
    ],
    "TEMPERATURE_SENSOR": [
        "Output is stuck (i.e. high or low)",
        "Output is floating (i.e. open circuit)",
        "Incorrect output voltage value (i.e. outside the expected \nrange)",
        "Output voltage accuracy too low, including drift",
        "Output voltage affected by spikes",
        "Output voltage oscillation within the expected range",
        "Incorrect start-up time (i.e. outside the expected range)",
    ],
    "CURRENT_SENSE_AMP": [
        "Output is stuck (i.e. high or low)",
        "Output is floating (i.e. open circuit)",
        "Incorrect output voltage value (i.e. outside the expected \nrange)",
        "Output voltage accuracy too low, including drift",
        "Output voltage affected by spikes",
        "Output voltage oscillation within the expected range",
        "Incorrect start-up time (i.e. outside the expected range)",
        "Quiescent current (i.e. current drawn by the regulator in order to control its internal circuitry for proper operation) exceeding the maximum value",
    ],
    "ADC": [
        "One or more outputs are stuck (i.e. high or low)",
        "One or more outputs are floating (i.e. open circuit)",
        "Accuracy error (i.e. Error exceeds the LSBs)",
        "Offset error not including stuck or floating conditions on the outputs, low resolution",
        "No monotonic conversion characteristic \n",
        "Full-scale error not including stuck or floating conditions on the outputs, low resolution ",
        "Linearity error with monotonic conversion curve not including stuck or floating conditions on the outputs, low resolution ",
        "Incorrect settling time (i.e. outside the expected range)",
    ],
    "CHARGE_PUMP": [
        "Output voltage higher than a high threshold of the prescribed range (i.e. over voltage — OV)",
        "Output voltage lower than a low threshold of the prescribed range (i.e. under voltage — UV)",
        "Output voltage affected by spikes",
        "Incorrect start-up time (i.e. outside the expected range)",
        "Output voltage oscillation within the expected range",
        "Quiescent current (i.e. current drawn by the regulator in order to control its internal circuitry for proper operation) exceeding the maximum value",
    ],
    "LOGIC_CONTROL": [
        "Output is stuck (i.e. high or low)",
        "Output is floating (i.e. open circuit)",
        "Incorrect output voltage value",
    ],
    "SERIAL_INTERFACE": [
        "TX: No message transferred as requested",
        "TX: Message transferred when not requested",
        "TX: Message transferred too early/late",
        "TX: Message transferred with incorrect value",
        "RX: No incoming message processed",
        "RX: Message transferred when not requested",
        "RX: Message transferred too early/late",
        "RX: Message transferred with incorrect value",
    ],
    "NVM_TRIM": [
        "Error of omission (i.e. not triggered when it should be)",
        "Error of comission (i.e. triggered when it shouldn't be)",
        "Incorrect settling time (i.e. outside the expected range)",
        "Incorrect output",
    ],
    "HS_LS_DRIVER": [
        "Driver is stuck in ON or OFF state",
        "Driver is floating (i.e. open circuit, tri-stated)",
        "Driver resistance too high when turned on",
        "Driver resistance too low when turned off",
        "Driver turn-on time too fast or too slow",
        "Driver turn-off time too fast or too slow",
    ],
    "SAFETY_MECHANISM": [
        "Fail to detect",
        "False detection",
    ],
}

# Real FMEDA few-shot examples — shown to the LLM as context
FEW_SHOT_EXAMPLES = """
EXAMPLES FROM A REAL AUTOMOTIVE IC FMEDA:

Block: REF (Bandgap voltage reference — feeds BIAS, ADC, TEMP, LDO, OSC)
Failure mode: "Output is stuck (i.e. high or low)"
→ effects on IC output:
• BIAS
    - Output reference voltage is stuck 
    - Output reference current is stuck 
    - Output bias current is stuck 
    - Quiescent current exceeding the maximum value
• REF
    - Quiescent current exceeding the maximum value
• ADC
    - REF output is stuck 
• TEMP
    - Output is stuck 
• LDO
    - Output is stuck 
• OSC
    - Oscillation does not start
→ effects on system: "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication"
→ memo: X
→ safety mechanisms IC: "SM01 SM15 SM16 SM17"
→ coverage SPF: 0.99

Block: BIAS (Bias current generator — feeds ADC, TEMP, LDO, OSC, SW_BANKx, CP)
Failure mode: "One or more outputs are stuck (i.e. high or low)"
→ effects on IC output:
• ADC
    - ADC measurement is incorrect.
• TEMP
    - Incorrect temperature measurement.
• LDO
    - Out of spec.
• OSC
    - Frequency out of spec.
• SW_BANKx
    - Out of spec.
• CP
    - Out of spec.
• CNSN
    - Incorrect reading.
→ effects on system: "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication"
→ memo: X

Block: OSC (Internal oscillator — feeds LOGIC)
Failure mode: "Output is stuck (i.e. high or low)"
→ effects on IC output:
• LOGIC
    - Cannot operate.
    - Communication error.
→ effects on system: "Fail-safe mode active\\nNo communication"
→ memo: X

Block: TEMP (Temp sensor — feeds ADC, SW_BANK_x)
Failure mode: "Output is stuck (i.e. high or low)"
→ effects on IC output:
• ADC
    - TEMP output is stuck low
• SW_BANK_x
    - SW is stuck in off state (DIETEMP)
→ effects on system: "Unintended LED ON"
→ memo: X

Block: CSNS (Current sense amp — feeds ADC)
Failure mode: "Output is stuck (i.e. high or low)"
→ effects on IC output:
• ADC
    - CSNS output is incorrect.
→ effects on system: "No effect"
→ memo: O

Block: OSC (duty cycle mode)
Failure mode: "Incorrect duty cycle of the output signal"
→ effects on IC output: No effect
→ effects on system: No effect
→ memo: O

SAFETY RULES:
- If IC effect is "No effect" → memo MUST be "O"
- If IC effect lists downstream failures → memo MUST be "X"
- Spikes, oscillation-within-range, startup-time, jitter, quiescent current modes → almost always "No effect", memo "O"
- Stuck, floating, out-of-range modes on source blocks → almost always affect all downstream consumers
"""


# ═════════════════════════════════════════════════════════════════════════════
# LLM HELPER
# ═════════════════════════════════════════════════════════════════════════════

def query_llm(prompt: str, model: str = OLLAMA_MODEL, temperature: float = 0.1) -> str:
    """Call Ollama and return the response text."""
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": 16384,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
            }
        }, timeout=OLLAMA_TIMEOUT)
        r.raise_for_status()
        return r.json()["response"].strip()
    except requests.exceptions.ConnectionError:
        print("  ERROR: Cannot connect to Ollama. Is it running? (ollama serve)")
        sys.exit(1)
    except Exception as e:
        print(f"  LLM error: {e}")
        return ""


def parse_json_from_response(text: str) -> dict | list | None:
    """Extract and parse JSON from LLM response (handles markdown code blocks)."""
    text = text.strip()
    # Remove <think>...</think> blocks (qwen3 thinking mode)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Remove markdown code fences
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
    # Find JSON object or array
    for pattern in [r'\{.*\}', r'\[.*\]']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except:
                pass
    return None


# ═════════════════════════════════════════════════════════════════════════════
# CACHE — avoid re-running LLM on unchanged input
# ═════════════════════════════════════════════════════════════════════════════

def load_cache() -> dict:
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ═════════════════════════════════════════════════════════════════════════════
# STEP 0 — READ DATASET
# ═════════════════════════════════════════════════════════════════════════════

def read_dataset(filepath: str) -> tuple[list, list]:
    xl = pd.ExcelFile(filepath)
    df_blk = pd.read_excel(filepath, sheet_name=BLK_SHEET, dtype=str).fillna('')
    blk_blocks = []
    for _, row in df_blk.iterrows():
        vals = [v.strip() for v in row.values if str(v).strip()]
        if len(vals) >= 2:
            blk_blocks.append({
                'id':       vals[0],
                'name':     vals[1],
                'function': vals[2] if len(vals) > 2 else '',
            })

    sm_blocks = []
    if SM_SHEET in xl.sheet_names:
        df_sm = pd.read_excel(filepath, sheet_name=SM_SHEET, dtype=str).fillna('')
        for _, row in df_sm.iterrows():
            vals = [v.strip() for v in row.values if str(v).strip()]
            if vals and re.match(r'sm[-_\s]?\d+', vals[0].lower()):
                sm_blocks.append({
                    'id':          vals[0],
                    'name':        vals[1] if len(vals) > 1 else '',
                    'description': vals[2] if len(vals) > 2 else '',
                })

    return blk_blocks, sm_blocks


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 1 — BLOCK ANALYST
# Determines: functional category, failure modes, downstream connections
# ═════════════════════════════════════════════════════════════════════════════

def agent1_analyze_blocks(blk_blocks: list, sm_blocks: list, cache: dict) -> list:
    """
    For each block, LLM determines:
    - fmeda_category (one of the IEC category keys)
    - failure_modes (list of exact IEC strings for that category)
    - downstream_blocks (list of block names this feeds)
    - fmeda_code (short code used in FMEDA: REF, BIAS, OSC, etc.)
    """
    cache_key = f"agent1_{json.dumps([b['name'] for b in blk_blocks], sort_keys=True)}"
    if not SKIP_CACHE and cache_key in cache:
        print("  [Agent 1] Using cached result")
        return cache[cache_key]

    # Build full chip context
    all_blocks_text = "\n".join(
        f"  {b['id']}: {b['name']} — {b['function']}" for b in blk_blocks
    )
    sm_text = "\n".join(
        f"  {s['id']}: {s['name']} — {s['description'][:100]}" for s in sm_blocks
    )

    # Available IEC categories
    categories_text = "\n".join(f"  - {k}" for k in IEC_FAILURE_MODES.keys())

    prompt = f"""You are an expert automotive IC functional safety engineer performing FMEDA analysis per ISO 26262.

You have received a chip dataset with the following blocks:

FUNCTIONAL BLOCKS:
{all_blocks_text}

SAFETY MECHANISMS:
{sm_text}

AVAILABLE IEC FAILURE MODE CATEGORIES:
{categories_text}

AVAILABLE FMEDA CODES (short internal names used in the FMEDA table):
REF, BIAS, LDO, OSC, TEMP, CSNS, ADC, CP, LOGIC, INTERFACE, TRIM,
SW_BANK_1, SW_BANK_2, SW_BANK_3, SW_BANK_4, SM01..SM24

TASK:
For EACH functional block (not SM blocks), determine:
1. "fmeda_code": the short FMEDA code (REF/BIAS/LDO/OSC/TEMP/CSNS/ADC/CP/LOGIC/INTERFACE/TRIM)
   - Bandgap/voltage reference → REF
   - Bias current generator → BIAS
   - Low-dropout regulator → LDO
   - Oscillator / watchdog (clock monitor) → OSC
   - Temperature sensor / thermal shutdown → TEMP
   - Current sense amplifier / comparator → CSNS
   - ADC / current DAC (both use ADC slot in this template) → ADC
   - Charge pump → CP
   - Logic controller → LOGIC
   - SPI/UART/serial interface → INTERFACE
   - NVM/trim/self-test/POST → TRIM
   - Switch bank / LED driver → SW_BANK_1 (then SW_BANK_2, etc.)
   - nFAULT driver / fault aggregator → CP (shares CP slot)
   
2. "iec_category": the IEC failure mode category key from the list above

3. "failure_modes": the EXACT list of failure mode strings for that category
   (copy exactly from the IEC library, do not invent new ones)

4. "downstream_blocks": list of OTHER block names (from this chip) whose operation
   depends on this block's output signal. Think about:
   - What does this block OUTPUT? (voltage, current, clock, data, enable signal)
   - Which blocks USE that output as input?

IMPORTANT: Multiple user blocks may map to the same fmeda_code.
If so, the FIRST occurrence takes priority. Mark duplicates with "duplicate": true.

Return a JSON ARRAY, one object per functional block, in the same order as the input:
[
  {{
    "id": "BLK-01",
    "name": "Bandgap Reference",
    "fmeda_code": "REF",
    "iec_category": "VOLTAGE_REFERENCE",
    "failure_modes": ["Output is stuck (i.e. high or low)", ...],
    "downstream_blocks": ["Internal BIAS Generator", "LDO Regulator", "ADC", ...],
    "duplicate": false
  }},
  ...
]

Return ONLY the JSON array, no explanation:"""

    print("  [Agent 1] Analyzing blocks and failure modes...")
    raw = query_llm(prompt, temperature=0.1)
    result = parse_json_from_response(raw)

    if not isinstance(result, list):
        print(f"  [Agent 1] Parse failed, raw: {raw[:200]}")
        # Fallback: use hardcoded category mapping
        result = _fallback_block_analysis(blk_blocks)

    # Add SM blocks (always category SAFETY_MECHANISM)
    for sm in sm_blocks:
        m = re.match(r'sm[-_\s]?(\d+)', sm['id'].lower())
        code = f"SM{int(m.group(1)):02d}" if m else sm['id'].upper().replace('-','').replace('_','')
        result.append({
            "id": sm['id'],
            "name": sm['name'],
            "fmeda_code": code,
            "iec_category": "SAFETY_MECHANISM",
            "failure_modes": IEC_FAILURE_MODES["SAFETY_MECHANISM"],
            "downstream_blocks": [],
            "is_sm": True,
        })

    cache[cache_key] = result
    save_cache(cache)
    return result


def _fallback_block_analysis(blk_blocks: list) -> list:
    """Hardcoded fallback if LLM fails."""
    KEYWORD_MAP = [
        (['bandgap','voltage reference','temperature-stable'],        'REF',  'VOLTAGE_REFERENCE'),
        (['bias current','reference current','bias generator'],       'BIAS', 'BIAS_CURRENT'),
        (['ldo','low dropout','linear regulator'],                    'LDO',  'LDO_REGULATOR'),
        (['oscillator','internal clock','4 mhz','watchdog','clock'],  'OSC',  'OSCILLATOR'),
        (['thermal shutdown','die temperature','on-chip diode'],      'TEMP', 'TEMPERATURE_SENSOR'),
        (['current sense','shunt','overcurrent comparator'],          'CSNS', 'CURRENT_SENSE_AMP'),
        (['current dac','8-bit current','dac for'],                   'ADC',  'ADC'),
        (['charge pump','boost'],                                     'CP',   'CHARGE_PUMP'),
        (['spi interface','serial interface','fault readback'],        'INTERFACE','SERIAL_INTERFACE'),
        (['self-test','post','validates dac','power-on self'],         'TRIM', 'NVM_TRIM'),
        (['nfault','open-drain fault','aggregates fault'],            'CP',   'CHARGE_PUMP'),
        (['logic','main control'],                                    'LOGIC','LOGIC_CONTROL'),
        (['open-load','short-to-gnd','switch bank','sw_bank'],        'LOGIC','LOGIC_CONTROL'),
    ]
    used = set()
    result = []
    for blk in blk_blocks:
        combined = (blk['name'] + ' ' + blk['function']).lower()
        code, cat = 'LOGIC', 'LOGIC_CONTROL'
        for keywords, c, cc in KEYWORD_MAP:
            if any(k in combined for k in keywords):
                code, cat = c, cc
                break
        dup = code in used
        if not dup: used.add(code)
        result.append({
            "id": blk['id'], "name": blk['name'],
            "fmeda_code": code, "iec_category": cat,
            "failure_modes": IEC_FAILURE_MODES.get(cat, []),
            "downstream_blocks": [],
            "duplicate": dup,
        })
    return result


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 2 — IC EFFECTS & SAFETY ANALYST
# For each (block, failure_mode) → IC effect, system effect, memo, SM columns
# ═════════════════════════════════════════════════════════════════════════════

def agent2_generate_effects(blocks_analyzed: list, cache: dict) -> list:
    """
    For each non-duplicate block, for each failure mode:
    Generate complete FMEDA row data including IC effects.
    Uses batching: one LLM call per block (not per mode).
    """
    result = []

    # Build chip architecture summary for context
    chip_arch = "\n".join(
        f"  {b.get('fmeda_code','?')} ({b['name']}): {b.get('iec_category','?')}"
        + (f" → feeds: {', '.join(b.get('downstream_blocks',[]))}" if b.get('downstream_blocks') else "")
        for b in blocks_analyzed
        if not b.get('duplicate') and not b.get('is_sm')
    )

    sm_list = [b for b in blocks_analyzed if b.get('is_sm')]
    sm_summary = "\n".join(f"  {s['fmeda_code']}: {s['name']}" for s in sm_list)

    for block in blocks_analyzed:
        code = block['fmeda_code']

        # SM blocks — fixed two-row pattern, no LLM needed
        if block.get('is_sm'):
            rows = _sm_rows(code)
            result.append({'fmeda_code': code, 'user_name': block['name'], 'rows': rows})
            print(f"  [Agent 2] {code} ({block['name']}): SM — 2 rows (hardcoded)")
            continue

        if block.get('duplicate'):
            print(f"  [Agent 2] {code} ({block['name']}): DUPLICATE — skipping")
            continue

        modes = block.get('failure_modes', [])
        if not modes:
            print(f"  [Agent 2] {code} ({block['name']}): no failure modes — skipping")
            continue

        cache_key = f"agent2_{code}_{block['name']}"
        if not SKIP_CACHE and cache_key in cache:
            print(f"  [Agent 2] {code}: using cache ({len(cache[cache_key])} rows)")
            result.append({'fmeda_code': code, 'user_name': block['name'], 'rows': cache[cache_key]})
            continue

        rows = _generate_block_effects(block, modes, chip_arch, sm_summary)
        cache[cache_key] = rows
        save_cache(cache)
        result.append({'fmeda_code': code, 'user_name': block['name'], 'rows': rows})
        print(f"  [Agent 2] {code} ({block['name']}): {len(rows)} rows generated")
        time.sleep(0.5)  # small pause between calls

    return result


def _generate_block_effects(block: dict, modes: list, chip_arch: str, sm_summary: str) -> list:
    """Call LLM to generate all row data for one block."""
    code      = block['fmeda_code']
    name      = block['name']
    func      = block.get('function', '')
    downstream = block.get('downstream_blocks', [])
    n         = len(modes)

    prompt = f"""You are an expert automotive IC functional safety engineer completing an FMEDA table per ISO 26262.

{FEW_SHOT_EXAMPLES}

═══════════════════════════════════════════════════
CHIP ARCHITECTURE (all blocks in this IC):
{chip_arch}

SAFETY MECHANISMS IN THIS IC:
{sm_summary}
═══════════════════════════════════════════════════
BLOCK TO ANALYZE:
  Code:     {code}
  Name:     {name}
  Function: {func}
  Known downstream blocks (receives signal from this block): {downstream}
═══════════════════════════════════════════════════
FAILURE MODES TO ANALYZE ({n} total):
{json.dumps(modes, indent=2)}
═══════════════════════════════════════════════════

TASK: For EACH failure mode, generate the complete FMEDA row data.

REASONING GUIDE:
1. What does {name} OUTPUT? (voltage level, current, clock signal, digital data, enable)
2. Which blocks in the chip USE that output as their input?
3. If this output fails in this way, what specifically goes wrong in each consumer block?
4. Does this failure ultimately cause: LED ON/OFF unintentionally? Fail-safe activation? Device damage? No visible effect?

{IC_EFFECT_FORMAT}

SAFETY COLUMN RULES:
- K (memo): "X" if failure can violate safety goal, "O" if not
- P (Single Point): "Y" if K=X, "N" if K=O
- R (% Safe Faults): 0 if K=X, 1 if K=O
- U (Coverage SPF): typical value 0.99 if safety mechanisms cover this failure
- S (SM IC): list the SM codes that detect/mitigate this failure
- Y (SM IC latent): same SM codes for latent coverage
- AA (Coverage latent): 1 if full latent coverage

SYSTEM EFFECT OPTIONS (choose the most accurate):
- "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication"
- "Fail-safe mode active\\nNo communication"
- "Unintended LED ON"
- "Unintended LED OFF"
- "Unintended LED ON/OFF"
- "Device damage"
- "Fail-safe mode active"
- "No effect"

Return a JSON ARRAY with {n} objects, one per failure mode, IN THE SAME ORDER:
[
  {{
    "G": "Output is stuck (i.e. high or low)",
    "I": "• BIAS\\n    - Output reference voltage is stuck\\n    - ...",
    "J": "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication",
    "K": "X",
    "P": "Y",
    "R": 0,
    "S": "SM01 SM15",
    "T": "",
    "U": 0.99,
    "X": "Y",
    "Y": "SM01 SM15",
    "Z": "",
    "AA": 1,
    "AB": "",
    "AD": "SM01 SM15 make the IC enter a safe-state. Latent coverage: 100%."
  }},
  ...
]

Return ONLY the JSON array ({n} items), no explanation:"""

    raw = query_llm(prompt, temperature=0.05)
    parsed = parse_json_from_response(raw)

    if isinstance(parsed, list) and len(parsed) >= len(modes):
        # Validate and clean each row
        rows = []
        for i, rd in enumerate(parsed[:len(modes)]):
            row = _validate_row(rd, modes[i])
            rows.append(row)
        return rows
    else:
        print(f"    WARNING: LLM returned {len(parsed) if isinstance(parsed, list) else 'non-list'}, expected {len(modes)}")
        print(f"    Using safe fallback for {code}")
        return _fallback_rows(modes, code)


def _validate_row(rd: dict, mode: str) -> dict:
    """Ensure row has all required fields and consistent values."""
    memo = str(rd.get('K', 'O')).strip()
    # Consistency: if IC effect is No effect → force memo O
    ic = str(rd.get('I', 'No effect')).strip()
    if ic == 'No effect':
        memo = 'O'
    rd['K'] = memo
    rd['G'] = mode  # always use the canonical mode string
    rd['P'] = 'Y' if memo.startswith('X') else 'N'
    rd['R'] = 0 if memo.startswith('X') else 1
    rd['X'] = rd.get('X', 'Y' if memo.startswith('X') else 'N')
    rd['O'] = 1
    # Ensure all expected keys exist
    for k in ['I','J','K','P','R','S','T','U','V','X','Y','Z','AA','AB','AD']:
        if k not in rd:
            rd[k] = '' if k in ['S','T','V','Y','Z','AB','AD'] else (None if k in ['U','AA'] else '')
    return rd


def _fallback_rows(modes: list, code: str) -> list:
    """Safe fallback if LLM fails — generates minimal valid rows."""
    rows = []
    SAFE_MODES = ['spike','oscillation within','start-up','startup','jitter',
                  'duty cycle','quiescent','settling','false detection']
    for mode in modes:
        m = mode.lower()
        is_safe = any(kw in m for kw in SAFE_MODES)
        memo = 'O' if is_safe else 'X'
        rows.append({
            'G': mode, 'I': 'No effect' if is_safe else f'• {code}\n    - Failure in {code} output',
            'J': 'No effect' if is_safe else 'Fail-safe mode active',
            'K': memo, 'O': 1, 'P': 'N' if is_safe else 'Y',
            'R': 1 if is_safe else 0,
            'S':'','T':'','U': '' if is_safe else 0.99,'V':'',
            'X':'N' if is_safe else 'Y','Y':'','Z':'','AA': '' if is_safe else 1,'AB':'','AD':'',
        })
    return rows


def _sm_rows(sm_code: str) -> list:
    """Fixed SM block rows — always Fail to detect + False detection."""
    SM_IC = {
        'SM01': ('Unintended LED ON',                        'Unintended LED ON'),
        'SM02': ('Device damage',                            'Device damage'),
        'SM03': ('Unintended LED ON',                        'Unintended LED ON'),
        'SM04': ('Unintended LED OFF',                       'Unintended LED OFF'),
        'SM05': ('Unintended LED OFF',                       'Unintended LED OFF'),
        'SM06': ('Unintended LED OFF',                       'Unintended LED OFF'),
        'SM07': ('Unintended LED ON/OFF',                    'Unintended LED ON/OFF'),
        'SM08': ('Unintended LED ON',                        'Unintended LED ON'),
        'SM09': ('UART Communication Error',                 'Fail-safe mode active'),
        'SM10': ('UART Communication Error',                 'Fail-safe mode active'),
        'SM11': ('UART Communication Error',                 'Fail-safe mode active'),
        'SM12': ('No PWM monitoring functionality',          'No effect'),
        'SM13': ('Unintended LED ON/OFF in FS mode',         'Unintended LED ON/OFF in FS mode'),
        'SM14': ('Unintended LED ON',                        'Unintended LED ON'),
        'SM15': ('Failures on LOGIC operation',              'Possible Fail-safe mode activation'),
        'SM16': ('Loss of reference control functionality',  'No effect'),
        'SM17': ('Device damage',                            'Device damage'),
        'SM18': ('Cannot trim part properly',                'Performance/Functionality degredation'),
        'SM19': ('Loss of safety mechanism functionality',   'Fail-safe mode active'),
        'SM20': ('Device damage',                            'Device damage'),
        'SM21': ('Unsynchronised PWM',                       'No effect'),
        'SM22': ('Unintended LED OFF',                       'Unintended LED OFF'),
        'SM23': ('Loss of thermal monitoring capability',    'Possible device damage'),
        'SM24': ('Loss of LED voltage monitoring capability','No effect'),
    }
    ic, sys = SM_IC.get(sm_code, ('Loss of safety mechanism functionality', 'Fail-safe mode active'))
    lat = 'Y'
    return [
        {'G':'Fail to detect', 'I':ic, 'J':sys, 'K':'X (Latent)',
         'O':1,'P':'Y','R':0,'S':'','T':'','U':'','V':'',
         'X':lat,'Y':'','Z':'','AA':'','AB':'','AD':''},
        {'G':'False detection','I':'No effect','J':'No effect','K':'O',
         'O':1,'P':'N','R':1,'S':'','T':'','U':'','V':'',
         'X':'N','Y':'','Z':'','AA':'','AB':'','AD':''},
    ]


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 3 — CRITIC / VALIDATOR
# Reviews full JSON for consistency issues
# ═════════════════════════════════════════════════════════════════════════════

def agent3_validate(fmeda_data: list, cache: dict) -> list:
    """
    LLM critic reviews the complete FMEDA data for consistency.
    Returns the (potentially corrected) data plus a validation report.
    """
    cache_key = f"agent3_{hash(json.dumps(fmeda_data, default=str, sort_keys=True))}"
    if not SKIP_CACHE and cache_key in cache:
        print("  [Agent 3] Using cached validation")
        return cache[cache_key]['data']

    # Build summary for the critic
    summary = []
    for block in fmeda_data:
        for row in block['rows']:
            summary.append({
                'block': block['fmeda_code'],
                'mode':  row['G'][:50],
                'ic':    str(row.get('I',''))[:60],
                'memo':  row.get('K',''),
            })

    prompt = f"""You are a functional safety FMEDA auditor reviewing an FMEDA table for an automotive IC.

Review the following FMEDA entries and identify any issues:

{json.dumps(summary, indent=2)[:6000]}

CHECK FOR:
1. MEMO INCONSISTENCY: If IC effect is "No effect" but memo is "X" → should be "O"
2. MEMO INCONSISTENCY: If IC effect describes failures but memo is "O" → should be "X"  
3. MISSING EFFECTS: Blocks that should affect downstream blocks but show "No effect"
4. FORMAT ERRORS: IC effect not using correct bullet format (• BLOCK\\n    - effect)

Return a JSON object:
{{
  "issues": [
    {{"block": "REF", "mode": "...", "issue": "memo should be X not O"}},
    ...
  ],
  "corrections": [
    {{"block": "REF", "mode_index": 0, "field": "K", "old": "O", "new": "X"}},
    ...
  ],
  "summary": "Overall quality assessment"
}}

If no issues found, return: {{"issues": [], "corrections": [], "summary": "FMEDA looks consistent"}}

Return ONLY the JSON:"""

    print("  [Agent 3] Running validation...")
    raw = query_llm(prompt, temperature=0.05)
    validation = parse_json_from_response(raw)

    if validation and isinstance(validation, dict):
        corrections = validation.get('corrections', [])
        issues      = validation.get('issues', [])
        summary_txt = validation.get('summary', '')

        print(f"  [Agent 3] Found {len(issues)} issues, {len(corrections)} corrections")
        print(f"  [Agent 3] {summary_txt}")

        # Apply corrections
        block_index = {b['fmeda_code']: b for b in fmeda_data}
        for corr in corrections:
            bcode = corr.get('block')
            midx  = corr.get('mode_index')
            field = corr.get('field')
            new_v = corr.get('new')
            if bcode in block_index and midx is not None and field:
                rows = block_index[bcode]['rows']
                if 0 <= midx < len(rows) and field in rows[midx]:
                    old_v = rows[midx][field]
                    rows[midx][field] = new_v
                    print(f"    CORRECTED: {bcode}[{midx}].{field}: {old_v} → {new_v}")

    cache[cache_key] = {'data': fmeda_data, 'validation': validation}
    save_cache(cache)
    return fmeda_data


# ═════════════════════════════════════════════════════════════════════════════
# AGENT 4 — TEMPLATE WRITER (100% hardcoded, deterministic)
# ═════════════════════════════════════════════════════════════════════════════

def scan_placeholders(ws) -> dict:
    idx = {}
    for ws_row in ws.iter_rows():
        for cell in ws_row:
            if cell.__class__.__name__ == 'MergedCell':
                continue
            v = str(cell.value) if cell.value is not None else ''
            if v.startswith('{{FMEDA_') and v.endswith('}}'):
                idx[v] = cell
    return idx


def get_block_groups(idx: dict, data_start: int = 22) -> list:
    d_rows = sorted({
        int(re.search(r'(\d+)', k).group(1))
        for k in idx
        if re.match(r'\{\{FMEDA_D\d+\}\}', k)
        and int(re.search(r'(\d+)', k).group(1)) >= data_start
    })
    all_rows = sorted({
        int(re.search(r'(\d+)', k).group(1))
        for k in idx
        if re.match(r'\{\{FMEDA_[A-Z]+\d+\}\}', k)
        and int(re.search(r'(\d+)', k).group(1)) >= data_start
    })
    groups = []
    for i, first in enumerate(d_rows):
        nxt = d_rows[i+1] if i+1 < len(d_rows) else 999999
        groups.append([r for r in all_rows if first <= r < nxt])
    return groups


def write_cell(idx: dict, col: str, row_num: int, value, wrap: bool = False):
    key = '{{FMEDA_' + col + str(row_num) + '}}'
    if key not in idx:
        return
    cell = idx[key]
    if value is None or str(value).strip() in ('', 'None', 'nan'):
        cell.value = None
        return
    cell.value = value
    if wrap and isinstance(value, str) and '\n' in value:
        old = cell.alignment or Alignment()
        cell.alignment = Alignment(
            wrap_text=True,
            vertical=old.vertical or 'center',
            horizontal=old.horizontal or 'left',
        )


def agent4_write_template(fmeda_data: list, template_path: str, output_path: str):
    shutil.copy2(template_path, output_path)
    wb = openpyxl.load_workbook(output_path)
    ws = wb['FMEDA']

    idx    = scan_placeholders(ws)
    groups = get_block_groups(idx)

    print(f"\n  [Agent 4] Template groups: {len(groups)}")
    print(f"  [Agent 4] Blocks to write: {len(fmeda_data)}")

    fm = 1  # global failure mode counter

    for bi, block in enumerate(fmeda_data):
        code = block['fmeda_code']
        rows = block['rows']

        if bi >= len(groups):
            print(f"  [Agent 4] WARNING: no more template groups at block {bi+1} ({code})")
            break

        group_rows = groups[bi]
        n_t = len(group_rows)
        n_d = len(rows)
        if n_d > n_t:
            rows = rows[:n_t]

        for mi, row_num in enumerate(group_rows):
            rd       = rows[mi] if mi < len(rows) else None
            is_first = (mi == 0)

            # B — Failure Mode Number
            write_cell(idx, 'B', row_num, f'FM_TTL_{fm}' if rd else None)
            # C — Component Name (every row)
            write_cell(idx, 'C', row_num, code)
            # D — Block Name (first row only)
            write_cell(idx, 'D', row_num, code if is_first else None)
            # E — Block FIT (blank — formula driven)
            write_cell(idx, 'E', row_num, None)

            if rd is None:
                write_cell(idx, 'G', row_num, None)
                continue

            memo = str(rd.get('K', 'O')).strip()
            sp   = str(rd.get('P', 'Y' if memo.startswith('X') else 'N')).strip()
            pct  = rd.get('R', 1 if memo == 'O' else 0)

            # F — Mode FIT fraction (blank — formula driven)
            write_cell(idx, 'F', row_num, None)
            # G — Standard failure mode
            write_cell(idx, 'G', row_num, rd.get('G', ''), wrap=True)
            # H — Failure Mode (blank per real FMEDA)
            write_cell(idx, 'H', row_num, None)
            # I — Effects on IC output
            write_cell(idx, 'I', row_num, rd.get('I', 'No effect'), wrap=True)
            # J — Effects on system
            write_cell(idx, 'J', row_num, rd.get('J', 'No effect'), wrap=True)
            # K — Memo
            write_cell(idx, 'K', row_num, memo)
            # O — Failure distribution
            write_cell(idx, 'O', row_num, 1)
            # P — Single Point Y/N
            write_cell(idx, 'P', row_num, sp)
            # Q — Failure rate FIT (blank — formula driven)
            write_cell(idx, 'Q', row_num, None)
            # R — Pct safe faults
            write_cell(idx, 'R', row_num, pct)
            # S — SM IC
            write_cell(idx, 'S', row_num, rd.get('S') or None, wrap=True)
            # T — SM System
            write_cell(idx, 'T', row_num, rd.get('T') or None, wrap=True)
            # U — Coverage SPF
            v = rd.get('U', '')
            write_cell(idx, 'U', row_num, v if v not in ('', None) else None)
            # V — Residual FIT (blank — formula)
            write_cell(idx, 'V', row_num, None)
            # X — Latent Y/N
            write_cell(idx, 'X', row_num, rd.get('X', 'Y' if memo.startswith('X') else 'N'))
            # Y — SM IC latent
            write_cell(idx, 'Y', row_num, rd.get('Y') or None, wrap=True)
            # Z — SM System latent
            write_cell(idx, 'Z', row_num, rd.get('Z') or None, wrap=True)
            # AA — Coverage latent
            v = rd.get('AA', '')
            write_cell(idx, 'AA', row_num, v if v not in ('', None) else None)
            # AB — Latent MPF FIT (blank — formula)
            write_cell(idx, 'AB', row_num, None)
            # AD — Comment
            write_cell(idx, 'AD', row_num, rd.get('AD') or None, wrap=True)

            fm += 1

        print(f"  [Agent 4] [{bi+1}/{len(fmeda_data)}] {code}: {min(n_d,n_t)} modes → FM_TTL_{fm-min(n_d,n_t)}–FM_TTL_{fm-1}")

    wb.save(output_path)
    print(f"\n  [Agent 4] Saved → {output_path}")
    print(f"  [Agent 4] Total failure modes: {fm - 1}")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def run():
    print("╔══════════════════════════════════════════════╗")
    print("║   FMEDA Multi-Agent Pipeline                 ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"\nDataset:  {DATASET_FILE}")
    print(f"Template: {TEMPLATE_FILE}")
    print(f"Model:    {OLLAMA_MODEL}")
    print(f"Output:   {OUTPUT_FILE}\n")

    cache = load_cache()

    # ── Step 0: Read dataset ──────────────────────────────────────────────────
    print("━━━ Step 0: Reading dataset ━━━")
    blk_blocks, sm_blocks = read_dataset(DATASET_FILE)
    print(f"  BLK blocks: {len(blk_blocks)}")
    for b in blk_blocks:
        print(f"    {b['id']}: {b['name']}")
    print(f"  SM mechanisms: {len(sm_blocks)}")
    for s in sm_blocks:
        print(f"    {s['id']}: {s['name']}")

    # ── Agent 1: Block Analysis ───────────────────────────────────────────────
    print("\n━━━ Agent 1: Block Analyst (LLM) ━━━")
    blocks_analyzed = agent1_analyze_blocks(blk_blocks, sm_blocks, cache)
    print(f"  Analyzed {len(blocks_analyzed)} blocks:")
    for b in blocks_analyzed:
        dup = " [DUPLICATE]" if b.get('duplicate') else ""
        sm  = " [SM]" if b.get('is_sm') else ""
        print(f"    {b['name']} → {b['fmeda_code']} ({b['iec_category']}) {dup}{sm}")

    # ── Agent 2: IC Effects Generation ────────────────────────────────────────
    print("\n━━━ Agent 2: IC Effects Analyst (LLM) ━━━")
    fmeda_data = agent2_generate_effects(blocks_analyzed, cache)

    # Save intermediate
    with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
        json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

    # ── Agent 3: Validation ────────────────────────────────────────────────────
    print("\n━━━ Agent 3: Critic / Validator (LLM) ━━━")
    fmeda_data = agent3_validate(fmeda_data, cache)

    # ── Agent 4: Template Writer ───────────────────────────────────────────────
    print("\n━━━ Agent 4: Template Writer (deterministic) ━━━")
    agent4_write_template(fmeda_data, TEMPLATE_FILE, OUTPUT_FILE)

    print("\n✅  Pipeline complete!")
    print(f"    Output:       {OUTPUT_FILE}")
    print(f"    Intermediate: {INTERMEDIATE_JSON}")
    print(f"    Cache:        {CACHE_FILE}")


if __name__ == '__main__':
    run()
