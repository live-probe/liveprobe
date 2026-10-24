"""Durable workflow attempts and replayable incident events.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False, server_default="{}"),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workflow_runs_incident_id", "workflow_runs", ["incident_id"])
    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incident_events_incident_id", "incident_events", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_incident_events_incident_id", table_name="incident_events")
    op.drop_table("incident_events")
    op.drop_index("ix_workflow_runs_incident_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
