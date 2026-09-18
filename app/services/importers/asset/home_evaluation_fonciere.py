# app/services/importers/asset/home_evaluation_fonciere.py

from __future__ import annotations
import re
import json
import pdfplumber
from datetime import datetime, timezone
from pathlib import Path
import jsonschema

from ..base import Importer

# ─────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────
from pathlib import Path as _P
_SCHEMA_PATH = _P(__file__).resolve().parents[1] / "schema.json"
with open(_SCHEMA_PATH, encoding="utf-8") as f:
    _SCHEMA = json.load(f)
_VALIDATOR = jsonschema.Draft7Validator(_SCHEMA)


class HomeEvaluationFonciereImporter(Importer):
    """
    Importer for Québec municipal property evaluation (FMV).
    NAV-only asset representing fair market value of a residence.
    """

    # ─────────────────────────────────────────────────────────────
    # Registry metadata
    # ─────────────────────────────────────────────────────────────
    key = "asset.home_fmv_evaluation_fonciere"
    label = "Home FMV (Municipal Evaluation)"
    account_side = "asset"
    account_class = "home_fmv"
    input_kind = "pdf"
    formats = ["pdf"]

    signature_name = "montreal.ca/role-evaluation-fonciere"
    institution_default = "Municipalité de Montréal-Ouest"

    # ─────────────────────────────────────────────────────────────
    # Detection
    # ─────────────────────────────────────────────────────────────
    def detect(self, path: Path) -> bool:
        try:
            with pdfplumber.open(path) as pdf:
                text = pdf.pages[0].extract_text() or ""
            return (
                "Rôle d’évaluation foncière" in text
                or "role-evaluation-fonciere" in text.lower()
            )
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # Core parser
    # ─────────────────────────────────────────────────────────────
    def parse_to_json(self, path: Path) -> dict:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        # Helper
        def _search(pattern, group=1, default=None):
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            return m.group(group).strip() if m else default

        # ─────────────────────────────────────────────────────────
        # Required fields
        # ─────────────────────────────────────────────────────────
        account_id = _search(r"Numéro de lot\s*:\s*([0-9]+)")
        account_name = _search(r"Adresse\s*:\s*(.+)")
        institution = self.institution_default

        valeur_terrain = _search(r"Valeur du terrain\s*:\s*([\d\s]+)\s*\$")
        valeur_batiment = _search(r"Valeur du bâtiment\s*:\s*([\d\s]+)\s*\$")
        valeur_immeuble = _search(r"Valeur de l'immeuble\s*:\s*([\d\s]+)\s*\$")

        report_date = _search(r"Date du rapport\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")

        # Normalize numbers
        def _num(x):
            return float(x.replace(" ", "")) if x else None

        terrain = _num(valeur_terrain)
        batiment = _num(valeur_batiment)
        immeuble = _num(valeur_immeuble)

        # ─────────────────────────────────────────────────────────
        # Validation
        # ─────────────────────────────────────────────────────────
        validation_msgs = []
        validation_passed = True

        if not account_id:
            validation_passed = False
            validation_msgs.append("Missing Numéro de lot.")

        if terrain and batiment and immeuble:
            if abs((terrain + batiment) - immeuble) > 1:
                validation_msgs.append(
                    f"Component mismatch: terrain + bâtiment = {terrain + batiment}, immeuble = {immeuble}"
                )
            else:
                validation_msgs.append("FMV component cross-check passed.")
        else:
            validation_msgs.append("FMV component cross-check unavailable.")

        export_date = datetime.now(timezone.utc).date().isoformat()
        if not report_date:
            report_date = export_date
            validation_msgs.append("Report date not found, using import date.")

        # ─────────────────────────────────────────────────────────
        # Final output
        # ─────────────────────────────────────────────────────────
        output = {
            "export_date": export_date,
            "document_signature": {
                "document_type": self.signature_name,
                "validation_passed": validation_passed,
                "validation_message": "; ".join(validation_msgs),
            },
            "validation_passed": validation_passed,
            "validation_message": "; ".join(validation_msgs),
            "account_summary": {
                "account_id": account_id,
                "account_name": account_name,
                "institution": institution,
                "account_side": "asset",
                "account_class": "home_fmv",
                "base_currency": "CAD",  # montreal.ca -> manually enforcing CAD prices.
            },
            "nav_snapshots": [
                {
                    "date": report_date,
                    "nav": immeuble,
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
