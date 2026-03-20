"""
main.py — точка входа приложения.

Запуск:
    cd backend
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Или через run.sh/run.bat в корне проекта.
"""
from __future__ import annotations

import asyncio
import os
import sys

# ── путь: backend/api/ нужен для импорта роутеров ───────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))          # backend/app/
_API  = os.path.join(_HERE, "..", "api")                    # backend/api/
for _p in (_HERE, _API):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings

# Импортируем общий db и ts из satellites.py (единственный источник истины)
from satellites import db, ts

# Роутеры
from satellites import router as satellites_router
from tle       import router as tle_router
from passes    import router as passes_router
from groups    import router as groups_router
from websocket import router as ws_router

cfg = get_settings()

# ══════════════════════════════════════════════════════════════════════════════
#  TLE BOOTSTRAP
# ══════════════════════════════════════════════════════════════════════════════

def _classify_orbit(l2: str) -> str:
    try:
        mean_motion = float(l2[52:63])
        period_min  = 1440.0 / mean_motion
        # Приближённая высота по периоду
        import math
        a_km = (398600.4418 * (period_min * 60 / (2 * math.pi)) ** 2) ** (1 / 3)
        alt  = a_km - 6371.0
        if alt < cfg.LEO_MAX_KM:   return "LEO"
        if alt < cfg.MEO_MAX_KM:   return "MEO"
        return "GEO"
    except Exception:
        return "Unknown"


def _load_tle_file(path: str) -> int:
    """Читает 3LE-файл и добавляет спутники в общий db."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return 0

    lines  = [ln.strip() for ln in content.splitlines() if ln.strip()]
    added  = 0
    i      = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            sat_type = _classify_orbit(l2)
            if db.add(name, l1, l2, "Various", sat_type):
                added += 1
            i += 3
        else:
            i += 1
    return added


def _iss_fallback() -> None:
    db.add(
        "ISS (ZARYA)",
        "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
        "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
        "NASA/Roscosmos", "LEO",
    )


def bootstrap_tle() -> None:
    """Загружает TLE при старте из локального файла или fallback."""
    # Путь относительно backend/
    tle_path = os.path.join(_HERE, "..", cfg.TLE_LOCAL_FILE)
    count    = _load_tle_file(tle_path)
    if count:
        print(f"[TLE] loaded {count} satellites from {cfg.TLE_LOCAL_FILE}")
    else:
        print("[TLE] local file not found — using ISS fallback")
        _iss_fallback()


# ══════════════════════════════════════════════════════════════════════════════
#  APP LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_tle()
    yield
    # graceful shutdown: отменяем фоновые задачи websocket-роутера
    from websocket import _tasks
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)


app = FastAPI(
    title=cfg.APP_NAME,
    version=cfg.APP_VERSION,
    description="Satellite pass monitoring web platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTERS
# ══════════════════════════════════════════════════════════════════════════════

app.include_router(satellites_router, prefix="/satellites", tags=["satellites"])
app.include_router(tle_router,        prefix="/tle",        tags=["tle"])
app.include_router(passes_router,     prefix="/passes",     tags=["passes"])
app.include_router(groups_router,     prefix="/groups",     tags=["groups"])
app.include_router(ws_router,                               tags=["websocket"])

# ══════════════════════════════════════════════════════════════════════════════
#  ROOT + HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", tags=["root"])
async def root():
    return {
        "app":        cfg.APP_NAME,
        "version":    cfg.APP_VERSION,
        "satellites": db.count(),
        "docs":       "/docs",
    }


@app.get("/health", tags=["root"])
async def health():
    return {"status": "ok", "satellites": db.count()}


# ── bulk positions (удобный shortcut на уровне корня) ─────────────────────────

from typing import Optional
from fastapi import Query

@app.get("/positions", tags=["satellites"])
async def get_all_positions(
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = None,
    limit:    int           = Query(100, le=cfg.BULK_POSITION_LIMIT),
    at:       Optional[str] = None,
):
    """Shortcut: позиции всех (или отфильтрованных) спутников."""
    from satellites import _sat_position, _parse_time
    subset = db.list(operator=operator, sat_type=sat_type)[:limit]
    t      = _parse_time(at)
    result = []
    for info in subset:
        rec = db.get(info["id"])
        if not rec:
            continue
        cached = db.cached_pos(info["id"]) if not at else None
        if cached:
            result.append(cached)
            continue
        pos = _sat_position(rec["satellite"], t)
        pos.update({"id": rec["id"], "name": rec["name"], "type": rec["type"]})
        db.store_pos(info["id"], pos)
        result.append(pos)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=cfg.HOST,
        port=cfg.PORT,
        reload=cfg.DEBUG,
        workers=cfg.WORKERS,
    )