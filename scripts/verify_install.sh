#!/bin/bash
# scripts/verify_install.sh
#
# Run after `git pull` to verify the install is healthy. Ensures dev
# dependencies (pytest, ruff) are installed by running `uv sync --all-extras`
# at the top — so `git pull && ./scripts/verify_install.sh` always works in one
# step. Exits 0 on pass, 1 on any failure.
#
# Local checks only. The one optional network step (a BigQuery reachability
# probe) is skipped automatically when no credential is configured, so this
# script is still useful offline. `bpd_health_check` inside the MCP is the
# cross-cutting audit.

set -e
cd "$(dirname "$0")/.."

HAS_FAIL=0

echo "=== bpd-mcp install verification ==="

# Use whichever python launcher is available: prefer uv, then .venv/bin/python.
if command -v uv >/dev/null 2>&1; then
    PY="uv run python"
    echo "[0/7] Ensuring dev dependencies are installed..."
    uv sync --all-extras --quiet 2>&1 | sed 's/^/  /' || {
        echo "  WARN: 'uv sync --all-extras' did not run cleanly; continuing"
    }
    echo "  PASS"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

echo "[1/7] Python deps installed..."
PYTHONPATH=src $PY -c "import bpd_mcp; print(f'  PASS  ({bpd_mcp.__version__})')" || { echo "  FAIL"; HAS_FAIL=1; }

echo "[2/7] Server imports (this is the check that used to fail on the DuckDB lock)..."
PYTHONPATH=src $PY -c "import bpd_mcp.server; print('  PASS')" || { echo "  FAIL"; HAS_FAIL=1; }

echo "[3/7] No leftover DuckDB warehouse holding a lock..."
# Purely informational. The BigQuery data layer takes no file lock, so a
# leftover file cannot break anything — it is just dead bytes now.
LEGACY=0
for f in "$HOME/.bpd-mcp/bpd.duckdb" "$HOME/.bpd-mcp/bpd.duckdb.wal" \
         "$HOME/.bpd-mcp/bpd.duckdb.ro" "$HOME/.bpd-mcp/bpd.duckdb.ro.wal"; do
    [ -e "$f" ] && LEGACY=1
done
if [ "$LEGACY" = "1" ]; then
    echo "  WARN: legacy DuckDB files remain in ~/.bpd-mcp. Nothing reads them."
    echo "        Safe to remove once no old bpd-mcp process is running:"
    echo "        rm -f ~/.bpd-mcp/bpd.duckdb*"
else
    echo "  PASS"
fi

echo "[4/7] Hermetic tests pass (no network, no credentials)..."
if $PY -c "import pytest" 2>/dev/null; then
    PYTHONPATH=src $PY -m pytest -q -m "not bq and not bq_live" 2>&1 | tail -2
    TEST_RC=${PIPESTATUS[0]}
    if [ "$TEST_RC" -ne 0 ]; then
        echo "  FAIL: pytest exit $TEST_RC"
        HAS_FAIL=1
    fi
else
    echo "  WARN: pytest not installed — skipping test step."
    echo "        Run 'uv sync --all-extras' to enable."
fi

echo "[5/7] Ruff clean..."
if $PY -c "import ruff" 2>/dev/null || command -v ruff >/dev/null 2>&1; then
    $PY -m ruff check src/ tests/ scripts/ >/dev/null 2>&1 \
        && echo "  PASS" \
        || { echo "  FAIL: run 'ruff check src/ tests/ scripts/' to see details"; HAS_FAIL=1; }
else
    echo "  WARN: ruff not installed — skipping lint step."
    echo "        Run 'uv sync --all-extras' to enable."
fi

echo "[6/7] Tool count matches EXPECTED_TOOL_COUNT..."
# `|| { ...; }`, not a bare command followed by `[ "$?" -ne 0 ]`: `set -e` is on,
# so a bare heredoc that exits non-zero aborts the whole script on the spot. The
# status line never ran, [7/7] never ran, and the "SOME CHECKS FAILED" banner
# never printed — the script died mid-output with exit 1 and no summary. Failures
# have to be CAUGHT to be accumulated.
PYTHONPATH=src $PY <<'PYEOF' || HAS_FAIL=1
from bpd_mcp.server import mcp
from bpd_mcp.tools.admin import EXPECTED_TOOL_COUNT

tools = sorted(mcp._tool_manager._tools.keys())
if len(tools) != EXPECTED_TOOL_COUNT:
    print(f'  FAIL: {len(tools)} tools registered, expected {EXPECTED_TOOL_COUNT}')
    print(f'        {tools}')
    raise SystemExit(1)
print(f'  PASS ({len(tools)} tools)')
PYEOF

echo "[7/7] BigQuery credential + reachability (skipped when unconfigured)..."
PYTHONPATH=src $PY <<'PYEOF' || HAS_FAIL=1
import os

if not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GCP_SA_KEY_B64")):
    print("  SKIP: neither GOOGLE_APPLICATION_CREDENTIALS nor GCP_SA_KEY_B64 is set.")
    print("        The MCP needs one of them; see README Quickstart.")
    raise SystemExit(0)

from bpd_mcp.bq import BigQueryWarehouse
from bpd_mcp.config import get_settings

s = get_settings()
try:
    wh = BigQueryWarehouse(project=s.bpd_bq_project, location=s.bpd_bq_location)
    _, rows = wh.execute_sql("SELECT SESSION_USER() AS u")
    wh.close()
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
    raise SystemExit(1)
print(f"  PASS (querying {s.bpd_bq_project}/{s.bpd_bq_location} as {rows[0][0]})")
PYEOF

echo
if [ "$HAS_FAIL" -eq 0 ]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
fi
echo "=== SOME CHECKS FAILED (see above) ==="
exit 1
