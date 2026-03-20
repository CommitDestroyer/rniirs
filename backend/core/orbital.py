"""
core/orbital.py — вся орбитальная математика проекта.

Подключение в satellites.py (заменить inline-реализации):
    from core.orbital import (
        parse_tle_meta,
        orbit_type_from_meta,
        sat_position,
        elev_az,
        parse_time,
        classify_tle,
        orbital_period_min,
        semi_major_axis_km,
        orbital_velocity_km_s,
        ground_track,
    )

    # Алиасы для обратной совместимости — менять в satellites.py НЕ нужно:
    _parse_tle_meta      = parse_tle_meta
    _orbit_type_from_meta = orbit_type_from_meta
    _sat_position        = sat_position
    _elev_az             = elev_az
    _parse_time          = parse_time
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from skyfield.api import EarthSatellite, Topos, load

# ══════════════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ
# ══════════════════════════════════════════════════════════════════════════════

_RE_KM    = 6371.0           # средний радиус Земли (км)
_MU_KM3   = 398_600.4418     # гравитационный параметр Земли (км³/с²)
_J2       = 1.08263e-3       # коэффициент сжатия Земли (для прецессии)
_SIDEREAL = 86164.1          # звёздные сутки (с)

# Skyfield timescale — один экземпляр на весь модуль
ts = load.timescale()

# ══════════════════════════════════════════════════════════════════════════════
#  ПАРСИНГ TLE
# ══════════════════════════════════════════════════════════════════════════════

def parse_tle_meta(l1: str, l2: str) -> dict:
    """
    Извлекает все числовые параметры из пары TLE-строк.

    Returns:
        Словарь с ключами:
            norad_id, epoch, inclination_deg, raan_deg, eccentricity,
            arg_perigee_deg, mean_anomaly_deg, mean_motion, period_min,
            apogee_km, perigee_km, semi_major_axis_km, drag_term
        Пустой словарь {} если парсинг не удался.
    """
    try:
        # ── Line 1 ──────────────────────────────────────────────────────────
        norad_id     = int(l1[2:7])
        classification = l1[7].strip() or "U"
        intl_desig   = l1[9:17].strip()
        epoch_year_2 = int(l1[18:20])
        epoch_day    = float(l1[20:32])
        # B* drag term формат: "NNNNN-N" → 0.NNNNN × 10^-N
        try:
            _bstar = l1[53:61].strip()
            if _bstar and _bstar not in ("00000-0", "00000+0"):
                # Разбиваем на мантиссу и экспоненту: "30270-3" → 0.30270e-3
                _sep = max(_bstar.rfind("-"), _bstar.rfind("+"))
                if _sep > 0:
                    _mant = float("0." + _bstar[:_sep].lstrip("+-"))
                    _exp  = int(_bstar[_sep:])
                    _sign = -1 if _bstar[0] == "-" else 1
                    drag_term = _sign * _mant * (10 ** _exp)
                else:
                    drag_term = 0.0
            else:
                drag_term = 0.0
        except Exception:
            drag_term = 0.0

        year = (2000 + epoch_year_2) if epoch_year_2 < 57 else (1900 + epoch_year_2)
        epoch_dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=epoch_day - 1)

        # ── Line 2 ──────────────────────────────────────────────────────────
        inclination    = float(l2[8:16])           # градусы
        raan           = float(l2[17:25])           # прямое восхождение восх. узла
        eccentricity   = float("0." + l2[26:33])   # без десятичной точки в TLE
        arg_perigee    = float(l2[34:42])           # аргумент перигея
        mean_anomaly   = float(l2[43:51])           # средняя аномалия
        mean_motion    = float(l2[52:63])           # об/сутки
        rev_number     = int(l2[63:68].strip() or 0)

        # ── Производные ─────────────────────────────────────────────────────
        period_min = 1440.0 / mean_motion

        # Большая полуось (3-й закон Кеплера)
        a_km = (_MU_KM3 * (period_min * 60 / (2 * math.pi)) ** 2) ** (1 / 3)

        apogee_km  = round(a_km * (1 + eccentricity) - _RE_KM, 1)
        perigee_km = round(a_km * (1 - eccentricity) - _RE_KM, 1)

        return {
            "norad_id":          norad_id,
            "classification":    classification,
            "intl_designator":   intl_desig,
            "epoch":             epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "epoch_dt":          epoch_dt,
            "inclination_deg":   round(inclination, 4),
            "raan_deg":          round(raan, 4),
            "eccentricity":      round(eccentricity, 7),
            "arg_perigee_deg":   round(arg_perigee, 4),
            "mean_anomaly_deg":  round(mean_anomaly, 4),
            "mean_motion":       round(mean_motion, 8),   # об/сутки
            "rev_number":        rev_number,
            "drag_term":         drag_term,
            "period_min":        round(period_min, 4),
            "semi_major_axis_km": round(a_km, 2),
            "apogee_km":         apogee_km,
            "perigee_km":        perigee_km,
        }
    except Exception:
        return {}


def orbit_type_from_meta(meta: dict) -> str:
    """
    Определяет тип орбиты по метаданным TLE.

    Классификация по высоте апогея:
        LEO  — до 2 000 км
        MEO  — 2 000 — 35 786 км
        GEO  — ~35 786 км (геостационарная)
        HEO  — выше GEO или высокоэллиптическая
    """
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


def classify_tle(l1: str, l2: str) -> str:
    """Быстрое определение типа орбиты по TLE-строкам."""
    return orbit_type_from_meta(parse_tle_meta(l1, l2))


# ══════════════════════════════════════════════════════════════════════════════
#  ПОЗИЦИЯ И СКОРОСТЬ
# ══════════════════════════════════════════════════════════════════════════════

def sat_position(sat: EarthSatellite, t) -> dict:
    """
    Геодезические координаты спутника + модуль вектора скорости.

    Args:
        sat: объект EarthSatellite из skyfield
        t:   момент времени (skyfield Time)

    Returns:
        {lat, lon, alt_km, velocity_km_s, timestamp}
    """
    geo = sat.at(t)
    sub = geo.subpoint()
    vel = round(float(np.linalg.norm(geo.velocity.km_per_s)), 3)
    return {
        "lat":           round(sub.latitude.degrees, 5),
        "lon":           round(sub.longitude.degrees, 5),
        "alt_km":        round(sub.elevation.km, 2),
        "velocity_km_s": vel,
        "timestamp":     t.utc_iso(),
    }


def elev_az(sat: EarthSatellite, observer: Topos, t) -> tuple[float, float]:
    """
    Топоцентрическая элевация и азимут спутника над наблюдателем.

    Returns:
        (elevation_deg, azimuth_deg)
        elevation — угол над горизонтом (−90..90)
        azimuth   — азимут от севера по часовой (0..360)
    """
    alt, az, _ = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees


def elev_az_range(sat: EarthSatellite, observer: Topos, t) -> tuple[float, float, float]:
    """
    Топоцентрические элевация, азимут и наклонная дальность.

    Returns:
        (elevation_deg, azimuth_deg, range_km)
    """
    alt, az, dist = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees, dist.km


def parse_time(at: Optional[str]):
    """
    Парсит UTC ISO-строку в skyfield Time.
    Если at=None — возвращает ts.now().

    Принимает форматы:
        "2025-04-01T12:00:00Z"
        "2025-04-01T12:00:00+00:00"
        "2025-04-01T12:00:00"
    """
    if not at:
        return ts.now()
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return ts.utc(dt.year, dt.month, dt.day,
                  dt.hour, dt.minute,
                  dt.second + dt.microsecond / 1_000_000)


# ══════════════════════════════════════════════════════════════════════════════
#  ОРБИТАЛЬНАЯ МЕХАНИКА
# ══════════════════════════════════════════════════════════════════════════════

def orbital_period_min(mean_motion_rev_per_day: float) -> float:
    """Орбитальный период (минуты) по среднему движению (об/сутки)."""
    return round(1440.0 / mean_motion_rev_per_day, 4)


def semi_major_axis_km(period_min: float) -> float:
    """
    Большая полуось эллипса (км) по орбитальному периоду.
    3-й закон Кеплера: a³ = μ · (T/2π)²
    """
    T_sec = period_min * 60
    a = (_MU_KM3 * (T_sec / (2 * math.pi)) ** 2) ** (1 / 3)
    return round(a, 3)


def orbital_velocity_km_s(alt_km: float) -> float:
    """
    Круговая орбитальная скорость на высоте alt_km (км/с).
    v = sqrt(μ / (Re + h))
    """
    r = _RE_KM + alt_km
    return round(math.sqrt(_MU_KM3 / r), 4)


def escape_velocity_km_s(alt_km: float) -> float:
    """
    Вторая космическая скорость на высоте alt_km (км/с).
    v_esc = sqrt(2μ / r)
    """
    r = _RE_KM + alt_km
    return round(math.sqrt(2 * _MU_KM3 / r), 4)


def vis_viva_km_s(r_km: float, a_km: float) -> float:
    """
    Скорость в произвольной точке эллиптической орбиты (уравнение Виса-Виво).

    Args:
        r_km: текущее расстояние от центра Земли (км)
        a_km: большая полуось (км)

    Returns:
        Скорость (км/с)
    """
    return round(math.sqrt(_MU_KM3 * (2 / r_km - 1 / a_km)), 4)


def raan_precession_deg_per_day(a_km: float, inclination_deg: float,
                                 eccentricity: float) -> float:
    """
    Скорость прецессии прямого восхождения восходящего узла (RAAN),
    градусы/сутки. Вызвана J2-членом геопотенциала.

    Формула:
        dΩ/dt = −(3/2) · n · J2 · (Re/p)² · cos(i)
    где p = a(1−e²) — фокальный параметр.
    """
    n = 2 * math.pi / (orbital_period_min(1440 / orbital_period_min(
        orbital_period_min(1440.0 / (math.sqrt(_MU_KM3 / a_km ** 3) * 86400 / (2 * math.pi)))
    )) * 60)   # рад/с
    # Проще: n = sqrt(μ/a³)
    n = math.sqrt(_MU_KM3 / a_km ** 3)         # рад/с
    p = a_km * (1 - eccentricity ** 2)          # фокальный параметр
    i = math.radians(inclination_deg)

    dOmega_rad_s = (-3 / 2) * n * _J2 * (_RE_KM / p) ** 2 * math.cos(i)
    dOmega_deg_day = math.degrees(dOmega_rad_s) * 86400
    return round(dOmega_deg_day, 6)


def sun_synchronous_inclination_deg(a_km: float, eccentricity: float = 0.0) -> float:
    """
    Наклонение для солнечно-синхронной орбиты (RAAN прецессирует ~0.9856°/сут).

    Решает: dΩ/dt = +0.9856°/сут относительно инерциальной системы.
    """
    target_deg_day = 0.9856   # ~360°/365.25 сут
    target_rad_s   = math.radians(target_deg_day) / 86400

    n = math.sqrt(_MU_KM3 / a_km ** 3)
    p = a_km * (1 - eccentricity ** 2)

    cos_i = -target_rad_s / ((-3 / 2) * n * _J2 * (_RE_KM / p) ** 2)
    cos_i = max(-1.0, min(1.0, cos_i))
    return round(math.degrees(math.acos(cos_i)), 3)


def ground_track(
    sat: EarthSatellite,
    duration_min: int  = 90,
    steps:        int  = 360,
    start:        Optional[str] = None,
) -> list[dict]:
    """
    Наземная трасса (ground track) — список позиций вдоль орбиты.

    Args:
        sat:          объект EarthSatellite
        duration_min: длительность трека (минуты)
        steps:        количество точек
        start:        начало трека (UTC ISO), None = сейчас

    Returns:
        Список словарей {lon, lat, alt_km, timestamp}
    """
    t0_dt    = parse_time(start).utc_datetime()
    step_sec = (duration_min * 60) / steps

    times = ts.utc([t0_dt + timedelta(seconds=step_sec * i)
                    for i in range(steps + 1)])
    subs  = sat.at(times).subpoint()

    return [
        {
            "lon":       round(float(subs.longitude.degrees[i]), 5),
            "lat":       round(float(subs.latitude.degrees[i]),  5),
            "alt_km":    round(float(subs.elevation.km[i]),      2),
            "timestamp": times[i].utc_iso(),
        }
        for i in range(len(times.tt))
    ]


def passes_between(
    sat:       EarthSatellite,
    observer:  Topos,
    t_start,
    t_end,
    min_el_deg: float = 5.0,
) -> list[dict]:
    """
    Находит все пролёты спутника над наблюдателем в интервале [t_start, t_end].

    Args:
        sat:        объект EarthSatellite
        observer:   наблюдатель (skyfield Topos)
        t_start:    начало интервала (skyfield Time)
        t_end:      конец интервала (skyfield Time)
        min_el_deg: минимальная элевация для засчёта пролёта

    Returns:
        Список словарей:
        {aos, aos_az, max_el, max_el_time, max_el_az, los, los_az, duration_s}
    """
    try:
        t_ev, evs = sat.find_events(observer, t_start, t_end,
                                     altitude_degrees=min_el_deg)
    except Exception:
        return []

    passes:  list[dict] = []
    current: dict       = {}

    for ti, ev in zip(t_ev, evs):
        el, az = elev_az(sat, observer, ti)

        if ev == 0:                           # AOS
            current = {
                "aos":         ti.utc_iso(),
                "aos_az":      round(az, 1),
                "max_el":      0.0,
                "max_el_time": ti.utc_iso(),
                "max_el_az":   round(az, 1),
            }
        elif ev == 1 and current:             # Transit (макс. элевация)
            current["max_el"]      = round(el, 1)
            current["max_el_time"] = ti.utc_iso()
            current["max_el_az"]   = round(az, 1)
        elif ev == 2 and current:             # LOS
            los_iso = ti.utc_iso()
            aos_dt  = datetime.fromisoformat(current["aos"].replace("Z", "+00:00"))
            los_dt  = datetime.fromisoformat(los_iso.replace("Z", "+00:00"))
            passes.append({
                **current,
                "los":        los_iso,
                "los_az":     round(az, 1),
                "duration_s": int((los_dt - aos_dt).total_seconds()),
            })
            current = {}

    return passes


def days_since_epoch(meta: dict) -> Optional[float]:
    """
    Количество суток, прошедших с эпохи TLE до текущего момента.
    Большое значение (>30 сут) означает устаревшие данные.
    """
    epoch_dt = meta.get("epoch_dt")
    if not epoch_dt:
        return None
    now = datetime.now(timezone.utc)
    return round((now - epoch_dt).total_seconds() / 86400, 2)


def is_tle_fresh(meta: dict, max_age_days: float = 14.0) -> bool:
    """True если TLE не старше max_age_days суток."""
    age = days_since_epoch(meta)
    return age is not None and age <= max_age_days


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАТНАЯ СОВМЕСТИМОСТЬ — алиасы для satellites.py
# ══════════════════════════════════════════════════════════════════════════════

# В satellites.py заменить inline-функции на:
#   from core.orbital import (
#       parse_tle_meta       as _parse_tle_meta,
#       orbit_type_from_meta as _orbit_type_from_meta,
#       sat_position         as _sat_position,
#       elev_az              as _elev_az,
#       parse_time           as _parse_time,
#   )

_parse_tle_meta       = parse_tle_meta
_orbit_type_from_meta = orbit_type_from_meta
_sat_position         = sat_position
_elev_az              = elev_az
_parse_time           = parse_time