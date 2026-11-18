# textreclaim — 错声明编码/BOM/全半角/多语种/模板噪声网页还原为干净正文并重建表格列表。

运行栈：Python 3 + 标准库 `sqlite3` + `pytest`（离线，无需联网）

把原始页面字节还原为可入库的正文、表格和列表，并且**每个内容块都能回到原始字节与页面位置**。

## 运行方式
```text
make run                                        # 摄取 fixtures/input.json
make test                                       # 运行流水线与幂等性、编码、结构、降噪测试
python -m textreclaim.cli path/to/input.json    # 摄取任意 JSON / JSONL
python -m textreclaim.cli fixtures/documents.json          # 多页文档样例（GBK + 跨页续表 + 脚注）
python -m textreclaim.cli --rules-version 2026.11.2 input.json
python -m textreclaim.cli --replay DOC_KEY      # 用记录中的规则版本重放历史文档
```
`textreclaim.cli` 退出码：`0` 成功；`1` 有文档失败（其余文档已提交）；`2` 回放键不存在。

## 输入形态
* **历史记录**（`fixtures/input.json`）：`{"id","src","name","value",...}`，行为与存储键完全保持原状。
* **文档记录**：`{"id", "kind": "document", "pages": [...]}`。每页可以是
  `content_b64` / `raw_b64`、`content_ref`（相对输入目录的文件，越界路径会被拒绝）或 `raw`；
  可选 `headers`/`content_type`/`declared_encoding` 提供编码声明。

两种形态可以在同一次运行中混合。

## 数据流与模块边界
`pipeline.py` 只做顺序编排，不含业务判断。

```
文档:  encoding → extract → normalize → clean → identify → keyer → store
历史:  normalize → identify → clean → keyer → store
```

| 模块 | 职责 |
| --- | --- |
| `encoding` | BOM / HTTP 头 / meta 声明 / 实际字节四方仲裁，保留证据与冲突，坏字节明确标出 |
| `extract` | 容错分词与建树，产出正文/标题/列表/表格/脚注块与定位；跨页续表的识别与缝合 |
| `normalize` | 只做不改变语义的归一化；代码与单位不动，列关系不动 |
| `clean` | 保守降噪：高分模板才移除，低分保留并标注，重复正文永远保留 |
| `identify` | 稳定内容指纹、来源映射校验、重复标注 |
| `keyer / store` | 规则版本化主键、原子写入、中断恢复、回放 |

## 关键取舍（噪声 vs 事实 vs 定位 vs 开销）
* **删除是不可逆的错误，保留是可审的错误。** 只有多信号一致且分数 ≥ `noise_high_threshold`
  才移除；中等分数（`noise_low_threshold`..`high`）原样保留并写 `noise_score`/`noise_reasons`；
  短文本本身**从不**构成删除理由。
* **重复正文 ≠ 重复模板。** 模板按祖先签名 + 结构槽位识别，且**首次出现永远保留**；
  正文中相同的句子在各页都保留，只加 `duplicate_text` 标注。
* **内容与来源共同确认。** 每个块带 `page / char_start..end / byte_start..end / line,col /
  raw_sha256`；`identify.source_check` 校验每个保留块都能回到存储的原始字节，
  否则置 `source_unconfirmed`。
* **定位精度优先于体积。** 字符→字节映射是稀疏检查点（每 8192 字符），
  大页面不必为每个字符存一个整数；随机访问仍可用。
* **规则换版可复现。** 规则集有 `version` 与内容指纹；键 = 文档标识 + 来源签名 + 规则指纹。
  换版不会覆盖历史，`pipeline.rerun(key)` 用**记录中的版本**与**原始字节**重放并比对内容哈希。
* **中断不留假成功。** 一个文档在单个事务内写 `documents`(pending→complete) + `document_sources`
  + `document_blocks`；未提交的待定行在下一次启动时由 `recover_incomplete` 清除。
* **内存有界。** 输入按元素流式解析；峰值内存由**块数量**而非页面字节数主导：
  每块约 0.7 KB（含定位与诊断），500 个大段落页 ≈ 7×页字节，而 8000 个短段落页 ≈ 200×。
  因此超大页面建议按页拆分；另有结构松手阈值：标签长度上限、注释长度上限、嵌套深度上限、
  `colspan/rowspan` 上限，超限只降级不中断。
* **局部坏块不吞后续。** 未闭合标签/注释、游离结束标签、超长标签、深层嵌套均就地重同步；
  坏页记 `page_error` 且其余页继续；坏文档不影响其它文档。

## 归一化边界
* 只用 NFC；**不使用 NFKC**（会改写连字、数学符号、兼容汉字）。
* 全角折叠只涉及全角数字/字母与 U+3000；CJK 标点、货币符号、单位与数字分隔符保留。
* `code`/`pre` 内容逐字节保留（连 NFC 都不做）；大小写、专名不改。

## 持久化与测试
数据落在项目内 SQLite：`documents`、`document_sources`（原始字节）、`document_blocks`、
`runs`（规则版本 + 指纹 + 时间）。测试夹具在 `tests/`，运行方式以 `Makefile` 为准。

## 已验证的边界行为
* `encoding`：BOM 与 HTTP/meta 冲突时以 BOM 为准并记录冲突（`bom_wins`）；
  声明 GBK 实为 GBK/声明 utf-8 实为 GBK 等错声明由字节证据纠正（`misdeclared_encoding`）；
  少量坏字节不换编码，而是给出 `undecodable[].byte_start/byte_end/preview_hex`。
* `extract`：未闭合 `<p>/<li>/<td>` 的定位范围止于下一个块（不吞后续）；
  重复表头只记一次且 `rows` 不重复；不同列数的表不缝合；跨页缝合后两块定位都在 `parts`。
* `clean`：`导航/页脚/侧栏` 只有高分且重复时才移除，且首次出现保留；`table/footnote/code` 永不删除。
* `store`：`documents.status='pending'` 的行对读取方不可见，下次启动清除；
  一个文档的 `documents + document_sources + document_blocks` 在同一事务内提交。
