#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -f app.pid ] && kill "$(cat app.pid)" 2>/dev/null; then echo "Stopped PID $(cat app.pid)"; else echo "Not running"; fi
rm -f app.pid
