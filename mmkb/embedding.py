"""Retrieval's injectable embedding contract and shared vector validation.

Model clients live in mmkb.providers. A caller can bridge this byte-oriented
contract to a client's own image input type when wiring the application.
"""
from __future__ import annotations

import math
from io import BytesIO
from collections.abc import Sequence
from numbers import Real
from typing import Literal, Protocol

from mmkb.config import EMBEDDING_DIMENSIONS
from mmkb.providers.common import ImageInput, ProviderError


class EmbeddingError(RuntimeError):
    """Encoding failed or produced a vector that cannot be indexed/searched."""

    def __init__(self, message: str, *, code: str = "invalid_response", status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def provider_status(error: ProviderError) -> int:
    return {"input_too_large": 413, "invalid_input": 422, "timeout": 504,
            "invalid_response": 502, "output_truncated": 502}.get(error.code, 503)


class ModelEmbeddingBridge:
    """Adapt retrieval's byte protocol to the model client's ImageInput."""

    def __init__(self, client):
        self.client = client

    def embed(self, *, purpose, text=None, image=None):
        from PIL import Image, UnidentifiedImageError
        from mmkb.images import FORMATS

        image_input = None
        if image is not None:
            try:
                with Image.open(BytesIO(image)) as decoded:
                    mime = FORMATS[decoded.format][1]
            except (KeyError, OSError, ValueError, UnidentifiedImageError):
                raise EmbeddingError("Use PNG, JPEG or WebP images", code="invalid_input", status_code=422) from None
            image_input = ImageInput(image, mime)
        try:
            return self.client.embed(purpose=purpose, text=text, image=image_input)
        except ProviderError as exc:
            raise EmbeddingError(str(exc), code=exc.code, status_code=provider_status(exc)) from None


class EmbeddingProvider(Protocol):
    """Encode text, image bytes, or their joint input with the same model.

    Implementations must respect the model's query/document conventions and
    report input limits or service failures instead of silently truncating.
    """

    def embed(
        self,
        *,
        text: str | None = None,
        image: bytes | None = None,
        purpose: Literal["query", "document"],
    ) -> Sequence[float]: ...


def normalize_embedding(embedding: Sequence[float]) -> list[float]:
    """Validate and normalize one 2048-dimensional vector for either purpose."""
    try:
        if isinstance(embedding, (str, bytes)):
            raise TypeError("Expected a numeric sequence")
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(f"Embedding must have {EMBEDDING_DIMENSIONS} dimensions")
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in embedding):
            raise TypeError("Expected numeric scalar components")
        values = [float(value) for value in embedding]
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingError("Embedding must be a sequence of finite numbers") from exc
    if not all(math.isfinite(value) for value in values):
        raise EmbeddingError("Embedding must contain only finite numbers")
    scale = max(abs(value) for value in values)
    if scale == 0:
        raise EmbeddingError("Embedding must be nonzero")
    # Scale first so very large or very small finite vectors also normalize.
    scaled = [value / scale for value in values]
    norm = math.hypot(*scaled)
    return [value / norm for value in scaled]
