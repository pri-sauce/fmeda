import json
import re

# Hardcoded file paths
TEMPLATE_JSON = "temp.json"
FORMULAS_JSON = "formulas.json"
OUTPUT_JSON = "merged_template.json"


def parse_cell_reference(cell_ref):
    """
    Parse cell reference like 'A1', 'AB123' into column letter and row number.
    Returns: (column_letter, row_number)
    """
    match = re.match(r'^([A-Z]+)(\d+)$', cell_ref)
    if match:
        return match.group(1), int(match.group(2))
    return None, None


def merge_formulas_into_template(template_json_file, formulas_json_file, output_json):
    """
    Merge formulas into template JSON.
    
    Template structure:
    {
      "SheetName": [
        null,  # Row 1 (index 0)
        null,  # Row 2 (index 1)
        {      # Row 3 (index 2)
          "H": "{{Cover_H3}}",
          "I": "{{Cover_I3}}"
        }
      ]
    }
    
    Formulas structure:
    {
      "SheetName": {
        "I3": "=F18",
        "H4": "=F19"
      }
    }
    
    Output: Adds formula fields to the template
    {
      "SheetName": [
        null,
        null,
        {
          "H": "{{Cover_H3}}",
          "I": "{{Cover_I3}}",
          "I_formula": "=F18"  # Added
        }
      ]
    }
    """
    
    # Load both JSON files
    with open(template_json_file, 'r', encoding='utf-8') as f:
        template = json.load(f)
    
    with open(formulas_json_file, 'r', encoding='utf-8') as f:
        formulas = json.load(f)
    
    total_formulas_added = 0
    
    # Process each sheet
    for sheet_name in template.keys():
        if sheet_name not in formulas:
            print(f"  Sheet '{sheet_name}': No formulas found")
            continue
        
        sheet_formulas = formulas[sheet_name]
        formulas_added = 0
        
        # Process each formula in this sheet
        for cell_ref, formula in sheet_formulas.items():
            col_letter, row_num = parse_cell_reference(cell_ref)
            
            if col_letter is None or row_num is None:
                print(f"  Warning: Could not parse cell reference '{cell_ref}'")
                continue
            
            # Array index is row_num - 1 (Excel rows are 1-indexed, arrays are 0-indexed)
            array_index = row_num - 1
            
            # Check if array index exists
            if array_index >= len(template[sheet_name]):
                print(f"  Warning: Row {row_num} not found in template for sheet '{sheet_name}'")
                continue
            
            # Get the row object
            row_obj = template[sheet_name][array_index]
            
            # If row is null, create a new object
            if row_obj is None:
                row_obj = {}
                template[sheet_name][array_index] = row_obj
            
            # Add formula field
            formula_field = f"{col_letter}_formula"
            row_obj[formula_field] = formula
            formulas_added += 1
            total_formulas_added += 1
        
        if formulas_added > 0:
            print(f"  ✓ Sheet '{sheet_name}': Added {formulas_added} formulas")
    
    # Save merged template
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    
    print(f"\n✓ Total formulas added: {total_formulas_added}")
    print(f"✓ Output saved to: {output_json}")
    
    return template


if __name__ == "__main__":
    try:
        print("Loading template JSON...")
        print("Loading formulas JSON...")
        merge_formulas_into_template(TEMPLATE_JSON, FORMULAS_JSON, OUTPUT_JSON)
        print(f"\n✅ Merge completed successfully!")
    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format - {e}")
    except Exception as e:
        print(f"Error: {e}")
