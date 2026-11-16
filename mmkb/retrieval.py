"""Whole-resource vector search and tokenization shared with processing."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import jieba
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mmkb.db.models import Resource, ResourceIndex
from mmkb.embedding import EmbeddingError, EmbeddingProvider, normalize_embedding


@dataclass(frozen=True)
class SearchItem:
    resource_id: UUID
    input_kind: str
    title: str
    tags: list[str]
    summary: str
    score: float


@dataclass(frozen=True)
class SearchResult:
    items: list[SearchItem]
    mode: Literal["vector"] = field(default="vector", init=False)


def segment_text(text: str) -> str:
    """Use Jieba precise mode and join non-whitespace tokens with one space."""
    return " ".join(token for token in jieba.cut(text, cut_all=False) if token.strip())


def search(
    session_factory: Callable[[], Session],
    embedder: EmbeddingProvider,
    *,
    text: str | None = None,
    image: bytes | None = None,
    tags: Sequence[str] = (),
    top_k: int = 20,
) -> SearchResult:
    """Encode once, then search ready, undeleted resources by cosine distance.

    Every supplied tag must match exactly. Embedding happens before opening a
    database session. The caller owns the engine; this function owns and closes
    the session returned by session_factory. No environment config is loaded.
    """
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string")
    text = (text.strip() or None) if text is not None else None
    if image is not None and (not isinstance(image, bytes) or not image):
        raise ValueError("image must be nonempty bytes")
    if text is None and image is None:
        raise ValueError("Provide nonempty text, image bytes, or both")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
        raise ValueError("tags must be a sequence of strings")
    if any(not isinstance(tag, str) for tag in tags):
        raise ValueError("tags must contain only strings")

    try:
        embedding = embedder.embed(text=text, image=image, purpose="query")
    except EmbeddingError:
        raise
    except Exception as exc:
        raise EmbeddingError("Query embedding failed") from exc
    vector = normalize_embedding(embedding)

    distance = ResourceIndex.embedding.cosine_distance(vector)
    statement = (
        sa.select(
            Resource.id,
            Resource.input_kind,
            Resource.title,
            Resource.tags,
            Resource.user_text,
            Resource.caption,
            Resource.ocr_text,
            distance.label("distance"),
        )
        .join(ResourceIndex, ResourceIndex.resource_id == Resource.id)
        .where(Resource.status == "ready", Resource.deleted_at.is_(None))
        .order_by(distance)
        .limit(top_k)
    )
    if tags:
        statement = statement.where(Resource.tags.contains(list(tags)))

    with session_factory() as session:
        items = []
        for row in session.execute(statement):
            summary = next(
                (value.strip() for value in (row.user_text, row.caption, row.ocr_text)
                 if value and value.strip()),
                "",
            )
            items.append(SearchItem(
                resource_id=row.id,
                input_kind=row.input_kind,
                title=row.title,
                tags=list(row.tags),
                summary=summary[:200],
                score=1.0 - row.distance,
            ))
    return SearchResult(items=items)
