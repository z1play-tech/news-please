#!/usr/bin/env bash
# Install / refresh scraper2 venv (editable ``./newspaper`` → news-please fork).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f requirements.txt ]]; then
  echo "error: requirements.txt not found in $(pwd)" >&2
  exit 1
fi

if [[ ! -d newspaper || ! -f newspaper/setup.py ]]; then
  echo "error: ./newspaper is missing (expected fork with setup.py)" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Installing uv via official installer..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "error: uv not found and neither curl nor wget is available." >&2
    echo "Install uv manually: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv still not found after install. Reload shell or add ~/.cargo/bin to PATH." >&2
  exit 1
fi

test -d .venv || uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python
echo "scraper2 deps OK (.venv + editable ./newspaper)"
