from pathlib import Path

import pandas as pd

from app.services import queries as q


def test_run_query_dispatches_fx_timeseries_with_root_and_returns_dataframe(
    monkeypatch, tmp_path
):
    parquet_root = tmp_path / "parquet"
    expected = pd.DataFrame(
        [{"month": "2026-01", "assets": 100.0, "liabilities": 25.0, "net_worth": 75.0}]
    )
    diagnostics = {"display_currency": "CAD"}
    captured = {}

    class FakeConnection:
        closed = False

        def close(self):
            self.closed = True

    connection = FakeConnection()

    def fake_build_txn_connection(root: Path):
        captured["build_root"] = root
        return connection

    def fake_net_worth_timeseries_fx(con, root: Path):
        captured["query_connection"] = con
        captured["query_root"] = root
        return expected, diagnostics

    monkeypatch.setattr(q, "build_txn_connection", fake_build_txn_connection)
    monkeypatch.setattr(q, "net_worth_timeseries_fx", fake_net_worth_timeseries_fx)

    result = q.run_query("net_worth_timeseries_fx", str(parquet_root))

    assert result is expected
    assert captured == {
        "build_root": parquet_root.resolve(),
        "query_connection": connection,
        "query_root": parquet_root.resolve(),
    }
    assert connection.closed is True
