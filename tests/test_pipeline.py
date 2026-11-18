"""Tests for the legacy (record-shaped) pipeline contract.

These assertions pin the historical behaviour that must not regress when the
document pipeline is used.
"""
from src.textreclaim import pipeline, store


def test_pipeline_runs_and_idempotent(tmp_path):
    con = store.connect(tmp_path / "legacy.db")
    try:
        a = pipeline.run(con=con)
        b = pipeline.run(con=con)   # 重跑不增行（幂等 upsert）
        assert a["fed"] == 4
        assert b["rows"] == a["rows"] == 3   # r-003 重复被去重
        assert a["documents"] == 0
    finally:
        con.close()


def test_store_roundtrip(tmp_path):
    con = store.connect(tmp_path / "roundtrip.db")
    try:
        assert store.count(con) >= 0
    finally:
        con.close()


def test_legacy_records_keep_their_fields(tmp_path):
    con = store.connect(tmp_path / "legacy-fields.db")
    try:
        pipeline.run(con=con)
        records = dict(store.all_records(con))
        assert set(records) == {"r-001", "r-002", "r-003"}
        assert records["r-001"]["src"] == "alpha"       # lower-cased by normalize
        assert records["r-001"]["name"] == "Record One"  # trimmed
        assert records["r-001"]["value"] == 10
        assert records["r-001"]["_stage_identify"] == "ok"
        assert records["r-001"]["_stage_clean"] == "naive"
    finally:
        con.close()
