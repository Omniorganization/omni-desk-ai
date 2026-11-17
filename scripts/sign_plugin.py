#!/usr/bin/env python3
"""Write sha256 + a v2 HMAC signature into a plugin manifest.

Usage: OMNIDESK_PLUGIN_SIGNING_SECRET=... python scripts/sign_plugin.py [--check | --force] PLUGIN_DIR

Signing (re-)signs with v2, which binds name, version, entrypoint, sandbox,
permissions, trusted and every file in PLUGIN_DIR. Review those fields and the
plugin files first: the signature vouches for whatever is there now.

A manifest that already carries a signature is re-signed only if that
signature still verifies (v1: the entrypoint digest; v2: fields and files).
After reviewing an intentional change, re-sign it with --force.

--check only verifies the existing v2 signature and exits 1 if the manifest is
still v1 or does not verify.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from omnidesk_agent.plugins.manifest import LEGACY_SIGNATURE_VERSION, PluginManifest, find_manifest


def _summary(manifest: PluginManifest) -> str:
    # CodeQL's sensitive-name heuristic treats a `trusted` attribute as a
    # secret; print one of two fixed words instead of the raw value.
    trust = "trusted" if manifest.trusted else "untrusted"
    return (
        f"{manifest.name} {manifest.version}: sandbox={manifest.sandbox}, {trust}, "
        f"permissions={sorted(manifest.permissions)}, entrypoint={manifest.entrypoint}"
    )


def _serialise(manifest: PluginManifest, manifest_path: Path) -> tuple[str, dict]:
    data = manifest.model_dump(exclude_none=True)
    if manifest_path.suffix == ".json":
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        return text, json.loads(text)
    import yaml

    text = yaml.safe_dump(data, sort_keys=True)
    return text, yaml.safe_load(text)


def main(argv: list[str]) -> int:
    args = argv[1:]
    mode = args[0] if args and args[0] in {"--check", "--force"} else ""
    if mode:
        args = args[1:]
    if len(args) != 1 or args[0].startswith("-"):
        print((__doc__ or "").strip(), file=sys.stderr)
        return 64
    secret = os.getenv("OMNIDESK_PLUGIN_SIGNING_SECRET", "")
    if not secret:
        print("OMNIDESK_PLUGIN_SIGNING_SECRET is required", file=sys.stderr)
        return 64
    plugin_dir = Path(args[0])
    try:
        # Same lookup as PluginRegistry, so the file signed is the file loaded.
        manifest_path = find_manifest(plugin_dir)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 65
    if manifest_path is None:
        print(f"no plugin manifest in {plugin_dir}", file=sys.stderr)
        return 66
    manifest = PluginManifest.load(manifest_path)
    # The fields as loaded: what --check verified or what is being signed.
    summary = _summary(manifest)

    if mode == "--check":
        if manifest.signature_version == LEGACY_SIGNATURE_VERSION:
            print(f"{manifest.name}: legacy v1 signature; re-sign with scripts/sign_plugin.py ({manifest_path})", file=sys.stderr)
            return 1
        try:
            manifest.verify(plugin_dir, secret)
        except (PermissionError, FileNotFoundError) as exc:
            print(f"{manifest.name}: {exc}", file=sys.stderr)
            return 1
        print(f"ok, v2 signature verifies: {summary}")
        return 0

    if manifest.signature and mode != "--force":
        try:
            manifest.verify(plugin_dir, secret, allow_legacy_v1=True)
        except (PermissionError, FileNotFoundError) as exc:
            print(
                f"{manifest.name}: existing signature does not verify ({exc}); "
                "review the plugin, then re-sign with --force",
                file=sys.stderr,
            )
            return 1

    manifest.sign_v2(plugin_dir, secret)
    text, written = _serialise(manifest, manifest_path)
    # Verify exactly what will be written before replacing the manifest.
    PluginManifest.model_validate(written).verify(plugin_dir, secret)
    manifest_path.write_text(text, encoding="utf-8")
    print(f"signed with a v2 signature: {summary} ({manifest_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
