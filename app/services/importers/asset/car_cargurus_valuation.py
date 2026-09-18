# app/services/importers/asset/car_cargurus_valuation.py

from __future__ import annotations

import re
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
import hashlib

import pdfplumber
import jsonschema

from ..base import Importer

# ─────────────────────────────────────────────────────────────
# Schema validation (same pattern as home_fmv)
# ─────────────────────────────────────────────────────────────
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)


def _norm_text(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s)
    s = s.replace("\u00a0", " ")
    return s


def _numeric_account_id(s: str, digits: int = 12) -> str:
    """
    Deterministic numeric ID as a digit-only STRING.
    Keeps Parquet schema happy (account_id is treated as an identifier string).
    """
    norm = _norm_text(s).lower().encode("utf-8")
    h = hashlib.sha256(norm).hexdigest()
    n = int(h, 16) % (10 ** digits)
    return f"{n:0{digits}d}"  # fixed-width digit string


def _extract_first(pattern: str, text: str, flags=re.IGNORECASE | re.MULTILINE) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _parse_money_to_float(raw: str | None) -> float | None:
    """
    Accepts values like:
      "$16,209" / "16,209" / "161,209.50" / "1 615 900" etc.
    """
    if not raw:
        return None
    s = raw.strip()
    s = s.replace("$", "").replace("CAD", "").strip()
    s = s.replace(" ", "")
    # keep digits, comma, dot, minus
    s = re.sub(r"[^0-9,.\-]", "", s)

    # Heuristic:
    # - If there are both comma and dot, assume comma thousands, dot decimals.
    # - If only comma, assume comma thousands (common in North America for these PDFs).
    if "," in s and "." not in s:
        s = s.replace(",", "")
    return float(s) if s else None


def _try_parse_date_to_ymd(raw: str | None) -> str | None:
    """
    Attempts to parse a date string and return YYYY-MM-DD.
    Supports:
      - YYYY-MM-DD, YYYY/MM/DD
      - MM/DD/YY or MM/DD/YYYY
      - DD/MM/YY or DD/MM/YYYY (ambiguous; we try both)
    """
    if not raw:
        return None
    s = raw.strip()

    # Fast path: yyyy-mm-dd or yyyy/mm/dd
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Try common numeric formats
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass

    return None


class CarGurusVehicleValuationImporter(Importer):
    """
    NAV-only importer for a car fair-market valuation PDF (CarGurus).

    Output:
      - account_summary
      - nav_snapshots (single snapshot)
      - no statements / no transactions
    """

    # ─────────────────────────────────────────────────────────────
    # Registry metadata
    # ─────────────────────────────────────────────────────────────
    key = "asset.car_fmv_cargurus"
    label = "Car FMV (CarGurus Valuation)"
    account_side = "asset"
    account_class = "car_fmv"
    input_kind = "pdf"
    formats = ["pdf"]

    signature_name = "cargurus-vehicle-valuation"
    institution_default = "CarGurus"

    # ─────────────────────────────────────────────────────────────
    # Detection
    # ─────────────────────────────────────────────────────────────
    def detect(self, path: Path) -> bool:
        try:
            with pdfplumber.open(path) as pdf:
                text = _norm_text(pdf.pages[0].extract_text() or "")
            t = text.lower()
            return ("www.cargurus.ca" in t) or ("cargurus" in t and "great deal" in t)
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # Core parser
    # ─────────────────────────────────────────────────────────────
    def parse_to_json(self, path: Path) -> dict:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(_norm_text(page.extract_text() or "") for page in pdf.pages)

        validation_msgs: list[str] = []
        validation_passed = True

        # ─────────────────────────────────────────────────────────
        # Asset identity (Year/Make/Model)
        # ─────────────────────────────────────────────────────────
        year = _extract_first(r"\bYear\s*:\s*(\d{4})\b", text)
        make = _extract_first(r"\bMake\s*:\s*([A-Za-z0-9\- ]+)\b", text)
        model = _extract_first(r"\bModel\s*:\s*([A-Za-z0-9\- ]+)\b", text)

        # Fallback: a single-line title like "2018 Mazda CX-9"
        if not (year and make and model):
            title = _extract_first(r"\b(\d{4})\s+([A-Za-z][A-Za-z0-9\-]+)\s+([A-Za-z0-9][A-Za-z0-9\-]+)\b", text)
            if title:
                # Re-run with a stricter capture on the whole match to avoid partials.
                m = re.search(r"\b(\d{4})\s+([A-Za-z][A-Za-z0-9\-]+)\s+([A-Za-z0-9][A-Za-z0-9\-]+)\b", title)
                if m:
                    year = year or m.group(1)
                    make = make or m.group(2)
                    model = model or m.group(3)

        if not (year and make and model):
            validation_passed = False
            validation_msgs.append("Missing vehicle identity (Year/Make/Model).")
            # Still build a best-effort name for UI visibility
            asset_name = "Unknown Vehicle"
        else:
            asset_name = f"{year} {make.strip()} {model.strip()}".strip()
            validation_msgs.append(f"Vehicle identified: {asset_name}")

        # ─────────────────────────────────────────────────────────
        # Valuation (NAV)
        # ─────────────────────────────────────────────────────────
        # Primary: CarGurus-style "Great Deal $16,209"
        nav_raw = _extract_first(r"\bGreat\s+Deal\b\s*\$?\s*([0-9][0-9,.\s]*)", text)

        # If not found, accept other deal labels (Good Deal/Fair Deal/etc.)
        if not nav_raw:
            nav_raw = _extract_first(
                r"\b(?:Good|Fair|Overpriced|High|Low)\s+Deal\b\s*\$?\s*([0-9][0-9,.\s]*)",
                text
            )

        nav = _parse_money_to_float(nav_raw) if nav_raw else None

        # Fallback: AutoTrader-style "Price Range $14,964 - $19,046" (midpoint)
        if nav is None:
            lo_raw = _extract_first(r"\bPrice\s+Range\b.*?\$?\s*([0-9][0-9,.\s]*)\s*-\s*\$?\s*([0-9][0-9,.\s]*)", text)
            # The helper above returns only group(1); do a direct regex for both groups:
            m = re.search(
                r"\bPrice\s+Range\b.*?\$?\s*([0-9][0-9,.\s]*)\s*-\s*\$?\s*([0-9][0-9,.\s]*)",
                text,
                re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
            if m:
                lo = _parse_money_to_float(m.group(1))
                hi = _parse_money_to_float(m.group(2))
                if lo is not None and hi is not None:
                    nav = (lo + hi) / 2.0
                    validation_msgs.append(f"Price Range found; midpoint NAV used: ({lo} + {hi})/2")

        if nav is None:
            validation_passed = False
            validation_msgs.append("Missing valuation (could not find Great Deal $X or Price Range $A - $B).")
        else:
            validation_msgs.append(f"Valuation parsed: {nav:.2f} CAD")

        # ─────────────────────────────────────────────────────────
        # Date (snapshot-only)
        # ─────────────────────────────────────────────────────────
        # Try to find an explicit date string in the PDF
        date_raw = (
            _extract_first(r"\bDate\s*:\s*([0-9]{4}[-/][0-9]{2}[-/][0-9]{2})\b", text)
            or _extract_first(r"\bDate\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\b", text)
            or _extract_first(r"\bAs\s+of\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\b", text)
        )
        snap_date = _try_parse_date_to_ymd(date_raw)
        if not snap_date:
            snap_date = datetime.utcnow().strftime("%Y-%m-%d")
            validation_msgs.append("Snapshot date not found; using import date (UTC).")
        else:
            validation_msgs.append(f"Snapshot date parsed: {snap_date}")

        # ─────────────────────────────────────────────────────────
        # account_id (slugify from asset name)
        # ─────────────────────────────────────────────────────────
        account_id = _numeric_account_id(asset_name)
        institution = self.institution_default

        # ─────────────────────────────────────────────────────────
        # Final output (NAV-only)
        # ─────────────────────────────────────────────────────────
        output = {
            "export_date": datetime.now(timezone.utc).date().isoformat(),
            "document_signature": {
                "document_type": self.signature_name,
                "validation_passed": validation_passed,
                "validation_message": "; ".join(validation_msgs),
            },
            "validation_passed": validation_passed,
            "validation_message": "; ".join(validation_msgs),
            "account_summary": {
                "account_id": account_id,
                "account_name": asset_name,
                "institution": institution,
                "account_side": "asset",
                "account_class": "car_fmv",
                "base_currency": "CAD",  # www.cargurus.ca -> manually enforcing CAD prices.
            },
            "nav_snapshots": [
                {
                    "date": snap_date,
                    "nav": nav,
                }
            ],
        }

        # ─────────────────────────────────────────────────────────
        # Schema validation
        # ─────────────────────────────────────────────────────────
        errors = sorted(_VALIDATOR.iter_errors(output), key=lambda e: e.path)
        if errors:
            raise ValueError(
                "Schema validation failed: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
            )

        return output
