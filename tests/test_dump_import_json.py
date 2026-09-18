"""Tests for the genericized scripts/dump_import_json.py.

Replaces hardcoded absolute paths and a fixed 4-importer list with CLI
arguments and full registry discovery -- these tests pin both properties,
plus an end-to-end run using the tracked synthetic PDF fixture from
Milestone 4 (never a real statement).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import dump_import_json as dij

FIXTURE_PDF = (
    Path(__file__).resolve().parent
    / "importers" / "fixtures" / "pdf" / "bmo_chequing_fr_demo.pdf"
)


def test_no_hardcoded_personal_path_in_source():
    source = Path(dij.__file__).read_text(encoding="utf-8")
    assert "pdf_statements" not in source
    assert "/media/" not in source


def test_default_input_dir_is_the_documented_personal_location():
    assert dij.DEFAULT_INPUT_DIR == dij.REPO_ROOT / "personal" / "statements"


def test_all_importers_discovers_every_registered_importer():
    keys = {imp.key for imp in dij.all_importers()}
    # Matches the full registry as of Milestone 4 -- not the old hardcoded
    # 4-importer subset.
    assert keys == {
        "cash.bmo_chequing_fr",
        "credit.bmo_mastercard_fr",
        "investment.bmo_nesbitt_ca",
        "investment.interactive_brokers_ca",
        "investment.questrade_equity",
        "loc.bmo_heloc_fr",
        "asset.car_fmv_cargurus",
        "asset.home_fmv_evaluation_fonciere",
    }


def test_detect_importer_finds_the_right_one_for_the_demo_fixture():
    importer = dij.detect_importer(FIXTURE_PDF, list(dij.all_importers()))
    assert importer is not None
    assert importer.key == "cash.bmo_chequing_fr"


def test_detect_importer_returns_none_for_an_unrecognized_pdf(tmp_path):
    fake_pdf = tmp_path / "not_a_statement.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert dij.detect_importer(fake_pdf, list(dij.all_importers())) is None


def test_end_to_end_export_of_the_synthetic_fixture(tmp_path):
    input_dir = tmp_path / "statements"
    input_dir.mkdir()
    shutil.copy(FIXTURE_PDF, input_dir / "demo_statement.pdf")

    rc = dij.main(["--input", str(input_dir)])

    assert rc == 0
    out_file = input_dir / "json_exports" / "demo_statement.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["document_signature"]["document_type"] == "BMO_CHEQUING_FRENCH"
    assert data["validation_passed"] is True


def test_custom_output_dir_is_honoured(tmp_path):
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    shutil.copy(FIXTURE_PDF, input_dir / "demo_statement.pdf")

    rc = dij.main(["--input", str(input_dir), "--output", str(output_dir)])

    assert rc == 0
    assert (output_dir / "demo_statement.json").exists()
    assert not (input_dir / "json_exports").exists()


def test_skips_already_exported_files_without_force(tmp_path, capsys):
    input_dir = tmp_path / "statements"
    input_dir.mkdir()
    shutil.copy(FIXTURE_PDF, input_dir / "demo_statement.pdf")

    dij.main(["--input", str(input_dir)])
    out_file = input_dir / "json_exports" / "demo_statement.json"
    first_mtime = out_file.stat().st_mtime_ns

    dij.main(["--input", str(input_dir)])
    assert out_file.stat().st_mtime_ns == first_mtime
    assert "already exported" in capsys.readouterr().out


def test_force_reexports_an_already_exported_file(tmp_path):
    input_dir = tmp_path / "statements"
    input_dir.mkdir()
    shutil.copy(FIXTURE_PDF, input_dir / "demo_statement.pdf")

    dij.main(["--input", str(input_dir)])
    out_file = input_dir / "json_exports" / "demo_statement.json"
    out_file.write_text("{}")  # corrupt it, to prove --force actually rewrites

    dij.main(["--input", str(input_dir), "--force"])
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["document_signature"]["document_type"] == "BMO_CHEQUING_FRENCH"


def test_missing_input_dir_reports_and_returns_nonzero(tmp_path, capsys):
    rc = dij.main(["--input", str(tmp_path / "does_not_exist")])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().out


def test_empty_input_dir_is_not_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert dij.main(["--input", str(empty)]) == 0
