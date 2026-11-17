"""Image classification policy; adapters own protocols, the Worker owns writes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
import math
import time
from typing import Any

from mmkb.config import RoutingConfig
from mmkb.image_types import CLASSIFICATION_STATE, IMAGE_CATEGORIES, IMAGE_WORKFLOWS, ROUTING_POLICY_VERSION
from mmkb.providers import ImageInput, ProviderError, Providers, SystemOneProvider, VisionProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    workflow: str
    backend: str
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    label_mass: float | None = None
    argmax_is_label: bool | None = None

    def diagnostics(self) -> dict[str, Any]:
        values = asdict(self)
        values["diagnostics_missing"] = [
            name for name in ("label_mass", "argmax_is_label") if values[name] is None
        ]
        return values


class SystemOneClassifier:
    def __init__(self, provider: SystemOneProvider):
        self.provider = provider

    def classify(self, image: ImageInput) -> ClassificationResult:
        answer = self.provider.choose(
            CLASSIFICATION_STATE, IMAGE_CATEGORIES, images=[image], question_id="image_type",
        )
        return ClassificationResult(
            answer.choice, "systemone", dict(answer.probabilities), answer.confidence,
            answer.label_mass, answer.argmax_is_label,
        )


class LLMClassifier:
    def __init__(self, provider: VisionProvider):
        self.provider = provider

    def classify(self, image: ImageInput) -> ClassificationResult:
        return ClassificationResult(self.provider.classify(image), "llm")


def decision_rejection(result: ClassificationResult, config: RoutingConfig) -> str | None:
    """The protocol checks numbers; policy also verifies winner consistency."""
    probabilities = result.probabilities
    if result.workflow not in IMAGE_WORKFLOWS or not probabilities or set(probabilities) != IMAGE_WORKFLOWS:
        return "invalid_response"
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           or not 0 <= v <= 1 for v in probabilities.values()):
        return "invalid_response"
    if abs(math.fsum(probabilities.values()) - 1) > 1e-3:
        return "invalid_response"
    chosen = probabilities[result.workflow]
    if chosen != max(probabilities.values()):
        return "invalid_response"
    if result.confidence is None or not math.isfinite(result.confidence) or not 0 <= result.confidence <= 1:
        return "invalid_response"
    if result.label_mass is not None and (not math.isfinite(result.label_mass) or not 0 <= result.label_mass <= 1):
        return "invalid_response"
    if result.argmax_is_label is False:
        return "argmax_not_label"
    if result.label_mass is not None and result.label_mass < config.min_label_mass:
        return "low_label_mass"
    if chosen < config.min_probability or result.confidence < config.min_confidence:
        return "low_confidence"
    return None


@dataclass(frozen=True)
class RouteSelection:
    workflow: str
    source: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    decision: dict[str, Any] | None = None

    def metadata(self, revision: int, config: RoutingConfig) -> dict[str, Any]:
        return {
            "policy_version": ROUTING_POLICY_VERSION,
            "revision": revision,
            "configured_backend": config.backend,
            "workflow": self.workflow,
            "source": self.source,
            "thresholds": {key: value for key, value in asdict(config).items() if key != "backend"},
            "attempts": self.attempts,
            "decision": self.decision,
        }


class ImageRouter:
    def __init__(self, providers: Providers, config: RoutingConfig):
        self.providers = providers
        self.config = config

    def select(self, image: ImageInput) -> RouteSelection:
        attempts, warnings = [], []
        decision = None
        backends = ("systemone", "llm") if self.config.backend == "systemone" else ("llm",)
        for backend in backends:
            provider = self.providers.decision if backend == "systemone" else self.providers.vision
            stage = "decision" if backend == "systemone" else "classify"
            if provider is None:
                attempts.append({"backend": backend, "status": "skipped", "code": "unavailable", "elapsed_ms": 0})
                if backend == "llm":
                    warnings.append({"stage": stage, "code": "unavailable"})
                continue
            started = time.monotonic()
            try:
                classifier = SystemOneClassifier(provider) if backend == "systemone" else LLMClassifier(provider)
                result = classifier.classify(image)
                code = decision_rejection(result, self.config) if backend == "systemone" else (
                    None if result.workflow in IMAGE_WORKFLOWS else "invalid_response"
                )
                # Only validated, bounded protocol values are exposed to clients.
                if backend == "systemone" and code != "invalid_response":
                    decision = result.diagnostics()
            except ProviderError as exc:
                code = exc.code
            elapsed_ms = round((time.monotonic() - started) * 1000)
            attempts.append({"backend": backend, "status": "accepted" if code is None else "rejected",
                             "code": code, "elapsed_ms": elapsed_ms})
            logger.info("routing backend=%s code=%s elapsed_ms=%s", backend, code or "accepted", elapsed_ms)
            if code is None:
                logger.info("routing source=%s workflow=%s", backend, result.workflow)
                return RouteSelection(result.workflow, backend, attempts, warnings, decision)
            warnings.append({"stage": stage, "code": code})
        logger.info("routing source=default workflow=visual_image")
        return RouteSelection("visual_image", "default", attempts, warnings, decision)
