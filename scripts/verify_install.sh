#!/bin/bash
# scripts/verify_install.sh
#
# Run after `git pull` to verify the install is healthy. The script ensures dev
# dependencies (pytest, ruff) are installed by running `uv sync --all-extras` at
# the top — so `git pull && ./scripts/verify_install.sh` always works in one step.
# Exits 0 on pass, 1 on any failure.
#
# Each check prints PASS/WARN/FAIL with a one-line explanation. Network calls
# are avoided — this script verifies local install state only.

set -e
cd "$(dirname "$0")/.."

HAS_FAIL=0

echo "=== bpd-mcp install verification ==="

# Use whichever python launcher is available: prefer uv, then .venv/bin/python.
if command -v uv >/dev/null 2>&1; then
    PY="uv run python"
    # Patch #4 Issue 4: make sure dev extras (pytest, ruff) are installed before
    # the script tries to use them. Idempotent — fast no-op if already current.
    echo "[0/8] Ensuring dev dependencies are installed..."
    uv sync --all-extras --quiet 2>&1 | sed 's/^/  /' || {
        echo "  WARN: 'uv sync --all-extras' did not run cleanly; continuing"
    }
    echo "  PASS"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

echo "[1/8] Python deps installed..."
PYTHONPATH=src $PY -c "import bpd_mcp; print(f'  PASS  ({bpd_mcp.__version__})')" || { echo "  FAIL"; HAS_FAIL=1; }

echo "[2/8] No legacy .ro snapshot..."
if [ -f "$HOME/.bpd-mcp/bpd.duckdb.ro" ] || [ -f "$HOME/.bpd-mcp/bpd.duckdb.ro.wal" ]; then
    echo "  FAIL: legacy bpd.duckdb.ro / .ro.wal exists. Kill MCP processes and delete them."
    echo "        pkill -f bpd-mcp && rm -f ~/.bpd-mcp/bpd.duckdb.ro ~/.bpd-mcp/bpd.duckdb.ro.wal"
    HAS_FAIL=1
else
    echo "  PASS"
fi

echo "[3/8] No orphan MCP processes..."
if pgrep -f bpd-mcp >/dev/null 2>&1; then
    echo "  WARN: bpd-mcp processes are running. Restart Claude Desktop after pulling."
else
    echo "  PASS"
fi

echo "[4/8] Tests pass..."
if $PY -c "import pytest" 2>/dev/null; then
    PYTHONPATH=src $PY -m pytest -q 2>&1 | tail -2
    TEST_RC=${PIPESTATUS[0]}
    if [ "$TEST_RC" -ne 0 ]; then
        echo "  FAIL: pytest exit $TEST_RC"
        HAS_FAIL=1
    fi
else
    echo "  WARN: pytest not installed — skipping test step."
    echo "        Run 'uv sync --all-extras' to enable."
fi

echo "[5/8] Ruff clean..."
if $PY -c "import ruff" 2>/dev/null || command -v ruff >/dev/null 2>&1; then
    $PY -m ruff check src/ tests/ scripts/ >/dev/null 2>&1 \
        && echo "  PASS" \
        || { echo "  FAIL: run 'ruff check src/ tests/ scripts/' to see details"; HAS_FAIL=1; }
else
    echo "  WARN: ruff not installed — skipping lint step."
    echo "        Run 'uv sync --all-extras' to enable."
fi

echo "[6/8] Warehouse schema up to date..."
PYTHONPATH=src $PY <<'PYEOF'
import os
import duckdb

db = os.path.expanduser('~/.bpd-mcp/bpd.duckdb')
if not os.path.exists(db):
    print('  (no warehouse yet; will be built on first sync)')
else:
    try:
        con = duckdb.connect(db, read_only=True)
        cols = [
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = '_file_ledger'"
            ).fetchall()
        ]
        con.close()
    except Exception as e:
        print(f'  FAIL: could not open {db}: {type(e).__name__}: {e}')
        raise SystemExit(1)
    required = [
        'file_id', 'file_name', 'folder_id', 'dataset', 'file_date', 'bytes',
        'fingerprint', 'downloaded_at', 'loaded_at', 'row_count', 'status',
        'error_message', 'parse_method',
    ]
    missing = [c for c in required if c not in cols]
    if missing:
        print(f'  FAIL: _file_ledger missing columns: {missing}')
        raise SystemExit(1)
    print(f'  PASS ({len(cols)} columns)')
PYEOF
[ "$?" -ne 0 ] && HAS_FAIL=1

echo "[7/8] Tokens file 0600..."
if [ -f "$HOME/.bpd-mcp/tokens.json" ]; then
    PERMS=$(stat -c "%a" "$HOME/.bpd-mcp/tokens.json" 2>/dev/null || stat -f "%A" "$HOME/.bpd-mcp/tokens.json")
    if [ "$PERMS" = "600" ]; then
        echo "  PASS"
    else
        echo "  FAIL: tokens.json mode is $PERMS, expected 600"
        HAS_FAIL=1
    fi
else
    echo "  (no token yet; run uv run bpd-bootstrap to create)"
fi

echo "[8/8] MCP entry point..."
# Importable + tool registration succeeds is enough; we don't actually start stdio.
PYTHONPATH=src $PY -c "
from bpd_mcp import server
n = len(server.mcp._tool_manager._tools)
if n != 20:
    print(f'  FAIL: tool count={n}, expected 20')
    raise SystemExit(1)
print(f'  PASS ({n} tools registered)')
" || HAS_FAIL=1

echo ""
if [ "$HAS_FAIL" -ne 0 ]; then
    echo "=== FAIL: some checks did not pass ==="
    exit 1
fi
echo "=== All verifications passed ==="
exit 0
