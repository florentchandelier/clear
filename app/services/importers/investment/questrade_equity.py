#app/services/importers/investment/questrade_equity.py

from __future__ import annotations
import re
import json
import pdfplumber
from datetime import datetime
from pathlib import Path
import jsonschema

import logging
log = logging.getLogger(__name__)

from ..base import Importer

# Load schema for runtime validation (optional consistency)
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)

# ─────────────────────────────────────────────────────────────
# Normalize Questrade Type → clean account name
# ─────────────────────────────────────────────────────────────

def _normalize_type_name(raw: str) -> str:
    if not raw:
        return ""

    t = raw.strip()

    # Detect known Canadian registered accounts
    if "Tax-Free Savings Account" in t or "(TFSA)" in t:
        return "TFSA/Cash"

    if "Registered Retirement Savings Plan" in t or "(RRSP)" in t:
        return "RRSP/Cash"

    # General pattern: "Individual Something"
    m = re.match(r"(Individual|Corporate)\s+(.+)", t)
    if m:
        prefix, rest = m.groups()
        return f"{prefix}/{rest.strip()}"

    # Default: return as-is
    return t

class QuestradeEquityImporter(Importer):
    """
    Importer for Questrade investment statements (NAV-only).
    Extracts account ID, account name/type, institution, currency,
    statement date, and current month balance (NAV).
    """

    # ─────────────────────────────────────────────────────────────
    # Registry metadata
    # ─────────────────────────────────────────────────────────────
    key = "investment.questrade_equity"
    label = "Questrade Equity (NAV)"
    account_side = "asset"
    account_class = "investment"
    input_kind = "pdf"
    formats = ["pdf"]
    signature_name = "QUESTRADE_NAV"
    institution_default = "Questrade"

    # ─────────────────────────────────────────────────────────────
    # Detection
    # ─────────────────────────────────────────────────────────────
    def detect(self, path: Path) -> bool:
        """Quick check to verify this is a Questrade equity statement."""
        try:
            with pdfplumber.open(path) as pdf:
                first_page = pdf.pages[0].extract_text() or ""
            return "Questrade" in first_page and "Account #" in first_page
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # Core parser
    # ─────────────────────────────────────────────────────────────
    def parse_to_json(self, path: Path) -> dict:
        """
        Parse a Questrade NAV-only investment statement.

        Authoritative rules:
        - NAV date comes from "Current month"
        - NAV value comes from "Current month balance"
        - base_currency is extracted from
          "Balance Changes Combined in (XXX)" on the SAME page
          where "Closing balance" appears
        """

        # ─────────────────────────────────────────────────────────────
        # Read PDF
        # ─────────────────────────────────────────────────────────────
        with pdfplumber.open(path) as pdf:
            pages = [(page, page.extract_text() or "") for page in pdf.pages]

        full_text = "\n".join(t for _, t in pages)
        log.debug(
            "from pdfplumber -> full_text (all pages):\n%s",
            full_text
        )

        # ─────────────────────────────────────────────────────────────
        # Signature / authenticity
        # ─────────────────────────────────────────────────────────────
        signature_ok = bool(
            re.search(r"(Dealer:\s*Questrade, Inc\.|©Questrade, Inc\.)", full_text)
        )

        # ─────────────────────────────────────────────────────────────
        # Helpers
        # ─────────────────────────────────────────────────────────────
        def _search(pattern, text, group=1, default=None):
            m = re.search(pattern, text)
            return m.group(group).strip() if m else default

        # ─────────────────────────────────────────────────────────────
        # Extract global fields
        # ─────────────────────────────────────────────────────────────
        account_id = _search(r"Account #:\s*(\d+)", full_text)
        account_name_raw = _search(
            r"Type:\s*(.+?)(?:Account opened|Currency:)",
            full_text
        )
        current_month = _search(
            r"Current month:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
            full_text
        )
        current_balance = _search(
            r"Current month balance:\s*\$?([\d,]+\.\d{2})",
            full_text
        )
        closing_balance = _search(
            r"Closing balance\s+([\d,]+\.\d{2})",
            full_text
        )

        normalized_account_name = _normalize_type_name(account_name_raw)

        # ─────────────────────────────────────────────────────────────
        # Locate page containing Closing balance
        # ─────────────────────────────────────────────────────────────
        closing_page_text = None
        for _, page_text in pages:
            if re.search(r"Closing balance\s+[\d,]+\.\d{2}", page_text):
                closing_page_text = page_text
                break

        if not closing_page_text:
            raise ValueError("Could not locate page containing Closing balance")

        # ─────────────────────────────────────────────────────────────
        # Extract authoritative base_currency from SAME page
        # ─────────────────────────────────────────────────────────────
        m = re.search(
            r"Balance\s+Changes\s+Combined\s+in\s*\(\s*([A-Z]{3})\s*\)\s*[\u00B9¹]?",
            closing_page_text,
            flags=re.IGNORECASE
        )
        if not m:
            raise ValueError(
                "Could not extract base_currency from "
                "'Balance Changes Combined in (XXX)'"
            )

        base_currency = m.group(1)

        # Final validation: enforce ISO currency
        if not re.fullmatch(r"[A-Z]{3}", base_currency):
            raise ValueError(f"Invalid base_currency extracted: {base_currency}")

        # ─────────────────────────────────────────────────────────────
        # Validation logic
        # ─────────────────────────────────────────────────────────────
        validation_msgs = []
        value_ok = True

        if not signature_ok:
            validation_msgs.append("Signature not found.")
        else:
            validation_msgs.append("Dealer signature validated.")

        if current_balance and closing_balance:
            val1 = float(current_balance.replace(",", ""))
            val2 = float(closing_balance.replace(",", ""))
            if abs(val1 - val2) > 0.01:
                value_ok = False
                validation_msgs.append(
                    f"Balance mismatch: Current={val1:.2f}, Closing={val2:.2f}"
                )
            else:
                validation_msgs.append("Balance cross-check passed.")
        else:
            validation_msgs.append("Balance cross-check unavailable.")

        validation_passed = signature_ok and value_ok

        # ─────────────────────────────────────────────────────────────
        # NAV snapshot
        # ─────────────────────────────────────────────────────────────
        try:
            snapshot_date = datetime.strptime(
                current_month, "%B %d, %Y"
            ).strftime("%Y-%m-%d")
        except Exception:
            raise ValueError(f"Could not parse NAV snapshot date: {current_month!r}")

        try:
            nav_value = float(current_balance.replace(",", ""))
        except Exception:
            raise ValueError("Could not parse NAV value")

        if not all([account_id, snapshot_date, nav_value]):
            raise ValueError("Missing required NAV fields")

        # ─────────────────────────────────────────────────────────────
        # Final JSON output (deterministic, NAV-only)
        # ─────────────────────────────────────────────────────────────
        output = {
            "export_date": snapshot_date,
            "document_signature": {
                "document_type": self.signature_name,
                "validation_passed": validation_passed,
                "validation_message": "; ".join(validation_msgs),
            },
            "account_summary": {
                "account_id": account_id,
                "account_name": normalized_account_name,
                "institution": self.institution_default,
                "account_side": "asset",
                "account_class": "investment",
                "base_currency": base_currency,
            },
            "nav_snapshots": [
                {
                    "date": snapshot_date,
                    "nav": nav_value,
                }
            ],
            "validation_passed": validation_passed,
            "validation_message": "; ".join(validation_msgs),
        }

        # ─────────────────────────────────────────────────────────────
        # Schema validation
        # ─────────────────────────────────────────────────────────────
        errors = sorted(_VALIDATOR.iter_errors(output), key=lambda e: e.path)
        if errors:
            raise ValueError(
                "Schema validation failed: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
            )

        return output

