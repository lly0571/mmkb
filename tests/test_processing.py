from unittest.mock import Mock
from uuid import uuid4

import pytest

from mmkb.embedding import EmbeddingError
from mmkb.processing import Processor, ResourceSnapshot
from mmkb.providers import ImageInput, ProviderError, Providers


def vector():
    return [3.0, 4.0] + [0.0] * 2046


@pytest.fixture
def components():
    embedding, vision = Mock(), Mock()
    embedding.embed.return_value = vector()
    vision.classify.return_value = "structured_image"
    vision.caption.return_value = "结构描述"
    vision.ocr.return_value = "OCR 原文"
    return Processor(Providers(embedding, vision)), embedding, vision


@pytest.mark.parametrize("kind,workflow,text,encoded_text,capability", [
    ("image", "structured_image", None, "结构描述", "caption"),
    ("image", "visual_image", None, None, "caption"),
    ("image", "visdoc", None, None, "ocr"),
    ("text_image", "text_image", "用户配文", "用户配文", None),
    ("text", "text", "完整文字", "完整文字", None),
])
def test_workflows_keep_visual_and_text_inputs_separate(components, kind, workflow, text, encoded_text, capability):
    processor, embedding, vision = components
    snapshot = ResourceSnapshot(uuid4(), 7, kind, workflow, "测试标题", ("标签",), text)
    image = ImageInput(b"original", "image/png") if kind != "text" else None
    result = processor.process(snapshot, image=image)
    embedding.embed.assert_called_once_with(purpose="document", image=image, text=encoded_text)
    vision.classify.assert_not_called()
    assert result.resource_id == snapshot.resource_id and result.revision == 7
    assert result.embedding[:2] == pytest.approx([0.6, 0.8])
    assert "标题" in result.segmented_text and "标签" in result.segmented_text
    assert result.warnings == []
    if capability == "caption":
        vision.caption.assert_called_once_with(image, structured=workflow == "structured_image")
        vision.ocr.assert_not_called()
    elif capability == "ocr":
        vision.ocr.assert_called_once_with(image)
        vision.caption.assert_not_called()
    else:
        vision.caption.assert_not_called()
        vision.ocr.assert_not_called()


@pytest.mark.parametrize("field,workflow", [("caption", "structured_image"), ("ocr_text", "visdoc")])
@pytest.mark.parametrize("value", ["", "人工修正"])
def test_non_null_manual_fields_are_never_regenerated(components, field, workflow, value):
    processor, embedding, vision = components
    snapshot = ResourceSnapshot(uuid4(), 2, "image", workflow, **{field: value})
    result = processor.process(snapshot, image=ImageInput(b"original", "image/png"))
    assert getattr(result, field) == value
    vision.caption.assert_not_called()
    vision.ocr.assert_not_called()
    expected = value if field == "caption" and value else None
    assert embedding.embed.call_args.kwargs["text"] == expected


def test_classification_failure_falls_back_and_caption_failure_is_a_warning(components):
    processor, embedding, vision = components
    vision.classify.side_effect = ProviderError("classify", "timeout", "safe")
    vision.caption.side_effect = ProviderError("caption", "output_truncated", "safe")
    result = processor.process(ResourceSnapshot(uuid4(), 1, "image"), image=ImageInput(b"image", "image/png"))
    assert result.workflow == "visual_image"
    assert result.warnings == [{"stage": "classify", "code": "timeout"},
                               {"stage": "caption", "code": "output_truncated"}]
    assert embedding.embed.call_args.kwargs["text"] is None


def test_disabled_vision_still_embeds_and_auto_routes(components):
    processor, embedding, _ = components
    processor.providers.vision = None
    result = processor.process(ResourceSnapshot(uuid4(), 1, "image"), image=ImageInput(b"image", "image/png"))
    assert result.workflow == "visual_image" and len(result.warnings) == 2
    embedding.embed.assert_called_once()


def test_classified_document_uses_ocr_without_feeding_it_to_embedding(components):
    processor, embedding, vision = components
    vision.classify.return_value = "visdoc"
    result = processor.process(ResourceSnapshot(uuid4(), 1, "image"), image=ImageInput(b"image", "image/png"))
    assert result.ocr_text == "OCR 原文" and "OCR" in result.segmented_text
    assert embedding.embed.call_args.kwargs["text"] is None


def test_oversized_optional_output_is_discarded_instead_of_truncated(components):
    processor, embedding, vision = components
    processor.max_text_chars = 2
    result = processor.process(ResourceSnapshot(uuid4(), 1, "image", "structured_image"),
                               image=ImageInput(b"image", "image/png"))
    assert result.caption is None
    assert result.warnings == [{"stage": "caption", "code": "input_too_large"}]
    assert embedding.embed.call_args.kwargs["text"] is None


@pytest.mark.parametrize("invalid", [[0.0] * 2048, [float("nan")] * 2048, [1.0]])
def test_invalid_vectors_abort_processing(components, invalid):
    processor, embedding, _ = components
    embedding.embed.return_value = invalid
    with pytest.raises(EmbeddingError):
        processor.process(ResourceSnapshot(uuid4(), 1, "text", user_text="text"))


def test_embedding_failure_is_required_not_a_warning(components):
    processor, embedding, _ = components
    embedding.embed.side_effect = ProviderError("embedding", "timeout", "safe")
    with pytest.raises(ProviderError):
        processor.process(ResourceSnapshot(uuid4(), 1, "text", user_text="text"))
