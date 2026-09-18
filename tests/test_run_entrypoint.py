"""run.py must work from a clean checkout.

Two things are being protected here:

1. The legacy `web` entrypoint (app/web/flask_app.py, which imported the
   untracked app/web/routes.py) has been removed -- it was dormant, and a
   clean checkout could never run it. Nothing may reintroduce an import of
   it.
2. Imports are done lazily, inside the branch that needs them, so one
   entrypoint's dependencies cannot break the others.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import run


def test_run_module_has_no_module_level_entrypoint_imports():
    """A module-level import of an entrypoint makes *every* command depend
    on it -- which is how a missing app/web/routes.py used to break the
    CLI and web_ui too."""
    source = Path(run.__file__).read_text(encoding="utf-8")
    module_level = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and any(name in line for name in ("flask_app", "flask_ui", "app.cli"))
    ]
    assert not module_level, f"run.py imports entrypoints eagerly: {module_level}"


def test_legacy_flask_app_module_is_gone():
    assert not (Path(run.__file__).parent / "app" / "web" / "flask_app.py").exists(), (
        "the legacy web entrypoint was removed and must not come back "
        "untracked -- it depends on app/web/routes.py, which is not tracked"
    )


def test_legacy_routes_module_is_gone():
    """BUG-06: app/web/routes.py was an untracked, unregistered blueprint
    (no import of it appears anywhere else in the app) kept alive only by
    its own dead /import route calling app/services/normalize.py. Both
    were deleted together; this must not come back."""
    assert not (Path(run.__file__).parent / "app" / "web" / "routes.py").exists(), (
        "the legacy, unregistered app/web/routes.py must not come back"
    )


def test_legacy_normalize_service_module_is_gone():
    """BUG-06: app/services/normalize.py implemented a self-contained
    ingest pipeline for a JSON shape (top-level document_type,
    statements[].regular_transactions/transfer_transactions) that no
    current importer produces -- its only caller was the deleted
    app/web/routes.py. cmd_normalize (app/cli/cli.py) called a
    same-named function on the wrong module and crashed unconditionally;
    the fix was to delete the whole dead call path, not patch the import."""
    assert not (Path(run.__file__).parent / "app" / "services" / "normalize.py").exists(), (
        "the legacy, uncalled app/services/normalize.py must not come back"
    )


def test_web_ui_command_dispatches_to_the_supported_ui(monkeypatch):
    called = {}
    from app.web import flask_ui

    monkeypatch.setattr(flask_ui, "run_web_ui", lambda: called.setdefault("ui", True))
    run.main(["run.py", "web_ui"])
    assert called == {"ui": True}


def test_other_commands_dispatch_to_the_cli(monkeypatch):
    called = {}
    from app.cli import cli

    monkeypatch.setattr(cli, "run_cli", lambda: called.setdefault("cli", True))
    run.main(["run.py", "query"])
    assert called == {"cli": True}


def test_removed_legacy_web_command_falls_through_to_the_cli(monkeypatch):
    """`run.py web` no longer has a branch of its own; it reaches the CLI,
    whose argparse reports the valid commands."""
    called = {}
    from app.cli import cli

    monkeypatch.setattr(cli, "run_cli", lambda: called.setdefault("cli", True))
    run.main(["run.py", "web"])
    assert called == {"cli": True}


def test_web_ui_works_without_the_untracked_legacy_routes_module(monkeypatch):
    """Simulate a clean checkout, where app/web/routes.py does not exist.

    Setting the entry to None makes `import app.web.routes` raise
    ImportError, exactly as an absent module does.
    """
    monkeypatch.setitem(sys.modules, "app.web.routes", None)
    called = {}
    from app.web import flask_ui

    monkeypatch.setattr(flask_ui, "run_web_ui", lambda: called.setdefault("ui", True))
    run.main(["run.py", "web_ui"])
    assert called == {"ui": True}
