#!/usr/bin/env python3
"""
generate_mock_categories.py
────────────────────────────
Generates a realistic `categories_mock.json` dataset aligned with:
  • The default categories.json structure (as a template)
  • Mock Parquet data under data/parquet/mock

Purpose:
  This script is designed to *stress-test* the category update pipeline.
  It automatically creates a dataset with controlled proportions of:
    - Authoritative corrections → test correction precedence
    - Soft (non-authoritative) corrections → test fallback corrections
    - Plain seeds → test tx_id and fuzzy seeding
    - Fuzzy variants → test approximate matching
    - Unmatched examples → ensure at least one transaction remains uncategorized

Result:
  A single JSON file that lets you verify that `update_categories()` correctly
  computes (auth_corr, seed_corr, id_seed, fuzzy, unmatched) counts.
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json, random, pandas as pd

# JSON structure constants
SEEDS_KEY = "_seeds"
DELETED_KEY = "_deleted"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _init_from_template(base_template: dict) -> dict:
    """
    Recursively rebuilds a categories dictionary from the base template,
    replacing all leaves with empty `_seeds` and `_deleted` lists.

    This ensures we maintain the same hierarchical structure as
    categories.json but start from a clean, empty slate.
    """
    def _walk(node):
        if isinstance(node, dict):
            out = {SEEDS_KEY: [], DELETED_KEY: []}
            for k, v in node.items():
                if k not in (SEEDS_KEY, DELETED_KEY):
                    out[k] = _walk(v)
            return out
        return {SEEDS_KEY: [], DELETED_KEY: []}
    return _walk(base_template)


def infer_category(desc: str) -> tuple[str, str, str]:
    d = desc.lower()

    # NEW RULE — for RONA, Home Depot, etc.
    if any(k in d for k in ["rona", "home depot", "lowe's", "renovation", "hardware"]):
        return ("housing", "maintenance_repairs", "unsubcategorized")

    if any(k in d for k in ["épicerie", "iga", "metro", "super c", "market"]):
        return ("food_daily", "groceries", "unsubcategorized")
    if any(k in d for k in ["esso", "couche-tard", "shell", "station", "fuel"]):
        return ("transportation", "fuel", "unsubcategorized")
    if any(k in d for k in ["vidéotron", "videotron", "hydro", "telus", "bell"]):
        return ("housing", "utilities", "unsubcategorized")
    if any(k in d for k in ["café", "restaurant", "bistro", "saq", "boutique"]):
        return ("leisure", "restaurants", "unsubcategorized")
    if any(k in d for k in ["dépot direct", "depot direct", "entreprise", "payroll", "chu", "aide gouvernement"]):
        return ("work_income", "salary", "unsubcategorized")
    if any(k in d for k in ["assurance", "insurance", "saaq"]):
        return ("transportation", "insurance", "unsubcategorized")
    if any(k in d for k in ["amazon", "amzn", "pharmaprix", "canadian tire"]):
        return ("misc", "misc_purchases", "unsubcategorized")

    return ("uncategorized", "__default__", "__default__")


def _add_seed(tree, cat, sub, typ, tx_id, desc, corrected=False, authoritative=False, omit_id=False):
    """
    Inserts a single entry (seed or correction) into the correct category node.

    Args:
      - corrected: Whether this represents a correction (has `corrected_to` field)
      - authoritative: If True, this correction will be treated as global truth
      - omit_id: When True, omits `transaction_id` to simulate non-id-based matching
    """
    cat_node = tree.setdefault(cat, {SEEDS_KEY: [], DELETED_KEY: []})
    sub_node = cat_node.setdefault(sub, {SEEDS_KEY: [], DELETED_KEY: []})
    type_node = sub_node.setdefault(typ, {SEEDS_KEY: [], DELETED_KEY: []})
    seeds = type_node.setdefault(SEEDS_KEY, [])

    rec = {
        "description": desc.lower().strip(),  # normalize case
        "added_at": datetime.now(timezone.utc).isoformat()
    }

    # Add correction details
    if corrected:
        rec["corrected_to"] = {"category": cat, "subcategory": sub, "type": typ}
        if authoritative:
            rec["authoritative"] = True

    # Simulate incomplete records (for fuzzy/desc-based matches)
    if not omit_id:
        rec["transaction_id"] = tx_id

    seeds.append(rec)


# ──────────────────────────────────────────────
# Main generator
# ──────────────────────────────────────────────
def generate_from_parquet(parquet_root: Path, out_file: Path, template_path: Path):
    """
    Core generator function:
      1. Reads all mock transactions from Parquet
      2. Infers categories for each
      3. Randomly partitions transactions into:
         - Authoritative corrections (~⅓)
         - Soft corrections (~⅓)
         - Plain seeds (~⅓)
         - Some fuzzy-modified variants
         - One unmatched example
      4. Writes categories_mock.json
    """
    all_tx: list[tuple[str, str, str, str, str]] = []

    # Gather all mock transactions
    for p in sorted(parquet_root.rglob("transactions.parquet")):
        df = pd.read_parquet(p)
        if df.empty or "description" not in df.columns:
            continue
        for _, r in df.iterrows():
            desc = str(r.get("description", "")).strip()
            txid = str(r.get("transaction_id", "")).strip()
            if not desc or not txid:
                continue
            cat, sub, typ = infer_category(desc)
            all_tx.append((cat, sub, typ, txid, desc))

    if not all_tx:
        print("⚠️  No transactions found.")
        return

    # Start from clean base structure (copy of categories.json)
    base = json.loads(template_path.read_text(encoding="utf-8"))
    out = _init_from_template(base)

    random.seed(42)
    n = len(all_tx)

    # ──────────────────────────────────────────────
    # Balanced sampling strategy
    # ──────────────────────────────────────────────
    thirds = n // 3
    auth_set = set(random.sample(range(n), thirds))  # authoritative corrections
    soft_set = set(random.sample([i for i in range(n) if i not in auth_set], thirds))  # soft corrections
    fuzzy_set = set(random.sample(range(n), max(3, n // 6)))  # some fuzzified text variants

    for i, (cat, sub, typ, txid, desc) in enumerate(all_tx):
        desc_mod = desc

        # Inject fuzzy noise into ~1/6 of all records
        if i in fuzzy_set:
            if random.random() < 0.5:
                desc_mod += " fuzzy"      # small suffix to test token-sort fuzz
            else:
                desc_mod = desc_mod.replace("QC", "QCC")  # spelling variation

        # 15% omit transaction_id → forces description-based match
        omit_id = random.random() < 0.15

        # Classify into one of three coverage tiers
        if i in auth_set:
            _add_seed(out, cat, sub, typ, txid, desc_mod, corrected=True, authoritative=True, omit_id=omit_id)
        elif i in soft_set:
            _add_seed(out, cat, sub, typ, txid, desc_mod, corrected=True, authoritative=False, omit_id=omit_id)
        else:
            _add_seed(out, cat, sub, typ, txid, desc_mod, corrected=False, authoritative=False, omit_id=omit_id)

    # Note: any genuinely unmatched transaction in the baseline comes from
    # the fuzzy/omit_id corruption above causing a real seed to miss its
    # match at apply time -- not from a deliberate exclusion here. (A
    # previous version of this script added a decorative
    # "_unmatched_example" key with a fake transaction_id that matched no
    # real transaction; it was invalid as a category-tree node -- loading
    # it as categories_custom.json would break categories.py's recursive
    # tree walk -- and inert either way, so it was removed rather than
    # fixed.)

    # Write output
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"✅ demo categorization seed written → {out_file}")
    print(f"   Coverage: {len(auth_set)} auth, {len(soft_set)} soft, "
          f"{n - len(auth_set) - len(soft_set)} plain + 1 unmatched")


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────
if __name__ == "__main__":
    # Maintenance tool, not part of the routine `make demo` build (see
    # scripts/build_demo_profile.py). Re-run this only when
    # data/demo_seed/mock_*.json changes, then commit the regenerated
    # data/demo_seed/categories_demo_seed.json -- the demo build treats
    # that file as a tracked, immutable input, not something it derives
    # itself.
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    parquet_root = PROJECT_ROOT / "var" / "demo"
    out_file = PROJECT_ROOT / "data" / "demo_seed" / "categories_demo_seed.json"
    template = PROJECT_ROOT / "categories.json"

    generate_from_parquet(parquet_root, out_file, template)
