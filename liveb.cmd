@echo off
rem QA11 live browser service on http://127.0.0.1:8790 (live sign-in for Auto apply). Ctrl+C to stop.
rem Usage, from job_finder_api:  .\liveb   (and set LIVE_BROWSER_URL=http://127.0.0.1:8790 for the API)
setlocal
cd /d "%~dp0"
set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m uvicorn app.live_browser.service:app --host 127.0.0.1 --port 8790
