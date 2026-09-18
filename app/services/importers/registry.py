""" app/services/importers/registry.py

Central importer registry.

Each importer must be a subclass of `Importer` (in app/services/importers/base.py)
and define:
    key: str              # unique key like "credit.bmo_mastercard_fr"
    label: str            # human-readable label
    account_side: str     # "asset" | "liability"
    account_class: str    # e.g. "cash", "investment", "credit_card", "loan"
    input_kind: str       # "pdf", "csv", etc.
    detect(path: Path) -> bool
    parse_to_json(path: Path) -> dict
"""

from __future__ import annotations
from typing import List, Dict, Any, Type
from pathlib import Path

from .base import Importer

# --------------------------------------------------------------------
# Import all known importer modules here
# --------------------------------------------------------------------
from .credit.bmo_mastercard_fr import BmoMastercardFrImporter
from .cash.bmo_chequing_fr import BmoChequingFrImporter
from .investment.questrade_equity import QuestradeEquityImporter
from .investment.interactive_brokers_ca import InteractiveBrokersCAImporter
from .investment.bmo_nesbitt_ca import BmoNesbittCAImporter
from .loc.bmo_heloc_fr import BmoHelocFrImporter
from .asset.home_evaluation_fonciere import HomeEvaluationFonciereImporter
from .asset.car_cargurus_valuation import CarGurusVehicleValuationImporter

# --------------------------------------------------------------------
# Registry initialization
# --------------------------------------------------------------------

# All available importer classes
_IMPORTER_CLASSES: List[Type[Importer]] = [
    BmoMastercardFrImporter,
    BmoChequingFrImporter,
    QuestradeEquityImporter,
    InteractiveBrokersCAImporter,
    BmoNesbittCAImporter,
    BmoHelocFrImporter,
    HomeEvaluationFonciereImporter,
    CarGurusVehicleValuationImporter,
]

# Instantiate all importers once
_ALL_IMPORTERS: List[Importer] = [cls() for cls in _IMPORTER_CLASSES]

# Index for quick lookup
_BY_KEY: Dict[str, Importer] = {imp.key: imp for imp in _ALL_IMPORTERS}

# --------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------

def importer_by_key(key: str) -> Importer | None:
    """Lookup importer by registry key."""
    return _BY_KEY.get(key)


def list_importers(account_side: str, account_class: str) -> List[Importer]:
    """Return all importers matching side + class."""
    return [
        imp for imp in _ALL_IMPORTERS
        if getattr(imp, "account_side", None) == account_side
        and getattr(imp, "account_class", None) == account_class
    ]


def public_catalog() -> Dict[str, Any]:
    """
    Return a catalog grouped by [side][class].
    Each importer entry includes:
      - key
      - label
      - input_kind
      - formats (list of supported formats, defaults to [input_kind])
      - signature (human-readable signature, if defined)
    """
    catalog: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for imp in _ALL_IMPORTERS:
        side = getattr(imp, "account_side", "unknown")
        cls = getattr(imp, "account_class", "unknown")
        entry = {
            "key": imp.key,
            "label": imp.label,
            "input_kind": getattr(imp, "input_kind", "pdf"),
            "formats": getattr(imp, "formats", [getattr(imp, "input_kind", "pdf")]),
            "signature": getattr(imp, "signature_name", None),
        }
        catalog.setdefault(side, {}).setdefault(cls, []).append(entry)
    return catalog

# --------------------------------------------------------------------
# Debug helper
# --------------------------------------------------------------------

if __name__ == "__main__":
    import json
    print(json.dumps(public_catalog(), indent=2))
