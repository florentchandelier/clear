#!/usr/bin/env python3
"""Build the demo profile (var/demo/) deterministically from the tracked
seed data under data/demo_seed/.

Contract:
  - Reads only tracked, immutable input under data/demo_seed/.
  - Writes scratch/hash-source files only inside a temporary build
    directory under var/ -- never beside the tracked seed inputs.
  - Builds in that temporary sibling, then atomically promotes it to
    var/demo/ (a single symlink-swap syscall, see _promote()) only after
    every step succeeds. A failed build, or a process killed before or
    after that syscall, leaves the previous var/demo/ (if any) completely
    untouched -- content is never deleted before its replacement is
    confirmed in place. The one unavoidable exception is documented in
    _promote(): the one-time upgrade from an older, pre-symlink var/demo/
    real directory cannot be a single syscall (POSIX rename(2) cannot
    atomically replace a non-empty real directory with a symlink), so a
    kill exactly inside that narrow, one-time window can leave demo_dir
    briefly absent -- but never the data itself, which survives under a
    `.previous-demo-*` name and is what the next `make demo` run would
    have replaced anyway.
  - Refuses to run unless its target resolves to exactly
    <repo_root>/var/demo -- never touches personal/ or anywhere else. This
    is enforced from the moment the app package is first imported, not
    just once build() starts (see the MINT_PROFILE_DIR guard below).
  - Makes no network request.
  - Deterministic: re-running from the same tracked inputs produces the
    same transaction IDs and the same categorization summary every time.

Usage:
  venv/bin/python scripts/build_demo_profile.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Guard against a personal profile being touched before this script's own
# isolation (_isolated_build_env, below) ever takes over.
#
# `app/__init__.py` calls configure_logging(), which reads settings, on
# *any* import of the app package -- including the imports right below.
# Without this, importing `app` here would resolve the active profile via
# the normal precedence (MINT_PROFILE_DIR > existing personal/settings.json
# > demo), which means an active personal profile gets its settings.json
# *opened* at that point, before build() has run and long before
# _isolated_build_env(build_dir) redirects anything. configure_logging()
# uses load_settings(create_missing=False) so this was never a write, but
# it was still a read of the user's real personal profile -- observable
# via strace as an open() of personal/settings.json.
#
# This assignment is UNCONDITIONAL, not os.environ.setdefault: this
# script's whole contract (see build()'s docstring) is that it always
# targets config.DEMO_DIR and never anything else, so an ambient
# MINT_PROFILE_DIR must not be honoured even if the caller (or the
# invoking shell) happens to have one set -- an earlier version used
# setdefault, which meant running with MINT_PROFILE_DIR=personal already
# exported still opened personal/settings.json at this exact import,
# before _isolated_build_env ever got a chance to override it. Pointing
# MINT_PROFILE_DIR at a directory that (almost certainly) does not exist
# means active_profile_dir() returns this guard path on its very first,
# highest-precedence check -- personal/settings.json is never even
# stat'd, let alone opened, regardless of what the environment says.
_GUARD_PROFILE_DIR = REPO_ROOT / "var" / ".import-guard-inactive-profile"
os.environ["MINT_PROFILE_DIR"] = str(_GUARD_PROFILE_DIR)

from app import config  # noqa: E402
from app.services import normalize_parquet as norm  # noqa: E402
from app.services import update_categories as upd  # noqa: E402

SEED_DIR = REPO_ROOT / "data" / "demo_seed"
CATEGORIES_SEED_PATH = SEED_DIR / "categories_demo_seed.json"

# Matches app.config's own convention (see _default_settings_for_profile()
# and personal/settings.example.json's "consolidated_statements":
# "consolidated_statements"): the Parquet data root is a subdirectory of
# the profile directory, not the profile directory itself.
# categories_custom.json and settings.json stay at the profile root.
#
# A second, independent BUG-03 regression:
# this script used to ingest directly into the profile root, so
# config.load_settings()["consolidated_statements"] (profile_dir /
# "consolidated_statements") pointed at a subdirectory that was never
# actually populated, even once BUG-03's stale-path issue was fixed.
_DATA_SUBDIR = "consolidated_statements"


def _data_dir(profile_dir: Path) -> Path:
    return profile_dir / _DATA_SUBDIR

# Canonical (institution, account_side, account_class) per seed file --
# must match app.config.ACCOUNT_CLASSES_BY_SIDE, never ad hoc strings like
# the "chequing"/"creditcard"/"manual" this replaces.
SEED_STATEMENTS: Dict[str, Dict[str, str]] = {
    "mock_bmo_chequing.json": dict(institution="BMO", account_side="asset", account_class="cash"),
    "mock_bmo_mastercard.json": dict(institution="BMO", account_side="liability", account_class="credit_card"),
    "mock_investment_statement.json": dict(institution="Questrade", account_side="asset", account_class="investment"),
    "mock_manual_asset.json": dict(institution="Manual Entry", account_side="asset", account_class="car_fmv"),
}

_SUMMARY_RE = re.compile(
    r"updated (\d+) tx \(auth_corr=(\d+), seed_corr=(\d+), id_seed=(\d+), "
    r"fuzzy=(\d+), unmatched=(\d+)\)"
)


def _assert_is_demo_dir(target: Path) -> None:
    """Refuse to touch anything but this repository's own var/demo.

    Checks, in order:
      - the target is exactly <repo root>/var/demo (not merely a path that
        happens to end in var/demo, which a path outside the repository
        could also do);
      - var/ itself (the target's parent) is not a symlink, which would
        let promotion's destructive half follow a link outside the repo;
      - if the target itself is already a symlink (which _promote() below
        leaves it as, from a previous successful build), it must resolve
        to a same-directory sibling under var/ -- never outside it. A
        symlink var/demo made by an earlier run of this same script is
        expected and fine; a symlink escaping var/ is not.

    Both the repo root and the demo dir are read from app.config, so a
    test that redirects config.DEMO_DIR must redirect config.REPO_ROOT
    with it -- there is no looser structural check to fall back on.
    """
    expected = Path(config.REPO_ROOT) / "var" / "demo"
    if target != expected:
        raise RuntimeError(
            f"refusing to build: target {target} is not this repository's "
            f"demo profile ({expected}) -- this script only ever rebuilds "
            f"var/demo/, never personal/ or anything outside the repo."
        )
    var_dir = target.parent
    if var_dir.is_symlink():
        raise RuntimeError(
            f"refusing to build: {var_dir} is a symlink; promotion's "
            f"destructive half must not be able to follow a link outside "
            f"the repository."
        )
    if target.is_symlink():
        link_target = (var_dir / os.readlink(target)).resolve()
        if link_target.parent != var_dir.resolve():
            raise RuntimeError(
                f"refusing to build: {target} is a symlink pointing outside "
                f"{var_dir} ({link_target}) -- promotion only manages "
                f"symlinks that stay within the repository's var/ directory."
            )


@contextmanager
def _isolated_build_env(build_dir: Path):
    """Force *every* settings lookup for the whole build into build_dir.

    This has to wrap the entire build, not just categorization: ingestion
    calls config.load_settings() too (normalize_parquet's FX prefetch
    does), so without this the build would read -- and, on first run,
    create or repair -- whichever profile happens to be active, which may
    be the user's real personal/ profile.

    MINT_DATA_DIR is cleared for the duration as well, so an ambient value
    cannot redirect the build's consolidated_statements somewhere outside
    build_dir.
    """
    tracked = ("MINT_PROFILE_DIR", "MINT_DATA_DIR")
    previous = {name: os.environ.get(name) for name in tracked}
    os.environ["MINT_PROFILE_DIR"] = str(build_dir)
    os.environ.pop("MINT_DATA_DIR", None)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _inject_stable_ids(importer_json: dict, source_pdf_path: Path) -> dict:
    """Deterministic transaction_id/source_statement_id injection, matching
    normalize_parquet's own hashing so ingestion produces the same IDs."""
    doc_sig = importer_json.get("document_signature", {}) or {}
    document_type = doc_sig.get("document_type") or importer_json.get("document_type") or "UNKNOWN"

    account_date = ""
    acc_sum = importer_json.get("account_summary") or {}
    if isinstance(acc_sum, dict):
        prev = acc_sum.get("previous_balance")
        if isinstance(prev, dict):
            account_date = prev.get("date", "") or ""

    fhash = norm._file_sha256(source_pdf_path)
    source_id = norm._stable16(f"{document_type}|{account_date}|{fhash}")

    for holder in importer_json.get("statements", []):
        info = holder.get("cardholder_info", {}) or {}
        cardholder_name = str(info.get("name", ""))
        for tx in holder.get("transactions", []) or []:
            desc = (tx.get("description") or "").strip()
            op_date = str(tx.get("operation_date") or "")
            raw_val = str(tx.get("amount") or tx.get("debit") or tx.get("credit") or "")
            tx_type = str(tx.get("transaction_type") or "expense")
            tx["transaction_id"] = norm._stable16(
                f"{source_id}|{cardholder_name}|{op_date}|{desc}|{raw_val}|{tx_type}"
            )
    return importer_json


def ingest_seed_statements(build_dir: Path) -> List[dict]:
    """Ingest every tracked demo-seed statement into
    build_dir/consolidated_statements (see _data_dir() -- matches
    app.config's own profile-data-subdirectory convention). Pure w.r.t.
    the tracked seed dir -- writes scratch files only under build_dir."""
    data_dir = _data_dir(build_dir)
    results = []
    for fname, cfg in SEED_STATEMENTS.items():
        seed_path = SEED_DIR / fname
        if not seed_path.exists():
            raise FileNotFoundError(f"missing tracked demo seed: {seed_path}")

        scratch_pdf = build_dir / "_scratch_sources" / f"{seed_path.stem}.pdf"
        scratch_pdf.parent.mkdir(parents=True, exist_ok=True)
        scratch_pdf.write_text(f"Deterministic demo source stand-in for {fname}")

        importer_json = json.loads(seed_path.read_text(encoding="utf-8"))
        importer_json = _inject_stable_ids(importer_json, scratch_pdf)

        result = norm.ingest_pdf_with_importer_json(
            importer_json,
            account_side=cfg["account_side"],
            account_class=cfg["account_class"],
            institution=cfg["institution"],
            source_pdf_path=scratch_pdf,
            out_dir=data_dir,
        )
        results.append({"file": fname, **result})
    return results


def apply_categorization(build_dir: Path) -> List[str]:
    """Apply the tracked demo categorization seed to the profile's parquet
    (build_dir/consolidated_statements -- see _data_dir()).
    categories_custom.json itself stays at the profile root, matching
    app.config's custom_categories_path convention.

    Assumes the caller has already entered _isolated_build_env(build_dir),
    which is what points the categorization engine's settings lookups at
    this directory.
    """
    if not CATEGORIES_SEED_PATH.exists():
        raise FileNotFoundError(f"missing tracked demo seed: {CATEGORIES_SEED_PATH}")

    (build_dir / "categories_custom.json").write_text(
        CATEGORIES_SEED_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )

    upd.invalidate_corrections_cache()
    return upd.update_categories(_data_dir(build_dir), threshold=80)


def _promote(build_dir: Path, demo_dir: Path) -> None:
    """Swap build_dir into place as demo_dir, atomically.

    demo_dir is maintained as a symlink to a same-directory sibling that
    holds the actual content (var/.demo-content-<n>/), never as a real
    directory itself. Promotion becomes a single os.replace() of that
    symlink onto demo_dir's path -- one rename(2) syscall, which POSIX
    guarantees is atomic. A process killed at any point either before or
    after that call leaves demo_dir resolving to a complete build (the old
    one, or the new one) -- never missing, and never pointing at a
    partially-written directory.

    That is a real guarantee the previous implementation (rename the old
    dir aside, then rename the new one into place -- two syscalls) did not
    have: a process killed between those two renames left demo_dir
    genuinely absent, recoverable only from a differently-named backup.

    One case remains genuinely non-atomic, unavoidably: the one-time
    upgrade from an older, pre-symlink var/demo/ that is still a real
    directory (from a build made before this fix). POSIX rename(2) cannot
    atomically replace a non-empty real directory with a symlink in one
    call -- the destination must either not exist or already be a
    non-directory -- so demo_dir's old name has to be vacated by a
    separate call before the symlink can take it. That vacating rename is
    itself atomic and never deletes anything: it moves the old real
    directory to a predictable `.previous-demo-*` name, so even a kill in
    the gap between that rename and the symlink swap leaves the previous
    content fully intact and discoverable, merely under a different name
    -- never silently destroyed, unlike an earlier version of this
    function which called shutil.rmtree() on the old directory before
    attempting the swap. A subsequent `make demo` recovers cleanly either
    way, since a missing demo_dir is exactly the first-build case, which
    *is* fully atomic. This whole paragraph applies only once per
    checkout: after the first successful build under this scheme,
    demo_dir is always a symlink, and every later promotion is the fully
    atomic single-syscall path above.
    """
    var_dir = demo_dir.parent

    content_dir = var_dir / f".demo-content-{build_dir.name.lstrip('.')}"
    if content_dir.exists():
        shutil.rmtree(content_dir)
    os.rename(build_dir, content_dir)  # same filesystem as build_dir's parent

    link_tmp = var_dir / f".demo-link-{os.getpid()}"
    if link_tmp.is_symlink() or link_tmp.exists():
        link_tmp.unlink()
    os.symlink(content_dir.name, link_tmp)  # relative -- stays valid within var_dir

    previous_content_dir = None
    if demo_dir.is_symlink():
        previous_content_dir = var_dir / os.readlink(demo_dir)
    elif demo_dir.exists():
        # One-time legacy migration (see docstring): vacate demo_dir's name
        # by moving the old real directory aside, atomically and without
        # deleting anything, rather than rmtree-ing it before the swap.
        legacy_backup = var_dir / f".previous-demo-{os.getpid()}"
        if legacy_backup.exists():
            shutil.rmtree(legacy_backup)
        os.rename(demo_dir, legacy_backup)
        previous_content_dir = legacy_backup

    os.replace(link_tmp, demo_dir)  # the single atomic step

    if previous_content_dir is not None and previous_content_dir != content_dir:
        shutil.rmtree(previous_content_dir, ignore_errors=True)


def _self_heal_stale_settings(demo_dir: Path) -> None:
    """Delete the promoted settings.json if it points outside demo_dir.

    Defense in depth for BUG-03: the
    primary fix (app/config.py's load_settings() no longer *persists*
    consolidated_statements/custom_categories_path on auto-creation, so a
    freshly-created settings.json can no longer go stale this way) means a
    build made with the fixed code never needs this. This exists for a
    profile that's already broken -- an older, pre-fix build's leftover
    settings.json still sitting in someone's `var/demo/`, including a value
    saved through the `/settings` form before the form guard was added.

    Deletes rather than rewrites in place: the next config.load_settings()
    call regenerates the file correctly on its own (via the same,
    already-correct creation path Milestone A fixed), so there is no
    separate "repair" logic to keep in sync with it.
    """
    settings_path = demo_dir / "settings.json"
    if not settings_path.exists():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return  # load_settings() already knows how to repair a corrupt file

    real_demo_dir = demo_dir.resolve()
    stale = False
    for key in ("consolidated_statements", "custom_categories_path"):
        value = data.get(key)
        if not value:
            continue
        try:
            resolved = Path(value).resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(real_demo_dir):
            stale = True
            break

    if stale:
        settings_path.unlink()
        print(
            f"[demo] self-healed a stale settings.json at {settings_path} "
            f"(a persisted path pointed outside the promoted profile) -- "
            f"it will be recreated correctly on next use"
        )


def summarize(messages: List[str]) -> Dict[str, int]:
    """Sum the per-file 'updated N tx (...)' messages into one aggregate."""
    totals = {"tx": 0, "auth_corr": 0, "seed_corr": 0, "id_seed": 0, "fuzzy": 0, "unmatched": 0}
    for msg in messages:
        m = _SUMMARY_RE.search(msg)
        if not m:
            continue
        tx, auth_corr, seed_corr, id_seed, fuzzy, unmatched = (int(g) for g in m.groups())
        totals["tx"] += tx
        totals["auth_corr"] += auth_corr
        totals["seed_corr"] += seed_corr
        totals["id_seed"] += id_seed
        totals["fuzzy"] += fuzzy
        totals["unmatched"] += unmatched
    return totals


def build(*, apply_categories: bool = True) -> Dict[str, int]:
    """Build the demo profile at config.DEMO_DIR. Returns the aggregate
    categorization summary (all zero if apply_categories=False).

    Always targets config.DEMO_DIR -- there is deliberately no parameter
    to redirect it elsewhere. Tests achieve isolation by monkeypatching
    config.REPO_ROOT *and* config.DEMO_DIR together, so the containment
    check in _assert_is_demo_dir() applies to the test target exactly as
    it applies to the real one.

    Raises on any failure -- the failed temp build dir is left behind
    under var/ for inspection, and the demo dir is never touched unless
    every step above succeeded. Promotion itself is a single atomic
    symlink swap once demo_dir is already a symlink (see _promote() for
    the one-time, unavoidably non-atomic exception when upgrading from an
    older real-directory demo_dir) -- content is never deleted before its
    replacement is confirmed in place, and a kill at any point is
    recoverable by simply running `make demo` again.
    """
    demo_dir = config.DEMO_DIR
    _assert_is_demo_dir(demo_dir)

    var_dir = demo_dir.parent
    var_dir.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(prefix=".build-", dir=var_dir))

    try:
        print(f"[demo] building in temporary sibling {build_dir} ...")
        # The isolation wraps the *whole* build: ingestion resolves
        # settings too, so anything outside this block could read or
        # create the active (possibly personal) profile.
        with _isolated_build_env(build_dir):
            for r in ingest_seed_statements(build_dir):
                print(
                    f"  -> {r['file']}: {r['written']} txns, {r['nav_written']} NAV, "
                    f"{r['accounts_upserted']} acct(s), meta {r['meta_written']}"
                )

            summary = {"tx": 0, "auth_corr": 0, "seed_corr": 0, "id_seed": 0, "fuzzy": 0, "unmatched": 0}
            if apply_categories:
                messages = apply_categorization(build_dir)
                for msg in messages:
                    print(f"  -> {msg}")
                summary = summarize(messages)
                print(
                    f"[demo] categorization summary: {summary['tx']} tx "
                    f"(auth_corr={summary['auth_corr']}, seed_corr={summary['seed_corr']}, "
                    f"id_seed={summary['id_seed']}, fuzzy={summary['fuzzy']}, "
                    f"unmatched={summary['unmatched']})"
                )

        _promote(build_dir, demo_dir)
        print(f"[demo] promoted to {demo_dir}")
        _self_heal_stale_settings(demo_dir)
        return summary
    except Exception:
        print(
            f"[demo] build FAILED -- {demo_dir} untouched. "
            f"Partial build left at {build_dir} for inspection.",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    build()
