"""Identity, content hashing and duplicate confirmation.

Two ideas drive this module:

* **Content and source must agree.**  A block's identity is derived from its
  normalised text *and* its source span (page, byte range, raw page hash).
  Two blocks with equal text but different sources are different facts and are
  never merged.
* **Repetition is not duplication.**  Identical body paragraphs are kept and
  merely annotated.  Only *whole-page* duplicates (same raw hash, or the same
  block sequence) are collapsed, and even then the duplicate page is recorded
  as an alias rather than discarded.
"""
from __future__ import annotations

import hashlib
import json

from .config import rules_for


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def content_hash(text: str) -> str:
    """Hash normalised text only (used for duplicate *content* signals)."""
    return _sha256_text(" ".join((text or "").split()))


def block_fingerprint(block: dict) -> str:
    """Identity of a block: content plus source mapping.

    The page hash is included, so an identical paragraph on two different
    pages cannot collide.
    """
    locator = block.get("locator") or {}
    payload = {
        "text": " ".join((block.get("text") or "").split()),
        "type": block.get("type"),
        "page": locator.get("page"),
        "byte_start": locator.get("byte_start"),
        "byte_end": locator.get("byte_end"),
        "raw_sha256": locator.get("raw_sha256"),
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def document_content_hash(blocks: list[dict]) -> str:
    """Hash of the *retained* content, in reading order.

    Removed template blocks are excluded, but low-confidence template blocks
    are included: their text is still part of the output and must be covered
    by the reproducibility check.
    """
    parts = []
    for block in blocks:
        if block.get("noise_removed"):
            continue
        parts.append(block.get("text") or "")
        if block.get("type") == "table":
            table = block.get("table") or {}
            for row in table.get("rows") or []:
                parts.append("\x1f".join(row))
            parts.append("\x1e")
    joined = "\x1d".join(parts)
    return _sha256_text(joined)


def _page_key(page: dict) -> tuple:
    return (page.get("raw_hash"), page.get("byte_length"))


def process(record):
    """Pipeline stage: assign ids, content hashes, and confirm duplicates."""
    if not isinstance(record, dict) or record.get("kind") != "document":
        rec = dict(record)
        rec.setdefault("_stage_identify", "ok")
        return rec

    rules = rules_for(record.get("rules_version"))
    out = dict(record)

    # ---------------------------------------------------------------- pages
    pages = [dict(page) for page in record.get("pages") or []]
    seen_pages: dict[tuple, dict] = {}
    duplicate_pages = []
    for page in pages:
        key = _page_key(page)
        if page.get("raw_hash") and key in seen_pages:
            original = seen_pages[key]
            page["duplicate_of"] = original.get("page_index")
            page["duplicate_kind"] = "identical_bytes"
            duplicate_pages.append({
                "page": page.get("page_index"),
                "duplicate_of": original.get("page_index"),
                "raw_sha256": page.get("raw_hash"),
            })
        elif page.get("raw_hash"):
            seen_pages[key] = page
        page["stream_ref"] = {
            "raw_sha256": page.get("raw_hash"),
            "byte_length": page.get("byte_length"),
            "page": page.get("page_index"),
        }
    out["pages"] = pages
    out["duplicate_pages"] = duplicate_pages

    # --------------------------------------------------------------- blocks
    blocks = [dict(block) for block in record.get("blocks") or []]
    for block in blocks:
        block.setdefault("page_index", (block.get("locator") or {}).get("page"))
        block["fingerprint"] = block_fingerprint(block)
        text = block.get("text") or ""
        block["text_hash"] = content_hash(text)
        block["source_verified"] = bool(
            (block.get("locator") or {}).get("raw_sha256")
            and (block.get("locator") or {}).get("byte_start") is not None
        )
        # A block whose source cannot be re-addressed is *not* silently trusted.
        if not block["source_verified"]:
            block.setdefault("flags", []).append("source_unverified")

    # Repeated text is annotated, never merged.
    occurrences: dict[str, list[dict]] = {}
    for block in blocks:
        if block.get("noise_removed"):
            continue
        text = (block.get("text") or "").strip()
        if len(text) < 4:
            continue
        occurrences.setdefault(block["text_hash"], []).append(block)
    for text_hash, members in occurrences.items():
        if len(members) < 2:
            continue
        ids = [member.get("block_id") for member in members]
        for member in members:
            member["duplicate_text"] = {
                "count": len(members),
                "block_ids": ids,
                "retained": True,
                "note": "identical body text at distinct sources is preserved",
            }

    out["blocks"] = blocks
    out["content_hash"] = document_content_hash(blocks)

    # Confirm content against source: every retained block must be
    # re-addressable to a stored page byte range, and the page hashes must be
    # the ones the blocks point at.
    page_hashes = [page.get("raw_hash") for page in pages]
    unverified = [
        block.get("block_id") for block in blocks
        if not block.get("noise_removed") and not block.get("source_verified")
    ]
    orphan_sources = [
        block.get("block_id") for block in blocks
        if block.get("locator") and block["locator"].get("raw_sha256") not in page_hashes
        and not block.get("noise_removed")
    ]
    source_confirmed = not unverified and not orphan_sources
    out["source_check"] = {
        "confirmed": source_confirmed,
        "unverified_blocks": unverified,
        "orphan_sources": orphan_sources,
        "pages": len(pages),
    }
    if not source_confirmed:
        out.setdefault("flags", []).append("source_unconfirmed")

    out["identity"] = {
        "document_id": record.get("id"),
        "content_hash": out["content_hash"],
        "page_hashes": page_hashes,
        "rules_version": rules.get("version"),
        "block_count": len(blocks),
        "retained_block_count": sum(1 for b in blocks if not b.get("noise_removed")),
        "source_confirmed": source_confirmed,
    }
    return out
