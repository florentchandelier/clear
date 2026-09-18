# app/web/routes_ui.py
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, List, Dict
import hashlib
import uuid
from app.services.normalize_parquet import _file_sha256, _stable16
from app.services.utils import statement_anchor_date

from flask import Blueprint, jsonify, render_template, request
from werkzeug.utils import secure_filename
import pandas as pd
import copy
import json

# Settings + queries
from app import config
from app.config import (
    load_settings,
    ACCOUNT_SIDES,
    ACCOUNT_CLASSES_BY_SIDE,
)
from app.services import queries as q

from app.services import update_categories as upd
from app.services import categories as cat_svc

from app.services.categories import (
    reset_to_defaults,
    save_base_categories,
    save_custom_categories,
)

# Importers + ingestion
from app.services.importers.registry import (
    list_importers,
    importer_by_key,
    public_catalog,
)
from app.services.normalize_parquet import (
    TransactionReconciliationError,
    ingest_pdf_with_importer_json,
    _meta_path,
)

import logging
log = logging.getLogger(__name__)

ui  = Blueprint("ui",  __name__, template_folder="templates", static_folder="static", static_url_path="/static")
api = Blueprint("api", __name__)

# ─────────────────────────────────────────────────────────────
# Staging storage (in-memory)
# ─────────────────────────────────────────────────────────────

_STAGED_IMPORTS: Dict[str, dict] = {}  # preview_id → { importer_json, account_side, account_class, institution, source_pdf }

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _year_from_request(parquet_path: str) -> int | None:
    # 1️⃣ Explicit query param always wins
    try:
        y = request.args.get("year")
        if y is not None:
            return int(y)
    except Exception:
        pass

    # 2️⃣ Data-driven default
    root = Path(parquet_path).resolve()
    con = q.build_txn_connection(root)
    try:
        return q.default_data_year(con, root)
    finally:
        try:
            con.close()
        except Exception:
            pass


def _settings_parquet_path() -> str:
    s = config.load_settings()
    return s.get("consolidated_statements", "")

def _parquet_root_path() -> Path:
    return Path(_settings_parquet_path()).resolve()

def _df_records(df) -> List[Dict[str, Any]]:
    try:
        return df.to_dict("records")
    except Exception:
        return []

def _format_month_series(rows: Iterable[Mapping[str, Any]]) -> dict:
    labels = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    data_by_m = {}
    for r in rows:
        m_raw = r.get("month", r.get("m", r.get("month_num")))
        if isinstance(m_raw, str):
            try:
                m_num = int(m_raw.split("-")[1])
            except Exception:
                continue
        else:
            try:
                m_num = int(m_raw)
            except Exception:
                continue
        if not (1 <= m_num <= 12):
            continue
        total = r.get("total_spent", r.get("total", 0.0))
        try:
            data_by_m[m_num] = float(total or 0.0)
        except Exception:
            data_by_m[m_num] = 0.0
    data = [data_by_m.get(i, 0.0) for i in range(1, 13)]
    return {"labels": labels, "datasets": [{"label": "Monthly spending", "data": data}]}

# ─────────────────────────────────────────────────────────────
# UI: Dashboard
# ─────────────────────────────────────────────────────────────

@ui.route("/")
def dashboard():
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)

    monthly_df = q.run_query("spending_by_month_for_year", parquet_path, year=year)
    cats_df    = q.run_query("total_spending_by_category", parquet_path, year=year)

    # networth view by asset classes
    nw_liq_df = q.run_query("net_worth_by_liquidity_timeseries", parquet_path)
    nw_nat_df = q.run_query("net_worth_by_asset_nature_timeseries", parquet_path)

    if year is not None:
        ystr = f"{year:04d}-"
        nw_liq_df = nw_liq_df[nw_liq_df["month"].str.startswith(ystr)]
        nw_nat_df = nw_nat_df[nw_nat_df["month"].str.startswith(ystr)]

    asset_matrix_df = q.run_query("asset_matrix_latest_snapshot", parquet_path)

    monthly_rows = _df_records(monthly_df)
    cat_rows     = _df_records(cats_df)

    monthly_chart    = _format_month_series(monthly_rows)

    def _tot(r):
        v = r.get("total_spent", r.get("total", 0.0))
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    top_n = sorted(cat_rows, key=lambda r: abs(r["total_spent"]), reverse=True)[:10]
    top_n_norm = [{"name": r.get("category", "other"), "total_spent": float(r["total_spent"] or 0)} for r in top_n]

    categories_chart = {
        "labels": [r["name"] for r in top_n_norm],
        "datasets": [{"label": "Spend by category", "data": [r["total_spent"] for r in top_n_norm]}],
    }

    return render_template(
        "dashboard.html",
        year=year,
        monthly_chart=monthly_chart,
        categories_chart=categories_chart,
        top_categories=top_n_norm,
        net_worth_liquidity=_df_records(nw_liq_df),
        net_worth_asset_nature=_df_records(nw_nat_df),
        asset_matrix=_df_records(asset_matrix_df),
        title="Dashboard",
    )


@api.route("/dashboard/spending_by_month_for_year")
def api_spending_by_month_for_year():
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)
    df = q.run_query("spending_by_month_for_year", parquet_path, year=year)
    rows = _df_records(df)
    return jsonify(_format_month_series(rows))

@api.route("/dashboard/total_spending_by_category")
def api_total_spending_by_category():
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)
    df = q.run_query("total_spending_by_category", parquet_path, year=year)
    rows = _df_records(df)

    def _tot(r):
        v = r.get("total_spent", r.get("total", 0.0))
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    table = [{"name": r.get("category", "other"), "total_spent": _tot(r)} for r in rows]
    table = sorted(table, key=lambda r: r["total_spent"], reverse=True)[:20]

    chart = {
        "labels": [r["name"] for r in table],
        "datasets": [{"label": "Spend by category", "data": [r["total_spent"] for r in table]}],
    }

    return jsonify({"chart": chart, "table": table})

@api.route("/dashboard/total_spending_by_tag")
def api_total_spending_by_tag():
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)
    df = q.run_query("total_spending_by_tag", parquet_path, year=year)
    rows = _df_records(df)

    def _tot(r):
        v = r.get("total_spent", r.get("total", 0.0))
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    table = [{"name": r.get("tag", "other"), "total_spent": _tot(r)} for r in rows]
    table = sorted(table, key=lambda r: r["total_spent"], reverse=True)[:20]

    chart = {
        "labels": [r["name"] for r in table],
        "datasets": [{"label": "Spend by tag", "data": [r["total_spent"] for r in table]}],
    }

    return jsonify({"chart": chart, "table": table})

# ─────────────────────────────────────────────────────────────
# Dashboard Endpoints (Spending Stacked + Income vs Expense)
# ─────────────────────────────────────────────────────────────

@api.route("/dashboard/spending_stacked_by_category")
def api_spending_stacked_by_category():
    """
    Returns stacked spending by category/subcategory/type for a given year.
    Used by the 'Category Breakdown (Stacked)' chart.

    Each bar (X-axis) = category total spending.
    Each stacked segment = subcategory/type contribution.
    Includes 'uncategorized' at all levels for completeness.
    Segments are ordered top-to-bottom by share of spending within each category.
    """
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)
    df = q.run_query("total_spending_by_category_subcategory_type", parquet_path, year=year)
    if df.empty:
        return jsonify({"labels": [], "datasets": []})

    # ─────────────────────────────────────────────
    # Normalize and clean labels
    # ─────────────────────────────────────────────
    def _label_clean(s: str) -> str:
        if not s or str(s).strip() in ("__default__", "unsubcategorized", "unspecified", "none", "nan"):
            return "uncategorized"
        return str(s).strip()

    df["category"] = df["category"].apply(_label_clean)
    df["subcategory"] = df["subcategory"].apply(_label_clean)
    df["type"] = df["type"].apply(_label_clean)

    # Flatten subcategory/type for stack label
    def _combine_sub_type(sub, typ):
        # Define placeholders that should be hidden in stacked labels
        hidden = {"other", "__default__", "uncategorized", "unsubcategorized", "unspecified", "none", "nan"}
        sub = str(sub or "").strip()
        typ = str(typ or "").strip()
        if not typ or typ.lower() in hidden:
            return sub
        return f"{sub} / {typ}"

    df["sub_type_label"] = df.apply(lambda r: _combine_sub_type(r["subcategory"], r["type"]), axis=1)

    # ─────────────────────────────────────────────
    # Compute total_spent and ensure positive magnitudes
    # ─────────────────────────────────────────────
    df["total_spent"] = df["total_spent"].astype(float).abs()

    # ─────────────────────────────────────────────
    # Compute share of each sub_type within its category
    # ─────────────────────────────────────────────
    df["category_total"] = df.groupby("category")["total_spent"].transform("sum")
    df["share_within_cat"] = df["total_spent"] / df["category_total"]

    # Sort: largest share first within each category
    df = df.sort_values(["category", "share_within_cat"], ascending=[True, False])

    categories = df["category"].dropna().unique().tolist()
    sub_types = df["sub_type_label"].dropna().unique().tolist()

    # ─────────────────────────────────────────────
    # Build pivot: sub_type_label → category → amount
    # ─────────────────────────────────────────────
    pivot = {}
    for _, r in df.iterrows():
        sub_type = r["sub_type_label"]
        cat = r["category"]
        pivot.setdefault(sub_type, {c: 0.0 for c in categories})
        pivot[sub_type][cat] += r["total_spent"]

    # ─────────────────────────────────────────────
    # Determine stacking order by average share rank
    # ─────────────────────────────────────────────
    avg_share = (
        df.groupby("sub_type_label")["share_within_cat"]
        .mean()
        .sort_values(ascending=False)
        .to_dict()
    )
    sorted_sub_types = sorted(sub_types, key=lambda s: avg_share.get(s, 0.0), reverse=True)

    # ─────────────────────────────────────────────
    # Build Chart.js datasets
    # ─────────────────────────────────────────────
    import random
    datasets = []
    for sub_type in sorted_sub_types:
        cat_map = pivot.get(sub_type, {})
        color = f"hsl({random.randint(0,360)}, 65%, 60%)"
        data = [cat_map.get(c, 0.0) for c in categories]
        datasets.append({
            "label": sub_type,
            "data": data,
            "backgroundColor": color,
            "borderWidth": 1
        })

    # ─────────────────────────────────────────────
    # Final payload
    # ─────────────────────────────────────────────
    return jsonify({
        "labels": categories,  # X-axis categories
        "datasets": datasets   # stacked segments sorted by share
    })

@api.route("/dashboard/income_vs_expense_for_year")
def api_income_vs_expense_for_year():
    """
    Returns monthly totals of income and expense (txn-only)
    for the 'Income vs Expense' chart.

    Relies on a new query you’ll add to queries.py (see below).
    Filters entry_txn = TRUE and groups by month.
    Returns monthly totals of income (positive) and expense (negative).
    """
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)
    df = q.run_query("income_vs_expense_for_year", parquet_path, year=year)
    if df.empty:
        return jsonify([])

    # Normalize and return
    df["month"] = df["month"].astype(str)
    df["income"] = df["income"].astype(float)
    df["expense"] = df["expense"].astype(float)
    return jsonify(_df_records(df))

# ─────────────────────────────────────────────────────────────
# UI: Accounts
# ─────────────────────────────────────────────────────────────
from app.config import ACCOUNT_CLASS_LABELS

def _humanize_account_class(cls: str) -> str:
    return ACCOUNT_CLASS_LABELS.get(cls, cls.capitalize())

@ui.route("/accounts")
def accounts_page():
    """
    Renders the /accounts page showing all asset and liability accounts
    with their latest known balances, using prioritized sources:
      1️⃣ Statement summary (_meta/account_balances.parquet)
      2️⃣ Manual values (_meta/manual_values.parquet)
      3️⃣ NAV snapshots (_meta/nav_snapshots.parquet)
      4️⃣ Transaction-derived balances
    """
    settings = load_settings()
    parquet_path = settings.get("consolidated_statements", "")
    root_path = Path(parquet_path).resolve()
    display_currency = settings.get("display_currency", "")

    # ─────────────────────────────────────────────
    # Load accounts metadata
    # ─────────────────────────────────────────────
    acc_path = root_path / "_meta" / "accounts.parquet"
    if not acc_path.exists():
        acc_path = root_path / "accounts.parquet"

    acc_df = pd.read_parquet(acc_path) if acc_path.exists() else pd.DataFrame(
        columns=[
            "account_id", "name", "institution", "account_side",
            "account_class", "status", "account_number", "parent_account_id", "base_currency"
        ]
    )

    # After reading:
    if "base_currency" not in acc_df.columns:
        acc_df["base_currency"] = pd.NA

    acc_df["base_currency"] = (
        acc_df["base_currency"]
        .astype("string")
        .str.upper()
        .str.strip()
    )

    # ─────────────────────────────────────────────
    # Get latest per-account snapshot (balances + as_of_date + source)
    # ─────────────────────────────────────────────
    try:
        bal_df = q.run_query("net_worth_accounts_snapshot", parquet_path)
        bal_fx_df = q.run_query("net_worth_accounts_snapshot_fx", parquet_path)
    except Exception as e:
        log.warning(f"Could not run net_worth_accounts_snapshot: {e}")
        bal_df = pd.DataFrame(columns=["account_id", "balance", "latest_date", "source"])

    # Ensure required columns exist
    for col in ["balance", "latest_date", "source"]:
        if col not in bal_df.columns:
            bal_df[col] = pd.NA

    # ─────────────────────────────────────────────
    # Merge with account metadata (native + FX)
    # ─────────────────────────────────────────────

    # Native balances
    merged = acc_df.merge(
        bal_df[["account_id", "balance", "latest_date", "source"]],
        on="account_id",
        how="left"
    )

    merged = merged.rename(columns={"balance": "latest_amount"})
    merged["latest_amount"] = pd.to_numeric(
        merged["latest_amount"], errors="coerce"
    ).fillna(0.0)

    merged["latest_date"] = pd.to_datetime(
        merged["latest_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    # FX balances (display currency)
    if not bal_fx_df.empty:
        bal_fx_df = bal_fx_df.rename(columns={"balance": "latest_amount_fx"})
        merged = merged.merge(
            bal_fx_df[["account_id", "latest_amount_fx"]],
            on="account_id",
            how="left"
        )
    else:
        merged["latest_amount_fx"] = 0.0

    merged["latest_amount_fx"] = pd.to_numeric(
        merged["latest_amount_fx"], errors="coerce"
    ).fillna(0.0)

    # ─────────────────────────────────────────────
    # Build child-account mapping for combined/parent accounts
    # ─────────────────────────────────────────────
    children_map = {}
    if "parent_account_id" in merged.columns:
        children_map = (
            merged[merged["parent_account_id"].notna()]
            .groupby("parent_account_id")[["account_id", "name", "institution"]]
            .apply(lambda g: g.to_dict("records"))
            .to_dict()
        )

    # Attach child_accounts to parent rows
    merged["child_accounts"] = merged["account_id"].map(children_map)
    merged["child_accounts"] = merged["child_accounts"].apply(
        lambda v: v if isinstance(v, list) else []
    )

    # ─────────────────────────────────────────────
    # Hide child accounts if their parent exists
    # ─────────────────────────────────────────────
    if "parent_account_id" in merged.columns:
        parents = set(merged["account_id"].dropna().tolist())
        is_child = merged["parent_account_id"].notna() & merged["parent_account_id"].isin(parents)
        merged = merged[~is_child].copy()

    # ─────────────────────────────────────────────
    # Split by account side
    # ─────────────────────────────────────────────
    merged["account_class_label"] = merged["account_class"].apply(_humanize_account_class)

    assets = merged[merged["account_side"] == "asset"].to_dict("records")
    liabilities = merged[merged["account_side"] == "liability"].to_dict("records")

    # ─────────────────────────────────────────────
    # Debug output
    # ─────────────────────────────────────────────
    log.debug(f"Loaded {len(assets)} assets, {len(liabilities)} liabilities from {parquet_path}")
    if not merged.empty:
        log.debug(f"Sample balances:")
        log.debug(merged[["account_id", "latest_amount", "latest_date", "source"]].head())

    # ─────────────────────────────────────────────
    # Render template
    # ─────────────────────────────────────────────
    return render_template(
        "accounts.html",
        title="Accounts",
        assets=assets,
        liabilities=liabilities,
        account_sides=ACCOUNT_SIDES,
        classes_by_side=ACCOUNT_CLASSES_BY_SIDE,
        display_currency=display_currency,
    )

@api.route("/accounts/create_manual", methods=["POST"])
def api_create_manual_account():
    root = _parquet_root_path()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    account_side  = (data.get("account_side") or "").strip()
    account_class = (data.get("account_class") or "").strip()
    institution   = (data.get("institution") or "MANUAL").strip()
    account_number= (data.get("account_number") or "").strip()
    base_currency = (data.get("base_currency") or pd.NA).strip().upper()

    if not name or account_side not in ACCOUNT_SIDES or account_class not in ACCOUNT_CLASSES_BY_SIDE.get(account_side, []):
        return jsonify({"ok": False, "error": "invalid name/side/class"}), 400

    from app.services.normalize_parquet import _upsert_accounts, _stable16
    account_id = _stable16(f"{institution}|{account_side}|{account_class}|{name}")

    row = pd.DataFrame([{
        "account_id": account_id,
        "account_number": account_number,
        "name": name,
        "institution": institution,
        "account_side": account_side,
        "account_class": account_class,
        "status": "open",
        "base_currency": base_currency,
    }])
    _upsert_accounts(root, row)
    return jsonify({"ok": True, "account_id": account_id})

@api.route("/accounts/add_manual_value", methods=["POST"])
def api_add_manual_value():
    root = _parquet_root_path()
    data = request.get_json(force=True)

    account_id = data.get("account_id")
    operation_date = data.get("date") or data.get("operation_date")
    amount = data.get("amount", data.get("value"))
    note = data.get("note", "")

    if not account_id or not operation_date or amount is None:
        return jsonify({"ok": False, "error": "account_id, date, amount required"}), 400

    from app.services.manual_values import upsert_values, load_values

    month = pd.to_datetime(operation_date, errors="coerce").strftime("%Y-%m")

    df = pd.DataFrame([{
        "account_id": account_id,
        "operation_date": operation_date,
        "month": month,
        "amount": float(amount),
        "description": note or "manual transaction",
        "transaction_id": hashlib.sha256(
            f"{account_id}|{operation_date}|{amount}|{note or 'manual'}".encode()
        ).hexdigest()[:16],
        "transaction_type": "manual_value",
        "currency": "CAD",
        "category": "uncategorized",
        "subcategory": "__default__",
        "type": "__default__",
        "fuzzy_score": None,
    }])

    upsert_values(root, df)

    all_vals = load_values(root)
    row = (
        all_vals[all_vals["account_id"] == account_id]
        .sort_values("operation_date")
        .tail(1)
        .iloc[0]
        .to_dict()
    )

    return jsonify({
        "ok": True,
        "account_id": row["account_id"],
        "date": row["operation_date"],  # Keep legacy key for front-end compatibility
        "month": row["month"],
        "amount": row["amount"],
        "description": row.get("description", "")
    })


@api.route("/net_worth/series")
def api_net_worth_series():
    parquet_path = _settings_parquet_path()
    df = q.run_query("net_worth_timeseries", parquet_path)
    return jsonify(df.to_dict("records"))


@api.route("/net_worth/by_liquidity")
def api_net_worth_by_liquidity():
    parquet_path = _settings_parquet_path()
    df = q.run_query("net_worth_by_liquidity_timeseries", parquet_path)
    return jsonify(df.to_dict("records"))


@api.route("/net_worth/by_asset_nature")
def api_net_worth_by_asset_nature():
    parquet_path = _settings_parquet_path()
    df = q.run_query("net_worth_by_asset_nature_timeseries", parquet_path)
    return jsonify(df.to_dict("records"))


@api.route("/net_worth/asset_matrix")
def api_asset_matrix_latest():
    parquet_path = _settings_parquet_path()
    df = q.run_query("asset_matrix_latest_snapshot", parquet_path)
    return jsonify(df.to_dict("records"))


@api.route("/net_worth/liquid_assets")
def api_liquid_assets():
    parquet_path = _settings_parquet_path()
    df = q.run_query("liquid_financial_assets_timeseries", parquet_path)
    return jsonify(df.to_dict("records"))

@api.route("/net_worth/real_assets")
def api_net_worth_real_assets():
    parquet_path = _settings_parquet_path()

    try:
        df = q.run_query("net_worth_accounts_snapshot", parquet_path)
    except Exception:
        return jsonify([])

    if df.empty:
        return jsonify([])

    # Join account metadata
    root = Path(parquet_path)
    acc_path = root / "_meta" / "accounts.parquet"
    if not acc_path.exists():
        return jsonify([])

    acc_df = pd.read_parquet(acc_path)

    merged = acc_df.merge(
        df[["account_id", "balance", "latest_date", "source"]],
        on="account_id",
        how="left"
    )

    # Keep REAL assets only
    merged = merged[
        (merged["account_side"] == "asset") &
        (merged["asset_nature"] == "real")
    ]

    if merged.empty:
        return jsonify([])

    out = []
    for _, r in merged.iterrows():
        out.append({
            "account_id": r["account_id"],
            "name": r["name"],
            "value": float(r["balance"] or 0.0),
            "date": r["latest_date"],
            "source": r.get("source"),
        })

    return jsonify(out)


@api.route("/net_worth/series_fx")
def api_net_worth_series_fx():
    parquet_path = _settings_parquet_path()
    df, diag = q.run_net_worth_fx(parquet_path)

    return jsonify({
        "series": df.to_dict("records"),
        "diagnostics": diag,
    })


@api.route("/net_worth/asset_matrix_fx")
def api_asset_matrix_latest_fx():
    root = _parquet_root_path()
    con = q.build_txn_connection(root)
    df = q.asset_matrix_latest_snapshot_fx(con, root)
    return jsonify(df.to_dict("records"))


@api.route("/net_worth/liquid_assets_fx")
def api_liquid_assets_fx():
    parquet_path = _settings_parquet_path()
    year = _year_from_request(parquet_path)

    root = _parquet_root_path()
    con = q.build_txn_connection(root)
    try:
        df = q.liquid_financial_assets_timeseries_fx(con, root)
    finally:
        con.close()

    if year is not None and not df.empty:
        ystr = f"{year:04d}-"
        df = df[df["month"].astype(str).str.startswith(ystr)]

    return jsonify(df.to_dict("records"))


@api.route("/net_worth/real_assets_fx")
def api_real_assets_fx():
    root = _parquet_root_path()
    con = q.build_txn_connection(root)
    df = q.net_worth_accounts_snapshot_fx(con, root)

    # filter REAL assets only
    df = df[
        (df["account_side"] == "asset") &
        (df["asset_nature"] == "real")
    ]

    out = [
        {
            "account_id": r["account_id"],
            "name": r["name"],
            "value": float(r["balance"]),
            "date": r["latest_month"],
        }
        for _, r in df.iterrows()
    ]

    return jsonify(out)

# ─────────────────────────────────────────────────────────────
# UI: Import
# ─────────────────────────────────────────────────────────────

@ui.route("/import")
def import_page():
    return render_template(
        "import.html",
        title="Import",
        account_sides=ACCOUNT_SIDES,
        classes_by_side=ACCOUNT_CLASSES_BY_SIDE,
        class_labels=ACCOUNT_CLASS_LABELS,
    )

@api.route("/import/catalog")
def api_import_catalog():
    account_side  = (request.args.get("account_side") or "").strip()
    account_class = (request.args.get("account_class") or "").strip()

    if account_side and account_class:
        try:
            imps = list_importers(account_side, account_class)
            out = [
                {
                    "key": i.key,
                    "name": i.label,
                    "account_side": i.account_side,
                    "account_class": i.account_class,
                    "formats": getattr(i, "formats", [getattr(i, "input_kind", "pdf")]),
                    "signature": getattr(i, "signature_name", None),
                    # default institution name for auto-fill
                    "institution_default": getattr(i, "institution_default", ""),
                }
                for i in imps
            ]
            return jsonify({
                "account_side": account_side,
                "account_class": account_class,
                "importers": out
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    # Fallback when no side/class specified: full public catalog
    return jsonify(public_catalog())

# ─────────────────────────────────────────────────────────────
# New staging endpoints
# ─────────────────────────────────────────────────────────────

@api.route("/import/preview", methods=["POST"])
def api_import_preview():
    account_side  = (request.form.get("account_side") or "").strip()
    account_class = (request.form.get("account_class") or "").strip()
    importer_key  = (request.form.get("importer_key") or "").strip()
    institution   = (request.form.get("institution") or "UNKNOWN").strip()
    f = request.files.get("file")

    # ─────────────────────────────────────────────────────────────
    # Validation of side/class/importer
    # ─────────────────────────────────────────────────────────────
    if account_side not in ACCOUNT_SIDES:
        return jsonify({"ok": False, "error": "invalid account_side"}), 400
    if account_class not in ACCOUNT_CLASSES_BY_SIDE.get(account_side, []):
        return jsonify({"ok": False, "error": "invalid account_class for side"}), 400

    imp = importer_by_key(importer_key)
    if (
        not imp
        or getattr(imp, "account_side", None) != account_side
        or getattr(imp, "account_class", None) != account_class
    ):
        return jsonify({"ok": False, "error": "invalid importer for side/class"}), 400
    if not f:
        return jsonify({"ok": False, "error": "file required"}), 400

    # ─────────────────────────────────────────────────────────────
    # Save upload temporarily
    # ─────────────────────────────────────────────────────────────
    upload_dir = Path("uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    disk_path = upload_dir / secure_filename(f.filename)
    f.save(disk_path.as_posix())

    # ─────────────────────────────────────────────────────────────
    # Detect and parse importer
    # ─────────────────────────────────────────────────────────────
    try:
        if hasattr(imp, "detect") and not imp.detect(disk_path):
            return jsonify({"ok": False, "error": "document signature mismatch"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"detect failed: {e}"}), 400

    try:
        importer_json = imp.parse_to_json(disk_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"parse failed: {e}"}), 400

    # ─────────────────────────────────────────────────────────────
    # Statement already imported ?
    # ─────────────────────────────────────────────────────────────
    parquet_root = _parquet_root_path()
    meta_df = pd.read_parquet(_meta_path(parquet_root, "statement_sources.parquet")) \
        if _meta_path(parquet_root, "statement_sources.parquet").exists() else pd.DataFrame()

    acc_sum = importer_json.get("account_summary", {}) or {}

    account_date = statement_anchor_date(importer_json)

    doc_type = (
            importer_json.get("document_signature", {}) or {}
    ).get("document_type", "UNKNOWN")

    fhash = _file_sha256(disk_path)
    source_statement_id = _stable16(f"{doc_type}|{account_date}|{fhash}")

    already_imported = (
            not meta_df.empty and source_statement_id in set(meta_df["source_statement_id"].tolist())
    )

    # ─────────────────────────────────────────────────────────────
    # Determine import type (NAV or Transactional)
    # ─────────────────────────────────────────────────────────────
    is_nav_import = bool(importer_json.get("nav_snapshots"))

    # Extract account info for preview header
    acc = importer_json.get("account_summary", {}) or {}
    account_id = acc.get("account_id", "")
    account_name = acc.get("account_name", "")
    institution_name = acc.get("institution", institution)
    base_currency = acc.get("base_currency", "CAD")

    nav_preview = []
    if is_nav_import:
        for snap in importer_json.get("nav_snapshots", []):
            nav_preview.append({
                "account_id": account_id,
                "account_name": account_name,
                "institution": institution_name,
                "base_currency": base_currency,
                "date": snap.get("date"),
                "nav": float(snap.get("nav", 0.0)),
            })

    # ─────────────────────────────────────────────────────────────
    # Build PREVIEW-ONLY JSON with synthetic cardholder if needed
    # ─────────────────────────────────────────────────────────────
    preview_json = copy.deepcopy(importer_json)

    if not is_nav_import:
        statements = preview_json.get("statements") or []
        # Do we already have real cardholders?
        has_real_cardholders = any(
            (st.get("cardholder_info") or {}).get("card_digits")
            for st in statements
        )

        # Only fake a cardholder if:
        #   - this is NOT a credit_card product (e.g. HELOC, LOC, chequing)
        #   - there are statements but no cardholder_info at all
        if account_class != "credit_card" and statements and not has_real_cardholders:
            pseudo_name = acc.get("account_name") or "Account"
            acct_digits = (acc.get("account_id") or "")[-4:]

            for st in statements:
                st["cardholder_info"] = {
                    "name": pseudo_name,
                    "card_number": acct_digits or None,
                    "card_digits": acct_digits or None,
                    # ❌ DO NOT INCLUDE parent_account_id HERE
                    # It’s only a UI label, not a real hierarchy.
                }

    # ─────────────────────────────────────────────────────────────
    # Stage ORIGINAL importer JSON (for commit)
    # ─────────────────────────────────────────────────────────────
    preview_id = str(uuid.uuid4())
    _STAGED_IMPORTS[preview_id] = {
        "importer_json": importer_json,  # <- ORIGINAL, untouched
        "account_side": account_side,
        "account_class": account_class,
        "institution": institution_name,
        "source_pdf": disk_path,
    }

    log.debug(
        "_STAGED_IMPORTS[preview_id] -> RAW IMPORTER_JSON:\n%s",
        json.dumps(importer_json, indent=2, ensure_ascii=False)
    )

    # ─────────────────────────────────────────────────────────────
    # Response payload (uses preview_json)
    # ─────────────────────────────────────────────────────────────
    return jsonify({
        "ok": True,
        "preview_id": preview_id,
        "already_imported": already_imported,
        "source_statement_id": source_statement_id,
        "is_nav_import": is_nav_import,
        "importer_json": preview_json,  # <- PREVIEW COPY
        "account_summary": {
            "account_id": account_id,
            "account_name": account_name,
            "institution": institution_name,
            "account_side": account_side,
            "account_class": account_class,
            "base_currency": base_currency,
        },
        "nav_preview": nav_preview
    })

@api.route("/import/commit", methods=["POST"])
def api_import_commit():
    """
    Finalize a staged import.

    Supports safe re-import of an already-imported statement by requiring
    explicit user confirmation (_overwrite_statement=true), which triggers
    statement-level replacement of all transactions.
    """
    data = request.get_json(force=True) or {}

    preview_id = data.get("preview_id")
    overwrite  = bool(data.get("_overwrite_statement", False))

    # ─────────────────────────────────────────────
    # Retrieve staged import
    # ─────────────────────────────────────────────
    staged = _STAGED_IMPORTS.pop(preview_id, None)
    if not staged:
        return jsonify({
            "ok": False,
            "error": "invalid preview_id"
        }), 400

    # 🔑 Inject overwrite flag into importer_json
    # This is the ONLY place overwrite intent is expressed.
    staged["importer_json"]["_overwrite_statement"] = overwrite

    out_root = _parquet_root_path()

    # ─────────────────────────────────────────────
    # Execute ingestion
    # ─────────────────────────────────────────────
    try:
        result = ingest_pdf_with_importer_json(
            staged["importer_json"],
            account_side=staged["account_side"],
            account_class=staged["account_class"],
            institution=staged["institution"],
            source_pdf_path=staged["source_pdf"],
            out_dir=out_root,
        )

        # ─────────────────────────────────────────────
        # Debug / audit log
        # ─────────────────────────────────────────────
        log.debug(
            "Import commit: written=%s, deleted=%s, meta_written=%s, "
            "accounts_upserted=%s, balances_written=%s, overwrite=%s",
            result.get("written"),
            result.get("deleted"),
            result.get("meta_written"),
            result.get("accounts_upserted"),
            result.get("balances_written"),
            overwrite,
        )

    except TransactionReconciliationError as e:
        return jsonify({
            "ok": False,
            "code": "transaction_reconciliation_failed",
            "error": "Import blocked: transaction reconciliation failed",
            "counts": e.counts,
        }), 422
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"commit failed: {e}"
        }), 500

    # ─────────────────────────────────────────────
    # Successful commit
    # ─────────────────────────────────────────────
    return jsonify({
        "ok": True,
        "result": result,
        "overwrite": overwrite,
    })

@api.route("/import/reject", methods=["POST"])
def api_import_reject():
    data = request.get_json(force=True)
    preview_id = data.get("preview_id")
    staged = _STAGED_IMPORTS.pop(preview_id, None)
    if not staged:
        return jsonify({"ok": False, "error": "invalid preview_id"}), 400
    return jsonify({"ok": True, "message": "staged import discarded"})

# ─────────────────────────────────────────────────────────────
# UI: Settings
# ─────────────────────────────────────────────────────────────

from flask import flash, redirect, url_for

@ui.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings_data = config.load_settings()
    if request.method == "POST":
        try:
            consolidated = request.form.get("consolidated_statements", "").strip()
            base_cat = request.form.get("base_categories_path", "").strip()
            custom_cat = request.form.get("custom_categories_path", "").strip()
            similarity_threshold = int(request.form.get("similarity_threshold", 80))
            min_cluster_tx = int(request.form.get("min_cluster_tx", 1))
            single_tx_bucket = request.form.get("single_tx_bucket", "OTHER_SMALL_CLUSTER").strip()
            overwrite_duplicates = bool(request.form.get("overwrite_duplicates"))

            settings_data.update({
                "consolidated_statements": consolidated,
                "base_categories_path": base_cat,
                "custom_categories_path": custom_cat,
                "similarity_threshold": similarity_threshold,
                "min_cluster_tx": min_cluster_tx,
                "single_tx_bucket": single_tx_bucket,
                "overwrite_duplicates": overwrite_duplicates
            })
            config.save_settings(settings_data)
            flash("Settings saved successfully.", "success")
            return redirect(url_for("ui.settings_page"))
        except Exception as e:
            flash(f"Failed to save settings: {e}", "danger")

    return render_template(
        "settings.html",
        settings=settings_data,
        title="Settings",
        profile_label=config.active_profile_label(),
        profile_dir=str(config.active_profile_dir()),
    )

@ui.route("/settings/reset_categories", methods=["POST"])
def settings_reset_categories():
    # Only reset base categories.json; keep user customizations (seeds & corrections)
    reset_to_defaults()
    flash("Base categories reset to defaults. "
          "Your custom seeds and corrections have been preserved.", "success")
    return redirect(url_for("ui.settings_page"))


# ─────────────────────────────────────────────────────────────
# UI: Transactions
# ─────────────────────────────────────────────────────────────

# UI: Transactions (viewer, not category seeding)
@ui.route("/transactions/view")
def transactions_view_page():
    return render_template("transactions_view.html", title="Transactions")

@api.route("/transactions/years")
def api_transaction_years():
    parquet_path = _settings_parquet_path()
    df = q.run_query("available_years", parquet_path)
    if df.empty:
        return jsonify([])
    return jsonify(df["year"].astype(int).tolist())


@api.route("/transactions/months_for_year")
def api_transaction_months_for_year():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty:
        return jsonify([])

    df["op_date"] = pd.to_datetime(df["op_date"], errors="coerce")
    year = request.args.get("year", type=int)
    if not year:
        return jsonify([])

    months = (
        df[df["op_date"].dt.year == year]["op_date"]
        .dt.month
        .dropna()
        .unique()
    )
    months = sorted(int(m) for m in months)
    return jsonify(months)

@api.route("/transactions/by_month")
def api_transactions_by_month():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    account = request.args.get("account", type=str)
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty or not year or not month:
        return jsonify([])

    df["op_date"] = pd.to_datetime(df["op_date"], errors="coerce")
    ym_str = f"{year}-{month:02d}"
    filtered = df[df["op_date"].dt.strftime("%Y-%m") == ym_str].copy()

    # ✅ Optional account filter
    if account:
        filtered = filtered[filtered["account_name"] == account]

    filtered["operation_date"] = pd.to_datetime(
        filtered["op_date"], errors="coerce"
    ).dt.strftime("%d %b %Y")

    rows = filtered[
        [
            "transaction_id",
            "operation_date",
            "account_name",
            "description",
            "description_norm",
            "amount",
            "transaction_type",
            "category",
            "subcategory",
            "type",
        ]
    ].to_dict("records")

    return jsonify(rows)

@api.route("/transactions/accounts")
def api_transactions_accounts():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty:
        return jsonify([])
    accounts = (
        df["account_name"]
        .dropna()
        .unique()
        .tolist()
    )
    accounts.sort()
    return jsonify(accounts)

@api.route("/transactions/latest")
def api_transactions_latest():
    """
    Return the most recent year and month based on regular transactions only.
    Excludes manual values or transfers.
    """
    parquet_path = _settings_parquet_path()
    root = Path(parquet_path).resolve()
    con = q.build_txn_connection(root)
    try:
        year = q.default_data_year(con, root)
    finally:
        con.close()

    if year is None:
        return jsonify({"year": None, "month": None})

    # Month is optional — best effort
    df = q.run_query("transactions_by_cardholder", parquet_path)
    if df.empty or "op_date" not in df.columns:
        return jsonify({"year": year, "month": None})

    df["op_date"] = pd.to_datetime(df["op_date"], errors="coerce")
    latest = df["op_date"].dropna().max()

    return jsonify({
        "year": int(year),
        "month": int(latest.month) if latest is not None else None
    })

# ─────────────────────────────────────────────────────────────
# Autocomplete: Categories / Subcategories / Types / Accounts
# ─────────────────────────────────────────────────────────────


@api.route("/transactions/filter")
def api_transactions_filter():
    """
    Dynamic filtering endpoint for the transaction table.
    Accepts query parameters:
      - year, month (optional)
      - category, subcategory, type, account
    Returns JSON list of matching transactions.
    """
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty:
        return jsonify([])

    # Normalize date column
    df["op_date"] = pd.to_datetime(df["op_date"], errors="coerce")
    df["operation_date"] = df["op_date"].dt.strftime("%d %b %Y")

    # Parameters
    year     = request.args.get("year", type=int)
    month    = request.args.get("month", type=int)
    category = (request.args.get("category") or "").strip().lower()
    subcat   = (request.args.get("subcategory") or "").strip().lower()
    typ      = (request.args.get("type") or "").strip().lower()
    account  = (request.args.get("account") or "").strip()

    # Apply filters
    if year:
        df = df[df["op_date"].dt.year == year]
    if month:
        df = df[df["op_date"].dt.month == month]
    if category:
        df = df[df["category"].str.lower() == category]
    if subcat:
        df = df[df["subcategory"].str.lower() == subcat]
    if typ:
        df = df[df["type"].str.lower() == typ]
    if account:
        df = df[df["account_name"] == account]

    # Format + return
    rows = df[
        [
            "transaction_id",
            "operation_date",
            "account_name",
            "description",
            "description_norm",
            "amount",
            "transaction_type",
            "category",
            "subcategory",
            "type",
        ]
    ].to_dict("records")

    return jsonify(rows)

# ─────────────────────────────────────────────────────────────
# Bulk categorization (multi-select)
# ─────────────────────────────────────────────────────────────
@api.route("/transactions/bulk-categorize", methods=["POST"])
def api_transactions_bulk_categorize():
    """
    Assign multiple transactions to a category/subcategory/type.
    Modes:
      - tx_only: one-time corrections (per tx_id)
      - authoritative: global rule (description_norm)
      - seed: add as seed examples
    """
    data = request.get_json(force=True) or {}
    tx_ids = data.get("tx_ids") or []
    cat = (data.get("category") or "").strip().lower()
    sub = (data.get("subcategory") or "").strip().lower() or None
    typ = (data.get("type") or "").strip().lower() or None
    mode = (data.get("mode") or "tx_only").strip().lower()

    if not tx_ids or not cat:
        return jsonify({"ok": False, "error": "tx_ids and category required"}), 400

    try:
        if mode == "tx_only":
            # one-off correction for each transaction using its real description
            from app import config
            from pathlib import Path
            import pandas as pd

            settings = config.load_settings()
            parquet_dir = Path(settings.get("consolidated_statements", ""))

            # Load all transactions once
            dfs = []
            for f in upd.collect_parquet_files(parquet_dir):
                if f.name in ("accounts.parquet",) or f.parent.name == "_meta":
                    continue
                df = pd.read_parquet(f, columns=["transaction_id", "description", "category"])
                dfs.append(df)
            all_df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

            for txid in tx_ids:
                tx_row = all_df.loc[all_df["transaction_id"] == txid].head(1)
                if not tx_row.empty:
                    tx_obj = tx_row.iloc[0].to_dict()
                else:
                    tx_obj = {"transaction_id": txid, "description": "(unknown)", "category": "uncategorized"}

                # 👇 Skip backfill for each individual correction
                upd.add_correction_or_seed(tx_obj, cat, sub, typ, authoritative=False, backfill=False)

            # ✅ Now perform one backfill at the end
            upd.update_categories(parquet_dir, all_tx=True)
            msg = f"Applied {len(tx_ids)} one-time corrections."

        elif mode == "authoritative":
            upd.add_authoritative_corrections_for_tx_ids(tx_ids, cat, sub, typ)
            msg = f"Added authoritative corrections for {len(tx_ids)} transactions."

        elif mode == "seed":
            upd.add_seeds_for_tx_ids(tx_ids, cat, sub, typ)
            msg = f"Added seeds for {len(tx_ids)} transactions."

        else:
            return jsonify({"ok": False, "error": f"invalid mode: {mode}"}), 400

        return jsonify({"ok": True, "message": msg, "updated": len(tx_ids)})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@api.route("/categories")
def api_autocomplete_categories():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty or "category" not in df.columns:
        return jsonify([])
    cats = sorted(df["category"].dropna().unique().tolist())
    return jsonify(cats)

@api.route("/subcategories")
def api_autocomplete_subcategories():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty or "subcategory" not in df.columns:
        return jsonify([])
    subs = sorted(df["subcategory"].dropna().unique().tolist())
    return jsonify(subs)

@api.route("/types")
def api_autocomplete_types():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty or "type" not in df.columns:
        return jsonify([])
    types = sorted(df["type"].dropna().unique().tolist())
    return jsonify(types)

@api.route("/accounts")
def api_autocomplete_accounts():
    parquet_path = _settings_parquet_path()
    df = q.run_query("all_transactions", parquet_path)
    if df.empty or "account_name" not in df.columns:
        return jsonify([])
    accounts = sorted(df["account_name"].dropna().unique().tolist())
    return jsonify(accounts)
