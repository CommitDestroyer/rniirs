@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Installing dependencies...
pip install -r backend\requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)

echo [2/3] Starting backend on http://localhost:8000 ...
start "Backend" cmd /k "cd /d "%~dp0backend" && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

timeout /t 3 /nobreak >nul

echo [3/3] Starting frontend on http://localhost:3000 ...
start "Frontend" cmd /k "cd /d "%~dp0backend\frontend" && python -m http.server 3000"

timeout /t 2 /nobreak >nul

start http://localhost:8000/docs
start http://localhost:3000

echo.
echo Backend:  http://localhost:8000
echo API docs: http://localhost:8000/docs
echo Frontend: http://localhost:3000