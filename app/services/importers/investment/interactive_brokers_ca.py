# app/services/importers/investment/interactive_brokers_ca.py
from __future__ import annotations

import re
import json
import camelot
import logging
from datetime import datetime, timezone
from pathlib import Path
import jsonschema
import pdfplumber

from ..base import Importer

LOG = logging.getLogger(__name__)
#logging.getLogger("app.services.importers.investment.interactive_brokers_ca").setLevel(logging.DEBUG)
#LOG.setLevel(logging.DEBUG)

# Schema loading
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)


class InteractiveBrokersCAImporter(Importer):
    """
    Importer for Interactive Brokers Canada investment statements (NAV-only).
    Extracts account ID, account name/type, institution, currency, statement date, and NAV value.
    """

    key = "investment.interactive_brokers_ca"
    label = "Interactive Brokers Canada (NAV)"
    account_side = "asset"
    account_class = "investment"
    input_kind = "pdf"
    formats = ["pdf"]
    signature_name = "INTERACTIVE_BROKERS_CA_NAV"
    institution_default = "Interactive Brokers Canada Inc."

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _search(pattern: str, text: str, group: int = 1, default=None):
        """Safe regex search with logging."""
        m = re.search(pattern, text)
        if not m:
            LOG.debug("Pattern not found: %s", pattern)
            return default
        try:
            value = m.group(group).strip()
            LOG.debug("Pattern '%s' matched → %s", pattern, value)
            return value
        except Exception:
            fallback = m.group(0).strip()
            LOG.debug("Pattern '%s' using fallback → %s", pattern, fallback)
            return fallback

    @staticmethod
    def _detect_nav_table(df):
        """
        Detects if a DataFrame is likely to be the IBKR NAV table.
        This variant avoids capturing-group warnings.
        """
        NAV_MARKERS = r"(?:Ending Value|Starting Value|Change in NAV)"
        return df.apply(
            lambda col: col.astype(str).str.contains(NAV_MARKERS, case=False, na=False)
        ).any().any()

    @staticmethod
    def _extract_latest_date(df):
        """Extracts all date-like strings and returns the max."""
        all_dates = df.apply(
            lambda col: col.astype(str).str.extract(r"([A-Za-z]+\s+\d{1,2},\s*\d{4})")[0],
            axis=0
        ).stack().dropna().tolist()

        LOG.debug("Extracted dates: %s", all_dates)
        if not all_dates:
            return None

        try:
            parsed = datetime.strptime(all_dates[-1], "%B %d, %Y").strftime("%Y-%m-%d")
            return parsed
        except Exception as e:
            LOG.warning("Date parsing failed for %s: %s", all_dates[-1], e)
            return None

    @staticmethod
    def _extract_ending_value(df):
        """Finds 'Ending Value' and returns the numeric value in the next cell."""
        for i in range(len(df)):
            for j in range(len(df.columns)):
                if "Ending Value" in str(df.iat[i, j]):
                    if j + 1 < len(df.columns):
                        raw = str(df.iat[i, j + 1]).replace(",", "")
                        LOG.debug("Found Ending Value raw: %s", raw)
                        try:
                            return float(raw)
                        except ValueError:
                            LOG.warning("Failed to parse Ending Value '%s'", raw)
        return None

    # ─────────────────────────────────────────────────────────────
    # Detection
    # ─────────────────────────────────────────────────────────────
    def detect(self, path: Path) -> bool:
        try:
            with pdfplumber.open(path) as pdf:
                first_page = pdf.pages[0].extract_text() or ""
            return (
                "Interactive Brokers Canada Inc." in first_page
                and "Activity Statement" in first_page
            )
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # Core parsing
    # ─────────────────────────────────────────────────────────────
    def parse_to_json(self, path: Path) -> dict:
        LOG.info("IBKR importer: starting parse for %s", path.name)

        # Extract metadata text
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        LOG.debug("Extracted text (first 500): %s", text[:500])

        signature_ok = "Interactive Brokers Canada Inc." in text
        LOG.info("Signature check: %s", signature_ok)

        # Metadata
        account_id = self._search(r"Account\s+([A-Z]\d+)", text)
        account_type = self._search(r"Account Type\s+([A-Za-z]+)", text, default="")
        account_cap = self._search(r"Account Capabilities\s+([A-Za-z]+)", text, default="")
        base_currency = self._search(r"Base Currency\s+([A-Z]{3})", text, default="USD")
        customer_type = self._search(r"Customer Type\s*([^\n\r]+)", text, default=None)

        # Normalize Customer Type → short codes
        customer_type_map = {
            "Registered Retirement Savings Plan": "RRSP",
            "Tax-Free Savings Account": "TFSA",
        }

        # Prefer Customer Type (e.g., Registered Retirement Savings Plan)
        if customer_type:
            account_name_core = customer_type_map.get(customer_type, customer_type)
        else:
            # fallback to Account Type
            account_name_core = account_type or ""

        account_name = f"{account_name_core}/{account_cap}".strip("/")

        LOG.info("Parsed metadata: account_id=%s, customer_type=%s, account_name=%s, currency=%s",
                 account_id, customer_type, account_name, base_currency)

        # Camelot extraction
        LOG.info("Running Camelot on %s ...", path)
        tables = camelot.read_pdf(str(path), pages="1", flavor="stream")
        LOG.info("Camelot extracted %d tables", len(tables))

        ending_value = None
        latest_date = None

        for idx, t in enumerate(tables):
            df = t.df
            LOG.info("Table %s shape = %s", idx, df.shape)
            LOG.debug("Table %s content:\n%s", idx, df.to_string())

            if df.empty:
                continue
            if not self._detect_nav_table(df):
                continue

            # Extract NAV date & ending value
            latest_date = self._extract_latest_date(df) or latest_date
            val = self._extract_ending_value(df)
            if val is not None:
                ending_value = val
                LOG.info("Ending Value found: %s", ending_value)
                break

        if not ending_value or not latest_date:
            LOG.error("NAV extraction failed: ending_value=%s latest_date=%s",
                      ending_value, latest_date)
            raise ValueError("Could not extract NAV table using Camelot.")

        LOG.info("NAV extraction success: nav=%s date=%s", ending_value, latest_date)

        # Validation messages
        validation_msgs = []
        validation_msgs.append(
            "Validated Interactive Brokers Canada statement."
            if signature_ok else "Signature not confirmed."
        )
        validation_msgs.append(f"NAV found: {ending_value:,.2f} {base_currency}")
        validation_passed = signature_ok and bool(ending_value)

        # Final JSON
        output = {
            "export_date": datetime.now(timezone.utc).date().isoformat(),
            "document_signature": {
                "document_type": self.signature_name,
                "validation_passed": validation_passed,
                "validation_message": "; ".join(validation_msgs),
            },
            "account_summary": {
                "account_id": account_id,
                "account_name": account_name,
                "institution": self.institution_default,
                "account_side": "asset",
                "account_class": "investment",
                "base_currency": base_currency,
            },
            "nav_snapshots": [{"date": latest_date, "nav": ending_value}],
            "validation_passed": validation_passed,
            "validation_message": "; ".join(validation_msgs),
        }

        # Schema validation
        errors = sorted(_VALIDATOR.iter_errors(output), key=lambda e: e.path)
        if errors:
            raise ValueError(
                "Schema validation failed: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
            )

        return output
