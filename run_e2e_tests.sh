#!/usr/bin/env bash
# Dedicated test runner script for Opaque-Box E2E Test Suite (Tiers 1-4)
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"
export FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE="false"

echo "================================================================================"
echo "  Executing Trading 212 Quantitative E2E Test Suite (Tiers 1-4)"
echo "================================================================================"

if [ -f "./venv/bin/python3" ]; then
    PYTHON_BIN="./venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

"${PYTHON_BIN}" "${SCRIPT_DIR}/tests/e2e/runner.py" "$@"
