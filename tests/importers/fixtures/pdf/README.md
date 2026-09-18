# Public synthetic PDF fixtures

This directory contains small, deterministic PDFs built entirely from invented
data. Together they provide reproducible parser and end-to-end import coverage
for every PDF importer in the public registry.

| Fixture | Registered importer |
| --- | --- |
| `bmo_chequing_fr_demo.pdf` | `cash.bmo_chequing_fr` |
| `bmo_mastercard_fr_demo.pdf` | `credit.bmo_mastercard_fr` |
| `bmo_heloc_fr_demo.pdf` | `loc.bmo_heloc_fr` |
| `bmo_nesbitt_ca_demo.pdf` | `investment.bmo_nesbitt_ca` |
| `interactive_brokers_ca_demo.pdf` | `investment.interactive_brokers_ca` |
| `questrade_equity_demo.pdf` | `investment.questrade_equity` |
| `car_cargurus_valuation_demo.pdf` | `asset.car_fmv_cargurus` |
| `home_evaluation_fonciere_demo.pdf` | `asset.home_fmv_evaluation_fonciere` |

Each PDF is paired with a reviewed `*.expected.json` parse result. The tests
also re-derive key counts, balances, dates, and NAV values from facts returned
by the generator so a golden file cannot merely preserve an incorrect parse.

Regenerate the PDFs and reviewed outputs with:

```bash
venv/bin/python scripts/generate_demo_pdfs.py --update-expected
```

Review every JSON diff before committing it. A changed expected file represents
a changed importer contract, not routine generated-file churn.

Run the public PDF coverage with:

```bash
venv/bin/python -m pytest -q \
  tests/test_synthetic_pdf_import.py \
  tests/test_synthetic_pdf_importers.py
```

The suite verifies valid PDF generation, registered detection, reviewed parser
output, Flask preview and commit, Parquet persistence through normal queries,
byte-for-byte deterministic regeneration, offline execution, and the absence
of fixture-specific branches in importer code. No private statement archive is
required or referenced.
