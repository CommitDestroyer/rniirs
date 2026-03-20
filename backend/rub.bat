@echo off
cd /d "%~dp0"

echo Installing dependencies...
pip install -r backend\requirements.txt

echo Starting backend (port 8000)...
start "" cmd /k "cd /d "%~dp0backend" && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

timeout /t 3 >nul

echo Starting frontend (port 3000)...
start "" cmd /k "cd /d "%~dp0backend\frontend" && python -m http.server 3000"

timeout /t 2 >nul

start http://localhost:8000/docs
start http://localhost:3000