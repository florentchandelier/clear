# Automatic Categorization — How It Works

Reference: `app/services/update_categories.py` → `update_categories()`

---

## Overview

When a transaction is added as a seed or correction, `update_categories()` re-runs
categorization across all Parquet files. It applies three strategies in strict
precedence order. A transaction is assigned by the first strategy that matches it;
lower-priority strategies are skipped.

---

## Matching strategies (in precedence order)

### 0 — Correction by description (exact `description_norm` match)

Seeds stored with a `corrected_to` field are indexed in a `corrections_map` keyed by
`normalize_desc(description)`. At categorization time, each transaction's
`description_norm` is looked up in the map — **exact string match only**.

- Authoritative corrections (`authoritative: true`) have global, permanent priority.
- Non-authoritative corrections behave the same at match time but carry lower intent.
- A match requires the normalized description to be **identical** to the stored seed
  description after normalization.

### 1 — Transaction ID seed (exact `transaction_id` match)

Seeds that carry a `transaction_id` are matched against the exact transaction they
were created from. This strategy never generalizes to other transactions.

### 2 — Fuzzy similarity (`fuzz.token_sort_ratio`, default threshold 80)

All seeds (with or without `corrected_to`) contribute their normalized description to
a fuzzy catalog. Every transaction not yet matched by steps 0–1 is scored against
every catalog entry using `rapidfuzz.fuzz.token_sort_ratio`. If the best score meets
or exceeds the threshold (default: 80), the transaction is assigned that category.
The score and the matched category are written back to the Parquet file.

---

## Description normalization

Both seed descriptions and transaction descriptions pass through `normalize_desc()`
before any comparison:

```python
desc = desc.lower()
desc = re.sub(r"[0-9]", "", desc)      # remove all digits
desc = re.sub(r"[^\w\s]", "", desc)    # remove punctuation
desc = re.sub(r"\s+", " ", desc)       # collapse whitespace
return desc.strip()
```

This is the single source of truth — identical logic is mirrored in
`queries.py` (`_sql_normalize_desc`) for SQL-side consistency.

---

## Merchant with variable order numbers — example

Amazon transactions embed a unique order number in the description:

```
AMZN Mktp CA*AS6NM5O23  →  amzn mktp caasnmo   (digits stripped, * stripped)
AMZN Mktp CA*K23WH9Q73  →  amzn mktp cakwhq
```

Digits are removed but **letters in the order number survive**. Each transaction
therefore has a distinct normalized description. Consequences:

| Strategy | Result |
|---|---|
| Step 0 (exact correction) | Only matches the exact order it was seeded from |
| Step 1 (tx_id) | Only matches the exact transaction |
| Step 2 (fuzzy) | `token_sort_ratio("amzn mktp caasnmo", "amzn mktp cakwhq")` ≈ 78% — below the default threshold of 80 → **no match** |

A single Amazon seed does **not** reliably generalize to a different Amazon order
under default settings.

---

## Practical guidance by merchant type

### Stable descriptions (e.g. `NETFLIX.COM`, `SPOTIFY AB`, utility billers)

Normalized description is identical across all transactions from the same merchant.
A single seed or correction is sufficient — step 0 or step 2 will match all future
transactions from that merchant.

### Variable descriptions with fixed prefix (e.g. Amazon, recurring subscriptions
with invoice numbers)

Letters from the variable suffix survive normalization and produce per-transaction
variation. Reliable classification requires one of:

1. **Accumulate multiple seeds**: the fuzzy catalog grows denser; new transactions
   become more likely to score ≥ 80 against at least one seed.
2. **Lower the similarity threshold** (Settings → Similarity Threshold): increases
   recall across all categories — evaluate false-positive risk before lowering.
3. **Accept per-transaction seeding**: for infrequent high-value merchants, seed
   each transaction individually as it appears.

### Ambiguous descriptions (same merchant, multiple categories)

Some merchants use a single description for transactions that belong to different
categories (e.g. a department store charging for food and for clothing). Seeds and
corrections match on description alone — they cannot distinguish by amount, weekday,
or account. For these cases, per-transaction (tx_id) correction via the transactions
view is the only reliable approach. See priorities.md §0 for planned improvements.

---

## Threshold configuration

The fuzzy threshold is read from `settings.json` (`similarity_threshold`, default 80).
It applies globally to all fuzzy matches. There is no per-category threshold.

Lowering the threshold increases automatic recall but risks mis-categorizing
transactions with superficially similar descriptions across different merchants.

---

## Key invariant

`normalize_desc()` in Python and `_sql_normalize_desc()` in SQL must remain
identical. If they diverge, corrections applied at categorization time will not
match the SQL-side `description_norm` column used by `_BASE_CTE`, breaking
correction lookup for queries. See `guardrails.md` module ownership for the
change-impact rule.
