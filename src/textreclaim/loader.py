"""Offline ingestion: read input records without loading everything at once.

``load()`` keeps the historical contract (a list of legacy records) for the
fixture.  ``iter_records`` yields one record at a time and accepts:

* a JSON array (streamed element by element when the file is large),
* JSON Lines,
* a single JSON object.

A ``kind: document`` object may reference its pages' raw bytes by
``content_b64``/``raw_b64`` or, for large inputs, by ``content_ref`` pointing at
a file next to the input.  Files are only read on demand, so peak memory is
bounded by the largest single page rather than the whole collection.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .config import FIXTURES

CHUNK = 1 << 16


class _PrefixedStream:
    """Re-attach a peeked prefix to a text stream."""

    def __init__(self, prefix: str, stream):
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> str:
        if self._prefix:
            if size is None or size < 0:
                data = self._prefix + self._stream.read()
                self._prefix = ""
                return data
            data, self._prefix = self._prefix[:size], self._prefix[size:]
            if len(data) < size:
                data += self._stream.read(size - len(data))
            return data
        return self._stream.read(size)


def _resolve_ref(ref: str, base: Path) -> Path:
    """Resolve a ``content_ref``; paths must stay inside the input directory."""
    base = base.resolve()
    path = (base / ref).resolve() if not Path(ref).is_absolute() else Path(ref).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise ValueError(f"content_ref escapes the input directory: {ref!r}")
    return path


def iter_json_array(stream):
    """Yield the elements of a JSON array incrementally.

    Only one element is decoded at a time, so a multi-gigabyte collection does
    not need to be materialised.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    closed = False
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        buffer += chunk
        while True:
            stripped = buffer.lstrip()
            if not stripped:
                buffer = ""
                break
            if not started:
                if stripped[0] != "[":
                    raise ValueError("expected a JSON array")
                buffer = stripped[1:]
                started = True
                continue
            if stripped[0] == "]":
                closed = True
                return
            if stripped[0] == ",":
                buffer = stripped[1:]
                continue
            try:
                value, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                if not chunk:
                    raise
                buffer = stripped
                break
            yield value
            buffer = stripped[end:]
    if started and not closed:
        # A truncated array (even one that ends right after a comma) is an
        # input error, but never a silent one: silently yielding the elements
        # read so far would look like a successful, complete ingest.
        raise ValueError("unterminated JSON array")


def iter_records(path: str | Path | None = None):
    """Yield records one at a time from *path*.

    JSON arrays are parsed element by element and JSON Lines is read line by
    line, so neither form materialises the whole file.  A single JSON object
    must be read in full (it is one record by definition).
    """
    path = Path(path or FIXTURES)
    base = path.parent
    with open(path, encoding="utf-8") as handle:
        prefix = ""
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            prefix += chunk
            if prefix.strip():
                break
        if not prefix.strip():
            return
        stream = _PrefixedStream(prefix, handle)
        stripped = prefix.lstrip()
        if stripped.startswith("["):
            for record in iter_json_array(stream):
                yield _materialize(record, base)
            return
        if stripped.startswith("{"):
            # One pretty-printed object vs. JSON Lines: the first non-empty
            # line decides.  A first line that is already a complete JSON
            # value means JSON Lines; otherwise the whole text is one object.
            buffer = ""
            first_line = None
            while first_line is None:
                index = buffer.find("\n")
                if index >= 0:
                    candidate, buffer = buffer[:index], buffer[index + 1:]
                    if candidate.strip():
                        first_line = candidate
                    continue
                chunk = stream.read(CHUNK)
                if not chunk:
                    first_line = buffer
                    buffer = ""
                    break
                buffer += chunk
            remainder = buffer + stream.read()
            if first_line is not None and _is_complete_json(first_line):
                for line in [first_line] + remainder.splitlines():
                    if line.strip():
                        yield _materialize(json.loads(line), base)
                return
            value = json.loads((first_line or "") + "\n" + remainder)
            if isinstance(value, list):
                for record in value:
                    yield _materialize(record, base)
            else:
                yield _materialize(value, base)
            return
        for line in _chain_lines("", stream):
            if line.strip():
                yield _materialize(json.loads(line), base)


def _is_complete_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


def _chain_lines(first: str, stream):
    buffer = ""
    pending = [first] if first else []
    for chunk in pending:
        yield chunk
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def _materialize(record, base: Path):
    """Load any external page payload and attach it to the record."""
    if not isinstance(record, dict) or record.get("kind") != "document":
        return record
    pages = []
    for page in record.get("pages") or []:
        page = dict(page)
        ref = page.pop("content_ref", None)
        if ref is not None and "content_b64" not in page and "raw_b64" not in page and "raw" not in page:
            path = _resolve_ref(str(ref), base)
            page["raw"] = path.read_bytes()
            page.setdefault("raw_source", "content_ref")
        pages.append(page)
    record = dict(record)
    record["pages"] = pages
    return record


def load(path: str | Path | None = None):
    """Historical entry point: a list of records."""
    return list(iter_records(path))


def decode_b64(payload: str) -> bytes:
    return base64.b64decode(payload)
