"""
fmeda_writer.py

Stage 3 of the FMEDA pipeline.
Fills FMEDA_TEMPLATE.xlsx with data from llm_output_with_effects.json.
Writes ONLY the FMEDA sheet data rows (row 22 onwards).
All template headers, styling, and formulas are preserved.

Column layout (matches real FMEDA exactly):
  B  = Failure Mode Number        (FM_TTL_1, FM_TTL_2, ...)
  C  = Component Name             (block name, every row)
  D  = Block Name                 (first row of each block only)
  E  = Block Failure rate [FIT]   (first row of each block only)
  F  = Failure mode separate rate (every row)
  G  = Standard failure mode
  H  = Failure Mode               (same as G)
  I  = Effects on IC output
  J  = Effects on system
  K  = Memo (X or O)
  O  = Failure distribution       (always 1)
  P  = Single Point Y/N
  Q  = Failure rate [FIT]
  R  = Percentage of Safe Faults
  S  = Safety mechanism(s) IC
  T  = Safety mechanism(s) System
  U  = Coverage SPF
  V  = Residual/SPF FIT
  X  = Latent failure Y/N
  Y  = SM IC latent
  Z  = SM System latent
  AA = Coverage latent
  AB = Latent MPF FIT
  AD = Comment

Usage:
  python fmeda_writer.py
"""

import json, shutil, openpyxl
from openpyxl.styles import Alignment
from copy import copy

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TEMPLATE_FILE  = 'FMEDA_TEMPLATE.xlsx'
JSON_INPUT     = 'llm_output_with_effects.json'
OUTPUT_FILE    = 'FMEDA_filled.xlsx'
DATA_START_ROW = 22
# ─────────────────────────────────────────────────────────────────────────────


def col_idx(letter):
    """Excel column letter(s) → 1-based column index."""
    result = 0
    for c in letter.upper():
        result = result * 26 + (ord(c) - ord('A') + 1)
    return result


def set_cell(ws, row, col_letter, value, wrap=True, style_ref=None):
    """Write value to cell, skipping merged cells, optionally copying style."""
    if value is None or value == '':
        return
    cell = ws.cell(row=row, column=col_idx(col_letter))
    if cell.__class__.__name__ == 'MergedCell':
        return  # skip — merged cells can't be written directly
    cell.value = value
    if style_ref and style_ref.__class__.__name__ != 'MergedCell' and style_ref.has_style:
        cell.font      = copy(style_ref.font)
        cell.fill      = copy(style_ref.fill)
        cell.border    = copy(style_ref.border)
        cell.number_format = style_ref.number_format
    if wrap:
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    else:
        cell.alignment = Alignment(wrap_text=False, vertical='top')


def get_style_refs(ws, start_row, max_search=10):
    """
    For each column we write, find the first non-merged cell at or after start_row
    to use as a style reference.
    """
    cols = ['B','C','D','E','F','G','H','I','J','K','O','P','Q','R',
            'S','T','U','V','X','Y','Z','AA','AB','AD']
    refs = {}
    for c in cols:
        ref = None
        for r in range(start_row, start_row + max_search):
            cell = ws.cell(row=r, column=col_idx(c))
            if cell.__class__.__name__ != 'MergedCell':
                ref = cell
                break
        refs[c] = ref  # may be None if all merged (rare)
    return refs


def fill_template(template_path, json_path, output_path):
    with open(json_path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)

    shutil.copy2(template_path, output_path)
    wb = openpyxl.load_workbook(output_path)
    ws = wb['FMEDA']

    # Collect style references from template row 22 before clearing
    style_refs = get_style_refs(ws, DATA_START_ROW)

    # Clear all data rows — skip merged cells
    for r in range(DATA_START_ROW, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(row=r, column=c)
            if cell.__class__.__name__ != 'MergedCell':
                cell.value = None

    current_row = DATA_START_ROW
    fm_counter  = 1

    for block in data:
        block_name = block.get('block_name', '')
        rows       = block.get('rows', [])
        if not rows:
            continue

        for idx, row in enumerate(rows):
            is_first = (idx == 0)
            mode     = row.get('Standard failure mode', '')
            s        = style_refs  # shorthand

            # ── B: Failure Mode Number ───────────────────────────────────────
            set_cell(ws, current_row, 'B', f'FM_TTL_{fm_counter}',
                     wrap=False, style_ref=s['B'])

            # ── C: Component Name (every row) ────────────────────────────────
            set_cell(ws, current_row, 'C', block_name,
                     wrap=False, style_ref=s['C'])

            # ── D: Block Name (first row only) ───────────────────────────────
            if is_first:
                set_cell(ws, current_row, 'D', block_name,
                         wrap=False, style_ref=s['D'])

            # ── E: Block FIT (first row only) ────────────────────────────────
            if is_first:
                v = row.get('Block Failure rate [FIT]', '')
                if v != '':
                    set_cell(ws, current_row, 'E', v,
                             wrap=False, style_ref=s['E'])

            # ── F: Mode FIT (every row) ──────────────────────────────────────
            v = row.get('Failure rate [FIT]', '')
            if v != '':
                set_cell(ws, current_row, 'F', v,
                         wrap=False, style_ref=s['F'])

            # ── G: Standard failure mode ─────────────────────────────────────
            set_cell(ws, current_row, 'G', mode,
                     wrap=True, style_ref=s['G'])

            # ── H: Failure Mode (same as G) ──────────────────────────────────
            set_cell(ws, current_row, 'H', mode,
                     wrap=True, style_ref=s['H'])

            # ── I: Effects on IC output ──────────────────────────────────────
            set_cell(ws, current_row, 'I',
                     row.get('effects on the IC output', 'No effect'),
                     wrap=True, style_ref=s['I'])

            # ── J: Effects on system ─────────────────────────────────────────
            set_cell(ws, current_row, 'J',
                     row.get('effects on the system', 'No effect'),
                     wrap=True, style_ref=s['J'])

            # ── K: Memo (X or O) ─────────────────────────────────────────────
            memo = row.get('memo', 'O')
            set_cell(ws, current_row, 'K', memo,
                     wrap=False, style_ref=s['K'])

            # ── O: Failure distribution (always 1) ───────────────────────────
            set_cell(ws, current_row, 'O', 1,
                     wrap=False, style_ref=s['O'])

            # ── P: Single Point Y/N ──────────────────────────────────────────
            sp = row.get('Single Point Failure mode', 'N' if memo == 'O' else 'Y')
            set_cell(ws, current_row, 'P', sp,
                     wrap=False, style_ref=s['P'])

            # ── Q: Failure rate FIT ──────────────────────────────────────────
            v = row.get('Failure rate [FIT]', '')
            if v != '':
                set_cell(ws, current_row, 'Q', v,
                         wrap=False, style_ref=s['Q'])

            # ── R: Percentage of Safe Faults ─────────────────────────────────
            pct = row.get('Percentage of Safe Faults', 1 if memo == 'O' else 0)
            set_cell(ws, current_row, 'R', pct,
                     wrap=False, style_ref=s['R'])

            # ── S: Safety mechanism(s) IC ────────────────────────────────────
            sm_ic = row.get('Safety mechanism(s) (IC) allowing to prevent the violation of the safety goal', '')
            if sm_ic:
                set_cell(ws, current_row, 'S', sm_ic,
                         wrap=True, style_ref=s['S'])

            # ── T: Safety mechanism(s) System ────────────────────────────────
            sm_sys = row.get('Safety mechanism(s) (System) allowing to prevent the violation of the safety goal', '')
            if sm_sys:
                set_cell(ws, current_row, 'T', sm_sys,
                         wrap=True, style_ref=s['T'])

            # ── U: Coverage SPF ──────────────────────────────────────────────
            v = row.get('Failure mode coverage wrt. violation of safety goal', '')
            if v != '':
                set_cell(ws, current_row, 'U', v,
                         wrap=False, style_ref=s['U'])

            # ── V: Residual/SPF FIT ──────────────────────────────────────────
            v = row.get('Residual or Single Point Fault failure rate [FIT]', '')
            if v != '':
                set_cell(ws, current_row, 'V', v,
                         wrap=False, style_ref=s['V'])

            # ── X: Latent failure Y/N ────────────────────────────────────────
            lat = row.get('Latent Failure mode', 'N' if memo == 'O' else 'Y')
            set_cell(ws, current_row, 'X', lat,
                     wrap=False, style_ref=s['X'])

            # ── Y: SM IC latent ──────────────────────────────────────────────
            v = row.get('Safety mechanism(s) (IC) to prevent latent faults', '')
            if v:
                set_cell(ws, current_row, 'Y', v,
                         wrap=True, style_ref=s['Y'])

            # ── Z: SM System latent ──────────────────────────────────────────
            v = row.get('Safety mechanism(s) (System) to prevent latent faults', '')
            if v:
                set_cell(ws, current_row, 'Z', v,
                         wrap=True, style_ref=s['Z'])

            # ── AA: Coverage latent ──────────────────────────────────────────
            v = row.get('Failure mode coverage wrt. Latent failures', '')
            if v != '':
                set_cell(ws, current_row, 'AA', v,
                         wrap=False, style_ref=s['AA'])

            # ── AB: Latent MPF FIT ───────────────────────────────────────────
            v = row.get('Latent Multiple Point Fault failure rate [FIT]', '')
            if v != '':
                set_cell(ws, current_row, 'AB', v,
                         wrap=False, style_ref=s['AB'])

            # ── AD: Comment ──────────────────────────────────────────────────
            v = row.get('comment', '') or row.get('Comment', '')
            if v:
                set_cell(ws, current_row, 'AD', v,
                         wrap=True, style_ref=s['AD'])

            current_row += 1
            fm_counter  += 1

    total_data_rows = current_row - DATA_START_ROW
    print(f"  Wrote {total_data_rows} rows across {len(data)} blocks (last row: {current_row-1})")
    wb.save(output_path)
    print(f"  Saved → {output_path}")


if __name__ == '__main__':
    print(f"=== FMEDA Template Filler ===")
    print(f"  Template : {TEMPLATE_FILE}")
    print(f"  Data     : {JSON_INPUT}")
    print(f"  Output   : {OUTPUT_FILE}")
    print()
    fill_template(TEMPLATE_FILE, JSON_INPUT, OUTPUT_FILE)
    print("\n✅ Done")
