"""RAG provenance + hybrid (lexical) search columns (KAN-54)

Additive only: a new rag_documents table and nullable columns / a generated tsvector on rag_chunks.
Existing chunks and citations stay valid (document_id NULL = indexed before provenance existed).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "rag_documents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("doc_key", sa.String(255), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("source_uri", sa.Text, nullable=False),
        sa.Column("license", sa.String(64), nullable=False),
        sa.Column("license_url", sa.Text),
        sa.Column("attribution", sa.Text),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("retrieved_at", TS),
        sa.Column("incident_id", sa.BigInteger, index=True),
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
        sa.Column("ingested_at", TS, nullable=False),
    )
    op.add_column("rag_chunks", sa.Column(
        "document_id", sa.BigInteger, sa.ForeignKey("rag_documents.id", ondelete="SET NULL")))
    op.create_index("ix_rag_chunks_document_id", "rag_chunks", ["document_id"])
    for name, typ in (("section", sa.Text), ("char_start", sa.Integer), ("char_end", sa.Integer),
                      ("line_start", sa.Integer), ("line_end", sa.Integer), ("locator", sa.Text),
                      ("retired_at", TS)):
        op.add_column("rag_chunks", sa.Column(name, typ))
    op.add_column("rag_chunks", sa.Column("provenance", JSONB))
    op.add_column("rag_chunks", sa.Column(
        "tsv", TSVECTOR, sa.Computed("to_tsvector('english', content)", persisted=True)))
    op.create_index("ix_rag_chunks_tsv", "rag_chunks", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_rag_chunks_tsv", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_document_id", table_name="rag_chunks")
    for name in ("tsv", "provenance", "retired_at", "locator", "line_end", "line_start", "char_end", "char_start", "section",
                 "document_id"):
        op.drop_column("rag_chunks", name)
    op.drop_table("rag_documents")
