from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from app import config
from app.services import normalize_parquet as normalizer


def _transaction(description: str = "REPEATED PURCHASE") -> dict:
    return {
        "operation_date": "2026-01-10",
        "description": description,
        "amount": -12.34,
        "debit": 12.34,
        "credit": 0.0,
        "transaction_type": "expense",
    }


def _importer_json(transactions: list[dict]) -> dict:
    return {
        "export_date": "2026-01-31T00:00:00+00:00",
        "document_signature": {"document_type": "SYNTHETIC_STATEMENT"},
        "account_summary": {
            "account_id": "synthetic-account",
            "account_name": "Synthetic account",
            "current_balance": {"date": "2026-01-31", "amount": 100.0},
        },
        "statements": [
            {
                "summary": {
                    "statement_date": "2026-01-31",
                    "validation_passed": True,
                },
                "transactions": transactions,
            }
        ],
    }


def _normalize(payload: dict, source_pdf) -> pd.DataFrame:
    transactions, _, _, _ = normalizer.normalize_importer_json(
        payload,
        institution="Synthetic Bank",
        account_side="liability",
        account_class="credit_card",
        source_pdf=source_pdf,
    )
    return transactions


def test_repeated_transactions_get_stable_occurrence_ids_without_changing_legacy_ids(
    tmp_path,
):
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    repeated = _transaction()
    other = _transaction("OTHER PURCHASE")

    legacy = _normalize(
        _importer_json([deepcopy(repeated), deepcopy(other)]),
        source_pdf,
    )
    with_repetitions = _normalize(
        _importer_json(
            [deepcopy(repeated), deepcopy(repeated), deepcopy(repeated), deepcopy(other)]
        ),
        source_pdf,
    )

    legacy_repeated_id = legacy.loc[
        legacy["description"] == repeated["description"], "transaction_id"
    ].item()
    legacy_other_id = legacy.loc[
        legacy["description"] == other["description"], "transaction_id"
    ].item()
    repeated_ids = with_repetitions.loc[
        with_repetitions["description"] == repeated["description"], "transaction_id"
    ].tolist()

    assert repeated_ids == [
        legacy_repeated_id,
        normalizer._stable16(f"{legacy_repeated_id}|occurrence|1"),
        normalizer._stable16(f"{legacy_repeated_id}|occurrence|2"),
    ]
    assert with_repetitions.loc[
        with_repetitions["description"] == other["description"], "transaction_id"
    ].item() == legacy_other_id
    assert with_repetitions["transaction_id"].is_unique

    rerun = _normalize(
        _importer_json(
            [deepcopy(repeated), deepcopy(repeated), deepcopy(repeated), deepcopy(other)]
        ),
        source_pdf,
    )
    assert rerun["transaction_id"].tolist() == with_repetitions[
        "transaction_id"
    ].tolist()


def test_normalization_rejects_any_remaining_transaction_id_collision(
    monkeypatch, tmp_path
):
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    monkeypatch.setattr(normalizer, "_stable16", lambda value: "forced-collision")

    with pytest.raises(normalizer.TransactionReconciliationError) as exc_info:
        _normalize(
            _importer_json([_transaction(), _transaction("OTHER PURCHASE")]),
            source_pdf,
        )

    assert exc_info.value.counts == {
        "extracted": 2,
        "normalized": 2,
        "unique_ids": 1,
        "missing_ids": 0,
    }


def test_reconciliation_rejects_loss_between_extraction_and_normalization(tmp_path):
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    payload = _importer_json([_transaction(), _transaction("OTHER PURCHASE")])
    normalized = _normalize(payload, source_pdf)

    with pytest.raises(normalizer.TransactionReconciliationError) as exc_info:
        normalizer._reconcile_transaction_batch(payload, normalized.iloc[:1])

    assert exc_info.value.counts == {
        "extracted": 2,
        "normalized": 1,
        "unique_ids": 1,
        "missing_ids": 0,
    }


def test_partition_writer_rejects_duplicate_ids_before_writing(tmp_path):
    batch = pd.DataFrame(
        [
            {"operation_date": "2026-01-10", "transaction_id": "duplicate"},
            {"operation_date": "2026-01-10", "transaction_id": "duplicate"},
        ]
    )

    with pytest.raises(ValueError, match="transaction write batch.*duplicate"):
        normalizer.write_partitioned(batch, tmp_path)

    assert not list(tmp_path.rglob("transactions.parquet"))


def test_ingest_preserves_repetitions_after_a_later_partition_write(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "load_settings", lambda: {"display_currency": "CAD"})
    out_dir = tmp_path / "parquet"
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    first_pdf.write_bytes(b"first synthetic statement")
    second_pdf.write_bytes(b"second synthetic statement")

    first = normalizer.ingest_pdf_with_importer_json(
        _importer_json([_transaction(), _transaction()]),
        account_side="liability",
        account_class="credit_card",
        institution="Synthetic Bank",
        source_pdf_path=first_pdf,
        out_dir=out_dir,
    )
    second = normalizer.ingest_pdf_with_importer_json(
        _importer_json([_transaction("OTHER PURCHASE")]),
        account_side="liability",
        account_class="credit_card",
        institution="Synthetic Bank",
        source_pdf_path=second_pdf,
        out_dir=out_dir,
    )

    stored = pd.concat(
        [pd.read_parquet(path) for path in out_dir.rglob("transactions.parquet")],
        ignore_index=True,
    )
    first_source_id = _normalize(
        _importer_json([_transaction(), _transaction()]), first_pdf
    )["source_statement_id"].iloc[0]

    assert first["written"] == 2
    assert second["written"] == 1
    assert len(stored) == 3
    assert stored["transaction_id"].is_unique
    assert int((stored["source_statement_id"] == first_source_id).sum()) == 2


def test_ingest_raises_if_the_post_write_row_count_is_wrong(monkeypatch, tmp_path):
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    monkeypatch.setattr(normalizer, "write_partitioned", lambda df, base: None)

    with pytest.raises(RuntimeError, match="persistence invariant failed"):
        normalizer.ingest_pdf_with_importer_json(
            _importer_json([_transaction(), _transaction()]),
            account_side="liability",
            account_class="credit_card",
            institution="Synthetic Bank",
            source_pdf_path=source_pdf,
            out_dir=tmp_path / "parquet",
        )


def test_failed_reconciliation_cannot_modify_an_existing_statement(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "load_settings", lambda: {"display_currency": "CAD"})
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    out_dir = tmp_path / "parquet"

    normalizer.ingest_pdf_with_importer_json(
        _importer_json([_transaction()]),
        account_side="liability",
        account_class="credit_card",
        institution="Synthetic Bank",
        source_pdf_path=source_pdf,
        out_dir=out_dir,
    )
    before = {
        path.relative_to(out_dir): path.read_bytes()
        for path in out_dir.rglob("*")
        if path.is_file()
    }

    original_stable16 = normalizer._stable16

    def collide_transaction_ids(value: str) -> str:
        if value.startswith("forced-collision|occurrence|") or value.count("|") == 5:
            return "forced-collision"
        return original_stable16(value)

    monkeypatch.setattr(normalizer, "_stable16", collide_transaction_ids)
    overwrite_payload = _importer_json(
        [_transaction(), _transaction("OTHER PURCHASE")]
    )
    overwrite_payload["_overwrite_statement"] = True

    with pytest.raises(normalizer.TransactionReconciliationError):
        normalizer.ingest_pdf_with_importer_json(
            overwrite_payload,
            account_side="liability",
            account_class="credit_card",
            institution="Synthetic Bank",
            source_pdf_path=source_pdf,
            out_dir=out_dir,
        )

    after = {
        path.relative_to(out_dir): path.read_bytes()
        for path in out_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_commit_surfaces_reconciliation_failure_as_422(monkeypatch, tmp_path):
    from app.web import routes_ui
    from app.web.flask_ui import create_ui_app

    preview_id = "reconciliation-test-preview"
    source_pdf = tmp_path / "statement.pdf"
    source_pdf.write_bytes(b"synthetic statement")
    routes_ui._STAGED_IMPORTS[preview_id] = {
        "importer_json": _importer_json([_transaction(), _transaction()]),
        "account_side": "liability",
        "account_class": "credit_card",
        "institution": "Synthetic Bank",
        "source_pdf": source_pdf,
    }

    def reject_import(*args, **kwargs):
        raise normalizer.TransactionReconciliationError(
            extracted=2,
            normalized=2,
            unique_ids=1,
            missing_ids=0,
        )

    monkeypatch.setattr(routes_ui, "ingest_pdf_with_importer_json", reject_import)
    monkeypatch.setattr(routes_ui, "_parquet_root_path", lambda: tmp_path / "parquet")
    app = create_ui_app()
    app.testing = True

    response = app.test_client().post(
        "/api/import/commit",
        json={"preview_id": preview_id},
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "ok": False,
        "code": "transaction_reconciliation_failed",
        "error": "Import blocked: transaction reconciliation failed",
        "counts": {
            "extracted": 2,
            "normalized": 2,
            "unique_ids": 1,
            "missing_ids": 0,
        },
    }
