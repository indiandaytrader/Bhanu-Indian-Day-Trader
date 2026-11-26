#!/bin/bash
# Bhanu Indian Day Trader — run the dashboard locally.
# Optional: export MARKETAUX_API_KEY=... (free key from marketaux.com)
# for extra news + per-entity sentiment in the news feed.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -m web.app
