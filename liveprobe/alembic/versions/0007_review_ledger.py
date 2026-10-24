"""P7: operator-review ledger (additive).

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", sa.BigInteger, sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason_code", sa.String(48), nullable=False, index=True),
        sa.Column("profile", sa.String(24), nullable=False),
        sa.Column("level", sa.String(16)),
        sa.Column("workload", sa.String(128)),
        sa.Column("proposed", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text, nullable=False, server_default=""),
        sa.Column("impact", sa.Text, nullable=False, server_default=""),
        sa.Column("outcome", sa.String(32)),
        sa.Column("rollback", sa.String(32)),
        sa.Column("operator_involved", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_table("review_ledger")
