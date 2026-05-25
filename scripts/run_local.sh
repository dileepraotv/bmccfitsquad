#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_local.sh — start the full BMCC bot stack for local development
#
# Starts:
#   1. uvicorn (FastAPI web server) on port 8000
#   2. celery worker (background task processor)
#
# Usage:
#   ./scripts/run_local.sh              # use default port 8000
#   PORT=9000 ./scripts/run_local.sh    # use a custom port
#
# Prerequisites:
#   • .env file exists in the project root (copy from .env.example and fill in)
#   • Virtual environment is activated, OR the script will try .venv/bin/python
#
# Ctrl+C stops both processes cleanly.
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve project root (the directory that contains this script's parent)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
ENV_FILE="${PROJECT_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo ""
  echo "  ERROR: .env file not found at ${ENV_FILE}"
  echo ""
  echo "  Fix:"
  echo "    cp .env.example .env"
  echo "    # Then fill in your credentials"
  echo ""
  exit 1
fi

echo "Loading environment from ${ENV_FILE} ..."
# Export every non-comment, non-blank line.
# We use 'set -a / set +a' so all variables are auto-exported.
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

# Override APP_ENV to development so docs are visible and polling is available
export APP_ENV="${APP_ENV:-development}"

# ---------------------------------------------------------------------------
# Resolve Python / uvicorn / celery binaries
# ---------------------------------------------------------------------------
if command -v python3 &>/dev/null && python3 -c "import uvicorn" 2>/dev/null; then
  PYTHON="python3"
elif [[ -f "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
else
  echo "ERROR: Python with uvicorn not found."
  echo "  Activate your virtual environment:  source .venv/bin/activate"
  exit 1
fi

UVICORN="${PYTHON} -m uvicorn"
CELERY="${PYTHON} -m celery"

PORT="${PORT:-8000}"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   BMCC Bot — local development       ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  APP_ENV  : ${APP_ENV}"
echo "  BASE_URL : ${BASE_URL:-http://localhost:${PORT}}"
echo "  DB       : ${DATABASE_URL:-<not set>}"
echo "  Redis    : ${REDIS_URL:-<not set>}"
echo ""
echo "  Web  → http://localhost:${PORT}"
echo "  Docs → http://localhost:${PORT}/docs"
echo ""

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------
# Track child PIDs so we can kill them on exit
WEB_PID=""
WORKER_PID=""

cleanup() {
  echo ""
  echo "Stopping processes..."
  [[ -n "${WEB_PID}"    ]] && kill "${WEB_PID}"    2>/dev/null || true
  [[ -n "${WORKER_PID}" ]] && kill "${WORKER_PID}" 2>/dev/null || true
  wait "${WEB_PID}"    2>/dev/null || true
  wait "${WORKER_PID}" 2>/dev/null || true
  echo "All processes stopped. Goodbye."
}

trap cleanup INT TERM EXIT

# ---------------------------------------------------------------------------
# Start uvicorn (web process)
# ---------------------------------------------------------------------------
echo "Starting FastAPI (uvicorn) on port ${PORT} ..."
${UVICORN} app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --reload \
  --reload-dir "${PROJECT_ROOT}/app" \
  2>&1 | sed 's/^/[web]    /' &
WEB_PID=$!

# Give uvicorn a moment to start so its banner doesn't interleave with Celery's
sleep 1

# ---------------------------------------------------------------------------
# Start Celery worker
# ---------------------------------------------------------------------------
echo "Starting Celery worker ..."
${CELERY} -A app.celery_app worker \
  --loglevel=info \
  --concurrency=2 \
  2>&1 | sed 's/^/[worker] /' &
WORKER_PID=$!

echo ""
echo "Both processes running. Press Ctrl+C to stop."
echo ""

# ---------------------------------------------------------------------------
# Wait — the trap will fire when either child exits or Ctrl+C is pressed
# ---------------------------------------------------------------------------
wait "${WEB_PID}" "${WORKER_PID}"
