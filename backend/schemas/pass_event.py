"""
schemas/pass_event.py — FastAPI request/response схемы для пролётов.

Отличие от models/pass_event.py:
    models/ — внутренние структуры данных и бизнес-логика
    schemas/ — контракт HTTP API: что принимают эндпоинты и что возвращают

Использование в passes.py:
    from schemas.pass_event import (
        PassesQueryParams,
        NextPassQueryParams,
        MultiSatQueryParams,
        PassDetailQueryParams,
        TimelineQueryParams,
        BestPassesQueryParams,
        PassStatsQueryParams,
        VisibilityWindowsQueryParams,
        OverPointsQueryParams,
        FilterByElevationParams,
        FilterByDurationParams,
        PassEventOut,
        PassesOverPointOut,
        NextPassOut,
        MultiSatNextPassOut,
        PassDetailOut,
        PassTimelineOut,
        BestPassesOut,
        PassStatsOut,
        VisibilityWindowsOut,
    )
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
#  ВХОДЯЩИЕ ПАРАМЕТРЫ ЗАПРОСОВ (Query Params)
# ══════════════════════════════════════════════════════════════════════════════

class _ObserverParams(BaseModel):
    """Базовые координаты наблюдателя — переиспользуется во всех запросах."""
    lat:   float = Field(..., ge=-90,  le=90,   description="Широта наблюдателя (°)")
    lon:   float = Field(..., ge=-180, le=180,  description="Долгота наблюдателя (°)")
    alt_m: float = Field(0.0, ge=0,   le=8848, description="Высота над уровнем моря (м)")


class PassesQueryParams(_ObserverParams):
    """Query-параметры для /passes/over-point."""
    days:     int            = Field(1,     ge=1,  le=10)
    min_el:   float          = Field(5.0,   ge=0,  le=90,  description="Мин. элевация (°)")
    sat_type: Optional[str]  = Field(None,                 description="LEO|MEO|GEO|HEO")
    operator: Optional[str]  = Field(None)
    limit:    int            = Field(200,   ge=1,  le=1000)
    sort_by:  str            = Field("aos",                description="aos|max_el|duration_s")
    desc:     bool           = Field(False)


class NextPassQueryParams(_ObserverParams):
    """Query-параметры для /passes/{sid}/next."""
    min_el:      float = Field(5.0, ge=0, le=90)
    search_days: int   = Field(3,   ge=1, le=7)


class MultiSatQueryParams(_ObserverParams):
    """Query-параметры для /passes/next/multi."""
    ids:         str   = Field(...,  description="sat_id через запятую: 1,2,5")
    min_el:      float = Field(5.0,  ge=0, le=90)
    search_days: int   = Field(3,    ge=1, le=7)


class PassDetailQueryParams(_ObserverParams):
    """Query-параметры для /passes/{sid}/detail."""
    min_el:        float = Field(5.0, ge=0,  le=90)
    pass_index:    int   = Field(0,   ge=0,             description="0 = ближайший")
    profile_steps: int   = Field(60,  ge=10, le=300,    description="Точек в кривой")


class TimelineQueryParams(_ObserverParams):
    """Query-параметры для /passes/timeline."""
    days:     int           = Field(1,   ge=1,  le=3)
    min_el:   float         = Field(5.0, ge=0,  le=90)
    sat_type: Optional[str] = Field(None)
    operator: Optional[str] = Field(None)
    limit:    int           = Field(100, ge=1,  le=500)


class BestPassesQueryParams(_ObserverParams):
    """Query-параметры для /passes/best."""
    days:     int           = Field(1,   ge=1, le=10)
    min_el:   float         = Field(5.0, ge=0, le=90)
    sat_type: Optional[str] = Field(None)
    operator: Optional[str] = Field(None)
    top_n:    int           = Field(20,  ge=1, le=100)


class PassStatsQueryParams(_ObserverParams):
    """Query-параметры для /passes/stats."""
    days:     int           = Field(1,   ge=1, le=10)
    min_el:   float         = Field(5.0, ge=0, le=90)
    sat_type: Optional[str] = Field(None)
    operator: Optional[str] = Field(None)


class VisibilityWindowsQueryParams(_ObserverParams):
    """Query-параметры для /passes/visibility-windows."""
    days:     int           = Field(1,   ge=1, le=3)
    min_el:   float         = Field(5.0, ge=0, le=90)
    sat_type: Optional[str] = Field(None)
    operator: Optional[str] = Field(None)


class OverPointsQueryParams(BaseModel):
    """Query-параметры для /passes/{sid}/over-points."""
    points:  str   = Field(..., description="lat:lon через запятую: 55.7:37.6,48.8:2.3")
    days:    int   = Field(1,   ge=1, le=3)
    min_el:  float = Field(5.0, ge=0, le=90)


class FilterByElevationParams(_ObserverParams):
    """Query-параметры для /passes/filter/by-elevation."""
    days:   int   = Field(1,    ge=1, le=10)
    min_el: float = Field(30.0, ge=0, le=90, description="Мин. максимальная элевация")
    limit:  int   = Field(50,   ge=1, le=200)


class FilterByDurationParams(_ObserverParams):
    """Query-параметры для /passes/filter/by-duration."""
    days:      int   = Field(1,   ge=1, le=10)
    min_dur_s: int   = Field(300, ge=1,       description="Мин. длительность (с)")
    min_el:    float = Field(5.0, ge=0, le=90)
    limit:     int   = Field(50,  ge=1, le=200)


# ══════════════════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ СХЕМЫ (Response)
# ══════════════════════════════════════════════════════════════════════════════

class _ObserverOut(BaseModel):
    """Наблюдатель в ответе."""
    lat:   float
    lon:   float
    alt_m: float = 0.0


class PassEventOut(BaseModel):
    """Одно событие пролёта в ответе API."""
    sat_id:      int
    sat_name:    str
    sat_type:    str
    operator:    str
    norad_id:    Optional[int]  = None
    aos:         str
    aos_az:      float
    max_el:      float
    max_el_time: str
    max_el_az:   float
    los:         str
    los_az:      float
    duration_s:  int

    @property
    def score(self) -> float:
        return round(min(self.max_el / 90.0, 1.0) * 60
                     + min(self.duration_s / 600.0, 1.0) * 40, 2)


class _ElevationPointOut(BaseModel):
    time:      str
    elevation: float
    azimuth:   float


class PassesOverPointOut(BaseModel):
    """Ответ /passes/over-point."""
    observer: _ObserverOut
    days:     int
    min_el:   float
    total:    int
    passes:   list[PassEventOut]


class NextPassOut(BaseModel):
    """Ответ /passes/{sid}/next."""
    sat_id:      int
    sat_name:    str
    observer:    _ObserverOut
    pass_event:  Optional[PassEventOut] = None
    search_days: int


class MultiSatNextPassOut(BaseModel):
    """Ответ /passes/next/multi."""
    observer: _ObserverOut
    results:  list[NextPassOut]


class PassDetailOut(BaseModel):
    """Ответ /passes/{sid}/detail — профиль одного пролёта."""
    sat_id:            int
    sat_name:          str
    observer:          _ObserverOut
    aos:               str
    los:               str
    max_el:            float
    duration_s:        int
    elevation_profile: list[_ElevationPointOut]


class _PassTimelineItemOut(BaseModel):
    sat_id:       int
    sat_name:     str
    aos:          str
    los:          str
    max_el:       float
    duration_s:   int
    overlap_with: list[int] = Field(default_factory=list)


class PassTimelineOut(BaseModel):
    """Ответ /passes/timeline."""
    observer:    _ObserverOut
    days:        int
    items:       list[_PassTimelineItemOut]
    max_overlap: int


class _BestPassItemOut(BaseModel):
    sat_id:     int
    sat_name:   str
    sat_type:   str
    aos:        str
    max_el:     float
    duration_s: int
    score:      float


class BestPassesOut(BaseModel):
    """Ответ /passes/best."""
    observer: _ObserverOut
    days:     int
    passes:   list[_BestPassItemOut]


class PassStatsOut(BaseModel):
    """Ответ /passes/stats."""
    observer:          _ObserverOut
    days:              int
    total_passes:      int
    total_covered_min: float
    avg_duration_s:    Optional[float] = None
    avg_max_el:        Optional[float] = None
    max_el_ever:       Optional[float] = None
    passes_per_day:    float
    by_sat_type:       dict[str, int]  = Field(default_factory=dict)
    by_hour_utc:       dict[int, int]  = Field(default_factory=dict)


class _VisibilityWindowItemOut(BaseModel):
    start:        str
    end:          str
    duration_min: float
    sats_visible: int
    sat_ids:      list[int]


class VisibilityWindowsOut(BaseModel):
    """Ответ /passes/visibility-windows."""
    observer:          _ObserverOut
    days:              int
    windows:           list[_VisibilityWindowItemOut]
    total_covered_min: float
    coverage_pct:      float