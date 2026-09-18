from __future__ import annotations

import json

import pandas as pd
import pytest

from app import config
from app.services import categories as cat_svc
from app.services import queries as q
from scripts import build_demo_profile as bdp


def _flag(tree: dict, cat: str, sub: str, typ: str | None = None) -> dict:
    node = tree[cat][sub]
    if typ is not None:
        node = node[typ]
    return node["_flags"]


@pytest.fixture
def isolated_custom_categories(monkeypatch, tmp_path):
    """Make `cat_svc.load_categories()` hermetic.

    BUG-02 regression: the two tests below used to
    call `load_categories()` with no fixture, so they silently read
    whatever `custom_categories_path` the *ambient* active profile
    resolved to. Before Milestone 2 that was always the real personal
    categories_custom.json, which happens to have a real family member's
    name as a category node -- so the test only ever passed by accident,
    and could (and did) fail depending on what an earlier test in the
    same run left `app.config`'s profile state pointed at. Injecting
    settings directly here removes both dependencies.
    """
    custom_path = tmp_path / "categories_custom.json"
    custom_path.write_text(json.dumps({
        "work_income": {
            "taxes": {
                "member_a": {},
                "member_b": {},
            },
        },
    }))
    monkeypatch.setattr(config, "load_settings", lambda: {"custom_categories_path": str(custom_path)})
    return custom_path


def test_semantic_overrides_propagate_to_descendants(isolated_custom_categories):
    tree = cat_svc.load_categories()

    taxes_root = _flag(tree, "work_income", "taxes")
    taxes_member_a = _flag(tree, "work_income", "taxes", "member_a")
    taxes_member_b = _flag(tree, "work_income", "taxes", "member_b")

    assert taxes_root["include_in_spending"] is False
    assert taxes_root["include_in_cash_flow"] is True

    assert taxes_member_a["include_in_spending"] is False
    assert taxes_member_a["include_in_cash_flow"] is True

    assert taxes_member_b["include_in_spending"] is False
    assert taxes_member_b["include_in_cash_flow"] is True


def test_savings_investments_wildcard_override_propagates(isolated_custom_categories):
    tree = cat_svc.load_categories()

    for sub_name, sub_node in tree["savings_investments"].items():
        if not isinstance(sub_node, dict):
            continue
        flags = sub_node.get("_flags", {})
        assert flags.get("include_in_spending") is False
        assert flags.get("include_in_cash_flow") is False


@pytest.fixture
def demo_parquet_root(monkeypatch, tmp_path):
    """Build the tracked synthetic seeds into an isolated profile."""
    demo_dir = tmp_path / "var" / "demo"
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEMO_DIR", demo_dir)
    monkeypatch.setattr(config, "PERSONAL_DIR", tmp_path / "personal")

    bdp.build()
    monkeypatch.setenv("MINT_PROFILE_DIR", str(demo_dir))
    return demo_dir / "consolidated_statements"


def test_dashboard_spending_and_cashflow_on_mock_fixture_are_stable(
    demo_parquet_root,
):
    """Pin dashboard totals to a clean build of the tracked demo seeds.

    The former 1625.29 August expectation came from an ignored ambient
    data/parquet/mock directory whose custom categories excluded the $250
    INTERAC expense. The public demo seed leaves that row uncategorized, whose
    default semantic flags include it in both spending and cash flow. Building
    the fixture here makes the 1875.29 baseline reproducible in a fresh clone.
    """
    spend = q.run_query("spending_by_month_for_year", demo_parquet_root, year=2025)
    cf = q.run_query("income_vs_expense_for_year", demo_parquet_root, year=2025)

    spend_map = {str(r["month"]): float(r["total_spent"]) for _, r in spend.iterrows()}
    cf_map = {
        str(r["month"]): (float(r["income"]), float(r["expense"]))
        for _, r in cf.iterrows()
    }

    assert spend_map["2025-07"] == pytest.approx(1332.95, abs=0.01)
    assert spend_map["2025-08"] == pytest.approx(1875.29, abs=0.01)
    assert spend_map["2025-09"] == pytest.approx(308.65, abs=0.01)

    assert cf_map["2025-07"] == pytest.approx((0.0, -1332.95), abs=0.01)
    assert cf_map["2025-08"] == pytest.approx((7600.5, -1875.29), abs=0.01)
    assert cf_map["2025-09"] == pytest.approx((250.0, -308.65), abs=0.01)


def test_json_declared_false_flag_not_covered_by_semantic_overrides_is_preserved(
    monkeypatch, tmp_path
):
    """BUG-08 regression: _ensure_seeds() used to be
    applied to _flags blocks too, corrupting every explicit boolean into
    a {_seeds: [], _deleted: []} dict that bool() always reads as True --
    silently discarding any JSON-declared include_in_spending: false not
    also covered by a SEMANTIC_OVERRIDES entry. "test_category" here is
    deliberately not one of the categories SEMANTIC_OVERRIDES touches, so
    this exercises the plain _flags path on its own, both directly and
    through an actual spending query.
    """
    custom_path = tmp_path / "categories_custom.json"
    custom_path.write_text(json.dumps({
        "test_category": {
            "test_sub": {
                "_flags": {
                    "include_in_spending": False,
                    "include_in_cash_flow": True,
                },
            },
        },
    }))
    monkeypatch.setattr(config, "load_settings", lambda: {"custom_categories_path": str(custom_path)})

    tree = cat_svc.load_categories()
    flags = tree["test_category"]["test_sub"]["_flags"]
    assert flags["include_in_spending"] is False
    assert flags["include_in_cash_flow"] is True

    parquet_dir = tmp_path / "parquet"
    part = parquet_dir / "year=2026" / "month=01"
    part.mkdir(parents=True)
    pd.DataFrame([
        {
            "operation_date": "2026-01-05",
            "posted_date": None,
            "description": "Excluded row",
            "description_norm": "excluded row",
            "amount": -50.0,
            "debit": 50.0,
            "credit": 0.0,
            "currency": "CAD",
            "transaction_type": "expense",
            "transaction_id": "tx1",
            "category": "test_category",
            "subcategory": "test_sub",
            "type": None,
        },
    ]).to_parquet(part / "transactions.parquet", index=False)

    spend = q.run_query("total_spending_by_category", str(parquet_dir))
    assert "test_category" not in set(spend["category"])
