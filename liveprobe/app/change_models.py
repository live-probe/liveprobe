"""P18 additive change lifecycle tables, sharing the application Base."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, utcnow


class ChangeRequest(Base):
    __tablename__ = 'change_requests'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey('incidents.id', ondelete='CASCADE'), index=True)
    system: Mapped[str] = mapped_column(String(255))
    service: Mapped[str] = mapped_column(String(255))
    namespace_origin: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), index=True)
    base_image: Mapped[str | None] = mapped_column(Text)
    base_commit: Mapped[str | None] = mapped_column(String(64))
    source_files: Mapped[list] = mapped_column(JSONB, default=list)
    diff: Mapped[str | None] = mapped_column(Text)
    diff_sha256: Mapped[str | None] = mapped_column(String(64))
    patch_summary_ko: Mapped[str | None] = mapped_column(Text)
    tests: Mapped[dict] = mapped_column(JSONB, default=dict)
    build: Mapped[dict] = mapped_column(JSONB, default=dict)
    stages: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (Index('uq_changes_deploying_target', 'service', 'status', unique=True,
                           postgresql_where=text("status IN ('dev_deploying','stg_deploying','prd_deploying')")),)


class RunnerJob(Base):
    __tablename__ = 'runner_jobs'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey('change_requests.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
