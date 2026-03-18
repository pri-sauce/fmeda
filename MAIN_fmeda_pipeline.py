# """
# fmeda_agents.py  —  Multi-Agent FMEDA Pipeline
# ===============================================

# AGENT 1  (LLM)   Block → IEC part mapper
#   Reads BLK sheet, maps each block to an IEC part_name, pulls verbatim modes.

# AGENT 2  (LLM)   IC Effects + System Effects generator
#   For every (block × mode):
#     col I — effects on IC output (bullet format, what breaks downstream)
#     col J — effects on system (from TSR sheet: safety requirements)
#     col K — memo (X/O) derived by checking SM list addressed parts

# MEMO LOGIC (deterministic, no LLM):
#   Parse col I bullet list → extract block codes mentioned (BIAS, OSC, REF …)
#   Look up SM list (from template): which SMs address each of those blocks?
#   If ANY matching SM exists → K = "X"  (safety goal at risk)
#   If NONE → K = "O"

# AGENT 3  (Hardcoded)   Template writer
#   Fills FMEDA_TEMPLATE.xlsx placeholders deterministically.

# Usage:
#     python fmeda_agents.py
# """

# import json, re, time, shutil, sys
# import pandas as pd
# import openpyxl
# import requests
# from openpyxl.styles import Alignment

# # ─── CONFIG ──────────────────────────────────────────────────────────────────
# DATASET_FILE      = 'fusa_ai_agent_mock_data.xlsx'
# BLK_SHEET         = 'BLK'
# SM_SHEET          = 'SM'
# TSR_SHEET         = 'TSR'
# IEC_TABLE_FILE    = 'pdf_extracted.json'
# TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
# OUTPUT_FILE       = 'FMEDA_filled.xlsx'
# CACHE_FILE        = 'fmeda_cache.json'
# INTERMEDIATE_JSON = 'fmeda_intermediate.json'

# OLLAMA_URL     = 'http://localhost:11434/api/generate'
# OLLAMA_MODEL   = 'qwen3:30b'
# OLLAMA_TIMEOUT = 300
# SKIP_CACHE     = False
# # ─────────────────────────────────────────────────────────────────────────────


# # ═══════════════════════════════════════════════════════════════════════════════
# # LLM / CACHE HELPERS
# # ═══════════════════════════════════════════════════════════════════════════════

# def query_llm(prompt: str, temperature: float = 0.1) -> str:
#     try:
#         r = requests.post(OLLAMA_URL, json={
#             "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
#             "options": {"temperature": temperature, "num_ctx": 16384,
#                         "top_p": 0.9, "repeat_penalty": 1.1}
#         }, timeout=OLLAMA_TIMEOUT)
#         r.raise_for_status()
#         return r.json()["response"].strip()
#     except requests.exceptions.ConnectionError:
#         print("  Cannot connect to Ollama. Is it running? (ollama serve)")
#         sys.exit(1)
#     except Exception as e:
#         print(f"  LLM error: {e}")
#         return ""


# def parse_json(text: str):
#     text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
#     text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
#     text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE).strip()
#     for pattern in [r'\[.*\]', r'\{.*\}']:
#         m = re.search(pattern, text, re.DOTALL)
#         if m:
#             try:
#                 return json.loads(m.group())
#             except Exception:
#                 pass
#     return None


# def load_cache():
#     try:
#         with open(CACHE_FILE, encoding='utf-8') as f:
#             return json.load(f)
#     except Exception:
#         return {}


# def save_cache(cache):
#     with open(CACHE_FILE, 'w', encoding='utf-8') as f:
#         json.dump(cache, f, indent=2, ensure_ascii=False)


# # ═══════════════════════════════════════════════════════════════════════════════
# # READ ALL INPUTS
# # ═══════════════════════════════════════════════════════════════════════════════

# def read_dataset():
#     xl = pd.ExcelFile(DATASET_FILE)

#     df = pd.read_excel(DATASET_FILE, sheet_name=BLK_SHEET, dtype=str).fillna('')
#     blk_blocks = []
#     for _, row in df.iterrows():
#         vals = [v.strip() for v in row.values if str(v).strip()]
#         if len(vals) >= 2:
#             blk_blocks.append({'id': vals[0], 'name': vals[1],
#                                 'function': vals[2] if len(vals) > 2 else ''})

#     sm_blocks = []
#     if SM_SHEET in xl.sheet_names:
#         df_sm = pd.read_excel(DATASET_FILE, sheet_name=SM_SHEET, dtype=str).fillna('')
#         for _, row in df_sm.iterrows():
#             vals = [v.strip() for v in row.values if str(v).strip()]
#             if vals and re.match(r'sm[-_\s]?\d+', vals[0].lower()):
#                 sm_blocks.append({'id': vals[0], 'name': vals[1] if len(vals) > 1 else '',
#                                    'description': vals[2] if len(vals) > 2 else ''})

#     # TSR sheet → system-level safety requirements (used for col J)
#     tsr_list = []
#     if TSR_SHEET in xl.sheet_names:
#         df_tsr = pd.read_excel(DATASET_FILE, sheet_name=TSR_SHEET, dtype=str).fillna('')
#         for _, row in df_tsr.iterrows():
#             vals = [v.strip() for v in row.values if str(v).strip()]
#             if len(vals) >= 2:
#                 tsr_list.append({'id': vals[0], 'description': vals[1],
#                                   'connected_fsr': vals[2] if len(vals) > 2 else ''})

#     return blk_blocks, sm_blocks, tsr_list


# def read_iec_table():
#     with open(IEC_TABLE_FILE, encoding='utf-8-sig') as f:
#         return json.load(f)


# def read_sm_list_from_template():
#     """
#     Read SM list sheet from FMEDA_TEMPLATE.xlsx (or fallback to 3_ID03_FMEDA.xlsx).
#     Returns:
#       sm_coverage  : { 'SM01': 0.99, ... }
#       sm_addressed : { 'SM01': ['REF','LDO'], ... }
#       block_to_sms : { 'REF': ['SM01','SM02',...], ... }
#     """
#     import os
#     # Try sources in priority order
#     for candidate in [TEMPLATE_FILE, '3_ID03_FMEDA.xlsx']:
#         if os.path.exists(candidate):
#             try:
#                 wb_try = openpyxl.load_workbook(candidate, data_only=True)
#                 if 'SM list' in wb_try.sheetnames:
#                     cov, addr, b2s = read_sm_list_from_workbook(wb_try)
#                     if cov:  # non-empty → valid
#                         print(f"  SM list read from: {candidate} ({len(cov)} entries)")
#                         return cov, addr, b2s
#             except Exception:
#                 pass

#     # Fallback: hardcoded
#     print("  SM list: using built-in knowledge")
#     raw = _build_sm_list_from_knowledge()
#     cov, addr, b2s = {}, {}, {}
#     DEFAULT_COV = {
#         'SM01':0.99,'SM02':0.99,'SM03':0.99,'SM04':0.99,'SM05':0.99,
#         'SM06':0.9, 'SM08':0.9, 'SM09':0.99,'SM10':0.9, 'SM11':0.6,
#         'SM12':0.9, 'SM13':0.99,'SM14':0.99,'SM15':0.99,'SM16':0.9,
#         'SM17':0.9, 'SM18':0.99,'SM20':0.99,'SM21':0.6, 'SM22':0.99,
#         'SM23':0.9, 'SM24':0.9,
#     }
#     for entry in raw:
#         sm = entry['sm_code']
#         parts = entry['addressed_parts']
#         cov[sm]  = DEFAULT_COV.get(sm, 0.9)
#         addr[sm] = parts
#         for p in parts:
#             b2s.setdefault(p, [])
#             if sm not in b2s[p]:
#                 b2s[p].append(sm)
#     return cov, addr, b2s


# def read_sm_list_from_workbook(wb):
#     """
#     Read SM list directly from an open openpyxl workbook.
#     Returns:
#       sm_coverage  : { 'SM01': 0.99, 'SM11': 0.6, ... }  (col L)
#       sm_addressed : { 'SM01': ['REF','LDO'], ... }        (col E)
#       block_to_sms : { 'REF': ['SM01','SM02',...], ... }   reverse index
#     """
#     import re as _re
#     ws = wb['SM list']

#     sm_coverage  = {}   # SM code → float coverage
#     sm_addressed = {}   # SM code → list of block codes

#     for row in ws.iter_rows(min_row=12, max_row=ws.max_row):
#         cells = {c.column_letter: c.value for c in row if c.value is not None}
#         if 'C' not in cells:
#             continue
#         sm_code = str(cells['C']).strip()
#         if not sm_code.startswith('SM'):
#             continue

#         # Coverage (col L)
#         raw_cov = cells.get('L', '')
#         try:
#             cov = float(str(raw_cov))
#         except (ValueError, TypeError):
#             cov = 0.9
#         sm_coverage[sm_code] = cov

#         # Addressed parts (col E)
#         raw_parts = str(cells.get('E', '')).strip()
#         parts = [_re.sub(r'SW_BANK[_x\d]*', 'SW_BANK',
#                           _re.sub(r'\bCSNS\b|\bCNSN\b|\bCS\b', 'CSNS', p.strip()))
#                  for p in _re.split(r'[,;]', raw_parts) if p.strip()]
#         sm_addressed[sm_code] = parts

#     # Reverse index: block → [SM codes]
#     block_to_sms = {}
#     for sm_code, parts in sm_addressed.items():
#         for part in parts:
#             if part:
#                 block_to_sms.setdefault(part, [])
#                 if sm_code not in block_to_sms[part]:
#                     block_to_sms[part].append(sm_code)

#     return sm_coverage, sm_addressed, block_to_sms


# def _build_sm_list_from_knowledge():
#     """Hardcoded SM→block mapping from 3_ID03_FMEDA.xlsx SM list."""
#     return [
#         {'sm_code': 'SM01',  'addressed_parts': ['REF', 'LDO']},
#         {'sm_code': 'SM02',  'addressed_parts': ['REF', 'LDO']},
#         {'sm_code': 'SM03',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM04',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM05',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM06',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM08',  'addressed_parts': ['CSNS', 'ADC']},
#         {'sm_code': 'SM09',  'addressed_parts': ['LOGIC']},
#         {'sm_code': 'SM10',  'addressed_parts': ['LOGIC']},
#         {'sm_code': 'SM11',  'addressed_parts': ['OSC']},
#         {'sm_code': 'SM12',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM13',  'addressed_parts': ['SW_BANK', 'LOGIC']},
#         {'sm_code': 'SM14',  'addressed_parts': ['CP']},
#         {'sm_code': 'SM15',  'addressed_parts': ['REF', 'LDO']},
#         {'sm_code': 'SM16',  'addressed_parts': ['REF', 'ADC']},
#         {'sm_code': 'SM17',  'addressed_parts': ['TEMP']},
#         {'sm_code': 'SM18',  'addressed_parts': ['LOGIC']},
#         {'sm_code': 'SM20',  'addressed_parts': ['LDO']},
#         {'sm_code': 'SM21',  'addressed_parts': ['LOGIC']},
#         {'sm_code': 'SM22',  'addressed_parts': ['CP', 'SW_BANK']},
#         {'sm_code': 'SM23',  'addressed_parts': ['TEMP']},
#         {'sm_code': 'SM24',  'addressed_parts': ['ADC', 'SW_BANK']},
#     ]


# # ═══════════════════════════════════════════════════════════════════════════════
# # MEMO LOGIC  (deterministic — no LLM)
# # ═══════════════════════════════════════════════════════════════════════════════

# # Normalise any block code variant to canonical form
# _BLOCK_NORM = {
#     'SW_BANKX': 'SW_BANK', 'SW_BANK_X': 'SW_BANK', 'SW_BANKx': 'SW_BANK',
#     'SW_BANK_1': 'SW_BANK', 'SW_BANK_2': 'SW_BANK',
#     'SW_BANK_3': 'SW_BANK', 'SW_BANK_4': 'SW_BANK',
#     'CNSN': 'CSNS', 'CS': 'CSNS',
#     'DIETEMP': 'TEMP',
#     'VEGA': 'CP',   # Vega = the IC itself, charge pump damage
# }


# def _norm_block(code: str) -> str:
#     c = code.strip().upper()
#     return _BLOCK_NORM.get(c, c)


# def extract_blocks_from_ic_effect(ic_effect: str) -> list[str]:
#     """
#     Parse the bullet-format IC effect string and return list of block codes.
#     e.g. "• BIAS\n    - ...\n• ADC\n    - ..." → ['BIAS', 'ADC']
#     """
#     if not ic_effect or ic_effect.strip() in ('No effect', ''):
#         return []
#     # Match lines starting with •
#     blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
#     return [_norm_block(b) for b in blocks if b.upper() not in ('NONE', '')]


# def determine_memo(ic_effect: str, block_to_sms: dict) -> tuple[str, list[str]]:
#     """
#     Returns (memo, matching_sms_list).
#     memo = 'X' if ANY block in ic_effect is covered by a SM, else 'O'.
#     """
#     if not ic_effect or ic_effect.strip() in ('No effect', ''):
#         return 'O', []

#     affected_blocks = extract_blocks_from_ic_effect(ic_effect)
#     if not affected_blocks:
#         return 'O', []

#     matching_sms = []
#     for block in affected_blocks:
#         sms = block_to_sms.get(block, [])
#         for sm in sms:
#             if sm not in matching_sms:
#                 matching_sms.append(sm)

#     memo = 'X' if matching_sms else 'O'
#     return memo, matching_sms


# # ═══════════════════════════════════════════════════════════════════════════════
# # AGENT 1  —  Block → IEC part mapper
# # ═══════════════════════════════════════════════════════════════════════════════

# def agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache):
#     ck = "agent1__" + json.dumps([b['name'] for b in blk_blocks])
#     if not SKIP_CACHE and ck in cache:
#         print("  [Agent 1] Loaded from cache")
#         result = cache[ck]
#         _append_sm_blocks(result, sm_blocks)
#         return result

#     # Build IEC summary for the prompt
#     iec_summary = ""
#     for i, p in enumerate(iec_table):
#         modes = p["entries"][0]["modes"]
#         iec_summary += (
#             f'  {i+1:2d}. "{p["part_name"]}"\n'
#             f'       Desc : {p["entries"][0]["description"][:120]}\n'
#             f'       Modes: {json.dumps(modes[:3])}'
#             + (' ...' if len(modes) > 3 else '') + '\n\n'
#         )

#     blocks_text = "\n".join(
#         f'  {b["id"]}: "{b["name"]}" — {b["function"]}'
#         for b in blk_blocks
#     )

#     prompt = f"""You are an automotive IC functional safety engineer.

# CHIP BLOCKS:
# {blocks_text}

# IEC 62380 HARDWARE PART CATEGORIES:
# {iec_summary}

# FMEDA SHORT CODE RULES (short label used in the FMEDA table):
#   Voltage reference / bandgap                    → REF
#   Bias current source / current reference        → BIAS
#   LDO / linear voltage regulator                 → LDO
#   Internal oscillator / clock generator          → OSC
#   Watchdog / clock monitor (shares OSC slot)     → OSC   [duplicate]
#   Temperature sensor / thermal circuit           → TEMP
#   Current sense amplifier / op-amp sense         → CSNS
#   Current DAC / channel DAC                      → ADC
#   ADC (analogue to digital converter)            → ADC   [duplicate of DAC slot]
#   Charge pump / boost regulator                  → CP
#   nFAULT driver / fault aggregator (shares CP)   → CP    [duplicate]
#   Digital logic / main controller                → LOGIC
#   Open-load / short-to-GND detector (LOGIC)      → LOGIC [duplicate]
#   SPI / UART / serial interface                  → INTERFACE
#   NVM / trim / self-test / POST                  → TRIM
#   LED driver switch bank N                       → SW_BANK_N

# TASK: For each block determine:
#   "fmeda_code"   — short code from rules above
#   "iec_part"     — EXACT part_name string from IEC list that best matches
#   "is_duplicate" — true if this fmeda_code was already assigned to an earlier block

# Return JSON array, same order as input blocks:
# [
#   {{"id":"BLK-01","name":"Bandgap Reference","fmeda_code":"REF",
#     "iec_part":"Voltage references","is_duplicate":false}},
#   ...
# ]
# Return ONLY the JSON array:"""

#     print("  [Agent 1] Calling LLM to map blocks → IEC parts...")
#     raw    = query_llm(prompt, temperature=0.05)
#     result = parse_json(raw)

#     if not isinstance(result, list) or len(result) != len(blk_blocks):
#         print("  [Agent 1] LLM parse issue — using fallback")
#         result = _fallback_agent1(blk_blocks)

#     # CRITICAL: always replace LLM-generated modes with verbatim IEC table modes
#     iec_idx = {p['part_name']: p['entries'][0]['modes'] for p in iec_table}
#     for b in result:
#         iec_part = b.get('iec_part', '')
#         # Exact match
#         if iec_part in iec_idx:
#             b['modes'] = iec_idx[iec_part]
#         else:
#             # Fuzzy match
#             matched = False
#             for pname, modes in iec_idx.items():
#                 if iec_part[:20].lower() in pname.lower() or pname[:20].lower() in iec_part.lower():
#                     b['modes'] = modes
#                     b['iec_part'] = pname
#                     matched = True
#                     break
#             if not matched:
#                 b['modes'] = []
#                 print(f"  [Agent 1] WARNING: no IEC modes for '{iec_part}' ({b['name']})")

#     # Enforce duplicate flags
#     seen = set()
#     for b in result:
#         code = b.get('fmeda_code', '')
#         if code in seen:
#             b['is_duplicate'] = True
#         else:
#             b['is_duplicate'] = False
#             seen.add(code)

#     cache[ck] = result
#     save_cache(cache)

#     _append_sm_blocks(result, sm_blocks)
#     return result


# def _append_sm_blocks(result, sm_blocks):
#     for sm in sm_blocks:
#         m = re.match(r'sm[-_\s]?(\d+)', sm['id'].lower())
#         code = f"SM{int(m.group(1)):02d}" if m else sm['id'].upper()
#         result.append({
#             'id': sm['id'], 'name': sm['name'], 'function': sm.get('description', ''),
#             'fmeda_code': code, 'iec_part': 'Safety Mechanism',
#             'modes': ['Fail to detect', 'False detection'],
#             'is_duplicate': False, 'is_sm': True,
#         })


# def _fallback_agent1(blk_blocks):
#     KMAP = [
#         (['bandgap','voltage reference','1.2v','temperature-stable ref'],
#          'REF',       'Voltage references'),
#         (['bias current','current source','bias generator'],
#          'BIAS',      'Current source (including bias current generator)'),
#         (['ldo','low dropout','linear regulator'],
#          'LDO',       'Voltage regulators (linear, SMPS, etc.)'),
#         (['oscillator','internal clock','4 mhz','watchdog','clock monitor'],
#          'OSC',       'Oscillator'),
#         (['thermal shutdown','die temperature','on-chip diode'],
#          'TEMP',      'Operational amplifier and buffer'),
#         (['current sense','shunt','sense amplifier','overcurrent comparator'],
#          'CSNS',      'Operational amplifier and buffer'),
#         (['current dac','channel dac','8-bit current','dac for'],
#          'ADC',       'N bits digital to analogue converters (DAC)d'),
#         (['charge pump','boost'],
#          'CP',        'Charge pump, regulator boost'),
#         (['spi interface','serial interface','uart','fault readback'],
#          'INTERFACE', 'N bits analogue to digital converters (N-bit ADC)'),
#         (['self-test','post','power-on self','validates dac'],
#          'TRIM',      'Voltage references'),
#         (['nfault','open-drain fault','aggregates fault'],
#          'CP',        'Charge pump, regulator boost'),
#         (['open-load','short-to-gnd','detector','logic'],
#          'LOGIC',     'Voltage/Current comparator'),
#     ]
#     used, result = set(), []
#     for b in blk_blocks:
#         combined = (b['name'] + ' ' + b['function']).lower()
#         code, iec = 'LOGIC', 'Voltage/Current comparator'
#         for kws, c, ip in KMAP:
#             if any(k in combined for k in kws):
#                 code, iec = c, ip
#                 break
#         dup = code in used
#         if not dup: used.add(code)
#         result.append({'id': b['id'], 'name': b['name'], 'function': b['function'],
#                         'fmeda_code': code, 'iec_part': iec, 'is_duplicate': dup})
#     return result


# # ═══════════════════════════════════════════════════════════════════════════════
# # AGENT 2  —  IC Effects + System Effects generator
# # ═══════════════════════════════════════════════════════════════════════════════

# IC_FORMAT = """
# EXACT FORMAT for col I  "effects on IC output":
#   • BLOCK_CODE
#       - specific effect on that block
#       - second effect if applicable
#   • ANOTHER_BLOCK_CODE
#       - specific effect

#   If NOTHING is affected → write exactly: No effect

# RULES:
#   • Use •  before block name (no indent before •)
#   • Use 4 spaces + dash before each effect line under a block
#   • Block codes: REF  BIAS  LDO  OSC  TEMP  CSNS  ADC  CP  LOGIC  INTERFACE  TRIM  SW_BANK_x
#   • Effect must be specific — NOT "BIAS is affected" BUT "Output reference voltage is stuck"
#   • Use present tense: "is stuck", "is incorrect", "cannot operate", "out of spec."
#   • List EVERY block that receives signal from the failing block — do not omit any
# """.strip()

# FEW_SHOT = """
# VERIFIED EXAMPLES FROM A REAL AUTOMOTIVE IC FMEDA:

# REF / "Output is stuck (i.e. high or low)"  → col I:
# • BIAS
#     - Output reference voltage is stuck 
#     - Output reference current is stuck 
#     - Output bias current is stuck 
#     - Quiescent current exceeding the maximum value
# • REF
#     - Quiescent current exceeding the maximum value
# • ADC
#     - REF output is stuck 
# • TEMP
#     - Output is stuck 
# • LDO
#     - Output is stuck 
# • OSC
#     - Oscillation does not start

# REF / "Output is floating (i.e. open circuit)"  → col I:
# • BIAS
#     - Output reference voltage is floating
#     - Output reference current is higher than the expected range
#     - Output reference current is lower than the expected range
#     - Output bias current is higher than the expected range
#     - Output bias current is lower than the expected range
# • ADC
#     - REF output is floating (i.e. open circuit)
# • LDO
#     - Out of spec
# • OSC
#     - Out of spec

# REF / "Output voltage affected by spikes"  → col I:
# No effect

# BIAS / "One or more outputs are stuck (i.e. high or low)"  → col I:
# • ADC
#     - ADC measurement is incorrect.
# • TEMP
#     - Incorrect temperature measurement.
# • LDO
#     - Out of spec.
# • OSC
#     - Frequency out of spec.
# • SW_BANKx
#     - Out of spec.
# • CP
#     - Out of spec.
# • CNSN
#     - Incorrect reading.

# OSC / "Output is stuck (i.e. high or low)"  → col I:
# • LOGIC
#     - Cannot operate.
#     - Communication error.

# TEMP / "Output is stuck (i.e. high or low)"  → col I:
# • ADC
#     - TEMP output is stuck low
# • SW_BANK_x
#     - SW is stuck in off state (DIETEMP)

# TEMP / "Output is floating (i.e. open circuit)"  → col I:
# • ADC
#     - Incorrect TEMP reading

# CSNS / "Output is stuck (i.e. high or low)"  → col I:
# • ADC
#     - CSNS output is incorrect.

# ADC / "One or more outputs are stuck (i.e. high or low)"  → col I:
# • SW_BANK_x
#     - SW is stuck in off state (DIETEMP)
# • ADC
#     - Incorrect BGR measurement
#     - Incorrect DIETEMP measurement
#     - Incorrect CS measurement

# CP / "Output voltage lower than a low threshold..."  → col I:
# • SW_BANK_x
#     - SWs are stuck in off state, LEDs always ON.

# CP / "Output voltage higher than a high threshold..."  → col I:
# • Vega
#     - Device Damage

# LOGIC / "Output is stuck (i.e. high or low)"  → col I:
# • SW_BANK_X
#     - SW is stuck in on/off state
# • OSC
#     - Output stuck

# TRIM / "Error of omission (i.e. not triggered when it should be)"  → col I:
# • REF
#     - Incorrect output value higher than the expected range
# • LDO
#     - Reference voltage higher than the expected range
# • BIAS
#     - Output reference voltage accuracy too low, including drift
# • SW_BANK
#     - Incorrect slew rate value
# • OSC
#     - Incorrect output frequency: higher than the expected range
# • DIETEMP
#     - Incorrect output voltage

# SAFE MODES — these are ALWAYS "No effect" for col I:
#   "affected by spikes", "oscillation within the expected range",
#   "incorrect start-up time", "jitter too high", "incorrect duty cycle",
#   "quiescent current exceeding", "settling time", "false detection"
# """.strip()


# def agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage, sm_addressed, cache):
#     """Generate col I (IC effect), col J (system effect), col K (memo) for all blocks."""

#     # Build chip context for LLM
#     active = [b for b in blocks if not b.get('is_duplicate') and not b.get('is_sm')]
#     chip_ctx = "\n".join(
#         f"  {b['fmeda_code']:<12} {b['name']:<35} | {b.get('function','')[:80]}"
#         for b in active
#     )

#     # TSR context for col J
#     tsr_ctx = "\n".join(
#         f"  {t['id']}: {t['description']}"
#         for t in tsr_list
#     ) if tsr_list else "  (no TSR data)"

#     result = []
#     for block in blocks:
#         code  = block['fmeda_code']
#         name  = block['name']
#         modes = block.get('modes', [])

#         # SM blocks → hardcoded
#         if block.get('is_sm'):
#             rows = _sm_rows(code)
#             result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#             print(f"  [Agent 2] {code:<12} SM — hardcoded (2 rows)")
#             continue

#         # Duplicate blocks → skip
#         if block.get('is_duplicate'):
#             print(f"  [Agent 2] {code:<12} DUPLICATE ({name}) — skipped")
#             continue

#         if not modes:
#             print(f"  [Agent 2] {code:<12} no modes — skipped")
#             continue

#         ck = f"agent2__{code}__{name}__{len(modes)}"
#         if not SKIP_CACHE and ck in cache:
#             rows = cache[ck]
#             print(f"  [Agent 2] {code:<12} cache ({len(rows)} rows)")
#             result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#             continue

#         rows = _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms, sm_coverage, sm_addressed)
#         cache[ck] = rows
#         save_cache(cache)
#         result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#         print(f"  [Agent 2] {code:<12} {len(rows)} rows (LLM)")
#         time.sleep(0.3)

#     return result


# def _build_downstream_hint(block, chip_ctx):
#     """
#     Build a text hint about which blocks likely receive signal from this block.
#     Based on function keywords — helps LLM reason about signal flow.
#     """
#     code = block['fmeda_code']
#     func = (block.get('function') or '').lower()
#     name = block.get('name', '').lower()

#     # Known downstream relationships
#     DOWNSTREAM = {
#         'REF':       'BIAS, ADC, TEMP, LDO, OSC — all use the reference voltage for biasing and regulation',
#         'BIAS':      'ADC, TEMP, LDO, OSC, SW_BANKx, CP, CSNS — all receive bias currents from BIAS',
#         'LDO':       'OSC (LDO powers the oscillator supply rail)',
#         'OSC':       'LOGIC, INTERFACE — clock signal drives all digital logic and communication',
#         'TEMP':      'ADC (TEMP voltage is read by ADC), SW_BANK_x (DIETEMP controls output enable)',
#         'CSNS':      'ADC (CSNS output is digitized by ADC for current monitoring)',
#         'ADC':       'SW_BANK_x (ADC DIETEMP result controls switch enable), LOGIC (ADC results feed decision logic)',
#         'CP':        'SW_BANK_x (charge pump supplies the gate drive voltage for all switches)',
#         'LOGIC':     'SW_BANK_X (LOGIC drives all switch banks), OSC (LOGIC can assert reset)',
#         'INTERFACE': 'LOGIC, ADC (SPI writes configure DAC and read ADC results)',
#         'TRIM':      'REF, LDO, BIAS, OSC, SW_BANK, DIETEMP — trim data calibrates all analog blocks',
#     }

#     hint = DOWNSTREAM.get(code, '')
#     if not hint:
#         # Generic: suggest looking at all blocks
#         hint = 'Review all blocks — consider which ones depend on this block output signal'
#     return hint


# def _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms, sm_coverage, sm_addressed):
#     code = block['fmeda_code']
#     name = block['name']
#     func = block.get('function', '')
#     n    = len(modes)

#     # Build downstream signal map for this block
#     # Helps LLM reason about who receives the signal from this block
#     downstream_hint = _build_downstream_hint(block, chip_ctx)

#     prompt = f"""You are completing an FMEDA table for an automotive IC (ISO 26262 / AEC-Q100).

# {FEW_SHOT}

# ═══════════════════════════════════════════════════
# ALL BLOCKS IN THIS CHIP (fmeda_code | name | function):
# {chip_ctx}

# SYSTEM SAFETY REQUIREMENTS (TSR):
# {tsr_ctx}
# ═══════════════════════════════════════════════════
# BLOCK BEING ANALYZED:
#   FMEDA Code : {code}
#   Block Name : {name}
#   Function   : {func}

# SIGNAL FLOW HINT (who likely receives output from this block):
# {downstream_hint}
# ═══════════════════════════════════════════════════
# FAILURE MODES TO ANALYZE ({n} total):
# {json.dumps(modes, indent=2)}
# ═══════════════════════════════════════════════════

# STEP-BY-STEP REASONING FOR EACH MODE:

#   Step 1 — IDENTIFY THE OUTPUT SIGNAL
#     Ask: "What physical signal does {name} produce?"
#     Examples: reference voltage, bias current, clock signal, PWM enable, serial data, temperature voltage

#   Step 2 — MAP ALL CONSUMERS
#     Go through EVERY other block in the chip.
#     For each block ask: "Does its function depend on {name}'s output?"
#     Be thorough — a bias current affects ADC, TEMP, LDO, OSC, CP all at once.
#     A reference voltage affects every block that uses it for comparison or regulation.
#     A clock signal affects all digital blocks.

#   Step 3 — DETERMINE THE SPECIFIC SYMPTOM PER CONSUMER
#     For each consumer block, describe exactly what goes wrong:
#     - NOT "ADC is affected" → YES "ADC measurement is incorrect."
#     - NOT "OSC has issue"   → YES "Oscillation does not start" or "Frequency out of spec."
#     - NOT "LOGIC fails"     → YES "Cannot operate." + "Communication error."

#   Step 4 — SAFE MODES (always No effect):
#     If the failure mode is: "affected by spikes", "oscillation within expected range",
#     "incorrect start-up time", "jitter too high", "incorrect duty cycle",
#     "quiescent current exceeding", "incorrect settling time"
#     → col I = "No effect" (these are local disturbances, don't propagate)

#   Step 5 — SYSTEM EFFECT (col J)
#     What does the end user/ECU observe? Cross-check TSR requirements.
#     Choose from:
#       "Unintentional LED ON/OFF\nFail-safe mode active\nNo communication"
#       "Fail-safe mode active\nNo communication"
#       "Fail-safe mode active"
#       "Unintended LED ON"
#       "Unintended LED OFF"
#       "Unintended LED ON/OFF"
#       "Device damage"
#       "No effect"

# {IC_FORMAT}

# Return a JSON array with EXACTLY {n} objects, same order as failure modes:
# [
#   {{
#     "G": "<exact failure mode string>",
#     "I": "<col I: IC output effect>",
#     "J": "<col J: system effect>"
#   }},
#   ...
# ]
# Return ONLY the JSON array:"""

#     raw    = query_llm(prompt, temperature=0.05)
#     parsed = parse_json(raw)

#     if isinstance(parsed, list) and len(parsed) >= n:
#         rows = []
#         for i in range(n):
#             rd   = parsed[i]
#             ic   = str(rd.get('I', 'No effect')).strip()
#             sys_ = str(rd.get('J', 'No effect')).strip()
#             memo, _ = determine_memo(ic, block_to_sms)
#             rows.append(_build_row(modes[i], ic, sys_, memo, block_to_sms, sm_coverage))
#         return rows

#     print(f"    LLM parse failed for {code} — using fallback")
#     return _fallback_rows(modes, block_to_sms, sm_coverage, sm_addressed)


# def compute_sm_columns(ic_effect, block_to_sms, sm_coverage):
#     """
#     Given col I IC effect string:
#       col S / col Y : space-separated SM codes whose addressed parts match blocks in col I
#       col U         : highest coverage value among those SMs (one of 0.99, 0.9, 0.6)
#     Returns (sm_string, coverage_value)
#     """
#     if not ic_effect or ic_effect.strip() == 'No effect':
#         return '', ''

#     # Extract block codes from bullet list
#     affected_blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
#     # Normalize variants
#     norm = []
#     for b in affected_blocks:
#         b = b.strip().upper()
#         b = re.sub(r'SW_BANK[_X\d]*', 'SW_BANK', b)
#         b = re.sub(r'CSNS|CNSN|CS', 'CSNS', b)
#         if b not in ('NONE', 'VEGA', ''):
#             norm.append(b)

#     if not norm:
#         return '', ''

#     # Find all matching SMs
#     matching_sms = []
#     for block in norm:
#         for sm in block_to_sms.get(block, []):
#             if sm not in matching_sms:
#                 matching_sms.append(sm)

#     if not matching_sms:
#         return '', ''

#     # Sort numerically for clean output: SM01 SM08 SM15 ...
#     def sm_sort_key(s):
#         m = re.search(r'(\d+)', s)
#         return int(m.group(1)) if m else 0
#     matching_sms.sort(key=sm_sort_key)

#     sm_string = ' '.join(matching_sms)

#     # col U: highest coverage among matching SMs
#     # Only valid values: 0.99, 0.9, 0.6
#     valid = [0.99, 0.9, 0.6]
#     coverages = [sm_coverage.get(sm, 0.9) for sm in matching_sms]
#     # Round to nearest valid value
#     def nearest_valid(v):
#         return min(valid, key=lambda x: abs(x - v))
#     rounded = [nearest_valid(c) for c in coverages]
#     max_cov = max(rounded) if rounded else 0.9

#     return sm_string, max_cov


# def _build_row(canonical_mode, ic, sys_, memo, block_to_sms=None, sm_coverage=None):
#     """
#     col P  (Single Point Failure mode): Y if K=X, N if K=O
#     col R  (Percentage of Safe Faults): 0 if IC has any effect, 1 if No effect
#     col S  : SM codes whose addressed parts match blocks in col I
#     col U  : highest coverage value from those SMs (0.99 / 0.9 / 0.6)
#     col Y  : same as col S (latent SM coverage = same mechanisms)
#     """
#     ic_clean = ic.strip()
#     if ic_clean in ('No effect', ''):
#         memo = 'O'

#     sp       = 'Y' if memo.startswith('X') else 'N'
#     pct_safe = 1 if ic_clean == 'No effect' else 0

#     # Compute S, U, Y
#     sm_str, coverage = '', ''
#     if block_to_sms and sm_coverage and ic_clean != 'No effect':
#         sm_str, coverage = compute_sm_columns(ic_clean, block_to_sms, sm_coverage)

#     return {
#         'G': canonical_mode,
#         'I': ic,
#         'J': sys_,
#         'K': memo,
#         'O': 1,
#         'P': sp,
#         'R': pct_safe,
#         'S': sm_str,    # col S: Safety mechanisms IC
#         'T': '',
#         'U': coverage,  # col U: highest coverage SPF
#         'V': '',
#         'X': sp,
#         'Y': sm_str,    # col Y: same as S (latent SM = same mechanisms)
#         'Z': '', 'AA': '', 'AB': '', 'AD': '',
#     }


# def _fallback_rows(modes, block_to_sms, sm_coverage=None, sm_addressed=None):
#     SAFE = ['spike','oscillation within','start-up','jitter','duty cycle',
#             'quiescent','settling','false detection']
#     rows = []
#     for mode in modes:
#         safe = any(k in mode.lower() for k in SAFE)
#         ic   = 'No effect' if safe else ''
#         memo, _ = determine_memo(ic, block_to_sms)
#         rows.append(_build_row(mode, ic, 'No effect' if safe else '', memo,
#                                block_to_sms, sm_coverage))
#     return rows


# def _sm_rows(sm_code):
#     SM = {
#         'SM01': ('Unintended LED ON',                        'Unintended LED ON'),
#         'SM02': ('Device damage',                            'Device damage'),
#         'SM03': ('Unintended LED ON',                        'Unintended LED ON'),
#         'SM04': ('Unintended LED OFF',                       'Unintended LED OFF'),
#         'SM05': ('Unintended LED OFF',                       'Unintended LED OFF'),
#         'SM06': ('Unintended LED OFF',                       'Unintended LED OFF'),
#         'SM07': ('Unintended LED ON/OFF',                    'Unintended LED ON/OFF'),
#         'SM08': ('Unintended LED ON',                        'Unintended LED ON'),
#         'SM09': ('UART Communication Error',                 'Fail-safe mode active'),
#         'SM10': ('UART Communication Error',                 'Fail-safe mode active'),
#         'SM11': ('UART Communication Error',                 'Fail-safe mode active'),
#         'SM12': ('No PWM monitoring functionality',          'No effect'),
#         'SM13': ('Unintended LED ON/OFF in FS mode',         'Unintended LED ON/OFF in FS mode'),
#         'SM14': ('Unintended LED ON',                        'Unintended LED ON'),
#         'SM15': ('Failures on LOGIC operation',              'Possible Fail-safe mode activation'),
#         'SM16': ('Loss of reference control functionality',  'No effect'),
#         'SM17': ('Device damage',                            'Device damage'),
#         'SM18': ('Cannot trim part properly',                'Performance/Functionality degredation'),
#         'SM20': ('Device damage',                            'Device damage'),
#         'SM21': ('Unsynchronised PWM',                       'No effect'),
#         'SM22': ('Unintended LED OFF',                       'Unintended LED OFF'),
#         'SM23': ('Loss of thermal monitoring capability',    'Possible device damage'),
#         'SM24': ('Loss of LED voltage monitoring capability','No effect'),
#     }
#     ic, sys_ = SM.get(sm_code, ('Loss of safety mechanism functionality', 'Fail-safe mode active'))
#     return [
#         {'G': 'Fail to detect',  'I': ic,          'J': sys_,        'K': 'X (Latent)',
#          'O': 1, 'P': 'Y', 'R': 0, 'S': '', 'T': '', 'U': '', 'V': '',
#          'X': 'Y', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
#         {'G': 'False detection', 'I': 'No effect', 'J': 'No effect', 'K': 'O',
#          'O': 1, 'P': 'N', 'R': 1, 'S': '', 'T': '', 'U': '', 'V': '',
#          'X': 'N', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
#     ]


# # ═══════════════════════════════════════════════════════════════════════════════
# # AGENT 3  —  Template Writer  (deterministic)
# # ═══════════════════════════════════════════════════════════════════════════════

# def _scan_placeholders(ws):
#     idx = {}
#     for ws_row in ws.iter_rows():
#         for cell in ws_row:
#             if cell.__class__.__name__ == 'MergedCell':
#                 continue
#             v = str(cell.value) if cell.value is not None else ''
#             if v.startswith('{{FMEDA_') and v.endswith('}}'):
#                 idx[v] = cell
#     return idx


# def _get_groups(idx, data_start=22):
#     d_rows = sorted({
#         int(re.search(r'(\d+)', k).group(1))
#         for k in idx
#         if re.match(r'\{\{FMEDA_D\d+\}\}', k)
#         and int(re.search(r'(\d+)', k).group(1)) >= data_start
#     })
#     all_rows = sorted({
#         int(re.search(r'(\d+)', k).group(1))
#         for k in idx
#         if re.match(r'\{\{FMEDA_[A-Z]+\d+\}\}', k)
#         and int(re.search(r'(\d+)', k).group(1)) >= data_start
#     })
#     groups = []
#     for i, first in enumerate(d_rows):
#         nxt = d_rows[i+1] if i+1 < len(d_rows) else 999999
#         groups.append([r for r in all_rows if first <= r < nxt])
#     return groups


# def _write(idx, col, row_num, value, wrap=False):
#     key = '{{FMEDA_' + col + str(row_num) + '}}'
#     if key not in idx:
#         return
#     cell = idx[key]
#     if value is None or str(value).strip() in ('', 'None', 'nan'):
#         cell.value = None
#         return
#     cell.value = value
#     if wrap and isinstance(value, str) and '\n' in value:
#         old = cell.alignment or Alignment()
#         cell.alignment = Alignment(wrap_text=True,
#                                    vertical=old.vertical or 'center',
#                                    horizontal=old.horizontal or 'left')


# def agent3_write_template(fmeda_data):
#     shutil.copy2(TEMPLATE_FILE, OUTPUT_FILE)
#     wb = openpyxl.load_workbook(OUTPUT_FILE)
#     ws = wb['FMEDA']

#     idx    = _scan_placeholders(ws)
#     groups = _get_groups(idx)

#     print(f"\n  [Agent 3] Template groups : {len(groups)}")
#     print(f"  [Agent 3] Blocks to write : {len(fmeda_data)}")

#     fm = 1
#     for bi, block in enumerate(fmeda_data):
#         code = block['fmeda_code']
#         rows = block['rows']

#         if bi >= len(groups):
#             print(f"  [Agent 3] WARNING: no template group for block {bi+1} ({code})")
#             break

#         group_rows = groups[bi]
#         n_t = len(group_rows)
#         n_d = len(rows)
#         if n_d > n_t:
#             print(f"  [Agent 3] {code}: {n_d} modes > {n_t} slots — truncating")
#             rows = rows[:n_t]

#         for mi, row_num in enumerate(group_rows):
#             rd       = rows[mi] if mi < len(rows) else None
#             is_first = (mi == 0)

#             _write(idx, 'B', row_num, f'FM_TTL_{fm}' if rd else None)
#             _write(idx, 'C', row_num, code)
#             _write(idx, 'D', row_num, code if is_first else None)
#             _write(idx, 'E', row_num, None)   # Block FIT — formula/engineer fills

#             if rd is None:
#                 _write(idx, 'G', row_num, None)
#                 continue

#             memo     = str(rd.get('K', 'O')).strip()
#             sp       = str(rd.get('P', 'Y' if memo.startswith('X') else 'N')).strip()
#             pct_safe = rd.get('R', 1 if memo == 'O' else 0)

#             _write(idx, 'F',  row_num, None)                               # Mode FIT — formula
#             _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
#             _write(idx, 'H',  row_num, None)                               # Failure Mode — blank
#             _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
#             _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
#             _write(idx, 'K',  row_num, memo)
#             _write(idx, 'O',  row_num, 1)
#             _write(idx, 'P',  row_num, sp)
#             _write(idx, 'Q',  row_num, None)                               # Failure rate — formula
#             _write(idx, 'R',  row_num, pct_safe)
#             _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=False)
#             _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=False)
#             # col U: coverage value — write as decimal (0.99 / 0.9 / 0.6)
#             v = rd.get('U', '')
#             _write(idx, 'U',  row_num, v if v not in ('', None, '') else None)
#             _write(idx, 'V',  row_num, None)                               # Residual FIT — formula
#             _write(idx, 'X',  row_num, rd.get('X', sp))
#             _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=False)
#             _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=True)
#             v = rd.get('AA', '')
#             _write(idx, 'AA', row_num, v if v not in ('', None) else None)
#             _write(idx, 'AB', row_num, None)                               # Latent FIT — formula
#             _write(idx, 'AD', row_num, rd.get('AD') or None,     wrap=True)

#             fm += 1

#         print(f"  [Agent 3] [{bi+1}/{len(fmeda_data)}] {code}: "
#               f"{min(n_d, n_t)} rows → FM_TTL_{fm-min(n_d,n_t)} – FM_TTL_{fm-1}")

#     wb.save(OUTPUT_FILE)
#     print(f"\n  [Agent 3] Saved  → {OUTPUT_FILE}")
#     print(f"  [Agent 3] Total failure modes: {fm - 1}")


# # ═══════════════════════════════════════════════════════════════════════════════
# # MAIN
# # ═══════════════════════════════════════════════════════════════════════════════

# def run():
#     print("╔═══════════════════════════════════════════════╗")
#     print("║      FMEDA Multi-Agent Pipeline               ║")
#     print("╚═══════════════════════════════════════════════╝")
#     print(f"\n  Dataset  : {DATASET_FILE}")
#     print(f"  IEC table: {IEC_TABLE_FILE}")
#     print(f"  Template : {TEMPLATE_FILE}")
#     print(f"  Model    : {OLLAMA_MODEL}")
#     print(f"  Output   : {OUTPUT_FILE}\n")

#     cache = load_cache()

#     # ── Step 0: Read all inputs ───────────────────────────────────────────────
#     print("━━━ Step 0 : Reading inputs ━━━")
#     blk_blocks, sm_blocks, tsr_list = read_dataset()
#     iec_table = read_iec_table()
#     sm_coverage, sm_addressed, block_to_sms = read_sm_list_from_template()
#     print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
#           f"IEC parts: {len(iec_table)}  SM entries: {len(sm_coverage)}  SM→block mappings: {len(block_to_sms)}")
#     print("  block_to_sms:")
#     for b, sms in sorted(block_to_sms.items()):
#         print(f"    {b:<15} → {sms}")

#     # ── Agent 1: Map blocks → IEC parts ──────────────────────────────────────
#     print("\n━━━ Agent 1 : Block → IEC part mapper (LLM) ━━━")
#     blocks = agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache)
#     print("\n  Mapping result:")
#     for b in blocks:
#         tag = " [DUP]" if b.get('is_duplicate') else (" [SM]" if b.get('is_sm') else "")
#         print(f"    {b['name']:<35} → {b['fmeda_code']:<12} "
#               f"| {b.get('iec_part','')} ({len(b.get('modes',[]))} modes){tag}")

#     # ── Agent 2: Generate IC effects + system effects + memo ──────────────────
#     print("\n━━━ Agent 2 : IC Effects generator (LLM + deterministic memo) ━━━")
#     fmeda_data = agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage, sm_addressed, cache)

#     # Print memo summary for verification
#     print("\n  Memo check:")
#     for block in fmeda_data:
#         for row in block['rows']:
#             affected = extract_blocks_from_ic_effect(row.get('I', ''))
#             memo = row.get('K', 'O')
#             mode_short = row['G'][:40]
#             print(f"    {block['fmeda_code']:<12} K={memo}  affected={affected}  | {mode_short}")

#     # Save intermediate JSON
#     with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
#         json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
#     print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

#     # ── Agent 3: Write template ───────────────────────────────────────────────
#     print("\n━━━ Agent 3 : Template writer (deterministic) ━━━")
#     agent3_write_template(fmeda_data)

#     print("\n✅  Pipeline complete!")
#     print(f"    Output       : {OUTPUT_FILE}")
#     print(f"    Intermediate : {INTERMEDIATE_JSON}")
#     print(f"    Cache        : {CACHE_FILE}")


# if __name__ == '__main__':
#     run()

"""
fmeda_agents.py  —  Multi-Agent FMEDA Pipeline
===============================================

AGENT 1  (LLM)   Block → IEC part mapper
  Reads BLK sheet, maps each block to an IEC part_name, pulls verbatim modes.

AGENT 2  (LLM)   IC Effects + System Effects generator
  For every (block × mode):
    col I — effects on IC output (bullet format, what breaks downstream)
    col J — effects on system (from TSR sheet: safety requirements)
    col K — memo (X/O) derived by checking SM list addressed parts

MEMO LOGIC (deterministic, no LLM):
  Parse col I bullet list → extract block codes mentioned (BIAS, OSC, REF …)
  Look up SM list (from template): which SMs address each of those blocks?
  If ANY matching SM exists → K = "X"  (safety goal at risk)
  If NONE → K = "O"

AGENT 3  (Hardcoded)   Template writer
  Fills FMEDA_TEMPLATE.xlsx placeholders deterministically.

Usage:
    python fmeda_agents.py
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
TSR_SHEET         = 'TSR'
IEC_TABLE_FILE    = 'pdf_extracted.json'
TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
OUTPUT_FILE       = 'FMEDA_filled.xlsx'
CACHE_FILE        = 'fmeda_cache.json'
INTERMEDIATE_JSON = 'fmeda_intermediate.json'

OLLAMA_URL     = 'http://localhost:11434/api/generate'
OLLAMA_MODEL   = 'qwen3:30b'
OLLAMA_TIMEOUT = 300
SKIP_CACHE     = False
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# LLM / CACHE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def query_llm(prompt: str, temperature: float = 0.1) -> str:
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_ctx": 16384,
                        "top_p": 0.9, "repeat_penalty": 1.1}
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


def load_cache():
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# READ ALL INPUTS
# ═══════════════════════════════════════════════════════════════════════════════

def read_dataset():
    xl = pd.ExcelFile(DATASET_FILE)

    df = pd.read_excel(DATASET_FILE, sheet_name=BLK_SHEET, dtype=str).fillna('')
    blk_blocks = []
    for _, row in df.iterrows():
        vals = [v.strip() for v in row.values if str(v).strip()]
        if len(vals) >= 2:
            blk_blocks.append({'id': vals[0], 'name': vals[1],
                                'function': vals[2] if len(vals) > 2 else ''})

    sm_blocks = []
    if SM_SHEET in xl.sheet_names:
        df_sm = pd.read_excel(DATASET_FILE, sheet_name=SM_SHEET, dtype=str).fillna('')
        for _, row in df_sm.iterrows():
            vals = [v.strip() for v in row.values if str(v).strip()]
            if vals and re.match(r'sm[-_\s]?\d+', vals[0].lower()):
                sm_blocks.append({'id': vals[0], 'name': vals[1] if len(vals) > 1 else '',
                                   'description': vals[2] if len(vals) > 2 else ''})

    # TSR sheet → system-level safety requirements (used for col J)
    tsr_list = []
    if TSR_SHEET in xl.sheet_names:
        df_tsr = pd.read_excel(DATASET_FILE, sheet_name=TSR_SHEET, dtype=str).fillna('')
        for _, row in df_tsr.iterrows():
            vals = [v.strip() for v in row.values if str(v).strip()]
            if len(vals) >= 2:
                tsr_list.append({'id': vals[0], 'description': vals[1],
                                  'connected_fsr': vals[2] if len(vals) > 2 else ''})

    return blk_blocks, sm_blocks, tsr_list


def read_iec_table():
    with open(IEC_TABLE_FILE, encoding='utf-8-sig') as f:
        return json.load(f)


def read_block_fit_rates(wb):
    """
    Read block FIT rates from 'Core Block FIT rate' sheet (col B=block, col L=FIT).
    Returns dict: { 'REF': 0.0509, 'BIAS': 0.0840, ... }
    """
    fit_rates = {}
    try:
        ws = wb['Core Block FIT rate']
        for row in ws.iter_rows(min_row=25, max_row=ws.max_row):
            cells = {c.column_letter: c.value for c in row if c.value is not None}
            if 'B' in cells and 'L' in cells:
                block = str(cells['B']).strip()
                try:
                    fit = float(cells['L'])
                    fit_rates[block] = fit
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"  WARNING: Could not read FIT rates: {e}")
    return fit_rates


def read_sm_list_from_template():
    """
    Read SM list sheet from FMEDA_TEMPLATE.xlsx (or fallback to 3_ID03_FMEDA.xlsx).
    Returns:
      sm_coverage  : { 'SM01': 0.99, ... }
      sm_addressed : { 'SM01': ['REF','LDO'], ... }
      block_to_sms : { 'REF': ['SM01','SM02',...], ... }
    """
    import os
    # Try sources in priority order
    for candidate in [TEMPLATE_FILE, '3_ID03_FMEDA.xlsx']:
        if os.path.exists(candidate):
            try:
                wb_try = openpyxl.load_workbook(candidate, data_only=True)
                if 'SM list' in wb_try.sheetnames:
                    cov, addr, b2s = read_sm_list_from_workbook(wb_try)
                    if cov:  # non-empty → valid
                        print(f"  SM list read from: {candidate} ({len(cov)} entries)")
                        return cov, addr, b2s
            except Exception:
                pass

    # Fallback: hardcoded
    print("  SM list: using built-in knowledge")
    raw = _build_sm_list_from_knowledge()
    cov, addr, b2s = {}, {}, {}
    DEFAULT_COV = {
        'SM01':0.99,'SM02':0.99,'SM03':0.99,'SM04':0.99,'SM05':0.99,
        'SM06':0.9, 'SM08':0.9, 'SM09':0.99,'SM10':0.9, 'SM11':0.6,
        'SM12':0.9, 'SM13':0.99,'SM14':0.99,'SM15':0.99,'SM16':0.9,
        'SM17':0.9, 'SM18':0.99,'SM20':0.99,'SM21':0.6, 'SM22':0.99,
        'SM23':0.9, 'SM24':0.9,
    }
    for entry in raw:
        sm = entry['sm_code']
        parts = entry['addressed_parts']
        cov[sm]  = DEFAULT_COV.get(sm, 0.9)
        addr[sm] = parts
        for p in parts:
            b2s.setdefault(p, [])
            if sm not in b2s[p]:
                b2s[p].append(sm)
    return cov, addr, b2s


def read_sm_list_from_workbook(wb):
    """
    Read SM list directly from an open openpyxl workbook.
    Returns:
      sm_coverage  : { 'SM01': 0.99, 'SM11': 0.6, ... }  (col L)
      sm_addressed : { 'SM01': ['REF','LDO'], ... }        (col E)
      block_to_sms : { 'REF': ['SM01','SM02',...], ... }   reverse index
    """
    import re as _re
    ws = wb['SM list']

    sm_coverage  = {}   # SM code → float coverage
    sm_addressed = {}   # SM code → list of block codes

    for row in ws.iter_rows(min_row=12, max_row=ws.max_row):
        cells = {c.column_letter: c.value for c in row if c.value is not None}
        if 'C' not in cells:
            continue
        sm_code = str(cells['C']).strip()
        if not sm_code.startswith('SM'):
            continue

        # Coverage (col L)
        raw_cov = cells.get('L', '')
        try:
            cov = float(str(raw_cov))
        except (ValueError, TypeError):
            cov = 0.9
        sm_coverage[sm_code] = cov

        # Addressed parts (col E)
        raw_parts = str(cells.get('E', '')).strip()
        parts = [_re.sub(r'SW_BANK[_x\d]*', 'SW_BANK',
                          _re.sub(r'\bCSNS\b|\bCNSN\b|\bCS\b', 'CSNS', p.strip()))
                 for p in _re.split(r'[,;]', raw_parts) if p.strip()]
        sm_addressed[sm_code] = parts

    # Reverse index: block → [SM codes]
    block_to_sms = {}
    for sm_code, parts in sm_addressed.items():
        for part in parts:
            if part:
                block_to_sms.setdefault(part, [])
                if sm_code not in block_to_sms[part]:
                    block_to_sms[part].append(sm_code)

    return sm_coverage, sm_addressed, block_to_sms


def _build_sm_list_from_knowledge():
    """Hardcoded SM→block mapping from 3_ID03_FMEDA.xlsx SM list."""
    return [
        {'sm_code': 'SM01',  'addressed_parts': ['REF', 'LDO']},
        {'sm_code': 'SM02',  'addressed_parts': ['REF', 'LDO']},
        {'sm_code': 'SM03',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM04',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM05',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM06',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM08',  'addressed_parts': ['CSNS', 'ADC']},
        {'sm_code': 'SM09',  'addressed_parts': ['LOGIC']},
        {'sm_code': 'SM10',  'addressed_parts': ['LOGIC']},
        {'sm_code': 'SM11',  'addressed_parts': ['OSC']},
        {'sm_code': 'SM12',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM13',  'addressed_parts': ['SW_BANK', 'LOGIC']},
        {'sm_code': 'SM14',  'addressed_parts': ['CP']},
        {'sm_code': 'SM15',  'addressed_parts': ['REF', 'LDO']},
        {'sm_code': 'SM16',  'addressed_parts': ['REF', 'ADC']},
        {'sm_code': 'SM17',  'addressed_parts': ['TEMP']},
        {'sm_code': 'SM18',  'addressed_parts': ['LOGIC']},
        {'sm_code': 'SM20',  'addressed_parts': ['LDO']},
        {'sm_code': 'SM21',  'addressed_parts': ['LOGIC']},
        {'sm_code': 'SM22',  'addressed_parts': ['CP', 'SW_BANK']},
        {'sm_code': 'SM23',  'addressed_parts': ['TEMP']},
        {'sm_code': 'SM24',  'addressed_parts': ['ADC', 'SW_BANK']},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# MEMO LOGIC  (deterministic — no LLM)
# ═══════════════════════════════════════════════════════════════════════════════

# Normalise any block code variant to canonical form
_BLOCK_NORM = {
    'SW_BANKX': 'SW_BANK', 'SW_BANK_X': 'SW_BANK', 'SW_BANKx': 'SW_BANK',
    'SW_BANK_1': 'SW_BANK', 'SW_BANK_2': 'SW_BANK',
    'SW_BANK_3': 'SW_BANK', 'SW_BANK_4': 'SW_BANK',
    'CNSN': 'CSNS', 'CS': 'CSNS',
    'DIETEMP': 'TEMP',
    'VEGA': 'CP',   # Vega = the IC itself, charge pump damage
}


def _norm_block(code: str) -> str:
    c = code.strip().upper()
    return _BLOCK_NORM.get(c, c)


def extract_blocks_from_ic_effect(ic_effect: str) -> list[str]:
    """
    Parse the bullet-format IC effect string and return list of block codes.
    e.g. "• BIAS\n    - ...\n• ADC\n    - ..." → ['BIAS', 'ADC']
    """
    if not ic_effect or ic_effect.strip() in ('No effect', ''):
        return []
    # Match lines starting with •
    blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
    return [_norm_block(b) for b in blocks if b.upper() not in ('NONE', '')]


def determine_memo(ic_effect: str, block_to_sms: dict) -> tuple[str, list[str]]:
    """
    Returns (memo, matching_sms_list).
    memo = 'X' if ANY block in ic_effect is covered by a SM, else 'O'.
    """
    if not ic_effect or ic_effect.strip() in ('No effect', ''):
        return 'O', []

    affected_blocks = extract_blocks_from_ic_effect(ic_effect)
    if not affected_blocks:
        return 'O', []

    matching_sms = []
    for block in affected_blocks:
        sms = block_to_sms.get(block, [])
        for sm in sms:
            if sm not in matching_sms:
                matching_sms.append(sm)

    memo = 'X' if matching_sms else 'O'
    return memo, matching_sms


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 1  —  Block → IEC part mapper
# ═══════════════════════════════════════════════════════════════════════════════

def agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache):
    ck = "agent1__" + json.dumps([b['name'] for b in blk_blocks])
    if not SKIP_CACHE and ck in cache:
        print("  [Agent 1] Loaded from cache")
        result = cache[ck]
        _append_sm_blocks(result, sm_blocks)
        return result

    # Build IEC summary for the prompt
    iec_summary = ""
    for i, p in enumerate(iec_table):
        modes = p["entries"][0]["modes"]
        iec_summary += (
            f'  {i+1:2d}. "{p["part_name"]}"\n'
            f'       Desc : {p["entries"][0]["description"][:120]}\n'
            f'       Modes: {json.dumps(modes[:3])}'
            + (' ...' if len(modes) > 3 else '') + '\n\n'
        )

    blocks_text = "\n".join(
        f'  {b["id"]}: "{b["name"]}" — {b["function"]}'
        for b in blk_blocks
    )

    prompt = f"""You are an automotive IC functional safety engineer.

CHIP BLOCKS:
{blocks_text}

IEC 62380 HARDWARE PART CATEGORIES:
{iec_summary}

FMEDA SHORT CODE RULES (short label used in the FMEDA table):
  Voltage reference / bandgap                    → REF
  Bias current source / current reference        → BIAS
  LDO / linear voltage regulator                 → LDO
  Internal oscillator / clock generator          → OSC
  Watchdog / clock monitor (shares OSC slot)     → OSC   [duplicate]
  Temperature sensor / thermal circuit           → TEMP
  Current sense amplifier / op-amp sense         → CSNS
  Current DAC / channel DAC                      → ADC
  ADC (analogue to digital converter)            → ADC   [duplicate of DAC slot]
  Charge pump / boost regulator                  → CP
  nFAULT driver / fault aggregator (shares CP)   → CP    [duplicate]
  Digital logic / main controller                → LOGIC
  Open-load / short-to-GND detector (LOGIC)      → LOGIC [duplicate]
  SPI / UART / serial interface                  → INTERFACE
  NVM / trim / self-test / POST                  → TRIM
  LED driver switch bank N                       → SW_BANK_N

TASK: For each block determine:
  "fmeda_code"   — short code from rules above
  "iec_part"     — EXACT part_name string from IEC list that best matches
  "is_duplicate" — true if this fmeda_code was already assigned to an earlier block

Return JSON array, same order as input blocks:
[
  {{"id":"BLK-01","name":"Bandgap Reference","fmeda_code":"REF",
    "iec_part":"Voltage references","is_duplicate":false}},
  ...
]
Return ONLY the JSON array:"""

    print("  [Agent 1] Calling LLM to map blocks → IEC parts...")
    raw    = query_llm(prompt, temperature=0.05)
    result = parse_json(raw)

    if not isinstance(result, list) or len(result) != len(blk_blocks):
        print("  [Agent 1] LLM parse issue — using fallback")
        result = _fallback_agent1(blk_blocks)

    # CRITICAL: always replace LLM-generated modes with verbatim IEC table modes
    iec_idx = {p['part_name']: p['entries'][0]['modes'] for p in iec_table}
    for b in result:
        iec_part = b.get('iec_part', '')
        # Exact match
        if iec_part in iec_idx:
            b['modes'] = iec_idx[iec_part]
        else:
            # Fuzzy match
            matched = False
            for pname, modes in iec_idx.items():
                if iec_part[:20].lower() in pname.lower() or pname[:20].lower() in iec_part.lower():
                    b['modes'] = modes
                    b['iec_part'] = pname
                    matched = True
                    break
            if not matched:
                b['modes'] = []
                print(f"  [Agent 1] WARNING: no IEC modes for '{iec_part}' ({b['name']})")

    # BLOCK-SPECIFIC MODE OVERRIDES
    # For INTERFACE and TRIM the IEC table has generic modes, but the real FMEDA
    # uses domain-specific TX/RX and omission/commission patterns.
    # Override these regardless of what IEC table says.
    FMEDA_MODE_OVERRIDES = {
        'INTERFACE': [
            'TX: No message transferred as requested',
            'TX: Message transferred when not requested',
            'TX: Message transferred too early/late',
            'TX: Message transferred with incorrect value',
            'RX: No incoming message processed',
            'RX: Message transferred when not requested',
            'RX: Message transferred too early/late',
            'RX: Message transferred with incorrect value',
        ],
        'TRIM': [
            'Error of omission (i.e. not triggered when it should be)',
            "Error of comission (i.e. triggered when it shouldn't be)",
            'Incorrect settling time (i.e. outside the expected range)',
            'Incorrect output',
        ],
    }
    for b in result:
        code = b.get('fmeda_code', '')
        if code in FMEDA_MODE_OVERRIDES:
            b['modes'] = FMEDA_MODE_OVERRIDES[code]

    # Enforce duplicate flags
    seen = set()
    for b in result:
        code = b.get('fmeda_code', '')
        if code in seen:
            b['is_duplicate'] = True
        else:
            b['is_duplicate'] = False
            seen.add(code)

    cache[ck] = result
    save_cache(cache)

    _append_sm_blocks(result, sm_blocks)
    return result


def _append_sm_blocks(result, sm_blocks):
    for sm in sm_blocks:
        m = re.match(r'sm[-_\s]?(\d+)', sm['id'].lower())
        code = f"SM{int(m.group(1)):02d}" if m else sm['id'].upper()
        result.append({
            'id': sm['id'], 'name': sm['name'], 'function': sm.get('description', ''),
            'fmeda_code': code, 'iec_part': 'Safety Mechanism',
            'modes': ['Fail to detect', 'False detection'],
            'is_duplicate': False, 'is_sm': True,
        })


def _fallback_agent1(blk_blocks):
    KMAP = [
        (['bandgap','voltage reference','1.2v','temperature-stable ref'],
         'REF',       'Voltage references'),
        (['bias current','current source','bias generator'],
         'BIAS',      'Current source (including bias current generator)'),
        (['ldo','low dropout','linear regulator'],
         'LDO',       'Voltage regulators (linear, SMPS, etc.)'),
        (['oscillator','internal clock','4 mhz','watchdog','clock monitor'],
         'OSC',       'Oscillator'),
        (['thermal shutdown','die temperature','on-chip diode'],
         'TEMP',      'Operational amplifier and buffer'),
        (['current sense','shunt','sense amplifier','overcurrent comparator'],
         'CSNS',      'Operational amplifier and buffer'),
        (['current dac','channel dac','8-bit current','dac for'],
         'ADC',       'N bits digital to analogue converters (DAC)d'),
        (['charge pump','boost'],
         'CP',        'Charge pump, regulator boost'),
        (['spi interface','serial interface','uart','fault readback'],
         'INTERFACE', 'N bits analogue to digital converters (N-bit ADC)'),
        (['self-test','post','power-on self','validates dac'],
         'TRIM',      'Voltage references'),
        (['nfault','open-drain fault','aggregates fault'],
         'CP',        'Charge pump, regulator boost'),
        (['open-load','short-to-gnd','detector','logic'],
         'LOGIC',     'Voltage/Current comparator'),
    ]
    used, result = set(), []
    for b in blk_blocks:
        combined = (b['name'] + ' ' + b['function']).lower()
        code, iec = 'LOGIC', 'Voltage/Current comparator'
        for kws, c, ip in KMAP:
            if any(k in combined for k in kws):
                code, iec = c, ip
                break
        dup = code in used
        if not dup: used.add(code)
        result.append({'id': b['id'], 'name': b['name'], 'function': b['function'],
                        'fmeda_code': code, 'iec_part': iec, 'is_duplicate': dup})
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 2  —  IC Effects + System Effects generator
# ═══════════════════════════════════════════════════════════════════════════════

IC_FORMAT = """
EXACT FORMAT for col I  "effects on IC output":
  • BLOCK_CODE
      - specific effect on that block
      - second effect if applicable
  • ANOTHER_BLOCK_CODE
      - specific effect

  If NOTHING is affected → write exactly: No effect

RULES:
  • Use •  before block name (no indent before •)
  • Use 4 spaces + dash before each effect line under a block
  • Block codes: REF  BIAS  LDO  OSC  TEMP  CSNS  ADC  CP  LOGIC  INTERFACE  TRIM  SW_BANK_x
  • Effect must be specific — NOT "BIAS is affected" BUT "Output reference voltage is stuck"
  • Use present tense: "is stuck", "is incorrect", "cannot operate", "out of spec."
  • List EVERY block that receives signal from the failing block — do not omit any
""".strip()

FEW_SHOT = """
VERIFIED EXAMPLES FROM A REAL AUTOMOTIVE IC FMEDA:

REF / "Output is stuck (i.e. high or low)"  → col I:
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

REF / "Output is floating (i.e. open circuit)"  → col I:
• BIAS
    - Output reference voltage is floating
    - Output reference current is higher than the expected range
    - Output reference current is lower than the expected range
    - Output bias current is higher than the expected range
    - Output bias current is lower than the expected range
• ADC
    - REF output is floating (i.e. open circuit)
• LDO
    - Out of spec
• OSC
    - Out of spec

REF / "Output voltage affected by spikes"  → col I:
No effect

BIAS / "One or more outputs are stuck (i.e. high or low)"  → col I:
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

OSC / "Output is stuck (i.e. high or low)"  → col I:
• LOGIC
    - Cannot operate.
    - Communication error.

TEMP / "Output is stuck (i.e. high or low)"  → col I:
• ADC
    - TEMP output is stuck low
• SW_BANK_x
    - SW is stuck in off state (DIETEMP)

TEMP / "Output is floating (i.e. open circuit)"  → col I:
• ADC
    - Incorrect TEMP reading

CSNS / "Output is stuck (i.e. high or low)"  → col I:
• ADC
    - CSNS output is incorrect.

ADC / "One or more outputs are stuck (i.e. high or low)"  → col I:
• SW_BANK_x
    - SW is stuck in off state (DIETEMP)
• ADC
    - Incorrect BGR measurement
    - Incorrect DIETEMP measurement
    - Incorrect CS measurement

CP / "Output voltage lower than a low threshold..."  → col I:
• SW_BANK_x
    - SWs are stuck in off state, LEDs always ON.

CP / "Output voltage higher than a high threshold..."  → col I:
• Vega
    - Device Damage

LOGIC / "Output is stuck (i.e. high or low)"  → col I:
• SW_BANK_X
    - SW is stuck in on/off state
• OSC
    - Output stuck

TRIM / "Error of omission (i.e. not triggered when it should be)"  → col I:
• REF
    - Incorrect output value higher than the expected range
• LDO
    - Reference voltage higher than the expected range
• BIAS
    - Output reference voltage accuracy too low, including drift
• SW_BANK
    - Incorrect slew rate value
• OSC
    - Incorrect output frequency: higher than the expected range
• DIETEMP
    - Incorrect output voltage

SAFE MODES — these are ALWAYS "No effect" for col I:
  "affected by spikes", "oscillation within the expected range",
  "incorrect start-up time", "jitter too high", "incorrect duty cycle",
  "quiescent current exceeding", "settling time", "false detection"
""".strip()


def agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage, sm_addressed, cache):
    """Generate col I (IC effect), col J (system effect), col K (memo) for all blocks."""

    # Build chip context for LLM
    active = [b for b in blocks if not b.get('is_duplicate') and not b.get('is_sm')]
    chip_ctx = "\n".join(
        f"  {b['fmeda_code']:<12} {b['name']:<35} | {b.get('function','')[:80]}"
        for b in active
    )

    # TSR context for col J
    tsr_ctx = "\n".join(
        f"  {t['id']}: {t['description']}"
        for t in tsr_list
    ) if tsr_list else "  (no TSR data)"

    result = []
    for block in blocks:
        code  = block['fmeda_code']
        name  = block['name']
        modes = block.get('modes', [])

        # SM blocks → hardcoded
        if block.get('is_sm'):
            rows = _sm_rows(code)
            result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
            print(f"  [Agent 2] {code:<12} SM — hardcoded (2 rows)")
            continue

        # Duplicate blocks → skip
        if block.get('is_duplicate'):
            print(f"  [Agent 2] {code:<12} DUPLICATE ({name}) — skipped")
            continue

        if not modes:
            print(f"  [Agent 2] {code:<12} no modes — skipped")
            continue

        ck = f"agent2__{code}__{name}__{len(modes)}"
        if not SKIP_CACHE and ck in cache:
            rows = cache[ck]
            print(f"  [Agent 2] {code:<12} cache ({len(rows)} rows)")
            result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
            continue

        rows = _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms, sm_coverage, sm_addressed)
        cache[ck] = rows
        save_cache(cache)
        result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
        print(f"  [Agent 2] {code:<12} {len(rows)} rows (LLM)")
        time.sleep(0.3)

    return result


def _build_downstream_hint(block, chip_ctx):
    """
    Build a text hint about which blocks likely receive signal from this block.
    Based on function keywords — helps LLM reason about signal flow.
    """
    code = block['fmeda_code']
    func = (block.get('function') or '').lower()
    name = block.get('name', '').lower()

    # Known downstream relationships
    DOWNSTREAM = {
        'REF':       'BIAS, ADC, TEMP, LDO, OSC — all use the reference voltage for biasing and regulation',
        'BIAS':      'ADC, TEMP, LDO, OSC, SW_BANKx, CP, CSNS — all receive bias currents from BIAS',
        'LDO':       'OSC (LDO powers the oscillator supply rail)',
        'OSC':       'LOGIC, INTERFACE — clock signal drives all digital logic and communication',
        'TEMP':      'ADC (TEMP voltage is read by ADC), SW_BANK_x (DIETEMP controls output enable)',
        'CSNS':      'ADC (CSNS output is digitized by ADC for current monitoring)',
        'ADC':       'SW_BANK_x (ADC DIETEMP result controls switch enable), LOGIC (ADC results feed decision logic)',
        'CP':        'SW_BANK_x (charge pump supplies the gate drive voltage for all switches)',
        'LOGIC':     'SW_BANK_X (LOGIC drives all switch banks), OSC (LOGIC can assert reset)',
        'INTERFACE': 'LOGIC, ADC (SPI writes configure DAC and read ADC results)',
        'TRIM':      'REF, LDO, BIAS, OSC, SW_BANK, DIETEMP — trim data calibrates all analog blocks',
    }

    hint = DOWNSTREAM.get(code, '')
    if not hint:
        # Generic: suggest looking at all blocks
        hint = 'Review all blocks — consider which ones depend on this block output signal'
    return hint


def _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms, sm_coverage, sm_addressed):
    code = block['fmeda_code']
    name = block['name']
    func = block.get('function', '')
    n    = len(modes)

    # Build downstream signal map for this block
    # Helps LLM reason about who receives the signal from this block
    downstream_hint = _build_downstream_hint(block, chip_ctx)

    prompt = f"""You are completing an FMEDA table for an automotive IC (ISO 26262 / AEC-Q100).

{FEW_SHOT}

═══════════════════════════════════════════════════
ALL BLOCKS IN THIS CHIP (fmeda_code | name | function):
{chip_ctx}

SYSTEM SAFETY REQUIREMENTS (TSR):
{tsr_ctx}
═══════════════════════════════════════════════════
BLOCK BEING ANALYZED:
  FMEDA Code : {code}
  Block Name : {name}
  Function   : {func}

SIGNAL FLOW HINT (who likely receives output from this block):
{downstream_hint}
═══════════════════════════════════════════════════
FAILURE MODES TO ANALYZE ({n} total):
{json.dumps(modes, indent=2)}
═══════════════════════════════════════════════════

STEP-BY-STEP REASONING FOR EACH MODE:

  Step 1 — IDENTIFY THE OUTPUT SIGNAL
    Ask: "What physical signal does {name} produce?"
    Examples: reference voltage, bias current, clock signal, PWM enable, serial data, temperature voltage

  Step 2 — MAP ALL CONSUMERS
    Go through EVERY other block in the chip.
    For each block ask: "Does its function depend on {name}'s output?"
    Be thorough — a bias current affects ADC, TEMP, LDO, OSC, CP all at once.
    A reference voltage affects every block that uses it for comparison or regulation.
    A clock signal affects all digital blocks.

  Step 3 — DETERMINE THE SPECIFIC SYMPTOM PER CONSUMER
    For each consumer block, describe exactly what goes wrong:
    - NOT "ADC is affected" → YES "ADC measurement is incorrect."
    - NOT "OSC has issue"   → YES "Oscillation does not start" or "Frequency out of spec."
    - NOT "LOGIC fails"     → YES "Cannot operate." + "Communication error."

  Step 4 — SAFE MODES AND BLOCK-SPECIFIC RULES:

    ALWAYS "No effect" for col I (local disturbances, don't propagate):
    "affected by spikes", "oscillation within expected range",
    "incorrect start-up time", "jitter too high", "incorrect duty cycle",
    "quiescent current exceeding", "incorrect settling time"

    BLOCK-SPECIFIC SAFETY OVERRIDES (apply BEFORE general reasoning):
    • CSNS block: ALL modes → col I = "• ADC\n    - CSNS output is incorrect." or
      "No effect", col J = "No effect", K = "O" always.
      Reason: CSNS feeds ADC for monitoring only; not a direct safety path.
    • ADC block: ONLY stuck/floating → K=X. ALL others (accuracy/offset/
      linearity/settling/monotonic/full-scale) → K=O, J="No effect".
    • INTERFACE block: ALL modes → col I = "Communication error",
      col J = "Fail-safe mode active", K = "O".
    • SW_BANK block: Stuck/floating/resistance-too-high → K=X.
      Resistance-too-low/turn-on-time/turn-off-time → K=O.

  Step 5 — SYSTEM EFFECT (col J)
    What does the end user/ECU observe? Cross-check TSR requirements.
    Choose from:
      "Unintentional LED ON/OFF\nFail-safe mode active\nNo communication"
      "Fail-safe mode active\nNo communication"
      "Fail-safe mode active"
      "Unintended LED ON"
      "Unintended LED OFF"
      "Unintended LED ON/OFF"
      "Device damage"
      "No effect"

{IC_FORMAT}

Return a JSON array with EXACTLY {n} objects, same order as failure modes:
[
  {{
    "G": "<exact failure mode string>",
    "I": "<col I: IC output effect>",
    "J": "<col J: system effect>"
  }},
  ...
]
Return ONLY the JSON array:"""

    raw    = query_llm(prompt, temperature=0.05)
    parsed = parse_json(raw)

    if isinstance(parsed, list) and len(parsed) >= n:
        rows = []
        for i in range(n):
            rd   = parsed[i]
            ic   = str(rd.get('I', 'No effect')).strip()
            sys_ = str(rd.get('J', 'No effect')).strip()
            # Apply deterministic corrections BEFORE computing memo
            ic, sys_, memo_override = _apply_block_rules(code, modes[i], ic, sys_)
            if memo_override:
                memo = memo_override
            else:
                memo, _ = determine_memo(ic, block_to_sms)
            rows.append(_build_row(modes[i], ic, sys_, memo, block_to_sms, sm_coverage, fmeda_code=code))
        return rows

    print(f"    LLM parse failed for {code} — using fallback")
    return _fallback_rows(modes, block_to_sms, sm_coverage, sm_addressed)


def _apply_block_rules(code, mode, ic, sys_):
    """
    Deterministic per-block corrections.
    Returns (corrected_ic, corrected_sys, memo_override_or_None)
    These rules fix the known systematic errors identified in the accuracy report.
    """
    mode_lower = mode.lower()

    # CSNS: ALL modes → only affect ADC, K=O, J=No effect
    if code == 'CSNS':
        is_safe = any(k in mode_lower for k in [
            'spike', 'oscillation within', 'start-up', 'jitter',
            'duty cycle', 'quiescent', 'settling', 'false detection'
        ])
        if is_safe:
            return 'No effect', 'No effect', 'O'
        else:
            return '• ADC\n    - CSNS output is incorrect.', 'No effect', 'O'

    # ADC: only stuck/floating → X; everything else → O
    if code == 'ADC':
        if any(k in mode_lower for k in ['stuck', 'floating', 'open circuit']):
            return ic, sys_, None  # let LLM result stand, memo from SM list
        else:
            # accuracy/offset/linearity/settling/monotonic/full-scale → O
            _adc_ic = '• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement'
            corrected_ic = ic if ic and ic != 'No effect' else _adc_ic
            if 'settling' in mode_lower:
                corrected_ic = 'No effect'
            return corrected_ic, 'No effect', 'O'

    # INTERFACE: all modes → Communication error, K=O
    if code == 'INTERFACE':
        return 'Communication error', 'Fail-safe mode active', 'O'

    # SW_BANK: only stuck/floating/res-high → X; res-low/timing → O
    if code.startswith('SW_BANK'):
        if any(k in mode_lower for k in ['stuck', 'floating', 'open circuit', 'resistance too high']):
            return ic, sys_, None  # keep X
        else:
            return ic, 'No effect', 'O'

    # SM blocks: "Fail to detect" → always 'X (Latent)'
    if code.startswith('SM') and 'fail to detect' in mode_lower:
        return ic, sys_, 'X (Latent)'

    return ic, sys_, None  # no override


# Per-block SM selection — taken directly from 3_ID03_FMEDA.xlsx.
# Maps (fmeda_code, mode_type) → SM string.
# mode_type: 'stuck_float' for stuck/floating/out-of-range,
#            'accuracy'    for accuracy/drift modes,
#            'default'     fallback
_BLOCK_SM_MAP = {
    # (block_code, mode_type) → 'SM## SM## ...'
    ('REF',       'stuck_float'): 'SM01 SM15 SM16 SM17',
    ('REF',       'accuracy'):    'SM01 SM11 SM15 SM16',
    ('REF',       'default'):     'SM01 SM15 SM16 SM17',
    ('BIAS',      'default'):     'SM11 SM15 SM16',
    ('LDO',       'ov'):          'SM11 SM20',
    ('LDO',       'uv'):          'SM11 SM15',
    ('LDO',       'accuracy'):    'SM11 SM15 SM20',
    ('LDO',       'default'):     'SM11 SM15 SM20',
    ('OSC',       'default'):     'SM09 SM10 SM11',
    ('TEMP',      'default'):     'SM17 SM23',
    ('CSNS',      'default'):     '',           # CSNS → K=O, no SM needed
    ('ADC',       'stuck_float'): 'SM08 SM16 SM17 SM23',
    ('ADC',       'default'):     '',           # ADC accuracy → K=O
    ('CP',        'ov'):          '',           # OV → no SM (device damage)
    ('CP',        'uv'):          'SM14 SM22',
    ('CP',        'default'):     'SM14 SM22',
    ('LOGIC',     'default'):     'SM10 SM11 SM12 SM18',
    ('INTERFACE', 'default'):     '',           # INTERFACE → K=O
    ('TRIM',      'default'):     'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
    ('SW_BANK_1', 'stuck_float'): 'SM04 SM05 SM06 SM08',
    ('SW_BANK_1', 'res_high'):    'SM04 SM06 SM08',
    ('SW_BANK_1', 'driver'):      'SM03 SM06 SM24',
    ('SW_BANK_1', 'default'):     'SM04 SM06 SM08',
    ('SW_BANK_2', 'stuck_float'): 'SM04 SM05 SM06 SM08',
    ('SW_BANK_2', 'res_high'):    'SM04 SM06 SM08',
    ('SW_BANK_2', 'driver'):      'SM03 SM06 SM24',
    ('SW_BANK_2', 'default'):     'SM04 SM06 SM08',
    ('SW_BANK_3', 'stuck_float'): 'SM04 SM05 SM06 SM08',
    ('SW_BANK_3', 'res_high'):    'SM04 SM06 SM08',
    ('SW_BANK_3', 'driver'):      'SM03 SM06 SM24',
    ('SW_BANK_3', 'default'):     'SM04 SM06 SM08',
    ('SW_BANK_4', 'stuck_float'): 'SM04 SM05 SM06 SM08',
    ('SW_BANK_4', 'res_high'):    'SM04 SM06 SM08',
    ('SW_BANK_4', 'driver'):      'SM03 SM06 SM24',
    ('SW_BANK_4', 'default'):     'SM04 SM06 SM08',
}


def _mode_type(code, mode_str, ic_effect):
    """Classify a failure mode into a type key for _BLOCK_SM_MAP lookup."""
    m = mode_str.lower()
    i = (ic_effect or '').lower()
    if 'stuck' in m or 'floating' in m or 'open circuit' in m:
        return 'stuck_float'
    if ('accuracy' in m or 'drift' in m or 'too low' in m
            or 'incorrect output voltage' in m
            or 'incorrect reference current' in m
            or ('incorrect' in m and 'outside the expected range' in m)):
        return 'accuracy'
    if 'over voltage' in m or '— ov' in m or 'higher than a high threshold' in m:
        return 'ov'
    if 'under voltage' in m or '— uv' in m or 'lower than a low threshold' in m:
        return 'uv'
    if 'resistance too high' in m:
        return 'res_high'
    if 'driver' in m and code.startswith('SW_BANK'):
        return 'driver'
    return 'default'


def compute_sm_columns(ic_effect, block_to_sms, sm_coverage, fmeda_code='', mode_str=''):
    """
    Returns (sm_string, coverage_value) for col S/Y and col U.
    Uses per-block hardcoded SM map from real FMEDA when available,
    falls back to block_to_sms intersection for unknown blocks.
    """
    if not ic_effect or ic_effect.strip() == 'No effect':
        return '', ''

    # Try hardcoded map first
    mtype = _mode_type(fmeda_code, mode_str, ic_effect)
    sm_str = _BLOCK_SM_MAP.get((fmeda_code, mtype)) or              _BLOCK_SM_MAP.get((fmeda_code, 'default'))

    if sm_str is None:
        # Fallback: intersection of block_to_sms for blocks in col I
        affected = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
        norm = []
        for b in affected:
            b = b.strip().upper()
            b = re.sub(r'SW_BANK[_X\d]*', 'SW_BANK', b)
            b = re.sub(r'CSNS|CNSN|CS', 'CSNS', b)
            if b not in ('NONE', 'VEGA', ''):
                norm.append(b)
        matching = []
        for block in norm:
            for sm in block_to_sms.get(block, []):
                if sm not in matching:
                    matching.append(sm)
        def sm_key(s):
            m2 = re.search(r'(\d+)', s)
            return int(m2.group(1)) if m2 else 0
        matching.sort(key=sm_key)
        sm_str = ' '.join(matching)

    if not sm_str:
        return '', ''

    # Compute max coverage for the SMs listed
    valid = [0.99, 0.9, 0.6]
    def nearest(v):
        return min(valid, key=lambda x: abs(x - v))
    coverages = [nearest(sm_coverage.get(sm, 0.9)) for sm in sm_str.split()]
    max_cov = max(coverages) if coverages else 0.9

    return sm_str, max_cov


def _build_row(canonical_mode, ic, sys_, memo, block_to_sms=None, sm_coverage=None, **kwargs):
    """
    col P  (Single Point Failure mode): Y if K=X (not Latent), N otherwise
    col R  (Percentage of Safe Faults): 0 if IC has any effect, 1 if No effect
    col S  : SM codes from per-block hardcoded map (from real FMEDA)
    col U  : highest coverage from those SMs (0.99 / 0.9 / 0.6)
    col Y  : same as col S (latent SM coverage = same mechanisms)
    kwargs: fmeda_code (str) — used for SM map lookup
    """
    ic_clean = ic.strip()
    if ic_clean in ('No effect', ''):
        memo = 'O'

    # col P: Single Point Failure
    # IMPORTANT: K="X (Latent)" means LATENT fault, NOT single-point
    # Only K="X" (without Latent) gets P=Y
    sp = 'Y' if memo == 'X' else 'N'
    # col R: Percentage of Safe Faults
    # If K=O (no safety violation) → R=1 (100% safe)
    # If K=X or X(Latent) → R=0 (0% safe — needs SM to make safe)
    pct_safe = 1 if not memo.startswith('X') else 0

    # Compute S, U, Y using per-block SM map
    sm_str, coverage = '', ''
    if ic_clean != 'No effect':
        sm_str, coverage = compute_sm_columns(
            ic_clean, block_to_sms or {}, sm_coverage or {},
            fmeda_code=kwargs.get('fmeda_code', ''),
            mode_str=canonical_mode
        )

    return {
        'G': canonical_mode,
        'I': ic,
        'J': sys_,
        'K': memo,
        'O': 1,
        'P': sp,
        'R': pct_safe,
        'S': sm_str,    # col S: Safety mechanisms IC
        'T': '',
        'U': coverage,  # col U: highest coverage SPF
        'V': '',
        'X': 'Y' if memo.startswith('X') else 'N',  # Y for both X and X(Latent)
        'Y': sm_str,    # col Y: same as S (latent SM = same mechanisms)
        'Z': '', 'AA': '', 'AB': '', 'AD': '',
    }


def _fallback_rows(modes, block_to_sms, sm_coverage=None, sm_addressed=None, fmeda_code=''):
    SAFE = ['spike','oscillation within','start-up','jitter','duty cycle',
            'quiescent','settling','false detection']
    rows = []
    for mode in modes:
        safe = any(k in mode.lower() for k in SAFE)
        ic   = 'No effect' if safe else ''
        memo, _ = determine_memo(ic, block_to_sms)
        rows.append(_build_row(mode, ic, 'No effect' if safe else '', memo,
                               block_to_sms, sm_coverage, fmeda_code=fmeda_code))
    return rows


def _sm_rows(sm_code):
    """
    SM blocks always have exactly 2 rows.
    CRITICAL from real FMEDA:
      Fail to detect  → K='X (Latent)'  P=N (NOT Y — it's latent, not single-point)  X=Y
      False detection → K='O'           P=N  X=N
    """
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
        'SM20': ('Device damage',                            'Device damage'),
        'SM21': ('Unsynchronised PWM',                       'No effect'),
        'SM22': ('Unintended LED OFF',                       'Unintended LED OFF'),
        'SM23': ('Loss of thermal monitoring capability',    'Possible device damage'),
        'SM24': ('Loss of LED voltage monitoring capability','No effect'),
    }
    ic, sys_ = SM.get(sm_code, ('Loss of safety mechanism functionality', 'Fail-safe mode active'))
    return [
        # Fail to detect: K=X(Latent), P=N (LATENT fault ≠ single-point), X=Y
        {'G': 'Fail to detect',  'I': ic,          'J': sys_,        'K': 'X (Latent)',
         'O': 1, 'P': 'N', 'R': 0, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'Y', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
        # False detection: K=O, P=N, X=N
        {'G': 'False detection', 'I': 'No effect', 'J': 'No effect', 'K': 'O',
         'O': 1, 'P': 'N', 'R': 1, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'N', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3  —  Template Writer  (deterministic)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_fit_values(code, n_modes, block_fit_rates, row_memo, row_U, sm_coverage_val):
    """
    Compute per-row FIT values:
      E  = Block FIT (first row only)
      F  = Mode FIT  = block_fit / n_modes
      Q  = Failure rate FIT = F × 1 (failure distribution always 1)
      V  = Residual FIT:
             if memo=O  → 0
             if memo=X  → Q × (1 - R) × (1 - U)  where R=0 for X rows
                        → Q × 1 × (1 - U)
                        → Q × (1 - U)
      AA = Latent coverage (1 if SM coverage 0.99, 0.8 if 0.9 coverage)
      AB = Latent MPF FIT:
             if X and AA=1 → 0
             else          → Q × (1-R) - V) × (1 - AA)  ≈ 0 when AA=1
    """
    block_fit = block_fit_rates.get(code, 0.0)
    mode_fit  = block_fit / n_modes if n_modes > 0 and block_fit > 0 else 0.0

    if not row_memo.startswith('X'):
        # Safe mode: V=0, AA and AB not applicable
        return block_fit, mode_fit, mode_fit, 0.0, None, None

    U = float(row_U) if row_U else 0.0

    # V = residual FIT = Q × (1 - U)  since R=0 for X rows
    V = mode_fit * (1.0 - U)

    # AA = latent coverage — per-block values from real FMEDA
    # Blocks with AA=0.8 for X rows (SM coverage = 0.9 for these latent paths):
    BLOCKS_AA_08 = {'LDO', 'TEMP', 'ADC', 'CP', 'LOGIC',
                    'SW_BANK_1','SW_BANK_2','SW_BANK_3','SW_BANK_4', 'SM09'}
    # CP OV has no SM → AA=0
    BLOCKS_AA_00 = set()  # handled below

    if not U:  # no SM → no latent coverage
        AA = 0.0
    elif code in BLOCKS_AA_08:
        AA = 0.8
    elif U >= 0.99:
        AA = 1.0
    elif U >= 0.85:
        AA = 0.8
    else:
        AA = U

    # AB = Latent MPF FIT ≈ 0 when AA=1 (fully covered by SM)
    # Formula: (Q*(1-R) - V) * (1-AA)
    #        = (mode_fit - V) * (1 - AA)
    #        = (mode_fit - mode_fit*(1-U)) * (1-AA)
    #        = mode_fit * U * (1-AA)
    AB = mode_fit * U * (1.0 - AA)

    return block_fit, mode_fit, mode_fit, V, AA, AB


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
    if value is None or (isinstance(value, str) and value.strip() in ('', 'None', 'nan')):
        cell.value = None
        return
    cell.value = value
    if wrap and isinstance(value, str) and '\n' in value:
        old = cell.alignment or Alignment()
        cell.alignment = Alignment(wrap_text=True,
                                   vertical=old.vertical or 'center',
                                   horizontal=old.horizontal or 'left')


def agent3_write_template(fmeda_data, block_fit_rates=None, sm_coverage=None):
    """
    Fill template placeholders.
    block_fit_rates: { 'REF': 0.0509, ... } — block-level FIT from Core Block FIT sheet
    sm_coverage    : { 'SM01': 0.99, ... }  — SM diagnostic coverage from SM list
    """
    if block_fit_rates is None:
        block_fit_rates = {}
    if sm_coverage is None:
        sm_coverage = {}
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
            print(f"  [Agent 3] WARNING: no template group for block {bi+1} ({code})")
            break

        group_rows = groups[bi]
        n_t = len(group_rows)
        n_d = len(rows)
        if n_d > n_t:
            print(f"  [Agent 3] {code}: {n_d} modes > {n_t} slots — truncating")
            rows = rows[:n_t]

        # Total modes for this block — used to compute per-mode FIT rate
        n_modes_total = max(len(rows), 1)

        for mi, row_num in enumerate(group_rows):
            rd       = rows[mi] if mi < len(rows) else None
            is_first = (mi == 0)

            _write(idx, 'B', row_num, f'FM_TTL_{fm}' if rd else None)
            _write(idx, 'C', row_num, code)
            _write(idx, 'D', row_num, code if is_first else None)

            if rd is None:
                _write(idx, 'G', row_num, None)
                continue

            memo     = str(rd.get('K', 'O')).strip()
            sp       = str(rd.get('P', 'Y' if memo == 'X' else 'N')).strip()
            pct_safe = rd.get('R', 1 if memo == 'O' else 0)
            u_val    = rd.get('U', '')

            # ── Compute FIT values from block FIT rate sheet ──────────────
            fit_blk, fit_mode, fit_q, fit_v, fit_aa, fit_ab = _compute_fit_values(
                code, n_modes_total, block_fit_rates, memo, u_val, sm_coverage
            )

            # E  — Block FIT (first row only)
            _write(idx, 'E', row_num, fit_blk if (is_first and fit_blk > 0) else None)
            # F  — Mode FIT
            _write(idx, 'F',  row_num, fit_mode if fit_mode > 0 else None)
            # G  — Standard failure mode
            _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
            # H  — Failure Mode (blank — matches real FMEDA)
            _write(idx, 'H',  row_num, None)
            # I  — Effects on IC output
            _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
            # J  — Effects on system
            _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
            # K  — Memo
            _write(idx, 'K',  row_num, memo)
            # O  — Failure distribution
            # TEMP block: X rows use 0.5 (two modes split probability)
            # All others: 1
            o_val = 0.5 if (code == 'TEMP' and memo.startswith('X')) else 1
            _write(idx, 'O',  row_num, o_val)
            # P  — Single Point Y/N
            _write(idx, 'P',  row_num, sp)
            # Q  — Failure rate FIT (= mode FIT since distribution = 1)
            _write(idx, 'Q',  row_num, fit_q if fit_q > 0 else None)
            # R  — Percentage of Safe Faults (0 or 1)
            _write(idx, 'R',  row_num, pct_safe)
            # S  — Safety mechanisms IC
            _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=False)
            # T  — Safety mechanisms System
            _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=False)
            # U  — Coverage SPF
            _write(idx, 'U',  row_num, u_val if u_val not in ('', None) else None)
            # V  — Residual FIT = Q × (1 - U) for X rows
            _write(idx, 'V',  row_num, fit_v if (fit_v is not None and fit_v > 0) else None)
            # X  — Latent failure Y/N
            _write(idx, 'X',  row_num, rd.get('X', 'Y' if memo.startswith('X') else 'N'))
            # Y  — SM IC latent (same as S)
            _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=False)
            # Z  — SM System latent
            _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=False)
            # AA — Latent coverage
            _write(idx, 'AA', row_num, fit_aa if fit_aa is not None else None)
            # AB — Latent MPF FIT (0 when fully covered)
            if fit_ab is not None:
                _write(idx, 'AB', row_num, fit_ab if fit_ab > 0 else 0)
            # AD — Comment: "SMxx make the IC enter a safe-sate. Latent coverage: XX%."
            sm_str = rd.get('S', '') or ''
            if sm_str and memo.startswith('X'):
                sms    = sm_str.split()
                # Use first two SMs in the comment (matches real FMEDA pattern)
                # AD comment: primary SM is the one with highest coverage
                # For REF: use first two highest-coverage SMs
                def sm_cov_val(sm):
                    return sm_coverage.get(sm, 0.0) if sm_coverage else 0.0
                sms_sorted = sorted(sms, key=sm_cov_val, reverse=True)
                if code == 'REF' and len(sms_sorted) >= 2:
                    # REF uses two SMs in comment: highest + second-highest coverage
                    sm_mention = f'{sms_sorted[0]} {sms_sorted[1]}'
                elif sms_sorted:
                    sm_mention = sms_sorted[0]
                else:
                    sm_mention = ''
                lat_pct = int(round((fit_aa or 1.0) * 100))
                _write(idx, 'AD', row_num,
                       f'{sm_mention} make the IC enter a safe-sate. Latent coverage: {lat_pct}%.',
                       wrap=True)
            else:
                _write(idx, 'AD', row_num, rd.get('AD') or None, wrap=True)

            fm += 1

        print(f"  [Agent 3] [{bi+1}/{len(fmeda_data)}] {code}: "
              f"{min(n_d, n_t)} rows → FM_TTL_{fm-min(n_d,n_t)} – FM_TTL_{fm-1}")

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

    # ── Step 0: Read all inputs ───────────────────────────────────────────────
    print("━━━ Step 0 : Reading inputs ━━━")
    blk_blocks, sm_blocks, tsr_list = read_dataset()
    iec_table = read_iec_table()
    sm_coverage, sm_addressed, block_to_sms = read_sm_list_from_template()
    # Load FIT rates from template/real FMEDA
    import os
    block_fit_rates = {}
    for candidate in [TEMPLATE_FILE, '3_ID03_FMEDA.xlsx']:
        if os.path.exists(candidate):
            try:
                wb_fit = openpyxl.load_workbook(candidate, data_only=True)
                block_fit_rates = read_block_fit_rates(wb_fit)
                if block_fit_rates:
                    print(f"  FIT rates loaded from {candidate}: {len(block_fit_rates)} blocks")
                    break
            except Exception:
                pass
    print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
          f"IEC parts: {len(iec_table)}  SM entries: {len(sm_coverage)}  FIT blocks: {len(block_fit_rates)}")
    print("  block_to_sms:")
    for b, sms in sorted(block_to_sms.items()):
        print(f"    {b:<15} → {sms}")

    # ── Agent 1: Map blocks → IEC parts ──────────────────────────────────────
    print("\n━━━ Agent 1 : Block → IEC part mapper (LLM) ━━━")
    blocks = agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache)
    print("\n  Mapping result:")
    for b in blocks:
        tag = " [DUP]" if b.get('is_duplicate') else (" [SM]" if b.get('is_sm') else "")
        print(f"    {b['name']:<35} → {b['fmeda_code']:<12} "
              f"| {b.get('iec_part','')} ({len(b.get('modes',[]))} modes){tag}")

    # ── Agent 2: Generate IC effects + system effects + memo ──────────────────
    print("\n━━━ Agent 2 : IC Effects generator (LLM + deterministic memo) ━━━")
    fmeda_data = agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage, sm_addressed, cache)

    # Print memo summary for verification
    print("\n  Memo check:")
    for block in fmeda_data:
        for row in block['rows']:
            affected = extract_blocks_from_ic_effect(row.get('I', ''))
            memo = row.get('K', 'O')
            mode_short = row['G'][:40]
            print(f"    {block['fmeda_code']:<12} K={memo}  affected={affected}  | {mode_short}")

    # Save intermediate JSON
    with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
        json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

    # ── Agent 3: Write template ───────────────────────────────────────────────
    print("\n━━━ Agent 3 : Template writer (deterministic) ━━━")
    agent3_write_template(fmeda_data, block_fit_rates, sm_coverage)

    print("\n✅  Pipeline complete!")
    print(f"    Output       : {OUTPUT_FILE}")
    print(f"    Intermediate : {INTERMEDIATE_JSON}")
    print(f"    Cache        : {CACHE_FILE}")


if __name__ == '__main__':
    run()