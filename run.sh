#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Activate venv if present (local dev). Inside Docker, packages are system-wide.
if [ -f ./.venv/bin/activate ]; then
	. ./.venv/bin/activate
fi

PORT="${PORT:-8000}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
