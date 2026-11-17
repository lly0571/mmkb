"""Health configuration snapshot: per-service parsing and the optional explicit switches."""
import os
from unittest.mock import patch

import pytest

from mmkb.config import (
    AppConfig, HealthConfig, ServiceConfig, health_config_from_app, health_config_from_payload,
    health_config_payload, load_model_config, resolve_health_config,
)
from mmkb.health_contract import check_result

BASE = {"DATABASE_URL": "postgresql://user:secret@db.internal:5432/mmkb",
        "EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}


def snapshot(**overrides):
    env = dict(BASE, **{key: ("" if value is None else str(value)) for key, value in overrides.items()})
    return resolve_health_config(env)


def probe(config: HealthConfig, key: str):
    return next(item for item in config.services if item.key == key)


def service(config: HealthConfig, key: str) -> ServiceConfig:
    found = probe(config, key).config
    assert found is not None
    return found


@pytest.mark.parametrize("value", ["", "  "])
def test_optional_services_default_to_expected_absence(value):
    config = snapshot(CAPTION_BASE_URL=value, CAPTION_MODEL=value, DECISION_ENABLED=value)
    assert [(probe(config, key).action, probe(config, key).code) for key in ("vision", "decision")] == [
        ("not_configured", "optional_not_configured"), ("not_configured", "optional_not_configured")]
    assert probe(config, "embedding").action == "probe"
    assert service(config, "embedding").base_url == "http://model.internal:8000/v1"
    assert config.database_url == BASE["DATABASE_URL"]


@pytest.mark.parametrize("value", ["false", "FALSE", "0"])
def test_explicit_false_disables_without_validating_residual_settings(value):
    config = snapshot(CAPTION_ENABLED=value, CAPTION_BASE_URL="http://broken", CAPTION_MODEL="vision-model")
    assert (probe(config, "vision").action, probe(config, "vision").code) == ("disabled", "service_disabled")
    assert probe(config, "vision").config is None
    # The strict loader and the diagnostic parse must agree, or the page would lie about the Worker.
    with patch.dict(os.environ, dict(BASE, CAPTION_ENABLED=value, CAPTION_BASE_URL="http://broken",
                                       CAPTION_MODEL="vision-model"), clear=True):
        assert load_model_config().caption is None


@pytest.mark.parametrize("value", ["true", "1", "True"])
def test_explicit_true_with_partial_configuration_is_reported_as_missing(value):
    config = snapshot(DECISION_ENABLED=value, DECISION_MODEL="decision-model")
    assert (probe(config, "decision").action, probe(config, "decision").code) == (
        "not_configured", "required_not_configured")
    # Aggregation counts an explicitly requested service as a degradation, unlike a plain absence.
    check = check_result("decision", "required_not_configured")
    assert check.state == "not_configured"
    with patch.dict(os.environ, dict(BASE, DECISION_ENABLED=value, DECISION_MODEL="decision-model"),
                      clear=True):
        with pytest.raises(ValueError, match="DECISION"):
            load_model_config()


@pytest.mark.parametrize("value", ["yes", "on", "2", "-1"])
def test_invalid_switch_is_down_not_disabled(value):
    config = snapshot(CAPTION_ENABLED=value, CAPTION_BASE_URL="http://vision.internal:8001/v1",
                      CAPTION_MODEL="vision-model")
    assert (probe(config, "vision").action, probe(config, "vision").code) == ("invalid", "invalid_config")
    with patch.dict(os.environ, dict(BASE, CAPTION_ENABLED=value), clear=True):
        with pytest.raises(ValueError, match="CAPTION_ENABLED"):
            load_model_config()


def test_complete_optional_configuration_is_probed():
    config = snapshot(CAPTION_ENABLED="true", CAPTION_BASE_URL="http://vision.internal:8001/v1",
                      CAPTION_MODEL="vision-model", DECISION_BASE_URL="http://d3.internal:8004/v1",
                      DECISION_MODEL="vllm-sr/d3-mini")
    assert probe(config, "vision").action == "probe"
    assert probe(config, "decision").action == "probe"
    assert service(config, "vision").timeout_seconds == 120.0
    assert service(config, "decision").timeout_seconds == 60.0


@pytest.mark.parametrize("overrides", [
    {"CAPTION_BASE_URL": "not-a-url", "CAPTION_MODEL": "vision-model"},
    {"CAPTION_BASE_URL": "http://vision.internal/v1", "CAPTION_MODEL": " "},
    {"CAPTION_BASE_URL": "http://vision.internal/v1", "CAPTION_MODEL": "vision-model",
     "CAPTION_TIMEOUT_SECONDS": "0"},
])
def test_one_broken_optional_service_does_not_hide_the_other_components(overrides):
    config = snapshot(**overrides)
    assert probe(config, "vision").code == "invalid_config"
    assert probe(config, "embedding").action == "probe"
    assert probe(config, "decision").action == "not_configured"
    assert config.storage is not None and config.storage.storage_backend == "local"


def test_required_embedding_is_reported_independently_and_never_disabled():
    config = snapshot(EMBEDDING_MODEL="")
    assert (probe(config, "embedding").action, probe(config, "embedding").code) == (
        "not_configured", "required_not_configured")
    # Embedding has no switch: a stray value must not silence a required component.
    config = snapshot(EMBEDDING_ENABLED="false")
    assert probe(config, "embedding").action == "probe"
    for value in ("1024", "bad"):
        config = snapshot(EMBEDDING_DIMENSIONS=value)
        assert probe(config, "embedding").code == "invalid_config"


def test_storage_failure_is_isolated_from_models_and_database():
    config = snapshot(STORAGE_BACKEND="minio")
    assert config.storage is None
    assert config.database_url == BASE["DATABASE_URL"]
    assert probe(config, "embedding").action == "probe"
    assert snapshot(STORAGE_BACKEND="s3", S3_BUCKET="bucket").storage.s3_bucket == "bucket"


def test_snapshot_round_trips_through_the_probe_child_payload():
    config = snapshot(CAPTION_BASE_URL="http://vision.internal:8001/v1", CAPTION_MODEL="vision-model",
                      CAPTION_API_KEY="caption-secret", STORAGE_BACKEND="s3", S3_BUCKET="bucket",
                      S3_CREDENTIAL_PROFILE="rustfs")
    restored = health_config_from_payload(health_config_payload(config))
    assert restored == config
    assert service(restored, "vision").api_key == "caption-secret"
    assert restored.storage.s3_credential_profile == "rustfs"
    assert "caption-secret" not in repr(restored)


def test_database_url_absent_is_a_configuration_fault_not_a_crash():
    env = {key: value for key, value in BASE.items() if key != "DATABASE_URL"}
    assert resolve_health_config(env).database_url == ""


def test_blank_and_disabled_values_are_kept_distinct():
    env = dict(BASE, CAPTION_ENABLED="", CAPTION_BASE_URL="", DECISION_ENABLED="0",
               DECISION_BASE_URL="http://d3.internal/v1", DECISION_MODEL="d3-mini")
    config = resolve_health_config(env)
    assert probe(config, "vision").action == "not_configured"
    assert probe(config, "decision").action == "disabled"


def test_the_health_snapshot_inherits_database_and_storage_from_the_app_config():
    served = AppConfig(database_url="postgresql://app-user@app-db:5432/app_db",
                       storage_backend="s3", storage_profile_name="app-profile",
                       s3_endpoint_url="http://s3.internal:9000", s3_bucket="app-bucket")
    env = {"DATABASE_URL": "postgresql://other@other-db:5432/other",
           "STORAGE_BACKEND": "local", "LOCAL_STORAGE_ROOT": "/tmp/other",
           "EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}
    snapshot = health_config_from_app(served, env)
    assert snapshot.database_url == served.database_url
    assert snapshot.storage is not None
    assert snapshot.storage.storage_profile_name == "app-profile"
    assert snapshot.storage.s3_bucket == "app-bucket"


def test_the_health_snapshot_defaults_a_blank_profile_name_and_drops_invalid_storage():
    served = AppConfig(database_url="postgresql://app@app-db:5432/app", storage_backend="s3",
                       storage_profile_name="", s3_endpoint_url="http://s3:9000", s3_bucket="b")
    env = {"EMBEDDING_BASE_URL": "http://model.internal:8000/v1", "EMBEDDING_MODEL": "embed-model"}
    assert health_config_from_app(served, env).storage.storage_profile_name == "s3-default"

    broken = AppConfig(database_url="postgresql://app@app-db:5432/app", storage_backend="swift")
    snapshot = health_config_from_app(broken, env)
    # An unusable storage setting hides only the storage check; the rest of the report still runs.
    assert snapshot.storage is None
    assert snapshot.database_url == broken.database_url
    assert [item.key for item in snapshot.services] == ["embedding", "vision", "decision"]
