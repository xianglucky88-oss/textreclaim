"""Tests for lossless normalisation and conservative noise removal."""
from src.textreclaim import clean, encoding, extract, normalize


def _normalize(text, profile="prose", rules=None):
    return normalize.normalize_text(text, profile, rules)


def test_fullwidth_alphanumerics_fold_but_punctuation_and_units_survive():
    text = "版本２．０，温度１００％ 与 10 kg，价格￥９９。"
    out, changes = _normalize(text)
    assert "2.0" in out or "２" not in out
    assert "１００" not in out
    assert "，" in out and "。" in out        # CJK punctuation unchanged
    assert "10 kg" in out                    # units and digits untouched
    assert "￥" in out                       # currency sign untouched
    assert any(change["kind"] == "width_fold" for change in changes)


def test_nfkc_compatibility_rewrites_do_not_happen():
    # ① (U+2460) and ﬁ (U+FB01) would become "1" and "fi" under NFKC.
    text = "① 参见 ﬁle"
    out, _ = _normalize(text)
    assert "①" in out
    assert "ﬁ" in out


def test_code_profile_is_byte_identical():
    text = "def f（x）:\n\treturn x  # 全角括号\n"
    out, changes = _normalize(text, "code")
    assert out == text
    assert changes == []


def test_whitespace_collapse_keeps_short_paragraphs():
    out, _ = _normalize("  短  \n\n  文本  ")
    assert out == "短\n\n文本"


def test_normalize_stage_leaves_code_blocks_alone():
    html = "<body><pre>for (i=0;i&lt;10;i++){}</pre><p>正文  多空格。</p></body>"
    page = encoding.decode_page({"page_index": 1, "raw": html.encode()})
    record = extract.process({"kind": "document", "pages": [page]})
    record = normalize.process(record)
    code = next(block for block in record["blocks"] if block["type"] == "code")
    assert code["text"] == "for (i=0;i<10;i++){}"


def test_normalize_tables_without_changing_columns_or_spans():
    html = ("<table><tr><th colspan='2'>标题</th></tr>"
            "<tr><td> １２ </td><td> 10 kg </td></tr></table>")
    page = encoding.decode_page({"page_index": 1, "raw": html.encode()})
    record = extract.process({"kind": "document", "pages": [page]})
    before = record["tables"][0]
    record = normalize.process(record)
    after = record["tables"][0]
    assert after["columns"] == before["columns"]
    assert [span["colspan"] for span in after["spans"]] == [2]
    assert after["rows"][0] == ["标题", ""]
    assert after["rows"][1] == ["12", "10 kg"]


def test_low_confidence_template_is_retained_with_score():
    html = ("<body><div class='sidebar'><p>相关阅读</p></div>"
            "<article class='post-content'><p>正文事实。</p></article></body>")
    page = encoding.decode_page({"page_index": 1, "raw": html.encode()})
    record = clean.process(extract.process({"kind": "document", "pages": [page]}))
    sidebar = [b for b in record["blocks"] if "相关阅读" in b["text"]][0]
    assert not sidebar.get("noise_removed")
    assert sidebar["noise_score"] > 0
    assert sidebar["noise_class"] in ("template_low_confidence", "content")


def test_short_body_text_is_never_deleted():
    html = ("<body><article class='post-content'><p>是。</p>"
            "<p>否。</p><p>这是一段正常长度的正文。</p></article></body>")
    page = encoding.decode_page({"page_index": 1, "raw": html.encode()})
    record = clean.process(extract.process({"kind": "document", "pages": [page]}))
    texts = [block["text"] for block in record["blocks"] if not block.get("noise_removed")]
    assert "是。" in texts
    assert "否。" in texts


def test_repeated_body_text_is_kept_everywhere():
    pages_html = [
        "<body><article class='post-content'><p>重复的正文句子。</p><p>独有 {0}。</p></article></body>".format(i)
        for i in range(3)
    ]
    pages = [encoding.decode_page({"page_index": i + 1, "raw": html.encode()})
             for i, html in enumerate(pages_html)]
    record = clean.process(extract.process({"kind": "document", "pages": pages}))
    repeated = [b for b in record["blocks"] if b["text"] == "重复的正文句子。"]
    assert len(repeated) == 3
    assert all(not block.get("noise_removed") for block in repeated)


def test_high_confidence_template_is_removed_but_recoverable():
    pages_html = [
        ("<body><nav class='site-nav'><a href='/'>首页</a><a href='/a'>产品</a>"
         "<a href='/b'>关于</a><a href='/c'>联系</a><a href='/d'>帮助</a></nav>"
         "<article class='post-content'><p>正文 {0}。</p></article>"
         "<footer class='site-footer'>版权所有 2026 某某公司</footer></body>").format(i)
        for i in range(3)
    ]
    pages = [encoding.decode_page({"page_index": i + 1, "raw": html.encode()})
             for i, html in enumerate(pages_html)]
    record = clean.process(extract.process({"kind": "document", "pages": pages}))
    navs = [b for b in record["blocks"] if "首页" in b["text"]]
    assert navs and navs[0]["noise_class"] == "template_first_occurrence"
    assert [b.get("noise_removed") for b in navs] == [False, True, True]
    # A footer with only moderate evidence is *kept* and annotated instead of
    # being deleted; only the strong navigation signal clears the threshold.
    footers = [b for b in record["blocks"] if "版权所有" in b["text"]]
    assert footers and all(not b["noise_removed"] for b in footers)
    assert all(b["noise_class"] == "template_low_confidence" for b in footers)
    # Removed blocks stay in the output with locators for review.
    assert len(record["removed"]) == 2
    for block in record["removed"]:
        assert block["locator"]["byte_start"] is not None


def test_footnote_and_table_are_protected_from_removal():
    html = ("<body><nav class='site-nav'><a href='/'>a</a><a href='/b'>b</a><a href='/c'>c</a>"
            "<a href='/d'>d</a><a href='/e'>e</a></nav>"
            "<table><tr><td>1</td></tr></table>"
            "<div class='footnotes'><p id='fn1'>[1] 注。</p></div></body>")
    page = encoding.decode_page({"page_index": 1, "raw": html.encode()})
    record = clean.process(extract.process({"kind": "document", "pages": [page]}))
    for block in record["blocks"]:
        if block["type"] in ("table", "footnote"):
            assert block["noise_class"] == "protected"
            assert not block.get("noise_removed")


def test_rules_version_changes_are_explicit():
    from src.textreclaim import config

    v1 = config.rules_for("2026.11.1")
    v2 = config.rules_for("2026.11.2")
    assert config.rules_fingerprint(v1) != config.rules_fingerprint(v2)
    assert v2["width_fold"] is False


def test_rules_are_copied_not_shared():
    from src.textreclaim import config

    first = config.rules_for("2026.11.1")
    first["width_fold"] = False
    second = config.rules_for("2026.11.1")
    assert second["width_fold"] is True


def test_unknown_rules_version_is_an_error():
    import pytest

    from src.textreclaim import config

    with pytest.raises(KeyError):
        config.rules_for("no-such-version")
