"""
satellites.py — APIRouter для всех операций со спутниками.

Подключение в main.py:
    from satellites import router as satellites_router
    app.include_router(satellites_router, prefix="/satellites", tags=["satellites"])
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from skyfield.api import EarthSatellite, Topos, load

from config import get_settings

cfg = get_settings()
ts  = load.timescale()
router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteBrief(BaseModel):
    id:        int
    name:      str
    operator:  str
    type:      str                        # LEO / MEO / GEO / HEO / Unknown
    norad_id:  Optional[int]  = None
    inclination_deg: Optional[float] = None
    period_min: Optional[float] = None


class SatelliteDetail(SatelliteBrief):
    line1:     str
    line2:     str
    epoch:     Optional[str]  = None      # дата эпохи TLE
    apogee_km: Optional[float] = None
    perigee_km: Optional[float] = None
    eccentricity: Optional[float] = None
    mean_motion:  Optional[float] = None  # об/сутки


class PositionResponse(BaseModel):
    id:         int
    name:       str
    operator:   str
    type:       str
    lat:        float
    lon:        float
    alt_km:     float
    timestamp:  str
    period_min: float
    velocity_km_s: Optional[float] = None  # модуль вектора скорости


class TrackPoint(BaseModel):
    lon:    float
    lat:    float
    alt_km: float


class OrbitTrackResponse(BaseModel):
    id:      int
    name:    str
    minutes: int
    steps:   int
    track:   list[TrackPoint]


class PassEvent(BaseModel):
    aos:           str            # время появления (UTC ISO)
    aos_az:        float          # азимут при появлении
    max_el:        float          # максимальная элевация (градусы)
    max_el_time:   str
    max_el_az:     float
    los:           str            # время исчезновения
    los_az:        float
    duration_s:    int


class PassesResponse(BaseModel):
    id:       int
    name:     str
    observer: dict
    passes:   list[PassEvent]


class CoverageResponse(BaseModel):
    id:          int
    name:        str
    center:      list[float]   # [lon, lat]
    alt_km:      float
    radius_km:   float
    min_elevation_deg: float
    polygon:     list[list[float]]   # [[lon, lat], ...]


class GroupCompareItem(BaseModel):
    type:        str
    count:       int
    avg_alt_km:  Optional[float]
    min_period_min: Optional[float]
    max_period_min: Optional[float]
    operators:   list[str]


# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY DB  (импортируется из main или передаётся зависимостью)
#  Для изоляции модуля — определяем здесь же, main.py делает `from satellites import db`
# ══════════════════════════════════════════════════════════════════════════════

class _PositionCache:
    """TTL-кэш позиций. Ключ — sat_id, значение — (monotonic_ts, payload)."""

    def __init__(self, ttl_s: float = 5.0) -> None:
        self._store: dict[int, tuple[float, dict]] = {}
        self._ttl = ttl_s

    def get(self, sid: int) -> Optional[dict]:
        entry = self._store.get(sid)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, sid: int, val: dict) -> None:
        self._store[sid] = (time.monotonic(), val)

    def invalidate(self, sid: int) -> None:
        self._store.pop(sid, None)

    def clear(self) -> None:
        self._store.clear()


class SatelliteDB:
    """
    In-memory хранилище спутников.
    Индексы по типу орбиты и оператору обеспечивают O(1) фильтрацию.
    """

    def __init__(self) -> None:
        self._data:     dict[int, dict]         = {}   # id → record
        self._by_type:  dict[str, set[int]]     = {}
        self._by_op:    dict[str, set[int]]     = {}
        self._norad:    dict[int, int]          = {}   # norad_id → sat_id
        self._next_id   = 1
        self._pos_cache = _PositionCache(cfg.POSITION_CACHE_TTL_S)
        self._lock      = asyncio.Lock()                # для async-safe записи

    # ── write ─────────────────────────────────────────────────────────────

    async def add_async(self, name: str, l1: str, l2: str,
                        operator: str = "Unknown",
                        sat_type: str = "Unknown") -> Optional[int]:
        async with self._lock:
            return self._add(name, l1, l2, operator, sat_type)

    def add(self, name: str, l1: str, l2: str,
            operator: str = "Unknown",
            sat_type: str = "Unknown") -> Optional[int]:
        return self._add(name, l1, l2, operator, sat_type)

    def _add(self, name: str, l1: str, l2: str,
             operator: str, sat_type: str) -> Optional[int]:
        try:
            sat = EarthSatellite(l1, l2, name, ts)
        except Exception as e:
            print(f"[DB] skip '{name}': {e}")
            return None

        meta = _parse_tle_meta(l1, l2)
        sid  = self._next_id
        self._next_id += 1

        rec = {
            "id":       sid,
            "name":     name,
            "satellite": sat,
            "line1":    l1,
            "line2":    l2,
            "operator": operator,
            "type":     sat_type,
            **meta,
        }
        self._data[sid] = rec

        self._by_type.setdefault(sat_type, set()).add(sid)
        self._by_op.setdefault(operator, set()).add(sid)
        if meta.get("norad_id"):
            self._norad[meta["norad_id"]] = sid

        return sid

    async def remove_async(self, sid: int) -> bool:
        async with self._lock:
            rec = self._data.pop(sid, None)
            if not rec:
                return False
            self._by_type.get(rec["type"], set()).discard(sid)
            self._by_op.get(rec["operator"], set()).discard(sid)
            if rec.get("norad_id"):
                self._norad.pop(rec["norad_id"], None)
            self._pos_cache.invalidate(sid)
            return True

    def clear(self) -> None:
        self._data.clear()
        self._by_type.clear()
        self._by_op.clear()
        self._norad.clear()
        self._pos_cache.clear()
        self._next_id = 1

    # ── read ──────────────────────────────────────────────────────────────

    def get(self, sid: int) -> Optional[dict]:
        return self._data.get(sid)

    def get_by_norad(self, norad_id: int) -> Optional[dict]:
        sid = self._norad.get(norad_id)
        return self._data.get(sid) if sid else None

    def list(self,
             operator:  Optional[str] = None,
             sat_type:  Optional[str] = None,
             name_like: Optional[str] = None,
             ) -> list[dict]:
        ids: Optional[set[int]] = None

        if operator:
            ids = self._by_op.get(operator, set()).copy()
        if sat_type:
            t = self._by_type.get(sat_type, set())
            ids = t.copy() if ids is None else ids & t

        pool = (self._data[i] for i in ids) if ids is not None else self._data.values()

        if name_like:
            q = name_like.lower()
            pool = (r for r in pool if q in r["name"].lower())

        return list(pool)

    def count(self) -> int:
        return len(self._data)

    def types(self) -> list[str]:
        return sorted(self._by_type.keys())

    def operators(self) -> list[str]:
        return sorted(self._by_op.keys())

    def all_records(self) -> list[dict]:
        return list(self._data.values())

    # ── position cache ────────────────────────────────────────────────────

    def cached_pos(self, sid: int) -> Optional[dict]:
        return self._pos_cache.get(sid)

    def store_pos(self, sid: int, pos: dict) -> None:
        self._pos_cache.set(sid, pos)


db = SatelliteDB()

# ══════════════════════════════════════════════════════════════════════════════
#  ORBITAL MATH
# ══════════════════════════════════════════════════════════════════════════════

_RE_KM  = 6371.0          # средний радиус Земли
_MU_KM3 = 398_600.4418    # гравитационный параметр Земли (км³/с²)


def _parse_tle_meta(l1: str, l2: str) -> dict:
    """Извлекает все числовые параметры из TLE-строк."""
    try:
        norad_id      = int(l1[2:7])
        epoch_year_2  = int(l1[18:20])
        epoch_day     = float(l1[20:32])
        year          = (2000 + epoch_year_2) if epoch_year_2 < 57 else (1900 + epoch_year_2)
        epoch_dt      = datetime(year, 1, 1) + timedelta(days=epoch_day - 1)

        inclination   = float(l2[8:16])
        eccentricity  = float("0." + l2[26:33])
        mean_motion   = float(l2[52:63])          # об/сутки
        period_min    = 1440.0 / mean_motion

        # Большая полуось по третьему закону Кеплера
        a_km = (_MU_KM3 * (period_min * 60 / (2 * math.pi)) ** 2) ** (1 / 3)
        apogee_km  = round(a_km * (1 + eccentricity) - _RE_KM, 1)
        perigee_km = round(a_km * (1 - eccentricity) - _RE_KM, 1)

        return {
            "norad_id":       norad_id,
            "epoch":          epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inclination_deg": round(inclination, 4),
            "eccentricity":   round(eccentricity, 7),
            "mean_motion":    round(mean_motion, 8),
            "period_min":     round(period_min, 4),
            "apogee_km":      apogee_km,
            "perigee_km":     perigee_km,
        }
    except Exception:
        return {}


def _orbit_type_from_meta(meta: dict) -> str:
    alt = meta.get("apogee_km")
    if alt is None:
        return "Unknown"
    if alt < 2_000:
        return "LEO"
    if alt < 35_786:
        return "MEO"
    if alt < 42_164 * 1.05:
        return "GEO"
    return "HEO"


def _sat_position(sat: EarthSatellite, t) -> dict:
    """Геодезические координаты + скорость (км/с)."""
    geo  = sat.at(t)
    sub  = geo.subpoint()
    vel  = round(float(np.linalg.norm(geo.velocity.km_per_s)), 3)
    return {
        "lat":           round(sub.latitude.degrees, 5),
        "lon":           round(sub.longitude.degrees, 5),
        "alt_km":        round(sub.elevation.km, 2),
        "velocity_km_s": vel,
        "timestamp":     t.utc_iso(),
    }


def _elev_az(sat: EarthSatellite, observer: Topos, t) -> tuple[float, float]:
    alt, az, _ = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees


def _coverage_polygon(lat: float, lon: float,
                      alt_km: float, min_el_deg: float = 0.0,
                      step_deg: float = 1.0) -> tuple[list[list[float]], float]:
    """
    Строит полигон зоны радиовидимости методом сферической тригонометрии.
    Возвращает (polygon [[lon,lat],...], radius_km).
    """
    # Центральный угол от надира (Earth central angle)
    rho = _RE_KM / (_RE_KM + alt_km)
    eta = math.acos(rho * math.cos(math.radians(min_el_deg))) - math.radians(min_el_deg)
    radius_km = round(_RE_KM * eta, 1)

    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    points: list[list[float]] = []
    for b_deg in np.arange(0, 360, step_deg):
        b = math.radians(b_deg)
        p_lat = math.asin(
            math.sin(lat_r) * math.cos(eta)
            + math.cos(lat_r) * math.sin(eta) * math.cos(b)
        )
        p_lon = lon_r + math.atan2(
            math.sin(b) * math.sin(eta) * math.cos(lat_r),
            math.cos(eta) - math.sin(lat_r) * math.sin(p_lat),
        )
        points.append([round(math.degrees(p_lon), 4), round(math.degrees(p_lat), 4)])

    points.append(points[0])
    return points, radius_km


def _parse_time(at: Optional[str]):
    """Парсит ISO-строку в skyfield Time, либо возвращает ts.now()."""
    if not at:
        return ts.now()
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def classify_tle(l1: str, l2: str) -> str:
    """Определяет тип орбиты по TLE."""
    meta = _parse_tle_meta(l1, l2)
    return _orbit_type_from_meta(meta)

# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── list ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SatelliteBrief], summary="Список спутников")
async def list_satellites(
    operator:  Optional[str] = Query(None, description="Фильтр по оператору"),
    type:      Optional[str] = Query(None, description="LEO | MEO | GEO | HEO"),
    name:      Optional[str] = Query(None, description="Поиск по имени (подстрока)"),
    limit:     int           = Query(200, ge=1, le=500),
    offset:    int           = Query(0,   ge=0),
    sort_by:   str           = Query("id", regex="^(id|name|period_min|apogee_km)$"),
    desc:      bool          = Query(False),
):
    records = db.list(operator=operator, sat_type=type, name_like=name)

    # Сортировка
    records.sort(key=lambda r: r.get(sort_by, 0) or 0, reverse=desc)

    page = records[offset: offset + limit]
    return [
        SatelliteBrief(
            id=r["id"], name=r["name"],
            operator=r["operator"], type=r["type"],
            norad_id=r.get("norad_id"),
            inclination_deg=r.get("inclination_deg"),
            period_min=r.get("period_min"),
        )
        for r in page
    ]


@router.get("/count", summary="Количество спутников в БД")
async def count_satellites(
    type:     Optional[str] = None,
    operator: Optional[str] = None,
):
    return {"count": len(db.list(operator=operator, sat_type=type))}


@router.get("/types", summary="Доступные типы орбит")
async def orbit_types():
    return {"types": db.types()}


@router.get("/operators", summary="Список операторов")
async def operators():
    return {"operators": db.operators()}


# ── single satellite info ──────────────────────────────────────────────────────

@router.get("/{sid}", response_model=SatelliteDetail, summary="Подробная карточка спутника")
async def get_satellite(sid: int):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")
    return SatelliteDetail(
        id=rec["id"], name=rec["name"],
        operator=rec["operator"], type=rec["type"],
        line1=rec["line1"], line2=rec["line2"],
        norad_id=rec.get("norad_id"),
        epoch=rec.get("epoch"),
        inclination_deg=rec.get("inclination_deg"),
        eccentricity=rec.get("eccentricity"),
        mean_motion=rec.get("mean_motion"),
        period_min=rec.get("period_min"),
        apogee_km=rec.get("apogee_km"),
        perigee_km=rec.get("perigee_km"),
    )


@router.delete("/{sid}", summary="Удалить спутник из БД")
async def delete_satellite(sid: int):
    removed = await db.remove_async(sid)
    if not removed:
        raise HTTPException(404, f"Satellite {sid} not found")
    return {"deleted": sid}


# ── position ──────────────────────────────────────────────────────────────────

@router.get("/{sid}/position", response_model=PositionResponse,
            summary="Текущая или заданная позиция спутника")
async def get_position(
    sid: int,
    at:  Optional[str] = Query(None, description="UTC ISO-8601, напр. 2025-04-01T12:00:00Z"),
):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    # Кэш только для «сейчас»
    if not at:
        cached = db.cached_pos(sid)
        if cached:
            return cached

    t   = _parse_time(at)
    pos = _sat_position(rec["satellite"], t)
    pos.update({
        "id":         rec["id"],
        "name":       rec["name"],
        "operator":   rec["operator"],
        "type":       rec["type"],
        "period_min": round(rec.get("period_min") or 0, 4),
    })
    db.store_pos(sid, pos)
    return pos


# ── orbit track ───────────────────────────────────────────────────────────────

@router.get("/{sid}/orbit", response_model=OrbitTrackResponse,
            summary="Трек орбиты — список точек [lon, lat, alt_km]")
async def get_orbit(
    sid:     int,
    minutes: int = Query(cfg.DEFAULT_TRACK_MINUTES, ge=1,  le=1440,
                         description="Длительность трека в минутах"),
    steps:   int = Query(cfg.DEFAULT_TRACK_STEPS,   ge=10, le=cfg.MAX_TRACK_STEPS,
                         description="Количество точек"),
    start:   Optional[str] = Query(None, description="Начало трека (UTC ISO)"),
):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    t0_dt    = _parse_time(start).utc_datetime()
    step_sec = (minutes * 60) / steps

    # Векторизованный расчёт — один вызов skyfield
    times = ts.utc([t0_dt + timedelta(seconds=step_sec * i) for i in range(steps + 1)])
    subs  = rec["satellite"].at(times).subpoint()

    track = [
        TrackPoint(
            lon=round(float(subs.longitude.degrees[i]), 5),
            lat=round(float(subs.latitude.degrees[i]), 5),
            alt_km=round(float(subs.elevation.km[i]), 2),
        )
        for i in range(len(times.tt))
    ]
    return OrbitTrackResponse(id=sid, name=rec["name"],
                              minutes=minutes, steps=steps, track=track)


# ── passes ────────────────────────────────────────────────────────────────────

@router.get("/{sid}/passes", response_model=PassesResponse,
            summary="Прогноз пролётов над заданной точкой")
async def get_passes(
    sid:    int,
    lat:    float = Query(..., ge=-90,   le=90,   description="Широта наблюдателя"),
    lon:    float = Query(..., ge=-180,  le=180,  description="Долгота наблюдателя"),
    alt_m:  float = Query(0.0, ge=0,    le=8848, description="Высота наблюдателя (м)"),
    days:   int   = Query(cfg.DEFAULT_PASS_DAYS, ge=1, le=cfg.MAX_PASS_DAYS),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG, ge=0, le=90,
                          description="Мин. элевация (градусы)"),
):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    observer = Topos(latitude_degrees=lat, longitude_degrees=lon,
                     elevation_m=alt_m)
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))

    try:
        t_ev, evs = rec["satellite"].find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception as e:
        raise HTTPException(500, f"Pass calculation failed: {e}")

    passes:  list[PassEvent] = []
    current: dict            = {}

    for ti, ev in zip(t_ev, evs):
        el, az = _elev_az(rec["satellite"], observer, ti)

        if ev == 0:                       # AOS
            current = {
                "aos":         ti.utc_iso(),
                "aos_az":      round(az, 1),
                "max_el":      0.0,
                "max_el_time": "",
                "max_el_az":   0.0,
                "los":         "",
                "los_az":      0.0,
            }
        elif ev == 1 and current:         # Transit (max elevation)
            current["max_el"]      = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
            current["max_el_az"]   = round(az, 1)
        elif ev == 2 and current:         # LOS
            current["los"]     = ti.utc_iso()
            current["los_az"]  = round(az, 1)

            aos_dt = datetime.fromisoformat(current["aos"].replace("Z", "+00:00"))
            los_dt = datetime.fromisoformat(current["los"].replace("Z", "+00:00"))
            duration = int((los_dt - aos_dt).total_seconds())

            passes.append(PassEvent(**current, duration_s=duration))
            current = {}

    return PassesResponse(
        id=sid, name=rec["name"],
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        passes=passes,
    )


# ── coverage ──────────────────────────────────────────────────────────────────

@router.get("/{sid}/coverage", response_model=CoverageResponse,
            summary="Зона радиовидимости (полигон GeoJSON)")
async def get_coverage(
    sid:    int,
    min_el: float        = Query(0.0, ge=0, le=45),
    at:     Optional[str]= Query(None),
    step:   float        = Query(1.0, ge=0.5, le=5.0,
                                 description="Шаг построения полигона (градусы)"),
):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    t   = _parse_time(at)
    pos = _sat_position(rec["satellite"], t)

    polygon, radius_km = _coverage_polygon(
        pos["lat"], pos["lon"], pos["alt_km"], min_el, step
    )
    return CoverageResponse(
        id=sid, name=rec["name"],
        center=[pos["lon"], pos["lat"]],
        alt_km=pos["alt_km"],
        radius_km=radius_km,
        min_elevation_deg=min_el,
        polygon=polygon,
    )


# ── multi-coverage ────────────────────────────────────────────────────────────

@router.get("/coverage/multi", summary="Зоны видимости нескольких спутников за один запрос")
async def get_multi_coverage(
    ids:    str   = Query(..., description="Через запятую: 1,2,5"),
    min_el: float = Query(0.0, ge=0, le=45),
    at:     Optional[str] = Query(None),
):
    try:
        sids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if len(sids) > 50:
        raise HTTPException(400, "Max 50 satellites per request")

    t = _parse_time(at)
    result = []
    for sid in sids:
        rec = db.get(sid)
        if not rec:
            continue
        pos = _sat_position(rec["satellite"], t)
        polygon, radius_km = _coverage_polygon(
            pos["lat"], pos["lon"], pos["alt_km"], min_el
        )
        result.append({
            "id": sid, "name": rec["name"],
            "center": [pos["lon"], pos["lat"]],
            "radius_km": radius_km,
            "polygon": polygon,
        })
    return result


# ── bulk positions ────────────────────────────────────────────────────────────

@router.get("/positions/bulk", summary="Позиции нескольких спутников (batch)")
async def bulk_positions(
    ids:      Optional[str] = Query(None, description="ID через запятую; если не задан — все"),
    type:     Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    at:       Optional[str] = Query(None),
    limit:    int            = Query(100, ge=1, le=cfg.BULK_POSITION_LIMIT),
):
    t = _parse_time(at)
    use_cache = (at is None)

    if ids:
        try:
            sids = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids must be integers")
        records = [r for sid in sids if (r := db.get(sid))]
    else:
        records = db.list(operator=operator, sat_type=type)[:limit]

    result = []
    for rec in records[:limit]:
        sid = rec["id"]
        if use_cache:
            cached = db.cached_pos(sid)
            if cached:
                result.append(cached)
                continue
        pos = _sat_position(rec["satellite"], t)
        pos.update({
            "id": sid, "name": rec["name"],
            "operator": rec["operator"], "type": rec["type"],
        })
        db.store_pos(sid, pos)
        result.append(pos)

    return result


# ── group comparison ──────────────────────────────────────────────────────────

@router.get("/groups/compare", response_model=list[GroupCompareItem],
            summary="Сравнение орбитальных группировок по типу")
async def compare_groups():
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in db.all_records():
        groups[rec["type"]].append(rec)

    result = []
    for orbit_type, recs in sorted(groups.items()):
        alts      = [r["apogee_km"] for r in recs if r.get("apogee_km") is not None]
        periods   = [r["period_min"] for r in recs if r.get("period_min") is not None]
        operators = sorted(set(r["operator"] for r in recs))
        result.append(GroupCompareItem(
            type=orbit_type,
            count=len(recs),
            avg_alt_km=round(sum(alts) / len(alts), 1) if alts else None,
            min_period_min=round(min(periods), 2) if periods else None,
            max_period_min=round(max(periods), 2) if periods else None,
            operators=operators,
        ))
    return result


# ── next pass for selected point ──────────────────────────────────────────────

@router.get("/{sid}/next-pass", summary="Ближайший пролёт над точкой")
async def next_pass(
    sid:    int,
    lat:    float = Query(..., ge=-90,  le=90),
    lon:    float = Query(..., ge=-180, le=180),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
):
    """Возвращает только первый пролёт — быстрее чем /passes с days=1."""
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=3))

    try:
        t_ev, evs = rec["satellite"].find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    current: dict = {}
    for ti, ev in zip(t_ev, evs):
        el, az = _elev_az(rec["satellite"], observer, ti)
        if ev == 0:
            current = {"aos": ti.utc_iso(), "aos_az": round(az, 1)}
        elif ev == 1 and current:
            current["max_el"] = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
        elif ev == 2 and current:
            current["los"] = ti.utc_iso()
            current["los_az"] = round(az, 1)
            aos_dt  = datetime.fromisoformat(current["aos"].replace("Z", "+00:00"))
            los_dt  = datetime.fromisoformat(current["los"].replace("Z", "+00:00"))
            current["duration_s"] = int((los_dt - aos_dt).total_seconds())
            return {"satellite": rec["name"], "pass": current}

    return {"satellite": rec["name"], "pass": None,
            "message": "No passes found in next 3 days"}