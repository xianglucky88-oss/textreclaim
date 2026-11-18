"""Noise classification and conservative removal.

The central rule is that *removal is the expensive mistake*.  Deleting a
navigation bar costs a few lines; deleting a three-word body paragraph loses
factual content that nothing downstream can recover.

Consequently:

* a block is removed only when several independent signals agree, producing a
  score at or above ``noise_high_threshold``;
* medium scores (``noise_low_threshold`` .. ``noise_high_threshold``) are kept
  verbatim and annotated with ``noise_score``/``noise_reasons``;
* *no* short-text heuristic ever deletes a block; short text only adds weight
  when other evidence is already strong;
* repeated text is never treated as noise on its own.  Repeated *template
  regions* are identified by structural position (the same ancestor signature
  on several pages), and even then the first occurrence is retained;
* footnote definitions and table blocks are never removed;
* removed blocks are retained in ``removed`` with their locator so a reviewer
  can restore them.
"""
from __future__ import annotations

import re

from .config import rules_for

# Structural hints that a region is boilerplate.  Values are *contributions*,
# not verdicts.
TEMPLATE_HINTS = {
    "nav": 0.45, "navigation": 0.45, "navbar": 0.5, "menu": 0.35, "topbar": 0.4,
    "toolbar": 0.3, "breadcrumb": 0.45, "breadcrumbs": 0.45, "pagination": 0.45,
    "pager": 0.45, "sidebar": 0.35, "side-bar": 0.35, "footer": 0.4,
    "site-footer": 0.5, "header": 0.3, "site-header": 0.5, "banner": 0.4,
    "contentinfo": 0.4, "complementary": 0.3, "cookie": 0.45, "cookies": 0.45,
    "consent": 0.4, "gdpr": 0.4, "share": 0.3, "sharebar": 0.4, "social": 0.3,
    "advert": 0.5, "advertisement": 0.55, "ads": 0.45, "sponsor": 0.4,
    "related": 0.25, "recommend": 0.25, "newsletter": 0.3, "subscribe": 0.3,
    "copyright": 0.4, "skip-link": 0.5, "hidden": 0.3, "promo": 0.4,
}

# Class/id tokens typically worn by actual content.  Their presence subtracts
# from the score, which is how a real article inside a <nav>-like wrapper
# survives.
CONTENT_HINTS = {
    "article", "content", "main", "entry", "post", "story", "body", "prose",
    "document", "text", "chapter", "section", "abstract", "summary", "detail",
    "markdown", "rich-text", "richtext", "post-content", "article-body",
}

BOILERPLATE_PHRASES = (
    "all rights reserved",
    "版权所有",
    "免责声明",
    "privacy policy",
    "terms of service",
    "cookie policy",
    "subscribe to our newsletter",
    "share this article",
    "advertisement",
    "点击查看",
    "扫码关注",
    "未经授权",
)

_CLASS_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")


def _tokens(hint: str) -> set[str]:
    """Split class/id hints into tokens, and keep compound tokens too.

    ``site-nav`` yields both ``site-nav`` and its parts, so ``nav`` is
    recognised without losing the more specific match.
    """
    tokens: set[str] = set()
    for compound in _CLASS_TOKEN_RE.findall(hint or ""):
        compound = compound.lower()
        tokens.add(compound)
        for part in re.split(r"[_\-]+", compound):
            if part:
                tokens.add(part)
    return tokens


def _hint_score(hint: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    tokens = _tokens(hint)
    for token in tokens:
        if token in TEMPLATE_HINTS:
            score += TEMPLATE_HINTS[token]
            reasons.append(f"template_token:{token}")
    for hint_token in CONTENT_HINTS:
        if hint_token in tokens:
            score -= 0.5
            reasons.append(f"content_token:{hint_token}")
    return score, reasons


def _link_density(block: dict) -> tuple[float, int]:
    """Fraction of visible text that belongs to anchor references."""
    text = block.get("text") or ""
    if not text:
        return 0.0, 0
    anchor_chars = 0
    anchors = 0
    for ref in block.get("refs") or []:
        if ref.get("kind") == "anchor":
            anchors += 1
            anchor_chars += len(ref.get("text") or "")
    # Inline anchors are collected with their visible text; when that text is
    # unavailable fall back to a conservative count-only signal elsewhere.
    return (anchor_chars / max(1, len(text))), anchors


def score_block(block: dict, context: dict | None = None) -> tuple[float, list[str]]:
    """Return ``(score, reasons)`` where score is in ``[0, 1]``."""
    context = context or {}
    reasons: list[str] = []
    score = 0.0
    kind = block.get("type")
    if kind in ("table", "table_continuation", "footnote", "code"):
        return 0.0, ["protected_type"]
    text = block.get("text") or ""
    stripped = text.strip()

    # Structural hints are weighted by proximity: the element that directly
    # wraps the block says more about it than a distant page wrapper.
    ancestors = block.get("ancestors") or []
    hint_score = 0.0
    for offset, ancestor in enumerate(reversed(ancestors)):
        weight = 1.0 / (1.0 + 0.5 * offset)
        part_score, part_reasons = _hint_score(ancestor.get("hint") or "")
        if part_score == 0:
            continue
        if part_score > 0:
            hint_score += min(0.45, part_score) * weight
            reasons.extend(part_reasons)
        else:
            hint_score += part_score * weight
            reasons.extend(part_reasons)
    own_hint_score, own_reasons = _hint_score(block.get("attrs_hint") or "")
    if own_hint_score > 0:
        hint_score += min(0.5, own_hint_score) * 1.2
        reasons.extend(own_reasons)
    elif own_hint_score < 0:
        hint_score += own_hint_score * 1.2
        reasons.extend(own_reasons)
    score += hint_score

    if not stripped and kind == "list_item":
        score += 0.1
        reasons.append("empty_list_item")

    lower = stripped.lower()
    for phrase in BOILERPLATE_PHRASES:
        if phrase in lower:
            score += 0.25
            reasons.append(f"boilerplate_phrase:{phrase}")
            break

    density, anchors = _link_density(block)
    if anchors >= 3 and density >= 0.5:
        score += 0.3
        reasons.append("high_link_density")
    elif anchors >= 6:
        score += 0.2
        reasons.append("many_links")

    repeats = context.get("repeat_count", {}).get(_norm(stripped), 0)
    if repeats >= 3 and kind in ("paragraph", "list_item", "heading"):
        # Repeated template text.  Only a contribution: genuine body text can
        # legitimately repeat (e.g. a refrain or a repeated table row).
        score += min(0.25, 0.08 * (repeats - 2))
        reasons.append(f"repeated_across_pages:{repeats}")

    # Short text is never evidence on its own; it only amplifies signals that
    # already fired.  A two-word body sentence therefore cannot be removed.
    short_max = int(context.get("short_text_max", 12))
    if stripped and len(stripped) <= short_max and score >= 0.45:
        score += 0.08
        reasons.append("short_text_amplifier")

    return max(0.0, min(1.0, score)), reasons


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def classify(blocks: list[dict], rules: dict) -> dict:
    high = float(rules.get("noise_high_threshold", 0.85))
    low = float(rules.get("noise_low_threshold", 0.45))

    # Repeat counts are computed over the *whole document*, but they are only
    # a contributing signal; no block is removed for repetition alone.
    repeat_count: dict[str, int] = {}
    for block in blocks:
        text = _norm(block.get("text") or "")
        if not text:
            continue
        repeat_count[text] = repeat_count.get(text, 0) + 1
    context = {"repeat_count": repeat_count,
               "short_text_max": rules.get("short_text_max", 12)}

    stats = {"removed": 0, "kept_low_confidence": 0, "kept": 0, "protected": 0,
             "kept_first_occurrence": 0}
    seen_templates: dict[str, str] = {}
    for block in blocks:
        score, reasons = score_block(block, context)
        block["noise_score"] = round(score, 3)
        block["noise_reasons"] = reasons
        # ``noise_removed`` is always present, so "kept" is never confused with
        # "not classified yet".
        block["noise_removed"] = False
        if block.get("type") in ("table", "table_continuation", "footnote", "code"):
            block["noise_class"] = "protected"
            stats["protected"] += 1
            continue
        if score >= high and (block.get("text") or "").strip():
            block["noise_class"] = "template_high_confidence"
            key = _norm(block.get("text") or "")
            if rules.get("remove_high_confidence_noise", True):
                if key in seen_templates and rules.get("deduplicate_templates", True):
                    block["noise_removed"] = True
                    stats["removed"] += 1
                    block["template_duplicate_of"] = seen_templates[key]
                else:
                    # The first occurrence of a repeated template region is
                    # retained so a reviewer can see what was suppressed
                    # elsewhere in the document.  With deduplication disabled
                    # every occurrence is retained and only annotated.
                    dedup = rules.get("deduplicate_templates", True)
                    if dedup:
                        seen_templates[key] = block.get("block_id")
                    block["noise_class"] = "template_first_occurrence"
                    block["noise_retained_reason"] = (
                        "first_occurrence_of_repeated_region"
                        if dedup else "template_deduplication_disabled"
                    )
                    stats["kept_first_occurrence"] += 1
            else:
                stats["kept"] += 1
        elif score >= low:
            block["noise_class"] = "template_low_confidence"
            # Never deleted: the caller decides, with the score in hand.
            stats["kept_low_confidence"] += 1
        else:
            block["noise_class"] = "content"
            stats["kept"] += 1
    return stats


def detect_repeated_regions(blocks: list[dict], min_pages: int = 2) -> list[dict]:
    """Identify template regions repeated at the *same structural position*.

    The signature is the ancestor chain plus the block's ordinal within its
    parent, so a banner that appears first on every page matches itself while
    a body sentence that happens to repeat at different depths does not.  This
    is a *reporting* signal: it never deletes anything by itself.
    """
    signatures: dict[tuple, list[dict]] = {}
    for block in blocks:
        if block.get("type") not in ("paragraph", "heading", "list_item"):
            continue
        ancestors = tuple(
            (a.get("tag"), (a.get("hint") or "")[:80])
            for a in (block.get("ancestors") or [])[-3:]
        )
        if not ancestors:
            continue
        key = (ancestors, block.get("slot"))
        signatures.setdefault(key, []).append(block)
    regions = []
    for (ancestors, slot), members in signatures.items():
        pages = {member.get("page_index") for member in members}
        if len(pages) >= min_pages and len(members) >= min_pages and slot is not None:
            regions.append({
                "ancestor_signature": list(ancestors),
                "slot": slot,
                "pages": sorted(pages),
                "block_ids": [member.get("block_id") for member in members],
                "count": len(members),
            })
    return regions


def process(record):
    if not isinstance(record, dict) or record.get("kind") != "document":
        rec = dict(record)
        rec.setdefault("_stage_clean", "naive")
        return rec
    rules = rules_for(record.get("rules_version"))
    out = dict(record)
    blocks = [dict(block) for block in record.get("blocks") or []]
    stats = classify(blocks, rules)
    regions = detect_repeated_regions(blocks)
    out["blocks"] = blocks
    out["removed"] = [block for block in blocks if block.get("noise_removed")]
    out["noise_stats"] = stats
    out["repeated_regions"] = regions
    out["tables"] = [block["table"] for block in blocks
                     if block.get("type") == "table" and block.get("table")]
    return out
