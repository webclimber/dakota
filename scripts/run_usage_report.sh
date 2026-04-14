#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="/Users/dakota"
export DAKOTA_BASE="$HOME/dakota"

cd "$DAKOTA_BASE"
source "$DAKOTA_BASE/.venv/bin/activate"

if [[ -f "$DAKOTA_BASE/.env" ]]; then
  set -a
  source "$DAKOTA_BASE/.env"
  set +a
fi

exec python "$DAKOTA_BASE/scripts/report_usage.py"
