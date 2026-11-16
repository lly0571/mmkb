"""Transient HTTP search input handling and lazy, owned query model clients."""
from __future__ import annotations

from threading import Lock

from mmkb.config import AppConfig, load_embedding_config
from mmkb.embedding import EmbeddingError, ModelEmbeddingBridge
from mmkb.errors import ResourceError
from mmkb.images import inspect_image
from mmkb.providers import EmbeddingProvider
from mmkb.resources import normalize_tags
from mmkb.retrieval import search


def build_query_client():
    return EmbeddingProvider(load_embedding_config())


class SearchService:
    def __init__(self, sessions, config: AppConfig, *, client_factory=None):
        self.sessions = sessions
        self.config = config
        self.client_factory = client_factory or build_query_client
        self._lock = Lock()
        self._client = None
        self._closed = False

    def _embedder(self):
        with self._lock:
            if self._closed:
                raise ResourceError("model_unavailable", "Search service is closed", 503)
            if self._client is None:
                try:
                    self._client = self.client_factory()
                except ValueError:
                    raise ResourceError("model_config", "Check Embedding service configuration", 503) from None
            return ModelEmbeddingBridge(self._client)

    def search(self, *, text=None, source=None, filename=None, tags=None, top_k=20):
        if text is not None and not isinstance(text, str):
            raise ResourceError("invalid_text", "Query text must be a string")
        if text is not None and len(text) > self.config.max_text_chars:
            raise ResourceError("text_too_large", "Query exceeds MAX_TEXT_CHARS", 413)
        text = (text.strip() or None) if text is not None else None
        if text is None and source is None:
            raise ResourceError("empty_input", "Provide an image or non-empty text")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ResourceError("invalid_top_k", "Use top_k 1..100")
        tags = normalize_tags([] if tags is None else tags)
        image = None
        if source is not None:
            inspect_image(source, filename, max_upload_bytes=self.config.max_upload_bytes)
            image = source.read(self.config.max_upload_bytes + 1)
            if len(image) > self.config.max_upload_bytes:
                raise ResourceError("file_too_large", "Image exceeds MAX_UPLOAD_BYTES", 413)
        try:
            result = search(self.sessions, self._embedder(), text=text, image=image, tags=tags, top_k=top_k)
        except EmbeddingError as exc:
            raise ResourceError(f"embedding_{exc.code}", str(exc), exc.status_code) from None
        return {"mode": result.mode, "items": [dict(
            resource_id=item.resource_id, input_kind=item.input_kind, title=item.title,
            tags=item.tags, summary=item.summary, score=item.score,
            detail_url=f"/api/resources/{item.resource_id}",
            original_url=f"/api/resources/{item.resource_id}/original" if item.input_kind != "text" else None,
        ) for item in result.items]}

    def close(self):
        with self._lock:
            self._closed = True
            if self._client is not None:
                self._client.close()
                self._client = None
