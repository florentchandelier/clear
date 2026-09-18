# CLEAR

> ## Are your finances CLEAR?

**CLEAR** is a private, local-first personal finance application built around a simple idea:

**better financial decisions start with a complete, honest view of your financial life.**

CLEAR brings your accounts, transactions, investments, assets, and liabilities together in one place so you can understand where your money goes, what you own, what you owe, and what your financial reality means for the life you want to build.

It is designed to help answer both today's questions and tomorrow's:

* Where does my money actually go?
* What is my financial position today?
* What patterns are shaping my future?
* What can I change to move closer to my goals?
* What does a realistic retirement plan look like for my life?

No generic rules. No hidden assumptions. No cloud account, hosted backend, or telemetry.

Just your numbers, your life, and a clearer foundation for action.

---

## The CLEAR philosophy

|                                       |                                                                                                                   |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **C — Capture every number**          | Bring together the financial data that defines where you are today.                                               |
| **L — Look at the whole picture**     | See income, expenses, assets, liabilities, savings, investments, and long-term needs together — not in isolation. |
| **E — Evaluate objectively**          | Let the numbers describe reality without shortcuts, assumptions, or wishful thinking.                             |
| **A — Account for real life**         | Reflect how you actually live: your priorities, family, goals, lifestyle, and changing needs.                     |
| **R — Respond with a realistic plan** | Turn a clear financial picture into practical decisions for today, tomorrow, and retirement.                      |

**Capture. Look. Evaluate. Account. Respond.**

### Get CLEAR about your financial life.

---

# Product principles

## Your whole financial life, in one view

Personal finance rarely exists in neat categories.

Your spending affects your savings.
Your savings affect your investments.
Your investments affect your retirement.
Your home, debt, family, lifestyle, and future plans affect all of it.

CLEAR is built to treat those things as one connected financial picture.

## Start with reality

Financial planning is only as useful as the information underneath it.

CLEAR starts with what actually happened:

* real transactions
* real account balances
* real assets
* real liabilities
* real spending patterns
* real corrections you make yourself

From there, you can build a plan based on your life rather than a generic model.

## Private by design

CLEAR is local-first.

Your personal financial data stays in your own local environment. There is no required cloud account, hosted backend, or telemetry.

Demo data and personal data are also deliberately separated so you can explore the application without touching your real financial information.

---

# Getting started

## Quickstart

```bash
git clone <this-repo-url>
cd clear

make install   # creates venv/, installs runtime + test dependencies
make demo      # builds a synthetic demo profile — no real data needed
make run       # starts the web UI at http://127.0.0.1:5000
```

That's the full path from a fresh clone to a populated dashboard.

The demo profile is entirely synthetic: fabricated transactions, fake merchant names, and invented account numbers.

See [`data/demo_seed/README.md`](data/demo_seed/README.md) for exactly how it is built and why it requires no PDF-parsing system dependency.

Run the full test suite with:

```bash
make test
```

Or use:

```bash
make test-fast       # skip slow markers
make test-importers  # run importer contract tests only
```

---

# Using CLEAR with your own data

Demo mode and personal mode are two separate, isolated profiles that never overlap.

Create your personal configuration:

```bash
cp personal/settings.example.json personal/settings.json
```

The moment `personal/settings.json` exists, every command uses the personal profile instead of the demo profile.

Your real statements, corrections, and settings live under:

```text
personal/
```

That entire directory is gitignored.

See [`personal/README.md`](personal/README.md) for the full contract, including:

* configuration precedence rules
* what gets written where
* the demo/personal isolation model
* what should never be committed

To switch back to demo mode, delete or rename `personal/settings.json`.

Nothing else changes.

---

# Importing financial data

CLEAR imports financial statements through the web UI.

From the **Import** page:

1. Upload a PDF.
2. Select the matching institution.
3. CLEAR detects the statement type.
4. Review the parsed preview.
5. Confirm the import.
6. Only then is data written.

Currently supported importers:

| Institution                          | Account type   | Importer key                         |
| ------------------------------------ | -------------- | ------------------------------------ |
| BMO (French chequing)                | Cash           | `cash.bmo_chequing_fr`               |
| BMO (French Mastercard)              | Credit card    | `credit.bmo_mastercard_fr`           |
| BMO (French HELOC/LOC)               | Line of credit | `loc.bmo_heloc_fr`                   |
| BMO Nesbitt Burns                    | Investment     | `investment.bmo_nesbitt_ca`          |
| Questrade                            | Investment     | `investment.questrade_equity`        |
| Interactive Brokers (CA)             | Investment     | `investment.interactive_brokers_ca`  |
| CarGurus valuation                   | Vehicle FMV    | `asset.car_fmv_cargurus`             |
| Quebec municipal évaluation foncière | Home FMV       | `asset.home_fmv_evaluation_fonciere` |

Adding a new institution means implementing the `Importer` protocol in:

```text
app/services/importers/
```

and registering it with the application.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full walkthrough.

---

# Technical architecture

The rest of this README describes how CLEAR works internally.

## Data pipeline

```text
PDF statement
  -> importer detect() + parse_to_json()      (app/services/importers/)
  -> normalize_importer_json()                (app/services/normalize_parquet.py)
  -> Parquet, partitioned by year/month
  -> update_categories()                      (app/services/update_categories.py)
  -> DuckDB analytical queries                (app/services/queries.py)
  -> Flask routes + templates                 (app/web/)
```

The architecture is intentionally simple:

* institution-specific importers translate source documents into structured JSON
* normalized financial data is stored in Parquet
* DuckDB handles analytical queries
* Flask provides the local web interface
* personal data remains separate from tracked demo data

---

# Financial invariants

A few rules are deliberately centralized because financial logic should not drift depending on which screen or query is being used.

## Spending and cash flow

**Spending** and **cash flow** are each defined by exactly one SQL filter:

```text
_SPEND_WHERE
_CASH_FLOW_WHERE
```

Both live in `queries.py`.

Every relevant query extends these definitions rather than reimplementing its own interpretation of spending or cash flow.

---

## Stable transaction IDs

Transaction IDs are deterministic.

Each ID is generated using a SHA256 hash of:

* source statement
* cardholder
* description
* amount
* date

This means re-importing the same statement does not create duplicate transactions.

---

## Categorization precedence

Transaction categorization follows a fixed precedence:

```text
authoritative corrections
  -> seed corrections
  -> transaction-ID seeds
  -> fuzzy matching
  -> unmatched
```

Corrections made through the UI become authoritative seeds.

Those corrections are then applied to future transactions with matching descriptions.

---

## Net worth balance priority

Net worth calculations use a fixed source priority:

```text
statement-reported balance
  -> manually entered value
  -> investment NAV
  -> balance derived from transaction history
```

This keeps account valuation deterministic when several potential balance sources exist.

---

# Project layout

| Path                                | Purpose                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| `app/services/importers/`           | One module per institution; `detect()` + `parse_to_json()`                     |
| `app/services/normalize_parquet.py` | Importer JSON → Parquet, transaction/account ID assignment                     |
| `app/services/categories.py`        | Category tree persistence: base template + personal corrections                |
| `app/services/update_categories.py` | Categorization engine: corrections, seeds, and fuzzy matching                  |
| `app/services/queries.py`           | DuckDB aggregations for spending, cash flow, and net worth                     |
| `app/web/`                          | Flask routes, templates, and static assets                                     |
| `data/demo_seed/`                   | Tracked synthetic input for the demo profile                                   |
| `personal/`                         | Gitignored personal financial data                                             |
| `tests/`                            | `test_*.py` unit/integration tests and per-institution importer contract tests |

---

# Security & privacy

CLEAR handles personal financial information, so your own fork or clone should be treated accordingly.

The project maintains a strict boundary between demo data and personal data.

See [`SECURITY.md`](SECURITY.md) for:

* the demo/personal data boundary
* how to verify that boundary in your checkout
* security considerations for local financial data
* vulnerability reporting

---

# Contributing

Contributions are welcome.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for:

* development setup
* running the test suite
* project conventions
* importer architecture
* adding support for a new institution

Contributions are governed by [`CLA.md`](CLA.md).

---

# License

CLEAR is **source-available, not OSI-approved open source**.

Qualifying noncommercial use is available under the [PolyForm Noncommercial License 1.0.0](LICENSE-POLYFORM-NONCOMMERCIAL-1.0.0.md).

Uses outside that license require a separate written commercial license.

See:

* [`LICENSING.md`](LICENSING.md) for the licensing overview
* [`LICENSE.md`](LICENSE.md) for the complete dual-license terms
* [`TRADEMARK-POLICY.md`](TRADEMARK-POLICY.md) for the separate branding policy
* [`CLA.md`](CLA.md) for contribution terms
