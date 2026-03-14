import pdfplumber
import json

# ─── CONFIG ──────────────────────────────────────────────────────────────────
PDF_FILE   = 'ISO+26262-11-2018.pdf'   # <-- change to your actual PDF filename
START_PAGE = 91                 # <-- first page with the table (1-indexed)
END_PAGE   = 98                # <-- last page with the table (1-indexed)
OUTPUT_FILE = 'pdf_extracted.json'

# Expected column headers — adjust if your PDF uses different names
COL_PART   = 'Part/subpart'       # exact or partial match
COL_DESC   = 'Short description'
COL_MODES  = 'Failure modes'
# ─────────────────────────────────────────────────────────────────────────────

def find_col_index(headers, keyword):
    """Find column index by partial case-insensitive match."""
    for i, h in enumerate(headers):
        if h and keyword.lower() in str(h).lower():
            return i
    return None

def clean(val, keep_newlines=False):
    if val is None:
        return ""
    text = str(val).strip()
    return text if keep_newlines else text.replace('\n', ' ')

def extract_table(pdf_path, start_page, end_page):
    all_rows = []
    headers  = None

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        end_page = min(end_page, total)

        for page_num in range(start_page - 1, end_page):  # 0-indexed
            page   = pdf.pages[page_num]
            tables = page.extract_tables()

            for table in tables:
                if not table:
                    continue

                # First row of first table = headers (only grab once)
                if headers is None:
                    headers = [clean(h) for h in table[0]]
                    data_rows = table[1:]
                else:
                    # On subsequent pages, skip repeated header rows
                    first_row = [clean(c) for c in table[0]]
                    if any(COL_PART.lower() in c.lower() for c in first_row if c):
                        data_rows = table[1:]  # skip repeated header
                    else:
                        data_rows = table      # no header on this page

                for row in data_rows:
                    if not any(row):
                        continue
                    all_rows.append([clean(c, keep_newlines=True) for c in row])

    return headers, all_rows

FOOTNOTE_TRIGGERS = [
    'an oscillation is an instability',
    'a spike is a non-repetitive',
    'drift is a slow and continuous',
    'several of the failure modes',
    'note 1',
    'note 2',
]

def is_footnote_row(row):
    """Rows where col 0 has long footnote text and cols 1,2 are empty."""
    if row[1].strip() == '' and row[2].strip() == '':
        text = row[0].lower().strip()
        return any(text.startswith(t) for t in FOOTNOTE_TRIGGERS)
    return False

def is_section_header(row):
    """Rows with only col 0 filled — section titles like 'Regulators and Power stages'."""
    return row[0].strip() != '' and row[1].strip() == '' and row[2].strip() == ''

def merge_wrapped(text):
    """Join lines that are wrapped mid-word (hyphen) or mid-sentence (next line lowercase)."""
    lines = text.split('\n')
    merged = []
    buffer = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # strip leading footnote markers like 'a', 'b', 'c', 'd' on their own
        if line in ('a', 'b', 'c', 'd', 'e'):
            continue
        if buffer:
            if buffer.endswith('-'):
                buffer = buffer[:-1] + line
            elif line[0].islower() or buffer.endswith(','):
                buffer = buffer + ' ' + line
            else:
                merged.append(buffer)
                buffer = line
        else:
            buffer = line
    if buffer:
        merged.append(buffer)
    return merged

def format_output(headers, rows):
    part_idx = find_col_index(headers, COL_PART)
    desc_idx = find_col_index(headers, COL_DESC)
    mode_idx = find_col_index(headers, COL_MODES)

    print(f"Headers found: {headers}")
    print(f"Mapped → Part:{part_idx} | Desc:{desc_idx} | Modes:{mode_idx}\n")

    result       = {}
    part_order   = []
    current_part = None
    overflow_modes = None  # carry over cut-off modes from previous row

    for row in rows:
        # Pad row to at least 3 cols
        while len(row) < 3:
            row.append('')

        if is_footnote_row(row) or is_section_header(row):
            overflow_modes = None
            continue

        part_val = merge_wrapped(row[part_idx])[0] if row[part_idx].strip() else ""
        desc_parts = merge_wrapped(row[desc_idx])
        desc_val = ' '.join(desc_parts)
        mode_raw = row[mode_idx].strip()

        # If this row's modes start with lowercase — it's an overflow from previous row
        if mode_raw and mode_raw[0].islower() and overflow_modes is not None:
            # Append the overflow continuation to the last mode of previous entry
            continuation = mode_raw.split('\n')[0].strip()
            overflow_modes[-1] = overflow_modes[-1] + ' ' + continuation
            # Remaining lines after the first are new modes for current part
            remaining = '\n'.join(mode_raw.split('\n')[1:]).strip()
            new_modes = merge_wrapped(remaining) if remaining else []

            if part_val and part_val != current_part:
                current_part = part_val
                if current_part not in result:
                    result[current_part] = []
                    part_order.append(current_part)

            if current_part and (desc_val or new_modes):
                result[current_part].append({
                    "description": desc_val,
                    "modes": new_modes
                })
            overflow_modes = new_modes if new_modes else None
            continue

        # Normal row
        if part_val and part_val != current_part:
            current_part = part_val
            if current_part not in result:
                result[current_part] = []
                part_order.append(current_part)

        if current_part is None:
            continue

        modes = merge_wrapped(mode_raw) if mode_raw else []

        # Check if last mode looks cut off (no sentence-ending punctuation)
        if modes and not modes[-1].rstrip().endswith((')', '.', 'e', 'n', 'l', 'r', 'w', 'h')):
            overflow_modes = modes
        else:
            overflow_modes = modes  # store anyway in case next row overflows

        if desc_val or modes:
            result[current_part].append({
                "description": desc_val,
                "modes": modes
            })

    return [
        {"part_name": part, "entries": result[part]}
        for part in part_order
    ]

if __name__ == '__main__':
    headers, rows = extract_table(PDF_FILE, START_PAGE, END_PAGE)
    print(f"Total rows extracted: {len(rows)}")

    # RAW DUMP — uncomment to debug extraction issues
    print("\n=== RAW ROWS ===")
    for i, row in enumerate(rows[:20]):  # first 20 rows
        print(f"Row {i}: {row}")
    print("=== END RAW ===\n")

    output = format_output(headers, rows)

    output = format_output(headers, rows)

    json_str = json.dumps(output, indent=2, ensure_ascii=False)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(json_str)

    print(f"\n✅ {len(output)} parts found:")
    for item in output:
        print(f"   {item['part_name']}: {len(item['entries'])} entries")
    print(f"\nSaved to {OUTPUT_FILE}")
