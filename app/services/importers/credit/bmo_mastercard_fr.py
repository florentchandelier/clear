# app/services/importers/credit/bmo_mastercard_fr.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List
import math
import re
import pdfplumber
from datetime import datetime, timezone
import json
import jsonschema
import sys

from ..template import TemplateImporter, Transaction, StatementSummary

# ─────────────────────────────────────────────
# Logging helper
# ─────────────────────────────────────────────
def _log(msg: str):
    print(f"[BMO_IMPORT] {msg}", file=sys.stderr)

# ─────────────────────────────────────────────
# Schema load
# ─────────────────────────────────────────────
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)

_FR_MONTHS = {
    "janv": "01", "janvier": "01",
    "févr": "02", "fevr": "02", "février": "02", "fev": "02", "fevrier": "02",
    "mars": "03",
    "avr": "04", "avril": "04",
    "mai": "05",
    "juin": "06",
    "juil": "07", "juillet": "07",
    "août": "08", "aout": "08",
    "sept": "09", "septembre": "09",
    "oct": "10", "octobre": "10",
    "nov": "11", "novembre": "11",
    "déc": "12", "décembre": "12", "dec": "12", "decembre": "12",
}
_FR_MONTH_PATTERN = "|".join(sorted((re.escape(m) for m in _FR_MONTHS.keys()), key=len, reverse=True))
_AMOUNT_TAIL_RE = re.compile(r"(.*\S)\s+(\d+(?:[\s,]\d{3})*,\d{2}(?:\s+CR)?)$")
_FX_HINT_RE = re.compile(r"\b(?:EUR|USD|GBP)\s*([0-9]+(?:\.[0-9]+)?)@([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)

# ─────────────────────────────────────────────
def _adjust_year_for_statement(month: int, year_hint: int, statement_month: int) -> int:
    """
    If the transaction month is AFTER the statement month (e.g., Dec > Jan),
    then it belongs to the PREVIOUS year.
    """
    if month > statement_month:
        return year_hint - 1
    return year_hint


def _fr_day_month_to_iso(s: str, default_year: int, statement_month: int) -> str:
    """
    Convert '15 juil' or '15 juil 2025' → YYYY-MM-DD
    with corrected cross-year handling using statement_month.
    """
    if not s:
        raise ValueError(f"Unparseable FR date: '{s}'")
    s = s.strip()

    # allow optional dot in month (e.g. 'avr.' or 'janv.')
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-zéûùôîïëêàâç\.]+?)(?:\s+(\d{4}))?\s*$", s)
    if not m:
        raise ValueError(f"Unparseable FR date: '{s}'")

    day = int(m.group(1))
    mon_key = m.group(2).lower().rstrip(".")  # strip trailing .
    mon = _FR_MONTHS.get(mon_key)
    if not mon:
        raise ValueError(f"Unparseable FR date: '{s}'")
    mon_int = int(mon)

    # If explicit year provided in statement → trust it
    if m.group(3):
        yr = int(m.group(3))
    else:
        # Apply rollover rule
        yr = _adjust_year_for_statement(mon_int, default_year, statement_month)

    return f"{yr:04d}-{mon}-{day:02d}"


# ─────────────────────────────────────────────
def _extract_parent_balance_from_text(text: str) -> tuple[float | None, str | None]:
    def _strip_accents(s: str) -> str:
        import unicodedata
        return "".join(
            c for c in unicodedata.normalize("NFD", s)
            if unicodedata.category(c) != "Mn"
        )

    def _parse_fr_amount_to_float(s: str | None) -> float | None:
        if not s:
            return None
        t = str(s).replace("\xa0", " ")
        t = re.sub(r"\s+", "", t)
        t = t.replace(",", ".").replace("$", "")
        try:
            return float(t)
        except Exception:
            return None

    norm = _strip_accents(text.lower())
    m = re.search(r"solde\s+d[uû]\s+([0-9][0-9\s\u00A0,\.]+)\s*\$?", norm, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"solde\s+total\s+([0-9][0-9\s\u00A0,\.]+)\s*\$?", norm, flags=re.IGNORECASE)
    if not m:
        m2 = re.search(r"solde\s+d[uû]\s*\n\s*([0-9][0-9\s\u00A0,\.]+)\s*\$?", text, flags=re.IGNORECASE)
        if not m2:
            m2 = re.search(r"solde\s+total\s*\n\s*([0-9][0-9\s\u00A0,\.]+)\s*\$?", text, flags=re.IGNORECASE)
        m = m2

    balance = _parse_fr_amount_to_float(m.group(1)) if m else None
    as_of_date = None
    mdate = re.search(
        r"date\s+du\s+releve\s*(\d{1,2})\s+([A-Za-zéûôîïàèêç\.]+)\s+(\d{4})",
        _strip_accents(text),
        re.IGNORECASE
    )

    if mdate:
        day_raw = mdate.group(1)
        month_raw = mdate.group(2)
        year_raw = mdate.group(3)

        mon_key = month_raw.lower().rstrip(".")
        stmt_month = int(_FR_MONTHS.get(mon_key, "01"))

        as_of_date = _fr_day_month_to_iso(
            f"{day_raw} {month_raw} {year_raw}",
            int(year_raw),
            stmt_month
        )
    else:
        _log("[WARN] Statement date not found. Dumping header excerpt:")
        _log(text[:600])

    return balance, as_of_date

# ─────────────────────────────────────────────
class BmoMastercardFrImporter(TemplateImporter):
    key = "credit.bmo_mastercard_fr"
    label = "BMO Mastercard (FR) PDF"
    account_side = "liability"
    account_class = "credit_card"
    input_kind = "pdf"
    signature_name = "BMO_MASTERCARD_FRENCH"
    institution_default = "BMO"

    _TEXT_SIGS = [
        "carte mastercard bmo",
        "bmo banque de montréal",
        "résumé de votre compte",
        "no de carte",
    ]
    _REGEX_SIGS = [
        r"xxxx\s+xxxx\s+xxxx\s+\d{4}",
        r"\d{1,2}\s+\w+\s+\d{4}",
        r"\d+(?:\s\d{3})*,\d{2}\s*\$",
    ]

    # -----------------------------------------------------------
    def detect(self, path: Path) -> bool:
        try:
            with pdfplumber.open(path) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception:
            return False
        tl = text.lower()
        if not any(sig in tl for sig in self._TEXT_SIGS[:2]):
            return False
        if not any(re.search(rx, text, re.IGNORECASE) for rx in self._REGEX_SIGS):
            return False
        return True

    # -----------------------------------------------------------
    def _validate(self, txs: List[Transaction], subtotal_val: float) -> StatementSummary:
        non_transfer = [t for t in txs if t["transaction_type"] != "transfer"]

        total_debit = round(sum(t["debit"] for t in non_transfer), 2)
        total_credit = round(sum(t["credit"] for t in non_transfer), 2)

        # Canonical net delta
        net_amount = round(sum(t["amount"] for t in non_transfer), 2)

        validation_passed = abs(net_amount - subtotal_val) < 1.0

        return {
            "total_transactions": len(txs),
            "total_debits": total_debit,
            "total_credits": total_credit,
            "net_amount": net_amount,
            "statement_subtotal": round(subtotal_val, 2),
            "validation_passed": validation_passed,
            "validation_message": (
                "Validation passed: sum(amount) ≈ statement subtotal."
                if validation_passed
                else "Validation failed: sum(amount) ≠ statement subtotal."
            ),
        }

    # -----------------------------------------------------------
    def parse_to_json(self, path: Path) -> Dict[str, Any]:
        text = self._extract_text(path)

        stmt_year = self._find_year(text)
        # --- Extract parent balance + statement date (used to determine stmt_month) ---
        combined_balance, as_of_date = _extract_parent_balance_from_text(text)

        if not as_of_date:
            raise ValueError(
                f"Could not determine statement date (as_of_date is None) for file {path}"
            )

        try:
            stmt_month = int(as_of_date.split("-")[1])
        except Exception as e:
            raise ValueError(
                f"Failed to parse statement month from as_of_date='{as_of_date}' "
                f"for file {path}"
            ) from e

        cardholders = self._find_cardholders(text)
        lines = text.split("\n")

        _log(f"Detected {len(cardholders)} cardholder(s) for year {stmt_year}")

        statements = []
        for idx, ch in enumerate(cardholders):
            _log(f"Processing cardholder: {ch['name']} ({ch['card_digits']})")

            start_ln = ch["line_number"]

            # End boundary = next cardholder header or EOF
            if idx + 1 < len(cardholders):
                next_ch_ln = cardholders[idx + 1]["line_number"]
            else:
                next_ch_ln = len(lines)

            end_ln, subtotal = self._subtotal_line_for(
                text,
                ch["name"],
                start=start_ln,
                end=next_ch_ln,
            )

            if start_ln >= end_ln:
                _log(
                    f"[WARN] Empty transaction range for {ch['name']} "
                    f"({ch['card_digits']}): start={start_ln}, end={end_ln}"
                )

            if end_ln is None:
                end_ln = next_ch_ln

            _log(f"  Subtotal line: {subtotal} (line {end_ln})")
            _log(f"  Extracting transactions from line {start_ln} to {end_ln}...")

            raw_txs = self._extract_transactions_for(
                text,
                start_ln,
                end_ln,
                stmt_year,
                stmt_month,
            )
            _log(f"  Extracted {len(raw_txs)} raw transactions")

            transactions: List[Transaction] = []
            for t in raw_txs:
                desc_norm = t["description"].lower()
                if "trsf" in desc_norm or "paiement" in desc_norm:
                    tx_type = "transfer"
                elif t["is_credit"] and any(word in desc_norm for word in ["refund", "remboursement"]):
                    tx_type = "refund"
                elif t["is_credit"]:
                    tx_type = "refund"
                else:
                    tx_type = "expense"
                t["transaction_type"] = tx_type
                transactions.append(t)

            total_debit = sum(t["debit"] for t in transactions)
            total_credit = sum(t["credit"] for t in transactions)
            _log(f"  Totals: debit={total_debit:.2f}, credit={total_credit:.2f}, net={total_debit - total_credit:.2f}")

            subtotal_val = self._parse_amount((subtotal or '0,00')).get('amount', 0.0)
            _log(f"  Statement subtotal value parsed: {subtotal_val:.2f}")

            summary = self._validate(transactions, subtotal_val)
            summary["statement_date"] = as_of_date
            _log(f"  Validation passed: {summary['validation_passed']} (net={summary['net_amount']}, subtotal={summary['statement_subtotal']})")

            statements.append({
                "cardholder_info": {
                    "name": ch["name"],
                    "card_number": ch["card_number"],
                    "card_digits": ch["card_digits"],
                },
                "summary": summary,
                "transactions": transactions,
            })

        combined_balance, as_of_date = _extract_parent_balance_from_text(text)
        _log(f"Parent combined balance: {combined_balance}, as of {as_of_date}")

        account_summary = {}
        if combined_balance is not None:
            account_summary = {
                "account_id": "bmo_credit_combined",
                "account_name": "BMO MasterCard (Combined)",
                "base_currency": "CAD",  # Manual CAD currency. MC Statement do not inform currency
                "account_side": self.account_side,
                "account_class": self.account_class,
                "institution": self.institution_default,
                "current_balance": {
                    "date": as_of_date,
                    "amount": -abs(combined_balance),
                },
                "notes": "combined statement balance for all cardholders (Solde dû / Solde total)",
            }

        for st in statements:
            st.setdefault("cardholder_info", {})["parent_account_id"] = "bmo_credit_combined"

        output = {
            "export_date": datetime.now(timezone.utc).date().isoformat(),
            "document_signature": {"document_type": self.signature_name},
            "validation_passed": summary["validation_passed"],
            "validation_message": summary["validation_message"],
            "account_summary": account_summary,
            "total_cardholders": len(statements),
            "statements": statements,
        }

        errors = sorted(_VALIDATOR.iter_errors(output), key=lambda e: e.path)
        if errors:
            raise ValueError("Schema validation failed: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors))

        _log(f"✅ Finished parsing {len(statements)} statements.")
        return output

    # -----------------------------------------------------------
    def _extract_text(self, path: Path) -> str:
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)

    def _find_year(self, text: str) -> int:
        """
        Extract the statement year.
        Priority 1: from 'Date du relevé ... <day> <month> <year>'
        Fallback: largest 20xx found in text.
        """
        # Try explicit "Date du relevé" first
        m = re.search(
            r"date\s+du\s+relev[ée]\s*(\d{1,2}\s+[A-Za-zéûùôîïëêàâç\.]+\s+\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            try:
                date_str = m.group(1)
                # parse year using corrected ISO function
                mm = re.match(r"(\d{1,2})\s+([A-Za-zéûùôîïëêàâç\.]+)\s+(\d{4})", date_str)
                mon_raw = mm.group(2)
                mon_key = mon_raw.lower().rstrip(".")
                stmt_month = int(_FR_MONTHS.get(mon_key, "01"))

                iso = _fr_day_month_to_iso(date_str, datetime.now().year, stmt_month)
                return int(iso[:4])
            except Exception:
                pass

        # Fallback: any 20xx in text, but use *median* not max
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
        if years:
            # discard outliers like 2099 by picking median value
            years.sort()
            return years[len(years) // 2]

        # Default: current year
        return datetime.now().year

    def _parse_amount(self, s: str) -> dict:
        s = s.strip()
        is_credit = s.endswith(" CR")
        if is_credit:
            s = s[:-3].strip()

        s = re.sub(r"(\d)\s+(\d)", r"\1\2", s).replace(",", ".")

        try:
            val = float(s)

            if is_credit:
                amount = +val
                debit = 0.0
                credit = val
            else:
                amount = -val
                debit = val
                credit = 0.0

            return {
                "amount": amount,
                "is_credit": is_credit,
                "debit": debit,
                "credit": credit,
            }
        except Exception:
            return {"amount": 0.0, "is_credit": False, "debit": 0.0, "credit": 0.0}

    def _find_cardholders(self, text: str):
        out = []
        for ln, line in enumerate(text.split("\n")):
            if "no de carte" in line.lower():
                m = re.search(
                    r"No de carte\s*:?\s*(XXXX\s+XXXX\s+XXXX\s+(\d{4}))\s+([A-Z][A-Z\s]+)",
                    line, re.IGNORECASE,
                )
                if m:
                    digits = m.group(2)
                    name = re.sub(r"\(suite.*", "", m.group(3)).strip()
                    if not any(ch["card_digits"] == digits for ch in out):
                        out.append({
                            "line_number": ln,
                            "card_number": m.group(1),
                            "card_digits": digits,
                            "name": name,
                        })
        return out

    def _subtotal_line_for(self, text: str, name: str, start: int, end: int):
        lines = text.split("\n")
        for ln in range(start, min(end, len(lines))):
            line = lines[ln]
            if "sous-total pour" in line.lower() and name.upper() in line.upper():
                m = re.search(r"(\d+(?:[\s,]\d{3})*,\d{2})", line)
                if m:
                    return ln, m.group(1)
        return None, None

    def _repair_compact_adjacent_dates(self, line: str) -> str:
        """
        Repair rows where the first date token is glued to the second day:
        '10 mars11 mars ...' -> '10 mars 11 mars ...'
        """
        pat = rf"^(\d{{1,2}}\s+(?:{_FR_MONTH_PATTERN}))(\d{{1,2}}\s+(?:{_FR_MONTH_PATTERN})\b)"
        return re.sub(pat, r"\1 \2", line, flags=re.IGNORECASE)

    def _disambiguate_amount_token(self, desc: str, amount_token: str) -> str:
        """
        On FX rows, PDF extraction can leak a 3-digit merchant/store number into
        the grouped amount token (e.g. '743 203,42'). Prefer the amount candidate
        closest to FX-derived CAD when this ambiguity appears.
        """
        if " " not in amount_token:
            return amount_token
        fx = _FX_HINT_RE.search(desc)
        if not fx:
            return amount_token
        try:
            foreign = float(fx.group(1))
            rate = float(fx.group(2))
            expected = foreign * rate
        except Exception:
            return amount_token

        whole = amount_token.strip()
        cents = whole[-2:]
        # last 3 digits before comma + cents, preserving optional CR suffix
        m = re.match(r"^(.*?)(\d{1,3},\d{2})(\s+CR)?$", whole)
        if not m:
            return amount_token
        short = f"{m.group(2)}{m.group(3) or ''}"

        def _as_num(tok: str) -> float:
            t = tok.replace(" CR", "").strip()
            t = re.sub(r"(\d)\s+(\d)", r"\1\2", t).replace(",", ".")
            return float(t)

        try:
            whole_val = _as_num(whole)
            short_val = _as_num(short)
        except Exception:
            return amount_token

        if math.fabs(short_val - expected) < math.fabs(whole_val - expected):
            return short
        return amount_token

    # -----------------------------------------------------------
    def _extract_transactions_for(self, text: str, start: int, end: int, default_year: int, statement_month: int):
        lines = text.split("\n")
        txs: List[Dict[str, Any]] = []

        skip = [
            r"DATE\s+DE\s+L",
            r"DESCRIP",
            r"MONTANT",
            r"Page\s+\d+",
            r"suite à la page",
            r"Transactions depuis",
        ]

        patts = [
            r"^(\d{1,2}\s+[A-Za-zéûùôîïëêàâç\.]+)\s+(\d{1,2}\s+[A-Za-zéûùôîïëêàâç\.]+)\s+(.+?)\s+(\d+(?:[\s,]\d{3})*,\d{2}(?:\s+CR)?)$",
            r"^(\d{1,2}\s+[A-Za-zéûùôîïëêàâç\.]+)\s+(\d{1,2}\s+[A-Za-zéûùôîïëêàâç\.]+)\s*(.+?)\s+(\d+(?:[\s,]\d{3})*,\d{2}(?:\s+CR)?)$",
        ]

        def _maybe_merge_with_next(line: str, next_line: str | None) -> str:
            """Try to repair a broken line missing month tokens, e.g. '25 26 ...' + 'mars mars'."""
            if not next_line:
                return line
            # detect pattern: 2 numbers then rest
            m = re.match(r"^(\d{1,2})\s+(\d{1,2})\s+(.+)$", line)
            if m and re.search(r"[A-Za-zéûùôîïëêàâç\.]+", next_line):
                parts = next_line.split()
                if len(parts) >= 2 and parts[0].rstrip(".").lower() in _FR_MONTHS and parts[1].rstrip(".").lower() in _FR_MONTHS:
                    d1, d2, rest = m.groups()
                    cont = " ".join(parts[2:]).strip()
                    amt = _AMOUNT_TAIL_RE.match(rest)
                    if amt and cont:
                        merged_rest = f"{amt.group(1)} {cont} {amt.group(2)}"
                    else:
                        merged_rest = rest
                    merged = f"{d1} {parts[0]} {d2} {parts[1]} {merged_rest}"
                    _log(f"    🔗 Fixed split-date pattern: {merged}")
                    return merged
            # fallback: naive concat
            merged = f"{line.strip()} {next_line.strip()}"
            _log(f"    🔗 Merged with next line for retry: {merged}")
            return merged

        i = start + 1
        while i < min(end, len(lines)):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            line = self._repair_compact_adjacent_dates(line)

            # skip headers or known non-data
            if any(re.search(p, line, re.IGNORECASE) for p in skip):
                i += 1
                continue

            matched = False

            # First attempt normal parse
            for p in patts:
                m = re.match(p, line)
                if m:
                    matched = True
                    op_iso = _fr_day_month_to_iso(m.group(1).strip(), default_year, statement_month)
                    desc = re.sub(r"^[⚬☆\s]+|[⚬☆\s]+$", "", m.group(3)).strip()
                    amount_tok = self._disambiguate_amount_token(desc, m.group(4).strip())
                    a = self._parse_amount(amount_tok)
                    txs.append({
                        "operation_date": op_iso,
                        "description": desc,
                        **a,
                    })
                    _log(f"    Parsed line {i}: {desc} → {a['amount']} ({'CR' if a['is_credit'] else 'DR'})")
                    break

            # If not matched, apply your heuristic
            if not matched and re.search(r"(\d{1,2}(?:[\s,]\d{3})*,\d{2}|CR)$", line):
                if re.match(r"^\d+", line):  # starts with number, might be missing date parts
                    next_line = lines[i + 1].strip() if i + 1 < len(lines) else None
                    merged = _maybe_merge_with_next(line, next_line)
                    merged = self._repair_compact_adjacent_dates(merged)
                    for p in patts:
                        m = re.match(p, merged)
                        if m:
                            matched = True
                            op_iso = _fr_day_month_to_iso(m.group(1).strip(), default_year, statement_month)
                            desc = re.sub(r"^[⚬☆\s]+|[⚬☆\s]+$", "", m.group(3)).strip()
                            amount_tok = self._disambiguate_amount_token(desc, m.group(4).strip())
                            a = self._parse_amount(amount_tok)
                            txs.append({
                                "operation_date": op_iso,
                                "description": desc,
                                **a,
                            })
                            _log(
                                f"    ✅ Fixed by merge (lines {i}-{i + 1}): {desc} → {a['amount']} ({'CR' if a['is_credit'] else 'DR'})")
                            i += 1  # consume next line
                            break
                    if not matched:
                        _log(f"    ⚠️ Warning: likely transaction but unparsable (ends with amount or CR): {line}")

            if not matched:
                _log(f"    Skipped non-date line {i}: {line}")

            i += 1

        return txs
