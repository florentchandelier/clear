"""
app/services/importers/loc/bmo_heloc_fr.py

Importer for BMO Home Equity Line of Credit (HELOC) – "Margexpress sur valeur domiciliaire" (FR).

This importer extracts structured transaction data from French BMO HELOC statements.
All relevant information is on the first page in the "Votre compte en un coup d'oeil"
block and the "Détails de vos transactions" section.

Design:
    - Single liability account (no cardholders).
    - Treats the HELOC like a credit product with:
        • Opening balance  ← "Solde précédent"
        • Closing balance  ← "Nouveau solde"
        • Transactions     ← "Détails de vos transactions"

    - Semantics:
        • Debits (no "CR") increase the debt (advances, interest, fees).
        • Credits ("CR") decrease the debt (payments, adjustments).
        • Transactions are signed as (debit>0, credit>0, amount=debit or credit).
        • For classification:
             - "intérêt"/"interet"/"frais"  → transaction_type = "expense"
             - everything else              → transaction_type = "transfer"
          (principal movements are transfers between accounts.)

Logging:
    - INFO  → Concise summaries (detection, balances, totals, validation)
    - DEBUG → Per-line parsing diagnostics as needed

Validation:
    - Checks that: opening_balance_signed + net_delta ≈ closing_balance_signed
      where net_delta = sum(-debit + credit) over all parsed transactions.
"""

from __future__ import annotations

import re
import json
import unicodedata
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.utils import detect_canadian_statement

import fitz  # PyMuPDF
import jsonschema

from ..template import TemplateImporter, Transaction, StatementSummary

# ─────────────────────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────────────────────
LOG = logging.getLogger(__name__)
# manual override takes precedence - Ensure at least DEBUG for this importer if root logger is higher
#logging.getLogger("app.services.importers.loc.bmo_heloc_fr").setLevel(logging.DEBUG)
#LOG.setLevel(logging.DEBUG)

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


def _strip_accents(s: str) -> str:
    """
    Normalize unicode accents AND fix common OCR glyph confusions
    seen in FR banking PDFs.
    """
    if not s:
        return s

    # 1) Unicode accent stripping
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )

    # 2) OCR post-fixes (very conservative)
    OCR_FIXES = {
        "doc": "dec",   # déc. → doc.
        "fovr": "fevr", # février OCR glitch
        "aoet": "aout", # août OCR glitch
    }

    lowered = s.lower()
    if lowered in OCR_FIXES:
        return OCR_FIXES[lowered]

    return s


def _fold_unicode(s: str) -> str:
    """
    Fully normalize and fold unicode to closest ASCII equivalents.
    Handles accented letters AND alternative Latin characters like 'Ø'.
    """
    # Step 1: NFKD expansion
    s = unicodedata.normalize("NFKD", s)
    # Step 2: remove combining marks
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    # Step 3: replace remaining non-ASCII characters by ASCII fallbacks
    replacements = {
        "ø": "o", "Ø": "O",
        "œ": "oe", "Œ": "OE",
        "ß": "ss",
    }
    out: List[str] = []
    for c in s:
        if c in replacements:
            out.append(replacements[c])
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append(" ")
    return "".join(out)

def _adjust_year_for_rollover(tx_month: int, year_hint: int, statement_month: int) -> int:
    """
    HELOC statements often include transactions from previous months.
    If a transaction month is AFTER the statement month (e.g. Dec > Jan),
    then the transaction belongs to the previous year.

        Example:
            statement_month = 1 (January, 2025)
            tx_month = 12 (December) → belongs to 2024

    This fixes mis-assigned months in January/February statements.
    """
    if tx_month > statement_month:
        return year_hint - 1
    return year_hint

def _fr_day_month_to_iso(date_str: str, year_hint: int) -> str:
    """
    Convert '15 juil', '15 juil.' or '15juil' to 'YYYY-MM-DD' using year_hint.
    """
    m = re.match(
        r"(\d{1,2})(?:\s*er)?\s*([A-Za-zéûôîïàèêçÉÂÊÎÔÛÀÇÙ\.]+)",
        date_str.strip()
    )
    if not m:
        raise ValueError(f"Unrecognized date: {date_str}")
    day = int(m.group(1))
    month_raw = m.group(2)
    month_txt = _strip_accents(month_raw.replace(".", "").lower())
    month_num = _FR_MONTHS.get(month_txt)
    if not month_num:
        raise ValueError(f"Unknown FR month token: '{month_raw}' → norm='{month_txt}'")
    return f"{year_hint:04d}-{month_num}-{day:02d}"


def _parse_amount_token(s: str | None) -> tuple[float, bool]:
    """
    Parse an amount token like '46 825,41', '47 000,00CR', '130,15 $ CR' into:
        (value, is_credit_flag)

    - value is always positive magnitude.
    - is_credit indicates presence of 'CR' suffix (credit to the customer).
    """
    if not s:
        raise ValueError("Empty amount token")
    txt = str(s).replace("\xa0", " ").strip()
    # Detect 'CR' suffix anywhere at end
    m_cr = re.search(r"\bCR\b\s*$", txt, flags=re.IGNORECASE)
    is_credit = bool(m_cr)
    if is_credit:
        txt = re.sub(r"\bCR\b\s*$", "", txt, flags=re.IGNORECASE).strip()
    # Remove currency symbol
    txt = txt.replace("$", "").strip()
    # Remove spaces within digits
    txt = re.sub(r"\s+", "", txt)
    # Normalize decimal separator
    if "," in txt and "." not in txt:
        txt = txt.replace(",", ".")
    try:
        val = float(txt)
    except Exception as e:
        raise ValueError(f"Cannot parse amount from '{s}' (normalized '{txt}'): {e}")
    return val, is_credit


# ─────────────────────────────────────────────────────────────
# Diagnostic structure
# ─────────────────────────────────────────────────────────────
@dataclass
class ImportDiag:
    pdf_name: str
    pages: int = 0
    tx_parsed: int = 0
    skipped_non_tx_lines: int = 0
    skipped_unmatched_pattern: int = 0
    sample_unmatched: List[str] = field(default_factory=list)

    def log_summary(self) -> None:
        LOG.info(
            "Parsed HELOC %s | pages=%d tx=%d (skips: non_tx=%d, unmatched=%d)",
            self.pdf_name,
            self.pages,
            self.tx_parsed,
            self.skipped_non_tx_lines,
            self.skipped_unmatched_pattern,
        )
        if self.sample_unmatched:
            LOG.info(
                "Examples of unmatched transaction lines (max 3): %s",
                "; ".join(self.sample_unmatched[:3]),
            )


# ─────────────────────────────────────────────────────────────
# PDF helpers (PyMuPDF)
# ─────────────────────────────────────────────────────────────
def _extract_all_text(path: Path, diag: Optional[ImportDiag] = None) -> str:
    """
    Extract all text using PyMuPDF, page by page, preserving reading order.
    """
    doc = fitz.open(path)
    if diag:
        diag.pages = len(doc)
    LOG.debug("📄 fitz: extracting %d pages from %s", len(doc), Path(path).name)
    chunks: List[str] = []
    for i, page in enumerate(doc, start=1):
        try:
            raw = page.get_text("text") or ""
            chunks.append(raw)
            LOG.debug("📄 Page %d text length=%d", i, len(raw))
        except Exception as e:
            LOG.warning("⚠️ Failed to extract text on page %d: %s", i, e)
    return "\n".join(chunks)


# ─────────────────────────────────────────────────────────────
# Importer class
# ─────────────────────────────────────────────────────────────
class BmoHelocFrImporter(TemplateImporter):
    """
    Importer for BMO Home Equity Line of Credit – Margexpress sur valeur domiciliaire (FR).
    """

    key = "loc.bmo_heloc_fr"
    label = "BMO – HELOC / Margexpress (FR)"
    account_side = "liability"
    account_class = "loc"
    input_kind = "pdf"
    formats = ["pdf"]
    signature_name = "BMO_HELOC_FRENCH"
    institution_default = "BMO"

    # ---------------------------------------------------------
    # Detect
    # ---------------------------------------------------------
    def detect(self, path: Path) -> bool:
        LOG.info("🔍 [HELOC DETECT] Starting detect() for %s", path.name)
        try:
            raw = _extract_all_text(path, None)
            if not raw or not raw.strip():
                LOG.info("🔍 [HELOC DETECT] EMPTY extracted text")
                return False

            norm = _fold_unicode(raw.lower())
            norm = re.sub(r"\s+", " ", norm)
            nospace = norm.replace(" ", "")

            LOG.info("🔍 [HELOC DETECT] First 300 chars norm: %s", norm[:3000])
            LOG.info("🔍 [HELOC DETECT] First 300 chars nospace: %s", nospace[:300])

            has_margexpress = "margexpress" in norm
            has_domiciliaire = "domiciliaire" in norm

            # OCR-safe: numero / numoro / numøro / numéro…
            acc_regex = re.search(r"num.\s*ro\s+de\s+compte", norm)
            acc_compact_regex = re.search(r"num.rodecompte", nospace)
            acc_ok = bool(acc_regex or acc_compact_regex)

            LOG.info("🔍 margexpress          → %s", has_margexpress)
            LOG.info("🔍 domiciliaire         → %s", has_domiciliaire)
            LOG.info("🔍 numero/numoro de compte → %s", "YES" if acc_ok else "NO")

            ok = has_margexpress and has_domiciliaire and acc_ok
            LOG.info("🔍 FINAL DECISION → %s", "MATCH" if ok else "NO MATCH")
            return ok
        except Exception as e:
            LOG.exception("❌ detect() error: %s", e)
            return False

    # ---------------------------------------------------------
    # Metadata extraction
    # ---------------------------------------------------------
    def _extract_metadata_from_text(self, text: str) -> Dict[str, Any]:
        """
        Extracts:
          - account_name (canonical → 'BMO HELOC')
          - account_number (full) + account_digits
          - statement_date (YYYY-MM-DD)
          - opening_balance_signed
          - closing_balance_signed
        """
        norm = _fold_unicode(text).lower()

        # Account name (canonical label)
        account_name = "BMO HELOC"

        # Account number
        acc_match = re.search(r"num.\s*ro\s+de\s+compte\s*:?\s*([0-9 ]+)",
            norm,
            flags=re.IGNORECASE,
        )
        if not acc_match:
            raise ValueError("Numéro de compte not found")
        account_number_raw = acc_match.group(1).strip()
        digits_only = re.sub(r"\D", "", account_number_raw)
        account_digits = digits_only[-4:] if len(digits_only) >= 4 else digits_only

        # Day + month from "Nouveau solde, 22 mai ..."
        m_ns = re.search(
            r"nouveau\s+solde[, ]+\s*(\d{1,2})\s*([a-z\.]{3,12})",
            norm,
            flags=re.IGNORECASE,
        )
        if not m_ns:
            raise ValueError("Could not extract date from 'Nouveau solde' block")

        day = int(m_ns.group(1))
        month_token = m_ns.group(2)
        LOG.info("📅 Found day/month from 'Nouveau solde': %s %s", day, month_token)

        month_norm = _strip_accents(month_token.lower().replace(".", ""))[:3]

        # Find full dates with year: "22mai 2025", "22 mai 2025", etc.
        full_matches = re.findall(
            r"(\d{1,2})\s*([a-z\.]{3,12})\s*(\d{4})",
            norm,
            flags=re.IGNORECASE,
        )
        LOG.info("🔎 Full date tokens found: %s", full_matches)

        year: Optional[int] = None
        for (d, m, y) in full_matches:
            m_norm = _strip_accents(m.lower().replace(".", ""))[:3]
            if m_norm == month_norm and int(d) == day:
                year = int(y)
                LOG.info("📅 Matched exact full date: %s %s %s", d, m, y)
                break

        if year is None:
            for (_, m, y) in full_matches:
                m_norm = _strip_accents(m.lower().replace(".", ""))[:3]
                if m_norm == month_norm:
                    year = int(y)
                    LOG.info("📅 Matched month/year: %s %s", m, y)
                    break

        if year is None:
            raise ValueError("Could not determine year for HELOC statement")

        statement_date_iso = _fr_day_month_to_iso(f"{day} {month_token}", year)
        statement_date = datetime.strptime(statement_date_iso, "%Y-%m-%d")
        LOG.info("📅 Final statement date: %s", statement_date_iso)

        # Opening balance: handles OCR distortions like "procodent"
        open_match = re.search(
            r"solde\s+pr\S{2,8}dent[^0-9]+(?:\d{1,2}[a-z\.]+)\s+([0-9][0-9\s.,]*\d(?:,\d{2})?)\s*\$?\s*(CR)?",
            norm,
            flags=re.IGNORECASE,
        )
        opening_balance_signed: Optional[float] = None
        if open_match:
            val_raw = open_match.group(1).strip()
            raw_block = open_match.group(0)

            val, _ = _parse_amount_token(val_raw)

            # detect CR anywhere in the matched block
            is_cr_open = bool(re.search(r"\bcr\b", raw_block, flags=re.IGNORECASE))

            sign_open = 1.0 if is_cr_open else -1.0
            opening_balance_signed = sign_open * val
            LOG.info(
                "Opening balance parsed from 'Solde précédent': %s → %.2f",
                val_raw,
                opening_balance_signed,
            )

        # Closing balance: "Nouveau solde, 22 juin 11 111,11 $"
        close_match = re.search(
            r"nouveau\s+solde[^0-9\n\r]*"  # up to date
            r"\d{1,2}\s+[a-z\.]{3,12}\s+"  # day + month
            r"([0-9][0-9\s.,]*\d(?:,\d{2})?)"  # amount
            r"\s*\$?\s*(CR)?\s*$",  # optional CR at END OF LINE
            norm,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        if not close_match:
            raise ValueError("Closing balance ('Nouveau solde') not found")

        close_raw = close_match.group(1).strip()
        close_cr_flag = bool(close_match.group(2))

        # Parse using amount parser
        close_val, is_cr_close = _parse_amount_token(
            close_raw + (" CR" if close_cr_flag else "")
        )

        # Liability semantics:
        closing_balance_signed = (+close_val) if is_cr_close else (-close_val)

        LOG.info(
            "Closing balance parsed from 'Nouveau solde': %s → %.2f",
            close_raw,
            closing_balance_signed,
        )

        meta: Dict[str, Any] = {
            "account_name": account_name,
            "account_number": digits_only,
            "account_digits": account_digits,
            "statement_date": statement_date,
            "statement_year": year,
            "opening_balance_signed": opening_balance_signed,
            "closing_balance_signed": closing_balance_signed,
        }
        return meta

    # ---------------------------------------------------------
    # Transaction extraction (semantic, line-merge)
    # ---------------------------------------------------------
    def _parse_fr_day_month(self, token: str, year: int, statement_month: int) -> str:
        """
        Convert compact FR formats like '9juin', '10juil.', '22avr', '1mar' into ISO yyyy-mm-dd.
        Assumes no spaces and no accents after normalization.
        """
        token = token.replace(".", "").strip().lower()

        # day = leading digits
        m = re.match(r"^(\d{1,2})([a-z]+)$", token)
        if not m:
            raise ValueError(f"Cannot parse FR compact date token: {token}")

        day = int(m.group(1))
        month_txt = m.group(2)

        MONTHS = {
            "janv": 1, "jan": 1, "janvier": 1,
            "fev": 2, "fevr": 2, "fevrier": 2, "fovr": 2,
            "mar": 3, "mars": 3,
            "avr": 4, "avril": 4,
            "mai": 5,
            "juin": 6,
            "jui": 7, "juil": 7, "juillet": 7,
            "aou": 8, "aout": 8, "aoet": 8,
            "sep": 9, "sept": 9, "septembre": 9,
            "oct": 10, "octobre": 10,
            "nov": 11, "novembre": 11,
            "dec": 12, "decembre": 12, "doc":12,
        }

        # find matching month prefix
        month = None
        for key, val in MONTHS.items():
            if month_txt.startswith(key):
                month = val
                break

        if month is None:
            raise ValueError(f"Unknown FR month in token: {token}")

        # apply rollover correction
        corrected_year = _adjust_year_for_rollover(month, year, statement_month)
        return f"{corrected_year:04d}-{month:02d}-{day:02d}"

    def _extract_transactions_from_text(self, norm: str, statement_date: datetime) -> list[dict]:
        """
        Extract HELOC transactions from the flattened PyMuPDF `norm` text,
        using the pattern:

            <op_date> <idx> <post_date> <description...> <amount>[CR]

        Classification rules:
          - Amount ending with "CR"  → transfer (credit to HELOC)
          - Amount without "CR"      → expense (debit from HELOC)
        """

        LOG.info("🔎 TXN: Starting transaction extraction from flattened norm text.")

        # --- Stop at "veuillez payer" ---
        stop_idx = norm.find("veuillez payer")
        scan = norm[:stop_idx] if stop_idx != -1 else norm
        LOG.debug("🔎 TXN: Using %d chars before stop marker.", len(scan))

        # Amount patterns
        amount_plain = r"\d{1,3}(?:\s\d{3})*,\d{2}"
        amount_cr = rf"{amount_plain}\s*cr"

        # Match CR amounts first so they don't get swallowed as plain amounts
        tx_re = re.compile(
            rf"(\d{{1,2}}[a-z\.]+)\s+(\d+)\s+(\d{{1,2}}[a-z\.]+)\s+(.*?)\s+({amount_cr}|{amount_plain})",
            re.IGNORECASE | re.DOTALL,
        )

        matches = list(tx_re.finditer(scan))
        LOG.info("🔎 TXN: Found %d candidate matches.", len(matches))

        txns = []

        for m in matches:
            op_token, idx, post_token, desc, amt_token = m.groups()

            LOG.debug("🔎 MATCH: %s | %s | %s | '%s' | %s",
                      op_token, idx, post_token, desc, amt_token)

            # Detect CR
            has_cr = amt_token.lower().endswith("cr")

            # Normalize clean numeric part
            amt_clean = amt_token.lower().replace("cr", "").strip()
            value = float(amt_clean.replace(" ", "").replace(",", "."))

            # --- ALWAYS assign accounting fields ---
            if has_cr:
                amount = +value
                debit = 0.0
                credit = value
                is_credit = True
            else:
                amount = -value
                debit = value
                credit = 0.0
                is_credit = False

            # --- THEN classify semantically ---
            desc_norm = _strip_accents(desc.lower())

            if has_cr:
                tx_type = "transfer"

            elif re.search(r"\bavance\s+de\s+fonds\b", desc_norm):
                tx_type = "transfer"

            else:
                tx_type = "expense"

            # Parse operation date
            op_date = self._parse_fr_day_month(op_token, statement_date.year, statement_date.month)

            tx = {
                "operation_date": op_date,
                "description": desc.strip(),
                "amount": amount,
                "debit": debit,
                "credit": credit,
                "is_credit": is_credit,
                "transaction_type": tx_type,
            }

            txns.append(tx)

        LOG.info("🧾 TXN: Parsed %d transactions.", len(txns))
        return txns

    # ---------------------------------------------------------
    # Validation
    # ---------------------------------------------------------
    def _validate(self, txs: List[Transaction], meta: Dict[str, Any]) -> StatementSummary:
        """
        Validate by reconciling:
            opening_balance_signed + net_delta ≈ closing_balance_signed

        Where:
            net_delta = sum(-debit + credit) over all transactions.

        We also compute total_debits and total_credits as simple sums.
        """
        opening = meta.get("opening_balance_signed")
        closing = meta.get("closing_balance_signed")

        total_debits = round(sum(t.get("debit", 0.0) for t in txs), 2)
        total_credits = round(sum(t.get("credit", 0.0) for t in txs), 2)
        net_delta = round(sum(-t.get("debit", 0.0) + t.get("credit", 0.0) for t in txs), 2)

        validation_passed = False
        msg = "Validation skipped (missing balances)."

        if opening is not None and closing is not None:
            expected_closing = round(opening + net_delta, 2)
            validation_passed = abs(expected_closing - closing) < 0.5
            if validation_passed:
                msg = (
                    "Validation passed: opening + net_delta ≈ closing "
                    f"({opening:.2f} + {net_delta:.2f} ≈ {closing:.2f})."
                )
            else:
                msg = (
                    "Validation failed: opening + net_delta ≠ closing "
                    f"({opening:.2f} + {net_delta:.2f} = {expected_closing:.2f}, "
                    f"closing={closing:.2f})."
                )

        LOG.info(
            "Validation summary: debits=%.2f credits=%.2f net_delta=%.2f (%s)",
            total_debits,
            total_credits,
            net_delta,
            "OK" if validation_passed else "MISMATCH",
        )
        LOG.info(msg)

        summary: StatementSummary = {
            "statement_date": meta["statement_date"].strftime("%Y-%m-%d"),
            "opening_balance": opening,
            "closing_balance": closing,
            "total_debits": total_debits,
            "total_credits": total_credits,
            "net_amount": net_delta,
            "statement_subtotal": closing,
            "validation_passed": bool(validation_passed),
            "validation_message": msg,
        }
        return summary

    # ---------------------------------------------------------
    # Main entrypoint (override TemplateImporter)
    # ---------------------------------------------------------
    def parse_to_json(self, path: Path) -> Dict[str, Any]:
        diag = ImportDiag(pdf_name=Path(path).name)
        LOG.info("Importing BMO HELOC: %s", diag.pdf_name)

        full_text = _extract_all_text(path, diag)
        meta = self._extract_metadata_from_text(full_text)

        norm = _fold_unicode(full_text).lower()
        norm = re.sub(r"\s+", " ", norm)

        txs = self._extract_transactions_from_text(
            norm=norm,  # or better: norm text
            statement_date=meta["statement_date"],
        )

        summary = self._validate(txs, meta)
        diag.log_summary()

        # Account summary for HELOC (single liability account)
        statement_date = meta["statement_date"]
        closing_signed = meta.get("closing_balance_signed")
        account_id = f"bmo_heloc_{meta['account_digits']}" if meta.get("account_digits") else "bmo_heloc"

        # authoritative currency
        is_canadian = detect_canadian_statement(norm)
        if not is_canadian:
            raise ValueError(
                "Unable to determine base currency: statement does not appear Canadian."
            )
        base_currency = "CAD"

        account_summary: Dict[str, Any] = {
            "account_id": account_id,
            "account_name": meta["account_name"],
            "account_side": self.account_side,
            "account_class": self.account_class,
            "institution": self.institution_default,
            "base_currency": base_currency,
            "current_balance": {
                "date": statement_date.strftime("%Y-%m-%d"),
                "amount": closing_signed,
            },
            "notes": (
                "HELOC balance derived from 'Nouveau solde' in 'Votre compte en un coup d'oeil'. "
                "Currency inferred as CAD from Canadian address on statement."
            ),
        }

        statements = [{
            "summary": summary,
            "transactions": txs,
            "parent_account_id": account_id,
        }]

        output: Dict[str, Any] = {
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
            raise ValueError("Schema validation failed for BMO HELOC importer.")

        LOG.info(
            "Done BMO HELOC: %s | transactions=%d closing_balance=%.2f",
            diag.pdf_name,
            len(txs),
            closing_signed if closing_signed is not None else 0.0,
        )
        return output
