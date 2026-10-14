#!/bin/zsh
# Local daily run (fallback when Google blocks GitHub runners): search, commit, push.
set -eu
cd "${0:A:h}/.."
export PATH="$HOME/.pyenv/shims:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
git pull --rebase --autostash -q
if [ -f .env ]; then uv run --env-file .env flight_watch.py; else uv run flight_watch.py; fi
git add data REPORT.md
git diff --cached --quiet || git commit -q -m "flight-watch $(date -u +%F) (local)"
git push -q
