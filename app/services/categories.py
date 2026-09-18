# app/services/categories.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional, List, Any
import copy
import json

import duckdb
import pandas as pd

HERE = Path(__file__).parent.resolve()
PROJECT_ROOT = HERE.parent.parent.resolve()

BASE_PATH   = PROJECT_ROOT / "categories.json"
CUSTOM_PATH = PROJECT_ROOT / "categories_custom.json"
TAGS_PATH   = PROJECT_ROOT / "tags.json"

FLAGS_KEY = "_flags"
SEEDS_KEY = "_seeds"
DELETED_KEY = "_deleted"

# ──────────────────────────────────────────────────────────────
# Default template (life-oriented categories)
# ──────────────────────────────────────────────────────────────
DEFAULT_TEMPLATE: Dict[str, Any] = {
    "housing": {
        "rent": {SEEDS_KEY: []},
        "mortgage": {SEEDS_KEY: []},
        "property_taxes": {SEEDS_KEY: []},
        "utilities": {
            "gas": {SEEDS_KEY: []},
            "electricity": {SEEDS_KEY: []},
            "water": {SEEDS_KEY: []},
            SEEDS_KEY: [],
        },
        "insurance": {SEEDS_KEY: []},
        "maintenance_repairs": {SEEDS_KEY: []},
        "furniture_appliances": {SEEDS_KEY: []},
        "connectivity": {
            "internet": {SEEDS_KEY: []},
            "mobile_phone": {SEEDS_KEY: []},
            "home_security": {SEEDS_KEY: []},
            SEEDS_KEY: [],
        },
        SEEDS_KEY: [],
    },
    "transportation": {
        "auto_purchase_lease": {SEEDS_KEY: []},
        "fuel": {SEEDS_KEY: []},
        "maintenance_repairs": {SEEDS_KEY: []},
        "insurance": {SEEDS_KEY: []},
        "parking_tolls": {SEEDS_KEY: []},
        "public_transit_rideshare": {SEEDS_KEY: []},
        "traffic_fines": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "health_wellness": {
        "insurance": {SEEDS_KEY: []},
        "medical_care": {SEEDS_KEY: []},
        "pharmacy": {SEEDS_KEY: []},
        "dental_vision": {SEEDS_KEY: []},
        "fitness": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "food_daily": {
        "groceries": {SEEDS_KEY: []},
        "household_supplies": {SEEDS_KEY: []},
        "alcohol": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "education": {
        "tuition": {SEEDS_KEY: []},
        "books_materials": {SEEDS_KEY: []},
        "courses_certifications": {SEEDS_KEY: []},
        "childcare_daycare": {SEEDS_KEY: []},
        "student_loan_payments": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "leisure": {
        "vacations": {SEEDS_KEY: []},
        "restaurants": {SEEDS_KEY: []},
        "entertainment": {SEEDS_KEY: []},
        "hobbies": {SEEDS_KEY: []},
        "gifts_celebrations": {SEEDS_KEY: []},
        "streaming": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "work_income": {
        "salary": {SEEDS_KEY: []},
        "business_income": {SEEDS_KEY: []},
        "side_hustles": {SEEDS_KEY: []},
        "taxes": {SEEDS_KEY: []},
        "professional_expenses": {SEEDS_KEY: []},
        "lunch_dining": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "savings_investments": {
        "emergency_fund": {SEEDS_KEY: []},
        "retirement": {SEEDS_KEY: []},
        "education_savings": {SEEDS_KEY: []},
        "working_capital": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "misc": {
        "donations": {SEEDS_KEY: []},
        "legal_financial": {
            "bank_fees": {SEEDS_KEY: []},
            SEEDS_KEY: [],
        },
        "misc_purchases": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    "lifestyle": {
        "clothing": {SEEDS_KEY: []},
        "beauty": {SEEDS_KEY: []},
        "digital": {SEEDS_KEY: []},
        SEEDS_KEY: [],
    },
    SEEDS_KEY: [],
}

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def _norm(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    s = str(name).strip().lower()
    return s or None

def safe_path_literal(p: Path) -> str:
    return p.as_posix().replace("'", "''")

def _lowerize_keys(node: Any) -> Any:
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            out[(k or "").lower()] = _lowerize_keys(v)
        return out
    if isinstance(node, list):
        return [_lowerize_keys(v) for v in node]
    return node

def _ensure_seeds(node: Any) -> Dict[str, Any]:
    if isinstance(node, dict):
        node = dict(node)
        node.setdefault(SEEDS_KEY, [])
        node.setdefault(DELETED_KEY, [])
        for k, v in list(node.items()):
            if k in (SEEDS_KEY, DELETED_KEY):
                if not isinstance(v, list):
                    node[k] = []
                continue
            if k == FLAGS_KEY:
                # BUG-08 regression: _flags holds
                # booleans, not category nodes -- recursing into it here
                # replaced e.g. `False` with {_seeds: [], _deleted: []},
                # which downstream bool() always reads as True.
                continue
            node[k] = _ensure_seeds(v)
        return node
    if isinstance(node, list):
        return {SEEDS_KEY: node, DELETED_KEY: []}
    return {SEEDS_KEY: [], DELETED_KEY: []}

def _ensure_flags(node: Any, *, defaults=None) -> Any:
    """
    Recursively ensure semantic flags exist on every category node.
    Backward-compatible: injects defaults when missing.
    """
    if not isinstance(node, dict):
        return node

    defaults = defaults or {
        "include_in_spending": True,
        "include_in_cash_flow": True,
    }

    node.setdefault(FLAGS_KEY, {})
    for k, v in defaults.items():
        node[FLAGS_KEY].setdefault(k, v)

    for k, v in node.items():
        if k in (SEEDS_KEY, DELETED_KEY, FLAGS_KEY):
            continue
        if isinstance(v, dict):
            _ensure_flags(v, defaults=defaults)

    return node


# ──────────────────────────────────────────────────────────────
# Semantic overrides (single source of truth)
# ──────────────────────────────────────────────────────────────

SEMANTIC_OVERRIDES = {
    # Mandatory cash flow, not discretionary spending
    ("work_income", "taxes"): {
        "include_in_spending": False,
        "include_in_cash_flow": True,
    },
    ("work_income", "professional_expenses"): {
        "include_in_spending": False,
        "include_in_cash_flow": True,
    },
    ("housing", "property_taxes"): {
        "include_in_spending": False,
        "include_in_cash_flow": True,
    },
    # Savings / investments are balance transfers, not cash flow
    ("savings_investments", "*"): {
        "include_in_spending": False,
        "include_in_cash_flow": False,
    },
    ("misc", "donations"): {
        "include_in_spending": False,
        "include_in_cash_flow": True,
    },
}


def _apply_semantic_overrides(tree: Dict[str, Any]) -> None:
    """
    Apply hard semantic rules on top of merged categories.
    Runs in-memory only; does not mutate on-disk JSON.
    """
    def _apply_flags_recursive(node: Dict[str, Any], flags: Dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        node.setdefault(FLAGS_KEY, {}).update(flags)
        for k, v in node.items():
            if k in (SEEDS_KEY, DELETED_KEY, FLAGS_KEY):
                continue
            if isinstance(v, dict):
                _apply_flags_recursive(v, flags)

    for (cat, sub), flags in SEMANTIC_OVERRIDES.items():
        cat_node = tree.get(cat)
        if not isinstance(cat_node, dict):
            continue

        if sub == "*":
            for k, v in cat_node.items():
                if isinstance(v, dict):
                    _apply_flags_recursive(v, flags)
        else:
            sub_node = cat_node.get(sub)
            if isinstance(sub_node, dict):
                _apply_flags_recursive(sub_node, flags)


def _ensure_node(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    node = parent.setdefault(key, {})
    if not isinstance(node, dict):
        node = {}
        parent[key] = node
    node.setdefault(SEEDS_KEY, [])
    node.setdefault(DELETED_KEY, [])
    return node

def _apply_tombstones(node: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively drop keys listed in _deleted."""
    if not isinstance(node, dict):
        return node
    deleted = set(node.get(DELETED_KEY, []))
    out = {}
    for k, v in node.items():
        if k in (SEEDS_KEY, DELETED_KEY):
            out[k] = v
        elif k in deleted:
            continue
        else:
            out[k] = _apply_tombstones(v)
    return out

def _ensure_other_nodes(tree: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively ensure every category and subcategory has a fallback 'other' node.
    Does not modify on-disk files; applied at runtime after merge and tombstones.
    """
    if not isinstance(tree, dict):
        return tree

    for cat, cat_node in tree.items():
        if not isinstance(cat_node, dict):
            continue

        # Ensure 'other' subcategory at category level
        cat_node.setdefault("other", {SEEDS_KEY: [], DELETED_KEY: []})

        # Recurse into subcategories
        for sub, sub_node in cat_node.items():
            if sub in (SEEDS_KEY, DELETED_KEY):
                continue
            if isinstance(sub_node, dict):
                sub_node.setdefault("other", {SEEDS_KEY: [], DELETED_KEY: []})
    return tree

# ──────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────
def load_base_categories() -> Dict[str, Any]:
    if not BASE_PATH.exists():
        save_base_categories(DEFAULT_TEMPLATE)
    try:
        raw = json.loads(BASE_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        save_base_categories(DEFAULT_TEMPLATE)
        return DEFAULT_TEMPLATE
    return _ensure_seeds(_lowerize_keys(raw))

def save_base_categories(data: Dict[str, Any]) -> None:
    BASE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def load_custom_categories() -> Dict[str, Any]:
    """Load user custom categories (custom or mock), with dynamic path support."""
    from app import config  # lazy import to avoid circular deps

    settings = config.load_settings()
    custom_path = settings.get("custom_categories_path")

    path = Path(custom_path) if custom_path else CUSTOM_PATH
    if not path.exists():
        # Initialize with empty structure
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        path.write_text("{}", encoding="utf-8")
        return {}
    return _ensure_seeds(_lowerize_keys(raw))

def save_custom_categories(data: Dict[str, Any]) -> None:
    """Save to the custom categories path defined in settings.json if available."""
    from app import config
    settings = config.load_settings()
    custom_path = settings.get("custom_categories_path")

    path = Path(custom_path) if custom_path else CUSTOM_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def _merge_categories(base: Dict[str, Any], custom: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    def merge_nodes(dst, src):
        for k, v in src.items():
            if k in (SEEDS_KEY, DELETED_KEY):
                dst.setdefault(k, [])
                dst[k].extend(v)
            else:
                if isinstance(v, dict):
                    child = dst.setdefault(k, {SEEDS_KEY: [], DELETED_KEY: []})
                    merge_nodes(child, v)
                else:
                    dst[k] = copy.deepcopy(v)
    merge_nodes(result, custom)
    return result

def load_categories() -> Dict[str, Any]:
    base = load_base_categories()
    custom = load_custom_categories()
    merged = _merge_categories(base, custom)
    merged = _apply_tombstones(merged)
    merged = _ensure_other_nodes(merged)
    merged = _ensure_flags(merged)
    _apply_semantic_overrides(merged)
    return merged

def reset_to_defaults() -> None:
    """Reset base categories to DEFAULT_TEMPLATE and clear custom overrides."""
    save_base_categories(DEFAULT_TEMPLATE)
    save_custom_categories({})

# ──────────────────────────────────────────────────────────────
# Tags
# ──────────────────────────────────────────────────────────────
def load_tags() -> Dict[str, List[str]]:
    if not TAGS_PATH.exists():
        return {}
    try:
        return json.loads(TAGS_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}

def tags_for_category_path(path: str) -> List[str]:
    tags = load_tags()
    out: List[str] = []
    for tag, paths in tags.items():
        if path in paths:
            out.append(tag)
    return out

# ──────────────────────────────────────────────────────────────
# CRUD (custom only, using tombstones)
# ──────────────────────────────────────────────────────────────
def ensure_category(cat: Optional[str], subcat: Optional[str] = None, type_name: Optional[str] = None) -> None:
    cat_k, sub_k, typ_k = _norm(cat), _norm(subcat), _norm(type_name)
    if cat_k is None:
        return
    custom = load_custom_categories()
    cat_node = _ensure_node(custom, cat_k)
    if sub_k:
        sub_node = _ensure_node(cat_node, sub_k)
        if typ_k:
            _ensure_node(sub_node, typ_k)
    save_custom_categories(custom)

def delete_type(cat: str, subcat: str, type_name: str) -> None:
    custom = load_custom_categories()
    node = _ensure_node(custom, cat)
    sub_node = _ensure_node(node, subcat)
    sub_node.setdefault(DELETED_KEY, []).append(type_name)
    save_custom_categories(custom)

def delete_subcategory(cat: str, subcat: str) -> None:
    custom = load_custom_categories()
    node = _ensure_node(custom, cat)
    node.setdefault(DELETED_KEY, []).append(subcat)
    save_custom_categories(custom)

def delete_category(cat: str) -> None:
    custom = load_custom_categories()
    custom.setdefault(DELETED_KEY, []).append(cat)
    save_custom_categories(custom)

def list_categories() -> Dict[str, Any]:
    return copy.deepcopy(load_categories())

# ──────────────────────────────────────────────────────────────
# Seed ops (custom only)
# ──────────────────────────────────────────────────────────────
def add_seed_transaction(cat: Optional[str], subcat: Optional[str], type_name: Optional[str],
                         tx_id: str, description: str) -> None:
    if not tx_id:
        return
    cat_k, sub_k, typ_k = _norm(cat), _norm(subcat), _norm(type_name)
    if cat_k is None:
        return
    custom = load_custom_categories()
    node = _ensure_node(custom, cat_k)
    if sub_k:
        node = _ensure_node(node, sub_k)
        if typ_k:
            node = _ensure_node(node, typ_k)
    seeds: List[Dict[str, str]] = node[SEEDS_KEY]
    if not any(s.get("transaction_id") == tx_id for s in seeds):
        seeds.append({"transaction_id": tx_id, "description": description})
        save_custom_categories(custom)

def remove_seed_transaction(cat: Optional[str], subcat: Optional[str], type_name: Optional[str], tx_id: str) -> None:
    if not tx_id:
        return
    custom = load_custom_categories()
    node = custom.get(cat, {})
    if subcat:
        node = node.get(subcat, {})
        if type_name:
            node = node.get(type_name, {})
    if isinstance(node, dict) and SEEDS_KEY in node:
        node[SEEDS_KEY] = [s for s in node[SEEDS_KEY] if s.get("transaction_id") != tx_id]
        save_custom_categories(custom)

# ──────────────────────────────────────────────────────────────
# Parquet helpers
# ──────────────────────────────────────────────────────────────
def collect_parquet_files(path: Path) -> List[Path]:
    if path.is_file() and path.suffix == ".parquet":
        return [path.resolve()]
    if path.is_dir():
        return sorted(path.rglob("*.parquet"))
    raise FileNotFoundError(f"Path not found: {path}")

def transactions_for_year_month(parquet_path: str, year: int, month: int) -> pd.DataFrame:
    files = collect_parquet_files(Path(parquet_path))
    if not files:
        return pd.DataFrame()
    con = duckdb.connect(database=":memory:")
    files_sql = ", ".join(f"'{safe_path_literal(p)}'" for p in files)
    con.execute(
        f"CREATE OR REPLACE VIEW df AS "
        f"SELECT * FROM read_parquet([{files_sql}], union_by_name=true)"
    )
    ym = f"{int(year):04d}-{int(month):02d}"
    query = f"""
    WITH base AS (
      SELECT df.*,
             COALESCE(TRY_CAST(operation_date AS DATE), TRY_CAST(posted_date AS DATE)) AS op_date
      FROM df
    )
    SELECT *
    FROM base
    WHERE transaction_type = 'regular_transactions'
      AND STRFTIME('%Y-%m', op_date) = '{ym}'
    ORDER BY op_date
    """
    return con.execute(query).df()
