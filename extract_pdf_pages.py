#!/usr/bin/env python3
"""
Extract specific page ranges from a PDF and combine them into a new PDF.

Usage:
    python extract_pdf_pages.py input.pdf output.pdf "6-8,18-19"
    python extract_pdf_pages.py input.pdf output.pdf "6-8,18-19,25"

Requirements:
    pip install pypdf
"""

import sys
from pypdf import PdfReader, PdfWriter


def parse_page_ranges(ranges_str: str, total_pages: int) -> list[int]:
    """
    Parse a page range string like "6-8,18-19,25" into a list of 0-indexed page numbers.
    Page numbers in the input are 1-indexed (human-friendly).
    """
    pages = []
    for part in ranges_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start.strip()), int(end.strip())
            if start < 1 or end > total_pages or start > end:
                raise ValueError(
                    f"Invalid range '{part}': document has {total_pages} pages."
                )
            pages.extend(range(start - 1, end))  # convert to 0-indexed
        else:
            page_num = int(part)
            if page_num < 1 or page_num > total_pages:
                raise ValueError(
                    f"Page {page_num} is out of range: document has {total_pages} pages."
                )
            pages.append(page_num - 1)  # convert to 0-indexed
    return pages


def extract_pages(input_path: str, output_path: str, ranges_str: str) -> None:
    reader = PdfReader(input_path)
    total_pages = len(reader.pages)
    print(f"Input PDF has {total_pages} pages.")

    pages_to_extract = parse_page_ranges(ranges_str, total_pages)
    print(f"Extracting pages: {[p + 1 for p in pages_to_extract]}")  # show 1-indexed

    writer = PdfWriter()
    for page_index in pages_to_extract:
        writer.add_page(reader.pages[page_index])

    with open(output_path, "wb") as out_file:
        writer.write(out_file)

    print(f"Done! Extracted {len(pages_to_extract)} page(s) → '{output_path}'")


def main():
    if len(sys.argv) != 4:
        print("Usage: python extract_pdf_pages.py <input.pdf> <output.pdf> <pages>")
        print('Example: python extract_pdf_pages.py input.pdf output.pdf "6-8,18-19"')
        sys.exit(1)

    input_path, output_path, ranges_str = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        extract_pages(input_path, output_path, ranges_str)
    except FileNotFoundError:
        print(f"Error: File '{input_path}' not found.")
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
