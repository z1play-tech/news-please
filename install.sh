#!/usr/bin/env bash
# Install / refresh scraper2 venv (editable ``./newspaper`` → news-please fork).
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found (https://docs.astral.sh/uv/)" >&2
  exit 1
fi
test -d .venv || uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python
echo "scraper2 deps OK (.venv → ./newspaper)"
