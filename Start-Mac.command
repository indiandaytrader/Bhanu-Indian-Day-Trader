#!/bin/sh
# Start-Mac.command
cd "$(dirname "$0")" || exit 1
exec python3 start_dashboard.py
