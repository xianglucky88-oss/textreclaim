"""SQLite persistence with transactional, resumable ingestion.

Design points that matter for crash safety:

* One document is written inside a single transaction.  ``pending`` marks rows
  whose bytes are already staged but whose transaction has not committed.  A
  reader (and the resume logic) only treats a row as a success when
  ``status='complete'``.
* On the next start, ``recover_incomplete`` removes any row that was staged but
  never confirmed.  A success can therefore never exist without its source
  mapping having been committed in the same transaction.
* ``document_sources`` stores the raw page bytes keyed by their hash.  The
  content and the source mapping are committed together, so both are present
  or neither is.  This is what makes "content and source jointly confirmed"
  checkable after the fact.
"""
from __future__ import annotations

import json
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
    key TEXT PRIMARY KEY,
    document_id TEXT,
    rules_version TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    source_signature TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS documents_doc ON documents(document_id);
CREATE TABLE IF NOT EXISTS document_sources(
    key TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw BLOB,
    byte_length INTEGER NOT NULL,
    PRIMARY KEY(key, page_index)
);
CREATE TABLE IF NOT EXISTS document_blocks(
    key TEXT NOT NULL,
    block_id TEXT NOT NULL,
    block_order INTEGER,
    block_type TEXT,
    page_index INTEGER,
    byte_start INTEGER,
    byte_end INTEGER,
    raw_sha256 TEXT,
    text_hash TEXT,
    noise_class TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY(key, block_id)
);
CREATE INDEX IF NOT EXISTS blocks_source ON document_blocks(raw_sha256, byte_start, byte_end);
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    rules_version TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary TEXT
);
"""


def connect(path=None):
    con = sqlite3.connect(str(path or DB_PATH))
    # Autocommit mode: transactions are explicit, which makes the atomic
    # document write below unambiguous.
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(con, record):
    """Legacy upsert (records table)."""
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False)),
    )


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def recover_incomplete(con, commit: bool = True) -> dict:
    """Discard staged rows that were never confirmed.

    Only *staged* rows are removed; a committed success is never touched.
    """
    removed_docs = con.execute(
        "DELETE FROM documents WHERE status != 'complete'"
    ).rowcount
    con.execute(
        "DELETE FROM document_blocks WHERE key NOT IN (SELECT key FROM documents)"
    )
    con.execute(
        "DELETE FROM document_sources WHERE key NOT IN (SELECT key FROM documents)"
    )
    if removed_docs and commit:
        con.commit()
    return {"discarded_incomplete": removed_docs}


def begin_document(con, revision: dict, data: dict) -> None:
    con.execute(
        "INSERT INTO documents(key,document_id,rules_version,rules_fingerprint,"
        "source_signature,content_hash,status,created_at,data) "
        "VALUES(?,?,?,?,?,?,'pending',?,?) "
        "ON CONFLICT(key) DO UPDATE SET status='pending', "
        "data=excluded.data, content_hash=excluded.content_hash",
        (
            revision["key"],
            revision.get("document_id"),
            revision["rules_version"],
            revision["rules_fingerprint"],
            revision["source_signature"],
            data.get("content_hash") or "",
            _now(),
            json.dumps(data, ensure_ascii=False),
        ),
    )


def stage_sources(con, key: str, pages: list[dict]) -> None:
    # Replace the *whole* source set: a shorter re-collection must not leave
    # stale pages behind that no longer match the stored content.
    con.execute("DELETE FROM document_sources WHERE key=?", (key,))
    for page in pages:
        raw = page.get("raw") or b""
        if not isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)
        con.execute(
            "INSERT OR REPLACE INTO document_sources(key,page_index,raw_sha256,raw,byte_length) "
            "VALUES(?,?,?,?,?)",
            (
                key,
                page.get("page_index"),
                page.get("raw_hash") or "",
                bytes(raw),
                len(raw),
            ),
        )


def stage_blocks(con, key: str, blocks: list[dict]) -> None:
    con.execute("DELETE FROM document_blocks WHERE key=?", (key,))
    for block in blocks:
        locator = block.get("locator") or {}
        con.execute(
            "INSERT OR REPLACE INTO document_blocks(key,block_id,block_order,block_type,"
            "page_index,byte_start,byte_end,raw_sha256,text_hash,noise_class,data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                block.get("block_id"),
                block.get("order"),
                block.get("type"),
                locator.get("page"),
                locator.get("byte_start"),
                locator.get("byte_end"),
                locator.get("raw_sha256"),
                block.get("text_hash"),
                block.get("noise_class"),
                json.dumps(block, ensure_ascii=False),
            ),
        )


def complete_document(con, key: str, data: dict, revision: dict) -> None:
    con.execute(
        "UPDATE documents SET status='complete', completed_at=?, data=? WHERE key=?",
        (_now(), json.dumps(data, ensure_ascii=False), key),
    )


def save_document(con, document: dict, revision: dict, sources: list[dict] | None = None) -> None:
    """Write one document atomically.

    Either the document row is ``complete`` with its sources and blocks, or the
    transaction is rolled back and nothing is visible.  ``sources`` must carry
    the raw page bytes; they are committed in the same transaction as the
    content that references them.
    """
    key = revision["key"]
    try:
        con.execute("BEGIN IMMEDIATE")
        begin_document(con, revision, document)
        stage_sources(con, key, sources if sources is not None else document.get("pages") or [])
        stage_blocks(con, key, document.get("blocks") or [])
        complete_document(con, key, document, revision)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def iter_documents(con):
    for key, data in con.execute("SELECT key,data FROM documents WHERE status='complete'"):
        yield key, json.loads(data)


def get_document(con, key: str) -> dict | None:
    row = con.execute(
        "SELECT data,status FROM documents WHERE key=?", (key,)
    ).fetchone()
    if not row:
        return None
    data, status = row
    if status != "complete":
        return None
    return json.loads(data)


def get_source_bytes(con, key: str, page_index: int) -> bytes | None:
    row = con.execute(
        "SELECT raw FROM document_sources WHERE key=? AND page_index=?",
        (key, page_index),
    ).fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


def block_at(con, raw_sha256: str, byte_start: int, byte_end: int):
    """Find the blocks whose source span overlaps a byte range."""
    return con.execute(
        "SELECT key,block_id,data FROM document_blocks "
        "WHERE raw_sha256=? AND byte_start < ? AND byte_end > ?",
        (raw_sha256, byte_end, byte_start),
    ).fetchall()


def record_run(con, run_id: str, rules: dict, rules_fingerprint: str,
               status: str, summary: dict, started_at: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO runs(run_id,rules_version,rules_fingerprint,started_at,"
        "finished_at,status,summary) VALUES(?,?,?,?,?,?,?)",
        (
            run_id,
            rules.get("version"),
            rules_fingerprint,
            started_at,
            _now(),
            status,
            json.dumps(summary, ensure_ascii=False),
        ),
    )
