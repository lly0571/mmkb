"""Initial retrieval defaults; no database-backed configuration registry."""
import os
from dataclasses import dataclass
from pathlib import Path

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


def load_retrieval_config() -> RetrievalConfig:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)
    if int(os.environ.get("EMBEDDING_DIMENSIONS", EMBEDDING_DIMENSIONS)) != EMBEDDING_DIMENSIONS:
        raise ValueError("EMBEDDING_DIMENSIONS must match the halfvec(2048) schema")
    service_config = {
        name: os.environ.get(name, "").strip()
        for name in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
    }
    missing = [name for name, value in service_config.items() if not value]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " in the environment or .env.local")
    return RetrievalConfig(
        embedding_base_url=service_config["EMBEDDING_BASE_URL"],
        embedding_model=service_config["EMBEDDING_MODEL"],
    )
