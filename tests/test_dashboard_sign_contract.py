from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.services import queries as q


def _write_transactions(root: Path) -> None:
    partition = root / "year=2026" / "month=03"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "operation_date": "2026-03-10",
                "posted_date": None,
                "description": "Synthetic groceries",
                "description_norm": "synthetic groceries",
                "amount": -100.0,
                "currency": "CAD",
                "transaction_type": "expense",
                "transaction_id": "sign-expense",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
                "institution": "Synthetic Shop",
                "cardholder_name": "Alice",
            },
            {
                "operation_date": "2026-03-11",
                "posted_date": None,
                "description": "Synthetic grocery refund",
                "description_norm": "synthetic grocery refund",
                "amount": 20.0,
                "currency": "CAD",
                "transaction_type": "refund",
                "transaction_id": "sign-refund-offset",
                "category": "food_daily",
                "subcategory": "groceries",
                "type": "produce",
                "institution": "Synthetic Shop",
                "cardholder_name": "Alice",
            },
            {
                "operation_date": "2026-03-12",
                "posted_date": None,
                "description": "Synthetic refund surplus",
                "description_norm": "synthetic refund surplus",
                "amount": 50.0,
                "currency": "CAD",
                "transaction_type": "refund",
                "transaction_id": "sign-refund-surplus",
                "category": "adjustments",
                "subcategory": "refunds",
                "type": "merchant",
                "institution": "Synthetic Refunds",
                "cardholder_name": "Bob",
            },
            {
                "operation_date": "2026-03-15",
                "posted_date": None,
                "description": "Synthetic salary",
                "description_norm": "synthetic salary",
                "amount": 200.0,
                "currency": "CAD",
                "transaction_type": "income",
                "transaction_id": "sign-income",
                "category": "income",
                "subcategory": "salary",
                "type": "regular",
                "institution": "Synthetic Employer",
                "cardholder_name": None,
            },
        ]
    ).to_parquet(partition / "transactions.parquet", index=False)


def _totals(df: pd.DataFrame, key: str) -> dict[str, float]:
    return {
        str(row[key]): float(row["total_spent"])
        for _, row in df.iterrows()
    }


def test_every_spending_query_uses_one_dashboard_sign_contract(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MINT_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)
    _write_transactions(tmp_path)
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
                },
                {
                    "tag": "adjustment",
                    "category": "adjustments",
                    "subcategory": None,
                    "type": None,
                },
            ]
        ),
    )

    expected = {"food_daily": 80.0, "adjustments": -50.0}
    assert _totals(
        q.run_query("total_spending_by_category", str(tmp_path)), "category"
    ) == expected

    hierarchy = q.run_query(
        "total_spending_by_category_subcategory_type", str(tmp_path)
    )
    assert _totals(hierarchy, "category") == expected
    subcategories = q.run_query(
        "total_spending_by_category_subcategory", str(tmp_path)
    )
    assert _totals(subcategories, "category") == expected
    assert _totals(
        q.run_query("total_spending_by_cardholder", str(tmp_path)),
        "cardholder_name",
    ) == {"Alice": 80.0, "Bob": -50.0}
    assert _totals(
        q.run_query("total_spending_by_institution", str(tmp_path)),
        "institution",
    ) == {"Synthetic Shop": 80.0, "Synthetic Refunds": -50.0}
    assert _totals(
        q.run_query("total_spending_by_tag", str(tmp_path)), "tag"
    ) == {"daily_life": 80.0, "adjustment": -50.0}
    assert _totals(
        q.run_query("total_spending_by_tag_month", str(tmp_path)), "tag"
    ) == {"daily_life": 80.0, "adjustment": -50.0}

    assert q.run_query("total_spending_by_month", str(tmp_path)).to_dict(
        "records"
    ) == [{"month": "2026-03", "total_spent": 30.0}]
    assert q.run_query(
        "spending_by_month_for_year", str(tmp_path), year=2026
    ).to_dict("records") == [{"month": "2026-03", "total_spent": 30.0}]
    assert _totals(
        q.run_query(
            "spending_for_year_month", str(tmp_path), year=2026, month=3
        ),
        "cardholder_name",
    ) == {"Alice": 80.0, "Bob": -50.0}
    assert q.run_query("spending_by_currency", str(tmp_path)).to_dict(
        "records"
    ) == [{"currency": "CAD", "total_spent": 30.0}]

    cash_flow = q.run_query(
        "income_vs_expense_for_year", str(tmp_path), year=2026
    )
    assert cash_flow.to_dict("records") == [
        {"month": "2026-03", "income": 200.0, "expense": 30.0}
    ]


@pytest.fixture
def dashboard_client(monkeypatch):
    from app.web import routes_ui
    from app.web.flask_ui import create_ui_app

    monkeypatch.setattr(routes_ui, "_settings_parquet_path", lambda: "unused")
    monkeypatch.setattr(routes_ui, "_year_from_request", lambda _: 2026)

    def fake_run_query(name, *_args, **_kwargs):
        if name in {
            "total_spending_by_category",
            "total_spending_by_tag",
        }:
            label = "category" if name.endswith("category") else "tag"
            return pd.DataFrame(
                [
                    {label: "outflow", "total_spent": 80.0},
                    {label: "net_refund", "total_spent": -120.0},
                ]
            )
        if name == "total_spending_by_category_subcategory_type":
            return pd.DataFrame(
                [
                    {
                        "category": "outflow",
                        "subcategory": "daily",
                        "type": "purchase",
                        "total_spent": 80.0,
                    },
                    {
                        "category": "net_refund",
                        "subcategory": "refunds",
                        "type": "merchant",
                        "total_spent": -50.0,
                    },
                ]
            )
        if name == "income_vs_expense_for_year":
            return pd.DataFrame(
                [{"month": "2026-03", "income": 200.0, "expense": 30.0}]
            )
        if name == "spending_by_month_for_year":
            return pd.DataFrame(
                [
                    {"month": "2026-03", "total_spent": 30.0},
                    {"month": "2026-04", "total_spent": -10.0},
                ]
            )
        raise AssertionError(f"unexpected query: {name}")

    monkeypatch.setattr(routes_ui.q, "run_query", fake_run_query)
    app = create_ui_app()
    app.testing = True
    return app.test_client()


def test_dashboard_endpoints_do_not_reinterpret_query_signs(dashboard_client):
    category = dashboard_client.get(
        "/api/dashboard/total_spending_by_category?year=2026"
    ).get_json()
    assert category["chart"]["datasets"][0]["data"] == [-120.0, 80.0]
    assert [row["total_spent"] for row in category["table"]] == [-120.0, 80.0]

    tag = dashboard_client.get(
        "/api/dashboard/total_spending_by_tag?year=2026"
    ).get_json()
    assert tag["chart"]["datasets"][0]["data"] == [-120.0, 80.0]

    monthly = dashboard_client.get(
        "/api/dashboard/spending_by_month_for_year?year=2026"
    ).get_json()
    assert monthly["datasets"][0]["data"][2:4] == [30.0, -10.0]

    stacked = dashboard_client.get(
        "/api/dashboard/spending_stacked_by_category?year=2026"
    ).get_json()
    stacked_values = [
        value
        for dataset in stacked["datasets"]
        for value in dataset["data"]
    ]
    assert 80.0 in stacked_values
    assert -50.0 in stacked_values

    cash_flow = dashboard_client.get(
        "/api/dashboard/income_vs_expense_for_year?year=2026"
    ).get_json()
    assert cash_flow == [{"expense": 30.0, "income": 200.0, "month": "2026-03"}]


def test_income_expense_javascript_uses_query_magnitudes_directly():
    script = (
        Path(__file__).parents[1]
        / "app"
        / "web"
        / "static"
        / "js"
        / "dashboard"
        / "income_vs_expense.js"
    ).read_text(encoding="utf-8")

    assert "Math.abs" not in script
    assert "v - expense[i]" in script
