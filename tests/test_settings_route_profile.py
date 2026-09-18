"""Settings route shows the active profile mode/path.

Uses MINT_PROFILE_DIR to point the whole app at an isolated tmp_path, so
this test never touches the repository's real personal/ or var/demo/.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_external_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("MINT_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)
    return tmp_path


@pytest.fixture
def client():
    from app.web.flask_ui import create_ui_app

    app = create_ui_app()
    app.testing = True
    return app.test_client()


def test_settings_page_shows_external_profile_label_and_path(client, isolated_external_profile):
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "Active profile" in body
    assert "External (MINT_PROFILE_DIR)" in body
    assert str(isolated_external_profile.resolve()) in body


def test_settings_page_shows_demo_label_without_mint_profile_dir(client, monkeypatch, isolated_external_profile):
    # Unset the override this test's own fixture set, and instead redirect
    # the demo profile itself into tmp_path so this still never touches the
    # real repository's var/demo/.
    from app import config

    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.setattr(config, "PERSONAL_DIR", isolated_external_profile / "personal")
    monkeypatch.setattr(config, "DEMO_DIR", isolated_external_profile / "var" / "demo")

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "Demo (synthetic data)" in body
