@echo off
rem Start the Audit Workbench (serves the built SPA + API on port 8000).
cd /d "%~dp0"
uv run --no-project uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
