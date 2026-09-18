#!/usr/bin/env python3
"""
One-shot migration to persist semantic category flags into categories.json.

Safe to run multiple times.
Does NOT touch categories_custom.json.
"""

from pathlib import Path
import json
import copy

# Adjust if needed
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = PROJECT_ROOT / "categories.json"

FLAGS_KEY = "_flags"
SEEDS_KEY = "_seeds"
DELETED_KEY = "_deleted"


# ──────────────────────────────────────────────────────────────
# Default semantics
# ──────────────────────────────────────────────────────────────

DEFAULT_FLAGS = {
    "include_in_spending": True,
    "include_in_cash_flow": True,
}

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
    ("misc", "legal_financial"): {
        "include_in_spending": False,
        "include_in_cash_flow": True,
    },
}


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def ensure_flags(node: dict):
    """
    Ensure default flags exist on this node.
    """
    node.setdefault(FLAGS_KEY, {})
    for k, v in DEFAULT_FLAGS.items():
        node[FLAGS_KEY].setdefault(k, v)


def walk(tree: dict):
    """
    Recursively inject default flags everywhere.
    """
    if not isinstance(tree, dict):
        return

    ensure_flags(tree)

    for k, v in tree.items():
        if k in (FLAGS_KEY, SEEDS_KEY, DELETED_KEY):
            continue
        if isinstance(v, dict):
            walk(v)


def apply_overrides(tree: dict):
    """
    Apply semantic overrides in-place.
    """
    for (cat, sub), flags in SEMANTIC_OVERRIDES.items():
        cat_node = tree.get(cat)
        if not isinstance(cat_node, dict):
            continue

        if sub == "*":
            for k, v in cat_node.items():
                if isinstance(v, dict):
                    v.setdefault(FLAGS_KEY, {}).update(flags)
        else:
            sub_node = cat_node.get(sub)
            if isinstance(sub_node, dict):
                sub_node.setdefault(FLAGS_KEY, {}).update(flags)


# ──────────────────────────────────────────────────────────────
# Migration
# ──────────────────────────────────────────────────────────────

def main():
    if not BASE_PATH.exists():
        raise SystemExit(f"categories.json not found at {BASE_PATH}")

    raw = json.loads(BASE_PATH.read_text(encoding="utf-8") or "{}")
    data = copy.deepcopy(raw)

    # 1️⃣ Inject defaults everywhere
    walk(data)

    # 2️⃣ Apply semantic overrides
    apply_overrides(data)

    # 3️⃣ Write back (pretty + stable)
    BASE_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("✅ Category semantic flags successfully migrated.")
    print(f"   Updated file: {BASE_PATH}")


if __name__ == "__main__":
    main()
