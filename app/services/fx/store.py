# app/services/fx/store.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd


# ─────────────────────────────────────────────────────────────
# Storage location
# ─────────────────────────────────────────────────────────────

def _project_root() -> Path:
    """
    Resolve project root assuming this file lives at:
        app/services/fx/store.py
    """
    return Path(__file__).resolve().parents[3]


def get_currency_parquet_path(parquet_root: Path) -> Path:
    """
    Returns:
        <parquet_root>/_meta/currency.parquet

    parquet_root MUST be provided explicitly.
    """
    if parquet_root is None:
        raise ValueError("parquet_root must be provided for FX storage")

    root = Path(parquet_root).resolve()
    return root / "_meta" / "currency.parquet"


# ─────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────

_FX_COLUMNS = [
    "date",          # datetime64[ns] (date normalized to midnight)
    "from_currency", # string
    "to_currency",   # string
    "rate",          # float64
    "source",        # string
    "fetched_at",    # datetime64[ns]
    "fallback_used", # bool
]


def _normalize_ccy(ccy: str) -> str:
    if not ccy:
        raise ValueError("Currency code cannot be empty.")
    c = str(ccy).strip().upper()
    if len(c) != 3:
        raise ValueError(f"Currency code must be 3 letters (got {ccy!r}).")
    return c


def _to_pd_date(d: date) -> pd.Timestamp:
    # store as timestamp at midnight (date semantics but parquet-friendly)
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"date must be datetime.date (got {type(d)}).")
    return pd.Timestamp(d)


def _ensure_meta_dir_exists(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _empty_fx_df() -> pd.DataFrame:
    df = pd.DataFrame(columns=_FX_COLUMNS)
    # set dtypes explicitly for stability
    df["date"] = pd.to_datetime(df["date"])
    df["from_currency"] = df["from_currency"].astype("string")
    df["to_currency"] = df["to_currency"].astype("string")
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce").astype("float64")
    df["source"] = df["source"].astype("string")
    df["fetched_at"] = pd.to_datetime(df["fetched_at"])
    df["fallback_used"] = df["fallback_used"].astype("boolean")
    return df


def _coerce_fx_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure required columns exist and dtypes are sane.
    This is defensive in case older files exist.
    """
    if df is None or df.empty:
        return _empty_fx_df()

    for col in _FX_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA

    out = df[_FX_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["from_currency"] = out["from_currency"].astype("string").str.upper().str.strip()
    out["to_currency"] = out["to_currency"].astype("string").str.upper().str.strip()
    out["rate"] = pd.to_numeric(out["rate"], errors="coerce").astype("float64")
    out["source"] = out["source"].astype("string")
    out["fetched_at"] = pd.to_datetime(out["fetched_at"], errors="coerce")
    # boolean dtype can be tricky; allow NA
    out["fallback_used"] = out["fallback_used"].astype("boolean")
    return out


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def load_fx_table(parquet_root: Path) -> pd.DataFrame:
    """
    Loads the FX table from parquet. If missing, returns an empty, correctly-typed DF.
    """
    path = get_currency_parquet_path(parquet_root)
    if not path.exists():
        return _empty_fx_df()

    df = pd.read_parquet(path)
    return _coerce_fx_df(df)


def fx_rate_exists(
    *,
    date: date,
    from_currency: str,
    to_currency: str,
    parquet_root: Path,
) -> bool:
    """
    True if an exact (date, from_currency, to_currency) rate exists in storage.
    """
    d = _to_pd_date(date)
    fc = _normalize_ccy(from_currency)
    tc = _normalize_ccy(to_currency)

    df = load_fx_table(parquet_root)
    if df.empty:
        return False

    m = (df["date"] == d) & (df["from_currency"] == fc) & (df["to_currency"] == tc)
    return bool(m.any())


def store_fx_rate(
    *,
    date: date,
    from_currency: str,
    to_currency: str,
    rate: float,
    source: str,
    fallback_used: bool = False,
    parquet_root: Path,
) -> None:
    """
    Append FX rate if it does not already exist.
    Never overwrites existing (date, from_currency, to_currency).
    Idempotent: calling twice with the same key will keep only one row.

    Writes are done atomically (write temp then replace).
    """
    d = _to_pd_date(date)
    fc = _normalize_ccy(from_currency)
    tc = _normalize_ccy(to_currency)

    if fc == tc:
        # We generally don't need to store identity rates; keeping storage clean.
        return

    try:
        r = float(rate)
    except Exception as e:
        raise ValueError(f"rate must be numeric (got {rate!r}).") from e
    if not (r > 0):
        raise ValueError(f"rate must be > 0 (got {r}).")

    src = (source or "").strip() or "unknown"
    fetched_at = pd.Timestamp.utcnow()

    path = get_currency_parquet_path(parquet_root)
    _ensure_meta_dir_exists(path)

    df = load_fx_table(parquet_root)

    # If already exists, do nothing (idempotent)
    if not df.empty:
        existing = (df["date"] == d) & (df["from_currency"] == fc) & (df["to_currency"] == tc)
        if bool(existing.any()):
            return

    new_row = pd.DataFrame([{
        "date": d,
        "from_currency": fc,
        "to_currency": tc,
        "rate": r,
        "source": src,
        "fetched_at": fetched_at,
        "fallback_used": bool(fallback_used),
    }])

    out = pd.concat([df, new_row], ignore_index=True)
    out = _coerce_fx_df(out)

    # Enforce uniqueness by key (date, from, to), keeping earliest fetched row
    out = out.sort_values(["date", "from_currency", "to_currency", "fetched_at"], kind="mergesort")
    out = out.drop_duplicates(subset=["date", "from_currency", "to_currency"], keep="first")

    # Atomic write
    tmp = path.with_suffix(".parquet.tmp")
    try:
        out.to_parquet(tmp, index=False)
        tmp.replace(path)
    except Exception as e:
        # Best-effort FX persistence:
        # - FX is advisory, not critical
        # - Failure must NOT block execution
        # - Log warning and continue

        import logging
        log = logging.getLogger(__name__)
        log.warning(
            "FX rate persistence skipped (%s→%s @ %s): %s",
            fc, tc, d.date() if hasattr(d, "date") else d,
            e,
        )

        # Defensive cleanup (if partially written)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

        return
