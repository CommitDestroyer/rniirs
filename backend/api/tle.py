"""
tle.py — APIRouter для всех операций с TLE-данными.

Подключение в main.py:
    from tle import router as tle_router
    app.include_router(tle_router, prefix="/tle", tags=["tle"])
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from config import get_settings
from satellites import _orbit_type_from_meta, _parse_tle_meta, db

cfg    = get_settings()
router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
#  ENUMS & CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

class CelestrakCategory(str, Enum):
    stations = "stations"
    starlink = "starlink"
    gps      = "gps"
    resource = "resource"
    active   = "active"
    weather  = "weather"
    noaa     = "noaa"
    goes     = "goes"
    amateur  = "amateur"
    debris   = "debris"


CELESTRAK_URLS: dict[str, str] = {
    "stations": "https://celestrak.org/SATCAT/stations.txt",
    "starlink":  "https://celestrak.org/SATCAT/starlink.txt",
    "gps":       "https://celestrak.org/SATCAT/gps-ops.txt",
    "resource":  "https://celestrak.org/SATCAT/resource.txt",
    "active":    "https://celestrak.org/SATCAT/active.txt",
    "weather":   "https://celestrak.org/SATCAT/weather.txt",
    "noaa":      "https://celestrak.org/SATCAT/noaa.txt",
    "goes":      "https://celestrak.org/SATCAT/goes.txt",
    "amateur":   "https://celestrak.org/SATCAT/amateur.txt",
    "debris":    "https://celestrak.org/SATCAT/iridium-33-debris.txt",
}

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class TLERecord(BaseModel):
    name:            str
    line1:           str
    line2:           str
    norad_id:        Optional[int]   = None
    epoch:           Optional[str]   = None
    orbit_type:      Optional[str]   = None
    inclination_deg: Optional[float] = None
    period_min:      Optional[float] = None
    apogee_km:       Optional[float] = None
    perigee_km:      Optional[float] = None
    eccentricity:    Optional[float] = None
    operator:        str             = "Unknown"
    source:          str             = "manual"


class TLEUploadResult(BaseModel):
    source:      str
    added:       int
    skipped:     int
    total_in_db: int
    sha256:      str
    loaded_at:   str


class TLEValidationResult(BaseModel):
    valid:      bool
    name:       Optional[str]   = None
    norad_id:   Optional[int]   = None
    orbit_type: Optional[str]   = None
    period_min: Optional[float] = None
    epoch:      Optional[str]   = None
    errors:     list[str]       = Field(default_factory=list)


class TLESourceStatus(BaseModel):
    category:     str
    url:          str
    last_fetched: Optional[str]
    count:        int
    sha256:       Optional[str]


class RawTLERequest(BaseModel):
    content:  str  = Field(..., min_length=10)
    operator: str  = Field("Manual")
    source:   str  = Field("raw")
    replace:  bool = Field(False)


class ValidateRequest(BaseModel):
    line1: str = Field(..., min_length=69, max_length=69)
    line2: str = Field(..., min_length=69, max_length=69)
    name:  str = Field("")


class AutoUpdateConfig(BaseModel):
    categories: list[str]     = Field(default_factory=list)
    interval_h: Optional[int] = Field(None, ge=1, le=168)

# ══════════════════════════════════════════════════════════════════════════════
#  LOAD HISTORY
# ══════════════════════════════════════════════════════════════════════════════

class _LoadHistory:
    def __init__(self) -> None:
        self._records:     list[TLEUploadResult] = []
        self._source_meta: dict[str, dict]       = {}

    def add(self, result: TLEUploadResult) -> None:
        self._records.append(result)
        self._source_meta[result.source] = {
            "last_fetched": result.loaded_at,
            "sha256":       result.sha256,
            "count":        result.added,
        }

    def history(self, limit: int = 20) -> list[TLEUploadResult]:
        return self._records[-limit:]

    def source_meta(self, source: str) -> Optional[dict]:
        return self._source_meta.get(source)

    def all_source_meta(self) -> dict[str, dict]:
        return dict(self._source_meta)


history = _LoadHistory()

# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def _checksum(line: str) -> int:
    total = 0
    for ch in line[:-1]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def validate_tle_lines(line1: str, line2: str) -> list[str]:
    """Возвращает список ошибок. Пустой список — TLE валидно."""
    errors: list[str] = []

    if not line1.startswith("1 "):
        errors.append("Line1 must start with '1 '")
    if not line2.startswith("2 "):
        errors.append("Line2 must start with '2 '")
    if len(line1) != 69:
        errors.append(f"Line1 length must be 69, got {len(line1)}")
    if len(line2) != 69:
        errors.append(f"Line2 length must be 69, got {len(line2)}")

    if not errors:
        cs1 = _checksum(line1)
        if cs1 != int(line1[-1]):
            errors.append(f"Line1 checksum: expected {int(line1[-1])}, got {cs1}")
        cs2 = _checksum(line2)
        if cs2 != int(line2[-1]):
            errors.append(f"Line2 checksum: expected {int(line2[-1])}, got {cs2}")

    try:
        norad1 = int(line1[2:7])
        norad2 = int(line2[2:7])
        if norad1 != norad2:
            errors.append(f"NORAD ID mismatch: {norad1} vs {norad2}")
    except ValueError:
        errors.append("Cannot parse NORAD ID")

    return errors

# ══════════════════════════════════════════════════════════════════════════════
#  PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_tle_text(content: str, source: str = "manual",
                   operator: str = "Various") -> tuple[list[TLERecord], int]:
    """
    Парсит 3LE / 2LE текст.
    Возвращает (список TLERecord, количество пропущенных строк).
    """
    lines   = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    records: list[TLERecord] = []
    skipped = 0
    i = 0

    while i < len(lines):
        # 3LE: имя + line1 + line2
        if (i + 2 < len(lines)
                and not lines[i].startswith("1 ")
                and lines[i + 1].startswith("1 ")
                and lines[i + 2].startswith("2 ")):
            name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
            i += 3
        # 2LE: line1 + line2 без имени
        elif (i + 1 < len(lines)
              and lines[i].startswith("1 ")
              and lines[i + 1].startswith("2 ")):
            name = f"NORAD-{lines[i][2:7].strip()}"
            l1, l2 = lines[i], lines[i + 1]
            i += 2
        else:
            skipped += 1
            i += 1
            continue

        errs = validate_tle_lines(l1, l2)
        if errs:
            skipped += 1
            continue

        meta       = _parse_tle_meta(l1, l2)
        orbit_type = _orbit_type_from_meta(meta)

        records.append(TLERecord(
            name=name.strip(), line1=l1, line2=l2,
            norad_id=meta.get("norad_id"),
            epoch=meta.get("epoch"),
            orbit_type=orbit_type,
            inclination_deg=meta.get("inclination_deg"),
            period_min=meta.get("period_min"),
            apogee_km=meta.get("apogee_km"),
            perigee_km=meta.get("perigee_km"),
            eccentricity=meta.get("eccentricity"),
            operator=operator,
            source=source,
        ))

    return records, skipped


def load_records_into_db(records: list[TLERecord]) -> tuple[int, int]:
    """Загружает TLERecord-ы в SatelliteDB. Дубли по NORAD ID пропускаются."""
    added = skipped = 0
    for rec in records:
        if rec.norad_id and db.get_by_norad(rec.norad_id):
            skipped += 1
            continue
        sid = db.add(
            name=rec.name, l1=rec.line1, l2=rec.line2,
            operator=rec.operator,
            sat_type=rec.orbit_type or "Unknown",
        )
        added += 1 if sid else 0
        skipped += 0 if sid else 1
    return added, skipped

# ══════════════════════════════════════════════════════════════════════════════
#  CELESTRAK FETCHER
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_celestrak(category: str) -> tuple[str, int]:
    url = CELESTRAK_URLS.get(category)
    if not url:
        raise HTTPException(400, f"Unknown category '{category}'")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(url)
    return r.text, r.status_code


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_result(source: str, content: str,
                 added: int, skipped: int) -> TLEUploadResult:
    return TLEUploadResult(
        source=source, added=added, skipped=skipped,
        total_in_db=db.count(),
        sha256=_sha256(content),
        loaded_at=_now_iso(),
    )

# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-UPDATER
# ══════════════════════════════════════════════════════════════════════════════

class _AutoUpdater:
    def __init__(self) -> None:
        self._task:       Optional[asyncio.Task] = None
        self._running     = False
        self._interval_h  = cfg.TLE_UPDATE_INTERVAL_H
        self._last_run:   Optional[str] = None
        self._categories: list[str]     = list(CELESTRAK_URLS.keys())

    async def _loop(self) -> None:
        while self._running:
            for cat in self._categories:
                try:
                    content, status = await _fetch_celestrak(cat)
                    if status == 200:
                        records, sp = parse_tle_text(content, source=cat)
                        added, sk   = load_records_into_db(records)
                        history.add(_make_result(cat, content, added, sp + sk))
                        print(f"[AutoUpdate] {cat}: +{added}")
                except Exception as e:
                    print(f"[AutoUpdate] {cat}: {e}")
            self._last_run = _now_iso()
            await asyncio.sleep(self._interval_h * 3600)

    def start(self, categories: Optional[list[str]] = None,
              interval_h: Optional[int] = None) -> None:
        if self._running:
            return
        if categories:
            self._categories = categories
        if interval_h:
            self._interval_h = interval_h
        self._running = True
        self._task    = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    @property
    def status(self) -> dict:
        return {
            "running":    self._running,
            "interval_h": self._interval_h,
            "last_run":   self._last_run,
            "categories": self._categories,
        }


auto_updater = _AutoUpdater()

# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/sources", response_model=list[TLESourceStatus],
            summary="Доступные источники TLE и статус последней загрузки")
async def list_sources():
    return [
        TLESourceStatus(
            category=cat, url=url,
            last_fetched=(m := history.source_meta(cat)) and m["last_fetched"] or None,
            count=m["count"] if m else 0,
            sha256=m["sha256"] if m else None,
        )
        for cat, url in CELESTRAK_URLS.items()
    ]


@router.post("/fetch/{category}", response_model=TLEUploadResult,
             summary="Скачать TLE из Celestrak по категории")
async def fetch_from_celestrak(
    category: CelestrakCategory,
    replace:  bool = Query(False, description="Очистить категорию перед загрузкой"),
):
    content, status = await _fetch_celestrak(category.value)
    if status != 200:
        raise HTTPException(502, f"Celestrak returned HTTP {status}")

    records, sp = parse_tle_text(content, source=category.value)
    if not records:
        raise HTTPException(422, "No valid TLE records in response")

    if replace:
        to_del = [
            r["id"] for r in db.list()
            if (rec := db.get(r["id"])) and
            rec.get("operator", "").lower() == category.value
        ]
        for sid in to_del:
            await db.remove_async(sid)

    added, sk = load_records_into_db(records)
    result    = _make_result(category.value, content, added, sp + sk)
    history.add(result)
    return result


@router.post("/fetch", response_model=list[TLEUploadResult],
             summary="Скачать несколько категорий одновременно")
async def fetch_multiple(
    categories:       list[CelestrakCategory],
    background_tasks: BackgroundTasks,
    background:       bool = Query(False),
):
    async def _do(cats: list[CelestrakCategory]) -> list[TLEUploadResult]:
        out = []
        for cat in cats:
            try:
                content, status = await _fetch_celestrak(cat.value)
                if status != 200:
                    continue
                records, sp = parse_tle_text(content, source=cat.value)
                added, sk   = load_records_into_db(records)
                r           = _make_result(cat.value, content, added, sp + sk)
                history.add(r)
                out.append(r)
            except Exception as e:
                print(f"[fetch_multiple] {cat}: {e}")
        return out

    if background:
        background_tasks.add_task(_do, categories)
        return []
    return await _do(categories)


@router.post("/upload", response_model=TLEUploadResult,
             summary="Загрузить TLE из файла (.txt / .tle)")
async def upload_file(
    file:     UploadFile = File(...),
    operator: str        = Query("Manual"),
    replace:  bool       = Query(False),
):
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    if not content.strip():
        raise HTTPException(400, "File is empty")
    if replace:
        db.clear()

    records, sp = parse_tle_text(content,
                                  source=file.filename or "upload",
                                  operator=operator)
    if not records:
        raise HTTPException(422, "No valid TLE records in file")

    added, sk = load_records_into_db(records)
    result    = _make_result(file.filename or "upload", content, added, sp + sk)
    history.add(result)
    return result


@router.post("/raw", response_model=TLEUploadResult,
             summary="Загрузить TLE как JSON-строку")
async def upload_raw(body: RawTLERequest):
    if body.replace:
        db.clear()
    records, sp = parse_tle_text(body.content, source=body.source,
                                  operator=body.operator)
    if not records:
        raise HTTPException(422, "No valid TLE records found")
    added, sk = load_records_into_db(records)
    result    = _make_result(body.source, body.content, added, sp + sk)
    history.add(result)
    return result


@router.post("/validate", response_model=TLEValidationResult,
             summary="Проверить пару TLE-строк на корректность")
async def validate_tle(body: ValidateRequest):
    errors = validate_tle_lines(body.line1, body.line2)
    if errors:
        return TLEValidationResult(valid=False, errors=errors)
    meta = _parse_tle_meta(body.line1, body.line2)
    return TLEValidationResult(
        valid=True,
        name=body.name or f"NORAD-{meta.get('norad_id', '?')}",
        norad_id=meta.get("norad_id"),
        orbit_type=_orbit_type_from_meta(meta),
        period_min=meta.get("period_min"),
        epoch=meta.get("epoch"),
    )


@router.post("/parse-preview", response_model=list[TLERecord],
             summary="Распарсить файл без записи в БД (предпросмотр)")
async def parse_preview(
    file:  UploadFile = File(...),
    limit: int        = Query(50, ge=1, le=500),
):
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    records, _ = parse_tle_text(content)
    return records[:limit]


@router.post("/load-local", response_model=TLEUploadResult,
             summary="Загрузить TLE из локального файла на сервере")
async def load_local(
    path:    str  = Query(cfg.TLE_LOCAL_FILE),
    replace: bool = Query(False),
):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"File not found: {path}")
    content = p.read_text(encoding="utf-8", errors="replace")
    if replace:
        db.clear()
    records, sp = parse_tle_text(content, source=p.name)
    added, sk   = load_records_into_db(records)
    result      = _make_result(p.name, content, added, sp + sk)
    history.add(result)
    return result


@router.get("/export", response_class=PlainTextResponse,
            summary="Экспорт TLE из БД в файл (3LE формат)")
async def export_tle(
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
    limit:    int            = Query(500, ge=1, le=5000),
):
    records = db.list(operator=operator, sat_type=sat_type)[:limit]
    lines: list[str] = []
    for info in records:
        rec = db.get(info["id"])
        if rec:
            lines.extend([rec["name"], rec["line1"], rec["line2"]])
    if not lines:
        raise HTTPException(404, "No satellites match the filter")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Content-Disposition": "attachment; filename=export.tle"},
    )


@router.delete("/clear", summary="Очистить всю БД")
async def clear_db():
    count = db.count()
    db.clear()
    return {"deleted": count, "total_in_db": 0}


@router.delete("/clear/{category}", summary="Удалить спутники категории")
async def clear_category(category: str):
    to_del = [
        r["id"] for r in db.list()
        if (rec := db.get(r["id"])) and
        rec.get("operator", "").lower() == category.lower()
    ]
    for sid in to_del:
        await db.remove_async(sid)
    return {"category": category, "deleted": len(to_del), "total_in_db": db.count()}


@router.get("/history", response_model=list[TLEUploadResult],
            summary="История загрузок TLE")
async def upload_history(limit: int = Query(20, ge=1, le=100)):
    return history.history(limit)


@router.get("/stats", summary="Статистика по загруженным TLE")
async def tle_stats():
    all_recs = db.all_records()
    if not all_recs:
        return {"total": 0}

    by_type:     dict[str, int] = {}
    by_operator: dict[str, int] = {}
    epochs:      list[datetime] = []
    periods:     list[float]    = []

    for rec in all_recs:
        t  = rec.get("type", "Unknown")
        op = rec.get("operator", "Unknown")
        by_type[t]      = by_type.get(t, 0) + 1
        by_operator[op] = by_operator.get(op, 0) + 1
        if rec.get("epoch"):
            try:
                epochs.append(datetime.fromisoformat(
                    rec["epoch"].replace("Z", "+00:00")))
            except Exception:
                pass
        if rec.get("period_min"):
            periods.append(rec["period_min"])

    return {
        "total":           len(all_recs),
        "by_orbit_type":   by_type,
        "by_operator":     dict(sorted(by_operator.items(),
                                        key=lambda x: -x[1])[:20]),
        "epoch_range": {
            "oldest": min(epochs).strftime("%Y-%m-%dT%H:%M:%SZ") if epochs else None,
            "newest": max(epochs).strftime("%Y-%m-%dT%H:%M:%SZ") if epochs else None,
        },
        "period_range_min": {
            "min": round(min(periods), 2) if periods else None,
            "max": round(max(periods), 2) if periods else None,
        },
        "sources_loaded": list(history.all_source_meta().keys()),
    }


@router.get("/norad/{norad_id}", summary="Найти спутник по NORAD ID")
async def get_by_norad(norad_id: int):
    rec = db.get_by_norad(norad_id)
    if not rec:
        raise HTTPException(404, f"NORAD {norad_id} not found")
    return {
        "id":         rec["id"],
        "name":       rec["name"],
        "norad_id":   rec.get("norad_id"),
        "type":       rec["type"],
        "operator":   rec["operator"],
        "epoch":      rec.get("epoch"),
        "period_min": rec.get("period_min"),
        "apogee_km":  rec.get("apogee_km"),
        "perigee_km": rec.get("perigee_km"),
        "line1":      rec["line1"],
        "line2":      rec["line2"],
    }


@router.post("/auto-update/start", summary="Запустить авто-обновление TLE")
async def start_auto_update(config: AutoUpdateConfig):
    if auto_updater.status["running"]:
        return {"message": "Already running", **auto_updater.status}
    unknown = [c for c in config.categories if c not in CELESTRAK_URLS]
    if unknown:
        raise HTTPException(400, f"Unknown categories: {unknown}")
    auto_updater.start(
        categories=config.categories or None,
        interval_h=config.interval_h,
    )
    return {"message": "Started", **auto_updater.status}


@router.post("/auto-update/stop", summary="Остановить авто-обновление")
async def stop_auto_update():
    auto_updater.stop()
    return {"message": "Stopped", **auto_updater.status}


@router.get("/auto-update/status", summary="Статус авто-обновления")
async def auto_update_status():
    return auto_updater.status