# tests/test_importer_schema_currency.py

import json
import pytest
from pathlib import Path
import jsonschema
from datetime import date
import pandas as pd
from app.services.fx.resolve import resolve_fx_rate


HERE = Path(__file__).resolve().parent
SCHEMA_PATH = (
    HERE.parent
    / "app"
    / "services"
    / "importers"
    / "schema.json"
)

@pytest.fixture(scope="module")
def schema():
    assert SCHEMA_PATH.exists(), f"schema.json not found at {SCHEMA_PATH}"
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate(payload, schema):
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    if errors:
        raise errors[0]


# ─────────────────────────────────────────────
# ✅ PASS: base_currency present
# ─────────────────────────────────────────────

def test_importer_schema_accepts_base_currency(schema):
    payload = {
        "export_date": "2025-01-01 00:00:00",
        "document_signature": {
            "document_type": "TEST_DOC"
        },
        "account_summary": {
            "account_id": "ABC123",
            "account_name": "Test Account",
            "institution": "Test Bank",
            "account_side": "asset",
            "account_class": "investment",
            "base_currency": "USD"
        },
        "nav_snapshots": [
            {
                "date": "2025-01-01",
                "nav": 12345.67
            }
        ]
    }

    # Should NOT raise
    validate(payload, schema)


# ─────────────────────────────────────────────
# ❌ FAIL: base_currency missing
# ─────────────────────────────────────────────

def test_importer_schema_rejects_missing_base_currency(schema):
    payload = {
        "export_date": "2025-01-01 00:00:00",
        "document_signature": {
            "document_type": "TEST_DOC"
        },
        "account_summary": {
            "account_id": "ABC123",
            "account_name": "Test Account",
            "institution": "Test Bank",
            "account_side": "asset",
            "account_class": "investment"
            # ❌ base_currency intentionally missing
        },
        "nav_snapshots": [
            {
                "date": "2025-01-01",
                "nav": 12345.67
            }
        ]
    }

    with pytest.raises(jsonschema.ValidationError) as exc:
        validate(payload, schema)

    # Strong assertion to ensure this is the exact failure
    msg = str(exc.value)
    assert "base_currency" in msg

# ─────────────────────────────────────────────
# ❌ FAIL: base_currency not ISO-4217 uppercase
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "bad_currency",
    ["usd", "Usd", "US", "USDD", "123", "", None],
)
def test_importer_schema_rejects_invalid_base_currency(schema, bad_currency):
    payload = {
        "export_date": "2025-01-01 00:00:00",
        "document_signature": {
            "document_type": "TEST_DOC"
        },
        "account_summary": {
            "account_id": "ABC123",
            "account_name": "Test Account",
            "institution": "Test Bank",
            "account_side": "asset",
            "account_class": "investment",
            "base_currency": bad_currency,
        },
        "nav_snapshots": [
            {
                "date": "2025-01-01",
                "nav": 12345.67
            }
        ]
    }

    with pytest.raises(jsonschema.ValidationError) as exc:
        validate(payload, schema)

    msg = str(exc.value)
    assert "base_currency" in msg

def test_transactional_importer_accepts_base_currency(schema):
    payload = {
        "export_date": "2025-01-01 00:00:00",
        "document_signature": {
            "document_type": "TEST_TXN_DOC"
        },
        "account_summary": {
            "account_id": "CHK123",
            "account_name": "Checking Account",
            "institution": "Test Bank",
            "account_side": "asset",
            "account_class": "cash",
            "base_currency": "CAD",
        },
        "statements": [
            {
                "summary": {
                    "validation_passed": True,
                    "validation_message": "ok"
                },
                "transactions": [
                    {
                        "operation_date": "2025-01-05",
                        "description": "Deposit",
                        "amount": 1000.0,
                        "debit": 0.0,
                        "credit": 1000.0,
                        "is_credit": True,
                        "transaction_type": "income"
                    }
                ]
            }
        ]
    }

    validate(payload, schema)

def test_transactional_importer_rejects_missing_base_currency(schema):
    payload = {
        "export_date": "2025-01-01 00:00:00",
        "document_signature": {
            "document_type": "TEST_TXN_DOC"
        },
        "account_summary": {
            "account_id": "CHK123",
            "account_name": "Checking Account",
            "institution": "Test Bank",
            "account_side": "asset",
            "account_class": "cash"
            # ❌ base_currency missing
        },
        "statements": [
            {
                "summary": {
                    "validation_passed": True,
                    "validation_message": "ok"
                },
                "transactions": [
                    {
                        "operation_date": "2025-01-05",
                        "description": "Deposit",
                        "amount": 1000.0,
                        "debit": 0.0,
                        "credit": 1000.0,
                        "is_credit": True,
                        "transaction_type": "income"
                    }
                ]
            }
        ]
    }

    with pytest.raises(jsonschema.ValidationError) as exc:
        validate(payload, schema)

    assert "base_currency" in str(exc.value)


@pytest.fixture
def fx_parquet_root(tmp_path: Path) -> Path:
    """
    Temporary parquet_root with seeded FX data.
    """
    meta_dir = tmp_path / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    # Seed FX rates for Jan 2025
    fx_data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2025-01-02"),
                "from_currency": "USD",
                "to_currency": "CAD",
                "rate": 1.45,
                "source": "test",
            }
        ]
    )

    fx_data.to_parquet(meta_dir / "currency.parquet", index=False)

    return tmp_path


def test_fx_anchor_for_nav_month(fx_parquet_root):
    """
    FX for NAV valuation must be anchored to the
    first business day of the valuation month.
    """

    rate, meta = resolve_fx_rate(
        valuation_date=pd.Timestamp("2025-01-31"),
        from_currency="USD",
        to_currency="CAD",
        parquet_root=fx_parquet_root,
    )

    # Jan 1, 2025 is a holiday → Jan 2, 2025
    assert meta["fx_date_used"].date() == date(2025, 1, 2)

    # FX must be resolved without fallback
    assert meta["fallback_used"] is False

    # Rate must come from seeded data
    assert rate == 1.45

