"""Command line entry point.

Usage::

    python -m textreclaim.cli                    # ingest fixtures/input.json
    python -m textreclaim.cli path/to/input.json # ingest another file
    python -m textreclaim.cli --rules-version 2026.11.2 path/to/input.json
    python -m textreclaim.cli --replay DOC_KEY   # reproduce a stored document
"""
from __future__ import annotations

import argparse
import json
import sys

from . import pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="textreclaim offline ingestion")
    parser.add_argument("input", nargs="?", help="input JSON / JSONL file")
    parser.add_argument("--rules-version", default=None,
                        help="rule set to apply (default: current default version)")
    parser.add_argument("--replay", metavar="DOC_KEY", default=None,
                        help="re-run a stored document with its recorded rules version")
    args = parser.parse_args(argv)

    if args.replay:
        try:
            result = pipeline.rerun(args.replay)
        except KeyError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    elif args.input:
        result = pipeline.run(documents=pipeline.iter_records(args.input),
                              rules_version=args.rules_version)
    else:
        result = pipeline.run(rules_version=args.rules_version)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("documents_failed") else 1


if __name__ == "__main__":
    sys.exit(main())
