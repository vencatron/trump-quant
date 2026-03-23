#!/usr/bin/env bash
# TrumpQuant Dashboard — launch script
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================"
echo "  TrumpQuant Dashboard"
echo "  http://localhost:7799"
echo "============================================"

# Ensure dependencies
pip install -q fastapi uvicorn sse-starlette yfinance 2>/dev/null || true

# Ensure data directory exists
mkdir -p data

exec python3 dashboard_server.py
