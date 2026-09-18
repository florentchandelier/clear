# app/services/importers/investment/bmo_nesbitt_ca.py

from __future__ import annotations
import re
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import jsonschema

from ..base import Importer

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
LOG = logging.getLogger(__name__)
#logging.getLogger("app.services.importers.investment.bmo_nesbitt_ca").setLevel(logging.DEBUG)
#LOG.setLevel(logging.DEBUG)

# ─────────────────────────────────────────────────────────────
# Load schema
# ─────────────────────────────────────────────────────────────
from pathlib import Path as _P

_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _parse_number(raw: str) -> float | None:
    """
    Parse both FR and EN formatted numbers reliably:

      FR: 226 776,04       → 226776.04
      EN: 162,009.06       → 162009.06

    Handles thousands separators, line breaks, and stray spaces.
    """
    if not raw:
        return None

    s = raw.strip().replace("\xa0", "")
    s = s.replace(" ", "").replace("\n", "").replace("\r", "")

    # English-style: thousand comma + decimal dot
    if "," in s and "." in s:
        return float(s.replace(",", ""))

    # French-style: decimal comma
    if "," in s and "." not in s:
        return float(s.replace(",", "."))

    # Plain
    return float(s)


_FR_MONTHS = {
    "janv": "01", "janvier": "01",
    "févr": "02", "fevr": "02", "février": "02",
    "mars": "03",
    "avr": "04", "avril": "04",
    "mai": "05",
    "juin": "06",
    "juil": "07", "juillet": "07",
    "août": "08", "aout": "08",
    "sep": "09", "sept": "09", "septembre": "09",
    "oct": "10", "octobre": "10",
    "nov": "11", "novembre": "11",
    "déc": "12", "dec": "12", "décembre": "12",
}


def _fr_date_to_iso(raw: str) -> str | None:
    m = re.search(
        r"(\d{1,2})\s*([A-Za-zéûùôîïëêàâç\.]+)\s*(\d{4})",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None

    day, month_raw, year = m.groups()
    month_key = month_raw.lower().replace(".", "")
    month = _FR_MONTHS.get(month_key)
    if not month:
        return None

    return f"{year}-{month}-{day.zfill(2)}"


# ─────────────────────────────────────────────────────────────
# Importer
# ─────────────────────────────────────────────────────────────

class BmoNesbittCAImporter(Importer):
    """
    BMO Nesbitt Burns investment NAV importer (French + English).
    Handles:
      FR  → CompteREER#331-15617-10
            30avril2025
            Totalglobal($CAD) 226776,04

      EN  → RRSPaccount#331-16008-15
            August31,2025
            GrandTotal(CAD$) 162,009.06
    """

    key = "investment.bmo_nesbitt_ca"
    label = "BMO Nesbitt Equity (NAV)"
    account_side = "asset"
    account_class = "investment"
    input_kind = "pdf"
    formats = ["pdf"]
    signature_name = "BMONESBITT_NAV"
    institution_default = "BMO Nesbitt"

    # ----------------------------------------------------------
    # Detect
    # ----------------------------------------------------------
    def detect(self, path: Path) -> bool:
        try:
            with pdfplumber.open(path) as pdf:
                text = pdf.pages[0].extract_text() or ""

            norm = text.replace(" ", "").replace("\xa0", "").lower()
            found = "bmonesbittburns.com" in norm

            LOG.debug("DETECT normalized preview:\n" + norm[:500])
            LOG.debug(f"DETECT: signature found = {found}")
            return found

        except Exception as e:
            LOG.debug(f"Detect error: {e}")
            return False

    # ----------------------------------------------------------
    # Parse
    # ----------------------------------------------------------
    def parse_to_json(self, path: Path) -> dict:
        LOG.info("Parsing BMO Nesbitt Burns (NAV) statement…")

        with pdfplumber.open(path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""

        LOG.debug("–––––––– FIRST PAGE TEXT DUMP ––––––––")
        LOG.debug(first_page_text)
        LOG.debug("–––––––– END FIRST PAGE ––––––––")

        # Normalize for matching
        norm = first_page_text.replace(" ", "").replace("\xa0", "")
        norm_lower = norm.lower()

        # ------------------------------------------------------
        # Detect language
        # ------------------------------------------------------
        is_french = ("Relevé" in first_page_text) or ("Releve" in first_page_text)
        is_english = ("Report" in first_page_text)

        LOG.debug(f"Language mode detected: {'FR' if is_french else 'EN'}")

        # ------------------------------------------------------
        # 1) Account name + ID
        # ------------------------------------------------------
        # Capture both FR and EN:
        #   CompteREER#...
        #   RRSPaccount#...
        m_acc = (
                re.search(r"(compte|account)\s*([A-Za-zÉé]+)\s*#?\s*([\d\-]+)",
                          first_page_text, flags=re.IGNORECASE)
                or
                re.search(r"(rrspaccount|comptereer)#?([\d\-]+)", norm_lower)
        )

        if not m_acc:
            LOG.error("ACCOUNT PARSE FAILED")
            LOG.debug("ACCOUNT DEBUG norm_lower:\n" + norm_lower[:800])
            raise ValueError("Could not locate account name + ID.")

        if len(m_acc.groups()) == 3:
            _, acct_name_raw, account_id = m_acc.groups()
        else:
            acct_name_raw = "RRSP" if "rrsp" in m_acc.group(1) else "REER"
            account_id = m_acc.group(2)

        account_name = acct_name_raw.upper()
        LOG.info(f"Account detected: {account_name} / {account_id}")

        # ------------------------------------------------------
        # 2) Date extraction (English + French safely)
        # ------------------------------------------------------
        lines = first_page_text.splitlines()[:8]
        top_text = " ".join(lines)

        m_date_en = re.search(
            r"\b([A-Za-z]{3,9})\s*(\d{1,2}),\s*(\d{4})\b",
            top_text
        )
        m_date_fr = re.search(
            r"\b(\d{1,2})\s*([A-Za-zéûùôîïëêàâç\.]{3,12})\s*(\d{4})\b",
            top_text,
            flags=re.IGNORECASE
        )

        if m_date_en:
            raw_date = m_date_en.group(0)
            LOG.debug(f"DATE RAW MATCH (EN): {repr(raw_date)}")
            snapshot_date = self._parse_date_dual_language(raw_date)

        elif m_date_fr:
            raw_date = m_date_fr.group(0)
            LOG.debug(f"DATE RAW MATCH (FR): {repr(raw_date)}")
            snapshot_date = self._parse_date_dual_language(raw_date)

        else:
            LOG.error("DATE PARSE FAILED in top section")
            LOG.debug("DATE DEBUG top_text:\n" + top_text)
            raise ValueError("Could not extract statement date.")

        # ------------------------------------------------------
        # 3) NAV extraction (FR & EN)
        # ------------------------------------------------------
        clean_text = first_page_text.replace("\n", " ")

        if is_french:
            nav_pattern = r"Total\s*global\s*\(\$?\s*([A-Z]{3})\)\s*([\d\s,\.]+)"
        else:
            nav_pattern = r"Grand\s*Total\s*\(\s*([A-Z]{3})\$\s*\)\s*([\d\s,\.]+)"

        m_nav = re.search(nav_pattern, clean_text, flags=re.IGNORECASE)

        if not m_nav:
            LOG.error("NAV PARSE FAILED")
            LOG.debug("NAV DEBUG clean_text:\n" + clean_text[:800])
            raise ValueError("Could not locate NAV.")

        base_currency = m_nav.group(1).upper()
        nav_raw = m_nav.group(2)

        nav_value = _parse_number(nav_raw)

        if not re.fullmatch(r"[A-Z]{3}", base_currency):
            raise ValueError(f"Invalid base_currency extracted: {base_currency}")

        LOG.info(f"NAV extracted: {nav_value} {base_currency}")

        # ------------------------------------------------------
        # Build JSON
        # ------------------------------------------------------
        output = {
            "export_date": datetime.now(timezone.utc).date().isoformat(),
            "document_signature": {
                "document_type": self.signature_name,
                "validation_passed": True,
                "validation_message": "Parsed successfully.",
            },
            "account_summary": {
                "account_id": account_id,
                "account_name": account_name,
                "institution": self.institution_default,
                "account_side": "asset",
                "account_class": "investment",
                "base_currency": base_currency,
            },
            "nav_snapshots": [
                {"date": snapshot_date, "nav": nav_value}
            ],
            "validation_passed": True,
            "validation_message": "Parsed successfully.",
        }

        # ------------------------------------------------------
        # Schema validation
        # ------------------------------------------------------
        errors = list(_VALIDATOR.iter_errors(output))
        if errors:
            for e in errors:
                LOG.error(f"SCHEMA ERROR {list(e.path)}: {e.message}")
            raise ValueError("; ".join(f"{list(e.path)}: {e.message}" for e in errors))

        return output

    # ----------------------------------------------------------
    # Date parser for FR + EN
    # ----------------------------------------------------------
    def _parse_date_dual_language(self, raw: str) -> str | None:
        raw = raw.strip()

        # Try French
        fr = _fr_date_to_iso(raw)
        if fr:
            return fr

        # Try English
        m = re.search(r"([A-Za-z]{3,9})\s*(\d{1,2}),\s*(\d{4})", raw)
        if m:
            month_name, day, year = m.groups()
            try:
                dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
            except ValueError:
                dt = datetime.strptime(f"{month_name} {day} {year}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")

        return None
