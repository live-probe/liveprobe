"""P15: Korean interpretation cache, operator commands, steering intent (additive).

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interpretations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("item_key", sa.String(128), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("ko", sa.Text, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("agent_run_id", sa.BigInteger, sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_interpretations_item", "interpretations", ["incident_id", "item_key", "raw_sha256"], unique=True)
    op.create_table(
        "operator_commands",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("message_id", sa.BigInteger, nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("instruction", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("plan", postgresql.JSONB, nullable=True),
        sa.Column("plan_sha256", sa.String(64), nullable=True),
        sa.Column("dry_run", postgresql.JSONB, nullable=True),
        sa.Column("side_effects", postgresql.JSONB, nullable=True),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("verification", postgresql.JSONB, nullable=True),
        sa.Column("rollback", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("operator_messages", sa.Column("intent", sa.String(24), nullable=True))
    op.add_column("operator_messages", sa.Column("intent_detail", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("operator_messages", "intent_detail")
    op.drop_column("operator_messages", "intent")
    op.drop_table("operator_commands")
    op.drop_index("uq_interpretations_item", table_name="interpretations")
    op.drop_table("interpretations")
