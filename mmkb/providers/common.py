"""Small shared HTTP and image helpers; no workflow or persistence logic."""
import base64
import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from mmkb.config import ServiceConfig

ImageKind = Literal["structured_image", "visual_image", "visdoc"]

ErrorCode = Literal[
    "invalid_input", "input_too_large", "timeout", "connection_error",
    "http_error", "invalid_response", "output_truncated", "model_not_found",
    "unsupported_model",
]
logger = logging.getLogger(__name__)
MODELS_PATH = "models"


class ProviderError(Exception):
    """Safe to display; never includes request bodies or raw service errors."""

    def __init__(self, capability: str, code: ErrorCode, message: str, *, status_code: int | None = None):
        self.capability = capability
        self.code = code
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{capability}: {message}{status}")


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime_type: str


def image_data_url(image: ImageInput, capability: str) -> str:
    """Encode original image bytes as the data URL the services expect."""
    if not isinstance(image, ImageInput) or not isinstance(image.data, bytes) or not image.data:
        raise ProviderError(capability, "invalid_input", "Provide non-empty image bytes")
    if image.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ProviderError(capability, "invalid_input", "Use PNG, JPEG or WebP images")
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime_type};base64,{encoded}"


def image_content(image: ImageInput, capability: str) -> dict:
    return {"type": "image_url", "image_url": {"url": image_data_url(image, capability)}}


def model_list_url(base_url: str) -> str:
    """Single URL rule for the model list, shared by business and health probes."""
    return base_url.rstrip("/") + "/" + MODELS_PATH


def validate_model_list(capability: str, payload: object, model: str) -> None:
    """Reject malformed model lists and models the service does not serve."""
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in models):
        raise ProviderError(capability, "invalid_response", "Invalid model list from service")
    if not any(item["id"] == model for item in models):
        raise ProviderError(capability, "model_not_found", "Configured model is not served; check the model ID")


class ServiceClient:
    def __init__(self, config: ServiceConfig, client: httpx.Client | None = None):
        self.config = config
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()
        self._timeout = httpx.Timeout(config.timeout_seconds, connect=5.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(self, capability: str, method: str, path: str, *, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        started = time.monotonic()
        error_code = None
        try:
            response = self._client.request(
                method, self.config.base_url.rstrip("/") + "/" + path,
                json=body, headers=headers, timeout=self._timeout,
            )
            if not response.is_success:
                code: ErrorCode = "http_error"
                message = "Model service rejected the request; check service availability and configuration"
                if response.status_code == 501:
                    code = "unsupported_model"
                    message = "Served model does not support this capability; check the model and server options"
                elif response.status_code in {400, 413, 422}:
                    code = "invalid_input"
                    message = "Model service rejected the input; check format and model input limits"
                    # Inspect only for categorization; never expose the remote error text.
                    detail = response.text.lower()
                    if response.status_code == 413 or any(term in detail for term in (
                        "context_length_exceeded", "maximum context length", "max_model_len",
                        "too many tokens", "maximum input length", "input is too long",
                    )):
                        code = "input_too_large"
                        message = "Input exceeds model limits; reduce the input size"
                raise ProviderError(capability, code, message, status_code=response.status_code)
            try:
                data = response.json()
            except ValueError:
                raise ProviderError(capability, "invalid_response", "Model service returned invalid JSON") from None
            if not isinstance(data, dict):
                raise ProviderError(capability, "invalid_response", "Expected a JSON object from model service")
            return data
        except httpx.TimeoutException:
            error_code = "timeout"
            raise ProviderError(capability, "timeout", "Model request timed out; retry or increase the timeout") from None
        except httpx.RequestError:
            error_code = "connection_error"
            raise ProviderError(capability, "connection_error", "Cannot reach model service; check address and network") from None
        except ProviderError as exc:
            error_code = exc.code
            raise
        finally:
            logger.info("model capability=%s elapsed_seconds=%.3f error_code=%s", capability, time.monotonic() - started, error_code)

    def check_connection(self, capability: str) -> None:
        validate_model_list(capability, self.request(capability, "GET", MODELS_PATH), self.config.model)
