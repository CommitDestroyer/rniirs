"""
main.py — точка входа FastAPI-приложения.

Структура проекта:
    backend/
    ├── app/
    │   ├── main.py      ← этот файл
    │   └── config.py
    ├── api/
    │   ├── satellites.py  (содержит SatelliteDB + db + все хелперы)
    │   ├── tle.py
    │   ├── passes.py
    │   ├── groups.py
    │   └── websocket.py
    └── tle_data.txt

Запуск из папки backend/:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ── исправляем sys.path ───────────────────────────────────────────────────────
# backend/app/main.py → нам нужны backend/app/ (config) и backend/api/ (роутеры)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))   # backend/app/
_API_DIR = os.path.join(_APP_DIR, "..", "api")           # backend/api/
_BCK_DIR = os.path.join(_APP_DIR, "..")                  # backend/

for _p in (_APP_DIR, _API_DIR, _BCK_DIR):
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── импорты ───────────────────────────────────────────────────────────────────
from config import get_settings                          # backend/app/config.py

# Единственный экземпляр db и ts — из satellites.py.
# Все роутеры импортируют db оттуда же → один общий объект.
from satellites import db, ts, _sat_position, _parse_time   # backend/api/satellites.py

from satellites import router as satellites_router
from tle        import router as tle_router
from passes     import router as passes_router
from groups     import router as groups_router
from websocket  import router as ws_router, _tasks as ws_tasks

cfg = get_settings()

# ══════════════════════════════════════════════════════════════════════════════
#  TLE BOOTSTRAP — загрузка при старте
# ══════════════════════════════════════════════════════════════════════════════

def _load_tle_file() -> None:
    """
    Ищет tle_data.txt рядом с backend/ и загружает через tle.parse_tle_text.
    Если файл не найден — добавляет МКС как fallback.
    """
    # Импортируем здесь чтобы не было циклов
    from tle import parse_tle_text, load_records_into_db

    # Ищем файл в нескольких местах
    candidates = [
        cfg.TLE_LOCAL_FILE,                              # относительный путь
        os.path.join(_BCK_DIR, cfg.TLE_LOCAL_FILE),     # backend/tle_data.txt
        os.path.join(_BCK_DIR, "..", cfg.TLE_LOCAL_FILE),  # корень проекта
    ]

    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            try:
                content = open(path, encoding="utf-8", errors="replace").read()
                records, skipped = parse_tle_text(content, source="local")
                added, _ = load_records_into_db(records)
                print(f"[TLE] loaded {added} satellites from {path} (skipped {skipped})")
                return
            except Exception as e:
                print(f"[TLE] error reading {path}: {e}")

    # Fallback — только МКС
    print("[TLE] tle_data.txt not found — using ISS fallback")
    db.add(
        "ISS (ZARYA)",
        "1 25544U 98067A   25079.50000000  .00016717  00000-0  30270-3 0  9999",
        "2 25544  51.6410 135.4567 0003679 101.2345 258.9876 15.49876543219876",
        "NASA/Roscosmos", "LEO",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  LIFESPAN
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_tle_file()
    yield
    # Останавливаем фоновые задачи WebSocket
    for task in ws_tasks:
        task.cancel()
    if ws_tasks:
        await asyncio.gather(*ws_tasks, return_exceptions=True)

# ══════════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title=cfg.APP_NAME,
    version=cfg.APP_VERSION,
    description="Satellite pass monitoring platform — РНИИРС",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
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
#  ROOT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", tags=["root"], summary="Статус API")
async def root():
    return {
        "app":        cfg.APP_NAME,
        "version":    cfg.APP_VERSION,
        "satellites": db.count(),
        "docs":       "/docs",
    }


@app.get("/health", tags=["root"], summary="Health-check")
async def health():
    return {"status": "ok", "satellites": db.count()}


@app.get("/positions", tags=["satellites"], summary="Текущие позиции всех (или отфильтрованных) спутников")
async def get_all_positions(
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
    limit:    int           = Query(100, ge=1, le=cfg.BULK_POSITION_LIMIT),
    at:       Optional[str] = Query(None, description="UTC ISO, напр. 2025-04-01T12:00:00Z"),
):
    subset = db.list(operator=operator, sat_type=sat_type)[:limit]
    t      = _parse_time(at)
    result = []
    for info in subset:
        rec = db.get(info["id"])
        if not rec:
            continue
        if not at:
            cached = db.cached_pos(info["id"])
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
        "app.main:app",
        host=cfg.HOST,
        port=cfg.PORT,
        reload=cfg.DEBUG,
        workers=cfg.WORKERS,
    )