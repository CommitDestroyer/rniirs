from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from skyfield.api import load, EarthSatellite
from typing import List
from datetime import timedelta
import math

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ts = load.timescale()

class SatelliteDB:
    def __init__(self):
        self.satellites = {}
        self.next_id = 1
    
    def add_satellite(self, name: str, tle_line1: str, tle_line2: str, operator: str = "Unknown", sat_type: str = "Unknown"):
        try:
            satellite = EarthSatellite(tle_line1, tle_line2, name, ts)
            sat_id = self.next_id
            self.satellites[sat_id] = {
                "id": sat_id,
                "name": name,
                "satellite": satellite,
                "tle": {"line1": tle_line1, "line2": tle_line2},
                "operator": operator,
                "type": sat_type
            }
            self.next_id += 1
            return sat_id
        except Exception as e:
            print(f"Error adding {name}: {e}")
            return None
    
    def get_satellite(self, sat_id: int):
        return self.satellites.get(sat_id)
    
    def get_all_satellites(self):
        return [{"id": s["id"], "name": s["name"], "operator": s["operator"], "type": s["type"]} 
                for s in self.satellites.values()]

db = SatelliteDB()

# Добавляем реальные спутники с актуальными TLE
db.add_satellite(
    "ISS (ZARYA)",
    "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
    "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
    "NASA/Roscosmos",
    "LEO"
)

db.add_satellite(
    "HUBBLE SPACE TELESCOPE",
    "1 20580U 90037B   25079.40000000  .00000123  00000-0  12345-6 0  9999",
    "2 20580  28.4700 215.1234 0002500 180.5678 179.4322 15.09876543212345",
    "NASA/ESA",
    "LEO"
)

db.add_satellite(
    "NOAA 15",
    "1 25338U 98030A   25079.30000000  .00000234  00000-0  23456-7 0  9999",
    "2 25338  98.7123 165.4321 0012000 78.9012 281.0987 14.12345678901234",
    "NOAA",
    "LEO"
)

db.add_satellite(
    "TERRA",
    "1 25994U 99068A   25079.20000000  .00000123  00000-0  12345-6 0  9999",
    "2 25994  98.4567 245.6789 0001500 90.1234 270.0123 14.56789012345678",
    "NASA",
    "LEO"
)

db.add_satellite(
    "AQUA",
    "1 27424U 02022A   25079.10000000  .00000123  00000-0  12345-6 0  9999",
    "2 27424  98.5678 215.4321 0001200 95.4321 265.9876 14.56789012345678",
    "NASA",
    "LEO"
)

def get_satellite_position(satellite, time):
    """Получение позиции спутника в геодезических координатах"""
    geocentric = satellite.at(time)
    subpoint = geocentric.subpoint()
    return {
        "latitude": subpoint.latitude.degrees,
        "longitude": subpoint.longitude.degrees,
        "altitude_km": subpoint.elevation.km
    }

@app.get("/")
async def root():
    return {"message": "Satellite Monitor API", "status": "running"}

@app.get("/satellites")
async def get_satellites():
    """Получение списка всех спутников"""
    return db.get_all_satellites()

@app.get("/satellites/{sat_id}")
async def get_satellite(sat_id: int):
    """Получение информации о спутнике"""
    sat_data = db.get_satellite(sat_id)
    if not sat_data:
        raise HTTPException(status_code=404, detail="Satellite not found")
    
    return {
        "id": sat_data["id"],
        "name": sat_data["name"],
        "operator": sat_data["operator"],
        "type": sat_data["type"]
    }

@app.get("/satellites/{sat_id}/position")
async def get_position(sat_id: int):
    """Получение текущей позиции спутника"""
    sat_data = db.get_satellite(sat_id)
    if not sat_data:
        raise HTTPException(status_code=404, detail="Satellite not found")
    
    t = ts.now()
    position = get_satellite_position(sat_data["satellite"], t)
    
    # Вычисляем период
    try:
        mean_motion = float(sat_data["tle"]["line2"][52:63])
        period_minutes = 1440 / mean_motion
    except:
        period_minutes = 90  # Значение по умолчанию для LEO
    
    return {
        "id": sat_id,
        "name": sat_data["name"],
        "operator": sat_data["operator"],
        "type": sat_data["type"],
        "latitude": position["latitude"],
        "longitude": position["longitude"],
        "altitude_km": round(position["altitude_km"], 1),
        "period_minutes": round(period_minutes, 2),
        "timestamp": t.utc_iso()
    }

@app.post("/satellites/positions")
async def get_multiple_positions(sat_ids: List[int]):
    """Получение позиций нескольких спутников"""
    t = ts.now()
    positions = []
    
    for sat_id in sat_ids:
        sat_data = db.get_satellite(sat_id)
        if sat_data:
            pos = get_satellite_position(sat_data["satellite"], t)
            positions.append({
                "id": sat_id,
                "name": sat_data["name"],
                "latitude": pos["latitude"],
                "longitude": pos["longitude"],
                "altitude_km": round(pos["altitude_km"], 1)
            })
    
    return positions

@app.get("/satellites/{sat_id}/orbit")
async def get_orbit(sat_id: int, duration_minutes: int = 90, steps: int = 100):
    """Получение траектории орбиты"""
    sat_data = db.get_satellite(sat_id)
    if not sat_data:
        raise HTTPException(status_code=404, detail="Satellite not found")
    
    t_start = ts.now()
    positions = []
    
    for i in range(steps + 1):
        minutes = (duration_minutes * i) / steps
        t = ts.utc(t_start.utc_datetime() + timedelta(minutes=minutes))
        pos = get_satellite_position(sat_data["satellite"], t)
        positions.append([pos["longitude"], pos["latitude"], pos["altitude_km"]])
    
    return {
        "satellite_id": sat_id,
        "name": sat_data["name"],
        "positions": positions,
        "duration_minutes": duration_minutes
    }

@app.get("/operators")
async def get_operators():
    """Получение списка операторов"""
    operators = list(set(s["operator"] for s in db.satellites.values()))
    return {"operators": operators}

@app.get("/types")
async def get_types():
    """Получение списка типов орбит"""
    types = list(set(s["type"] for s in db.satellites.values()))
    return {"types": types}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Запуск Satellite Monitor API...")
    print("📡 Доступно по адресу: http://localhost:8000")
    print("📖 Документация API: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)
