"""
services/position_service.py — сервис расчёта и кэширования позиций спутников.

Инкапсулирует логику которая дублировалась в satellites.py, websocket.py и main.py:
    - вычисление геодезических координат через skyfield
    - TTL-кэш позиций
    - bulk-расчёт для списка спутников
    - обогащение позиции метаданными из записи БД

Использование в satellites.py:
    from services.position_service import PositionService, compute_position

    _svc = PositionService(ttl_s=5)
    pos  = _svc.get(rec, t)           # одиночный с кэшем
    bulk = _svc.bulk(records, t)      # пакетный

Использование в websocket.py:
    from services.position_service import get_position_service
    _svc = get_position_service()
    data = _svc.bulk_for_ws(recs, session.clock.now())
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from skyfield.api import EarthSatellite, Topos


# ══════════════════════════════════════════════════════════════════════════════
#  НИЗКОУРОВНЕВЫЕ ФУНКЦИИ — прямая замена inline-кода из satellites.py
# ══════════════════════════════════════════════════════════════════════════════

def compute_position(sat: EarthSatellite, t) -> dict:
    """
    Вычисляет геодезические координаты и скорость спутника.

    Returns:
        {lat, lon, alt_km, velocity_km_s, timestamp}
    """
    geo = sat.at(t)
    sub = geo.subpoint()
    return {
        "lat":           round(sub.latitude.degrees, 5),
        "lon":           round(sub.longitude.degrees, 5),
        "alt_km":        round(sub.elevation.km, 2),
        "velocity_km_s": round(float(np.linalg.norm(geo.velocity.km_per_s)), 3),
        "timestamp":     t.utc_iso(),
    }


def compute_elev_az(sat: EarthSatellite, observer: Topos,
                    t) -> tuple[float, float]:
    """Топоцентрическая элевация и азимут. Returns: (el_deg, az_deg)."""
    alt, az, _ = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees


def compute_elev_az_range(sat: EarthSatellite, observer: Topos,
                        t) -> tuple[float, float, float]:
    """Элевация, азимут и наклонная дальность. Returns: (el_deg, az_deg, km)."""
    alt, az, dist = (sat - observer).at(t).altaz()
    return alt.degrees, az.degrees, dist.km


def enrich_position(pos: dict, rec: dict) -> dict:
    """Добавляет id/name/operator/type из записи БД в словарь позиции (in-place)."""
    pos.update({
        "id":       rec["id"],
        "name":     rec["name"],
        "operator": rec.get("operator", "Unknown"),
        "type":     rec.get("type", "Unknown"),
    })
    return pos


def is_visible(sat: EarthSatellite, observer: Topos,
            t, min_el_deg: float = 5.0) -> bool:
    """Быстрая O(1) проверка видимости без лишних аллокаций."""
    el, _, _ = (sat - observer).at(t).altaz()
    return el.degrees >= min_el_deg


# ══════════════════════════════════════════════════════════════════════════════
#  TTL-КЭШ
# ══════════════════════════════════════════════════════════════════════════════

class _PositionCache:
    """TTL-кэш позиций. Ключ — sat_id, значение — (monotonic_ts, payload)."""

    __slots__ = ("_store", "_ttl")

    def __init__(self, ttl_s: float) -> None:
        self._store: dict[int, tuple[float, dict]] = {}
        self._ttl = ttl_s

    def get(self, sid: int) -> Optional[dict]:
        e = self._store.get(sid)
        return e[1] if e and time.monotonic() - e[0] < self._ttl else None

    def set(self, sid: int, pos: dict) -> None:
        self._store[sid] = (time.monotonic(), pos)

    def invalidate(self, sid: int) -> None:
        self._store.pop(sid, None)

    def clear(self) -> None:
        self._store.clear()

    def purge_expired(self) -> int:
        now = time.monotonic()
        expired = [sid for sid, (ts, _) in self._store.items()
                if now - ts >= self._ttl]
        for sid in expired:
            del self._store[sid]
        return len(expired)

    @property
    def size(self) -> int:
        return len(self._store)


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ СЕРВИС
# ══════════════════════════════════════════════════════════════════════════════

class PositionService:
    """
    Сервис позиций с TTL-кэшем.

    Заменяет _sat_position из satellites.py и inline-расчёты из websocket.py.
    """

    def __init__(self, ttl_s: float = 5.0) -> None:
        self._cache = _PositionCache(ttl_s)
        self._ttl   = ttl_s

    # ── одиночный ─────────────────────────────────────────────────────────────

    def compute(self, sat: EarthSatellite, t) -> dict:
        """Вычисляет позицию без кэша и без метаданных."""
        return compute_position(sat, t)

    def get(self, rec: dict, t, use_cache: bool = True) -> dict:
        """
        Позиция спутника с метаданными, с опциональным кэшем.

        Args:
            rec:       запись из SatelliteDB
            t:         skyfield Time
            use_cache: False если запрашиваем историческое время

        Returns:
            {id, name, operator, type, lat, lon, alt_km, velocity_km_s, timestamp}
        """
        sid = rec["id"]
        if use_cache:
            cached = self._cache.get(sid)
            if cached:
                return cached
        pos = compute_position(rec["satellite"], t)
        enrich_position(pos, rec)
        if use_cache:
            self._cache.set(sid, pos)
        return pos

    def get_full(self, rec: dict, t,
                use_cache: bool = True) -> dict:
        """
        Расширенная позиция — дополнительно включает period_min.
        Используется в /satellites/{sid}/position.
        """
        pos = self.get(rec, t, use_cache)
        if "period_min" not in pos:
            pos["period_min"] = round(rec.get("period_min") or 0, 4)
        return pos

    # ── bulk ──────────────────────────────────────────────────────────────────

    def bulk(self, records: list[dict], t,
            use_cache: bool = True,
            enrich: bool = True,
            limit: int = 0) -> list[dict]:
        """
        Пакетный расчёт позиций.

        Args:
            records:   записи из SatelliteDB
            t:         общий момент времени для всех
            use_cache: использовать TTL-кэш
            enrich:    добавлять id/name/operator/type
            limit:     ограничить количество (0 = все)
        """
        pool   = records[:limit] if limit else records
        result = []
        for rec in pool:
            sid = rec["id"]
            if use_cache:
                cached = self._cache.get(sid)
                if cached:
                    result.append(cached)
                    continue
            pos = compute_position(rec["satellite"], t)
            if enrich:
                enrich_position(pos, rec)
            if use_cache:
                self._cache.set(sid, pos)
            result.append(pos)
        return result

    def bulk_for_ws(self, records: list[dict], t) -> list[dict]:
        """
        Bulk для WebSocket broadcast — обогащает без кэша.
        Кэш не используется т.к. у каждого WS-клиента свой SimClock.
        """
        result = []
        for rec in records:
            pos = compute_position(rec["satellite"], t)
            pos.update({
                "id":       rec["id"],
                "name":     rec["name"],
                "type":     rec.get("type"),
                "operator": rec.get("operator"),
            })
            result.append(pos)
        return result

    # ── кэш ──────────────────────────────────────────────────────────────────

    def invalidate(self, sid: int) -> None:
        self._cache.invalidate(sid)

    def clear_cache(self) -> None:
        self._cache.clear()

    def purge_cache(self) -> int:
        return self._cache.purge_expired()

    @property
    def cache_size(self) -> int:
        return self._cache.size

    def stats(self) -> dict:
        return {"ttl_s": self._ttl, "cache_size": self._cache.size}


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_service: Optional[PositionService] = None


def get_position_service(ttl_s: float = 5.0) -> PositionService:
    """
    Глобальный singleton PositionService.

    Использование:
        from services.position_service import get_position_service
        _svc = get_position_service()
    """
    global _default_service
    if _default_service is None:
        _default_service = PositionService(ttl_s=ttl_s)
    return _default_service


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def _parse_time(at: Optional[str], ts):
    """Парсит UTC ISO в skyfield Time или возвращает ts.now()."""
    if not at:
        return ts.now()
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return ts.utc(dt.year, dt.month, dt.day,
                dt.hour, dt.minute,
                dt.second + dt.microsecond / 1_000_000)


def orbital_velocity_km_s(alt_km: float) -> float:
    """Круговая орбитальная скорость на высоте alt_km (км/с). v = √(μ/r)"""
    return round(math.sqrt(398_600.4418 / (6371.0 + alt_km)), 4)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")