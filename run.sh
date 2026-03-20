#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "[1/3] Installing dependencies..."
pip install -r backend/requirements.txt

echo "[2/3] Starting backend on :8000..."
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
cd ..

sleep 2

echo "[3/3] Starting frontend on :3000..."
cd backend/frontend
python -m http.server 3000 &
FRONTEND_PID=$!
cd ../..

echo ""
echo "Backend:  http://localhost:8000"
echo "API docs: http://localhost:8000/docs"
echo "Frontend: http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both servers"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait