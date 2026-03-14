import json

INPUT_FILE  = 'fmeda.json'   # <-- your extracted json file
OUTPUT_FILE = 'fmeda_formatted.json'

with open(INPUT_FILE, 'r', encoding='utf-8-sig') as f:
    data = json.load(f)

# Handle both plain list and {"FMEDA": [...]} wrapper
rows = data["FMEDA"] if isinstance(data, dict) else data

# First row is the header — build col_letter → label_name map
header_map = {}  # { "E": "Block Failure rate [FIT]", ... }
if rows:
    first_row = rows[0]
    for k, v in first_row.items():
        if isinstance(v, str) and v.strip():
            header_map[k] = v.strip()

def map_row(row):
    """Replace column letter keys with their full label names, drop Block Name."""
    mapped = {}
    for k, v in row.items():
        label = header_map.get(k, k)
        if label == "Block Name":
            continue  # already captured at block level
        mapped[label] = v
    return mapped

result      = {}
block_order = []
current_block = None

for row in rows[1:]:  # skip header row
    d_val = str(row.get("D", "")).strip()

    if d_val and d_val != current_block:
        current_block = d_val
        if current_block not in result:
            result[current_block] = []
            block_order.append(current_block)

    if current_block is None:
        current_block = "UNKNOWN"
        result[current_block] = []
        block_order.append(current_block)

    result[current_block].append(map_row(row))

output = [
    {"block_name": block, "rows": result[block]}
    for block in block_order
]

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"✅ Done! {len(block_order)} blocks found:")
for block in block_order:
    print(f"   {block}: {len(result[block])} rows")
print(f"\nSaved to {OUTPUT_FILE}")
