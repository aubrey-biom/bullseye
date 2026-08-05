#!/usr/bin/env bash
# Install the daily bpd-sync launchd job on macOS (Patch #15).
#
# Usage:
#   ./scripts/install_launchd.sh            # default: 07:05 local time
#   ./scripts/install_launchd.sh 6 30       # 06:30 local time
#   ./scripts/install_launchd.sh --uninstall
#
# Idempotent: re-running replaces the existing job.
set -euo pipefail

LABEL="com.biom.bpd-sync"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_DIR}/scripts/com.biom.bpd-sync.plist.template"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Uninstalled ${LABEL}."
    exit 0
fi

HOUR="${1:-7}"
MINUTE="${2:-5}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: launchd is macOS-only. On Linux, use a systemd timer or cron" >&2
    echo "       running: <uv> run --directory ${REPO_DIR} bpd-sync" >&2
    exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    echo "ERROR: 'uv' not found on PATH. Install it (brew install uv) first." >&2
    exit 1
fi

if [[ ! -f "${REPO_DIR}/.env" ]]; then
    echo "WARNING: ${REPO_DIR}/.env not found — bpd-sync needs KITEWORKS_USERNAME/" >&2
    echo "         KITEWORKS_PASSWORD there (or already-valid ~/.bpd-mcp/tokens.json)." >&2
fi

echo "Verifying bpd-sync runs (dry-run, no writes)..."
if ! "$UV_BIN" run --directory "$REPO_DIR" bpd-sync --dry-run --skip-health; then
    echo "ERROR: dry-run failed — fix that before scheduling the job." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.bpd-mcp/logs"
sed -e "s|__UV_BIN__|${UV_BIN}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__HOME__|${HOME}|g" \
    -e "s|__HOUR__|${HOUR}|g" \
    -e "s|__MINUTE__|${MINUTE}|g" \
    "$TEMPLATE" > "$PLIST_DEST"

# bootout first so re-running this script cleanly replaces the job.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"

printf 'Installed %s — daily at %02d:%02d local time.\n' "$LABEL" "$HOUR" "$MINUTE"
cat <<EOF

Useful commands:
  launchctl print gui/$(id -u)/${LABEL} | head -20   # status
  launchctl kickstart -p gui/$(id -u)/${LABEL}       # run it right now
  tail -f ~/.bpd-mcp/logs/bpd-sync.out.log           # watch results
  ${REPO_DIR}/scripts/install_launchd.sh --uninstall # remove

Exit codes in the log: 0 ok · 1 file(s) failed · 2 health check failed
                       3 fatal · 75 warehouse locked (MCP running) — skipped
EOF
