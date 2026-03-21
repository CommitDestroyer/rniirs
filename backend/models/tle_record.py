"""
models/tle_record.py — все Pydantic-модели для работы с TLE-данными.

Использование:
    from models.tle_record import (
        TLERecord, TLEUploadResult, TLEValidationResult,
        TLESourceStatus, RawTLERequest, ValidateRequest,
        AutoUpdateConfig, TLEExportRequest, TLEStats,
        tle_record_from_meta,
    )
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, field_validator, computed_field


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНЫЕ МОДЕЛИ
# ══════════════════════════════════════════════════════════════════════════════

class TLERecord(BaseModel):
    """Одна запись спутника, распарсенная из TLE-файла."""
    name:            str
    line1:           str   = Field(..., min_length=69, max_length=69)
    line2:           str   = Field(..., min_length=69, max_length=69)
    norad_id:        Optional[int]   = None
    epoch:           Optional[str]   = None
    orbit_type:      Optional[str]   = Field(None, description="LEO/MEO/GEO/HEO/Unknown")
    inclination_deg: Optional[float] = None
    period_min:      Optional[float] = None
    apogee_km:       Optional[float] = None
    perigee_km:      Optional[float] = None
    eccentricity:    Optional[float] = None
    operator:        str             = "Unknown"
    source:          str             = "manual"

    @field_validator("line1")
    @classmethod
    def line1_format(cls, v: str) -> str:
        if not v.startswith("1 "):
            raise ValueError("Line1 must start with '1 '")
        return v

    @field_validator("line2")
    @classmethod
    def line2_format(cls, v: str) -> str:
        if not v.startswith("2 "):
            raise ValueError("Line2 must start with '2 '")
        return v

    @computed_field
    @property
    def alt_approx_km(self) -> Optional[float]:
        """Средняя высота орбиты (среднее апогея и перигея)."""
        if self.apogee_km is not None and self.perigee_km is not None:
            return round((self.apogee_km + self.perigee_km) / 2, 1)
        return None

    @computed_field
    @property
    def norad_str(self) -> str:
        """NORAD ID как строка с ведущими нулями, напр. '00025544'."""
        return str(self.norad_id).zfill(5) if self.norad_id else "?????"

    def to_3le(self) -> str:
        """Сериализует запись в 3LE-формат (3 строки)."""
        return f"{self.name}\n{self.line1}\n{self.line2}"


class TLEUploadResult(BaseModel):
    """Результат загрузки TLE — возвращается всеми upload/fetch-эндпоинтами."""
    source:      str
    added:       int  = Field(..., ge=0)
    skipped:     int  = Field(..., ge=0)
    total_in_db: int  = Field(..., ge=0)
    sha256:      str  = Field(..., description="Первые 16 символов SHA-256 контента")
    loaded_at:   str  = Field(..., description="UTC ISO timestamp загрузки")

    @computed_field
    @property
    def success_rate(self) -> float:
        """Процент успешно загруженных записей."""
        total = self.added + self.skipped
        return round(self.added / total * 100, 1) if total else 0.0


class TLEValidationResult(BaseModel):
    """Результат валидации одной пары TLE-строк."""
    valid:       bool
    name:        Optional[str]   = None
    norad_id:    Optional[int]   = None
    orbit_type:  Optional[str]   = None
    period_min:  Optional[float] = None
    epoch:       Optional[str]   = None
    errors:      list[str]       = Field(default_factory=list)

    @computed_field
    @property
    def error_count(self) -> int:
        return len(self.errors)


# ══════════════════════════════════════════════════════════════════════════════
#  ИСТОЧНИКИ И СТАТУС
# ══════════════════════════════════════════════════════════════════════════════

class TLESourceStatus(BaseModel):
    """Статус источника TLE — ответ /tle/sources."""
    category:     str
    url:          str
    last_fetched: Optional[str]  = Field(None, description="UTC ISO последней загрузки")
    count:        int            = Field(0, ge=0)
    sha256:       Optional[str]  = None

    @computed_field
    @property
    def ever_fetched(self) -> bool:
        return self.last_fetched is not None


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПРОСЫ (входящие тела)
# ══════════════════════════════════════════════════════════════════════════════

class RawTLERequest(BaseModel):
    """Тело запроса POST /tle/raw — загрузка TLE текстом."""
    content:  str  = Field(..., min_length=10, description="Текст TLE (3LE или 2LE)")
    operator: str  = Field("Manual",           description="Оператор/владелец спутников")
    source:   str  = Field("raw",              description="Имя источника")
    replace:  bool = Field(False,              description="Очистить БД перед загрузкой")


class ValidateRequest(BaseModel):
    """Тело запроса POST /tle/validate — валидация пары строк."""
    line1: str = Field(..., min_length=69, max_length=69)
    line2: str = Field(..., min_length=69, max_length=69)
    name:  str = Field("", description="Имя спутника (опционально)")


class AutoUpdateConfig(BaseModel):
    """Тело запроса POST /tle/auto-update/start — настройка авто-обновления."""
    categories: list[str]     = Field(default_factory=list,
                                    description="Категории Celestrak. Пусто = все")
    interval_h: Optional[int] = Field(None, ge=1, le=168,
                                    description="Интервал обновления (1..168 часов)")


class TLEExportRequest(BaseModel):
    """Параметры экспорта TLE — используется как Query в /tle/export."""
    sat_type: Optional[str] = Field(None, description="Фильтр по типу орбиты")
    operator: Optional[str] = Field(None, description="Фильтр по оператору")
    limit:    int            = Field(500, ge=1, le=5000)


# ══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════════════

class EpochRange(BaseModel):
    """Диапазон эпох TLE в базе."""
    oldest: Optional[str] = None
    newest: Optional[str] = None

    @computed_field
    @property
    def is_fresh(self) -> bool:
        """True если самая новая эпоха не старше 14 суток."""
        if not self.newest:
            return False
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(self.newest.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).days
            return age <= 14
        except Exception:
            return False


class PeriodRange(BaseModel):
    """Диапазон орбитальных периодов (мин)."""
    min: Optional[float] = None
    max: Optional[float] = None


class TLEStats(BaseModel):
    """Статистика по TLE в базе — ответ /tle/stats."""
    total:            int              = Field(0, ge=0)
    by_orbit_type:    dict[str, int]   = Field(default_factory=dict)
    by_operator:      dict[str, int]   = Field(default_factory=dict,
                                                description="Топ-20 операторов")
    epoch_range:      EpochRange       = Field(default_factory=EpochRange)
    period_range_min: PeriodRange      = Field(default_factory=PeriodRange)
    sources_loaded:   list[str]        = Field(default_factory=list)

    @computed_field
    @property
    def operator_count(self) -> int:
        return len(self.by_operator)


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТА: meta dict → TLERecord
# ══════════════════════════════════════════════════════════════════════════════

def tle_record_from_meta(name: str, l1: str, l2: str,
                        meta: dict,
                        operator: str = "Unknown",
                        source:   str = "manual",
                        orbit_type: Optional[str] = None) -> TLERecord:
    """
    Создаёт TLERecord из распарсенных метаданных TLE.

    Args:
        name:       название спутника
        l1, l2:     строки TLE
        meta:       словарь из _parse_tle_meta / parse_tle_meta
        operator:   оператор
        source:     источник данных
        orbit_type: тип орбиты (если не передан — берётся из meta)
    """
    return TLERecord(
        name=name.strip(),
        line1=l1, line2=l2,
        norad_id=meta.get("norad_id"),
        epoch=meta.get("epoch"),
        orbit_type=orbit_type or meta.get("orbit_type"),
        inclination_deg=meta.get("inclination_deg"),
        period_min=meta.get("period_min"),
        apogee_km=meta.get("apogee_km"),
        perigee_km=meta.get("perigee_km"),
        eccentricity=meta.get("eccentricity"),
        operator=operator,
        source=source,
    )