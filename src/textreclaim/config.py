"""Configuration and versioned processing rules.

The rules are intentionally small, data-only ``dict`` objects.  A rule set is
identified by both a human readable *version* and a content fingerprint.  The
fingerprint is what makes historical results reproducible: changing a rule
creates a new version instead of silently re-interpreting old output.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(os.environ.get("TEXTRECLAIM_FIXTURES", ROOT / "fixtures" / "input.json"))
DB_PATH = Path(os.environ.get("TEXTRECLAIM_DB", ROOT / "textreclaim.db"))

DEFAULT_RULES_VERSION = "2026.11.1"

# A rule set is deliberately conservative.  In particular:
#   * prose normalisation never uses NFKC (it would rewrite units, ligatures
#     and mathematical symbols);
#   * high-confidence noise may be removed, low confidence noise is retained
#     and merely annotated;
#   * no short-text heuristic is allowed to delete a block;
#   * repeated body text is never treated as duplicate noise.
_RULES_V1: dict = {
    "version": DEFAULT_RULES_VERSION,
    "width_fold": True,              # full-width ASCII letters/digits -> ASCII
    "collapse_whitespace": True,
    "remove_high_confidence_noise": True,
    "noise_high_threshold": 0.85,
    "noise_low_threshold": 0.45,
    "short_text_max": 12,            # only ever amplifies existing signals
    "deduplicate_templates": True,   # false keeps every template occurrence
    "stitch_tables": True,
    "link_footnotes": True,
    "max_rowspan": 1000,
    "max_colspan": 1000,
    "max_table_columns": 256,
    "on_error": "record",            # "record" keeps going, "raise" aborts
}

# A second, deliberately different rule set is kept so that the versioning
# behaviour is exercisable without editing library code.
_RULES_V2: dict = copy.deepcopy(_RULES_V1)
_RULES_V2.update(
    {
        "version": "2026.11.2",
        "width_fold": False,
        "noise_high_threshold": 0.92,
    }
)

RULE_SETS: dict[str, dict] = {
    _RULES_V1["version"]: _RULES_V1,
    _RULES_V2["version"]: _RULES_V2,
}


def rules_for(version: str | None = None) -> dict:
    """Return a defensive copy of the rules for *version*.

    ``version=None`` selects the default.  An unknown version is an error: it
    is better to fail loudly than to reproduce history with the wrong rules.
    """
    version = version or DEFAULT_RULES_VERSION
    try:
        return copy.deepcopy(RULE_SETS[version])
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"unknown rules version: {version!r}") from exc


def rules_fingerprint(rules: dict) -> str:
    """Stable, order-independent fingerprint of a rule set."""
    payload = json.dumps(rules, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register_rules(rules: dict) -> dict:
    """Register a custom rule set and return its copy.

    This is the supported way for callers to experiment: the version must be
    present and immutable once registered (re-registering the same version
    with a different body is rejected).
    """
    if not isinstance(rules, dict) or not rules.get("version"):
        raise ValueError("rules must be a dict with a non-empty 'version'")
    version = str(rules["version"])
    existing = RULE_SETS.get(version)
    if existing is not None and rules_fingerprint(existing) != rules_fingerprint(rules):
        raise ValueError(f"rules version {version!r} is already registered with different content")
    RULE_SETS[version] = copy.deepcopy(rules)
    return copy.deepcopy(rules)
