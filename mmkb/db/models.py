"""MMKB resources, retrieval rows, storage profiles and durable import receipts."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from mmkb.config import EMBEDDING_DIMENSIONS


class Base(DeclarativeBase):
    metadata = sa.MetaData(schema="mmkb")


class ImportItem(Base):
    """A durable reservation; intentionally survives physical resource deletion."""
    __tablename__ = "import_items"
    __table_args__ = {"comment": "逐项导入回执和原件定位；资源删除后保留幂等记录"}

    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_hash: Mapped[str] = mapped_column(sa.Text)
    sha256: Mapped[str | None] = mapped_column(sa.Text)
    planned_resource_id: Mapped[UUID] = mapped_column()
    # No resource FK: deleting a resource must never allow its request to be replayed as new.
    resource_id: Mapped[UUID | None] = mapped_column()
    storage_profile: Mapped[dict[str, Any] | None] = mapped_column(pg.JSONB)
    object_key: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[str] = mapped_column(sa.Text, server_default="reserved")
    outcome: Mapped[str | None] = mapped_column(sa.Text)
    duplicate: Mapped[bool] = mapped_column(server_default=sa.false())
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


class StorageProfile(Base):
    """Shared storage settings. Credentials are resolved by name outside the DB."""
    __tablename__ = "storage_profiles"
    __table_args__ = {"comment": "本地目录或 S3 的公共存储配置；具体文件位置保存在资源表"}

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()"),
        comment="存储配置 ID",
    )
    name: Mapped[str] = mapped_column(
        sa.Text, unique=True, comment="存储配置唯一名称，如 local-default 或 s3-default",
    )
    backend: Mapped[str] = mapped_column(
        sa.Text, comment="存储后端；应用允许 local 或 s3",
    )
    local_root: Mapped[str | None] = mapped_column(
        sa.Text, comment="本地后端的绝对根目录；S3 配置为空",
    )
    endpoint_url: Mapped[str | None] = mapped_column(
        sa.Text, comment="S3 兼容 API 地址；本地配置为空",
    )
    bucket: Mapped[str | None] = mapped_column(
        sa.Text, comment="S3 Bucket 名称；本地配置为空",
    )
    credential_ref: Mapped[str | None] = mapped_column(
        sa.Text, comment="服务端凭证配置名称，不存密钥值；无需凭证引用时为空",
    )

    resources: Mapped[list[Resource]] = relationship(
        back_populates="storage_profile", passive_deletes="all",
    )


class Resource(Base):
    """Image/text metadata and a small, restartable processing state."""
    __tablename__ = "resources"

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()"),
        comment="图文资料 ID，同时用于生成该资料独有的文件 key",
    )
    input_kind: Mapped[str] = mapped_column(
        sa.Text, comment="原始输入形式；应用允许 image、text_image、text",
    )
    workflow: Mapped[str | None] = mapped_column(
        sa.Text,
        comment="处理路线：structured_image、visual_image、visdoc、text_image、text；未路由时为空",
    )
    title: Mapped[str] = mapped_column(
        sa.Text, server_default="", comment="资料展示标题，未设置时为空串",
    )
    tags: Mapped[list[str]] = mapped_column(
        pg.ARRAY(sa.Text), server_default=sa.text("'{}'::text[]"),
        comment="资料分类和标签",
    )
    user_text: Mapped[str | None] = mapped_column(
        sa.Text, comment="用户原文或图片配文；仅图片输入时为空",
    )
    caption: Mapped[str | None] = mapped_column(
        sa.Text, comment="图片描述，可由模型生成或用户修正；",
    )
    ocr_text: Mapped[str | None] = mapped_column(
        sa.Text, comment="OCR 识别全文，可由用户修正；未识别时为空",
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        pg.JSONB, server_default=sa.text("'{}'::jsonb"),
        comment="附加元数据，如宽高（像素）、大小（字节）、MIME、原文件名、来源和解析警告",
    )
    storage_profile_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("mmkb.storage_profiles.id", ondelete="RESTRICT"),
        comment="图片使用的存储配置 ID；纯文本为空，有引用时禁止删除对应配置",
    )
    object_key: Mapped[str | None] = mapped_column(
        sa.Text, comment="根目录或 Bucket 内的相对文件 key；纯文本为空，不保存签名 URL",
    )
    sha256: Mapped[str | None] = mapped_column(
        sa.Text, comment="文件内容 SHA-256，应用写入 64 位十六进制；用于校验和去重",
    )
    status: Mapped[str] = mapped_column(
        sa.Text, server_default="pending",
        comment="处理状态：pending、processing、ready、failed；状态流转由应用管理",
    )
    error: Mapped[str | None] = mapped_column(
        sa.Text, comment="最近一次处理或文件清理失败原因；无错误时为空",
    )
    revision: Mapped[int] = mapped_column(
        server_default="1", comment="编辑时由应用递增的计数，供 Worker 拒绝过期结果；不保存历史版本",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), comment="资料创建时间，带时区",
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(),
        comment="资料最后更新时间，带时区；后续修改由应用显式更新",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), comment="逻辑删除时间，带时区；为空表示未删除，非空资料等待文件清理",
    )

    storage_profile: Mapped[StorageProfile | None] = relationship(back_populates="resources")
    search_index: Mapped[ResourceIndex | None] = relationship(
        back_populates="resource", cascade="all, delete-orphan", passive_deletes=True,
    )

    __table_args__ = (
        sa.UniqueConstraint("storage_profile_id", "object_key", name="resource_file_unique"),
        sa.Index("resource_pending", "status", "created_at", postgresql_where=sa.text("deleted_at IS NULL")),
        sa.Index("resource_sha256", "sha256"),
        {"comment": "图片、图文和纯文本资料的元数据、文件位置及处理状态"},
    )


class ResourceIndex(Base):
    """One whole-resource embedding plus optional lexical retrieval."""
    __tablename__ = "resource_indexes"

    resource_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("mmkb.resources.id", ondelete="CASCADE"), primary_key=True,
        comment="资料 ID，主键兼外键，保证每份资料最多一条索引；物理删除资料时级联删除",
    )
    embedding: Mapped[Any] = mapped_column(
        HALFVEC(EMBEDDING_DIMENSIONS), nullable=False,
        comment="2048 维半精度多模态向量；编码模型由部署配置指定",
    )
    segmented_text: Mapped[str] = mapped_column(
        sa.Text, server_default="",
        comment="标题、标签、用户文字、OCR 和 Caption 经 Jieba 分词后以空格连接的辅助检索文本",
    )
    search_tsv: Mapped[Any] = mapped_column(
        pg.TSVECTOR,
        sa.Computed("to_tsvector('simple'::regconfig, segmented_text)", persisted=True),
        nullable=False,
        comment="PostgreSQL 由 segmented_text 按 simple 配置生成的 tsvector；",
    )
    indexed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(),
        comment="本次索引写入时间，带时区；替换索引内容时由应用更新",
    )

    resource: Mapped[Resource] = relationship(back_populates="search_index")

    __table_args__ = (
        sa.Index(
            "resource_embedding_hnsw", "embedding", postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        {"comment": "每份资料的唯一多模态向量和辅助分词检索字段；不分块"},
    )
