"""
core/passes_calculator.py — вся логика расчёта пролётов спутников.

Подключение в passes.py (заменить inline-реализации):
    from core.passes_calculator import (
        PassEvent,
        find_passes_for_rec,
        make_observer,
        pass_score,
        parse_dt,
        dt_diff_s,
        merge_intervals,
        passes_stats_from_list,
        build_timeline,
        build_visibility_windows,
        elevation_profile,
    )

    # Алиасы для обратной совместимости:
    _find_passes_for_rec = find_passes_for_rec
    _make_observer       = make_observer
    _pass_score          = pass_score
    _parse_dt            = parse_dt
    _dt_diff_s           = dt_diff_s
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel
from skyfield.api import EarthSatellite, Topos, load

from core.orbital import elev_az, ts

# ══════════════════════════════════════════════════════════════════════════════
#  СХЕМЫ ДАННЫХ
#  Определены здесь — passes.py импортирует их отсюда вместо inline-описания
# ══════════════════════════════════════════════════════════════════════════════

class PassEvent(BaseModel):
    sat_id:       int
    sat_name:     str
    sat_type:     str
    operator:     str
    norad_id:     Optional[int]   = None
    aos:          str             # UTC ISO — появление над горизонтом
    aos_az:       float           # азимут при появлении (°)
    max_el:       float           # максимальная элевация (°)
    max_el_time:  str
    max_el_az:    float
    los:          str             # UTC ISO — уход за горизонт
    los_az:       float
    duration_s:   int             # длительность прохода (с)


class ElevationPoint(BaseModel):
    time:      str
    elevation: float
    azimuth:   float


class PassTimelineItem(BaseModel):
    sat_id:       int
    sat_name:     str
    aos:          str
    los:          str
    max_el:       float
    duration_s:   int
    overlap_with: list[int]       # sat_id спутников с одновременным пролётом


class VisibilityWindowItem(BaseModel):
    start:        str
    end:          str
    duration_min: float
    sats_visible: int
    sat_ids:      list[int]


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def parse_dt(iso: str) -> datetime:
    """Парсит UTC ISO-строку в timezone-aware datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def dt_diff_s(a: str, b: str) -> int:
    """Разница между двумя ISO-строками в секундах (b − a)."""
    return int((parse_dt(b) - parse_dt(a)).total_seconds())


def make_observer(lat: float, lon: float, alt_m: float = 0.0) -> Topos:
    """
    Создаёт объект наблюдателя skyfield Topos.

    Args:
        lat:   широта (°)
        lon:   долгота (°)
        alt_m: высота над уровнем моря (м)
    """
    return Topos(latitude_degrees=lat, longitude_degrees=lon, elevation_m=alt_m)


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: РАСЧЁТ ПРОЛЁТОВ
# ══════════════════════════════════════════════════════════════════════════════

def find_passes_for_rec(
    rec:      dict,
    observer: Topos,
    t0,
    t1,
    min_el:   float,
) -> list[PassEvent]:
    """
    Находит все пролёты спутника из записи БД над наблюдателем.

    Args:
        rec:      запись из SatelliteDB (dict с ключами id, name, satellite, ...)
        observer: наблюдатель (skyfield Topos)
        t0:       начало интервала (skyfield Time)
        t1:       конец интервала (skyfield Time)
        min_el:   минимальная элевация для засчёта (°)

    Returns:
        Список PassEvent, отсортированных по AOS.
    """
    try:
        t_ev, evs = rec["satellite"].find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception:
        return []

    passes:  list[PassEvent] = []
    current: dict            = {}

    for ti, ev in zip(t_ev, evs):
        el, az = elev_az(rec["satellite"], observer, ti)

        if ev == 0:                           # AOS — появление над горизонтом
            current = {
                "aos":         ti.utc_iso(),
                "aos_az":      round(az, 1),
                "max_el":      0.0,
                "max_el_time": ti.utc_iso(),
                "max_el_az":   round(az, 1),
            }
        elif ev == 1 and current:             # Transit — максимальная элевация
            current["max_el"]      = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
            current["max_el_az"]   = round(az, 1)
        elif ev == 2 and current:             # LOS — уход за горизонт
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
                duration_s=dt_diff_s(current["aos"], ti.utc_iso()),
            ))
            current = {}

    return passes


def find_next_pass(
    rec:         dict,
    observer:    Topos,
    min_el:      float = 5.0,
    search_days: int   = 3,
) -> Optional[PassEvent]:
    """
    Находит только ближайший пролёт. Быстрее чем find_passes_for_rec за весь период.

    Returns:
        Первый PassEvent или None если пролётов нет.
    """
    t0 = ts.now()
    t1 = ts.utc(t0.utc_datetime() + timedelta(days=search_days))
    passes = find_passes_for_rec(rec, observer, t0, t1, min_el)
    return passes[0] if passes else None


def find_passes_multi(
    records:  list[dict],
    observer: Topos,
    t0,
    t1,
    min_el:   float,
    limit:    int = 1000,
) -> list[PassEvent]:
    """
    Пролёты для списка спутников над одним наблюдателем.
    Возвращает объединённый отсортированный список.

    Args:
        records: список записей из SatelliteDB
        limit:   максимальное число возвращаемых пролётов
    """
    all_passes: list[PassEvent] = []
    for rec in records:
        all_passes.extend(find_passes_for_rec(rec, observer, t0, t1, min_el))
    all_passes.sort(key=lambda p: p.aos)
    return all_passes[:limit]


# ══════════════════════════════════════════════════════════════════════════════
#  ПРОФИЛЬ ЭЛЕВАЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def elevation_profile(
    sat:           EarthSatellite,
    observer:      Topos,
    pass_event:    PassEvent,
    steps:         int = 60,
) -> list[ElevationPoint]:
    """
    Строит кривую элевация/азимут для конкретного пролёта.
    Используется для отрисовки полярной диаграммы на фронтенде.

    Args:
        sat:        объект EarthSatellite
        observer:   наблюдатель
        pass_event: событие пролёта (содержит AOS/LOS)
        steps:      количество точек (10..300)

    Returns:
        Список ElevationPoint от AOS до LOS.
    """
    aos_dt  = parse_dt(pass_event.aos)
    los_dt  = parse_dt(pass_event.los)
    total_s = (los_dt - aos_dt).total_seconds()

    if total_s <= 0 or steps < 2:
        return []

    step_s = total_s / (steps - 1)
    profile: list[ElevationPoint] = []

    for i in range(steps):
        pt_dt = aos_dt + timedelta(seconds=step_s * i)
        pt_t  = ts.utc(pt_dt.year, pt_dt.month, pt_dt.day,
                       pt_dt.hour, pt_dt.minute,
                       pt_dt.second + pt_dt.microsecond / 1_000_000)
        el, az = elev_az(sat, observer, pt_t)
        profile.append(ElevationPoint(
            time=pt_t.utc_iso(),
            elevation=round(el, 2),
            azimuth=round(az, 2),
        ))

    return profile


# ══════════════════════════════════════════════════════════════════════════════
#  ОЦЕНКА КАЧЕСТВА ПРОЛЁТА
# ══════════════════════════════════════════════════════════════════════════════

def pass_score(p: PassEvent) -> float:
    """
    Взвешенная оценка качества пролёта [0..100].

    Веса:
        60% — максимальная элевация (нормировано к 90°)
        40% — длительность        (нормировано к 600 с = 10 мин)

    Высокий балл = спутник высоко над горизонтом и долго виден.
    """
    el_score  = min(p.max_el / 90.0, 1.0) * 60
    dur_score = min(p.duration_s / 600.0, 1.0) * 40
    return round(el_score + dur_score, 2)


def best_passes(
    passes: list[PassEvent],
    top_n:  int = 20,
) -> list[PassEvent]:
    """Возвращает top_n лучших пролётов по pass_score."""
    return sorted(passes, key=pass_score, reverse=True)[:top_n]


# ══════════════════════════════════════════════════════════════════════════════
#  ТАЙМЛАЙН И OVERLAP
# ══════════════════════════════════════════════════════════════════════════════

def build_timeline(
    passes: list[PassEvent],
) -> tuple[list[PassTimelineItem], int]:
    """
    Строит хронологический таймлайн пролётов с поиском одновременных (overlap).

    Args:
        passes: список PassEvent (будет отсортирован по AOS)

    Returns:
        (items, max_overlap)
        items       — список PassTimelineItem с полем overlap_with
        max_overlap — максимальное число одновременно видимых спутников
    """
    passes = sorted(passes, key=lambda p: p.aos)
    items: list[PassTimelineItem] = []
    max_overlap = 0

    for i, p in enumerate(passes):
        overlaps = [
            other.sat_id
            for j, other in enumerate(passes)
            if i != j and p.aos < other.los and p.los > other.aos
        ]
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

    return items, max_overlap


# ══════════════════════════════════════════════════════════════════════════════
#  СЛИЯНИЕ ИНТЕРВАЛОВ / ОКНА ВИДИМОСТИ
# ══════════════════════════════════════════════════════════════════════════════

def merge_intervals(
    intervals: list[tuple[datetime, datetime, int]],
) -> list[dict]:
    """
    Сливает пересекающиеся временны́е интервалы, сохраняя sat_ids.

    Args:
        intervals: список (start_dt, end_dt, sat_id)

    Returns:
        Список словарей {start, end, sat_ids: set}
    """
    if not intervals:
        return []

    intervals = sorted(intervals, key=lambda x: x[0])
    merged: list[dict] = [
        {"start": intervals[0][0], "end": intervals[0][1],
         "sat_ids": {intervals[0][2]}}
    ]

    for start, end, sid in intervals[1:]:
        cur = merged[-1]
        if start <= cur["end"]:
            cur["end"] = max(cur["end"], end)
            cur["sat_ids"].add(sid)
        else:
            merged.append({"start": start, "end": end, "sat_ids": {sid}})

    return merged


def build_visibility_windows(
    passes:   list[PassEvent],
    t0_dt:    datetime,
    t1_dt:    datetime,
) -> tuple[list[VisibilityWindowItem], float, float]:
    """
    Строит непрерывные окна видимости из списка пролётов.

    Args:
        passes:  список PassEvent
        t0_dt:   начало наблюдаемого периода (timezone-aware)
        t1_dt:   конец наблюдаемого периода  (timezone-aware)

    Returns:
        (windows, total_covered_min, coverage_pct)
        windows           — список VisibilityWindowItem
        total_covered_min — суммарное время покрытия (мин)
        coverage_pct      — процент времени с хотя бы одним спутником
    """
    if not passes:
        return [], 0.0, 0.0

    intervals = [
        (parse_dt(p.aos), parse_dt(p.los), p.sat_id)
        for p in passes
    ]
    merged    = merge_intervals(intervals)
    total_s   = (t1_dt - t0_dt).total_seconds()
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

    return windows, round(covered_s / 60, 1), round(covered_s / total_s * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════════════

def passes_stats_from_list(
    passes: list[PassEvent],
    days:   int,
    lat:    float,
    lon:    float,
    alt_m:  float = 0.0,
) -> dict:
    """
    Считает статистику по списку пролётов.

    Returns:
        Словарь совместимый с PassStatsResponse из passes.py.
    """
    if not passes:
        return {
            "observer":          {"lat": lat, "lon": lon, "alt_m": alt_m},
            "days":              days,
            "total_passes":      0,
            "total_covered_min": 0.0,
            "avg_duration_s":    None,
            "avg_max_el":        None,
            "max_el_ever":       None,
            "passes_per_day":    0.0,
            "by_sat_type":       {},
            "by_hour_utc":       {},
        }

    durations = [p.duration_s for p in passes]
    elevs     = [p.max_el     for p in passes]

    by_type: dict[str, int] = defaultdict(int)
    by_hour: dict[int, int] = defaultdict(int)
    for p in passes:
        by_type[p.sat_type] += 1
        try:
            by_hour[parse_dt(p.aos).hour] += 1
        except Exception:
            pass

    return {
        "observer":          {"lat": lat, "lon": lon, "alt_m": alt_m},
        "days":              days,
        "total_passes":      len(passes),
        "total_covered_min": round(sum(durations) / 60, 1),
        "avg_duration_s":    round(statistics.mean(durations), 1),
        "avg_max_el":        round(statistics.mean(elevs), 1),
        "max_el_ever":       round(max(elevs), 1),
        "passes_per_day":    round(len(passes) / days, 2),
        "by_sat_type":       dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "by_hour_utc":       {h: by_hour[h] for h in sorted(by_hour)},
    }


# ══════════════════════════════════════════════════════════════════════════════
#  COVERAGE GAPS (используется в groups.py)
# ══════════════════════════════════════════════════════════════════════════════

def coverage_gaps_from_passes(
    passes:   list[PassEvent],
    t0_dt:    datetime,
    t1_dt:    datetime,
) -> tuple[list[dict], float, float]:
    """
    Находит промежутки (gaps) когда ни один спутник не виден.

    Args:
        passes:  список PassEvent
        t0_dt:   начало периода (timezone-aware)
        t1_dt:   конец периода  (timezone-aware)

    Returns:
        (gaps, covered_min, coverage_pct)
        gaps — список {start, end, gap_min}
    """
    if not passes:
        gap_min = (t1_dt - t0_dt).total_seconds() / 60
        return (
            [{"start": t0_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end":   t1_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "gap_min": round(gap_min, 1)}],
            0.0, 0.0,
        )

    intervals = [(parse_dt(p.aos), parse_dt(p.los), p.sat_id) for p in passes]
    merged    = merge_intervals(intervals)
    total_s   = (t1_dt - t0_dt).total_seconds()
    covered_s = sum((w["end"] - w["start"]).total_seconds() for w in merged)

    gaps: list[dict] = []
    prev_end = t0_dt

    for w in merged:
        if w["start"] > prev_end:
            gap_s = (w["start"] - prev_end).total_seconds()
            gaps.append({
                "start":   prev_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":     w["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gap_min": round(gap_s / 60, 1),
            })
        prev_end = w["end"]

    if prev_end < t1_dt:
        gap_s = (t1_dt - prev_end).total_seconds()
        gaps.append({
            "start":   prev_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":     t1_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gap_min": round(gap_s / 60, 1),
        })

    return gaps, round(covered_s / 60, 1), round(covered_s / total_s * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАТНАЯ СОВМЕСТИМОСТЬ — алиасы для passes.py
# ══════════════════════════════════════════════════════════════════════════════

# В passes.py заменить inline-функции на:
#   from core.passes_calculator import (
#       PassEvent,
#       find_passes_for_rec  as _find_passes_for_rec,
#       make_observer        as _make_observer,
#       pass_score           as _pass_score,
#       parse_dt             as _parse_dt,
#       dt_diff_s            as _dt_diff_s,
#   )

_find_passes_for_rec = find_passes_for_rec
_make_observer       = make_observer
_pass_score          = pass_score
_parse_dt            = parse_dt
_dt_diff_s           = dt_diff_s