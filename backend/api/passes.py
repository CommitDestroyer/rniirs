"""
passes.py — APIRouter для всех операций с пролётами спутников.

Подключение в main.py:
    from passes import router as passes_router
    app.include_router(passes_router, prefix="/passes", tags=["passes"])
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from skyfield.api import Topos

from config import get_settings
from satellites import (
    _elev_az,
    _parse_time,
    _sat_position,
    db,
    ts,
)

cfg    = get_settings()
router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class PassEvent(BaseModel):
    sat_id:       int
    sat_name:     str
    sat_type:     str
    operator:     str
    norad_id:     Optional[int]
    aos:          str               # UTC ISO — появление над горизонтом
    aos_az:       float             # азимут при появлении
    max_el:       float             # максимальная элевация (градусы)
    max_el_time:  str
    max_el_az:    float
    los:          str               # UTC ISO — уход за горизонт
    los_az:       float
    duration_s:   int               # длительность прохода (секунды)


class PassesOverPointResponse(BaseModel):
    observer:     dict
    days:         int
    min_el:       float
    total:        int
    passes:       list[PassEvent]


class NextPassResponse(BaseModel):
    sat_id:       int
    sat_name:     str
    observer:     dict
    pass_event:   Optional[PassEvent]
    search_days:  int


class MultiSatNextPassResponse(BaseModel):
    observer:     dict
    results:      list[NextPassResponse]


class PassTimelineItem(BaseModel):
    sat_id:       int
    sat_name:     str
    aos:          str
    los:          str
    max_el:       float
    duration_s:   int
    overlap_with: list[int]         # sat_id спутников с одновременным пролётом


class PassTimelineResponse(BaseModel):
    observer:     dict
    days:         int
    items:        list[PassTimelineItem]
    max_overlap:  int               # максимальное число одновременных спутников


class ElevationPoint(BaseModel):
    time:         str
    elevation:    float
    azimuth:      float


class PassDetailResponse(BaseModel):
    sat_id:       int
    sat_name:     str
    observer:     dict
    aos:          str
    los:          str
    max_el:       float
    duration_s:   int
    elevation_profile: list[ElevationPoint]   # кривая элевации по времени


class BestPassItem(BaseModel):
    sat_id:       int
    sat_name:     str
    sat_type:     str
    aos:          str
    max_el:       float
    duration_s:   int
    score:        float             # взвешенная оценка качества пролёта


class BestPassesResponse(BaseModel):
    observer:     dict
    days:         int
    passes:       list[BestPassItem]


class PassStatsResponse(BaseModel):
    observer:        dict
    days:            int
    total_passes:    int
    total_covered_min: float
    avg_duration_s:  Optional[float]
    avg_max_el:      Optional[float]
    max_el_ever:     Optional[float]
    passes_per_day:  float
    by_sat_type:     dict[str, int]
    by_hour_utc:     dict[int, int]  # распределение пролётов по часам суток


class VisibilityWindowItem(BaseModel):
    start:        str
    end:          str
    duration_min: float
    sats_visible: int
    sat_ids:      list[int]


class VisibilityWindowsResponse(BaseModel):
    observer:     dict
    days:         int
    windows:      list[VisibilityWindowItem]
    total_covered_min: float
    coverage_pct: float


# ══════════════════════════════════════════════════════════════════════════════
#  CORE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _dt_diff_s(a: str, b: str) -> int:
    return int((_parse_dt(b) - _parse_dt(a)).total_seconds())


def _find_passes_for_rec(
    rec:      dict,
    observer: Topos,
    t0,
    t1,
    min_el:   float,
) -> list[PassEvent]:
    """
    Находит все пролёты спутника `rec` над наблюдателем в интервале [t0, t1].
    Возвращает список PassEvent.
    """
    try:
        t_ev, evs = rec["satellite"].find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception:
        return []

    passes: list[PassEvent] = []
    current: dict = {}

    for ti, ev in zip(t_ev, evs):
        el, az = _elev_az(rec["satellite"], observer, ti)

        if ev == 0:                           # AOS
            current = {
                "aos":         ti.utc_iso(),
                "aos_az":      round(az, 1),
                "max_el":      0.0,
                "max_el_time": ti.utc_iso(),
                "max_el_az":   round(az, 1),
            }
        elif ev == 1 and current:             # Transit
            current["max_el"]      = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
            current["max_el_az"]   = round(az, 1)
        elif ev == 2 and current:             # LOS
            passes.append(PassEvent(
                sat_id=rec["id"],
                sat_name=rec["name"],
                sat_type=rec.get("type", "Unknown"),
                operator=rec.get("operator", "Unknown"),
                norad_id=rec.get("norad_id"),
                aos=current["aos"],
                aos_az=current["aos_az"],
                max_el=current["max_el"],
                max_el_time=current["max_el_time"],
                max_el_az=current["max_el_az"],
                los=ti.utc_iso(),
                los_az=round(az, 1),
                duration_s=_dt_diff_s(current["aos"], ti.utc_iso()),
            ))
            current = {}

    return passes


def _make_observer(lat: float, lon: float, alt_m: float = 0.0) -> Topos:
    return Topos(latitude_degrees=lat, longitude_degrees=lon, elevation_m=alt_m)


def _pass_score(p: PassEvent) -> float:
    """
    Взвешенная оценка качества пролёта.
    Учитывает максимальную элевацию (60%) и длительность (40%).
    Нормировано к [0, 100].
    """
    el_score  = min(p.max_el / 90.0, 1.0) * 60
    dur_score = min(p.duration_s / 600.0, 1.0) * 40   # 600 сек = 10 мин — максимум
    return round(el_score + dur_score, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── все пролёты над точкой ────────────────────────────────────────────────────

@router.get("/over-point", response_model=PassesOverPointResponse,
            summary="Все пролёты всех спутников над заданной точкой")
async def passes_over_point(
    lat:      float          = Query(..., ge=-90,  le=90),
    lon:      float          = Query(..., ge=-180, le=180),
    alt_m:    float          = Query(0.0, ge=0, le=8848),
    days:     int            = Query(1,   ge=1, le=cfg.MAX_PASS_DAYS),
    min_el:   float          = Query(cfg.MIN_ELEVATION_DEG, ge=0, le=90),
    sat_type: Optional[str]  = Query(None, alias="type"),
    operator: Optional[str]  = Query(None),
    limit:    int            = Query(200, ge=1, le=1000),
    sort_by:  str            = Query("aos", regex="^(aos|max_el|duration_s)$"),
    desc:     bool           = Query(False),
):
    records  = db.list(operator=operator, sat_type=sat_type)
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    all_passes: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if not rec:
            continue
        all_passes.extend(_find_passes_for_rec(rec, observer, t0, t1, min_el))

    all_passes.sort(key=lambda p: getattr(p, sort_by), reverse=desc)

    return PassesOverPointResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        days=days,
        min_el=min_el,
        total=len(all_passes),
        passes=all_passes[:limit],
    )


# ── ближайший пролёт спутника ─────────────────────────────────────────────────

@router.get("/{sid}/next", response_model=NextPassResponse,
            summary="Ближайший пролёт конкретного спутника над точкой")
async def next_pass(
    sid:         int,
    lat:         float = Query(..., ge=-90,  le=90),
    lon:         float = Query(..., ge=-180, le=180),
    alt_m:       float = Query(0.0, ge=0, le=8848),
    min_el:      float = Query(cfg.MIN_ELEVATION_DEG),
    search_days: int   = Query(3, ge=1, le=7),
):
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=search_days))
    passes   = _find_passes_for_rec(rec, observer, t0, t1, min_el)

    return NextPassResponse(
        sat_id=sid,
        sat_name=rec["name"],
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        pass_event=passes[0] if passes else None,
        search_days=search_days,
    )


# ── ближайшие пролёты нескольких спутников ────────────────────────────────────

@router.get("/next/multi", response_model=MultiSatNextPassResponse,
            summary="Ближайший пролёт для нескольких спутников (batch)")
async def next_pass_multi(
    ids:         str   = Query(..., description="sat_id через запятую: 1,2,5"),
    lat:         float = Query(..., ge=-90,  le=90),
    lon:         float = Query(..., ge=-180, le=180),
    alt_m:       float = Query(0.0),
    min_el:      float = Query(cfg.MIN_ELEVATION_DEG),
    search_days: int   = Query(3, ge=1, le=7),
):
    try:
        sids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if len(sids) > 50:
        raise HTTPException(400, "Max 50 satellites per request")

    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=search_days))
    results  = []

    for sid in sids:
        rec = db.get(sid)
        if not rec:
            continue
        passes = _find_passes_for_rec(rec, observer, t0, t1, min_el)
        results.append(NextPassResponse(
            sat_id=sid,
            sat_name=rec["name"],
            observer={"lat": lat, "lon": lon, "alt_m": alt_m},
            pass_event=passes[0] if passes else None,
            search_days=search_days,
        ))

    results.sort(key=lambda r: r.pass_event.aos if r.pass_event else "9999")
    return MultiSatNextPassResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        results=results,
    )


# ── детальный профиль пролёта ─────────────────────────────────────────────────

@router.get("/{sid}/detail", response_model=PassDetailResponse,
            summary="Детальный профиль пролёта: кривая элевации по времени")
async def pass_detail(
    sid:       int,
    lat:       float = Query(..., ge=-90,  le=90),
    lon:       float = Query(..., ge=-180, le=180),
    alt_m:     float = Query(0.0),
    min_el:    float = Query(cfg.MIN_ELEVATION_DEG),
    pass_index: int  = Query(0, ge=0, description="Индекс пролёта (0 = ближайший)"),
    profile_steps: int = Query(60, ge=10, le=300,
                               description="Количество точек в кривой элевации"),
):
    """
    Возвращает профиль одного конкретного пролёта с кривой elevation/azimuth.
    Полезно для отрисовки полярной диаграммы на фронтенде.
    """
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=3))
    passes   = _find_passes_for_rec(rec, observer, t0, t1, min_el)

    if not passes:
        raise HTTPException(404, "No passes found in next 3 days")
    if pass_index >= len(passes):
        raise HTTPException(400, f"pass_index {pass_index} out of range (found {len(passes)})")

    p = passes[pass_index]

    # Строим кривую элевации с шагом
    aos_dt = _parse_dt(p.aos)
    los_dt = _parse_dt(p.los)
    total_s = (los_dt - aos_dt).total_seconds()
    step_s  = total_s / (profile_steps - 1)

    profile: list[ElevationPoint] = []
    for i in range(profile_steps):
        pt_dt = aos_dt + timedelta(seconds=step_s * i)
        pt_t  = ts.utc(pt_dt.year, pt_dt.month, pt_dt.day,
                       pt_dt.hour, pt_dt.minute, pt_dt.second + pt_dt.microsecond / 1e6)
        el, az = _elev_az(rec["satellite"], observer, pt_t)
        profile.append(ElevationPoint(
            time=pt_t.utc_iso(),
            elevation=round(el, 2),
            azimuth=round(az, 2),
        ))

    return PassDetailResponse(
        sat_id=sid,
        sat_name=rec["name"],
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        aos=p.aos, los=p.los,
        max_el=p.max_el,
        duration_s=p.duration_s,
        elevation_profile=profile,
    )


# ── таймлайн пролётов с overlap ───────────────────────────────────────────────

@router.get("/timeline", response_model=PassTimelineResponse,
            summary="Таймлайн пролётов с поиском одновременных пролётов (overlap)")
async def passes_timeline(
    lat:      float         = Query(..., ge=-90,  le=90),
    lon:      float         = Query(..., ge=-180, le=180),
    alt_m:    float         = Query(0.0),
    days:     int           = Query(1, ge=1, le=3),
    min_el:   float         = Query(cfg.MIN_ELEVATION_DEG),
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
    limit:    int           = Query(100, ge=1, le=500),
):
    """
    Строит хронологический таймлайн пролётов.
    Для каждого пролёта указывает какие другие спутники видны одновременно.
    """
    records  = db.list(operator=operator, sat_type=sat_type)
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    all_passes: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if rec:
            all_passes.extend(_find_passes_for_rec(rec, observer, t0, t1, min_el))

    all_passes.sort(key=lambda p: p.aos)
    all_passes = all_passes[:limit]

    # Для каждого пролёта ищем пересечения по времени
    items: list[PassTimelineItem] = []
    max_overlap = 0

    for i, p in enumerate(all_passes):
        overlaps = []
        for j, other in enumerate(all_passes):
            if i == j:
                continue
            # Пролёты пересекаются если один начинается до конца другого
            if p.aos < other.los and p.los > other.aos:
                overlaps.append(other.sat_id)
        max_overlap = max(max_overlap, len(overlaps) + 1)
        items.append(PassTimelineItem(
            sat_id=p.sat_id,
            sat_name=p.sat_name,
            aos=p.aos,
            los=p.los,
            max_el=p.max_el,
            duration_s=p.duration_s,
            overlap_with=overlaps,
        ))

    return PassTimelineResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        days=days,
        items=items,
        max_overlap=max_overlap,
    )


# ── лучшие пролёты ────────────────────────────────────────────────────────────

@router.get("/best", response_model=BestPassesResponse,
            summary="Лучшие пролёты по взвешенной оценке (элевация + длительность)")
async def best_passes(
    lat:      float         = Query(..., ge=-90,  le=90),
    lon:      float         = Query(..., ge=-180, le=180),
    alt_m:    float         = Query(0.0),
    days:     int           = Query(1, ge=1, le=cfg.MAX_PASS_DAYS),
    min_el:   float         = Query(cfg.MIN_ELEVATION_DEG),
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
    top_n:    int           = Query(20, ge=1, le=100),
):
    records  = db.list(operator=operator, sat_type=sat_type)
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    all_passes: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if rec:
            all_passes.extend(_find_passes_for_rec(rec, observer, t0, t1, min_el))

    scored = sorted(all_passes, key=_pass_score, reverse=True)[:top_n]

    return BestPassesResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        days=days,
        passes=[
            BestPassItem(
                sat_id=p.sat_id,
                sat_name=p.sat_name,
                sat_type=p.sat_type,
                aos=p.aos,
                max_el=p.max_el,
                duration_s=p.duration_s,
                score=_pass_score(p),
            )
            for p in scored
        ],
    )


# ── статистика пролётов ───────────────────────────────────────────────────────

@router.get("/stats", response_model=PassStatsResponse,
            summary="Статистика пролётов над точкой за период")
async def passes_stats(
    lat:      float         = Query(..., ge=-90,  le=90),
    lon:      float         = Query(..., ge=-180, le=180),
    alt_m:    float         = Query(0.0),
    days:     int           = Query(1, ge=1, le=cfg.MAX_PASS_DAYS),
    min_el:   float         = Query(cfg.MIN_ELEVATION_DEG),
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
):
    records  = db.list(operator=operator, sat_type=sat_type)
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    all_passes: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if rec:
            all_passes.extend(_find_passes_for_rec(rec, observer, t0, t1, min_el))

    if not all_passes:
        return PassStatsResponse(
            observer={"lat": lat, "lon": lon, "alt_m": alt_m},
            days=days, total_passes=0, total_covered_min=0.0,
            avg_duration_s=None, avg_max_el=None, max_el_ever=None,
            passes_per_day=0.0, by_sat_type={}, by_hour_utc={},
        )

    durations = [p.duration_s for p in all_passes]
    elevs     = [p.max_el     for p in all_passes]

    by_type: dict[str, int] = defaultdict(int)
    by_hour: dict[int, int] = defaultdict(int)
    for p in all_passes:
        by_type[p.sat_type] += 1
        try:
            h = _parse_dt(p.aos).hour
            by_hour[h] += 1
        except Exception:
            pass

    total_covered_s = sum(durations)

    return PassStatsResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        days=days,
        total_passes=len(all_passes),
        total_covered_min=round(total_covered_s / 60, 1),
        avg_duration_s=round(statistics.mean(durations), 1),
        avg_max_el=round(statistics.mean(elevs), 1),
        max_el_ever=round(max(elevs), 1),
        passes_per_day=round(len(all_passes) / days, 2),
        by_sat_type=dict(sorted(by_type.items(), key=lambda x: -x[1])),
        by_hour_utc={h: by_hour[h] for h in sorted(by_hour)},
    )


# ── окна видимости (merged) ────────────────────────────────────────────────────

@router.get("/visibility-windows", response_model=VisibilityWindowsResponse,
            summary="Непрерывные окна видимости хотя бы одного спутника")
async def visibility_windows(
    lat:      float         = Query(..., ge=-90,  le=90),
    lon:      float         = Query(..., ge=-180, le=180),
    alt_m:    float         = Query(0.0),
    days:     int           = Query(1, ge=1, le=3),
    min_el:   float         = Query(cfg.MIN_ELEVATION_DEG),
    sat_type: Optional[str] = Query(None, alias="type"),
    operator: Optional[str] = Query(None),
):
    """
    Сливает все пролёты в непрерывные окна видимости.
    Показывает сколько спутников одновременно видно в каждом окне.
    """
    records  = db.list(operator=operator, sat_type=sat_type)
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))
    t0_dt    = t0.utc_datetime().replace(tzinfo=timezone.utc)
    t1_dt    = t1.utc_datetime().replace(tzinfo=timezone.utc)
    total_s  = (t1_dt - t0_dt).total_seconds()

    # Собираем интервалы с sat_id
    intervals: list[tuple[datetime, datetime, int]] = []
    for info in records:
        rec = db.get(info["id"])
        if not rec:
            continue
        passes = _find_passes_for_rec(rec, observer, t0, t1, min_el)
        for p in passes:
            intervals.append((
                _parse_dt(p.aos),
                _parse_dt(p.los),
                p.sat_id,
            ))

    if not intervals:
        return VisibilityWindowsResponse(
            observer={"lat": lat, "lon": lon, "alt_m": alt_m},
            days=days, windows=[], total_covered_min=0.0, coverage_pct=0.0,
        )

    intervals.sort(key=lambda x: x[0])

    # Сливаем пересекающиеся интервалы, сохраняя sat_ids
    merged: list[dict] = []
    cur = {"start": intervals[0][0], "end": intervals[0][1],
           "sat_ids": {intervals[0][2]}}

    for start, end, sid in intervals[1:]:
        if start <= cur["end"]:
            cur["end"] = max(cur["end"], end)
            cur["sat_ids"].add(sid)
        else:
            merged.append(cur)
            cur = {"start": start, "end": end, "sat_ids": {sid}}
    merged.append(cur)

    covered_s = sum((w["end"] - w["start"]).total_seconds() for w in merged)

    windows = [
        VisibilityWindowItem(
            start=w["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=w["end"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_min=round((w["end"] - w["start"]).total_seconds() / 60, 1),
            sats_visible=len(w["sat_ids"]),
            sat_ids=sorted(w["sat_ids"]),
        )
        for w in merged
    ]

    return VisibilityWindowsResponse(
        observer={"lat": lat, "lon": lon, "alt_m": alt_m},
        days=days,
        windows=windows,
        total_covered_min=round(covered_s / 60, 1),
        coverage_pct=round(covered_s / total_s * 100, 2),
    )


# ── пролёты спутника по всем точкам (reverse lookup) ─────────────────────────

@router.get("/{sid}/over-points",
            summary="Над какими точками пролетит спутник (reverse lookup)")
async def passes_over_points(
    sid:    int,
    points: str   = Query(...,
                          description="lat:lon пары через запятую: 55.7:37.6,48.8:2.3"),
    days:   int   = Query(1, ge=1, le=3),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
):
    """
    Для одного спутника проверяет пролёты над несколькими точками одновременно.
    Удобно для анализа покрытия маршрутов или набора наземных станций.
    """
    rec = db.get(sid)
    if not rec:
        raise HTTPException(404, f"Satellite {sid} not found")

    try:
        coords = []
        for pair in points.split(","):
            lat_s, lon_s = pair.strip().split(":")
            coords.append((float(lat_s), float(lon_s)))
    except Exception:
        raise HTTPException(400, "points format: lat:lon,lat:lon  e.g. 55.7:37.6,48.8:2.3")

    if len(coords) > 20:
        raise HTTPException(400, "Max 20 points per request")

    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=days))

    result = []
    for lat, lon in coords:
        observer = _make_observer(lat, lon)
        passes   = _find_passes_for_rec(rec, observer, t0, t1, min_el)
        result.append({
            "point":   {"lat": lat, "lon": lon},
            "passes":  len(passes),
            "next_aos": passes[0].aos if passes else None,
            "max_el":  max((p.max_el for p in passes), default=None),
        })

    return {
        "sat_id":   sid,
        "sat_name": rec["name"],
        "days":     days,
        "points":   result,
    }


# ── фильтр по элевации ────────────────────────────────────────────────────────

@router.get("/filter/by-elevation",
            summary="Пролёты с максимальной элевацией выше порога")
async def filter_by_elevation(
    lat:    float = Query(..., ge=-90,  le=90),
    lon:    float = Query(..., ge=-180, le=180),
    alt_m:  float = Query(0.0),
    days:   int   = Query(1, ge=1, le=cfg.MAX_PASS_DAYS),
    min_el: float = Query(30.0, ge=0,  le=90,
                          description="Минимальная максимальная элевация"),
    limit:  int   = Query(50,   ge=1,  le=200),
):
    records  = db.list()
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    high_el: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if not rec:
            continue
        passes = _find_passes_for_rec(rec, observer, t0, t1, min_el_deg := 0.0)
        high_el.extend(p for p in passes if p.max_el >= min_el)

    high_el.sort(key=lambda p: p.max_el, reverse=True)
    return high_el[:limit]


# ── фильтр по длительности ────────────────────────────────────────────────────

@router.get("/filter/by-duration",
            summary="Пролёты длиннее заданного порога (секунды)")
async def filter_by_duration(
    lat:       float = Query(..., ge=-90,  le=90),
    lon:       float = Query(..., ge=-180, le=180),
    alt_m:     float = Query(0.0),
    days:      int   = Query(1, ge=1, le=cfg.MAX_PASS_DAYS),
    min_dur_s: int   = Query(300, ge=1, description="Минимальная длительность (сек)"),
    min_el:    float = Query(cfg.MIN_ELEVATION_DEG),
    limit:     int   = Query(50, ge=1, le=200),
):
    records  = db.list()
    observer = _make_observer(lat, lon, alt_m)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    long_passes: list[PassEvent] = []
    for info in records:
        rec = db.get(info["id"])
        if not rec:
            continue
        passes = _find_passes_for_rec(rec, observer, t0, t1, min_el)
        long_passes.extend(p for p in passes if p.duration_s >= min_dur_s)

    long_passes.sort(key=lambda p: p.duration_s, reverse=True)
    return long_passes[:limit]