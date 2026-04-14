#!/bin/zsh
set -euo pipefail

TARGETS=(
  "$HOME/.zshrc"
  "$HOME/.zprofile"
  "$HOME/.bash_profile"
  "$HOME/.profile"
  "$HOME/gauth"
  "$HOME/.openclaw"
  "$HOME/.config"
  "$HOME"
)

PATTERN='OPENAI_API_KEY|GOOGLE_API_KEY|GEMINI|GOOGLE_APPLICATION_CREDENTIALS|OPENAI_API_BASE|GOG_ACCOUNT|GOG_KEYRING_PASSWORD'

print "Searching likely locations on River for API-related material..."
for target in $TARGETS; do
  [[ -e "$target" ]] || continue
  if [[ -d "$target" ]]; then
    rg -n --hidden -S -g '!**/.git/**' -g '!**/.venv/**' -e "$PATTERN" "$target" || true
  else
    rg -n -S -e "$PATTERN" "$target" || true
  fi
done
