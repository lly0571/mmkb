from dataclasses import replace
import json
from unittest.mock import Mock, patch

import httpx
import pytest

from mmkb.config import RoutingConfig, ServiceConfig, load_routing_config
from mmkb.image_types import IMAGE_CATEGORIES
from mmkb.providers import ImageInput, ProviderError, Providers
from mmkb.providers.systemone import Answer, SystemOneProvider
from mmkb.providers.vision import VisionProvider
from mmkb.routing import ClassificationResult, ImageRouter, decision_rejection

IMAGE = ImageInput(b"original", "image/png")


def answer(workflow="info_rich_image", **changes):
    probabilities = {name: 0.02 for name in IMAGE_CATEGORIES}
    probabilities[workflow] = 0.94
    return replace(Answer("image_type", "choice", probabilities, 0.90,
                          choice=workflow, label_mass=0.95, argmax_is_label=True), **changes)


def providers():
    embedding, vision, decision = Mock(), Mock(), Mock()
    embedding.embed.return_value = [1.0] + [0.0] * 2047
    vision.classify.return_value = "visual_image"
    decision.choose.return_value = answer()
    return Providers(embedding, vision, decision)


@pytest.mark.parametrize("workflow", list(IMAGE_CATEGORIES))
def test_default_systemone_routes_all_four_classes_without_llm(workflow):
    p = providers()
    p.decision.choose.return_value = answer(workflow)
    result = ImageRouter(p, RoutingConfig()).select(IMAGE)
    assert (result.workflow, result.source) == (workflow, "systemone")
    p.vision.classify.assert_not_called()
    args, kwargs = p.decision.choose.call_args
    assert args[1] == IMAGE_CATEGORIES and kwargs["images"] == [IMAGE]
    assert "instructions" not in kwargs
    assert result.metadata(4, RoutingConfig())["revision"] == 4


def test_llm_mode_bypasses_decision_and_does_not_fabricate_probabilities():
    p = providers()
    p.decision.choose.side_effect = AssertionError("must not call")
    route = ImageRouter(p, RoutingConfig(backend="llm")).select(IMAGE)
    assert route.source == "llm" and route.decision is None
    assert [a["backend"] for a in route.attempts] == ["llm"]


@pytest.mark.parametrize("changes,code", [
    ({"confidence": 0.1}, "low_confidence"),
    ({"label_mass": 0.2}, "low_label_mass"),
    ({"argmax_is_label": False}, "argmax_not_label"),
    ({"choice": "text"}, "invalid_response"),
    ({"choice": "visual_image"}, "invalid_response"),
    ({"probabilities": {name: 0.25 for name in IMAGE_CATEGORIES}}, "low_confidence"),
])
def test_rejected_decision_falls_back_with_explainable_warning(changes, code):
    p = providers()
    p.decision.choose.return_value = answer(**changes)
    route = ImageRouter(p, RoutingConfig()).select(IMAGE)
    assert route.source == "llm"
    assert route.warnings == [{"stage": "decision", "code": code}]
    assert route.attempts[0]["status"] == "rejected"


def test_missing_diagnostics_accepts_available_confidence_and_records_absence():
    p = providers()
    p.decision.choose.return_value = answer(label_mass=None, argmax_is_label=None)
    route = ImageRouter(p, RoutingConfig()).select(IMAGE)
    assert route.source == "systemone" and route.warnings == []
    assert route.decision["diagnostics_missing"] == ["label_mass", "argmax_is_label"]
    assert route.decision["label_mass"] is None


@pytest.mark.parametrize("code", ["timeout", "connection_error", "invalid_response", "unsupported_model"])
def test_provider_errors_and_total_failure_preserve_image_fallback(code):
    p = providers()
    p.decision.choose.side_effect = ProviderError("decision", code, "private remote response")
    p.vision.classify.side_effect = ProviderError("classify", "timeout", "private")
    route = ImageRouter(p, RoutingConfig()).select(IMAGE)
    assert (route.workflow, route.source) == ("visual_image", "default")
    assert route.warnings == [{"stage": "decision", "code": code}, {"stage": "classify", "code": "timeout"}]
    assert "private" not in json.dumps(route.metadata(1, RoutingConfig()))


def test_no_optional_providers_still_selects_image_encoding():
    route = ImageRouter(Providers(Mock()), RoutingConfig()).select(IMAGE)
    assert route.source == "default"
    assert route.warnings == [{"stage": "classify", "code": "unavailable"}]
    assert all(attempt["status"] == "skipped" for attempt in route.attempts)




def test_real_protocol_adapters_reach_systemone_and_fall_back_to_chat():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/systemone"):
            return httpx.Response(200, json={"answers": {"image_type": {
                "type": "choice", "choice": "info_rich_image", "confidence": 0.1,
                "probabilities": {name: (0.94 if name == "info_rich_image" else 0.02) for name in IMAGE_CATEGORIES},
            }}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "visdoc"}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        service = ServiceConfig("https://model.example/v1", "vllm-sr/d3-mini")
        p = Providers(Mock(), VisionProvider(service, client=client), SystemOneProvider(service, client=client))
        route = ImageRouter(p, RoutingConfig()).select(IMAGE)
    assert route.source == "llm" and route.workflow == "visdoc"
    assert [r.url.path for r in requests] == ["/v1/systemone", "/v1/chat/completions"]
    assert "instructions" not in json.loads(requests[0].content)
    assert "images" in json.loads(requests[0].content)


def test_routing_config_defaults_and_environment_are_independent_of_model_credentials(monkeypatch):
    with patch("mmkb.config.load_dotenv"):
        for key in ("IMAGE_ROUTING_BACKEND", "ROUTING_MIN_PROBABILITY", "ROUTING_MIN_CONFIDENCE", "ROUTING_MIN_LABEL_MASS"):
            monkeypatch.delenv(key, raising=False)
        assert load_routing_config() == RoutingConfig()
        monkeypatch.setenv("IMAGE_ROUTING_BACKEND", "LLM")
        monkeypatch.setenv("ROUTING_MIN_CONFIDENCE", "0.75")
        assert load_routing_config() == RoutingConfig(backend="llm", min_confidence=0.75)


@pytest.mark.parametrize("kwargs", [{"backend": "auto"}, {"min_probability": -0.1},
    {"min_confidence": float("nan")}, {"min_label_mass": float("inf")}, {"min_confidence": True}])
def test_invalid_policy_is_a_configuration_error(kwargs):
    with pytest.raises(ValueError):
        RoutingConfig(**kwargs)


def test_probability_and_confidence_are_different_gates():
    result = ClassificationResult("info_rich_image", "systemone", dict(answer().probabilities), 0.2, 0.95, True)
    assert decision_rejection(result, RoutingConfig()) == "low_confidence"
