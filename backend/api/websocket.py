"""
websocket.py — WebSocket роутер для real-time стриминга позиций спутников.

Подключение в main.py:
    from websocket import router as ws_router
    app.include_router(ws_router, tags=["websocket"])
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from config import get_settings
from satellites import _coverage_polygon, _elev_az, _parse_time, _sat_position, db, ts
from passes import _find_passes_for_rec, _make_observer

cfg    = get_settings()
router = APIRouter()

# ══════════════════════════════════════════════════════════════════════════════
#  JSON ПРОТОКОЛ  клиент → сервер / сервер → клиент
# ══════════════════════════════════════════════════════════════════════════════
#
#  Входящие (action):
#   ping | subscribe | unsubscribe | set_filter | set_speed | seek
#   get_position | get_stats | set_observer
#
#  Исходящие (type):
#   connected | pong | subscribed | unsubscribed | positions | coverage
#   pass_alert | position | stats | filter_applied | speed_set | seeked
#   observer_set | heartbeat | error

# ══════════════════════════════════════════════════════════════════════════════
#  CLIENT STATE
# ══════════════════════════════════════════════════════════════════════════════

class ClientFilter:
    """Фильтр какие спутники слать конкретному клиенту."""

    def __init__(self) -> None:
        self.sat_type:  Optional[str]      = None
        self.operator:  Optional[str]      = None
        self.ids:       Optional[set[int]] = None
        self.limit:     int                = 100

    def apply(self) -> list[dict]:
        if self.ids:
            return [r for sid in self.ids if (r := db.get(sid))]
        brief = db.list(operator=self.operator, sat_type=self.sat_type)
        return [r for info in brief[:self.limit] if (r := db.get(info["id"]))]

    def update(self, params: dict) -> None:
        if "type"     in params: self.sat_type = params["type"] or None
        if "operator" in params: self.operator = params["operator"] or None
        if "ids"      in params:
            self.ids = set(int(i) for i in params["ids"]) if params["ids"] else None
        if "limit"    in params:
            self.limit = max(1, min(int(params["limit"]), cfg.BULK_POSITION_LIMIT))

    def as_dict(self) -> dict:
        return {
            "type":     self.sat_type,
            "operator": self.operator,
            "ids":      list(self.ids) if self.ids else None,
            "limit":    self.limit,
        }


class SimClock:
    """
    Симулятор времени. multiplier=1 → реальное время, multiplier=10 → ×10,
    multiplier=-1 → реверс. Каждый клиент имеет свой независимый clock.
    """

    def __init__(self) -> None:
        self._real_start  = time.monotonic()
        self._sim_start   = datetime.now(timezone.utc)
        self._multiplier  = 1.0

    def now(self):
        elapsed = time.monotonic() - self._real_start
        sim_dt  = self._sim_start + timedelta(seconds=elapsed * self._multiplier)
        return ts.utc(sim_dt.year, sim_dt.month, sim_dt.day,
                      sim_dt.hour, sim_dt.minute,
                      sim_dt.second + sim_dt.microsecond / 1e6)

    def seek(self, iso: str) -> None:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        self._real_start = time.monotonic()
        self._sim_start  = dt

    def set_speed(self, multiplier: float) -> None:
        current          = self.now().utc_datetime().replace(tzinfo=timezone.utc)
        self._real_start = time.monotonic()
        self._sim_start  = current
        self._multiplier = multiplier

    @property
    def multiplier(self) -> float:
        return self._multiplier

    @property
    def sim_iso(self) -> str:
        return self.now().utc_datetime().replace(
            tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClientSession:
    """Полное состояние одного подключённого клиента."""

    def __init__(self, ws: WebSocket, client_id: str) -> None:
        self.ws            = ws
        self.client_id     = client_id
        self.filter        = ClientFilter()
        self.clock         = SimClock()
        self.channels:     set[str]        = set()
        self.observer:     Optional[dict]  = None
        self.connected_at  = time.monotonic()
        self._lock         = asyncio.Lock()

    async def send(self, payload: dict) -> bool:
        try:
            async with self._lock:
                await self.ws.send_json(payload)
            return True
        except Exception:
            return False

    def uptime_s(self) -> float:
        return round(time.monotonic() - self.connected_at, 1)

# ══════════════════════════════════════════════════════════════════════════════
#  CONNECTION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self) -> None:
        self._sessions:  dict[str, ClientSession] = {}
        self._counter    = 0
        self._lock       = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> ClientSession:
        await ws.accept()
        async with self._lock:
            self._counter += 1
            cid     = f"client-{self._counter}"
            session = ClientSession(ws, cid)
            self._sessions[cid] = session
        print(f"[WS] +{cid} total={len(self._sessions)}")
        return session

    async def disconnect(self, session: ClientSession) -> None:
        async with self._lock:
            self._sessions.pop(session.client_id, None)
        print(f"[WS] -{session.client_id} total={len(self._sessions)}")

    def by_channel(self, ch: str) -> list[ClientSession]:
        return [s for s in self._sessions.values() if ch in s.channels]

    @property
    def count(self) -> int:
        return len(self._sessions)

    def stats(self) -> dict:
        ch_cnt: dict[str, int] = defaultdict(int)
        for s in self._sessions.values():
            for ch in s.channels:
                ch_cnt[ch] += 1
        return {"total_connections": self.count, "channels": dict(ch_cnt)}


manager = ConnectionManager()

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _broadcast_dead(dead: list[ClientSession]) -> None:
    for s in dead:
        await manager.disconnect(s)

# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND BROADCAST LOOPS
# ══════════════════════════════════════════════════════════════════════════════

async def _positions_loop() -> None:
    """Шлёт текущие позиции подписчикам канала 'positions'."""
    while True:
        dead = []
        for session in manager.by_channel("positions"):
            t    = session.clock.now()
            recs = session.filter.apply()
            data = []
            for rec in recs:
                pos = _sat_position(rec["satellite"], t)
                pos.update({
                    "id": rec["id"], "name": rec["name"],
                    "type": rec.get("type"), "operator": rec.get("operator"),
                })
                data.append(pos)
            ok = await session.send({
                "type":     "positions",
                "sim_time": session.clock.sim_iso,
                "speed":    session.clock.multiplier,
                "ts":       _now_iso(),
                "count":    len(data),
                "data":     data,
            })
            if not ok:
                dead.append(session)
        await _broadcast_dead(dead)
        await asyncio.sleep(cfg.WS_BROADCAST_INTERVAL_S)


async def _coverage_loop() -> None:
    """Шлёт зоны покрытия подписчикам канала 'coverage'."""
    while True:
        dead = []
        for session in manager.by_channel("coverage"):
            t    = session.clock.now()
            recs = session.filter.apply()[:50]
            data = []
            for rec in recs:
                pos = _sat_position(rec["satellite"], t)
                polygon, radius_km = _coverage_polygon(
                    pos["lat"], pos["lon"], pos["alt_km"], 0.0
                )
                data.append({
                    "id":        rec["id"],
                    "name":      rec["name"],
                    "center":    [pos["lon"], pos["lat"]],
                    "radius_km": radius_km,
                    "polygon":   polygon,
                })
            ok = await session.send({
                "type": "coverage",
                "ts":   _now_iso(),
                "data": data,
            })
            if not ok:
                dead.append(session)
        await _broadcast_dead(dead)
        await asyncio.sleep(cfg.WS_BROADCAST_INTERVAL_S * 2)


async def _pass_alert_loop() -> None:
    """Уведомляет за 5 минут до пролёта. Проверяет каждые 30 сек."""
    notified: set[str] = set()   # ключ: "{sat_id}:{aos_iso}"

    while True:
        dead = []
        t0   = ts.now()
        t1   = ts.utc(t0.utc_datetime() + timedelta(minutes=6))

        for session in manager.by_channel("pass_alerts"):
            if not session.observer:
                continue
            obs      = session.observer
            observer = _make_observer(obs["lat"], obs["lon"], obs.get("alt_m", 0.0))
            recs     = session.filter.apply()

            for rec in recs:
                passes = _find_passes_for_rec(
                    rec, observer, t0, t1, cfg.MIN_ELEVATION_DEG
                )
                for p in passes:
                    key = f"{p.sat_id}:{p.aos}"
                    if key in notified:
                        continue
                    try:
                        aos_dt  = datetime.fromisoformat(p.aos.replace("Z", "+00:00"))
                        now_dt  = datetime.now(timezone.utc)
                        delta_s = (aos_dt - now_dt).total_seconds()
                        if 0 <= delta_s <= 300:
                            ok = await session.send({
                                "type": "pass_alert",
                                "ts":   _now_iso(),
                                "data": {
                                    "sat_id":    p.sat_id,
                                    "sat_name":  p.sat_name,
                                    "aos":       p.aos,
                                    "max_el":    p.max_el,
                                    "duration_s": p.duration_s,
                                    "in_seconds": int(delta_s),
                                },
                            })
                            if not ok:
                                dead.append(session)
                            notified.add(key)
                    except Exception:
                        pass

        await _broadcast_dead(dead)

        # Чистим ключи старше 10 минут
        now_dt = datetime.now(timezone.utc)
        fresh: set[str] = set()
        for k in notified:
            try:
                aos_iso = k.split(":", 1)[1]        # всё после первого ":"
                aos_dt  = datetime.fromisoformat(aos_iso.replace("Z", "+00:00"))
                if (now_dt - aos_dt).total_seconds() < 600:
                    fresh.add(k)
            except Exception:
                pass
        notified = fresh

        await asyncio.sleep(30)


_tasks: list[asyncio.Task] = []


def _ensure_loops() -> None:
    if not _tasks:
        _tasks.append(asyncio.create_task(_positions_loop()))
        _tasks.append(asyncio.create_task(_coverage_loop()))
        _tasks.append(asyncio.create_task(_pass_alert_loop()))

# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════

_VALID_ACTIONS = [
    "ping", "subscribe", "unsubscribe", "set_filter",
    "set_speed", "seek", "get_position", "get_stats", "set_observer",
]

_VALID_CHANNELS = {"positions", "coverage", "pass_alerts"}


async def _handle(session: ClientSession, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await session.send({"type": "error", "message": "Invalid JSON"})
        return

    action  = msg.get("action", "")
    params  = msg.get("params", {})
    channel = msg.get("channel", "")

    if action == "ping":
        await session.send({"type": "pong", "ts": _now_iso(),
                            "uptime_s": session.uptime_s()})

    elif action == "subscribe":
        if channel not in _VALID_CHANNELS:
            await session.send({"type": "error",
                                "message": f"Unknown channel. Valid: {sorted(_VALID_CHANNELS)}"})
            return
        if channel == "pass_alerts":
            if "lat" not in params or "lon" not in params:
                await session.send({"type": "error",
                                    "message": "pass_alerts requires params.lat and params.lon"})
                return
            session.observer = {
                "lat":   float(params["lat"]),
                "lon":   float(params["lon"]),
                "alt_m": float(params.get("alt_m", 0.0)),
            }
        session.channels.add(channel)
        await session.send({"type": "subscribed", "channel": channel, "ts": _now_iso()})

    elif action == "unsubscribe":
        session.channels.discard(channel)
        await session.send({"type": "unsubscribed", "channel": channel, "ts": _now_iso()})

    elif action == "set_filter":
        session.filter.update(params)
        await session.send({"type": "filter_applied", "ts": _now_iso(),
                            "filter": session.filter.as_dict()})

    elif action == "set_speed":
        mult = float(params.get("multiplier", 1.0))
        if not -1000 <= mult <= 1000:
            await session.send({"type": "error",
                                "message": "multiplier must be in [-1000, 1000]"})
            return
        session.clock.set_speed(mult)
        await session.send({"type": "speed_set", "multiplier": mult,
                            "sim_time": session.clock.sim_iso, "ts": _now_iso()})

    elif action == "seek":
        at = params.get("at")
        if not at:
            await session.send({"type": "error",
                                "message": "seek requires params.at (UTC ISO)"})
            return
        try:
            session.clock.seek(at)
        except Exception as e:
            await session.send({"type": "error", "message": f"Invalid time: {e}"})
            return
        await session.send({"type": "seeked", "sim_time": session.clock.sim_iso,
                            "ts": _now_iso()})

    elif action == "get_position":
        sid = params.get("sat_id")
        if sid is None:
            await session.send({"type": "error",
                                "message": "get_position requires params.sat_id"})
            return
        rec = db.get(int(sid))
        if not rec:
            await session.send({"type": "error",
                                "message": f"Satellite {sid} not found"})
            return
        pos = _sat_position(rec["satellite"], session.clock.now())
        pos.update({"id": rec["id"], "name": rec["name"],
                    "type": rec.get("type"), "operator": rec.get("operator")})
        await session.send({"type": "position", "ts": _now_iso(), "data": pos})

    elif action == "set_observer":
        if "lat" not in params or "lon" not in params:
            await session.send({"type": "error",
                                "message": "set_observer requires params.lat and params.lon"})
            return
        session.observer = {
            "lat":   float(params["lat"]),
            "lon":   float(params["lon"]),
            "alt_m": float(params.get("alt_m", 0.0)),
        }
        await session.send({"type": "observer_set",
                            "observer": session.observer, "ts": _now_iso()})

    elif action == "get_stats":
        await session.send({
            "type": "stats",
            "ts":   _now_iso(),
            "data": {
                "satellites_in_db": db.count(),
                "your_channels":    sorted(session.channels),
                "your_filter":      session.filter.as_dict(),
                "sim_time":         session.clock.sim_iso,
                "speed":            session.clock.multiplier,
                "uptime_s":         session.uptime_s(),
                **manager.stats(),
            },
        })

    else:
        await session.send({"type": "error",
                            "message": f"Unknown action '{action}'",
                            "valid_actions": _VALID_ACTIONS})

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.websocket("/ws")
async def ws_main(websocket: WebSocket):
    """
    Главный WebSocket-эндпоинт с полным протоколом.

    Быстрый старт:
      → { "action": "subscribe", "channel": "positions" }
      ← { "type": "positions", "data": [...] }
    """
    _ensure_loops()
    session = await manager.connect(websocket)

    await session.send({
        "type":      "connected",
        "client_id": session.client_id,
        "ts":        _now_iso(),
        "satellites": db.count(),
        "hint":      'Send {"action":"subscribe","channel":"positions"} to start',
    })

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle(session, raw)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] {session.client_id} error: {e}")
    finally:
        await manager.disconnect(session)


@router.websocket("/ws/positions")
async def ws_positions_quick(
    websocket: WebSocket,
    type:     Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    limit:    int           = Query(100, ge=1, le=cfg.BULK_POSITION_LIMIT),
):
    """
    Упрощённый стриминг позиций через query string.
    ?type=LEO&limit=50
    """
    _ensure_loops()
    session = await manager.connect(websocket)
    session.filter.update({"type": type, "operator": operator, "limit": limit})
    session.channels.add("positions")

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if raw.strip() in ("ping", "{}"):
                    await session.send({"type": "pong", "ts": _now_iso()})
            except asyncio.TimeoutError:
                pass    # keep-alive, нормально
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(session)


@router.websocket("/ws/passes")
async def ws_pass_alerts(
    websocket: WebSocket,
    lat:    float         = Query(...),
    lon:    float         = Query(...),
    alt_m:  float         = Query(0.0),
    min_el: float         = Query(cfg.MIN_ELEVATION_DEG),
    type:   Optional[str] = Query(None),
):
    """
    Уведомления о пролётах над точкой.
    Шлёт pass_alert за 5 минут до AOS.
    ?lat=55.75&lon=37.62
    """
    _ensure_loops()
    session              = await manager.connect(websocket)
    session.observer     = {"lat": lat, "lon": lon, "alt_m": alt_m}
    session.filter.update({"type": type})
    session.channels.add("pass_alerts")

    await session.send({
        "type":    "connected",
        "channel": "pass_alerts",
        "observer": session.observer,
        "ts":      _now_iso(),
        "message": "Alerts arrive 5 min before each satellite pass",
    })

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                await _handle(session, raw)
            except asyncio.TimeoutError:
                await session.send({"type": "heartbeat", "ts": _now_iso()})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(session)


# ── HTTP stats ────────────────────────────────────────────────────────────────

@router.get("/ws/stats", summary="Статистика WebSocket-подключений")
async def ws_stats():
    return {
        "ts":                       _now_iso(),
        "broadcast_interval_s":    cfg.WS_BROADCAST_INTERVAL_S,
        "background_tasks_running": len(_tasks),
        **manager.stats(),
    }