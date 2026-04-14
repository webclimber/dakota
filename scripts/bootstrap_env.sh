#!/bin/zsh
set -euo pipefail

cd "$HOME/dakota"
[[ -f .env ]] || cp .env.example .env
mkdir -p logs reports tmp memory/{structured,vector,index,cache}
chmod 700 "$HOME/dakota"
chmod 600 .env || true

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt

print "Bootstrap complete. Edit ~/.env before starting services."
