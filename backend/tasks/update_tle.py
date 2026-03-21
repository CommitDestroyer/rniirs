"""
tasks/update_tle.py — фоновый таск периодического обновления TLE.

Отвечает за полный цикл обновления:
    1. Скачать TLE с Celestrak (через services/tle_fetcher.py)
    2. Распарсить и провалидировать (через core/tle_parser.py)
    3. Загрузить в SatelliteDB (дедупликация по NORAD ID)
    4. Сохранить резервную копию на диск
    5. Инвалидировать кэш пролётов (tasks/precompute_passes.py)
    6. Записать в историю и вывести статистику

Использование в app/main.py (lifespan):
    from tasks.update_tle import TLEUpdateTask, get_update_task

    task = get_update_task(db=db)
    task.start(categories=["stations", "active"], interval_h=24)

    # В lifespan yield:
    yield
    task.stop()
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
#  РЕЗУЛЬТАТ ОДНОГО ОБНОВЛЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

class UpdateResult:
    """Итог одного цикла обновления TLE."""

    __slots__ = ("category", "added", "skipped", "errors",
                "duration_s", "started_at", "finished_at", "saved_to")

    def __init__(self, category: str) -> None:
        self.category    = category
        self.added       = 0
        self.skipped     = 0
        self.errors:     list[str] = []
        self.duration_s  = 0.0
        self.started_at  = _now_iso()
        self.finished_at: Optional[str] = None
        self.saved_to:    Optional[str] = None

    def finish(self, duration_s: float) -> None:
        self.duration_s  = round(duration_s, 2)
        self.finished_at = _now_iso()

    @property
    def ok(self) -> bool:
        return self.added > 0 and not self.errors

    def to_dict(self) -> dict:
        return {
            "category":    self.category,
            "added":       self.added,
            "skipped":     self.skipped,
            "errors":      self.errors,
            "duration_s":  self.duration_s,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "saved_to":    self.saved_to,
            "ok":          self.ok,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ ТАСК
# ══════════════════════════════════════════════════════════════════════════════

class TLEUpdateTask:
    """
    Фоновый asyncio-таск периодического обновления TLE.

    Связывает вместе tle_fetcher → tle_parser → SatelliteDB → PassesCache.
    """

    def __init__(
        self,
        db,                              # SatelliteDB
        interval_h:    int   = 24,
        categories:    Optional[list[str]] = None,
        backup_dir:    Optional[str]       = None,
        clear_on_update: bool              = False,
        invalidate_passes_cache: bool      = True,
    ) -> None:
        self._db            = db
        self._interval_h    = interval_h
        self._categories    = categories or []
        self._backup_dir    = backup_dir
        self._clear_on_upd  = clear_on_update
        self._inv_cache     = invalidate_passes_cache

        self._task:         Optional[asyncio.Task] = None
        self._running       = False
        self._last_run:     Optional[str] = None
        self._results:      list[UpdateResult] = []
        self._total_added   = 0
        self._total_errors  = 0
        self._run_count     = 0

    # ── запуск / остановка ────────────────────────────────────────────────────

    def start(
        self,
        categories:  Optional[list[str]] = None,
        interval_h:  Optional[int]       = None,
    ) -> bool:
        """
        Запускает фоновый таск.
        Returns: True если запущен, False если уже работал.
        """
        if self._running:
            return False

        from services.tle_fetcher import CELESTRAK_URLS, unknown_categories
        cats = categories or self._categories or list(CELESTRAK_URLS.keys())
        bad  = unknown_categories(cats)
        if bad:
            raise ValueError(f"Неизвестные категории: {bad}")

        self._categories = cats
        if interval_h is not None:
            self._interval_h = interval_h

        self._running = True
        self._task    = asyncio.create_task(self._loop())
        print(f"[TLEUpdate] запущен: {len(self._categories)} категорий, "
            f"интервал {self._interval_h}ч")
        return True

    def stop(self) -> bool:
        """Returns: True если был запущен."""
        if not self._running:
            return False
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        print("[TLEUpdate] остановлен")
        return True

    def reconfigure(
        self,
        categories:  Optional[list[str]] = None,
        interval_h:  Optional[int]       = None,
    ) -> None:
        """Меняет настройки (применятся в следующем цикле)."""
        if categories:
            self._categories = categories
        if interval_h is not None:
            self._interval_h = interval_h

    # ── один цикл обновления ──────────────────────────────────────────────────

    async def run_once(
        self,
        categories: Optional[list[str]] = None,
    ) -> list[UpdateResult]:
        """
        Выполняет один цикл обновления вне расписания.
        Удобно для ручного запуска через POST /tle/auto-update/start.

        Returns:
            Список UpdateResult по каждой категории.
        """
        cats    = categories or self._categories
        results = []
        for cat in cats:
            result = await self._update_category(cat)
            results.append(result)
        self._last_run = _now_iso()
        self._run_count += 1
        return results

    async def _update_category(self, category: str) -> UpdateResult:
        """Полный цикл обновления одной категории."""
        import time as _time
        from services.tle_fetcher import fetch_celestrak

        result = UpdateResult(category)
        t_start = _time.monotonic()

        # 1. Скачать
        fetch_result = await fetch_celestrak(category, timeout=25.0, retries=2)
        if not fetch_result.ok:
            result.errors.append(f"fetch failed: {fetch_result.error}")
            result.finish(_time.monotonic() - t_start)
            self._total_errors += 1
            return result

        # 2. Распарсить
        try:
            from core.tle_parser import parse_tle_text, load_records_into_db
            records, skipped_parse = parse_tle_text(
                fetch_result.content,
                source=category,
                operator=category.upper(),
            )
        except ImportError:
            # Fallback: используем inline-реализации из main.py
            records, skipped_parse = _fallback_parse(
                fetch_result.content, category
            )

        if not records:
            result.errors.append("no valid TLE records parsed")
            result.finish(_time.monotonic() - t_start)
            return result

        # 3. Загрузить в БД
        if self._clear_on_upd:
            self._db.clear()

        try:
            from core.tle_parser import load_records_into_db
            added, skipped_db = load_records_into_db(records, self._db)
        except ImportError:
            added, skipped_db = _fallback_load(records, self._db)

        result.added   = added
        result.skipped = skipped_parse + skipped_db

        # 4. Сохранить на диск (резервная копия)
        if self._backup_dir and fetch_result.content:
            saved = await self._save_backup(
                category, fetch_result.content
            )
            result.saved_to = saved

        # 5. Инвалидировать кэш пролётов
        if self._inv_cache and added > 0:
            try:
                from tasks.precompute_passes import get_passes_cache
                get_passes_cache().invalidate()
            except Exception:
                pass

        result.finish(_time.monotonic() - t_start)
        self._total_added  += added
        self._results.append(result)
        if len(self._results) > 100:
            self._results.pop(0)

        print(f"[TLEUpdate] {category}: +{added} skip={result.skipped} "
            f"({result.duration_s}с)")
        return result

    async def _save_backup(self, category: str, content: str) -> Optional[str]:
        """Сохраняет TLE-контент на диск как резервную копию."""
        try:
            backup_dir = Path(self._backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts_str  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            fname   = backup_dir / f"{category}_{ts_str}.txt"
            fname.write_text(content, encoding="utf-8")
            return str(fname)
        except Exception as e:
            print(f"[TLEUpdate] backup failed for {category}: {e}")
            return None

    # ── фоновый цикл ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            await self.run_once()
            self._last_run = _now_iso()
            self._run_count += 1
            await asyncio.sleep(self._interval_h * 3600)

    # ── статистика и статус ───────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def recent_results(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in self._results[-limit:]]

    @property
    def status(self) -> dict:
        """Совместим с API старого _AutoUpdater.status."""
        return {
            "running":    self._running,
            "interval_h": self._interval_h,
            "last_run":   self._last_run,
            "categories": self._categories,
            "runs":       self._run_count,
            "total_added":  self._total_added,
            "total_errors": self._total_errors,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_task: Optional[TLEUpdateTask] = None


def get_update_task(
    db=None,
    interval_h:    int   = 24,
    backup_dir:    Optional[str] = None,
    clear_on_update: bool = False,
) -> TLEUpdateTask:
    """
    Глобальный singleton TLEUpdateTask.

    Использование в main.py (lifespan):
        from tasks.update_tle import get_update_task
        task = get_update_task(db=db, interval_h=24)
        task.start(categories=["stations", "active"])
        yield
        task.stop()
    """
    global _default_task
    if _default_task is None:
        if db is None:
            raise ValueError("db обязателен при первом вызове get_update_task()")
        _default_task = TLEUpdateTask(
            db=db,
            interval_h=interval_h,
            backup_dir=backup_dir,
            clear_on_update=clear_on_update,
        )
    return _default_task


# ══════════════════════════════════════════════════════════════════════════════
#  FALLBACK РЕАЛИЗАЦИИ (если core/ не установлены)
# ══════════════════════════════════════════════════════════════════════════════

def _fallback_parse(content: str, operator: str) -> tuple[list, int]:
    """Простой 3LE-парсер без core/tle_parser.py."""
    lines   = [l.strip() for l in content.splitlines() if l.strip()]
    records = []
    skipped = 0
    i       = 0
    while i < len(lines):
        if (i + 2 < len(lines)
                and not lines[i].startswith("1 ")
                and lines[i+1].startswith("1 ")
                and lines[i+2].startswith("2 ")):
            records.append({
                "name": lines[i], "line1": lines[i+1], "line2": lines[i+2],
                "operator": operator, "orbit_type": None,
            })
            i += 3
        elif (i + 1 < len(lines)
            and lines[i].startswith("1 ")
            and lines[i+1].startswith("2 ")):
            records.append({
                "name": f"NORAD-{lines[i][2:7].strip()}",
                "line1": lines[i], "line2": lines[i+1],
                "operator": operator, "orbit_type": None,
            })
            i += 2
        else:
            skipped += 1; i += 1
    return records, skipped


def _fallback_load(records: list, db) -> tuple[int, int]:
    """Загрузка без core/tle_parser.py — через db.add напрямую."""
    import math
    added = skipped = 0
    for rec in records:
        name = rec.get("name", ""); l1 = rec["line1"]; l2 = rec["line2"]
        sat_type = "Unknown"
        try:
            mm  = float(l2[52:63]); per = 1440.0 / mm
            a   = (398600.4418 * (per*60/(2*math.pi))**2)**(1/3)
            alt = a*(1+float("0."+l2[26:33])) - 6371.0
            sat_type = ("LEO" if alt < 2000 else "MEO" if alt < 35786
                        else "GEO" if alt < 42164*1.05 else "HEO")
        except Exception:
            pass
        sid = db.add(name, l1, l2, rec.get("operator","Unknown"), sat_type)
        if sid: added += 1
        else:   skipped += 1
    return added, skipped


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_update_in_s(last_run_iso: Optional[str],
                    interval_h: int) -> Optional[float]:
    """
    Секунд до следующего обновления. None если last_run неизвестен.
    """
    if not last_run_iso:
        return None
    try:
        last = datetime.fromisoformat(last_run_iso.replace("Z", "+00:00"))
        from datetime import timedelta
        next_run = last + timedelta(hours=interval_h)
        delta    = (next_run - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, round(delta, 1))
    except Exception:
        return None