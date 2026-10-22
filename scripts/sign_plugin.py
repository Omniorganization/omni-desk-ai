#!/usr/bin/env python3
"""Write sha256 + a v2 HMAC signature into a plugin manifest.

Usage: OMNIDESK_PLUGIN_SIGNING_SECRET=... python scripts/sign_plugin.py PLUGIN_DIR
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from omnidesk_agent.plugins.manifest import MANIFEST_FILE_NAMES, PluginManifest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 64
    secret = os.getenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "")
    if not secret:
        print("OMNIDESK_PLUGIN_SIGNING_SECRET is required", file=sys.stderr)
        return 64
    plugin_dir = Path(argv[1])
    manifest_path = next((plugin_dir / name for name in sorted(MANIFEST_FILE_NAMES) if (plugin_dir / name).exists()), None)
    if manifest_path is None:
        print(f"no plugin manifest in {plugin_dir}", file=sys.stderr)
        return 66
    manifest = PluginManifest.load(manifest_path)
    manifest.sign_v2(plugin_dir, secret)
    data = manifest.model_dump(exclude_none=True)
    if manifest_path.suffix == ".json":
        manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        import yaml

        manifest_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    print(f"signed {manifest.name} ({manifest_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
