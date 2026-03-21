"""
schemas/satellite.py — FastAPI request/response схемы для эндпоинтов спутников.

Использование в satellites.py:
    from schemas.satellite import (
        SatelliteListParams,
        SatelliteCountParams,
        CoverageQueryParams,
        NextPassQueryParams,
        SatellitePassesParams,
        SatelliteBriefOut,
        SatelliteDetailOut,
        SatelliteCountOut,
        SatelliteTypesOut,
        SatelliteOperatorsOut,
        SatelliteDeleteOut,
        NextPassOut,
        CoverageOut,
        MultiCoverageOut,
        GroupCompareOut,
    )
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, computed_field


# ══════════════════════════════════════════════════════════════════════════════
#  ВХОДЯЩИЕ ПАРАМЕТРЫ ЗАПРОСОВ
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteListParams(BaseModel):
    """Query-параметры для GET /satellites — список с фильтрацией и сортировкой."""
    operator: Optional[str] = Field(None, description="Фильтр по оператору")
    type:     Optional[str] = Field(None, description="LEO | MEO | GEO | HEO")
    name:     Optional[str] = Field(None, description="Поиск по имени (подстрока)")
    limit:    int           = Field(200, ge=1, le=500)
    offset:   int           = Field(0,   ge=0)
    sort_by:  str           = Field("id",
                                    pattern="^(id|name|period_min|apogee_km)$",
                                    description="Поле сортировки")
    desc:     bool          = Field(False, description="Сортировка по убыванию")


class SatelliteCountParams(BaseModel):
    """Query-параметры для GET /satellites/count."""
    type:     Optional[str] = Field(None)
    operator: Optional[str] = Field(None)


class CoverageQueryParams(BaseModel):
    """Query-параметры для GET /satellites/{sid}/coverage."""
    min_el: float         = Field(0.0, ge=0,   le=45,  description="Мин. элевация (°)")
    at:     Optional[str] = Field(None,                description="UTC ISO момент")
    step:   float         = Field(1.0, ge=0.5, le=5.0, description="Шаг полигона (°)")


class NextPassQueryParams(BaseModel):
    """Query-параметры для GET /satellites/{sid}/next-pass."""
    lat:    float = Field(..., ge=-90,  le=90,  description="Широта наблюдателя (°)")
    lon:    float = Field(..., ge=-180, le=180, description="Долгота наблюдателя (°)")
    min_el: float = Field(5.0, ge=0,   le=90,  description="Мин. элевация (°)")


class SatellitePassesParams(BaseModel):
    """Query-параметры для GET /satellites/{sid}/passes."""
    lat:    float = Field(..., ge=-90,  le=90)
    lon:    float = Field(..., ge=-180, le=180)
    alt_m:  float = Field(0.0, ge=0,   le=8848)
    days:   int   = Field(3,   ge=1,   le=10)
    min_el: float = Field(5.0, ge=0,   le=90)


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ — СПИСОК И КАРТОЧКИ
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteBriefOut(BaseModel):
    """Краткая карточка — элемент GET /satellites."""
    id:              int
    name:            str
    operator:        str
    type:            str
    norad_id:        Optional[int]   = None
    inclination_deg: Optional[float] = None
    period_min:      Optional[float] = None


class SatelliteDetailOut(SatelliteBriefOut):
    """Полная карточка — ответ GET /satellites/{sid}."""
    line1:              str
    line2:              str
    epoch:              Optional[str]   = None
    apogee_km:          Optional[float] = None
    perigee_km:         Optional[float] = None
    eccentricity:       Optional[float] = None
    mean_motion:        Optional[float] = None
    semi_major_axis_km: Optional[float] = None
    raan_deg:           Optional[float] = None
    arg_perigee_deg:    Optional[float] = None

    @computed_field
    @property
    def alt_approx_km(self) -> Optional[float]:
        """Средняя высота орбиты (apogee + perigee) / 2."""
        if self.apogee_km is not None and self.perigee_km is not None:
            return round((self.apogee_km + self.perigee_km) / 2, 1)
        return None


class SatelliteCountOut(BaseModel):
    """Ответ GET /satellites/count."""
    count: int


class SatelliteTypesOut(BaseModel):
    """Ответ GET /satellites/types."""
    types: list[str]


class SatelliteOperatorsOut(BaseModel):
    """Ответ GET /satellites/operators."""
    operators: list[str]


class SatelliteDeleteOut(BaseModel):
    """Ответ DELETE /satellites/{sid}."""
    deleted: int


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ — ПОКРЫТИЕ
# ══════════════════════════════════════════════════════════════════════════════

class CoverageOut(BaseModel):
    """Зона покрытия — ответ GET /satellites/{sid}/coverage."""
    id:                int
    name:              str
    center:            list[float]       = Field(..., description="[lon, lat]")
    alt_km:            float
    radius_km:         float
    min_elevation_deg: float
    polygon:           list[list[float]] = Field(..., description="[[lon, lat], ...]")

    @computed_field
    @property
    def center_latlng(self) -> list[float]:
        """Центр в формате [lat, lon] для Leaflet."""
        return [self.center[1], self.center[0]]


class _CoverageItem(BaseModel):
    """Один спутник в bulk-ответе покрытий."""
    id:        int
    name:      str
    center:    list[float]
    radius_km: float
    polygon:   list[list[float]]


class MultiCoverageOut(BaseModel):
    """Ответ GET /satellites/coverage/multi."""
    items: list[_CoverageItem]
    count: int

    @computed_field
    @property
    def total_radius_km(self) -> float:
        """Суммарный радиус всех зон покрытия."""
        return round(sum(i.radius_km for i in self.items), 1)


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ — NEXT PASS
# ══════════════════════════════════════════════════════════════════════════════

class _PassBriefOut(BaseModel):
    """Краткий пролёт в next-pass ответе."""
    aos:         str
    aos_az:      float
    max_el:      float
    max_el_time: str
    los:         str
    los_az:      float
    duration_s:  int


class NextPassOut(BaseModel):
    """Ответ GET /satellites/{sid}/next-pass."""
    satellite: str
    pass_data: Optional[_PassBriefOut] = Field(None, alias="pass")
    message:   Optional[str]          = None

    model_config = {"populate_by_name": True}


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ — ГРУППИРОВКИ
# ══════════════════════════════════════════════════════════════════════════════

class GroupCompareOut(BaseModel):
    """Один тип орбиты в ответе GET /satellites/groups/compare."""
    type:           str
    count:          int
    avg_alt_km:     Optional[float] = None
    min_period_min: Optional[float] = None
    max_period_min: Optional[float] = None
    operators:      list[str]       = Field(default_factory=list)

    @computed_field
    @property
    def period_spread_min(self) -> Optional[float]:
        """Разброс периодов (max - min)."""
        if self.max_period_min is not None and self.min_period_min is not None:
            return round(self.max_period_min - self.min_period_min, 3)
        return None