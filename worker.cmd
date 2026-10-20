@echo off
rem Celery worker for the local dev queues (default_dev, apply_dev, maintenance_dev). Ctrl+C to stop.
rem Usage, from job_finder_api:  .\worker
setlocal
cd /d "%~dp0"
set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set ENVIRONMENT=development
set DATABASE_URL=sqlite:///./output/dev.db
"%PY%" -m celery -A app.core.celery_app:celery_app worker --pool=solo --loglevel=info -Q default_dev,apply_dev,maintenance_dev,email_dev
