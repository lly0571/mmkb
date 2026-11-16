"""Caption, OCR and image classification using one vision chat service."""
from typing import cast

import httpx

from mmkb.config import ServiceConfig
from mmkb.providers.common import ImageInput, ImageKind, ProviderError, ServiceClient, image_content

CAPTION_PROMPT = "用中文描述图片的主体、场景和关键视觉信息。只输出描述正文，不编造无法确认的信息。"
STRUCTURED_PROMPT = "用中文描述图表或结构化图片的组成、关系、结构和趋势。只输出描述正文；无法辨认的数字不要猜测。"
OCR_PROMPT = "逐字提取图片中可辨认的全部文字，保留原语言和换行。只输出识别文字，不解释、不总结、不补写；没有文字时输出空字符串。"
CLASSIFY_PROMPT = (
    "将图片归为一类：structured_image（图表、流程图或结构关系图），"
    "visdoc（以段落、表格或文档文字为主的文本密集图片），visual_image（其他图片）。"
    "只输出一个上述英文标识，不附加解释或标点。"
)


class VisionProvider:
    def __init__(self, config: ServiceConfig, *, client: httpx.Client | None = None):
        self._service = ServiceClient(config, client)

    def _generate(self, capability: str, image: ImageInput, prompt: str, max_tokens: int) -> str:
        result = self._service.request(capability, "POST", "chat/completions", body={
            "model": self._service.config.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": [image_content(image, capability)]},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": max_tokens,
            # These extraction tasks need the final answer, not a reasoning trace.
            "chat_template_kwargs": {"enable_thinking": False},
        })
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError(capability, "invalid_response", "Expected exactly one chat result")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError(capability, "output_truncated", "Model output was truncated; reduce input or use a dedicated parser")
        message = choice.get("message")
        if (choice.get("finish_reason") != "stop" or not isinstance(message, dict)
                or "content" not in message or message.get("refusal")
                or message.get("tool_calls") or message.get("function_call")):
            raise ProviderError(capability, "invalid_response", "Expected completed text from vision service")
        content = message["content"]
        # Some chat servers serialize a zero-token, normally completed OCR as null.
        if capability == "ocr" and content is None:
            return ""
        if not isinstance(content, str):
            raise ProviderError(capability, "invalid_response", "Expected text from vision service")
        text = content.strip()
        if capability != "ocr" and not text:
            raise ProviderError(capability, "invalid_response", "Vision service returned empty text")
        return text

    def caption(self, image: ImageInput, *, structured: bool = False) -> str:
        return self._generate("caption", image, STRUCTURED_PROMPT if structured else CAPTION_PROMPT, 1024)

    def ocr(self, image: ImageInput) -> str:
        return self._generate("ocr", image, OCR_PROMPT, 4096)

    def classify(self, image: ImageInput) -> ImageKind:
        kind = self._generate("classify", image, CLASSIFY_PROMPT, 32)
        if kind not in {"structured_image", "visual_image", "visdoc"}:
            raise ProviderError("classify", "invalid_response", "Vision service returned an unknown image category")
        return cast(ImageKind, kind)

    def check_connection(self) -> None:
        self._service.check_connection("vision")

    def close(self) -> None:
        self._service.close()

    def __enter__(self) -> "VisionProvider":
        return self

    def __exit__(self, *args) -> None:
        self.close()
