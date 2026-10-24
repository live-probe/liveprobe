"""KAN-135: workload-level action lock key (additive).

The existing ``uq_actions_one_active_per_target`` index serialises actions on the
same ``target`` string. Typed actions address different objects of one workload
(Pod vs. Deployment vs. ConfigMap), so a second partial unique index on a
workload-scoped ``lock_key`` prevents e.g. a pod delete racing a rollout restart.
Rows created before this revision keep ``lock_key`` NULL and are unaffected. An
``indeterminate`` action (write or compensation outcome unknown) keeps holding the
workload lock so no further automated action runs until a human resolves it.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("lock_key", sa.String(255), nullable=True))
    op.create_index("uq_actions_one_active_per_lock", "actions", ["lock_key"], unique=True,
                    postgresql_where=sa.text(
                        "status IN ('executing','approved','indeterminate') AND lock_key IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_actions_one_active_per_lock", table_name="actions")
    op.drop_column("actions", "lock_key")
