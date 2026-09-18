# app/services/queries.py
from __future__ import annotations

# ─────────────────────────────────────────────────────────────
# Standard library
# ─────────────────────────────────────────────────────────────
import logging
import json
import re
from pathlib import Path
from typing import Iterable

# ─────────────────────────────────────────────────────────────
# Third-party
# ─────────────────────────────────────────────────────────────
import duckdb
import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────────────
# App services
# ─────────────────────────────────────────────────────────────
from app import config
from app.services import categories as cat_svc
from app.services.fx.fetch_yfinance import ensure_fx_rate
from app.services.fx.resolve import (
    resolve_fx_rate,
    first_business_day_of_month,
    FXMissingError,
)
from app.services.fx.validate import validate_fx_scope, FXScopeError

LOG = logging.getLogger(__name__)

# ============================================================
# SECTION A — Normalization helpers (single source of truth)
# ============================================================

_DESC_NORM_RE_DIGITS = re.compile(r"[0-9]")
_DESC_NORM_RE_NONWORD = re.compile(r"[^\w\s]")
_DESC_NORM_RE_SPACES = re.compile(r"\s+")


def normalize_desc(desc: str | None) -> str:
    """
    Normalize transaction descriptions for:
      - corrections
      - seeds
      - fuzzy matching
      - analytics consistency

    MUST stay in sync with SQL normalization.
    """
    s = (desc or "").lower()
    s = _DESC_NORM_RE_DIGITS.sub("", s)
    s = _DESC_NORM_RE_NONWORD.sub("", s)
    s = _DESC_NORM_RE_SPACES.sub(" ", s)
    return s.strip()


def _sql_normalize_desc(expr: str) -> str:
    """
    SQL expression that exactly mirrors normalize_desc().
    """
    return f"""
        trim(
          regexp_replace(
            regexp_replace(
              regexp_replace(lower(coalesce({expr}, '')), '[0-9]', ''),
              '[^a-z\\s]', ''
            ),
            '\\s+', ' '
          )
        )
    """

# ============================================================
# SECTION B — Parquet discovery & safe SQL literals
# ============================================================

def collect_parquet_files(path: Path | None) -> list[Path]:
    """
    Collect parquet files recursively.
    Returns empty list if path is None or does not exist.
    """
    if not path or not path.exists():
        return []
    if path.is_file():
        return [path.resolve()]
    return sorted(path.rglob("*.parquet"))


def safe_path_literal(p: Path) -> str:
    """
    Escape path for safe embedding into SQL string literal.
    """
    return p.as_posix().replace("'", "''")


def build_scan_sql(file_paths: Iterable[Path]) -> str:
    """
    Build DuckDB read_parquet SQL with schema-stable fallback.
    """
    paths = list(file_paths)
    if not paths:
        # Empty, schema-compatible view
        return """
        SELECT
          CAST(NULL AS VARCHAR) AS source_statement_id,
          CAST(NULL AS VARCHAR) AS document_type,
          CAST(NULL AS VARCHAR) AS export_date,
          CAST(NULL AS VARCHAR) AS account_date,
          CAST(NULL AS VARCHAR) AS institution,
          CAST(NULL AS VARCHAR) AS account_side,
          CAST(NULL AS VARCHAR) AS account_class,
          CAST(NULL AS VARCHAR) AS account_id,
          CAST(NULL AS VARCHAR) AS account_number,
          CAST(NULL AS VARCHAR) AS cardholder_name,
          CAST(NULL AS DATE)    AS operation_date,
          CAST(NULL AS DATE)    AS posted_date,
          CAST(NULL AS VARCHAR) AS description,
          CAST(NULL AS VARCHAR) AS description_norm,
          CAST(NULL AS DOUBLE)  AS amount,
          CAST(NULL AS DOUBLE)  AS amount_raw,
          CAST(NULL AS BOOLEAN) AS is_refund,
          CAST(NULL AS DOUBLE)  AS debit,
          CAST(NULL AS DOUBLE)  AS credit,
          CAST(NULL AS VARCHAR) AS currency,
          CAST(NULL AS VARCHAR) AS transaction_type,
          CAST(NULL AS VARCHAR) AS transaction_id,
          CAST(NULL AS VARCHAR) AS category,
          CAST(NULL AS VARCHAR) AS subcategory,
          CAST(NULL AS VARCHAR) AS type,
          CAST(NULL AS DOUBLE)  AS fuzzy_score
        """
    files_sql = ", ".join(f"'{safe_path_literal(p)}'" for p in paths)
    return f"SELECT * FROM read_parquet([{files_sql}], union_by_name=true)"


def _meta_path(root: Path, name: str) -> Path:
    return (root / "_meta" / name).resolve()


# ============================================================
# SECTION C — Core SQL CTE (single authoritative base)
# Responsibilities:
#   • Normalize raw transaction fields (dates, descriptions, category type)
#   • Eliminate placeholder / UI sentinel values (e.g. 'unsubcategorized')
#   • Resolve category-level semantic flags deterministically
#   • Produce the single source of truth for:
#       - is_spending   (Consumption semantics)
#       - is_cash_flow  (Accounting cash-flow semantics)
#
# IMPORTANT:
#   All Spending and Cash Flow queries MUST derive from this CTE.
#   Downstream queries must never reimplement semantic filtering.
# ============================================================

_BASE_CTE = f"""
WITH df_fixed AS (
  SELECT
    *,
    NULL AS posted_date,
    NULL AS description_norm
  FROM df
),
base_raw AS (
  SELECT
    df_fixed.* EXCLUDE ("type"),

    CASE
      WHEN lower("type") IN ('unsubcategorized', '__default__', '')
      THEN NULL
      ELSE lower("type")
    END AS type,
    
    COALESCE(
      TRY_CAST(operation_date AS DATE),
      TRY_CAST(posted_date AS DATE)
    ) AS op_date,

    COALESCE(
      df_fixed.description_norm,
      {_sql_normalize_desc("df_fixed.description")}
    ) AS description_norm

  FROM df_fixed
),
base AS (
  SELECT
    br.*,
    COALESCE(cf.include_in_spending, TRUE)  AS is_spending,
    COALESCE(cf.include_in_cash_flow, TRUE) AS is_cash_flow

  FROM base_raw br

  LEFT JOIN category_flags cf
    ON lower(br.category) = cf.category
   AND lower(br.subcategory) IS NOT DISTINCT FROM cf.subcategory
   AND lower(br."type") IS NOT DISTINCT FROM cf.type
)
"""

# ============================================================
# SECTION D — Transaction type filters
# ============================================================

# IMPORTANT:
# Any query representing "Spending (Consumption)" MUST use _SPEND_WHERE.
# Do not inline or reimplement spending logic elsewhere.
_SPEND_WHERE = (
    "transaction_type IN ('expense','refund') "
    "AND amount IS NOT NULL "
    "AND is_spending"
)

# _CASH_FLOW_WHERE: full cash-flow domain (income + outflows)
_CASH_FLOW_WHERE = (
    "transaction_type IN ('income','expense','refund') "
    "AND amount IS NOT NULL "
    "AND is_cash_flow"
)

_ALL_MAIN_WHERE = (
    "transaction_type IN ('expense','income','refund','transfer') "
    "AND amount IS NOT NULL"
)

# ============================================================
# SECTION E — Tag helpers (pure, no SQL)
# ============================================================
def _resolved_category_flags_df() -> pd.DataFrame:
    """
    Resolve category semantic flags deterministically.

    One row per *exact* (category, subcategory, type) path.
    Inheritance is resolved in Python to prevent SQL fan-out.
    """
    tree = cat_svc.load_categories()
    rows: list[dict] = []

    DEFAULT_FLAGS = {
        "include_in_spending": True,
        "include_in_cash_flow": True,
    }

    def walk(node: dict, path: list[str], inherited_flags: dict):
        flags = dict(inherited_flags)
        flags.update(node.get("_flags", {}))

        rows.append({
            "category": path[0].lower(),
            "subcategory": path[1].lower() if len(path) > 1 else None,
            "type": path[2].lower() if len(path) > 2 else None,
            "include_in_spending": bool(flags["include_in_spending"]),
            "include_in_cash_flow": bool(flags["include_in_cash_flow"]),
        })

        for k, v in node.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                walk(v, path + [k], flags)

    for cat, node in tree.items():
        if isinstance(node, dict):
            walk(node, [cat], DEFAULT_FLAGS)

    return pd.DataFrame(rows)


def _register_category_flags(con: duckdb.DuckDBPyConnection) -> None:
    df = _resolved_category_flags_df()
    if df.empty:
        df = pd.DataFrame(columns=[
            "category",
            "subcategory",
            "type",
            "include_in_spending",
            "include_in_cash_flow",
        ])
    _register_df(con, "category_flags", df)


def _tagmap_df() -> pd.DataFrame:
    """
    Flatten tag → category/subcategory/type mapping
    from categories service.
    """
    rows: list[dict] = []
    tags = cat_svc.load_tags()

    for tag, paths in tags.items():
        for p in paths:
            parts = [x.strip().lower() for x in p.split("/") if x.strip()]
            if len(parts) == 2:
                rows.append({
                    "tag": tag,
                    "category": parts[0],
                    "subcategory": parts[1],
                    "type": None,
                })
            elif len(parts) >= 3:
                rows.append({
                    "tag": tag,
                    "category": parts[0],
                    "subcategory": parts[1],
                    "type": parts[2],
                })

    if not rows:
        return pd.DataFrame(columns=["tag", "category", "subcategory", "type"])

    return pd.DataFrame(rows)


def _register_df(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> None:
    """
    Replace a DuckDB registered dataframe safely.
    """
    try:
        con.unregister(name)
    except Exception:
        pass
    con.register(name, df)


def _year_filter_sql(year: int | None) -> str:
    if year is None:
        return ""
    return f" AND STRFTIME('%Y', op_date) = '{int(year):04d}'"

# ============================================================
# SECTION F — Years & availability (public helpers)
# ============================================================

def default_data_year(con, root: Path) -> int | None:
    """
    Determine the default year to show in the UI.

    Source of truth:
      1) transactions (op_date)
      2) account_balances metadata (as_of_date)

    Note: retains the existing behavior of calling run_query()
    even though it builds its own connection internally.
    """
    years: list[int] = []

    # 1) transactions
    df = run_query("all_transactions", root)  # root may be Path; run_query tolerates it
    if not df.empty and "op_date" in df.columns:
        dt = pd.to_datetime(df["op_date"], errors="coerce")
        years += dt.dt.year.dropna().astype(int).tolist()

    # 2) account balances meta
    bal_path = root / "_meta" / "account_balances.parquet"
    if bal_path.exists():
        bal = pd.read_parquet(bal_path)
        if "as_of_date" in bal.columns:
            dt = pd.to_datetime(bal["as_of_date"], errors="coerce")
            years += dt.dt.year.dropna().astype(int).tolist()

    return max(years) if years else None


def available_years(con, parquet_root: Path) -> list[int]:
    """
    Available years derived STRICTLY from financial effective dates (op_date).
    """
    try:
        df = con.execute(
            _BASE_CTE + """
            SELECT DISTINCT EXTRACT(year FROM op_date) AS y
            FROM base
            WHERE op_date IS NOT NULL
            """
        ).df()

        years = df["y"].dropna().astype(int).tolist()
        return sorted(set(years), reverse=True)

    except Exception:
        return []


# ============================================================
# SECTION G — Spending queries (DuckDB-first, thin wrappers)
# ============================================================

def total_spending_by_category(con, year: int | None = None):
    """
    Totals by category for true spending only (exclude income).
    Refunds reduce totals.
    """
    return con.execute(_BASE_CTE + f"""
        SELECT
            COALESCE(category,'uncategorized') AS category,
            SUM(amount) AS total_spent,
            ABS(SUM(amount)) AS abs_amount
        FROM base
        WHERE {_SPEND_WHERE}
        {_year_filter_sql(year)}
        GROUP BY category
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_category_subcategory(con, year: int | None = None):
    """
    Totals by category/subcategory for spending only (exclude income).
    """
    return con.execute(_BASE_CTE + f"""
        SELECT
            COALESCE(category,'uncategorized') AS category,
            COALESCE(subcategory,'__default__') AS subcategory,
            SUM(amount) AS total_spent,
            ABS(SUM(amount)) AS abs_amount
        FROM base
        WHERE {_SPEND_WHERE}
        {_year_filter_sql(year)}
        GROUP BY category, subcategory
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_category_subcategory_type(con, year: int | None = None):
    return con.execute(_BASE_CTE + f"""
        SELECT
            COALESCE(category,'uncategorized') AS category,
            COALESCE(subcategory,'__default__') AS subcategory,
            COALESCE("type",'__default__') AS "type",
            SUM(amount) AS total_spent,
            ABS(SUM(amount)) AS abs_amount
        FROM base
        WHERE {_SPEND_WHERE}
        {_year_filter_sql(year)}
        GROUP BY category, subcategory, "type"
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_cardholder(con):
    return con.execute(_BASE_CTE + f"""
        SELECT
            COALESCE(CAST(base.cardholder_name AS VARCHAR), 'unknown') AS cardholder_name,
            SUM(amount) AS total_spent,
            ABS(SUM(amount)) AS abs_amount
        FROM base
        WHERE {_SPEND_WHERE}
        GROUP BY COALESCE(CAST(base.cardholder_name AS VARCHAR), 'unknown')
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_institution(con):
    return con.execute(_BASE_CTE + f"""
        SELECT
            COALESCE(base.institution, base.description, 'unknown') AS institution,
            SUM(amount) AS total_spent,
            ABS(SUM(amount)) AS abs_amount
        FROM base
        WHERE {_SPEND_WHERE}
        GROUP BY COALESCE(base.institution, base.description, 'unknown')
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_month(con):
    return con.execute(_BASE_CTE + f"""
        SELECT
            STRFTIME('%Y-%m', op_date) AS month,
            SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END) -
            SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END) AS total_spent
        FROM base
        WHERE {_SPEND_WHERE}
        GROUP BY STRFTIME('%Y-%m', op_date)
        ORDER BY month
    """).df()


def total_spending_by_tag(con, year: int | None = None):
    tagdf = _tagmap_df()
    if tagdf.empty:
        return pd.DataFrame(columns=["tag", "total_spent", "abs_amount"])
    _register_df(con, "tagmap", tagdf)

    return con.execute(_BASE_CTE + f"""
        SELECT
          tm.tag AS tag,
          SUM(CASE WHEN base.transaction_type='expense' THEN ABS(base.amount) ELSE 0 END) -
          SUM(CASE WHEN base.transaction_type='refund' THEN ABS(base.amount) ELSE 0 END) AS total_spent,
          SUM(ABS(base.amount)) AS abs_amount
        FROM base
        JOIN tagmap tm
          ON lower(base.category) = tm.category
         AND (tm.subcategory IS NULL OR lower(base.subcategory) = tm.subcategory)
         AND (tm."type" IS NULL OR lower(base."type") = tm."type")
        WHERE {_SPEND_WHERE}
        {_year_filter_sql(year)}
        GROUP BY tm.tag
        ORDER BY abs_amount DESC
    """).df()


def total_spending_by_tag_month(con):
    tagdf = _tagmap_df()
    if tagdf.empty:
        return pd.DataFrame(columns=["month", "tag", "total_spent"])
    _register_df(con, "tagmap", tagdf)

    return con.execute(_BASE_CTE + f"""
        SELECT
          STRFTIME('%Y-%m', op_date) AS month,
          tm.tag AS tag,
          SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END) -
          SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END) AS total_spent
        FROM base
        JOIN tagmap tm
          ON lower(base.category) = tm.category
         AND (tm.subcategory IS NULL OR lower(base.subcategory) = tm.subcategory)
         AND (tm."type" IS NULL OR lower(base."type") = tm."type")
        WHERE {_SPEND_WHERE}
        GROUP BY STRFTIME('%Y-%m', base.op_date), tm.tag
        ORDER BY month, tm.tag
    """).df()


def spending_by_month_for_year(con, year: int | None):
    """
    Monthly spending only (exclude income).
    Refunds reduce total spending.
    """
    if year is None:
        return pd.DataFrame(columns=["month", "total_spent"])

    y = int(year)
    return con.execute(_BASE_CTE + f"""
        SELECT
          STRFTIME('%Y-%m', op_date) AS month,
          SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END) -
          SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END) AS total_spent
        FROM base
        WHERE {_SPEND_WHERE}
          AND STRFTIME('%Y', op_date) = '{y:04d}'
        GROUP BY STRFTIME('%Y-%m', op_date)
        ORDER BY month
    """).df()


def spending_for_year_month(con, year: int, month: int):
    ym = f"{int(year):04d}-{int(month):02d}"
    return con.execute(_BASE_CTE + f"""
        SELECT
          COALESCE(CAST(base.cardholder_name AS VARCHAR), 'unknown') AS cardholder_name,
          SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END) -
          SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END) AS total_spent
        FROM base
        WHERE {_SPEND_WHERE}
          AND STRFTIME('%Y-%m', op_date) = '{ym}'
        GROUP BY COALESCE(CAST(base.cardholder_name AS VARCHAR), 'unknown')
        ORDER BY total_spent DESC
    """).df()


def spending_by_currency(con):
    return con.execute(_BASE_CTE + f"""
        SELECT
          COALESCE(currency,'CAD') AS currency,
          SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END) -
          SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END) AS total_spent
        FROM base
        WHERE {_SPEND_WHERE}
        GROUP BY currency
        ORDER BY total_spent DESC
    """).df()


def income_vs_expense_for_year(con, year: int):
    y = int(year)
    return con.execute(_BASE_CTE + f"""
        SELECT
            STRFTIME('%Y-%m', op_date) AS month,

            SUM(
                CASE WHEN transaction_type = 'income'
                     THEN amount
                     ELSE 0
                END
            ) AS income,

            SUM(
                CASE
                    WHEN transaction_type IN ('expense','refund')
                    THEN amount
                    ELSE 0
                END
            ) AS expense

        FROM base
        WHERE {_CASH_FLOW_WHERE}
          AND STRFTIME('%Y', op_date) = '{y:04d}'
        GROUP BY STRFTIME('%Y-%m', op_date)
        ORDER BY month
    """).df()


# ============================================================
# SECTION H — Transaction listings (thin, stable semantics)
# ============================================================

def transactions_by_cardholder(con):
    """
    Return only true transactional records (expense, income, refund),
    excluding NAV snapshots or manual values.
    """
    return con.execute(_BASE_CTE + """
        SELECT *
        FROM base
        WHERE transaction_type IN ('expense','income','refund')
          AND cardholder_name IS NOT NULL
        ORDER BY cardholder_name, op_date
    """).df()


def transactions_by_institution(con):
    return con.execute(_BASE_CTE + """
        SELECT *
        FROM base
        WHERE transaction_type IN ('expense','income','refund')
        ORDER BY institution NULLS LAST, description, op_date
    """).df()


def all_transfers(con):
    return con.execute(_BASE_CTE + """
        SELECT *
        FROM base
        WHERE transaction_type = 'transfer'
        ORDER BY cardholder_name, op_date
    """).df()


def all_transactions(con, parquet_root: Path):
    """
    Transactions (expense/income/refund) + optional manual_value rows,
    plus account_name join if accounts.parquet exists.
    """
    base_df = con.execute(_BASE_CTE + """
        SELECT *
        FROM base
        WHERE transaction_type IN ('expense','income','refund')
    """).df()

    # ─────────────────────────────────────────────
    # Manual values appended as synthetic tx rows
    # ─────────────────────────────────────────────
    man_p = _meta_path(parquet_root, "manual_values.parquet")
    if man_p.exists():
        man_df = con.execute(f"""
            SELECT
              account_id,
              TRY_CAST(operation_date AS DATE) AS op_date,
              description,
              {_sql_normalize_desc("description")} AS description_norm,
              amount,
              'manual_value' AS transaction_type,
              category,
              subcategory,
              type
            FROM read_parquet('{safe_path_literal(man_p)}', union_by_name=true)
        """).df()

        # Ensure union schema stability (match base_df columns)
        for col in base_df.columns:
            if col not in man_df.columns:
                man_df[col] = None
        for col in man_df.columns:
            if col not in base_df.columns:
                base_df[col] = None

        df = pd.concat([base_df, man_df], ignore_index=True)
    else:
        df = base_df

    # ─────────────────────────────────────────────
    # Attach account_name if available
    # ─────────────────────────────────────────────
    acc_p = _meta_path(parquet_root, "accounts.parquet")
    if acc_p.exists():
        acc_df = pd.read_parquet(acc_p)
        if "name" in acc_df.columns:
            acc_df["account_name"] = acc_df["name"]
        elif "institution" in acc_df.columns:
            acc_df["account_name"] = acc_df["institution"]
        else:
            acc_df["account_name"] = acc_df.get("account_id", "")

        acc_df = acc_df[["account_id", "account_name"]].drop_duplicates()
        df = df.merge(acc_df, on="account_id", how="left")
    else:
        df["account_name"] = df.get("account_id")

    return df.sort_values("op_date", ascending=False).reset_index(drop=True)

# ============================================================
# SECTION I — Monthly account balances (core net-worth engine)
# ============================================================

def _monthly_account_balances(con, parquet_root: Path) -> pd.DataFrame:
    """
    Build monthly per-account balances with account metadata attached.

    Output columns:
      account_id, month_start, month, balance,
      account_side, account_class, asset_nature, liquidity_class
    """

    acc_p = _meta_path(parquet_root, "accounts.parquet")
    man_p = _meta_path(parquet_root, "manual_values.parquet")
    nav_p = _meta_path(parquet_root, "nav_snapshots.parquet")
    ab_p  = _meta_path(parquet_root, "account_balances.parquet")

    # ─────────────────────────────────────────────
    # Fast exit if nothing exists
    # ─────────────────────────────────────────────
    has_any = (
        any(parquet_root.glob("*.parquet"))
        or acc_p.exists()
        or man_p.exists()
        or nav_p.exists()
        or ab_p.exists()
    )

    if not has_any:
        return pd.DataFrame(columns=[
            "account_id","month_start","month","balance",
            "account_side","account_class","asset_nature","liquidity_class"
        ])

    # ============================================================
    # A) Accounts metadata (DuckDB view)
    # ============================================================
    if acc_p.exists():
        con.execute(f"""
            CREATE OR REPLACE VIEW acc AS
            SELECT *
            FROM read_parquet('{safe_path_literal(acc_p)}', union_by_name=true)
        """)
    else:
        con.execute("""
            CREATE OR REPLACE VIEW acc AS
            SELECT
              CAST(NULL AS VARCHAR) AS account_id,
              CAST(NULL AS VARCHAR) AS account_side,
              CAST(NULL AS VARCHAR) AS account_class,
              CAST(NULL AS VARCHAR) AS asset_nature,
              CAST(NULL AS VARCHAR) AS liquidity_class
            WHERE 1=0
        """)

    # ============================================================
    # B) Manual values (DuckDB view)
    # ============================================================
    if man_p.exists():
        con.execute(f"""
            CREATE OR REPLACE VIEW mv AS
            SELECT *
            FROM read_parquet('{safe_path_literal(man_p)}', union_by_name=true)
        """)
    else:
        con.execute("""
            CREATE OR REPLACE VIEW mv AS
            SELECT
              CAST(NULL AS VARCHAR) AS account_id,
              CAST(NULL AS DATE)    AS operation_date,
              CAST(NULL AS DOUBLE)  AS amount
            WHERE 1=0
        """)

    # ============================================================
    # C) Transaction monthly deltas (all accounts)
    # ============================================================
    tx_all = con.execute(_BASE_CTE + f"""
        SELECT
          account_id,
          DATE_TRUNC('month', op_date) AS m,
          SUM(amount) AS delta
        FROM base
        WHERE {_ALL_MAIN_WHERE}
        GROUP BY account_id, DATE_TRUNC('month', op_date)
        ORDER BY account_id, m
    """).df()

    if not tx_all.empty:
        tx_all["month_start"] = (
            pd.to_datetime(tx_all["m"])
              .dt.to_period("M")
              .dt.to_timestamp(how="S")
        )
    else:
        tx_all = pd.DataFrame(columns=["account_id","month_start","delta"])

    # ============================================================
    # D) Statement base balances (non-investment only)
    # ============================================================
    if ab_p.exists():
        ab = pd.read_parquet(ab_p)
        if not ab.empty:
            ab["as_of_date"] = pd.to_datetime(ab["as_of_date"], errors="coerce")
            ab = ab.dropna(subset=["as_of_date"])
            ab = ab[ab["account_class"] != "investment"]

            if not ab.empty:
                ab["year"] = ab["as_of_date"].dt.year
                ab = (
                    ab.sort_values(["account_id", "year", "as_of_date"])
                      .groupby(["account_id", "year"], as_index=False)
                      .first()
                )

                ab = ab.rename(columns={"balance": "base_balance"})
                ab["base_as_of_date"] = ab["as_of_date"]
                ab["base_month_start"] = (
                    ab["base_as_of_date"]
                      .dt.to_period("M")
                      .dt.to_timestamp(how="S")
                )

                ab_first = ab[[
                    "account_id","year",
                    "base_as_of_date","base_balance","base_month_start"
                ]].copy()
            else:
                ab_first = pd.DataFrame(columns=[
                    "account_id","year",
                    "base_as_of_date","base_balance","base_month_start"
                ])
        else:
            ab_first = pd.DataFrame(columns=[
                "account_id","year",
                "base_as_of_date","base_balance","base_month_start"
            ])
    else:
        ab_first = pd.DataFrame(columns=[
            "account_id","year",
            "base_as_of_date","base_balance","base_month_start"
        ])

    # ============================================================
    # E) Transaction deltas after base (base accounts only)
    # ============================================================
    if not ab_first.empty:
        try:
            con.unregister("ab_first")
        except Exception:
            pass
        con.register("ab_first", ab_first)

        tx_base = con.execute(_BASE_CTE + f"""
            SELECT
              b.account_id,
              DATE_TRUNC('month', op_date) AS m,
              ab.year,
              ab.base_as_of_date,
              ab.base_balance,
              SUM(b.amount) AS delta
            FROM base b
            JOIN ab_first ab
              ON b.account_id = ab.account_id
             AND STRFTIME('%Y', op_date) = CAST(ab.year AS VARCHAR)
            WHERE {_ALL_MAIN_WHERE}
              AND op_date > ab.base_as_of_date
            GROUP BY
              b.account_id,
              DATE_TRUNC('month', op_date),
              ab.year,
              ab.base_as_of_date,
              ab.base_balance
            ORDER BY b.account_id, m
        """).df()
    else:
        tx_base = pd.DataFrame(columns=[
            "account_id","m","year",
            "base_as_of_date","base_balance","delta"
        ])

    if not tx_base.empty:
        tx_base["month_start"] = (
            pd.to_datetime(tx_base["m"])
              .dt.to_period("M")
              .dt.to_timestamp(how="S")
        )
    else:
        tx_base = pd.DataFrame(columns=[
            "account_id","month_start","year",
            "base_as_of_date","base_balance","delta"
        ])

    # ============================================================
    # F) Apply base + deltas (authoritative algorithm)
    # ============================================================
    def _apply_base_group(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("month_start").copy()

        base_date = g["base_as_of_date"].iloc[0]
        base_balance = float(g["base_balance"].iloc[0])
        base_month = base_date.to_period("M").to_timestamp(how="S")

        if base_month not in g["month_start"].values:
            g = pd.concat([
                g,
                pd.DataFrame([{
                    "account_id": g["account_id"].iloc[0],
                    "year": g["year"].iloc[0],
                    "month_start": base_month,
                    "base_as_of_date": base_date,
                    "base_balance": base_balance,
                    "delta": 0.0,
                }])
            ], ignore_index=True).sort_values("month_start")

        g["cs"] = g["delta"].cumsum()

        cs_candidates = g.loc[g["month_start"] < base_month, "cs"]
        cs_at_base = cs_candidates.max() if not cs_candidates.empty else 0.0
        cs_at_base = float(cs_at_base or 0.0)

        g["ytd_delta"] = g["cs"] - cs_at_base
        g.loc[g["month_start"] == base_month, "ytd_delta"] = 0.0

        g["balance"] = base_balance + g["ytd_delta"]

        return g[["account_id","month_start","balance"]]

    if not tx_base.empty:
        base_balances = (
            tx_base
            .groupby(["account_id","year"], group_keys=False)
            .apply(_apply_base_group)
            .reset_index(drop=True)
        )
    else:
        base_balances = pd.DataFrame(columns=["account_id","month_start","balance"])

    # Base-only rows (no tx after base)
    if not ab_first.empty:
        base_rows = ab_first.rename(columns={
            "base_month_start": "month_start",
            "base_balance": "balance"
        })[["account_id","month_start","balance"]]

        if not base_balances.empty:
            merged = base_rows.merge(
                base_balances[["account_id","month_start"]],
                on=["account_id","month_start"],
                how="left",
                indicator=True
            )
            base_rows = merged[merged["_merge"] == "left_only"][
                ["account_id","month_start","balance"]
            ]

        frames = []
        if not base_balances.empty:
            frames.append(base_balances)
        if not base_rows.empty:
            frames.append(base_rows)

        if frames:
            base_balances = pd.concat(frames, ignore_index=True)
        else:
            base_balances = pd.DataFrame(columns=["account_id", "month_start", "balance"])

    # ============================================================
    # G) Fallback balances (accounts without base)
    # ============================================================
    base_accounts = set(ab_first["account_id"]) if not ab_first.empty else set()
    tx_no_base = tx_all[~tx_all["account_id"].isin(base_accounts)].copy()

    if not tx_no_base.empty:
        tx_no_base = (
            tx_no_base.sort_values("month_start")
            .groupby("account_id", group_keys=False)
            .apply(
                lambda g: (
                    g.assign(
                        balance=g["delta"].cumsum(),
                        account_id=g.name,  # 👈 explicitly restore group key
                    )
                ),
                include_groups=False
            )
            .reset_index(drop=True)
        )[["account_id", "month_start", "balance"]]

    else:
        tx_no_base = pd.DataFrame(columns=["account_id","month_start","balance"])

    # ============================================================
    # H) Manual values (forward-filled monthly)
    # ============================================================
    mv_df = con.execute("""
        SELECT
          account_id,
          TRY_CAST(operation_date AS DATE) AS d,
          CAST(amount AS DOUBLE) AS amount
        FROM mv
        WHERE account_id IS NOT NULL AND operation_date IS NOT NULL
        ORDER BY account_id, d
    """).df()

    if not mv_df.empty:
        mv_df = (
            mv_df.dropna(subset=["d"])
                 .sort_values(["account_id","d"])
                 .groupby("account_id", group_keys=False)
                 .apply(
                     lambda g: (
                         g.set_index("d")
                          .resample("ME")
                          .ffill()
                          .assign(account_id=g.name)
                     ),
                     include_groups=False
                 )
                 .reset_index()
        )

        mv_df["month_start"] = (
            pd.to_datetime(mv_df["d"])
              .dt.to_period("M")
              .dt.to_timestamp(how="S")
        )

        mv_df = mv_df.rename(columns={"amount":"balance"})[
            ["account_id","month_start","balance"]
        ]
    else:
        mv_df = pd.DataFrame(columns=["account_id","month_start","balance"])

    # ============================================================
    # I) NAV snapshots (authoritative)
    # ============================================================
    if nav_p.exists():
        nav_df = pd.read_parquet(nav_p)
        if not nav_df.empty:
            if "nav_date" in nav_df.columns:
                nav_df = nav_df.rename(columns={
                    "nav_date": "operation_date",
                    "nav_value": "balance"
                })
            elif "nav" in nav_df.columns:
                nav_df = nav_df.rename(columns={"nav":"balance"})

            nav_df["operation_date"] = pd.to_datetime(
                nav_df["operation_date"], errors="coerce"
            )
            nav_df = nav_df.dropna(subset=["operation_date"])

            nav_df["month_start"] = (
                nav_df["operation_date"]
                  .dt.to_period("M")
                  .dt.to_timestamp(how="S")
            )

            nav_df = nav_df[["account_id","month_start","balance"]].copy()
        else:
            nav_df = pd.DataFrame(columns=["account_id","month_start","balance"])
    else:
        nav_df = pd.DataFrame(columns=["account_id","month_start","balance"])

    # ============================================================
    # J) Combine all sources
    # ============================================================
    frames = [df for df in (base_balances, tx_no_base, mv_df, nav_df) if not df.empty]
    if not frames:
        return pd.DataFrame(columns=[
            "account_id","month_start","month","balance",
            "account_side","account_class","asset_nature","liquidity_class"
        ])

    balances = pd.concat(frames, ignore_index=True)

    # ============================================================
    # K) Forward-fill missing months
    # ============================================================
    month_min = balances["month_start"].min()
    month_max = balances["month_start"].max()

    month_index = pd.period_range(
        month_min.to_period("M"),
        month_max.to_period("M"),
        freq="M"
    ).to_timestamp()

    balances = (
        balances
        .groupby("account_id", group_keys=False)
        .apply(
            lambda g: (
                g.set_index("month_start")
                 .sort_index()
                 .reindex(month_index, method="ffill")
                 .rename_axis("month_start")
                 .reset_index()
                 .assign(account_id=g.name)
            ),
            include_groups=False
        )
        .reset_index(drop=True)
    )

    # ============================================================
    # L) Attach account metadata
    # ============================================================
    acc_df = con.execute("""
        SELECT
          account_id,
          account_side,
          account_class,
          asset_nature,
          liquidity_class
        FROM acc
    """).df()

    balances = balances.merge(acc_df, on="account_id", how="left")
    balances["month"] = pd.to_datetime(balances["month_start"]).dt.strftime("%Y-%m")

    return balances[[
        "account_id","month_start","month","balance",
        "account_side","account_class","asset_nature","liquidity_class"
    ]]


# ============================================================
# SECTION J — Net worth aggregations (built on monthly balances)
# ============================================================

def net_worth_timeseries(con, parquet_root: Path) -> pd.DataFrame:
    balances = _monthly_account_balances(con, parquet_root)

    if balances.empty:
        return pd.DataFrame(columns=["month", "assets", "liabilities", "net_worth"])

    assets_df = (
        balances[balances["account_side"] == "asset"]
        .groupby("month", as_index=False)["balance"]
        .sum()
        .rename(columns={"balance": "assets"})
    )

    liabs_df = (
        balances[balances["account_side"] == "liability"]
        .groupby("month", as_index=False)["balance"]
        .sum()
        .rename(columns={"balance": "liabilities_raw"})
    )

    out = pd.merge(assets_df, liabs_df, on="month", how="outer").fillna(0.0)
    out["liabilities"] = -out["liabilities_raw"]
    out["net_worth"] = out["assets"] - out["liabilities"]

    return out.sort_values("month")[["month", "assets", "liabilities", "net_worth"]]


def net_worth_by_liquidity_timeseries(con, parquet_root: Path) -> pd.DataFrame:
    balances = _monthly_account_balances(con, parquet_root)

    if balances.empty:
        return pd.DataFrame(columns=[
            "month", "liquid_assets", "illiquid_assets", "liabilities", "net_worth"
        ])

    assets = balances[balances["account_side"] == "asset"]
    liabs = balances[balances["account_side"] == "liability"]

    liquid = (
        assets[assets["liquidity_class"] == "liquid"]
        .groupby("month", as_index=False)["balance"]
        .sum()
        .rename(columns={"balance": "liquid_assets"})
    )

    illiquid = (
        assets[assets["liquidity_class"] == "illiquid"]
        .groupby("month", as_index=False)["balance"]
        .sum()
        .rename(columns={"balance": "illiquid_assets"})
    )

    liabilities = (
        liabs
        .groupby("month", as_index=False)["balance"]
        .sum()
        .rename(columns={"balance": "liabilities_raw"})
    )

    out = (
        liquid
        .merge(illiquid, on="month", how="outer")
        .merge(liabilities, on="month", how="outer")
        .fillna(0.0)
    )

    out["liabilities"] = -out["liabilities_raw"]
    out["net_worth"] = out["liquid_assets"] + out["illiquid_assets"] - out["liabilities"]

    return out.sort_values("month")[
        ["month", "liquid_assets", "illiquid_assets", "liabilities", "net_worth"]
    ]


def net_worth_by_asset_nature_timeseries(con, parquet_root: Path) -> pd.DataFrame:
    balances = _monthly_account_balances(con, parquet_root)

    if balances.empty:
        return pd.DataFrame(columns=[
            "month",
            "financial_assets",
            "real_assets",
            "intangible_assets",
            "liabilities",
            "net_worth",
        ])

    rows = []
    for month, b in balances.groupby("month"):
        financial = b.loc[
            (b["account_side"] == "asset") & (b["asset_nature"] == "financial"),
            "balance"
        ].sum()

        real = b.loc[
            (b["account_side"] == "asset") & (b["asset_nature"] == "real"),
            "balance"
        ].sum()

        intangible = b.loc[
            (b["account_side"] == "asset") & (b["asset_nature"] == "intangible"),
            "balance"
        ].sum()

        liabilities = -b.loc[b["account_side"] == "liability", "balance"].sum()

        rows.append({
            "month": month,
            "financial_assets": financial,
            "real_assets": real,
            "intangible_assets": intangible,
            "liabilities": liabilities,
            "net_worth": financial + real + intangible - liabilities,
        })

    return pd.DataFrame(rows).sort_values("month")


# ============================================================
# SECTION K — Asset matrix & liquid financial assets (non-FX)
# ============================================================

def asset_matrix_latest_snapshot(con, parquet_root: Path) -> pd.DataFrame:
    balances = _monthly_account_balances(con, parquet_root)

    if balances.empty:
        return pd.DataFrame(columns=["asset_nature", "liquidity_class", "balance"])

    latest_month = balances["month"].max()

    out = (
        balances[
            (balances["month"] == latest_month) &
            (balances["account_side"] == "asset")
        ]
        .groupby(["asset_nature", "liquidity_class"], as_index=False)["balance"]
        .sum()
        .sort_values(["asset_nature", "liquidity_class"])
    )

    return out


def liquid_financial_assets_timeseries(con, parquet_root: Path) -> pd.DataFrame:
    """
    Monthly liquid financial components used for the Net Worth dashboard.

    Components:
      - cash        (asset, liquid)
      - investment  (asset, liquid)
      - liability   (liability)

    Output:
      month, account_class, balance   (balance always positive)
    """
    df = _monthly_account_balances(con, parquet_root)

    if df.empty:
        return pd.DataFrame(columns=["month", "account_class", "balance"])

    assets = (
        df[
            (df["account_side"] == "asset") &
            (df["liquidity_class"] == "liquid") &
            (df["account_class"].isin(["cash", "investment"]))
        ]
        .groupby(["month", "account_class"], as_index=False)["balance"]
        .sum()
    )

    liabilities = (
        df[df["account_side"] == "liability"]
        .groupby("month", as_index=False)["balance"]
        .sum()
        .assign(account_class="liability")
    )

    liabilities["balance"] = liabilities["balance"].abs()

    out = pd.concat([assets, liabilities], ignore_index=True)
    out = out.replace([np.nan, np.inf, -np.inf], None)

    return out.sort_values(["month", "account_class"])


# ============================================================
# SECTION L — Account currency resolution
# ============================================================

def _account_currency_map(
    con,
    parquet_root: Path,
    default_currency: str,
) -> dict[str, str]:
    """
    Determine best-effort base currency per account_id.

    Priority:
      1) accounts.parquet → base_currency | currency
      2) transaction frequency inference
      3) default_currency
    """
    default_currency = (default_currency or "CAD").upper().strip()
    mapping: dict[str, str] = {}

    acc_p = _meta_path(parquet_root, "accounts.parquet")

    # 1) Explicit account currency
    if acc_p.exists():
        try:
            acc_df = pd.read_parquet(acc_p)
            if "account_id" in acc_df.columns:
                ccol = None
                if "base_currency" in acc_df.columns:
                    ccol = "base_currency"
                elif "currency" in acc_df.columns:
                    ccol = "currency"

                if ccol:
                    tmp = acc_df[["account_id", ccol]].copy()
                    tmp["account_id"] = tmp["account_id"].astype(str)
                    tmp[ccol] = tmp[ccol].astype(str).str.upper().str.strip()
                    tmp = tmp[tmp[ccol].str.len() == 3]
                    mapping.update(dict(zip(tmp["account_id"], tmp[ccol])))
        except Exception:
            pass

    # 2) Infer from transactions
    try:
        tx_cur = con.execute(_BASE_CTE + """
            SELECT
              CAST(account_id AS VARCHAR) AS account_id,
              COALESCE(NULLIF(upper(trim(currency)), ''), ?) AS currency,
              COUNT(*) AS n
            FROM base
            WHERE account_id IS NOT NULL
            GROUP BY 1, 2
        """, [default_currency]).df()

        if not tx_cur.empty:
            tx_cur = (
                tx_cur[tx_cur["currency"].str.len() == 3]
                .sort_values(["account_id", "n"], ascending=[True, False])
                .groupby("account_id", as_index=False)
                .first()
            )

            for _, r in tx_cur.iterrows():
                if r["account_id"] not in mapping:
                    mapping[str(r["account_id"])] = str(r["currency"])
    except Exception:
        pass

    return mapping


# ============================================================
# SECTION M — FX conversion helper (balances → balance_fx)
# ============================================================

def _fx_convert_balances(
    balances: pd.DataFrame,
    con,
    parquet_root: Path,
    *,
    display_currency: str,
) -> pd.DataFrame:
    """
    Attach balance_fx to balances using per-account base currency.
    """
    acct_ccy = _account_currency_map(
        con,
        parquet_root,
        default_currency=display_currency,
    )

    out = balances.copy()
    out["currency"] = out["account_id"].map(acct_ccy).fillna(display_currency)
    out["currency"] = out["currency"].astype(str).str.upper().str.strip()

    out["valuation_date"] = (
        pd.to_datetime(out["month_start"]) + pd.offsets.MonthEnd(0)
    ).dt.date

    fx_cache: dict[tuple, float] = {}

    def _convert(row):
        bal = row["balance"]
        fc = row["currency"]
        d = row["valuation_date"]

        if bal is None or pd.isna(bal) or fc == display_currency:
            return float(bal or 0.0)

        key = (d, fc, display_currency)
        if key not in fx_cache:
            fx_cache[key] = resolve_fx_rate(
                valuation_date=d,
                from_currency=fc,
                to_currency=display_currency,
                parquet_root=parquet_root,
            )[0]

        return float(bal) * fx_cache[key]

    out["balance_fx"] = out.apply(_convert, axis=1)
    return out


# ============================================================
# SECTION N — FX-aware net worth (strict, diagnostic)
# ============================================================

def net_worth_timeseries_fx(
    con,
    parquet_root: Path,
    *,
    display_currency: str | None = None,
    supported_currencies: set[str] | None = None,
):
    """
    FX-aware net worth.

    Returns:
      (df, diagnostics)
    """
    settings = config.load_settings()
    disp = (display_currency or settings.get("display_currency") or "CAD").upper().strip()

    supported_currencies = {
        str(c).upper().strip()
        for c in (supported_currencies or settings.get("fx_supported_currencies") or [])
        if isinstance(c, str)
    }

    balances = _monthly_account_balances(con, parquet_root)
    if balances.empty:
        empty = pd.DataFrame(columns=["month", "assets", "liabilities", "net_worth"])
        return empty, {
            "display_currency": disp,
            "supported_currencies": sorted(supported_currencies),
            "currencies_seen": [],
            "pairs_used": {},
            "fallback_used_any": False,
        }

    acct_ccy = _account_currency_map(con, parquet_root, default_currency=disp)
    balances = balances.copy()
    balances["currency"] = balances["account_id"].map(acct_ccy).fillna(disp)

    currencies_used = set(balances["currency"].unique())
    validate_fx_scope(
        currencies_used=currencies_used,
        display_currency=disp,
        supported_currencies=supported_currencies,
    )

    balances["valuation_date"] = (
        pd.to_datetime(balances["month_start"]) + pd.offsets.MonthEnd(0)
    ).dt.date

    fx_cache: dict[tuple, tuple] = {}
    pairs_used: dict[str, dict] = {}
    fallback_used_any = False

    def _fx_rate(d, fc, tc):
        nonlocal fallback_used_any
        key = (d, fc, tc)

        if key in fx_cache:
            return fx_cache[key]

        if fc == tc:
            meta = {"fx_date_used": d, "fallback_used": False, "source": "identity"}
            fx_cache[key] = (1.0, meta)
            return fx_cache[key]

        fx_anchor = first_business_day_of_month(d)

        ensure_fx_rate(
            date=fx_anchor,
            from_currency=fc,
            to_currency=tc,
            parquet_root=parquet_root,
        )

        rate, meta = resolve_fx_rate(
            valuation_date=d,
            from_currency=fc,
            to_currency=tc,
            parquet_root=parquet_root,
        )

        fx_cache[key] = (rate, meta)

        pair_key = f"{fc}→{tc}"
        pairs_used[pair_key] = {
            "fx_date_used": meta.get("fx_date_used"),
            "fallback_used": bool(meta.get("fallback_used", False)),
            "source": meta.get("source"),
        }

        if meta.get("fallback_used"):
            fallback_used_any = True

        return fx_cache[key]

    converted = []
    for _, r in balances.iterrows():
        bal = r["balance"]
        fc = r["currency"]
        d = r["valuation_date"]

        if bal is None or pd.isna(bal):
            converted.append(np.nan)
            continue

        if fc == disp:
            converted.append(float(bal))
            continue

        rate, _ = _fx_rate(d, fc, disp)
        converted.append(float(bal) * float(rate))

    balances["balance_fx"] = converted

    assets = (
        balances[balances["account_side"] == "asset"]
        .groupby("month", as_index=False)["balance_fx"]
        .sum()
        .rename(columns={"balance_fx": "assets"})
    )

    liabs = (
        balances[balances["account_side"] == "liability"]
        .groupby("month", as_index=False)["balance_fx"]
        .sum()
        .rename(columns={"balance_fx": "liabilities_raw"})
    )

    out = pd.merge(assets, liabs, on="month", how="outer").fillna(0.0)
    out["liabilities"] = -out["liabilities_raw"]
    out["net_worth"] = out["assets"] - out["liabilities"]

    out = out.sort_values("month")[["month", "assets", "liabilities", "net_worth"]]
    out[["assets", "liabilities", "net_worth"]] = out[[
        "assets", "liabilities", "net_worth"
    ]].round(2)

    diagnostics = {
        "display_currency": disp,
        "supported_currencies": sorted(supported_currencies),
        "currencies_seen": sorted(currencies_used),
        "pairs_used": pairs_used,
        "fallback_used_any": fallback_used_any,
        "rows_converted": int((balances["currency"] != disp).sum()),
    }

    return out, diagnostics


# ============================================================
# SECTION O — FX-aware asset & liquid snapshots
# ============================================================

def asset_matrix_latest_snapshot_fx(con, parquet_root: Path) -> pd.DataFrame:
    settings = config.load_settings()
    disp = settings.get("display_currency", "CAD")

    balances = _monthly_account_balances(con, parquet_root)
    if balances.empty:
        return balances

    balances = _fx_convert_balances(
        balances,
        con,
        parquet_root,
        display_currency=disp,
    )

    latest_month = balances["month"].max()

    out = (
        balances[
            (balances["month"] == latest_month) &
            (balances["account_side"] == "asset")
        ]
        .groupby(["asset_nature", "liquidity_class"], as_index=False)["balance_fx"]
        .sum()
        .rename(columns={"balance_fx": "balance"})
        .sort_values(["asset_nature", "liquidity_class"])
    )

    return out


def liquid_financial_assets_timeseries_fx(con, parquet_root: Path) -> pd.DataFrame:
    settings = config.load_settings()
    disp = settings.get("display_currency", "CAD")

    balances = _monthly_account_balances(con, parquet_root)
    if balances.empty:
        return balances

    balances = _fx_convert_balances(
        balances,
        con,
        parquet_root,
        display_currency=disp,
    )

    assets = (
        balances[
            (balances["account_side"] == "asset") &
            (balances["liquidity_class"] == "liquid") &
            (balances["account_class"].isin(["cash", "investment"]))
        ]
        .groupby(["month", "account_class"], as_index=False)["balance_fx"]
        .sum()
        .rename(columns={"balance_fx": "balance"})
    )

    liabilities = (
        balances[balances["account_side"] == "liability"]
        .groupby("month", as_index=False)["balance_fx"]
        .sum()
        .rename(columns={"balance_fx": "balance"})
        .assign(account_class="liability")
    )

    liabilities["balance"] = liabilities["balance"].abs()

    return (
        pd.concat([assets, liabilities], ignore_index=True)
        .sort_values(["month", "account_class"])
    )


# ============================================================
# SECTION P — Accounts snapshots (latest per account)
# ============================================================

def net_worth_accounts_snapshot(con, parquet_root: Path) -> pd.DataFrame:
    """
    Returns the latest known balance and date per account,
    combining (by priority):
      1) Statement-provided balances (_meta/account_balances.parquet)
      2) Manual values (_meta/manual_values.parquet)
      3) NAV snapshots (_meta/nav_snapshots.parquet)
      4) Transaction-derived cumulative balances
    Used by /accounts page.
    """
    acc_p = _meta_path(parquet_root, "accounts.parquet")
    man_p = _meta_path(parquet_root, "manual_values.parquet")
    nav_p = _meta_path(parquet_root, "nav_snapshots.parquet")
    ab_p  = _meta_path(parquet_root, "account_balances.parquet")

    acc_df = (
        pd.read_parquet(acc_p)
        if acc_p.exists()
        else pd.DataFrame(columns=["account_id", "account_side", "account_class", "name"])
    )

    balances: list[pd.DataFrame] = []

    # 1️⃣ Statement balances (authoritative)
    if ab_p.exists():
        ab = pd.read_parquet(ab_p)
        if not ab.empty:
            ab = ab.rename(columns={"as_of_date": "latest_date"})
            ab["latest_date"] = pd.to_datetime(ab["latest_date"], errors="coerce")
            ab = ab[["account_id", "latest_date", "balance"]].copy()
            ab["source"] = "statement"
            balances.append(ab)

    # 2️⃣ Transactions
    tx_files = [
        p for p in parquet_root.rglob("*.parquet")
        if "_meta" not in str(p)
        and p.name not in {
            "manual_values.parquet",
            "nav_snapshots.parquet",
            "account_balances.parquet",
        }
    ]

    if tx_files:
        files_sql = ", ".join(f"'{safe_path_literal(p)}'" for p in tx_files)
        tx_df = con.execute(f"""
            SELECT
                account_id,
                TRY_CAST(operation_date AS DATE) AS op_date,
                SUM(amount) OVER (
                    PARTITION BY account_id
                    ORDER BY TRY_CAST(operation_date AS DATE)
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS balance,
                MAX(TRY_CAST(operation_date AS DATE))
                  OVER (PARTITION BY account_id) AS latest_date
            FROM read_parquet([{files_sql}], union_by_name=true)
            WHERE transaction_type IN ('expense','income','refund','transfer')
        """).df()

        tx_latest = (
            tx_df.sort_values(["account_id", "op_date"])
                 .groupby("account_id", as_index=False)
                 .tail(1)[["account_id", "latest_date", "balance"]]
        )
        tx_latest["source"] = "transactions"
        balances.append(tx_latest)

    # 3️⃣ Manual values
    if man_p.exists():
        mv = pd.read_parquet(man_p)
        if not mv.empty:
            mv["operation_date"] = pd.to_datetime(mv["operation_date"], errors="coerce")
            mv_latest = (
                mv.sort_values(["account_id", "operation_date"])
                  .groupby("account_id", as_index=False)
                  .tail(1)[["account_id", "operation_date", "amount"]]
            )
            mv_latest = mv_latest.rename(columns={
                "operation_date": "latest_date",
                "amount": "balance"
            })
            mv_latest["source"] = "manual"
            balances.append(mv_latest)

    # 4️⃣ NAV snapshots
    if nav_p.exists():
        nav = pd.read_parquet(nav_p)
        if not nav.empty:
            nav["operation_date"] = pd.to_datetime(
                nav.get("operation_date", nav.get("nav_date")),
                errors="coerce"
            )
            if "nav_value" in nav.columns:
                nav["balance"] = nav["nav_value"]
            elif "nav" in nav.columns:
                nav["balance"] = nav["nav"]

            nav_latest = (
                nav.sort_values(["account_id", "operation_date"])
                   .groupby("account_id", as_index=False)
                   .tail(1)[["account_id", "operation_date", "balance"]]
            )
            nav_latest = nav_latest.rename(columns={"operation_date": "latest_date"})
            nav_latest["source"] = "nav"
            balances.append(nav_latest)

    if not balances:
        return acc_df.assign(balance=0.0, latest_date=pd.NaT)

    merged = pd.concat(balances, ignore_index=True)
    merged["latest_date"] = pd.to_datetime(merged["latest_date"], errors="coerce")

    source_rank = {"statement": 4, "manual": 3, "nav": 2, "transactions": 1}
    merged["rank"] = merged["source"].map(source_rank).fillna(0)

    merged = (
        merged.sort_values(["account_id", "rank", "latest_date"])
              .groupby("account_id", as_index=False)
              .tail(1)
    )

    out = acc_df.merge(merged, on="account_id", how="left")
    out["balance"] = pd.to_numeric(out["balance"], errors="coerce").fillna(0.0)
    out["latest_date"] = pd.to_datetime(out["latest_date"], errors="coerce")

    return out


def net_worth_accounts_snapshot_fx(con, parquet_root: Path) -> pd.DataFrame:
    settings = config.load_settings()
    disp = settings.get("display_currency", "CAD")

    balances = _monthly_account_balances(con, parquet_root)
    if balances.empty:
        return balances

    balances = _fx_convert_balances(
        balances,
        con,
        parquet_root,
        display_currency=disp,
    )

    latest = (
        balances.sort_values(["account_id", "month_start"])
                .groupby("account_id", as_index=False)
                .tail(1)
    )

    acc_p = _meta_path(parquet_root, "accounts.parquet")
    if not acc_p.exists():
        return pd.DataFrame()

    acc_df = pd.read_parquet(acc_p)

    out = acc_df.merge(
        latest[["account_id", "balance_fx", "month"]],
        on="account_id",
        how="left"
    )

    out = out.rename(columns={
        "balance_fx": "balance",
        "month": "latest_month"
    })

    out["balance"] = pd.to_numeric(out["balance"], errors="coerce").fillna(0.0)
    return out


# ============================================================
# Public convenience wrapper (UI compatibility)
# ============================================================

def run_net_worth_fx(parquet_path: str):
    """
    Run FX-aware net worth and return (df, diagnostics).

    Compatibility wrapper used by UI routes.
    """
    settings = config.load_settings()
    root = Path(parquet_path).resolve()

    con = build_txn_connection(root)
    try:
        return net_worth_timeseries_fx(
            con,
            root,
            display_currency=settings.get("display_currency", "CAD"),
            supported_currencies=set(settings.get("fx_supported_currencies") or []),
        )
    finally:
        try:
            con.close()
        except Exception:
            pass


# ============================================================
# SECTION Q — Public query registry
# ============================================================

QUERY_MAP = {
    "total_spending_by_category": total_spending_by_category,
    "total_spending_by_category_subcategory": total_spending_by_category_subcategory,
    "total_spending_by_category_subcategory_type": total_spending_by_category_subcategory_type,
    "total_spending_by_tag": total_spending_by_tag,
    "total_spending_by_tag_month": total_spending_by_tag_month,
    "total_spending_by_cardholder": total_spending_by_cardholder,
    "total_spending_by_institution": total_spending_by_institution,
    "total_spending_by_month": total_spending_by_month,
    "transactions_by_cardholder": transactions_by_cardholder,
    "transactions_by_institution": transactions_by_institution,
    "all_transactions": all_transactions,
    "all_transfers": all_transfers,
    "spending_by_month_for_year": spending_by_month_for_year,
    "spending_for_year_month": spending_for_year_month,
    "spending_by_currency": spending_by_currency,
    "net_worth_timeseries": net_worth_timeseries,
    "net_worth_by_liquidity_timeseries": net_worth_by_liquidity_timeseries,
    "net_worth_by_asset_nature_timeseries": net_worth_by_asset_nature_timeseries,
    "asset_matrix_latest_snapshot": asset_matrix_latest_snapshot,
    "asset_matrix_latest_snapshot_fx": asset_matrix_latest_snapshot_fx,
    "liquid_financial_assets_timeseries": liquid_financial_assets_timeseries,
    "liquid_financial_assets_timeseries_fx": liquid_financial_assets_timeseries_fx,
    "net_worth_timeseries_fx": net_worth_timeseries_fx,
    "net_worth_accounts_snapshot": net_worth_accounts_snapshot,
    "net_worth_accounts_snapshot_fx": net_worth_accounts_snapshot_fx,
    "income_vs_expense_for_year": income_vs_expense_for_year,
    "available_years": available_years,
}


def available_years_query(con, parquet_root: Path) -> pd.DataFrame:
    return pd.DataFrame({"year": available_years(con, parquet_root)})


# ============================================================
# SECTION R — DuckDB connection builder
# ============================================================

def build_txn_connection(parquet_root: Path) -> duckdb.DuckDBPyConnection:
    """
    Build an in-memory DuckDB connection with `df` view
    containing ONLY transactional parquet files.
    """
    con = duckdb.connect(database=":memory:")

    all_files = collect_parquet_files(parquet_root)

    def _is_txn_file(p: Path) -> bool:
        if "_meta" in p.parts:
            return False
        return p.name not in {
            "account_balances.parquet",
            "accounts.parquet",
            "statement_sources.parquet",
            "manual_values.parquet",
            "nav_snapshots.parquet",
        }

    files = [p for p in all_files if _is_txn_file(p)]
    LOG.debug("build_txn_connection files=%d", len(files))

    if not files:
        con.execute(f"CREATE OR REPLACE VIEW df AS {build_scan_sql([])}")
    else:
        files_sql = ", ".join(f"'{safe_path_literal(p)}'" for p in files)
        con.execute(
            f"CREATE OR REPLACE VIEW df AS "
            f"SELECT * FROM read_parquet([{files_sql}], union_by_name=true)"
        )

    # ─────────────────────────────────────────────
    # Register category semantic flags (once)
    # ─────────────────────────────────────────────
    _register_category_flags(con)
    return con


# ============================================================
# SECTION S — Unified query dispatcher
# ============================================================

def run_query(name: str, parquet_path: str, **kwargs) -> pd.DataFrame:
    parquet_root = Path(parquet_path).resolve() if parquet_path else Path(".").resolve()
    con = build_txn_connection(parquet_root)

    try:
        if name == "spending_by_month_for_year":
            return spending_by_month_for_year(con, kwargs["year"])
        if name == "spending_for_year_month":
            return spending_for_year_month(con, kwargs["year"], kwargs["month"])
        if name == "income_vs_expense_for_year":
            return income_vs_expense_for_year(con, kwargs["year"])
        if name == "total_spending_by_category":
            return total_spending_by_category(con, kwargs.get("year"))
        if name == "total_spending_by_category_subcategory":
            return total_spending_by_category_subcategory(con, kwargs.get("year"))
        if name == "total_spending_by_category_subcategory_type":
            return total_spending_by_category_subcategory_type(con, kwargs.get("year"))
        if name == "total_spending_by_tag":
            return total_spending_by_tag(con, kwargs.get("year"))

        if name == "net_worth_timeseries":
            return net_worth_timeseries(con, parquet_root)
        if name == "net_worth_by_liquidity_timeseries":
            return net_worth_by_liquidity_timeseries(con, parquet_root)
        if name == "net_worth_by_asset_nature_timeseries":
            return net_worth_by_asset_nature_timeseries(con, parquet_root)
        if name == "asset_matrix_latest_snapshot":
            return asset_matrix_latest_snapshot(con, parquet_root)
        if name == "asset_matrix_latest_snapshot_fx":
            return asset_matrix_latest_snapshot_fx(con, parquet_root)
        if name == "liquid_financial_assets_timeseries":
            return liquid_financial_assets_timeseries(con, parquet_root)
        if name == "liquid_financial_assets_timeseries_fx":
            return liquid_financial_assets_timeseries_fx(con, parquet_root)
        if name == "net_worth_accounts_snapshot":
            return net_worth_accounts_snapshot(con, parquet_root)
        if name == "net_worth_accounts_snapshot_fx":
            return net_worth_accounts_snapshot_fx(con, parquet_root)
        if name == "all_transactions":
            return all_transactions(con, parquet_root)
        if name == "available_years":
            return available_years_query(con, parquet_root)

        func = QUERY_MAP.get(name)
        if not func:
            raise ValueError(f"Unknown query {name}")

        return func(con)

    finally:
        try:
            con.close()
        except Exception:
            pass
