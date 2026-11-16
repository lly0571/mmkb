"""Initial retrieval defaults; no database-backed configuration registry."""
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# A schema change and a full reindex are required to change this value.
EMBEDDING_DIMENSIONS = 2048


@dataclass(frozen=True)
class RetrievalConfig:
    embedding_base_url: str
    embedding_model: str
    dimensions: int = EMBEDDING_DIMENSIONS
    top_k: int = 20
    lexical_assist_weight: float = 0.1


@dataclass(frozen=True)
class ServiceConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
            raise ValueError("Model base_url must be an HTTP(S) API root without query or fragment")
        if not self.model.strip():
            raise ValueError("Model ID must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Model timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class ModelConfig:
    embedding: ServiceConfig
    caption: ServiceConfig | None = None


def _load_environment() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)


def _load_embedding_config() -> ServiceConfig:
    if int(os.environ.get("EMBEDDING_DIMENSIONS", EMBEDDING_DIMENSIONS)) != EMBEDDING_DIMENSIONS:
        raise ValueError("EMBEDDING_DIMENSIONS must match the halfvec(2048) schema")
    return _load_service_config("EMBEDDING", default_timeout=60.0)


def _load_service_config(prefix: str, *, default_timeout: float) -> ServiceConfig:
    values = {name: os.environ.get(f"{prefix}_{name}", "").strip() for name in ("BASE_URL", "MODEL")}
    missing = [f"{prefix}_{name}" for name, value in values.items() if not value]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " in the environment or .env.local")
    return ServiceConfig(
        base_url=values["BASE_URL"],
        model=values["MODEL"],
        api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
        timeout_seconds=float(os.environ.get(f"{prefix}_TIMEOUT_SECONDS", str(default_timeout))),
    )


def load_model_config() -> ModelConfig:
    _load_environment()
    embedding = _load_embedding_config()
    has_caption = any(os.environ.get(f"CAPTION_{name}", "").strip() for name in ("BASE_URL", "MODEL"))
    caption = _load_service_config("CAPTION", default_timeout=120.0) if has_caption else None
    return ModelConfig(embedding=embedding, caption=caption)


def load_retrieval_config() -> RetrievalConfig:
    _load_environment()
    embedding = _load_embedding_config()
    return RetrievalConfig(
        embedding_base_url=embedding.base_url,
        embedding_model=embedding.model,
    )
