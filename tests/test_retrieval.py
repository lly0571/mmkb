"""Retrieval contracts that do not require a model service or database."""
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mmkb.config import EMBEDDING_DIMENSIONS
from mmkb.embedding import EmbeddingError, normalize_embedding
from mmkb.retrieval import search, segment_text


def vector(*values):
    return list(values) + [0.0] * (EMBEDDING_DIMENSIONS - len(values))


@pytest.fixture
def dependencies():
    embedder = Mock()
    embedder.embed.return_value = vector(3, 4)
    session = MagicMock(spec=Session)
    session.__enter__.return_value = session
    session.execute.return_value = []
    return Mock(return_value=session), embedder, session


@pytest.mark.parametrize("inputs, expected_text", [
    ({"text": "  图表  "}, "图表"),
    ({"image": b"image"}, None),
    ({"text": "配文", "image": b"image"}, "配文"),
    ({"text": " \t", "image": b"image"}, None),
])
def test_encodes_once_before_creating_session(dependencies, inputs, expected_text):
    factory, embedder, session = dependencies
    order = []

    def encode(**kwargs):
        order.append("encode")
        factory.assert_not_called()
        return vector(3, 4)

    def open_session():
        order.append("session")
        return session

    embedder.embed.side_effect = encode
    factory.side_effect = open_session
    result = search(factory, embedder, **inputs)

    assert order == ["encode", "session"]
    embedder.embed.assert_called_once_with(
        text=expected_text, image=inputs.get("image"), purpose="query",
    )
    assert result.items == []
    assert result.mode == "vector"
    session.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("inputs", [
    {}, {"text": " \t\n"}, {"text": 123}, {"image": b""},
    {"text": "query", "image": b""}, {"image": "image.png"},
    {"text": "q", "top_k": 0}, {"text": "q", "top_k": -1},
    {"text": "q", "top_k": 1.5}, {"text": "q", "top_k": True},
    {"text": "q", "tags": "金融"}, {"text": "q", "tags": [123]},
    {"text": "q", "tags": None},
])
def test_invalid_requests_do_not_encode_or_open_session(dependencies, inputs):
    factory, embedder, _ = dependencies
    with pytest.raises(ValueError):
        search(factory, embedder, **inputs)
    embedder.embed.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("invalid", [
    None, [], [1.0] * (EMBEDDING_DIMENSIONS - 1),
    [1.0] * (EMBEDDING_DIMENSIONS + 1), vector(0),
    vector(float("nan")), vector(float("inf")), vector(float("-inf")),
    vector("not numeric"), vector("1"), vector(True), vector(None), vector([1]),
    vector(10 ** 400), "1" * EMBEDDING_DIMENSIONS,
])
def test_bad_embedding_does_not_open_session(dependencies, invalid):
    factory, embedder, _ = dependencies
    embedder.embed.return_value = invalid
    with pytest.raises(EmbeddingError):
        search(factory, embedder, text="q")
    factory.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("remote timeout"), EmbeddingError("unavailable")])
def test_embedding_failure_is_explicit(dependencies, error):
    factory, embedder, _ = dependencies
    embedder.embed.side_effect = error
    with pytest.raises(EmbeddingError) as caught:
        search(factory, embedder, text="q")
    if isinstance(error, EmbeddingError):
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error
        assert str(caught.value) == "Query embedding failed"
    factory.assert_not_called()


def test_database_error_propagates_and_closes_session(dependencies):
    factory, embedder, session = dependencies
    error = SQLAlchemyError("query failed")
    session.execute.side_effect = error
    with pytest.raises(SQLAlchemyError) as caught:
        search(factory, embedder, text="q")
    assert caught.value is error
    session.__exit__.assert_called_once()
    assert session.__exit__.call_args.args[1] is error


def test_results_are_detached_values_and_query_vector_is_normalized(dependencies):
    factory, embedder, session = dependencies
    resource_id = uuid4()
    session.execute.return_value = [SimpleNamespace(
        id=resource_id, input_kind="text", title="title", tags=["tag"],
        user_text="  摘要  ", caption="caption", ocr_text="OCR", distance=0.25,
    )]
    result = search(factory, embedder, text="q")
    item = result.items[0]
    assert (item.resource_id, item.input_kind, item.title, item.tags) == (
        resource_id, "text", "title", ["tag"],
    )
    assert item.summary == "摘要"
    assert item.score == 0.75
    statement = session.execute.call_args.args[0]
    bound_vectors = [value for value in statement.compile().params.values() if isinstance(value, list)]
    assert bound_vectors == [vector(0.6, 0.8)]


@pytest.mark.parametrize("scale", [1.0, 1e308, 1e-308])
def test_normalization_is_stable_for_finite_nonzero_vectors(scale):
    source = vector(scale, -scale)
    normalized = normalize_embedding(source)
    assert len(normalized) == EMBEDDING_DIMENSIONS
    assert math.hypot(*normalized) == pytest.approx(1.0)
    assert normalized[:2] == pytest.approx([2 ** -0.5, -(2 ** -0.5)])
    assert source[:2] == [scale, -scale]


@pytest.mark.parametrize("text, expected", [
    ("", ""), (" \n\t\u3000", ""),
    ("我来到北京清华大学", "我 来到 北京 清华大学"),
    ("北京  Python3\n知识库", "北京 Python3 知识库"),
])
def test_shared_tokenization(text, expected):
    assert segment_text(text) == expected
