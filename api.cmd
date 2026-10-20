@echo off
rem Backend API on http://127.0.0.1:8000 (local dev settings). Ctrl+C to stop.
rem Usage, from job_finder_api:  .\api
setlocal
cd /d "%~dp0"
set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set ENVIRONMENT=development
set DATABASE_URL=sqlite:///./output/dev.db
set EMAIL_BACKEND=console
set REFRESH_COOKIE_SECURE=false
set REFRESH_COOKIE_SAMESITE=lax
if not exist output mkdir output
"%PY%" -m alembic upgrade head || exit /b 1
"%PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
