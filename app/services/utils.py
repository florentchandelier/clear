from __future__ import annotations
from typing import Dict
import re


def statement_anchor_date(importer_json: Dict) -> str:
    """
    Canonical statement anchor date used for:
      - source_statement_id
      - overwrite detection
      - FX anchoring
      - audit consistency

    Priority:
      1) First NAV snapshot date
      2) account_summary balance dates (ordered)
      3) empty string (caller decides behavior)
    """
    navs = importer_json.get("nav_snapshots") or []
    if navs and navs[0].get("date"):
        return navs[0]["date"]

    acc = importer_json.get("account_summary") or {}
    for key in ("current_balance", "previous_balance", "opening_balance", "closing_balance"):
        block = acc.get(key)
        if isinstance(block, dict) and block.get("date"):
            return block["date"]

    return ""

###############################################
# ADDRESS DETECTION FOR CANADA
###############################################
_CANADIAN_PROVINCES = {
    "qc", "on", "bc", "ab", "mb", "sk", "ns", "nb", "nl", "pe", "yt", "nt", "nu"
}

_CANADIAN_POSTAL_RE = re.compile(
    r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVXY]\s?\d[ABCEGHJ-NPRSTVXY]\d\b",
    re.IGNORECASE,
)


def detect_canadian_statement(norm_text: str) -> bool:
    """
    Detects whether a statement is Canadian using address signals.
    `norm_text` should already be lowercased and unicode-folded.
    """

    # 1. Canadian postal code (strongest signal)
    if _CANADIAN_POSTAL_RE.search(norm_text):
        return True

    # 2. Province code (qc, on, bc, etc.)
    tokens = set(re.findall(r"\b[a-z]{2}\b", norm_text))
    if tokens & _CANADIAN_PROVINCES:
        return True

    # 3. Montreal / Quebec explicit mention
    if "montreal" in norm_text and "qc" in norm_text:
        return True

    return False
