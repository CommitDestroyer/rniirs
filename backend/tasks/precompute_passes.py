"""
tasks/precompute_passes.py — фоновый таск предрасчёта пролётов.

Проблема которую решает:
    find_events() из skyfield — тяжёлая операция (~50мс на спутник).
    При 500 спутниках первый запрос /passes/over-point занимает 25 секунд.
    Предрасчёт делает это заранее в фоне и кэширует результат.

Использование в app/main.py:
    from tasks.precompute_passes import PassesCache, get_passes_cache

    # В lifespan:
    cache = get_passes_cache()
    cache.start(db=db, ts=ts)   # запускает фоновый цикл

    # В passes.py (вместо _find_passes_for_rec):
    cache = get_passes_cache()
    cached = cache.get(sat_id=1, lat=55.75, lon=37.62)
    if cached is not None:
        passes = cached
    else:
        passes = _find_passes_for_rec(rec, observer, t0, t1, min_el)
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════════════════════
#  КЛЮЧ КЭША
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(sat_id: int, lat: float, lon: float,
            min_el: float, days: int) -> str:
    """
    Детерминированный ключ для кэша.
    Округляем координаты до 2 знаков (~1км точность) чтобы близкие точки
    получали один кэш.
    """
    return f"{sat_id}:{lat:.2f}:{lon:.2f}:{min_el:.1f}:{days}"


def _observer_hash(observers: list[dict]) -> str:
    """SHA-8 от списка наблюдателей — для инвалидации при смене точек."""
    raw = ";".join(f"{o['lat']:.2f},{o['lon']:.2f}" for o in observers)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ КЭША
# ══════════════════════════════════════════════════════════════════════════════

class _CacheEntry:
    """Одна запись в кэше пролётов."""

    __slots__ = ("passes", "computed_at", "expires_at", "sat_id",
                "lat", "lon", "min_el", "days")

    def __init__(self, sat_id: int, lat: float, lon: float,
                min_el: float, days: int,
                passes: list, ttl_s: int) -> None:
        self.sat_id      = sat_id
        self.lat         = lat
        self.lon         = lon
        self.min_el      = min_el
        self.days        = days
        self.passes      = passes
        self.computed_at = time.monotonic()
        self.expires_at  = self.computed_at + ttl_s

    @property
    def is_fresh(self) -> bool:
        return time.monotonic() < self.expires_at

    @property
    def age_s(self) -> float:
        return round(time.monotonic() - self.computed_at, 1)

    @property
    def pass_count(self) -> int:
        return len(self.passes)


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ КЭШ
# ══════════════════════════════════════════════════════════════════════════════

class PassesCache:
    """
    TTL-кэш предрасчитанных пролётов.

    Фоновый таск обходит все спутники × все наблюдательные точки
    и заполняет кэш заранее. Эндпоинты берут из кэша без блокировки.
    """

    def __init__(
        self,
        ttl_s:              int   = 300,     # время жизни записи (5 мин)
        refresh_interval_s: int   = 240,     # как часто обновлять кэш (4 мин)
        default_days:       int   = 1,       # горизонт расчёта по умолчанию
        default_min_el:     float = 5.0,     # минимальная элевация
        max_observers:      int   = 20,      # максимум точек наблюдения
        max_satellites:     int   = 500,     # максимум спутников в кэше
        batch_size:         int   = 20,      # спутников за одну итерацию
        batch_delay_s:      float = 0.1,     # пауза между батчами
    ) -> None:
        self._ttl              = ttl_s
        self._refresh_interval = refresh_interval_s
        self._default_days     = default_days
        self._default_min_el   = default_min_el
        self._max_observers    = max_observers
        self._max_sats         = max_satellites
        self._batch_size       = batch_size
        self._batch_delay      = batch_delay_s

        self._store:     dict[str, _CacheEntry] = {}
        self._observers: list[dict]             = []  # [{lat, lon, alt_m}]
        self._task:      Optional[asyncio.Task] = None
        self._running    = False
        self._lock       = asyncio.Lock()

        # Статистика
        self._hits   = 0
        self._misses = 0
        self._computed = 0
        self._errors   = 0
        self._last_run: Optional[str] = None

    # ── получение из кэша ─────────────────────────────────────────────────────

    def get(self, sat_id: int, lat: float, lon: float,
            min_el: float = 5.0, days: int = 1) -> Optional[list]:
        """
        Возвращает кэшированные пролёты или None если кэш устарел/отсутствует.

        Args:
            sat_id: ID спутника в SatelliteDB
            lat, lon: координаты наблюдателя
            min_el: минимальная элевация (°)
            days: горизонт расчёта

        Returns:
            Список пролётов (dict или PassEvent) или None.
        """
        key   = _cache_key(sat_id, lat, lon, min_el, days)
        entry = self._store.get(key)
        if entry and entry.is_fresh:
            self._hits += 1
            return entry.passes
        self._misses += 1
        return None

    def get_or_compute(self, sat_id: int, lat: float, lon: float,
                    min_el: float, days: int,
                    compute_fn) -> list:
        """
        Возвращает из кэша или вычисляет синхронно и кэширует.

        Используется как fallback когда фоновый таск ещё не заполнил кэш.

        Args:
            compute_fn: callable() → list[passes] без аргументов
        """
        cached = self.get(sat_id, lat, lon, min_el, days)
        if cached is not None:
            return cached
        passes = compute_fn()
        self._store_result(sat_id, lat, lon, min_el, days, passes)
        return passes

    # ── запись в кэш ──────────────────────────────────────────────────────────

    def _store_result(self, sat_id: int, lat: float, lon: float,
                    min_el: float, days: int, passes: list) -> None:
        key = _cache_key(sat_id, lat, lon, min_el, days)
        self._store[key] = _CacheEntry(
            sat_id=sat_id, lat=lat, lon=lon,
            min_el=min_el, days=days,
            passes=passes, ttl_s=self._ttl,
        )

    def invalidate(self, sat_id: Optional[int] = None) -> int:
        """
        Инвалидирует кэш.

        Args:
            sat_id: если задан — только для этого спутника, иначе весь кэш.

        Returns:
            Количество удалённых записей.
        """
        if sat_id is None:
            count = len(self._store)
            self._store.clear()
            return count

        prefix  = f"{sat_id}:"
        to_del  = [k for k in self._store if k.startswith(prefix)]
        for k in to_del:
            del self._store[k]
        return len(to_del)

    def purge_expired(self) -> int:
        """Удаляет устаревшие записи. Returns: количество удалённых."""
        expired = [k for k, e in self._store.items() if not e.is_fresh]
        for k in expired:
            del self._store[k]
        return len(expired)

    # ── управление наблюдателями ──────────────────────────────────────────────

    def add_observer(self, lat: float, lon: float, alt_m: float = 0.0) -> bool:
        """
        Добавляет точку наблюдения в список для предрасчёта.

        Returns:
            True если добавлена, False если уже есть или лимит достигнут.
        """
        if len(self._observers) >= self._max_observers:
            return False
        obs = {"lat": round(lat, 2), "lon": round(lon, 2), "alt_m": alt_m}
        if obs not in self._observers:
            self._observers.append(obs)
            return True
        return False

    def remove_observer(self, lat: float, lon: float) -> bool:
        obs = {"lat": round(lat, 2), "lon": round(lon, 2), "alt_m": 0.0}
        if obs in self._observers:
            self._observers.remove(obs)
            self.invalidate()   # сбрасываем весь кэш
            return True
        return False

    def set_observers(self, observers: list[dict]) -> None:
        """Заменяет весь список наблюдателей и инвалидирует кэш."""
        self._observers = [
            {"lat": round(o["lat"], 2),
            "lon": round(o["lon"], 2),
            "alt_m": o.get("alt_m", 0.0)}
            for o in observers[:self._max_observers]
        ]
        self.invalidate()

    # ── фоновый таск ──────────────────────────────────────────────────────────

    async def _compute_one(self, rec: dict, obs: dict, ts_obj: Any) -> None:
        """Вычисляет пролёты для одного спутника × одного наблюдателя."""
        try:
            from skyfield.api import Topos
            observer = Topos(
                latitude_degrees=obs["lat"],
                longitude_degrees=obs["lon"],
                elevation_m=obs.get("alt_m", 0.0),
            )
            t0 = ts_obj.now()
            t1 = ts_obj.utc(
                t0.utc_datetime() + timedelta(days=self._default_days)
            )
            t_ev, evs = rec["satellite"].find_events(
                observer, t0, t1, altitude_degrees=self._default_min_el
            )

            passes: list[dict] = []
            cur: dict = {}
            for ti, ev in zip(t_ev, evs):
                alt, az, _ = (rec["satellite"] - observer).at(ti).altaz()
                el, az_deg = alt.degrees, az.degrees

                if ev == 0:
                    cur = {"aos": ti.utc_iso(), "aos_az": round(az_deg, 1),
                        "max_el": 0.0, "max_el_time": ti.utc_iso(),
                        "max_el_az": round(az_deg, 1)}
                elif ev == 1 and cur:
                    cur["max_el"]      = round(el, 1)
                    cur["max_el_time"] = ti.utc_iso()
                    cur["max_el_az"]   = round(az_deg, 1)
                elif ev == 2 and cur:
                    cur["los"]     = ti.utc_iso()
                    cur["los_az"]  = round(az_deg, 1)
                    cur["duration_s"] = int((
                        datetime.fromisoformat(ti.utc_iso().replace("Z", "+00:00"))
                        - datetime.fromisoformat(cur["aos"].replace("Z", "+00:00"))
                    ).total_seconds())
                    passes.append(cur)
                    cur = {}

            self._store_result(
                sat_id=rec["id"],
                lat=obs["lat"], lon=obs["lon"],
                min_el=self._default_min_el,
                days=self._default_days,
                passes=passes,
            )
            self._computed += 1

        except Exception as e:
            self._errors += 1
            print(f"[PassesCache] error sat={rec.get('id')} obs={obs}: {e}")

    async def _loop(self, db_ref: Any, ts_obj: Any) -> None:
        """Основной цикл предрасчёта."""
        while self._running:
            if not self._observers:
                await asyncio.sleep(self._refresh_interval)
                continue

            recs = db_ref.all_records()[:self._max_sats]
            total = len(recs) * len(self._observers)
            done  = 0

            for i in range(0, len(recs), self._batch_size):
                if not self._running:
                    break
                batch = recs[i: i + self._batch_size]

                await asyncio.gather(*[
                    self._compute_one(rec, obs, ts_obj)
                    for rec in batch
                    for obs in self._observers
                ])
                done += len(batch) * len(self._observers)

                if self._batch_delay > 0:
                    await asyncio.sleep(self._batch_delay)

            self._last_run = _now_iso()
            self.purge_expired()
            print(f"[PassesCache] цикл завершён: {done}/{total} записей,"
                f" в кэше {len(self._store)}, ошибок {self._errors}")

            await asyncio.sleep(self._refresh_interval)

    # ── жизненный цикл ────────────────────────────────────────────────────────

    def start(self, db, ts) -> bool:
        """
        Запускает фоновый таск предрасчёта.

        Args:
            db: экземпляр SatelliteDB
            ts: skyfield timescale

        Returns:
            True если запущен, False если уже работал.
        """
        if self._running:
            return False
        self._running = True
        self._task    = asyncio.create_task(self._loop(db, ts))
        print(f"[PassesCache] запущен: ttl={self._ttl}s, "
            f"refresh={self._refresh_interval}s, "
            f"observers={len(self._observers)}")
        return True

    def stop(self) -> bool:
        """Останавливает фоновый таск."""
        if not self._running:
            return False
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    # ── статистика ────────────────────────────────────────────────────────────

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return round(self._hits / total * 100, 1) if total else 0.0

    def stats(self) -> dict:
        return {
            "running":          self._running,
            "cache_size":       len(self._store),
            "observers_count":  len(self._observers),
            "hits":             self._hits,
            "misses":           self._misses,
            "hit_rate_pct":     self.hit_rate,
            "computed":         self._computed,
            "errors":           self._errors,
            "last_run":         self._last_run,
            "ttl_s":            self._ttl,
            "refresh_interval_s": self._refresh_interval,
            "default_days":     self._default_days,
            "default_min_el":   self._default_min_el,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_cache: Optional[PassesCache] = None


def get_passes_cache(
    ttl_s:              int   = 300,
    refresh_interval_s: int   = 240,
    default_days:       int   = 1,
    default_min_el:     float = 5.0,
) -> PassesCache:
    """
    Глобальный singleton PassesCache.

    Использование в main.py (lifespan):
        from tasks.precompute_passes import get_passes_cache
        _cache = get_passes_cache()
        _cache.add_observer(lat=55.75, lon=37.62)   # Москва
        _cache.start(db=db, ts=ts)

    Использование в passes.py:
        from tasks.precompute_passes import get_passes_cache
        _cache = get_passes_cache()
        cached = _cache.get(sat_id=rec["id"], lat=lat, lon=lon)
        if cached is not None:
            passes = cached
        else:
            passes = _find_passes_for_rec(rec, observer, t0, t1, min_el)
            # Кэш заполнится сам при следующем цикле фонового таска
    """
    global _default_cache
    if _default_cache is None:
        _default_cache = PassesCache(
            ttl_s=ttl_s,
            refresh_interval_s=refresh_interval_s,
            default_days=default_days,
            default_min_el=default_min_el,
        )
    return _default_cache


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def estimate_compute_time_s(sat_count: int, observer_count: int,
                            ms_per_sat: float = 50.0) -> float:
    """
    Оценочное время полного цикла предрасчёта (секунды).

    Args:
        sat_count:      количество спутников в БД
        observer_count: количество точек наблюдения
        ms_per_sat:     среднее время расчёта одного спутника (мс)
    """
    return round(sat_count * observer_count * ms_per_sat / 1000, 1)


def passes_in_window(passes: list, hours: int = 24) -> list:
    """
    Фильтрует список пролётов — оставляет только те что начнутся
    в ближайшие `hours` часов от текущего момента.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    result = []
    for p in passes:
        try:
            aos = p.get("aos") if isinstance(p, dict) else getattr(p, "aos", None)
            if not aos:
                continue
            aos_dt = datetime.fromisoformat(aos.replace("Z", "+00:00"))
            if now <= aos_dt <= cutoff:
                result.append(p)
        except Exception:
            pass
    return result