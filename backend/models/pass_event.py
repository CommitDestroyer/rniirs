"""
models/pass_event.py — все Pydantic-модели связанные с пролётами спутников.

Использование:
    from models.pass_event import (
        PassEvent,
        PassEventBrief,
        PassesResponse,
        PassesOverPointResponse,
        NextPassResponse,
        MultiSatNextPassResponse,
        PassTimelineItem,
        PassDetailResponse,
        ElevationPoint,
        BestPassItem,
        BestPassesResponse,
        PassStatsResponse,
        VisibilityWindowItem,
        VisibilityWindowsResponse,
        Observer,
    )
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════════════════════════════════════

class Observer(BaseModel):
    """Координаты наблюдателя."""
    lat:   float = Field(..., ge=-90,  le=90,   description="Широта (°)")
    lon:   float = Field(..., ge=-180, le=180,  description="Долгота (°)")
    alt_m: float = Field(0.0, ge=0,   le=8848, description="Высота над уровнем моря (м)")


class ElevationPoint(BaseModel):
    """Одна точка кривой элевации/азимута — для полярной диаграммы."""
    time:      str   = Field(..., description="UTC ISO timestamp")
    elevation: float = Field(..., ge=-90, le=90,  description="Элевация (°)")
    azimuth:   float = Field(..., ge=0,   le=360, description="Азимут (°)")


# ══════════════════════════════════════════════════════════════════════════════
#  PASS EVENT — два варианта
# ══════════════════════════════════════════════════════════════════════════════

class PassEventBrief(BaseModel):
    """
    Краткое событие пролёта — используется в /satellites/{sid}/passes.
    Не содержит sat_id/sat_name — спутник уже известен из контекста запроса.
    """
    aos:          str   = Field(..., description="UTC ISO — появление над горизонтом")
    aos_az:       float = Field(..., ge=0, le=360, description="Азимут при AOS (°)")
    max_el:       float = Field(..., ge=0, le=90,  description="Максимальная элевация (°)")
    max_el_time:  str   = Field(..., description="UTC ISO момента максимальной элевации")
    max_el_az:    float = Field(..., ge=0, le=360, description="Азимут при max элевации (°)")
    los:          str   = Field(..., description="UTC ISO — уход за горизонт")
    los_az:       float = Field(..., ge=0, le=360, description="Азимут при LOS (°)")
    duration_s:   int   = Field(..., ge=0,          description="Длительность прохода (с)")

    @field_validator("duration_s")
    @classmethod
    def duration_positive(cls, v: int) -> int:
        return max(0, v)


class PassEvent(BaseModel):
    """
    Полное событие пролёта — содержит идентификаторы спутника.
    Используется в /passes/over-point, /groups/{key}/passes и WebSocket pass_alert.
    """
    sat_id:       int            = Field(..., description="ID спутника в БД")
    sat_name:     str            = Field(..., description="Название спутника")
    sat_type:     str            = Field("Unknown", description="Тип орбиты: LEO/MEO/GEO/HEO")
    operator:     str            = Field("Unknown", description="Оператор/владелец")
    norad_id:     Optional[int]  = Field(None, description="NORAD Catalog Number")
    aos:          str            = Field(..., description="UTC ISO — AOS")
    aos_az:       float          = Field(..., ge=0, le=360)
    max_el:       float          = Field(..., ge=0, le=90)
    max_el_time:  str            = Field(...)
    max_el_az:    float          = Field(..., ge=0, le=360)
    los:          str            = Field(..., description="UTC ISO — LOS")
    los_az:       float          = Field(..., ge=0, le=360)
    duration_s:   int            = Field(..., ge=0)

    def to_brief(self) -> PassEventBrief:
        """Конвертирует в краткий формат (без sat_id/sat_name)."""
        return PassEventBrief(
            aos=self.aos, aos_az=self.aos_az,
            max_el=self.max_el, max_el_time=self.max_el_time, max_el_az=self.max_el_az,
            los=self.los, los_az=self.los_az, duration_s=self.duration_s,
        )

    @property
    def score(self) -> float:
        """Взвешенная оценка качества пролёта [0..100]: 60% элевация + 40% длительность."""
        return round(min(self.max_el / 90.0, 1.0) * 60
                     + min(self.duration_s / 600.0, 1.0) * 40, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  ОТВЕТЫ ДЛЯ /satellites/{sid}/...
# ══════════════════════════════════════════════════════════════════════════════

class PassesResponse(BaseModel):
    """Ответ /satellites/{sid}/passes — пролёты конкретного спутника."""
    id:       int
    name:     str
    observer: Observer
    passes:   list[PassEventBrief]


class PassDetailResponse(BaseModel):
    """Ответ /passes/{sid}/detail — один пролёт с кривой элевации."""
    sat_id:            int
    sat_name:          str
    observer:          Observer
    aos:               str
    los:               str
    max_el:            float
    duration_s:        int
    elevation_profile: list[ElevationPoint]


# ══════════════════════════════════════════════════════════════════════════════
#  ОТВЕТЫ ДЛЯ /passes/...
# ══════════════════════════════════════════════════════════════════════════════

class PassesOverPointResponse(BaseModel):
    """Ответ /passes/over-point — все пролёты всех спутников над точкой."""
    observer: Observer
    days:     int   = Field(..., ge=1)
    min_el:   float = Field(..., ge=0, le=90)
    total:    int
    passes:   list[PassEvent]


class NextPassResponse(BaseModel):
    """Ответ /passes/{sid}/next — ближайший пролёт одного спутника."""
    sat_id:      int
    sat_name:    str
    observer:    Observer
    pass_event:  Optional[PassEvent] = None
    search_days: int = Field(..., ge=1)


class MultiSatNextPassResponse(BaseModel):
    """Ответ /passes/next/multi — ближайшие пролёты нескольких спутников."""
    observer: Observer
    results:  list[NextPassResponse]


# ══════════════════════════════════════════════════════════════════════════════
#  ТАЙМЛАЙН
# ══════════════════════════════════════════════════════════════════════════════

class PassTimelineItem(BaseModel):
    """Один элемент таймлайна с информацией об одновременных пролётах."""
    sat_id:       int
    sat_name:     str
    aos:          str
    los:          str
    max_el:       float
    duration_s:   int
    overlap_with: list[int] = Field(
        default_factory=list,
        description="sat_id спутников, видимых одновременно"
    )


class PassTimelineResponse(BaseModel):
    """Ответ /passes/timeline."""
    observer:    Observer
    days:        int
    items:       list[PassTimelineItem]
    max_overlap: int = Field(..., description="Макс. число одновременно видимых спутников")


# ══════════════════════════════════════════════════════════════════════════════
#  ЛУЧШИЕ ПРОЛЁТЫ
# ══════════════════════════════════════════════════════════════════════════════

class BestPassItem(BaseModel):
    """Элемент списка лучших пролётов с оценкой качества."""
    sat_id:     int
    sat_name:   str
    sat_type:   str
    aos:        str
    max_el:     float
    duration_s: int
    score:      float = Field(..., ge=0, le=100, description="Оценка [0..100]")


class BestPassesResponse(BaseModel):
    """Ответ /passes/best."""
    observer: Observer
    days:     int
    passes:   list[BestPassItem]


# ══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════════════

class PassStatsResponse(BaseModel):
    """Ответ /passes/stats — сводная статистика пролётов."""
    observer:          Observer
    days:              int
    total_passes:      int
    total_covered_min: float = Field(..., description="Суммарное время покрытия (мин)")
    avg_duration_s:    Optional[float] = None
    avg_max_el:        Optional[float] = None
    max_el_ever:       Optional[float] = None
    passes_per_day:    float
    by_sat_type:       dict[str, int]  = Field(default_factory=dict)
    by_hour_utc:       dict[int, int]  = Field(
        default_factory=dict,
        description="Распределение пролётов по часам суток UTC"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ОКНА ВИДИМОСТИ
# ══════════════════════════════════════════════════════════════════════════════

class VisibilityWindowItem(BaseModel):
    """Одно непрерывное окно когда хотя бы один спутник виден."""
    start:        str
    end:          str
    duration_min: float = Field(..., ge=0)
    sats_visible: int   = Field(..., ge=1)
    sat_ids:      list[int]


class VisibilityWindowsResponse(BaseModel):
    """Ответ /passes/visibility-windows."""
    observer:          Observer
    days:              int
    windows:           list[VisibilityWindowItem]
    total_covered_min: float
    coverage_pct:      float = Field(..., ge=0, le=100)


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТА: конвертация dict → PassEvent
# ══════════════════════════════════════════════════════════════════════════════

def pass_event_from_dict(d: dict, rec: dict) -> PassEvent:
    """
    Создаёт PassEvent из сырого словаря (результат find_events) и записи БД.
    Удобно использовать прямо в расчётных функциях.

    Args:
        d:   словарь с ключами aos, aos_az, max_el, max_el_time, max_el_az,
            los, los_az, duration_s
        rec: запись из SatelliteDB (dict с id, name, type, operator, norad_id)
    """
    return PassEvent(
        sat_id=rec["id"],
        sat_name=rec["name"],
        sat_type=rec.get("type", "Unknown"),
        operator=rec.get("operator", "Unknown"),
        norad_id=rec.get("norad_id"),
        aos=d["aos"],
        aos_az=d.get("aos_az", 0.0),
        max_el=d.get("max_el", 0.0),
        max_el_time=d.get("max_el_time", d["aos"]),
        max_el_az=d.get("max_el_az", 0.0),
        los=d.get("los", d["aos"]),
        los_az=d.get("los_az", 0.0),
        duration_s=d.get("duration_s", 0),
    )