"""Qwen3-VL embeddings through vLLM's messages extension."""
import math
from typing import Literal

import httpx

from mmkb.config import EMBEDDING_DIMENSIONS, ServiceConfig
from mmkb.providers.common import ImageInput, ProviderError, ServiceClient, image_content

INSTRUCTIONS = {
    "query": "Retrieve images or text relevant to the user's query.",
    "document": "Represent the user's input.",
}


class EmbeddingProvider:
    def __init__(self, config: ServiceConfig, *, client: httpx.Client | None = None):
        self._service = ServiceClient(config, client)

    def embed(
        self, *, purpose: Literal["query", "document"],
        text: str | None = None, image: ImageInput | None = None,
    ) -> list[float]:
        if purpose not in INSTRUCTIONS:
            raise ProviderError("embedding", "invalid_input", "Purpose must be query or document")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise ProviderError("embedding", "invalid_input", "Text must not be empty or whitespace")
        if text is None and image is None:
            raise ProviderError("embedding", "invalid_input", "Provide text, an image, or both")
        content = []
        if image is not None:
            content.append(image_content(image, "embedding"))
        if text is not None:
            content.append({"type": "text", "text": text})
        result = self._service.request("embedding", "POST", "embeddings", body={
            "model": self._service.config.model,
            "dimensions": EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
            "add_generation_prompt": True,
            "messages": [
                {"role": "system", "content": INSTRUCTIONS[purpose]},
                {"role": "user", "content": content},
            ],
        })
        data = result.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ProviderError("embedding", "invalid_response", "Expected exactly one embedding")
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise ProviderError("embedding", "invalid_response", "Expected a 2048-dimensional embedding")
        try:
            valid = all(type(value) in (int, float) and math.isfinite(value) for value in vector)
        except OverflowError:
            valid = False
        if not valid:
            raise ProviderError("embedding", "invalid_response", "Embedding must contain only finite numbers")
        scale = max(abs(value) for value in vector)
        if scale == 0:
            raise ProviderError("embedding", "invalid_response", "Embedding must be non-zero")
        # Scale first to avoid overflow/underflow even for extreme finite vectors.
        scaled = [value / scale for value in vector]
        norm = math.sqrt(math.fsum(value * value for value in scaled))
        return [value / norm for value in scaled]

    def check_connection(self) -> None:
        self._service.check_connection("embedding")

    def close(self) -> None:
        self._service.close()

    def __enter__(self) -> "EmbeddingProvider":
        return self

    def __exit__(self, *args) -> None:
        self.close()
