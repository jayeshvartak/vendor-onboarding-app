#!/usr/bin/env bash
# Start Vendor Master in the background (web default 0.0.0.0:8787, MCP streamable HTTP default 0.0.0.0:8788/mcp).
cd "$(dirname "$0")"
if [ -f app.pid ] && kill -0 "$(cat app.pid)" 2>/dev/null; then echo "Already running (PID $(cat app.pid))"; exit 0; fi
if [ ! -d .venv ]; then python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; fi
nohup .venv/bin/python app.py >> app.log 2>&1 &
echo $! > app.pid
echo "Started PID $(cat app.pid) on http://127.0.0.1:${VO_PORT:-8787} and MCP http://127.0.0.1:${VO_MCP_PORT:-8788}${VO_MCP_PATH:-/mcp} (log: app.log)"
