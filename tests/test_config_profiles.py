"""Tests for the demo/personal profile resolution in app/config.py.

Every test monkeypatches config.PERSONAL_DIR / config.DEMO_DIR to
directories under tmp_path, so none of them touch this repository's real
personal/ or var/demo/ directories.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import config


@pytest.fixture(autouse=True)
def clean_profile_env(monkeypatch):
    """Never let a stray MINT_PROFILE_DIR / MINT_DATA_DIR leak into a test."""
    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)


@pytest.fixture
def profiles(monkeypatch, tmp_path):
    """Redirect PERSONAL_DIR/DEMO_DIR to tmp_path so tests never touch the
    real repository's personal/ or var/demo/."""
    personal_dir = tmp_path / "personal"
    demo_dir = tmp_path / "var" / "demo"
    monkeypatch.setattr(config, "PERSONAL_DIR", personal_dir)
    monkeypatch.setattr(config, "DEMO_DIR", demo_dir)
    return personal_dir, demo_dir


def test_demo_is_default_profile(profiles):
    personal_dir, demo_dir = profiles
    assert config.active_profile_dir() == demo_dir.resolve()
    assert not personal_dir.exists()


def test_personal_profile_selected_once_its_settings_exist(profiles):
    personal_dir, demo_dir = profiles
    personal_dir.mkdir(parents=True)
    (personal_dir / "settings.json").write_text("{}")
    assert config.active_profile_dir() == personal_dir.resolve()


def test_mint_profile_dir_env_overrides_personal_and_demo(profiles, monkeypatch, tmp_path):
    personal_dir, demo_dir = profiles
    personal_dir.mkdir(parents=True)
    (personal_dir / "settings.json").write_text("{}")

    external = tmp_path / "external_profile"
    external.mkdir()
    monkeypatch.setenv("MINT_PROFILE_DIR", str(external))

    assert config.active_profile_dir() == external.resolve()


def test_load_settings_creates_demo_only_never_personal(profiles):
    personal_dir, demo_dir = profiles
    data = config.load_settings()

    assert (demo_dir / "settings.json").exists()
    assert not (personal_dir / "settings.json").exists()
    assert data["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    )


def test_auto_created_settings_file_never_persists_path_keys(profiles):
    """BUG-03 regression: an auto-created settings.json must never freeze
    consolidated_statements/custom_categories_path, since those are
    computed from the profile directory's current location and can go
    stale if that directory is ever renamed after this write (exactly
    what scripts/build_demo_profile.py's atomic promotion does).
    base_categories_path is exempt -- it's profile-independent, so it
    can't go stale this way -- and every other (non-path) key is still
    persisted as before.
    """
    _personal_dir, demo_dir = profiles
    data = config.load_settings()

    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert "consolidated_statements" not in persisted
    assert "custom_categories_path" not in persisted
    assert persisted["base_categories_path"] == data["base_categories_path"]
    assert persisted["similarity_threshold"] == data["similarity_threshold"]
    assert persisted["display_currency"] == data["display_currency"]

    # The in-memory return value for *this* call is unaffected -- callers
    # still get a usable, fully-resolved path immediately.
    assert data["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    )


def test_repaired_corrupt_settings_file_never_persists_path_keys(profiles):
    """Same guarantee as above, for the corrupted-JSON repair branch."""
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text("{ not valid json")

    config.load_settings()

    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert "consolidated_statements" not in persisted
    assert "custom_categories_path" not in persisted


def test_settings_survive_a_profile_directory_rename():
    """The exact BUG-03 reproduction: a profile directory is renamed after
    its settings.json is first written (what scripts/build_demo_profile.py's
    _promote() does to build_dir). Before the fix, load_settings() after
    the rename would return a path pointing at the old, now-nonexistent
    directory name; after the fix, it always resolves fresh against
    wherever the profile directory actually is.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    try:
        original = tmp / "var" / ".build-abc123"
        original.mkdir(parents=True)

        os.environ["MINT_PROFILE_DIR"] = str(original)
        try:
            config.load_settings()  # creates original/settings.json
        finally:
            del os.environ["MINT_PROFILE_DIR"]

        renamed = tmp / "var" / ".demo-content-abc123"
        os.rename(original, renamed)

        os.environ["MINT_PROFILE_DIR"] = str(renamed)
        try:
            data = config.load_settings()
        finally:
            del os.environ["MINT_PROFILE_DIR"]

        assert data["consolidated_statements"] == str(
            (renamed / "consolidated_statements").resolve()
        )
        assert str(original) not in data["consolidated_statements"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_settings_never_creates_personal_profile_implicitly(profiles):
    # Calling load_settings() repeatedly must never cause personal/ to spring
    # into existence -- activating it is an explicit, separate action.
    personal_dir, _demo_dir = profiles
    for _ in range(3):
        config.load_settings()
    assert not personal_dir.exists()


def test_save_then_load_symmetry(profiles):
    _personal_dir, demo_dir = profiles
    data = config.load_settings()
    data["similarity_threshold"] = 42
    config.save_settings(data)

    reloaded = config.load_settings()
    assert reloaded["similarity_threshold"] == 42
    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert persisted["similarity_threshold"] == 42
    assert "consolidated_statements" not in persisted
    assert "custom_categories_path" not in persisted


def test_save_settings_never_persists_demo_managed_paths(profiles, tmp_path):
    """Even a forged caller value must not turn a generated demo path into
    durable configuration."""
    _personal_dir, demo_dir = profiles
    data = config.load_settings()
    data["consolidated_statements"] = str(tmp_path / "outside-data")
    data["custom_categories_path"] = str(tmp_path / "outside-categories.json")
    data["similarity_threshold"] = 41

    config.save_settings(data)

    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert "consolidated_statements" not in persisted
    assert "custom_categories_path" not in persisted
    assert persisted["similarity_threshold"] == 41

    reloaded = config.load_settings()
    assert reloaded["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    )
    assert reloaded["custom_categories_path"] == str(
        (demo_dir / "categories_custom.json").resolve()
    )


def test_legacy_demo_path_overrides_are_ignored_on_load(profiles, tmp_path):
    """Existing files written by older versions become safe immediately."""
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text(json.dumps({
        "consolidated_statements": str(tmp_path / "stale-data"),
        "custom_categories_path": str(tmp_path / "stale-categories.json"),
        "similarity_threshold": 37,
    }))

    data = config.load_settings()

    assert data["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    )
    assert data["custom_categories_path"] == str(
        (demo_dir / "categories_custom.json").resolve()
    )
    assert data["similarity_threshold"] == 37


def test_explicit_save_survives_demo_profile_rename_without_stale_paths(
    profiles, monkeypatch
):
    """Save the resolved form values, rename the profile, then confirm both
    generated paths follow it."""
    _personal_dir, original = profiles
    data = config.load_settings()
    config.save_settings(data)

    renamed = original.with_name("demo-promoted")
    os.rename(original, renamed)
    monkeypatch.setattr(config, "DEMO_DIR", renamed)

    reloaded = config.load_settings()
    assert reloaded["consolidated_statements"] == str(
        (renamed / "consolidated_statements").resolve()
    )
    assert reloaded["custom_categories_path"] == str(
        (renamed / "categories_custom.json").resolve()
    )


def test_save_writes_to_active_profile_not_the_other_one(profiles):
    personal_dir, demo_dir = profiles
    personal_dir.mkdir(parents=True)
    (personal_dir / "settings.json").write_text("{}")

    data = config.load_settings()
    data["similarity_threshold"] = 99
    data["consolidated_statements"] = "/personal/data"
    data["custom_categories_path"] = "/personal/categories.json"
    config.save_settings(data)

    persisted = json.loads((personal_dir / "settings.json").read_text())
    assert persisted["similarity_threshold"] == 99
    assert persisted["consolidated_statements"] == "/personal/data"
    assert persisted["custom_categories_path"] == "/personal/categories.json"
    assert not demo_dir.exists()


def test_save_preserves_managed_path_keys_for_explicit_external_profile(
    profiles, monkeypatch, tmp_path
):
    _personal_dir, _demo_dir = profiles
    external = tmp_path / "external"
    monkeypatch.setenv("MINT_PROFILE_DIR", str(external))
    data = config.load_settings()
    data["consolidated_statements"] = "/external/data"
    data["custom_categories_path"] = "/external/categories.json"

    config.save_settings(data)

    persisted = json.loads((external / "settings.json").read_text())
    assert persisted["consolidated_statements"] == "/external/data"
    assert persisted["custom_categories_path"] == "/external/categories.json"


def test_relative_consolidated_statements_anchored_at_external_profile_dir(
    profiles, monkeypatch
):
    _personal_dir, demo_dir = profiles
    monkeypatch.setenv("MINT_PROFILE_DIR", str(demo_dir))
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text(
        json.dumps({"consolidated_statements": "my_data", "custom_categories_path": "my_custom.json"})
    )

    data = config.load_settings()

    assert data["consolidated_statements"] == str((demo_dir / "my_data").resolve())
    assert data["custom_categories_path"] == str((demo_dir / "my_custom.json").resolve())


def test_absolute_consolidated_statements_pass_through_external_profile(
    profiles, monkeypatch, tmp_path
):
    _personal_dir, demo_dir = profiles
    monkeypatch.setenv("MINT_PROFILE_DIR", str(demo_dir))
    demo_dir.mkdir(parents=True)
    absolute = tmp_path / "elsewhere"
    (demo_dir / "settings.json").write_text(
        json.dumps({"consolidated_statements": str(absolute)})
    )

    data = config.load_settings()
    assert data["consolidated_statements"] == str(absolute.resolve())


def test_relative_base_categories_path_anchored_at_repo_root(profiles):
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text(
        json.dumps({"base_categories_path": "some_categories.json"})
    )

    data = config.load_settings()
    assert data["base_categories_path"] == str(
        (config.REPO_ROOT / "some_categories.json").resolve()
    )


def test_corrupted_settings_json_is_repaired_without_crossing_profiles(profiles):
    personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text("{not valid json")

    data = config.load_settings()

    assert data["similarity_threshold"] == 80  # back to defaults
    assert json.loads((demo_dir / "settings.json").read_text())["similarity_threshold"] == 80
    assert not personal_dir.exists()


def test_mint_data_dir_overrides_only_consolidated_statements(profiles, monkeypatch, tmp_path):
    _personal_dir, demo_dir = profiles
    override = tmp_path / "container_data"
    monkeypatch.setenv("MINT_DATA_DIR", str(override))

    data = config.load_settings()

    assert data["consolidated_statements"] == str(override.resolve())
    assert data["custom_categories_path"] == str(
        (demo_dir / "categories_custom.json").resolve()
    )
    # The override is in-memory only -- never baked into the persisted file.
    # Since the BUG-03 fix, consolidated_statements isn't persisted by an
    # auto-created settings.json at all (see
    # test_auto_created_settings_file_never_persists_path_keys), which is
    # an even stronger guarantee than "differs from the override value".
    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert "consolidated_statements" not in persisted
    # ...including via data_dir_env, which load_settings() also honours.
    assert persisted.get("data_dir_env") in ("", None), (
        "MINT_DATA_DIR must not be persisted into the profile"
    )


def test_mint_data_dir_override_does_not_outlive_the_env_var(profiles, monkeypatch, tmp_path):
    """Regression: the env var used to be copied into the profile's
    data_dir_env on first run and persisted, after which load_settings()
    kept applying it even once the variable was gone."""
    _personal_dir, demo_dir = profiles
    override = tmp_path / "container_data"

    monkeypatch.setenv("MINT_DATA_DIR", str(override))
    assert config.load_settings()["consolidated_statements"] == str(override.resolve())

    monkeypatch.delenv("MINT_DATA_DIR")
    after = config.load_settings()
    assert after["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    ), "the data-dir override outlived the environment variable that set it"


def test_explicit_data_dir_env_in_a_settings_file_is_still_honoured(profiles, tmp_path):
    """Not persisting the env var must not break a user who sets
    data_dir_env deliberately in their own settings file."""
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    chosen = tmp_path / "user_chosen_data"
    (demo_dir / "settings.json").write_text(json.dumps({"data_dir_env": str(chosen)}))

    data = config.load_settings()
    assert data["consolidated_statements"] == str(chosen.resolve())


def test_read_only_load_does_not_create_a_missing_settings_file(profiles):
    _personal_dir, demo_dir = profiles
    data = config.load_settings(create_missing=False)

    assert data["similarity_threshold"] == 80  # defaults still returned
    assert not (demo_dir / "settings.json").exists(), (
        "create_missing=False must not write anything"
    )


def test_read_only_load_does_not_repair_a_corrupt_settings_file(profiles):
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    corrupt = "{ not valid json"
    (demo_dir / "settings.json").write_text(corrupt)

    data = config.load_settings(create_missing=False)

    assert data["similarity_threshold"] == 80
    assert (demo_dir / "settings.json").read_text() == corrupt


def test_importing_app_never_writes_the_active_profile(tmp_path):
    """Regression: app/__init__.py calls configure_logging(), which reads
    settings. Before this was made read-only, *any* import of the package
    -- including by `make demo` -- created or repaired the active
    profile's settings file. That silently rewrote a real personal
    profile.

    This has to run in a subprocess: the import side effect happens once,
    at interpreter start, long before an in-process test could observe it.
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    corrupt = "{ hand-edited by the user, currently broken"
    (profile / "settings.json").write_text(corrupt)

    env = {
        **os.environ,
        "MINT_PROFILE_DIR": str(profile),
        "PYTHONPATH": str(config.REPO_ROOT),
    }
    env.pop("MINT_DATA_DIR", None)

    result = subprocess.run(
        [sys.executable, "-c", "import app  # noqa"],
        capture_output=True, text=True, env=env, cwd=str(config.REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr

    assert (profile / "settings.json").read_text() == corrupt, (
        "importing the app package rewrote the active profile's settings file"
    )
    assert not (profile / "consolidated_statements").exists()


def test_base_categories_path_is_never_profile_scoped(profiles):
    _personal_dir, demo_dir = profiles
    demo_dir.mkdir(parents=True)
    (demo_dir / "settings.json").write_text("{}")

    data = config.load_settings()
    # Equal to the fixed repo-root constant, not anything under the (tmp_path)
    # demo profile dir -- proves the base template is never profile-scoped.
    assert data["base_categories_path"] == str(config.BASE_CATEGORIES_PATH)
    assert str(demo_dir) not in data["base_categories_path"]
