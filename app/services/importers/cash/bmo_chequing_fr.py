"""app/services/importers/cash/bmo_chequing_fr.py

Importer for BMO Chequing (French) PDF statements.

This importer extracts structured transaction data from multipage French BMO PDF statements
using pdfplumber (for text diagnostics) and Camelot (for tabular extraction). It also parses
metadata such as the statement period end, totals, and closing balance. Designed for robustness
against layout shifts and month name variations.

Logging:
    - INFO  → Concise summaries (progress, totals, validation)
    - DEBUG → Detailed per-page/table dumps for troubleshooting extraction issues
"""

from __future__ import annotations

import hashlib
import re
import json
import unicodedata
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import camelot
import pdfplumber
import jsonschema

from ..template import TemplateImporter, Transaction, StatementSummary

# ─────────────────────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────────────────────
LOG = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Schema validation setup
# ─────────────────────────────────────────────────────────────
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)

# ─────────────────────────────────────────────────────────────
# Helpers: normalization and parsing utilities
# ─────────────────────────────────────────────────────────────

_FR_MONTHS = {
    "janv": "01", "janvier": "01",
    "fevr": "02", "fevrier": "02", "févr": "02", "février": "02", "fév": "02", "fev": "02",
    "mars": "03",
    "avr": "04", "avril": "04",
    "mai": "05",
    "juin": "06",
    "juil": "07", "juillet": "07",
    "aout": "08", "août": "08",
    "sept": "09", "septembre": "09",
    "oct": "10", "octobre": "10",
    "nov": "11", "novembre": "11",
    "dec": "12", "déc": "12", "decembre": "12", "décembre": "12",
}


TRANSFER_MATCHERS = [
    # Online transfers (TF), robust to duplication and spacing
    re.compile(r"\bVirement\s+en\s+ligne\b.*\bTF\b", re.I),

    re.compile(r"\bBMO\s+NB\s+DEPOTS\b", re.I),

    # Placements / brokerage transfers
    re.compile(r"^Transfert de patrimoine\b", re.I),
    re.compile(r"\bPlacement\s+de\s+Nesbitt\s+Burns\b", re.I),
    re.compile(r"\bNESB(IT)?T?\s*BRNS\b", re.I),  # optional: catch shortened variants
]

def _is_transfer(desc: str) -> bool:
    return any(rx.search(desc) for rx in TRANSFER_MATCHERS)

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _parse_fr_amount(s: str | None) -> Optional[float]:
    if not s:
        return None
    t = str(s).replace("\xa0", " ").strip()
    t = t.replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def _fr_day_month_to_iso(date_str: str, year_hint: int, statement_month: int) -> str:
    """
    Convert '15 janv', '3 déc', '12 fevr.' → YYYY-MM-DD with correct year rollover.

    Rules:
      - If parsed month > statement_month → belongs to previous year.
      - Else → belongs to year_hint.

    Example:
      Statement ends Jan 2025 → statement_month = 1
      Parsed '18 déc' → month=12 > 1 → year = 2024
    """
    m = re.match(r"(\d{1,2})(?:\s*er)?\s*([A-Za-zéûôîïàèêçÉÂÊÎÔÛÀÇÙ\.]+)", date_str.strip())
    if not m:
        raise ValueError(f"Unrecognized date: {date_str}")

    day = int(m.group(1))
    month_raw = m.group(2)
    month_txt = _strip_accents(month_raw.replace('.', '').lower())
    month_num_str = _FR_MONTHS.get(month_txt)
    if not month_num_str:
        raise ValueError(f"Unknown FR month token: '{month_raw}' → norm='{month_txt}'")

    month_num = int(month_num_str)

    # --- YEAR ROLLOVER FIX ---
    if month_num > statement_month:
        year = year_hint - 1
    else:
        year = year_hint

    return f"{year:04d}-{month_num:02d}-{day:02d}"



def _stable16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────
# Diagnostic structure
# ─────────────────────────────────────────────────────────────
@dataclass
class ImportDiag:
    pdf_name: str
    pages: int = 0
    tables: int = 0
    tx_parsed: int = 0
    skipped_no_date_or_desc: int = 0
    skipped_bad_date: int = 0
    skipped_no_amount: int = 0
    sample_bad_date: List[str] = field(default_factory=list)
    sample_rows: List[str] = field(default_factory=list)

    def log_summary(self) -> None:
        LOG.info(
            "Parsed %s | pages=%d tables=%d tx=%d (skips: no_date/desc=%d, bad_date=%d, no_amount=%d)",
            self.pdf_name, self.pages, self.tables, self.tx_parsed,
            self.skipped_no_date_or_desc, self.skipped_bad_date, self.skipped_no_amount,
        )
        if self.sample_bad_date:
            LOG.info("Examples of bad dates (max 3): %s", "; ".join(self.sample_bad_date[:3]))


# ─────────────────────────────────────────────────────────────
# PDF helpers
# ─────────────────────────────────────────────────────────────

def _extract_all_text(path: Path, diag: Optional[ImportDiag] = None) -> str:
    chunks: List[str] = []
    with pdfplumber.open(path) as pdf:
        if diag:
            diag.pages = len(pdf.pages)
        LOG.debug("📄 pdfplumber: extracting %d pages from %s", len(pdf.pages), path.name)
        for i, page in enumerate(pdf.pages, start=1):
            try:
                raw = page.extract_text() or ""
                chunks.append(raw)
                LOG.debug("📄 Page %d text length=%d", i, len(raw))
            except Exception as e:
                LOG.warning("⚠️ Failed to extract text on page %d: %s", i, e)
    return "\n".join(chunks)


def _extract_totals_from_tables(tables) -> tuple[float, float]:
    for t in tables:
        for r in t.df.values.tolist():
            cells = [str(c or "").strip() for c in r]
            joined_norm = _strip_accents(" ".join(cells).lower())
            if "totaux" in joined_norm and "fermeture" in joined_norm:
                nums = [_parse_fr_amount(c) for c in cells if _parse_fr_amount(c) is not None]
                if len(nums) >= 2:
                    deb, cred = nums[-2], nums[-1]
                    LOG.info("🏁 Found totals in Camelot: debits=%.2f credits=%.2f", deb, cred)
                    return deb, cred
    return 0.0, 0.0


def _extract_closing_balance_from_tables(tables) -> Optional[float]:
    try:
        for t in reversed(tables):
            df = t.df
            if df.shape[1] < 2:
                continue
            balance_col = df.shape[1] - 1
            for r in reversed(df.values.tolist()):
                val = _parse_fr_amount(r[balance_col]) if len(r) > balance_col else None
                if val is not None:
                    LOG.info("💰 Found closing balance in Camelot: %.2f", val)
                    return val
    except Exception as e:
        LOG.warning("⚠️ Failed to extract closing balance: %s", e)
    return None


# ─────────────────────────────────────────────────────────────
# Importer class
# ─────────────────────────────────────────────────────────────
class BmoChequingFrImporter(TemplateImporter):
    key = "cash.bmo_chequing_fr"
    label = "BMO – Compte de chèques (FR)"
    account_side = "asset"
    account_class = "cash"
    input_kind = "pdf"
    formats = ["pdf"]
    signature_name = "BMO_CHEQUING_FRENCH"
    institution_default = "BMO"

    PERIOD_RE = re.compile(r"P[ée]riode\s+termin[ée]e\s+le\s+(\d{1,2})\s+([A-Za-zéûôîïàèêç\.]+)\s+(\d{4})", re.I)
    DOMICILE_RE = re.compile(r"Num[ée]ro\s+de\s+domiciliation\s*:\s*(\d+)", re.I)
    # "Compte de chèques principal #32223987-056"
    ACCNUM_RE = re.compile(
        r"comptedech[eè]quesprincipal(?:\s*\(suite\))?\s*#?\s*([0-9\-]{6,20}?)(?=\s*\d{5,})",
        re.IGNORECASE
    )

    def detect(self, path: Path) -> bool:
        try:
            text = _extract_all_text(path)
            norm = _strip_accents(text.lower()).replace(" ", "").replace("\xa0", "")
            return "relevedeservicesbancairescourants" in norm and "comptedechequesprincipal" in norm
        except Exception:
            return False

    def _extract_metadata(self, path: Path, diag: ImportDiag) -> Dict[str, Any]:
        full_text = _extract_all_text(path, diag)
        m = self.PERIOD_RE.search(full_text)
        if not m:
            raise ValueError("Missing period end date")
        day, fr_month, year = int(m.group(1)), m.group(2), int(m.group(3))
        # Use year_hint = year (statement year), and statement_month for rollover
        # We temporarily pass statement_month = parsed month itself so its year remains unchanged.
        period_end_iso = _fr_day_month_to_iso(f"{day} {fr_month}", year,
                                              statement_month=int(_FR_MONTHS[_strip_accents(fr_month.lower())]))
        period_end = datetime.strptime(period_end_iso, "%Y-%m-%d")
        LOG.info("Period end: %s (from '%s %s %s')", period_end_iso, day, fr_month, year)

        dom_match = self.DOMICILE_RE.search(full_text)
        domiciliation = dom_match.group(1) if dom_match else "0000"

        # Use domiciliation as account number / digits
        acc_match = self.ACCNUM_RE.search(full_text)
        if acc_match:
            raw_acc = acc_match.group(1)
            # Remove spaces/dashes
            clean_acc = re.sub(r"[^\d]", "", raw_acc)
            account_number = clean_acc
            account_digits = clean_acc[-4:]
            LOG.info(f"Parsed account number: raw='{raw_acc}' → {account_number}")
        else:
            account_number = "UNKNOWN"
            account_digits = "0000"
            LOG.warning("Account number not found in statement.")

        return {
            "account_name": "Compte de chèques principal",
            "account_number": account_number,
            "account_digits": account_digits,
            "period_end": period_end,
            "statement_month": period_end.month,
            "domiciliation": domiciliation,
        }

    def _extract_transactions(self, path: Path, year_hint: int, diag: ImportDiag, statement_month: int) -> List[Transaction]:
        LOG.info("📊 Extracting transactions (year hint=%d, stmt month=%d) from %s",
                 year_hint, statement_month, path.name)
        tables = camelot.read_pdf(str(path), pages="all", flavor="stream")
        diag.tables = len(tables)
        LOG.info("📑 Camelot detected %d tables", diag.tables)
        transactions: List[Transaction] = []
        transfer_pattern = re.compile(r"^Virement en ligne, TF\s+[A-Za-z0-9]+", re.I)

        for t in tables:
            for row in t.df.values.tolist():
                row_clean = [str(c or "").strip() for c in row]
                if not any(row_clean):
                    continue
                row_norm = _strip_accents(" ".join(row_clean).lower())
                if "totaux" in row_norm and "fermeture" in row_norm:
                    continue

                date_str = row_clean[0] if len(row_clean) > 0 else ""
                desc = row_clean[1] if len(row_clean) > 1 else ""
                debit_raw = row_clean[2] if len(row_clean) > 2 else None
                credit_raw = row_clean[3] if len(row_clean) > 3 else None

                if not date_str or not desc:
                    diag.skipped_no_date_or_desc += 1
                    continue
                try:
                    op_date = _fr_day_month_to_iso(date_str, year_hint, statement_month)
                except Exception as e:
                    diag.skipped_bad_date += 1
                    continue

                dval = _parse_fr_amount(debit_raw)
                cval = _parse_fr_amount(credit_raw)
                if dval is None and cval is None:
                    diag.skipped_no_amount += 1
                    continue

                ###############################################################################
                # Every dollar in the system originates from income (or an opening balance).
                # Transfers only move dollars around.
                # Only expenses can destroy dollars.
                #
                # here are two legitimate “origins” of money:
                #   Income (external inflow)
                #   Opening balance (pre-app wealth)
                #
                # Once those are set:
                #   Transfers conserve value
                #   Expenses consume value
                #
                # This is why:
                #   importing statement balances is essential
                #   importing from “day 1 with zero” is not required
                ################################################################################
                if _is_transfer(desc):
                    tx_type = "transfer"
                elif cval and cval > 0:
                    tx_type = "income"
                else:
                    tx_type = "expense"

                ##############################################################################
                # system-wide invariant: amount = +credit - debit
                # so amount must be signed as it's used in net worth that includes transfers
                ##############################################################################
                if dval and dval > 0:
                    amount = -dval
                    debit = dval
                    credit = 0.0
                elif cval and cval > 0:
                    amount = +cval
                    debit = 0.0
                    credit = cval
                else:
                    amount = 0.0
                    debit = 0.0
                    credit = 0.0

                transactions.append({
                    "operation_date": op_date,
                    "description": desc,
                    "amount": amount,  # signed
                    "debit": debit,
                    "credit": credit,
                    "is_credit": credit > 0,
                    "transaction_type": tx_type,
                })
                diag.tx_parsed += 1

        LOG.info("✅ Parsed transactions: %d", len(transactions))
        return transactions

    def _validate(self, txs: List[Transaction], metadata: Dict[str, Any]) -> StatementSummary:
        try:
            tables = camelot.read_pdf(str(metadata["pdf_path"]), pages="all", flavor="stream")
            total_debits_pdf, total_credits_pdf = _extract_totals_from_tables(tables)
            closing_balance = _extract_closing_balance_from_tables(tables)
        except Exception as e:
            LOG.warning("⚠️ Failed to extract totals via Camelot: %s", e)
            total_debits_pdf, total_credits_pdf, closing_balance = 0.0, 0.0, None

        computed_debits = round(sum(t.get("debit", 0.0) for t in txs), 2)
        computed_credits = round(sum(t.get("credit", 0.0) for t in txs), 2)

        validation_passed = (
            abs(computed_debits - total_debits_pdf) < 0.5 and abs(computed_credits - total_credits_pdf) < 0.5
        )

        msg = (
            "Debits and credits reconcile with statement totals."
            if validation_passed
            else f"Debit/credit reconciliation failed (expected debits={total_debits_pdf}, credits={total_credits_pdf})"
        )
        LOG.info(msg)
        return {
            "opening_balance": None,
            "total_debits": total_debits_pdf,
            "total_credits": total_credits_pdf,
            "closing_balance": closing_balance,
            "validation_passed": bool(validation_passed),
            "validation_message": msg,
        }

    def parse_to_json(self, path: Path) -> Dict[str, Any]:
        diag = ImportDiag(pdf_name=Path(path).name)
        LOG.info("Importing: %s", diag.pdf_name)

        meta = self._extract_metadata(path, diag)
        meta["pdf_path"] = path
        txs = self._extract_transactions(path, meta["period_end"].year, diag, meta["statement_month"])
        summary = self._validate(txs, meta)

        diag.log_summary()
        if diag.sample_rows:
            LOG.info(
                "Examples of skipped rows without date/desc (max 3): %s",
                "; ".join(diag.sample_rows[:3])
            )

        # ─────────────────────────────────────────────
        # Build account summary (single chequing account)
        # ─────────────────────────────────────────────
        closing_balance = summary.get("closing_balance")
        period_end = meta["period_end"]
        summary["statement_date"] = period_end.strftime("%Y-%m-%d")
        domiciliation = meta.get("domiciliation", "")
        account_number = meta.get("account_number", domiciliation)
        account_digits = meta.get("account_digits", domiciliation[-4:] if domiciliation else "")

        # Stable, human-readable-ish account_id
        account_id = f"bmo_chequing_{account_digits}" if account_digits else _stable16(
            f"bmo_chequing|{domiciliation or path.stem}"
        )

        account_summary: Dict[str, Any] = {
            "account_id": account_id,
            "account_name": f"BMO Cash {account_digits}",
            "account_side": self.account_side,
            "account_class": self.account_class,
            "institution": self.institution_default,
            "base_currency": "CAD",  # there is no reference to explicit base currency in bmo statements
            "account_number": account_number,
            "current_balance": {
                "date": period_end.strftime("%Y-%m-%d"),
                "amount": closing_balance,
            },
            "notes": "Chequing balance derived from statement (closing balance).",
        }

        # ─────────────────────────────────────────────
        # Statements: attach directly to parent account
        # (no real cardholder model for cash)
        # ─────────────────────────────────────────────

        statements = [{
            "summary": summary,
            "transactions": txs,
            "parent_account_id": account_id,
        }]

        output = {
            "export_date": datetime.now(timezone.utc).date().isoformat(),
            "document_signature": {"document_type": self.signature_name},

            "validation_passed": summary["validation_passed"],
            "validation_message": summary["validation_message"],

            "account_summary": account_summary,
            "statements": statements,
        }

        errors = sorted(_VALIDATOR.iter_errors(output), key=lambda e: e.path)
        if errors:
            for e in errors:
                LOG.error("Schema error at %s: %s", list(e.path), e.message)
            raise ValueError("Schema validation failed.")

        LOG.info(
            "Done: %s | transactions=%d closing_balance=%s",
            diag.pdf_name,
            len(txs),
            f"{closing_balance:.2f}" if closing_balance is not None else "n/a",
        )
        return output
