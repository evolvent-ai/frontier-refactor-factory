#!/usr/bin/env python3
"""Collect bounded open-world language/source eligibility evidence.

This is deliberately a source/smoke matrix, not a claim that every language is certified. Full
freeze/verifier/Harbor runs are expensive and are launched separately for eligible rows.
"""
from __future__ import annotations

import argparse
import json

from frf.automation import _index
from frf.core.capabilities import capability


def collect(languages: list[str], scale: str, count: int) -> list[dict]:
    rows = []
    for language in languages:
        item = {"language": language, "scale": scale,
                "capability": capability(language, scale=scale).__dict__,
                "candidates": [], "errors": []}
        try:
            index_name = "github" if scale == "repo" else "github-packages"
            index = _index(index_name, subset=language, scale=scale)
            for candidate in list(index.page(0, size=count))[:count]:
                item["candidates"].append({"identity": candidate.identity,
                                           "language": candidate.language,
                                           "capability": candidate.capability})
            item["source_rejections"] = dict(getattr(index, "rejection_counts", {}))
        except Exception as exc:
            item["errors"].append(str(exc)[:1000])
        rows.append(item)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=("package", "repo"), required=True)
    parser.add_argument("--languages", default="python,javascript,typescript,go,rust,java,ruby,cpp")
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(collect([x.strip() for x in args.languages.split(",") if x.strip()],
                             args.scale, max(1, args.count)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
