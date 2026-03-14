import pandas as pd
import json

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_FILE   = 'C:\\Users\\KIIT01\\Documents\\office\\fusa\\3_ID03_FMEDA.xlsx'
HEADER_ROW   = 21        # Row number (1-indexed) that has column labels
DATA_START   = 22        # First data row
SQ_COL       = 'B'       # Column with SQ number
BLOCK_COL    = 'D'       # Column with Block Name

# Label columns to extract (add/remove as needed — will be replaced with real names later)
LABEL_COLS = ['E', 'F', 'G','H', 'I', 'J','K', 'L', 'M', 'N', 'O', 'P','Q', 'R', 'S','T', 'U', 'V', 'W', 'X', 'Y','Z', 'AA', 'AB']   # FIT, SFM, CFM, TRM
# ─────────────────────────────────────────────────────────────────────────────

def col_letter_to_index(letter):
    """Convert Excel column letter to 0-based index."""
    result = 0
    for char in letter.upper():
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1

def extract_blocks(filepath):
    # Read raw sheet — no header, all as strings for safety
    df = pd.read_excel(filepath, sheet_name=0, header=None, dtype=str)
    df = df.fillna('')

    # Row 21 in Excel = index 20 in 0-based df
    header_idx = HEADER_ROW - 1
    header_row = df.iloc[header_idx]

    # Map column letters to their label names from header row
    label_map = {}   # { col_letter: label_name }
    for col_letter in LABEL_COLS:
        col_idx = col_letter_to_index(col_letter)
        label_name = header_row.iloc[col_idx].strip()
        if label_name:
            label_map[col_letter] = label_name

    sq_col_idx    = col_letter_to_index(SQ_COL)
    block_col_idx = col_letter_to_index(BLOCK_COL)

    # Slice data rows
    data_df = df.iloc[DATA_START - 1:].reset_index(drop=True)

    # Group rows by Block Name, preserving order
    blocks = {}       # { block_name: { sq_number: { label: value } } }
    block_order = []  # preserve insertion order

    for _, row in data_df.iterrows():
        sq_val    = row.iloc[sq_col_idx].strip()
        block_val = row.iloc[block_col_idx].strip()
        if not block_val or not sq_val:
            continue

        if block_val not in blocks:
            blocks[block_val] = {}
            block_order.append(block_val)

        blocks[block_val][sq_val] = {}
        for col_letter, label_name in label_map.items():
            col_idx = col_letter_to_index(col_letter)
            blocks[block_val][sq_val][label_name] = row.iloc[col_idx].strip()

    return blocks, block_order, label_map

def format_output(blocks, block_order, label_map):
    result = []
    for block_name in block_order:
        sq_data = blocks[block_name]
        sq_nums = list(sq_data.keys())
        sq_range = f"{sq_nums[0]}-{sq_nums[-1]}" if len(sq_nums) > 1 else sq_nums[0]

        block_obj = {
            "sq_range": sq_range,
            "block_name": block_name,
        }
        for label_name in label_map.values():
            block_obj[label_name] = {
                sq_num: label_vals.get(label_name, '')
                for sq_num, label_vals in sq_data.items()
            }

        result.append(block_obj)

    return result

if __name__ == '__main__':
    blocks, block_order, label_map = extract_blocks(EXCEL_FILE)
    output = format_output(blocks, block_order, label_map)

    json_str = json.dumps(output, indent=2)
    print(json_str)

    with open('/mnt/user-data/outputs/extracted_blocks.json', 'w') as f:
        f.write(json_str)
    print("\n✅ Saved to extracted_blocks.json")