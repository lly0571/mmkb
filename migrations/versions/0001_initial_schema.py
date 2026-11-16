"""Initial three-table schema generated from SQLAlchemy ORM with Alembic.

PostgreSQL must already have the pgvector extension enabled in public.
Historical migrations keep their generated definitions independent of current models.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import HALFVEC

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.schema.CreateSchema("mmkb"))
    # Generated from ORM metadata with Alembic; reviewed before application.
    op.create_table('storage_profiles',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='存储配置 ID'),
        sa.Column('name', sa.Text(), nullable=False, comment='存储配置唯一名称，如 local-default 或 s3-default'),
        sa.Column('backend', sa.Text(), nullable=False, comment='存储后端；应用允许 local 或 s3'),
        sa.Column('local_root', sa.Text(), nullable=True, comment='本地后端的绝对根目录；S3 配置为空'),
        sa.Column('endpoint_url', sa.Text(), nullable=True, comment='S3 兼容 API 地址；本地配置为空'),
        sa.Column('bucket', sa.Text(), nullable=True, comment='S3 Bucket 名称；本地配置为空'),
        sa.Column('credential_ref', sa.Text(), nullable=True, comment='服务端凭证配置名称，不存密钥值；无需凭证引用时为空'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
        schema='mmkb',
        comment='本地目录或 S3 的公共存储配置；具体文件位置保存在资源表'
    )
    op.create_table('resources',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='图文资料 ID，同时用于生成该资料独有的文件 key'),
        sa.Column('input_kind', sa.Text(), nullable=False, comment='原始输入形式；应用允许 image、text_image、text'),
        sa.Column('workflow', sa.Text(), nullable=True, comment='处理路线：structured_image、visual_image、visdoc、text_image、text；未路由时为空'),
        sa.Column('title', sa.Text(), server_default='', nullable=False, comment='资料展示标题，未设置时为空串'),
        sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False, comment='资料分类和标签'),
        sa.Column('user_text', sa.Text(), nullable=True, comment='用户原文或图片配文；仅图片输入时为空'),
        sa.Column('caption', sa.Text(), nullable=True, comment='图片描述，可由模型生成或用户修正；'),
        sa.Column('ocr_text', sa.Text(), nullable=True, comment='OCR 识别全文，可由用户修正；未识别时为空'),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False, comment='附加元数据，如宽高（像素）、大小（字节）、MIME、原文件名、来源和解析警告'),
        sa.Column('storage_profile_id', sa.Uuid(), nullable=True, comment='图片使用的存储配置 ID；纯文本为空，有引用时禁止删除对应配置'),
        sa.Column('object_key', sa.Text(), nullable=True, comment='根目录或 Bucket 内的相对文件 key；纯文本为空，不保存签名 URL'),
        sa.Column('sha256', sa.Text(), nullable=True, comment='文件内容 SHA-256，应用写入 64 位十六进制；用于校验和去重'),
        sa.Column('status', sa.Text(), server_default='pending', nullable=False, comment='处理状态：pending、processing、ready、failed；状态流转由应用管理'),
        sa.Column('error', sa.Text(), nullable=True, comment='最近一次处理或文件清理失败原因；无错误时为空'),
        sa.Column('revision', sa.Integer(), server_default='1', nullable=False, comment='编辑时由应用递增的计数，供 Worker 拒绝过期结果；不保存历史版本'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='资料创建时间，带时区'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='资料最后更新时间，带时区；后续修改由应用显式更新'),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True, comment='逻辑删除时间，带时区；为空表示未删除，非空资料等待文件清理'),
        sa.ForeignKeyConstraint(['storage_profile_id'], ['mmkb.storage_profiles.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_profile_id', 'object_key', name='resource_file_unique'),
        schema='mmkb',
        comment='图片、图文和纯文本资料的元数据、文件位置及处理状态'
    )
    op.create_index('resource_pending', 'resources', ['status', 'created_at'], unique=False, schema='mmkb', postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('resource_sha256', 'resources', ['sha256'], unique=False, schema='mmkb')
    op.create_table('resource_indexes',
        sa.Column('resource_id', sa.Uuid(), nullable=False, comment='资料 ID，主键兼外键，保证每份资料最多一条索引；物理删除资料时级联删除'),
        sa.Column('embedding', HALFVEC(2048), nullable=False, comment='2048 维半精度多模态向量；编码模型由部署配置指定'),
        sa.Column('segmented_text', sa.Text(), server_default='', nullable=False, comment='标题、标签、用户文字、OCR 和 Caption 经 Jieba 分词后以空格连接的辅助检索文本'),
        sa.Column('search_tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple'::regconfig, segmented_text)", persisted=True), nullable=False, comment='PostgreSQL 由 segmented_text 按 simple 配置生成的 tsvector；'),
        sa.Column('indexed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='本次索引写入时间，带时区；替换索引内容时由应用更新'),
        sa.ForeignKeyConstraint(['resource_id'], ['mmkb.resources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('resource_id'),
        schema='mmkb',
        comment='每份资料的唯一多模态向量和辅助分词检索字段；不分块'
    )
    op.create_index('resource_embedding_hnsw', 'resource_indexes', ['embedding'], unique=False, schema='mmkb', postgresql_using='hnsw', postgresql_ops={'embedding': 'halfvec_cosine_ops'})


def downgrade():
    # Generated from ORM metadata with Alembic; reviewed before application.
    op.drop_index('resource_embedding_hnsw', table_name='resource_indexes', schema='mmkb', postgresql_using='hnsw', postgresql_ops={'embedding': 'halfvec_cosine_ops'})
    op.drop_table('resource_indexes', schema='mmkb')
    op.drop_index('resource_sha256', table_name='resources', schema='mmkb')
    op.drop_index('resource_pending', table_name='resources', schema='mmkb', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('resources', schema='mmkb')
    op.drop_table('storage_profiles', schema='mmkb')
    op.execute(sa.schema.DropSchema("mmkb"))
