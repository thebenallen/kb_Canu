#!/bin/bash
# ---------------------------------------------------------------------------
# entrypoint.sh
#
# KBase SDK module entrypoint.  Starts the gevent/WSGI JSON-RPC server and
# handles graceful shutdown on SIGTERM/SIGINT.
#
# NOTE: Do NOT use "set -e" at the top level — the KBase SDK test harness
# calls this script in environments where optional files may be absent and
# a bare non-zero exit would abort the container startup.
# ---------------------------------------------------------------------------

log()  { echo "[entrypoint $(date -u +%H:%M:%S)] $*"; }
err()  { echo "[entrypoint $(date -u +%H:%M:%S)] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

# ---- Source the KBase deployment environment if present -------------------
# This file exists in production KBase containers but NOT during kb-sdk test.
# We guard explicitly and never use set -e around this.
if [ -f /kb/deployment/user-env.sh ]; then
    # shellcheck disable=SC1091
    . /kb/deployment/user-env.sh
    log "Sourced /kb/deployment/user-env.sh"
else
    log "INFO: /kb/deployment/user-env.sh not found — continuing without it (normal during kb-sdk test)"
fi

# ---- Validate required environment ----------------------------------------
if [ -z "${SDK_CALLBACK_URL:-}" ]; then
    log "WARNING: SDK_CALLBACK_URL not set — KBase service calls will fail"
fi

if [ -z "${KB_AUTH_TOKEN:-}" ]; then
    log "WARNING: KB_AUTH_TOKEN not set — authenticated calls will fail"
fi

# ---- Ensure scratch directory exists --------------------------------------
SCRATCH="${SCRATCH:-/kb/module/work/tmp}"
mkdir -p "${SCRATCH}"
log "Scratch directory: ${SCRATCH}"

# ---- Verify Canu is available ---------------------------------------------
if ! command -v canu >/dev/null 2>&1; then
    die "Canu binary not found on PATH. The Docker image may be broken."
fi
CANU_VER=$(canu --version 2>&1 | head -1 || echo "unknown")
log "Canu: ${CANU_VER}"

# ---- Graceful shutdown trap ------------------------------------------------
PID_FILE="/tmp/kb_canu_server.pid"

cleanup() {
    log "Caught shutdown signal — stopping server"
    if [ -f "${PID_FILE}" ]; then
        kill "$(cat "${PID_FILE}")" 2>/dev/null || true
        rm -f "${PID_FILE}"
    fi
    log "Done."
}
trap cleanup TERM INT QUIT

# ---- Start the JSON-RPC server --------------------------------------------
cd /kb/module
log "Starting kb_canu JSON-RPC server on port 5000..."

sh ./scripts/start_server.sh &
SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"
log "Server PID: ${SERVER_PID}"

wait "${SERVER_PID}"
EXIT_CODE=$?
log "Server exited with code ${EXIT_CODE}"
exit "${EXIT_CODE}"
