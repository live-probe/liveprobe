"""P18 observed workload system (nullable; never inferred)."""
from alembic import op
import sqlalchemy as sa
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("incidents", sa.Column("system", sa.String(255), nullable=True))

def downgrade():
    op.drop_column("incidents", "system")
