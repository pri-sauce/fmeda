"""
fmeda_agents.py  —  Two-Agent FMEDA Pipeline
=============================================

AGENT 1  (LLM)
  Input : each block's name + function  +  the full IEC failure mode table
  Task  : decide which IEC part_name this block maps to,
          then pull those exact failure modes verbatim
  Output: { block_code, block_name, iec_part, modes[] }

AGENT 2  (LLM)
  Input : one block  +  the complete list of all other blocks in the chip
          +  one failure mode at a time
  Task  : reason about which downstream blocks are affected and how,
          produce the exact bullet-format IC effect string
  Output: adds  effects_on_ic_output,  effects_on_system,  memo  to each row

AGENT 3  (Hardcoded)  —  Template Writer
  Input : completed JSON
  Task  : fill FMEDA_TEMPLATE.xlsx placeholder cells with zero deviation

Usage:
    python fmeda_agents.py

Config: edit the CONFIG section below.
"""

import json, re, time, shutil, sys
import pandas as pd
import openpyxl
import requests
from openpyxl.styles import Alignment

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DATASET_FILE      = 'fusa_ai_agent_mock_data.xlsx'
BLK_SHEET         = 'BLK'
SM_SHEET          = 'SM'
IEC_TABLE_FILE    = 'pdf_extracted.json'
TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
OUTPUT_FILE       = 'FMEDA_filled.xlsx'
CACHE_FILE        = 'fmeda_cache.json'
INTERMEDIATE_JSON = 'fmeda_intermediate.json'

OLLAMA_URL        = 'http://localhost:11434/api/generate'
OLLAMA_MODEL      = 'qwen3:30b'
OLLAMA_TIMEOUT    = 300
SKIP_CACHE        = False   # True = re-run LLM even if cached
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def query_llm(prompt: str, temperature: float = 0.1) -> str:
    try:
        r = requests.post(OLLAMA_URL, json={
            "model":  OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature":    temperature,
                "num_ctx":        16384,
                "top_p":          0.9,
                "repeat_penalty": 1.1,
            }
        }, timeout=OLLAMA_TIMEOUT)
        r.raise_for_status()
        return r.json()["response"].strip()
    except requests.exceptions.ConnectionError:
        print("  Cannot connect to Ollama. Is it running? (ollama serve)")
        sys.exit(1)
    except Exception as e:
        print(f"  LLM error: {e}")
        return ""


def parse_json(text: str):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE).strip()
    for pattern in [r'\[.*\]', r'\{.*\}']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# READ INPUTS
# ═══════════════════════════════════════════════════════════════════════════════

def read_dataset():
    xl = pd.ExcelFile(DATASET_FILE)
    df = pd.read_excel(DATASET_FILE, sheet_name=BLK_SHEET, dtype=str).fillna('')
    blk_blocks = []
    for _, row in df.iterrows():
        vals = [v.strip() for v in row.values if str(v).strip()]
        if len(vals) >= 2:
            blk_blocks.append({
                'id':       vals[0],
                'name':     vals[1],
                'function': vals[2] if len(vals) > 2 else '',
            })

    sm_blocks = []
    if SM_SHEET in xl.sheet_names:
        df_sm = pd.read_excel(DATASET_FILE, sheet_name=SM_SHEET, dtype=str).fillna('')
        for _, row in df_sm.iterrows():
            vals = [v.strip() for v in row.values if str(v).strip()]
            if vals and re.match(r'sm[-_\s]?\d+', vals[0].lower()):
                sm_blocks.append({
                    'id':          vals[0],
                    'name':        vals[1] if len(vals) > 1 else '',
                    'description': vals[2] if len(vals) > 2 else '',
                })

    return blk_blocks, sm_blocks


def read_iec_table():
    with open(IEC_TABLE_FILE, encoding='utf-8-sig') as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 1 — BLOCK to IEC PART MAPPER
# ═══════════════════════════════════════════════════════════════════════════════

def agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache):
    cache_key = "agent1__" + json.dumps([b['name'] for b in blk_blocks])
    if not SKIP_CACHE and cache_key in cache:
        print("  [Agent 1] Loaded from cache")
        result = cache[cache_key]
        _append_sm_blocks(result, sm_blocks)
        return result

    # Compact IEC table summary for prompt
    iec_summary = ""
    for i, p in enumerate(iec_table):
        modes_preview = p["entries"][0]["modes"][:3]
        iec_summary += (
            f'  {i+1:2d}. "{p["part_name"]}"\n'
            f'       Desc : {p["entries"][0]["description"][:110]}\n'
            f'       Modes: {json.dumps(modes_preview)}'
            + (' ...' if len(p["entries"][0]["modes"]) > 3 else '') + '\n\n'
        )

    blocks_text = "\n".join(
        f'  {b["id"]}: "{b["name"]}" — {b["function"]}'
        for b in blk_blocks
    )

    prompt = f"""You are an automotive IC functional safety engineer assigning IEC failure mode categories.

CHIP BLOCKS:
{blocks_text}

IEC HARDWARE PART CATEGORIES (from IEC 62380 / AEC-Q100):
{iec_summary}

FMEDA SHORT CODE RULES:
  Voltage reference / bandgap             → REF
  Bias current source / generator         → BIAS
  LDO / linear voltage regulator          → LDO
  Internal oscillator / clock generator   → OSC
  Watchdog / clock monitor                → OSC   (duplicate of OSC)
  Temperature sensor                      → TEMP
  Current sense amplifier                 → CSNS
  Current DAC / channel DAC               → ADC
  ADC (analogue-to-digital converter)     → ADC
  Charge pump / boost                     → CP
  nFAULT driver / fault aggregator        → CP    (duplicate of CP)
  Digital logic / main controller         → LOGIC
  Open-load detector / short-to-GND det. → LOGIC  (duplicate of LOGIC)
  SPI / UART / serial interface           → INTERFACE
  NVM / trim / self-test / POST           → TRIM
  Switch bank / LED driver bank N         → SW_BANK_N

TASK: For each block, determine:
  1. "fmeda_code"    — short code from the rules above
  2. "iec_part"      — exact "part_name" string from the IEC list that BEST fits this block
  3. "is_duplicate"  — true if this fmeda_code was already assigned to an earlier block

Return JSON array (same order as input blocks):
[
  {{
    "id": "BLK-01",
    "name": "Bandgap Reference",
    "fmeda_code": "REF",
    "iec_part": "Voltage references",
    "is_duplicate": false
  }},
  ...
]

Return ONLY the JSON array:"""

    print("  [Agent 1] Calling LLM...")
    raw    = query_llm(prompt, temperature=0.05)
    result = parse_json(raw)

    if not isinstance(result, list) or len(result) != len(blk_blocks):
        print("  [Agent 1] Parse issue — using fallback")
        result = _fallback_agent1(blk_blocks, iec_table)

    # KEY STEP: replace LLM modes with verbatim modes from the IEC table
    iec_modes_index = {p['part_name']: p['entries'][0]['modes'] for p in iec_table}
    for b in result:
        iec_part = b.get('iec_part', '')
        # Exact match
        if iec_part in iec_modes_index:
            b['modes'] = iec_modes_index[iec_part]
        else:
            # Fuzzy match on first 25 chars
            matched = False
            for part_name, modes in iec_modes_index.items():
                if iec_part[:25].lower() in part_name.lower() or \
                   part_name[:25].lower() in iec_part.lower():
                    b['modes'] = modes
                    b['iec_part'] = part_name
                    matched = True
                    break
            if not matched:
                b['modes'] = []
                print(f"  [Agent 1] WARNING: no IEC modes found for '{iec_part}' ({b['name']})")

    # Enforce duplicate flags correctly
    seen_codes = set()
    for b in result:
        code = b.get('fmeda_code', '')
        if code in seen_codes:
            b['is_duplicate'] = True
        else:
            b['is_duplicate'] = False
            seen_codes.add(code)

    cache[cache_key] = result
    save_cache(cache)

    _append_sm_blocks(result, sm_blocks)
    return result


def _append_sm_blocks(result, sm_blocks):
    for sm in sm_blocks:
        m = re.match(r'sm[-_\s]?(\d+)', sm['id'].lower())
        code = f"SM{int(m.group(1)):02d}" if m else sm['id'].upper()
        result.append({
            'id':           sm['id'],
            'name':         sm['name'],
            'function':     sm.get('description', ''),
            'fmeda_code':   code,
            'iec_part':     'Safety Mechanism',
            'modes':        ['Fail to detect', 'False detection'],
            'is_duplicate': False,
            'is_sm':        True,
        })


def _fallback_agent1(blk_blocks, iec_table):
    iec_names = [p['part_name'] for p in iec_table]
    KMAP = [
        (['bandgap','voltage reference','1.2v','temperature-stable ref'], 'REF',       'Voltage references'),
        (['bias current','current source','bias generator'],               'BIAS',      'Current source (including bias current generator)'),
        (['ldo','low dropout','linear regulator'],                         'LDO',       'Voltage regulators (linear, SMPS, etc.)'),
        (['oscillator','internal clock','4 mhz','watchdog','clock'],       'OSC',       'Oscillator'),
        (['thermal shutdown','die temperature','on-chip diode'],           'TEMP',      'Operational amplifier and buffer'),
        (['current sense','shunt','sense amplifier','overcurrent'],        'CSNS',      'Operational amplifier and buffer'),
        (['current dac','channel dac','8-bit current','dac for'],          'ADC',       'N bits digital to analogue converters (DAC)d'),
        (['charge pump','boost'],                                          'CP',        'Charge pump, regulator boost'),
        (['spi interface','serial interface','uart','fault readback'],      'INTERFACE', 'N bits analogue to digital converters (N-bit ADC)'),
        (['self-test','post','power-on self','validates dac'],             'TRIM',      'Voltage references'),
        (['nfault','open-drain fault','aggregates fault'],                 'CP',        'Charge pump, regulator boost'),
        (['open-load','short-to-gnd','detector','logic'],                  'LOGIC',     'Voltage/Current comparator'),
    ]
    used = set()
    result = []
    for b in blk_blocks:
        combined = (b['name'] + ' ' + b['function']).lower()
        code, iec_part = 'LOGIC', 'Voltage/Current comparator'
        for kws, c, ip in KMAP:
            if any(k in combined for k in kws):
                code, iec_part = c, ip
                break
        dup = code in used
        if not dup:
            used.add(code)
        result.append({
            'id': b['id'], 'name': b['name'], 'function': b['function'],
            'fmeda_code': code, 'iec_part': iec_part, 'is_duplicate': dup,
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 2 — IC EFFECTS GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

IC_FORMAT = """
EXACT FORMAT for col I "effects on IC output":
  • BLOCK_CODE
      - specific effect line
      - another effect if applicable
  • ANOTHER_BLOCK_CODE
      - specific effect

  If nothing is affected → write exactly: No effect

RULES:
  - Use • before block name, no indent
  - Use 4 spaces + dash before each effect line
  - Use the short FMEDA block codes (REF, BIAS, LDO, OSC, TEMP, CSNS, ADC, CP, LOGIC, INTERFACE, TRIM, SW_BANK_x)
  - Be specific: "Output reference voltage is stuck" not just "BIAS is affected"
""".strip()

FEW_SHOT = """
REAL FMEDA VERIFIED EXAMPLES:

REF / "Output is stuck (i.e. high or low)":
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
  → system: "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication"  memo: X

REF / "Output voltage affected by spikes":
No effect  → system: "No effect"  memo: O

BIAS / "One or more outputs are stuck (i.e. high or low)":
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
  → system: "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication"  memo: X

OSC / "Output is stuck (i.e. high or low)":
• LOGIC
    - Cannot operate.
    - Communication error.
  → system: "Fail-safe mode active\\nNo communication"  memo: X

TEMP / "Output is stuck (i.e. high or low)":
• ADC
    - TEMP output is stuck low
• SW_BANK_x
    - SW is stuck in off state (DIETEMP)
  → system: "Unintended LED ON"  memo: X

CSNS / "Output is stuck (i.e. high or low)":
• ADC
    - CSNS output is incorrect.
  → system: "No effect"  memo: O

SAFE MODES (always No effect, memo O):
  spikes, oscillation within range, start-up time, jitter, duty cycle,
  quiescent current, settling time, false detection (SM blocks)
""".strip()


def agent2_generate_effects(blocks, cache):
    # Active block context
    active = [b for b in blocks if not b.get('is_duplicate') and not b.get('is_sm')]
    chip_ctx = "\n".join(
        f"  {b['fmeda_code']:<12} {b['name']:<35} | {b.get('function','')[:80]}"
        for b in active
    )

    result = []
    for block in blocks:
        code  = block['fmeda_code']
        name  = block['name']
        modes = block.get('modes', [])

        # SM blocks — hardcoded, no LLM
        if block.get('is_sm'):
            rows = _sm_rows(code)
            result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
            print(f"  [Agent 2] {code:<12} SM hardcoded (2 rows)")
            continue

        # Duplicates — skip
        if block.get('is_duplicate'):
            print(f"  [Agent 2] {code:<12} DUPLICATE — skipped")
            continue

        if not modes:
            print(f"  [Agent 2] {code:<12} no modes — skipped")
            continue

        ck = f"agent2__{code}__{name}__{len(modes)}"
        if not SKIP_CACHE and ck in cache:
            print(f"  [Agent 2] {code:<12} cache ({len(cache[ck])} rows)")
            result.append({'fmeda_code': code, 'user_name': name, 'rows': cache[ck]})
            continue

        rows = _llm_effects(block, chip_ctx, modes)
        cache[ck] = rows
        save_cache(cache)
        result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
        print(f"  [Agent 2] {code:<12} {len(rows)} rows generated")
        time.sleep(0.3)

    return result


def _llm_effects(block, chip_ctx, modes):
    code = block['fmeda_code']
    name = block['name']
    func = block.get('function', '')
    n    = len(modes)

    prompt = f"""You are an automotive IC FMEDA expert filling the "effects on IC output" column.

{FEW_SHOT}

═══════════════════════════════════════
ALL BLOCKS IN THIS CHIP:
{chip_ctx}
═══════════════════════════════════════
BLOCK BEING ANALYZED:
  Code     : {code}
  Name     : {name}
  Function : {func}
═══════════════════════════════════════
FAILURE MODES TO ANALYZE ({n} total):
{json.dumps(modes, indent=2)}
═══════════════════════════════════════

For EACH failure mode, reason step by step:
  1. What does {name} output? (voltage, current, clock, data)
  2. Which blocks in the chip receive that output as input?
  3. If this failure mode occurs, what breaks in each receiver?
  4. What does the end user observe? (LED ON/OFF, fail-safe, damage, nothing)

Then fill:
  "I" = effects on IC output  (exact bullet format below)
  "J" = effects on system     (choose from: "Unintentional LED ON/OFF\\nFail-safe mode active\\nNo communication" / "Fail-safe mode active\\nNo communication" / "Unintended LED ON" / "Unintended LED OFF" / "Unintended LED ON/OFF" / "Device damage" / "Fail-safe mode active" / "No effect")
  "K" = memo — "X" if safety goal violated, "O" if not
        RULE: if I == "No effect" → K must be "O"
        RULE: if I lists downstream failures → K must be "X"

{IC_FORMAT}

Return JSON array with exactly {n} objects:
[
  {{"G": "<exact mode string>", "I": "<IC effect>", "J": "<system effect>", "K": "X or O"}},
  ...
]

Return ONLY the JSON array:"""

    raw    = query_llm(prompt, temperature=0.05)
    parsed = parse_json(raw)

    if isinstance(parsed, list) and len(parsed) >= n:
        return [_build_row(parsed[i], modes[i]) for i in range(n)]

    print(f"    LLM parse failed for {code} — using fallback")
    return _fallback_rows(modes)


def _build_row(rd, canonical_mode):
    memo = str(rd.get('K', 'O')).strip()
    ic   = str(rd.get('I', 'No effect')).strip()
    sys_ = str(rd.get('J', 'No effect')).strip()

    if ic == 'No effect':
        memo = 'O'
        sys_ = 'No effect'
    if memo not in ('X', 'O'):
        memo = 'O'

    return {
        'G': canonical_mode,
        'I': ic,
        'J': sys_,
        'K': memo,
        'O': 1,
        'P': 'Y' if memo == 'X' else 'N',
        'R': 0   if memo == 'X' else 1,
        'S': '', 'T': '', 'U': '', 'V': '',
        'X': 'Y' if memo == 'X' else 'N',
        'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': '',
    }


def _fallback_rows(modes):
    SAFE = ['spike','oscillation within','start-up','jitter','duty cycle',
            'quiescent','settling','false detection']
    rows = []
    for mode in modes:
        safe = any(k in mode.lower() for k in SAFE)
        rows.append(_build_row({'G': mode, 'I': 'No effect' if safe else '',
                                'J': 'No effect' if safe else '', 'K': 'O' if safe else 'X'}, mode))
    return rows


def _sm_rows(sm_code):
    SM = {
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
    ic, sys_ = SM.get(sm_code, ('Loss of safety mechanism functionality', 'Fail-safe mode active'))
    return [
        {'G': 'Fail to detect',  'I': ic,          'J': sys_,        'K': 'X (Latent)',
         'O': 1, 'P': 'Y', 'R': 0, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'Y', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
        {'G': 'False detection', 'I': 'No effect', 'J': 'No effect', 'K': 'O',
         'O': 1, 'P': 'N', 'R': 1, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'N', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3 — TEMPLATE WRITER  (deterministic)
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_placeholders(ws):
    idx = {}
    for ws_row in ws.iter_rows():
        for cell in ws_row:
            if cell.__class__.__name__ == 'MergedCell':
                continue
            v = str(cell.value) if cell.value is not None else ''
            if v.startswith('{{FMEDA_') and v.endswith('}}'):
                idx[v] = cell
    return idx


def _get_groups(idx, data_start=22):
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


def _write(idx, col, row_num, value, wrap=False):
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
        cell.alignment = Alignment(wrap_text=True,
                                   vertical=old.vertical or 'center',
                                   horizontal=old.horizontal or 'left')


def agent3_write_template(fmeda_data):
    shutil.copy2(TEMPLATE_FILE, OUTPUT_FILE)
    wb = openpyxl.load_workbook(OUTPUT_FILE)
    ws = wb['FMEDA']

    idx    = _scan_placeholders(ws)
    groups = _get_groups(idx)

    print(f"\n  [Agent 3] Template groups : {len(groups)}")
    print(f"  [Agent 3] Blocks to write : {len(fmeda_data)}")

    fm = 1

    for bi, block in enumerate(fmeda_data):
        code = block['fmeda_code']
        rows = block['rows']

        if bi >= len(groups):
            print(f"  [Agent 3] WARNING: no more groups at block {bi+1} ({code})")
            break

        group_rows = groups[bi]
        n_t = len(group_rows)
        n_d = len(rows)
        if n_d > n_t:
            print(f"  [Agent 3] {code}: {n_d} modes > {n_t} slots — truncating")
            rows = rows[:n_t]

        for mi, row_num in enumerate(group_rows):
            rd       = rows[mi] if mi < len(rows) else None
            is_first = (mi == 0)

            _write(idx, 'B', row_num, f'FM_TTL_{fm}' if rd else None)
            _write(idx, 'C', row_num, code)
            _write(idx, 'D', row_num, code if is_first else None)
            _write(idx, 'E', row_num, None)   # Block FIT — formula/engineer

            if rd is None:
                _write(idx, 'G', row_num, None)
                continue

            memo = str(rd.get('K', 'O')).strip()
            sp   = str(rd.get('P', 'Y' if memo.startswith('X') else 'N')).strip()
            pct  = rd.get('R', 1 if memo == 'O' else 0)

            _write(idx, 'F',  row_num, None)                              # Mode FIT — formula
            _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
            _write(idx, 'H',  row_num, None)                              # Failure Mode — blank
            _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
            _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
            _write(idx, 'K',  row_num, memo)
            _write(idx, 'O',  row_num, 1)
            _write(idx, 'P',  row_num, sp)
            _write(idx, 'Q',  row_num, None)                              # Failure rate — formula
            _write(idx, 'R',  row_num, pct)
            _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=True)
            _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=True)
            v = rd.get('U', '')
            _write(idx, 'U',  row_num, v if v not in ('', None) else None)
            _write(idx, 'V',  row_num, None)                              # Residual FIT — formula
            _write(idx, 'X',  row_num, rd.get('X', 'Y' if memo.startswith('X') else 'N'))
            _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=True)
            _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=True)
            v = rd.get('AA', '')
            _write(idx, 'AA', row_num, v if v not in ('', None) else None)
            _write(idx, 'AB', row_num, None)                              # Latent FIT — formula
            _write(idx, 'AD', row_num, rd.get('AD') or None,     wrap=True)

            fm += 1

        print(f"  [Agent 3] [{bi+1}/{len(fmeda_data)}] {code}: "
              f"{min(n_d, n_t)} rows → FM_TTL_{fm - min(n_d, n_t)} – FM_TTL_{fm-1}")

    wb.save(OUTPUT_FILE)
    print(f"\n  [Agent 3] Saved  → {OUTPUT_FILE}")
    print(f"  [Agent 3] Total failure modes: {fm - 1}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    print("╔═══════════════════════════════════════════════╗")
    print("║      FMEDA Multi-Agent Pipeline               ║")
    print("╚═══════════════════════════════════════════════╝")
    print(f"\n  Dataset  : {DATASET_FILE}")
    print(f"  IEC table: {IEC_TABLE_FILE}")
    print(f"  Template : {TEMPLATE_FILE}")
    print(f"  Model    : {OLLAMA_MODEL}")
    print(f"  Output   : {OUTPUT_FILE}\n")

    cache = load_cache()

    print("━━━ Step 0: Reading inputs ━━━")
    blk_blocks, sm_blocks = read_dataset()
    iec_table             = read_iec_table()
    print(f"  BLK: {len(blk_blocks)}   SM: {len(sm_blocks)}   IEC parts: {len(iec_table)}")

    print("\n━━━ Agent 1: Block → IEC part mapper (LLM) ━━━")
    blocks = agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache)
    print("\n  Result:")
    for b in blocks:
        tag = " [DUP]" if b.get('is_duplicate') else (" [SM]" if b.get('is_sm') else "")
        print(f"    {b['name']:<35} → {b['fmeda_code']:<12} | {b.get('iec_part','')}{tag}")
        if not b.get('is_duplicate') and not b.get('is_sm'):
            print(f"      modes ({len(b.get('modes',[]))}): {[m[:40] for m in b.get('modes',[])[:2]]} ...")

    print("\n━━━ Agent 2: IC Effects generator (LLM) ━━━")
    fmeda_data = agent2_generate_effects(blocks, cache)

    with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
        json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

    print("\n━━━ Agent 3: Template writer (deterministic) ━━━")
    agent3_write_template(fmeda_data)

    print("\n✅  Done!")
    print(f"    Output file  : {OUTPUT_FILE}")
    print(f"    Intermediate : {INTERMEDIATE_JSON}")
    print(f"    Cache file   : {CACHE_FILE}")


if __name__ == '__main__':
    run()
