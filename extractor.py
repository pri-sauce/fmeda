import pandas as pd
import json

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE  = '3_ID03_FMEDA.xlsx'   # <-- change to your actual filename
HEADER_ROW  = 21                 # Row with label names (FIT, SFM, etc.)
DATA_START  = 22                 # First data row
SQ_COL      = 'B'                # SQ number column
BLOCK_COL   = 'D'                # Block name column
SKIP_COLS   = ['A', 'B', 'C', 'D']  # Cols to ignore (non-label cols)
# ─────────────────────────────────────────────────────────────────────────────

def col_to_idx(col):
    """Excel column letter → 0-based index. e.g. A→0, B→1, AB→27"""
    result = 0
    for c in col.upper():
        result = result * 26 + (ord(c) - ord('A') + 1)
    return result - 1

def idx_to_col(idx):
    """0-based index → Excel column letter"""
    col = ''
    n = idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        col = chr(65 + r) + col
    return col

def extract_blocks(filepath):
    df = pd.read_excel(filepath, sheet_name=0, header=None, dtype=str)
    df = df.fillna('')

    header_row = df.iloc[HEADER_ROW - 1]
    sq_idx     = col_to_idx(SQ_COL)
    block_idx  = col_to_idx(BLOCK_COL)
    skip_idxs  = {col_to_idx(c) for c in SKIP_COLS}

    # Build label map: col_index → label name, skipping non-label cols
    label_map = {}  # { col_index: label_name }
    for i, val in enumerate(header_row):
        if i in skip_idxs:
            continue
        label = str(val).strip()
        if label and label.lower() != 'nan':
            label_map[i] = label

    # Parse data rows
    blocks      = {}  # { block_name: { label_name: { sq: value } } }
    block_order = []

    for row_num, row in df.iloc[DATA_START - 1:].iterrows():
        excel_row = row_num + 1  # 0-based → Excel row number
        sq_val    = str(row.iloc[sq_idx]).strip()
        block_val = str(row.iloc[block_idx]).strip()

        if not block_val or block_val.lower() == 'nan':
            continue
        if not sq_val or sq_val.lower() == 'nan':
            continue

        if block_val not in blocks:
            blocks[block_val] = {label: {} for label in label_map.values()}
            block_order.append(block_val)

        for col_idx, label_name in label_map.items():
            val = str(row.iloc[col_idx]).strip()
            if val.lower() == 'nan':
                val = ''
            blocks[block_val][label_name][str(excel_row)] = val

    return blocks, block_order

def format_output(blocks, block_order):
    result = []
    for block_name in block_order:
        label_data = blocks[block_name]
        # Get SQ range from the first label's keys
        all_rows = list(next(iter(label_data.values())).keys()) if label_data else []
        sq_range = f"{all_rows[0]}-{all_rows[-1]}" if len(all_rows) > 1 else (all_rows[0] if all_rows else '')

        block_obj = {
            "sq_range": sq_range,
            "block_name": block_name,
        }
        block_obj.update(label_data)
        result.append(block_obj)

    return result

if __name__ == '__main__':
    blocks, block_order = extract_blocks(EXCEL_FILE)
    output = format_output(blocks, block_order)

    json_str = json.dumps(output, indent=2)
    print(json_str)

    out_file = EXCEL_FILE.rsplit('.', 1)[0] + '_extracted.json'
    with open(out_file, 'w') as f:
        f.write(json_str)
    print(f"\n✅ Saved to {out_file}")
