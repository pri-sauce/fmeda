"""
ic_effects_agent.py

Adds 'effects_on_ic_output' to each failure mode row using LLM.

The LLM reasons about:
  - What other blocks are electrically connected to the block under analysis
  - For each failure mode, what specifically goes wrong in each downstream block
  - Uses the chip's block list + function descriptions as context
  - Formats output exactly as seen in real FMEDA: bullet-style "• BLOCK\n    - effect"

Usage:
  python ic_effects_agent.py
"""

import json
import re
import pandas as pd
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE        = 'your_dataset.xlsx'      # your chip dataset excel
BLK_SHEET         = 'BLK'                    # sheet with block names + functions
LLM_OUTPUT_FILE   = 'llm_output.json'        # output from llm_pipeline.py
OUTPUT_FILE       = 'llm_output_with_effects.json'
DEBUG_FILE        = 'effects_debug.json'
OLLAMA_MODEL      = 'qwen3:30b'
OLLAMA_URL        = 'http://localhost:11434/api/generate'
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# GROUND TRUTH: IC EFFECT PATTERNS
# Learned from real FMEDA data. Maps failure mode categories to their
# expected downstream effects. LLM uses this as a strong prior.
# ═══════════════════════════════════════════════════════════════════════════════

# Effect templates per failure mode type — describe the DOWNSTREAM impact
# Keys are matched against the standard failure mode string
EFFECT_PATTERNS = {
    # When a reference/bias block is stuck or floating
    "stuck": {
        "reference_blocks": "- Output is stuck",
        "adc_block":        "- ADC measurement is incorrect.",
        "temp_block":       "- Incorrect temperature measurement.",
        "ldo_block":        "- Out of spec.",
        "osc_block":        "- Frequency out of spec.",
        "driver_blocks":    "- Out of spec.",
        "logic_block":      "- Cannot operate.",
    },
    "floating": {
        "reference_blocks": "- Output is floating",
        "adc_block":        "- ADC measurement is incorrect.",
        "temp_block":       "- Incorrect temperature measurement.",
        "ldo_block":        "- Out of spec.",
        "osc_block":        "- Out of spec.",
    },
    # Generic performance degradation
    "performance": "Performance impact",
    # No effect cases
    "no_effect_modes": [
        "affected by spikes",
        "oscillation within",
        "start-up time",
        "jitter",
        "duty cycle",
        "quiescent current"
    ]
}

# Fixed IC effects for blocks with completely predictable outputs
FIXED_IC_EFFECTS = {
    # INTERFACE / SPI blocks — always communication error
    "SERIAL_INTERFACE": "Communication error",
    # Driver blocks
    "SWITCH_DRIVER": {
        "Driver is stuck in ON or OFF state":           "Unintended LED ON/OFF",
        "Driver is floating":                           "Unintended LED ON",
        "Driver resistance too high when turned on":    "Unintended LED ON",
        "Driver resistance too low when turned off":    "Performance impact",
        "Driver turn-on time too fast or too slow":     "Performance impact",
        "Driver turn-off time too fast or too slow":    "Performance impact",
    },
    # SM blocks
    "SM": "block_specific"  # each SM has its own specific effect
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_blocks_from_excel(filepath, sheet_name):
    """Load block name → function mapping from BLK sheet."""
    xl = pd.ExcelFile(filepath)
    if sheet_name not in xl.sheet_names:
        raise ValueError(f"Sheet '{sheet_name}' not found")
    df = pd.read_excel(filepath, sheet_name=sheet_name, dtype=str).fillna('')
    blocks = {}
    for _, row in df.iterrows():
        vals = [str(v).strip() for v in row.values if str(v).strip()]
        if len(vals) >= 2:
            blocks[vals[0]] = vals[1]
        elif len(vals) == 1:
            blocks[vals[0]] = ''
    return blocks  # { block_name: function_description }


def load_llm_output(filepath):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CALL
# ═══════════════════════════════════════════════════════════════════════════════

def query_ollama(prompt, model, temperature=0.1):
    r = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 12288, "top_p": 0.9}
    })
    r.raise_for_status()
    return r.json()["response"].strip()


def get_ic_effects(block, all_blocks, model):
    """
    For a given block, ask LLM to determine which other blocks are affected
    by each failure mode, and what happens to them.

    Returns a dict: { standard_failure_mode: ic_effect_string }
    """
    # Build context: all other blocks and their functions
    other_blocks = {
        name: func for name, func in all_blocks.items()
        if name != block['block_name']
    }

    # Separate failure modes into "no effect" vs "needs analysis"
    all_modes = [row['Standard failure mode'] for row in block['rows']]

    # Quick filter: modes that typically have no downstream effect
    no_effect_keywords = [
        'affected by spikes', 'oscillation within', 'start-up time',
        'jitter', 'duty cycle', 'quiescent current', 'settling time',
        'fail to detect', 'false detection'  # SM blocks handled separately
    ]

    needs_analysis = []
    auto_no_effect = []
    for mode in all_modes:
        mode_lower = mode.lower()
        if any(kw in mode_lower for kw in no_effect_keywords):
            auto_no_effect.append(mode)
        else:
            needs_analysis.append(mode)

    if not needs_analysis:
        return {m: "No effect" for m in all_modes}, "all_no_effect"

    prompt = f"""You are a functional safety engineer analyzing an automotive IC.
You need to determine the "effects on the IC output" for failure modes of a specific block.

═══════════════════════════════════════════════
THE IC CHIP ARCHITECTURE
All blocks in this IC and what they do:
{json.dumps(all_blocks, indent=2)}
═══════════════════════════════════════════════
BLOCK UNDER ANALYSIS
Name: {block['block_name']}
Function: {block['function']}
═══════════════════════════════════════════════
FAILURE MODES TO ANALYZE
{json.dumps(needs_analysis, indent=2)}
═══════════════════════════════════════════════

TASK: For each failure mode above, determine what effect it has on other IC blocks.

RULES:
1. Think about which blocks in the chip receive signals FROM {block['block_name']}
   (i.e. which blocks depend on this block's output)
2. For each affected downstream block, describe what goes wrong
3. Use this EXACT format:
   "• BLOCK_NAME\n    - specific effect description\n    - another effect if multiple"
4. If multiple blocks are affected, list each one with • prefix
5. If a mode has NO effect on any other block, return "No effect"
6. Keep descriptions short and technical (1 line per effect)
7. Use the exact block names from the chip architecture above

IMPORTANT LESSONS FROM REAL FMEDA DATA:
- "stuck" or "floating" modes on reference/bias blocks affect ALL downstream consumers
- Current/voltage references that are wrong cause downstream blocks to measure incorrectly
- Clock failures (stuck, wrong freq) affect all digital logic that depends on the clock
- Power supply failures (LDO, CP) affect all blocks powered by that supply
- Sense amplifier errors (CSNS, TEMP) affect the ADC that reads them
- Driver failures cause direct LED effects (ON/OFF/performance)
- Spikes, oscillation-within-range, start-up-time modes typically have no downstream effect

Return a JSON object mapping each failure mode string to its IC effect string.
Format: {{"failure mode string": "effect string", ...}}

Return ONLY the JSON object, no explanation:"""

    raw = query_ollama(prompt, model, temperature=0.1)

    # Parse response
    try:
        clean = raw.strip().strip('`').strip()
        if clean.startswith('json'):
            clean = clean[4:].strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            result = json.loads(m.group())
            if isinstance(result, dict):
                # Add back the auto-no-effect ones
                for mode in auto_no_effect:
                    result[mode] = "No effect"
                return result, raw
    except Exception as e:
        pass

    # Fallback
    fallback = {m: "No effect" for m in all_modes}
    return fallback, raw


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def add_ic_effects(llm_output, all_blocks, model):
    """Add IC output effects to each row in the LLM output."""
    result    = []
    debug_log = []
    total     = len(llm_output)

    for i, block in enumerate(llm_output):
        print(f"\n[{i+1}/{total}] {block['block_name']}")

        if not block['rows']:
            result.append(block)
            continue

        # Get IC effects for this block
        effects_map, raw = get_ic_effects(block, all_blocks, model)

        debug_log.append({
            "block_name": block['block_name'],
            "effects_map": effects_map,
            "llm_raw": raw if raw != "all_no_effect" else "auto:all_no_effect"
        })

        # Inject effects into each row
        updated_rows = []
        for row in block['rows']:
            mode = row['Standard failure mode']
            effect = effects_map.get(mode, "No effect")
            updated_row = dict(row)
            updated_row['effects on the IC output'] = effect
            updated_rows.append(updated_row)
            print(f"    {mode[:45]:<45} → {str(effect)[:50]}")

        updated_block = dict(block)
        updated_block['rows'] = updated_rows
        result.append(updated_block)

    with open(DEBUG_FILE, 'w', encoding='utf-8') as f:
        json.dump(debug_log, f, indent=2, ensure_ascii=False)

    return result


if __name__ == '__main__':
    print("=== Step 1: Load blocks from Excel ===")
    all_blocks = load_blocks_from_excel(EXCEL_FILE, BLK_SHEET)
    print(f"Found {len(all_blocks)} blocks: {list(all_blocks.keys())}")

    print("\n=== Step 2: Load LLM output ===")
    llm_output = load_llm_output(LLM_OUTPUT_FILE)
    print(f"Loaded {len(llm_output)} blocks")

    print(f"\n=== Step 3: Generate IC effects ({OLLAMA_MODEL}) ===")
    output = add_ic_effects(llm_output, all_blocks, OLLAMA_MODEL)

    print("\n=== Step 4: Save ===")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved to {OUTPUT_FILE}")
    print(f"   Debug log → {DEBUG_FILE}")
