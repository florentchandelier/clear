"""Tests for the deterministic synthetic demo-profile builder.

The integration tests monkeypatch config.DEMO_DIR to a tmp_path so they
never touch this repository's real var/demo/.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from app import config
from scripts import build_demo_profile as bdp


@pytest.fixture(autouse=True)
def clean_profile_env(monkeypatch):
    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)


def test_assert_is_demo_dir_accepts_the_repository_demo_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    bdp._assert_is_demo_dir(tmp_path / "var" / "demo")  # must not raise


@pytest.mark.parametrize(
    "bad_path",
    [
        "personal",
        "var/personal",
        "somewhere/else",
    ],
)
def test_assert_is_demo_dir_rejects_anything_else(monkeypatch, tmp_path, bad_path):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    with pytest.raises(RuntimeError):
        bdp._assert_is_demo_dir(tmp_path / bad_path)


def test_assert_is_demo_dir_rejects_a_lookalike_outside_the_repository(
    monkeypatch, tmp_path
):
    """A path that merely *ends* in var/demo but lives outside the
    repository must be refused -- promotion deletes that directory."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    outsider = tmp_path / "elsewhere" / "var" / "demo"
    with pytest.raises(RuntimeError):
        bdp._assert_is_demo_dir(outsider)


def test_assert_is_demo_dir_rejects_symlinked_target(monkeypatch, tmp_path):
    """A symlink at var/demo (or at var/) could redirect the destructive
    half of promotion outside the repository."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    outside = tmp_path / "outside_target"
    outside.mkdir()
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    link = var_dir / "demo"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        bdp._assert_is_demo_dir(link)
    assert outside.exists(), "the symlink target must not have been touched"


def test_summarize_aggregates_per_file_messages():
    messages = [
        "transactions.parquet: updated 25 tx (auth_corr=8, seed_corr=8, id_seed=8, fuzzy=1, unmatched=0)",
        "transactions.parquet: updated 15 tx (auth_corr=5, seed_corr=5, id_seed=4, fuzzy=1, unmatched=0)",
    ]
    totals = bdp.summarize(messages)
    assert totals == {"tx": 40, "auth_corr": 13, "seed_corr": 13, "id_seed": 12, "fuzzy": 2, "unmatched": 0}


def test_summarize_ignores_unrelated_messages():
    assert bdp.summarize(["some other message", ""]) == {
        "tx": 0, "auth_corr": 0, "seed_corr": 0, "id_seed": 0, "fuzzy": 0, "unmatched": 0,
    }


@pytest.fixture
def demo_dir(monkeypatch, tmp_path):
    target = tmp_path / "var" / "demo"
    # REPO_ROOT must move with DEMO_DIR: _assert_is_demo_dir() requires the
    # target to be exactly <repo root>/var/demo, so that a path merely
    # *ending* in var/demo (or reached through a symlink) is refused.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEMO_DIR", target)
    # Keep personal/ isolated too, even though this milestone's build never
    # touches it -- belt and suspenders against any accidental coupling.
    monkeypatch.setattr(config, "PERSONAL_DIR", tmp_path / "personal")
    return target


def test_build_populates_demo_dir_from_tracked_seed(demo_dir):
    summary = bdp.build()

    assert (demo_dir / "categories_custom.json").exists()
    assert (demo_dir / "settings.json").exists()

    tx_files = list(demo_dir.rglob("transactions.parquet"))
    assert tx_files, "expected at least one transactions.parquet under the demo dir"

    all_tx = pd.concat([pd.read_parquet(f) for f in tx_files])
    assert len(all_tx) == summary["tx"]
    # Canonical account classes only -- not the old "chequing"/"creditcard"/"manual".
    assert set(all_tx["account_class"].unique()) <= {"cash", "credit_card"}


def test_config_load_settings_finds_the_real_built_data(demo_dir):
    """BUG-03 regression test: every other assertion in this file reads data via
    demo_dir.rglob(...) or similar direct filesystem access -- which is
    exactly why three prior review passes never caught that
    config.load_settings()["consolidated_statements"] pointed nowhere
    real. This test goes through the actual consumption path instead: the
    same function the CLI and the live web app call.
    """
    bdp.build()

    data = config.load_settings()
    consolidated = Path(data["consolidated_statements"])
    assert consolidated.is_absolute()
    assert consolidated.is_dir(), (
        f"consolidated_statements ({consolidated}) does not exist -- "
        f"config.load_settings() and the real built data have diverged"
    )
    assert list(consolidated.rglob("transactions.parquet")), (
        "consolidated_statements exists but has no transaction data in it"
    )

    from app.services import queries as q

    df = q.run_query("all_transactions", str(consolidated))
    assert not df.empty, "run_query() against the settings-reported path found no data"


def test_the_real_demo_build_serves_data_via_config_load_settings():
    """Fresh-subprocess version of the test above, against the real
    repository (matching this file's other real-build subprocess tests):
    runs `make demo`'s actual script, then -- in a separate process, never
    sharing any in-memory state with the build -- confirms
    config.load_settings() and a real query find the data. This is the
    exact regression reproduction for the stale-profile-path defect.
    """
    repo_root = bdp.REPO_ROOT
    env = dict(os.environ)
    env.pop("MINT_PROFILE_DIR", None)
    env.pop("MINT_DATA_DIR", None)

    build = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "build_demo_profile.py")],
        capture_output=True, text=True, env=env, cwd=str(repo_root),
    )
    assert build.returncode == 0, build.stdout + build.stderr

    check = subprocess.run(
        [sys.executable, "-c", (
            "from app import config\n"
            "from app.services import queries as q\n"
            "s = config.load_settings()\n"
            "df = q.run_query('all_transactions', s['consolidated_statements'])\n"
            "assert not df.empty, 'demo profile serves no data via load_settings()'\n"
            "print('OK', len(df))\n"
        )],
        capture_output=True, text=True, env=env, cwd=str(repo_root),
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "OK" in check.stdout


def test_build_uses_canonical_account_classes_for_asset_accounts(demo_dir):
    bdp.build()
    # Parquet data lives under demo_dir/consolidated_statements/ (matching
    # app.config's profile-data-subdirectory convention), not demo_dir
    # itself -- see bdp._data_dir().
    accounts = pd.read_parquet(demo_dir / "consolidated_statements" / "_meta" / "accounts.parquet")
    assert set(accounts["account_class"].unique()) <= {"cash", "credit_card", "investment", "car_fmv"}
    assert "chequing" not in accounts["account_class"].values
    assert "creditcard" not in accounts["account_class"].values
    assert "manual" not in accounts["account_class"].values


# The documented regression baseline. A build
# that is merely *deterministic* but categorizes differently must fail.
EXPECTED_BASELINE = {
    "tx": 40,
    "auth_corr": 13,
    "seed_corr": 13,
    "id_seed": 12,
    "fuzzy": 2,
    "unmatched": 0,
}


def _output_fingerprint(demo_dir):
    """Stable identity of the built dataset: transaction ids plus the
    fields the categorization pipeline is responsible for."""
    frames = [pd.read_parquet(f) for f in sorted(demo_dir.rglob("transactions.parquet"))]
    df = pd.concat(frames).sort_values("transaction_id")
    cols = ["transaction_id", "operation_date", "description", "amount",
            "category", "subcategory", "transaction_type"]
    return [tuple(str(v) for v in row) for row in df[cols].itertuples(index=False)]


def test_build_matches_the_documented_regression_baseline(demo_dir):
    assert bdp.build() == EXPECTED_BASELINE


def test_build_is_idempotent_in_summary_and_in_data(demo_dir):
    first = bdp.build()
    first_rows = _output_fingerprint(demo_dir)

    second = bdp.build()
    second_rows = _output_fingerprint(demo_dir)

    assert first == second == EXPECTED_BASELINE
    # Not just the same counts -- the same transactions, with the same
    # stable ids and the same categorization.
    assert first_rows == second_rows
    assert len({r[0] for r in first_rows}) == EXPECTED_BASELINE["tx"], (
        "transaction ids must be unique and cover the whole baseline"
    )


def test_build_never_creates_personal_dir(demo_dir, monkeypatch, tmp_path):
    personal_dir = tmp_path / "personal"
    bdp.build()
    assert not personal_dir.exists()


def test_build_never_writes_an_active_personal_profile(demo_dir, tmp_path):
    """Regression: the whole build must be isolated, not just the
    categorization step. Ingestion resolves settings too (normalize_parquet
    prefetches FX), so with a personal profile active an un-isolated build
    reached the user's real settings file.

    The personal settings file here is deliberately *corrupt*, because
    that is the case where config.load_settings() does not merely read but
    rewrites the file: before the fix this test's build replaced the
    user's hand-edited file with generated defaults.
    """
    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    personal_settings = personal_dir / "settings.json"
    corrupt = "{ hand-edited by the user, currently broken"
    personal_settings.write_text(corrupt)

    bdp.build()

    assert personal_settings.read_text() == corrupt, (
        "the build rewrote the active personal profile's settings file"
    )
    # Nothing from the demo build may have leaked into the personal profile.
    assert not (personal_dir / "consolidated_statements").exists()
    assert not (personal_dir / "categories_custom.json").exists()


def test_build_does_not_depend_on_display_currency_of_active_profile(demo_dir, tmp_path):
    """A non-CAD display currency in the active profile must not reach the
    build: ingestion's FX prefetch only short-circuits (and so only stays
    offline) when the currencies match."""
    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    (personal_dir / "settings.json").write_text(
        json.dumps({"display_currency": "JPY"}, indent=2)
    )

    import socket

    original_connect = socket.socket.connect

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted during demo build")

    socket.socket.connect = _blocked
    try:
        bdp.build()
    finally:
        socket.socket.connect = original_connect


def test_build_does_not_create_demo_settings_before_promotion(demo_dir, monkeypatch):
    """Regression: a failed build must leave the demo dir untouched. An
    unisolated ingestion created var/demo/settings.json as a side effect
    *before* the temporary build had succeeded."""
    def _boom(_build_dir):
        raise RuntimeError("simulated categorization failure")

    monkeypatch.setattr(bdp, "apply_categorization", _boom)

    with pytest.raises(RuntimeError, match="simulated categorization failure"):
        bdp.build()

    assert not demo_dir.exists(), (
        "a failed build must not have created the demo profile directory"
    )


def test_failed_build_preserves_the_previous_demo_profile(demo_dir, monkeypatch):
    bdp.build()
    marker = demo_dir / "categories_custom.json"
    assert marker.exists()
    previous = marker.read_text(encoding="utf-8")

    def _boom(_build_dir):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(bdp, "apply_categorization", _boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        bdp.build()

    assert demo_dir.exists(), "the previous demo profile was lost"
    assert marker.read_text(encoding="utf-8") == previous


def _read_marker(demo_dir: Path) -> str:
    return (demo_dir / "categories_custom.json").read_text(encoding="utf-8")


def test_promotion_leaves_the_previous_demo_untouched_if_content_rename_fails(
    demo_dir, monkeypatch
):
    """A failure before the atomic swap (building/renaming the new content
    into place) must leave the previous demo profile exactly as it was --
    this covers the ordinary (non-crash) failure path."""
    bdp.build()
    previous = _read_marker(demo_dir)
    previous_target = os.readlink(demo_dir)

    def failing_rename(_src, _dst):
        raise OSError("simulated failure before the atomic swap")

    monkeypatch.setattr(bdp.os, "rename", failing_rename)

    with pytest.raises(OSError, match="simulated failure before the atomic swap"):
        bdp.build()

    assert demo_dir.is_symlink()
    assert os.readlink(demo_dir) == previous_target
    assert _read_marker(demo_dir) == previous


def test_promotion_is_committed_the_instant_the_atomic_swap_returns(
    demo_dir, monkeypatch
):
    """Regression: a real kill test (process terminated immediately after
    the first of two sequential os.rename calls) left var/demo missing
    entirely, recoverable only from a differently-named backup directory
    -- the two-rename scheme was not atomic. Promotion is now a single
    os.replace() of a symlink, which POSIX guarantees is atomic: once it
    returns, the new content is in place, full stop, regardless of
    whatever happens immediately afterward (including a crash the test
    cannot literally simulate here, but nothing meaningful can happen
    between "the syscall returned" and "the outcome is visible" -- they
    are the same instant).
    """
    bdp.build()
    old_target = os.readlink(demo_dir)
    # The build is deterministic (same tracked seed every time), so file
    # *content* is identical across builds by design -- what differs is
    # which content directory demo_dir points at, since each build gets a
    # freshly named temp directory. That symlink target is what proves a
    # promotion actually happened.

    real_replace = os.replace
    observed = {}

    def replace_then_blow_up(src, dst):
        real_replace(src, dst)
        # By the time we get here, the syscall has already returned --
        # demo_dir already resolves to the new content. Simulate the
        # process encountering a problem in this exact instant.
        observed["target_immediately_after_replace"] = os.readlink(dst)
        raise RuntimeError("simulated crash immediately after the atomic swap")

    monkeypatch.setattr(bdp.os, "replace", replace_then_blow_up)

    with pytest.raises(RuntimeError, match="simulated crash immediately after"):
        bdp.build()

    # The new content was already live at the moment of the simulated crash...
    assert observed["target_immediately_after_replace"] != old_target
    # ...and remains live now -- there was never a window where demo_dir
    # was missing or pointed at the old content after this point.
    assert demo_dir.is_symlink()
    assert os.readlink(demo_dir) == observed["target_immediately_after_replace"]


@pytest.mark.skipif(sys.platform == "win32", reason="fork() is POSIX-only")
def test_hard_kill_immediately_after_the_atomic_swap_leaves_demo_committed(
    demo_dir, tmp_path
):
    """Literal process-kill reproduction of the review's own method (a
    caught Python exception is not the same evidence as a real SIGKILL):
    fork a child that performs a build, patched to write a sentinel file
    the instant os.replace() returns and then block -- the parent sends
    SIGKILL the moment the sentinel appears, i.e. as close as observable
    to "immediately after the atomic swap" as a real kill can land. This
    is the exact scenario (kill right after the promotion's destructive
    half) that left var/demo entirely missing under the old two-rename
    implementation.
    """
    bdp.build()  # a "previous" build to distinguish the new one from
    old_target = os.readlink(demo_dir)

    sentinel = tmp_path / "replaced.marker"

    def child():
        real_replace = os.replace

        def patched(src, dst):
            real_replace(src, dst)
            sentinel.write_text("done")
            time.sleep(30)  # parent SIGKILLs us well before this elapses

        bdp.os.replace = patched
        bdp.build()

    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=child)
    proc.start()
    try:
        deadline = time.monotonic() + 10
        while not sentinel.exists():
            if time.monotonic() > deadline:
                proc.terminate()
                proc.join(timeout=5)
                pytest.fail("child process never reached the atomic swap in time")
            time.sleep(0.005)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join()

    assert demo_dir.is_symlink(), "demo_dir must still exist (as a symlink) after the hard kill"
    assert os.readlink(demo_dir) != old_target, (
        "the kill landed after the swap syscall returned, so the new "
        "content must already be live -- there is no window where a kill "
        "at this point leaves demo_dir missing or pointing at stale content"
    )


def test_promotion_never_deletes_the_previous_content_before_the_swap_commits(
    demo_dir, monkeypatch
):
    """If the atomic swap itself fails outright (raises without taking
    effect), the previous content directory must still be there and still
    be what demo_dir points to -- nothing is deleted ahead of a promotion
    that hasn't actually succeeded."""
    bdp.build()
    previous_target = os.readlink(demo_dir)
    previous_content_dir = demo_dir.parent / previous_target
    previous = _read_marker(demo_dir)

    def failing_replace(_src, _dst):
        raise OSError("simulated failure -- the swap never took effect")

    monkeypatch.setattr(bdp.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated failure -- the swap never took effect"):
        bdp.build()

    assert previous_content_dir.exists(), "the previous content was deleted before the swap committed"
    assert demo_dir.is_symlink()
    assert os.readlink(demo_dir) == previous_target
    assert _read_marker(demo_dir) == previous


# ── One-time legacy migration: demo_dir starts as a real directory ──────
#
# Upgrading from a pre-symlink var/demo/ real directory cannot be a single
# atomic syscall (POSIX rename(2) cannot atomically replace a non-empty
# real directory with a symlink), so these tests hold it to a narrower,
# honestly-documented guarantee: data is never deleted before its
# replacement is confirmed, and an interrupted migration self-heals on the
# next build -- not full atomicity, which the symlink-to-symlink case above
# already covers.

def _seed_legacy_real_demo_dir(demo_dir, marker_text: str = "LEGACY-MARKER") -> None:
    demo_dir.parent.mkdir(parents=True, exist_ok=True)
    demo_dir.mkdir()
    (demo_dir / "categories_custom.json").write_text(marker_text)


def test_legacy_migration_preserves_old_content_if_interrupted_before_the_swap(
    demo_dir, monkeypatch
):
    """Regression: an earlier version called shutil.rmtree() on the old
    real directory before attempting the swap, so a kill (or any failure)
    between the two calls destroyed the previous demo permanently. It must
    now be renamed aside (a non-destructive, atomic vacate), never
    deleted, until the new content is confirmed in place."""
    _seed_legacy_real_demo_dir(demo_dir)

    def failing_replace(_src, _dst):
        raise OSError("simulated failure between vacating demo_dir and the swap")

    monkeypatch.setattr(bdp.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated failure between vacating"):
        bdp.build()

    assert not demo_dir.exists(), (
        "demo_dir's old name is necessarily vacated first -- this is the "
        "one documented, unavoidable non-atomic gap"
    )
    backups = list(demo_dir.parent.glob(".previous-demo-*"))
    assert len(backups) == 1, "the old real directory must survive under a backup name"
    assert (backups[0] / "categories_custom.json").read_text() == "LEGACY-MARKER", (
        "the previous content must be fully recoverable, not destroyed"
    )


def test_legacy_migration_self_heals_on_the_next_build(demo_dir, monkeypatch):
    """After an interrupted migration (demo_dir absent, old content sitting
    in a .previous-demo-* backup), the very next build must succeed and
    fully replace demo_dir -- the missing-demo_dir state left behind is
    exactly the ordinary first-build case, which is fully atomic.

    Restores os.replace by hand rather than via monkeypatch.undo(): undo()
    reverts *every* patch made so far by this test's shared monkeypatch
    fixture instance, including the demo_dir fixture's own redirection of
    config.REPO_ROOT/DEMO_DIR/PERSONAL_DIR -- which would silently point
    the second build() call at this repository's real var/demo instead of
    the test's tmp_path. That is exactly the kind of test-isolation break
    this whole test suite exists to avoid.
    """
    _seed_legacy_real_demo_dir(demo_dir)

    real_replace = os.replace

    def failing_replace(_src, _dst):
        raise OSError("simulated interruption")

    monkeypatch.setattr(bdp.os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        bdp.build()
    assert not demo_dir.exists()

    bdp.os.replace = real_replace  # restore only this, nothing else
    summary = bdp.build()

    assert summary == EXPECTED_BASELINE
    assert demo_dir.is_symlink()
    assert (demo_dir / "categories_custom.json").read_text() != "LEGACY-MARKER"


def test_legacy_migration_succeeds_end_to_end_when_uninterrupted(demo_dir):
    _seed_legacy_real_demo_dir(demo_dir)

    summary = bdp.build()

    assert summary == EXPECTED_BASELINE
    assert demo_dir.is_symlink(), "demo_dir must be converted to the symlink scheme"
    # The old real directory's name is gone; its content was moved, not
    # copied, and the temporary backup is cleaned up after a successful swap.
    assert list(demo_dir.parent.glob(".previous-demo-*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="fork() is POSIX-only")
def test_hard_kill_during_legacy_migration_leaves_old_content_recoverable(
    demo_dir, tmp_path
):
    """Literal process-kill version of the two tests above, using the
    review's own method: SIGKILL sent the instant the old real directory
    has been vacated (renamed aside) but before the symlink swap runs.
    This is precisely the review's reproduction
    (legacy_demo_exists=False, legacy_content_dirs=1, legacy_temp_links=1)
    -- the fix does not make this instant atomic (which is not possible
    without an OS-specific syscall), but it does guarantee the old content
    is never unrecoverable.
    """
    _seed_legacy_real_demo_dir(demo_dir)
    sentinel = tmp_path / "vacated.marker"

    def child():
        real_rename = os.rename

        def patched(src, dst):
            real_rename(src, dst)
            if str(src) == str(demo_dir):
                sentinel.write_text("done")
                time.sleep(30)

        bdp.os.rename = patched
        bdp.build()

    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=child)
    proc.start()
    try:
        deadline = time.monotonic() + 10
        while not sentinel.exists():
            if time.monotonic() > deadline:
                proc.terminate()
                proc.join(timeout=5)
                pytest.fail("child process never reached the vacate-rename in time")
            time.sleep(0.005)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join()

    # The one documented non-atomic gap: demo_dir's old name is vacated
    # first, so it is legitimately absent right after this exact kill.
    assert not demo_dir.exists()
    backups = list(demo_dir.parent.glob(".previous-demo-*"))
    assert len(backups) == 1
    assert (backups[0] / "categories_custom.json").read_text() == "LEGACY-MARKER", (
        "the old content must survive the kill, recoverable under the backup name"
    )

    # And the next build recovers fully.
    summary = bdp.build()
    assert summary == EXPECTED_BASELINE
    assert demo_dir.is_symlink()


def test_build_ignores_ambient_mint_data_dir(demo_dir, monkeypatch, tmp_path):
    """MINT_DATA_DIR must not be able to redirect the build's data path
    out of the build directory."""
    stray = tmp_path / "stray_data"
    monkeypatch.setenv("MINT_DATA_DIR", str(stray))

    bdp.build()

    assert not stray.exists(), "the build wrote outside its build directory"
    assert list(demo_dir.rglob("transactions.parquet"))
    # And the caller's environment is restored afterwards.
    assert os.environ["MINT_DATA_DIR"] == str(stray)


def test_build_restores_the_surrounding_profile_env(demo_dir, monkeypatch, tmp_path):
    external = tmp_path / "external_profile"
    monkeypatch.setenv("MINT_PROFILE_DIR", str(external))
    bdp.build()
    assert os.environ["MINT_PROFILE_DIR"] == str(external)


def test_build_never_creates_scratch_files_beside_tracked_seed():
    # The tracked seed directory must contain only the seed JSON files and
    # its own README -- no dummy .pdf stand-ins or other build scratch
    # output (the old mock_import_all.py's exact flaw, per Surprise #7).
    seed_files = list(bdp.SEED_DIR.iterdir())
    for f in seed_files:
        assert f.suffix in (".json", ".md"), f"unexpected non-seed file in {bdp.SEED_DIR}: {f}"


def test_build_makes_no_network_request(demo_dir, monkeypatch):
    import socket

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted during demo build")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    bdp.build()  # must complete without tripping the guard above


def test_ingest_only_leaves_categorization_summary_at_zero(demo_dir):
    summary = bdp.build(apply_categories=False)
    assert summary == {"tx": 0, "auth_corr": 0, "seed_corr": 0, "id_seed": 0, "fuzzy": 0, "unmatched": 0}
    assert not (demo_dir / "categories_custom.json").exists()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions, so the unreadable-file probe below is meaningless",
)
def test_the_real_build_never_opens_an_active_personal_settings_file():
    """Regression: `import app` (which build_demo_profile.py does at
    module level, to reach app.config/normalize_parquet/update_categories)
    triggers app/__init__.py's configure_logging(), which read the active
    profile's settings.json *before* build()'s own isolation
    (_isolated_build_env) or the module-level MINT_PROFILE_DIR guard could
    take effect. That was a real, observed open() of the file (caught via
    strace against a real personal/ profile), even though it was never a
    write.

    This runs the real script (scripts/build_demo_profile.py, exactly as
    `make demo` does) as a subprocess against this actual repository, with
    a real (temporary) personal/ directory whose settings.json is made
    unreadable. If anything tries to open it, config.load_settings()'s
    "failed to load" warning -- which names the path and the OS error --
    appears in the subprocess's output; the guard fix means it must not
    ever be attempted, so that warning must never appear.
    """
    repo_root = bdp.REPO_ROOT  # already a Path
    personal_dir = repo_root / "personal"
    # personal/README.md and personal/settings.example.json are tracked
    # (Milestone 5), so personal/ itself always exists in any checkout --
    # what must never already be real is settings.json, the file that
    # actually activates personal mode.
    settings_path = personal_dir / "settings.json"
    if settings_path.exists():
        pytest.skip(
            "a real personal/settings.json already exists in this checkout "
            "-- refusing to touch it"
        )

    personal_dir.mkdir(exist_ok=True)
    settings_path.write_text("{}")
    settings_path.chmod(0o000)
    try:
        env = dict(os.environ)
        env.pop("MINT_PROFILE_DIR", None)
        env.pop("MINT_DATA_DIR", None)

        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "build_demo_profile.py")],
            capture_output=True, text=True, env=env, cwd=str(repo_root),
        )
        combined = result.stdout + result.stderr

        assert "Permission denied" not in combined, (
            "the build attempted to open the active personal profile's "
            f"settings file (see [SETTINGS] warning above):\n{combined}"
        )
        assert result.returncode == 0, combined
        assert "categorization summary" in combined
    finally:
        settings_path.chmod(0o644)
        settings_path.unlink()  # only the file this test created, never the
        # tracked personal/README.md or settings.example.json beside it


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions, so the unreadable-file probe below is meaningless",
)
def test_the_real_build_ignores_an_explicitly_exported_mint_profile_dir():
    """Regression: the module-level guard originally used
    os.environ.setdefault(), which only takes effect when MINT_PROFILE_DIR
    is unset. Exporting MINT_PROFILE_DIR=<path to personal/> before running
    `make demo` -- exactly what a user who normally works in personal mode
    would have in their shell -- meant the guard did nothing, and
    personal/settings.json was opened at import time regardless. The
    correct behaviour is that this script's target is *never* configurable
    via the environment; it always targets config.DEMO_DIR.

    Same unreadable-file probe as the test above, but with MINT_PROFILE_DIR
    explicitly pointed at the real personal/ directory rather than left
    unset -- the review's exact reproduction.
    """
    repo_root = bdp.REPO_ROOT
    personal_dir = repo_root / "personal"
    # personal/README.md and personal/settings.example.json are tracked
    # (Milestone 5), so personal/ itself always exists -- only settings.json
    # (what actually activates personal mode) must not already be real.
    settings_path = personal_dir / "settings.json"
    if settings_path.exists():
        pytest.skip(
            "a real personal/settings.json already exists in this checkout "
            "-- refusing to touch it"
        )

    personal_dir.mkdir(exist_ok=True)
    settings_path.write_text("{}")
    settings_path.chmod(0o000)
    try:
        env = dict(os.environ)
        env["MINT_PROFILE_DIR"] = str(personal_dir)  # the exact exploit
        env.pop("MINT_DATA_DIR", None)

        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "build_demo_profile.py")],
            capture_output=True, text=True, env=env, cwd=str(repo_root),
        )
        combined = result.stdout + result.stderr

        assert "Permission denied" not in combined, (
            "MINT_PROFILE_DIR pointed at personal/ still let the build open "
            f"its settings file:\n{combined}"
        )
        assert result.returncode == 0, combined
        assert "categorization summary" in combined
        # It really did still build the demo profile, not silently no-op.
        assert (repo_root / "var" / "demo").is_symlink()
    finally:
        settings_path.chmod(0o644)
        settings_path.unlink()  # only the file this test created


# ── BUG-03 fix: self-healing promotion (Milestone B, defense in depth) ──

def test_self_heal_deletes_a_stale_settings_json(demo_dir):
    """Simulates a leftover from an older, pre-fix build: settings.json
    already promoted, but still pointing at a directory outside demo_dir
    (exactly BUG-03's original symptom)."""
    demo_dir.mkdir(parents=True)
    stale_target = demo_dir.parent / ".build-old-and-gone"
    (demo_dir / "settings.json").write_text(json.dumps({
        "consolidated_statements": str(stale_target / "consolidated_statements"),
        "custom_categories_path": str(stale_target / "categories_custom.json"),
        "similarity_threshold": 80,
    }))

    bdp._self_heal_stale_settings(demo_dir)

    assert not (demo_dir / "settings.json").exists()


def test_self_heal_leaves_a_correct_settings_json_alone(demo_dir):
    demo_dir.mkdir(parents=True)
    correct = {
        "consolidated_statements": str(demo_dir / "consolidated_statements"),
        "custom_categories_path": str(demo_dir / "categories_custom.json"),
        "similarity_threshold": 80,
    }
    (demo_dir / "settings.json").write_text(json.dumps(correct))

    bdp._self_heal_stale_settings(demo_dir)

    assert json.loads((demo_dir / "settings.json").read_text()) == correct


def test_self_heal_is_a_noop_when_there_is_no_settings_file(demo_dir):
    demo_dir.mkdir(parents=True)
    bdp._self_heal_stale_settings(demo_dir)  # must not raise
    assert not (demo_dir / "settings.json").exists()


def test_self_heal_ignores_a_corrupt_settings_file(demo_dir):
    """A corrupt file is load_settings()'s job to repair, not this one's."""
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text("{ not valid json")

    bdp._self_heal_stale_settings(demo_dir)  # must not raise or delete it

    assert (demo_dir / "settings.json").read_text() == "{ not valid json"


def test_full_build_self_heals_a_pre_existing_stale_settings_file(demo_dir):
    """End-to-end: seed a stale settings.json (as if left by an old, pre-fix
    build) directly at config.DEMO_DIR *before* running build() at all, then
    confirm build() heals it and the final profile serves correctly."""
    demo_dir.mkdir(parents=True)
    stale_target = demo_dir.parent / ".build-old-and-gone"
    (demo_dir / "settings.json").write_text(json.dumps({
        "consolidated_statements": str(stale_target / "consolidated_statements"),
    }))

    summary = bdp.build()

    assert summary == EXPECTED_BASELINE
    data = json.loads((demo_dir / "settings.json").read_text())
    # Milestone A means a freshly (re)created file has no path keys at all --
    # confirms the self-heal deleted the stale one and let it regenerate
    # correctly, rather than leaving the old content in place.
    assert "consolidated_statements" not in data


def test_categories_demo_seed_has_no_decorative_non_tree_keys():
    # Regression: an earlier generator wrote a decorative top-level
    # "_unmatched_example" key ({"transaction_id": ..., "description": ...})
    # that was never a real seed and, as a dict shaped unlike a category
    # node (no _seeds/_deleted/_flags, no nested category children), would
    # break categories.py's recursive tree walk if ever loaded as
    # categories_custom.json.
    data = json.loads(bdp.CATEGORIES_SEED_PATH.read_text(encoding="utf-8"))
    assert "_unmatched_example" not in data

    def _is_category_node(node: dict) -> bool:
        structural_keys = {"_seeds", "_deleted", "_flags"}
        return bool(structural_keys & node.keys()) or all(
            isinstance(v, dict) for v in node.values()
        )

    for key, node in data.items():
        if key in ("_seeds", "_deleted"):
            assert isinstance(node, list), f"{key!r} must be a list"
            continue
        assert isinstance(node, dict), f"top-level key {key!r} is not a category node"
        assert _is_category_node(node), f"top-level key {key!r} does not look like a category node"
