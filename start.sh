#!/bin/bash
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
APP_PORT="${PORT:-8080}"
echo "Starting uvicorn on port $APP_PORT"
python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port "$APP_PORT" &
sleep 3
echo "Starting agent..."
python3 main.py &
wait