# 🪙 CLEAR — `app/`

A modular, Python-based **personal finance management system** that normalizes, categorizes, and analyzes financial data stored in **Parquet**.
It supports **PDF and JSON imports**, **automatic categorization**, **hierarchical tagging**, and a full **Flask web UI** for visualization.

This is the architecture reference for the `app/` package. For install/run
instructions, the demo dataset, and contributing, see the
[top-level README](../README.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
and [`SECURITY.md`](../SECURITY.md).

---

## 🚀 Overview

The app processes raw financial statements (banking, credit, or investment) into a unified dataset through three coordinated stages:

| Stage | Module | Purpose |
|-------|---------|----------|
| **1. Importers** | `app/services/importers/` | Parse raw documents (PDF/JSON) into structured schema. |
| **2. Normalization** | `app/services/normalize_parquet.py` | Convert importer JSON into parquet tables with deduplication. |
| **3. Queries** | `app/services/queries.py` | Run analytical queries (spending, income, net worth). |

Each layer is isolated but connected, forming a complete ingestion → normalization → analysis pipeline.

---

## 🗂️ Profiles: demo vs. personal

Every path below (`consolidated_statements/`, `categories_custom.json`,
`settings.json`) is **profile-relative**, not fixed. `app/config.py` owns
this resolution — see [Configuration](#️-configuration) — with precedence:

1. `MINT_PROFILE_DIR` env var, if set.
2. `personal/settings.json`, if it already exists.
3. `var/demo/` — the default, synthetic profile, built by `make demo`
   from the tracked seed under `data/demo_seed/`.

`categories.json` (the base category template) is the one exception:
it's tracked and shared by every profile, never profile-scoped.

---

## ✨ Features

### 1. Import & Normalize Statements

- Converts PDF or JSON statements into standardized **transaction + balance tables**.
- Handles **cash**, **credit**, **investment**, **line-of-credit**, and **asset-valuation** accounts differently:
  - Cash → single account per statement.
  - Credit → multiple cardholders, one combined parent account.
  - Investment → NAV-only accounts (no transactions).
  - Line of credit / asset valuation (home, car) → balance- or NAV-style entries, no transactions.
- Ensures:
  - **Unique `transaction_id`** and **`source_statement_id`**.
  - Deduplication across re-ingested statements.
  - Storage in `year=/month=/transactions.parquet` partitions, under the active profile's `consolidated_statements/`.
  - Parent–child account relationships for credit cards.

### 2. Categorization & Seeds

- Category hierarchy: `category → subcategory → type`.
- Base template in `categories.json` (tracked); your overrides and corrections in the active profile's `categories_custom.json`.
- Managed via `/categories/manage`.
- **Seed transactions** drive consistent categorization.
- Supports **fuzzy matching** using RapidFuzz for unseeded transactions.
- CRUD support through CLI or web interface.

### 3. Queries & Analytics

- Backed by **DuckDB**, supporting real-time aggregation.
- Built-in queries for:
  - **Spending** (category, tag, institution, cardholder, month).
  - **Income vs Expense** over time.
  - **Net worth** (from transactions, manual values, and NAVs).
- Handles manual and investment balances seamlessly.
- All queries exposed via `run_query(name, parquet_path)`.

### 4. Web Interface (Flask)

Served by `app/web/flask_ui.py` (the `web_ui` entry point — `make run` /
`python run.py web_ui`), which registers `routes_ui.py` and
`routes_categories.py`:

- `/` → Dashboard: spending & net worth visualization.
- `/accounts` → Latest asset/liability balances.
- `/transactions/view` → Transaction browser.
- `/categories/manage` → Manage categories & seeds.
- `/import` → Upload PDFs or JSONs for ingestion (preview, then commit).
- `/settings` → View the active profile and configure thresholds/paths.
- `/api/*` → JSON endpoints the pages above call (dashboard aggregates,
  transaction filters, category tree, import preview/commit/reject).

### 5. CLI Interface

Run categorization and queries directly from the command line (`app/cli/cli.py`, via `run.py`):

```bash
# Run a query by name (defaults to the active profile's data if --parquet is omitted)
python run.py query --name total_spending_by_category
python run.py query --name spending_by_month_for_year --year 2025

# Apply categorization (seeds + fuzzy matching) to a profile's Parquet
python run.py update_categories --threshold 85
```

Ingesting a statement (PDF → Parquet) is done through the web UI's
`/import` flow (`app/web/routes_ui.py`), which drives
`app/services/normalize_parquet.py:ingest_pdf_with_importer_json()` —
the only ingestion path in the app. There is no standalone `normalize`
CLI command: an earlier one called a same-named function on the wrong
module and crashed unconditionally, and its only real destination
(`app/services/normalize.py`, a self-contained pipeline for a JSON shape
no current importer produces) had no other caller, so both were removed
rather than patched (BUG-06). See
`python run.py --help` and `python run.py <command> --help` for every
remaining subcommand and flag (category CRUD, seeds, manual
accounts/values, etc.).

---

## 🧱 Directory Structure

```
app/
  cli/
    cli.py                     # Command-line interface
  services/
    importers/                 # Institution-specific importers, one subpackage per account side
      asset/                   # car_fmv, home_fmv valuation importers
      cash/                    # chequing
      credit/                  # credit card (multi-cardholder)
      investment/              # NAV-snapshot importers
      loc/                     # line of credit / HELOC
      base.py                  # Importer protocol, ImporterJson TypedDicts
      registry.py              # importer_by_key(), list_importers(), public_catalog()
      schema.json              # shared ImporterJson schema, validated on parse_to_json()
      template.py              # TemplateImporter base class
    normalize_parquet.py       # Normalize importer JSON → Parquet
    queries.py                 # DuckDB-based query engine
    categories.py              # Category + tag management
    update_categories.py       # Apply seeds & fuzzy matching
    meta_balances.py           # Statement balance persistence
    manual_values.py           # Manual FMV/liability value CRUD
    fx/                        # FX rate fetch/store/resolve
  web/
    flask_ui.py                # Flask app factory -- the real entry point (`make run`)
    routes_ui.py                # UI + /api routes (dashboard, accounts, transactions, import, settings)
    routes_categories.py       # Category CRUD endpoints (/categories/*)
    templates/                 # Jinja2 HTML templates
    static/js/                 # JS assets
  config.py                    # Account model constants + profile/settings resolution
categories.json                 # Hierarchical category and seed definitions (tracked, shared by every profile)
data/demo_seed/                 # Tracked, synthetic input for the demo profile (see its own README)
personal/                       # Gitignored -- your real data, if you use any (see its own README)
var/demo/                       # Gitignored, disposable -- the built demo profile (`make demo`)
```

---

## 🔗 Module Relationships

| Module | Role | Key Interaction |
|--------|------|----------------|
| `importers/` | Parse raw data | Feed importer JSON to normalization |
| `normalize_parquet.py` | Normalize & persist | Writes `_meta/` parquet structure |
| `meta_balances.py` | Manage statement balances | Ensures authoritative account balances |
| `queries.py` | Analyze | Aggregates spending, income, and balances |
| `categories.py` | Classify transactions | Supplies categories/tags for query filtering |
| `config.py` | Resolve active profile | Every module above reads its paths through `config.load_settings()` |
| `web/routes_ui.py` | Display data | Uses `queries.run_query()` for dashboards |

---

## ⚙️ Data Storage Schema

All normalized data is stored under the active profile's
`consolidated_statements/` (e.g. `var/demo/consolidated_statements/` or
`personal/consolidated_statements/` — never a single fixed path), as
partitioned parquet files:

```
consolidated_statements/
├── year=2025/
│   └── month=8/transactions.parquet
└── _meta/
    ├── accounts.parquet
    ├── account_balances.parquet
    ├── manual_values.parquet
    ├── nav_snapshots.parquet
    └── statement_sources.parquet
```

---

## 🧠 Balance Priority Logic

When calculating account balances (e.g., `/accounts` page):

| Rank | Source | Description |
|------|---------|-------------|
| 1️⃣ | Statement Summary | From `_meta/account_balances.parquet` (authoritative) |
| 2️⃣ | Manual Values | User-entered fair market or debt values |
| 3️⃣ | NAV Snapshots | From investment account imports |
| 4️⃣ | Transactions | Derived from cumulative sums |

---

## ⚙️ Configuration

Owned by `app/config.py`. Account model constants:

```python
ACCOUNT_SIDES = ["asset", "liability"]

ACCOUNT_CLASSES_BY_SIDE = {
    "asset":     ["cash", "investment", "home_fmv", "car_fmv", "private_equity", "insurance_fv", "other_fmv"],
    "liability": ["loan", "loc", "credit_card", "other_debt"],
}
```

Settings are **not** a single fixed dict — `load_settings()` resolves the
active profile (see [Profiles](#️-profiles-demo-vs-personal)) and returns
that profile's settings, merging a fresh profile's fixed defaults
(`_STATIC_DEFAULTS`: `similarity_threshold`, `min_cluster_tx`,
`single_tx_bucket`, `overwrite_duplicates`, `debug`, `log_level`,
`display_currency`, `fx_supported_currencies`) with whatever that
profile's own `settings.json` overrides. Per-profile paths
(`consolidated_statements`, `custom_categories_path`) are computed
relative to the active profile directory, except `base_categories_path`,
which always points at the tracked, repo-root `categories.json`.

```python
# What load_settings() returns for a fresh profile -- not a literal
# module constant; see app/config.py:_default_settings_for_profile().
{
    "consolidated_statements": "<profile_dir>/consolidated_statements",
    "custom_categories_path": "<profile_dir>/categories_custom.json",
    "base_categories_path": "<repo_root>/categories.json",
    "similarity_threshold": 80,
    "min_cluster_tx": 1,
    "single_tx_bucket": "OTHER_SMALL_CLUSTER",
    "overwrite_duplicates": False,
    "display_currency": "CAD",
    "fx_supported_currencies": ["CAD", "USD", "EUR"],
}
```

`MINT_DATA_DIR` (env var) overrides `consolidated_statements` only, for
that process's lifetime — never persisted into a profile's `settings.json`.
`load_settings(create_missing=False)` is a read-only mode (used by
`app/logging_config.py`, which runs on any import of the `app` package)
that never creates or repairs a missing/corrupt settings file as a side
effect.

---

## 🧮 Example Data Flow

```
BMO_MASTERCARD_FRENCH.pdf
        ↓
Importer.parse_to_json()
        ↓
normalize_parquet.ingest_pdf_with_importer_json()
        ↓
Parquet files written to <profile>/consolidated_statements/{_meta/,year=/month=/}
        ↓
queries.run_query('net_worth_accounts_snapshot')
        ↓
Flask /accounts page displays unified balances
```

---

## 📈 Data Model Snapshot

| Table | Description |
|--------|-------------|
| `transactions.parquet` | Transaction-level data |
| `_meta/accounts.parquet` | Account registry |
| `_meta/account_balances.parquet` | Authoritative end-of-statement balances |
| `_meta/manual_values.parquet` | Manual FMV or liability entries |
| `_meta/nav_snapshots.parquet` | Investment NAV values |
| `_meta/statement_sources.parquet` | Provenance / dedup record per ingested statement |

---

## 🧰 Requirements

See [`requirements.txt`](../requirements.txt) (runtime) and
[`requirements-dev.txt`](../requirements-dev.txt) (adds `pytest`) —
`make install` installs both. No minimum Python version is declared or
enforced anywhere in the repo; this project's own `venv/` is built with
Python 3.12. Real PDF parsing (`camelot-py`) additionally needs the
`ghostscript` system package — not required for `make demo`, `make test`,
or day-to-day dashboard development.

---

## 🪄 Notes

- Use **`amount`** (signed) + `transaction_type` for accurate flow analysis.
- Manual values forward-fill monthly until replaced.
- Categories and subcategories are stored lowercase.
- Seeds always override fuzzy-matched categories.
- Statement balances override derived ones (see Balance Priority Logic above).
