#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Activate venv if present (local dev). Inside Docker, packages are system-wide.
if [ -f ./.venv/bin/activate ]; then
	. ./.venv/bin/activate
fi

# Export variables from .env if present (helps inside Docker and locally)
set +u
if [ -f ./.env ]; then
	set -a
	. ./.env
	set +a
fi
set -u

PORT="${PORT:-8000}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
