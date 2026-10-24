"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("fingerprint", sa.String(255), nullable=False, index=True),
        sa.Column("family_hint", sa.String(64)),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("workload", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("severity_level", sa.String(32)),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("summary", sa.Text),
        sa.Column("handoff", JSONB),
        sa.Column("signal_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("signal_reasons", JSONB, nullable=False, server_default="[]"),
        sa.Column("status_reason", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("closed_at", TS),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("target", sa.String(255)),
        sa.Column("collected_at", TS, nullable=False),
        sa.Column("payload", JSONB),
        sa.Column("error", sa.Text),
    )
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("workflow", sa.String(64), nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("decided_by", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("reasoning_effort", sa.String(16)),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("estimated_cost_usd", sa.Float),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("output", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("doc", sa.String(255), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("chunk_ix", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("embedding", Vector(384)),
    )
    op.create_table(
        "citations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("agent_run_id", sa.BigInteger, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("chunk_id", sa.BigInteger, sa.ForeignKey("rag_chunks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
    )
    op.create_table(
        "actions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("params", JSONB, nullable=False, server_default="{}"),
        sa.Column("policy_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("before_snapshot", JSONB),
        sa.Column("after_state", JSONB),
        sa.Column("reason", sa.Text),
        sa.Column("executed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index(
        "uq_actions_one_active_per_target", "actions", ["target"], unique=True,
        postgresql_where=sa.text("status IN ('executing','approved')"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ts", TS, nullable=False, index=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.BigInteger, index=True),
        sa.Column("detail", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )


def downgrade() -> None:
    for t in ["settings", "audit_log", "actions", "citations", "rag_chunks", "agent_runs",
              "routing_decisions", "evidence", "incidents"]:
        op.drop_table(t)
