#!/usr/bin/env bash
# run.sh — запуск бэкенда + фронтенда
# Linux/macOS/Git Bash: bash run.sh

cd "$(dirname "$0")/backend" || exit 1

echo "[run] Installing dependencies..."
pip install -r requirements.txt

echo "[run] Starting backend on http://localhost:8000 ..."
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

echo "[run] Starting frontend on http://localhost:3000 ..."
cd ../frontend
python -m http.server 3000 &
FRONTEND_PID=$!

echo ""
echo "  Backend:  http://localhost:8000"
echo "  API docs: http://localhost:8000/docs"
echo "  Frontend: http://localhost:3000"
echo "Press Ctrl+C to stop"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait