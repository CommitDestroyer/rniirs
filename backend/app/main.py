from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from skyfield.api import EarthSatellite, Topos, load

from config import get_settings

# ─────────────────────────── globals ────────────────────────────────────────

cfg = get_settings()
ts  = load.timescale()

# ──────────────────────────── DB layer ──────────────────────────────────────

class SatelliteDB:
    """In-memory хранилище с индексами для быстрой фильтрации."""

    def __init__(self) -> None:
        self._data:       dict[int, dict]       = {}
        self._by_type:    dict[str, set[int]]   = {}
        self._by_operator: dict[str, set[int]]  = {}
        self._next_id = 1
        self._pos_cache:  dict[int, tuple[float, dict]] = {}  # id → (ts, pos)

    # ── write ────────────────────────────────────────────────────────────────

    def add(self, name: str, l1: str, l2: str,
            operator: str = "Unknown", sat_type: str = "Unknown") -> Optional[int]:
        try:
            sat = EarthSatellite(l1, l2, name, ts)
        except Exception as e:
            print(f"[TLE] skip {name}: {e}")
            return None

        sid = self._next_id
        self._next_id += 1
        rec = {
            "id": sid, "name": name, "satellite": sat,
            "line1": l1, "line2": l2,
            "operator": operator, "type": sat_type,
        }
        self._data[sid] = rec
        self._by_type.setdefault(sat_type, set()).add(sid)
        self._by_operator.setdefault(operator, set()).add(sid)
        return sid

    def clear(self) -> None:
        self._data.clear()
        self._by_type.clear()
        self._by_operator.clear()
        self._pos_cache.clear()
        self._next_id = 1

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, sid: int) -> Optional[dict]:
        return self._data.get(sid)

    def list(self, operator: Optional[str] = None,
             sat_type: Optional[str] = None) -> list[dict]:
        ids: Optional[set[int]] = None

        if operator:
            ids = self._by_operator.get(operator, set())
        if sat_type:
            by_t = self._by_type.get(sat_type, set())
            ids = by_t if ids is None else ids & by_t

        subset = (self._data[i] for i in ids) if ids is not None else self._data.values()
        return [
            {"id": r["id"], "name": r["name"],
             "operator": r["operator"], "type": r["type"]}
            for r in subset
        ]

    def all_ids(self) -> list[int]:
        return list(self._data.keys())

    def types(self) -> list[str]:
        return list(self._by_type.keys())

    def operators(self) -> list[str]:
        return list(self._by_operator.keys())

    def count(self) -> int:
        return len(self._data)

    # ── position cache ───────────────────────────────────────────────────────

    def cached_position(self, sid: int) -> Optional[dict]:
        entry = self._pos_cache.get(sid)
        if entry and (time.monotonic() - entry[0]) < cfg.POSITION_CACHE_TTL_S:
            return entry[1]
        return None

    def set_position_cache(self, sid: int, pos: dict) -> None:
        self._pos_cache[sid] = (time.monotonic(), pos)


db = SatelliteDB()

# ─────────────────────────── orbital math ───────────────────────────────────

def _orbit_type_by_period(period_min: float) -> str:
    alt_km = ((period_min / (2 * math.pi) * 6371 ** 1.5) ** (2 / 3)) - 6371
    if alt_km < cfg.LEO_MAX_KM:
        return "LEO"
    if alt_km < cfg.MEO_MAX_KM:
        return "MEO"
    return "GEO"


def _period_min(line2: str) -> float:
    return 1440.0 / float(line2[52:63])


def _satellite_position(sat: EarthSatellite, t) -> dict:
    sub = sat.at(t).subpoint()
    return {
        "lat": sub.latitude.degrees,
        "lon": sub.longitude.degrees,
        "alt_km": round(sub.elevation.km, 2),
        "timestamp": t.utc_iso(),
    }


def _elevation_azimuth(sat: EarthSatellite, observer: Topos, t) -> tuple[float, float]:
    """Возвращает (elevation_deg, azimuth_deg) спутника над наблюдателем."""
    diff = sat - observer
    topocentric = diff.at(t)
    alt, az, _ = topocentric.altaz()
    return alt.degrees, az.degrees


def coverage_circle(lat_deg: float, lon_deg: float,
                    alt_km: float, min_el_deg: float = 0.0) -> list[list[float]]:
    """
    Вычисляет полигон зоны радиовидимости.
    Возвращает список [lon, lat] по кругу.
    """
    Re = 6371.0
    # Полуугол зоны покрытия (центральный угол от надира)
    half_angle = math.degrees(
        math.acos(Re / (Re + alt_km) * math.cos(math.radians(min_el_deg)))
    ) - min_el_deg

    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    ha_r  = math.radians(half_angle)

    points: list[list[float]] = []
    step = cfg.COVERAGE_STEP_DEG
    for bearing_deg in np.arange(0, 360, step):
        b = math.radians(bearing_deg)
        plat = math.asin(
            math.sin(lat_r) * math.cos(ha_r)
            + math.cos(lat_r) * math.sin(ha_r) * math.cos(b)
        )
        plon = lon_r + math.atan2(
            math.sin(b) * math.sin(ha_r) * math.cos(lat_r),
            math.cos(ha_r) - math.sin(lat_r) * math.sin(plat),
        )
        points.append([round(math.degrees(plon), 4), round(math.degrees(plat), 4)])
    points.append(points[0])   # замыкаем кольцо
    return points

# ─────────────────────────── TLE loading ────────────────────────────────────

def _classify_and_add(name: str, l1: str, l2: str, operator: str = "Various") -> None:
    try:
        period = _period_min(l2)
        sat_type = _orbit_type_by_period(period)
    except Exception:
        sat_type = "Unknown"
    db.add(name, l1, l2, operator, sat_type)


def parse_tle_text(content: str, operator: str = "Various") -> int:
    """Парсит 3LE-формат, возвращает количество добавленных."""
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    added = 0
    i = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            _classify_and_add(name, l1, l2, operator)
            added += 1
            i += 3
        else:
            i += 1
    return added


async def fetch_tle_from_celestrak(category: str) -> int:
    url = cfg.TLE_SOURCES.get(category)
    if not url:
        return 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
        return parse_tle_text(r.text, operator=category.upper())
    except Exception as e:
        print(f"[Celestrak] {category}: {e}")
        return 0


def load_default_tle() -> None:
    try:
        with open(cfg.TLE_LOCAL_FILE, "r", encoding="utf-8") as f:
            count = parse_tle_text(f.read())
        print(f"[TLE] loaded {count} satellites from {cfg.TLE_LOCAL_FILE}")
    except FileNotFoundError:
        print("[TLE] local file not found — using ISS fallback")
        db.add(
            "ISS (ZARYA)",
            "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
            "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
            "NASA/Roscosmos", "LEO",
        )

# ─────────────────────────── WebSocket hub ──────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._sockets.discard(ws)

    async def broadcast(self, payload: Any) -> None:
        dead: set[WebSocket] = set()
        for ws in self._sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._sockets -= dead

    @property
    def count(self) -> int:
        return len(self._sockets)


manager = ConnectionManager()


async def _broadcast_loop() -> None:
    while True:
        if manager.count:
            t = ts.now()
            positions = []
            for sid, rec in list(db._data.items())[:cfg.BULK_POSITION_LIMIT]:
                pos = _satellite_position(rec["satellite"], t)
                pos.update({"id": sid, "name": rec["name"]})
                positions.append(pos)
            await manager.broadcast({"type": "positions", "data": positions})
        await asyncio.sleep(cfg.WS_BROADCAST_INTERVAL_S)

# ─────────────────────────── app lifecycle ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_default_tle()
    task = asyncio.create_task(_broadcast_loop())
    yield
    task.cancel()

app = FastAPI(
    title=cfg.APP_NAME,
    version=cfg.APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── endpoints ──────────────────────────────────────

@app.get("/")
async def root():
    return {"app": cfg.APP_NAME, "version": cfg.APP_VERSION,
            "satellites": db.count()}


# ── list / filter ─────────────────────────────────────────────────────────

@app.get("/satellites")
async def get_satellites(
    operator: Optional[str] = None,
    sat_type: Optional[str] = Query(None, alias="type"),
    limit: int = Query(200, le=cfg.MAX_SIMULTANEOUS_SATELLITES),
    offset: int = 0,
):
    result = db.list(operator=operator, sat_type=sat_type)
    return result[offset: offset + limit]


@app.get("/satellite-types")
async def get_types():
    return {"types": db.types()}


@app.get("/operators")
async def get_operators():
    return {"operators": db.operators()}


# ── single satellite ──────────────────────────────────────────────────────

@app.get("/satellites/{sid}/position")
async def get_position(sid: int, at: Optional[str] = None):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, "Satellite not found")

    # Если запрашивается текущий момент — отдаём кэш
    if not at:
        cached = db.cached_position(sid)
        if cached:
            return cached

    if at:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    else:
        t = ts.now()

    pos = _satellite_position(rec["satellite"], t)
    period = _period_min(rec["line2"])
    pos.update({
        "id": sid,
        "name": rec["name"],
        "operator": rec["operator"],
        "type": rec["type"],
        "period_min": round(period, 2),
    })
    db.set_position_cache(sid, pos)
    return pos


@app.get("/satellites/{sid}/orbit")
async def get_orbit(
    sid: int,
    minutes: int = Query(cfg.DEFAULT_TRACK_MINUTES, le=1440),
    steps:   int = Query(cfg.DEFAULT_TRACK_STEPS,   le=cfg.MAX_TRACK_STEPS),
):
    """Трек орбиты — список [lon, lat, alt_km]."""
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, "Satellite not found")

    t0 = ts.now()
    dt0 = t0.utc_datetime()
    step_sec = (minutes * 60) / steps

    # Векторизованный расчёт через skyfield
    times = ts.utc([dt0 + timedelta(seconds=step_sec * i) for i in range(steps + 1)])
    subs  = rec["satellite"].at(times).subpoint()

    track = [
        [round(subs.longitude.degrees[i], 5),
         round(subs.latitude.degrees[i], 5),
         round(subs.elevation.km[i], 2)]
        for i in range(len(times.tt))
    ]
    return {"id": sid, "minutes": minutes, "steps": steps, "track": track}


@app.get("/satellites/{sid}/passes")
async def get_passes(
    sid: int,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    days: int = Query(cfg.DEFAULT_PASS_DAYS, le=cfg.MAX_PASS_DAYS),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG, ge=0, le=90),
):
    """Прогноз пролётов с азимутом, элевацией, длительностью."""
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, "Satellite not found")

    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))

    try:
        t_events, events = rec["satellite"].find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception as e:
        raise HTTPException(500, f"Pass calculation error: {e}")

    passes: list[dict] = []
    current: dict = {}

    for ti, ev in zip(t_events, events):
        if ev == 0:  # AOS — спутник поднимается
            el, az = _elevation_azimuth(rec["satellite"], observer, ti)
            current = {
                "aos": ti.utc_iso(),
                "aos_az": round(az, 1),
                "max_el": 0.0,
                "max_el_time": None,
                "los": None,
                "los_az": None,
                "duration_s": None,
            }
        elif ev == 1:  # Максимальная элевация
            el, az = _elevation_azimuth(rec["satellite"], observer, ti)
            current["max_el"] = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
        elif ev == 2 and current:  # LOS — спутник уходит
            el, az = _elevation_azimuth(rec["satellite"], observer, ti)
            current["los"] = ti.utc_iso()
            current["los_az"] = round(az, 1)

            # Длительность прохода в секундах
            aos_dt = datetime.fromisoformat(current["aos"].replace("Z", "+00:00"))
            los_dt = datetime.fromisoformat(current["los"].replace("Z", "+00:00"))
            current["duration_s"] = round((los_dt - aos_dt).total_seconds())

            passes.append(current)
            current = {}

    return {"satellite": rec["name"], "observer": {"lat": lat, "lon": lon}, "passes": passes}


@app.get("/satellites/{sid}/coverage")
async def get_coverage(
    sid: int,
    min_el: float = Query(0.0, ge=0, le=45),
    at: Optional[str] = None,
):
    """Зона радиовидимости (полигон) для текущего или заданного момента."""
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, "Satellite not found")

    if at:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    else:
        t = ts.now()

    pos = _satellite_position(rec["satellite"], t)
    polygon = coverage_circle(pos["lat"], pos["lon"], pos["alt_km"], min_el)
    return {
        "id": sid,
        "center": [pos["lon"], pos["lat"]],
        "alt_km": pos["alt_km"],
        "min_elevation_deg": min_el,
        "polygon": polygon,
    }


# ── bulk positions ────────────────────────────────────────────────────────

@app.get("/positions")
async def get_all_positions(
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = None,
    limit: int = Query(100, le=cfg.BULK_POSITION_LIMIT),
    at: Optional[str] = None,
):
    """Позиции всех (или отфильтрованных) спутников за один запрос."""
    subset = db.list(operator=operator, sat_type=sat_type)[:limit]

    if at:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        t = ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    else:
        t = ts.now()

    result = []
    for info in subset:
        sid = info["id"]
        cached = db.cached_position(sid) if not at else None
        if cached:
            result.append(cached)
            continue
        rec = db.get(sid)
        if rec:
            pos = _satellite_position(rec["satellite"], t)
            pos.update({"id": sid, "name": rec["name"], "type": rec["type"]})
            db.set_position_cache(sid, pos)
            result.append(pos)

    return result


# ── passes for ground point ───────────────────────────────────────────────

@app.get("/passes")
async def passes_over_point(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    days: int = Query(1, le=3),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
    limit: int = Query(50, le=200),
):
    """Все спутники, которые пролетят над точкой в ближайшее время."""
    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))

    upcoming: list[dict] = []
    for sid, rec in list(db._data.items())[:cfg.BULK_POSITION_LIMIT]:
        try:
            t_ev, evs = rec["satellite"].find_events(
                observer, t0, t1, altitude_degrees=min_el
            )
        except Exception:
            continue
        for ti, ev in zip(t_ev, evs):
            if ev == 1:  # только момент максимума
                el, az = _elevation_azimuth(rec["satellite"], observer, ti)
                upcoming.append({
                    "id": sid,
                    "name": rec["name"],
                    "max_el": round(el, 1),
                    "time": ti.utc_iso(),
                })
                break  # первый пролёт

    upcoming.sort(key=lambda x: x["time"])
    return upcoming[:limit]


# ── TLE management ────────────────────────────────────────────────────────

@app.post("/tle/upload")
async def upload_tle(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    added = parse_tle_text(content)
    return {"added": added, "total": db.count()}


@app.post("/tle/fetch/{category}")
async def fetch_tle(category: str):
    if category not in cfg.TLE_SOURCES:
        raise HTTPException(400, f"Unknown category. Available: {list(cfg.TLE_SOURCES)}")
    added = await fetch_tle_from_celestrak(category)
    return {"category": category, "added": added, "total": db.count()}


@app.delete("/tle/clear")
async def clear_tle():
    db.clear()
    return {"message": "DB cleared"}


# ── WebSocket ─────────────────────────────────────────────────────────────

@app.websocket("/ws/positions")
async def ws_positions(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep-alive / клиент может слать ping
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── run ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=cfg.HOST, port=cfg.PORT,
                reload=cfg.DEBUG, workers=cfg.WORKERS)