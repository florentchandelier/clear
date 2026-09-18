"""Milestone 4: one real synthetic-PDF import, end to end.

Each test below maps to one of the seven promotion criteria for the
BMO chequing (FR) prototype, so a failure names the criterion that broke.

Nothing here reads a private statement: the only PDF involved is the
tracked, generated fixture under tests/importers/fixtures/pdf/.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from app import config
from app.services import queries as q
from app.services.importers.registry import importer_by_key
from scripts import generate_demo_pdfs as gen

PDF_NAME = "bmo_chequing_fr_demo.pdf"
IMPORTER_KEY = "cash.bmo_chequing_fr"
UPLOAD_FILENAME = "clear_test_bmo_chequing_demo.pdf"

FIXTURE_PDF = gen.PDF_FIXTURE_DIR / PDF_NAME
FIXTURE_EXPECTED = gen.expected_json_path(FIXTURE_PDF)


@pytest.fixture(autouse=True)
def clean_profile_env(monkeypatch):
    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)


# ── Criterion 1: generator output is a valid PDF ──────────────────────

def test_c1_generated_file_is_a_valid_pdf(tmp_path):
    out = tmp_path / PDF_NAME
    gen.build_bmo_chequing_pdf(out)

    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")

    import pdfplumber

    with pdfplumber.open(out) as pdf:
        assert len(pdf.pages) >= 1
        text = pdf.pages[0].extract_text() or ""
    assert "Relevé de services bancaires courants" in text


def test_c1_tracked_fixture_pdf_exists_and_is_small_enough_to_track():
    assert FIXTURE_PDF.exists(), "the tracked synthetic PDF fixture is missing"
    # Keep tracked binaries tiny; this one is ~2 KB.
    assert FIXTURE_PDF.stat().st_size < 200_000


# ── Criterion 2: the *registered* importer detects it, no fixture-only logic ──

def test_c2_registered_importer_detects_the_synthetic_pdf():
    importer = importer_by_key(IMPORTER_KEY)
    assert importer is not None, f"{IMPORTER_KEY} is not in the registry"
    assert importer.detect(FIXTURE_PDF) is True


def test_c2_importers_contain_no_fixture_specific_logic():
    """Decision D8: importers must not be weakened or special-cased for a
    fixture. No importer source may mention the fixture file or its
    directory."""
    importer_root = Path(config.REPO_ROOT) / "app" / "services" / "importers"
    forbidden = ("bmo_chequing_fr_demo", "fixtures/pdf", "fixtures\\pdf")
    offenders = []
    for py in importer_root.rglob("*.py"):
        source = py.read_text(encoding="utf-8", errors="ignore")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{py.relative_to(config.REPO_ROOT)}: {needle}")
    assert not offenders, f"fixture-specific logic found in importers: {offenders}"


# ── Criterion 3: parsed JSON matches the reviewed fixture ─────────────

def _parse_fixture_pdf() -> dict:
    importer = importer_by_key(IMPORTER_KEY)
    return importer.parse_to_json(FIXTURE_PDF)


def test_c3_parse_matches_reviewed_expected_fixture():
    parsed = _parse_fixture_pdf()
    expected = json.loads(FIXTURE_EXPECTED.read_text(encoding="utf-8"))

    # export_date is datetime.now() inside the importer, so it is compared
    # for shape, not value (the golden file stores a placeholder).
    assert expected["export_date"] == gen.EXPORT_DATE_PLACEHOLDER
    parse_date = parsed.pop("export_date")
    expected.pop("export_date")
    assert len(parse_date) == 10 and parse_date.count("-") == 2

    assert parsed == expected


def test_c3_parse_reflects_what_the_document_actually_says(tmp_path):
    """Guard against a golden file that merely records a wrong parse: the
    numbers are re-derived from the generator's own return value."""
    stated = gen.build_bmo_chequing_pdf(tmp_path / PDF_NAME)
    parsed = _parse_fixture_pdf()
    summary = parsed["statements"][0]["summary"]
    txs = parsed["statements"][0]["transactions"]

    assert len(txs) == stated["transaction_count"]
    assert summary["total_debits"] == pytest.approx(stated["total_debits"], abs=0.005)
    assert summary["total_credits"] == pytest.approx(stated["total_credits"], abs=0.005)
    assert summary["closing_balance"] == pytest.approx(stated["closing_balance"], abs=0.005)
    assert summary["validation_passed"] is True
    assert parsed["account_summary"]["current_balance"]["date"] == stated["period_end"]


def test_c3_transaction_types_and_year_rollover_are_parsed():
    txs = _parse_fixture_pdf()["statements"][0]["transactions"]
    assert {t["transaction_type"] for t in txs} == {"income", "expense", "transfer"}
    # "28 déc" on a statement ending in January must roll back a year.
    december = [t for t in txs if t["operation_date"].startswith("2024-12")]
    assert len(december) == 1, "expected exactly one prior-year (December) transaction"
    # Signed-amount invariant: amount = credit - debit.
    for t in txs:
        assert t["amount"] == pytest.approx(t["credit"] - t["debit"], abs=0.005)


# ── Criteria 4 + 5: preview/commit through Flask, then normal queries ──

@pytest.fixture
def isolated_demo_profile(monkeypatch, tmp_path):
    """Point the whole app at a throwaway profile so the import never
    touches the real var/demo/ or personal/."""
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("MINT_PROFILE_DIR", str(profile))
    return profile


@pytest.fixture
def client():
    from app.web.flask_ui import create_ui_app

    app = create_ui_app()
    app.testing = True
    return app.test_client()


@pytest.fixture
def isolated_upload_dir(monkeypatch, tmp_path):
    """The preview route saves uploads to ./uploads, resolved against the
    process CWD -- and in a real checkout that directory holds the user's
    own uploaded statements. Run the test from a tmp_path instead, so the
    upload lands in a throwaway directory and can neither overwrite nor
    leave anything behind in the real one.

    Everything else the request touches is addressed absolutely (the
    profile via MINT_PROFILE_DIR, the fixture via an absolute path,
    templates via the Flask package), so changing CWD is safe here.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir / "uploads"


def test_c4_c5_preview_commit_and_query_end_to_end(
    client, isolated_demo_profile, isolated_upload_dir
):
    pdf_bytes = FIXTURE_PDF.read_bytes()

    preview = client.post(
        "/api/import/preview",
        data={
            "account_side": "asset",
            "account_class": "cash",
            "importer_key": IMPORTER_KEY,
            "institution": "BMO",
            "file": (io.BytesIO(pdf_bytes), UPLOAD_FILENAME),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200, preview.get_data(as_text=True)
    body = preview.get_json()
    assert body["ok"] is True, body
    assert body["is_nav_import"] is False
    assert body["already_imported"] is False
    assert body["account_summary"]["account_class"] == "cash"
    preview_id = body["preview_id"]

    commit = client.post("/api/import/commit", json={"preview_id": preview_id})
    assert commit.status_code == 200, commit.get_data(as_text=True)
    commit_body = commit.get_json()
    assert commit_body["ok"] is True, commit_body
    expected_txs = json.loads(FIXTURE_EXPECTED.read_text(encoding="utf-8"))[
        "statements"
    ][0]["transactions"]
    assert commit_body["result"]["written"] == len(expected_txs)

    # Criterion 5: the committed transactions come back through the normal
    # query path, not by reading the parquet directly. all_transactions
    # deliberately excludes transfers (they have their own query), so the
    # split is asserted explicitly -- that also proves the importer's
    # transfer classification survived all the way to the query layer.
    expected_transfers = [t for t in expected_txs if t["transaction_type"] == "transfer"]
    expected_regular = [t for t in expected_txs if t["transaction_type"] != "transfer"]
    assert expected_transfers and expected_regular, "fixture should cover both"

    parquet_root = config.load_settings()["consolidated_statements"]

    df = q.run_query("all_transactions", parquet_root)
    assert len(df) == len(expected_regular)
    assert any("DEMO EMPLOYEUR TEST" in d for d in df["description"].astype(str))

    transfers = q.run_query("all_transfers", parquet_root)
    assert len(transfers) == len(expected_transfers)
    assert "Virement en ligne, TF 123" in set(transfers["description"].astype(str))

    # And the demo profile is where it landed -- nothing escaped into the
    # real repo profiles, and the upload stayed in the throwaway CWD.
    assert str(isolated_demo_profile) in str(Path(parquet_root))
    assert (isolated_upload_dir / UPLOAD_FILENAME).exists()
    assert not (Path(config.REPO_ROOT) / "uploads" / UPLOAD_FILENAME).exists()


def test_c4_commit_is_rejected_for_an_unknown_preview_id(client, isolated_demo_profile):
    resp = client.post("/api/import/commit", json={"preview_id": "does-not-exist"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ── Criterion 6: generation is deterministic ──────────────────────────

def test_c6_generation_is_byte_deterministic(tmp_path):
    hashes = []
    for i in range(3):
        out = tmp_path / f"run{i}.pdf"
        gen.build_bmo_chequing_pdf(out)
        hashes.append(hashlib.sha256(out.read_bytes()).hexdigest())
    assert len(set(hashes)) == 1, "PDF generation is not byte-deterministic"


def test_c6_tracked_fixture_matches_a_fresh_regeneration(tmp_path):
    """The committed PDF must be exactly what the generator produces today,
    so the fixture can never silently drift from its source."""
    out = tmp_path / PDF_NAME
    gen.build_bmo_chequing_pdf(out)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(
        FIXTURE_PDF.read_bytes()
    ).hexdigest(), (
        "tests/importers/fixtures/pdf/ is stale -- re-run "
        "`venv/bin/python scripts/generate_demo_pdfs.py --update-expected`"
    )


# ── Criterion 7: no private pdf_statements/ path is used ──────────────

def test_c7_no_private_statement_path_is_referenced():
    # The generator and the reviewed golden fixture must be free of any
    # reference to the author's private statement archive. (This test file
    # itself necessarily names the directory it is asserting about, so it
    # is not part of the scan.)
    private_dir = "pdf" + "_statements"
    for path in (Path(gen.__file__), FIXTURE_EXPECTED):
        source = path.read_text(encoding="utf-8")
        assert private_dir not in source, f"{path} references a private path"

    # The fixture the whole milestone depends on lives in the tracked
    # fixture directory, not anywhere private.
    assert "tests/importers/fixtures/pdf" in str(FIXTURE_PDF).replace("\\", "/")
