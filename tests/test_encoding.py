"""Tests for encoding recovery and the character/byte source mapping."""
import codecs

from src.textreclaim import encoding


def _page(raw, **kw):
    return encoding.decode_page({"page_index": 1, "raw": raw, **kw})


def test_bom_wins_over_conflicting_header():
    body = "<html><body><p>中文内容</p></body></html>"
    raw = codecs.BOM_UTF16_LE + body.encode("utf-16-le")
    page = _page(raw, headers={"Content-Type": "text/html; charset=iso-8859-1"})
    meta = page["encoding"]
    assert meta["chosen"] == "utf-16-le"
    assert meta["bom"] == "utf-16-le"
    assert "中文内容" in page["text"]
    kinds = {conflict["kind"] for conflict in meta["conflicts"]}
    assert "bom_vs_header" in kinds
    assert any(c["resolution"] == "bom_wins" for c in meta["conflicts"])


def test_declared_utf8_but_gbk_bytes_is_repaired_with_evidence():
    body = ("<html><head><meta charset='utf-8'></head><body>"
            "<p>中文正文测试，这是很长的一段中文内容。</p></body></html>")
    page = _page(body.encode("gbk"))
    meta = page["encoding"]
    assert meta["chosen"] == "gb18030"
    assert "中文正文测试" in page["text"]
    assert any(c["kind"] == "misdeclared_encoding" for c in meta["conflicts"])
    # The wrong declaration is preserved in the evidence record.
    assert any(e["source"] == "meta" and e["declared"] == "utf-8" for e in meta["evidence"])


def test_undecodable_bytes_are_explicit_not_hidden():
    raw = b"<p>" + "中文".encode() + b"\xff" + "更多内容".encode() + b"</p>"
    page = _page(raw, declared_encoding="utf-8")
    meta = page["encoding"]
    assert meta["chosen"] == "utf-8"
    assert meta["reliable"] is False
    assert len(meta["undecodable"]) == 1
    entry = meta["undecodable"][0]
    assert raw[entry["byte_start"]:entry["byte_end"]] == b"\xff"
    assert entry["preview_hex"] == "ff"


def test_mostly_ascii_page_is_not_relabelled_by_one_bad_byte():
    raw = b"<html><body><p>hello world</p>" + b"\xed\xa0\x80" + b"<p>more</p></body></html>"
    page = _page(raw, declared_encoding="utf-8")
    assert page["encoding"]["chosen"] == "utf-8"
    assert page["encoding"]["reliable"] is False
    assert page["encoding"]["undecodable"]


def test_cp1252_page_without_declaration():
    raw = "<p>Café crème — naïve</p>".encode("cp1252")
    page = _page(raw)
    assert page["encoding"]["chosen"] == "cp1252"
    assert "Café crème" in page["text"]
    assert page["encoding"]["reliable"] is True


def test_gbk_page_without_declaration_is_sniffed():
    raw = "<p>中文正文测试这是很长的一段中文内容</p>".encode("gbk")
    page = _page(raw)
    assert page["encoding"]["chosen"] == "gb18030"
    assert "中文正文测试" in page["text"]


def test_byte_mapping_is_exact_across_multibyte_and_surrogates():
    body = "<p>abc中文def</p><p>second</p>"
    raw = body.encode("utf-8")
    page = _page(raw)
    mapper = page["_byte_map"]
    for needle in ("abc", "中文", "def", "second"):
        index = page["text"].index(needle)
        byte_index, exact = mapper.byte_at(index)
        assert exact is True
        assert raw[byte_index:byte_index + len(needle.encode())] == needle.encode()

    text = "<p>a😀b</p>"
    raw16 = codecs.BOM_UTF16_BE + text.encode("utf-16-be")
    page = _page(raw16)
    mapper = page["_byte_map"]
    for needle in ("a", "😀", "b"):
        index = page["text"].index(needle)
        byte_index, exact = mapper.byte_at(index)
        assert exact is True
        assert raw16[byte_index:byte_index + len(needle.encode("utf-16-be"))] == needle.encode("utf-16-be")


def test_line_and_column_are_one_based():
    raw = b"<p>one</p>\n<p>two</p>"
    page = _page(raw)
    mapper = page["_byte_map"]
    index = page["text"].index("two")
    line, column = mapper.line_col(index)
    assert line == 2
    assert column == 3


def test_unknown_charset_label_is_recorded_but_not_fatal():
    raw = "<p>hello there</p>".encode()
    page = _page(raw, headers={"Content-Type": "text/html; charset=made-up-charset"})
    assert page["encoding"]["chosen"] == "utf-8"
    assert any(e["valid_label"] is False for e in page["encoding"]["evidence"])


def test_broken_page_does_not_stop_the_document():
    out = encoding.process({
        "kind": "document",
        "pages": [
            {"page_index": 1, "raw": b"<p>good</p>"},
            {"page_index": 2, "content_b64": "!!!not base64!!!"},
            {"page_index": 3, "raw": b"<p>also good</p>"},
        ],
    })
    assert out["pages"][0]["text"] == "<p>good</p>"
    assert out["pages"][2]["text"] == "<p>also good</p>"
    assert out["pages"][1]["encoding"]["reason"] == "page_error"
    assert out["pages"][1]["encoding"]["reliable"] is False
