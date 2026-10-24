"""Evaluation lock: while a campaign owns the fault namespace the live watcher/alert intake stays quiet.

Stored in the `settings` table so it is visible to the API process and the evaluation process; it carries an
expiry so a crashed campaign can never silence the live system for long.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import db
from .models import Setting

KEY = "evaluation_lock"
_cache: tuple[float, bool] = (0.0, False)


def set_lock(owner: str, ttl_s: int = 900) -> None:
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
    with db.session() as s:
        row = s.get(Setting, KEY)
        if row is None:
            s.add(Setting(key=KEY, value={"owner": owner, "until": until}))
        else:
            row.value = {"owner": owner, "until": until}


def clear_lock() -> None:
    with db.session() as s:
        row = s.get(Setting, KEY)
        if row is not None:
            row.value = {"owner": None, "until": datetime.now(timezone.utc).isoformat()}


def evaluation_lock_active() -> bool:
    """Cheap (5 s cache) check used on every incoming signal."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < 5.0:
        return _cache[1]
    active = False
    try:
        with db.session() as s:
            row = s.get(Setting, KEY)
            until = (row.value or {}).get("until") if row else None
            if until and (row.value or {}).get("owner"):
                active = datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001 - a DB blip must not silence live alerts
        active = False
    _cache = (now, active)
    return active
