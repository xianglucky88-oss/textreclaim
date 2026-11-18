"""Encoding recovery for raw page bytes.

The module resolves the four competing sources of truth for a page's encoding:

1. a byte-order mark (BOM),
2. an HTTP header charset,
3. an in-document ``<meta charset>`` declaration,
4. the actual bytes.

Every decision is recorded as evidence, including conflicts.  Bytes that
cannot be decoded are not silently replaced: their exact byte ranges are
retained on the page and later blocks that overlap them are flagged.

The byte -> character mapping is *sparse*: checkpoints are created while
blocks are located in document order, so a large page does not require an
O(number-of-characters) integer array in memory.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import re
import threading
from bisect import bisect_right

from .config import rules_for

# Longest first: UTF-32-LE starts with the UTF-16-LE BOM.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le", "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be", "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8", "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"),
)

# Labels used by browsers and by the wild.  ``gb2312``/``gbk`` are decoded with
# the gb18030 superset; ``iso-8859-1`` follows the HTML rule and is treated as
# windows-1252.
_LABEL_ALIASES = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
    "us-ascii": "ascii",
    "ascii": "ascii",
    "iso-8859-1": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "windows-1252": "cp1252",
    "cp1252": "cp1252",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "big5": "big5",
    "big5-hkscs": "big5hkscs",
    "shift_jis": "shift_jis",
    "shift-jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-jp": "euc-jp",
    "euc-kr": "euc-kr",
    "utf-16": "utf-16",
    "utf-16le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "utf-32": "utf-32",
}

# Ordered fallbacks.  latin-1/cp1252 can decode any byte, so it is deliberately
# last; it only wins on plausibility, never merely because it never raises.
_FALLBACKS = (
    "utf-8",
    "gb18030",
    "big5",
    "shift_jis",
    "euc-jp",
    "euc-kr",
    "cp1252",
)

_META_CHARSET_RE = re.compile(
    rb"<meta\b[^>]{0,4096}?charset\s*=\s*[\"']?\s*([A-Za-z0-9._:\-]+)", re.IGNORECASE
)
_META_HTTP_EQUIV_RE = re.compile(
    rb"<meta\b[^>]{0,4096}?content\s*=\s*[\"'][^\"']{0,200}?charset\s*=\s*([A-Za-z0-9._:\-]+)",
    re.IGNORECASE,
)
_CONTENT_TYPE_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9._:\-]+)", re.IGNORECASE)
_SNIFF_LIMIT = 8192


def canonical_label(label: str | None) -> str | None:
    """Map a charset label to a Python codec name, or ``None`` if unusable."""
    if not label:
        return None
    text = str(label).strip().strip("\"'").lower()
    if not text:
        return None
    if text in _LABEL_ALIASES:
        return _LABEL_ALIASES[text]
    try:
        info = codecs.lookup(text)
    except LookupError:
        return None
    return info.name


def _bom_of(raw: bytes) -> tuple[str | None, int, str | None]:
    for bom, encoding, label in _BOMS:
        if raw.startswith(bom):
            return encoding, len(bom), label
    return None, 0, None


def _meta_charset(raw: bytes) -> str | None:
    sniff = raw[:_SNIFF_LIMIT]
    match = _META_CHARSET_RE.search(sniff) or _META_HTTP_EQUIV_RE.search(sniff)
    if not match:
        return None
    try:
        return match.group(1).decode("ascii", "ignore")
    except Exception:  # pragma: no cover - defensive
        return None


def _header_charset(page: dict) -> tuple[str | None, str | None]:
    """Return ``(raw_label, source_description)`` from common input shapes."""
    for key in ("header_charset", "http_charset", "content_charset"):
        if page.get(key):
            return str(page[key]), key
    content_type = page.get("content_type") or page.get("content-type")
    headers = page.get("headers")
    if not content_type and isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() in ("content-type", "content_type"):
                content_type = value
                break
    if content_type:
        match = _CONTENT_TYPE_CHARSET_RE.search(str(content_type))
        if match:
            return match.group(1), "content-type"
    return None, None


_DECODE_STATE = threading.local()
_ERROR_HANDLER_NAME = "textreclaim-collect"


def _collecting_handler(exc):  # pragma: no cover - invoked by CPython
    stack = getattr(_DECODE_STATE, "stack", None)
    if not stack:
        raise exc
    errors, base = stack[-1]
    if isinstance(exc, UnicodeDecodeError):
        errors.append((base + exc.start, base + exc.end))
        return ("�", exc.end)
    raise exc


# Registered exactly once for the process; per-decode state lives on a
# thread-local stack, so repeated decodes never grow the codec registry.
codecs.register_error(_ERROR_HANDLER_NAME, _collecting_handler)


def _decode_with_evidence(raw: bytes, start: int, encoding: str) -> tuple[str, list[tuple[int, int]], bool]:
    """Decode *raw[start:]*, recording exact bad byte ranges.

    Every undecodable run becomes one U+FFFD in the text and one byte interval
    in ``errors``; the two stay aligned because a single error handler emits
    exactly one replacement character per run.
    """
    errors: list[tuple[int, int]] = []
    stack = getattr(_DECODE_STATE, "stack", None)
    if stack is None:
        stack = []
        _DECODE_STATE.stack = stack
    stack.append((errors, start))
    try:
        text = raw[start:].decode(encoding, _ERROR_HANDLER_NAME)
    finally:
        stack.pop()
    return text, errors, not errors


def _ascii_ratio(raw: bytes) -> float:
    """Share of bytes that are plain ASCII.

    A page that is overwhelmingly ASCII is a Latin/HTML page: a few high
    bytes in it are *damage*, not evidence that the whole page is CJK.
    """
    if not raw:
        return 0.0
    ascii_count = sum(1 for byte in raw if byte < 0x80)
    return ascii_count / len(raw)


def _plausibility(text: str) -> float:
    """Lower is better.  Penalises C0/C1 controls and replacement chars.

    A legacy Chinese page decoded as latin-1 is full of C1 controls, so this
    cheap statistic is enough to reject an otherwise "successful" decode.
    """
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        code = ord(ch)
        if ch in "\t\n\r\f":
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F:
            bad += 1
        elif code == 0xFFFD:
            bad += 1
    return bad / len(text)


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    non_ascii = 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        if code < 0x80:
            continue
        non_ascii += 1
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
                or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF
                or 0xF900 <= code <= 0xFAFF):
            cjk += 1
    if not non_ascii:
        return 0.0
    return cjk / non_ascii


def _score_candidate(raw: bytes, start: int, encoding: str) -> dict:
    try:
        text, errors, strict = _decode_with_evidence(raw, start, encoding)
    except (LookupError, UnicodeError):
        return {
            "encoding": encoding,
            "decodable": False,
            "strict": False,
            "error_count": len(raw) - start,
            "replacement_ratio": 1.0,
            "plausibility": 1.0,
            "text": "",
            "errors": [(start, len(raw))],
        }
    total = max(1, len(text))
    return {
        "encoding": encoding,
        "decodable": True,
        "strict": strict,
        "error_count": len(errors),
        "replacement_ratio": len(errors) / total,
        "plausibility": _plausibility(text),
        "text": text,
        "errors": errors,
    }


class CharByteMap:
    """Sparse character-offset -> byte-offset mapper.

    Blocks are located in document order by :mod:`extract`, so the mapper can
    advance a cursor instead of materialising one integer per character.
    Checkpoints (every ``CHECKPOINT_EVERY`` characters) keep random access for
    callers that inspect a block out of order.
    """

    CHECKPOINT_EVERY = 8192

    def __init__(self, raw: bytes, start: int, encoding: str, text: str, errors: list[tuple[int, int]]):
        self.raw = raw
        self.start = start
        self.encoding = encoding
        self.text = text
        self.errors = sorted(errors)
        self._error_starts = [a for a, _ in self.errors]
        self._checkpoints = [(0, start)]
        self._cursor_char = 0
        self._cursor_byte = start
        self._line_starts = None

    # -- line/column ------------------------------------------------------
    def line_starts(self) -> list[int]:
        if self._line_starts is None:
            starts = [0]
            append = starts.append
            for index, ch in enumerate(self.text):
                if ch == "\n":
                    append(index + 1)
            self._line_starts = starts
        return self._line_starts

    def line_col(self, char_index: int) -> tuple[int, int]:
        """Return 1-based line and 0-based column for a character offset."""
        char_index = max(0, min(char_index, len(self.text)))
        starts = self.line_starts()
        line = bisect_right(starts, char_index) - 1
        return line + 1, char_index - starts[line]

    # -- mapping ----------------------------------------------------------
    def _checkpoint_for(self, char_index: int) -> None:
        if char_index >= self._cursor_char:
            return
        # Binary search for the greatest checkpoint at or below the target.
        lo, hi = 0, len(self._checkpoints) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._checkpoints[mid][0] <= char_index:
                lo = mid
            else:
                hi = mid - 1
        self._cursor_char, self._cursor_byte = self._checkpoints[lo]

    def _record_checkpoint(self) -> None:
        if self._cursor_char % self.CHECKPOINT_EVERY == 0:
            last = self._checkpoints[-1][0]
            if self._cursor_char > last:
                self._checkpoints.append((self._cursor_char, self._cursor_byte))

    def _at_error(self, byte_index: int) -> int | None:
        pos = bisect_right(self._error_starts, byte_index) - 1
        if pos >= 0:
            start, end = self.errors[pos]
            if start <= byte_index < end:
                return end
        return None

    def byte_at(self, char_index: int) -> tuple[int, bool]:
        """Map a character offset to ``(byte_offset, exact)``."""
        char_index = max(0, min(char_index, len(self.text)))
        self._checkpoint_for(char_index)
        raw = self.raw
        exact = True
        while self._cursor_char < char_index:
            end = self._at_error(self._cursor_byte)
            if end is not None:
                self._cursor_byte = end
                self._cursor_char += 1
                continue
            if self._cursor_byte >= len(raw):
                exact = False
                self._cursor_char = char_index
                break
            step = self._char_byte_length(self._cursor_byte)
            if step <= 0:
                exact = False
                step = 1
            self._cursor_byte += step
            self._cursor_char += 1
            self._record_checkpoint()
        return self._cursor_byte, exact and self._cursor_char == char_index

    def _char_byte_length(self, byte_index: int) -> int:
        enc = self.encoding
        raw = self.raw
        if enc in ("utf-8", "utf8"):
            b = raw[byte_index]
            if b < 0x80:
                return 1
            if 0xC2 <= b <= 0xDF:
                return 2
            if 0xE0 <= b <= 0xEF:
                return 3
            if 0xF0 <= b <= 0xF4:
                return 4
            return 1
        if enc in ("ascii", "cp1252", "latin-1", "iso8859-1"):
            return 1
        if enc in ("utf-32-le", "utf-32-be"):
            return 4
        if enc in ("utf-16-le", "utf-16-be"):
            # A surrogate pair is one Python character but four bytes.
            if byte_index + 4 <= len(raw):
                unit = int.from_bytes(raw[byte_index:byte_index + 2], "little" if enc.endswith("le") else "big")
                if 0xD800 <= unit <= 0xDBFF:
                    unit2 = int.from_bytes(raw[byte_index + 2:byte_index + 4], "little" if enc.endswith("le") else "big")
                    if 0xDC00 <= unit2 <= 0xDFFF:
                        return 4
            return 2
        # Generic path: try increasing lengths until the byte sequence decodes
        # to exactly one character.
        for size in range(1, 9):
            chunk = raw[byte_index:byte_index + size]
            if len(chunk) < size:
                break
            try:
                decoded = chunk.decode(enc, "strict")
            except UnicodeDecodeError:
                continue
            if len(decoded) == 1:
                return size
        return 1


def _choose(raw: bytes, start: int, declared: str | None, header: str | None, bom: str | None) -> dict:
    evidence = []
    candidates = []

    # Evidence order mirrors the HTML standard: BOM > HTTP > meta > sniff.
    if bom:
        candidates.append(("bom", bom))
    if header:
        candidates.append(("header", header))
    if declared:
        candidates.append(("meta", declared))

    seen = set()
    for source, candidate in candidates:
        canonical = canonical_label(candidate)
        evidence.append(
            {
                "source": source,
                "declared": str(candidate),
                "canonical": canonical,
                "valid_label": canonical is not None,
            }
        )
        if canonical and canonical not in seen:
            seen.add(canonical)

    ordered = []
    for source, candidate in candidates:
        canonical = canonical_label(candidate)
        if canonical and canonical not in ordered:
            ordered.append(canonical)
    for fallback in _FALLBACKS:
        if fallback not in ordered:
            ordered.append(fallback)
    if "cp1252" not in ordered:
        ordered.append("cp1252")

    scored = []
    for index, encoding in enumerate(ordered):
        result = _score_candidate(raw, start, encoding)
        result["priority"] = index
        # The label's position in the evidence list is the only tie-breaker.
        result["declared"] = any(canonical_label(str(c)) == encoding for _, c in candidates)
        result["cjk_ratio"] = _cjk_ratio(result["text"])
        scored.append(result)

    single_byte = {"cp1252", "latin-1", "iso8859-1", "cp1250", "mac_roman"}

    def rank(item):
        # A declaration is honoured when the bytes are *nearly* consistent with
        # it.  Local damage (a few bad byte runs) is then exposed as explicit
        # undecodable ranges instead of silently switching the whole page to
        # another encoding.
        if not item["decodable"]:
            return (4, 1.0, 1.0, item["priority"])
        extra_bad = item["plausibility"] - item["replacement_ratio"]
        if item["declared"] and item["replacement_ratio"] <= 0.10 and extra_bad <= 0.005:
            return (0, item["replacement_ratio"], extra_bad, item["priority"])
        if item["strict"] and item["plausibility"] < 0.005:
            return (1, 0.0, 0.0, item["priority"])
        if item["strict"]:
            return (2, item["plausibility"], 0.0, item["priority"])
        return (3, item["plausibility"], item["replacement_ratio"], item["priority"])

    scored.sort(key=rank)
    best = scored[0]

    # A BOM is authoritative even if the payload is damaged: honour it and
    # expose the damage rather than guessing another encoding.
    if bom:
        chosen = next((item for item in scored if item["encoding"] == canonical_label(bom)), None)
        if chosen is None:  # pragma: no cover - defensive
            chosen = best
    else:
        chosen = best

    # Misdeclaration repair.  A declaration is only *kept* if the bytes are
    # broadly consistent with it.  Two cases are repaired explicitly:
    #   * a single-byte label that turns multi-byte CJK text into mojibake;
    #   * a declared multi-byte label when the bytes are clearly another
    #     multi-byte script (e.g. GBK declared as Shift_JIS).
    repaired_from = None
    if not bom:
        declared_item = next((item for item in scored if item["declared"]), None)
        if declared_item is not None and declared_item["replacement_ratio"] > 0.10:
            for item in scored:
                if item is declared_item or not item["strict"]:
                    continue
                if item["plausibility"] < 0.005 and item["encoding"] not in single_byte:
                    repaired_from = declared_item["encoding"]
                    chosen = item
                    break
        if (declared_item is None or chosen["encoding"] in single_byte) and chosen["encoding"] in single_byte:
            for item in scored:
                if item["encoding"] in single_byte or item.get("cjk_ratio", 0) < 0.3:
                    continue
                if item["strict"] and item["plausibility"] < 0.005:
                    repaired_from = declared_item["encoding"] if declared_item else chosen["encoding"]
                    chosen = item
                    break
        # A mostly-ASCII page whose declaration is utf-8 but which contains a
        # few invalid sequences must not be silently re-labelled as latin-1:
        # honour the declaration and expose the bad byte ranges.
        if (chosen["encoding"] in single_byte and declared_item is not None
                and declared_item["encoding"] == "utf-8"
                and declared_item["replacement_ratio"] <= 0.30
                and _ascii_ratio(raw[start:]) >= 0.85):
            chosen = declared_item
            repaired_from = None

    conflicts = []
    canonical_header = canonical_label(header) if header else None
    canonical_declared = canonical_label(declared) if declared else None
    canonical_bom = canonical_label(bom) if bom else None

    if canonical_bom and canonical_header and canonical_bom != canonical_header:
        conflicts.append(
            {
                "kind": "bom_vs_header",
                "bom": canonical_bom,
                "header": canonical_header,
                "resolution": "bom_wins",
            }
        )
    if canonical_bom and canonical_declared and canonical_bom != canonical_declared:
        conflicts.append(
            {
                "kind": "bom_vs_meta",
                "bom": canonical_bom,
                "meta": canonical_declared,
                "resolution": "bom_wins",
            }
        )
    if canonical_header and canonical_declared and canonical_header != canonical_declared:
        conflicts.append(
            {
                "kind": "header_vs_meta",
                "header": canonical_header,
                "meta": canonical_declared,
                "resolution": "header_wins",
            }
        )
    declared_for_source = {
        "bom": canonical_bom,
        "header": canonical_header,
        "meta": canonical_declared,
    }
    for source, encoding in declared_for_source.items():
        if encoding and encoding != chosen["encoding"]:
            conflicts.append(
                {
                    "kind": "declaration_vs_bytes",
                    "declared_source": source,
                    "declared": encoding,
                    "actual": chosen["encoding"],
                    "resolution": "declaration_rejected" if source != "bom" else "bom_wins",
                }
            )

    # A declaration that the bytes contradict is itself evidence worth keeping
    # even when the chosen encoding equals one of the other declarations.
    if chosen["declared"] and chosen["replacement_ratio"] > 0:
        conflicts.append(
            {
                "kind": "declaration_partially_invalid",
                "declared": chosen["encoding"],
                "replacement_ratio": round(chosen["replacement_ratio"], 6),
                "resolution": "kept_with_explicit_undecodable_ranges",
            }
        )
    if repaired_from:
        conflicts.append(
            {
                "kind": "misdeclared_encoding",
                "declared": repaired_from,
                "actual": chosen["encoding"],
                "resolution": "byte_evidence_override",
            }
        )

    if chosen["encoding"] == "ascii":
        # ascii is a strict subset of utf-8; prefer the wider label.
        chosen = next(item for item in scored if item["encoding"] == "utf-8") if any(
            item["encoding"] == "utf-8" for item in scored
        ) else chosen

    reason = "bom" if bom else ("header" if header else ("meta" if declared else "byte_sniff"))
    if bom and chosen["encoding"] != canonical_bom:
        reason = "bom"
    return {
        "chosen": chosen["encoding"],
        "reason": reason,
        "conflicts": conflicts,
        "evidence": evidence,
        "candidates": [
            {
                "encoding": item["encoding"],
                "strict": item["strict"],
                "error_count": item["error_count"],
                "replacement_ratio": round(item["replacement_ratio"], 6),
                "plausibility": round(item["plausibility"], 6),
            }
            for item in sorted(scored, key=lambda item: item["priority"])
        ],
        "chosen_result": chosen,
    }


def _raw_bytes(page: dict) -> tuple[bytes, str]:
    """Return raw bytes and a description of how they were obtained."""
    for key in ("content_b64", "raw_b64", "bytes_b64"):
        if page.get(key) is not None:
            value = page[key]
            if isinstance(value, bytes):
                return value, key
            try:
                return base64.b64decode(str(value), validate=False), key
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"invalid base64 in {key}: {exc}") from exc
    if "raw" in page and page["raw"] is not None:
        value = page["raw"]
        if isinstance(value, bytes):
            return value, "raw"
        return str(value).encode("utf-8"), "raw"
    for key in ("html", "content", "body"):
        if page.get(key) is not None:
            return str(page[key]).encode("utf-8"), "provided_text"
    raise ValueError("page has no content (expected content_b64, raw, html or content)")


def decode_page(page: dict, rules: dict | None = None) -> dict:
    """Decode one page, returning a copy with text and provenance."""
    rules = rules or rules_for()
    out = dict(page)
    raw, raw_source = _raw_bytes(page)
    bom_encoding, bom_length, bom_label = _bom_of(raw)
    header_label, header_source = _header_charset(page)
    declared_label = page.get("declared_encoding") or page.get("charset") or page.get("meta_charset")
    meta_label = _meta_charset(raw)

    # Prefer an explicit meta declaration supplied by the caller; otherwise use
    # the value sniffed from the bytes.
    meta_label = declared_label or meta_label

    decision = _choose(
        raw,
        bom_length,
        str(meta_label) if meta_label else None,
        header_label,
        bom_encoding,
    )
    chosen = decision["chosen_result"]
    start = bom_length
    text = chosen["text"]
    errors = chosen["errors"]
    reliable = chosen["decodable"] and chosen["replacement_ratio"] == 0.0
    if chosen["encoding"] in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        reliable = reliable and chosen["error_count"] == 0

    byte_map = CharByteMap(raw, start, chosen["encoding"], text, errors)
    error_ranges = [
        {
            "byte_start": begin,
            "byte_end": end,
            "char_start": None,  # filled lazily by callers if needed
            "preview_hex": raw[begin:end][:16].hex(),
        }
        for begin, end in errors
    ]

    encoding_meta = {
        "chosen": chosen["encoding"],
        "reason": decision["reason"],
        "bom": bom_label,
        "declared": str(meta_label) if meta_label else None,
        "header": header_label,
        "header_source": header_source,
        "raw_source": raw_source,
        "conflicts": decision["conflicts"],
        "evidence": decision["evidence"],
        "candidates": decision["candidates"],
        "strict": chosen["strict"],
        "reliable": reliable,
        "replacement_count": len(errors),
        "replacement_ratio": round(chosen["replacement_ratio"], 6),
        "undecodable": error_ranges,
        "content_byte_start": start,
        "raw_length": len(raw),
    }
    out["raw"] = raw
    out["raw_hash"] = _sha256(raw)
    out["byte_length"] = len(raw)
    out["text"] = text
    out["encoding"] = encoding_meta
    out["undecodable"] = error_ranges
    out["_byte_map"] = byte_map
    return out


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def process(record):
    """Pipeline stage: decode every page of a document.

    Legacy record-shaped input is returned unchanged so existing callers keep
    working.  A page that cannot be read produces an error annotation but does
    not abort the other pages; even a non-object page becomes a recorded
    failure rather than an exception.
    """
    if not isinstance(record, dict) or record.get("kind") != "document":
        return record
    rules = rules_for(record.get("rules_version"))
    out = dict(record)
    pages = []
    for index, page in enumerate(record.get("pages") or []):
        if not isinstance(page, dict):
            pages.append({
                "page_index": index + 1,
                "text": "",
                "raw": b"",
                "raw_hash": None,
                "byte_length": 0,
                "encoding": {
                    "chosen": None, "reason": "page_error", "reliable": False,
                    "error": f"page is not an object: {type(page).__name__}",
                    "undecodable": [], "conflicts": [], "evidence": [],
                    "candidates": [], "replacement_count": 0,
                    "replacement_ratio": 1.0, "raw_hash": None, "byte_length": 0,
                    "raw_length": 0,
                },
                "undecodable": [],
            })
            continue
        page = dict(page)
        page.setdefault("page_index", page.get("index", index + 1))
        try:
            pages.append(decode_page(page, rules))
        except Exception as exc:  # a broken page must not swallow the document
            page["text"] = ""
            page["raw"] = b""
            page["raw_hash"] = None
            page["byte_length"] = 0
            page["encoding"] = {
                "chosen": None,
                "reason": "page_error",
                "reliable": False,
                "error": f"{type(exc).__name__}: {exc}",
                "undecodable": [],
                "conflicts": [],
                "evidence": [],
                "candidates": [],
                "replacement_count": 0,
                "replacement_ratio": 1.0,
                "raw_hash": None,
                "byte_length": 0,
                "raw_length": 0,
            }
            page["undecodable"] = []
            pages.append(page)
    out["pages"] = pages
    return out
