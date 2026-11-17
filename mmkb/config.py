"""Initial retrieval defaults; no database-backed configuration registry."""
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
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


def resolve_storage_config(env: Mapping[str, str] | None = None) -> StorageConfig:
    """Derive storage settings from one environment mapping; raises ValueError when invalid."""
    source = os.environ if env is None else env
    backend = source.get("STORAGE_BACKEND", "local").strip()
    name = source.get("STORAGE_PROFILE_NAME", "").strip() or f"{backend}-default"
    if backend not in {"local", "s3"}:
        raise ValueError("STORAGE_BACKEND must be local or s3")
    return StorageConfig(
        storage_backend=backend,
        storage_profile_name=name,
        local_storage_root=source.get("LOCAL_STORAGE_ROOT", "./data/assets").strip(),
        s3_endpoint_url=source.get("S3_ENDPOINT_URL", "").strip() or None,
        s3_bucket=source.get("S3_BUCKET", "").strip() or None,
        s3_region=source.get("S3_REGION", "us-east-1").strip(),
        s3_credential_profile=source.get("S3_CREDENTIAL_PROFILE", "").strip() or None,
        s3_addressing_style=source.get("S3_ADDRESSING_STYLE", "path").strip(),
        s3_signature_version=source.get("S3_SIGNATURE_VERSION", "s3v4").strip(),
    )


def load_storage_config() -> StorageConfig:
    """Load storage independently of database and model configuration."""
    _load_environment()
    return resolve_storage_config(os.environ)


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
    decision: ServiceConfig | None = None
    # Request dialect of the decision service: auto, d3, clef or generic.
    decision_profile: str = "auto"


@dataclass(frozen=True)
class RoutingConfig:
    backend: Literal["systemone", "llm"] = "systemone"
    # Calibrated on the frozen four-class P1 sample set; deployments can override.
    min_probability: float = 0.80
    min_confidence: float = 0.80
    min_label_mass: float = 0.90

    def __post_init__(self) -> None:
        if self.backend not in {"systemone", "llm"}:
            raise ValueError("IMAGE_ROUTING_BACKEND must be systemone or llm")
        for name in ("min_probability", "min_confidence", "min_label_mass"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"Routing {name} must be a finite number between 0 and 1")


def load_routing_config() -> RoutingConfig:
    _load_environment()
    return RoutingConfig(
        backend=os.environ.get("IMAGE_ROUTING_BACKEND", "").strip().lower() or "systemone",
        min_probability=float(os.environ.get("ROUTING_MIN_PROBABILITY", "0.80")),
        min_confidence=float(os.environ.get("ROUTING_MIN_CONFIDENCE", "0.80")),
        min_label_mass=float(os.environ.get("ROUTING_MIN_LABEL_MASS", "0.90")),
    )


def _load_environment() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)


# Optional CAPTION_ENABLED / DECISION_ENABLED switches: blank keeps the historical behaviour.
SWITCH_SUFFIX = "ENABLED"
_TRUE_VALUES = {"true": True, "1": True}
_FALSE_VALUES = {"false": False, "0": False}
ProbeAction = Literal["probe", "not_configured", "disabled", "invalid"]


def _optional_switch(prefix: str, env: Mapping[str, str]) -> tuple[bool | None, str | None]:
    """Return the switch value and, when unusable, the offending variable name."""
    raw = (env.get(f"{prefix}_{SWITCH_SUFFIX}") or "").strip()
    if not raw:
        return None, None
    key = raw.lower()
    if key in _TRUE_VALUES:
        return True, None
    if key in _FALSE_VALUES:
        return False, None
    return None, f"{prefix}_{SWITCH_SUFFIX}"


def _validate_dimensions(env: Mapping[str, str]) -> None:
    if int(env.get("EMBEDDING_DIMENSIONS", EMBEDDING_DIMENSIONS)) != EMBEDDING_DIMENSIONS:
        raise ValueError("EMBEDDING_DIMENSIONS must match the halfvec(2048) schema")


def _load_embedding_config() -> ServiceConfig:
    _validate_dimensions(os.environ)
    return _load_service_config("EMBEDDING", default_timeout=60.0)


def load_embedding_config() -> ServiceConfig:
    """Load only query encoding settings, independently of optional vision."""
    _load_environment()
    return _load_embedding_config()


def _load_service_config(prefix: str, *, default_timeout: float,
                         env: Mapping[str, str] | None = None) -> ServiceConfig:
    source = os.environ if env is None else env
    values = {name: source.get(f"{prefix}_{name}", "").strip() for name in ("BASE_URL", "MODEL")}
    missing = [f"{prefix}_{name}" for name, value in values.items() if not value]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " in the environment or .env.local")
    return ServiceConfig(
        base_url=values["BASE_URL"],
        model=values["MODEL"],
        api_key=source.get(f"{prefix}_API_KEY", "").strip(),
        timeout_seconds=float(source.get(f"{prefix}_TIMEOUT_SECONDS", str(default_timeout))),
    )


@dataclass(frozen=True)
class ServiceProbe:
    """One model service resolved for diagnostics; never raises, unlike strict loading."""

    key: str            # health check key: embedding, vision or decision
    prefix: str         # environment variable prefix
    required: bool
    action: ProbeAction
    code: str | None    # health error code when the service is not probed
    config: ServiceConfig | None = None


def resolve_service_probe(key: str, prefix: str, *, required: bool, default_timeout: float,
                          env: Mapping[str, str]) -> ServiceProbe:
    """Resolve one service for the health report with the same rules the Worker uses to load it."""
    def result(action: ProbeAction, code: str | None = None, config: ServiceConfig | None = None):
        return ServiceProbe(key, prefix, required, action, code, config)

    # A required service has no disable switch: an EMBEDDING_ENABLED value must not silence it.
    enabled, issue = (None, None) if required else _optional_switch(prefix, env)
    if issue:
        return result("invalid", "invalid_config")
    if enabled is False:
        # An explicit switch wins: residual service settings are not validated or used.
        return result("disabled", "service_disabled")
    configured = [(env.get(f"{prefix}_{name}") or "").strip() for name in ("BASE_URL", "MODEL")]
    if not any(configured):
        if required or enabled:
            return result("not_configured", "required_not_configured")
        return result("not_configured", "optional_not_configured")
    if (required or enabled) and not all(configured):
        # A missing variable for a needed service is a configuration gap; a malformed value is a fault.
        return result("not_configured", "required_not_configured")
    try:
        if prefix == "EMBEDDING":
            _validate_dimensions(env)
        return result("probe", None, _load_service_config(prefix, default_timeout=default_timeout, env=env))
    except (ValueError, TypeError):
        return result("invalid", "invalid_config")


def _optional_model_service(prefix: str, health_key: str, *, default_timeout: float) -> ServiceConfig | None:
    """Share the switch and single-service rules between strict loading and health parsing."""
    probe = resolve_service_probe(health_key, prefix, required=False, default_timeout=default_timeout,
                                  env=os.environ)
    if probe.action == "invalid":
        raise ValueError(f"{probe.prefix}_{SWITCH_SUFFIX} must be true, false, 1 or 0")
    if probe.code == "required_not_configured":
        # The operator asked for this service explicitly, so a partial configuration is an error.
        raise ValueError(f"Set {probe.prefix}_BASE_URL and {probe.prefix}_MODEL to enable {probe.prefix}_ENABLED")
    return probe.config


def load_model_config() -> ModelConfig:
    _load_environment()
    embedding = _load_embedding_config()
    caption = _optional_model_service("CAPTION", "vision", default_timeout=120.0)
    decision = _optional_model_service("DECISION", "decision", default_timeout=60.0)
    decision_profile = os.environ.get("DECISION_MODEL_PROFILE", "").strip().lower() or "auto"
    if decision_profile not in {"auto", "d3", "clef", "generic"}:
        raise ValueError("DECISION_MODEL_PROFILE must be auto, d3, clef or generic")
    return ModelConfig(embedding=embedding, caption=caption, decision=decision, decision_profile=decision_profile)


def load_retrieval_config() -> RetrievalConfig:
    _load_environment()
    embedding = _load_embedding_config()
    return RetrievalConfig(
        embedding_base_url=embedding.base_url,
        embedding_model=embedding.model,
    )


@dataclass(frozen=True)
class HealthConfig:
    """Startup snapshot for the read-only dependency probes; components are resolved separately."""

    database_url: str = field(repr=False)
    storage: StorageConfig | None
    services: tuple[ServiceProbe, ...]


def _health_services(source: Mapping[str, str]) -> tuple[ServiceProbe, ...]:
    return (
        resolve_service_probe("embedding", "EMBEDDING", required=True, default_timeout=60.0, env=source),
        resolve_service_probe("vision", "CAPTION", required=False, default_timeout=120.0, env=source),
        resolve_service_probe("decision", "DECISION", required=False, default_timeout=60.0, env=source),
    )


def resolve_health_config(env: Mapping[str, str] | None = None) -> HealthConfig:
    """Parse every health component independently; one bad component never hides the others."""
    source = os.environ if env is None else env
    try:
        storage: StorageConfig | None = resolve_storage_config(source)
    except ValueError:
        storage = None
    return HealthConfig(
        database_url=(source.get("DATABASE_URL") or "").strip(),
        storage=storage,
        services=_health_services(source),
    )


def storage_config_from_app(config: AppConfig) -> StorageConfig:
    """Project the storage settings one running app instance uses onto the probe view."""
    backend = (config.storage_backend or "").strip()
    if backend not in {"local", "s3"}:
        raise ValueError("STORAGE_BACKEND must be local or s3")
    root = (config.local_storage_root or "").strip()
    return StorageConfig(
        storage_backend=backend,
        storage_profile_name=(config.storage_profile_name or "").strip() or f"{backend}-default",
        local_storage_root=root or "./data/assets",
        s3_endpoint_url=(config.s3_endpoint_url or "").strip() or None,
        s3_bucket=(config.s3_bucket or "").strip() or None,
        s3_region=(config.s3_region or "").strip() or "us-east-1",
        s3_credential_profile=(config.s3_credential_profile or "").strip() or None,
        s3_addressing_style=(config.s3_addressing_style or "").strip() or "path",
        s3_signature_version=(config.s3_signature_version or "").strip() or "s3v4",
    )


def health_config_from_app(config: AppConfig,
                           env: Mapping[str, str] | None = None) -> HealthConfig:
    """Snapshot the dependencies this app instance actually serves with, for the health report.

    ``create_app`` callers may inject an ``AppConfig`` that differs from the process environment, and
    a report about a different deployment would be worse than no report. The database and storage
    settings therefore come from the app config; model services are not part of ``AppConfig`` and keep
    coming from the environment, which is where the Worker loads them from too.
    """
    if env is None:
        _load_environment()
        source: Mapping[str, str] = os.environ
    else:
        source = env
    try:
        storage: StorageConfig | None = storage_config_from_app(config)
    except ValueError:
        storage = None
    return HealthConfig(
        database_url=(config.database_url or "").strip(),
        storage=storage,
        services=_health_services(source),
    )


def health_config_payload(config: HealthConfig) -> dict[str, Any]:
    """Serialise the snapshot for the probe child process; callers never log the result."""
    return {
        "v": 1,
        "database_url": config.database_url,
        "storage": None if config.storage is None else asdict(config.storage),
        "services": [
            {
                "key": probe.key, "prefix": probe.prefix, "required": probe.required,
                "action": probe.action, "code": probe.code,
                "config": None if probe.config is None else {
                    "base_url": probe.config.base_url, "model": probe.config.model,
                    "api_key": probe.config.api_key, "timeout_seconds": probe.config.timeout_seconds,
                },
            }
            for probe in config.services
        ],
    }


def health_config_from_payload(data: Mapping[str, Any]) -> HealthConfig:
    """Rebuild the snapshot inside the probe child from validated parent data."""
    if data.get("v") != 1 or not isinstance(data.get("services"), list):
        raise ValueError("Unsupported health configuration payload")
    storage_data = data.get("storage")
    storage = None if storage_data is None else StorageConfig(**dict(storage_data))
    services = []
    for item in data["services"]:
        raw = item.get("config")
        config = None if raw is None else ServiceConfig(
            base_url=raw["base_url"], model=raw["model"], api_key=raw.get("api_key", ""),
            timeout_seconds=raw.get("timeout_seconds", 60.0),
        )
        services.append(ServiceProbe(
            key=item["key"], prefix=item["prefix"], required=bool(item["required"]),
            action=item["action"], code=item.get("code"), config=config,
        ))
    return HealthConfig(database_url=str(data.get("database_url") or ""), storage=storage, services=tuple(services))
