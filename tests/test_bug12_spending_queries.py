from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services import queries as q


def _write_hive_partitioned_transactions(root: Path) -> None:
    partition = root / "year=2026" / "month=03"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "operation_date": "2026-03-10",
                "posted_date": None,
                "description": "Fallback Institution",
                "description_norm": "fallback institution",
                "amount": -100.0,
                "debit": 100.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "bug12-t1",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
                "institution": None,
                "cardholder_name": "Alice",
            },
            {
                "operation_date": "2026-03-11",
                "posted_date": None,
                "description": "Synthetic Bank Purchase",
                "description_norm": "synthetic bank purchase",
                "amount": -50.0,
                "debit": 50.0,
                "credit": 0.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "bug12-t2",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
                "institution": "Synthetic Bank",
                "cardholder_name": None,
            },
            {
                "operation_date": "2026-03-12",
                "posted_date": None,
                "description": "Synthetic Bank Refund",
                "description_norm": "synthetic bank refund",
                "amount": 20.0,
                "debit": 0.0,
                "credit": 20.0,
                "currency": "CAD",
                "transaction_type": "refund",
                "transaction_id": "bug12-t3",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
                "institution": "Synthetic Bank",
                "cardholder_name": None,
            },
        ]
    ).to_parquet(partition / "transactions.parquet", index=False)


def test_total_spending_by_institution_groups_the_fallback_expression(tmp_path):
    _write_hive_partitioned_transactions(tmp_path)

    result = q.run_query("total_spending_by_institution", str(tmp_path))

    assert list(result.columns) == ["institution", "total_spent", "abs_amount"]
    by_institution = result.set_index("institution")
    assert float(by_institution.loc["Fallback Institution", "total_spent"]) == -100.0
    assert float(by_institution.loc["Synthetic Bank", "total_spent"]) == -30.0


def test_total_spending_by_tag_month_ignores_the_hive_month_column(
    monkeypatch, tmp_path
):
    _write_hive_partitioned_transactions(tmp_path)
    monkeypatch.setattr(
        q,
        "_tagmap_df",
        lambda: pd.DataFrame(
            [
                {
                    "tag": "daily_life",
                    "category": "food_daily",
                    "subcategory": None,
                    "type": None,
                }
            ]
        ),
    )

    result = q.run_query("total_spending_by_tag_month", str(tmp_path))

    assert list(result.columns) == ["month", "tag", "total_spent"]
    assert result.to_dict("records") == [
        {"month": "2026-03", "tag": "daily_life", "total_spent": 130.0}
    ]


def test_spending_for_year_month_exposes_and_orders_total_spent(tmp_path):
    _write_hive_partitioned_transactions(tmp_path)

    result = q.run_query(
        "spending_for_year_month",
        str(tmp_path),
        year=2026,
        month=3,
    )

    assert list(result.columns) == ["cardholder_name", "total_spent"]
    assert result.to_dict("records") == [
        {"cardholder_name": "Alice", "total_spent": 100.0},
        {"cardholder_name": "unknown", "total_spent": 30.0},
    ]


def test_cardholder_queries_accept_an_all_null_parquet_column(tmp_path):
    _write_hive_partitioned_transactions(tmp_path)
    path = tmp_path / "year=2026" / "month=03" / "transactions.parquet"
    transactions = pd.read_parquet(path)
    transactions["cardholder_name"] = None
    transactions.to_parquet(path, index=False)

    all_months = q.run_query("total_spending_by_cardholder", str(tmp_path))
    one_month = q.run_query(
        "spending_for_year_month",
        str(tmp_path),
        year=2026,
        month=3,
    )

    assert all_months.to_dict("records") == [
        {"cardholder_name": "unknown", "total_spent": -130.0, "abs_amount": 130.0}
    ]
    assert one_month.to_dict("records") == [
        {"cardholder_name": "unknown", "total_spent": 130.0}
    ]
