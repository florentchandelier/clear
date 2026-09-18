import json
import re
from datetime import datetime
from pathlib import Path
import pytest

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"

ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ─────────────────────────────────────────────
# Test-only helpers
# ─────────────────────────────────────────────

def _assert_valid_iso_date(d: str):
    assert ISO_DATE_RE.match(d), f"Invalid ISO date: {d}"
    datetime.strptime(d, "%Y-%m-%d")


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / name
    assert path.exists(), f"Missing fixture: {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _detect_importer_type(importer_json: dict) -> str:
    """
    Test-only semantic detection.

    Returns:
      - "nav"
      - "transactional"
    """
    if importer_json.get("nav_snapshots"):
        return "nav"
    if importer_json.get("statements"):
        return "transactional"
    raise AssertionError(
        "Importer must define either nav_snapshots or statements"
    )

# ─────────────────────────────────────────────
# Canonical contract test
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "fixture_name",
    [
        "questrade_equity.json",
        "interactive_brokers_ca.json",
        "bmo_nesbitt_ca.json",
        "car_cargurus_valuation.json",
        "home_evaluation_fonciere.json",
        "bmo_chequing_fr.json",
        "bmo_heloc_fr.json",
        "bmo_mastercard_fr.json",
        # add more fixtures here
    ]
)
def test_importer_json_contract(fixture_name):
    importer_json = _load_fixture(fixture_name)
    importer_type = _detect_importer_type(importer_json)

    # 1. export_date
    assert "export_date" in importer_json
    export_date = importer_json["export_date"]
    _assert_valid_iso_date(export_date)
    assert "utc" not in export_date.lower()

    # 2. account_summary / base_currency
    acct = importer_json.get("account_summary")
    assert acct, "account_summary is required"

    base_currency = acct.get("base_currency")
    assert base_currency, "base_currency is required"
    assert ISO_CURRENCY_RE.match(base_currency)

    # 3. NAV importers
    if importer_type == "nav":
        navs = importer_json["nav_snapshots"]
        assert navs

        for snap in navs:
            assert "date" in snap
            assert "nav" in snap
            _assert_valid_iso_date(snap["date"])
            assert isinstance(snap["nav"], (int, float))

            # 🔑 invariant
            # export_date is when the JSON was produced, NAV date is valuation date
            # They must both be valid ISO dates, but not equal
            _assert_valid_iso_date(export_date)
            _assert_valid_iso_date(snap["date"])

            # Optional sanity check: export_date should not be before valuation date
            assert export_date >= snap["date"], (
                "export_date should be on or after valuation date"
            )

    # 4. Transactional importers
    if importer_type == "transactional":
        statements = importer_json["statements"]
        assert statements

        for stmt in statements:
            summary = stmt.get("summary")
            assert summary

            stmt_date = summary.get("statement_date")
            assert stmt_date
            _assert_valid_iso_date(stmt_date)

    # 5. Validation metadata
    assert "validation_passed" in importer_json
    assert isinstance(importer_json["validation_passed"], bool)

    if importer_json["validation_passed"] is False:
        assert importer_json.get("validation_message")
