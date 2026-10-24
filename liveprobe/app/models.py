"""SQLAlchemy models. Schema is created/managed by Alembic (alembic/versions)."""
from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, Computed, DateTime, Float, ForeignKey, Index, Integer, String, Text, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 384


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


INCIDENT_OPEN_STATES = ("open", "analyzing", "action_pending", "acting")
# "closed" (P17): an operator ended an incident the AI already stopped on. It records a decision; nothing ran.
INCIDENT_CLOSED_STATES = ("resolved", "handed_off", "failed", "closed")


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(255), index=True)
    family_hint: Mapped[str | None] = mapped_column(String(64))
    namespace: Mapped[str] = mapped_column(String(128))
    workload: Mapped[str] = mapped_column(String(255))
    system: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    severity_level: Mapped[str | None] = mapped_column(String(32))  # SIMPLE | COMPLEX | HIGH_RISK
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str | None] = mapped_column(Text)
    handoff: Mapped[dict | None] = mapped_column(JSONB)
    signal_count: Mapped[int] = mapped_column(Integer, default=1)
    signal_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    evidence: Mapped[list["Evidence"]] = relationship(back_populates="incident", order_by="Evidence.id")


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(255))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict | list | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(32))
    workflow: Mapped[str] = mapped_column(String(64))
    features: Mapped[dict] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)
    policy: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    decided_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # succeeded | failed
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RagDocument(Base):
    """One ingested document with its provenance (KAN-54, revision 0002)."""
    __tablename__ = "rag_documents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(255), unique=True)   # == rag_chunks.doc
    kind: Mapped[str] = mapped_column(String(32))                    # runbook | public_doc | past_incident | reference_guide
    source: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    source_uri: Mapped[str] = mapped_column(Text)                    # immutable document location
    license: Mapped[str] = mapped_column(String(64))
    license_url: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    version_hash: Mapped[str] = mapped_column(String(64))            # sha256 of the ingested file/text
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incident_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RagChunk(Base):
    __tablename__ = "rag_chunks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    doc: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64))
    chunk_ix: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    embedding = mapped_column(Vector(EMBED_DIM))
    # provenance/location (0002; NULL for chunks indexed before provenance existed)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("rag_documents.id", ondelete="SET NULL"), index=True)
    section: Mapped[str | None] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    locator: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict | None] = mapped_column(JSONB)   # immutable citation snapshot across document refresh
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # superseded; kept for citations
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True))


class Citation(Base):
    __tablename__ = "citations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("rag_chunks.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255))  # namespace/Kind/name
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    policy_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    before_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    # KAN-135 (migration 0005): workload-scoped lock, e.g. "liveprobe-target/workload/inventory".
    lock_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index(
            "uq_actions_one_active_per_target", "target", unique=True,
            postgresql_where=text("status IN ('executing','approved')"),
        ),
        Index(
            "uq_actions_one_active_per_lock", "lock_key", unique=True,
            postgresql_where=text("status IN ('executing','approved','indeterminate') AND lock_key IS NOT NULL"),
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class WorkflowRun(Base):
    """Durable workflow attempt; interrupted work is never retried blindly."""
    __tablename__ = "workflow_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IncidentEvent(Base):
    """Append-only SSE replay log; IDs are cursors, retained until explicit archival."""
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvent(Base):
    """Ordered feed of what the pipeline did for one incident (P5). No hidden reasoning is stored."""
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="liveprobe")
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperatorMessage(Base):
    __tablename__ = "operator_messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # P15 (0008): steering intent classified from the message (instruction|escalate|reroute|stop|cluster_change).
    intent: Mapped[str | None] = mapped_column(String(24))
    intent_detail: Mapped[dict | None] = mapped_column(JSONB)


class Interpretation(Base):
    """P15: Korean reading of one raw evidence/agent text, cached per raw-text hash. Display only."""
    __tablename__ = "interpretations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str] = mapped_column(String(128))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    ko: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))          # rules | llm
    agent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("uq_interpretations_item", "incident_id", "item_key", "raw_sha256", unique=True),)


class OperatorCommand(Base):
    """P15: an operator instruction the AI turns into a Kubernetes change (plan -> dry-run -> confirm -> run)."""
    __tablename__ = "operator_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    actor: Mapped[str] = mapped_column(String(64))
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    plan: Mapped[dict | None] = mapped_column(JSONB)
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    dry_run: Mapped[list | dict | None] = mapped_column(JSONB)
    side_effects: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    verification: Mapped[dict | None] = mapped_column(JSONB)
    rollback: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunControl(Base):
    __tablename__ = "run_control"
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running")   # running | paused | stopping
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewLedger(Base):
    """Why a human review was judged necessary, and what happened (P7). Data for the operator's later criteria."""
    __tablename__ = "review_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason_code: Mapped[str] = mapped_column(String(48), index=True)
    profile: Mapped[str] = mapped_column(String(24))
    level: Mapped[str | None] = mapped_column(String(16))
    workload: Mapped[str | None] = mapped_column(String(128))
    proposed: Mapped[dict] = mapped_column(JSONB, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String(32))
    rollback: Mapped[str | None] = mapped_column(String(32))
    operator_involved: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
