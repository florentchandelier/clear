# Corrections, Seeds, and Fuzzy Matching Workflow

This document summarizes how transaction categorization works with corrections, seeds, and fuzzy similarity.

## 1. Seeds
- **Seeds** represent known authoritative examples of transaction descriptions for a given category/subcategory/type.
- Seeds are stored in `categories_custom.json` under the `_seeds` list of each node.
- They are added:
  - Manually (user correction promoted from "uncategorized")
  - From importer defaults or user input
- Seeds participate in **fuzzy similarity matching**.

## 2. Corrections
- **Corrections** are applied when a transaction has been misclassified or needs a one-time/manual reassignment.
- Stored in `categories_custom.json` as special seed-like entries with a `corrected_to` field.
- Two types:
  - **One-time correction** (`authoritative = False`): applies only to the specific transaction ID.
  - **Authoritative correction** (`authoritative = True`): propagates to all future similar transactions (matched by normalized description).

## 3. Corrections vs. Seeds
- If a transaction was originally *uncategorized* and the user applies an authoritative correction → it is promoted to a **Seed** (not a correction).
- Otherwise, it is saved as a correction record.

## 4. Fuzzy Matching
- Implemented in `update_categories.py`.
- Operates exclusively on **normalized descriptions (`description_norm`)**:
  - Lowercased
  - Digits removed
  - Punctuation removed
  - Spaces collapsed
- Catalog: seeds are flattened into `(desc_norm, cat, subcat, type, rank)`.
- Matching: for each transaction, compute `fuzz.token_sort_ratio(dnorm, seed_desc)` and assign if above threshold.

## 5. Processing Order
When updating parquet transactions (`update_categories`):
1. **Corrections by description_norm** (authoritative corrections win first).
2. **Transaction ID matches** (seeds or corrections by tx_id).
3. **Fuzzy similarity** against seeds catalog.

## 6. Backfill & Propagation
- After adding a correction or seed, parquet files are automatically reprocessed (backfilled) if `authoritative = True` or if a new Seed was created.
- Ensures consistent categorization across all past and future transactions.

---

**Key Files:**
- `app/services/update_categories.py` — logic for applying corrections, seeds, and fuzzy matching.
- `categories_custom.json` — storage for user-defined seeds and corrections.
- `queries.py` — ensures `description_norm` is always available for analysis.

