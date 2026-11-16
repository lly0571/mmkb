"""Model adapters shared by processing and retrieval."""
from dataclasses import dataclass

from mmkb.config import ModelConfig, load_model_config
from mmkb.providers.common import ImageInput, ImageKind, ProviderError
from mmkb.providers.embedding import EmbeddingProvider
from mmkb.providers.vision import VisionProvider

__all__ = ["EmbeddingProvider", "VisionProvider", "ImageInput", "ImageKind", "ProviderError", "Providers", "build_providers"]


@dataclass
class Providers:
    embedding: EmbeddingProvider
    vision: VisionProvider | None

    def close(self) -> None:
        try:
            self.embedding.close()
        finally:
            if self.vision is not None:
                self.vision.close()

    def __enter__(self) -> "Providers":
        return self

    def __exit__(self, *args) -> None:
        self.close()


def build_providers(config: ModelConfig | None = None) -> Providers:
    config = config if config is not None else load_model_config()
    embedding = EmbeddingProvider(config.embedding)
    try:
        vision = VisionProvider(config.caption) if config.caption is not None else None
    except Exception:
        embedding.close()
        raise
    return Providers(embedding=embedding, vision=vision)
