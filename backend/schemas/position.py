"""
schemas/position.py — FastAPI request/response схемы для позиций и орбитальных треков.

Использование в satellites.py и groups.py:
    from schemas.position import (
        PositionQueryParams,
        OrbitQueryParams,
        BulkPositionQueryParams,
        GroupPositionQueryParams,
        MultiCoverageQueryParams,
        PositionOut,
        BulkPositionOut,
        OrbitTrackOut,
        TrackPointOut,
    )
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, computed_field


# ══════════════════════════════════════════════════════════════════════════════
#  ВХОДЯЩИЕ ПАРАМЕТРЫ ЗАПРОСОВ (Query Params)
# ══════════════════════════════════════════════════════════════════════════════

class PositionQueryParams(BaseModel):
    """Query-параметры для GET /satellites/{sid}/position."""
    at: Optional[str] = Field(
        None,
        description="UTC ISO-8601, напр. 2025-04-01T12:00:00Z. "
                    "Если не задан — текущий момент с кэшем."
    )


class OrbitQueryParams(BaseModel):
    """Query-параметры для GET /satellites/{sid}/orbit."""
    minutes: int           = Field(90,   ge=1,  le=1440, description="Длительность трека (мин)")
    steps:   int           = Field(360,  ge=10, le=1440, description="Количество точек")
    start:   Optional[str] = Field(None,                  description="Начало трека (UTC ISO)")


class BulkPositionQueryParams(BaseModel):
    """Query-параметры для GET /satellites/positions/bulk."""
    ids:      Optional[str] = Field(None, description="sat_id через запятую. Если нет — все")
    type:     Optional[str] = Field(None, description="Фильтр по типу орбиты: LEO|MEO|GEO|HEO")
    operator: Optional[str] = Field(None, description="Фильтр по оператору")
    at:       Optional[str] = Field(None, description="UTC ISO момент. Если нет — сейчас + кэш")
    limit:    int           = Field(100,  ge=1, le=200)


class GroupPositionQueryParams(BaseModel):
    """Query-параметры для GET /groups/{key}/positions."""
    by:    str           = Field("type", pattern="^(type|operator)$")
    at:    Optional[str] = Field(None)
    limit: int           = Field(100, ge=1, le=200)


class MultiCoverageQueryParams(BaseModel):
    """Query-параметры для GET /satellites/coverage/multi."""
    ids:    str           = Field(..., description="sat_id через запятую, макс. 50")
    min_el: float         = Field(0.0, ge=0, le=45)
    at:     Optional[str] = Field(None)


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ (Response)
# ══════════════════════════════════════════════════════════════════════════════

class PositionOut(BaseModel):
    """Позиция одного спутника — ответ /satellites/{sid}/position."""
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

    @computed_field
    @property
    def latlng(self) -> list[float]:
        """[lat, lon] — удобный формат для Leaflet."""
        return [self.lat, self.lon]


class BulkPositionOut(BaseModel):
    """Позиция спутника в bulk-ответе — без period_min (не всегда нужен)."""
    id:            int
    name:          str
    operator:      str
    type:          str
    lat:           float
    lon:           float
    alt_km:        float
    timestamp:     str
    velocity_km_s: Optional[float] = None


class TrackPointOut(BaseModel):
    """Одна точка наземного трека."""
    lon:    float = Field(..., ge=-180, le=180)
    lat:    float = Field(..., ge=-90,  le=90)
    alt_km: float = Field(..., ge=0)


class OrbitTrackOut(BaseModel):
    """Орбитальный трек — ответ /satellites/{sid}/orbit."""
    id:      int
    name:    str
    minutes: int
    steps:   int
    track:   list[TrackPointOut]

    @computed_field
    @property
    def polyline(self) -> list[list[float]]:
        """Трек в формате [[lat, lon], ...] — напрямую для Leaflet Polyline."""
        return [[p.lat, p.lon] for p in self.track]

    @computed_field
    @property
    def polyline_3d(self) -> list[list[float]]:
        """Трек в формате [[lat, lon, alt_km], ...] — для 3D-глобуса."""
        return [[p.lat, p.lon, p.alt_km] for p in self.track]