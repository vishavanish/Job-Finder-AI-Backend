@echo off
rem One-command local stack: API + Celery worker + frontend. Ctrl+C stops all.
rem Usage (PowerShell or cmd, from job_finder_api): dev
setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%run_dev.py" %*
