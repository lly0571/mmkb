"""Alembic integration; importing this file never creates application tables implicitly."""
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import context
from dotenv import load_dotenv
from pgvector.sqlalchemy import HALFVEC

from mmkb.db.models import Base

load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "mmkb"
    return True


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, HALFVEC):
        autogen_context.imports.add("from pgvector.sqlalchemy import HALFVEC")
        return f"HALFVEC({obj.dim})"
    return False


common = dict(
    target_metadata=target_metadata,
    include_schemas=True,
    include_name=include_name,
    compare_type=True,
    render_item=render_item,
    version_table="mmkb_alembic_version",
    version_table_schema="public",
)

if context.is_offline_mode():
    # Alembic's offline mode supports validation without a database connection.
    context.configure(url="postgresql://", literal_binds=True, dialect_opts={"paramstyle": "named"}, **common)
    with context.begin_transaction():
        context.run_migrations()
else:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL in the environment or .env.local")
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, **common)
        with context.begin_transaction():
            context.run_migrations()
