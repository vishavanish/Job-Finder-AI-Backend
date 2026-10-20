@echo off
rem Make an account admin in the LOCAL dev database (output/dev.db) only.
rem Usage, from job_finder_api:  .\admin you@example.com
rem Creates the account if it doesn't exist (then sign up / sign in with that email).
setlocal
if "%~1"=="" (
  echo Usage: .\admin you@example.com
  exit /b 1
)
cd /d "%~dp0"
set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set ENVIRONMENT=development
set DATABASE_URL=sqlite:///./output/dev.db
if not exist output mkdir output
"%PY%" -m alembic upgrade head || exit /b 1
"%PY%" -m app.scripts.set_role %1 admin
