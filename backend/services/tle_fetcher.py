"""
services/tle_fetcher.py — сервис загрузки TLE из внешних источников.

Инкапсулирует логику из tle.py:
    - словарь Celestrak URL
    - HTTP-загрузка с retry и таймаутом
    - параллельная загрузка нескольких категорий
    - автообновление по расписанию
    - история загрузок

Использование в tle.py:
    from services.tle_fetcher import (
        CELESTRAK_URLS,
        fetch_celestrak,
        fetch_many,
        get_auto_updater,
        get_load_history,
        TLEFetchResult,
        LoadHistory,
    )
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

import httpx


# ══════════════════════════════════════════════════════════════════════════════
#  ИСТОЧНИКИ CELESTRAK
# ══════════════════════════════════════════════════════════════════════════════

CELESTRAK_URLS: dict[str, str] = {
    "stations": "https://celestrak.org/SATCAT/stations.txt",
    "starlink":  "https://celestrak.org/SATCAT/starlink.txt",
    "gps":       "https://celestrak.org/SATCAT/gps-ops.txt",
    "resource":  "https://celestrak.org/SATCAT/resource.txt",
    "active":    "https://celestrak.org/SATCAT/active.txt",
    "weather":   "https://celestrak.org/SATCAT/weather.txt",
    "noaa":      "https://celestrak.org/SATCAT/noaa.txt",
    "goes":      "https://celestrak.org/SATCAT/goes.txt",
    "amateur":   "https://celestrak.org/SATCAT/amateur.txt",
    "debris":    "https://celestrak.org/SATCAT/iridium-33-debris.txt",
}


# ══════════════════════════════════════════════════════════════════════════════
#  РЕЗУЛЬТАТ ЗАГРУЗКИ
# ══════════════════════════════════════════════════════════════════════════════

class TLEFetchResult:
    """Результат одной HTTP-загрузки TLE."""

    __slots__ = ("category", "url", "content", "status_code",
                "error", "fetched_at", "sha256")

    def __init__(self, category: str, url: str,
                content: str = "", status_code: int = 0,
                error: Optional[str] = None) -> None:
        self.category    = category
        self.url         = url
        self.content     = content
        self.status_code = status_code
        self.error       = error
        self.fetched_at  = _now_iso()
        self.sha256      = _sha256(content) if content else ""

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.error

    @property
    def line_count(self) -> int:
        return self.content.count("\n") if self.content else 0

    def __repr__(self) -> str:
        s = "OK" if self.ok else f"ERR({self.status_code or self.error})"
        return f"TLEFetchResult({self.category!r}, {s}, lines={self.line_count})"


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP ЗАГРУЗКА
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_celestrak(
    category: str,
    timeout:  float = 20.0,
    retries:  int   = 2,
) -> TLEFetchResult:
    """
    Загружает TLE одной категории с Celestrak.

    Args:
        category: ключ из CELESTRAK_URLS
        timeout:  таймаут HTTP-запроса (секунды)
        retries:  число повторных попыток при ошибке сети

    Returns:
        TLEFetchResult — ok=True если загрузка успешна.
    """
    url = CELESTRAK_URLS.get(category)
    if not url:
        return TLEFetchResult(
            category=category, url="", status_code=400,
            error=f"Unknown category '{category}'. "
                f"Available: {sorted(CELESTRAK_URLS)}"
        )

    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "SatelliteMonitor/1.0"},
            ) as client:
                r = await client.get(url)

            if r.status_code == 200:
                return TLEFetchResult(
                    category=category, url=url,
                    content=r.text, status_code=200,
                )
            last_err = f"HTTP {r.status_code}"

        except httpx.TimeoutException:
            last_err = f"timeout after {timeout}s"
        except httpx.NetworkError as e:
            last_err = f"network error: {e}"
        except Exception as e:
            last_err = str(e)

        if attempt < retries:
            await asyncio.sleep(2 ** attempt)   # backoff: 1s, 2s

    return TLEFetchResult(category=category, url=url, status_code=0, error=last_err)


async def fetch_many(
    categories:  list[str],
    timeout:     float = 20.0,
    retries:     int   = 1,
    concurrency: int   = 3,
) -> list[TLEFetchResult]:
    """
    Загружает несколько категорий параллельно с ограничением concurrency.

    Returns:
        Список TLEFetchResult в том же порядке что categories.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(cat: str) -> TLEFetchResult:
        async with sem:
            return await fetch_celestrak(cat, timeout=timeout, retries=retries)

    return list(await asyncio.gather(*[_one(c) for c in categories]))


# ══════════════════════════════════════════════════════════════════════════════
#  ИСТОРИЯ ЗАГРУЗОК
# ══════════════════════════════════════════════════════════════════════════════

class LoadHistory:
    """
    In-memory история загрузок TLE.
    Прямая замена _LoadHistory из tle.py — с тем же API.
    """

    def __init__(self, max_records: int = 200) -> None:
        self._records: list[dict]      = []
        self._meta:    dict[str, dict] = {}
        self._max = max_records

    def add(self, source: str, added: int, skipped: int,
            sha256: str = "", loaded_at: Optional[str] = None,
            total_in_db: int = 0) -> dict:
        """Записывает результат загрузки, возвращает запись."""
        entry = {
            "source":      source,
            "added":       added,
            "skipped":     skipped,
            "sha256":      sha256,
            "loaded_at":   loaded_at or _now_iso(),
            "total_in_db": total_in_db,
        }
        self._records.append(entry)
        if len(self._records) > self._max:
            self._records.pop(0)
        self._meta[source] = {
            "last_fetched": entry["loaded_at"],
            "sha256":       sha256,
            "count":        added,
        }
        return entry

    def add_from_result(self, result: TLEFetchResult,
                        added: int, skipped: int,
                        total_in_db: int = 0) -> dict:
        """Shortcut: добавить из TLEFetchResult."""
        return self.add(
            source=result.category, added=added, skipped=skipped,
            sha256=result.sha256, loaded_at=result.fetched_at,
            total_in_db=total_in_db,
        )

    def history(self, limit: int = 20) -> list[dict]:
        return self._records[-limit:]

    def source_meta(self, source: str) -> Optional[dict]:
        return self._meta.get(source)

    def all_source_meta(self) -> dict[str, dict]:
        return dict(self._meta)

    def sources_loaded(self) -> list[str]:
        return list(self._meta)

    @property
    def total_records(self) -> int:
        return len(self._records)


# ══════════════════════════════════════════════════════════════════════════════
#  АВТООБНОВЛЕНИЕ
# ══════════════════════════════════════════════════════════════════════════════

FetchCallback = Callable[[TLEFetchResult], Coroutine[None, None, tuple[int, int]]]


class TLEAutoUpdater:
    """
    Фоновый asyncio-таск периодического обновления TLE.

    Прямая замена _AutoUpdater из tle.py — более тестируемый и настраиваемый.
    """

    def __init__(
        self,
        on_fetch:   Optional[FetchCallback] = None,
        history:    Optional[LoadHistory]   = None,
        interval_h: int                     = 24,
    ) -> None:
        self._on_fetch   = on_fetch
        self._history    = history
        self._interval_h = interval_h
        self._categories: list[str]              = list(CELESTRAK_URLS)
        self._task:       Optional[asyncio.Task] = None
        self._running    = False
        self._last_run:  Optional[str]           = None
        self._runs       = 0
        self._errors     = 0

    async def _loop(self) -> None:
        while self._running:
            print(f"[AutoUpdate] цикл: {len(self._categories)} категорий")
            for cat in self._categories:
                if not self._running:
                    break
                result = await fetch_celestrak(cat)
                if result.ok:
                    added = skipped = 0
                    if self._on_fetch:
                        try:
                            added, skipped = await self._on_fetch(result)
                        except Exception as e:
                            print(f"[AutoUpdate] callback {cat}: {e}")
                            self._errors += 1
                    if self._history:
                        self._history.add_from_result(result, added, skipped)
                    print(f"[AutoUpdate] {cat}: +{added} skip={skipped}")
                else:
                    print(f"[AutoUpdate] {cat}: {result.error}")
                    self._errors += 1

            self._last_run = _now_iso()
            self._runs    += 1
            await asyncio.sleep(self._interval_h * 3600)

    def start(self, categories: Optional[list[str]] = None,
              interval_h:  Optional[int]       = None) -> bool:
        """
        Запускает фоновый таск.
        Returns: True если запущен, False если уже работал.
        """
        if self._running:
            return False
        if categories:
            bad = [c for c in categories if c not in CELESTRAK_URLS]
            if bad:
                raise ValueError(f"Unknown categories: {bad}")
            self._categories = categories
        if interval_h is not None:
            self._interval_h = interval_h
        self._running = True
        self._task    = asyncio.create_task(self._loop())
        return True

    def stop(self) -> bool:
        """Останавливает таск. Returns: True если был запущен."""
        if not self._running:
            return False
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        return True

    def reconfigure(self, categories: Optional[list[str]] = None,
                    interval_h:  Optional[int]       = None) -> None:
        """Меняет настройки (применятся в следующем цикле)."""
        if categories:
            self._categories = categories
        if interval_h is not None:
            self._interval_h = interval_h

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status(self) -> dict:
        """Совместим с API старого _AutoUpdater.status."""
        return {
            "running":    self._running,
            "interval_h": self._interval_h,
            "last_run":   self._last_run,
            "categories": self._categories,
            "runs":       self._runs,
            "errors":     self._errors,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  СИНГЛТОНЫ
# ══════════════════════════════════════════════════════════════════════════════

_default_updater: Optional[TLEAutoUpdater] = None
_default_history: Optional[LoadHistory]    = None


def get_load_history(max_records: int = 200) -> LoadHistory:
    """Глобальный singleton LoadHistory."""
    global _default_history
    if _default_history is None:
        _default_history = LoadHistory(max_records=max_records)
    return _default_history


def get_auto_updater(
    on_fetch:   Optional[FetchCallback] = None,
    interval_h: int                     = 24,
) -> TLEAutoUpdater:
    """
    Глобальный singleton TLEAutoUpdater.

    Использование в tle.py:
        from services.tle_fetcher import get_auto_updater
        auto_updater = get_auto_updater(on_fetch=my_callback)
        auto_updater.start(interval_h=24)
    """
    global _default_updater
    if _default_updater is None:
        _default_updater = TLEAutoUpdater(
            on_fetch=on_fetch,
            history=get_load_history(),
            interval_h=interval_h,
        )
    return _default_updater


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def is_valid_category(category: str) -> bool:
    return category in CELESTRAK_URLS


def unknown_categories(categories: list[str]) -> list[str]:
    return [c for c in categories if c not in CELESTRAK_URLS]