# app/services/fx/fetch_yfinance.py
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from .store import fx_rate_exists, store_fx_rate


class FXFetchError(RuntimeError):
    """Raised when an FX rate cannot be fetched or parsed."""


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _pair_to_yfinance_ticker(from_currency: str, to_currency: str) -> str:
    """
    Convert currency pair to Yahoo Finance ticker.
    Example: USD -> CAD  ==>  USDCAD=X
    """
    return f"{from_currency.upper()}{to_currency.upper()}=X"


def _fetch_close_for_day(
    *,
    ticker: str,
    d: date,
) -> float | None:
    """
    Try to fetch the FX close for a specific calendar day.
    Returns None if no data is available.
    """
    start = pd.Timestamp(d)
    end = start + pd.Timedelta(days=1)

    try:
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
    except Exception:
        return None

    if df is None or df.empty:
        return None

    for col in ("Close", "Adj Close"):
        if col not in df.columns:
            continue

        value = df[col].iloc[0]

        # Handle possible Series (MultiIndex columns)
        if isinstance(value, pd.Series):
            if value.empty:
                continue
            value = value.iloc[0]

        if pd.isna(value):
            continue

        try:
            rate = float(value)
        except Exception:
            continue

        if rate > 0:
            return rate

    return None


# ─────────────────────────────────────────────────────────────
# Core fetch logic
# ─────────────────────────────────────────────────────────────

def fetch_monthly_fx_rate_yfinance(
    *,
    month: date,
    from_currency: str,
    to_currency: str,
    forward_look_days: int = 7,
    backward_fallback_days: int = 7,
) -> tuple[float, date, bool]:
    """
    Fetch the FX rate for a month using the **first business day of the month**.

    > IMPORTANT
    The caller decides the anchor date (1st BD of the month). The fetcher must respect it
    The fetcher may probe around that date, but must not redefine it

    Algorithm:
    1. Start at YYYY-MM-01
    2. Walk forward up to `forward_look_days` to find first trading day
    3. If none found, walk backward up to `backward_fallback_days`
    4. Return (rate, effective_date, fallback_used)

    Returns:
        rate: float
        effective_date: date used for FX
        fallback_used: bool (True if backward fallback was needed)

    Raises:
        FXFetchError if no FX rate can be found.
    """
    if from_currency.upper() == to_currency.upper():
        return 1.0, month, False

    ticker = _pair_to_yfinance_ticker(from_currency, to_currency)
    month_start = month

    # 1 Forward search: first business day of month
    for i in range(forward_look_days):
        d = month_start + timedelta(days=i)
        rate = _fetch_close_for_day(ticker=ticker, d=d)
        if rate is not None:
            return rate, d, False

    # 2 Backward fallback: last known prior business day
    for i in range(1, backward_fallback_days + 1):
        d = month_start - timedelta(days=i)
        rate = _fetch_close_for_day(ticker=ticker, d=d)
        if rate is not None:
            return rate, d, True

    raise FXFetchError(
        f"No FX data found for {ticker} around {month_start.isoformat()}"
    )


def ensure_fx_rate(
    *,
    date: date,
    from_currency: str,
    to_currency: str,
    source: str = "yfinance",
    parquet_root=None,
) -> None:
    """
    Ensure that a **monthly FX rate** exists for the month of `date`.
    date should already be enforced to first_business_day_of_month(d)

    Behavior:
    - FX is anchored to the first business day of the month
    - If FX already exists for the resolved effective date → do nothing
    - Otherwise fetch + store
    """
    if from_currency.upper() == to_currency.upper():
        return

    month_anchor = date

    rate, effective_date, fallback_used = fetch_monthly_fx_rate_yfinance(
        month=month_anchor,
        from_currency=from_currency,
        to_currency=to_currency,
    )

    # Idempotency check (NOW ROOT-AWARE)
    if fx_rate_exists(
        date=effective_date,
        from_currency=from_currency,
        to_currency=to_currency,
        parquet_root=parquet_root,
    ):
        return

    store_fx_rate(
        date=effective_date,
        from_currency=from_currency,
        to_currency=to_currency,
        rate=rate,
        source=source,
        fallback_used=fallback_used,
        parquet_root=parquet_root,
    )

