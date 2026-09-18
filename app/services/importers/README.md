# 🧾 Importer Architecture — `app/services/importers/`

The `importers` module is responsible for parsing raw financial statements (PDF, CSV, JSON, etc.) into structured data that can be normalized and stored in CLEAR's parquet dataset.

Each importer class:
- Targets a specific institution + account type.
- Extracts transactions, balances, and metadata from statements.
- Produces a JSON output conforming to the shared schema (`app/services/schema.json`).
- Delegates normalization and persistence to the ingestion pipeline (`normalize_parquet.py`).

---

## 🧩 Core Concepts

### 1. Importer Families

The importers are organized by account type:

| Folder | Account Type | Example | Balance Source | Transaction Source |
|---------|---------------|----------|----------------|--------------------|
| `cash/` | Everyday banking accounts (chequing, savings) | `bmo_chequing_fr.py` | Statement-reported end-of-period balance | PDF line-by-line transaction extraction |
| `credit/` | Credit card accounts (possibly multiple cardholders) | `bmo_mastercard_fr.py` | Statement-level *“Solde dû / Solde total”* (aggregated) | Per-cardholder sub-statements |
| `invest/` | Investment or brokerage accounts | `questrade_pdf.py` (for example) | NAV or portfolio snapshot | NAV entries (not transaction-based) |

All importers follow a **TemplateImporter** base pattern — defining detection, parsing, and validation methods.

---

### 2. The Importer Output Schema

Each importer produces a structured JSON document with up to these keys:

```json
{
  "export_date": "2025-08-15 10:00:00",
  "document_signature": {"document_type": "BMO_MASTERCARD_FRENCH"},
  "account_summary": {...},
  "statements": [...],
  "nav_snapshots": [...]
}
```

#### Key Components

| Field | Purpose |
|-------|----------|
| **`account_summary`** | Global metadata about the account (ID, class, side, balance). |
| **`statements`** | One or more cardholders or statement segments, each with its own transactions and validation summary. |
| **`nav_snapshots`** | Optional for investment accounts — NAV values over time instead of discrete transactions. |

---

## 💳 Credit Importers — Parent Account Design

Credit card importers are more complex than cash accounts because each statement can include **multiple cardholders** under a **shared total balance**.

Example:  
> A BMO MasterCard statement lists separate pages for “Alice” and “Bob” cardholders,  
> but the “Solde dû” line at the top of the statement is **one combined balance**.

To handle this correctly:

- Each **cardholder** becomes a *child account*:
  ```
  account_id: bmo_credit_1163
  parent_account_id: bmo_credit_combined
  ```
- Each child has its own **transactions**, but **no balance** (since that would double-count).
- A **parent account** `bmo_credit_combined` is created automatically by the importer using `account_summary.current_balance`.
  - This parent stores the **true statement balance** (`Solde dû`, negative for liabilities).
  - It’s written into `_meta/account_balances.parquet` as the authoritative source.

At runtime, the `/accounts` route:
- Hides the child accounts if the parent exists.
- Displays one aggregated card with a child list of cardholders.
- Uses the parent’s `current_balance.amount` for the displayed balance.

This ensures:
✅ Accurate totals in net worth calculations.  
✅ Correct parent-child visual hierarchy.  
✅ No duplicate liabilities across cardholders.

---

## 💰 Cash Importers

Cash importers (e.g., `bmo_chequing_fr.py`) represent simpler accounts where:
- Each statement corresponds to a single account.
- The statement explicitly reports:
  - **Opening balance**
  - **Closing balance**
  - **List of transactions**
- The importer extracts transactions and validates totals against the statement summary.

`normalize_parquet.py` then:
- Registers the account in `_meta/accounts.parquet`.
- Appends all transactions to the partitioned `year=/month=/transactions.parquet` files.
- Adds any end-of-period balance to `_meta/account_balances.parquet` for completeness.

Balances from statement summaries are always prioritized over derived ones (see below).

---

## 📈 Investment Importers

Investment importers differ conceptually:
- They may not contain *transactions*.
- Instead, they export **NAV snapshots** (`nav_snapshots` list).
- These snapshots (e.g., daily or monthly portfolio values) are appended into `_meta/nav_snapshots.parquet`.
- In `/accounts`, they behave like assets with continuously tracked balance history.

---

## ⚙️ Balance Priority Hierarchy

The query engine (`queries.py → net_worth_accounts_snapshot`) combines all data sources to determine the latest known balance per account, with the following priority:

| Rank | Source | Description |
|------|---------|-------------|
| 1️⃣ | `_meta/account_balances.parquet` | **Authoritative statement summary** (e.g., credit card “Solde dû”) |
| 2️⃣ | `_meta/manual_values.parquet` | Manually entered balances |
| 3️⃣ | `_meta/nav_snapshots.parquet` | Investment NAV or portfolio value snapshots |
| 4️⃣ | Transactions (derived) | Cumulative transactional amounts over time |

This design ensures that **statement-provided balances always override** inferred or manual values when available.

---

## 🧠 Design Philosophy

1. **Separation of Concerns**  
   Importers parse raw data.  
   Normalization handles persistence and schema integrity.  
   Queries handle aggregation and visualization.

2. **Declarative Priority**  
   Each balance source defines its trust level.  
   The UI always prefers the most authoritative data available.

3. **Parent-Child Clarity**  
   Child accounts provide transactional visibility.  
   Parent accounts provide financial truth.

4. **Future Extensibility**  
   New importers can be added easily (e.g., new institutions, new formats).  
   The same schema and priority logic apply automatically.

---

## 📦 Example Data Flow (BMO MasterCard FR)

```
BMO_MASTERCARD_FRENCH.pdf
        ↓
BmoMastercardFrImporter.parse_to_json()
        ↓
normalize_parquet.ingest_pdf_with_importer_json()
        ↓
    ├── transactions → year=2025/month=08/transactions.parquet
    ├── accounts → _meta/accounts.parquet
    ├── balances → _meta/account_balances.parquet
    └── meta → _meta/statement_sources.parquet
        ↓
net_worth_accounts_snapshot()
        ↓
    → /accounts route (merged, deduped, prioritized)
```

Result in UI:
```
BMO MasterCard (Combined)      -453.47
BMO                            as of Aug 15, 2025 [statement]
👥 Cardholders:
  • BMO credit card 1163
  • BMO credit card 6057
```

---

## 🧩 Summary

| Account Type | Has Transactions | Has Balance | May Have Parent | Stored In |
|---------------|------------------|--------------|------------------|-----------|
| **Cash / Chequing** | ✅ | ✅ (from statement) | ❌ | `_meta/account_balances.parquet` + partitioned transactions |
| **Credit Cards** | ✅ (per cardholder) | ✅ (parent only) | ✅ | `_meta/account_balances.parquet` + `_meta/accounts.parquet` |
| **Investments** | ❌ (NAV only) | ✅ (NAV snapshots) | ❌ | `_meta/nav_snapshots.parquet` |

---

## 🧱 Adding a New Importer

To add a new importer for another institution or account type:

1. **Create a new file** under the correct subfolder, e.g.:
   ```
   app/services/importers/cash/mybank_chequing_en.py
   ```

2. **Subclass the base template:**
   ```python
   from ..template import TemplateImporter

   class MyBankChequingEnImporter(TemplateImporter):
       key = "cash.mybank_chequing_en"
       label = "MyBank Chequing (EN) PDF"
       account_side = "asset"
       account_class = "chequing"
       input_kind = "pdf"
       signature_name = "MYBANK_CHEQUING_EN"
       institution_default = "MyBank"

       def detect(self, path: Path) -> bool:
           # Implement logic to identify this document type
           ...

       def parse_to_json(self, path: Path) -> Dict[str, Any]:
           # Extract all relevant info (transactions, summary)
           # Return schema-compliant dict
           ...
   ```

3. **Validate output** using the shared schema:
   ```python
   from jsonschema import validate
   validate(instance=output, schema=_SCHEMA)
   ```

4. **Ingest into the system** with:
   ```bash
   python app/scripts/ingest_pdf.py --path /path/to/file.pdf --importer cash.mybank_chequing_en
   ```

This will handle normalization, parquet writes, and metadata updates automatically.

---

### 🔖 TL;DR

- **Cash accounts** → one account, one balance, many transactions.  
- **Credit accounts** → many cardholders, one combined balance.  
- **Investment accounts** → one account, many NAV values, no transactions.  
- **Balances** → always prefer statement summaries f