"""Packaged expectation of the applied schema, readable without a repository checkout.

The health probe compares the live Alembic revision with this contract instead of reading
``alembic.ini`` or ``migrations/`` at run time: an installed wheel has neither. Version order is
taken from the explicit history below, never from string or numeric comparison of revision ids.
``tests/test_health.py`` fails when the repository migration heads drift from EXPECTED_REVISIONS.
"""
from __future__ import annotations

# Alembic records applied revisions in this table; the default ``alembic_version`` is not ours.
VERSION_TABLE_SCHEMA = "public"
VERSION_TABLE_NAME = "mmkb_alembic_version"
VERSION_TABLE = f"{VERSION_TABLE_SCHEMA}.{VERSION_TABLE_NAME}"
VERSION_COLUMN = "version_num"

# Linear history in application order; the last entry is the only acceptable head.
EXPECTED_REVISIONS: tuple[str, ...] = ("0001_initial_schema", "0002_import_items", "0003_image_routing")
EXPECTED_HEAD = EXPECTED_REVISIONS[-1]

# Business tables the retrieval and resource paths must be able to read.
SCHEMA_NAME = "mmkb"
REQUIRED_TABLES: tuple[str, ...] = ("storage_profiles", "resources", "resource_indexes", "import_items")

# pgvector must be installed in public and the vector column must keep the deployed dimension.
VECTOR_EXTENSION = "vector"
EMBEDDING_TABLE = "resource_indexes"
EMBEDDING_COLUMN = "embedding"
EXPECTED_EMBEDDING_TYPE = "halfvec(2048)"
