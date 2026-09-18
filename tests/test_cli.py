"""Tests for app/cli/cli.py.

BUG-05 regression: `run.py update_categories` crashed
with `'str' object has no attribute 'is_file'` because
update_categories.update_categories() requires a Path (it calls
collect_parquet_files(), which calls .is_file() on its argument), while
settings values and --parquet are always plain strings.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from app import config
from app.cli import cli
from scripts import build_demo_profile as bdp


def argparse_namespace(**kwargs):
    return argparse.Namespace(**kwargs)


@pytest.fixture(autouse=True)
def clean_profile_env(monkeypatch):
    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)


@pytest.fixture
def demo_profile(monkeypatch, tmp_path):
    """A real, built demo profile isolated under tmp_path -- matches the
    pattern in tests/test_build_demo_profile.py."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEMO_DIR", tmp_path / "var" / "demo")
    monkeypatch.setattr(config, "PERSONAL_DIR", tmp_path / "personal")
    bdp.build()
    return config.DEMO_DIR


def test_cmd_update_categories_passes_a_path_not_a_string(monkeypatch, demo_profile):
    """Regression: pins the actual type-fix, independent of whatever
    collect_parquet_files() happens to do internally with it."""
    captured = {}

    def fake_update_categories(parquet_dir, **kwargs):
        captured["parquet_dir"] = parquet_dir
        return []

    monkeypatch.setattr(cli.update_categories, "update_categories", fake_update_categories)

    args = argparse_namespace(parquet=None, threshold=85, all_tx=False, year=None,
                               uncategorized=False, unsubcategorized=False)
    cli.cmd_update_categories(args)

    assert isinstance(captured["parquet_dir"], Path)
    assert captured["parquet_dir"] == Path(config.load_settings()["consolidated_statements"])


def test_cmd_update_categories_wraps_an_explicit_parquet_arg_too(monkeypatch, demo_profile, tmp_path):
    captured = {}
    monkeypatch.setattr(
        cli.update_categories, "update_categories",
        lambda parquet_dir, **kwargs: captured.setdefault("parquet_dir", parquet_dir) or []
    )

    explicit = str(demo_profile / "consolidated_statements")
    args = argparse_namespace(parquet=explicit, threshold=80, all_tx=False, year=None,
                               uncategorized=False, unsubcategorized=False)
    cli.cmd_update_categories(args)

    assert captured["parquet_dir"] == Path(explicit)


def test_run_cli_update_categories_succeeds_end_to_end(demo_profile, capsys):
    """The exact reproduction from bug.md, run for real against a built
    demo profile -- must not crash, and must actually update categories."""
    cli.run_cli(["update_categories", "--threshold", "85"])

    out = capsys.readouterr().out
    assert "Categories updated successfully." in out
    assert "Update failed" not in out


def test_run_cli_update_categories_with_explicit_parquet_arg(demo_profile, capsys):
    consolidated = str(demo_profile / "consolidated_statements")
    cli.run_cli(["update_categories", "--parquet", consolidated, "--threshold", "80"])

    out = capsys.readouterr().out
    assert "Categories updated successfully." in out


def test_normalize_subcommand_was_removed(capsys):
    """BUG-06: `normalize` called normalize_parquet.normalize_and_store(),
    which doesn't exist -- an unconditional crash. Its only caller
    (app/services/normalize.py) and its only other user (the dead,
    unregistered app/web/routes.py) were both dead code, so the command
    was removed rather than patched. Must not come back as a
    silently-broken subcommand."""
    assert not hasattr(cli, "cmd_normalize")

    with pytest.raises(SystemExit):
        cli.run_cli(["normalize", "some_file.json"])

    err = capsys.readouterr().err
    assert "invalid choice: 'normalize'" in err
