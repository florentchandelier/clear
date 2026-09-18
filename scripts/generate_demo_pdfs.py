#!/usr/bin/env python3
"""Generate synthetic statement PDFs that the *real* importers can parse.

Importers are never weakened to accommodate a fixture: these PDFs must
satisfy the existing signature/layout rules as they
already stand).

Currently covers:
  - cash.bmo_chequing_fr  (BmoChequingFrImporter)

Everything in the output is invented: fake account numbers, fake merchant
names, obviously-synthetic amounts. Nothing here derives from a real
statement.

Written with PyMuPDF (already a project dependency via requirements.txt's
`pymupdf`), so no new dependency is needed. Text is positioned by explicit
coordinates because the BMO chequing importer reads transactions with
Camelot's `flavor="stream"`, which infers columns from whitespace
geometry -- consistently aligned columns are what make that work.

Usage:
  venv/bin/python scripts/generate_demo_pdfs.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import fitz  # PyMuPDF

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PDF_FIXTURE_DIR = REPO_ROOT / "tests" / "importers" / "fixtures" / "pdf"

# Fixed metadata so repeated runs are byte-identical rather than embedding
# a fresh timestamp each time (Milestone 4 promotion criterion 6).
_FIXED_DATE = "D:20250101000000Z"
_METADATA = {
    "title": "Synthetic BMO chequing statement (demo fixture)",
    "author": "CLEAR demo fixture generator",
    "subject": "Synthetic test data -- not a real bank statement",
    "keywords": "synthetic, demo, fixture",
    "creator": "scripts/generate_demo_pdfs.py",
    "producer": "PyMuPDF",
    "creationDate": _FIXED_DATE,
    "modDate": _FIXED_DATE,
}

FONT = "helv"
FONT_BOLD = "hebo"

# ─────────────────────────────────────────────────────────────
# BMO chequing (FR)
# ─────────────────────────────────────────────────────────────

# Column x-positions. Dates and descriptions are left-aligned; the three
# money columns are right-aligned at these x values so Camelot sees three
# clean, well-separated columns.
_X_DATE = 42.0
_X_DESC = 96.0
_X_DEBIT_RIGHT = 372.0
_X_CREDIT_RIGHT = 452.0
_X_BALANCE_RIGHT = 545.0

_PERIOD_END_DAY = 31
_PERIOD_END_MONTH_FR = "janvier"
_PERIOD_END_YEAR = 2025
_DOMICILIATION = "0004"
# Invented account number. The importer's ACCNUM_RE captures the
# "[0-9-]{6,20}" run that is followed by whitespace and 5+ more digits.
# Repeated digits keep it unmistakably fake to a human reader and to
# scripts/audit_public_tree.py's unmasked-identifier heuristic.
_ACCOUNT_NUMBER_VISIBLE = "11111111-111"
_ACCOUNT_TRAILING_DIGITS = "11111111"

# (day_month_fr, description, debit, credit) -- debit XOR credit.
# "28 déc" deliberately exercises the importer's year-rollover rule
# (month 12 > statement month 1 => previous year).
_TRANSACTIONS: List[tuple[str, str, float | None, float | None]] = [
    ("28 déc", "Dépôt direct, DEMO EMPLOYEUR TEST", None, 2400.00),
    ("3 janv", "Prélèvement automatique, DEMO HYDRO TEST", 142.35, None),
    ("6 janv", "Achat par carte de débit, DEMO EPICERIE TEST", 87.42, None),
    ("9 janv", "Virement en ligne, TF 123", 500.00, None),
    ("14 janv", "Dépôt direct, DEMO EMPLOYEUR TEST", None, 2400.00),
    ("17 janv", "Achat par carte de débit, DEMO CAFE TEST", 12.75, None),
    ("22 janv", "Prélèvement automatique, DEMO TELECOM TEST", 95.50, None),
    ("28 janv", "Virement INTERAC envoyé, DEMO", 250.00, None),
]

_OPENING_BALANCE = 5000.00


def _fr_amount(value: float) -> str:
    """Format like a French BMO statement: comma decimal separator, and no
    thousands separator (a space inside a number would read as a column
    break to Camelot)."""
    return f"{value:.2f}".replace(".", ",")


def _bmo_chequing_rows() -> List[Dict[str, Any]]:
    """Transaction rows plus the running balance shown on the statement."""
    rows: List[Dict[str, Any]] = []
    balance = _OPENING_BALANCE
    for day_month, desc, debit, credit in _TRANSACTIONS:
        balance = balance - (debit or 0.0) + (credit or 0.0)
        rows.append({
            "date": day_month,
            "description": desc,
            "debit": debit,
            "credit": credit,
            "balance": round(balance, 2),
        })
    return rows


def build_bmo_chequing_pdf(out_path: Path) -> Dict[str, Any]:
    """Write a synthetic BMO chequing (FR) statement PDF.

    Returns the values the statement asserts, so a caller (or a test) can
    check the importer's parse against what the document actually says
    rather than against a hand-maintained duplicate.
    """
    rows = _bmo_chequing_rows()
    total_debits = round(sum(r["debit"] or 0.0 for r in rows), 2)
    total_credits = round(sum(r["credit"] or 0.0 for r in rows), 2)
    closing_balance = rows[-1]["balance"]

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    def text(x: float, y: float, s: str, *, size: float = 9.0, font: str = FONT) -> None:
        page.insert_text((x, y), s, fontname=font, fontsize=size)

    def text_right(x_right: float, y: float, s: str, *, size: float = 9.0, font: str = FONT) -> None:
        width = fitz.get_text_length(s, fontname=font, fontsize=size)
        page.insert_text((x_right - width, y), s, fontname=font, fontsize=size)

    # ── Header ────────────────────────────────────────────────
    text(_X_DATE, 60, "BMO Banque de Montréal", size=13, font=FONT_BOLD)
    text(_X_DATE, 78, "Relevé de services bancaires courants", size=11, font=FONT_BOLD)
    text(_X_DATE, 96, "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION", size=8)
    text(
        _X_DATE, 118,
        f"Période terminée le {_PERIOD_END_DAY} {_PERIOD_END_MONTH_FR} {_PERIOD_END_YEAR}",
        size=10,
    )
    text(_X_DATE, 134, f"Numéro de domiciliation : {_DOMICILIATION}", size=9)

    # The importer's ACCNUM_RE matches the account label with no spaces
    # between words (real statements extract that way from this tightly
    # kerned header), then captures the digits/dashes run that is followed
    # by whitespace and 5+ digits. The words are therefore drawn as
    # separate runs closer together than pdfplumber's space threshold, and
    # the trailing digit group is placed far enough right to keep a real
    # gap.
    label_parts = ["Compte", "de", "chèques", "principal", f"#{_ACCOUNT_NUMBER_VISIBLE}"]
    x = _X_DATE
    for part in label_parts:
        text(x, 152, part, size=9, font=FONT_BOLD)
        x += fitz.get_text_length(part, fontname=FONT_BOLD, fontsize=9) + 1.2
    text(x + 24, 152, _ACCOUNT_TRAILING_DIGITS, size=9)

    # ── Transaction table ─────────────────────────────────────
    y = 196
    text(_X_DATE, y, "Date", size=9, font=FONT_BOLD)
    text(_X_DESC, y, "Description", size=9, font=FONT_BOLD)
    text_right(_X_DEBIT_RIGHT, y, "Débits", size=9, font=FONT_BOLD)
    text_right(_X_CREDIT_RIGHT, y, "Crédits", size=9, font=FONT_BOLD)
    text_right(_X_BALANCE_RIGHT, y, "Solde", size=9, font=FONT_BOLD)

    y += 20
    text(_X_DESC, y, "Solde d'ouverture")
    text_right(_X_BALANCE_RIGHT, y, _fr_amount(_OPENING_BALANCE))

    for row in rows:
        y += 18
        text(_X_DATE, y, row["date"])
        text(_X_DESC, y, row["description"])
        if row["debit"] is not None:
            text_right(_X_DEBIT_RIGHT, y, _fr_amount(row["debit"]))
        if row["credit"] is not None:
            text_right(_X_CREDIT_RIGHT, y, _fr_amount(row["credit"]))
        text_right(_X_BALANCE_RIGHT, y, _fr_amount(row["balance"]))

    # Totals row: the importer looks for a row containing both "totaux"
    # and "fermeture" and reads the LAST TWO numbers on it as
    # (debits, credits) -- so the balance column stays empty here.
    y += 26
    text(_X_DESC, y, "Totaux et solde de fermeture", font=FONT_BOLD)
    text_right(_X_DEBIT_RIGHT, y, _fr_amount(total_debits), font=FONT_BOLD)
    text_right(_X_CREDIT_RIGHT, y, _fr_amount(total_credits), font=FONT_BOLD)

    # Closing balance on its own row below, in the last column: the
    # importer takes the bottom-most parseable number in the last column
    # of the last table as the closing balance.
    y += 18
    text(
        _X_DESC, y,
        f"Solde de fermeture au {_PERIOD_END_DAY} {_PERIOD_END_MONTH_FR} {_PERIOD_END_YEAR}",
        font=FONT_BOLD,
    )
    text_right(_X_BALANCE_RIGHT, y, _fr_amount(closing_balance), font=FONT_BOLD)

    doc.set_metadata(_METADATA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # no_new_id keeps the PDF's /ID stable across runs; without it PyMuPDF
    # generates a fresh identifier each save and the output would only be
    # semantically, not byte-, deterministic.
    doc.save(str(out_path), garbage=4, deflate=True, clean=True, no_new_id=True)
    doc.close()

    return {
        "period_end": f"{_PERIOD_END_YEAR}-01-{_PERIOD_END_DAY:02d}",
        "total_debits": total_debits,
        "total_credits": total_credits,
        "closing_balance": closing_balance,
        "transaction_count": len(rows),
        "rows": rows,
    }


GENERATORS = {
    "bmo_chequing_fr_demo.pdf": build_bmo_chequing_pdf,
}

# Which registered importer each generated PDF is meant to be parsed by.
IMPORTER_KEYS = {
    "bmo_chequing_fr_demo.pdf": "cash.bmo_chequing_fr",
}

# export_date is `datetime.now()` inside the importers, so it can't be part
# of a stable golden file. It is replaced by this placeholder in the
# reviewed .expected.json and skipped when tests compare.
EXPORT_DATE_PLACEHOLDER = "<parse-date>"


def expected_json_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix("").with_suffix(".expected.json")


def generate_all(out_dir: Path = PDF_FIXTURE_DIR) -> Dict[str, Dict[str, Any]]:
    results = {}
    for filename, builder in GENERATORS.items():
        target = out_dir / filename
        results[filename] = builder(target)
        print(f"✅ wrote {target}")
    return results


def update_expected(out_dir: Path = PDF_FIXTURE_DIR) -> None:
    """Re-parse each generated PDF with its registered importer and rewrite
    the reviewed golden JSON beside it.

    Only this path needs Camelot/ghostscript installed; plain PDF
    generation does not. Review the resulting diff before committing --
    these files are the reviewed record of what the real importer
    produces (Milestone 4 promotion criterion 3).
    """
    import json

    from app.services.importers.registry import importer_by_key

    for filename, key in IMPORTER_KEYS.items():
        pdf_path = out_dir / filename
        importer = importer_by_key(key)
        if importer is None:
            raise RuntimeError(f"no registered importer for key {key!r}")
        parsed = importer.parse_to_json(pdf_path)
        parsed["export_date"] = EXPORT_DATE_PLACEHOLDER
        target = expected_json_path(pdf_path)
        target.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"✅ wrote {target}")


if __name__ == "__main__":
    generate_all()
    if "--update-expected" in sys.argv:
        update_expected()
