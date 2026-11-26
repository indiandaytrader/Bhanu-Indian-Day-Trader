@echo off
REM Bhanu Indian Day Trader — run the dashboard locally (Windows).
REM Optional: set MARKETAUX_API_KEY=... (free key from marketaux.com)
REM for extra news + per-entity sentiment in the news feed.
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m web.app
