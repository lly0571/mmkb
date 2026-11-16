"""Exact-search checks on an explicitly configured, migrated test database."""
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from mmkb.config import EMBEDDING_DIMENSIONS
from mmkb.db.models import Resource, ResourceIndex
from mmkb.retrieval import search

pytestmark = pytest.mark.integration


def vector(x=1.0, y=0.0):
    return [x, y] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


class FixedEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, **kwargs):
        self.calls.append(kwargs)
        return vector()


@pytest.fixture(scope="module")
def retrieval_engine():
    url = os.environ.get("MMKB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MMKB_TEST_DATABASE_URL to a migrated PostgreSQL test database")
    engine = sa.create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            # Fail explicitly if the configured test database is not migrated.
            connection.execute(sa.select(ResourceIndex.resource_id).limit(0))
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def retrieval_db(retrieval_engine):
    with retrieval_engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Initial migrations already create HNSW. Restrict exact scanning
            # to this transaction; production SQL stays index-compatible.
            connection.execute(sa.text("SET LOCAL enable_indexscan = off"))
            factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
            yield factory
        finally:
            transaction.rollback()


def add_resource(factory, *, embedding=None, indexed=True, **fields):
    resource_id = uuid4()
    values = dict(id=resource_id, input_kind="text", status="ready", title="title")
    values.update(fields)
    with factory.begin() as session:
        session.add(Resource(**values))
        session.flush()
        if indexed:
            session.add(ResourceIndex(
                resource_id=resource_id,
                embedding=vector() if embedding is None else embedding,
            ))
    return resource_id


@pytest.mark.parametrize("inputs", [
    {"text": "文本查询"}, {"image": b"test image bytes"},
    {"text": "图文查询", "image": b"test image bytes"},
])
def test_three_query_types_use_halfvec_cosine_ranking(retrieval_db, inputs):
    factory = retrieval_db
    # Insert out of order so insertion order cannot satisfy the expectation.
    opposite = add_resource(factory, embedding=vector(-1, 0))
    near = add_resource(factory, embedding=vector(0.8, 0.6))
    orthogonal = add_resource(factory, embedding=vector(0, 1))
    exact = add_resource(factory)
    embedder = FixedEmbedder()

    result = search(factory, embedder, **inputs)

    assert result.mode == "vector"
    assert [item.resource_id for item in result.items] == [exact, near, orthogonal, opposite]
    assert [item.score for item in result.items] == pytest.approx([1, 0.8, 0, -1], abs=0.001)
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == dict(text=inputs.get("text"), image=inputs.get("image"), purpose="query")
    limited = search(factory, embedder, top_k=2, **inputs)
    assert [item.resource_id for item in limited.items] == [exact, near]


def test_only_ready_undeleted_resources_with_indexes_are_visible(retrieval_db):
    for status in ("pending", "processing", "failed"):
        add_resource(retrieval_db, status=status)
    add_resource(retrieval_db, deleted_at=datetime.now(timezone.utc))
    add_resource(retrieval_db, indexed=False)
    visible = add_resource(retrieval_db, embedding=vector(0, 1))

    result = search(retrieval_db, FixedEmbedder(), text="q")
    assert [item.resource_id for item in result.items] == [visible]


def test_all_tags_must_match_before_top_k(retrieval_db):
    add_resource(retrieval_db, tags=["图表"])
    add_resource(retrieval_db, tags=["金融"])
    add_resource(retrieval_db, tags=["图表", "金融"], status="pending")
    match = add_resource(retrieval_db, tags=["图表", "金融", "年度"], embedding=vector(0, 1))

    result = search(retrieval_db, FixedEmbedder(), text="q", tags=["图表", "金融"], top_k=1)
    assert [item.resource_id for item in result.items] == [match]
    assert result.items[0].tags == ["图表", "金融", "年度"]
    assert search(retrieval_db, FixedEmbedder(), text="q", tags=["不存在"]).items == []
    assert len(search(retrieval_db, FixedEmbedder(), text="q", tags=[]).items) == 3


def test_empty_library_returns_no_results(retrieval_db):
    assert search(retrieval_db, FixedEmbedder(), text="q").items == []


def test_default_top_k_is_twenty(retrieval_db):
    for _ in range(25):
        add_resource(retrieval_db)
    assert len(search(retrieval_db, FixedEmbedder(), text="q").items) == 20


def test_summary_precedence_and_truncation(retrieval_db):
    cases = [
        ({"user_text": "  用户文字  ", "caption": "描述", "ocr_text": "识别"}, "用户文字"),
        ({"user_text": " \t\n", "caption": "  描述  ", "ocr_text": "识别"}, "描述"),
        ({"ocr_text": "  识别  "}, "识别"),
        ({}, ""),
        ({"user_text": "字" * 201}, "字" * 200),
    ]
    expected = {}
    for fields, summary in cases:
        resource_id = add_resource(retrieval_db, **fields)
        expected[resource_id] = summary
    result = search(retrieval_db, FixedEmbedder(), text="q")
    assert {item.resource_id: item.summary for item in result.items} == expected


def test_search_closes_real_session_without_committing(retrieval_db):
    resource_id = add_resource(retrieval_db)
    created = []

    class TrackedSession(Session):
        def __init__(self):
            super().__init__(
                bind=retrieval_db.kw["bind"], join_transaction_mode="create_savepoint",
            )
            self.was_closed = False
            created.append(self)

        def close(self):
            super().close()
            self.was_closed = True

        def commit(self):
            pytest.fail("Search must not commit")

    result = search(TrackedSession, FixedEmbedder(), text="q")
    assert result.items[0].resource_id == resource_id
    assert len(created) == 1 and created[0].was_closed
