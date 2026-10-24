"""Engine/session management and Alembic migration entrypoint."""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .redact import redact_obj
from .models import AuditLog

_engine: Engine | None = None
_Session: sessionmaker | None = None

ROOT = Path(__file__).resolve().parent.parent


def init_db(url: str) -> Engine:
    global _engine, _Session
    _engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=5)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> Engine:
    assert _engine is not None, "init_db() not called"
    return _engine


@contextlib.contextmanager
def session() -> Iterator[Session]:
    assert _Session is not None, "init_db() not called"
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["sqlalchemy_url"] = url
    # Serialize Alembic across restarts/rollouts, including another process on the same DB.
    # Hold a dedicated session-level advisory lock until Alembic's separate connection finishes.
    with create_engine(url).connect() as lock:
        lock.execute(text("SELECT pg_advisory_lock(48949)"))
        try:
            command.upgrade(cfg, "head")
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(48949)"))


def db_ok() -> bool:
    try:
        with engine().connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False


def audit(s: Session, actor: str, event: str, incident_id: int | None = None, **detail) -> AuditLog:
    row = AuditLog(actor=actor, event=event, incident_id=incident_id, detail=redact_obj(detail))
    s.add(row)
    s.flush()
    return row
