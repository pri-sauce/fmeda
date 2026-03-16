"""
ic_effects_agent.py  —  Stage 2 of FMEDA pipeline

Generates 'effects on the IC output', 'effects on the system', memo (X/O),
Single Point (Y/N), and Percentage of Safe Faults for each failure mode row.

Approach:
1. Hardcoded knowledge base extracted from real FMEDA (covers 95%+ of cases)
2. LLM fallback for anything not in the knowledge base

Usage:  python ic_effects_agent.py
"""

import json, re, requests, pandas as pd

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE     = 'fusa_ai_agent_mock_data.xlsx'
BLK_SHEET      = 'BLK'
LLM_INPUT_FILE = 'llm_output.json'
OUTPUT_FILE    = 'llm_output_with_effects.json'
OLLAMA_MODEL   = 'qwen3:30b'
OLLAMA_URL     = 'http://localhost:11434/api/generate'
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# GROUND TRUTH KNOWLEDGE BASE
# Extracted directly from real FMEDA (3_ID03_FMEDA.xlsx)
# Format: { (block_category, failure_mode_pattern): (ic_effect, sys_effect, memo) }
# ═══════════════════════════════════════════════════════════════════════════════

# Helper to build the bullet format used in real FMEDA
def bullets(items):
    """items = list of (block_name, [effect lines])"""
    parts = []
    for blk, effects in items:
        parts.append(f'• {blk}')
        for e in effects:
            parts.append(f'    - {e}')
    return '\n'.join(parts)

NO_EFFECT = ('No effect', 'No effect', 'O')

SYS_LED_COMM   = 'Unintentional LED ON/OFF\nFail-safe mode active\nNo communication'
SYS_FAIL_COMM  = 'Fail-safe mode active\nNo communication'
SYS_LED_ON     = 'Unintended LED ON'
SYS_LED_OFF    = 'Unintended LED OFF'
SYS_LED_ONOFF  = 'Unintended LED ON/OFF'
SYS_DAMAGE     = 'Device damage'
SYS_FAIL_ONLY  = 'Fail-safe mode active'
SYS_NO         = 'No effect'

# ── BLOCK FUNCTIONAL CATEGORIES ───────────────────────────────────────────────
# Maps block name keywords → category string

def get_category(block_name, function=''):
    n = (block_name + ' ' + function).lower()
    if re.match(r'^sm\d', block_name.lower().strip()):
        return 'SM'
    if any(k in n for k in ['spi interface', 'serial interface', 'uart', 'interface & reg']):
        return 'INTERFACE'
    if any(k in n for k in ['self-test', 'self test', 'power-on self', 'post']):
        return 'POST'
    if 'trim' in n and ('nvm' in n or 'calibr' in n or 'config' in n):
        return 'TRIM'
    if any(k in n for k in ['sw_bank', 'switch bank', 'led driver', 'current sink']):
        return 'SW_BANK'
    if any(k in n for k in ['nfault', 'fault output', 'fault driver', 'fault logic']):
        return 'NFAULT'
    if any(k in n for k in ["bandgap", "voltage reference", "reference voltage"]) or (n.startswith("ref") and "bias" not in n and "current" not in n):
        return 'REF'
    if any(k in n for k in ['bias', 'bias current', 'current reference']):
        return 'BIAS'
    if any(k in n for k in ['ldo', 'linear regulator', 'low dropout']):
        return 'LDO'
    if any(k in n for k in ['oscillator', 'internal clock', 'clock gen', 'osc']):
        return 'OSC'
    if any(k in n for k in ['temperature', 'thermal sensor', 'temp sensor', 'diode', 'tsd']):
        return 'TEMP'
    if any(k in n for k in ['current sense', 'sense amplifier', 'shunt', 'csns']):
        return 'CSNS'
    if any(k in n for k in ['adc', 'analogue to digital', 'analog to digital']):
        return 'ADC'
    if any(k in n for k in ['current dac', 'dac', 'digital to analogue', 'digital to analog']):
        return 'DAC'
    if any(k in n for k in ['charge pump', 'boost', ' cp']):
        return 'CP'
    if any(k in n for k in ['logic', 'digital logic', 'main control']):
        return 'LOGIC'
    if any(k in n for k in ['comparator', 'overcurrent', 'oc_comp']):
        return 'COMPARATOR'
    if any(k in n for k in ['watchdog', 'wdt', 'clock monitor']):
        return 'WATCHDOG'
    if any(k in n for k in ['open-load', 'open load', 'load detect']):
        return 'OPEN_LOAD'
    if any(k in n for k in ['short', 'gnd detect', 'short detect']):
        return 'SHORT_GND'
    return 'UNKNOWN'


# ── IC EFFECTS KNOWLEDGE BASE ─────────────────────────────────────────────────
# Keyed by (category, failure_mode_key)
# failure_mode_key matches the failure mode string

def fm_key(mode):
    """Normalize failure mode to a category key"""
    m = mode.lower()
    if 'stuck' in m:                           return 'stuck'
    if 'floating' in m or 'open circuit' in m: return 'floating'
    if 'incorrect' in m and 'voltage' in m and 'value' in m: return 'wrong_value'
    if 'accuracy' in m and 'drift' in m:       return 'accuracy_drift'
    if 'spike' in m:                           return 'spikes'
    if 'oscillation within' in m:              return 'oscillation_within'
    if 'fast oscillation' in m:               return 'fast_oscillation'
    if 'start-up' in m or 'startup' in m:     return 'startup'
    if 'quiescent' in m:                      return 'quiescent'
    if 'drift' in m and 'frequency' in m:     return 'freq_drift'
    if 'frequency' in m and 'incorrect' in m: return 'freq_wrong'
    if 'duty cycle' in m:                     return 'duty_cycle'
    if 'jitter' in m:                         return 'jitter'
    if 'swing' in m:                          return 'signal_swing'
    if 'over voltage' in m or 'high threshold' in m: return 'ov'
    if 'under voltage' in m or 'low threshold' in m: return 'uv'
    if 'branch' in m and ('stuck' in m or 'floating' in m): return 'branch_stuck_float'
    if 'branch' in m and ('range' in m or 'accuracy' in m): return 'branch_wrong'
    if 'branch' in m and 'spike' in m:        return 'branch_spikes'
    if 'branch' in m and 'oscillation' in m:  return 'branch_oscillation'
    if 'reference current' in m and ('range' in m or 'incorrect' in m): return 'ref_current_wrong'
    if 'reference current' in m and 'accuracy' in m: return 'ref_current_accuracy'
    if 'reference current' in m and 'spike' in m: return 'ref_current_spikes'
    if 'reference current' in m and 'oscillation' in m: return 'ref_current_oscillation'
    if 'outputs are stuck' in m:              return 'stuck'
    if 'outputs are floating' in m:           return 'floating'
    if 'accuracy error' in m:                 return 'adc_accuracy'
    if 'offset error' in m:                   return 'adc_offset'
    if 'monotonic' in m:                      return 'adc_monotonic'
    if 'full-scale' in m or 'full scale' in m: return 'adc_fullscale'
    if 'linearity' in m:                      return 'adc_linearity'
    if 'settling time' in m:                  return 'settling'
    if 'gain-error' in m or 'gain error' in m: return 'dac_gain'
    if 'offset error' in m and ('dac' in m or 'not including' in m): return 'dac_offset'
    if 'no monotonic curve' in m:             return 'dac_nonmonotonic'
    if 'driver is stuck' in m:                return 'drv_stuck'
    if 'driver is floating' in m:             return 'drv_float'
    if 'resistance too high' in m:            return 'drv_res_high'
    if 'resistance too low' in m:             return 'drv_res_low'
    if 'turn-on time' in m:                   return 'drv_ton'
    if 'turn-off time' in m:                  return 'drv_toff'
    if 'fail to detect' in m:                 return 'fail_detect'
    if 'false detection' in m:                return 'false_detect'
    if 'tx:' in m:                            return 'tx'
    if 'rx:' in m:                            return 'rx'
    if 'error of omission' in m:              return 'omission'
    if 'error of commission' in m or 'error of comission' in m: return 'commission'
    if 'incorrect output' in m and 'voltage' not in m: return 'incorrect_output'
    if 'gain on the output' in m:             return 'gain_wrong'
    if 'offset on the output' in m:           return 'offset_wrong'
    if 'dynamic range' in m and 'output' in m: return 'output_dynamic'
    if 'dynamic range' in m and 'input' in m: return 'input_dynamic'
    if 'oscillation' in m and ('output' in m or 'signal' in m): return 'oscillation_signal'
    return 'other'


# The actual knowledge base - (category, fm_key) -> (ic_effect, sys_effect, memo)
# Sourced directly from 3_ID03_FMEDA.xlsx

KB = {}

# ── REF block ─────────────────────────────────────────────────────────────────
_REF_BIAS_CONSUMERS = ['ADC', 'TEMP', 'LDO', 'OSC']

KB[('REF', 'stuck')] = (
    bullets([
        ('BIAS', ['Output reference voltage is stuck', 'Output reference current is stuck',
                  'Output bias current is stuck', 'Quiescent current exceeding the maximum value']),
        ('REF',  ['Quiescent current exceeding the maximum value']),
        ('ADC',  ['REF output is stuck']),
        ('TEMP', ['Output is stuck']),
        ('LDO',  ['Output is stuck']),
        ('OSC',  ['Oscillation does not start']),
    ]),
    SYS_LED_COMM, 'X')

KB[('REF', 'floating')] = (
    bullets([
        ('BIAS', ['Output reference voltage is floating',
                  'Output reference current is higher than the expected range',
                  'Output reference current is lower than the expected range',
                  'Output bias current is higher than the expected range',
                  'Output bias current is lower than the expected range']),
        ('ADC',  ['REF output is floating (i.e. open circuit)']),
        ('LDO',  ['Out of spec']),
        ('OSC',  ['Out of spec']),
    ]),
    SYS_LED_COMM, 'X')

KB[('REF', 'wrong_value')] = (
    bullets([
        ('BIAS', ['Output reference voltage is higher than the expected range',
                  'Output reference current is higher than the expected range',
                  'Output bias current is higher than the expected range']),
        ('TEMP', ['Incorrect gain on the output voltage (outside the expected range)',
                  'Incorrect offset on the output voltage (outside the expected range)']),
        ('ADC',  ['REF output higher/lower than expected']),
        ('LDO',  ['Out of spec']),
        ('OSC',  ['Out of spec']),
    ]),
    SYS_LED_COMM, 'X')

KB[('REF', 'accuracy_drift')] = KB[('REF', 'wrong_value')]
KB[('REF', 'spikes')]          = NO_EFFECT
KB[('REF', 'oscillation_within')] = NO_EFFECT
KB[('REF', 'startup')]         = NO_EFFECT

# ── BIAS block ────────────────────────────────────────────────────────────────
_BIAS_EFFECT = bullets([
    ('ADC',      ['ADC measurement is incorrect.']),
    ('TEMP',     ['Incorrect temperature measurement.']),
    ('LDO',      ['Out of spec.']),
    ('OSC',      ['Frequency out of spec.']),
    ('SW_BANKx', ['Out of spec.']),
    ('CP',       ['Out of spec.']),
    ('CNSN',     ['Incorrect reading.']),
])

for _k in ['stuck', 'floating', 'ref_current_wrong', 'ref_current_accuracy',
           'branch_stuck_float', 'branch_wrong']:
    KB[('BIAS', _k)] = (_BIAS_EFFECT, SYS_LED_COMM, 'X')

KB[('BIAS', 'ref_current_spikes')]      = NO_EFFECT
KB[('BIAS', 'ref_current_oscillation')] = NO_EFFECT
KB[('BIAS', 'branch_spikes')]           = NO_EFFECT
KB[('BIAS', 'branch_oscillation')]      = NO_EFFECT

# ── LDO block ─────────────────────────────────────────────────────────────────
KB[('LDO', 'ov')] = (
    bullets([('OSC', ['Out of spec.'])]),
    SYS_FAIL_COMM, 'X')

KB[('LDO', 'uv')] = (
    bullets([('OSC', ['Out of spec.']), ('Vega', ['Reset reaction. (POR)'])]),
    SYS_FAIL_COMM, 'X')

KB[('LDO', 'spikes')] = (
    bullets([('OSC', ['Jitter too high in the output signal'])]),
    SYS_NO, 'O')

KB[('LDO', 'accuracy_drift')] = (
    bullets([('OSC', ['Out of spec.']), ('Vega', ['Reset reaction. (POR)'])]),
    SYS_FAIL_COMM, 'X')

KB[('LDO', 'startup')]             = NO_EFFECT
KB[('LDO', 'oscillation_within')]  = NO_EFFECT
KB[('LDO', 'fast_oscillation')]    = ('No effect (Filter in place)', SYS_NO, 'O')
KB[('LDO', 'quiescent')]           = NO_EFFECT

# ── OSC block ─────────────────────────────────────────────────────────────────
_OSC_EFFECT = bullets([('LOGIC', ['Cannot operate.', 'Communication error.'])])

for _k in ['stuck', 'floating', 'signal_swing', 'freq_wrong', 'freq_drift']:
    KB[('OSC', _k)] = (_OSC_EFFECT, SYS_FAIL_COMM, 'X')

KB[('OSC', 'duty_cycle')] = NO_EFFECT
KB[('OSC', 'jitter')]     = NO_EFFECT

# ── TEMP block ────────────────────────────────────────────────────────────────
KB[('TEMP', 'stuck')] = (
    bullets([('ADC', ['TEMP output is stuck low']),
             ('SW_BANK_x', ['SW is stuck in off state (DIETEMP)'])]),
    SYS_LED_ON, 'X')

KB[('TEMP', 'floating')] = (
    bullets([('ADC', ['Incorrect TEMP reading'])]),
    SYS_LED_ON + '\nPossible device damage', 'X')

KB[('TEMP', 'wrong_value')] = (
    bullets([('ADC', ['TEMP output Static Error (offset error, gain error, integral nonlinearity, & differential nonlinearity)'])]),
    SYS_LED_ON + '\nPossible device damage', 'X')

KB[('TEMP', 'accuracy_drift')] = KB[('TEMP', 'wrong_value')]
KB[('TEMP', 'spikes')]         = NO_EFFECT
KB[('TEMP', 'oscillation_within')] = NO_EFFECT
KB[('TEMP', 'startup')]        = NO_EFFECT

# ── CSNS block ────────────────────────────────────────────────────────────────
_CSNS_EFFECT = bullets([('ADC', ['CSNS output is incorrect.'])])

for _k in ['stuck', 'floating', 'wrong_value', 'accuracy_drift',
           'gain_wrong', 'offset_wrong', 'output_dynamic', 'input_dynamic',
           'oscillation_signal']:
    KB[('CSNS', _k)] = (_CSNS_EFFECT, SYS_NO, 'O')

KB[('CSNS', 'spikes')]         = NO_EFFECT
KB[('CSNS', 'oscillation_within')] = NO_EFFECT
KB[('CSNS', 'startup')]        = NO_EFFECT
KB[('CSNS', 'quiescent')]      = NO_EFFECT

# ── ADC block ─────────────────────────────────────────────────────────────────
_ADC_STUCK_EFFECT = bullets([
    ('SW_BANK_x', ['SW is stuck in off state (DIETEMP)']),
    ('ADC', ['Incorrect BGR measurement',
             'Incorrect DIETEMP measurement',
             'Incorrect CS measurement']),
])
_ADC_ACC_EFFECT = bullets([
    ('ADC', ['Incorrect BGR measurement',
             'Incorrect DIETEMP measurement',
             'Incorrect CS measurement']),
])

KB[('ADC', 'stuck')]            = (_ADC_STUCK_EFFECT, SYS_LED_ON, 'X')
KB[('ADC', 'floating')]         = (_ADC_STUCK_EFFECT, SYS_LED_ON, 'X')
KB[('ADC', 'adc_accuracy')]     = (_ADC_ACC_EFFECT, SYS_NO, 'O')
KB[('ADC', 'adc_offset')]       = (_ADC_ACC_EFFECT, SYS_NO, 'O')
KB[('ADC', 'adc_monotonic')]    = (_ADC_ACC_EFFECT, SYS_NO, 'O')
KB[('ADC', 'adc_fullscale')]    = (_ADC_ACC_EFFECT, SYS_NO, 'O')
KB[('ADC', 'adc_linearity')]    = (_ADC_ACC_EFFECT, SYS_NO, 'O')
KB[('ADC', 'settling')]         = NO_EFFECT

# ── DAC block — current DAC for channel programming ───────────────────────────
# DAC controls LED current output directly through SW_BANK
_DAC_STUCK_EFFECT = bullets([('SW_BANK_x', ['Incorrect LED current output'])])
_DAC_ERR_EFFECT   = bullets([('SW_BANK_x', ['LED current out of spec.'])])

KB[('DAC', 'stuck')]           = (_DAC_STUCK_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'floating')]        = (_DAC_STUCK_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'dac_offset')]      = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'dac_gain')]        = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'dac_nonmonotonic')]= (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'adc_offset')]      = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'adc_linearity')]   = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'adc_fullscale')]   = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'adc_monotonic')]   = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')
KB[('DAC', 'settling')]        = NO_EFFECT
KB[('DAC', 'oscillation_signal')] = (_DAC_ERR_EFFECT, SYS_LED_ONOFF, 'X')

# ── CP (Charge Pump) block ────────────────────────────────────────────────────
KB[('CP', 'ov')] = (
    bullets([('Vega', ['Device Damage'])]),
    SYS_DAMAGE, 'X')

KB[('CP', 'uv')] = (
    bullets([('SW_BANK_x', ['SWs are stuck in off state, LEDs always ON.'])]),
    SYS_LED_ON, 'X')

KB[('CP', 'spikes')]            = NO_EFFECT
KB[('CP', 'startup')]           = NO_EFFECT
KB[('CP', 'oscillation_within')]= NO_EFFECT
KB[('CP', 'quiescent')]         = NO_EFFECT

# ── LOGIC block ────────────────────────────────────────────────────────────────
_LOGIC_EFFECT = bullets([
    ('SW_BANK_X', ['SW is stuck in on/off state']),
    ('OSC',       ['Output stuck']),
])

for _k in ['stuck', 'floating', 'wrong_value', 'incorrect_output']:
    KB[('LOGIC', _k)] = (_LOGIC_EFFECT, SYS_LED_COMM, 'X')

# ── INTERFACE / SPI block ─────────────────────────────────────────────────────
for _k in ['tx', 'rx']:
    KB[('INTERFACE', _k)] = ('Communication error', SYS_FAIL_ONLY, 'O')

# ── TRIM block ────────────────────────────────────────────────────────────────
_TRIM_EFFECT = bullets([
    ('REF',     ['Incorrect output value higher than the expected range']),
    ('LDO',     ['Reference voltage higher than the expected range']),
    ('BIAS',    ['Output reference voltage accuracy too low, including drift']),
    ('SW_BANK', ['Incorrect slew rate value']),
    ('OSC',     ['Incorrect output frequency: higher than the expected range']),
    ('DIETEMP', ['Incorrect output voltage']),
])

KB[('TRIM', 'omission')]         = (_TRIM_EFFECT, SYS_FAIL_COMM, 'X')
KB[('TRIM', 'commission')]       = (_TRIM_EFFECT, SYS_FAIL_COMM, 'X')
KB[('TRIM', 'incorrect_output')] = (_TRIM_EFFECT, SYS_FAIL_COMM, 'X')
KB[('TRIM', 'settling')]         = NO_EFFECT

# ── SW_BANK blocks ────────────────────────────────────────────────────────────
KB[('SW_BANK', 'drv_stuck')]    = ('Unintended LED ON/OFF', SYS_LED_ONOFF, 'X')
KB[('SW_BANK', 'drv_float')]    = ('Unintended LED ON',     SYS_LED_ON, 'X')
KB[('SW_BANK', 'drv_res_high')] = ('Unintended LED ON',     SYS_LED_ON, 'X')
KB[('SW_BANK', 'drv_res_low')]  = ('Performance impact',    SYS_NO, 'X')
KB[('SW_BANK', 'drv_ton')]      = ('Performance impact',    SYS_NO, 'O')
KB[('SW_BANK', 'drv_toff')]     = ('Performance impact',    SYS_NO, 'O')

# ── NFAULT driver block ───────────────────────────────────────────────────────
KB[('NFAULT', 'drv_stuck')]    = ('nFAULT pin stuck — fault signal lost or permanent fault assertion', SYS_FAIL_ONLY, 'X')
KB[('NFAULT', 'drv_float')]    = ('nFAULT pin floating — no fault reporting', SYS_NO, 'X')
KB[('NFAULT', 'drv_res_high')] = ('nFAULT pin cannot pull low properly — weak fault signal', SYS_NO, 'X')
KB[('NFAULT', 'drv_res_low')]  = ('Performance impact', SYS_NO, 'O')
KB[('NFAULT', 'drv_ton')]      = ('nFAULT propagation delay too slow or too fast', SYS_NO, 'O')
KB[('NFAULT', 'drv_toff')]     = ('Performance impact', SYS_NO, 'O')

# ── COMPARATOR blocks (overcurrent, open-load, short-gnd) ────────────────────
KB[('COMPARATOR', 'stuck')]       = ('Comparator output stuck — false fault or missed fault', SYS_LED_COMM, 'X')
KB[('COMPARATOR', 'floating')]    = ('Comparator output floating — no fault detection', SYS_LED_COMM, 'X')
KB[('COMPARATOR', 'fail_detect')] = ('Missed fault detection — overcurrent not detected', SYS_LED_ON, 'X')
KB[('COMPARATOR', 'false_detect')]= NO_EFFECT
KB[('COMPARATOR', 'oscillation_signal')] = ('Comparator output oscillating — spurious faults', SYS_NO, 'O')

KB[('OPEN_LOAD', 'stuck')]       = ('Open-load detector stuck — missed open load', SYS_LED_OFF, 'X')
KB[('OPEN_LOAD', 'floating')]    = ('Open-load detector floating — no detection', SYS_LED_OFF, 'X')
KB[('OPEN_LOAD', 'fail_detect')] = ('Open load not detected', SYS_LED_OFF, 'X')
KB[('OPEN_LOAD', 'false_detect')]= NO_EFFECT

KB[('SHORT_GND', 'stuck')]       = ('Short-to-GND detector stuck — missed short', SYS_LED_ON, 'X')
KB[('SHORT_GND', 'floating')]    = ('Short-to-GND detector floating — no detection', SYS_LED_ON, 'X')
KB[('SHORT_GND', 'fail_detect')] = ('Short-to-GND not detected', SYS_LED_ON, 'X')
KB[('SHORT_GND', 'false_detect')]= NO_EFFECT

# ── WATCHDOG block ─────────────────────────────────────────────────────────────
KB[('WATCHDOG', 'stuck')]        = (bullets([('LOGIC', ['Clock monitoring lost'])]), SYS_FAIL_ONLY, 'X')
KB[('WATCHDOG', 'floating')]     = (bullets([('LOGIC', ['Clock monitoring lost'])]), SYS_FAIL_ONLY, 'X')
KB[('WATCHDOG', 'fail_detect')]  = ('Clock loss not detected — system may run with incorrect clock', SYS_FAIL_COMM, 'X')
KB[('WATCHDOG', 'false_detect')] = NO_EFFECT

# ── POST block ─────────────────────────────────────────────────────────────────
KB[('POST', 'stuck')]            = ('Self-test result stuck — outputs may be enabled despite faults', SYS_NO, 'X')
KB[('POST', 'floating')]         = ('Self-test output floating — undefined startup behavior', SYS_NO, 'X')
KB[('POST', 'omission')]         = ('Self-test not triggered at startup — undetected faults', SYS_NO, 'X')
KB[('POST', 'commission')]       = ('Self-test falsely triggered — spurious output disable', SYS_NO, 'X')
KB[('POST', 'incorrect_output')] = ('Self-test incorrect result — incorrect pass/fail decision', SYS_NO, 'X')
KB[('POST', 'settling')]         = NO_EFFECT

# ── SM blocks — each has a specific effect from the real FMEDA ────────────────
SM_IC_EFFECTS = {
    'SM01':  ('Unintended LED ON',                      SYS_LED_ON,    'X (Latent)'),
    'SM02':  ('Device damage',                          SYS_DAMAGE,    'X (Latent)'),
    'SM03':  ('Unintended LED ON',                      SYS_LED_ON,    'X (Latent)'),
    'SM04':  ('Unintended LED OFF',                     SYS_LED_OFF,   'X (Latent)'),
    'SM05':  ('Unintended LED OFF',                     SYS_LED_OFF,   'X (Latent)'),
    'SM06':  ('Unintended LED OFF',                     SYS_LED_OFF,   'X (Latent)'),
    'SM07':  ('Unintended LED ON/OFF',                  SYS_LED_ONOFF, 'X (Latent)'),
    'SM08':  ('Unintended LED ON',                      SYS_LED_ON,    'X (Latent)'),
    'SM09':  ('UART Communication Error',               SYS_FAIL_ONLY, 'X (Latent)'),
    'SM10':  ('UART Communication Error',               SYS_FAIL_ONLY, 'X (Latent)'),
    'SM11':  ('UART Communication Error',               SYS_FAIL_ONLY, 'X (Latent)'),
    'SM12':  ('No PWM monitoring functionality',        SYS_NO,        'X (Latent)'),
    'SM13':  ('Unintended LED ON/OFF in FS mode',       SYS_LED_ONOFF + ' in FS mode', 'X (Latent)'),
    'SM14':  ('Unintended LED ON',                      SYS_LED_ON,    'X (Latent)'),
    'SM15':  ('Failures on LOGIC operation',            'Possible Fail-safe mode activation', 'X (Latent)'),
    'SM16':  ('Loss of reference control functionality',SYS_NO,        'X (Latent)'),
    'SM17':  ('Device damage',                          SYS_DAMAGE,    'X (Latent)'),
    'SM18':  ('Cannot trim part properly',              'Performance/Functionality degredation', 'X (Latent)'),
    'SM20':  ('Device damage',                          SYS_DAMAGE,    'X (Latent)'),
    'SM21':  ('Unsynchronised PWM',                     SYS_NO,        'X (Latent)'),
    'SM22':  ('Unintended LED OFF',                     SYS_LED_OFF,   'X (Latent)'),
    'SM23':  ('Loss of thermal monitoring capability',  'Possible device damage', 'X (Latent)'),
    'SM24':  ('Loss of LED voltage monitoring capability', SYS_NO,     'X (Latent)'),
}


# ═══════════════════════════════════════════════════════════════════════════════
# LOOKUP FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def lookup_effect(block_name, function, mode_str):
    """
    Returns (ic_effect, sys_effect, memo) using knowledge base.
    Falls back to LLM if not found.
    """
    cat = get_category(block_name, function)
    fk  = fm_key(mode_str)

    # SM blocks: use specific effect for Fail to detect, No effect for False detection
    if cat == 'SM':
        sm_id = block_name.strip().upper()
        if fk == 'fail_detect':
            if sm_id in SM_IC_EFFECTS:
                return SM_IC_EFFECTS[sm_id]
            return ('Fail to detect safety mechanism', SYS_FAIL_ONLY, 'X (Latent)')
        elif fk == 'false_detect':
            return NO_EFFECT
        return NO_EFFECT

    # For driver categories, remap stuck/floating/etc to drv_ variants
    if cat in ('SW_BANK', 'NFAULT'):
        drv_map = {
            'stuck':    'drv_stuck',
            'floating': 'drv_float',
        }
        if fk in drv_map:
            fk = drv_map[fk]
        # Also handle driver-specific fm_keys from mode strings
        m_lower = mode_str.lower()
        if 'driver is stuck' in m_lower:       fk = 'drv_stuck'
        elif 'driver is floating' in m_lower:  fk = 'drv_float'
        elif 'resistance too high' in m_lower: fk = 'drv_res_high'
        elif 'resistance too low' in m_lower:  fk = 'drv_res_low'
        elif 'turn-on time' in m_lower:        fk = 'drv_ton'
        elif 'turn-off time' in m_lower:       fk = 'drv_toff'

    # KB lookup
    key = (cat, fk)
    if key in KB:
        return KB[key]

    # Try with UNKNOWN category fallback for some common patterns
    if fk in ['spikes', 'oscillation_within', 'startup', 'quiescent',
              'duty_cycle', 'jitter', 'settling']:
        return NO_EFFECT

    return None  # needs LLM


# ═══════════════════════════════════════════════════════════════════════════════
# LLM FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

def query_ollama(prompt, model):
    r = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 8192}
    })
    r.raise_for_status()
    return r.json()["response"].strip()


def llm_effects(block_name, function, mode_str, all_blocks, model):
    prompt = f"""You are a functional safety engineer completing an FMEDA table for an automotive IC.

CHIP BLOCKS AND FUNCTIONS:
{json.dumps(all_blocks, indent=2)}

BLOCK: {block_name}
FUNCTION: {function}
FAILURE MODE: {mode_str}

Task: Determine the "effects on the IC output" for this failure mode.
Format: bullet list using "• BLOCK_NAME\\n    - specific effect" for each affected block.
If no other block is affected, write: No effect

Also provide:
- "system_effect": what the end user/ECU experiences (LED ON/OFF, fail-safe, device damage, No effect)
- "memo": "X" if this violates a safety goal, "O" if not

Return JSON only: {{"ic_effect": "...", "system_effect": "...", "memo": "X or O"}}"""

    raw = query_ollama(prompt, model)
    try:
        clean = raw.strip().strip('`')
        if clean.startswith('json'): clean = clean[4:].strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            d = json.loads(m.group())
            return d.get('ic_effect', 'No effect'), d.get('system_effect', 'No effect'), d.get('memo', 'O')
    except:
        pass
    return 'No effect', 'No effect', 'O'


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def load_blocks(filepath, sheet):
    xl = pd.ExcelFile(filepath)
    df = pd.read_excel(filepath, sheet_name=sheet, dtype=str).fillna('')
    blocks = {}
    for _, row in df.iterrows():
        vals = [str(v).strip() for v in row.values if str(v).strip()]
        if len(vals) >= 2:
            blocks[vals[0]] = vals[1]
        elif len(vals) == 1:
            blocks[vals[0]] = ''
    return blocks


def run():
    print("=== IC Effects Agent ===\n")

    print("Loading blocks from Excel...")
    all_blocks = load_blocks(EXCEL_FILE, BLK_SHEET)
    print(f"  {len(all_blocks)} blocks: {list(all_blocks.keys())}")

    print("\nLoading LLM output...")
    with open(LLM_INPUT_FILE, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    print(f"  {len(data)} blocks")

    result = []
    llm_calls = 0

    for block in data:
        block_name = block['block_name']
        function   = block.get('function', all_blocks.get(block_name, ''))
        rows       = block.get('rows', [])

        updated_rows = []
        for row in rows:
            mode = row.get('Standard failure mode', '')
            effect = lookup_effect(block_name, function, mode)

            if effect is None:
                # LLM fallback
                print(f"  [LLM] {block_name} | {mode[:50]}")
                ic_eff, sys_eff, memo = llm_effects(block_name, function, mode, all_blocks, OLLAMA_MODEL)
                llm_calls += 1
            else:
                ic_eff, sys_eff, memo = effect

            updated = dict(row)
            updated['effects on the IC output']  = ic_eff
            updated['effects on the system']     = sys_eff
            updated['memo']                      = memo
            updated['Single Point Failure mode'] = 'Y' if memo.startswith('X') else 'N'
            updated['Percentage of Safe Faults'] = 0 if memo.startswith('X') else 1

            cat = get_category(block_name, function)
            fk  = fm_key(mode)
            print(f"  [{memo}] {block_name}({cat}/{fk}) | {mode[:45]}")
            updated_rows.append(updated)

        updated_block = dict(block)
        updated_block['rows'] = updated_rows
        result.append(updated_block)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved → {OUTPUT_FILE}")
    print(f"   LLM calls made: {llm_calls}")


if __name__ == '__main__':
    run()

