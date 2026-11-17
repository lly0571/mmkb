"""Document the six workflows; no resource or index values are changed."""
from alembic import op
import sqlalchemy as sa

revision = "0003_image_routing"
down_revision = "0002_import_items"
branch_labels = None
depends_on = None

OLD_COMMENT = "处理路线：structured_image、visual_image、visdoc、text_image、text；未路由时为空"
NEW_COMMENT = "处理路线：structured_image、info_rich_image、visual_image、visdoc、text_image、text；未路由时为空"


def upgrade():
    op.alter_column("resources", "workflow", schema="mmkb", existing_type=sa.Text(),
                    comment=NEW_COMMENT, existing_comment=OLD_COMMENT)


def downgrade():
    op.alter_column("resources", "workflow", schema="mmkb", existing_type=sa.Text(),
                    comment=OLD_COMMENT, existing_comment=NEW_COMMENT)
