"""Public synthetic-PDF coverage for every non-chequing PDF importer.

The fixtures are generated from invented data and exercise the registered
production importers without fixture-only parser branches. The original BMO
chequing prototype remains covered in ``test_synthetic_pdf_import.py``.
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


PDF_NAMES = tuple(name for name in gen.GENERATORS if name != "bmo_chequing_fr_demo.pdf")


def _pdf_path(name: str) -> Path:
    return gen.PDF_FIXTURE_DIR / name


def _parse(name: str) -> dict:
    importer = importer_by_key(gen.IMPORTER_KEYS[name])
    assert importer is not None
    return importer.parse_to_json(_pdf_path(name))


def _transactions(parsed: dict) -> list[dict]:
    return [
        transaction
        for statement in parsed.get("statements", [])
        for transaction in statement.get("transactions", [])
    ]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("MINT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("MINT_DATA_DIR", raising=False)

    # Imports must stay deterministic and offline. FX prefetch is non-critical
    # to ingestion, so the test replaces only that external boundary.
    from app.services.fx import fetch_yfinance

    monkeypatch.setattr(fetch_yfinance, "ensure_fx_rate", lambda **_kwargs: None)


@pytest.fixture
def client():
    from app.web.flask_ui import create_ui_app

    app = create_ui_app()
    app.testing = True
    return app.test_client()


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_generated_fixture_is_a_small_valid_pdf(pdf_name, tmp_path):
    tracked = _pdf_path(pdf_name)
    generated = tmp_path / pdf_name
    gen.GENERATORS[pdf_name](generated)

    assert tracked.exists()
    assert tracked.stat().st_size < 200_000
    assert generated.read_bytes().startswith(b"%PDF-")

    import pdfplumber

    with pdfplumber.open(generated) as pdf:
        assert pdf.pages
        assert "DOCUMENT SYNTHETIQUE" in (pdf.pages[0].extract_text() or "")


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_registered_importer_detects_generated_fixture(pdf_name):
    importer = importer_by_key(gen.IMPORTER_KEYS[pdf_name])
    assert importer is not None
    assert importer.detect(_pdf_path(pdf_name)) is True


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_parse_matches_reviewed_expected_fixture(pdf_name):
    parsed = _parse(pdf_name)
    expected_path = gen.expected_json_path(_pdf_path(pdf_name))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    assert expected.pop("export_date") == gen.EXPORT_DATE_PLACEHOLDER
    parse_date = parsed.pop("export_date")
    assert len(parse_date) == 10 and parse_date.count("-") == 2
    assert parsed == expected


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_parse_reflects_values_stated_by_the_generator(pdf_name, tmp_path):
    stated = gen.GENERATORS[pdf_name](tmp_path / pdf_name)
    parsed = _parse(pdf_name)
    account = parsed["account_summary"]

    assert parsed["validation_passed"] is True
    if "account_id" in stated:
        assert account["account_id"] == stated["account_id"]
    if "account_name" in stated:
        assert account["account_name"] == stated["account_name"]

    if stated["kind"] == "nav":
        assert parsed["nav_snapshots"] == [
            {"date": stated["snapshot_date"], "nav": stated["nav"]}
        ]
        return

    txs = _transactions(parsed)
    assert len(txs) == stated["transaction_count"]
    assert sum(tx["transaction_type"] == "transfer" for tx in txs) == stated[
        "transfer_count"
    ]
    assert sum(tx["transaction_type"] != "transfer" for tx in txs) == stated[
        "regular_count"
    ]
    assert account["current_balance"] == {
        "date": stated["period_end"],
        "amount": stated["closing_balance"],
    }


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_preview_commit_and_normal_query_end_to_end(
    pdf_name, client, monkeypatch, tmp_path
):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("MINT_PROFILE_DIR", str(profile))

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    importer = importer_by_key(gen.IMPORTER_KEYS[pdf_name])
    assert importer is not None
    source = _pdf_path(pdf_name)
    upload_name = f"public-{pdf_name}"

    preview = client.post(
        "/api/import/preview",
        data={
            "account_side": importer.account_side,
            "account_class": importer.account_class,
            "importer_key": importer.key,
            "institution": importer.institution_default,
            "file": (io.BytesIO(source.read_bytes()), upload_name),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200, preview.get_data(as_text=True)
    preview_body = preview.get_json()
    assert preview_body["ok"] is True
    assert preview_body["already_imported"] is False

    commit = client.post(
        "/api/import/commit", json={"preview_id": preview_body["preview_id"]}
    )
    assert commit.status_code == 200, commit.get_data(as_text=True)
    result = commit.get_json()["result"]

    parsed = json.loads(gen.expected_json_path(source).read_text(encoding="utf-8"))
    parquet_root = config.load_settings()["consolidated_statements"]
    assert str(profile) in str(Path(parquet_root))
    assert (workdir / "uploads" / upload_name).exists()
    assert not (Path(config.REPO_ROOT) / "uploads" / upload_name).exists()

    if parsed.get("nav_snapshots"):
        assert preview_body["is_nav_import"] is True
        assert result["nav_written"] == len(parsed["nav_snapshots"])
        accounts = q.run_query("net_worth_accounts_snapshot", parquet_root)
        row = accounts.loc[
            accounts["account_id"].astype(str)
            == str(parsed["account_summary"]["account_id"])
        ]
        assert len(row) == 1
        assert float(row.iloc[0]["balance"]) == pytest.approx(
            parsed["nav_snapshots"][0]["nav"]
        )
    else:
        assert preview_body["is_nav_import"] is False
        txs = _transactions(parsed)
        assert result["written"] == len(txs)
        regular = q.run_query("all_transactions", parquet_root)
        transfers = q.run_query("all_transfers", parquet_root)
        assert len(regular) == sum(
            tx["transaction_type"] != "transfer" for tx in txs
        )
        assert len(transfers) == sum(
            tx["transaction_type"] == "transfer" for tx in txs
        )


@pytest.mark.parametrize("pdf_name", PDF_NAMES)
def test_generation_is_byte_deterministic_and_fixture_is_current(pdf_name, tmp_path):
    hashes = []
    for index in range(2):
        generated = tmp_path / f"{index}-{pdf_name}"
        gen.GENERATORS[pdf_name](generated)
        hashes.append(hashlib.sha256(generated.read_bytes()).hexdigest())

    tracked_hash = hashlib.sha256(_pdf_path(pdf_name).read_bytes()).hexdigest()
    assert hashes[0] == hashes[1] == tracked_hash


def test_importers_have_no_fixture_specific_logic():
    importer_root = Path(config.REPO_ROOT) / "app" / "services" / "importers"
    fixture_stems = [Path(name).stem for name in PDF_NAMES]
    forbidden = fixture_stems + ["fixtures/pdf", "fixtures\\pdf"]
    offenders = []
    for source_path in importer_root.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8", errors="ignore")
        for needle in forbidden:
            if needle in source:
                offenders.append(
                    f"{source_path.relative_to(config.REPO_ROOT)}: {needle}"
                )
    assert not offenders, f"fixture-specific logic found in importers: {offenders}"


def test_public_fixture_sources_do_not_reference_private_statement_paths():
    private_dir = "pdf" + "_statements"
    paths = [Path(gen.__file__)] + [
        gen.expected_json_path(_pdf_path(name)) for name in PDF_NAMES
    ]
    for path in paths:
        assert private_dir not in path.read_text(encoding="utf-8")
