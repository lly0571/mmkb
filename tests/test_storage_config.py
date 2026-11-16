from dataclasses import replace
import os
from unittest.mock import Mock, patch
from uuid import uuid4

from dotenv import load_dotenv
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from mmkb.config import PROJECT_ROOT, StorageConfig, load_storage_config
from mmkb.db.models import StorageProfile
from mmkb.errors import ResourceError, StorageConfigError, StorageError
from mmkb.storage import LocalStorage, S3Storage, StorageResolver, initialize_profile, main


@pytest.fixture
def storage_environment(monkeypatch):
    from mmkb import config as module
    loader = Mock()
    monkeypatch.setattr(module, "load_dotenv", loader)
    unrelated = {name: value for name, value in os.environ.items()
                 if not name.startswith(("STORAGE_", "LOCAL_STORAGE_", "S3_", "EMBEDDING_", "CAPTION_"))
                 and name != "DATABASE_URL"}
    # Restore variables added by load_dotenv as well as variables set by tests.
    with patch.dict(os.environ, unrelated, clear=True):
        yield loader


@pytest.fixture
def profile_sessions():
    # Exercise actual transactions without requiring PostgreSQL in default tests.
    # Only this isolated test table is created, never production metadata.create_all.
    engine = sa.create_engine("sqlite://", execution_options={"schema_translate_map": {"mmkb": None}})
    StorageProfile.__table__.create(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def test_storage_config_independent_of_models_and_database(storage_environment):
    config = load_storage_config()
    assert config.storage_backend == "local"
    assert config.storage_profile_name == "local-default"
    assert config.local_storage_root == "./data/assets"
    assert config.s3_addressing_style == "path" and config.s3_signature_version == "s3v4"
    assert config.s3_region == "us-east-1"
    storage_environment.assert_called_once_with(PROJECT_ROOT / ".env.local", override=False)


def test_backend_defaults_and_profile_selection(storage_environment, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    assert load_storage_config().storage_profile_name == "s3-default"
    monkeypatch.setenv("STORAGE_PROFILE_NAME", "existing-profile")
    assert load_storage_config().storage_profile_name == "existing-profile"


def test_storage_environment_takes_precedence_over_dotenv(storage_environment, monkeypatch, tmp_path):
    dotenv = tmp_path / ".env.local"
    dotenv.write_text("STORAGE_BACKEND=s3\nSTORAGE_PROFILE_NAME=file-profile\nS3_BUCKET=file-bucket\n")
    storage_environment.side_effect = lambda ignored, override: load_dotenv(dotenv, override=override)
    monkeypatch.setenv("STORAGE_PROFILE_NAME", "process-profile")
    config = load_storage_config()
    assert config.storage_profile_name == "process-profile"
    assert config.s3_bucket == "file-bucket"


def test_invalid_backend(storage_environment, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "unknown")
    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        load_storage_config()


def test_initialization_creates_reuses_and_rejects_conflicts(profile_sessions, tmp_path):
    config = StorageConfig(local_storage_root=str(tmp_path / "original"))
    resolver = StorageResolver(config)
    first = initialize_profile(profile_sessions, config, resolver)
    second = initialize_profile(profile_sessions, config, resolver)
    assert first == second
    assert first.name == "local-default" and first.backend == "local"
    with profile_sessions() as session:
        row = session.get(StorageProfile, first.id)
        assert row.local_root == str(tmp_path / "original")
        assert row.bucket is None and row.endpoint_url is None and row.credential_ref is None
        assert session.scalar(sa.select(sa.func.count()).select_from(StorageProfile)) == 1
    with pytest.raises(ResourceError) as error:
        initialize_profile(profile_sessions, replace(config, local_storage_root=str(tmp_path / "different")), resolver)
    assert error.value.code == "profile_conflict"
    with profile_sessions() as session:
        assert session.get(StorageProfile, first.id).local_root == str(tmp_path / "original")
    assert list((tmp_path / "original").iterdir()) == []
    resolver.close()


def test_relative_root_is_based_on_project_not_working_directory(profile_sessions, tmp_path, monkeypatch):
    from mmkb import storage as module
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(module, "PROJECT_ROOT", project)
    monkeypatch.chdir(tmp_path)
    config = StorageConfig(local_storage_root="./assets")
    resolver = StorageResolver(config)
    info = initialize_profile(profile_sessions, config, resolver)
    with profile_sessions() as session:
        assert session.get(StorageProfile, info.id).local_root == str(project / "assets")
    resolver.close()


def test_local_probe_failure_does_not_open_db_transaction(tmp_path, monkeypatch):
    from mmkb import storage as module
    config = StorageConfig(local_storage_root=str(tmp_path))
    sessions = Mock()
    resolver = StorageResolver(config)
    monkeypatch.setattr(module.tempfile, "TemporaryFile", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(StorageError):
        initialize_profile(sessions, config, resolver)
    sessions.begin.assert_not_called()
    resolver.close()


def test_s3_probe_runs_before_database_write_and_does_not_create_bucket(profile_sessions):
    config = StorageConfig(storage_backend="s3", storage_profile_name="s3-default", s3_bucket="bucket")
    client = Mock()

    def check(**kwargs):
        with profile_sessions() as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(StorageProfile)) == 0
    client.head_bucket.side_effect = check
    resolver = Mock(return_value=S3Storage("bucket", client))
    info = initialize_profile(profile_sessions, config, resolver)
    assert info.backend == "s3"
    client.head_bucket.assert_called_once_with(Bucket="bucket")
    client.create_bucket.assert_not_called()


def test_profile_selection_keeps_old_local_files_readable(profile_sessions, tmp_path):
    first = StorageConfig(local_storage_root=str(tmp_path / "old"))
    second = replace(first, storage_profile_name="new-default", local_storage_root=str(tmp_path / "new"))
    resolver = StorageResolver(first)
    old_info = initialize_profile(profile_sessions, first, resolver)
    initialize_profile(profile_sessions, second, resolver)
    with profile_sessions() as session:
        profile = session.get(StorageProfile, old_info.id)
    resolver(profile).put("same-key", b"old", "image/png")
    new_resolver = StorageResolver(second)
    with new_resolver(profile).open("same-key") as stream:
        assert stream.read() == b"old"
    assert not (tmp_path / "new/same-key").exists()
    resolver.close()
    new_resolver.close()


def test_factory_does_not_touch_database_or_create_directories(tmp_path):
    resolver = StorageResolver(StorageConfig())
    storage = resolver(StorageProfile(id=uuid4(), backend="local", local_root=str(tmp_path / "absent")))
    assert isinstance(storage, LocalStorage)
    assert not (tmp_path / "absent").exists()
    resolver.close()


@pytest.mark.parametrize("config", [StorageConfig(storage_backend="unknown"),
                                    StorageConfig(storage_profile_name=" "),
                                    StorageConfig(local_storage_root=""),
                                    StorageConfig(storage_backend="s3")])
def test_initialization_rejects_invalid_configuration(config):
    sessions = Mock()
    resolver = StorageResolver(config)
    with pytest.raises(StorageConfigError):
        initialize_profile(sessions, config, resolver)
    sessions.begin.assert_not_called()
    resolver.close()


def test_cli_needs_no_model_settings_and_prints_profile_identity(storage_environment, monkeypatch, profile_sessions, tmp_path, capsys):
    from mmkb import storage as module
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(module, "create_session_factory", lambda url: profile_sessions)
    monkeypatch.setattr("sys.argv", ["mmkb.storage", "init"])
    main()
    output = capsys.readouterr().out
    assert "id=" in output and "name=local-default" in output and "backend=local" in output


def test_cli_missing_database_exits_cleanly(storage_environment, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mmkb.storage", "init"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_cli_database_failure_hides_connection_details(storage_environment, monkeypatch, capsys):
    from mmkb import storage as module
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:private-password@invalid/database")
    monkeypatch.setattr("sys.argv", ["mmkb.storage", "init"])
    monkeypatch.setattr(module, "create_session_factory", Mock(side_effect=sa.exc.SQLAlchemyError("private-password")))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert "database_unavailable" in output and "private-password" not in output
