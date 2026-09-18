# Contributing to CLEAR

## Contributor License Agreement

**By intentionally submitting a contribution to this Project, you agree to
the [Contributor License Agreement](CLA.md).**

No separate CLA signature, registration, or CLA bot is required. If you do not
agree to the CLA, do not submit a contribution.

Submitting includes opening a pull request, pushing a commit directly to a
Project repository if you have write access, or otherwise intentionally sending
code or other protectable material through a contribution channel designated
by the Project.

Merely forking, cloning, or pushing changes only to your own repository does
not by itself constitute submission to this Project.

If your contribution includes third-party material, clearly identify its
source and license. You may submit only material that you have the right and
authority to contribute under the CLA.

## Setup

```bash
make install   # creates venv/, installs runtime + dev dependencies
make demo      # populates the synthetic demo profile
make test      # full suite
```

`make install` installs `requirements.txt` (runtime) and
`requirements-dev.txt` (currently just `pytest`). `requirements.txt`
includes `camelot-py`, which needs a system package
(`ghostscript` — `sudo apt-get install ghostscript python3-tk` on
Debian/Ubuntu) to actually parse a real PDF. You do **not** need it for
`make demo`, `make test`, or day-to-day dashboard development — the
default demo path ingests tracked JSON directly and never touches
Camelot. You only need it for the real PDF-import path (uploading an
actual statement, or the synthetic-PDF importer tests under
`tests/importers/`).

## Running tests

```bash
make test              # everything
make test-fast         # skip slow-marked tests
make test-importers    # importer contract tests only
venv/bin/pytest tests/test_queries.py -k spending   # one file / one -k filter
```

Tests never require real financial data. If you're adding a test that
needs a populated dataset, use the demo profile
(`data/demo_seed/`, built by `make demo`) or a synthetic fixture under
`tests/importers/fixtures/` — never a real statement, even temporarily.

## Adding a new importer

An importer is one class implementing `detect()` and `parse_to_json()`
against a PDF path, returning the schema defined in
`app/services/importers/schema.json`. `app/services/importers/template.py`
provides `TemplateImporter`, a base class that most importers build on —
look at an existing one close to what you're adding (e.g.
`app/services/importers/cash/bmo_chequing_fr.py` for a text/table-based
statement, or `app/services/importers/investment/questrade_equity.py`
for a NAV-snapshot statement) as a starting point.

Steps:

1. Implement the class under `app/services/importers/<side>/<name>.py`
   (side = `cash`, `credit`, `investment`, `loan`, or `asset`, matching
   `app.config.ACCOUNT_CLASSES_BY_SIDE`).
2. Register it in `app/services/importers/registry.py`.
3. Add a contract fixture under `tests/importers/fixtures/` and a case
   in `tests/importers/test_importer_contract.py`.
4. If you want it to run through the real PDF-parsing path in CI (not
   just against hand-authored JSON), add a generator to
   `scripts/generate_demo_pdfs.py` and a test in
   `tests/test_synthetic_pdf_import.py` — see that file for the
   established pattern (a synthetic PDF that exercises `detect()` and
   `parse_to_json()` for real, with a reviewed golden-JSON fixture next
   to it). This is meaningfully more work than a JSON-only fixture and is
   optional; not every importer needs it.

Whatever you add, never use a real statement as a fixture, even with
identifying details removed by hand — build a synthetic one instead.

## Conventions

- Prefer the smallest correct change; avoid opportunistic refactors in
  unrelated code.
- `_BASE_CTE`, `_SPEND_WHERE`, and `_CASH_FLOW_WHERE` in `queries.py` are
  the single source of truth for spending/cash-flow semantics — never
  reimplement that logic in a route, template, or a new query.
- Routes call service functions and pass the result through; they never
  compute a financial value themselves (a route doing arithmetic on a
  query result is a bug, not a shortcut).

## Before opening a PR

```bash
venv/bin/python scripts/audit_public_tree.py --tracked --history
```

This project's release-safety scanner. It flags forbidden paths, private
path fragments, and unmasked-looking identifiers in anything you've
added — never printing the matched value itself, only where it found
something. A finding is a prompt to look closer, not automatic proof of
a problem (it can't always tell a real identifier from an obviously
synthetic one), but it should never be silently ignored.

See [`SECURITY.md`](SECURITY.md) for the personal/demo data boundary
this check exists to protect.

## Commercial licensing

Contributing does not grant you a Commercial License to the Project.
Commercial use remains governed by the Project's licensing terms in
[`LICENSE.md`](LICENSE.md).
