"""
models/satellite.py — все Pydantic-модели связанные со спутниками,
группировками и зонами покрытия.

Использование:
    from models.satellite import (
        SatelliteBrief, SatelliteDetail,
        PositionResponse, TrackPoint, OrbitTrackResponse,
        CoverageResponse, CoveragePolygonItem, CoverageUnionResponse,
        GroupCompareItem, GroupSummary, GroupStats, CompareResult,
        SatBriefInGroup, ConstellationGapResponse, GapPeriod,
    )
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, computed_field

# Типы орбит — используются как Literal для валидации
OrbitType = Literal["LEO", "MEO", "GEO", "HEO", "Unknown"]

# ══════════════════════════════════════════════════════════════════════════════
#  СПУТНИК — КАРТОЧКИ
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteBrief(BaseModel):
    """Краткая карточка спутника — для списков и фильтрации."""
    id:              int
    name:            str
    operator:        str
    type:            str               = Field("Unknown", description="LEO/MEO/GEO/HEO/Unknown")
    norad_id:        Optional[int]     = None
    inclination_deg: Optional[float]   = None
    period_min:      Optional[float]   = None


class SatelliteDetail(SatelliteBrief):
    """Полная карточка спутника — для /satellites/{sid}."""
    line1:               str
    line2:               str
    epoch:               Optional[str]   = None
    apogee_km:           Optional[float] = None
    perigee_km:          Optional[float] = None
    eccentricity:        Optional[float] = None
    mean_motion:         Optional[float] = None
    semi_major_axis_km:  Optional[float] = None
    raan_deg:            Optional[float] = Field(None, description="Прямое восхождение восходящего узла (°)")
    arg_perigee_deg:     Optional[float] = Field(None, description="Аргумент перигея (°)")

    @computed_field
    @property
    def alt_approx_km(self) -> Optional[float]:
        """Средняя высота орбиты (среднее апогея и перигея)."""
        if self.apogee_km is not None and self.perigee_km is not None:
            return round((self.apogee_km + self.perigee_km) / 2, 1)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  ПОЗИЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

class PositionResponse(BaseModel):
    """Текущая или историческая позиция спутника."""
    id:            int
    name:          str
    operator:      str
    type:          str
    lat:           float = Field(..., ge=-90,  le=90)
    lon:           float = Field(..., ge=-180, le=180)
    alt_km:        float = Field(..., ge=0)
    timestamp:     str
    period_min:    float
    velocity_km_s: Optional[float] = Field(None, description="Модуль вектора скорости (км/с)")


# ══════════════════════════════════════════════════════════════════════════════
#  ОРБИТАЛЬНЫЙ ТРЕК
# ══════════════════════════════════════════════════════════════════════════════

class TrackPoint(BaseModel):
    """Одна точка наземного трека."""
    lon:    float = Field(..., ge=-180, le=180)
    lat:    float = Field(..., ge=-90,  le=90)
    alt_km: float = Field(..., ge=0)


class OrbitTrackResponse(BaseModel):
    """Ответ /satellites/{sid}/orbit — наземный трек."""
    id:      int
    name:    str
    minutes: int  = Field(..., ge=1, le=1440)
    steps:   int  = Field(..., ge=10)
    track:   list[TrackPoint]

    @computed_field
    @property
    def track_as_latlons(self) -> list[list[float]]:
        """Трек в формате [[lat, lon], ...] — удобно для Leaflet Polyline."""
        return [[p.lat, p.lon] for p in self.track]


# ══════════════════════════════════════════════════════════════════════════════
#  ЗОНЫ ПОКРЫТИЯ
# ══════════════════════════════════════════════════════════════════════════════

class CoverageResponse(BaseModel):
    """Зона радиовидимости одного спутника — ответ /satellites/{sid}/coverage."""
    id:                int
    name:              str
    center:            list[float]        = Field(..., description="[lon, lat] подспутниковой точки")
    alt_km:            float
    radius_km:         float              = Field(..., ge=0, description="Радиус зоны по поверхности (км)")
    min_elevation_deg: float              = Field(..., ge=0, le=90)
    polygon:           list[list[float]]  = Field(..., description="[[lon, lat], ...] замкнутый контур")

    @computed_field
    @property
    def center_latlng(self) -> list[float]:
        """Центр в формате [lat, lon] — для Leaflet."""
        return [self.center[1], self.center[0]]


class CoveragePolygonItem(BaseModel):
    """Один спутник в bulk-ответе зон покрытия."""
    id:        int
    name:      str
    center:    list[float]       = Field(..., description="[lon, lat]")
    radius_km: float
    polygon:   list[list[float]] = Field(..., description="[[lon, lat], ...]")


class CoverageUnionResponse(BaseModel):
    """Зоны покрытия группы спутников — /groups/{key}/coverage."""
    group_key: str
    count:     int
    polygons:  list[CoveragePolygonItem]
    timestamp: str


# ══════════════════════════════════════════════════════════════════════════════
#  ГРУППИРОВКИ
# ══════════════════════════════════════════════════════════════════════════════

class GroupCompareItem(BaseModel):
    """Краткое сравнение группировок — /satellites/groups/compare."""
    type:           str
    count:          int
    avg_alt_km:     Optional[float] = None
    min_period_min: Optional[float] = None
    max_period_min: Optional[float] = None
    operators:      list[str]       = Field(default_factory=list)


class GroupSummary(BaseModel):
    """Список группировок — /groups."""
    group_by:  str        = Field(..., description="'type' или 'operator'")
    key:       str        = Field(..., description="Значение группировки (напр. 'LEO')")
    count:     int
    sat_ids:   list[int]
    norad_ids: list[int]  = Field(default_factory=list)


class GroupStats(BaseModel):
    """Орбитальная статистика группировки — /groups/{key}/stats."""
    key:              str
    count:            int
    orbit_types:      dict[str, int]  = Field(default_factory=dict, description="LEO: 40, MEO: 5 ...")
    avg_alt_km:       Optional[float] = None
    avg_period_min:   Optional[float] = None
    min_period_min:   Optional[float] = None
    max_period_min:   Optional[float] = None
    avg_inclination:  Optional[float] = None
    eccentricity_avg: Optional[float] = None
    operators:        list[str]       = Field(default_factory=list)

    @computed_field
    @property
    def dominant_orbit_type(self) -> Optional[str]:
        """Тип орбиты с наибольшим числом спутников."""
        return max(self.orbit_types, key=self.orbit_types.get) if self.orbit_types else None


class CompareResult(BaseModel):
    """Сравнение нескольких группировок — /groups/compare."""
    groups: list[GroupStats]
    diff:   Optional[dict] = Field(None, description="Разница числовых полей (если запрошено 2 группы)")


class SatBriefInGroup(BaseModel):
    """Краткая карточка спутника в контексте группировки."""
    id:              int
    name:            str
    norad_id:        Optional[int]   = None
    type:            str
    operator:        str
    period_min:      Optional[float] = None
    apogee_km:       Optional[float] = None
    inclination_deg: Optional[float] = None


# ══════════════════════════════════════════════════════════════════════════════
#  GAPS АНАЛИЗ
# ══════════════════════════════════════════════════════════════════════════════

class GapPeriod(BaseModel):
    """Один промежуток без покрытия."""
    start:   str
    end:     str
    gap_min: float = Field(..., ge=0, description="Длительность паузы (мин)")


class ConstellationGapResponse(BaseModel):
    """Анализ покрытия группировки — /groups/{key}/coverage-gaps."""
    group_key:    str
    observer:     dict
    days:         int
    total_passes: int
    covered_min:  float = Field(..., ge=0, description="Суммарное покрытие (мин)")
    gap_periods:  list[GapPeriod]
    coverage_pct: float = Field(..., ge=0, le=100)

    @computed_field
    @property
    def gap_count(self) -> int:
        return len(self.gap_periods)

    @computed_field
    @property
    def max_gap_min(self) -> Optional[float]:
        return max((g.gap_min for g in self.gap_periods), default=None)


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТА: запись БД → модель
# ══════════════════════════════════════════════════════════════════════════════

def satellite_brief_from_rec(rec: dict) -> SatelliteBrief:
    """Создаёт SatelliteBrief из записи SatelliteDB."""
    return SatelliteBrief(
        id=rec["id"], name=rec["name"],
        operator=rec.get("operator", "Unknown"),
        type=rec.get("type", "Unknown"),
        norad_id=rec.get("norad_id"),
        inclination_deg=rec.get("inclination_deg"),
        period_min=rec.get("period_min"),
    )


def satellite_detail_from_rec(rec: dict) -> SatelliteDetail:
    """Создаёт SatelliteDetail из записи SatelliteDB."""
    return SatelliteDetail(
        id=rec["id"], name=rec["name"],
        operator=rec.get("operator", "Unknown"),
        type=rec.get("type", "Unknown"),
        norad_id=rec.get("norad_id"),
        inclination_deg=rec.get("inclination_deg"),
        period_min=rec.get("period_min"),
        line1=rec["line1"], line2=rec["line2"],
        epoch=rec.get("epoch"),
        apogee_km=rec.get("apogee_km"),
        perigee_km=rec.get("perigee_km"),
        eccentricity=rec.get("eccentricity"),
        mean_motion=rec.get("mean_motion"),
        semi_major_axis_km=rec.get("semi_major_axis_km"),
        raan_deg=rec.get("raan_deg"),
        arg_perigee_deg=rec.get("arg_perigee_deg"),
    )