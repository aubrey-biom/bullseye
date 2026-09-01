#!/usr/bin/env bash
# Bring a fresh container up to where scripts/pos_brief.py can run.
#
# Containers are ephemeral: the repo is re-cloned with no .venv and none of the
# reporting dependencies. The scheduled brief has to build its own environment
# before it can generate anything, so this is idempotent and safe to run every
# time — it does nothing when the venv is already good.
#
# Usage:  bash scripts/setup_reporting.sh        # just what the brief needs
#         bash scripts/setup_reporting.sh dev    # plus pytest/ruff, to fix things
set -euo pipefail
cd "$(dirname "$0")/.."

EXTRAS="reporting"
[ "${1:-}" = "dev" ] && EXTRAS="reporting,dev"

[ -x .venv/bin/python ] || uv venv .venv

if ! .venv/bin/python -c 'import google.cloud.bigquery' 2>/dev/null; then
  uv pip install --quiet --python .venv/bin/python -e ".[${EXTRAS}]"
fi

.venv/bin/python - <<'PY'
import google.cloud.bigquery as bq
print(f"ready: google-cloud-bigquery {bq.__version__}")
PY
