"""
fmeda_writer.py  —  fills FMEDA_TEMPLATE.xlsx from llm_output_with_effects.json

Strategy: scan every cell for {{FMEDA_Xnn}} placeholders, build a dict
{ placeholder_string -> cell_object }, then replace values directly.
No closures, no nested functions, no index tricks — plain cell.value = data.

Usage:  python fmeda_writer.py
"""

import json, re, shutil, openpyxl
from openpyxl.styles import Alignment

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TEMPLATE_FILE = 'FMEDA_TEMPLATE.xlsx'
JSON_INPUT    = 'llm_output_with_effects.json'
OUTPUT_FILE   = 'FMEDA_filled.xlsx'
# ─────────────────────────────────────────────────────────────────────────────


def run():
    # ── Load data ─────────────────────────────────────────────────────────────
    with open(JSON_INPUT, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)

    shutil.copy2(TEMPLATE_FILE, OUTPUT_FILE)
    wb = openpyxl.load_workbook(OUTPUT_FILE)
    ws = wb['FMEDA']

    # ── Build placeholder → cell map ─────────────────────────────────────────
    # Walk every cell, store cell objects keyed by their placeholder string
    cells = {}
    for ws_row in ws.iter_rows():
        for cell in ws_row:
            if cell.__class__.__name__ == 'MergedCell':
                continue
            v = str(cell.value) if cell.value is not None else ''
            if v.startswith('{{FMEDA_') and v.endswith('}}'):
                cells[v] = cell

    print(f'  Placeholders found: {len(cells)}')

    # ── Find block groups (rows with a D placeholder = first row of block) ───
    # Only consider data rows (row 22+) — skip header/meta rows above
    DATA_START = 22

    d_rows = sorted(
        int(re.search(r'(\d+)', k).group(1))
        for k in cells
        if re.match(r'\{\{FMEDA_D\d+\}\}', k)
        and int(re.search(r'(\d+)', k).group(1)) >= DATA_START
    )
    all_data_rows = sorted(set(
        int(re.search(r'(\d+)', k).group(1))
        for k in cells
        if re.match(r'\{\{FMEDA_[A-Z]+\d+\}\}', k)
        and int(re.search(r'(\d+)', k).group(1)) >= DATA_START
    ))

    groups = []
    for i, first in enumerate(d_rows):
        nxt = d_rows[i + 1] if i + 1 < len(d_rows) else 999999
        groups.append([r for r in all_data_rows if first <= r < nxt])

    print(f'  Block groups: {len(groups)}')
    print(f'  JSON blocks:  {len(data)}')

    # ── Helper: write one cell ────────────────────────────────────────────────
    def put(col, row_num, value, wrap=False):
        k = '{{FMEDA_' + col + str(row_num) + '}}'
        if k not in cells:
            return
        c = cells[k]
        if value is None or value == '':
            c.value = None
            return
        c.value = value
        if wrap and isinstance(value, str) and '\n' in value:
            old = c.alignment or Alignment()
            c.alignment = Alignment(
                wrap_text=True,
                vertical=old.vertical or 'center',
                horizontal=old.horizontal or 'left'
            )

    # ── Fill each block ───────────────────────────────────────────────────────
    fm = 1  # failure mode counter

    for bi, block in enumerate(data):
        block_name = block.get('block_name', '')
        brows      = block.get('rows', [])

        if not brows:
            continue
        if bi >= len(groups):
            print(f'  WARNING: ran out of template rows at block {bi+1}')
            break

        trows = groups[bi]
        n_t   = len(trows)
        n_d   = len(brows)

        if n_d > n_t:
            print(f'  [{bi+1}] {block_name}: {n_d} modes > {n_t} template rows — truncating')
            brows = brows[:n_t]

        for mi, row_num in enumerate(trows):
            rd       = brows[mi] if mi < len(brows) else None
            is_first = (mi == 0)

            # ── B: Failure Mode Number ────────────────────────────────────────
            put('B', row_num, 'FM_TTL_' + str(fm) if rd else None)

            # ── D: Block Name (first row only) ────────────────────────────────
            put('D', row_num, block_name if is_first else None)

            # ── E: Block FIT (first row only) ────────────────────────────────
            if is_first:
                put('E', row_num, brows[0].get('Block Failure rate [FIT]', '') or None)

            if rd is None:
                # no data for this template row — clear G too
                put('G', row_num, None)
                continue

            memo = rd.get('memo', 'O')

            # ── F: Mode FIT ───────────────────────────────────────────────────
            put('F', row_num, rd.get('Failure rate [FIT]', '') or None)

            # ── G: Standard failure mode ──────────────────────────────────────
            put('G', row_num, rd.get('Standard failure mode', ''), wrap=True)

            # ── H: Failure Mode — LEFT EMPTY (matches real FMEDA) ────────────
            put('H', row_num, None)

            # ── I: Effects on IC output ───────────────────────────────────────
            put('I', row_num, rd.get('effects on the IC output', 'No effect'), wrap=True)

            # ── J: Effects on system ──────────────────────────────────────────
            put('J', row_num, rd.get('effects on the system', 'No effect'), wrap=True)

            # ── K: Memo (X or O) ──────────────────────────────────────────────
            put('K', row_num, memo)

            # ── O: Failure distribution ───────────────────────────────────────
            put('O', row_num, 1)

            # ── P: Single Point Y/N ───────────────────────────────────────────
            put('P', row_num, rd.get('Single Point Failure mode', 'Y' if memo == 'X' else 'N'))

            # ── Q: Failure rate FIT ───────────────────────────────────────────
            v = rd.get('Failure rate [FIT]', '')
            put('Q', row_num, v if v != '' else None)

            # ── R: Percentage of Safe Faults ──────────────────────────────────
            pct = rd.get('Percentage of Safe Faults', 1 if memo == 'O' else 0)
            put('R', row_num, pct)

            # ── S: Safety mechanism IC ────────────────────────────────────────
            put('S', row_num,
                rd.get('Safety mechanism(s) (IC) allowing to prevent the violation of the safety goal', '') or None,
                wrap=True)

            # ── T: Safety mechanism System ────────────────────────────────────
            put('T', row_num,
                rd.get('Safety mechanism(s) (System) allowing to prevent the violation of the safety goal', '') or None,
                wrap=True)

            # ── U: Coverage SPF ───────────────────────────────────────────────
            v = rd.get('Failure mode coverage wrt. violation of safety goal', '')
            put('U', row_num, v if v != '' else None)

            # ── V: Residual FIT ───────────────────────────────────────────────
            v = rd.get('Residual or Single Point Fault failure rate [FIT]', '')
            put('V', row_num, v if v != '' else None)

            # ── X: Latent Y/N ─────────────────────────────────────────────────
            put('X', row_num, rd.get('Latent Failure mode', 'Y' if memo == 'X' else 'N'))

            # ── Y: SM IC latent ───────────────────────────────────────────────
            put('Y', row_num,
                rd.get('Safety mechanism(s) (IC) to prevent latent faults', '') or None,
                wrap=True)

            # ── Z: SM System latent ───────────────────────────────────────────
            put('Z', row_num,
                rd.get('Safety mechanism(s) (System) to prevent latent faults', '') or None,
                wrap=True)

            # ── AA: Coverage latent ───────────────────────────────────────────
            v = rd.get('Failure mode coverage wrt. Latent failures', '')
            put('AA', row_num, v if v != '' else None)

            # ── AB: Latent MPF FIT ────────────────────────────────────────────
            v = rd.get('Latent Multiple Point Fault failure rate [FIT]', '')
            put('AB', row_num, v if v != '' else None)

            # ── AD: Comment ───────────────────────────────────────────────────
            put('AD', row_num,
                rd.get('comment', '') or rd.get('Comment', '') or None,
                wrap=True)

            fm += 1

        print(f'  [{bi+1}/{len(data)}] {block_name}: {min(n_d, n_t)} modes → FM_TTL_{fm - min(n_d,n_t)} to FM_TTL_{fm-1}')

    wb.save(OUTPUT_FILE)
    print(f'\n  Saved → {OUTPUT_FILE}')
    print(f'  Total failure modes written: {fm - 1}')


if __name__ == '__main__':
    print('=== FMEDA Template Filler ===')
    print(f'  Template : {TEMPLATE_FILE}')
    print(f'  Data     : {JSON_INPUT}')
    print(f'  Output   : {OUTPUT_FILE}\n')
    run()
    print('\n✅ Done')
