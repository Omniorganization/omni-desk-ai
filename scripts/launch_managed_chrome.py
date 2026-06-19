#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path


def _signature_payload(marker: dict) -> bytes:
    unsigned = {k: v for k, v in marker.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_marker(marker: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _signature_payload(marker), hashlib.sha256).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch Chrome with an OmniDesk-managed dedicated profile and write a profile attestation marker.")
    parser.add_argument("--chrome", default=os.getenv("CHROME_BIN") or "google-chrome")
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--devtools-host", default="127.0.0.1")
    parser.add_argument("--devtools-port", type=int, default=9222)
    parser.add_argument("--start-url", default="about:blank")
    parser.add_argument("--print-command", action="store_true", help="Print the command instead of launching Chrome.")
    args = parser.parse_args(argv)

    profile = args.profile_dir.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    marker = profile / ".omnidesk_chrome_profile_attestation.json"
    command = [
        args.chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-address={args.devtools_host}",
        f"--remote-debugging-port={args.devtools_port}",
        "--no-first-run",
        "--no-default-browser-check",
        args.start_url,
    ]
    if args.print_command:
        print(" ".join(command))
        return 0
    proc = subprocess.Popen(command)  # noqa: S603 - explicit operator-launched browser command
    marker_payload = {
        "schema_version": 2,
        "purpose": "omnidesk-managed-chrome-profile",
        "profile_dir": str(profile),
        "profile_dir_sha256": hashlib.sha256(str(profile).encode("utf-8")).hexdigest(),
        "devtools_host": args.devtools_host,
        "devtools_port": args.devtools_port,
        "created_at": time.time(),
        "launcher_pid": os.getpid(),
        "browser_pid": proc.pid,
        "argv": command,
    }
    secret = os.getenv("OMNIDESK_CHROME_LAUNCHER_SECRET", "")
    if secret:
        marker_payload["signature"] = "sha256=" + _sign_marker(marker_payload, secret)
    marker.write_text(json.dumps(marker_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    marker.chmod(0o600)
    print(str(marker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
