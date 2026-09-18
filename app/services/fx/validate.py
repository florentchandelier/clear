# app/services/fx/validate.py
from __future__ import annotations

from typing import Iterable, Set


class FXScopeError(RuntimeError):
    """Raised when FX scope validation fails."""


def _normalize_ccy(ccy: str) -> str:
    if not isinstance(ccy, str):
        raise FXScopeError(f"Invalid currency value: {ccy!r}")
    c = ccy.strip().upper()
    if len(c) != 3:
        raise FXScopeError(f"Invalid currency code: {ccy!r}")
    return c


def validate_fx_scope(
    *,
    currencies_used: Iterable[str],
    display_currency: str,
    supported_currencies: Iterable[str],
) -> None:
    """
    Validate that FX conversion can be safely performed.

    Rules:
    - display_currency must be in supported_currencies
    - every currency in currencies_used must be in supported_currencies
    - currencies are normalized to ISO-4217 (3-letter, uppercase)

    Raises:
        FXScopeError if validation fails.
    """
    disp = _normalize_ccy(display_currency)

    supported: Set[str] = {
        _normalize_ccy(c) for c in supported_currencies
    }

    if disp not in supported:
        raise FXScopeError(
            f"Display currency {disp} is not in fx_supported_currencies "
            f"({sorted(supported)})"
        )

    used: Set[str] = {
        _normalize_ccy(c) for c in currencies_used
    }

    unsupported = used - supported
    if unsupported:
        raise FXScopeError(
            "Unsupported currency(ies) encountered during FX conversion: "
            f"{sorted(unsupported)}. "
            f"Supported currencies: {sorted(supported)}"
        )
