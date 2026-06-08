#!/bin/bash
echo "Starting on PORT: $PORT"
python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8000} &
python3 main.py &
wait