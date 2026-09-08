#!/usr/bin/env python3
"""Build, verify and export a task image in E2B as an anonymous Docker archive."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frf.observe.images import export, prepare_task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task')
    parser.add_argument('--output', required=True)
    parser.add_argument('--prepare-task', type=Path,
                        help='also prepare a separate Harbor task using the exported image')
    args = parser.parse_args()
    if args.prepare_task is not None and args.prepare_task.exists():
        parser.error('--prepare-task destination already exists')
    image = export(args.task, args.output,
                   log=lambda message: print(message, file=sys.stderr, flush=True))
    result = dict(image)
    if args.prepare_task is not None:
        result['prepared_task'] = prepare_task(args.task, args.prepare_task, image)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
