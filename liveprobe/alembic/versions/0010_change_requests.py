"""P18 additive change requests and outward-polling runner jobs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('change_requests',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('incident_id', sa.BigInteger, sa.ForeignKey('incidents.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('system', sa.String(255), nullable=False),
        sa.Column('service', sa.String(255), nullable=False),
        sa.Column('namespace_origin', sa.String(128), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, index=True),
        sa.Column('base_image', sa.Text), sa.Column('base_commit', sa.String(64)),
        sa.Column('source_files', JSONB, nullable=False, server_default='[]'),
        sa.Column('diff', sa.Text), sa.Column('diff_sha256', sa.String(64)),
        sa.Column('patch_summary_ko', sa.Text),
        sa.Column('tests', JSONB, nullable=False, server_default='{}'),
        sa.Column('build', JSONB, nullable=False, server_default='{}'),
        sa.Column('stages', JSONB, nullable=False, server_default='{}'),
        sa.Column('error', sa.Text), sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('uq_changes_deploying_target', 'change_requests', ['service', 'status'], unique=True,
                    postgresql_where=sa.text("status IN ('dev_deploying','stg_deploying','prd_deploying')"))
    op.create_table('runner_jobs',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('change_id', sa.BigInteger, sa.ForeignKey('change_requests.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, index=True),
        sa.Column('payload', JSONB, nullable=False, server_default='{}'), sa.Column('result', JSONB),
        sa.Column('claimed_at', sa.DateTime(timezone=True)), sa.Column('finished_at', sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table('runner_jobs')
    op.drop_table('change_requests')
