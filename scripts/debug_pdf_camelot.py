# scripts/debug_pdf_camelot.py
import camelot
import sys
from pathlib import Path

def dump_camelot_tables(pdf_path: Path):
    print(f"=== Camelot extraction for: {pdf_path} ===")

    # Try stream mode (better for whitespace-aligned tables)
    tables = camelot.read_pdf(str(pdf_path), pages="all", flavor="stream")

    print(f"Found {len(tables)} tables")
    for idx, t in enumerate(tables, start=1):
        print(f"\n--- Table {idx} ---")
        print(t.df.head(20))  # show first 20 rows

        # If you want to inspect full CSV:
        csv_path = f"camelot_table_{idx}.csv"
        t.to_csv(csv_path)
        print(f"Saved {csv_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_pdf_camelot.py <path-to-pdf>")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    dump_camelot_tables(pdf_path)
