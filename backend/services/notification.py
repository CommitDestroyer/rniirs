"""
services/notification.py — сервис уведомлений о пролётах спутников.

Инкапсулирует всю логику которая была размазана по websocket.py:
    - дедупликация уведомлений (один алерт на пролёт)
    - TTL-очистка истории уведомлений
    - форматирование payload pass_alert
    - определение попадания в окно оповещения

Использование в websocket.py:
    from services.notification import get_alert_service

    _svc = get_alert_service()

    # Вместо inline-логики в _pass_alert_loop:
    alerts = _svc.collect(passes)
    for alert in alerts:
        await session.send(alert.to_ws_payload())
    _svc.purge_expired()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
#  СТРУКТУРЫ ДАННЫХ
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PassAlert:
    """Одно уведомление о предстоящем пролёте."""
    sat_id:     int
    sat_name:   str
    aos:        str    # UTC ISO
    max_el:     float  # градусы
    duration_s: int    # секунды
    in_seconds: int    # секунд до AOS

    def to_ws_payload(self) -> dict:
        """WebSocket-сообщение в формате нашего протокола."""
        return {
            "type": "pass_alert",
            "ts":   _now_iso(),
            "data": {
                "sat_id":     self.sat_id,
                "sat_name":   self.sat_name,
                "aos":        self.aos,
                "max_el":     self.max_el,
                "duration_s": self.duration_s,
                "in_seconds": self.in_seconds,
            },
        }

    @property
    def key(self) -> str:
        """Ключ дедупликации: sat_id:aos_iso."""
        return f"{self.sat_id}:{self.aos}"

    @property
    def is_urgent(self) -> bool:
        """True если пролёт начинается менее чем через 60 секунд."""
        return self.in_seconds <= 60

    @property
    def quality_label(self) -> str:
        """Оценка качества пролёта по максимальной элевации."""
        if self.max_el >= 60: return "excellent"
        if self.max_el >= 30: return "good"
        if self.max_el >= 10: return "fair"
        return "low"


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ СЕРВИС
# ══════════════════════════════════════════════════════════════════════════════

class PassAlertService:
    """
    Сервис уведомлений о предстоящих пролётах.

    Отвечает за:
    - Дедупликацию: один пролёт → одно уведомление
    - Окно оповещения: [0, window_s] секунд до AOS
    - TTL-очистку истории: удаляет записи старше ttl_s секунд
    """

    def __init__(
        self,
        window_s:          int   = 300,
        ttl_s:             int   = 600,
        check_interval_s:  int   = 30,
        min_elevation_deg: float = 5.0,
    ) -> None:
        self.window_s          = window_s
        self.ttl_s             = ttl_s
        self.check_interval_s  = check_interval_s
        self.min_elevation_deg = min_elevation_deg
        self._notified: set[str] = set()
        self._lock = asyncio.Lock()

    # ── публичный API ─────────────────────────────────────────────────────────

    def collect(self, passes: list) -> list[PassAlert]:
        """
        Фильтрует список пролётов и возвращает те, о которых нужно уведомить.

        Args:
            passes: список объектов с атрибутами sat_id, sat_name, aos, max_el, duration_s

        Returns:
            Список PassAlert — только новые, попавшие в окно оповещения.
        """
        now_dt = datetime.now(timezone.utc)
        alerts: list[PassAlert] = []

        for p in passes:
            key = f"{p.sat_id}:{p.aos}"
            if key in self._notified:
                continue

            delta = _seconds_until(p.aos, now_dt)
            if delta is None or not (0 <= delta <= self.window_s):
                continue

            alerts.append(PassAlert(
                sat_id=p.sat_id,
                sat_name=p.sat_name,
                aos=p.aos,
                max_el=p.max_el,
                duration_s=p.duration_s,
                in_seconds=int(delta),
            ))
            self._notified.add(key)

        return alerts

    def already_notified(self, sat_id: int, aos: str) -> bool:
        """Проверяет был ли уже отправлен алерт для этого пролёта."""
        return f"{sat_id}:{aos}" in self._notified

    def mark_sent(self, sat_id: int, aos: str) -> None:
        """Явно помечает пролёт как уведомлённый."""
        self._notified.add(f"{sat_id}:{aos}")

    def purge_expired(self) -> int:
        """
        Удаляет истёкшие записи из истории уведомлений.
        Вызывать после каждого цикла _pass_alert_loop.

        Returns:
            Количество удалённых записей.
        """
        now_dt = datetime.now(timezone.utc)
        fresh: set[str] = set()

        for k in self._notified:
            try:
                aos_iso = k.split(":", 1)[1]
                aos_dt  = datetime.fromisoformat(aos_iso.replace("Z", "+00:00"))
                if (now_dt - aos_dt).total_seconds() < self.ttl_s:
                    fresh.add(k)
            except Exception:
                pass

        removed = len(self._notified) - len(fresh)
        self._notified = fresh
        return removed

    def reset(self) -> None:
        """Полная очистка истории уведомлений."""
        self._notified.clear()

    @property
    def pending_count(self) -> int:
        return len(self._notified)

    def stats(self) -> dict:
        return {
            "window_s":         self.window_s,
            "ttl_s":            self.ttl_s,
            "check_interval_s": self.check_interval_s,
            "notified_count":   self.pending_count,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  ГЛОБАЛЬНЫЙ SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_service: Optional[PassAlertService] = None


def get_alert_service(
    window_s:         int   = 300,
    ttl_s:            int   = 600,
    check_interval_s: int   = 30,
    min_el:           float = 5.0,
) -> PassAlertService:
    """
    Возвращает глобальный singleton PassAlertService.
    При первом вызове создаёт экземпляр с переданными параметрами.

    Использование в websocket.py:
        from services.notification import get_alert_service
        _svc = get_alert_service()
    """
    global _default_service
    if _default_service is None:
        _default_service = PassAlertService(
            window_s=window_s, ttl_s=ttl_s,
            check_interval_s=check_interval_s,
            min_elevation_deg=min_el,
        )
    return _default_service


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_until(aos_iso: str,
                now_dt: Optional[datetime] = None) -> Optional[float]:
    """Секунд до AOS. Отрицательное — уже начался. None — ошибка парсинга."""
    try:
        aos_dt = datetime.fromisoformat(aos_iso.replace("Z", "+00:00"))
        return (aos_dt - (now_dt or datetime.now(timezone.utc))).total_seconds()
    except Exception:
        return None


def in_alert_window(aos_iso: str, window_s: int = 300) -> tuple[bool, int]:
    """
    Проверяет попадает ли AOS в окно оповещения.

    Returns:
        (in_window, seconds_until_aos)
    """
    delta = _seconds_until(aos_iso)
    if delta is None:
        return False, 0
    return 0 <= delta <= window_s, int(delta)


def format_alert_message(alert: PassAlert) -> str:
    """
    Человекочитаемое описание алерта для логов.

    Пример: "ISS (ZARYA) → AOS через 4 мин 32 с, макс. элевация 45.2° [good]"
    """
    mins, secs = divmod(alert.in_seconds, 60)
    time_str   = f"{mins} мин {secs} с" if mins else f"{secs} с"
    return (f"{alert.sat_name} → AOS через {time_str}, "
            f"макс. элевация {alert.max_el}° [{alert.quality_label}]")