"""Thin orchestration.  No business judgement lives here.

The pipeline dispatches between two input shapes:

* legacy records (the original ``fixtures/input.json``), which keep the exact
  historical behaviour and storage key semantics;
* ``kind: document`` records, which run through
  ``encoding → extract → normalize → clean → identify`` and are persisted
  atomically with their source bytes.

Every stage is wrapped so that a failure on one document is recorded and the
remaining documents still run.  ``on_error: raise`` in the rule set switches
to fail-fast for tests and debugging.
"""
from __future__ import annotations

import importlib
import json
import uuid

from . import store as _store
from . import loader as _loader
from .loader import iter_records
from .config import DEFAULT_RULES_VERSION, rules_fingerprint, rules_for
from .keyer import key_of, revision_of
from .models import Record

LEGACY_CHAIN = ["normalize", "identify", "clean"]
DOCUMENT_CHAIN = ["encoding", "extract", "normalize", "clean", "identify"]

# Backwards-compatible name for the historical chain.
CHAIN_NAMES = LEGACY_CHAIN

# Page fields that are either non-JSON (bytes / mapper) or already persisted
# separately in ``document_sources``.
_VOLATILE_PAGE_KEYS = {"_byte_map", "raw"}


def _stage_functions(names):
    chain = []
    for name in names:
        module = importlib.import_module("." + name, __package__)
        chain.append((name, module.process))
    return chain


def _is_document(record) -> bool:
    return isinstance(record, dict) and record.get("kind") == "document"


def _jsonable_page(page: dict) -> dict:
    return {key: value for key, value in page.items() if key not in _VOLATILE_PAGE_KEYS}


def run(con=None, documents=None, rules_version: str | None = None):
    """Run the pipeline.

    ``documents`` may be supplied directly (list of dicts); otherwise the
    loader is used.  Legacy and document records can be mixed in one input.
    """
    own = con is None
    con = con or _store.connect()
    started_at = _now()
    run_id = uuid.uuid4().hex
    run_rules = rules_for(rules_version)
    fingerprint = rules_fingerprint(run_rules)
    summary = {
        "fed": 0,
        "legacy_rows": 0,
        "documents": 0,
        "documents_failed": 0,
        "discarded_incomplete": 0,
        "errors": [],
    }
    try:
        _store.record_run(con, run_id, run_rules, fingerprint, "running", summary, started_at)
        con.commit()

        recovered = _store.recover_incomplete(con)
        summary["discarded_incomplete"] = recovered["discarded_incomplete"]
        con.commit()

        legacy_chain = _stage_functions(LEGACY_CHAIN)
        document_chain = _stage_functions(DOCUMENT_CHAIN)

        items = documents if documents is not None else _loader.iter_records()
        try:
            for raw in items:
                if not isinstance(raw, dict):
                    summary["errors"].append({"document": None, "error": "record is not an object"})
                    continue
                summary["fed"] += 1
                if _is_document(raw):
                    _run_document(con, raw, document_chain, run_rules, summary,
                                  force_rules=rules_version is not None)
                else:
                    rec = raw
                    for name, fn in legacy_chain:
                        rec = fn(rec)
                    _store.upsert(con, Record(key=key_of(rec), data=rec))
                    summary["legacy_rows"] += 1
        except Exception as exc:
            # The input stream itself failed (e.g. a truncated JSON array).
            # Committed documents stay committed; the run is marked failed.
            summary.setdefault("input_error", f"{type(exc).__name__}: {exc}")
            _store.record_run(con, run_id, run_rules, fingerprint, "failed", summary, started_at)
            _store.commit(con)
            raise

        _store.commit(con)
        result = {
            "fed": summary["fed"],
            "rows": _store.count(con),
            "documents": summary["documents"],
            "documents_failed": summary["documents_failed"],
            "discarded_incomplete": summary["discarded_incomplete"],
            "errors": summary["errors"],
        }
        _store.record_run(con, run_id, run_rules, fingerprint, "complete", result, started_at)
        _store.commit(con)
        return result
    except Exception as exc:
        try:
            summary["fatal"] = f"{type(exc).__name__}: {exc}"
            _store.record_run(con, run_id, run_rules, fingerprint, "failed", summary, started_at)
            _store.commit(con)
        except Exception:  # pragma: no cover - preserve the original error
            pass
        raise
    finally:
        if own:
            con.close()


def _run_document(con, raw, chain, run_rules, summary, force_rules: bool = False) -> None:
    record = dict(raw)
    if force_rules:
        # An explicit run-level rule version wins over the document's own.
        record["rules_version"] = run_rules["version"]
    else:
        record.setdefault("rules_version", run_rules["version"])
    rules = rules_for(record["rules_version"])
    fail_fast = rules.get("on_error", "record") == "raise"
    try:
        for name, fn in chain:
            record = fn(record)
        revision = revision_of(record, rules)
        document = {
            "id": record.get("id"),
            "kind": "document",
            "rules_version": rules["version"],
            "rules_fingerprint": revision["rules_fingerprint"],
            "source_signature": revision["source_signature"],
            "content_hash": record.get("content_hash"),
            "identity": record.get("identity"),
            "pages": [_jsonable_page(page) for page in record.get("pages") or []],
            "blocks": record.get("blocks") or [],
            "tables": record.get("tables") or [],
            "footnotes": record.get("footnotes") or {},
            "removed": record.get("removed") or [],
            "skipped": record.get("skipped") or [],
            "recovered": record.get("recovered") or [],
            "duplicate_pages": record.get("duplicate_pages") or [],
            "pages_meta": record.get("pages_meta") or [],
            "noise_stats": record.get("noise_stats") or {},
            "source_check": record.get("source_check") or {},
        }
        # Content and its source mapping are written in one transaction.
        _store.save_document(con, document, revision,
                             sources=record.get("pages") or [])
        summary["documents"] += 1
    except Exception as exc:
        summary["documents_failed"] += 1
        summary["errors"].append({
            "document": raw.get("id"),
            "error": f"{type(exc).__name__}: {exc}",
        })
        if fail_fast:
            raise


# ---------------------------------------------------------------------------
# Reproducibility helpers
# ---------------------------------------------------------------------------


def rerun(key: str, con=None):
    """Re-process a stored document with the rules version it was produced by.

    This is the supported "replay history" path: the rule set recorded
    alongside the original result is pinned instead of today's default, and
    the original page bytes are reloaded from ``document_sources``.  The
    resulting content hash can therefore be compared with the stored one.
    """
    own = con is None
    con = con or _store.connect()
    try:
        row = con.execute(
            "SELECT data,rules_version,status FROM documents WHERE key=?", (key,)
        ).fetchone()
        if row is None or row[2] != "complete":
            raise KeyError(f"no complete document stored for key {key!r}")
        stored = json.loads(row[0])
        version = row[1]
        pages = []
        for page in stored.get("pages") or []:
            page = dict(page)
            page["raw"] = _store.get_source_bytes(con, key, page.get("page_index"))
            pages.append(page)
        document = {
            "id": stored.get("id"),
            "kind": "document",
            "pages": pages,
            "rules_version": version,
        }
        result = run(con=con, documents=[document], rules_version=version)
        revision = revision_of(document, rules_for(version))
        result["key"] = revision["key"]
        result["rules_version"] = version
        result["original_key"] = key
        result["reproduced"] = revision["key"] == key
        result["original_content_hash"] = stored.get("content_hash")
        again = _store.get_document(con, revision["key"])
        result["content_hash"] = (again or {}).get("content_hash")
        result["content_matches"] = result["content_hash"] == result["original_content_hash"]
        return result
    finally:
        if own:
            con.close()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
