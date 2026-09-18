# app/services/fx/resolve.py
from __future__ import annotations

from datetime import date
from typing import Tuple, Dict
from pathlib import Path

import pandas as pd

from .store import load_fx_table


class FXMissingError(RuntimeError):
    """Raised when no FX rate (exact or fallback) can be resolved."""


def _normalize_ccy(ccy: str) -> str:
    if not ccy:
        raise ValueError("Currency code cannot be empty.")
    c = str(ccy).strip().upper()
    if len(c) != 3:
        raise ValueError(f"Currency code must be 3 letters (got {ccy!r}).")
    return c


def resolve_fx_rate(
    *,
    valuation_date: date,
    from_currency: str,
    to_currency: str,
    parquet_root: Path,
) -> Tuple[float, Dict]:
    """
    Resolve an FX rate using **monthly FX anchored to the first business day**.

    Strategy:
    - Prefer earliest FX date within the valuation month
    - Fallback to latest FX date before the month
    """

    fc = _normalize_ccy(from_currency)
    tc = _normalize_ccy(to_currency)

    if fc == tc:
        return 1.0, {
            "fx_date_used": valuation_date,
            "fallback_used": False,
            "source": "identity",
        }

    df = load_fx_table(parquet_root)
    if df.empty:
        raise FXMissingError(f"No FX rates available for {fc}→{tc}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["from_currency"] = df["from_currency"].str.upper()
    df["to_currency"] = df["to_currency"].str.upper()

    pair_df = df[
        (df["from_currency"] == fc) &
        (df["to_currency"] == tc)
    ]

    if pair_df.empty:
        raise FXMissingError(f"No FX rates available for currency pair {fc}→{tc}")

    # Month boundaries
    valuation_ts = pd.Timestamp(valuation_date).normalize()
    month_start = valuation_ts.replace(day=1)
    month_end = month_start + pd.offsets.MonthBegin(1)

    # 1 Prefer FX inside the same month (first business day)
    in_month = pair_df[
        (pair_df["date"] >= month_start) &
        (pair_df["date"] < month_end)
    ]

    if not in_month.empty:
        row = in_month.sort_values("date").iloc[0]
        return float(row["rate"]), {
            "fx_date_used": row["date"],
            "fallback_used": False,
            "source": row["source"],
        }

    # 2 Fallback to latest FX before the month
    prior = pair_df[pair_df["date"] < month_start]

    if not prior.empty:
        row = prior.sort_values("date", ascending=False).iloc[0]
        return float(row["rate"]), {
            "fx_date_used": row["date"],
            "fallback_used": True,
            "source": row["source"],
        }

    raise FXMissingError(
        f"No FX rate found for {fc}→{tc} for or before {month_start}"
    )


def first_business_day_of_month(d: date) -> date:
    ts = pd.Timestamp(d).replace(day=1)
    return (ts + pd.offsets.BDay(0)).date()