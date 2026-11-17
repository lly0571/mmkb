"""Whole-resource workflows. Model work returns values, never database writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mmkb.embedding import normalize_embedding
from mmkb.config import RoutingConfig
from mmkb.errors import ResourceError
from mmkb.providers import ImageInput, ProviderError, Providers
from mmkb.resources import input_route
from mmkb.retrieval import segment_text
from mmkb.routing import ImageRouter, RouteSelection


@dataclass(frozen=True)
class ResourceSnapshot:
    resource_id: UUID
    revision: int
    input_kind: str
    workflow: str | None = None
    title: str = ""
    tags: tuple[str, ...] = ()
    user_text: str | None = None
    caption: str | None = None
    ocr_text: str | None = None


@dataclass(frozen=True)
class ProcessingResult:
    resource_id: UUID
    revision: int
    workflow: str
    caption: str | None
    ocr_text: str | None
    embedding: list[float]
    segmented_text: str
    warnings: list[dict[str, str]]
    routing: dict[str, Any] = field(default_factory=dict)
    encoding: str = ""


class Processor:
    def __init__(self, providers: Providers, *, max_text_chars: int = 100_000,
                 routing_config: RoutingConfig | None = None):
        self.providers = providers
        self.max_text_chars = max_text_chars
        self.routing_config = routing_config or RoutingConfig()

    def process(self, snapshot: ResourceSnapshot, *, image: ImageInput | None = None) -> ProcessingResult:
        kind, workflow = input_route(image is not None, snapshot.user_text, snapshot.workflow)
        if kind != snapshot.input_kind:
            raise ResourceError("invalid_resource", "Resource input does not match its stored input kind")
        warnings = []
        vision = self.providers.vision

        def optional(stage, operation):
            if vision is None:
                warnings.append({"stage": stage, "code": "unavailable"})
                return None
            try:
                value = operation()
                if not isinstance(value, str) or not value.strip():
                    raise ProviderError(stage, "invalid_response", "Expected text from vision service")
                if len(value) > self.max_text_chars:
                    raise ProviderError(stage, "input_too_large", "Generated text exceeds MAX_TEXT_CHARS")
                return value
            except ProviderError as exc:
                warnings.append({"stage": stage, "code": exc.code})
                return None

        if workflow is None:
            route = ImageRouter(self.providers, self.routing_config).select(image)
            workflow = route.workflow
            warnings.extend(route.warnings)
        else:
            route = RouteSelection(workflow, "preset" if kind == "image" else "input_kind")

        caption, ocr_text = snapshot.caption, snapshot.ocr_text
        if workflow == "structured_image" and caption is None:
            caption = optional("caption", lambda: vision.caption(image, structured=True))
        if workflow == "info_rich_image" and caption is None:
            caption = optional("caption", lambda: vision.caption_info_rich(image))

        text = None
        if workflow in {"text", "text_image"}:
            text = snapshot.user_text
        elif workflow in {"structured_image", "info_rich_image"} and caption and caption.strip():
            text = caption
        encoding = ("text_only" if image is None else "image_user_text" if kind == "text_image"
                    else "image_caption" if text is not None else "image_only")
        embedding = normalize_embedding(self.providers.embedding.embed(
            purpose="document", text=text, image=image,
        ))
        segments = [snapshot.title, *snapshot.tags, snapshot.user_text, ocr_text, caption]
        return ProcessingResult(
            snapshot.resource_id, snapshot.revision, workflow, caption, ocr_text, embedding,
            segment_text("\n".join(value for value in segments if value)), warnings,
            route.metadata(snapshot.revision, self.routing_config), encoding,
        )
