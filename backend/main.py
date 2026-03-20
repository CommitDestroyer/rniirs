from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from skyfield.api import load, EarthSatellite, Topos, load_constellation_map
from skyfield.position import position_of_radec
from skyfield.timelib import Time
import numpy as np
from typing import List, Dict, Optional
import io
import re
from datetime import datetime, timedelta
import math

app = FastAPI()

# CORS для фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Загрузка данных времени
ts = load.timescale()

# Хранилище спутников
class SatelliteDB:
    def __init__(self):
        self.satellites = {}  # id -> {name, satellite_object, tle, operator, type}
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
            print(f"Error adding satellite {name}: {e}")
            return None
    
    def get_satellite(self, sat_id: int):
        return self.satellites.get(sat_id)
    
    def get_all_satellites(self):
        return [{"id": s["id"], "name": s["name"], "operator": s["operator"], "type": s["type"]} 
                for s in self.satellites.values()]

db = SatelliteDB()

def load_default_tle():
    """Загрузка TLE из файла"""
    try:
        with open("tle_data.txt", "r") as f:
            content = f.read()
        parse_tle_content(content)
    except FileNotFoundError:
        print("tle_data.txt not found, using hardcoded data")
        # Добавляем МКС как fallback
        db.add_satellite(
            "ISS (ZARYA)",
            "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
            "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
            "NASA/Roscosmos",
            "LEO"
        )

def parse_tle_content(content: str):
    """Парсинг TLE файла"""
    lines = content.strip().split('\n')
    i = 0
    while i < len(lines):
        if i + 2 < len(lines):
            name = lines[i].strip()
            line1 = lines[i + 1].strip()
            line2 = lines[i + 2].strip()
            
            if line1.startswith('1') and line2.startswith('2'):
                # Определяем тип орбиты по наклонению и периоду
                try:
                    mean_motion = float(line2[52:63])
                    period = 1440 / mean_motion  # minutes
                    if period < 128:
                        sat_type = "LEO"
                    elif period < 225:
                        sat_type = "MEO"
                    else:
                        sat_type = "GEO"
                except:
                    sat_type = "Unknown"
                
                db.add_satellite(name, line1, line2, "Various", sat_type)
                i += 3
                continue
        i += 1

def calculate_orbit_type(altitude_km: float) -> str:
    """Определение типа орбиты по высоте"""
    if altitude_km < 2000:
        return "LEO"
    elif altitude_km < 35786:
        return "MEO"
    else:
        return "GEO"

def get_satellite_position(satellite, time):
    """Получение позиции спутника в геодезических координатах"""
    geocentric = satellite.at(time)
    subpoint = geocentric.subpoint()
    return {
        "latitude": subpoint.latitude.degrees,
        "longitude": subpoint.longitude.degrees,
        "altitude_km": subpoint.elevation.km,
        "timestamp": time.utc_iso()
    }

@app.get("/")
async def root():
    return {"message": "Satellite Monitor API", "status": "running"}

@app.get("/satellites")
async def get_satellites(operator: Optional[str] = None, sat_type: Optional[str] = None):
    """Получение списка спутников с фильтрацией"""
    satellites = db.get_all_satellites()
    
    if operator:
        satellites = [s for s in satellites if s["operator"].lower() == operator.lower()]
    if sat_type:
        satellites = [s for s in satellites if s["type"].lower() == sat_type.lower()]
    
    return satellites

@app.get("/satellites/{sat_id}/position")
async def get_position(sat_id: int, time: Optional[str] = None):
    """Получение текущей позиции спутника"""
    sat_data = db.get_satellite(sat_id)
    if not sat_data:
        raise HTTPException(status_code=404, detail="Satellite not found")
    
    if time:
        # Парсим время из строки
        dt = datetime.fromisoformat(time.replace('Z', '+00:00'))
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    else:
        t = ts.now()
    
    position = get_satellite_position(sat_data["satellite"], t)
    position["name"] = sat_data["name"]
    position["operator"] = sat_data["operator"]
    position["type"] = sat_data["type"]
    position["orbit_type"] = calculate_orbit_type(position["altitude_km"])
    
    # Вычисляем период (примерно)
    mean_motion = float(sat_data["tle"]["line2"][52:63])
    period_minutes = 1440 / mean_motion
    position["period_minutes"] = round(period_minutes, 2)
    
    return position

@app.post("/satellites/positions")
async def get_multiple_positions(sat_ids: List[int]):
    """Получение позиций нескольких спутников"""
    t = ts.now()
    positions = []
    
    for sat_id in sat_ids:
        sat_data = db.get_satellite(sat_id)
        if sat_data:
            pos = get_satellite_position(sat_data["satellite"], t)
            pos["id"] = sat_id
            pos["name"] = sat_data["name"]
            positions.append(pos)
    
    return positions

@app.get("/satellites/{sat_id}/orbit")
async def get_orbit(sat_id: int, duration_minutes: int = 90, steps: int = 360):
    """Получение траектории орбиты на заданный период"""
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
    
    return {"positions": positions, "duration_minutes": duration_minutes}

@app.get("/satellites/{sat_id}/passes")
async def get_passes(sat_id: int, lat: float, lon: float, days: int = 3):
    """Прогноз пролетов над заданной точкой"""
    sat_data = db.get_satellite(sat_id)
    if not sat_data:
        raise HTTPException(status_code=404, detail="Satellite not found")
    
    ground_station = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))
    
    passes = []
    try:
        t, events = sat_data["satellite"].find_events(ground_station, t0, t1, altitude_degrees=0)
        
        for ti, event in zip(t, events):
            if event == 0:  # Восход
                rise_time = ti.utc_datetime()
                # Ищем закат для этого прохода
                # Упрощенно: берем следующий закат
                passes.append({
                    "rise_time": rise_time.isoformat(),
                    "max_altitude": None,  # Можно вычислить, но для простоты опустим
                    "duration_minutes": None
                })
    except Exception as e:
        print(f"Error calculating passes: {e}")
        return []
    
    return passes[:10]  # Возвращаем не более 10 пролетов

@app.post("/upload-tle")
async def upload_tle(file: UploadFile = File(...)):
    """Загрузка TLE из файла"""
    content = await file.read()
    text_content = content.decode('utf-8')
    parse_tle_content(text_content)
    return {"message": f"TLE file processed", "satellites_count": len(db.satellites)}

@app.get("/satellite-types")
async def get_types():
    """Получение типов орбит для фильтрации"""
    types = list(set(s["type"] for s in db.satellites.values()))
    return {"types": types}

@app.get("/operators")
async def get_operators():
    """Получение операторов для фильтрации"""
    operators = list(set(s["operator"] for s in db.satellites.values()))
    return {"operators": operators}

# Загружаем начальные данные при старте
load_default_tle()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)