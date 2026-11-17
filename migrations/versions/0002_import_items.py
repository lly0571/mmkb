"""Durable per-item import reservations and receipts.

Generated from the ImportItem ORM definition; reviewed to preserve all existing data.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_import_items"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "import_items",
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("planned_resource_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("storage_profile", postgresql.JSONB(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="reserved", nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("duplicate", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"), schema="mmkb",
        comment="逐项导入回执和原件定位；资源删除后保留幂等记录",
    )


def downgrade():
    op.drop_table("import_items", schema="mmkb")
