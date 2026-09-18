#!/usr/bin/env python3
"""Generate synthetic statement PDFs that the *real* importers can parse.

Importers are never weakened to accommodate a fixture: these PDFs must
satisfy the existing signature/layout rules as they
already stand).

Currently covers every registered PDF importer:
  - cash.bmo_chequing_fr
  - credit.bmo_mastercard_fr
  - loc.bmo_heloc_fr
  - investment.bmo_nesbitt_ca
  - investment.interactive_brokers_ca
  - investment.questrade_equity
  - asset.car_fmv_cargurus
  - asset.home_fmv_evaluation_fonciere

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
_PUBLIC_FIXTURE_METADATA = {
    **_METADATA,
    "title": "Synthetic CLEAR importer fixture",
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
        "kind": "transactional",
        "account_id": _ACCOUNT_NUMBER_VISIBLE,
        "period_end": f"{_PERIOD_END_YEAR}-01-{_PERIOD_END_DAY:02d}",
        "total_debits": total_debits,
        "total_credits": total_credits,
        "closing_balance": closing_balance,
        "transaction_count": len(rows),
        "rows": rows,
    }


# ─────────────────────────────────────────────────────────────
# Shared helpers for the remaining public fixtures
# ─────────────────────────────────────────────────────────────

def _save_document(doc: fitz.Document, out_path: Path) -> None:
    """Save a tiny PDF with stable metadata and a stable PDF identifier."""
    doc.set_metadata(_PUBLIC_FIXTURE_METADATA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path), garbage=4, deflate=True, clean=True, no_new_id=True)
    doc.close()


def _build_lines_pdf(
    out_path: Path,
    lines: List[str],
    *,
    font_size: float = 10.0,
    line_height: float = 18.0,
) -> None:
    """Build a deterministic, text-only statement with one line per item."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 54.0
    for line in lines:
        page.insert_text((42.0, y), line, fontname=FONT, fontsize=font_size)
        y += line_height
    _save_document(doc, out_path)


# ─────────────────────────────────────────────────────────────
# BMO Mastercard (FR)
# ─────────────────────────────────────────────────────────────

def build_bmo_mastercard_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "BMO Banque de Montréal",
        "Carte Mastercard BMO",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Date du relevé 15 janvier 2025",
        "Solde dû 80,00 $",
        "No de carte: XXXX XXXX XXXX 1111 DEMO CLIENT",
        "1 janv 2 janv DEMO EPICERIE TEST 100,00",
        "3 janv 3 janv PAIEMENT DEMO TEST 20,00 CR",
        "Sous-total pour DEMO CLIENT 100,00",
    ]
    _build_lines_pdf(out_path, lines)
    return {
        "kind": "transactional",
        "account_id": "bmo_credit_combined",
        "period_end": "2025-01-15",
        "closing_balance": -80.0,
        "transaction_count": 2,
        "regular_count": 1,
        "transfer_count": 1,
    }


# ─────────────────────────────────────────────────────────────
# BMO HELOC (FR)
# ─────────────────────────────────────────────────────────────

def build_bmo_heloc_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "BMO Banque de Montréal",
        "Margexpress sur valeur domiciliaire",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Numéro de compte : 1111 1111 1234",
        "Montréal QC H1H 1H1",
        "22 janvier 2025",
        "Solde précédent, 22déc 1 000,00 $",
        "Nouveau solde, 22 janv 900,00 $",
        "20déc 1 20déc INTERET DEMO TEST 50,00",
        "10janv 2 10janv PAIEMENT DEMO TEST 150,00 CR",
        "Veuillez payer selon votre convention.",
    ]
    _build_lines_pdf(out_path, lines)
    return {
        "kind": "transactional",
        "account_id": "bmo_heloc_1234",
        "period_end": "2025-01-22",
        "closing_balance": -900.0,
        "transaction_count": 2,
        "regular_count": 1,
        "transfer_count": 1,
    }


# ─────────────────────────────────────────────────────────────
# BMO Nesbitt Burns (NAV)
# ─────────────────────────────────────────────────────────────

def build_bmo_nesbitt_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "Relevé BMO Nesbitt Burns",
        "31 janvier 2025",
        "Compte REER #111-11111-11",
        "bmonesbittburns.com",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Total global ($CAD) 12 345,67",
    ]
    _build_lines_pdf(out_path, lines)
    return {
        "kind": "nav",
        "account_id": "111-11111-11",
        "snapshot_date": "2025-01-31",
        "nav": 12345.67,
    }


# ─────────────────────────────────────────────────────────────
# Interactive Brokers Canada (NAV + Camelot stream table)
# ─────────────────────────────────────────────────────────────

def build_interactive_brokers_pdf(out_path: Path) -> Dict[str, Any]:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    def text(x: float, y: float, value: str, *, bold: bool = False) -> None:
        page.insert_text(
            (x, y), value, fontname=FONT_BOLD if bold else FONT, fontsize=10.0
        )

    text(42, 54, "Interactive Brokers Canada Inc.", bold=True)
    text(42, 72, "Activity Statement", bold=True)
    text(42, 90, "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION")
    text(42, 108, "Account U111111")
    text(42, 126, "Account Type Individual")
    text(42, 144, "Account Capabilities Cash")
    text(42, 162, "Base Currency CAD")
    text(42, 180, "Customer Type Tax-Free Savings Account")

    # Camelot stream mode infers these two columns from their horizontal
    # separation. The importer deliberately reads the cell immediately to
    # the right of "Ending Value".
    text(42, 228, "Net Asset Value", bold=True)
    text(42, 246, "Period")
    text(250, 246, "January 31, 2025")
    text(42, 264, "Starting Value")
    text(250, 264, "10,000.00")
    text(42, 282, "Change in NAV")
    text(250, 282, "2,345.67")
    text(42, 300, "Ending Value", bold=True)
    text(250, 300, "12,345.67", bold=True)

    _save_document(doc, out_path)
    return {
        "kind": "nav",
        "account_id": "U111111",
        "snapshot_date": "2025-01-31",
        "nav": 12345.67,
    }


# ─────────────────────────────────────────────────────────────
# Questrade (NAV)
# ─────────────────────────────────────────────────────────────

def build_questrade_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "Questrade",
        "Dealer: Questrade, Inc.",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Account #: 11111111",
        "Type: Tax-Free Savings Account (TFSA) Account opened: January 1, 2020",
        "Current month: January 31, 2025",
        "Current month balance: $12,345.67",
        "Balance Changes Combined in (CAD)",
        "Closing balance 12,345.67",
    ]
    _build_lines_pdf(out_path, lines)
    return {
        "kind": "nav",
        "account_id": "11111111",
        "snapshot_date": "2025-01-31",
        "nav": 12345.67,
    }


# ─────────────────────────────────────────────────────────────
# CarGurus vehicle valuation (NAV)
# ─────────────────────────────────────────────────────────────

def build_cargurus_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "www.cargurus.ca",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Year: 2018",
        "Make: Demo",
        "Model: Roadster",
        "Great Deal $12,345",
        "Date: 2025-01-31",
    ]
    _build_lines_pdf(out_path, lines)
    # Keep the expected ID independently visible to the test rather than
    # importing the production hash helper into fixture generation.
    return {
        "kind": "nav",
        "account_name": "2018 Demo Roadster",
        "snapshot_date": "2025-01-31",
        "nav": 12345.0,
    }


# ─────────────────────────────────────────────────────────────
# Québec municipal home valuation (NAV)
# ─────────────────────────────────────────────────────────────

def build_home_valuation_pdf(out_path: Path) -> Dict[str, Any]:
    lines = [
        "role-evaluation-fonciere",
        "DOCUMENT SYNTHETIQUE - DONNEES DE DEMONSTRATION",
        "Numéro de lot : 1111111",
        "Adresse : 123 Avenue Démonstration",
        "Valeur du terrain : 100 000 $",
        "Valeur du bâtiment : 200 000 $",
        "Valeur de l'immeuble : 300 000 $",
        "Date du rapport : 2025-01-31",
    ]
    _build_lines_pdf(out_path, lines)
    return {
        "kind": "nav",
        "account_id": "1111111",
        "snapshot_date": "2025-01-31",
        "nav": 300000.0,
    }


GENERATORS = {
    "bmo_chequing_fr_demo.pdf": build_bmo_chequing_pdf,
    "bmo_mastercard_fr_demo.pdf": build_bmo_mastercard_pdf,
    "bmo_heloc_fr_demo.pdf": build_bmo_heloc_pdf,
    "bmo_nesbitt_ca_demo.pdf": build_bmo_nesbitt_pdf,
    "interactive_brokers_ca_demo.pdf": build_interactive_brokers_pdf,
    "questrade_equity_demo.pdf": build_questrade_pdf,
    "car_cargurus_valuation_demo.pdf": build_cargurus_pdf,
    "home_evaluation_fonciere_demo.pdf": build_home_valuation_pdf,
}

# Which registered importer each generated PDF is meant to be parsed by.
IMPORTER_KEYS = {
    "bmo_chequing_fr_demo.pdf": "cash.bmo_chequing_fr",
    "bmo_mastercard_fr_demo.pdf": "credit.bmo_mastercard_fr",
    "bmo_heloc_fr_demo.pdf": "loc.bmo_heloc_fr",
    "bmo_nesbitt_ca_demo.pdf": "investment.bmo_nesbitt_ca",
    "interactive_brokers_ca_demo.pdf": "investment.interactive_brokers_ca",
    "questrade_equity_demo.pdf": "investment.questrade_equity",
    "car_cargurus_valuation_demo.pdf": "asset.car_fmv_cargurus",
    "home_evaluation_fonciere_demo.pdf": "asset.home_fmv_evaluation_fonciere",
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
