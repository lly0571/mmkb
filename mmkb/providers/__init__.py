"""Model adapters shared by processing and retrieval."""
from dataclasses import dataclass

from mmkb.config import ModelConfig, load_model_config
from mmkb.providers.common import ImageInput, ImageKind, ProviderError
from mmkb.providers.embedding import EmbeddingProvider
from mmkb.providers.systemone import SystemOneProvider
from mmkb.providers.vision import VisionProvider

__all__ = [
    "EmbeddingProvider", "VisionProvider", "SystemOneProvider", "ImageInput", "ImageKind", "ProviderError",
    "Providers", "build_providers",
]


@dataclass
class Providers:
    embedding: EmbeddingProvider
    vision: VisionProvider | None = None
    decision: SystemOneProvider | None = None

    def close(self) -> None:
        try:
            self.embedding.close()
        finally:
            try:
                if self.vision is not None:
                    self.vision.close()
            finally:
                if self.decision is not None:
                    self.decision.close()

    def __enter__(self) -> "Providers":
        return self

    def __exit__(self, *args) -> None:
        self.close()


def build_providers(config: ModelConfig | None = None) -> Providers:
    config = config if config is not None else load_model_config()
    embedding = EmbeddingProvider(config.embedding)
    vision: VisionProvider | None = None
    decision: SystemOneProvider | None = None
    try:
        if config.caption is not None:
            vision = VisionProvider(config.caption)
        if config.decision is not None:
            decision = SystemOneProvider(config.decision, profile=config.decision_profile)
    except Exception:
        if vision is not None:
            vision.close()
        embedding.close()
        raise
    return Providers(embedding=embedding, vision=vision, decision=decision)
