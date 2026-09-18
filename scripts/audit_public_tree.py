#!/usr/bin/env python3
"""Deterministic safety scanner for the CLEAR source-available release.

Checks a git working tree / history for:
  - tracked paths that must never be published (forbidden-path rules),
  - tracked text files containing known local/private path fragments,
  - JSON fields that look like unmasked real identity/account data,
  - non-standard git refs outside refs/heads, refs/remotes, refs/tags.

This scanner is a backstop, not a substitute for human review of every
tracked JSON, PDF, image, archive, and generated binary before a push. A
finding is a prompt to look closer, not automatically proof of a leak --
some of this repo's own already-reviewed mock fixtures use synthetic
values (e.g. sequential digits) that this scanner cannot fully tell apart
from real ones by pattern alone.

It never prints a matched sensitive value -- only the offending path and
a rule id -- and it never hardcodes a personal path/string into source;
private fragments are derived at run time (repo root, $HOME, current
username) plus any --extra-fragment the caller supplies.
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, NamedTuple, Optional


class Finding(NamedTuple):
    rule: str
    path: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"[{self.rule}] {self.path}{suffix}"


class Waiver(NamedTuple):
    """A finding that a human reviewed and deliberately accepted."""

    rule: str
    path: str
    detail: str
    reason: str

    def __str__(self) -> str:
        return f"[waived: {self.rule}] {self.path} ({self.detail}) -- {self.reason}"


# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

# fnmatch-style patterns, relative to repo root (posix separators), for
# paths that must never be tracked in the public release. Matched against
# the current tracked tree AND against every path name ever seen in
# history (--history), since a name can leak via an old commit even if
# absent today.
FORBIDDEN_PATH_PATTERNS: list[str] = [
    "settings.json",
    "categories_custom.json",
    "categories_custom.json.backup",
    "*.backup",
    "consolidated_statements/*",
    "debug_parquet_statements*",
    "parquet_statements/*",
    "pdf_statements/*",
    "uploads/*",
    "mint_app_aggregated.txt",
    "personal/*",
    "var/*",
    "venv/*",
    "venv2/*",
    ".idea/*",
]

# Explicit exceptions: paths that would otherwise match a forbidden
# pattern above but are intentionally tracked release content.
ALLOWLIST_PATTERNS: list[str] = [
    "personal/README.md",
    "personal/settings.example.json",
    "tests/importers/fixtures/pdf/*",
]

def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# Reviewed exceptions to the unmasked-identifier rule below.
#
# Each key is an EXACT (tracked path, JSON field path) pair -- deliberately
# not a pattern -- mapped to the sha256 of the SPECIFIC VALUE that was
# reviewed, plus the reason. A different file, a different field in the
# same file, or -- critically -- a *different value* at that same location
# is still reported and must be reviewed on its own merits: this is what
# stops someone from later dropping a real account number into the
# reviewed field and having it silently pass. The general rule is never
# weakened by a location-only match. Waived items are printed by the CLI
# rather than silently dropped, so a zero-findings run still shows what
# was accepted and why -- never the value itself, only its hash's first
# few characters, which is not reversible to the underlying value for an
# unmasked identifier of realistic length.
REVIEWED_JSON_FIELD_EXCEPTIONS: dict[tuple[str, str], tuple[str, str]] = {
    (
        "data/demo_seed/mock_bmo_chequing.json",
        "$.statements[0].cardholder_info.card_number",
    ): (
        # sha256("12345678-001") -- recompute with
        # `python -c "import hashlib; print(hashlib.sha256(b'...').hexdigest())"`
        # if this seed's value is ever deliberately changed.
        "426752c8914f853bf1c5639a34b20d6151c138fac9306719b5ddb9285a0d3473",
        "invented sequential digits in a tracked synthetic demo seed; the "
        "value is not real, and changing it would shift the demo build's "
        "transaction ids and the documented EXPECTED_BASELINE in "
        "tests/test_build_demo_profile.py (reviewed 2026-09-04)",
    ),
}

# JSON key names (matched with re.search, case-insensitive) whose string
# values are worth a second look unless they're clearly masked/placeholder.
SUSPICIOUS_JSON_FIELD_RE = re.compile(
    r"(card_number|account_number|sin|ssn|social_insurance|email)$",
    re.IGNORECASE,
)
MASK_CHAR_RE = re.compile(r"[Xx*]")
REPEATED_CHAR_RE = re.compile(r"^(.)\1*$")
PLACEHOLDER_WORD_RE = re.compile(
    r"^(unknown|n/?a|test.*|todo|example.*|mystery.*)$", re.IGNORECASE
)

NON_STANDARD_REF_RE = re.compile(r"^refs/(heads|remotes|tags)/")

_TEXT_SCAN_EXTENSIONS = {
    ".py", ".json", ".md", ".txt", ".html", ".js", ".css",
    ".cfg", ".ini", ".toml", ".yaml", ".yml",
}


# ---------------------------------------------------------------------------
# Pure rule functions (no git/filesystem access -- exercised directly by
# tests/test_audit_public_tree.py)
# ---------------------------------------------------------------------------

def is_forbidden(path: str) -> Optional[str]:
    """Return the matching forbidden pattern, or None if path is allowed."""
    posix = path.replace(os.sep, "/")
    for pattern in ALLOWLIST_PATTERNS:
        if fnmatch.fnmatch(posix, pattern):
            return None
    for pattern in FORBIDDEN_PATH_PATTERNS:
        if fnmatch.fnmatch(posix, pattern):
            return pattern
    return None


def audit_tracked_paths(paths: Iterable[str]) -> list[Finding]:
    findings = []
    for path in paths:
        rule = is_forbidden(path)
        if rule:
            findings.append(Finding("FORBIDDEN_PATH", path, rule))
    return findings


def default_private_fragments(repo_root: Path) -> list[str]:
    """Fragments considered private to *this* machine/checkout.

    Derived at run time so the scanner's own source never embeds a
    personal path string.
    """
    fragments = {str(Path(repo_root).resolve()), str(Path.home())}
    try:
        fragments.add(getpass.getuser())
    except Exception:
        pass
    return sorted(f for f in fragments if f and f not in ("/", ""))


def scan_text_for_fragments(text: str, fragments: Iterable[str]) -> bool:
    """True if any fragment appears in text.

    Path-like fragments (containing a path separator) are matched as a
    plain substring, since they're long/specific enough not to collide
    with ordinary words. Bare word-like fragments (e.g. a short username)
    are matched with word boundaries instead -- a raw substring check on
    something like the username "devuser" would otherwise false-positive on
    every occurrence of "cash flow", "workflow", etc. in a finance app.
    """
    for fragment in fragments:
        if not fragment:
            continue
        if "/" in fragment or "\\" in fragment:
            if fragment in text:
                return True
        elif re.search(r"\b" + re.escape(fragment) + r"\b", text):
            return True
    return False


def _looks_masked_or_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if MASK_CHAR_RE.search(value):
        return True
    if PLACEHOLDER_WORD_RE.match(stripped):
        return True
    condensed = re.sub(r"[\s-]", "", stripped)
    if condensed and REPEATED_CHAR_RE.match(condensed):
        return True
    return False


def scan_json_for_unmasked_fields(obj, path: str = "$") -> list[tuple[str, str]]:
    """Return (JSON-path, value) pairs for suspicious, apparently-unmasked
    fields.

    The value is returned alongside its location -- not to print it (the
    CLI never does), but so a caller checking a reviewed exception against
    REVIEWED_JSON_FIELD_EXCEPTIONS can hash it and confirm it is still the
    exact value that was reviewed, not merely at a reviewed location.
    """
    findings: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if isinstance(value, str) and SUSPICIOUS_JSON_FIELD_RE.search(key):
                if not _looks_masked_or_placeholder(value):
                    findings.append((child_path, value))
            findings.extend(scan_json_for_unmasked_fields(value, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            findings.extend(scan_json_for_unmasked_fields(item, f"{path}[{i}]"))
    return findings


def audit_file_contents(
    repo_root: Path,
    paths: Iterable[str],
    fragments: list[str],
    waived: Optional[list] = None,
) -> list[Finding]:
    """Scan tracked files for private fragments and unmasked identifiers.

    Pass `waived` to collect the reviewed exceptions that were applied
    (see REVIEWED_JSON_FIELD_EXCEPTIONS); they are excluded from the
    returned findings so the caller can report them separately.
    """
    findings = []
    for rel in paths:
        full = Path(repo_root) / rel
        if full.suffix.lower() not in _TEXT_SCAN_EXTENSIONS or not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if scan_text_for_fragments(content, fragments):
            findings.append(Finding("PRIVATE_PATH_FRAGMENT", rel))
        if full.suffix.lower() == ".json":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                continue
            for field_path, value in scan_json_for_unmasked_fields(data):
                posix_rel = str(rel).replace(os.sep, "/")
                exception = REVIEWED_JSON_FIELD_EXCEPTIONS.get((posix_rel, field_path))
                if exception is not None:
                    expected_hash, reason = exception
                    if _hash_value(value) == expected_hash:
                        if waived is not None:
                            waived.append(
                                Waiver("UNMASKED_JSON_FIELD", posix_rel, field_path, reason)
                            )
                        continue
                    # Same file, same field -- but the value has changed
                    # since it was reviewed. The exception does not apply;
                    # fall through and report it as a fresh finding, exactly
                    # as if no exception existed for this location at all.
                findings.append(Finding("UNMASKED_JSON_FIELD", rel, field_path))
    return findings


def audit_refs(ref_names: Iterable[str]) -> list[Finding]:
    return [
        Finding("NON_STANDARD_REF", ref)
        for ref in ref_names
        if not NON_STANDARD_REF_RE.match(ref)
    ]


# ---------------------------------------------------------------------------
# Git plumbing -- thin wrappers kept separate from the rule functions above.
# ---------------------------------------------------------------------------

def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def tracked_paths(repo_root: Path) -> list[str]:
    return [p for p in _git(repo_root, "ls-files").splitlines() if p]


def all_ref_names(repo_root: Path) -> list[str]:
    out = _git(repo_root, "for-each-ref", "--format=%(refname)")
    return [line for line in out.splitlines() if line]


def _historical_blob_index(repo_root: Path) -> list[tuple[str, str]]:
    """(sha, path) for every object reachable from branches/tags/remotes."""
    out = _git(
        repo_root, "rev-list", "--objects", "--branches", "--tags", "--remotes",
    )
    entries = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            entries.append((parts[0], parts[1]))
    return entries


def historical_blob_paths(repo_root: Path) -> list[str]:
    return [path for _sha, path in _historical_blob_index(repo_root)]


def _cat_file_batch(repo_root: Path, shas: list[str]) -> dict[str, bytes]:
    """Read many blobs in one `git cat-file --batch` pass.

    Returns {sha: content}. Objects git reports as missing are skipped.
    """
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input="\n".join(shas).encode() + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    buf = proc.stdout
    out: dict[str, bytes] = {}
    pos = 0
    while pos < len(buf):
        nl = buf.find(b"\n", pos)
        if nl == -1:
            break
        header = buf[pos:nl].decode("utf-8", errors="replace").split()
        pos = nl + 1
        # "<sha> missing" for anything unreadable.
        if len(header) < 3:
            continue
        sha, _obj_type, size_str = header[0], header[1], header[2]
        try:
            size = int(size_str)
        except ValueError:
            continue
        out[sha] = buf[pos:pos + size]
        pos += size + 1  # payload plus its trailing newline
    return out


def audit_history_contents(
    repo_root: Path, fragments: list[str], max_blob_bytes: int = 2_000_000
) -> list[Finding]:
    """Scan the *content* of historical blobs, not just their path names.

    A path-name scan cannot see a private string that was committed inside
    an innocuously-named file and later removed or sanitised: the blob is
    still in history and still reachable. This reads every text-ish blob
    reachable from branches/tags/remotes and reports the historical path
    it was found at (never the matched value).
    """
    entries = [
        (sha, path)
        for sha, path in _historical_blob_index(repo_root)
        if Path(path).suffix.lower() in _TEXT_SCAN_EXTENSIONS
    ]
    # One entry per blob; the same content can appear at several paths.
    by_sha: dict[str, list[str]] = {}
    for sha, path in entries:
        by_sha.setdefault(sha, []).append(path)

    contents = _cat_file_batch(repo_root, sorted(by_sha))

    findings: list[Finding] = []
    for sha, blob in contents.items():
        if len(blob) > max_blob_bytes:
            continue
        text = blob.decode("utf-8", errors="ignore")
        if scan_text_for_fragments(text, fragments):
            for path in sorted(set(by_sha[sha])):
                findings.append(
                    Finding("HISTORICAL_PRIVATE_FRAGMENT", path, f"blob {sha[:12]}")
                )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(
    root: Path,
    check_tracked: bool,
    check_history: bool,
    extra_fragments: list[str],
    waived: Optional[list] = None,
) -> list[Finding]:
    findings: list[Finding] = []
    fragments = default_private_fragments(root) + list(extra_fragments)

    if check_tracked:
        paths = tracked_paths(root)
        findings.extend(audit_tracked_paths(paths))
        findings.extend(audit_file_contents(root, paths, fragments, waived=waived))

    if check_history:
        findings.extend(audit_refs(all_ref_names(root)))
        findings.extend(audit_tracked_paths(historical_blob_paths(root)))
        # Path names alone would miss a private string committed inside an
        # innocuous file and later removed -- that blob is still reachable.
        findings.extend(audit_history_contents(root, fragments))

    return findings


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root (default: cwd)")
    parser.add_argument(
        "--tracked", action="store_true", help="Audit the current tracked tree"
    )
    parser.add_argument(
        "--history", action="store_true", help="Audit refs and historical blob paths"
    )
    parser.add_argument(
        "--extra-fragment",
        action="append",
        default=[],
        help="Additional private path/string fragment to search for (repeatable)",
    )
    args = parser.parse_args(argv)

    if not args.tracked and not args.history:
        args.tracked = args.history = True

    root = Path(args.root).resolve()
    waived: list[Waiver] = []
    findings = run(root, args.tracked, args.history, args.extra_fragment, waived=waived)

    for finding in findings:
        print(finding)

    # Reported, never silent: a clean run still shows what was accepted.
    for waiver in waived:
        print(waiver)

    if findings:
        print(
            f"\naudit_public_tree: {len(findings)} finding(s) -- review each one; "
            "a finding is a prompt to look closer, not automatic proof of a leak.",
            file=sys.stderr,
        )
        return 1

    suffix = f" ({len(waived)} reviewed exception(s) applied)" if waived else ""
    print(f"audit_public_tree: no findings{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
