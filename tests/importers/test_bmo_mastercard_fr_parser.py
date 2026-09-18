from __future__ import annotations

from pathlib import Path
import contextlib
import io

import pytest

from app.services.importers.credit.bmo_mastercard_fr import BmoMastercardFrImporter


def test_extract_transactions_repairs_compact_adjacent_dates():
    importer = BmoMastercardFrImporter()
    text = "\n".join(
        [
            "header",
            "10 mars10 mars CLUB MED SALES CANADA MONTREAL 2 549,00",
            "10 mars11 mars KABABGY MONTREAL QC 22,02",
            "11 mars12 mars SQ *NORTHBOUND JIU JIT Chambly QC 1 679,55",
            "9 mars 10 mars UBER CANADA/UBERTRIP TORONTO ON 28,47",
            "footer",
        ]
    )

    txs = importer._extract_transactions_for(
        text=text,
        start=0,
        end=5,
        default_year=2026,
        statement_month=3,
    )

    assert len(txs) == 4
    assert txs[0]["amount"] == pytest.approx(-2549.00)
    assert txs[1]["amount"] == pytest.approx(-22.02)
    assert txs[2]["amount"] == pytest.approx(-1679.55)
    assert txs[3]["amount"] == pytest.approx(-28.47)


def test_bmo_mastercard_ok_statement_regression_set():
    importer = BmoMastercardFrImporter()
    base = Path("pdf_statements/done/bmo_creditcard/ok")
    if not base.exists():
        pytest.skip("Missing local regression statement directory")

    expected_totals = {
        "April 15, 2025.pdf": 138,
        "August 15, 2025.pdf": 131,
        "December 15, 2025.pdf": 124,
        "February 15, 2025.pdf": 117,
        "January 15, 2025.pdf": 146,
        "July 15, 2025.pdf": 128,
        "June 15, 2025.pdf": 113,
        "March 15, 2025.pdf": 113,
        "May 15, 2025.pdf": 163,
        "November 15, 2025.pdf": 125,
        "October 15, 2025.pdf": 122,
        "September 15, 2025.pdf": 126,
    }

    paths = sorted(base.glob("*.pdf"))
    assert len(paths) == len(expected_totals)

    for path in paths:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            data = importer.parse_to_json(path)

        assert data["validation_passed"] is True
        for st in data.get("statements", []):
            assert st["summary"]["validation_passed"] is True

        total = sum(len(st.get("transactions", [])) for st in data.get("statements", []))
        assert total >= expected_totals[path.name]

        assert "likely transaction but unparsable" not in stderr.getvalue().lower()


def test_extract_transactions_repairs_split_day_month_with_continuation():
    importer = BmoMastercardFrImporter()
    text = "\n".join(
        [
            "header",
            "24 25 EUR 29@1.556551724 SNCF-VOYAGEURS 45,14 CR",
            "mars mars MITRY MORY IDF",
            "footer",
        ]
    )

    txs = importer._extract_transactions_for(
        text=text,
        start=0,
        end=3,
        default_year=2026,
        statement_month=4,
    )

    assert len(txs) == 1
    assert txs[0]["amount"] == pytest.approx(45.14)
    assert "SNCF-VOYAGEURS" in txs[0]["description"]
    assert "MITRY MORY IDF" in txs[0]["description"]


def test_extract_transactions_prefers_fx_consistent_amount_candidate():
    importer = BmoMastercardFrImporter()
    text = "\n".join(
        [
            "header",
            "14 mars16 mars EUR 126@1.614444444 MAILLE SOUPLE 743 203,42",
            "footer",
        ]
    )

    txs = importer._extract_transactions_for(
        text=text,
        start=0,
        end=2,
        default_year=2026,
        statement_month=4,
    )

    assert len(txs) == 1
    assert txs[0]["amount"] == pytest.approx(-203.42)


def test_bmo_mastercard_2026_done_regression_set():
    importer = BmoMastercardFrImporter()
    base = Path("pdf_statements/2026/bmo/MC/done")
    if not base.exists():
        pytest.skip("Missing local 2026 regression statement directory")

    expected_totals = {
        "January 15, 2026.pdf": 122,
        "February 15, 2026.pdf": 129,
        "March 15, 2026.pdf": 105,
    }

    paths = sorted(base.glob("*.pdf"))
    assert len(paths) == len(expected_totals)

    for path in paths:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            data = importer.parse_to_json(path)

        assert data["validation_passed"] is True
        for st in data.get("statements", []):
            assert st["summary"]["validation_passed"] is True

        total = sum(len(st.get("transactions", [])) for st in data.get("statements", []))
        assert total >= expected_totals[path.name]

        assert "likely transaction but unparsable" not in stderr.getvalue().lower()


def test_april_2026_missed_statement_validates():
    importer = BmoMastercardFrImporter()
    path = Path("pdf_statements/2026/bmo/MC/missed/April 15, 2026.pdf")
    if not path.exists():
        pytest.skip("Missing April 2026 missed statement")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        data = importer.parse_to_json(path)

    assert data["validation_passed"] is True
    assert len(data.get("statements", [])) == 2

    st1 = data["statements"][0]["summary"]
    st2 = data["statements"][1]["summary"]
    assert st1["validation_passed"] is True
    assert st2["validation_passed"] is True
    assert st1["statement_subtotal"] == pytest.approx(-13650.91)
    assert st1["net_amount"] == pytest.approx(-13650.91)
    assert st2["statement_subtotal"] == pytest.approx(-11507.12)

    assert "likely transaction but unparsable" not in stderr.getvalue().lower()
