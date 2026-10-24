"""P5: run events, operator messages and run control.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False, server_default="liveprobe"),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_run_events_incident_seq", "run_events", ["incident_id", "seq"], unique=True)
    op.create_table(
        "operator_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "run_control",
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="running"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("run_control")
    op.drop_table("operator_messages")
    op.drop_index("uq_run_events_incident_seq", table_name="run_events")
    op.drop_table("run_events")
