#!/bin/bash
set -e

echo "Starting on PORT: ${PORT:-8000}"

# Start uvicorn in background
python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8000} &
UVICORN_PID=$!

echo "Uvicorn PID: $UVICORN_PID"

# Give uvicorn 3 seconds to bind
sleep 3

echo "Starting agent..."

# Start agent in background
python3 main.py &
AGENT_PID=$!

echo "Agent PID: $AGENT_PID"

# Wait for both
wait $UVICORN_PID $AGENT_PID