# app/web/routes_categories.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from pathlib import Path
import pandas as pd

from app import config
from app.services import categories as cat_svc
from app.services import update_categories as updater
from app.services import queries as q
from app.web.routes_ui import _settings_parquet_path

categories_bp = Blueprint("categories", __name__, template_folder="templates")


def _norm(s: str | None):
    """Normalize form values: lowercase + strip, preserve empty string instead of None."""
    if s is None:
        return None
    return str(s).strip().lower()


def _latest_year_month():
    """Return the most recent year and month from regular transactions only."""
    parquet_path = _settings_parquet_path()
    df = q.run_query("transactions_by_cardholder", parquet_path)
    if df.empty:
        return None, None
    df["op_date"] = pd.to_datetime(df["op_date"], errors="coerce")
    df = df.dropna(subset=["op_date"])
    if df.empty:
        return None, None
    latest_date = df["op_date"].max()
    return int(latest_date.year), int(latest_date.month)


@categories_bp.route("/categories/manage", methods=["GET"])
def categories_manage():
    cats = cat_svc.list_categories()  # node-with-_seeds tree
    return render_template("categories_manage.html", categories=cats)


@categories_bp.route("/categories/add_category", methods=["POST"])
def add_category():
    cat = _norm(request.form.get("category"))
    sub = _norm(request.form.get("subcategory"))
    typ = _norm(request.form.get("type"))
    if not cat:
        flash("Category is required.", "warning")
        return redirect(url_for("categories.categories_manage"))
    cat_svc.ensure_category(cat, sub, typ)
    flash("Category/Subcategory/Type ensured.", "success")
    return redirect(url_for("categories.categories_manage"))


@categories_bp.route("/categories/delete_category", methods=["POST"])
def delete_category():
    cat = _norm(request.form.get("category"))
    sub = _norm(request.form.get("subcategory"))
    typ = _norm(request.form.get("type"))
    if cat and sub and typ:
        cat_svc.delete_type(cat, sub, typ)
        flash("Type deleted.", "success")
    elif cat and sub:
        cat_svc.delete_subcategory(cat, sub)
        flash("Subcategory deleted.", "success")
    elif cat:
        cat_svc.delete_category(cat)
        flash("Category deleted.", "success")
    else:
        flash("Nothing to delete.", "warning")
    return redirect(url_for("categories.categories_manage"))


@categories_bp.route("/categories/remove_seed", methods=["POST"])
def remove_seed():
    cat = _norm(request.form.get("category"))
    sub = _norm(request.form.get("subcategory"))
    typ = _norm(request.form.get("type"))
    txid = request.form.get("transaction_id")

    if not (cat and txid):
        flash("Missing category or transaction id.", "warning")
        return redirect(url_for("categories.categories_manage"))

    cat_svc.remove_seed_transaction(cat, sub, typ, txid)
    flash("Seed removed.", "success")
    return redirect(url_for("categories.categories_manage"))


@categories_bp.route("/categories/update", methods=["POST"])
def update_categories_web():
    settings = config.load_settings()
    parquet_dir = Path(settings.get("consolidated_statements") or "")

    try:
        threshold = int(request.form.get("threshold", 80))
    except ValueError:
        threshold = 80

    year = request.form.get("year")
    try:
        year_i = int(year) if year else None
    except ValueError:
        year_i = None

    all_tx = request.form.get("all_tx") == "on"
    uncategorized = request.form.get("uncategorized") == "on"
    unsubcategorized = request.form.get("unsubcategorized") == "on"

    summaries = updater.update_categories(
        parquet_dir,
        threshold=threshold,
        all_tx=all_tx,
        year=year_i,
        uncategorized=uncategorized,
        unsubcategorized=unsubcategorized,
    )

    if summaries:
        for s in summaries:
            flash(s, "info")
        flash("✅ Categories successfully updated across all statements.", "success")
    else:
        flash("No eligible transactions found to update.", "warning")

    return redirect(url_for("categories.categories_manage"))


@categories_bp.route("/categories/add_correction", methods=["POST"])
def add_correction_web():
    """Add a correction or seed for a transaction (one-time or authoritative)."""
    cat = _norm(request.form.get("category"))
    sub = _norm(request.form.get("subcategory"))
    typ = _norm(request.form.get("type"))
    txid = request.form.get("transaction_id")
    desc = request.form.get("description", "")
    authoritative = request.form.get("authoritative") == "on"

    if not (txid and desc and cat):
        flash("Missing required fields: transaction_id, description, or category.", "warning")
        return redirect(url_for("categories.categories_manage"))

    tx_data = {
        "transaction_id": txid,
        "description": desc,
        "category": request.form.get("current_category", "uncategorized"),  # 🔹 ensure current category context
    }

    try:
        # NEW: unified entry point → seeds if uncategorized, corrections otherwise
        from app.services import update_categories as updater
        updater.add_correction_or_seed(
            tx_data,
            cat,
            sub,
            typ,
            authoritative=authoritative,
            backfill=True,
        )

        if authoritative:
            flash(f"Categorization rule updated: similar transactions will now map to {cat}/{sub or ''}/{typ or ''}",
                  "success")
        else:
            flash(f"Correction applied to this transaction only.", "info")

    except Exception as e:
        flash(f"Failed to add correction: {e}", "danger")

    return redirect(url_for("categories.categories_manage"))


# ──────────────────────────────────────────────────────────────
# API endpoint for category tree (used by transactions_view editing UI)
# ──────────────────────────────────────────────────────────────
@categories_bp.route("/api/categories/tree", methods=["GET"])
def api_categories_tree():
    cats = cat_svc.list_categories()
    return jsonify(cats)
