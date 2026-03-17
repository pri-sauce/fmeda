
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
#     Read SM list sheet from FMEDA_TEMPLATE.xlsx.
#     Returns:
#       sm_list        — list of {sm_code, addressed_parts[], description}
#       block_to_sms   — dict {block_code: [SM codes that cover it]}
#     """
#     wb = openpyxl.load_workbook(TEMPLATE_FILE)

#     # Try template first (has placeholders); fall back to real FMEDA for structure
#     # The template SM list has placeholders — we need the real data from the dataset SM sheet
#     # Actually we already have it from 3_ID03 structure, so we read it from template
#     # BUT template is blank (placeholders). We need structure from real FMEDA instead.
#     # Strategy: read real FMEDA SM list if available, else build from dataset SM sheet.

#     sm_list = []

#     # Try reading from the real FMEDA (if user put it next to the script)
#     import os
#     for candidate in ['3_ID03_FMEDA.xlsx']:
#         if os.path.exists(candidate):
#             wb_real = openpyxl.load_workbook(candidate, data_only=True)
#             if 'SM list' in wb_real.sheetnames:
#                 ws = wb_real['SM list']
#                 for row in ws.iter_rows(min_row=12, max_row=ws.max_row):
#                     cells = {c.column_letter: c.value for c in row if c.value}
#                     if 'C' in cells and 'E' in cells:
#                         sm_code = str(cells['C']).strip()
#                         parts_raw = str(cells['E']).strip()
#                         parts = [p.strip() for p in re.split(r'[,;]', parts_raw) if p.strip()]
#                         sm_list.append({'sm_code': sm_code, 'addressed_parts': parts})
#                 print(f"  Read SM list from {candidate}: {len(sm_list)} entries")
#                 break

#     # Fallback: build from dataset SM sheet + known mapping
#     if not sm_list:
#         sm_list = _build_sm_list_from_knowledge()
#         print(f"  Built SM list from internal knowledge: {len(sm_list)} entries")

#     # Build reverse index: block_code → [SM codes]
#     block_to_sms = {}
#     for s in sm_list:
#         for part in s['addressed_parts']:
#             # Normalize: SW_BANK_x, SW_BANKx → SW_BANK
#             part_norm = re.sub(r'SW_BANK[_x\d]*', 'SW_BANK', part).strip()
#             part_norm = re.sub(r'CSNS|CNSN|CS', 'CSNS', part_norm)
#             if part_norm:
#                 block_to_sms.setdefault(part_norm, [])
#                 if s['sm_code'] not in block_to_sms[part_norm]:
#                     block_to_sms[part_norm].append(s['sm_code'])

#     return sm_list, block_to_sms


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


# def agent2_generate_effects(blocks, tsr_list, block_to_sms, cache):
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

#         rows = _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms)
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


# def _llm_block_effects(block, chip_ctx, tsr_ctx, modes, block_to_sms):
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
#             # Determine memo using SM list (deterministic, not LLM)
#             memo, matching_sms = determine_memo(ic, block_to_sms)
#             rows.append(_build_row(modes[i], ic, sys_, memo))
#         return rows

#     print(f"    LLM parse failed for {code} — using fallback")
#     return _fallback_rows(modes, block_to_sms)


# def _build_row(canonical_mode, ic, sys_, memo):
#     """
#     col P  (Single Point Failure mode): Y if K=X, N if K=O
#     col R  (Percentage of Safe Faults): 0 if IC effect has anything, 1 if "No effect"
#             — R is expressed as 0 or 1 here; Excel template formats as 0% / 100%
#     """
#     # Enforce consistency: No effect → always O
#     ic_clean = ic.strip()
#     if ic_clean in ('No effect', ''):
#         memo = 'O'

#     # col P: Single Point — Y if memo X, N if memo O
#     sp = 'Y' if memo.startswith('X') else 'N'

#     # col R: Percentage of Safe Faults
#     #   0%   (value=0) if IC effect is NOT "No effect" (i.e. something is affected)
#     #   100% (value=1) if IC effect IS "No effect"
#     pct_safe = 1 if ic_clean == 'No effect' else 0

#     return {
#         'G': canonical_mode,
#         'I': ic,
#         'J': sys_,
#         'K': memo,
#         'O': 1,
#         'P': sp,       # col P: Single Point Failure mode (Y/N)
#         'R': pct_safe, # col R: Percentage of Safe Faults (0=0%, 1=100%)
#         'S': '', 'T': '', 'U': '', 'V': '',
#         'X': sp,       # Latent Y/N mirrors single point
#         'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': '',
#     }


# def _fallback_rows(modes, block_to_sms):
#     SAFE = ['spike','oscillation within','start-up','jitter','duty cycle',
#             'quiescent','settling','false detection']
#     rows = []
#     for mode in modes:
#         safe = any(k in mode.lower() for k in SAFE)
#         ic = 'No effect' if safe else ''
#         memo, _ = determine_memo(ic, block_to_sms)
#         rows.append(_build_row(mode, ic, 'No effect' if safe else '', memo))
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
#             _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=True)
#             _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=True)
#             v = rd.get('U', '')
#             _write(idx, 'U',  row_num, v if v not in ('', None) else None)
#             _write(idx, 'V',  row_num, None)                               # Residual FIT — formula
#             _write(idx, 'X',  row_num, rd.get('X', sp))
#             _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=True)
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
#     sm_list, block_to_sms = read_sm_list_from_template()
#     print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
#           f"IEC parts: {len(iec_table)}  SM→block mappings: {len(block_to_sms)}")
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
#     fmeda_data = agent2_generate_effects(blocks, tsr_list, block_to_sms, cache)

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

  Step 4 — SAFE MODES (always No effect):
    If the failure mode is: "affected by spikes", "oscillation within expected range",
    "incorrect start-up time", "jitter too high", "incorrect duty cycle",
    "quiescent current exceeding", "incorrect settling time"
    → col I = "No effect" (these are local disturbances, don't propagate)

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
            memo, _ = determine_memo(ic, block_to_sms)
            rows.append(_build_row(modes[i], ic, sys_, memo, block_to_sms, sm_coverage))
        return rows

    print(f"    LLM parse failed for {code} — using fallback")
    return _fallback_rows(modes, block_to_sms, sm_coverage, sm_addressed)


def compute_sm_columns(ic_effect, block_to_sms, sm_coverage):
    """
    Given col I IC effect string:
      col S / col Y : space-separated SM codes whose addressed parts match blocks in col I
      col U         : highest coverage value among those SMs (one of 0.99, 0.9, 0.6)
    Returns (sm_string, coverage_value)
    """
    if not ic_effect or ic_effect.strip() == 'No effect':
        return '', ''

    # Extract block codes from bullet list
    affected_blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
    # Normalize variants
    norm = []
    for b in affected_blocks:
        b = b.strip().upper()
        b = re.sub(r'SW_BANK[_X\d]*', 'SW_BANK', b)
        b = re.sub(r'CSNS|CNSN|CS', 'CSNS', b)
        if b not in ('NONE', 'VEGA', ''):
            norm.append(b)

    if not norm:
        return '', ''

    # Find all matching SMs
    matching_sms = []
    for block in norm:
        for sm in block_to_sms.get(block, []):
            if sm not in matching_sms:
                matching_sms.append(sm)

    if not matching_sms:
        return '', ''

    # Sort numerically for clean output: SM01 SM08 SM15 ...
    def sm_sort_key(s):
        m = re.search(r'(\d+)', s)
        return int(m.group(1)) if m else 0
    matching_sms.sort(key=sm_sort_key)

    sm_string = ' '.join(matching_sms)

    # col U: highest coverage among matching SMs
    # Only valid values: 0.99, 0.9, 0.6
    valid = [0.99, 0.9, 0.6]
    coverages = [sm_coverage.get(sm, 0.9) for sm in matching_sms]
    # Round to nearest valid value
    def nearest_valid(v):
        return min(valid, key=lambda x: abs(x - v))
    rounded = [nearest_valid(c) for c in coverages]
    max_cov = max(rounded) if rounded else 0.9

    return sm_string, max_cov


def _build_row(canonical_mode, ic, sys_, memo, block_to_sms=None, sm_coverage=None):
    """
    col P  (Single Point Failure mode): Y if K=X, N if K=O
    col R  (Percentage of Safe Faults): 0 if IC has any effect, 1 if No effect
    col S  : SM codes whose addressed parts match blocks in col I
    col U  : highest coverage value from those SMs (0.99 / 0.9 / 0.6)
    col Y  : same as col S (latent SM coverage = same mechanisms)
    """
    ic_clean = ic.strip()
    if ic_clean in ('No effect', ''):
        memo = 'O'

    sp       = 'Y' if memo.startswith('X') else 'N'
    pct_safe = 1 if ic_clean == 'No effect' else 0

    # Compute S, U, Y
    sm_str, coverage = '', ''
    if block_to_sms and sm_coverage and ic_clean != 'No effect':
        sm_str, coverage = compute_sm_columns(ic_clean, block_to_sms, sm_coverage)

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
        'X': sp,
        'Y': sm_str,    # col Y: same as S (latent SM = same mechanisms)
        'Z': '', 'AA': '', 'AB': '', 'AD': '',
    }


def _fallback_rows(modes, block_to_sms, sm_coverage=None, sm_addressed=None):
    SAFE = ['spike','oscillation within','start-up','jitter','duty cycle',
            'quiescent','settling','false detection']
    rows = []
    for mode in modes:
        safe = any(k in mode.lower() for k in SAFE)
        ic   = 'No effect' if safe else ''
        memo, _ = determine_memo(ic, block_to_sms)
        rows.append(_build_row(mode, ic, 'No effect' if safe else '', memo,
                               block_to_sms, sm_coverage))
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
# AGENT 3  —  Template Writer  (deterministic)
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
            print(f"  [Agent 3] WARNING: no template group for block {bi+1} ({code})")
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
            _write(idx, 'E', row_num, None)   # Block FIT — formula/engineer fills

            if rd is None:
                _write(idx, 'G', row_num, None)
                continue

            memo     = str(rd.get('K', 'O')).strip()
            sp       = str(rd.get('P', 'Y' if memo.startswith('X') else 'N')).strip()
            pct_safe = rd.get('R', 1 if memo == 'O' else 0)

            _write(idx, 'F',  row_num, None)                               # Mode FIT — formula
            _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
            _write(idx, 'H',  row_num, None)                               # Failure Mode — blank
            _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
            _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
            _write(idx, 'K',  row_num, memo)
            _write(idx, 'O',  row_num, 1)
            _write(idx, 'P',  row_num, sp)
            _write(idx, 'Q',  row_num, None)                               # Failure rate — formula
            _write(idx, 'R',  row_num, pct_safe)
            _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=False)
            _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=False)
            # col U: coverage value — write as decimal (0.99 / 0.9 / 0.6)
            v = rd.get('U', '')
            _write(idx, 'U',  row_num, v if v not in ('', None, '') else None)
            _write(idx, 'V',  row_num, None)                               # Residual FIT — formula
            _write(idx, 'X',  row_num, rd.get('X', sp))
            _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=False)
            _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=True)
            v = rd.get('AA', '')
            _write(idx, 'AA', row_num, v if v not in ('', None) else None)
            _write(idx, 'AB', row_num, None)                               # Latent FIT — formula
            _write(idx, 'AD', row_num, rd.get('AD') or None,     wrap=True)

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
    print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
          f"IEC parts: {len(iec_table)}  SM entries: {len(sm_coverage)}  SM→block mappings: {len(block_to_sms)}")
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
    agent3_write_template(fmeda_data)

    print("\n✅  Pipeline complete!")
    print(f"    Output       : {OUTPUT_FILE}")
    print(f"    Intermediate : {INTERMEDIATE_JSON}")
    print(f"    Cache        : {CACHE_FILE}")


if __name__ == '__main__':
    run()