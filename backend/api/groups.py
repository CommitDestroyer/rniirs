"""
groups.py — APIRouter для анализа и сравнения группировок спутников.

Подключение в main.py:
    from groups import router as groups_router
    app.include_router(groups_router, prefix="/groups", tags=["groups"])
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
    SatelliteDB,
    _coverage_polygon,
    _elev_az,
    _orbit_type_from_meta,
    _parse_tle_meta,
    _sat_position,
    _parse_time,
    db,
    ts,
)

cfg    = get_settings()
router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class GroupSummary(BaseModel):
    group_by:    str                    # "type" | "operator" | "custom"
    key:         str                    # значение группировки
    count:       int
    norad_ids:   list[int]
    sat_ids:     list[int]


class GroupStats(BaseModel):
    key:             str
    count:           int
    orbit_types:     dict[str, int]     # LEO: 40, MEO: 5 ...
    avg_alt_km:      Optional[float]
    avg_period_min:  Optional[float]
    min_period_min:  Optional[float]
    max_period_min:  Optional[float]
    avg_inclination: Optional[float]
    eccentricity_avg: Optional[float]
    operators:       list[str]


class CompareResult(BaseModel):
    groups:      list[GroupStats]
    diff:        Optional[dict]         # разница между двумя группами (если запрошено 2)


class CoverageUnionResponse(BaseModel):
    group_key:   str
    count:       int
    polygons:    list[dict]             # [{id, name, center, polygon, radius_km}]
    timestamp:   str


class PassOverlapItem(BaseModel):
    sat_id:      int
    sat_name:    str
    aos:         str
    los:         str
    max_el:      float
    duration_s:  int


class GroupPassesResponse(BaseModel):
    group_key:   str
    observer:    dict
    passes:      list[PassOverlapItem]


class ConstellationGapResponse(BaseModel):
    group_key:     str
    observer:      dict
    days:          int
    total_passes:  int
    covered_min:   float                # суммарное покрытие в минутах
    gap_periods:   list[dict]           # [{start, end, gap_min}]
    coverage_pct:  float                # процент времени покрытия


class SatBriefInGroup(BaseModel):
    id:          int
    name:        str
    norad_id:    Optional[int]
    type:        str
    operator:    str
    period_min:  Optional[float]
    apogee_km:   Optional[float]
    inclination_deg: Optional[float]


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _group_records(by: str, key: Optional[str] = None) -> dict[str, list[dict]]:
    """
    Группирует записи из DB по полю `by` (type | operator).
    Если key задан — возвращает только эту группу.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in db.all_records():
        val = rec.get(by, "Unknown") or "Unknown"
        groups[val].append(rec)

    if key:
        matched = {k: v for k, v in groups.items() if k.lower() == key.lower()}
        if not matched:
            raise HTTPException(404, f"Group '{key}' not found for field '{by}'")
        return matched
    return dict(groups)


def _compute_stats(key: str, records: list[dict]) -> GroupStats:
    """Вычисляет статистику по списку записей спутников."""
    orbit_types: dict[str, int] = defaultdict(int)
    alts, periods, incls, eccs = [], [], [], []
    operators: set[str] = set()

    for rec in records:
        orbit_types[rec.get("type", "Unknown")] += 1
        operators.add(rec.get("operator", "Unknown"))

        if rec.get("apogee_km") is not None:
            alts.append(rec["apogee_km"])
        if rec.get("period_min") is not None:
            periods.append(rec["period_min"])
        if rec.get("inclination_deg") is not None:
            incls.append(rec["inclination_deg"])
        if rec.get("eccentricity") is not None:
            eccs.append(rec["eccentricity"])

    def _avg(lst: list) -> Optional[float]:
        return round(statistics.mean(lst), 3) if lst else None

    return GroupStats(
        key=key,
        count=len(records),
        orbit_types=dict(orbit_types),
        avg_alt_km=_avg(alts),
        avg_period_min=_avg(periods),
        min_period_min=round(min(periods), 3) if periods else None,
        max_period_min=round(max(periods), 3) if periods else None,
        avg_inclination=_avg(incls),
        eccentricity_avg=_avg(eccs),
        operators=sorted(operators),
    )


def _stats_diff(a: GroupStats, b: GroupStats) -> dict:
    """Считает разницу числовых полей между двумя группами."""
    fields = ("avg_alt_km", "avg_period_min", "avg_inclination",
              "eccentricity_avg", "count")
    result = {}
    for f in fields:
        va, vb = getattr(a, f), getattr(b, f)
        if va is not None and vb is not None:
            result[f] = round(vb - va, 3)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── list groups ───────────────────────────────────────────────────────────────

@router.get("", response_model=list[GroupSummary],
            summary="Список всех группировок")
async def list_groups(
    by: str = Query("type", regex="^(type|operator)$",
                    description="Поле группировки: type | operator"),
):
    groups = _group_records(by)
    result = []
    for key, recs in sorted(groups.items()):
        result.append(GroupSummary(
            group_by=by,
            key=key,
            count=len(recs),
            sat_ids=[r["id"] for r in recs],
            norad_ids=[r["norad_id"] for r in recs if r.get("norad_id")],
        ))
    return result


# ── group detail ──────────────────────────────────────────────────────────────

@router.get("/{key}/satellites", response_model=list[SatBriefInGroup],
            summary="Список спутников в группе")
async def group_satellites(
    key:    str,
    by:     str = Query("type", regex="^(type|operator)$"),
    limit:  int = Query(200, ge=1, le=500),
    offset: int = Query(0,   ge=0),
    sort_by: str = Query("id", regex="^(id|name|period_min|apogee_km|inclination_deg)$"),
    desc:   bool = Query(False),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]
    recs.sort(key=lambda r: r.get(sort_by, 0) or 0, reverse=desc)

    return [
        SatBriefInGroup(
            id=r["id"], name=r["name"],
            norad_id=r.get("norad_id"),
            type=r.get("type", "Unknown"),
            operator=r.get("operator", "Unknown"),
            period_min=r.get("period_min"),
            apogee_km=r.get("apogee_km"),
            inclination_deg=r.get("inclination_deg"),
        )
        for r in recs[offset: offset + limit]
    ]


@router.get("/{key}/stats", response_model=GroupStats,
            summary="Орбитальная статистика группы")
async def group_stats(
    key: str,
    by:  str = Query("type", regex="^(type|operator)$"),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]
    return _compute_stats(key, recs)


# ── compare ───────────────────────────────────────────────────────────────────

@router.get("/compare", response_model=CompareResult,
            summary="Сравнение двух или более группировок")
async def compare_groups(
    keys: str = Query(..., description="Ключи через запятую, напр. LEO,MEO"),
    by:   str = Query("type", regex="^(type|operator)$"),
):
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    if len(key_list) < 2:
        raise HTTPException(400, "Provide at least 2 keys to compare")
    if len(key_list) > 10:
        raise HTTPException(400, "Max 10 groups per comparison")

    stats_list: list[GroupStats] = []
    for k in key_list:
        try:
            groups = _group_records(by, k)
            recs   = list(groups.values())[0]
            stats_list.append(_compute_stats(k, recs))
        except HTTPException:
            continue

    if len(stats_list) < 2:
        raise HTTPException(404, "Not enough valid groups found")

    diff = _stats_diff(stats_list[0], stats_list[1]) if len(stats_list) == 2 else None
    return CompareResult(groups=stats_list, diff=diff)


# ── full compare (all groups) ─────────────────────────────────────────────────

@router.get("/compare/all", response_model=list[GroupStats],
            summary="Статистика по всем группировкам сразу")
async def compare_all(
    by: str = Query("type", regex="^(type|operator)$"),
):
    groups = _group_records(by)
    return [_compute_stats(k, v) for k, v in sorted(groups.items())]


# ── positions ─────────────────────────────────────────────────────────────────

@router.get("/{key}/positions",
            summary="Текущие позиции всех спутников группы")
async def group_positions(
    key:   str,
    by:    str           = Query("type", regex="^(type|operator)$"),
    at:    Optional[str] = Query(None),
    limit: int           = Query(100, ge=1, le=cfg.BULK_POSITION_LIMIT),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0][:limit]
    t      = _parse_time(at)

    result = []
    for rec in recs:
        pos = _sat_position(rec["satellite"], t)
        pos.update({"id": rec["id"], "name": rec["name"],
                    "type": rec.get("type"), "operator": rec.get("operator")})
        result.append(pos)
    return result


# ── coverage union ────────────────────────────────────────────────────────────

@router.get("/{key}/coverage", response_model=CoverageUnionResponse,
            summary="Зоны радиовидимости всех спутников группы")
async def group_coverage(
    key:    str,
    by:     str           = Query("type", regex="^(type|operator)$"),
    min_el: float         = Query(0.0, ge=0, le=45),
    at:     Optional[str] = Query(None),
    limit:  int           = Query(50, ge=1, le=200),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0][:limit]
    t      = _parse_time(at)

    polygons = []
    for rec in recs:
        pos = _sat_position(rec["satellite"], t)
        polygon, radius_km = _coverage_polygon(
            pos["lat"], pos["lon"], pos["alt_km"], min_el
        )
        polygons.append({
            "id":         rec["id"],
            "name":       rec["name"],
            "center":     [pos["lon"], pos["lat"]],
            "radius_km":  radius_km,
            "polygon":    polygon,
        })

    return CoverageUnionResponse(
        group_key=key,
        count=len(polygons),
        polygons=polygons,
        timestamp=t.utc_iso(),
    )


# ── passes over point ─────────────────────────────────────────────────────────

@router.get("/{key}/passes", response_model=GroupPassesResponse,
            summary="Пролёты всех спутников группы над заданной точкой")
async def group_passes(
    key:    str,
    lat:    float = Query(..., ge=-90,  le=90),
    lon:    float = Query(..., ge=-180, le=180),
    days:   int   = Query(1,   ge=1,   le=cfg.MAX_PASS_DAYS),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
    by:     str   = Query("type", regex="^(type|operator)$"),
    limit:  int   = Query(50,  ge=1, le=200),
):
    groups   = _group_records(by, key)
    recs     = list(groups.values())[0]
    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))

    all_passes: list[PassOverlapItem] = []

    for rec in recs[:limit]:
        try:
            t_ev, evs = rec["satellite"].find_events(
                observer, t0, t1, altitude_degrees=min_el
            )
        except Exception:
            continue

        current: dict = {}
        for ti, ev in zip(t_ev, evs):
            el, az = _elev_az(rec["satellite"], observer, ti)
            if ev == 0:
                current = {"aos": ti.utc_iso(), "max_el": 0.0}
            elif ev == 1 and current:
                current["max_el"] = round(el, 1)
            elif ev == 2 and current:
                aos_dt = datetime.fromisoformat(current["aos"].replace("Z", "+00:00"))
                los_dt = datetime.fromisoformat(ti.utc_iso().replace("Z", "+00:00"))
                all_passes.append(PassOverlapItem(
                    sat_id=rec["id"],
                    sat_name=rec["name"],
                    aos=current["aos"],
                    los=ti.utc_iso(),
                    max_el=current["max_el"],
                    duration_s=int((los_dt - aos_dt).total_seconds()),
                ))
                current = {}

    all_passes.sort(key=lambda p: p.aos)
    return GroupPassesResponse(
        group_key=key,
        observer={"lat": lat, "lon": lon},
        passes=all_passes,
    )


# ── coverage gap analysis ─────────────────────────────────────────────────────

@router.get("/{key}/coverage-gaps", response_model=ConstellationGapResponse,
            summary="Анализ непрерывности покрытия группировки над точкой")
async def coverage_gaps(
    key:    str,
    lat:    float = Query(..., ge=-90,  le=90),
    lon:    float = Query(..., ge=-180, le=180),
    days:   int   = Query(1,  ge=1,   le=3),
    min_el: float = Query(cfg.MIN_ELEVATION_DEG),
    by:     str   = Query("type", regex="^(type|operator)$"),
    limit:  int   = Query(100, ge=1, le=300),
):
    """
    Строит таймлайн покрытия точки группировкой.
    Находит промежутки (gaps) когда ни один спутник не виден.
    """
    groups   = _group_records(by, key)
    recs     = list(groups.values())[0]
    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    t0       = ts.now()
    t1       = ts.utc(t0.utc_datetime() + timedelta(days=days))
    t0_dt    = t0.utc_datetime().replace(tzinfo=timezone.utc)
    t1_dt    = t1.utc_datetime().replace(tzinfo=timezone.utc)
    total_s  = (t1_dt - t0_dt).total_seconds()

    # Собираем все интервалы [aos_dt, los_dt]
    intervals: list[tuple[datetime, datetime]] = []

    for rec in recs[:limit]:
        try:
            t_ev, evs = rec["satellite"].find_events(
                observer, t0, t1, altitude_degrees=min_el
            )
        except Exception:
            continue
        aos_dt: Optional[datetime] = None
        for ti, ev in zip(t_ev, evs):
            if ev == 0:
                aos_dt = datetime.fromisoformat(ti.utc_iso().replace("Z", "+00:00"))
            elif ev == 2 and aos_dt:
                los_dt = datetime.fromisoformat(ti.utc_iso().replace("Z", "+00:00"))
                intervals.append((aos_dt, los_dt))
                aos_dt = None

    if not intervals:
        return ConstellationGapResponse(
            group_key=key,
            observer={"lat": lat, "lon": lon},
            days=days,
            total_passes=0,
            covered_min=0.0,
            gap_periods=[{"start": t0_dt.isoformat(), "end": t1_dt.isoformat(),
                          "gap_min": round(total_s / 60, 1)}],
            coverage_pct=0.0,
        )

    # Сливаем пересекающиеся интервалы
    intervals.sort(key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = [intervals[0]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Считаем покрытие и gaps
    covered_s = sum((e - s).total_seconds() for s, e in merged)
    gaps: list[dict] = []

    prev_end = t0_dt
    for start, end in merged:
        if start > prev_end:
            gap_s = (start - prev_end).total_seconds()
            gaps.append({
                "start":   prev_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gap_min": round(gap_s / 60, 1),
            })
        prev_end = end

    if prev_end < t1_dt:
        gap_s = (t1_dt - prev_end).total_seconds()
        gaps.append({
            "start":   prev_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":     t1_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gap_min": round(gap_s / 60, 1),
        })

    return ConstellationGapResponse(
        group_key=key,
        observer={"lat": lat, "lon": lon},
        days=days,
        total_passes=len(intervals),
        covered_min=round(covered_s / 60, 1),
        gap_periods=gaps,
        coverage_pct=round(covered_s / total_s * 100, 2),
    )


# ── orbit distribution ────────────────────────────────────────────────────────

@router.get("/{key}/orbit-distribution",
            summary="Распределение по высотам орбит внутри группы")
async def orbit_distribution(
    key:    str,
    by:     str = Query("operator", regex="^(type|operator)$"),
    bins:   int = Query(10, ge=2, le=50, description="Количество бинов гистограммы"),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]

    alts = [r["apogee_km"] for r in recs if r.get("apogee_km") is not None]
    if not alts:
        raise HTTPException(404, "No altitude data available for this group")

    min_alt, max_alt = min(alts), max(alts)
    if min_alt == max_alt:
        return {"key": key, "bins": [{"range": [min_alt, max_alt], "count": len(alts)}]}

    step = (max_alt - min_alt) / bins
    histogram = []
    for i in range(bins):
        lo = min_alt + i * step
        hi = lo + step
        count = sum(1 for a in alts if lo <= a < hi)
        histogram.append({
            "range": [round(lo, 1), round(hi, 1)],
            "count": count,
        })
    # последний бин включает максимум
    histogram[-1]["count"] += sum(1 for a in alts if a == max_alt)

    return {
        "key":    key,
        "count":  len(alts),
        "min_km": round(min_alt, 1),
        "max_km": round(max_alt, 1),
        "avg_km": round(statistics.mean(alts), 1),
        "bins":   histogram,
    }


# ── inclination distribution ──────────────────────────────────────────────────

@router.get("/{key}/inclination-distribution",
            summary="Распределение по наклонению орбит внутри группы")
async def inclination_distribution(
    key: str,
    by:  str = Query("operator", regex="^(type|operator)$"),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]

    incls = [r["inclination_deg"] for r in recs if r.get("inclination_deg") is not None]
    if not incls:
        raise HTTPException(404, "No inclination data available")

    # Бины по 10 градусов: 0-10, 10-20 ... 80-90, 90-180 (ретроградные)
    bins_def = [(i * 10, (i + 1) * 10) for i in range(18)]
    histogram = []
    for lo, hi in bins_def:
        count = sum(1 for inc in incls if lo <= inc < hi)
        if count:
            histogram.append({"range": [lo, hi], "count": count})

    return {
        "key":          key,
        "count":        len(incls),
        "avg_deg":      round(statistics.mean(incls), 2),
        "median_deg":   round(statistics.median(incls), 2),
        "histogram":    histogram,
    }


# ── top satellites in group ───────────────────────────────────────────────────

@router.get("/{key}/top", summary="Топ спутников группы по заданному параметру")
async def group_top(
    key:    str,
    by:     str = Query("type",     regex="^(type|operator)$"),
    metric: str = Query("apogee_km", regex="^(apogee_km|period_min|inclination_deg|eccentricity)$"),
    n:      int = Query(10, ge=1, le=100),
    desc:   bool= Query(True),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]

    ranked = [r for r in recs if r.get(metric) is not None]
    ranked.sort(key=lambda r: r[metric], reverse=desc)

    return [
        {
            "id":      r["id"],
            "name":    r["name"],
            "norad_id": r.get("norad_id"),
            metric:    r[metric],
        }
        for r in ranked[:n]
    ]


# ── operator breakdown inside orbit type ─────────────────────────────────────

@router.get("/{key}/operators",
            summary="Разбивка по операторам внутри группы (тип орбиты)")
async def group_operators(
    key: str,
    by:  str = Query("type", regex="^(type|operator)$"),
):
    groups = _group_records(by, key)
    recs   = list(groups.values())[0]

    op_count: dict[str, int] = defaultdict(int)
    for rec in recs:
        op_count[rec.get("operator", "Unknown")] += 1

    ranked = sorted(op_count.items(), key=lambda x: -x[1])
    return {
        "key":   key,
        "total": len(recs),
        "operators": [{"operator": op, "count": cnt} for op, cnt in ranked],
    }