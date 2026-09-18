# app/services/importers/base.py
from __future__ import annotations
from pathlib import Path
from typing import Protocol, Literal, Dict, Any, List, Optional, TypedDict

# --------------------------------------------------------------------
# Account model (side + class)
# --------------------------------------------------------------------

# High-level grouping
AccountSide = Literal["asset", "liability"]

# Classes depend on the side
AssetClass = Literal["cash", "investment", "home_fmv", "car_fmv", "other_fmv"]
LiabilityClass = Literal["loan", "credit_card", "other_debt"]

# Union of all possible account classes
AccountClass = AssetClass | LiabilityClass


# --------------------------------------------------------------------
# Canonical importer JSON schema (Python typing)
# --------------------------------------------------------------------

class Transaction(TypedDict):
    """Normalized transaction structure."""
    operation_date: str  # YYYY-MM-DD
    description: str
    amount: float
    debit: float
    credit: float
    is_credit: bool


class StatementSummary(TypedDict, total=False):
    """Per-statement totals + validation results."""
    opening_balance: Optional[float]
    closing_balance: Optional[float]
    total_debits: Optional[float]
    total_credits: Optional[float]
    net_amount: Optional[float]
    statement_subtotal: Optional[float]
    validation_passed: bool
    validation_message: str


class CardholderInfo(TypedDict, total=False):
    """Card/account identifying information."""
    name: str
    card_number: str
    card_digits: str


class Statement(TypedDict):
    """One account/cardholder statement."""
    cardholder_info: CardholderInfo
    summary: StatementSummary
    regular_transactions: List[Transaction]
    transfer_transactions: List[Transaction]


class Reconciliation(TypedDict, total=False):
    """Optional reconciliation details."""
    declared_debits: Optional[float]
    computed_debits: Optional[float]
    declared_credits: Optional[float]
    computed_credits: Optional[float]
    debits_match: Optional[bool]
    credits_match: Optional[bool]


class ImporterJson(TypedDict, total=False):
    """
    Canonical output for all importers.

    Required:
      - export_date (str)
      - document_signature (dict with document_type)
      - statements (list of Statement)

    Optional:
      - account_summary (dict)
      - total_cardholders (int)
      - reconciliation (dict with declared/computed totals)
    """
    export_date: str
    document_signature: Dict[str, Any]
    account_summary: Dict[str, Any]
    total_cardholders: int
    statements: List[Statement]
    reconciliation: Reconciliation


# --------------------------------------------------------------------
# Importer protocol
# --------------------------------------------------------------------

class Importer(Protocol):
    """
    All importers must:
      - define metadata (key, label, account_side, account_class, input_kind)
      - implement `detect(path)` to check if a file belongs to this importer
      - implement `parse_to_json(path)` returning ImporterJson

    JSON schema reference:
      - top-level: export_date, document_signature, account_summary, statements
      - each statement: cardholder_info, summary (with validation_passed/message),
                        regular_transactions, transfer_transactions
      - transactions: operation_date, description, amount, debit, credit, is_credit
    """

    key: str  # unique key, e.g. "credit.bmo_mastercard_fr"
    label: str  # human-readable label for UI
    account_side: AccountSide  # "asset" | "liability"
    account_class: AccountClass  # one of the defined classes
    input_kind: Literal["pdf", "csv", "json"]  # for UI hint

    def detect(self, path: Path) -> bool: ...

    def parse_to_json(self, path: Path) -> ImporterJson: ...
