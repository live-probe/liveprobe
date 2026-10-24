#!/usr/bin/env bash
# Run the evaluation harness for real (Codex in-cluster + Kubernetes fault worker).
#   scripts/eval-real.sh <store-dir> [--case ID]... [--arm ARM]... [--max-pairs N]
# Pins PATH (kubectl lives in ~/.rd/bin, which background shells lack) and the repo venv python.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.rd/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
PY="liveprobe/.venv/bin/python"
[ -x "$PY" ] || { echo "missing $PY"; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl not on PATH"; exit 2; }
STORE="$1"; shift
exec "$PY" -m evaluation.harness --real --approved-by "${EVAL_APPROVED_BY:-hwany(delegated 2026-10-05)}" \
  --store "$STORE" --cost-ceiling-usd "${EVAL_COST_CEILING_USD:-100000}" "$@"
