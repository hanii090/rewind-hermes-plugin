#!/usr/bin/env bash
# Thin wrapper — implementation lives under tests/ so `hermes plugins validate`
# does not flag private Hermes imports used only by this optional live probe.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$ROOT/tests/manual_hermes_live_probe.py" "$@"
