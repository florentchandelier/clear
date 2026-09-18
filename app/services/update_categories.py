# app/services/update_categories.py
from pathlib import Path
from typing import Optional, Dict, Any, Iterable, Tuple, List
import re
import pandas as pd
from rapidfuzz import fuzz
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services import categories
from app import config  # for backfill parquet dir

SEEDS_KEY = "_seeds"
DELETED_KEY = "_deleted"

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
@dataclass
class Correction:
    transaction_id: str
    description: str
    corrected_to: Dict[str, str]
    authoritative: bool = False   # default is one-time, not global
    added_at: str = datetime.now(timezone.utc).isoformat()

def is_correction(seed: Dict[str, Any]) -> bool:
    """Return True if a seed has correction fields."""
    return bool(seed.get("corrected_to"))

def normalize_desc(desc: str) -> str:
    """Normalize description for fuzzy matching and corrections lookup."""
    desc = (desc or "").lower()
    desc = re.sub(r"[0-9]", "", desc)        # remove digits
    desc = re.sub(r"[^\w\s]", "", desc)      # remove punctuation
    desc = re.sub(r"\s+", " ", desc)         # collapse spaces
    return desc.strip()

def collect_parquet_files(path: Path):
    """Return all parquet files in a directory (recursively)."""
    if path.is_file() and path.suffix == ".parquet":
        return [path.resolve()]
    if path.is_dir():
        return sorted(path.rglob("*.parquet"))
    raise FileNotFoundError(f"Path not found: {path}")

# ──────────────────────────────────────────────────────────────
# Internal tree traversal
# ──────────────────────────────────────────────────────────────
def _iter_seed_nodes(tree: Dict[str, Any]) -> Iterable[Tuple[str, Optional[str], Optional[str], List[Dict[str, str]], int]]:
    """Iterate all nodes and yield: (cat, subcat, type, seeds, rank)."""
    for cat, cat_node in tree.items():
        if not isinstance(cat_node, dict):
            continue
        if cat_node.get(SEEDS_KEY):
            yield (cat, None, None, cat_node[SEEDS_KEY], 1)
        for sub, sub_node in cat_node.items():
            if sub == SEEDS_KEY or not isinstance(sub_node, dict):
                continue
            if sub_node.get(SEEDS_KEY):
                yield (cat, sub, None, sub_node[SEEDS_KEY], 2)
            for typ, typ_node in sub_node.items():
                if typ == SEEDS_KEY or not isinstance(typ_node, dict):
                    continue
                if typ_node.get(SEEDS_KEY):
                    yield (cat, sub, typ, typ_node[SEEDS_KEY], 3)

# ──────────────────────────────────────────────────────────────
# Corrections cache
# ──────────────────────────────────────────────────────────────
_CORRECTIONS_CACHE: Optional[Dict[str, Dict[str, str]]] = None

def _collect_corrections(tree: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Build normalized-description → correction record.
    Includes both authoritative and soft (non-authoritative) corrections.
    """
    mapping: Dict[str, Dict[str, Any]] = {}
    for _, _, _, seeds, _ in _iter_seed_nodes(tree):
        for s in seeds:
            if not s.get("corrected_to"):
                continue
            d = normalize_desc(s.get("description", ""))
            if not d:
                continue
            mapping[d] = s
    return mapping

def get_corrections_cache() -> Dict[str, Dict[str, str]]:
    """Return corrections mapping, rebuild if cache is empty."""
    global _CORRECTIONS_CACHE
    if _CORRECTIONS_CACHE is None:
        tree = categories.load_custom_categories()
        _CORRECTIONS_CACHE = _collect_corrections(tree)
    return _CORRECTIONS_CACHE

def invalidate_corrections_cache() -> None:
    """Clear corrections cache so it will be rebuilt on next access."""
    global _CORRECTIONS_CACHE
    _CORRECTIONS_CACHE = None

def lookup_correction(description: str) -> Optional[Dict[str, str]]:
    """Return corrected_to if there is an authoritative correction for this description."""
    corrections = get_corrections_cache()
    return corrections.get(normalize_desc(description))

# ──────────────────────────────────────────────────────────────
# Helpers for corrections
# ──────────────────────────────────────────────────────────────
def _ensure_path(tree: Dict[str, Any], cat: str, sub: Optional[str], typ: Optional[str]) -> Dict[str, Any]:
    """Ensure cat/sub/typ nodes exist inside the given custom categories tree."""
    def _ensure_node(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
        node = parent.setdefault(key, {})
        if not isinstance(node, dict):
            node = {}
            parent[key] = node
        node.setdefault(SEEDS_KEY, [])
        node.setdefault(DELETED_KEY, [])
        return node

    cat = (cat or "").strip().lower()
    sub = (sub or "").strip().lower() or None
    typ = (typ or "").strip().lower() or None

    node = _ensure_node(tree, cat)
    if sub:
        node = _ensure_node(node, sub)
        if typ:
            node = _ensure_node(node, typ)
    return node

def _prune_conflicts(tree: Dict[str, Any], tx_id: Optional[str] = None, desc_norm: Optional[str] = None) -> None:
    """Remove existing conflicting seeds before adding new correction."""
    for cat, sub, typ, seeds, _ in _iter_seed_nodes(tree):
        filtered: List[Dict[str, Any]] = []
        for s in seeds:
            s_tx = s.get("transaction_id")
            s_desc_norm = normalize_desc(s.get("description", ""))
            is_prior_auth_corr = bool(s.get("corrected_to")) and bool(s.get("authoritative"))
            if (tx_id and s_tx == tx_id) or (desc_norm and is_prior_auth_corr and s_desc_norm == desc_norm):
                continue
            filtered.append(s)
        node = tree[cat]
        if sub:
            node = node[sub]
        if typ:
            node = node[typ]
        node[SEEDS_KEY] = filtered

# ──────────────────────────────────────────────────────────────
# Add correction OR seed
# ──────────────────────────────────────────────────────────────
def add_correction_or_seed(
    tx: Dict[str, Any],
    cat: str,
    sub: Optional[str],
    typ: Optional[str],
    authoritative: bool = False,
    backfill: bool = True,
) -> None:
    """
    If tx.category == 'uncategorized' and authoritative=True → save as SEED.
    Else → save as CORRECTION.
    """
    desc = tx.get("description", "")
    tx_id = tx.get("transaction_id")

    if not tx_id or not desc:
        raise ValueError("transaction_id and description required")

    if tx.get("category", "uncategorized").lower() == "uncategorized" and authoritative:
        # Save as SEED
        print("[Corrections] Promoted uncategorized transaction → SEED")
        categories.add_seed_transaction(cat, sub, typ, tx_id, desc)

        settings = config.load_settings()
        parquet_dir = Path(settings.get("consolidated_statements", ""))
        if parquet_dir.exists():
            print("[Corrections] Backfilling parquet with new seed…")
            update_categories(parquet_dir, all_tx=True)

        return

    # Else save as correction
    tree = categories.load_custom_categories()
    node = _ensure_path(tree, cat, sub, typ)
    _prune_conflicts(tree, tx_id=tx_id, desc_norm=normalize_desc(desc))

    record = {
        "transaction_id": tx_id,
        "description": desc,
        "corrected_to": {"category": cat, "subcategory": sub, "type": typ},
        "authoritative": bool(authoritative),
        "added_at": datetime.utcnow().isoformat(),
    }
    node.setdefault(SEEDS_KEY, []).append(record)
    categories.save_custom_categories(tree)
    invalidate_corrections_cache()

    print("[Corrections] Saved correction:", record)

    # backfill after any correction (even non-authoritative) IF not done as part of a bulk update
    if backfill:
        settings = config.load_settings()
        parquet_dir = Path(settings.get("consolidated_statements", ""))
        if parquet_dir.exists():
            print("[Corrections] Backfilling parquet with new correction (all_tx=True)…")
            update_categories(parquet_dir, all_tx=True)

# ──────────────────────────────────────────────────────────────
# Indices with precedence tweaks
# ──────────────────────────────────────────────────────────────
def _txid_best_map(tree: Dict[str, Any]) -> Dict[str, Tuple[str, Optional[str], Optional[str], int]]:
    """tx_id → (cat, subcat, type, eff_rank), with authoritative correction seeds preferred."""
    best: Dict[str, Tuple[str, Optional[str], Optional[str], int]] = {}
    for cat, sub, typ, seeds, rank in _iter_seed_nodes(tree):
        for s in seeds:
            txid = s.get("transaction_id")
            if not txid:
                continue
            is_auth_corr = bool(s.get("corrected_to")) and bool(s.get("authoritative"))
            eff_rank = rank + (100 if is_auth_corr else 0)
            prev = best.get(txid)
            if prev is None or eff_rank > prev[3]:
                best[txid] = (cat, sub, typ, eff_rank)
    return best

def _fuzzy_catalog(tree: Dict[str, Any]) -> List[Tuple[str, str, Optional[str], Optional[str], int]]:
    """Flatten seed descriptions into (desc_norm, cat, subcat, type, rank)."""
    out: List[Tuple[str, str, Optional[str], Optional[str], int]] = []
    for cat, sub, typ, seeds, rank in _iter_seed_nodes(tree):
        for s in seeds:
            d = normalize_desc(s.get("description", ""))
            if d:
                out.append((d, cat, sub, typ, rank))
    return out

# ──────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────
def validate_corrections(tree: Dict[str, Any]) -> List[str]:
    """Ensure all corrected_to paths exist (auto-create if missing)."""
    warnings = []
    for _, _, _, seeds, _ in _iter_seed_nodes(tree):
        for s in seeds:
            if not s.get("corrected_to"):
                continue
            ct = s["corrected_to"]
            try:
                _ensure_path(tree, ct.get("category"), ct.get("subcategory"), ct.get("type"))
            except Exception as e:
                warnings.append(f"Correction for {s.get('description')} invalid: {e}")
    return warnings

# ──────────────────────────────────────────────────────────────
# Bulk helpers for multi-select UI
# ──────────────────────────────────────────────────────────────
def add_authoritative_corrections_for_tx_ids(
    tx_ids: list[str],
    cat: str,
    sub: Optional[str],
    typ: Optional[str],
) -> None:
    """
    Create authoritative corrections for a batch of tx_ids.
    - Fetch description_norms from parquet.
    - Add authoritative correction for each unique normalized description.
    - Backfill immediately.
    """
    from app import config
    from app.services import categories as cat_svc

    settings = config.load_settings()
    parquet_dir = Path(settings.get("consolidated_statements", ""))
    if not parquet_dir.exists():
        raise FileNotFoundError("consolidated_statements path missing or invalid")

    # Collect descriptions from all parquet files
    dfs = []
    for f in collect_parquet_files(parquet_dir):
        if f.name in ("accounts.parquet",) or f.parent.name == "_meta":
            continue
        df = pd.read_parquet(f, columns=["transaction_id", "description"])
        dfs.append(df)
    if not dfs:
        return

    all_df = pd.concat(dfs, ignore_index=True)
    tx_df = all_df[all_df["transaction_id"].isin(tx_ids)]
    if tx_df.empty:
        return

    tree = cat_svc.load_custom_categories()
    for _, row in tx_df.iterrows():
        desc = str(row["description"])
        desc_norm = normalize_desc(desc)
        _prune_conflicts(tree, desc_norm=desc_norm)
        node = _ensure_path(tree, cat, sub, typ)
        record = {
            "transaction_id": row["transaction_id"],
            "description": desc,
            "corrected_to": {"category": cat, "subcategory": sub, "type": typ},
            "authoritative": True,
            "added_at": datetime.utcnow().isoformat(),
        }
        node.setdefault(SEEDS_KEY, []).append(record)

    cat_svc.save_custom_categories(tree)
    invalidate_corrections_cache()
    print(f"[Bulk] Added {len(tx_df)} authoritative corrections for {cat}/{sub}/{typ}")
    update_categories(parquet_dir, all_tx=True)


def add_seeds_for_tx_ids(
    tx_ids: list[str],
    cat: str,
    sub: Optional[str],
    typ: Optional[str],
) -> None:
    """
    Save a batch of transactions as SEEDs for the chosen category path.
    """
    from app import config
    from app.services import categories as cat_svc

    settings = config.load_settings()
    parquet_dir = Path(settings.get("consolidated_statements", ""))
    if not parquet_dir.exists():
        raise FileNotFoundError("consolidated_statements path missing or invalid")

    dfs = []
    for f in collect_parquet_files(parquet_dir):
        if f.name in ("accounts.parquet",) or f.parent.name == "_meta":
            continue
        df = pd.read_parquet(f, columns=["transaction_id", "description"])
        dfs.append(df)
    if not dfs:
        return

    all_df = pd.concat(dfs, ignore_index=True)
    tx_df = all_df[all_df["transaction_id"].isin(tx_ids)]
    if tx_df.empty:
        return

    for _, row in tx_df.iterrows():
        cat_svc.add_seed_transaction(cat, sub, typ, row["transaction_id"], row["description"])

    print(f"[Bulk] Added {len(tx_df)} seed transactions for {cat}/{sub}/{typ}")
    update_categories(parquet_dir, all_tx=True)

# ──────────────────────────────────────────────────────────────
# Main update
# ──────────────────────────────────────────────────────────────
def update_categories(
    parquet_dir: Path,
    *,
    threshold: int = 80,
    all_tx: bool = False,
    year: Optional[int] = None,
    uncategorized: bool = False,
    unsubcategorized: bool = False
) -> List[str]:
    """
    Update categories using precedence:
      0) Corrections by description (authoritative vs non-authoritative)
      1) Exact tx_id matches (authoritative seeds win)
      2) Fuzzy similarity
    Ensures description_norm is filled.
    Returns a list of per-file summary messages for UI flashing.
    """
    files = collect_parquet_files(parquet_dir)
    tree = categories.load_categories()

    for w in validate_corrections(tree):
        print("[Correction Warning]", w)

    txid_map = _txid_best_map(tree)
    fuzz_list = _fuzzy_catalog(tree)
    corrections_map = get_corrections_cache()

    per_file_results: List[Dict[str, int | str]] = []

    for f in files:
        if f.name == "accounts.parquet" or f.parent.name == "_meta":
            continue

        df = pd.read_parquet(f)
        if df.empty:
            continue

        # ──────────────────────────────
        # Ensure required columns
        # ──────────────────────────────
        for col, default in [
            ("category", "uncategorized"),
            ("subcategory", "uncategorized"),
            ("type", "unsubcategorized"),
            ("fuzzy_score", None),
        ]:
            if col not in df.columns:
                df[col] = default

        # Normalize descriptions
        if "description_norm" not in df.columns:
            df["description_norm"] = df["description"].astype(str).map(normalize_desc)
        else:
            empty_mask = df["description_norm"].isna() | (df["description_norm"].astype(str).str.strip() == "")
            if empty_mask.any():
                df.loc[empty_mask, "description_norm"] = df.loc[empty_mask, "description"].astype(str).map(normalize_desc)

        if "transaction_id" not in df.columns or "description" not in df.columns:
            raise KeyError(f"{f} missing transaction_id or description")

        # ──────────────────────────────
        # Filter eligible transactions
        # ──────────────────────────────
        mask = pd.Series(True, index=df.index)
        if not all_tx:
            if year is not None and "operation_date" in df.columns:
                op_dt = pd.to_datetime(df["operation_date"], errors="coerce")
                mask &= (op_dt.dt.year == int(year))
            if uncategorized:
                mask &= df["category"].fillna("uncategorized").str.lower().eq("uncategorized")
            if unsubcategorized:
                mask &= df["type"].fillna("unsubcategorized").str.lower().eq("unsubcategorized")

        idx_eligible = df.index[mask]
        if idx_eligible.empty:
            df.to_parquet(f, index=False)
            continue

        # ──────────────────────────────
        # 0) Corrections
        # ──────────────────────────────
        idx_corrected = pd.Index([])
        n_corrected = 0
        n_auth_corr = 0
        n_seed_corr = 0

        if corrections_map:
            desc_norms = df.loc[idx_eligible, "description_norm"].astype(str)
            take = desc_norms.map(lambda d: d in corrections_map)
            idx_corrected = desc_norms.index[take]
            for idx in idx_corrected:
                corr = corrections_map[df.at[idx, "description_norm"]]
                corrected_to = corr.get("corrected_to", {})
                df.at[idx, "category"] = corrected_to.get("category", "uncategorized")
                df.at[idx, "subcategory"] = corrected_to.get("subcategory", "uncategorized")
                df.at[idx, "type"] = corrected_to.get("type", "unsubcategorized")
                df.at[idx, "fuzzy_score"] = None

                if corr.get("authoritative", False):
                    n_auth_corr += 1
                else:
                    n_seed_corr += 1

            n_corrected = len(idx_corrected)

        # ──────────────────────────────
        # 1) tx_id seeds
        # ──────────────────────────────
        elig_ids = set(df.loc[idx_eligible, "transaction_id"].astype(str))
        auth_hits = {tx: v for tx, v in txid_map.items() if tx in elig_ids}
        idx_txid = pd.Index([])
        n_txid_seed = 0
        if auth_hits:
            hit_mask = (
                df.index.isin(idx_eligible)
                & ~df.index.isin(idx_corrected)
                & df["transaction_id"].astype(str).isin(auth_hits.keys())
            )
            idx_txid = df.index[hit_mask]
            for i in idx_txid:
                cat, sub, typ, _ = auth_hits[str(df.at[i, "transaction_id"])]
                df.at[i, "category"] = cat
                df.at[i, "subcategory"] = sub or "uncategorized"
                df.at[i, "type"] = typ or "unsubcategorized"
                df.at[i, "fuzzy_score"] = None
            n_txid_seed = len(idx_txid)

        # ──────────────────────────────
        # 2) Fuzzy similarity
        # ──────────────────────────────
        n_fuzzy = 0
        if fuzz_list:
            remaining = idx_eligible.difference(idx_corrected.union(idx_txid))
            if not remaining.empty:
                descs_norm = df.loc[remaining, "description_norm"].astype(str)
                for idx, dnorm in descs_norm.items():
                    best_score = -1
                    best_rank = -1
                    best_tuple: Optional[Tuple[str, Optional[str], Optional[str]]] = None
                    for seed_desc, cat, sub, typ, rank in fuzz_list:
                        score = fuzz.token_sort_ratio(dnorm, seed_desc)
                        if score > best_score or (score == best_score and rank > best_rank):
                            best_score = score
                            best_rank = rank
                            best_tuple = (cat, sub, typ)
                    if best_tuple and best_score >= int(threshold):
                        cat, sub, typ = best_tuple
                        df.at[idx, "category"] = cat
                        df.at[idx, "subcategory"] = sub or "uncategorized"
                        df.at[idx, "type"] = typ or "unsubcategorized"
                        df.at[idx, "fuzzy_score"] = float(best_score)
                        n_fuzzy += 1

        # ──────────────────────────────
        # Metrics summary
        # ──────────────────────────────
        n_unmatched = len(idx_eligible) - (n_auth_corr + n_seed_corr + n_txid_seed + n_fuzzy)
        if n_unmatched < 0:
            n_unmatched = 0

        df.to_parquet(f, index=False)
        per_file_results.append({
            "name": f.name,
            "eligible": len(idx_eligible),
            "auth_corr": n_auth_corr,
            "seed_corr": n_seed_corr,
            "txid_seed": n_txid_seed,
            "fuzzy": n_fuzzy,
            "unmatched": n_unmatched,
        })

    # ──────────────────────────────
    # Aggregate summaries by filename
    # ──────────────────────────────
    grouped: Dict[str, Dict[str, int]] = {}
    for r in per_file_results:
        key = r["name"]
        if key not in grouped:
            grouped[key] = {
                "eligible": 0, "auth_corr": 0, "seed_corr": 0,
                "txid_seed": 0, "fuzzy": 0, "unmatched": 0
            }
        for k in grouped[key]:
            grouped[key][k] += r[k]

    # ──────────────────────────────
    # Compact summary messages
    # ──────────────────────────────
    messages = [
        f"{name}: updated {vals['eligible']} tx "
        f"(auth_corr={vals['auth_corr']}, seed_corr={vals['seed_corr']}, "
        f"id_seed={vals['txid_seed']}, fuzzy={vals['fuzzy']}, "
        f"unmatched={vals['unmatched']})"
        for name, vals in grouped.items()
    ]

    return messages
