#!/bin/bash

set -e

echo "Applying database migrations..."
alembic upgrade head

# Derive the queue list from settings so the worker always consumes the
# exact queues the API publishes to (default/apply, or *_dev outside
# production). A hardcoded "-Q default" left /apply tasks pending forever.
QUEUES=$(python -c "from app.core.config import get_settings; print(get_settings().worker_queues)" 2>/dev/null)

echo "Starting Celery worker on queues: ${QUEUES}"

celery -A app.core.celery_app:celery_app worker \
    --loglevel=info \
    --concurrency=2 \
    -Q "${QUEUES}" &

# Exactly ONE beat process per deployment (it schedules job alerts; P3.2).
# On the Oracle VM with separate systemd units, run beat as its own unit
# (job_finder_beat.service) instead and set START_BEAT=0 here.
if [ "${START_BEAT:-1}" = "1" ]; then
    echo "Starting Celery beat..."
    celery -A app.core.celery_app:celery_app beat --loglevel=info &
fi

echo "Starting FastAPI..."

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}"
