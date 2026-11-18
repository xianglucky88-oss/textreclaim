"""Stable keys for idempotent storage.

Legacy records keep their historical key semantics (the ``id`` field, else a
hash of the record).  Documents get a key that binds together:

* the document's own identity,
* the rule-set fingerprint (so a rule change cannot overwrite history),
* the source mapping (page hashes and length).

The last point is what makes a re-run with identical input idempotent while a
re-collection with different bytes becomes a new revision instead of silently
replacing the old one.
"""
from __future__ import annotations

import hashlib
import json

from .config import rules_fingerprint, rules_for

DOC_KEY_PREFIX = "doc:"


def key_of(record):
    """Key for a legacy record (unchanged historical behaviour)."""
    if isinstance(record, dict) and record.get("kind") == "document":
        return document_key(record)
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def source_signature(record: dict) -> str:
    """Hash of the source mapping: page hashes, byte lengths and positions."""
    pages = []
    for page in record.get("pages") or []:
        pages.append(
            {
                "page": page.get("page_index"),
                "raw_sha256": page.get("raw_hash"),
                "byte_length": page.get("byte_length"),
            }
        )
    payload = json.dumps(pages, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def document_key(record: dict, rules: dict | None = None) -> str:
    rules = rules or rules_for(record.get("rules_version"))
    identity = {
        "id": record.get("id") or record.get("url") or record.get("source_id"),
        "source": source_signature(record),
        "rules": rules_fingerprint(rules),
        "version": rules.get("version"),
    }
    payload = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{DOC_KEY_PREFIX}{digest}"


def revision_of(record: dict, rules: dict | None = None) -> dict:
    rules = rules or rules_for(record.get("rules_version"))
    return {
        "key": document_key(record, rules),
        "source_signature": source_signature(record),
        "rules_version": rules.get("version"),
        "rules_fingerprint": rules_fingerprint(rules),
        "document_id": record.get("id"),
    }
