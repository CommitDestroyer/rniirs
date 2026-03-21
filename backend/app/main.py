"""
backend/app/main.py — единственная точка входа FastAPI-приложения.

Запуск:
    cd backend
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Документация:  http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from skyfield.api import EarthSatellite, Topos, load

# ── sys.path: config.py (app/) и роутеры (api/) ──────────────────────────────
_APP = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.normpath(os.path.join(_APP, "..", "api"))
_BCK = os.path.normpath(os.path.join(_APP, ".."))
for _p in (_APP, _API, _BCK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import get_settings  # noqa: E402

cfg = get_settings()
ts  = load.timescale()

# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY DB
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteDB:
    """In-memory хранилище с индексами O(1) по типу орбиты и оператору."""

    def __init__(self) -> None:
        self._data:    dict[int, dict]       = {}
        self._by_type: dict[str, set[int]]   = {}
        self._by_op:   dict[str, set[int]]   = {}
        self._norad:   dict[int, int]        = {}
        self._next_id  = 1
        self._cache:   dict[int, tuple[float, dict]] = {}
        self._lock     = asyncio.Lock()

    def add(self, name: str, l1: str, l2: str,
            operator: str = "Unknown", sat_type: str = "Unknown") -> Optional[int]:
        try:
            sat = EarthSatellite(l1, l2, name, ts)
        except Exception as e:
            print(f"[DB] skip '{name}': {e}")
            return None
        meta = _parse_tle_meta(l1, l2)
        sid  = self._next_id
        self._next_id += 1
        self._data[sid] = {"id": sid, "name": name, "satellite": sat,
                        "line1": l1, "line2": l2,
                           "operator": operator, "type": sat_type, **meta}
        self._by_type.setdefault(sat_type, set()).add(sid)
        self._by_op.setdefault(operator, set()).add(sid)
        if meta.get("norad_id"):
            self._norad[meta["norad_id"]] = sid
        return sid

    async def remove_async(self, sid: int) -> bool:
        async with self._lock:
            rec = self._data.pop(sid, None)
            if not rec: return False
            self._by_type.get(rec["type"], set()).discard(sid)
            self._by_op.get(rec["operator"], set()).discard(sid)
            if rec.get("norad_id"): self._norad.pop(rec["norad_id"], None)
            self._cache.pop(sid, None)
            return True

    def clear(self) -> None:
        self._data.clear(); self._by_type.clear()
        self._by_op.clear(); self._norad.clear()
        self._cache.clear(); self._next_id = 1

    def get(self, sid: int) -> Optional[dict]:
        return self._data.get(sid)

    def get_by_norad(self, norad_id: int) -> Optional[dict]:
        sid = self._norad.get(norad_id)
        return self._data.get(sid) if sid else None

    def list(self, operator: Optional[str] = None, sat_type: Optional[str] = None,
            name_like: Optional[str] = None) -> list[dict]:
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

    def all_records(self) -> list[dict]:
        return list(self._data.values())

    def count(self) -> int:           return len(self._data)
    def types(self) -> list[str]:     return sorted(self._by_type)
    def operators(self) -> list[str]: return sorted(self._by_op)

    def cached_pos(self, sid: int) -> Optional[dict]:
        e = self._cache.get(sid)
        return e[1] if e and time.monotonic() - e[0] < cfg.POSITION_CACHE_TTL_S else None

    def store_pos(self, sid: int, pos: dict) -> None:
        self._cache[sid] = (time.monotonic(), pos)

    # Обратная совместимость
    def cached_position(self, sid: int) -> Optional[dict]: return self.cached_pos(sid)
    def set_position_cache(self, sid: int, pos: dict) -> None: self.store_pos(sid, pos)


db = SatelliteDB()

# ══════════════════════════════════════════════════════════════════════════════
#  ОРБИТАЛЬНАЯ МАТЕМАТИКА
# ══════════════════════════════════════════════════════════════════════════════

_RE_KM  = 6371.0
_MU_KM3 = 398_600.4418


def _parse_tle_meta(l1: str, l2: str) -> dict:
    try:
        ey2 = int(l1[18:20])
        year = (2000 + ey2) if ey2 < 57 else (1900 + ey2)
        epoch_dt = datetime(year, 1, 1) + timedelta(days=float(l1[20:32]) - 1)
        ecc = float("0." + l2[26:33])
        mm  = float(l2[52:63])
        per = 1440.0 / mm
        a   = (_MU_KM3 * (per * 60 / (2 * math.pi)) ** 2) ** (1 / 3)
        return {
            "norad_id":           int(l1[2:7]),
            "epoch":              epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inclination_deg":    round(float(l2[8:16]), 4),
            "eccentricity":       round(ecc, 7),
            "mean_motion":        round(mm, 8),
            "period_min":         round(per, 4),
            "semi_major_axis_km": round(a, 2),
            "apogee_km":          round(a * (1 + ecc) - _RE_KM, 1),
            "perigee_km":         round(a * (1 - ecc) - _RE_KM, 1),
        }
    except Exception:
        return {}


def _orbit_type(meta: dict) -> str:
    alt = meta.get("apogee_km")
    if alt is None:       return "Unknown"
    if alt < 2_000:       return "LEO"
    if alt < 35_786:      return "MEO"
    if alt < 42_164*1.05: return "GEO"
    return "HEO"


def _sat_position(sat: EarthSatellite, t) -> dict:
    geo = sat.at(t); sub = geo.subpoint()
    return {
        "lat":           round(sub.latitude.degrees, 5),
        "lon":           round(sub.longitude.degrees, 5),
        "alt_km":        round(sub.elevation.km, 2),
        "velocity_km_s": round(float(np.linalg.norm(geo.velocity.km_per_s)), 3),
        "timestamp":     t.utc_iso(),
    }


def _elev_az(sat: EarthSatellite, observer: Topos, t) -> tuple[float, float]:
    alt, az, _ = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees


def _coverage_polygon(lat: float, lon: float, alt_km: float,
                    min_el: float = 0.0, step: float = 1.0
                    ) -> tuple[list[list[float]], float]:
    rho = _RE_KM / (_RE_KM + alt_km)
    eta = math.acos(rho * math.cos(math.radians(min_el))) - math.radians(min_el)
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    pts = []
    for b in np.arange(0, 360, step):
        b = math.radians(b)
        p_lat = math.asin(math.sin(lat_r)*math.cos(eta)
                        + math.cos(lat_r)*math.sin(eta)*math.cos(b))
        p_lon = lon_r + math.atan2(math.sin(b)*math.sin(eta)*math.cos(lat_r),
                                    math.cos(eta) - math.sin(lat_r)*math.sin(p_lat))
        pts.append([round(math.degrees(p_lon), 4), round(math.degrees(p_lat), 4)])
    pts.append(pts[0])
    return pts, round(_RE_KM * eta, 1)


def _parse_time(at: Optional[str]):
    if not at: return ts.now()
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return ts.utc(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)

# ══════════════════════════════════════════════════════════════════════════════
#  TLE ЗАГРУЗКА
# ══════════════════════════════════════════════════════════════════════════════

def _add_tle(name: str, l1: str, l2: str, operator: str = "Various") -> None:
    meta = _parse_tle_meta(l1, l2)
    db.add(name, l1, l2, operator, _orbit_type(meta) if meta else "Unknown")


def parse_tle_text(content: str, operator: str = "Various") -> int:
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    added = 0; i = 0
    while i < len(lines):
        if (i + 2 < len(lines) and not lines[i].startswith("1 ")
                and lines[i+1].startswith("1 ") and lines[i+2].startswith("2 ")):
            _add_tle(lines[i], lines[i+1], lines[i+2], operator); added += 1; i += 3
        elif (i + 1 < len(lines) and lines[i].startswith("1 ")
            and lines[i+1].startswith("2 ")):
            _add_tle(f"NORAD-{lines[i][2:7].strip()}", lines[i], lines[i+1], operator)
            added += 1; i += 2
        else:
            i += 1
    return added


async def fetch_tle_from_celestrak(category: str) -> int:
    url = cfg.TLE_SOURCES.get(category)
    if not url: return 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url); r.raise_for_status()
        return parse_tle_text(r.text, operator=category.upper())
    except Exception as e:
        print(f"[Celestrak] {category}: {e}"); return 0


def _load_tle_on_startup() -> None:
    for path in [cfg.TLE_LOCAL_FILE, os.path.join(_BCK, cfg.TLE_LOCAL_FILE)]:
        if os.path.isfile(path):
            try:
                count = parse_tle_text(open(path, encoding="utf-8").read())
                print(f"[TLE] loaded {count} satellites from {path}"); return
            except Exception as e:
                print(f"[TLE] error: {e}")
    print("[TLE] using ISS fallback")
    db.add("ISS (ZARYA)",
        "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
        "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
        "NASA/Roscosmos", "LEO")

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET — полный протокол
# ══════════════════════════════════════════════════════════════════════════════

class _SimClock:
    def __init__(self) -> None:
        self._r = time.monotonic()
        self._s = datetime.now(timezone.utc)
        self._m = 1.0

    def now(self):
        sim = self._s + timedelta(seconds=(time.monotonic()-self._r)*self._m)
        return ts.utc(sim.year, sim.month, sim.day,
                    sim.hour, sim.minute, sim.second + sim.microsecond/1e6)

    def seek(self, iso: str) -> None:
        self._r = time.monotonic()
        self._s = datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def set_speed(self, m: float) -> None:
        cur = self.now().utc_datetime().replace(tzinfo=timezone.utc)
        self._r = time.monotonic(); self._s = cur; self._m = m

    @property
    def mult(self) -> float: return self._m

    @property
    def sim_iso(self) -> str:
        return self.now().utc_datetime().replace(
            tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Session:
    def __init__(self, ws: WebSocket, cid: str) -> None:
        self.ws = ws; self.cid = cid
        self.channels: set[str] = set()
        self.clock = _SimClock()
        self.ftype: Optional[str]      = None
        self.fop:   Optional[str]      = None
        self.fids:  Optional[set[int]] = None
        self.flim   = 100
        self.obs:   Optional[dict]     = None
        self._lk    = asyncio.Lock()

    def recs(self) -> list[dict]:
        if self.fids:
            return [r for s in self.fids if (r := db.get(s))]
        return [r for i in db.list(operator=self.fop, sat_type=self.ftype)[:self.flim]
                if (r := db.get(i["id"]))]

    async def send(self, p: dict) -> bool:
        try:
            async with self._lk: await self.ws.send_json(p)
            return True
        except Exception:
            return False


class _WS:
    def __init__(self) -> None:
        self._s: dict[str, _Session] = {}; self._n = 0; self._lk = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> _Session:
        await ws.accept()
        async with self._lk:
            self._n += 1; cid = f"c{self._n}"
            s = _Session(ws, cid); self._s[cid] = s
        return s

    async def disconnect(self, s: _Session) -> None:
        async with self._lk: self._s.pop(s.cid, None)

    def ch(self, ch: str) -> list[_Session]:
        return [s for s in self._s.values() if ch in s.channels]

    @property
    def count(self) -> int: return len(self._s)


_ws_mgr = _WS()
_ws_tasks: list[asyncio.Task] = []
_NOW = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _pos_loop() -> None:
    while True:
        dead = []
        for s in _ws_mgr.ch("positions"):
            t = s.clock.now()
            data = []
            for rec in s.recs():
                p = _sat_position(rec["satellite"], t)
                p.update({"id": rec["id"], "name": rec["name"],
                        "type": rec.get("type"), "operator": rec.get("operator")})
                data.append(p)
            if not await s.send({"type": "positions", "sim_time": s.clock.sim_iso,
                                "speed": s.clock.mult, "ts": _NOW(),
                                "count": len(data), "data": data}):
                dead.append(s)
        for s in dead: await _ws_mgr.disconnect(s)
        await asyncio.sleep(cfg.WS_BROADCAST_INTERVAL_S)


async def _cov_loop() -> None:
    while True:
        dead = []
        for s in _ws_mgr.ch("coverage"):
            t = s.clock.now(); data = []
            for rec in s.recs()[:50]:
                pos = _sat_position(rec["satellite"], t)
                poly, r = _coverage_polygon(pos["lat"], pos["lon"], pos["alt_km"])
                data.append({"id": rec["id"], "name": rec["name"],
                            "center": [pos["lon"], pos["lat"]],
                            "radius_km": r, "polygon": poly})
            if not await s.send({"type": "coverage", "ts": _NOW(), "data": data}):
                dead.append(s)
        for s in dead: await _ws_mgr.disconnect(s)
        await asyncio.sleep(cfg.WS_BROADCAST_INTERVAL_S * 2)


async def _alert_loop() -> None:
    notified: set[str] = set()
    while True:
        t0 = ts.now(); t1 = ts.utc(t0.utc_datetime() + timedelta(minutes=6))
        dead = []
        for s in _ws_mgr.ch("pass_alerts"):
            if not s.obs: continue
            obs = Topos(latitude_degrees=s.obs["lat"], longitude_degrees=s.obs["lon"],
                        elevation_m=s.obs.get("alt_m", 0.0))
            for rec in s.recs():
                try: t_ev, evs = rec["satellite"].find_events(
                        obs, t0, t1, altitude_degrees=cfg.MIN_ELEVATION_DEG)
                except Exception: continue
                cur: dict = {}
                for ti, ev in zip(t_ev, evs):
                    el, az = _elev_az(rec["satellite"], obs, ti)
                    if ev == 0: cur = {"aos": ti.utc_iso(), "max_el": 0.0}
                    elif ev == 1 and cur: cur["max_el"] = round(el, 1)
                    elif ev == 2 and cur:
                        key = f"{rec['id']}:{cur['aos']}"
                        if key not in notified:
                            try:
                                d = (datetime.fromisoformat(cur["aos"].replace("Z","+00:00"))
                                    - datetime.now(timezone.utc)).total_seconds()
                                if 0 <= d <= 300:
                                    ok = await s.send({"type": "pass_alert", "ts": _NOW(),
                                        "data": {"sat_id": rec["id"], "sat_name": rec["name"],
                                                "aos": cur["aos"], "max_el": cur["max_el"],
                                                "duration_s": int((
                                                    datetime.fromisoformat(
                                                        ti.utc_iso().replace("Z","+00:00"))
                                                    - datetime.fromisoformat(
                                                        cur["aos"].replace("Z","+00:00"))
                                                ).total_seconds()),
                                                "in_seconds": int(d)}})
                                    if not ok: dead.append(s)
                                    notified.add(key)
                            except Exception: pass
                        cur = {}
        for s in dead: await _ws_mgr.disconnect(s)
        now_dt = datetime.now(timezone.utc)
        notified = {k for k in notified if (
            now_dt - datetime.fromisoformat(k.split(":",1)[1].replace("Z","+00:00"))
        ).total_seconds() < 600}
        await asyncio.sleep(30)


def _ensure_ws() -> None:
    if not _ws_tasks:
        _ws_tasks.extend([
            asyncio.create_task(_pos_loop()),
            asyncio.create_task(_cov_loop()),
            asyncio.create_task(_alert_loop()),
        ])


async def _handle(s: _Session, raw: str) -> None:
    import json
    try: msg = json.loads(raw)
    except Exception:
        await s.send({"type": "error", "message": "Invalid JSON"}); return

    action = msg.get("action", ""); params = msg.get("params", {})
    ch     = msg.get("channel", "")

    if action == "ping":
        await s.send({"type": "pong", "ts": _NOW()})
    elif action == "subscribe":
        if ch not in {"positions", "coverage", "pass_alerts"}:
            await s.send({"type": "error", "message": f"Unknown channel: {ch}"}); return
        if ch == "pass_alerts":
            if "lat" not in params or "lon" not in params:
                await s.send({"type": "error", "message": "pass_alerts needs lat/lon"}); return
            s.obs = {"lat": float(params["lat"]), "lon": float(params["lon"]),
                    "alt_m": float(params.get("alt_m", 0.0))}
        s.channels.add(ch)
        await s.send({"type": "subscribed", "channel": ch, "ts": _NOW()})
    elif action == "unsubscribe":
        s.channels.discard(ch)
        await s.send({"type": "unsubscribed", "channel": ch, "ts": _NOW()})
    elif action == "set_filter":
        s.ftype = params.get("type") or None; s.fop = params.get("operator") or None
        s.fids  = set(int(i) for i in params["ids"]) if params.get("ids") else None
        s.flim  = max(1, min(int(params.get("limit", 100)), cfg.BULK_POSITION_LIMIT))
        await s.send({"type": "filter_applied", "ts": _NOW()})
    elif action == "set_speed":
        m = float(params.get("multiplier", 1.0))
        if not -1000 <= m <= 1000:
            await s.send({"type": "error", "message": "multiplier out of range"}); return
        s.clock.set_speed(m)
        await s.send({"type": "speed_set", "multiplier": m,
                    "sim_time": s.clock.sim_iso, "ts": _NOW()})
    elif action == "seek":
        at = params.get("at")
        if not at:
            await s.send({"type": "error", "message": "seek needs params.at"}); return
        try: s.clock.seek(at)
        except Exception as e:
            await s.send({"type": "error", "message": str(e)}); return
        await s.send({"type": "seeked", "sim_time": s.clock.sim_iso, "ts": _NOW()})
    elif action == "set_observer":
        if "lat" not in params or "lon" not in params:
            await s.send({"type": "error", "message": "needs lat/lon"}); return
        s.obs = {"lat": float(params["lat"]), "lon": float(params["lon"]),
                "alt_m": float(params.get("alt_m", 0.0))}
        await s.send({"type": "observer_set", "observer": s.obs, "ts": _NOW()})
    elif action == "get_position":
        sid = params.get("sat_id"); rec = db.get(int(sid)) if sid else None
        if not rec:
            await s.send({"type": "error", "message": f"Satellite {sid} not found"}); return
        p = _sat_position(rec["satellite"], s.clock.now())
        p.update({"id": rec["id"], "name": rec["name"], "type": rec.get("type")})
        await s.send({"type": "position", "ts": _NOW(), "data": p})
    else:
        await s.send({"type": "error", "message": f"Unknown action: {action}"})

# ══════════════════════════════════════════════════════════════════════════════
#  ПОДКЛЮЧАЕМ api/ РОУТЕРЫ (если существуют)
# ══════════════════════════════════════════════════════════════════════════════

def _try_routers(app: FastAPI) -> None:
    for mod_name, prefix, tags in [
        ("satellites", "/satellites", ["satellites"]),
        ("tle",        "/tle",        ["tle"]),
        ("passes",     "/passes",     ["passes"]),
        ("groups",     "/groups",     ["groups"]),
        ("websocket",  "",            ["websocket"]),
    ]:
        try:
            mod = __import__(mod_name)
            app.include_router(mod.router, prefix=prefix, tags=tags)
            print(f"[Router] {prefix or '/ws'} подключён")
        except Exception as e:
            print(f"[Router] {mod_name} пропущен: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  LIFESPAN + APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_tle_on_startup()
    _ensure_ws()
    yield
    for t in _ws_tasks: t.cancel()
    await asyncio.gather(*_ws_tasks, return_exceptions=True)

app = FastAPI(
    title=cfg.APP_NAME, version=cfg.APP_VERSION,
    description="Satellite pass monitoring platform — РНИИРС",
    lifespan=lifespan, docs_url="/docs",
)
app.add_middleware(CORSMiddleware, allow_origins=cfg.CORS_ORIGINS,
                allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_try_routers(app)

# ══════════════════════════════════════════════════════════════════════════════
#  ЭНДПОИНТЫ
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", tags=["root"])
async def root():
    return {"app": cfg.APP_NAME, "version": cfg.APP_VERSION,
            "satellites": db.count(), "docs": "/docs"}

@app.get("/health", tags=["root"])
async def health():
    return {"status": "ok", "satellites": db.count()}

@app.get("/satellites", tags=["satellites"])
async def get_satellites(
    operator: Optional[str] = None,
    sat_type: Optional[str] = Query(None, alias="type"),
    name:     Optional[str] = None,
    limit:    int = Query(200, le=cfg.MAX_SIMULTANEOUS_SATELLITES),
    offset:   int = 0,
):
    return db.list(operator=operator, sat_type=sat_type, name_like=name)[offset:offset+limit]

@app.get("/satellites/types",    tags=["satellites"])
@app.get("/satellite-types",     tags=["satellites"])
async def get_types():     return {"types": db.types()}

@app.get("/satellites/operators", tags=["satellites"])
@app.get("/operators",            tags=["satellites"])
async def get_operators(): return {"operators": db.operators()}

@app.get("/satellites/{sid}", tags=["satellites"])
async def get_satellite(sid: int):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    return {k: v for k, v in rec.items() if k != "satellite"}

@app.get("/satellites/{sid}/position", tags=["satellites"])
async def get_position(sid: int, at: Optional[str] = None):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    if not at:
        c = db.cached_pos(sid)
        if c: return c
    t = _parse_time(at)
    pos = _sat_position(rec["satellite"], t)
    pos.update({"id": sid, "name": rec["name"], "operator": rec["operator"],
                "type": rec["type"], "period_min": round(rec.get("period_min") or 0, 4)})
    db.store_pos(sid, pos)
    return pos

@app.get("/satellites/{sid}/orbit", tags=["satellites"])
async def get_orbit(
    sid:     int,
    minutes: int           = Query(cfg.DEFAULT_TRACK_MINUTES, le=1440),
    steps:   int           = Query(cfg.DEFAULT_TRACK_STEPS,   le=cfg.MAX_TRACK_STEPS),
    start:   Optional[str] = None,
):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    t0   = _parse_time(start).utc_datetime()
    secs = (minutes * 60) / steps
    tms  = ts.utc([t0 + timedelta(seconds=secs*i) for i in range(steps+1)])
    sub  = rec["satellite"].at(tms).subpoint()
    return {"id": sid, "name": rec["name"], "minutes": minutes, "steps": steps,
            "track": [{"lon": round(float(sub.longitude.degrees[i]),5),
                    "lat": round(float(sub.latitude.degrees[i]),5),
                    "alt_km": round(float(sub.elevation.km[i]),2)}
                    for i in range(len(tms.tt))]}

@app.get("/satellites/{sid}/passes", tags=["satellites"])
async def get_passes(
    sid:    int,
    lat:    float = Query(..., ge=-90, le=90),
    lon:    float = Query(..., ge=-180, le=180),
    alt_m:  float = Query(0.0),
    days:   int   = Query(cfg.DEFAULT_PASS_DAYS, le=cfg.MAX_PASS_DAYS),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    obs = Topos(latitude_degrees=lat, longitude_degrees=lon, elevation_m=alt_m)
    t0 = ts.now(); t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))
    try: t_ev, evs = rec["satellite"].find_events(obs, t0, t1, altitude_degrees=min_el)
    except Exception as e: raise HTTPException(500, str(e))
    passes: list[dict] = []; cur: dict = {}
    for ti, ev in zip(t_ev, evs):
        el, az = _elev_az(rec["satellite"], obs, ti)
        if ev == 0:
            cur = {"aos": ti.utc_iso(), "aos_az": round(az,1),
                "max_el": 0.0, "max_el_time": "", "max_el_az": 0.0, "los": "", "los_az": 0.0}
        elif ev == 1 and cur:
            cur["max_el"] = round(el,1); cur["max_el_time"] = ti.utc_iso(); cur["max_el_az"] = round(az,1)
        elif ev == 2 and cur:
            cur["los"] = ti.utc_iso(); cur["los_az"] = round(az,1)
            cur["duration_s"] = int((
                datetime.fromisoformat(ti.utc_iso().replace("Z","+00:00")) -
                datetime.fromisoformat(cur["aos"].replace("Z","+00:00"))
            ).total_seconds())
            passes.append(cur); cur = {}
    return {"satellite": rec["name"],
            "observer": {"lat": lat, "lon": lon, "alt_m": alt_m}, "passes": passes}

@app.get("/satellites/{sid}/next-pass", tags=["satellites"])
async def next_pass(
    sid:    int,
    lat:    float = Query(..., ge=-90, le=90),
    lon:    float = Query(..., ge=-180, le=180),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    obs = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now(); t1 = ts.utc(t0.utc_datetime() + timedelta(days=3))
    try: t_ev, evs = rec["satellite"].find_events(obs, t0, t1, altitude_degrees=min_el)
    except Exception as e: raise HTTPException(500, str(e))
    cur: dict = {}
    for ti, ev in zip(t_ev, evs):
        el, az = _elev_az(rec["satellite"], obs, ti)
        if ev == 0: cur = {"aos": ti.utc_iso(), "aos_az": round(az,1)}
        elif ev == 1 and cur: cur["max_el"] = round(el,1); cur["max_el_time"] = ti.utc_iso()
        elif ev == 2 and cur:
            cur["los"] = ti.utc_iso(); cur["los_az"] = round(az,1)
            cur["duration_s"] = int((
                datetime.fromisoformat(ti.utc_iso().replace("Z","+00:00")) -
                datetime.fromisoformat(cur["aos"].replace("Z","+00:00"))
            ).total_seconds())
            return {"satellite": rec["name"], "pass": cur}
    return {"satellite": rec["name"], "pass": None}

@app.get("/satellites/{sid}/coverage", tags=["satellites"])
async def get_coverage(
    sid:    int,
    min_el: float         = Query(0.0, ge=0, le=45),
    at:     Optional[str] = None,
    step:   float         = Query(1.0, ge=0.5, le=5.0),
):
    rec = db.get(sid)
    if not rec: raise HTTPException(404, "Satellite not found")
    pos = _sat_position(rec["satellite"], _parse_time(at))
    poly, r = _coverage_polygon(pos["lat"], pos["lon"], pos["alt_km"], min_el, step)
    return {"id": sid, "name": rec["name"], "center": [pos["lon"], pos["lat"]],
            "alt_km": pos["alt_km"], "radius_km": r,
            "min_elevation_deg": min_el, "polygon": poly}

@app.get("/positions", tags=["satellites"])
async def get_all_positions(
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = None,
    limit:    int           = Query(100, le=cfg.BULK_POSITION_LIMIT),
    at:       Optional[str] = None,
):
    t = _parse_time(at)
    result = []
    for info in db.list(operator=operator, sat_type=sat_type)[:limit]:
        sid = info["id"]
        if not at:
            c = db.cached_pos(sid)
            if c: result.append(c); continue
        rec = db.get(sid)
        if rec:
            pos = _sat_position(rec["satellite"], t)
            pos.update({"id": sid, "name": rec["name"], "type": rec["type"]})
            db.store_pos(sid, pos); result.append(pos)
    return result

@app.get("/passes", tags=["passes"])
async def passes_over_point(
    lat:    float = Query(..., ge=-90, le=90),
    lon:    float = Query(..., ge=-180, le=180),
    days:   int   = Query(1, le=3),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
    limit:  int   = Query(50, le=200),
):
    obs = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0 = ts.now(); t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))
    result = []
    for sid, rec in list(db._data.items())[:cfg.BULK_POSITION_LIMIT]:
        try: t_ev, evs = rec["satellite"].find_events(obs, t0, t1, altitude_degrees=min_el)
        except Exception: continue
        for ti, ev in zip(t_ev, evs):
            if ev == 1:
                el, _ = _elev_az(rec["satellite"], obs, ti)
                result.append({"id": sid, "name": rec["name"],
                            "max_el": round(el,1), "time": ti.utc_iso()})
                break
    result.sort(key=lambda x: x["time"])
    return result[:limit]

@app.post("/tle/upload", tags=["tle"])
async def upload_tle(file: UploadFile = File(...)):
    raw = await file.read()
    content = raw.decode("utf-8") if raw[:3] != b'\xef\xbb\xbf' else raw.decode("utf-8-sig")
    try: content = raw.decode("utf-8")
    except UnicodeDecodeError: content = raw.decode("latin-1")
    return {"added": parse_tle_text(content), "total": db.count()}

@app.post("/tle/fetch/{category}", tags=["tle"])
async def fetch_tle(category: str):
    if category not in cfg.TLE_SOURCES:
        raise HTTPException(400, f"Unknown category. Available: {list(cfg.TLE_SOURCES)}")
    return {"category": category, "added": await fetch_tle_from_celestrak(category),
            "total": db.count()}

@app.delete("/tle/clear", tags=["tle"])
async def clear_tle():
    count = db.count(); db.clear()
    return {"deleted": count, "total_in_db": 0}

@app.get("/ws/stats", tags=["websocket"])
async def ws_stats():
    return {"total_connections": _ws_mgr.count, "ts": _NOW()}

# /predict — совместимость с фронтендом из satellite-platform.zip
@app.get("/predict", tags=["compat"])
async def predict_compat():
    ids = list(db._data.keys())
    if not ids: return []
    rec = db.get(ids[0])
    if not rec: return []
    t0 = ts.now().utc_datetime(); secs = (90*60)/180
    tms = ts.utc([t0 + timedelta(seconds=secs*i) for i in range(181)])
    sub = rec["satellite"].at(tms).subpoint()
    return [[round(float(sub.latitude.degrees[i]),5),
            round(float(sub.longitude.degrees[i]),5)] for i in range(len(tms.tt))]

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET ЭНДПОИНТЫ
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def ws_main(websocket: WebSocket):
    _ensure_ws(); s = await _ws_mgr.connect(websocket)
    await s.send({"type": "connected", "client_id": s.cid,
                "satellites": db.count(), "ts": _NOW(),
                "hint": 'Send {"action":"subscribe","channel":"positions"} to start'})
    try:
        while True:
            await _handle(s, await websocket.receive_text())
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[WS] {s.cid}: {e}")
    finally: await _ws_mgr.disconnect(s)

@app.websocket("/ws/positions")
async def ws_positions_quick(
    websocket: WebSocket,
    type:     Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    limit:    int           = Query(100, ge=1, le=cfg.BULK_POSITION_LIMIT),
):
    _ensure_ws(); s = await _ws_mgr.connect(websocket)
    s.ftype = type; s.fop = operator; s.flim = limit
    s.channels.add("positions")
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if raw.strip() in ("ping", "{}"): await s.send({"type": "pong", "ts": _NOW()})
            except asyncio.TimeoutError: pass
    except WebSocketDisconnect: pass
    finally: await _ws_mgr.disconnect(s)

@app.websocket("/ws/passes")
async def ws_pass_alerts(
    websocket: WebSocket,
    lat:   float         = Query(...),
    lon:   float         = Query(...),
    alt_m: float         = Query(0.0),
    type:  Optional[str] = Query(None),
):
    _ensure_ws(); s = await _ws_mgr.connect(websocket)
    s.obs = {"lat": lat, "lon": lon, "alt_m": alt_m}; s.ftype = type
    s.channels.add("pass_alerts")
    await s.send({"type": "connected", "channel": "pass_alerts",
                "observer": s.obs, "ts": _NOW()})
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                await _handle(s, raw)
            except asyncio.TimeoutError:
                await s.send({"type": "heartbeat", "ts": _NOW()})
    except WebSocketDisconnect: pass
    finally: await _ws_mgr.disconnect(s)

# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=cfg.HOST, port=cfg.PORT,
                reload=cfg.DEBUG, workers=cfg.WORKERS)