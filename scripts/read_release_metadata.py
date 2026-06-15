#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _get(payload: dict[str, Any], dotted_key: str) -> Any:
    cur: Any = payload
    for part in dotted_key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted_key)
        cur = cur[part]
    return cur


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read a dotted key from dist/release_metadata.json.')
    parser.add_argument('dist', nargs='?', default='dist')
    parser.add_argument('key', help='Dotted key, for example image.digest')
    args = parser.parse_args(argv)
    metadata = json.loads((Path(args.dist) / 'release_metadata.json').read_text(encoding='utf-8'))
    value = _get(metadata, args.key)
    if isinstance(value, (dict, list)):
        print(json.dumps(value, sort_keys=True))
    else:
        print(value)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
