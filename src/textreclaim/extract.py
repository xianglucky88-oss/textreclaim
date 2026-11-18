"""Structure recovery for damaged, noisy pages.

The extractor is intentionally tolerant of broken markup.  It never lets a
local defect consume the rest of the page:

* an unterminated tag or comment is treated as literal text and scanning
  resumes at the next plausible ``<``;
* an unclosed container is closed implicitly at EOF or at the next block
  boundary;
* unknown elements are traversed rather than dropped;
* a broken table becomes a ``table`` block with ``damage`` recorded, not a
  hole in the document.

Every block carries a locator with page number, character offsets, byte
offsets into the original page bytes, line/column and the raw page hash, so a
downstream consumer can always return to the source bytes.
"""
from __future__ import annotations

import html as _html
import re
from bisect import bisect_left

from .config import rules_for

# ---------------------------------------------------------------------------
# Tag sets
# ---------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

SKIP_CONTENT_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "head",
    "title", "meta", "link", "base",
}

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

CONTAINER_TAGS = {
    "div", "section", "article", "main", "figure", "header", "footer", "aside",
    "nav", "form", "fieldset", "details", "summary", "center", "body", "html",
    "head", "map", "picture", "dialog",
}

INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "big", "cite", "code", "data", "del", "dfn",
    "em", "font", "i", "ins", "kbd", "label", "mark", "q", "rp", "rt", "ruby",
    "s", "samp", "small", "span", "strike", "strong", "sub", "sup", "time",
    "tt", "u", "var", "wbr", "nobr", "blink",
}

BLOCK_LEVEL = {
    "address", "article", "aside", "blockquote", "details", "dialog", "dd",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "table", "ul", "caption", "thead", "tbody",
    "tfoot", "tr", "td", "th",
}

P_CLOSERS = BLOCK_LEVEL | {"body", "html"}

FOOTNOTE_ATTR_RE = re.compile(
    r"(?:^|[\s_\-])(?:footnote|footnotes|endnote|endnotes|fn|ftn)(?:[_\-\s]|$)",
    re.IGNORECASE,
)
FOOTNOTE_ID_RE = re.compile(r"^(?:fn|footnote|ftn|note|endnote)[-_:.]?([0-9]+)$", re.IGNORECASE)
CONTINUED_RE = re.compile(
    r"(续\s*表|续表|接上页|续下页|continued|continuation|cont(?:inued)?\.?\s*$)", re.IGNORECASE
)
BRACKET_REF_RE = re.compile(r"\[\^?([0-9]{1,4}|注\s*[0-9]{0,4}|[*†‡])\]")

MAX_TAG_LENGTH = 4096
MAX_COMMENT_LENGTH = 65536
MAX_DEPTH = 256           # maximum element nesting retained in the tree
MAX_TRAVERSE_DEPTH = 96   # maximum recursion depth during block emission
MAX_REF_SCAN = 4000
MAX_ANCESTORS = 8


# ---------------------------------------------------------------------------
# Tolerant tokenizer
# ---------------------------------------------------------------------------


class Token:
    __slots__ = ("kind", "start", "end", "name", "attrs", "self_closing", "closed")

    def __init__(self, kind, start, end, name=None, attrs=None, self_closing=False, closed=True):
        self.kind = kind
        self.start = start
        self.end = end
        self.name = name
        self.attrs = attrs or {}
        self.self_closing = self_closing
        self.closed = closed

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Token({self.kind},{self.name},{self.start}:{self.end})"


_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9:_.\-]*")
_ATTR_RE = re.compile(
    r"""([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?"""
)


def _find_tag_end(text: str, start: int) -> int | None:
    """Find the ``>`` ending a tag, honouring quotes and a hard bound.

    ``None`` means the tag is unterminated or a new ``<`` appeared first; the
    caller then resynchronises at the next character instead of swallowing the
    remainder of the page.
    """
    limit = min(len(text), start + MAX_TAG_LENGTH)
    quote = None
    index = start + 1
    while index < limit:
        ch = text[index]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return index
        elif ch == "<":
            return None
        index += 1
    return None


def _parse_attrs(fragment: str) -> dict:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(fragment):
        name = match.group(1).lower()
        if name == "/":
            continue
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[name] = _html.unescape(value) if value is not None else ""
    return attrs


def _resync_span(text: str, start: int) -> int:
    """Where scanning resumes after a malformed ``<`` at *start*.

    The literal text runs to the next ``<`` or the tag-length bound, whichever
    comes first.  This keeps a 10 kB broken tag as one text run rather than
    emitting one token per character.
    """
    limit = min(len(text), start + MAX_TAG_LENGTH)
    remainder = text.find("<", start + 1, limit)
    if remainder < 0:
        if limit < len(text):
            # We hit the length bound: continue the literal run until the next
            # plausible tag start.
            remainder = text.find("<", limit)
    return remainder if remainder >= 0 else limit


def tokenize(text: str) -> list[Token]:
    """Split *text* into tags/text without ever raising on malformed input.

    Text is referenced by offset, not copied, which keeps peak memory close to
    the size of the decoded page.
    """
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        lt = text.find("<", index)
        if lt < 0:
            tokens.append(Token("text", index, length))
            break
        if lt > index:
            tokens.append(Token("text", index, lt))
        nxt = text[lt + 1:lt + 2]

        if text.startswith("<!--", lt):
            close = text.find("-->", lt + 4, lt + MAX_COMMENT_LENGTH)
            if close >= 0:
                tokens.append(Token("comment", lt, close + 3))
                index = close + 3
                continue
            # Unterminated comment: do not swallow the page.
            tokens.append(Token("text", lt, lt + 4))
            index = lt + 4
            continue

        if text.startswith("<![CDATA[", lt):
            close = text.find("]]>", lt + 9, lt + MAX_TAG_LENGTH)
            if close >= 0:
                tokens.append(Token("text", lt + 9, close))
                index = close + 3
            else:
                span = _resync_span(text, lt)
                tokens.append(Token("text", lt, span))
                index = span
            continue

        if nxt in ("!", "?"):
            end = _find_tag_end(text, lt)
            if end is None:
                span = _resync_span(text, lt)
                tokens.append(Token("text", lt, span))
                index = span
            else:
                tokens.append(Token("decl", lt, end + 1))
                index = end + 1
            continue

        if nxt == "/":
            match = _NAME_RE.match(text, lt + 2)
            end = _find_tag_end(text, lt) if match else None
            if not match or end is None:
                span = _resync_span(text, lt) if match else lt + 1
                tokens.append(Token("text", lt, span))
                index = span
                continue
            tokens.append(Token("end", lt, end + 1, name=match.group(0).lower()))
            index = end + 1
            continue

        match = _NAME_RE.match(text, lt + 1)
        if not match:
            tokens.append(Token("text", lt, lt + 1))
            index = lt + 1
            continue
        end = _find_tag_end(text, lt)
        if end is None:
            span = _resync_span(text, lt)
            tokens.append(Token("text", lt, span))
            index = span
            continue
        name = match.group(0).lower()
        raw = text[match.end():end]
        self_closing = raw.rstrip().endswith("/")
        if self_closing:
            raw = raw.rstrip()[:-1]
        tokens.append(Token("start", lt, end + 1, name=name, attrs=_parse_attrs(raw),
                            self_closing=self_closing))
        index = end + 1
    return tokens


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


class Node:
    """A lightweight node; text nodes are identified by ``tag is None``."""

    __slots__ = (
        "tag", "attrs", "start", "end", "children", "closed",
        "self_closing", "parent", "implicit_close",
    )

    def __init__(self, tag=None, attrs=None, start=0, end=None, parent=None, self_closing=False):
        self.tag = tag
        self.attrs = attrs or {}
        self.start = start
        self.end = end
        self.children: list[Node] = []
        self.closed = tag is None
        self.self_closing = self_closing
        self.parent = parent
        self.implicit_close = False

    @property
    def is_text(self):
        return self.tag is None

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    def classes(self) -> list[str]:
        value = self.attrs.get("class", "")
        return [part for part in re.split(r"\s+", value) if part]

    def hint_text(self) -> str:
        parts = []
        for key in ("class", "id", "role", "epub:type", "itemprop", "aria-label", "data-type"):
            value = self.attrs.get(key)
            if value:
                parts.append(str(value))
        return " ".join(parts)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Node({self.tag},{self.start}:{self.end},children={len(self.children)})"


def _find_open(stack, tag, stop_tags):
    for depth in range(len(stack) - 1, 0, -1):
        node = stack[depth]
        if node.tag == tag:
            return depth
        if node.tag in stop_tags:
            return None
    return None


def _close_to(stack, depth, at, implicit=True):
    """Close every open element strictly inside ``depth``.

    The element at ``depth`` itself is left on the stack; callers pop it after
    recording the closing position.
    """
    while len(stack) - 1 > depth:
        node = stack.pop()
        if node.end is None:
            node.end = at
            node.implicit_close = implicit


def _implicitly_close(stack, depth, at):
    """Close inner elements and implicitly terminate the element at *depth*."""
    _close_to(stack, depth, at, implicit=True)
    node = stack.pop()
    if node.end is None:
        node.end = at
        node.implicit_close = True


def build_tree(text: str, tokens: list[Token]) -> tuple[Node, list[dict]]:
    root = Node(tag="#document", start=0, end=len(text))
    stack = [root]
    recovered = []

    def add_text(token):
        node = Node(tag=None, start=token.start, end=token.end, parent=stack[-1])
        stack[-1].children.append(node)

    for token in tokens:
        if token.kind == "text":
            add_text(token)
            continue
        if token.kind in ("comment", "decl"):
            continue
        if token.kind == "end":
            depth = _find_open(stack, token.name, set())
            if depth is None:
                recovered.append({"kind": "stray_end", "tag": token.name,
                                  "char_start": token.start, "char_end": token.end})
                continue
            _close_to(stack, depth, token.start, implicit=False)
            stack[-1].end = token.end
            stack.pop()
            continue

        name = token.name
        if name in SKIP_CONTENT_TAGS:
            node = Node(tag=name, attrs=token.attrs, start=token.start, end=token.end,
                        parent=stack[-1], self_closing=token.self_closing)
            stack[-1].children.append(node)
            if not token.self_closing and name not in VOID_TAGS:
                stack.append(node)
            continue

        # Implicit closing rules ----------------------------------------
        if name == "li":
            depth = _find_open(stack, "li", {"ul", "ol", "menu", "table"})
            if depth is not None:
                _implicitly_close(stack, depth, token.start)
        elif name in ("td", "th"):
            depth = _find_open(stack, "td", {"tr", "table"})
            if depth is None:
                depth = _find_open(stack, "th", {"tr", "table"})
            if depth is not None:
                _implicitly_close(stack, depth, token.start)
        elif name == "tr":
            for cell in ("td", "th"):
                depth = _find_open(stack, cell, {"tr", "table"})
                if depth is not None:
                    _implicitly_close(stack, depth, token.start)
            depth = _find_open(stack, "tr", {"table"})
            if depth is not None:
                _implicitly_close(stack, depth, token.start)
        elif name in ("thead", "tbody", "tfoot"):
            for other in ("td", "th", "tr", "thead", "tbody", "tfoot"):
                depth = _find_open(stack, other, {"table"})
                if depth is not None:
                    _implicitly_close(stack, depth, token.start)
        elif name == "p":
            depth = _find_open(stack, "p", P_CLOSERS - {"p"})
            if depth is not None:
                _implicitly_close(stack, depth, token.start)
        elif name in P_CLOSERS and name not in ("td", "th", "tr"):
            depth = _find_open(stack, "p", P_CLOSERS - {"p"})
            if depth is not None:
                _implicitly_close(stack, depth, token.start)

        node = Node(tag=name, attrs=token.attrs, start=token.start,
                    end=(token.end if (token.self_closing or name in VOID_TAGS) else None),
                    parent=stack[-1], self_closing=token.self_closing)
        stack[-1].children.append(node)
        if token.self_closing or name in VOID_TAGS:
            pass
        elif len(stack) >= MAX_DEPTH:
            recovered.append({"kind": "max_depth", "tag": name,
                              "char_start": token.start, "char_end": token.end})
            node.implicit_close = True
        else:
            stack.append(node)
    while len(stack) > 1:
        node = stack.pop()
        if node.end is None:
            node.end = len(text)
            node.implicit_close = True
    return root, recovered


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _raw(node: Node, source: str) -> str:
    return source[node.start:node.end]


def _flatten_iterative(node: Node, source: str) -> str:
    """Collect all descendant text without recursion.

    Used only when the tree is too deep for the normal walk: content must be
    preserved, so this is a flattening fallback rather than a cutoff.
    """
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.is_text:
            parts.append(_html.unescape(source[current.start:current.end]))
            continue
        if current.tag in SKIP_CONTENT_TAGS:
            continue
        if current.tag == "br":
            parts.append("\n")
            continue
        if current.tag == "img":
            alt = current.attrs.get("alt")
            if alt:
                parts.append(alt)
            continue
        stack.extend(reversed(current.children))
    return "".join(parts)


def _inline_text(node: Node, source: str, refs: list[dict] | None = None,
                 skip_blocks: bool = True, depth: int = 0,
                 skip_block_children: bool = False) -> str:
    """Flatten inline content, collecting footnote-like references.

    ``depth`` bounds recursion for pathological nesting; past the bound the
    subtree is flattened iteratively so no text is lost.

    ``skip_block_children`` drops *block-level* descendants (used when a
    container's children are emitted as their own blocks, so their text is not
    duplicated into the container's paragraph).
    """
    if node.is_text:
        return _html.unescape(_raw(node, source))
    tag = node.tag
    if tag in SKIP_CONTENT_TAGS:
        return ""
    if tag == "br":
        return "\n"
    if tag == "img":
        return node.attrs.get("alt", "") or ""
    if skip_blocks and tag in ("table", "ul", "ol", "dl"):
        if refs is not None:
            refs.append({"kind": "nested_block", "tag": tag,
                         "char_start": node.start, "char_end": node.end})
        return ""
    if depth > MAX_TRAVERSE_DEPTH:
        return _flatten_iterative(node, source)
    if tag == "a" and refs is not None:
        href = node.attrs.get("href", "")
        refs.append({"kind": "anchor", "href": href,
                     "internal": href.startswith("#"),
                     "char_start": node.start, "char_end": node.end,
                     "text": _collapse(_inline_text(node, source, None, False, depth + 1))})
    if tag in ("sup", "sub") and refs is not None:
        marker = _collapse(_inline_text(node, source, None, False, depth + 1))
        if marker:
            refs.append({"kind": "sup", "marker": marker,
                         "char_start": node.start, "char_end": node.end})
    parts: list[str] = []
    for child in node.children:
        if child.is_text:
            parts.append(_html.unescape(_raw(child, source)))
        elif child.tag in SKIP_CONTENT_TAGS:
            continue
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag == "img":
            alt = child.attrs.get("alt")
            if alt:
                parts.append(alt)
        elif child.tag in ("table", "ul", "ol", "dl") and skip_blocks:
            continue
        elif skip_block_children and child.tag in BLOCK_LEVEL:
            # This child becomes its own block; do not duplicate its text.
            continue
        else:
            parts.append(_inline_text(child, source, refs, skip_blocks, depth + 1,
                                      skip_block_children))
    return "".join(parts)


def _direct_inline_text(node: Node, source: str,
                        skip_block_children: bool = False) -> tuple[str, list[dict]]:
    refs: list[dict] = []
    return _inline_text(node, source, refs, skip_blocks=True,
                        skip_block_children=skip_block_children), refs


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------


class PageContext:
    def __init__(self, page: dict, index: int):
        self.page = page
        self.index = index
        self.text = page.get("text", "") or ""
        self.mapper = page.get("_byte_map")
        self.raw_hash = page.get("raw_hash")
        self.encoding = page.get("encoding") or {}
        self.undecodable = page.get("undecodable") or []
        self.reliable = bool(self.encoding.get("reliable", False))
        self.page_index = page.get("page_index", index + 1)

    def locator(self, start: int, end: int, text_start: int | None = None,
                text_end: int | None = None) -> tuple[dict, bool]:
        start = max(0, min(start, len(self.text)))
        end = max(start, min(end, len(self.text)))
        exact = True
        if self.mapper is not None:
            byte_start, e1 = self.mapper.byte_at(start)
            byte_end, e2 = self.mapper.byte_at(end)
            line_start, col_start = self.mapper.line_col(start)
            line_end, col_end = self.mapper.line_col(end)
            exact = e1 and e2
        else:
            byte_start = byte_end = None
            line_start = self.text.count("\n", 0, start) + 1
            line_end = self.text.count("\n", 0, end) + 1
            col_start = start - (self.text.rfind("\n", 0, start) + 1)
            col_end = end - (self.text.rfind("\n", 0, end) + 1)
        if text_start is None:
            text_start = start
        if text_end is None:
            text_end = end
        locator = {
            "page": self.page_index,
            "char_start": start,
            "char_end": end,
            "text_char_start": max(start, min(text_start, len(self.text))),
            "text_char_end": max(start, min(text_end, len(self.text))),
            "byte_start": byte_start,
            "byte_end": byte_end,
            "line_start": line_start,
            "line_end": line_end,
            "col_start": col_start,
            "col_end": col_end,
            "exact": bool(exact),
            "raw_sha256": self.raw_hash,
            "raw_byte_length": self.encoding.get("raw_length"),
        }
        return locator, exact

    def overlaps_undecodable(self, start: int, end: int) -> bool:
        if self.mapper is None or not self.mapper.errors:
            return False
        b1, _ = self.mapper.byte_at(start)
        b2, _ = self.mapper.byte_at(end)
        for a, b in self.mapper.errors:
            if a < b2 and b > b1:
                return True
        return False


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _int_attr(node: Node, name: str, limit: int) -> tuple[int, bool]:
    value = node.attrs.get(name)
    if not value:
        return 1, False
    match = re.match(r"\s*([0-9]+)", str(value))
    if not match:
        return 1, True
    number = int(match.group(1))
    if number < 1:
        return 1, True
    if number > limit:
        return limit, True
    return number, False


def _cell_text(cell: Node, source: str, refs: list[dict]) -> str:
    text = _inline_text(cell, source, refs, skip_blocks=True)
    return _collapse(text.replace("\n", " "))


def _nearest_table_ancestor(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.tag != "#document":
        if parent.tag == "table":
            return parent
        parent = parent.parent
    return None


def _collect_rows(table: Node) -> tuple[list[Node], bool]:
    rows: list[Node] = []
    truncated = False

    def visit(node: Node, depth: int):
        nonlocal truncated
        if depth > 64:
            truncated = True
            return
        for child in node.children:
            if child.is_text or child.tag == "table":
                continue
            if child.tag == "tr":
                if _nearest_table_ancestor(child) is table:
                    rows.append(child)
            else:
                visit(child, depth + 1)

    visit(table, 0)
    if not rows:
        cells = []

        def gather(node: Node, depth: int):
            if depth > 64:
                truncated = True
                return
            for child in node.children:
                if child.is_text or child.tag == "table":
                    continue
                if child.tag in ("td", "th"):
                    if _nearest_table_ancestor(child) is table:
                        cells.append(child)
                else:
                    gather(child, depth + 1)

        gather(table, 0)
        if cells:
            synthetic = Node(tag="tr", start=table.start, end=table.end, parent=table)
            synthetic.children = cells
            for cell in cells:
                cell.parent = synthetic
            rows = [synthetic]
            truncated = True
    return rows, truncated


def parse_table(node: Node, source: str, ctx: PageContext, rules: dict, block_id: str) -> dict:
    max_colspan = int(rules.get("max_colspan", 1000))
    max_rowspan = int(rules.get("max_rowspan", 1000))
    max_columns = int(rules.get("max_table_columns", 256))
    rows, structural_damage = _collect_rows(node)

    caption = ""
    for child in node.children:
        if child.is_text:
            continue
        if child.tag == "caption":
            caption = _collapse(_inline_text(child, source, None, True))
            break

    grid: list[list[dict | None]] = []
    header_rows: set[int] = set()
    damage: list[str] = []
    cell_records: list[dict] = []

    for row_index, row in enumerate(rows):
        ancestor = row.parent
        while ancestor is not None and ancestor.tag != "table":
            if ancestor.tag in ("thead", "tbody", "tfoot"):
                if ancestor.tag == "thead":
                    header_rows.add(row_index)
                break
            ancestor = ancestor.parent

        while len(grid) <= row_index:
            grid.append([])
        col = 0
        for cell in [c for c in row.children if not c.is_text and c.tag in ("td", "th")]:
            while col < len(grid[row_index]) and grid[row_index][col] is not None:
                col += 1
            if col >= max_columns:
                damage.append("max_columns_exceeded")
                break
            rowspan, rs_clamped = _int_attr(cell, "rowspan", max_rowspan)
            colspan, cs_clamped = _int_attr(cell, "colspan", max_columns)
            if rs_clamped or cs_clamped:
                damage.append("span_clamped")
            if cell.tag == "th":
                header_rows.add(row_index)
            refs: list[dict] = []
            text = _cell_text(cell, source, refs)
            cell_loc, _ = ctx.locator(cell.start, cell.end)
            record = {
                "r": row_index,
                "c": col,
                "text": text,
                "rowspan": rowspan,
                "colspan": colspan,
                "header": cell.tag == "th",
                "origin": {"r": row_index, "c": col},
                "synthesized": False,
                "locator": cell_loc,
                "attrs_hint": cell.hint_text(),
                "refs": [r for r in refs if r.get("kind") != "nested_block"],
            }
            cell_records.append(record)
            for dr in range(rowspan):
                target_row = row_index + dr
                while len(grid) <= target_row:
                    grid.append([])
                for dc in range(colspan):
                    target_col = col + dc
                    if target_col >= max_columns:
                        damage.append("max_columns_exceeded")
                        continue
                    while len(grid[target_row]) <= target_col:
                        grid[target_row].append(None)
                    if grid[target_row][target_col] is not None:
                        damage.append("overlapping_span")
                        continue
                    if dr == 0 and dc == 0:
                        placed = record
                    else:
                        placed = {
                            "r": target_row,
                            "c": target_col,
                            "text": "",
                            "rowspan": 1,
                            "colspan": 1,
                            "header": record["header"],
                            "origin": {"r": row_index, "c": col},
                            "synthesized": True,
                            "locator": cell_loc,
                        }
                    grid[target_row][target_col] = placed
            col += colspan

    columns = max((len(row) for row in grid), default=0)
    for row in grid:
        while len(row) < columns:
            row.append(None)

    # Explicit placeholders instead of merging holes: absent cells stay absent.
    filled_grid: list[list[dict]] = []
    for r, row in enumerate(grid):
        filled = []
        for c, cell in enumerate(row):
            if cell is None:
                cell = {
                    "r": r, "c": c, "text": "", "rowspan": 1, "colspan": 1,
                    "header": False, "origin": {"r": r, "c": c},
                    "synthesized": True, "missing": True, "locator": None,
                }
            filled.append(cell)
        filled_grid.append(filled)

    text_rows = [["" if cell.get("synthesized") else cell.get("text", "") for cell in row]
                 for row in filled_grid]
    flat = (caption + "\n") if caption else ""
    flat += "\n".join(" | ".join(values) for values in text_rows)

    locator, exact = ctx.locator(node.start, node.end)
    if not exact:
        damage.append("locator_inexact")
    if structural_damage:
        damage.append("structural_recovery")
    if ctx.overlaps_undecodable(node.start, node.end):
        damage.append("undecodable_bytes")

    return {
        "table_id": block_id,
        "caption": caption,
        "columns": columns,
        "header_rows": sorted(header_rows),
        "rows": text_rows,
        "grid": filled_grid,
        "cells": cell_records,
        "spans": [
            {k: cell[k] for k in ("r", "c", "rowspan", "colspan", "text", "origin")}
            for cell in cell_records
            if cell["rowspan"] > 1 or cell["colspan"] > 1
        ],
        "damage": sorted(set(damage)),
        "locator": locator,
        "parts": [{"page": ctx.page_index, "locator": locator}],
        "continued": False,
        "continuations": [],
    }


# ---------------------------------------------------------------------------
# Block emission / traversal
# ---------------------------------------------------------------------------


class Extractor:
    def __init__(self, page: dict, index: int, rules: dict):
        self.ctx = PageContext(page, index)
        self.source = self.ctx.text
        self.rules = rules
        self.blocks: list[dict] = []
        self.skipped: list[dict] = []
        self._page_block_index = 0
        self._slots: dict[tuple, int] = {}

    # -- helpers --------------------------------------------------------
    def _next_id(self) -> str:
        self._page_block_index += 1
        return f"p{self.ctx.page_index}-b{self._page_block_index:04d}"

    @staticmethod
    def _signature(ancestors: list[dict]) -> tuple:
        return tuple((a.get("tag"), (a.get("hint") or "")[:80]) for a in (ancestors or [])[-3:])

    def _add(self, block: dict) -> dict:
        block.setdefault("block_id", self._next_id())
        block.setdefault("page_index", self.ctx.page_index)
        block.setdefault("attrs_hint", "")
        block.setdefault("ancestors", [])
        flags = block.get("flags") or []
        # Keep flags ordered and unique: 600 nested divs must not produce 600
        # copies of the same diagnostic.
        block["flags"] = list(dict.fromkeys(flags))
        # Deterministic structural position: the ordinal of this block among
        # blocks of the same type under the same ancestor chain.  Used for
        # repeated-template reporting, never for deletion.
        key = (self._signature(block["ancestors"]), block.get("type"))
        block["slot"] = self._slots.get(key, 0)
        self._slots[key] = block["slot"] + 1
        self.blocks.append(block)
        return block

    def _make_block(self, kind: str, text: str, start: int, end: int,
                    text_start: int | None = None, text_end: int | None = None,
                    **extra) -> dict:
        locator, exact = self.ctx.locator(start, end, text_start, text_end)
        flags = list(extra.pop("flags", []))
        if not exact:
            flags.append("locator_inexact")
        if not self.ctx.reliable:
            flags.append("encoding_unreliable")
        if self.ctx.overlaps_undecodable(start, end):
            flags.extend(["undecodable_bytes", "content_incomplete"])
        block = {
            "type": kind,
            "text": text,
            "locator": locator,
            "source": {
                "page": self.ctx.page_index,
                "char_start": locator["char_start"],
                "char_end": locator["char_end"],
                "byte_start": locator["byte_start"],
                "byte_end": locator["byte_end"],
                "raw_sha256": locator["raw_sha256"],
            },
            "flags": flags,
            "confidence": 0.5 if not exact else (0.9 if self.ctx.reliable else 0.7),
        }
        block.update(extra)
        return self._add(block)

    def _flush(self, pending: dict, state: dict) -> None:
        text = pending.get("text") or ""
        if not text.strip():
            pending.clear()
            return
        self._make_block(
            "paragraph", text, pending.get("start", 0), pending.get("end", 0),
            pending.get("text_start"), pending.get("text_end"),
            refs=pending.get("refs") or [],
            attrs_hint=pending.get("attrs_hint", ""),
            ancestors=list(state.get("ancestors") or []),
        )
        pending.clear()

    def _append_text(self, pending: dict, node: Node) -> None:
        if "start" not in pending:
            pending["start"] = node.start
            pending["text_start"] = node.start
        pending["end"] = node.end
        pending["text_end"] = node.end
        pending["text"] = (pending.get("text") or "") + _html.unescape(
            self.source[node.start:node.end])
        pending.setdefault("refs", [])

    # -- dispatch -------------------------------------------------------
    def run(self, root: Node):
        self._visit(root, {"ancestors": [], "list_depth": 0, "list_parent": None,
                           "quote": False, "depth": 0})
        return self.blocks

    def _visit(self, node: Node, state: dict) -> None:
        depth = state.get("depth", 0)
        if depth > MAX_TRAVERSE_DEPTH:
            # Pathological nesting: flatten this subtree instead of recursing
            # further.  ``skip_blocks=False`` keeps table/list cell text so no
            # content disappears; the block is flagged so structure loss is
            # visible downstream.
            text = _collapse(_inline_text(node, self.source, None, skip_blocks=False,
                                          depth=depth + 1))
            if text:
                self._make_block("paragraph", text, node.start, node.end,
                                 ancestors=list(state.get("ancestors") or []),
                                 flags=["depth_limited", "structure_flattened"])
            return
        pending: dict = {}
        for child in node.children:
            if child.is_text:
                self._append_text(pending, child)
                continue
            self._element(child, state, pending)
        self._flush(pending, state)

    def _child_state(self, node: Node, state: dict) -> dict:
        ancestors = list(state.get("ancestors") or [])
        hint = node.hint_text().lower()
        if hint:
            ancestors.append({"tag": node.tag, "hint": hint[:300]})
            if len(ancestors) > MAX_ANCESTORS:
                ancestors = ancestors[-MAX_ANCESTORS:]
        new_state = dict(state)
        new_state["ancestors"] = ancestors
        new_state["depth"] = state.get("depth", 0) + 1
        return new_state

    def _element(self, node: Node, state: dict, pending: dict) -> None:
        tag = node.tag
        if tag in SKIP_CONTENT_TAGS:
            self._flush(pending, state)
            locator, _ = self.ctx.locator(node.start, node.end)
            self.skipped.append({
                "kind": "inactive_template" if tag in ("script", "style", "template") else "skipped_element",
                "tag": tag,
                "locator": locator,
                "reason": f"<{tag}> content is not visible body text",
            })
            return
        if tag == "br":
            if "text" in pending:
                pending["text"] += "\n"
                pending["end"] = node.end
                pending["text_end"] = node.end
            return
        if tag in HEADING_TAGS:
            self._flush(pending, state)
            self._heading(node, state)
            return
        if tag == "pre":
            self._flush(pending, state)
            self._code(node, state)
            return
        if tag == "table":
            self._flush(pending, state)
            self._table(node, state)
            return
        if tag in ("ul", "ol", "menu"):
            self._flush(pending, state)
            self._list(node, state)
            return
        if self._is_footnote_def(node):
            self._flush(pending, state)
            self._footnote(node, state)
            return
        if tag == "p":
            self._flush(pending, state)
            self._paragraph(node, state)
            return
        if tag == "blockquote":
            self._flush(pending, state)
            quote_state = self._child_state(node, state)
            quote_state["quote"] = True
            self._paragraph(node, quote_state)
            return
        if tag == "hr":
            self._flush(pending, state)
            return
        if tag in CONTAINER_TAGS or tag in (
            "dl", "dd", "dt", "figcaption", "caption", "address", "form",
            "fieldset", "legend",
        ):
            self._flush(pending, state)
            self._visit(node, self._child_state(node, state))
            return
        if tag in INLINE_TAGS:
            if "start" not in pending:
                pending["start"] = node.start
                pending["text_start"] = node.start
            text = _inline_text(node, self.source, pending.setdefault("refs", []))
            pending["text"] = (pending.get("text") or "") + text
            pending["end"] = node.end
            pending["text_end"] = node.end
            return
        # Unknown element: block children are traversed, inline content is
        # flattened so an unrecognised wrapper can never hide a paragraph.
        has_block = any(
            not child.is_text and (child.tag in BLOCK_LEVEL or child.tag in CONTAINER_TAGS)
            for child in node.children
        )
        if has_block:
            self._flush(pending, state)
            self._visit(node, self._child_state(node, state))
        else:
            if "start" not in pending:
                pending["start"] = node.start
                pending["text_start"] = node.start
            pending["text"] = (pending.get("text") or "") + _inline_text(
                node, self.source, pending.setdefault("refs", []))
            pending["end"] = node.end
            pending["text_end"] = node.end

    # -- element handlers ----------------------------------------------
    def _heading(self, node: Node, state: dict) -> None:
        text, refs = _direct_inline_text(node, self.source)
        text = _collapse(text)
        if not text:
            return
        self._make_block(
            "heading", text, node.start, node.end, refs=refs,
            level=HEADING_TAGS[node.tag], attrs_hint=node.hint_text(),
            ancestors=list(state.get("ancestors") or []),
            flags=["damaged"] if node.implicit_close else [],
        )

    def _paragraph(self, node: Node, state: dict) -> None:
        # Block children are emitted separately below, so their text is not
        # folded into this paragraph (that would duplicate content and make a
        # block's locator span cover text it does not own).
        text, refs = _direct_inline_text(node, self.source, skip_block_children=True)
        if text.strip():
            self._make_block(
                "paragraph", text, node.start, node.end, refs=refs,
                attrs_hint=node.hint_text(), quote=bool(state.get("quote")),
                ancestors=list(state.get("ancestors") or []),
            )
        for child in node.children:
            if child.is_text or child.tag in INLINE_TAGS or child.tag == "br":
                continue
            self._element(child, state, {})

    def _code(self, node: Node, state: dict) -> None:
        text = _inline_text(node, self.source, None, skip_blocks=False)
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return
        self._make_block("code", text, node.start, node.end,
                         attrs_hint=node.hint_text(),
                         ancestors=list(state.get("ancestors") or []))

    def _list(self, node: Node, state: dict) -> None:
        list_type = "ol" if node.tag == "ol" else "ul"
        depth = state.get("list_depth", 0) + 1
        ordinal = 1
        start_attr = node.attrs.get("start")
        if start_attr and str(start_attr).strip().lstrip("-").isdigit():
            ordinal = int(str(start_attr).strip())
        list_state = self._child_state(node, state)
        for child in node.children:
            if child.is_text:
                if not self.source[child.start:child.end].strip():
                    continue
                # Loose text directly inside a list: keep it, flagged.
                self._make_block(
                    "paragraph", _collapse(_html.unescape(self.source[child.start:child.end])),
                    child.start, child.end, list_depth=depth,
                    ancestors=list(list_state.get("ancestors") or []),
                    flags=["loose_list_text"],
                )
                continue
            if child.tag == "li":
                self._list_item(child, list_state, list_type, depth, ordinal)
                ordinal += 1
            elif child.tag in ("ul", "ol", "menu"):
                self._list(child, list_state)
            else:
                self._element(child, list_state, {})

    def _list_item(self, node: Node, state: dict, list_type: str, depth: int, ordinal: int) -> None:
        text, refs = _direct_inline_text(node, self.source)
        item_state = self._child_state(node, state)
        block = None
        has_nested = any(
            not child.is_text and child.tag in ("ul", "ol", "menu", "table", "p", "div")
            for child in node.children
        )
        if text.strip():
            block = self._make_block(
                "list_item", text, node.start, node.end, refs=refs,
                list_type=list_type, depth=depth, ordinal=ordinal,
                list_parent=state.get("list_parent"),
                attrs_hint=node.hint_text(),
                ancestors=list(item_state.get("ancestors") or []),
                flags=["damaged"] if node.implicit_close else [],
            )
        elif not has_nested:
            block = self._make_block(
                "list_item", "", node.start, node.end,
                list_type=list_type, depth=depth, ordinal=ordinal,
                list_parent=state.get("list_parent"),
                ancestors=list(item_state.get("ancestors") or []),
                flags=["empty_list_item"],
            )
        nested_state = dict(item_state)
        nested_state["list_depth"] = depth
        nested_state["list_parent"] = block["block_id"] if block else state.get("list_parent")
        for child in node.children:
            if child.is_text:
                continue
            if child.tag in ("ul", "ol", "menu"):
                self._list(child, nested_state)
            elif child.tag == "table":
                self._table(child, nested_state)
            elif child.tag in HEADING_TAGS:
                self._heading(child, nested_state)
            else:
                # Paragraphs/divs inside a list item stay attached to it.
                nested_text, nested_refs = _direct_inline_text(child, self.source)
                if nested_text.strip():
                    self._make_block(
                        "paragraph", nested_text, child.start, child.end,
                        refs=nested_refs, list_depth=depth,
                        list_parent=block["block_id"] if block else None,
                        ancestors=list(nested_state.get("ancestors") or []),
                        flags=["list_continuation"],
                    )
                for grandchild in child.children:
                    if grandchild.is_text or grandchild.tag in INLINE_TAGS:
                        continue
                    if grandchild.tag in (BLOCK_LEVEL | CONTAINER_TAGS):
                        self._element(grandchild, nested_state, {})

    def _table(self, node: Node, state: dict) -> None:
        block_id = self._next_id()
        table = parse_table(node, self.source, self.ctx, self.rules, block_id)
        block = self._make_block(
            "table", table.get("caption") or "", node.start, node.end,
            table=table, attrs_hint=node.hint_text(),
            ancestors=list(state.get("ancestors") or []),
            flags=list(table.get("damage") or []),
        )
        table["table_id"] = block["block_id"]

    def _is_footnote_def(self, node: Node) -> bool:
        attrs = node.attrs
        id_value = attrs.get("id", "")
        role = attrs.get("role", "").lower()
        epub_type = attrs.get("epub:type", "").lower()
        hint = node.hint_text()
        if role in ("doc-footnote", "doc-endnote"):
            return True
        if "footnote" in epub_type or "endnote" in epub_type:
            return True
        if id_value and FOOTNOTE_ID_RE.match(id_value):
            return True
        if not FOOTNOTE_ATTR_RE.search(hint):
            return False
        if node.tag not in ("li", "div", "p", "aside", "section", "dd"):
            return False
        # A *container* named "footnotes" that holds individual footnote
        # definitions is traversed, not flattened into one block: merging them
        # would destroy which id each note belongs to.
        for child in node.children:
            if not child.is_text and self._is_leaf_footnote(child):
                return False
        return True

    def _is_leaf_footnote(self, node: Node) -> bool:
        if node.is_text:
            return False
        if node.attrs.get("id") and FOOTNOTE_ID_RE.match(node.attrs.get("id", "")):
            return True
        role = node.attrs.get("role", "").lower()
        if role in ("doc-footnote", "doc-endnote"):
            return True
        return node.tag in ("p", "li", "dd") and bool(FOOTNOTE_ATTR_RE.search(node.hint_text()))

    def _footnote(self, node: Node, state: dict) -> None:
        text, refs = _direct_inline_text(node, self.source)
        text = _collapse(text)
        marker = None
        id_value = node.attrs.get("id", "")
        match = FOOTNOTE_ID_RE.match(id_value) if id_value else None
        if match:
            marker = match.group(1)
        if marker is None and text:
            leading = re.match(r"\s*\[?([0-9]{1,3}|[*†‡])\]?", text)
            if leading:
                marker = leading.group(1)
        parent = state.get("list_parent")
        # Container-style footnotes: emit each definition separately so ids and
        # markers stay attached to the right note.
        if any(not child.is_text and self._is_leaf_footnote(child) for child in node.children):
            for child in node.children:
                if child.is_text or not self._is_leaf_footnote(child):
                    continue
                self._footnote(child, state)
            return
        if not text:
            for child in node.children:
                if child.is_text or child.tag not in ("p", "div", "li", "dd"):
                    continue
                nested_text, nested_refs = _direct_inline_text(child, self.source)
                if nested_text.strip():
                    self._make_block(
                        "footnote", _collapse(nested_text), child.start, child.end,
                        refs=nested_refs, footnote_id=id_value or None, marker=marker,
                        attrs_hint=node.hint_text(), list_parent=parent,
                        ancestors=list(state.get("ancestors") or []),
                    )
                    return
            return
        self._make_block(
            "footnote", text, node.start, node.end, refs=refs,
            footnote_id=id_value or None, marker=marker,
            attrs_hint=node.hint_text(), list_parent=parent,
            ancestors=list(state.get("ancestors") or []),
            flags=["damaged"] if node.implicit_close else [],
        )


# ---------------------------------------------------------------------------
# Plain text fallback
# ---------------------------------------------------------------------------


def _extract_plain_text(ctx: PageContext) -> list[dict]:
    blocks = []
    text = ctx.text
    cursor = 0
    pattern = re.compile(r"\n[ \t\r\f\v]*\n+")
    pieces = []
    while True:
        match = pattern.search(text, cursor)
        if not match:
            pieces.append((cursor, len(text)))
            break
        pieces.append((cursor, match.start()))
        cursor = match.end()
    for start, end in pieces:
        raw = text[start:end]
        if not raw.strip():
            continue
        locator, exact = ctx.locator(start, end)
        flags = []
        if not exact:
            flags.append("locator_inexact")
        if not ctx.reliable:
            flags.append("encoding_unreliable")
        if ctx.overlaps_undecodable(start, end):
            flags.extend(["undecodable_bytes", "content_incomplete"])
        blocks.append({
            "block_id": f"p{ctx.page_index}-t{len(blocks) + 1:04d}",
            "type": "paragraph",
            "text": raw,
            "locator": locator,
            "source": {
                "page": ctx.page_index,
                "char_start": locator["char_start"],
                "char_end": locator["char_end"],
                "byte_start": locator["byte_start"],
                "byte_end": locator["byte_end"],
                "raw_sha256": locator["raw_sha256"],
            },
            "flags": flags,
            "confidence": 0.8 if exact else 0.5,
            "attrs_hint": "",
            "ancestors": [],
        })
    return blocks


# ---------------------------------------------------------------------------
# Footnote resolution
# ---------------------------------------------------------------------------


def resolve_footnotes(blocks: list[dict], rules: dict | None = None) -> dict:
    if rules is not None and not rules.get("link_footnotes", True):
        # Linking disabled: definitions are still emitted as footnote blocks,
        # references are left unannotated rather than silently resolved.
        return {"definitions": sum(1 for b in blocks if b.get("type") == "footnote"),
                "linked": 0, "unresolved": 0, "by_marker": {}, "disabled": True}
    definitions: dict[str, dict] = {}
    for block in blocks:
        if block.get("type") != "footnote":
            continue
        keys = []
        if block.get("marker") is not None:
            keys.append(str(block["marker"]))
        fid = block.get("footnote_id")
        if fid:
            keys.append(str(fid).lstrip("#"))
            match = FOOTNOTE_ID_RE.match(str(fid))
            if match:
                keys.append(match.group(1))
        for key in keys:
            definitions.setdefault(key, block)

    linked = 0
    unresolved = 0
    for block in blocks:
        if block.get("type") == "footnote":
            continue
        refs = [r for r in (block.get("refs") or []) if r.get("kind") != "nested_block"]
        text = block.get("text") or ""
        if text:
            for match in BRACKET_REF_RE.finditer(text[:MAX_REF_SCAN]):
                refs.append({"kind": "bracket", "marker": match.group(1).strip(),
                             "offset": match.start(), "length": len(match.group(0))})
        resolved_refs = []
        for ref in refs:
            target = None
            marker = str(ref.get("marker", "")).strip()
            href = ref.get("href")
            if href and str(href).startswith("#"):
                target = definitions.get(str(href).lstrip("#"))
            if target is None and marker:
                target = (definitions.get(marker)
                          or definitions.get(f"fn{marker}")
                          or definitions.get(f"footnote{marker}"))
            ref = dict(ref)
            if target is not None:
                ref["target_block_id"] = target.get("block_id")
                ref["target_footnote_id"] = target.get("footnote_id")
                ref["resolved"] = True
                resolved_refs.append(ref)
                linked += 1
                target.setdefault("referenced_by", [])
                if block.get("block_id") not in target["referenced_by"]:
                    target["referenced_by"].append(block.get("block_id"))
            elif ref.get("kind") == "bracket" or (href and str(href).startswith("#")):
                # A footnote-shaped reference that has no definition is
                # reported, not dropped.
                ref["resolved"] = False
                resolved_refs.append(ref)
                unresolved += 1
            else:
                # Ordinary hyperlink: not a footnote reference.
                resolved_refs.append(ref)
        block["refs"] = resolved_refs

    return {
        "definitions": len({id(v) for v in definitions.values()}),
        "linked": linked,
        "unresolved": unresolved,
        "by_marker": {k: v.get("block_id") for k, v in definitions.items()},
    }


# ---------------------------------------------------------------------------
# Cross-page table stitching
# ---------------------------------------------------------------------------


def _can_continue(previous: dict, following: dict) -> tuple[bool, str]:
    a = previous.get("table")
    b = following.get("table")
    if not a or not b:
        return False, "not_tables"
    if not a.get("columns") or a.get("columns") != b.get("columns"):
        return False, "column_mismatch"
    caption_b = (b.get("caption") or "").strip()
    if caption_b and CONTINUED_RE.search(caption_b):
        return True, "continued_caption"
    if caption_b:
        # A genuinely new caption means a genuinely new table.
        return False, "different_caption"
    header_a = a.get("header_rows") or []
    header_b = b.get("header_rows") or []
    if header_a and header_b:
        ra = a["rows"][header_a[0]] if header_a[0] < len(a["rows"]) else []
        rb = b["rows"][header_b[0]] if header_b[0] < len(b["rows"]) else []
        if ra and ra == rb:
            return True, "repeated_header"
        return False, "header_mismatch"
    if (a.get("caption") or "").strip():
        # A captioned table whose continuation repeats no caption and no
        # header is ambiguous; do not merge it silently.
        return False, "ambiguous_caption"
    return True, "page_break_aligned_table"


def _is_page_furniture(block: dict) -> bool:
    """Running headers/footers must not break a cross-page table join."""
    if block.get("type") == "footnote":
        return True
    hints = " ".join(str(a.get("hint") or "") for a in (block.get("ancestors") or []))
    hints += " " + str(block.get("attrs_hint") or "")
    lowered = hints.lower()
    for token in ("footer", "header", "page-number", "pagination", "pager",
                  "copyright", "running-head", "runninghead", "页脚", "页码"):
        if token in lowered:
            return True
    return False


def stitch_tables(blocks: list[dict], rules: dict) -> list[dict]:
    """Merge a table continued across a page break.

    The continuation block is retained as ``table_continuation`` (with its own
    locator) and linked from the merged table's ``parts``.
    """
    if not rules.get("stitch_tables", True):
        return blocks
    by_page: dict[int, list[dict]] = {}
    for block in blocks:
        by_page.setdefault(block.get("page_index", 0), []).append(block)

    content_types = ("heading", "paragraph", "list_item", "code")
    for page in sorted(by_page):
        next_blocks = by_page.get(page + 1)
        if not next_blocks:
            continue
        current_blocks = by_page[page]
        prev = None
        for block in reversed(current_blocks):
            if _is_page_furniture(block):
                continue
            if block.get("type") == "table":
                prev = block
                break
            if block.get("type") in content_types:
                break
        if prev is None:
            continue
        nxt = None
        for block in next_blocks:
            if _is_page_furniture(block):
                continue
            if block.get("type") == "table":
                nxt = block
                break
            if block.get("type") in content_types:
                break
        if nxt is None:
            continue
        ok, reason = _can_continue(prev, nxt)
        if not ok:
            continue
        a = prev["table"]
        b = nxt["table"]
        if a.get("continued_into"):
            continue
        start_row = len(a["grid"])
        # A repeated header on the continuation page is a layout artefact: it
        # is recorded once (`repeated_header_rows`) and not duplicated as data.
        repeated_indices: set[int] = set()
        if reason == "repeated_header":
            repeated_indices = set(range(len(b.get("header_rows") or [])))
        retained_rows = [(index, row) for index, row in enumerate(b["grid"])
                         if index not in repeated_indices]
        merged_grid = [list(row) for row in a["grid"]]
        offset = {}
        for index, row in retained_rows:
            offset[index] = len(merged_grid)
            merged_grid.append([
                dict(cell, r=len(merged_grid),
                     origin={"r": len(merged_grid), "c": cell["origin"]["c"]})
                for cell in row
            ])
        a["grid"] = merged_grid
        a["rows"] = ([list(row) for row in a["rows"]]
                     + [list(row) for row in b["rows"]][len(repeated_indices):])
        a["cells"] = a.get("cells", []) + [
            dict(cell, r=offset[cell["r"]],
                 origin={"r": offset[cell["r"]], "c": cell["origin"]["c"]},
                 part=len(a.get("parts") or []))
            for cell in b.get("cells", [])
            if cell["r"] in offset
        ]
        for cell in a.get("cells", []):
            cell.setdefault("part", 0)
        a["spans"] = a.get("spans", []) + [
            dict(span, r=offset[span["r"]],
                 origin={"r": offset[span["r"]], "c": span["origin"]["c"]})
            for span in b.get("spans", [])
            if span["r"] in offset
        ]
        repeated = len(repeated_indices)
        a["header_rows"] = sorted(set(a.get("header_rows") or []))
        a.setdefault("parts", []).extend(b.get("parts") or [])
        a.setdefault("continuations", []).append({
            "block_id": nxt.get("block_id"),
            "page": nxt.get("page_index"),
            "locator": nxt.get("locator"),
            "reason": reason,
            "repeated_header_rows": repeated,
        })
        a["continued"] = True
        a["continued_into"] = nxt.get("block_id")
        prev["flags"] = sorted(set(prev.get("flags", []) + ["stitched_table"]))
        nxt["type"] = "table_continuation"
        nxt["stitched_into"] = prev.get("block_id")
        nxt["continuation_reason"] = reason
        nxt.setdefault("flags", []).append("stitched_source")
    return blocks


# ---------------------------------------------------------------------------
# Page / document entry points
# ---------------------------------------------------------------------------


def extract_page(page: dict, index: int, rules: dict) -> dict:
    text = page.get("text")
    if text is None:
        text = page.get("html") or ""
    ctx = PageContext(page, index)
    tokens = tokenize(text)
    has_element = any(token.kind in ("start", "end") for token in tokens)
    if not has_element:
        blocks = _extract_plain_text(ctx)
        skipped, recovered = [], []
    else:
        extractor = Extractor(page, index, rules)
        root, recovered = build_tree(text, tokens)
        blocks = extractor.run(root)
        skipped = extractor.skipped
        starts = [block["locator"]["char_start"] for block in blocks]
        for event in recovered:
            loc, _ = ctx.locator(event["char_start"], event["char_end"])
            event["locator"] = loc
            index_after = bisect_left(starts, event["char_start"])
            if index_after < len(blocks):
                flags = blocks[index_after].setdefault("flags", [])
                if "after_markup_recovery" not in flags:
                    flags.append("after_markup_recovery")
    return {"index": index, "page": page, "blocks": blocks,
            "skipped": skipped, "recovered": recovered}


def process(record):
    """Pipeline stage: recover blocks, tables, lists and footnote links."""
    if not isinstance(record, dict) or record.get("kind") != "document":
        return record
    rules = rules_for(record.get("rules_version"))
    out = dict(record)
    pages = out.get("pages") or []
    all_blocks: list[dict] = []
    page_meta = []
    skipped = []
    recovered = []
    for index, page in enumerate(pages):
        result = extract_page(page, index, rules)
        for block in result["blocks"]:
            block["order"] = len(all_blocks)
            all_blocks.append(block)
        skipped.extend(result["skipped"])
        page_number = result["page"].get("page_index", index + 1)
        for item in result["recovered"]:
            item["page"] = page_number
            recovered.append(item)
        page_meta.append({
            "index": index,
            "page_index": page_number,
            "raw_sha256": result["page"].get("raw_hash"),
            "byte_length": result["page"].get("byte_length"),
            "chars": len(result["page"].get("text") or ""),
            "blocks": len(result["blocks"]),
            "skipped": len(result["skipped"]),
            "recovered": len(result["recovered"]),
            "encoding_reliable": (result["page"].get("encoding") or {}).get("reliable", False),
        })

    all_blocks = stitch_tables(all_blocks, rules)
    footnotes = resolve_footnotes(all_blocks, rules)

    out["blocks"] = all_blocks
    out["tables"] = [b["table"] for b in all_blocks
                     if b.get("type") == "table" and b.get("table")]
    out["footnotes"] = footnotes
    out["skipped"] = skipped
    out["recovered"] = recovered
    out["pages_meta"] = page_meta
    return out
