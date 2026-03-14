import pandas as pd

EXCEL_FILE = 'C:\\Users\\KIIT01\\Documents\\office\\fusa\\3_ID03_FMEDA.xlsx'  # <-- change to your actual file name
HEADER_ROW = 21

df = pd.read_excel(EXCEL_FILE, sheet_name=0, header=None, dtype=str)
df = df.fillna('')

header_idx = HEADER_ROW - 1
header_row = df.iloc[header_idx]

print(f"Total columns in sheet: {len(header_row)}")
print(f"\nRow 21 contents (col index → value):")
for i, val in enumerate(header_row):
    if val.strip():
        # Convert index to Excel column letter
        n = i + 1
        col_letter = ''
        while n:
            n, r = divmod(n - 1, 26)
            col_letter = chr(65 + r) + col_letter
        print(f"  Col {col_letter} (index {i}): '{val}'")

print(f"\nFirst 3 data rows preview:")
for row_idx in range(HEADER_ROW, min(HEADER_ROW + 3, len(df))):
    row = df.iloc[row_idx]
    non_empty = {i: v for i, v in enumerate(row) if v.strip()}
    print(f"  Row {row_idx + 1}: {non_empty}")
