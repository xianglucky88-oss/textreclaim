"""Normalisation that is deliberately *lossless for meaning*.

Rules that this module follows:

* Unicode is normalised with NFC only.  NFKC is never used: it rewrites
  ligatures, mathematical symbols, units and compatibility ideographs.
* Full-width ASCII variants are folded only when they are ASCII-range
  full-width forms.  This is a width change, not a compatibility rewrite.
* Case is preserved.  Proper names, code, identifiers and product names keep
  their exact spelling.
* Whitespace is never inserted into or removed from code.  In prose only runs
  of whitespace are collapsed and block edges trimmed.
* Numeric values, decimal separators, currency signs and units are untouched.
* Table cells are normalised independently and in place; column order and
  header/data relationships are never changed.
"""
from __future__ import annotations

import unicodedata

from .config import rules_for

# Inline code / preformatted content is left byte-for-byte intact.
CODE_TYPES = {"code", "preformatted"}

_WS = " \t\r\n\f\v"


def fold_fullwidth(text: str) -> tuple[str, int]:
    """Fold full-width *alphanumerics* to ASCII; return ``(text, changed)``.

    Only U+FF10..FF19 (digits), U+FF21..FF3A (upper), U+FF41..FF5A (lower) and
    U+3000 (ideographic space) are folded.  Full-width punctuation such as
    ``，。：；！？（）`` is deliberately preserved: in CJK prose it carries
    meaning and spacing conventions, and rewriting it would alter the text.
    """
    out = []
    changed = 0
    for ch in text:
        code = ord(ch)
        if (0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A
                or 0xFF41 <= code <= 0xFF5A):
            out.append(chr(code - 0xFEE0))
            changed += 1
        elif code == 0x3000:
            out.append(" ")
            changed += 1
        else:
            out.append(ch)
    return "".join(out), changed


def collapse_whitespace(text: str) -> tuple[str, int]:
    """Collapse whitespace runs, trim edges, preserve line breaks."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out_lines = []
    changed = 0
    for line in lines:
        cleaned = " ".join(line.split())
        if cleaned != line:
            changed += 1
        out_lines.append(cleaned)
    result = "\n".join(out_lines)
    # Trim leading/trailing blank lines without touching interior newlines.
    stripped = result.strip()
    return stripped, changed


def normalize_text(text: str, profile: str = "prose", rules: dict | None = None) -> tuple[str, list[dict]]:
    """Return normalised text plus an explicit list of applied changes."""
    rules = rules or rules_for()
    changes: list[dict] = []
    if text is None:
        return "", changes
    text = str(text)
    if profile in CODE_TYPES:
        # Code is not touched at all, not even NFC: identifiers may rely on the
        # exact code point sequence.
        return text, changes

    original = text
    if rules.get("width_fold", True):
        folded, count = fold_fullwidth(text)
        if count:
            changes.append({"kind": "width_fold", "characters": count})
        text = folded

    # NFC is safe for prose: it composes combining marks without compatibility
    # substitutions that would alter symbols or units.
    composed = unicodedata.normalize("NFC", text)
    if composed != text:
        changes.append({"kind": "nfc", "chars": len(text)})
    text = composed

    if rules.get("collapse_whitespace", True):
        if profile == "cell":
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            collapsed = [" ".join(line.split()) for line in lines]
            new_text = "\n".join(collapsed).strip()
            if new_text != text:
                changes.append({"kind": "whitespace", "profile": "cell"})
            text = new_text
        else:
            new_text, changed_lines = collapse_whitespace(text)
            if changed_lines:
                changes.append({"kind": "whitespace", "lines": changed_lines})
            text = new_text

    if text != original:
        changes.append({"kind": "changed"})
    return text, changes


def normalize_block(block: dict, rules: dict) -> dict:
    """Normalise one extracted block (returns a copy)."""
    block = dict(block)
    profile = "code" if block.get("type") in CODE_TYPES else "prose"
    if block.get("type") == "table":
        table = dict(block.get("table") or {})
        # Normalise the grid in place and re-derive the row view from it, so
        # the two representations can never disagree.  Column order and spans
        # are untouched.
        grid = table.get("grid")
        if isinstance(grid, list):
            new_grid = []
            for row in grid:
                if not isinstance(row, list):
                    new_grid.append(row)
                    continue
                new_grid.append([
                    normalize_cell(cell, rules) if isinstance(cell, dict) else cell
                    for cell in row
                ])
            table["grid"] = new_grid
            table["rows"] = [
                ["" if cell.get("synthesized") else cell.get("text", "") for cell in row]
                for row in new_grid if isinstance(row, list)
            ]
        cells = table.get("cells")
        if isinstance(cells, list):
            table["cells"] = [normalize_cell(cell, rules) if isinstance(cell, dict) else cell
                              for cell in cells]
        caption, caption_changes = normalize_text(table.get("caption", ""), "cell", rules)
        if caption_changes:
            table["caption"] = caption
        block["table"] = table
        text, changes = normalize_text(table.get("caption", ""), "cell", rules)
        block["text"] = text
        if changes:
            block.setdefault("normalizations", []).extend(changes)
        return block

    source_text = block.get("text", "")
    text, changes = normalize_text(source_text, profile, rules)
    if changes:
        block["text"] = text
        block.setdefault("normalizations", []).extend(changes)
        if "original_text_hash" not in block:
            block["original_text_hash"] = _text_hash(source_text)
    return block


def normalize_cell_text(value, rules: dict) -> str:
    if value is None:
        return ""
    text, _ = normalize_text(str(value), "cell", rules)
    return text


def normalize_cell(cell: dict, rules: dict) -> dict:
    cell = dict(cell)
    cell["text"] = normalize_cell_text(cell.get("text", ""), rules)
    return cell


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def process(record):
    """Pipeline stage.

    * Document records: normalise every block, including table cells.
    * Legacy records: keep the historical field behaviour exactly.
    """
    if not isinstance(record, dict) or record.get("kind") != "document":
        rec = dict(record)
        rec["src"] = str(rec.get("src", "")).strip().lower()
        rec["name"] = str(rec.get("name", "")).strip()
        rec["value"] = int(rec.get("value", 0))
        return rec

    rules = rules_for(record.get("rules_version"))
    out = dict(record)
    blocks = []
    for block in record.get("blocks") or []:
        blocks.append(normalize_block(block, rules))
    out["blocks"] = blocks
    out["tables"] = [block["table"] for block in blocks if block.get("type") == "table" and block.get("table")]
    return out
