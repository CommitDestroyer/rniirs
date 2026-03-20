"""
core/coverage.py — вся математика зон радиовидимости и покрытия спутников.

Подключение в satellites.py (заменить inline-реализацию):
    from core.coverage import (
        coverage_polygon,
        coverage_radius_km,
        max_elevation_deg,
        horizon_distance_km,
        footprint_area_km2,
        is_point_in_coverage,
        multi_sat_coverage_union,
    )

    # Алиас для обратной совместимости (используется в websocket.py и groups.py)
    _coverage_polygon = coverage_polygon
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

# ══════════════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ
# ══════════════════════════════════════════════════════════════════════════════

_RE_KM  = 6371.0          # средний радиус Земли (км)
_RE_M   = _RE_KM * 1000   # в метрах


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНЫЕ ГЕОМЕТРИЧЕСКИЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def _earth_central_angle(alt_km: float, min_el_deg: float = 0.0) -> float:
    """
    Вычисляет Earth Central Angle (η) — полуугол зоны покрытия (рад).

    Формула:
        ρ = Re / (Re + h)
        η = arccos(ρ · cos(ε)) − ε
    где ε — минимальная элевация над горизонтом.
    """
    rho = _RE_KM / (_RE_KM + alt_km)
    eps = math.radians(min_el_deg)
    eta = math.acos(rho * math.cos(eps)) - eps
    return eta


def coverage_radius_km(alt_km: float, min_el_deg: float = 0.0) -> float:
    """
    Радиус зоны покрытия по поверхности Земли (км дуги).

    Args:
        alt_km:      высота орбиты (км)
        min_el_deg:  минимальная элевация наблюдателя (градусы)

    Returns:
        Радиус в км по поверхности Земли.
    """
    eta = _earth_central_angle(alt_km, min_el_deg)
    return round(_RE_KM * eta, 2)


def horizon_distance_km(alt_km: float) -> float:
    """
    Расстояние до радиогоризонта от точки на высоте alt_km (км).
    Формула: d = sqrt(h² + 2·Re·h)
    """
    return round(math.sqrt(alt_km ** 2 + 2 * _RE_KM * alt_km), 2)


def max_elevation_deg(sat_lat: float, sat_lon: float, sat_alt_km: float,
                      obs_lat: float, obs_lon: float) -> float:
    """
    Максимальная (зенитная) элевация спутника над наблюдателем (градусы).
    Использует центральный угол между точками на сфере.

    Args:
        sat_lat/lon:  координаты подспутниковой точки (градусы)
        sat_alt_km:   высота спутника (км)
        obs_lat/lon:  координаты наблюдателя (градусы)

    Returns:
        Элевация в градусах, −90..90.
        Отрицательное значение означает, что спутник за горизонтом.
    """
    # Центральный угол (γ) между подспутниковой точкой и наблюдателем
    phi1 = math.radians(sat_lat)
    lam1 = math.radians(sat_lon)
    phi2 = math.radians(obs_lat)
    lam2 = math.radians(obs_lon)

    sin_gamma = math.sqrt(
        (math.cos(phi2) * math.sin(lam2 - lam1)) ** 2
        + (math.cos(phi1) * math.sin(phi2)
           - math.sin(phi1) * math.cos(phi2) * math.cos(lam2 - lam1)) ** 2
    )
    cos_gamma = (math.sin(phi1) * math.sin(phi2)
                 + math.cos(phi1) * math.cos(phi2) * math.cos(lam2 - lam1))
    gamma = math.atan2(sin_gamma, cos_gamma)   # 0..π

    # Расстояние от центра Земли до спутника
    r_sat = _RE_KM + sat_alt_km

    # Закон косинусов треугольника (Земля-наблюдатель-спутник)
    # d² = Re² + r_sat² − 2·Re·r_sat·cos(γ)
    d = math.sqrt(_RE_KM ** 2 + r_sat ** 2 - 2 * _RE_KM * r_sat * math.cos(gamma))

    if d < 1e-9:
        return 90.0   # спутник прямо в зените

    # Элевация: угол между горизонтом наблюдателя и направлением на спутник
    cos_el = (r_sat ** 2 - _RE_KM ** 2 - d ** 2) / (2 * _RE_KM * d)
    cos_el = max(-1.0, min(1.0, cos_el))   # clamp для числовой стабильности
    el_rad = math.asin(cos_el)
    return round(math.degrees(el_rad), 4)


def footprint_area_km2(alt_km: float, min_el_deg: float = 0.0) -> float:
    """
    Площадь зоны покрытия (сферический сегмент, км²).

    Формула: A = 2π·Re²·(1 − cos(η))
    """
    eta = _earth_central_angle(alt_km, min_el_deg)
    area = 2 * math.pi * _RE_KM ** 2 * (1 - math.cos(eta))
    return round(area, 1)


# ══════════════════════════════════════════════════════════════════════════════
#  ПОСТРОЕНИЕ ПОЛИГОНА
# ══════════════════════════════════════════════════════════════════════════════

def coverage_polygon(
    lat: float,
    lon: float,
    alt_km: float,
    min_el_deg: float = 0.0,
    step_deg: float   = 1.0,
) -> tuple[list[list[float]], float]:
    """
    Строит полигон зоны радиовидимости методом сферической тригонометрии.

    Args:
        lat:        широта подспутниковой точки (градусы, −90..90)
        lon:        долгота подспутниковой точки (градусы, −180..180)
        alt_km:     высота спутника (км)
        min_el_deg: минимальная элевация для видимости (градусы)
        step_deg:   шаг построения контура (градусы, 0.5−5)

    Returns:
        (polygon, radius_km)
        polygon  — список точек [[lon, lat], ...], замкнутый (первая = последняя)
        radius_km — радиус зоны по поверхности Земли (км)
    """
    eta       = _earth_central_angle(alt_km, min_el_deg)
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
        points.append([round(math.degrees(p_lon), 4),
                       round(math.degrees(p_lat), 4)])

    points.append(points[0])   # замыкаем кольцо → валидный GeoJSON Polygon
    return points, radius_km


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def is_point_in_coverage(
    obs_lat: float, obs_lon: float,
    sat_lat: float, sat_lon: float,
    sat_alt_km: float,
    min_el_deg: float = 0.0,
) -> bool:
    """
    Проверяет, находится ли наблюдатель в зоне покрытия спутника.

    Быстрая проверка через сравнение центрального угла — без построения полигона.

    Returns:
        True если наблюдатель видит спутник под элевацией >= min_el_deg.
    """
    # Центральный угол между подспутниковой точкой и наблюдателем
    phi1 = math.radians(sat_lat)
    lam1 = math.radians(sat_lon)
    phi2 = math.radians(obs_lat)
    lam2 = math.radians(obs_lon)

    dlam = lam2 - lam1
    cos_gamma = (math.sin(phi1) * math.sin(phi2)
                 + math.cos(phi1) * math.cos(phi2) * math.cos(dlam))
    cos_gamma = max(-1.0, min(1.0, cos_gamma))
    gamma = math.acos(cos_gamma)

    eta = _earth_central_angle(sat_alt_km, min_el_deg)
    return gamma <= eta


def nadir_angle_deg(alt_km: float, gamma_deg: float) -> float:
    """
    Надирный угол спутника (угол от надира до направления на наблюдателя).

    Args:
        alt_km:    высота спутника (км)
        gamma_deg: центральный угол Земля-спутник-наблюдатель (градусы)

    Returns:
        Надирный угол (градусы).
    """
    rho = _RE_KM / (_RE_KM + alt_km)
    gamma = math.radians(gamma_deg)
    nadir = math.atan2(rho * math.sin(gamma), 1 - rho * math.cos(gamma))
    return round(math.degrees(nadir), 4)


def slant_range_km(alt_km: float, el_deg: float) -> float:
    """
    Наклонная дальность от наблюдателя до спутника (км).

    Формула по углу элевации:
        d = −Re·sin(ε) + sqrt(Re²·sin²(ε) + h²+ 2·Re·h)
    """
    el = math.radians(el_deg)
    re = _RE_KM
    d = -re * math.sin(el) + math.sqrt(
        re ** 2 * math.sin(el) ** 2 + alt_km ** 2 + 2 * re * alt_km
    )
    return round(d, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-SATELLITE UNION
# ══════════════════════════════════════════════════════════════════════════════

def multi_sat_coverage_union(
    satellites: list[dict],
    min_el_deg: float = 0.0,
    step_deg:   float = 1.0,
) -> list[dict]:
    """
    Строит зоны покрытия для списка спутников.

    Args:
        satellites: список словарей с ключами lat, lon, alt_km, id, name
        min_el_deg: минимальная элевация
        step_deg:   шаг полигона

    Returns:
        Список словарей:
        {id, name, center, polygon, radius_km, area_km2}
    """
    result = []
    for sat in satellites:
        lat    = sat["lat"]
        lon    = sat["lon"]
        alt_km = sat["alt_km"]
        polygon, radius_km = coverage_polygon(lat, lon, alt_km, min_el_deg, step_deg)
        result.append({
            "id":        sat.get("id"),
            "name":      sat.get("name", ""),
            "center":    [round(lon, 5), round(lat, 5)],
            "polygon":   polygon,
            "radius_km": radius_km,
            "area_km2":  footprint_area_km2(alt_km, min_el_deg),
        })
    return result


def coverage_overlap_pct(
    lat1: float, lon1: float, alt1_km: float,
    lat2: float, lon2: float, alt2_km: float,
    min_el_deg: float = 0.0,
) -> float:
    """
    Приблизительный процент перекрытия двух зон покрытия.

    Использует сравнение центральных углов — O(1), без растеризации.

    Returns:
        0.0 — нет перекрытия, 100.0 — одна зона полностью внутри другой.
    """
    # Центральный угол между подспутниковыми точками
    phi1 = math.radians(lat1); lam1 = math.radians(lon1)
    phi2 = math.radians(lat2); lam2 = math.radians(lon2)

    cos_d = (math.sin(phi1) * math.sin(phi2)
             + math.cos(phi1) * math.cos(phi2) * math.cos(lam2 - lam1))
    cos_d = max(-1.0, min(1.0, cos_d))
    d = math.acos(cos_d)   # угловое расстояние (рад)

    eta1 = _earth_central_angle(alt1_km, min_el_deg)
    eta2 = _earth_central_angle(alt2_km, min_el_deg)

    if d >= eta1 + eta2:
        return 0.0   # зоны не пересекаются

    if d <= abs(eta1 - eta2):
        # Одна зона полностью внутри другой
        smaller = min(eta1, eta2)
        larger  = max(eta1, eta2)
        area_small = 2 * math.pi * _RE_KM ** 2 * (1 - math.cos(smaller))
        area_large = 2 * math.pi * _RE_KM ** 2 * (1 - math.cos(larger))
        return round(area_small / area_large * 100, 1)

    # Частичное перекрытие — приближение через площадь сферического биугольника
    # Используем формулу площади пересечения двух сферических шапок
    cos_alpha = (math.cos(eta2) - math.cos(eta1) * math.cos(d)) / (math.sin(eta1) * math.sin(d) + 1e-12)
    cos_beta  = (math.cos(eta1) - math.cos(eta2) * math.cos(d)) / (math.sin(eta2) * math.sin(d) + 1e-12)
    cos_alpha = max(-1.0, min(1.0, cos_alpha))
    cos_beta  = max(-1.0, min(1.0, cos_beta))
    alpha = math.acos(cos_alpha)
    beta  = math.acos(cos_beta)

    overlap = 2 * _RE_KM ** 2 * (alpha + beta - math.pi / 2 * (
        1 - math.cos(eta1) + 1 - math.cos(eta2)
    ) / math.pi)
    overlap = max(0.0, overlap)

    area1 = 2 * math.pi * _RE_KM ** 2 * (1 - math.cos(eta1))
    return round(min(overlap / area1 * 100, 100.0), 1)


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАТНАЯ СОВМЕСТИМОСТЬ — алиас для satellites.py
# ══════════════════════════════════════════════════════════════════════════════

# satellites.py использует _coverage_polygon → импортировать так:
#   from core.coverage import coverage_polygon as _coverage_polygon
_coverage_polygon = coverage_polygon