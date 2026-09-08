#!/usr/bin/env python3
"""Audit a Docker archive's metadata and historical layers without extracting it."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frf.core import credentials
from frf.observe.image_audit import audit_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    secrets = [credentials.get(name) for name in ('LLM_API_KEY', 'E2B_API_KEY', 'GITHUB_TOKEN')]
    report = audit_image(args.archive, secret_values=secrets,
                         log=lambda text: print(text, file=sys.stderr, flush=True))
    text = json.dumps(report, indent=2)
    if args.json:
        args.json.write_text(text + '\n')
    print(text)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
