# app/services/meta_balances.py
from __future__ import annotations
from pathlib import Path
from datetime import datetime
import pandas as pd

# ─────────────────────────────────────────────
# Consistent with normalize_parquet conventions
# ─────────────────────────────────────────────

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _meta_path(base: Path) -> Path:
    _ensure_dir(base / "_meta")
    return (base / "_meta" / "account_balances.parquet").resolve()

# Canonical schema
SCHEMA_COLUMNS = [
    "account_id",
    "account_side",
    "account_class",
    "balance",
    "as_of_date",
    "institution",
    "source_file",
    "source_type",
    "imported_at",
    "notes",
]

def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=SCHEMA_COLUMNS)

# ─────────────────────────────────────────────
# Load / Upsert helpers
# ─────────────────────────────────────────────

def load(base: Path) -> pd.DataFrame:
    p = _meta_path(base)
    if not p.exists():
        return _empty_df()
    df = pd.read_parquet(p)
    for c in SCHEMA_COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[SCHEMA_COLUMNS]

def upsert(base: Path, rows: pd.DataFrame) -> None:
    if rows is None or rows.empty:
        return
    rows = rows.copy()
    for c in SCHEMA_COLUMNS:
        if c not in rows.columns:
            rows[c] = None
    rows = rows[SCHEMA_COLUMNS]

    p = _meta_path(base)
    if p.exists():
        old = pd.read_parquet(p)
        for c in SCHEMA_COLUMNS:
            if c not in old.columns:
                old[c] = None
        all_df = pd.concat([old[SCHEMA_COLUMNS], rows], ignore_index=True)
    else:
        all_df = rows
    all_df = all_df.drop_duplicates(subset=["account_id", "as_of_date"], keep="last")
    all_df.to_parquet(p, index=False)

# ─────────────────────────────────────────────
# Row builder
# ─────────────────────────────────────────────
def make_row(
    *,
    account_id: str,
    account_side: str,
    account_class: str,
    balance: float,
    as_of_date: str,
    institution: str,
    source_file: str,
    source_type: str = "statement_summary",
    notes: str | None = None,
) -> dict:
    return {
        "account_id": account_id,
        "account_side": account_side,
        "account_class": account_class,
        "balance": float(balance),
        "as_of_date": as_of_date,
        "institution": institution,
        "source_file": source_file,
        "source_type": source_type,
        "imported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes or "",
    }
