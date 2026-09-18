# app/services/manual_values.py
from __future__ import annotations
from pathlib import Path
import pandas as pd
from .normalize_parquet import _meta_path, _ensure_dir


def values_path(base: Path) -> Path:
    """Return the full path to the manual values Parquet file."""
    return _meta_path(base, "manual_values.parquet")


def _expected_columns() -> list[str]:
    """Expected schema for manual values."""
    return [
        "account_id",
        "operation_date",
        "month",
        "amount",
        "description",
        "transaction_id",
        "transaction_type",
        "currency",
        "category",
        "subcategory",
        "type",
        "fuzzy_score",
    ]


def load_values(base: Path) -> pd.DataFrame:
    """
    Load manual values into a DataFrame with normalized schema:
    account_id, operation_date (YYYY-MM-DD), month (YYYY-MM), amount, description, etc.
    Raises if schema is invalid.
    """
    p = values_path(base)
    if not p.exists():
        return pd.DataFrame(columns=_expected_columns())

    df = pd.read_parquet(p)

    # Backward compatibility: rename 'date' → 'operation_date' if needed
    if "date" in df.columns and "operation_date" not in df.columns:
        df = df.rename(columns={"date": "operation_date"})

    missing = [
        c for c in ["account_id", "operation_date", "month", "amount", "description"]
        if c not in df.columns
    ]
    if missing:
        raise KeyError(f"{p} is missing required columns: {', '.join(missing)}")

    if not df.empty:
        df["operation_date"] = pd.to_datetime(df["operation_date"], errors="coerce")
        df = df.dropna(subset=["operation_date"])
        df["month"] = df["operation_date"].dt.strftime("%Y-%m")
        df["operation_date"] = df["operation_date"].dt.strftime("%Y-%m-%d")

    return df


def upsert_values(base: Path, rows: pd.DataFrame) -> None:
    """
    Insert or update manual values, keyed by (account_id, operation_date).
    Enforces strict schema (amount column required).
    """
    p = values_path(base)

    # Backward compatibility for callers that still use 'date'
    if "operation_date" not in rows.columns and "date" in rows.columns:
        rows = rows.rename(columns={"date": "operation_date"})

    if "amount" not in rows.columns:
        raise KeyError("rows must contain 'amount' column (value is not supported)")

    if p.exists():
        old = load_values(base)
        all_df = pd.concat([old, rows], ignore_index=True)
    else:
        all_df = rows.copy()

    # Normalize operation_date
    all_df["operation_date"] = pd.to_datetime(all_df["operation_date"], errors="coerce")
    all_df = all_df.dropna(subset=["operation_date"])
    all_df["month"] = all_df["operation_date"].dt.strftime("%Y-%m")
    all_df["operation_date"] = all_df["operation_date"].dt.strftime("%Y-%m-%d")

    # Enforce expected schema (drop extras, add missing as None)
    for col in _expected_columns():
        if col not in all_df.columns:
            all_df[col] = None
    all_df = all_df[_expected_columns()]

    # Deduplicate by (account_id, operation_date)
    all_df = (
        all_df.sort_values(["account_id", "operation_date"])
              .drop_duplicates(subset=["account_id", "operation_date"], keep="last")
    )

    _ensure_dir(p.parent)
    all_df.to_parquet(p, index=False)
