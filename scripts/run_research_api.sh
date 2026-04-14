#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1
export HOME="/Users/dakota"
export DAKOTA_BASE="$HOME/dakota"

cd "$DAKOTA_BASE"
source "$DAKOTA_BASE/.venv/bin/activate"

if [[ -f "$DAKOTA_BASE/.env" ]]; then
  set -a
  source "$DAKOTA_BASE/.env"
  set +a
fi

exec uvicorn services.research_api.app:app \
  --host 127.0.0.1 \
  --port 8787
