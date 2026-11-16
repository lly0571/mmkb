from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy.orm import Session

from mmkb.api import create_app
from mmkb.config import AppConfig, load_embedding_config
from mmkb.errors import ResourceError
from mmkb.providers import ProviderError
from mmkb.search import SearchService


@pytest.fixture
def image_bytes():
    source = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(source, format="PNG")
    return source.getvalue()


@pytest.fixture
def dependencies():
    session = MagicMock(spec=Session)
    session.__enter__.return_value = session
    session.execute.return_value = [SimpleNamespace(
        id=uuid4(), input_kind="image", title="title", tags=["tag"],
        user_text=None, caption="caption", ocr_text=None, distance=0.25,
    )]
    sessions = Mock(return_value=session)
    model = Mock()
    model.embed.return_value = [1.0] + [0.0] * 2047
    factory = Mock(return_value=model)
    config = AppConfig(database_url="postgresql://unused")
    service = SearchService(sessions, config, client_factory=factory)
    yield config, service, factory, model, sessions
    service.close()


@pytest.mark.parametrize("query", ["text", "image", "joint"])
def test_http_search_bridges_query_bytes_and_returns_safe_links(dependencies, image_bytes, query):
    config, service, factory, model, sessions = dependencies
    data = {"text": "  查询  ", "tags": [" tag ", "tag"], "top_k": "1"} if query != "image" else {}
    files = {"file": ("wrong.txt", image_bytes, "text/plain")} if query != "text" else None
    with TestClient(create_app(config, sessions, Mock(), search_service=service)) as client:
        response = client.post("/api/search", data=data, files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "vector" and body["items"][0]["score"] == 0.75
    assert body["items"][0]["original_url"].endswith("/original")
    assert body["items"][0]["detail_url"].startswith("/api/resources/")
    factory.assert_called_once()
    model.embed.assert_called_once()
    args = model.embed.call_args.kwargs
    assert args["purpose"] == "query"
    assert args["text"] == (None if query == "image" else "查询")
    if query != "text":
        assert args["image"].data == image_bytes and args["image"].mime_type == "image/png"
    else:
        assert args["image"] is None


@pytest.mark.parametrize("data,status", [({}, 422), ({"text": "  "}, 422),
    ({"text": "q", "top_k": "0"}, 422), ({"text": "q", "top_k": "101"}, 422)])
def test_invalid_queries_do_not_initialize_model(dependencies, data, status):
    config, service, factory, _, sessions = dependencies
    with TestClient(create_app(config, sessions, Mock(), service)) as client:
        assert client.post("/api/search", data=data).status_code == status
    factory.assert_not_called()
    sessions.assert_not_called()


@pytest.mark.parametrize("code,status", [("timeout", 504), ("connection_error", 503),
    ("invalid_response", 502), ("input_too_large", 413), ("invalid_input", 422)])
def test_provider_errors_are_explicit_and_never_become_empty_results(dependencies, code, status):
    config, service, _, model, sessions = dependencies
    model.embed.side_effect = ProviderError("embedding", code, "Safe message")
    with TestClient(create_app(config, sessions, Mock(), service)) as client:
        response = client.post("/api/search", data={"text": "q"})
    assert response.status_code == status
    assert response.json()["code"] == f"embedding_{code}"
    sessions.assert_not_called()


def test_limits_and_invalid_image_are_checked_before_model_creation(dependencies, image_bytes):
    config, service, factory, _, sessions = dependencies
    service.config = replace(config, max_text_chars=2, max_upload_bytes=len(image_bytes) - 1)
    with TestClient(create_app(config, sessions, Mock(), service)) as client:
        assert client.post("/api/search", data={"text": "long"}).status_code == 413
        assert client.post("/api/search", files={"file": ("x.png", image_bytes)}).status_code == 413
        assert client.post("/api/search", files={"file": ("x.png", b"invalid")}).status_code == 415
    factory.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_upload_stream_is_closed_for_both_outcomes(dependencies, image_bytes, failure):
    config, _, _, _, sessions = dependencies
    streams = []

    def query(**kwargs):
        streams.append(kwargs["source"])
        if failure:
            raise ResourceError("test_error", "safe", 503)
        return {"mode": "vector", "items": []}

    service = Mock()
    service.search.side_effect = query
    with TestClient(create_app(config, sessions, Mock(), service)) as client:
        response = client.post("/api/search", files={"file": ("x.png", image_bytes)})
    assert response.status_code == (503 if failure else 200)
    assert streams[0].closed
    service.close.assert_not_called()  # An injected service belongs to the caller.


def test_default_service_is_lazy_and_closes_its_client(dependencies, monkeypatch):
    config, _, factory, model, sessions = dependencies
    monkeypatch.setattr("mmkb.search.build_query_client", factory)
    app = create_app(config, sessions, Mock())
    app.state.resources.list = Mock(return_value={"items": [], "total": 0, "limit": 20, "offset": 0})
    with TestClient(app) as client:
        assert client.get("/api/resources").status_code == 200
        factory.assert_not_called()
        assert client.post("/api/search", data={"text": "query"}).status_code == 200
    model.close.assert_called_once()


def test_missing_model_configuration_is_sanitized_and_resources_still_work(dependencies, monkeypatch):
    config, _, _, _, sessions = dependencies
    factory = Mock(side_effect=ValueError("private deployment details"))
    monkeypatch.setattr("mmkb.search.build_query_client", factory)
    app = create_app(config, sessions, Mock())
    app.state.resources.list = Mock(return_value={"items": [], "total": 0, "limit": 20, "offset": 0})
    with TestClient(app) as client:
        assert client.get("/api/resources").status_code == 200
        response = client.post("/api/search", data={"text": "query"})
    assert response.status_code == 503 and response.json()["code"] == "model_config"
    assert "private" not in response.text


def test_client_initialization_is_thread_safe(dependencies):
    _, service, factory, _, _ = dependencies
    with ThreadPoolExecutor(max_workers=8) as pool:
        bridges = list(pool.map(lambda _: service._embedder(), range(32)))
    factory.assert_called_once()
    assert all(bridge.client is bridges[0].client for bridge in bridges)


def test_query_configuration_does_not_require_optional_vision(monkeypatch):
    monkeypatch.setattr("mmkb.config.load_dotenv", Mock())
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-model")
    monkeypatch.setenv("CAPTION_BASE_URL", "incomplete")
    monkeypatch.delenv("CAPTION_MODEL", raising=False)
    assert load_embedding_config().model == "test-model"
