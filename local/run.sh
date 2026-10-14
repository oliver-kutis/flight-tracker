#!/bin/zsh
# Local daily run (residential IP fallback when Google blocks GitHub runners): track, commit, push.
set -eu
cd "${0:A:h}/.."
export PATH="$HOME/.pyenv/shims:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
git pull --rebase --autostash -q
[ -d .vendor/fast_flights_fork ] || uv pip install -q --target .vendor/fast_flights_fork \
  "git+https://github.com/Mohamed3on/flights@d1620895da9d770efbb7196b67c95c47dc4a3ab1"
if [ -f .env ]; then uv run --env-file .env tracker; else uv run tracker; fi
git add data
git diff --cached --quiet || git commit -q -m "data: run $(date -u +%F) (local)"
git push -q
