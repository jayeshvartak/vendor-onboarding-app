#!/usr/bin/env bash
# Start the Vendor Onboarding app in the background (default 0.0.0.0:8787).
cd "$(dirname "$0")"
if [ -f app.pid ] && kill -0 "$(cat app.pid)" 2>/dev/null; then echo "Already running (PID $(cat app.pid))"; exit 0; fi
if [ ! -d .venv ]; then python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; fi
nohup .venv/bin/python app.py >> app.log 2>&1 &
echo $! > app.pid
echo "Started PID $(cat app.pid) on http://127.0.0.1:${VO_PORT:-8787} (log: app.log)"
