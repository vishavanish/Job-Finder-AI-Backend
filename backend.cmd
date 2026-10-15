@echo off
rem Backend only: API (http://127.0.0.1:8000) + Celery worker in one terminal. Ctrl+C stops both.
rem Usage, from job_finder_api:  .\backend
call "%~dp0dev.cmd" --backend-only %*
