#!/bin/bash
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
export PYTHONUNBUFFERED=1
export DATA_DIR="${DATA_DIR:-/app/data}"
export LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$DATA_DIR" "$LOG_DIR"
APP_PORT="${PORT:-8080}"
echo "Starting uvicorn on port $APP_PORT"
python3 -u -m uvicorn dashboard.app:app --host 0.0.0.0 --port "$APP_PORT" &
sleep 3
echo "Starting agent..."
python3 -u main.py &
wait
