#!/usr/bin/env bash
set -euo pipefail

if ! command -v pyright >/dev/null 2>&1; then
  echo "pyright is required. Install with: npm install -g pyright"
  exit 127
fi

pyright omnidesk_agent/core omnidesk_agent/security omnidesk_agent/tools omnidesk_agent/self_upgrade omnidesk_agent/server.py omnidesk_agent/daemon.py
