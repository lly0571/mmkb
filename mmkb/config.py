"""Initial retrieval defaults; no database-backed configuration registry."""
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# A schema change and a full reindex are required to change this value.
EMBEDDING_DIMENSIONS = 2048
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StorageConfig:
    storage_backend: str = "local"
    storage_profile_name: str = "local-default"
    local_storage_root: str = "./data/assets"
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_credential_profile: str | None = None
    s3_addressing_style: str = "path"
    s3_signature_version: str = "s3v4"


def load_storage_config() -> StorageConfig:
    """Load storage independently of database and model configuration."""
    _load_environment()
    backend = os.environ.get("STORAGE_BACKEND", "local").strip()
    name = os.environ.get("STORAGE_PROFILE_NAME", "").strip() or f"{backend}-default"
    if backend not in {"local", "s3"}:
        raise ValueError("STORAGE_BACKEND must be local or s3")
    return StorageConfig(
        storage_backend=backend,
        storage_profile_name=name,
        local_storage_root=os.environ.get("LOCAL_STORAGE_ROOT", "./data/assets").strip(),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", "").strip() or None,
        s3_bucket=os.environ.get("S3_BUCKET", "").strip() or None,
        s3_region=os.environ.get("S3_REGION", "us-east-1").strip(),
        s3_credential_profile=os.environ.get("S3_CREDENTIAL_PROFILE", "").strip() or None,
        s3_addressing_style=os.environ.get("S3_ADDRESSING_STYLE", "path").strip(),
        s3_signature_version=os.environ.get("S3_SIGNATURE_VERSION", "s3v4").strip(),
    )


@dataclass(frozen=True)
class AppConfig:
    database_url: str = field(repr=False)
    storage_backend: str = "local"
    storage_profile_name: str = "local-default"
    local_storage_root: str = "./data/assets"
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    # Compatibility for existing resource callers; never applied by storage adapters.
    s3_prefix: str = ""
    s3_credential_profile: str | None = None
    s3_addressing_style: str = "path"
    s3_signature_version: str = "s3v4"
    max_upload_bytes: int = 20 * 1024 * 1024
    max_text_chars: int = 100_000


def load_app_config() -> AppConfig:
    storage = load_storage_config()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("Set DATABASE_URL in the environment or .env.local")
    config = AppConfig(
        database_url=database_url,
        **asdict(storage),
        s3_prefix=os.environ.get("S3_PREFIX", ""),
        max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)),
        max_text_chars=int(os.environ.get("MAX_TEXT_CHARS", 100_000)),
    )
    if config.storage_backend not in {"local", "s3"}:
        raise ValueError("STORAGE_BACKEND must be local or s3")
    if not config.storage_profile_name or min(config.max_upload_bytes, config.max_text_chars) < 1:
        raise ValueError("Profile name and positive input limits are required")
    return config


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


def load_embedding_config() -> ServiceConfig:
    """Load only query encoding settings, independently of optional vision."""
    _load_environment()
    return _load_embedding_config()


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
