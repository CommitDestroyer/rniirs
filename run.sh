cd backend
pip install -r requirements.txt
Start-Process python -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
Start-Sleep -Seconds 3
Start-Process "http://localhost:8000"
Start-Process "http://localhost:3000"
cd frontend
python -m http.server 3000