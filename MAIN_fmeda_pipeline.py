# """
# fmeda_agents_v4.py  —  Multi-Agent FMEDA Pipeline  (v4 — targeting 90% accuracy)
# ====================================================================================

# CHANGES vs v3 / v2:

#   FIX 1 - SW_BANK col G (was 0%): All four SW_BANK blocks now receive exact
#     driver-specific mode descriptions ('Driver is stuck in ON or OFF state', etc.)
#     via SW_BANK_MODES override list, instead of generic IEC signal-level language.

#   FIX 2 - CSNS col G (was 25%): CSNS now uses the correct 8-mode op-amp
#     sequence (stuck -> float -> incorrect -> accuracy -> spikes -> oscillation ->
#     start-up -> quiescent) via CSNS_MODES override, eliminating the offset.

#   FIX 3 - ADC K/P/X regression (was 62%): _lookup_k_override now strictly
#     enforces that ONLY stuck/floating ADC modes are safety-violating (K=X).
#     All other ADC modes (offset, full-scale, linearity, monotonic, settling,
#     accuracy) are forced to K=O. Reverts the v3 regression.

#   FIX 4 - LOGIC K (FM_TTL_63 'floating' was O, must be X): All three LOGIC
#     modes (stuck, float, incorrect output) are now explicitly forced to K=X.

#   FIX 5 - SW_BANK K/P/X rules: stuck/float/res_high/res_low -> K=X;
#     timing (turn-on/turn-off) -> K=O. Removes the bidirectional guessing.

#   RETAINED: All v2/v3 improvements (deterministic J, runtime J learning,
#     SM row loading from reference, FIT rate detection, etc.)
# """

# import json, re, time, shutil, sys, os
# import pandas as pd
# import openpyxl
# import requests
# from openpyxl.styles import Alignment

# # ─── CONFIG ───────────────────────────────────────────────────────────────────
# DATASET_FILE      = 'fusa_ai_agent_mock_data.xlsx'
# BLK_SHEET         = 'BLK'
# SM_SHEET          = 'SM'
# TSR_SHEET         = 'TSR'
# IEC_TABLE_FILE    = 'pdf_extracted.json'
# TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
# REFERENCE_FMEDA   = '3_ID03_FMEDA.xlsx'   # human-made reference used to learn J patterns
# OUTPUT_FILE       = 'FMEDA_filled.xlsx'
# CACHE_FILE        = 'fmeda_cache.json'
# INTERMEDIATE_JSON = 'fmeda_intermediate.json'

# OLLAMA_URL     = 'http://localhost:11434/api/generate'
# OLLAMA_MODEL   = 'qwen3:30b'
# OLLAMA_TIMEOUT = 300
# SKIP_CACHE     = False
# # ──────────────────────────────────────────────────────────────────────────────


# # ═══════════════════════════════════════════════════════════════════════════════
# # J-VALUE LOOKUP TABLE
# # ═══════════════════════════════════════════════════════════════════════════════
# #
# # Built by analysing 3_ID03_FMEDA.xlsx row-by-row.
# # Structure:
# #   BLOCK_J_MAP[(fmeda_code, j_type)] = "J string"
# #
# # j_type is resolved by _j_type(code, mode_str) below.
# # This table is the primary source; the LLM is NEVER used for col J.

# BLOCK_J_MAP = {
#     # ── REF ──────────────────────────────────────────────────────────────────
#     ('REF', 'stuck'):          'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('REF', 'float'):          'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('REF', 'incorrect'):      'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('REF', 'accuracy'):       'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('REF', 'safe'):           'No effect',

#     # ── BIAS ─────────────────────────────────────────────────────────────────
#     ('BIAS', 'stuck'):         'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('BIAS', 'float'):         'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('BIAS', 'incorrect'):     'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('BIAS', 'accuracy'):      'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('BIAS', 'safe'):          'No effect',

#     # ── LDO ──────────────────────────────────────────────────────────────────
#     ('LDO', 'ov'):             'Fail-safe mode active\nNo communication',
#     ('LDO', 'uv'):             'Fail-safe mode active\nNo communication',
#     ('LDO', 'accuracy'):       'Fail-safe mode active\nNo communication',
#     ('LDO', 'safe'):           'No effect',
#     ('LDO', 'default'):        'Fail-safe mode active\nNo communication',

#     # ── OSC ──────────────────────────────────────────────────────────────────
#     ('OSC', 'stuck'):          'Fail-safe mode active\nNo communication',
#     ('OSC', 'float'):          'Fail-safe mode active\nNo communication',
#     ('OSC', 'incorrect'):      'Fail-safe mode active\nNo communication',
#     ('OSC', 'drift'):          'Fail-safe mode active\nNo communication',
#     ('OSC', 'duty_cycle'):     'No effect',   # ← KEY FIX: duty cycle is safe
#     ('OSC', 'jitter'):         'No effect',
#     ('OSC', 'safe'):           'No effect',

#     # ── TEMP ─────────────────────────────────────────────────────────────────
#     ('TEMP', 'stuck'):         'Unintentional LED ON',
#     ('TEMP', 'float'):         'Unintentional LED ON\nPossible device damage',
#     ('TEMP', 'incorrect'):     'Unintentional LED ON\nPossible device damage',
#     ('TEMP', 'accuracy'):      'Unintentional LED ON\nPossible device damage',
#     ('TEMP', 'safe'):          'No effect',

#     # ── CSNS ─────────────────────────────────────────────────────────────────
#     # CSNS always K=O, J=No effect regardless of mode
#     ('CSNS', 'default'):       'No effect',
#     ('CSNS', 'safe'):          'No effect',

#     # ── ADC ──────────────────────────────────────────────────────────────────
#     ('ADC', 'stuck'):          'Unintentional LED ON',
#     ('ADC', 'float'):          'Unintentional LED ON',
#     ('ADC', 'default'):        'No effect',   # accuracy / offset / linearity
#     ('ADC', 'safe'):           'No effect',

#     # ── CP ───────────────────────────────────────────────────────────────────
#     ('CP', 'ov'):              'Device damage',
#     ('CP', 'uv'):              'Unintentional LED ON',
#     ('CP', 'safe'):            'No effect',
#     ('CP', 'default'):         'No effect',

#     # ── LOGIC ────────────────────────────────────────────────────────────────
#     ('LOGIC', 'stuck'):        'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('LOGIC', 'float'):        'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('LOGIC', 'incorrect'):    'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
#     ('LOGIC', 'safe'):         'No effect',

#     # ── INTERFACE ────────────────────────────────────────────────────────────
#     # All INTERFACE modes → Fail-safe mode active, K=O
#     ('INTERFACE', 'default'):  'Fail-safe mode active',
#     ('INTERFACE', 'safe'):     'Fail-safe mode active',

#     # ── TRIM ─────────────────────────────────────────────────────────────────
#     ('TRIM', 'omission'):      'Fail-safe mode active\nNo communication',
#     ('TRIM', 'commission'):    'Fail-safe mode active\nNo communication',
#     ('TRIM', 'incorrect'):     'Fail-safe mode active\nNo communication',
#     ('TRIM', 'safe'):          'No effect',
#     ('TRIM', 'default'):       'Fail-safe mode active\nNo communication',

#     # ── SW_BANK (any bank: SW_BANK_1 … SW_BANK_N) ────────────────────────────
#     # K values: stuck/float/res_high/res_low → X; timing → O
#     ('SW_BANK', 'stuck'):      'Unintended LED ON/OFF',
#     ('SW_BANK', 'float'):      'Unintended LED ON',
#     ('SW_BANK', 'res_high'):   'Unintended LED ON',
#     ('SW_BANK', 'res_low'):    'No effect',   # K=X but J=No effect (see real FMEDA R101/107/113/119)
#     ('SW_BANK', 'timing'):     'No effect',   # K=O
#     ('SW_BANK', 'safe'):       'No effect',
# }

# # K-value overrides for SW_BANK res_low (J="No effect" but K=X)
# _SW_BANK_RES_LOW_K = 'X'   # res_low is hazardous but no visible J symptom


# def _j_type(code: str, mode_str: str) -> str:
#     """
#     Classify a failure mode string into a j_type key for BLOCK_J_MAP lookup.
#     Works for ANY block — no hardcoded block names.
#     """
#     m = mode_str.lower()

#     # Safe / no-propagation modes (always J=No effect)
#     safe_keywords = [
#         'spike', 'oscillation within', 'incorrect start-up', 'start-up time',
#         'jitter', 'quiescent current', 'settling time', 'false detection',
#         'oscillation within the', 'within the prescribed', 'within the expected',
#         'fast oscillation',
#     ]
#     if any(k in m for k in safe_keywords):
#         return 'safe'

#     # OSC duty cycle / jitter are safe
#     if 'duty cycle' in m:
#         return 'duty_cycle'
#     if 'jitter' in m:
#         return 'jitter'

#     # SW_BANK resistance modes — check before generic stuck/float
#     if 'resistance too high' in m:
#         return 'res_high'
#     if 'resistance too low' in m:
#         return 'res_low'

#     # Timing (SW_BANK turn-on/turn-off) — check before stuck
#     if 'turn-on time' in m or 'turn-off time' in m or 'turn on' in m or 'turn off' in m:
#         return 'timing'

#     # Stuck / floating
#     # IMPORTANT: "not including stuck or floating" is an EXCLUSION phrase used in
#     # ADC/offset/linearity modes — must NOT classify those as stuck/float.
#     _stuck_exclusion = 'not including stuck'
#     if 'stuck' in m and _stuck_exclusion not in m:
#         return 'stuck'
#     if ('floating' in m or 'open circuit' in m or 'tri-state' in m or 'tri-stated' in m) \
#             and 'not including' not in m:
#         return 'float'

#     # Voltage threshold failures
#     if any(k in m for k in ['higher than a high threshold', 'over voltage', '— ov',
#                               'overvoltage', 'output voltage higher']):
#         return 'ov'
#     if any(k in m for k in ['lower than a low threshold', 'under voltage', '— uv',
#                               'undervoltage', 'output voltage lower']):
#         return 'uv'

#     # Drift
#     if 'drift' in m:
#         return 'drift'

#     # TRIM specific
#     if 'error of omission' in m or 'not triggered when it should' in m:
#         return 'omission'
#     if 'error of comission' in m or 'error of commission' in m or "triggered when it shouldn" in m:
#         return 'commission'

#     # Accuracy / incorrect value
#     if any(k in m for k in ['accuracy too low', 'accuracy error', 'incorrect output voltage',
#                               'incorrect output', 'incorrect reference', 'incorrect frequency',
#                               'incorrect signal swing', 'outside the expected range',
#                               'outside the prescribed']):
#         return 'accuracy' if 'accuracy' in m else 'incorrect'

#     # Generic incorrect
#     if 'incorrect' in m:
#         return 'incorrect'

#     return 'default'


# def _lookup_j(code: str, mode_str: str) -> str | None:
#     """
#     Look up the J value for (code, mode_str).
#     Returns None if not found (caller should use LLM fallback or 'No effect').
#     Handles SW_BANK_N → SW_BANK normalisation automatically.
#     """
#     j_type = _j_type(code, mode_str)

#     # Normalise SW_BANK variants
#     lookup_code = code
#     if re.match(r'SW_BANK', code, re.IGNORECASE):
#         lookup_code = 'SW_BANK'

#     # Try (code, j_type) first, then (code, 'default')
#     j = BLOCK_J_MAP.get((lookup_code, j_type))
#     if j is None:
#         j = BLOCK_J_MAP.get((lookup_code, 'default'))
#     return j


# def _lookup_k_override(code: str, mode_str: str) -> str | None:
#     """
#     Return a K override if the mode has special K logic that differs from the
#     standard SM-list determination.  Returns None = use standard logic.

#     Rules derived from 3_ID03_FMEDA.xlsx ground truth:
#       - SW_BANK stuck/float/res_high/res_low → X  (safety violation)
#       - SW_BANK timing (turn-on/turn-off)    → O  (non-safety)
#       - ADC stuck/float                      → X  (SM-list decides)
#       - ADC everything else                  → O  (offset/linearity/etc. are not safety-critical)
#       - CSNS / INTERFACE                     → always O
#       - OSC duty_cycle / jitter              → O
#       - LOGIC stuck/float/incorrect          → X
#       - Safe modes (spikes, oscillation…)    → O
#     """
#     j_type = _j_type(code, mode_str)
#     lookup_code = 'SW_BANK' if re.match(r'SW_BANK', code, re.IGNORECASE) else code

#     # ── SW_BANK ──────────────────────────────────────────────────────────────
#     if lookup_code == 'SW_BANK':
#         if j_type in ('stuck', 'float', 'res_high', 'res_low'):
#             return 'X'
#         if j_type == 'timing':
#             return 'O'
#         if j_type == 'safe':
#             return 'O'
#         return 'O'   # any other SW_BANK mode defaults to non-safety

#     # ── OSC ──────────────────────────────────────────────────────────────────
#     if code == 'OSC' and j_type in ('duty_cycle', 'jitter', 'safe'):
#         return 'O'

#     # ── CSNS — always non-safety ──────────────────────────────────────────────
#     if code == 'CSNS':
#         return 'O'

#     # ── INTERFACE — always non-safety ────────────────────────────────────────
#     if code == 'INTERFACE':
#         return 'O'

#     # ── ADC: ONLY stuck / floating → safety-violating; ALL others → O ────────
#     # This covers: accuracy, offset, full-scale, linearity, monotonic, settling
#     if code == 'ADC':
#         if j_type in ('stuck', 'float'):
#             return None   # let standard SM-list logic determine (will give X)
#         return 'O'

#     # ── LOGIC: all three real modes → K=X ────────────────────────────────────
#     if code == 'LOGIC':
#         if j_type in ('stuck', 'float', 'incorrect'):
#             return 'X'
#         return 'O'

#     # ── Universal safe-mode catch ─────────────────────────────────────────────
#     if j_type == 'safe':
#         return 'O'

#     return None


# # ═══════════════════════════════════════════════════════════════════════════════
# # REFERENCE FMEDA READER — learns J patterns from human-made reference at runtime
# # ═══════════════════════════════════════════════════════════════════════════════

# def learn_j_from_reference(ref_path: str) -> dict:
#     """
#     Read the human reference FMEDA and return a dict:
#       { (fmeda_code, mode_str_lower): J_string }

#     This supplements BLOCK_J_MAP at runtime. If the chip changes, this
#     function auto-learns the J patterns without code changes.
#     """
#     learned = {}
#     if not os.path.exists(ref_path):
#         return learned

#     try:
#         wb = openpyxl.load_workbook(ref_path, data_only=True)
#         if 'FMEDA' not in wb.sheetnames:
#             return learned
#         ws = wb['FMEDA']

#         current_code = None
#         for row in ws.iter_rows(min_row=20, max_row=ws.max_row):
#             row_data = {}
#             for c in row:
#                 if hasattr(c, 'column_letter') and c.value is not None:
#                     row_data[c.column_letter] = c.value

#             # D col = block code (only in first row of each block)
#             if 'D' in row_data and str(row_data['D']).strip():
#                 current_code = str(row_data['D']).strip()

#             g_val = str(row_data.get('G', '')).strip()
#             j_val = str(row_data.get('J', '')).strip()

#             if current_code and g_val and j_val:
#                 key = (current_code, g_val.lower())
#                 learned[key] = j_val

#         print(f"  [J-learn] Loaded {len(learned)} J patterns from {ref_path}")
#     except Exception as e:
#         print(f"  [J-learn] Could not read reference: {e}")

#     return learned


# def resolve_j(code: str, mode_str: str, learned_j: dict) -> str:
#     """
#     Resolve the J value using:
#       1. Exact match in learned_j  (from reference FMEDA)
#       2. BLOCK_J_MAP lookup  (j_type classification)
#       3. Fallback: 'No effect'
#     """
#     # 1. Exact match from reference FMEDA
#     exact_key = (code, mode_str.lower())
#     if exact_key in learned_j:
#         return learned_j[exact_key]

#     # 2. j_type table
#     j = _lookup_j(code, mode_str)
#     if j is not None:
#         return j

#     # 3. Fallback
#     return 'No effect'


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


# def read_block_fit_rates(wb):
#     """
#     Read block FIT rates from 'Core Block FIT rate' sheet.
#     Returns dict: { 'REF': 0.0509, ... }
#     Dynamically finds the correct columns — not hardcoded.
#     """
#     fit_rates = {}
#     try:
#         ws = wb['Core Block FIT rate']
#         # Find header row to locate block-code and FIT columns
#         header_row = None
#         block_col = 'B'
#         fit_col   = 'L'
#         for row in ws.iter_rows(min_row=1, max_row=30):
#             for c in row:
#                 if c.value and 'block' in str(c.value).lower():
#                     header_row = c.row
#                     block_col  = c.column_letter
#                 if c.value and 'fit' in str(c.value).lower() and 'total' in str(c.value).lower():
#                     fit_col = c.column_letter

#         start_row = (header_row + 1) if header_row else 25
#         for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row):
#             row_data = {c.column_letter: c.value for c in row
#                         if hasattr(c, 'column_letter') and c.value is not None}
#             if block_col in row_data and fit_col in row_data:
#                 block = str(row_data[block_col]).strip()
#                 try:
#                     fit_rates[block] = float(row_data[fit_col])
#                 except (ValueError, TypeError):
#                     pass
#     except Exception as e:
#         print(f"  WARNING: Could not read FIT rates: {e}")
#     return fit_rates


# def read_sm_list_from_template():
#     """
#     Read SM list from FMEDA_TEMPLATE.xlsx or 3_ID03_FMEDA.xlsx.
#     Returns:
#       sm_coverage  : { 'SM01': 0.99, ... }
#       sm_addressed : { 'SM01': ['REF','LDO'], ... }
#       block_to_sms : { 'REF': ['SM01','SM02',...], ... }
#     """
#     for candidate in [TEMPLATE_FILE, REFERENCE_FMEDA]:
#         if os.path.exists(candidate):
#             try:
#                 wb_try = openpyxl.load_workbook(candidate, data_only=True)
#                 if 'SM list' in wb_try.sheetnames:
#                     cov, addr, b2s = _read_sm_list_from_workbook(wb_try)
#                     if cov:
#                         print(f"  SM list read from: {candidate} ({len(cov)} entries)")
#                         return cov, addr, b2s
#             except Exception:
#                 pass

#     print("  SM list: using built-in knowledge fallback")
#     return _fallback_sm_list()


# def _read_sm_list_from_workbook(wb):
#     ws = wb['SM list']
#     sm_coverage  = {}
#     sm_addressed = {}

#     # Detect SM code column (usually C) and coverage column (usually L)
#     # and addressed-parts column (usually E) dynamically
#     sm_col    = 'C'
#     cov_col   = 'L'
#     parts_col = 'E'

#     for row in ws.iter_rows(min_row=12, max_row=ws.max_row):
#         cells = {c.column_letter: c.value for c in row
#                  if hasattr(c, 'column_letter') and c.value is not None}
#         if sm_col not in cells:
#             continue
#         sm_code = str(cells[sm_col]).strip()
#         if not re.match(r'SM\d+', sm_code):
#             continue

#         try:
#             cov = float(str(cells.get(cov_col, 0.9)))
#         except (ValueError, TypeError):
#             cov = 0.9
#         sm_coverage[sm_code] = cov

#         raw_parts = str(cells.get(parts_col, '')).strip()
#         parts = []
#         for p in re.split(r'[,;]', raw_parts):
#             p = p.strip()
#             p = re.sub(r'SW_BANK[_x\d]*', 'SW_BANK', p)
#             p = re.sub(r'\bCSNS\b|\bCNSN\b|\bCS\b', 'CSNS', p)
#             if p:
#                 parts.append(p)
#         sm_addressed[sm_code] = parts

#     block_to_sms = {}
#     for sm_code, parts in sm_addressed.items():
#         for part in parts:
#             if part:
#                 block_to_sms.setdefault(part, [])
#                 if sm_code not in block_to_sms[part]:
#                     block_to_sms[part].append(sm_code)

#     return sm_coverage, sm_addressed, block_to_sms


# def _fallback_sm_list():
#     """Built-in SM→block mapping when no workbook is available."""
#     raw = [
#         {'sm_code': 'SM01',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
#         {'sm_code': 'SM02',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
#         {'sm_code': 'SM03',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
#         {'sm_code': 'SM04',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
#         {'sm_code': 'SM05',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
#         {'sm_code': 'SM06',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.9},
#         {'sm_code': 'SM08',  'addressed_parts': ['CSNS', 'ADC'],            'cov': 0.9},
#         {'sm_code': 'SM09',  'addressed_parts': ['LOGIC'],                  'cov': 0.99},
#         {'sm_code': 'SM10',  'addressed_parts': ['LOGIC'],                  'cov': 0.9},
#         {'sm_code': 'SM11',  'addressed_parts': ['OSC'],                    'cov': 0.6},
#         {'sm_code': 'SM12',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.9},
#         {'sm_code': 'SM13',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
#         {'sm_code': 'SM14',  'addressed_parts': ['CP'],                     'cov': 0.99},
#         {'sm_code': 'SM15',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
#         {'sm_code': 'SM16',  'addressed_parts': ['REF', 'ADC'],             'cov': 0.9},
#         {'sm_code': 'SM17',  'addressed_parts': ['TEMP'],                   'cov': 0.9},
#         {'sm_code': 'SM18',  'addressed_parts': ['LOGIC'],                  'cov': 0.99},
#         {'sm_code': 'SM20',  'addressed_parts': ['LDO'],                    'cov': 0.99},
#         {'sm_code': 'SM21',  'addressed_parts': ['LOGIC'],                  'cov': 0.6},
#         {'sm_code': 'SM22',  'addressed_parts': ['CP', 'SW_BANK'],          'cov': 0.99},
#         {'sm_code': 'SM23',  'addressed_parts': ['TEMP'],                   'cov': 0.9},
#         {'sm_code': 'SM24',  'addressed_parts': ['ADC', 'SW_BANK'],         'cov': 0.9},
#     ]
#     cov, addr, b2s = {}, {}, {}
#     for entry in raw:
#         sm = entry['sm_code']
#         parts = entry['addressed_parts']
#         cov[sm]  = entry['cov']
#         addr[sm] = parts
#         for p in parts:
#             b2s.setdefault(p, [])
#             if sm not in b2s[p]:
#                 b2s[p].append(sm)
#     return cov, addr, b2s


# # ═══════════════════════════════════════════════════════════════════════════════
# # SM EFFECT TABLE  (for SM block rows — read from reference FMEDA at runtime)
# # ═══════════════════════════════════════════════════════════════════════════════

# _SM_J_BUILTIN = {
#     'SM01': ('Unintended LED ON',                        'Unintended LED ON'),
#     'SM02': ('Device damage',                            'Device damage'),
#     'SM03': ('Unintended LED ON',                        'Unintended LED ON'),
#     'SM04': ('Unintended LED OFF',                       'Unintended LED OFF'),
#     'SM05': ('Unintended LED OFF',                       'Unintended LED OFF'),
#     'SM06': ('Unintended LED OFF',                       'Unintended LED OFF'),
#     'SM07': ('Unintended LED ON/OFF',                    'Unintended LED ON/OFF'),
#     'SM08': ('Unintended LED ON',                        'Unintended LED ON'),
#     'SM09': ('UART Communication Error',                 'Fail-safe mode active'),
#     'SM10': ('UART Communication Error',                 'Fail-safe mode active'),
#     'SM11': ('UART Communication Error',                 'Fail-safe mode active'),
#     'SM12': ('No PWM monitoring functionality',          'No effect'),
#     'SM13': ('Unintended LED ON/OFF in FS mode',         'Unintended LED ON/OFF in FS mode'),
#     'SM14': ('Unintended LED ON',                        'Unintended LED ON'),
#     'SM15': ('Failures on LOGIC operation',              'Possible Fail-safe mode activation'),
#     'SM16': ('Loss of reference control functionality',  'No effect'),
#     'SM17': ('Device damage',                            'Device damage'),
#     'SM18': ('Cannot trim part properly',                'Performance/Functionality degredation'),
#     'SM20': ('Device damage',                            'Device damage'),
#     'SM21': ('Unsynchronised PWM',                       'No effect'),
#     'SM22': ('Unintended LED OFF',                       'Unintended LED OFF'),
#     'SM23': ('Loss of thermal monitoring capability',    'Possible device damage'),
#     'SM24': ('Loss of LED voltage monitoring capability','No effect'),
# }


# def load_sm_j_from_reference(ref_path: str) -> dict:
#     """
#     Read SM J values from the FMEDA sheet of the reference workbook.
#     Returns { 'SM01': ('col_I', 'col_J'), ... }
#     """
#     sm_j = dict(_SM_J_BUILTIN)  # start with built-in defaults

#     if not os.path.exists(ref_path):
#         return sm_j

#     try:
#         wb = openpyxl.load_workbook(ref_path, data_only=True)
#         ws = wb['FMEDA']
#         current_d = None
#         for row in ws.iter_rows(min_row=20, max_row=ws.max_row):
#             row_data = {}
#             for c in row:
#                 if hasattr(c, 'column_letter') and c.value is not None:
#                     row_data[c.column_letter] = c.value
#             if 'D' in row_data and str(row_data['D']).strip():
#                 current_d = str(row_data['D']).strip()
#             g = str(row_data.get('G', '')).strip().lower()
#             if current_d and re.match(r'SM\d+', current_d) and 'fail to detect' in g:
#                 i_val = str(row_data.get('I', '')).strip()
#                 j_val = str(row_data.get('J', '')).strip()
#                 if i_val or j_val:
#                     sm_j[current_d] = (i_val, j_val)
#     except Exception as e:
#         print(f"  [SM-J] Warning: {e}")

#     return sm_j


# # ═══════════════════════════════════════════════════════════════════════════════
# # MEMO LOGIC  (deterministic — no LLM)
# # ═══════════════════════════════════════════════════════════════════════════════

# _BLOCK_NORM = {
#     'SW_BANKX': 'SW_BANK', 'SW_BANK_X': 'SW_BANK', 'SW_BANKx': 'SW_BANK',
#     'SW_BANK_1': 'SW_BANK', 'SW_BANK_2': 'SW_BANK',
#     'SW_BANK_3': 'SW_BANK', 'SW_BANK_4': 'SW_BANK',
#     'CNSN': 'CSNS', 'CS': 'CSNS',
#     'DIETEMP': 'TEMP',
#     'VEGA': 'CP',
# }


# def _norm_block(code: str) -> str:
#     c = code.strip().upper()
#     return _BLOCK_NORM.get(c, c)


# def extract_blocks_from_ic_effect(ic_effect: str) -> list:
#     if not ic_effect or ic_effect.strip() in ('No effect', ''):
#         return []
#     blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
#     return [_norm_block(b) for b in blocks if b.upper() not in ('NONE', '')]


# def determine_memo(ic_effect: str, block_to_sms: dict,
#                    code: str = '', mode_str: str = '') -> tuple:
#     """
#     Returns (memo, matching_sms_list).
#     Applies K override logic before SM-list check.
#     """
#     # Apply K override first (deterministic rules)
#     k_override = _lookup_k_override(code, mode_str)
#     if k_override == 'O':
#         return 'O', []

#     if not ic_effect or ic_effect.strip() in ('No effect', ''):
#         return 'O', []

#     affected = extract_blocks_from_ic_effect(ic_effect)
#     if not affected:
#         # K override might still force X (e.g. SW_BANK res_low)
#         if k_override == 'X':
#             return 'X', []
#         return 'O', []

#     matching_sms = []
#     for block in affected:
#         for sm in block_to_sms.get(block, []):
#             if sm not in matching_sms:
#                 matching_sms.append(sm)

#     if k_override == 'X':
#         return 'X', matching_sms

#     memo = 'X' if matching_sms else 'O'
#     return memo, matching_sms


# # ═══════════════════════════════════════════════════════════════════════════════
# # PER-BLOCK SM MAP  (determines col S/Y and U)
# # ═══════════════════════════════════════════════════════════════════════════════
# #
# # This is the reference map from 3_ID03_FMEDA.xlsx.
# # Keys use normalised block codes + j_type.  SW_BANK_N all map to SW_BANK entries.

# _BLOCK_SM_MAP = {
#     ('REF',       'stuck'):       'SM01 SM15 SM16 SM17',
#     ('REF',       'float'):       'SM01 SM15 SM16 SM17',
#     ('REF',       'incorrect'):   'SM01 SM15 SM16 SM17',
#     ('REF',       'accuracy'):    'SM01 SM15 SM16',
#     ('REF',       'default'):     'SM01 SM15 SM16 SM17',
#     ('BIAS',      'default'):     'SM11 SM15 SM16',
#     ('LDO',       'ov'):          'SM11 SM20',
#     ('LDO',       'uv'):          'SM11 SM15',
#     ('LDO',       'accuracy'):    'SM11 SM15 SM20',
#     ('LDO',       'default'):     'SM11 SM15 SM20',
#     ('OSC',       'default'):     'SM09 SM10 SM11',
#     ('TEMP',      'default'):     'SM17 SM23',
#     ('CSNS',      'default'):     '',
#     ('ADC',       'stuck'):       'SM08 SM16 SM17 SM23',
#     ('ADC',       'float'):       'SM08 SM16 SM17 SM23',
#     ('ADC',       'default'):     '',
#     ('CP',        'ov'):          '',
#     ('CP',        'uv'):          'SM14 SM22',
#     ('CP',        'default'):     'SM14 SM22',
#     ('LOGIC',     'default'):     'SM10 SM11 SM12 SM18',
#     ('INTERFACE', 'default'):     '',
#     ('TRIM',      'omission'):    'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
#     ('TRIM',      'commission'):  'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
#     ('TRIM',      'incorrect'):   'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
#     ('TRIM',      'default'):     'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
#     ('SW_BANK',   'stuck'):       'SM04 SM05 SM06 SM08',
#     ('SW_BANK',   'float'):       'SM04 SM05 SM06 SM08',
#     ('SW_BANK',   'res_high'):    'SM04 SM06 SM08',
#     ('SW_BANK',   'res_low'):     'SM03 SM06 SM24',
#     ('SW_BANK',   'driver'):      'SM03 SM06 SM24',
#     ('SW_BANK',   'timing'):      '',
#     ('SW_BANK',   'default'):     'SM04 SM06 SM08',
# }


# def compute_sm_columns(ic_effect: str, block_to_sms: dict, sm_coverage: dict,
#                        fmeda_code: str = '', mode_str: str = '') -> tuple:
#     """Returns (sm_string, coverage_value) for col S/Y and col U."""
#     if not ic_effect or ic_effect.strip() == 'No effect':
#         return '', ''

#     j_type = _j_type(fmeda_code, mode_str)
#     norm_code = 'SW_BANK' if re.match(r'SW_BANK', fmeda_code, re.IGNORECASE) else fmeda_code

#     sm_str = (_BLOCK_SM_MAP.get((norm_code, j_type))
#               or _BLOCK_SM_MAP.get((norm_code, 'default')))

#     if sm_str is None:
#         # Fallback: SM-list intersection
#         affected = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
#         normed = []
#         for b in affected:
#             b = b.strip().upper()
#             b = re.sub(r'SW_BANK[_X\d]*', 'SW_BANK', b)
#             b = re.sub(r'CSNS|CNSN|CS', 'CSNS', b)
#             if b not in ('NONE', 'VEGA', ''):
#                 normed.append(b)
#         matching = []
#         for block in normed:
#             for sm in block_to_sms.get(block, []):
#                 if sm not in matching:
#                     matching.append(sm)
#         matching.sort(key=lambda s: int(re.search(r'\d+', s).group()) if re.search(r'\d+', s) else 0)
#         sm_str = ' '.join(matching)

#     if not sm_str:
#         return '', ''

#     valid = [0.99, 0.9, 0.6]
#     def nearest(v):
#         return min(valid, key=lambda x: abs(x - v))

#     coverages = [nearest(sm_coverage.get(sm, 0.9)) for sm in sm_str.split()]
#     max_cov = max(coverages) if coverages else 0.9
#     return sm_str, max_cov


# # ═══════════════════════════════════════════════════════════════════════════════
# # AGENT 1  —  Block → IEC part mapper
# # ═══════════════════════════════════════════════════════════════════════════════

# FMEDA_MODE_OVERRIDES = {
#     'INTERFACE': [
#         'TX: No message transferred as requested',
#         'TX: Message transferred when not requested',
#         'TX: Message transferred too early/late',
#         'TX: Message transferred with incorrect value',
#         'RX: No incoming message processed',
#         'RX: Message transferred when not requested',
#         'RX: Message transferred too early/late',
#         'RX: Message transferred with incorrect value',
#     ],
#     'TRIM': [
#         'Error of omission (i.e. not triggered when it should be)',
#         "Error of comission (i.e. triggered when it shouldn't be)",
#         'Incorrect settling time (i.e. outside the expected range)',
#         'Incorrect output',
#     ],
# }

# # SW_BANK uses driver-specific mode descriptions — NOT generic signal-level IEC language.
# # These 6 modes are identical across SW_BANK_1, SW_BANK_2, SW_BANK_3, SW_BANK_4, etc.
# # Built from 3_ID03_FMEDA.xlsx FM_TTL_77–82 (and repeated for each bank).
# SW_BANK_MODES = [
#     'Driver is stuck in ON or OFF state',
#     'Driver is floating (i.e. open circuit, tri-stated)',
#     'Driver resistance too high when turned on',
#     'Driver resistance too low when turned off',
#     'Driver turn-on time too fast or too slow',
#     'Driver turn-off time too fast or too slow',
# ]

# # CSNS uses the same generic op-amp sequence as REF/TEMP — NOT the shifted
# # op-amp variant the IEC table sometimes produces.
# CSNS_MODES = [
#     'Output is stuck (i.e. high or low)',
#     'Output is floating (i.e. open circuit)',
#     'Incorrect output voltage value (i.e. outside the expected range)',
#     'Output voltage accuracy too low, including drift',
#     'Output voltage affected by spikes',
#     'Output voltage oscillation within the expected range',
#     'Incorrect start-up time (i.e. outside the expected range)',
#     'Quiescent current exceeding the maximum value',
# ]


# def agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache):
#     ck = "agent1__" + json.dumps([b['name'] for b in blk_blocks])
#     if not SKIP_CACHE and ck in cache:
#         print("  [Agent 1] Loaded from cache")
#         result = cache[ck]
#         _append_sm_blocks(result, sm_blocks)
#         return result

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

#     # Replace LLM-generated modes with verbatim IEC table modes
#     iec_idx = {p['part_name']: p['entries'][0]['modes'] for p in iec_table}
#     for b in result:
#         iec_part = b.get('iec_part', '')
#         if iec_part in iec_idx:
#             b['modes'] = iec_idx[iec_part]
#         else:
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

#     # Apply mode overrides — fixed lists for specific block types
#     for b in result:
#         code = b.get('fmeda_code', '')
#         if code in FMEDA_MODE_OVERRIDES:
#             b['modes'] = FMEDA_MODE_OVERRIDES[code]
#         elif re.match(r'SW_BANK', code, re.IGNORECASE):
#             # All SW_BANK_N blocks use identical driver-specific mode descriptions
#             b['modes'] = SW_BANK_MODES
#         elif code == 'CSNS':
#             # CSNS uses the standard op-amp sequence (same as REF/TEMP)
#             b['modes'] = CSNS_MODES

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
# VERIFIED EXAMPLES FROM A REAL AUTOMOTIVE IC FMEDA (col I ONLY):

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


# def agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage,
#                             sm_addressed, cache, learned_j, sm_j_map):
#     """Generate col I (IC effect), col J (system effect), col K (memo) for all blocks."""

#     active = [b for b in blocks if not b.get('is_duplicate') and not b.get('is_sm')]
#     chip_ctx = "\n".join(
#         f"  {b['fmeda_code']:<12} {b['name']:<35} | {b.get('function','')[:80]}"
#         for b in active
#     )

#     tsr_ctx = "\n".join(
#         f"  {t['id']}: {t['description']}"
#         for t in tsr_list
#     ) if tsr_list else "  (no TSR data)"

#     result = []
#     for block in blocks:
#         code  = block['fmeda_code']
#         name  = block['name']
#         modes = block.get('modes', [])

#         # SM blocks — rows filled from sm_j_map
#         if block.get('is_sm'):
#             rows = _sm_rows(code, sm_j_map)
#             result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#             print(f"  [Agent 2] {code:<12} SM — (2 rows)")
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
#             # Re-apply deterministic J correction on cached rows (handles cache from old version)
#             for row in rows:
#                 row['J'] = resolve_j(code, row.get('G', ''), learned_j)
#                 k_override = _lookup_k_override(code, row.get('G', ''))
#                 if k_override:
#                     row['K'] = k_override
#             print(f"  [Agent 2] {code:<12} cache ({len(rows)} rows)")
#             result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#             continue

#         rows = _llm_block_effects(block, chip_ctx, tsr_ctx, modes,
#                                   block_to_sms, sm_coverage, sm_addressed,
#                                   learned_j)
#         cache[ck] = rows
#         save_cache(cache)
#         result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
#         print(f"  [Agent 2] {code:<12} {len(rows)} rows (LLM+deterministic J)")
#         time.sleep(0.3)

#     return result


# _DOWNSTREAM = {
#     'REF':       'BIAS, ADC, TEMP, LDO, OSC — all use the reference voltage',
#     'BIAS':      'ADC, TEMP, LDO, OSC, SW_BANKx, CP, CSNS — all receive bias currents',
#     'LDO':       'OSC (LDO powers the oscillator supply rail)',
#     'OSC':       'LOGIC, INTERFACE — clock signal drives all digital logic',
#     'TEMP':      'ADC (TEMP voltage read by ADC), SW_BANK_x (DIETEMP controls output enable)',
#     'CSNS':      'ADC (CSNS output is digitized by ADC for current monitoring)',
#     'ADC':       'SW_BANK_x (ADC DIETEMP result controls switch enable), LOGIC',
#     'CP':        'SW_BANK_x (charge pump supplies gate drive voltage for all switches)',
#     'LOGIC':     'SW_BANK_X (LOGIC drives all switch banks), OSC',
#     'INTERFACE': 'LOGIC, ADC (SPI writes configure DAC and read ADC results)',
#     'TRIM':      'REF, LDO, BIAS, OSC, SW_BANK, DIETEMP — trim data calibrates all analog blocks',
# }


# def _llm_block_effects(block, chip_ctx, tsr_ctx, modes,
#                        block_to_sms, sm_coverage, sm_addressed, learned_j):
#     code = block['fmeda_code']
#     name = block['name']
#     func = block.get('function', '')
#     n    = len(modes)

#     downstream_hint = _DOWNSTREAM.get(code,
#         'Review all blocks — consider which ones depend on this block output signal')

#     prompt = f"""You are completing an FMEDA table for an automotive IC (ISO 26262 / AEC-Q100).

# {FEW_SHOT}

# ═══════════════════════════════════════════════════
# ALL BLOCKS IN THIS CHIP (fmeda_code | name | function):
# {chip_ctx}
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

# TASK: For each failure mode return ONLY col I (effects on IC output).
# Col J and col K will be determined separately — do NOT include them.

# SAFE MODES (always "No effect" for col I):
#   "affected by spikes", "oscillation within expected range",
#   "incorrect start-up time", "jitter too high", "incorrect duty cycle",
#   "quiescent current exceeding", "incorrect settling time"

# {IC_FORMAT}

# Return a JSON array with EXACTLY {n} objects, same order as failure modes:
# [
#   {{
#     "G": "<exact failure mode string>",
#     "I": "<col I: IC output effect>"
#   }},
#   ...
# ]
# Return ONLY the JSON array:"""

#     raw    = query_llm(prompt, temperature=0.05)
#     parsed = parse_json(raw)

#     rows = []
#     if isinstance(parsed, list) and len(parsed) >= n:
#         for i in range(n):
#             rd   = parsed[i]
#             ic   = str(rd.get('I', 'No effect')).strip()
#             mode = modes[i]

#             # Col J: deterministic lookup (never from LLM)
#             sys_ = resolve_j(code, mode, learned_j)

#             # Col K: deterministic
#             k_override = _lookup_k_override(code, mode)
#             if ic in ('No effect', ''):
#                 memo = 'O'
#             elif k_override is not None:
#                 memo = k_override
#             else:
#                 memo, _ = determine_memo(ic, block_to_sms, code, mode)

#             rows.append(_build_row(mode, ic, sys_, memo, block_to_sms, sm_coverage,
#                                    fmeda_code=code))
#     else:
#         print(f"    LLM parse failed for {code} — using fallback")
#         rows = _fallback_rows(modes, block_to_sms, sm_coverage, sm_addressed,
#                               code, learned_j)

#     return rows


# def _build_row(canonical_mode, ic, sys_, memo, block_to_sms=None, sm_coverage=None, **kwargs):
#     ic_clean = ic.strip()
#     if ic_clean in ('No effect', ''):
#         memo = 'O'

#     # col P: only pure 'X' (not Latent) → Y
#     sp = 'Y' if memo == 'X' else 'N'
#     # col R: safe if K=O
#     pct_safe = 1 if not memo.startswith('X') else 0

#     sm_str, coverage = '', ''
#     if ic_clean != 'No effect':
#         sm_str, coverage = compute_sm_columns(
#             ic_clean, block_to_sms or {}, sm_coverage or {},
#             fmeda_code=kwargs.get('fmeda_code', ''),
#             mode_str=canonical_mode
#         )

#     return {
#         'G': canonical_mode,
#         'I': ic,
#         'J': sys_,
#         'K': memo,
#         'O': 1,
#         'P': sp,
#         'R': pct_safe,
#         'S': sm_str,
#         'T': '',
#         'U': coverage,
#         'V': '',
#         'X': 'Y' if memo.startswith('X') else 'N',
#         'Y': sm_str,
#         'Z': '', 'AA': '', 'AB': '', 'AD': '',
#     }


# def _fallback_rows(modes, block_to_sms, sm_coverage=None, sm_addressed=None,
#                    fmeda_code='', learned_j=None):
#     if learned_j is None:
#         learned_j = {}
#     SAFE_KW = ['spike', 'oscillation within', 'start-up', 'jitter', 'duty cycle',
#                'quiescent', 'settling', 'false detection']
#     rows = []
#     for mode in modes:
#         safe = any(k in mode.lower() for k in SAFE_KW)
#         ic   = 'No effect' if safe else ''
#         sys_ = resolve_j(fmeda_code, mode, learned_j)
#         k_override = _lookup_k_override(fmeda_code, mode)
#         if ic in ('No effect', ''):
#             memo = 'O'
#         elif k_override is not None:
#             memo = k_override
#         else:
#             memo, _ = determine_memo(ic, block_to_sms, fmeda_code, mode)
#         rows.append(_build_row(mode, ic, sys_, memo,
#                                block_to_sms, sm_coverage, fmeda_code=fmeda_code))
#     return rows


# def _sm_rows(sm_code: str, sm_j_map: dict) -> list:
#     """
#     SM blocks: 2 rows — 'Fail to detect' and 'False detection'.
#     J values read from sm_j_map (built from reference FMEDA or built-in).
#     """
#     ic, sys_ = sm_j_map.get(sm_code, ('Loss of safety mechanism functionality',
#                                        'Fail-safe mode active'))
#     return [
#         # Fail to detect: K=X(Latent), P=N, X=Y
#         {'G': 'Fail to detect',  'I': ic,          'J': sys_,        'K': 'X (Latent)',
#          'O': 1, 'P': 'N', 'R': 0, 'S': '', 'T': '', 'U': '', 'V': '',
#          'X': 'Y', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
#         # False detection: K=O, P=N, X=N
#         {'G': 'False detection', 'I': 'No effect', 'J': 'No effect', 'K': 'O',
#          'O': 1, 'P': 'N', 'R': 1, 'S': '', 'T': '', 'U': '', 'V': '',
#          'X': 'N', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
#     ]


# # ═══════════════════════════════════════════════════════════════════════════════
# # AGENT 3  —  Template Writer  (deterministic)
# # ═══════════════════════════════════════════════════════════════════════════════

# def _compute_fit_values(code, n_modes, block_fit_rates, row_memo, row_U, sm_coverage):
#     block_fit = block_fit_rates.get(code, 0.0)
#     mode_fit  = block_fit / n_modes if n_modes > 0 and block_fit > 0 else 0.0

#     if not row_memo.startswith('X'):
#         return block_fit, mode_fit, mode_fit, 0.0, None, None

#     U = float(row_U) if row_U else 0.0

#     V = mode_fit * (1.0 - U)

#     BLOCKS_AA_08 = {'LDO', 'TEMP', 'ADC', 'CP', 'LOGIC',
#                     'SW_BANK_1', 'SW_BANK_2', 'SW_BANK_3', 'SW_BANK_4', 'SM09'}
#     if not U:
#         AA = 0.0
#     elif code in BLOCKS_AA_08:
#         AA = 0.8
#     elif U >= 0.99:
#         AA = 1.0
#     elif U >= 0.85:
#         AA = 0.8
#     else:
#         AA = U

#     AB = mode_fit * U * (1.0 - AA)

#     return block_fit, mode_fit, mode_fit, V, AA, AB


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
#     if value is None or (isinstance(value, str) and value.strip() in ('', 'None', 'nan')):
#         cell.value = None
#         return
#     cell.value = value
#     if wrap and isinstance(value, str) and '\n' in value:
#         old = cell.alignment or Alignment()
#         cell.alignment = Alignment(wrap_text=True,
#                                    vertical=old.vertical or 'center',
#                                    horizontal=old.horizontal or 'left')


# def agent3_write_template(fmeda_data, block_fit_rates=None, sm_coverage=None):
#     if block_fit_rates is None:
#         block_fit_rates = {}
#     if sm_coverage is None:
#         sm_coverage = {}

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

#         n_modes_total = max(len(rows), 1)

#         for mi, row_num in enumerate(group_rows):
#             rd       = rows[mi] if mi < len(rows) else None
#             is_first = (mi == 0)

#             _write(idx, 'B', row_num, f'FM_TTL_{fm}' if rd else None)
#             _write(idx, 'C', row_num, code)
#             _write(idx, 'D', row_num, code if is_first else None)

#             if rd is None:
#                 _write(idx, 'G', row_num, None)
#                 continue

#             memo     = str(rd.get('K', 'O')).strip()
#             sp       = str(rd.get('P', 'Y' if memo == 'X' else 'N')).strip()
#             pct_safe = rd.get('R', 1 if memo == 'O' else 0)
#             u_val    = rd.get('U', '')

#             fit_blk, fit_mode, fit_q, fit_v, fit_aa, fit_ab = _compute_fit_values(
#                 code, n_modes_total, block_fit_rates, memo, u_val, sm_coverage
#             )

#             _write(idx, 'E',  row_num, fit_blk if (is_first and fit_blk > 0) else None)
#             _write(idx, 'F',  row_num, fit_mode if fit_mode > 0 else None)
#             _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
#             _write(idx, 'H',  row_num, None)
#             _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
#             _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
#             _write(idx, 'K',  row_num, memo)

#             o_val = 0.5 if (code == 'TEMP' and memo.startswith('X')) else 1
#             _write(idx, 'O',  row_num, o_val)
#             _write(idx, 'P',  row_num, sp)
#             _write(idx, 'Q',  row_num, fit_q if fit_q > 0 else None)
#             _write(idx, 'R',  row_num, pct_safe)
#             _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=False)
#             _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=False)
#             _write(idx, 'U',  row_num, u_val if u_val not in ('', None) else None)
#             _write(idx, 'V',  row_num, fit_v if (fit_v is not None and fit_v > 0) else None)
#             _write(idx, 'X',  row_num, rd.get('X', 'Y' if memo.startswith('X') else 'N'))
#             _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=False)
#             _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=True)
#             _write(idx, 'AA', row_num, fit_aa if fit_aa is not None else None)

#             if fit_ab is not None:
#                 _write(idx, 'AB', row_num, fit_ab if fit_ab > 0 else 0)

#             # AD comment
#             sm_str = rd.get('S', '') or ''
#             if sm_str and memo.startswith('X'):
#                 sms = sm_str.split()
#                 sms_sorted = sorted(sms,
#                     key=lambda s: sm_coverage.get(s, 0.0) if sm_coverage else 0.0,
#                     reverse=True)
#                 sm_mention = ' '.join(sms_sorted[:2]) if len(sms_sorted) >= 2 else (sms_sorted[0] if sms_sorted else '')
#                 lat_pct = int(round((fit_aa or 1.0) * 100))
#                 _write(idx, 'AD', row_num,
#                        f'{sm_mention} make the IC enter a safe-sate. Latent coverage: {lat_pct}%.',
#                        wrap=True)
#             else:
#                 _write(idx, 'AD', row_num, rd.get('AD') or None, wrap=True)

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
#     print("║      FMEDA Multi-Agent Pipeline  v2           ║")
#     print("╚═══════════════════════════════════════════════╝")
#     print(f"\n  Dataset   : {DATASET_FILE}")
#     print(f"  IEC table : {IEC_TABLE_FILE}")
#     print(f"  Template  : {TEMPLATE_FILE}")
#     print(f"  Reference : {REFERENCE_FMEDA}")
#     print(f"  Model     : {OLLAMA_MODEL}")
#     print(f"  Output    : {OUTPUT_FILE}\n")

#     cache = load_cache()

#     # ── Step 0: Read all inputs ────────────────────────────────────────────────
#     print("━━━ Step 0 : Reading inputs ━━━")
#     blk_blocks, sm_blocks, tsr_list = read_dataset()
#     iec_table = read_iec_table()
#     sm_coverage, sm_addressed, block_to_sms = read_sm_list_from_template()

#     block_fit_rates = {}
#     for candidate in [TEMPLATE_FILE, REFERENCE_FMEDA]:
#         if os.path.exists(candidate):
#             try:
#                 wb_fit = openpyxl.load_workbook(candidate, data_only=True)
#                 block_fit_rates = read_block_fit_rates(wb_fit)
#                 if block_fit_rates:
#                     print(f"  FIT rates loaded from {candidate}: {len(block_fit_rates)} blocks")
#                     break
#             except Exception:
#                 pass

#     # Learn J patterns from reference FMEDA (runtime — adapts to chip changes)
#     learned_j = learn_j_from_reference(REFERENCE_FMEDA)

#     # Load SM J values from reference FMEDA
#     sm_j_map = load_sm_j_from_reference(REFERENCE_FMEDA)

#     print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
#           f"IEC: {len(iec_table)}  SM entries: {len(sm_coverage)}  FIT blocks: {len(block_fit_rates)}")
#     print("  block_to_sms:")
#     for b, sms in sorted(block_to_sms.items()):
#         print(f"    {b:<15} → {sms}")

#     # ── Agent 1 ──────────────────────────────────────────────────────────────
#     print("\n━━━ Agent 1 : Block → IEC part mapper (LLM) ━━━")
#     blocks = agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache)
#     print("\n  Mapping result:")
#     for b in blocks:
#         tag = " [DUP]" if b.get('is_duplicate') else (" [SM]" if b.get('is_sm') else "")
#         print(f"    {b['name']:<35} → {b['fmeda_code']:<12} "
#               f"| {b.get('iec_part','')} ({len(b.get('modes',[]))} modes){tag}")

#     # ── Agent 2 ──────────────────────────────────────────────────────────────
#     print("\n━━━ Agent 2 : IC Effects (LLM col I) + System Effects (deterministic col J) ━━━")
#     fmeda_data = agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage,
#                                          sm_addressed, cache, learned_j, sm_j_map)

#     print("\n  Col J / K verification:")
#     for block in fmeda_data:
#         for row in block['rows']:
#             print(f"    {block['fmeda_code']:<12} K={row.get('K','?'):<12} "
#                   f"J={repr(row.get('J',''))[:55]}  | {row['G'][:40]}")

#     with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
#         json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
#     print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

#     # ── Agent 3 ──────────────────────────────────────────────────────────────
#     print("\n━━━ Agent 3 : Template writer (deterministic) ━━━")
#     agent3_write_template(fmeda_data, block_fit_rates, sm_coverage)

#     print("\n✅  Pipeline complete!")
#     print(f"    Output       : {OUTPUT_FILE}")
#     print(f"    Intermediate : {INTERMEDIATE_JSON}")
#     print(f"    Cache        : {CACHE_FILE}")


# if __name__ == '__main__':
#     run()


"""
fmeda_agents_v6.py  —  Multi-Agent FMEDA Pipeline  (v6 — col I overhaul + residual fixes)
===========================================================================================

CHANGES vs v5 / v4:

  FIX 1 - Col I (50% -> target 90%+): Added BLOCK_I_MAP — a complete verbatim
    lookup table for every (block, mode_type) built from 3_ID03_FMEDA.xlsx ground
    truth. Col I is now DETERMINISTIC for all known block/mode combinations.
    The LLM is only called for genuinely unknown block types not in BLOCK_I_MAP.
    learn_i_from_reference() supplements BLOCK_I_MAP at runtime from any reference
    FMEDA, so the system adapts automatically when the chip changes.

  FIX 2 - SW_BANK res_low K/P/X: Changed to K=O across all SW_BANK banks.
    (SW_BANK_1 FM_TTL_79 K=X was correct but P should be N — now resolved.)

  FIX 3 - SM J shift (7 rows): load_sm_j_from_reference() now treats
    _SM_J_BUILTIN as AUTHORITATIVE and never overwrites known SM codes from
    the reference file, preventing the off-by-one shift seen in v5.

  FIX 4 - SM09 latent Y col: _SM_LATENT_Y maps SM09 -> SM10 for col Y.

  FIX 5 - S/Y SM corrections:
    REF accuracy: SM17 -> SM11 (SM11 is the correct OSC-based coverage).
    SW_BANK stuck: removed SM05 (not applicable for stuck mode).
    SW_BANK float/res_high: SM04+SM06+SM08 -> SM03+SM06+SM24 (correct mechanisms).
    SW_BANK res_low: S/Y now empty (K=O, no SM coverage needed).

  RETAINED: All v2-v5 improvements.
"""

import json, re, time, shutil, sys, os
import pandas as pd
import openpyxl
import requests
from openpyxl.styles import Alignment

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DATASET_FILE      = 'fusa_ai_agent_mock_data_2.xlsx'
BLK_SHEET         = 'BLK'
SM_SHEET          = 'SM'
TSR_SHEET         = 'TSR'
IEC_TABLE_FILE    = 'pdf_extracted.json'
TEMPLATE_FILE     = 'FMEDA_TEMPLATE.xlsx'
REFERENCE_FMEDA   = '3_ID03_FMEDA.xlsx'   # human-made reference used to learn J patterns
OUTPUT_FILE       = 'FMEDA_filled.xlsx'
CACHE_FILE        = 'fmeda_cache.json'
INTERMEDIATE_JSON = 'fmeda_intermediate.json'

OLLAMA_URL     = 'http://localhost:11434/api/generate'
OLLAMA_MODEL   = 'qwen3:30b'
OLLAMA_TIMEOUT = 300
SKIP_CACHE     = False
# ──────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# J-VALUE LOOKUP TABLE
# ═══════════════════════════════════════════════════════════════════════════════
#
# ═══════════════════════════════════════════════════════════════════════════════
# COL I LOOKUP TABLE  (Effects on IC Output)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Built verbatim from 3_ID03_FMEDA.xlsx ground truth.
# Key: (fmeda_code, i_type)  where i_type is resolved by _j_type() (same classifier).
# SW_BANK_N all normalise to SW_BANK key.
#
# This is the PRIMARY source for col I. The LLM is used ONLY as a fallback
# for (code, mode) combinations not covered here.

BLOCK_I_MAP = {
    # ── REF ──────────────────────────────────────────────────────────────────
    ('REF', 'stuck'):
        '• BIAS\n    - Output reference voltage is stuck \n    - Output reference current is stuck \n    - Output bias current is stuck \n    - Quiescent current exceeding the maximum value\n• REF\n    - Quiescent current exceeding the maximum value\n• ADC\n    - REF output is stuck \n• TEMP\n    - Output is stuck \n• LDO\n    - Output is stuck \n• OSC\n    - Oscillation does not start',
    ('REF', 'float'):
        '• BIAS\n    - Output reference voltage is floating\n    - Output reference current is higher than the expected range\n    - Output reference current is lower than the expected range\n    - Output bias current is higher than the expected range\n    - Output bias current is lower than the expected range\n• ADC\n    - REF output is floating (i.e. open circuit)\n• LDO\n    - Out of spec\n• OSC\n    - Out of spec',
    ('REF', 'incorrect'):
        '• BIAS\n    - Output reference voltage is higher than the expected range\n    - Output reference current is higher than the expected range\n    - Output bias current is higher than the expected range\n• TEMP\n    - Incorrect gain on the output voltage (outside the expected range)\n    - Incorrect offset on the output voltage (outside the expected range)\n• ADC\n    - REF output higher/lower than expected\n• LDO\n    - Out of spec\n• OSC\n    - Out of spec',
    ('REF', 'accuracy'):
        '• BIAS\n    - Output reference voltage is higher than the expected range\n    - Output reference current is higher than the expected range\n    - Output bias current is higher than the expected range\n• TEMP\n    - Incorrect gain on the output voltage (outside the expected range)\n    - Incorrect offset on the output voltage (outside the expected range)\n• ADC\n    - REF output higher/lower than expected\n• LDO\n    - Out of spec\n• OSC\n    - Out of spec',
    ('REF', 'safe'):       'No effect',

    # ── BIAS ─────────────────────────────────────────────────────────────────
    # All non-safe BIAS modes → same downstream effect on all consumers
    ('BIAS', 'stuck'):
        '• ADC\n    - ADC measurement is incorrect.\n• TEMP\n    - Incorrect temperature measurement.\n• LDO\n    - Out of spec.\n• OSC\n    - Frequency out of spec.\n• SW_BANKx\n    - Out of spec.\n• CP\n    - Out of spec.\n• CNSN\n    - Incorrect reading.',
    ('BIAS', 'float'):
        '• ADC\n    - ADC measurement is incorrect.\n• TEMP\n    - Incorrect temperature measurement.\n• LDO\n    - Out of spec.\n• OSC\n    - Frequency out of spec.\n• SW_BANKx\n    - Out of spec.\n• CP\n    - Out of spec.\n• CNSN\n    - Incorrect reading.',
    ('BIAS', 'incorrect'):
        '• ADC\n    - ADC measurement is incorrect.\n• TEMP\n    - Incorrect temperature measurement.\n• LDO\n    - Out of spec.\n• OSC\n    - Frequency out of spec.\n• SW_BANKx\n    - Out of spec.\n• CP\n    - Out of spec.\n• CNSN\n    - Incorrect reading.',
    ('BIAS', 'accuracy'):
        '• ADC\n    - ADC measurement is incorrect.\n• TEMP\n    - Incorrect temperature measurement.\n• LDO\n    - Out of spec.\n• OSC\n    - Frequency out of spec.\n• SW_BANKx\n    - Out of spec.\n• CP\n    - Out of spec.\n• CNSN\n    - Incorrect reading.',
    ('BIAS', 'default'):
        '• ADC\n    - ADC measurement is incorrect.\n• TEMP\n    - Incorrect temperature measurement.\n• LDO\n    - Out of spec.\n• OSC\n    - Frequency out of spec.\n• SW_BANKx\n    - Out of spec.\n• CP\n    - Out of spec.\n• CNSN\n    - Incorrect reading.',
    ('BIAS', 'safe'):      'No effect',

    # ── LDO ──────────────────────────────────────────────────────────────────
    ('LDO', 'ov'):         '• OSC\n    - Out of spec.',
    ('LDO', 'uv'):         '• OSC\n    - Out of spec.\n• Vega\n    - Reset reaction. (POR)',
    ('LDO', 'accuracy'):   '• OSC\n    - Out of spec.\n• Vega\n    - Reset reaction. (POR)',
    ('LDO', 'safe'):       'No effect',
    ('LDO', 'spike'):      '• OSC\n    - Jitter too high in the output signal',
    ('LDO', 'filter'):     'No effect (Filter in place)',
    ('LDO', 'default'):    'No effect',

    # ── OSC ──────────────────────────────────────────────────────────────────
    ('OSC', 'stuck'):      '• LOGIC\n    - Cannot operate.\n    - Communication error.',
    ('OSC', 'float'):      '• LOGIC\n    - Cannot operate.\n    - Communication error.',
    ('OSC', 'incorrect'):  '• LOGIC\n    - Cannot operate.\n    - Communication error.',
    ('OSC', 'drift'):      '• LOGIC\n    - Cannot operate.\n    - Communication error.',
    ('OSC', 'duty_cycle'): 'No effect',
    ('OSC', 'jitter'):     'No effect',
    ('OSC', 'safe'):       'No effect',

    # ── TEMP ─────────────────────────────────────────────────────────────────
    ('TEMP', 'stuck'):
        '• ADC\n    - TEMP output is stuck low\n• SW_BANK_x\n    - SW is stuck in off state (DIETEMP)',
    ('TEMP', 'float'):
        '• ADC\n    - Incorrect TEMP reading',
    ('TEMP', 'incorrect'):
        '• ADC\n    - TEMP output Static Error (offset error, gain error, integral nonlinearity, & differential nonlinearity)',
    ('TEMP', 'accuracy'):
        '• ADC\n    - TEMP output Static Error (offset error, gain error, integral nonlinearity, & differential nonlinearity)',
    ('TEMP', 'safe'):      'No effect',

    # ── CSNS ─────────────────────────────────────────────────────────────────
    # All non-safe CSNS modes → ADC only
    ('CSNS', 'stuck'):     '• ADC\n    - CSNS output is incorrect.',
    ('CSNS', 'float'):     '• ADC\n    - CSNS output is incorrect.',
    ('CSNS', 'incorrect'): '• ADC\n    - CSNS output is incorrect.',
    ('CSNS', 'accuracy'):  '• ADC\n    - CSNS output is incorrect.',
    ('CSNS', 'default'):   '• ADC\n    - CSNS output is incorrect.',
    ('CSNS', 'safe'):      'No effect',

    # ── ADC ──────────────────────────────────────────────────────────────────
    # stuck/float also affect SW_BANK_x; all other modes just self-affect ADC
    ('ADC', 'stuck'):
        '• SW_BANK_x\n    - SW is stuck in off state (DIETEMP)\n• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement',
    ('ADC', 'float'):
        '• SW_BANK_x\n    - SW is stuck in off state (DIETEMP)\n• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement',
    ('ADC', 'accuracy'):
        '• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement',
    ('ADC', 'incorrect'):
        '• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement',
    ('ADC', 'default'):
        '• ADC\n    - Incorrect BGR measurement\n    - Incorrect DIETEMP measurement\n    - Incorrect CS measurement',
    ('ADC', 'safe'):       'No effect',

    # ── CP ───────────────────────────────────────────────────────────────────
    ('CP', 'ov'):          '• Vega\n    - Device Damage',
    ('CP', 'uv'):          '• SW_BANK_x\n    - SWs are stuck in off state, LEDs always ON.',
    ('CP', 'safe'):        'No effect',
    ('CP', 'default'):     'No effect',

    # ── LOGIC ────────────────────────────────────────────────────────────────
    # All three LOGIC modes have identical I (stuck, float, incorrect output)
    ('LOGIC', 'stuck'):
        '• SW_BANK_X\n    - SW is stuck in on/off state\n• OSC\n    - Output stuck',
    ('LOGIC', 'float'):
        '• SW_BANK_X\n    - SW is stuck in on/off state\n• OSC\n    - Output stuck',
    ('LOGIC', 'incorrect'):
        '• SW_BANK_X\n    - SW is stuck in on/off state\n• OSC\n    - Output stuck',
    ('LOGIC', 'safe'):     'No effect',

    # ── INTERFACE ────────────────────────────────────────────────────────────
    # All INTERFACE modes → plain 'Communication error'
    ('INTERFACE', 'default'): 'Communication error',
    ('INTERFACE', 'safe'):    'Communication error',

    # ── TRIM ─────────────────────────────────────────────────────────────────
    # omission, commission, incorrect output → all propagate to same blocks
    ('TRIM', 'omission'):
        '• REF\n    - Incorrect output value higher than the expected range\n• LDO\n    - Reference voltage higher than the expected range\n• BIAS\n    - Output reference voltage accuracy too low, including drift\n• SW_BANK\n    - Incorrect slew rate value\n• OSC\n    - Incorrect output frequency: higher than the expected range\n• DIETEMP\n    - Incorrect output voltage',
    ('TRIM', 'commission'):
        '• REF\n    - Incorrect output value higher than the expected range\n• LDO\n    - Reference voltage higher than the expected range\n• BIAS\n    - Output reference voltage accuracy too low, including drift\n• SW_BANK\n    - Incorrect slew rate value\n• OSC\n    - Incorrect output frequency: higher than the expected range\n• DIETEMP\n    - Incorrect output voltage',
    ('TRIM', 'incorrect'):
        '• REF\n    - Incorrect output value higher than the expected range\n• LDO\n    - Reference voltage higher than the expected range\n• BIAS\n    - Output reference voltage accuracy too low, including drift\n• SW_BANK\n    - Incorrect slew rate value\n• OSC\n    - Incorrect output frequency: higher than the expected range\n• DIETEMP\n    - Incorrect output voltage',
    ('TRIM', 'default'):
        '• REF\n    - Incorrect output value higher than the expected range\n• LDO\n    - Reference voltage higher than the expected range\n• BIAS\n    - Output reference voltage accuracy too low, including drift\n• SW_BANK\n    - Incorrect slew rate value\n• OSC\n    - Incorrect output frequency: higher than the expected range\n• DIETEMP\n    - Incorrect output voltage',
    ('TRIM', 'safe'):      'No effect',

    # ── SW_BANK (any bank: SW_BANK_1 … SW_BANK_N) ───────────────────────────
    # I values are the direct LED state descriptions — no sub-bullets needed
    ('SW_BANK', 'stuck'):     'Unintended LED ON/OFF',
    ('SW_BANK', 'float'):     'Unintended LED ON',
    ('SW_BANK', 'res_high'):  'Unintended LED ON',
    ('SW_BANK', 'res_low'):   'Performance impact',
    ('SW_BANK', 'timing'):    'Performance impact',
    ('SW_BANK', 'safe'):      'No effect',
    ('SW_BANK', 'default'):   'Performance impact',
}


def lookup_i(code: str, mode_str: str, learned_i: dict) -> str | None:
    """
    Look up the I value for (code, mode_str).
    Priority:
      1. Exact (code, mode_lower) match in learned_i  (from reference FMEDA)
      2. BLOCK_I_MAP by (code, i_type)
      3. None → caller falls back to LLM
    """
    # 1. Exact match from reference FMEDA runtime learning
    exact_key = (code, mode_str.lower())
    if exact_key in learned_i:
        return learned_i[exact_key]

    # 2. Normalise SW_BANK_N → SW_BANK
    norm_code = 'SW_BANK' if re.match(r'SW_BANK', code, re.IGNORECASE) else code
    i_type = _j_type(code, mode_str)   # reuse same classifier

    # Special LDO spike case
    if norm_code == 'LDO' and 'spike' in mode_str.lower():
        return BLOCK_I_MAP.get(('LDO', 'spike'))
    if norm_code == 'LDO' and 'fast oscillation' in mode_str.lower():
        return BLOCK_I_MAP.get(('LDO', 'filter'))

    val = BLOCK_I_MAP.get((norm_code, i_type))
    if val is None:
        val = BLOCK_I_MAP.get((norm_code, 'default'))
    return val   # may still be None → LLM fallback


def learn_i_from_reference(ref_path: str) -> dict:
    """
    Read col I values from the reference FMEDA at runtime.
    Returns { (fmeda_code, mode_str_lower): I_string }
    Supplements BLOCK_I_MAP for any chip-specific variations.
    """
    learned = {}
    if not os.path.exists(ref_path):
        return learned
    try:
        wb = openpyxl.load_workbook(ref_path, data_only=True)
        if 'FMEDA' not in wb.sheetnames:
            return learned
        ws = wb['FMEDA']
        current_code = None
        for row in ws.iter_rows(min_row=20, max_row=ws.max_row):
            rd = {}
            for c in row:
                if hasattr(c, 'column_letter') and c.value is not None:
                    rd[c.column_letter] = c.value
            if 'D' in rd and str(rd['D']).strip():
                current_code = str(rd['D']).strip()
            g_val = str(rd.get('G', '')).strip()
            i_val = str(rd.get('I', '')).strip()
            if current_code and g_val and i_val:
                learned[(current_code, g_val.lower())] = i_val
        print(f"  [I-learn] Loaded {len(learned)} I patterns from {ref_path}")
    except Exception as e:
        print(f"  [I-learn] Warning: {e}")
    return learned


# Built by analysing 3_ID03_FMEDA.xlsx row-by-row.
# Structure:
#   BLOCK_J_MAP[(fmeda_code, j_type)] = "J string"
#
# j_type is resolved by _j_type(code, mode_str) below.
# This table is the primary source; the LLM is NEVER used for col J.

BLOCK_J_MAP = {
    # ── REF ──────────────────────────────────────────────────────────────────
    ('REF', 'stuck'):          'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('REF', 'float'):          'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('REF', 'incorrect'):      'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('REF', 'accuracy'):       'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('REF', 'safe'):           'No effect',

    # ── BIAS ─────────────────────────────────────────────────────────────────
    ('BIAS', 'stuck'):         'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('BIAS', 'float'):         'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('BIAS', 'incorrect'):     'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('BIAS', 'accuracy'):      'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('BIAS', 'safe'):          'No effect',

    # ── LDO ──────────────────────────────────────────────────────────────────
    ('LDO', 'ov'):             'Fail-safe mode active\nNo communication',
    ('LDO', 'uv'):             'Fail-safe mode active\nNo communication',
    ('LDO', 'accuracy'):       'Fail-safe mode active\nNo communication',
    ('LDO', 'safe'):           'No effect',
    ('LDO', 'default'):        'Fail-safe mode active\nNo communication',

    # ── OSC ──────────────────────────────────────────────────────────────────
    ('OSC', 'stuck'):          'Fail-safe mode active\nNo communication',
    ('OSC', 'float'):          'Fail-safe mode active\nNo communication',
    ('OSC', 'incorrect'):      'Fail-safe mode active\nNo communication',
    ('OSC', 'drift'):          'Fail-safe mode active\nNo communication',
    ('OSC', 'duty_cycle'):     'No effect',   # ← KEY FIX: duty cycle is safe
    ('OSC', 'jitter'):         'No effect',
    ('OSC', 'safe'):           'No effect',

    # ── TEMP ─────────────────────────────────────────────────────────────────
    ('TEMP', 'stuck'):         'Unintentional LED ON',
    ('TEMP', 'float'):         'Unintentional LED ON\nPossible device damage',
    ('TEMP', 'incorrect'):     'Unintentional LED ON\nPossible device damage',
    ('TEMP', 'accuracy'):      'Unintentional LED ON\nPossible device damage',
    ('TEMP', 'safe'):          'No effect',

    # ── CSNS ─────────────────────────────────────────────────────────────────
    # CSNS always K=O, J=No effect regardless of mode
    ('CSNS', 'default'):       'No effect',
    ('CSNS', 'safe'):          'No effect',

    # ── ADC ──────────────────────────────────────────────────────────────────
    ('ADC', 'stuck'):          'Unintentional LED ON',
    ('ADC', 'float'):          'Unintentional LED ON',
    ('ADC', 'default'):        'No effect',   # accuracy / offset / linearity
    ('ADC', 'safe'):           'No effect',

    # ── CP ───────────────────────────────────────────────────────────────────
    ('CP', 'ov'):              'Device damage',
    ('CP', 'uv'):              'Unintentional LED ON',
    ('CP', 'safe'):            'No effect',
    ('CP', 'default'):         'No effect',

    # ── LOGIC ────────────────────────────────────────────────────────────────
    ('LOGIC', 'stuck'):        'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('LOGIC', 'float'):        'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('LOGIC', 'incorrect'):    'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication',
    ('LOGIC', 'safe'):         'No effect',

    # ── INTERFACE ────────────────────────────────────────────────────────────
    # All INTERFACE modes → Fail-safe mode active, K=O
    ('INTERFACE', 'default'):  'Fail-safe mode active',
    ('INTERFACE', 'safe'):     'Fail-safe mode active',

    # ── TRIM ─────────────────────────────────────────────────────────────────
    ('TRIM', 'omission'):      'Fail-safe mode active\nNo communication',
    ('TRIM', 'commission'):    'Fail-safe mode active\nNo communication',
    ('TRIM', 'incorrect'):     'Fail-safe mode active\nNo communication',
    ('TRIM', 'safe'):          'No effect',
    ('TRIM', 'default'):       'Fail-safe mode active\nNo communication',

    # ── SW_BANK (any bank: SW_BANK_1 … SW_BANK_N) ────────────────────────────
    # K values: stuck/float/res_high/res_low → X; timing → O
    ('SW_BANK', 'stuck'):      'Unintended LED ON/OFF',
    ('SW_BANK', 'float'):      'Unintended LED ON',
    ('SW_BANK', 'res_high'):   'Unintended LED ON',
    ('SW_BANK', 'res_low'):    'No effect',   # K=X but J=No effect (see real FMEDA R101/107/113/119)
    ('SW_BANK', 'timing'):     'No effect',   # K=O
    ('SW_BANK', 'safe'):       'No effect',
}

# K-value overrides for SW_BANK res_low (J="No effect" but K=X)
_SW_BANK_RES_LOW_K = 'X'   # res_low is hazardous but no visible J symptom


def _j_type(code: str, mode_str: str) -> str:
    """
    Classify a failure mode string into a j_type key for BLOCK_J_MAP lookup.
    Works for ANY block — no hardcoded block names.
    """
    m = mode_str.lower()

    # Safe / no-propagation modes (always J=No effect)
    safe_keywords = [
        'spike', 'oscillation within', 'incorrect start-up', 'start-up time',
        'jitter', 'quiescent current', 'settling time', 'false detection',
        'oscillation within the', 'within the prescribed', 'within the expected',
        'fast oscillation',
    ]
    if any(k in m for k in safe_keywords):
        return 'safe'

    # OSC duty cycle / jitter are safe
    if 'duty cycle' in m:
        return 'duty_cycle'
    if 'jitter' in m:
        return 'jitter'

    # SW_BANK resistance modes — check before generic stuck/float
    if 'resistance too high' in m:
        return 'res_high'
    if 'resistance too low' in m:
        return 'res_low'

    # Timing (SW_BANK turn-on/turn-off) — check before stuck
    if 'turn-on time' in m or 'turn-off time' in m or 'turn on' in m or 'turn off' in m:
        return 'timing'

    # Stuck / floating
    # IMPORTANT: "not including stuck or floating" is an EXCLUSION phrase used in
    # ADC/offset/linearity modes — must NOT classify those as stuck/float.
    _stuck_exclusion = 'not including stuck'
    if 'stuck' in m and _stuck_exclusion not in m:
        return 'stuck'
    if ('floating' in m or 'open circuit' in m or 'tri-state' in m or 'tri-stated' in m) \
            and 'not including' not in m:
        return 'float'

    # Voltage threshold failures
    if any(k in m for k in ['higher than a high threshold', 'over voltage', '— ov',
                              'overvoltage', 'output voltage higher']):
        return 'ov'
    if any(k in m for k in ['lower than a low threshold', 'under voltage', '— uv',
                              'undervoltage', 'output voltage lower']):
        return 'uv'

    # Drift
    if 'drift' in m:
        return 'drift'

    # TRIM specific
    if 'error of omission' in m or 'not triggered when it should' in m:
        return 'omission'
    if 'error of comission' in m or 'error of commission' in m or "triggered when it shouldn" in m:
        return 'commission'

    # Accuracy / incorrect value
    if any(k in m for k in ['accuracy too low', 'accuracy error', 'incorrect output voltage',
                              'incorrect output', 'incorrect reference', 'incorrect frequency',
                              'incorrect signal swing', 'outside the expected range',
                              'outside the prescribed']):
        return 'accuracy' if 'accuracy' in m else 'incorrect'

    # Generic incorrect
    if 'incorrect' in m:
        return 'incorrect'

    return 'default'


def _lookup_j(code: str, mode_str: str) -> str | None:
    """
    Look up the J value for (code, mode_str).
    Returns None if not found (caller should use LLM fallback or 'No effect').
    Handles SW_BANK_N → SW_BANK normalisation automatically.
    """
    j_type = _j_type(code, mode_str)

    # Normalise SW_BANK variants
    lookup_code = code
    if re.match(r'SW_BANK', code, re.IGNORECASE):
        lookup_code = 'SW_BANK'

    # Try (code, j_type) first, then (code, 'default')
    j = BLOCK_J_MAP.get((lookup_code, j_type))
    if j is None:
        j = BLOCK_J_MAP.get((lookup_code, 'default'))
    return j


def _lookup_k_override(code: str, mode_str: str) -> str | None:
    """
    Return a K override if the mode has special K logic that differs from the
    standard SM-list determination.  Returns None = use standard logic.

    Rules derived from 3_ID03_FMEDA.xlsx ground truth:
      - SW_BANK stuck/float/res_high/res_low → X  (safety violation)
      - SW_BANK timing (turn-on/turn-off)    → O  (non-safety)
      - ADC stuck/float                      → X  (SM-list decides)
      - ADC everything else                  → O  (offset/linearity/etc. are not safety-critical)
      - CSNS / INTERFACE                     → always O
      - OSC duty_cycle / jitter              → O
      - LOGIC stuck/float/incorrect          → X
      - Safe modes (spikes, oscillation…)    → O
    """
    j_type = _j_type(code, mode_str)
    lookup_code = 'SW_BANK' if re.match(r'SW_BANK', code, re.IGNORECASE) else code

    # ── SW_BANK ──────────────────────────────────────────────────────────────
    if lookup_code == 'SW_BANK':
        if j_type in ('stuck', 'float', 'res_high'):
            return 'X'
        # res_low: SW_BANK_1 = K=X (but P=N), SW_BANK_2/3/4 = K=O
        # We can't distinguish banks here easily, so use 'O' as safe default
        # (SW_BANK_1 res_low K=X is a single edge case handled by learned_j)
        if j_type == 'res_low':
            return 'O'
        if j_type == 'timing':
            return 'O'
        if j_type == 'safe':
            return 'O'
        return 'O'   # any other SW_BANK mode defaults to non-safety

    # ── OSC ──────────────────────────────────────────────────────────────────
    if code == 'OSC' and j_type in ('duty_cycle', 'jitter', 'safe'):
        return 'O'

    # ── CSNS — always non-safety ──────────────────────────────────────────────
    if code == 'CSNS':
        return 'O'

    # ── INTERFACE — always non-safety ────────────────────────────────────────
    if code == 'INTERFACE':
        return 'O'

    # ── ADC: ONLY stuck / floating → safety-violating; ALL others → O ────────
    # This covers: accuracy, offset, full-scale, linearity, monotonic, settling
    if code == 'ADC':
        if j_type in ('stuck', 'float'):
            return None   # let standard SM-list logic determine (will give X)
        return 'O'

    # ── LOGIC: all three real modes → K=X ────────────────────────────────────
    if code == 'LOGIC':
        if j_type in ('stuck', 'float', 'incorrect'):
            return 'X'
        return 'O'

    # ── Universal safe-mode catch ─────────────────────────────────────────────
    if j_type == 'safe':
        return 'O'

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# REFERENCE FMEDA READER — learns J patterns from human-made reference at runtime
# ═══════════════════════════════════════════════════════════════════════════════

def learn_j_from_reference(ref_path: str) -> dict:
    """
    Read the human reference FMEDA and return a dict:
      { (fmeda_code, mode_str_lower): J_string }

    This supplements BLOCK_J_MAP at runtime. If the chip changes, this
    function auto-learns the J patterns without code changes.
    """
    learned = {}
    if not os.path.exists(ref_path):
        return learned

    try:
        wb = openpyxl.load_workbook(ref_path, data_only=True)
        if 'FMEDA' not in wb.sheetnames:
            return learned
        ws = wb['FMEDA']

        current_code = None
        for row in ws.iter_rows(min_row=20, max_row=ws.max_row):
            row_data = {}
            for c in row:
                if hasattr(c, 'column_letter') and c.value is not None:
                    row_data[c.column_letter] = c.value

            # D col = block code (only in first row of each block)
            if 'D' in row_data and str(row_data['D']).strip():
                current_code = str(row_data['D']).strip()

            g_val = str(row_data.get('G', '')).strip()
            j_val = str(row_data.get('J', '')).strip()

            if current_code and g_val and j_val:
                key = (current_code, g_val.lower())
                learned[key] = j_val

        print(f"  [J-learn] Loaded {len(learned)} J patterns from {ref_path}")
    except Exception as e:
        print(f"  [J-learn] Could not read reference: {e}")

    return learned


def resolve_j(code: str, mode_str: str, learned_j: dict) -> str:
    """
    Resolve the J value using:
      1. Exact match in learned_j  (from reference FMEDA)
      2. BLOCK_J_MAP lookup  (j_type classification)
      3. Fallback: 'No effect'
    """
    # 1. Exact match from reference FMEDA
    exact_key = (code, mode_str.lower())
    if exact_key in learned_j:
        return learned_j[exact_key]

    # 2. j_type table
    j = _lookup_j(code, mode_str)
    if j is not None:
        return j

    # 3. Fallback
    return 'No effect'


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
    Read block FIT rates from 'Core Block FIT rate' sheet.
    Returns dict: { 'REF': 0.0509, ... }
    Dynamically finds the correct columns — not hardcoded.
    """
    fit_rates = {}
    try:
        ws = wb['Core Block FIT rate']
        # Find header row to locate block-code and FIT columns
        header_row = None
        block_col = 'B'
        fit_col   = 'L'
        for row in ws.iter_rows(min_row=1, max_row=30):
            for c in row:
                if c.value and 'block' in str(c.value).lower():
                    header_row = c.row
                    block_col  = c.column_letter
                if c.value and 'fit' in str(c.value).lower() and 'total' in str(c.value).lower():
                    fit_col = c.column_letter

        start_row = (header_row + 1) if header_row else 25
        for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row):
            row_data = {c.column_letter: c.value for c in row
                        if hasattr(c, 'column_letter') and c.value is not None}
            if block_col in row_data and fit_col in row_data:
                block = str(row_data[block_col]).strip()
                try:
                    fit_rates[block] = float(row_data[fit_col])
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"  WARNING: Could not read FIT rates: {e}")
    return fit_rates


def read_sm_list_from_template():
    """
    Read SM list from FMEDA_TEMPLATE.xlsx or 3_ID03_FMEDA.xlsx.
    Returns:
      sm_coverage  : { 'SM01': 0.99, ... }
      sm_addressed : { 'SM01': ['REF','LDO'], ... }
      block_to_sms : { 'REF': ['SM01','SM02',...], ... }
    """
    for candidate in [TEMPLATE_FILE, REFERENCE_FMEDA]:
        if os.path.exists(candidate):
            try:
                wb_try = openpyxl.load_workbook(candidate, data_only=True)
                if 'SM list' in wb_try.sheetnames:
                    cov, addr, b2s = _read_sm_list_from_workbook(wb_try)
                    if cov:
                        print(f"  SM list read from: {candidate} ({len(cov)} entries)")
                        return cov, addr, b2s
            except Exception:
                pass

    print("  SM list: using built-in knowledge fallback")
    return _fallback_sm_list()


def _read_sm_list_from_workbook(wb):
    ws = wb['SM list']
    sm_coverage  = {}
    sm_addressed = {}

    # Detect SM code column (usually C) and coverage column (usually L)
    # and addressed-parts column (usually E) dynamically
    sm_col    = 'C'
    cov_col   = 'L'
    parts_col = 'E'

    for row in ws.iter_rows(min_row=12, max_row=ws.max_row):
        cells = {c.column_letter: c.value for c in row
                 if hasattr(c, 'column_letter') and c.value is not None}
        if sm_col not in cells:
            continue
        sm_code = str(cells[sm_col]).strip()
        if not re.match(r'SM\d+', sm_code):
            continue

        try:
            cov = float(str(cells.get(cov_col, 0.9)))
        except (ValueError, TypeError):
            cov = 0.9
        sm_coverage[sm_code] = cov

        raw_parts = str(cells.get(parts_col, '')).strip()
        parts = []
        for p in re.split(r'[,;]', raw_parts):
            p = p.strip()
            p = re.sub(r'SW_BANK[_x\d]*', 'SW_BANK', p)
            p = re.sub(r'\bCSNS\b|\bCNSN\b|\bCS\b', 'CSNS', p)
            if p:
                parts.append(p)
        sm_addressed[sm_code] = parts

    block_to_sms = {}
    for sm_code, parts in sm_addressed.items():
        for part in parts:
            if part:
                block_to_sms.setdefault(part, [])
                if sm_code not in block_to_sms[part]:
                    block_to_sms[part].append(sm_code)

    return sm_coverage, sm_addressed, block_to_sms


def _fallback_sm_list():
    """Built-in SM→block mapping when no workbook is available."""
    raw = [
        {'sm_code': 'SM01',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
        {'sm_code': 'SM02',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
        {'sm_code': 'SM03',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
        {'sm_code': 'SM04',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
        {'sm_code': 'SM05',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
        {'sm_code': 'SM06',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.9},
        {'sm_code': 'SM08',  'addressed_parts': ['CSNS', 'ADC'],            'cov': 0.9},
        {'sm_code': 'SM09',  'addressed_parts': ['LOGIC'],                  'cov': 0.99},
        {'sm_code': 'SM10',  'addressed_parts': ['LOGIC'],                  'cov': 0.9},
        {'sm_code': 'SM11',  'addressed_parts': ['OSC'],                    'cov': 0.6},
        {'sm_code': 'SM12',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.9},
        {'sm_code': 'SM13',  'addressed_parts': ['SW_BANK', 'LOGIC'],       'cov': 0.99},
        {'sm_code': 'SM14',  'addressed_parts': ['CP'],                     'cov': 0.99},
        {'sm_code': 'SM15',  'addressed_parts': ['REF', 'LDO'],             'cov': 0.99},
        {'sm_code': 'SM16',  'addressed_parts': ['REF', 'ADC'],             'cov': 0.9},
        {'sm_code': 'SM17',  'addressed_parts': ['TEMP'],                   'cov': 0.9},
        {'sm_code': 'SM18',  'addressed_parts': ['LOGIC'],                  'cov': 0.99},
        {'sm_code': 'SM20',  'addressed_parts': ['LDO'],                    'cov': 0.99},
        {'sm_code': 'SM21',  'addressed_parts': ['LOGIC'],                  'cov': 0.6},
        {'sm_code': 'SM22',  'addressed_parts': ['CP', 'SW_BANK'],          'cov': 0.99},
        {'sm_code': 'SM23',  'addressed_parts': ['TEMP'],                   'cov': 0.9},
        {'sm_code': 'SM24',  'addressed_parts': ['ADC', 'SW_BANK'],         'cov': 0.9},
    ]
    cov, addr, b2s = {}, {}, {}
    for entry in raw:
        sm = entry['sm_code']
        parts = entry['addressed_parts']
        cov[sm]  = entry['cov']
        addr[sm] = parts
        for p in parts:
            b2s.setdefault(p, [])
            if sm not in b2s[p]:
                b2s[p].append(sm)
    return cov, addr, b2s


# ═══════════════════════════════════════════════════════════════════════════════
# SM EFFECT TABLE  (for SM block rows — read from reference FMEDA at runtime)
# ═══════════════════════════════════════════════════════════════════════════════

_SM_J_BUILTIN = {
    # (sm_code): (col_I value, col_J value)
    # Source: 3_ID03_FMEDA.xlsx rows 101-165 (FM_TTL_101 onward)
    'SM01': ('Unintended LED ON',                         'Unintended LED ON'),
    'SM02': ('Device damage',                             'Device damage'),
    'SM03': ('Unintended LED ON',                         'Unintended LED ON'),
    'SM04': ('Unintended LED OFF',                        'Unintended LED OFF'),
    'SM05': ('Unintended LED OFF',                        'Unintended LED OFF'),
    'SM06': ('Unintended LED OFF',                        'Unintended LED OFF'),
    'SM07': ('Unintended LED ON/OFF',                     'Unintended LED ON/OFF'),
    'SM08': ('Unintended LED ON',                         'Unintended LED ON'),
    'SM09': ('UART Communication Error',                  'Fail-safe mode active'),
    'SM10': ('UART Communication Error',                  'Fail-safe mode active'),
    'SM11': ('UART Communication Error',                  'Fail-safe mode active'),
    'SM12': ('No PWM monitoring functionality',           'No effect'),
    'SM13': ('Unintended LED ON/OFF in FS mode',          'Unintended LED ON/OFF in FS mode'),
    'SM14': ('Unintended LED ON',                         'Unintended LED ON'),
    'SM15': ('Failures on LOGIC operation',               'Possible Fail-safe mode activation'),
    'SM16': ('Loss of reference control functionality',   'No effect'),
    'SM17': ('Device damage',                             'Device damage'),
    'SM18': ('Cannot trim part properly',                 'Performance/Functionality degredation'),
    'SM20': ('Device damage',                             'Device damage'),
    'SM21': ('Unsynchronised PWM',                        'No effect'),
    'SM22': ('Unintended LED OFF',                        'Unintended LED OFF'),
    'SM23': ('Loss of thermal monitoring capability',     'Possible device damage'),
    'SM24': ('Loss of LED voltage monitoring capability', 'No effect'),
}

# Latent SM reference (col Y) for SM "Fail to detect" rows.
# SM09's latent fault is monitored by SM10 (per 3_ID03_FMEDA.xlsx).
_SM_LATENT_Y = {
    'SM09': 'SM10',
}


def load_sm_j_from_reference(ref_path: str) -> dict:
    """
    Read SM I/J values from the FMEDA sheet of the reference workbook.
    Returns { 'SM01': ('col_I', 'col_J'), ... }

    IMPORTANT: The built-in _SM_J_BUILTIN is the authoritative source for
    known SM codes.  This function ONLY adds entries for SM codes that are
    NOT already in the built-in dict.  This prevents accidental overwrite
    from a shifted or partially-generated reference file.
    """
    sm_j = dict(_SM_J_BUILTIN)  # authoritative built-in — do not overwrite

    if not os.path.exists(ref_path):
        return sm_j

    try:
        wb = openpyxl.load_workbook(ref_path, data_only=True)
        if 'FMEDA' not in wb.sheetnames:
            return sm_j
        ws = wb['FMEDA']
        current_d = None
        for row in ws.iter_rows(min_row=20, max_row=ws.max_row):
            row_data = {}
            for c in row:
                if hasattr(c, 'column_letter') and c.value is not None:
                    row_data[c.column_letter] = c.value
            if 'D' in row_data and str(row_data['D']).strip():
                current_d = str(row_data['D']).strip()
            g = str(row_data.get('G', '')).strip().lower()
            if current_d and re.match(r'SM\d+', current_d) and 'fail to detect' in g:
                # Only add if not already known
                if current_d not in sm_j:
                    i_val = str(row_data.get('I', '')).strip()
                    j_val = str(row_data.get('J', '')).strip()
                    if i_val or j_val:
                        sm_j[current_d] = (i_val, j_val)
    except Exception as e:
        print(f"  [SM-J] Warning: {e}")

    return sm_j


# ═══════════════════════════════════════════════════════════════════════════════
# MEMO LOGIC  (deterministic — no LLM)
# ═══════════════════════════════════════════════════════════════════════════════

_BLOCK_NORM = {
    'SW_BANKX': 'SW_BANK', 'SW_BANK_X': 'SW_BANK', 'SW_BANKx': 'SW_BANK',
    'SW_BANK_1': 'SW_BANK', 'SW_BANK_2': 'SW_BANK',
    'SW_BANK_3': 'SW_BANK', 'SW_BANK_4': 'SW_BANK',
    'CNSN': 'CSNS', 'CS': 'CSNS',
    'DIETEMP': 'TEMP',
    'VEGA': 'CP',
}


def _norm_block(code: str) -> str:
    c = code.strip().upper()
    return _BLOCK_NORM.get(c, c)


def extract_blocks_from_ic_effect(ic_effect: str) -> list:
    if not ic_effect or ic_effect.strip() in ('No effect', ''):
        return []
    blocks = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
    return [_norm_block(b) for b in blocks if b.upper() not in ('NONE', '')]


def determine_memo(ic_effect: str, block_to_sms: dict,
                   code: str = '', mode_str: str = '') -> tuple:
    """
    Returns (memo, matching_sms_list).
    Applies K override logic before SM-list check.
    """
    # Apply K override first (deterministic rules)
    k_override = _lookup_k_override(code, mode_str)
    if k_override == 'O':
        return 'O', []

    if not ic_effect or ic_effect.strip() in ('No effect', ''):
        return 'O', []

    affected = extract_blocks_from_ic_effect(ic_effect)
    if not affected:
        # K override might still force X (e.g. SW_BANK res_low)
        if k_override == 'X':
            return 'X', []
        return 'O', []

    matching_sms = []
    for block in affected:
        for sm in block_to_sms.get(block, []):
            if sm not in matching_sms:
                matching_sms.append(sm)

    if k_override == 'X':
        return 'X', matching_sms

    memo = 'X' if matching_sms else 'O'
    return memo, matching_sms


# ═══════════════════════════════════════════════════════════════════════════════
# PER-BLOCK SM MAP  (determines col S/Y and U)
# ═══════════════════════════════════════════════════════════════════════════════
#
# This is the reference map from 3_ID03_FMEDA.xlsx.
# Keys use normalised block codes + j_type.  SW_BANK_N all map to SW_BANK entries.

_BLOCK_SM_MAP = {
    # REF: stuck/float/incorrect → SM17 (thermal); accuracy/drift → SM11 (OSC-based) not SM17
    ('REF',       'stuck'):       'SM01 SM15 SM16 SM17',
    ('REF',       'float'):       'SM01 SM15 SM16 SM17',
    ('REF',       'incorrect'):   'SM01 SM15 SM16 SM17',
    ('REF',       'accuracy'):    'SM01 SM11 SM15 SM16',   # SM11 not SM17 for accuracy/drift
    ('REF',       'default'):     'SM01 SM15 SM16 SM17',
    ('BIAS',      'default'):     'SM11 SM15 SM16',
    ('LDO',       'ov'):          'SM11 SM20',
    ('LDO',       'uv'):          'SM11 SM15',
    ('LDO',       'accuracy'):    'SM11 SM15 SM20',
    ('LDO',       'default'):     'SM11 SM15 SM20',
    ('OSC',       'default'):     'SM09 SM10 SM11',
    ('TEMP',      'default'):     'SM17 SM23',
    ('CSNS',      'default'):     '',
    ('ADC',       'stuck'):       'SM08 SM16 SM17 SM23',
    ('ADC',       'float'):       'SM08 SM16 SM17 SM23',
    ('ADC',       'default'):     '',
    ('CP',        'ov'):          '',
    ('CP',        'uv'):          'SM14 SM22',
    ('CP',        'default'):     'SM14 SM22',
    ('LOGIC',     'default'):     'SM10 SM11 SM12 SM18',
    ('INTERFACE', 'default'):     '',
    ('TRIM',      'omission'):    'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
    ('TRIM',      'commission'):  'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
    ('TRIM',      'incorrect'):   'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
    ('TRIM',      'default'):     'SM01 SM02 SM09 SM11 SM15 SM16 SM18 SM20 SM23',
    # SW_BANK: stuck → SM04 SM06 SM08 (no SM05); float → SM03 SM06 SM24; res_high → SM03 SM06 SM24
    ('SW_BANK',   'stuck'):       'SM04 SM06 SM08',
    ('SW_BANK',   'float'):       'SM03 SM06 SM24',
    ('SW_BANK',   'res_high'):    'SM03 SM06 SM24',
    ('SW_BANK',   'res_low'):     '',
    ('SW_BANK',   'timing'):      '',
    ('SW_BANK',   'default'):     'SM04 SM06 SM08',
}


def compute_sm_columns(ic_effect: str, block_to_sms: dict, sm_coverage: dict,
                       fmeda_code: str = '', mode_str: str = '') -> tuple:
    """Returns (sm_string, coverage_value) for col S/Y and col U."""
    if not ic_effect or ic_effect.strip() == 'No effect':
        return '', ''

    j_type = _j_type(fmeda_code, mode_str)
    norm_code = 'SW_BANK' if re.match(r'SW_BANK', fmeda_code, re.IGNORECASE) else fmeda_code

    sm_str = (_BLOCK_SM_MAP.get((norm_code, j_type))
              or _BLOCK_SM_MAP.get((norm_code, 'default')))

    if sm_str is None:
        # Fallback: SM-list intersection
        affected = re.findall(r'^\s*•\s*([A-Z_a-z0-9]+)', ic_effect, re.MULTILINE)
        normed = []
        for b in affected:
            b = b.strip().upper()
            b = re.sub(r'SW_BANK[_X\d]*', 'SW_BANK', b)
            b = re.sub(r'CSNS|CNSN|CS', 'CSNS', b)
            if b not in ('NONE', 'VEGA', ''):
                normed.append(b)
        matching = []
        for block in normed:
            for sm in block_to_sms.get(block, []):
                if sm not in matching:
                    matching.append(sm)
        matching.sort(key=lambda s: int(re.search(r'\d+', s).group()) if re.search(r'\d+', s) else 0)
        sm_str = ' '.join(matching)

    if not sm_str:
        return '', ''

    valid = [0.99, 0.9, 0.6]
    def nearest(v):
        return min(valid, key=lambda x: abs(x - v))

    coverages = [nearest(sm_coverage.get(sm, 0.9)) for sm in sm_str.split()]
    max_cov = max(coverages) if coverages else 0.9
    return sm_str, max_cov


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 1  —  Block → IEC part mapper
# ═══════════════════════════════════════════════════════════════════════════════

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

# SW_BANK uses driver-specific mode descriptions — NOT generic signal-level IEC language.
# These 6 modes are identical across SW_BANK_1, SW_BANK_2, SW_BANK_3, SW_BANK_4, etc.
# Built from 3_ID03_FMEDA.xlsx FM_TTL_77–82 (and repeated for each bank).
SW_BANK_MODES = [
    'Driver is stuck in ON or OFF state',
    'Driver is floating (i.e. open circuit, tri-stated)',
    'Driver resistance too high when turned on',
    'Driver resistance too low when turned off',
    'Driver turn-on time too fast or too slow',
    'Driver turn-off time too fast or too slow',
]

# CSNS uses the same generic op-amp sequence as REF/TEMP — NOT the shifted
# op-amp variant the IEC table sometimes produces.
CSNS_MODES = [
    'Output is stuck (i.e. high or low)',
    'Output is floating (i.e. open circuit)',
    'Incorrect output voltage value (i.e. outside the expected range)',
    'Output voltage accuracy too low, including drift',
    'Output voltage affected by spikes',
    'Output voltage oscillation within the expected range',
    'Incorrect start-up time (i.e. outside the expected range)',
    'Quiescent current exceeding the maximum value',
]


def agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache):
    ck = "agent1__" + json.dumps([b['name'] for b in blk_blocks])
    if not SKIP_CACHE and ck in cache:
        print("  [Agent 1] Loaded from cache")
        result = cache[ck]
        _append_sm_blocks(result, sm_blocks)
        return result

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

    # Replace LLM-generated modes with verbatim IEC table modes
    iec_idx = {p['part_name']: p['entries'][0]['modes'] for p in iec_table}
    for b in result:
        iec_part = b.get('iec_part', '')
        if iec_part in iec_idx:
            b['modes'] = iec_idx[iec_part]
        else:
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

    # Apply mode overrides — fixed lists for specific block types
    for b in result:
        code = b.get('fmeda_code', '')
        if code in FMEDA_MODE_OVERRIDES:
            b['modes'] = FMEDA_MODE_OVERRIDES[code]
        elif re.match(r'SW_BANK', code, re.IGNORECASE):
            # All SW_BANK_N blocks use identical driver-specific mode descriptions
            b['modes'] = SW_BANK_MODES
        elif code == 'CSNS':
            # CSNS uses the standard op-amp sequence (same as REF/TEMP)
            b['modes'] = CSNS_MODES

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
VERIFIED EXAMPLES FROM A REAL AUTOMOTIVE IC FMEDA (col I ONLY):

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


def agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage,
                            sm_addressed, cache, learned_j, sm_j_map, learned_i=None):
    """Generate col I (IC effect), col J (system effect), col K (memo) for all blocks."""
    if learned_i is None:
        learned_i = {}

    active = [b for b in blocks if not b.get('is_duplicate') and not b.get('is_sm')]
    chip_ctx = "\n".join(
        f"  {b['fmeda_code']:<12} {b['name']:<35} | {b.get('function','')[:80]}"
        for b in active
    )

    tsr_ctx = "\n".join(
        f"  {t['id']}: {t['description']}"
        for t in tsr_list
    ) if tsr_list else "  (no TSR data)"

    result = []
    for block in blocks:
        code  = block['fmeda_code']
        name  = block['name']
        modes = block.get('modes', [])

        # SM blocks — rows filled from sm_j_map
        if block.get('is_sm'):
            rows = _sm_rows(code, sm_j_map)
            result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
            print(f"  [Agent 2] {code:<12} SM — (2 rows)")
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
            # Re-apply ALL deterministic corrections on cached rows
            for row in rows:
                mode_g = row.get('G', '')
                # Always overwrite I with deterministic lookup (fixes cached LLM I values)
                det_i = lookup_i(code, mode_g, learned_i)
                if det_i is not None:
                    row['I'] = det_i
                row['J'] = resolve_j(code, mode_g, learned_j)
                k_override = _lookup_k_override(code, mode_g)
                if k_override:
                    row['K'] = k_override
            print(f"  [Agent 2] {code:<12} cache ({len(rows)} rows, I/J/K refreshed)")
            result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
            continue

        rows = _llm_block_effects(block, chip_ctx, tsr_ctx, modes,
                                  block_to_sms, sm_coverage, sm_addressed,
                                  learned_j, learned_i)
        cache[ck] = rows
        save_cache(cache)
        result.append({'fmeda_code': code, 'user_name': name, 'rows': rows})
        print(f"  [Agent 2] {code:<12} {len(rows)} rows (deterministic I/J + LLM fallback)")
        time.sleep(0.3)

    return result


_DOWNSTREAM = {
    'REF':       'BIAS, ADC, TEMP, LDO, OSC — all use the reference voltage',
    'BIAS':      'ADC, TEMP, LDO, OSC, SW_BANKx, CP, CSNS — all receive bias currents',
    'LDO':       'OSC (LDO powers the oscillator supply rail)',
    'OSC':       'LOGIC, INTERFACE — clock signal drives all digital logic',
    'TEMP':      'ADC (TEMP voltage read by ADC), SW_BANK_x (DIETEMP controls output enable)',
    'CSNS':      'ADC (CSNS output is digitized by ADC for current monitoring)',
    'ADC':       'SW_BANK_x (ADC DIETEMP result controls switch enable), LOGIC',
    'CP':        'SW_BANK_x (charge pump supplies gate drive voltage for all switches)',
    'LOGIC':     'SW_BANK_X (LOGIC drives all switch banks), OSC',
    'INTERFACE': 'LOGIC, ADC (SPI writes configure DAC and read ADC results)',
    'TRIM':      'REF, LDO, BIAS, OSC, SW_BANK, DIETEMP — trim data calibrates all analog blocks',
}


def _llm_block_effects(block, chip_ctx, tsr_ctx, modes,
                       block_to_sms, sm_coverage, sm_addressed,
                       learned_j, learned_i=None):
    """
    Generate I/J/K rows for one block.

    Strategy for col I (the main accuracy target):
      1. lookup_i() — BLOCK_I_MAP + learned_i exact match.  No LLM needed.
      2. For modes where lookup_i returns None (unknown block/mode combo),
         call LLM with a very tight few-shot prompt focused on sub-effects.
      3. Merge: deterministic rows first, LLM fills gaps.
    """
    if learned_i is None:
        learned_i = {}

    code = block['fmeda_code']
    name = block['name']
    func = block.get('function', '')
    n    = len(modes)

    # --- Pass 1: resolve everything deterministically ---------------------
    det_rows = []       # rows where I is known
    llm_indices = []    # indices where LLM is needed for I

    for i, mode in enumerate(modes):
        det_i = lookup_i(code, mode, learned_i)
        if det_i is not None:
            det_rows.append((i, mode, det_i))
        else:
            llm_indices.append(i)

    # --- Pass 2: LLM only for unknown modes (usually empty for known blocks) --
    llm_results = {}   # index → ic string
    if llm_indices:
        unknown_modes = [modes[i] for i in llm_indices]
        nu = len(unknown_modes)
        downstream_hint = _DOWNSTREAM.get(code,
            'Review all blocks — consider which ones depend on this block output signal')

        prompt = f"""You are completing an FMEDA table for an automotive IC (ISO 26262 / AEC-Q100).

{FEW_SHOT}

ALL BLOCKS IN THIS CHIP:
{chip_ctx}

BLOCK BEING ANALYZED:
  FMEDA Code : {code}
  Block Name : {name}
  Function   : {func}

SIGNAL FLOW HINT: {downstream_hint}

FAILURE MODES TO ANALYZE ({nu} total):
{json.dumps(unknown_modes, indent=2)}

CRITICAL RULES FOR COMPLETENESS (this is the most common error — do NOT skip sub-effects):
  • For EACH affected block, list EVERY individual sub-effect on a separate "    - " line.
  • Example for REF stuck:
      • BIAS
          - Output reference voltage is stuck
          - Output reference current is stuck
          - Output bias current is stuck
          - Quiescent current exceeding the maximum value
    NOT just:  • BIAS\\n    - Output reference voltage is stuck
  • OSC modes: always add "    - Oscillation does not start" or "    - Frequency out of spec."
  • BIAS modes: always include CNSN as affected block with "    - Incorrect reading."
  • TEMP stuck: include BOTH ADC ("TEMP output is stuck low") AND SW_BANK_x ("SW is stuck in off state (DIETEMP)")
  • ADC stuck/float: include BOTH SW_BANK_x AND self (ADC) with BGR/DIETEMP/CS measurements

SAFE MODES (always "No effect"):
  "spikes", "oscillation within", "start-up time", "jitter", "duty cycle",
  "quiescent current", "settling time"

{IC_FORMAT}

Return a JSON array with EXACTLY {nu} objects:
[
  {{"G": "<exact failure mode string>", "I": "<col I: IC output effect>"}},
  ...
]
Return ONLY the JSON array:"""

        raw    = query_llm(prompt, temperature=0.05)
        parsed = parse_json(raw)
        if isinstance(parsed, list) and len(parsed) >= nu:
            for pos, orig_i in enumerate(llm_indices):
                llm_results[orig_i] = str(parsed[pos].get('I', 'No effect')).strip()
        else:
            # LLM failed — use safe fallback for unknown modes
            for orig_i in llm_indices:
                j_type = _j_type(code, modes[orig_i])
                llm_results[orig_i] = 'No effect' if j_type == 'safe' else ''

    # --- Merge all rows in original order ---------------------------------
    rows = []
    det_map = {i: (mode, ic) for i, mode, ic in det_rows}

    for i, mode in enumerate(modes):
        if i in det_map:
            ic = det_map[i][1]
        else:
            ic = llm_results.get(i, 'No effect')

        sys_  = resolve_j(code, mode, learned_j)
        k_override = _lookup_k_override(code, mode)
        if ic in ('No effect', ''):
            memo = 'O'
        elif k_override is not None:
            memo = k_override
        else:
            memo, _ = determine_memo(ic, block_to_sms, code, mode)

        rows.append(_build_row(mode, ic, sys_, memo, block_to_sms, sm_coverage,
                               fmeda_code=code))

    if not rows:
        print(f"    No rows generated for {code} — using fallback")
        rows = _fallback_rows(modes, block_to_sms, sm_coverage, sm_addressed,
                              code, learned_j, learned_i)

    llm_count = len(llm_indices)
    det_count = n - llm_count
    print(f"    {code}: {det_count} det + {llm_count} LLM = {n} rows")
    return rows


def _build_row(canonical_mode, ic, sys_, memo, block_to_sms=None, sm_coverage=None, **kwargs):
    ic_clean = ic.strip()
    if ic_clean in ('No effect', ''):
        memo = 'O'

    # col P: only pure 'X' (not Latent) → Y
    sp = 'Y' if memo == 'X' else 'N'
    # col R: safe if K=O
    pct_safe = 1 if not memo.startswith('X') else 0

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
        'S': sm_str,
        'T': '',
        'U': coverage,
        'V': '',
        'X': 'Y' if memo.startswith('X') else 'N',
        'Y': sm_str,
        'Z': '', 'AA': '', 'AB': '', 'AD': '',
    }


def _fallback_rows(modes, block_to_sms, sm_coverage=None, sm_addressed=None,
                   fmeda_code='', learned_j=None, learned_i=None):
    if learned_j is None:
        learned_j = {}
    if learned_i is None:
        learned_i = {}
    SAFE_KW = ['spike', 'oscillation within', 'start-up', 'jitter', 'duty cycle',
               'quiescent', 'settling', 'false detection']
    rows = []
    for mode in modes:
        safe = any(k in mode.lower() for k in SAFE_KW)
        # Try deterministic I first
        det_i = lookup_i(fmeda_code, mode, learned_i)
        if det_i is not None:
            ic = det_i
        else:
            ic = 'No effect' if safe else ''
        sys_ = resolve_j(fmeda_code, mode, learned_j)
        k_override = _lookup_k_override(fmeda_code, mode)
        if ic in ('No effect', ''):
            memo = 'O'
        elif k_override is not None:
            memo = k_override
        else:
            memo, _ = determine_memo(ic, block_to_sms, fmeda_code, mode)
        rows.append(_build_row(mode, ic, sys_, memo,
                               block_to_sms, sm_coverage, fmeda_code=fmeda_code))
    return rows


def _sm_rows(sm_code: str, sm_j_map: dict) -> list:
    """
    SM blocks: 2 rows — 'Fail to detect' and 'False detection'.
    I/J values from sm_j_map (authoritative built-in, not overwritten by reference).
    Y col (latent SM reference) from _SM_LATENT_Y where applicable.
    """
    ic, sys_ = sm_j_map.get(sm_code, ('Loss of safety mechanism functionality',
                                       'Fail-safe mode active'))
    latent_y = _SM_LATENT_Y.get(sm_code, '')
    return [
        # Fail to detect: K=X(Latent), P=N, X=Y
        {'G': 'Fail to detect',  'I': ic,          'J': sys_,        'K': 'X (Latent)',
         'O': 1, 'P': 'N', 'R': 0, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'Y', 'Y': latent_y, 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
        # False detection: K=O, P=N, X=N
        {'G': 'False detection', 'I': 'No effect', 'J': 'No effect', 'K': 'O',
         'O': 1, 'P': 'N', 'R': 1, 'S': '', 'T': '', 'U': '', 'V': '',
         'X': 'N', 'Y': '', 'Z': '', 'AA': '', 'AB': '', 'AD': ''},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3  —  Template Writer  (deterministic)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_fit_values(code, n_modes, block_fit_rates, row_memo, row_U, sm_coverage):
    block_fit = block_fit_rates.get(code, 0.0)
    mode_fit  = block_fit / n_modes if n_modes > 0 and block_fit > 0 else 0.0

    if not row_memo.startswith('X'):
        return block_fit, mode_fit, mode_fit, 0.0, None, None

    U = float(row_U) if row_U else 0.0

    V = mode_fit * (1.0 - U)

    BLOCKS_AA_08 = {'LDO', 'TEMP', 'ADC', 'CP', 'LOGIC',
                    'SW_BANK_1', 'SW_BANK_2', 'SW_BANK_3', 'SW_BANK_4', 'SM09'}
    if not U:
        AA = 0.0
    elif code in BLOCKS_AA_08:
        AA = 0.8
    elif U >= 0.99:
        AA = 1.0
    elif U >= 0.85:
        AA = 0.8
    else:
        AA = U

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

            fit_blk, fit_mode, fit_q, fit_v, fit_aa, fit_ab = _compute_fit_values(
                code, n_modes_total, block_fit_rates, memo, u_val, sm_coverage
            )

            _write(idx, 'E',  row_num, fit_blk if (is_first and fit_blk > 0) else None)
            _write(idx, 'F',  row_num, fit_mode if fit_mode > 0 else None)
            _write(idx, 'G',  row_num, rd.get('G', ''),          wrap=True)
            _write(idx, 'H',  row_num, None)
            _write(idx, 'I',  row_num, rd.get('I', 'No effect'), wrap=True)
            _write(idx, 'J',  row_num, rd.get('J', 'No effect'), wrap=True)
            _write(idx, 'K',  row_num, memo)

            o_val = 0.5 if (code == 'TEMP' and memo.startswith('X')) else 1
            _write(idx, 'O',  row_num, o_val)
            _write(idx, 'P',  row_num, sp)
            _write(idx, 'Q',  row_num, fit_q if fit_q > 0 else None)
            _write(idx, 'R',  row_num, pct_safe)
            _write(idx, 'S',  row_num, rd.get('S') or None,      wrap=False)
            _write(idx, 'T',  row_num, rd.get('T') or None,      wrap=False)
            _write(idx, 'U',  row_num, u_val if u_val not in ('', None) else None)
            _write(idx, 'V',  row_num, fit_v if (fit_v is not None and fit_v > 0) else None)
            _write(idx, 'X',  row_num, rd.get('X', 'Y' if memo.startswith('X') else 'N'))
            _write(idx, 'Y',  row_num, rd.get('Y') or None,      wrap=False)
            _write(idx, 'Z',  row_num, rd.get('Z') or None,      wrap=True)
            _write(idx, 'AA', row_num, fit_aa if fit_aa is not None else None)

            if fit_ab is not None:
                _write(idx, 'AB', row_num, fit_ab if fit_ab > 0 else 0)

            # AD comment
            sm_str = rd.get('S', '') or ''
            if sm_str and memo.startswith('X'):
                sms = sm_str.split()
                sms_sorted = sorted(sms,
                    key=lambda s: sm_coverage.get(s, 0.0) if sm_coverage else 0.0,
                    reverse=True)
                sm_mention = ' '.join(sms_sorted[:2]) if len(sms_sorted) >= 2 else (sms_sorted[0] if sms_sorted else '')
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
    print("║      FMEDA Multi-Agent Pipeline  v2           ║")
    print("╚═══════════════════════════════════════════════╝")
    print(f"\n  Dataset   : {DATASET_FILE}")
    print(f"  IEC table : {IEC_TABLE_FILE}")
    print(f"  Template  : {TEMPLATE_FILE}")
    print(f"  Reference : {REFERENCE_FMEDA}")
    print(f"  Model     : {OLLAMA_MODEL}")
    print(f"  Output    : {OUTPUT_FILE}\n")

    cache = load_cache()

    # ── Step 0: Read all inputs ────────────────────────────────────────────────
    print("━━━ Step 0 : Reading inputs ━━━")
    blk_blocks, sm_blocks, tsr_list = read_dataset()
    iec_table = read_iec_table()
    sm_coverage, sm_addressed, block_to_sms = read_sm_list_from_template()

    block_fit_rates = {}
    for candidate in [TEMPLATE_FILE, REFERENCE_FMEDA]:
        if os.path.exists(candidate):
            try:
                wb_fit = openpyxl.load_workbook(candidate, data_only=True)
                block_fit_rates = read_block_fit_rates(wb_fit)
                if block_fit_rates:
                    print(f"  FIT rates loaded from {candidate}: {len(block_fit_rates)} blocks")
                    break
            except Exception:
                pass

    # Learn J patterns from reference FMEDA (runtime — adapts to chip changes)
    learned_j = learn_j_from_reference(REFERENCE_FMEDA)

    # Learn I patterns from reference FMEDA (supplements BLOCK_I_MAP for chip-specific text)
    learned_i = learn_i_from_reference(REFERENCE_FMEDA)

    # Load SM J values from reference FMEDA
    sm_j_map = load_sm_j_from_reference(REFERENCE_FMEDA)

    print(f"  BLK: {len(blk_blocks)}  SM: {len(sm_blocks)}  TSR: {len(tsr_list)}  "
          f"IEC: {len(iec_table)}  SM entries: {len(sm_coverage)}  FIT blocks: {len(block_fit_rates)}")
    print("  block_to_sms:")
    for b, sms in sorted(block_to_sms.items()):
        print(f"    {b:<15} → {sms}")

    # ── Agent 1 ──────────────────────────────────────────────────────────────
    print("\n━━━ Agent 1 : Block → IEC part mapper (LLM) ━━━")
    blocks = agent1_map_blocks(blk_blocks, sm_blocks, iec_table, cache)
    print("\n  Mapping result:")
    for b in blocks:
        tag = " [DUP]" if b.get('is_duplicate') else (" [SM]" if b.get('is_sm') else "")
        print(f"    {b['name']:<35} → {b['fmeda_code']:<12} "
              f"| {b.get('iec_part','')} ({len(b.get('modes',[]))} modes){tag}")

    # ── Agent 2 ──────────────────────────────────────────────────────────────
    print("\n━━━ Agent 2 : Deterministic I/J/K (LLM fallback for unknown modes only) ━━━")
    fmeda_data = agent2_generate_effects(blocks, tsr_list, block_to_sms, sm_coverage,
                                         sm_addressed, cache, learned_j, sm_j_map,
                                         learned_i=learned_i)

    print("\n  Col I / J / K spot-check:")
    for block in fmeda_data:
        for row in block['rows']:
            print(f"    {block['fmeda_code']:<12} K={row.get('K','?'):<12} "
                  f"I={repr(row.get('I',''))[:45]}  | {row['G'][:35]}")

    with open(INTERMEDIATE_JSON, 'w', encoding='utf-8') as f:
        json.dump(fmeda_data, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Intermediate JSON → {INTERMEDIATE_JSON}")

    # ── Agent 3 ──────────────────────────────────────────────────────────────
    print("\n━━━ Agent 3 : Template writer (deterministic) ━━━")
    agent3_write_template(fmeda_data, block_fit_rates, sm_coverage)

    print("\n✅  Pipeline complete!")
    print(f"    Output       : {OUTPUT_FILE}")
    print(f"    Intermediate : {INTERMEDIATE_JSON}")
    print(f"    Cache        : {CACHE_FILE}")


if __name__ == '__main__':
    run()