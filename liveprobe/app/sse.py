"""In-process pub/sub for Server-Sent Events. Thread-safe publish from worker threads.

Contract (KAN-34-api-contract.md): SSE event name `incident`, data
`{"incident_id": int, "status": str, "event": str}`,
event in created | updated | agent_run | action | resolved | handed_off | failed | closed.
"""
from __future__ import annotations

import asyncio
import json
import threading

from .models import IncidentEvent, Incident
from . import db
from sqlalchemy import delete, select

EVENTS = ("created", "updated", "agent_run", "action", "resolved", "handed_off", "failed", "closed")


class Broker:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def _deliver(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def publish(self, incident_id: int, event: str = "updated", status: str | None = None) -> None:
        """`status` may be None; the stream fills in the current status before sending."""
        msg = {"incident_id": int(incident_id), "status": status, "event": event if event in EVENTS else "updated"}
        # Serialize writes in this single-replica deployment so cursor order follows commit order.
        with self._lock:
            with db.session() as s:
                if msg["status"] is None:
                    inc = s.get(Incident, incident_id)
                    msg["status"] = inc.status if inc else None
                row = IncidentEvent(**msg)
                s.add(row)
                s.flush()
                if row.id % 100 == 0:
                    s.execute(delete(IncidentEvent).where(IncidentEvent.id < row.id - 10000))
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(msg)
        else:
            loop.call_soon_threadsafe(self._deliver, msg)

    @staticmethod
    def encode(msg: dict) -> str:
        return json.dumps(msg)


broker = Broker()
