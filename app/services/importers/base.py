# app/services/importers/base.py
from __future__ import annotations
from pathlib import Path
from typing import Protocol, Literal, List, Optional, TypedDict

# --------------------------------------------------------------------
# Account model (side + class)
# --------------------------------------------------------------------

# High-level grouping
AccountSide = Literal["asset", "liability"]

# Classes depend on the side
AssetClass = Literal[
    "cash",
    "investment",
    "home_fmv",
    "car_fmv",
    "private_equity",
    "insurance_fv",
    "other_fmv",
]
LiabilityClass = Literal["loan", "loc", "credit_card", "other_debt"]

# Union of all possible account classes
AccountClass = AssetClass | LiabilityClass
TransactionType = Literal["expense", "income", "transfer", "refund"]


# --------------------------------------------------------------------
# Canonical importer JSON schema (Python typing)
# --------------------------------------------------------------------

class DocumentSignature(TypedDict):
    """Required document-identification block."""
    document_type: str


class AccountSummary(TypedDict):
    """Fields required from every importer account summary."""
    account_id: str
    account_name: str
    institution: str
    account_side: AccountSide
    account_class: AccountClass
    base_currency: str


class Transaction(TypedDict):
    """Normalized transaction structure."""
    operation_date: str  # YYYY-MM-DD
    description: str
    amount: float
    debit: float
    credit: float
    is_credit: bool
    transaction_type: TransactionType


class NavSnapshot(TypedDict):
    """One dated account valuation."""
    date: str  # YYYY-MM-DD
    nav: float


class _StatementSummaryRequired(TypedDict):
    validation_passed: bool
    validation_message: str


class StatementSummary(_StatementSummaryRequired, total=False):
    """Required validation result plus optional per-statement totals."""
    opening_balance: Optional[float]
    closing_balance: Optional[float]
    total_debits: Optional[float]
    total_credits: Optional[float]
    net_amount: Optional[float]
    statement_subtotal: Optional[float]


class _CardholderInfoRequired(TypedDict):
    name: str
    card_digits: str


class CardholderInfo(_CardholderInfoRequired, total=False):
    """Required cardholder identity plus an optional masked card number."""
    card_number: str


class _StatementRequired(TypedDict):
    summary: StatementSummary
    transactions: List[Transaction]


class Statement(_StatementRequired, total=False):
    """One transactional statement with optional account hierarchy metadata."""
    cardholder_info: CardholderInfo
    parent_account_id: str


class Reconciliation(TypedDict, total=False):
    """Optional reconciliation details."""
    declared_debits: Optional[float]
    computed_debits: Optional[float]
    declared_credits: Optional[float]
    computed_credits: Optional[float]
    debits_match: Optional[bool]
    credits_match: Optional[bool]


class _ImporterJsonRequired(TypedDict):
    export_date: str
    document_signature: DocumentSignature
    account_summary: AccountSummary


class ImporterJson(_ImporterJsonRequired, total=False):
    """
    Canonical output for all importers.

    Required:
      - export_date (str)
      - document_signature (dict with document_type)
      - account_summary
      - at least one of statements or nav_snapshots (enforced by schema.json)

    Optional:
      - total_cardholders (int)
      - statements (list of Statement)
      - nav_snapshots (list of NavSnapshot)
      - reconciliation (dict with declared/computed totals)
      - top-level validation result emitted by current importers
    """
    total_cardholders: int
    statements: List[Statement]
    nav_snapshots: List[NavSnapshot]
    reconciliation: Reconciliation
    validation_passed: bool
    validation_message: str


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
      - top-level: export_date, document_signature, account_summary, and either
                   statements or nav_snapshots
      - each statement: summary, transactions, and optional cardholder/account
                        hierarchy metadata
      - transactions: operation_date, description, amount, debit, credit,
                      is_credit, transaction_type
    """

    key: str  # unique key, e.g. "credit.bmo_mastercard_fr"
    label: str  # human-readable label for UI
    account_side: AccountSide  # "asset" | "liability"
    account_class: AccountClass  # one of the defined classes
    input_kind: Literal["pdf", "csv", "json"]  # for UI hint

    def detect(self, path: Path) -> bool: ...

    def parse_to_json(self, path: Path) -> ImporterJson: ...
