# 🧱 Normalization Pipeline — `app/services/normalize_parquet.py`

This module is the **bridge** between raw importer outputs (from `app/services/importers/`) and the unified, queryable parquet dataset that powers CLEAR.
It standardizes, validates, and writes statement data into the correct meta and transactional structures.

---

## 🧩 Purpose

Importers extract structured JSON data from source documents (PDF, CSV, etc.).  
`normalize_parquet.py` takes that JSON and **normalizes it into four canonical tables**:

1. **Transactions** → partitioned parquet files (`year=/month=/transactions.parquet`)
2. **Meta (Statement Sources)** → `_meta/statement_sources.parquet`
3. **Accounts** → `_meta/accounts.parquet`
4. **Balances** → `_meta/account_balances.parquet`

It ensures consistent schema, deduplication, and proper cross-referencing between accounts, cardholders, and balances.

---

## ⚙️ Core Responsibilities

### 1. Normalize importer JSON

The main entry point, `normalize_importer_json()`, converts a validated importer output into four pandas DataFrames:

```python
transactions_df, meta_df, accounts_df, balances_df = normalize_importer_json(
    importer_json, institution, account_side, account_class, source_pdf
)
```

Each importer output can represent one of several types of financial accounts:

| Account Type | JSON Key | Produces Transactions? | Produces Balances? |
|---------------|-----------|------------------------|--------------------|
| Cash / Chequing | `statements` | ✅ | ✅ (from closing balance) |
| Credit Card | `statements` + `account_summary` | ✅ (per cardholder) | ✅ (combined parent) |
| Investment | `nav_snapshots` | ❌ | ✅ (from NAV data) |

### 2. Record Metadata

Every ingested statement is uniquely identified by a **SHA-256 hash** of its file contents, stored in:

```
_meta/statement_sources.parquet
```

Fields include:
- `source_statement_id`: stable 16-character hash ID
- `file_sha256`: content checksum
- `document_type`: importer signature name
- `account_id`, `institution`, `account_side`, `account_class`

This ensures re-ingestion of the same PDF won’t duplicate records.

### 3. Maintain Account Registry

Each account (chequing, cardholder, or investment) is persisted in:
```
_meta/accounts.parquet
```

This table includes:
- `account_id`
- `account_number`
- `institution`
- `account_side` (asset/liability)
- `account_class` (chequing, credit_card, brokerage, etc.)
- `parent_account_id` (for multi-card credit statements)

Parent accounts are automatically created for credit statements when a combined `account_summary.current_balance` is found.

### 4. Persist Balances

Balances extracted from statement summaries (authoritative) are written to:
```
_meta/account_balances.parquet
```

These values are then prioritized by the query engine (`queries.py → net_worth_accounts_snapshot`) above any derived or manual balances.

### 5. Write Transactions

All normalized transactions are partitioned by year/month for efficient querying:
```
year=2025/month=08/transactions.parquet
```

Duplicate entries (matching `transaction_id`) are replaced on re-ingestion.

### 6. Append NAV Snapshots

Investment importers can output `nav_snapshots`, which are appended to:
```
_meta/nav_snapshots.parquet
```

Each snapshot includes `account_id`, `nav_value`, `nav_date`, and source information.

---

## 🔄 Relation to Importers

`normalize_parquet.py` is the **downstream consumer** of all importer outputs.  
It translates each importer’s JSON schema into CLEAR’s internal parquet model.

```
PDF → Importer.parse_to_json() → normalize_parquet.ingest_pdf_with_importer_json()
```

### Step-by-step pipeline

1. **Importer produces JSON:**
   - Extracts transactions and balances.
   - Provides `account_summary`, `statements`, and/or `nav_snapshots`.

2. **`normalize_importer_json()` parses and flattens:**
   - Converts JSON into four tabular DataFrames.
   - Ensures consistent columns and IDs.
   - Adds `parent_account_id` for multi-card statements.

3. **`ingest_pdf_with_importer_json()` writes data:**
   - Calls helper functions to append transactions, metadata, accounts, and balances.
   - Deduplicates by statement ID and transaction ID.

4. **Query layer consumes parquet files:**
   - The `queries.py` module merges these tables for UI display (`/accounts`, `/transactions`, etc.).

---

## 🧱 Key Components

### `_file_sha256()` and `_stable16()`
Ensure consistent and deterministic statement IDs based on file content and document metadata.

### `_upsert_meta()` and `_upsert_accounts()`
Safely merge new statement metadata and account definitions without losing previous entries.

### `write_partitioned()`
Writes transactions into partitioned directories by year and month, for optimized DuckDB scanning.

### `_append_nav_parquet()`
Handles investment account NAV data and merges historical entries.

### `mb.upsert()`
Uses the `meta_balances` service to persist authoritative statement balances in `_meta/account_balances.parquet`.

---

## 🪄 Data Integrity Guarantees

- **Deduplication:** Statements and transactions are idempotent (re-importing the same PDF doesn’t duplicate records).
- **Schema Stability:** Every parquet file written has a consistent set of columns.
- **Referential Integrity:** Account IDs are consistent across transactions, balances, and NAVs.
- **Parent Awareness:** Child cardholders link to parent accounts using `parent_account_id`.
- **Traceability:** Each transaction, balance, or NAV record includes its originating `source_file` and `file_sha256`.

---

## 📈 Example Flow

### Ingesting a BMO MasterCard Statement
```
PDF (BMO_MASTERCARD_FRENCH.pdf)
        ↓
BmoMastercardFrImporter.parse_to_json()
        ↓
normalize_parquet.ingest_pdf_with_importer_json()
        ↓
├── year=2025/month=08/transactions.parquet
├── _meta/accounts.parquet
├── _meta/account_balances.parquet
└── _meta/statement_sources.parquet
```

### Ingesting a BMO Chequing Statement
```
PDF (BMO_CHEQUING_FR.pdf)
        ↓
BmoChequingFrImporter.parse_to_json()
        ↓
normalize_parquet.ingest_pdf_with_importer_json()
        ↓
├── year=2025/month=08/transactions.parquet
├── _meta/accounts.parquet
├── _meta/account_balances.parquet
└── _meta/statement_sources.parquet
```

### Ingesting an Investment Account Snapshot
```
PDF (QUESTRADE_NAV.pdf)
        ↓
QuestradeImporter.parse_to_json()
        ↓
normalize_parquet.ingest_pdf_with_importer_json()
        ↓
└── _meta/nav_snapshots.parquet
```

---

## 🧠 Summary

| File / Table | Role | Created / Updated By |
|---------------|------|-----------------------|
| `transactions.parquet` | Individual transactions | `write_partitioned()` |
| `_meta/accounts.parquet` | Registry of all accounts | `_upsert_accounts()` |
| `_meta/account_balances.parquet` | Authoritative end-of-statement balances | `mb.upsert()` |
| `_meta/statement_sources.parquet` | Unique statement registry | `_upsert_meta()` |
| `_meta/nav_snapshots.parquet` | Investment NAV values | `_append_nav_parquet()` |

---

### 🔖 TL;DR
- **Importers extract → normalizer persists.**
- Handles cash, credit, and investment types uniformly.
- Ensures balance integrity, parent/child structure, and reproducible ingestion.
- Feeds the `/accounts` and `/transactions` UIs with reliable parquet
