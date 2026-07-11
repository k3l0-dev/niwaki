#!/usr/bin/env bash
# Build the documentation as static HTML (no server, no container).
#
#   bash scripts/docs.sh          # build into docs/_build/html
#   bash scripts/docs.sh open     # build, then open in the default browser
#
# The vocabulary book is regenerated first so the pages can never lag behind
# domain/vocabulary.yaml (the drift test enforces the same invariant in CI).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[docs] regenerating the vocabulary book..."
uv run python -m niwaki._codegen.generate_docs

echo "[docs] sphinx-build (nitpicky, warnings are errors)..."
uv run sphinx-build -b html -W docs docs/_build/html

echo "[docs] OK — docs/_build/html/index.html"
if [[ "${1:-}" == "open" ]]; then
    open docs/_build/html/index.html
fi
