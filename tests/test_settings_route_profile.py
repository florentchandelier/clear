"""Settings route shows the active profile mode/path.

Uses MINT_PROFILE_DIR to point the whole app at an isolated tmp_path, so
this test never touches the repository's real personal/ or var/demo/.
"""
from __future__ import annotations

import json
import re

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
    assert not re.search(r'id="consolidated_statements"[^>]*readonly', body)
    assert not re.search(r'id="custom_categories_path"[^>]*readonly', body)


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
    assert "managed by <code>make demo</code>" in body
    assert re.search(r'id="consolidated_statements"[^>]*readonly', body)
    assert re.search(r'id="custom_categories_path"[^>]*readonly', body)


def test_demo_settings_post_cannot_persist_managed_paths(
    client, monkeypatch, isolated_external_profile
):
    """The server-side guard must hold when a client bypasses the read-only
    HTML fields and forges arbitrary path values."""
    from app import config

    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.setattr(config, "PERSONAL_DIR", isolated_external_profile / "personal")
    demo_dir = isolated_external_profile / "var" / "demo"
    monkeypatch.setattr(config, "DEMO_DIR", demo_dir)

    response = client.post(
        "/settings",
        data={
            "consolidated_statements": str(isolated_external_profile / "outside-data"),
            "base_categories_path": str(config.BASE_CATEGORIES_PATH),
            "custom_categories_path": str(
                isolated_external_profile / "outside-categories.json"
            ),
            "similarity_threshold": "43",
            "min_cluster_tx": "2",
            "single_tx_bucket": "DEMO_SMALL",
            "overwrite_duplicates": "on",
        },
    )

    assert response.status_code == 302
    persisted = json.loads((demo_dir / "settings.json").read_text())
    assert "consolidated_statements" not in persisted
    assert "custom_categories_path" not in persisted
    assert persisted["similarity_threshold"] == 43
    assert persisted["min_cluster_tx"] == 2
    assert persisted["single_tx_bucket"] == "DEMO_SMALL"
    assert persisted["overwrite_duplicates"] is True

    reloaded = config.load_settings()
    assert reloaded["consolidated_statements"] == str(
        (demo_dir / "consolidated_statements").resolve()
    )
    assert reloaded["custom_categories_path"] == str(
        (demo_dir / "categories_custom.json").resolve()
    )
