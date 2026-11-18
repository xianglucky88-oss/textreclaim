"""End-to-end tests: identity, atomic persistence, reproducibility, limits."""
import base64
import json

import pytest

from src.textreclaim import pipeline, store

PAGE_ONE = """<html><head><meta charset='utf-8'></head><body>
<nav class='site-nav'><a href='/'>首页</a><a href='/a'>栏目</a><a href='/b'>关于</a>
<a href='/c'>联系</a><a href='/d'>帮助</a></nav>
<article class='post-content'><h1>销量分析</h1>
<p>这是正文第一段，数值 １２３ 与单位 10 kg。</p>
<table><thead><tr><th>地区</th><th>数量</th></tr></thead>
<tbody><tr><td>华东</td><td>12</td></tr></tbody></table>
</article></body></html>"""

PAGE_TWO = """<html><head><meta charset='utf-8'></head><body>
<header class='site-header'>站点名称</header>
<table><thead><tr><th>地区</th><th>数量</th></tr></thead>
<tbody><tr><td>华北</td><td>56</td></tr></tbody></table>
<article class='post-content'><p>正文续段[1]。</p></article>
<div class='footnotes'><p id='fn1'>[1] 脚注说明。</p></div>
</body></html>"""


def _doc(doc_id="doc-1", pages=(PAGE_ONE, PAGE_TWO), **extra):
    record = {
        "id": doc_id,
        "kind": "document",
        "pages": [
            {"page_index": index + 1, "content_b64": base64.b64encode(page.encode()).decode()}
            for index, page in enumerate(pages)
        ],
    }
    record.update(extra)
    return record


def _run(tmp_path, docs, **kw):
    con = store.connect(tmp_path / "docs.db")
    try:
        result = pipeline.run(con=con, documents=docs, **kw)
        return con, result
    except Exception:
        con.close()
        raise


def test_document_end_to_end(tmp_path):
    con, result = _run(tmp_path, [_doc()])
    try:
        assert result["documents"] == 1
        assert result["documents_failed"] == 0
        key, document = next(iter(store.iter_documents(con)))
        assert document["source_check"]["confirmed"] is True
        assert document["identity"]["source_confirmed"] is True
        # Cross-page table was stitched and the second page still addressable.
        table = document["tables"][0]
        assert table["rows"] == [["地区", "数量"], ["华东", "12"], ["华北", "56"]]
        assert [part["page"] for part in table["parts"]] == [1, 2]
        # Footnote linked.
        assert document["footnotes"]["linked"] == 1
        # Every retained block maps back to the stored bytes.
        for block in document["blocks"]:
            if block.get("noise_removed"):
                continue
            locator = block["locator"]
            raw = store.get_source_bytes(con, key, locator["page"])
            assert raw is not None
            assert locator["byte_end"] <= len(raw)
            assert locator["raw_sha256"]
            assert store.block_at(con, locator["raw_sha256"],
                                  locator["byte_start"], locator["byte_end"])
    finally:
        con.close()


def test_rerun_is_idempotent(tmp_path):
    docs = [_doc()]
    con, first = _run(tmp_path, docs)
    try:
        second = pipeline.run(con=con, documents=docs)
        assert second["documents"] == 1
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM document_sources").fetchone()[0] == 2
    finally:
        con.close()


def test_replay_uses_recorded_rules_and_reproduces_content(tmp_path):
    con, _ = _run(tmp_path, [_doc()])
    try:
        key = con.execute("SELECT key FROM documents").fetchone()[0]
        replay = pipeline.rerun(key, con=con)
        assert replay["reproduced"] is True
        assert replay["content_matches"] is True
        assert replay["rules_version"] == "2026.11.1"
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    finally:
        con.close()


def test_new_rules_version_keeps_history(tmp_path):
    con, _ = _run(tmp_path, [_doc()])
    try:
        v1_key = con.execute("SELECT key FROM documents").fetchone()[0]
        pipeline.run(con=con, documents=[_doc()], rules_version="2026.11.2")
        rows = con.execute("SELECT key, rules_version FROM documents ORDER BY rules_version").fetchall()
        assert len(rows) == 2
        assert rows[0] == (v1_key, "2026.11.1")
        assert rows[1][1] == "2026.11.2"
    finally:
        con.close()


def test_pending_rows_are_discarded_not_returned(tmp_path):
    con, _ = _run(tmp_path, [_doc()])
    try:
        con.execute("BEGIN IMMEDIATE")
        store.begin_document(
            con,
            {"key": "doc:staged", "document_id": "x", "rules_version": "2026.11.1",
             "rules_fingerprint": "f", "source_signature": "s"},
            {"content_hash": "c"},
        )
        store.stage_sources(con, "doc:staged",
                            [{"page_index": 1, "raw_hash": "h", "raw": b"partial"}])
        con.execute("COMMIT")

        assert store.get_document(con, "doc:staged") is None
        recovered = store.recover_incomplete(con)
        assert recovered["discarded_incomplete"] == 1
        assert con.execute("SELECT COUNT(*) FROM document_sources WHERE key='doc:staged'").fetchone()[0] == 0
        # The committed document is untouched.
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    finally:
        con.close()


def _broken_document(doc_id="bad"):
    record = {
        "id": doc_id,
        "kind": "document",
        "pages": [
            {"page_index": 1, "content_b64": base64.b64encode(b"<p>ok</p>").decode()},
            {"page_index": 2, "content_b64": "!!! not base64 !!!"},
        ],
    }
    return record


def _fatal_document(doc_id="fatal"):
    # A page carrying a non-serialisable value cannot be persisted; saving it
    # must fail atomically rather than storing a half-written success.
    return {
        "id": doc_id,
        "kind": "document",
        "pages": [{"page_index": 1, "raw": b"<p>ok</p>", "bad": object()}],
    }


def test_damaged_page_is_isolated_not_fatal(tmp_path):
    con, result = _run(tmp_path, [_broken_document()])
    try:
        assert result["documents"] == 1
        key, document = next(iter(store.iter_documents(con)))
        statuses = [page["encoding"]["reason"] for page in document["pages"]]
        assert "page_error" in statuses
        # The healthy page still produced content and source bytes.
        assert any(block["page_index"] == 1 for block in document["blocks"])
        assert store.get_source_bytes(con, key, 1) == b"<p>ok</p>"
    finally:
        con.close()


def test_failed_document_does_not_block_others(tmp_path):
    good = _doc("good")
    bad = _fatal_document()
    con, result = _run(tmp_path, [bad, good])
    try:
        assert result["documents"] == 1
        assert result["documents_failed"] == 1
        assert result["errors"][0]["document"] == "fatal"
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert con.execute("SELECT document_id FROM documents").fetchone()[0] == "good"
    finally:
        con.close()


def test_fail_fast_rule_raises(tmp_path):
    from src.textreclaim import config

    rules = config.rules_for("2026.11.1")
    rules["version"] = "test-fail-fast"
    rules["on_error"] = "raise"
    config.register_rules(rules)
    con = store.connect(tmp_path / "failfast.db")
    try:
        with pytest.raises(Exception):
            pipeline.run(con=con, documents=[_fatal_document()],
                         rules_version="test-fail-fast")
    finally:
        con.close()


def test_source_check_flags_unverified_blocks(tmp_path):
    con = store.connect(tmp_path / "survey.db")
    try:
        record = _doc()
        pages = [
            {"page_index": 1, "content_b64": base64.b64encode(PAGE_ONE.encode()).decode()}
        ]
        record["pages"] = pages
        pipeline.run(con=con, documents=[record])
        key, document = next(iter(store.iter_documents(con)))
        assert document["source_check"]["confirmed"] is True
    finally:
        con.close()


def test_legacy_and_document_records_can_be_mixed(tmp_path):
    con = store.connect(tmp_path / "mixed.db")
    try:
        result = pipeline.run(
            con=con,
            documents=[{"id": "r-100", "src": "Alpha", "name": " x ", "value": 1}, _doc()],
        )
        assert result["fed"] == 2
        assert result["rows"] == 1
        assert result["documents"] == 1
    finally:
        con.close()


def test_large_page_is_processed_with_bounded_memory(tmp_path):
    paragraphs = "".join(f"<p>段落 {i}，内容。</p>" for i in range(4000))
    rows = "".join(f"<tr><td>{i}</td><td>{i * 2}</td></tr>" for i in range(2000))
    page = f"<html><body><article class='post-content'>{paragraphs}<table>{rows}</table></article></body></html>"
    con, result = _run(tmp_path, [_doc(pages=(page,))])
    try:
        assert result["documents"] == 1
        key, document = next(iter(store.iter_documents(con)))
        assert len(document["blocks"]) >= 4001
        assert len(document["tables"][0]["rows"]) == 2000
    finally:
        con.close()


def test_line_column_and_byte_offsets_point_at_the_original_bytes(tmp_path):
    page = "<html><body><article class='post-content'><p>第一段。</p><p>第二段 12 kg。</p></article></body></html>"
    con, result = _run(tmp_path, [_doc(pages=(page,))])
    try:
        key, document = next(iter(store.iter_documents(con)))
        raw = store.get_source_bytes(con, key, 1)
        assert raw == page.encode()
        paragraphs = [b for b in document["blocks"] if b["type"] == "paragraph"]
        assert [b["text"] for b in paragraphs] == ["第一段。", "第二段 12 kg。"]
        for block in paragraphs:
            locator = block["locator"]
            slice_bytes = raw[locator["byte_start"]:locator["byte_end"]]
            assert block["text"].encode() in slice_bytes
            assert locator["line_start"] >= 1
    finally:
        con.close()


def test_content_hash_covers_exactly_the_retained_blocks(tmp_path):
    from src.textreclaim.identify import document_content_hash

    con = store.connect(tmp_path / "noise.db")
    try:
        pages = [
            ("<body><nav class='site-nav'><a href='/'>首页</a><a href='/a'>产品</a>"
             "<a href='/b'>关于</a><a href='/c'>联系</a><a href='/d'>帮助</a></nav>"
             "<article class='post-content'><p>同一段正文。</p></article></body>")
            for _ in range(3)
        ]
        pipeline.run(con=con, documents=[_doc("with-nav", pages=pages)])
        key, document = next(iter(store.iter_documents(con)))
        # Duplicate template occurrences were removed (the first is kept for
        # review); the hash must cover the retained content only, so it can be
        # recomputed exactly from the stored blocks.
        assert document["content_hash"] == document_content_hash(document["blocks"])
        assert any(block["noise_removed"] for block in document["blocks"])
        assert any(not block["noise_removed"] and "首页" in block["text"]
                   for block in document["blocks"])
    finally:
        con.close()


def test_bundled_document_fixture_ingests_cleanly(tmp_path):
    from src.textreclaim import loader

    records = list(loader.iter_records(loader.FIXTURES.parent / "documents.json"))
    con, result = _run(tmp_path, records)
    try:
        assert result["documents"] == 1
        key, document = next(iter(store.iter_documents(con)))
        assert document["pages"][0]["encoding"]["chosen"] == "gb18030"
        assert document["source_check"]["confirmed"] is True
        table = document["tables"][0]
        assert table["rows"] == [["地区", "数量"], ["华东", "12"], ["华北", "56"]]
        assert document["footnotes"]["linked"] == 1
        texts = [block["text"] for block in document["blocks"] if not block["noise_removed"]]
        # Short body text survives; the footnote marker in it stays attached.
        assert any("短句也是事实。" in text for text in texts)
        assert "第二页正文要点。" in texts
    finally:
        con.close()


def test_loader_handles_all_input_shapes(tmp_path):
    from src.textreclaim import loader

    (tmp_path / "arr.json").write_text('[{"id":"1"},{"id":"2"}]', encoding="utf-8")
    (tmp_path / "obj.json").write_text('{\n  "id": "single"\n}', encoding="utf-8")
    (tmp_path / "lines.jsonl").write_text('{"id":"a"}\n{"id":"b"}\n', encoding="utf-8")
    (tmp_path / "page.bin").write_bytes(b"<p>ref</p>")
    (tmp_path / "ref.json").write_text(json.dumps({
        "id": "r", "kind": "document",
        "pages": [{"page_index": 1, "content_ref": "page.bin"}],
    }), encoding="utf-8")

    assert [r["id"] for r in loader.iter_records(tmp_path / "arr.json")] == ["1", "2"]
    assert [r["id"] for r in loader.iter_records(tmp_path / "obj.json")] == ["single"]
    assert [r["id"] for r in loader.iter_records(tmp_path / "lines.jsonl")] == ["a", "b"]
    ref = list(loader.iter_records(tmp_path / "ref.json"))[0]
    assert ref["pages"][0]["raw"] == b"<p>ref</p>"


def test_loader_rejects_truncated_and_escaping_input(tmp_path):
    from src.textreclaim import loader

    (tmp_path / "truncated.json").write_text('[{"id":"a"},', encoding="utf-8")
    with pytest.raises(ValueError):
        list(loader.iter_records(tmp_path / "truncated.json"))

    (tmp_path / "escape.json").write_text(json.dumps({
        "id": "e", "kind": "document",
        "pages": [{"page_index": 1, "content_ref": "../../etc/passwd"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        list(loader.iter_records(tmp_path / "escape.json"))
