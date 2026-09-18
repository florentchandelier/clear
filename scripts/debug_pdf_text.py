# debug_pdf_text.py
from pathlib import Path
import pdfplumber
import re
import sys
import unicodedata


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def dump_pdf_lines(pdf_path: Path, max_lines: int = 200):
    print(f"=== Extracting text (line mode) from: {pdf_path} ===")
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"\n--- Page {page_num} (extract_text) ---")
            text = page.extract_text() or ""
            lines = text.splitlines()
            for i, line in enumerate(lines):
                raw = line.replace("\xa0", " ")
                norm = re.sub(r"\s+", " ", raw).strip()
                no_accents = _strip_accents(norm.lower())

                print(f"{i+1:02d}: {repr(line)}")
                print(f"     norm: {repr(norm)}")
                print(f"     no_accents: {repr(no_accents)}")

                if i + 1 >= max_lines:
                    print("... (truncated)")
                    break


def dump_pdf_tables(pdf_path: Path, max_rows: int = 50):
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"\n=== Page {page_num} tables ===")

            # 1. Default strategy
            table = page.extract_table()
            print("\n--- Default extract_table() ---")
            if table:
                for i, row in enumerate(table):
                    print(f"{i+1:02d}: {row}")
                    if i + 1 >= max_rows:
                        print("... (truncated)")
                        break
            else:
                print("No table detected with default strategy.")

            # 2. Lattice strategy (use lines)
            table = page.extract_table({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
            print("\n--- Lattice strategy (lines) ---")
            if table:
                for i, row in enumerate(table):
                    print(f"{i+1:02d}: {row}")
                    if i + 1 >= max_rows:
                        print("... (truncated)")
                        break
            else:
                print("No table detected with lattice strategy.")

            # 3. Stream strategy (use text positions)
            table = page.extract_table({"vertical_strategy": "text", "horizontal_strategy": "text"})
            print("\n--- Stream strategy (text) ---")
            if table:
                for i, row in enumerate(table):
                    print(f"{i+1:02d}: {row}")
                    if i + 1 >= max_rows:
                        print("... (truncated)")
                        break
            else:
                print("No table detected with stream strategy.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_pdf_text.py <path-to-pdf>")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    dump_pdf_lines(pdf_path)
    dump_pdf_tables(pdf_path)
