from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services import queries as q


def _write_txn_parquet(root: Path) -> None:
    out = root / "year=2026" / "month=03"
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {
                "operation_date": "2025-01-10",
                "posted_date": None,
                "description": "Groceries A",
                "description_norm": "groceries a",
                "amount": -100.0,
                "debit": 100.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "t1",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
            },
            {
                "operation_date": "2026-03-10",
                "posted_date": None,
                "description": "Groceries B",
                "description_norm": "groceries b",
                "amount": -300.0,
                "debit": 300.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "t2",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
            },
            {
                "operation_date": "2026-03-15",
                "posted_date": None,
                "description": "Groceries refund",
                "description_norm": "groceries refund",
                "amount": -50.0,
                "debit": 50.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "refund",
                "transaction_id": "t3",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
            },
        ]
    )
    df.to_parquet(out / "transactions.parquet", index=False)


def test_total_spending_by_category_respects_year_filter(tmp_path: Path):
    _write_txn_parquet(tmp_path)

    all_years = q.run_query("total_spending_by_category", str(tmp_path))
    y2025 = q.run_query("total_spending_by_category", str(tmp_path), year=2025)
    y2026 = q.run_query("total_spending_by_category", str(tmp_path), year=2026)

    all_val = float(all_years.loc[all_years["category"] == "food_daily", "total_spent"].iloc[0])
    val_2025 = float(y2025.loc[y2025["category"] == "food_daily", "total_spent"].iloc[0])
    val_2026 = float(y2026.loc[y2026["category"] == "food_daily", "total_spent"].iloc[0])

    assert all_val == -450.0
    assert val_2025 == -100.0
    assert val_2026 == -350.0


def test_total_spending_by_tag_respects_year_filter(tmp_path: Path, monkeypatch):
    _write_txn_parquet(tmp_path)
    monkeypatch.setattr(
        q,
        "_tagmap_df",
        lambda: pd.DataFrame(
            [{"tag": "daily_life", "category": "food_daily", "subcategory": None, "type": None}]
        ),
    )

    all_years = q.run_query("total_spending_by_tag", str(tmp_path))
    y2025 = q.run_query("total_spending_by_tag", str(tmp_path), year=2025)
    y2026 = q.run_query("total_spending_by_tag", str(tmp_path), year=2026)

    all_val = float(all_years.loc[all_years["tag"] == "daily_life", "total_spent"].iloc[0])
    val_2025 = float(y2025.loc[y2025["tag"] == "daily_life", "total_spent"].iloc[0])
    val_2026 = float(y2026.loc[y2026["tag"] == "daily_life", "total_spent"].iloc[0])

    assert all_val == 350.0
    assert val_2025 == 100.0
    assert val_2026 == 250.0


def test_total_spending_by_category_subtype_respects_year_filter(tmp_path: Path):
    _write_txn_parquet(tmp_path)

    y2025 = q.run_query("total_spending_by_category_subcategory_type", str(tmp_path), year=2025)
    y2026 = q.run_query("total_spending_by_category_subcategory_type", str(tmp_path), year=2026)

    mask_2025 = (
        (y2025["category"] == "food_daily")
        & (y2025["subcategory"] == "groceries")
        & (y2025["type"] == "produce")
    )
    mask_2026 = (
        (y2026["category"] == "food_daily")
        & (y2026["subcategory"] == "groceries")
        & (y2026["type"] == "produce")
    )

    assert float(y2025.loc[mask_2025, "total_spent"].iloc[0]) == -100.0
    assert float(y2026.loc[mask_2026, "total_spent"].iloc[0]) == -350.0


def _write_multi_currency_txn_parquet(root: Path) -> None:
    out = root / "year=2026" / "month=03"
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {
                "operation_date": "2026-03-10",
                "posted_date": None,
                "description": "Groceries CAD",
                "description_norm": "groceries cad",
                "amount": -100.0,
                "debit": 100.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "t1",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
            },
            {
                "operation_date": "2026-03-11",
                "posted_date": None,
                "description": "Hotel USD",
                "description_norm": "hotel usd",
                "amount": -80.0,
                "debit": 80.0,
                "credit": 0.0,
                "currency": "USD",
                "transaction_type": "expense",
                "transaction_id": "t2",
                "category": "leisure",
                "subcategory": "travel",
                "type": "hotel",
            },
            {
                "operation_date": "2026-03-12",
                "posted_date": None,
                "description": "Hotel USD refund",
                "description_norm": "hotel usd refund",
                "amount": -20.0,
                "debit": 20.0,
                "credit": 0.0,
                "currency": "USD",
                "transaction_type": "refund",
                "transaction_id": "t3",
                "category": "leisure",
                "subcategory": "travel",
                "type": "hotel",
            },
        ]
    )
    df.to_parquet(out / "transactions.parquet", index=False)


def test_spending_by_currency_returns_totals_without_crashing(tmp_path: Path):
    """BUG-07 regression: the query's ORDER BY
    referenced an alias (total_spent) the SELECT never assigned, so this
    crashed unconditionally with a DuckDB binder error -- on any data,
    demo or real. Never exercised by a test before this."""
    _write_multi_currency_txn_parquet(tmp_path)

    result = q.run_query("spending_by_currency", str(tmp_path))

    by_currency = dict(zip(result["currency"], result["total_spent"]))
    assert by_currency == {"CAD": 100.0, "USD": 60.0}
