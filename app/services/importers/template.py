# app/services/importers/template.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import json

from .base import Importer, ImporterJson, Transaction, Statement, StatementSummary


class TemplateImporter(Importer):
    """
    Base class for structured importers.

    Subclasses must implement:
      - detect(path: Path) -> bool
      - _extract_metadata(path: Path) -> dict
      - _extract_transactions(path: Path, year_hint: int) -> List[Transaction]
      - _validate(txs: List[Transaction], metadata: dict) -> StatementSummary

    The `parse_to_json` method is implemented here and produces a canonical
    ImporterJson according to the schema defined in base.py.
    """

    # ------------------------------------------------------------------
    # Metadata to override in subclass
    # ------------------------------------------------------------------
    key: str = "example.importer"
    label: str = "Example Importer"
    account_side: str = "asset"
    account_class: str = "cash"
    input_kind: str = "pdf"
    signature_name: Optional[str] = None
    institution_default: str = "UNKNOWN"

    # ------------------------------------------------------------------
    # Abstract methods (to implement in subclass)
    # ------------------------------------------------------------------
    def detect(self, path: Path) -> bool:
        """Check if a given file belongs to this importer."""
        raise NotImplementedError

    def _extract_metadata(self, path: Path) -> Dict[str, Any]:
        """
        Extract statement-level metadata such as period end, account number,
        domiciliation, or cardholder name.
        Must return a dict including at least:
            - name
            - card_number
            - card_digits
        """
        raise NotImplementedError

    def _extract_transactions(self, path: Path, year_hint: int) -> List[Transaction]:
        """
        Extract transactions from the file. Each must match the Transaction schema:
            - operation_date (YYYY-MM-DD)
            - description
            - amount
            - debit
            - credit
            - is_credit
        """
        raise NotImplementedError

    def _validate(self, txs: List[Transaction], metadata: Dict[str, Any]) -> StatementSummary:
        """
        Perform validation between parsed totals and declared totals.
        Must return a StatementSummary that includes:
            - total_debits
            - total_credits
            - (optionally) opening_balance, closing_balance, net_amount, statement_subtotal
            - validation_passed (bool)
            - validation_message (str)
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def parse_to_json(self, path: Path) -> ImporterJson:
        # extract metadata
        meta = self._extract_metadata(path)
        year_hint = datetime.now().year  # subclasses can refine

        # extract transactions
        txs = self._extract_transactions(path, year_hint=year_hint)

        # run validation
        summary = self._validate(txs, meta)

        # build canonical statement
        statement: Statement = {
            "cardholder_info": {
                "name": meta.get("name", "Unknown"),
                "card_number": meta.get("card_number", ""),
                "card_digits": meta.get("card_digits", ""),
            },
            "summary": summary,
            "regular_transactions": txs,
            "transfer_transactions": meta.get("transfer_transactions", []),
        }

        importer_json: ImporterJson = {
            "export_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "document_signature": {"document_type": self.signature_name or self.key.upper()},
            "account_summary": {},
            "statements": [statement],
        }

        # add reconciliation block if provided
        reconciliation = meta.get("reconciliation")
        if reconciliation:
            importer_json["reconciliation"] = reconciliation

        return importer_json

    # ------------------------------------------------------------------
    # Debug helper
    # ------------------------------------------------------------------
    def debug_parse(self, path: Path, to_file: Optional[Path] = None) -> None:
        """Run parse_to_json and print or save the result."""
        data = self.parse_to_json(path)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if to_file:
            Path(to_file).write_text(text, encoding="utf-8")
        else:
            print(text)
