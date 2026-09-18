# Your personal data lives here

This directory is the **only** place real financial data is ever meant to
live in this repository. Everything in it except this file and
`settings.example.json` is gitignored — `git status` will never show your
real statements, corrections, or settings as things to commit, and
`scripts/audit_public_tree.py` (the release-safety scanner this project
uses before every publish) treats anything else appearing under
`personal/` as an automatic finding.

## Activating personal mode

By default, the app runs in **demo mode**: synthetic data, safe to poke
at, regenerated any time with `make demo`. Nothing you do in demo mode
touches this directory.

To switch to your own real data:

```bash
cp personal/settings.example.json personal/settings.json
```

That's it — the moment `personal/settings.json` exists, every command
(`make run`, the CLI, tests that don't explicitly ask for demo/mock data)
uses it instead of the demo profile. Edit the copied file to change
thresholds, your display currency, or where your data lives; the default
paths inside it (`consolidated_statements`, `categories_custom.json`) are
relative to this directory, so they don't need to be touched to get
started.

To switch back to demo mode, delete or rename `personal/settings.json`.
Nothing else changes — your real data stays exactly where it is, just
inactive.

## What ends up here

Once active, personal mode reads and writes, all inside this directory:

- `settings.json` — your real configuration (created by the copy above).
- `categories_custom.json` — your real category corrections and seeds.
- `consolidated_statements/` — your real ingested Parquet data.
- `statements/` — a reasonable place to keep the original PDF statements
  you import from, if you want them archived somewhere (nothing in the
  app reads this path automatically; it's just a suggested location).

## A few rules worth internalizing

- **Never `git add -f` anything under here.** If you ever find yourself
  about to force-add a file in `personal/`, stop — that's exactly the
  mistake this whole boundary exists to prevent.
- **Never copy a real statement, corrections file, or settings.json out
  of this directory into anywhere else in the repo** (a test fixture, a
  script's hardcoded default, a scratch file at the repo root). If you
  need a fixture for testing, build a synthetic one instead — see
  `data/demo_seed/README.md` for how the project's own demo data is
  built the same way.
- **Before pushing anywhere, or opening a PR from a fork,** run:
  ```bash
  venv/bin/python scripts/audit_public_tree.py --tracked --history
  ```
  It never prints matched values, only file paths and rule names — safe
  to run and share the output of, even if something real did leak.
