#!/bin/bash

set -e

echo "Starting Celery worker..."

celery -A app.core.celery_app worker \
    --loglevel=info \
    --concurrency=2 &

CELERY_PID=$!

echo "Celery started with PID $CELERY_PID"

echo "Starting FastAPI..."

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port ${PORT:-10000}