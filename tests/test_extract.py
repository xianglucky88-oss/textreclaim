"""Tests for structure recovery: damaged markup, tables, lists, footnotes."""
from src.textreclaim import clean, encoding, extract


def _extract(html, pages=None):
    pages = pages or [html]
    decoded = [
        encoding.decode_page({"page_index": index + 1, "raw": page.encode()})
        for index, page in enumerate(pages)
    ]
    return extract.process({"kind": "document", "id": "t", "pages": decoded})


def _texts(result, kind=None):
    return [block["text"] for block in result["blocks"]
            if kind is None or block["type"] == kind]


def test_unclosed_paragraphs_and_divs_do_not_swallow_the_page():
    html = ("<html><body><article>"
            "<p>First paragraph"
            "<div>nested div text</div> after"
            "<p>Second paragraph"
            "<table><tr><td>a</td><td>b</td></tr></table>"
            "<p>After table paragraph.</p>"
            "</article></body></html>")
    result = _extract(html)
    texts = "\n".join(_texts(result))
    assert "First paragraph" in texts
    assert "nested div text" in texts
    assert "Second paragraph" in texts
    assert "After table paragraph." in texts
    assert result["tables"][0]["rows"] == [["a", "b"]]


def test_unterminated_comment_does_not_eat_following_content():
    html = "<p>before</p><!-- never closed <p>after broken comment</p><p>final</p>"
    result = _extract(html)
    texts = _texts(result)
    assert "before" in texts
    assert "after broken comment" in texts
    assert "final" in texts


def test_stray_end_tag_is_recorded_not_fatal():
    result = _extract("<div>a</div></span><p>b</p>")
    assert any(event["kind"] == "stray_end" for event in result["recovered"])
    assert "b" in _texts(result)


def test_pathological_nesting_is_bounded_and_flagged():
    html = ("<div>" * 400) + "<p>deep body</p>" + ("</div>" * 400) + "<p>tail</p>"
    result = _extract(html)
    assert "deep body" in "\n".join(_texts(result))
    assert "tail" in _texts(result)


def test_merged_cells_and_column_relationships():
    html = ("<table><thead><tr><th colspan='2'>Merged head</th></tr></thead>"
            "<tbody><tr><td rowspan='2'>1</td><td>2</td></tr>"
            "<tr><td>3</td></tr></tbody></table>")
    result = _extract(html)
    table = result["tables"][0]
    assert table["columns"] == 2
    assert table["rows"] == [["Merged head", ""], ["1", "2"], ["", "3"]]
    assert {(span["r"], span["c"], span["rowspan"], span["colspan"]) for span in table["spans"]} == {
        (0, 0, 1, 2), (1, 0, 2, 1)
    }
    # The covered cell keeps pointing at its origin so the column relation is
    # recoverable.
    covered = table["grid"][2][0]
    assert covered["synthesized"] is True
    assert covered["origin"] == {"r": 1, "c": 0}


def test_missing_cells_are_placeholders_not_merged():
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    table = _extract(html)["tables"][0]
    assert table["rows"] == [["a", "b", "c"], ["d", "", ""]]
    assert table["grid"][1][1].get("missing") is True


def test_absurd_spans_are_clamped_and_flagged():
    html = "<table><tr><td rowspan='99999'>x</td><td>y</td></tr></table>"
    table = _extract(html)["tables"][0]
    assert "span_clamped" in table["damage"]
    assert len(table["grid"]) < 2000


def test_nested_lists_keep_parent_and_depth():
    html = "<ul><li>one</li><li>two<ol><li>nested a</li><li>nested b</li></ol></li><li>three</li></ul>"
    result = _extract(html)
    items = [b for b in result["blocks"] if b["type"] == "list_item"]
    assert [item["text"] for item in items] == ["one", "two", "nested a", "nested b", "three"]
    assert items[0]["depth"] == 1
    assert items[2]["depth"] == 2
    assert items[2]["list_parent"] == items[1]["block_id"]
    assert items[2]["list_type"] == "ol"


def test_footnote_reference_links_to_definition():
    html = ("<article><p>Body text with a note<a href='#fn1'>1</a> and [2].</p></article>"
            "<div class='footnotes'><p id='fn1'>[1] First note.</p>"
            "<p id='fn2'>[2] Second note.</p></div>")
    result = _extract(html)
    notes = {note["text"]: note for note in result["blocks"] if note["type"] == "footnote"}
    paragraph = next(b for b in result["blocks"] if b["type"] == "paragraph")
    resolved = {ref.get("marker") or ref.get("href"): ref for ref in paragraph["refs"]}
    assert resolved["#fn1"]["resolved"] is True
    assert resolved["#fn1"]["target_block_id"] == notes["[1] First note."]["block_id"]
    assert resolved["2"]["resolved"] is True
    assert result["footnotes"]["linked"] == 2
    assert result["footnotes"]["unresolved"] == 0


def test_unresolved_footnote_is_reported_not_dropped():
    result = _extract("<p>Text with [7] but no definition.</p>")
    paragraph = next(b for b in result["blocks"] if b["type"] == "paragraph")
    ref = paragraph["refs"][0]
    assert ref["resolved"] is False
    assert result["footnotes"]["unresolved"] == 1


def test_script_and_style_are_skipped_with_locators():
    html = "<body><script>var x = '<p>fake</p>';</script><style>p{}</style><p>real</p></body>"
    result = _extract(html)
    assert _texts(result) == ["real"]
    assert {item["tag"] for item in result["skipped"]} == {"script", "style"}
    for item in result["skipped"]:
        assert item["locator"]["byte_start"] is not None


def test_cross_page_table_stitching_keeps_both_locators():
    first = ("<html><body><article class='post-content'>"
             "<table><thead><tr><th>地区</th><th>数量</th></tr></thead>"
             "<tbody><tr><td>华东</td><td>12</td></tr></tbody></table>"
             "</article><footer class='site-footer'>第 1 页</footer></body></html>")
    second = ("<html><body><header class='site-header'>站点</header>"
              "<table><thead><tr><th>地区</th><th>数量</th></tr></thead>"
              "<tbody><tr><td>华北</td><td>56</td></tr></tbody></table>"
              "</body></html>")
    result = _extract(first, pages=[first, second])
    table = result["tables"][0]
    assert table["rows"] == [["地区", "数量"], ["华东", "12"], ["华北", "56"]]
    assert [part["page"] for part in table["parts"]] == [1, 2]
    continuation = next(b for b in result["blocks"] if b["type"] == "table_continuation")
    assert continuation["stitched_into"] == table["table_id"]
    assert continuation["locator"]["page"] == 2


def test_tables_with_different_columns_are_not_stitched():
    first = "<table><tr><td>a</td><td>b</td></tr></table>"
    second = "<table><tr><td>c</td><td>d</td><td>e</td></tr></table>"
    result = _extract(first, pages=[first, second])
    assert len(result["tables"]) == 2


def test_table_is_never_noise_removed():
    html = ("<nav class='site-nav'><a href='/'>a</a><a href='/x'>b</a><a href='/y'>c</a>"
            "<a href='/z'>d</a><a href='/w'>e</a></nav>"
            "<table><tr><td>12</td><td>34</td></tr></table>")
    result = _extract(html)
    result = clean.process(result)
    table = next(b for b in result["blocks"] if b["type"] == "table")
    assert not table.get("noise_removed")
    assert table["noise_class"] == "protected"


def test_plain_text_input_falls_back_to_paragraph_blocks():
    result = _extract("first line\nstill first\n\nsecond paragraph\n\nthird")
    assert [block["text"] for block in result["blocks"]] == [
        "first line\nstill first", "second paragraph", "third"
    ]


def test_implicitly_closed_elements_have_bounded_spans():
    result = _extract("<ul><li>one<li>two</ul><p>x</p>")
    items = [b for b in result["blocks"] if b["type"] == "list_item"]
    assert [item["text"] for item in items] == ["one", "two"]
    # The first item is marked damaged and must not extend over the second.
    assert items[0]["flags"] == ["damaged"]
    assert items[0]["locator"]["char_end"] <= items[1]["locator"]["char_start"]
    assert items[0]["locator"]["char_end"] - items[0]["locator"]["char_start"] < 16


def test_block_children_are_not_duplicated_into_container_text():
    result = _extract("<blockquote><p>引用第一段。</p><p>引用第二段。</p></blockquote>")
    assert [b["text"] for b in result["blocks"]] == ["引用第一段。", "引用第二段。"]
    assert all(b["quote"] is True for b in result["blocks"])
