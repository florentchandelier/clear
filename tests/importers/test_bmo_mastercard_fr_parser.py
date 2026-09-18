from __future__ import annotations

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
