# Security & Privacy

## The demo/personal boundary

This is a personal-finance app, and it's built around one hard rule:
**real financial data only ever lives under `personal/`, and everywhere
else in the repository is safe to publish by construction.**

- `personal/` is entirely gitignored except for its own `README.md` and
  `settings.example.json` — see [`personal/README.md`](personal/README.md)
  for the full contract.
- Everything checked into git outside `personal/` — including the demo
  dataset under `data/demo_seed/` — is synthetic: fabricated
  transactions, invented merchant names and account numbers, generated
  test PDFs. None of it derives from a real statement.
- `make demo` builds and rebuilds the demo profile without ever reading,
  writing, or deleting anything under `personal/`, regardless of which
  profile is active when you run it.

If you're auditing a fork or reviewing a PR, the check that verifies all
of this mechanically is:

```bash
venv/bin/python scripts/audit_public_tree.py --tracked --history
```

It flags forbidden paths (things that should never be tracked at all),
private local-path fragments, and JSON fields that look like unmasked
real identifiers — checking both the current tree (`--tracked`) and
every blob reachable from any branch/tag/remote (`--history`, so a
secret committed once and later removed still gets caught). It never
prints a matched value, only the file and rule that flagged it, so its
output is always safe to paste into an issue or PR comment. A finding is
a prompt for a human to look closer, not automatic proof of a problem —
it can't always distinguish a real identifier from an obviously
synthetic one (a small number of reviewed, value-hash-bound exceptions
exist for exactly this in the scanner's own source).

## Reporting a vulnerability

If you find a security issue — a way real data could leak between
profiles, a way the app could be tricked into reading/writing outside
its intended paths, or anything else with a real security impact — please
report it privately rather than opening a public issue first, so there's
time to fix it before it's widely known. Open an issue asking for a
private contact if no other reporting channel is listed in this
repository at the time you find it.

Please don't open public issues for:
- Missing features or usability complaints (use a normal issue).
- Findings from the `audit_public_tree.py` scanner against your own
  personal data — those are expected and by design; the scanner exists
  precisely so you catch them yourself before pushing anywhere.
