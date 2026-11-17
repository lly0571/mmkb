"""Read-only dependency probes; started as a child process by ``mmkb.health_runner``.

The child sees one configuration snapshot on stdin and writes one whitelisted result per line, so a
slow or wedged dependency can be killed without touching API or Worker connections. These functions
are also callable in-process for tests. Probing is strictly a read: never migrate, create, mkdir,
put, delete, list objects, claim work, run inference or take advisory locks here.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, TypeVar

import httpx
import sqlalchemy as sa
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from mmkb.config import HealthConfig, ServiceProbe, StorageConfig, health_config_from_payload
from mmkb.errors import StorageConfigError, StorageCredentialError
from mmkb.health import HealthBudgets
from mmkb.health_contract import CHECK_DATABASE, CHECK_STORAGE, Check, check_result
from mmkb.providers.common import ProviderError, model_list_url, validate_model_list
from mmkb.schema_contract import (
    EMBEDDING_COLUMN, EMBEDDING_TABLE, EXPECTED_EMBEDDING_TYPE, EXPECTED_HEAD, EXPECTED_REVISIONS,
    REQUIRED_TABLES, SCHEMA_NAME, VECTOR_EXTENSION, VERSION_COLUMN, VERSION_TABLE,
)
from mmkb.storage import build_s3_client, candidate_profile_fields

logger = logging.getLogger(__name__)
PROFILE_FIELDS = ("backend", "local_root", "endpoint_url", "bucket", "credential_ref")
# Provider error codes mapped onto the health vocabulary; unknown ones become generic.
_PROVIDER_CODES = {
    "timeout": "model_timeout", "connection_error": "model_unreachable", "http_error": "model_http_error",
    "unsupported_model": "model_http_error", "invalid_response": "model_invalid_response",
    "model_not_found": "model_not_found",
}
_AUTH_STATUSES = frozenset({401, 403})
# SQLSTATE prefixes worth naming for the operator; anything else is an unreachable database.
_SQLSTATE_CODES = {
    "28000": "database_auth_failed", "28P01": "database_auth_failed", "28000 ": "database_auth_failed",
    "42501": "database_permission_denied", "42P01": "schema_missing", "3F000": "schema_missing",
    "3D000": "database_unreachable",
}
# psycopg2 raises connect-phase failures with no pgcode at all, so libpq's stable wording is the only
# signal available there. These phrases are matched to pick a code; the driver text itself is never
# logged or returned, because it carries user names, hosts and DSN fragments.
_AUTH_PHRASES = (
    "password authentication failed", "no password supplied", "peer authentication failed",
    "ident authentication failed", "pam authentication failed", "invalid_password",
    "rejecting connections: authentication failed", "expected authentication request from server",
)
_PERMISSION_PHRASES = ("permission denied for", "permission denied to", "must be owner of")

_Result = TypeVar("_Result")


class _ProbeBudgetExceeded(TimeoutError):
    """The observation did not complete within its component budget."""


def _within_budget(operation: Callable[[], _Result], seconds: float) -> _Result:
    """Bound synchronous observations inside the disposable probe process.

    A daemon thread cannot interrupt a driver, DNS lookup or filesystem call. It lets the child
    report an unknown observation on time, discard any late result and exit without waiting for
    that call. All clients belong to the operation's thread; process exit closes remaining OS
    handles. These probes must never run inside the long-lived API or Worker process.
    """
    deadline = time.monotonic() + seconds
    future: Future[_Result] = Future()

    def run() -> None:
        try:
            future.set_result(operation())
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=run, name="mmkb-probe-operation", daemon=True).start()
    done, _ = wait((future,), timeout=max(0.0, deadline - time.monotonic()))
    if not done or time.monotonic() >= deadline:
        raise _ProbeBudgetExceeded from None
    return future.result()


@dataclass(frozen=True)
class ProfileRow:
    """The registered profile row, kept inside the probe child: it never reaches the browser."""

    backend: str
    local_root: str | None
    endpoint_url: str | None
    bucket: str | None
    credential_ref: str | None


def sqlstate_of(error: BaseException) -> str:
    for candidate in (getattr(error, "orig", None), error):
        for attribute in ("pgcode", "sqlstate", "code"):
            value = getattr(candidate, attribute, None)
            if isinstance(value, str) and value:
                return value
    return ""


def driver_text_of(error: BaseException) -> str:
    """Lower-cased driver wording, used only to choose a code; never shown or logged as-is."""
    parts: list[str] = []
    for item in (error, getattr(error, "orig", None)):
        if item is None:
            continue
        for argument in getattr(item, "args", ()) or ():
            if isinstance(argument, (bytes, bytearray)):
                parts.append(argument.decode("utf-8", "ignore"))
            elif isinstance(argument, str):
                parts.append(argument)
        message = getattr(item, "message", None)
        if isinstance(message, str):
            parts.append(message)
    return " ".join(parts).lower()


def database_code_for(error: BaseException) -> str:
    """Map a driver failure onto the fixed vocabulary, SQLSTATE first, wording only as a fallback."""
    code = _SQLSTATE_CODES.get(sqlstate_of(error))
    if code:
        return code
    text = driver_text_of(error)
    if any(phrase in text for phrase in _PERMISSION_PHRASES):
        return "database_permission_denied"
    if any(phrase in text for phrase in _AUTH_PHRASES):
        return "database_auth_failed"
    return "database_unreachable"


def is_timeout(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    return any("Timeout" in item.__name__ or "timeout" in item.__name__ for item in type(error).__mro__)


def database_connect_arguments(budgets: HealthBudgets) -> dict[str, Any]:
    """Libpq and server-side limits for one short read-only probe connection."""
    options = (
        f"-c statement_timeout={budgets.database_statement_ms} "
        f"-c lock_timeout={budgets.database_lock_ms} "
        f"-c idle_in_transaction_session_timeout={budgets.database_idle_ms} "
        f"-c default_transaction_read_only=on"
    )
    return {
        "connect_timeout": max(1, math.ceil(budgets.database_connect_seconds)),
        "options": options,
        "application_name": "mmkb-health",
    }


def classify_revisions(rows: tuple[str, ...]) -> str | None:
    """Compare the applied revisions with the packaged contract; order comes from the contract only."""
    if not rows:
        return "schema_missing"
    if len(set(rows)) > 1:
        # Several rows mean several heads or a half-applied history: the order is not comparable.
        return "schema_incompatible"
    revision = rows[0]
    if revision == EXPECTED_HEAD:
        return None
    if revision in EXPECTED_REVISIONS:
        # A member of the known history that is not the head is behind; order comes from the contract.
        return "schema_outdated"
    return "schema_incompatible"


def _read_revisions(connection) -> tuple[str, ...]:
    present = connection.scalar(sa.text(f"SELECT to_regclass('{VERSION_TABLE}') IS NOT NULL"))
    if not present:
        return ()
    return tuple(row[0] for row in connection.execute(sa.text(
        f"SELECT {VERSION_COLUMN} FROM {VERSION_TABLE} ORDER BY {VERSION_COLUMN}"
    )))


def _read_embedding_type(connection) -> str | None:
    row = connection.execute(sa.text(
        "SELECT format_type(a.atttypid, a.atttypmod) AS type FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :schema AND c.relname = :table AND a.attname = :column "
        "AND a.attnum > 0 AND NOT a.attisdropped"
    ), {"schema": SCHEMA_NAME, "table": EMBEDDING_TABLE, "column": EMBEDDING_COLUMN}).first()
    return None if row is None else row[0]


def _read_profile(connection, name: str) -> ProfileRow | None:
    row = connection.execute(sa.text(
        f"SELECT {', '.join(PROFILE_FIELDS)} FROM {SCHEMA_NAME}.storage_profiles "
        "WHERE name = :name LIMIT 1"
    ), {"name": name}).first()
    if row is None:
        return None
    return ProfileRow(*(row[index] for index in range(len(PROFILE_FIELDS))))


def inspect_database(connection, config: HealthConfig) -> tuple[str, ProfileRow | None]:
    """Run every bounded read-only statement; returns a code and the internal profile snapshot."""
    connection.execute(sa.text("SELECT 1"))
    broken = classify_revisions(_read_revisions(connection))
    if broken:
        return broken, None
    # Zero-row reads prove the account can see the tables without scanning resources or vectors.
    for table in REQUIRED_TABLES:
        connection.execute(sa.text(f"SELECT 1 FROM {SCHEMA_NAME}.{table} WHERE false"))
    extension = connection.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = :name"),
                                   {"name": VECTOR_EXTENSION}).first()
    if extension is None:
        return "schema_incompatible", None
    if _read_embedding_type(connection) != EXPECTED_EMBEDDING_TYPE:
        return "schema_incompatible", None
    if config.storage is None:
        # Storage settings are unusable, but the database itself answered and is healthy.
        return "database_ready", None
    return "database_ready", _read_profile(connection, config.storage.storage_profile_name)


def probe_database(config: HealthConfig, budgets: HealthBudgets) -> tuple[Check, ProfileRow | None]:
    started = time.monotonic()
    try:
        return _within_budget(lambda: _probe_database(config, budgets), budgets.database_seconds)
    except _ProbeBudgetExceeded:
        return check_result(CHECK_DATABASE, "check_deadline_exceeded",
                            latency_ms=max(0, int((time.monotonic() - started) * 1000))), None


def _probe_database(config: HealthConfig, budgets: HealthBudgets) -> tuple[Check, ProfileRow | None]:
    if not config.database_url:
        return check_result(CHECK_DATABASE, "invalid_config"), None
    try:
        engine = sa.create_engine(config.database_url, poolclass=sa.pool.NullPool, hide_parameters=True,
                                 connect_args=database_connect_arguments(budgets))
    except (sa.exc.SQLAlchemyError, ValueError, TypeError):
        # A malformed DSN must not stop model or storage checks, and the DSN is never echoed.
        return check_result(CHECK_DATABASE, "invalid_config"), None
    started = time.monotonic()
    code, row = "database_unreachable", None
    try:
        with engine.connect() as connection:
            code, row = inspect_database(connection, config)
    except sa.exc.SQLAlchemyError as error:
        code = database_code_for(error)
        logger.warning("health_probe component=database error_type=%s sqlstate=%s",
                       type(error).__name__, sqlstate_of(error) or "-")
    except (OSError, TimeoutError):
        code = "database_unreachable"
    finally:
        engine.dispose()
    latency = max(0, int((time.monotonic() - started) * 1000))
    return check_result(CHECK_DATABASE, code, latency_ms=latency), row


def classify_model_status(status_code: int) -> str:
    if status_code in _AUTH_STATUSES:
        return "model_auth_failed"
    return "model_http_error"


def list_models(config, budgets: HealthBudgets, *, transport=None) -> str:
    """GET the model list with a hard total budget and return the health code for it.

    Redirects are never followed, the decoded body is capped, and no inference endpoint is called.
    """
    timeout = httpx.Timeout(budgets.model_seconds, connect=budgets.model_connect_seconds,
                            read=budgets.model_read_seconds, write=budgets.model_read_seconds,
                            pool=budgets.model_pool_seconds)
    headers = {"Accept": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    deadline = time.monotonic() + budgets.model_seconds
    body = bytearray()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", model_list_url(config.base_url), headers=headers) as response:
                if not response.is_success:
                    return classify_model_status(response.status_code)
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > budgets.model_response_limit_bytes:
                        return "model_invalid_response"
                    if time.monotonic() > deadline:
                        # A drip-fed response is a timeout, not a small successful body.
                        return "model_timeout"
    except httpx.TimeoutException:
        return "model_timeout"
    except httpx.RequestError:
        return "model_unreachable"
    try:
        payload = json.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        return "model_invalid_response"
    try:
        validate_model_list("model", payload, config.model)
    except ProviderError as error:
        return _PROVIDER_CODES.get(error.code, "model_invalid_response")
    return "model_list_verified"


def probe_model(probe: ServiceProbe, budgets: HealthBudgets, *, transport=None) -> Check:
    """Probe one configured model service; other services are checked independently."""
    if probe.action != "probe":
        code = {"not_configured": probe.code or "required_not_configured",
                "disabled": "service_disabled", "invalid": "invalid_config"}[probe.action]
        return check_result(probe.key, code)
    started = time.monotonic()
    try:
        code = _within_budget(lambda: list_models(probe.config, budgets, transport=transport),
                              budgets.model_seconds)
    except _ProbeBudgetExceeded:
        code = "check_deadline_exceeded"
    return check_result(probe.key, code, latency_ms=max(0, int((time.monotonic() - started) * 1000)))


class _Settings:
    """Adapt a storage snapshot to the Settings protocol the storage helpers accept."""

    def __init__(self, config: StorageConfig):
        self.storage_backend = config.storage_backend
        self.storage_profile_name = config.storage_profile_name
        self.local_storage_root = config.local_storage_root
        self.s3_endpoint_url = config.s3_endpoint_url
        self.s3_bucket = config.s3_bucket
        self.s3_region = config.s3_region
        self.s3_credential_profile = config.s3_credential_profile
        self.s3_addressing_style = config.s3_addressing_style
        self.s3_signature_version = config.s3_signature_version


def profile_conflict_code(row: ProfileRow, config: StorageConfig) -> bool:
    """Reuse the initialization comparison instead of inventing a second normalisation rule."""
    try:
        fields = candidate_profile_fields(_Settings(config))
    except StorageConfigError:
        return False
    return any(getattr(row, name) != value for name, value in fields.items())


def probe_local_directory(root: str | None) -> str:
    """Confirm the registered root is a directory we can open; never create or traverse anything."""
    if not root:
        return "local_root_unavailable"
    path = Path(root)
    try:
        if not path.is_dir():
            return "local_root_unavailable"
        with os.scandir(path):  # Opening the directory handle is the whole access check.
            pass
    except FileNotFoundError:
        return "local_root_unavailable"
    except NotADirectoryError:
        return "local_root_unavailable"
    except PermissionError:
        return "storage_access_denied"
    except OSError:
        return "local_root_unavailable"
    return "local_directory_accessible"


def classify_s3_error(error: BaseException) -> str:
    if isinstance(error, StorageCredentialError) or isinstance(error, NoCredentialsError):
        return "storage_access_denied"
    if isinstance(error, StorageConfigError):
        return "invalid_config"
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "Forbidden", "403"}:
            return "storage_access_denied"
        # A generic 404 cannot prove the bucket is absent rather than unreadable, so stay general.
        return "bucket_unavailable"
    if is_timeout(error):
        return "storage_timeout"
    return "bucket_unavailable"


def probe_s3_bucket(row: ProfileRow, config: StorageConfig, budgets: HealthBudgets, *, client_factory=None) -> str:
    """HeadBucket on the registered profile only: no listing, no writes, no bucket creation."""
    factory = client_factory or _health_s3_client
    try:
        client = factory(row, config, budgets)
        try:
            client.head_bucket(Bucket=row.bucket)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except (ClientError, BotoCoreError, NoCredentialsError, StorageConfigError, OSError, ValueError) as error:
        return classify_s3_error(error)
    return "bucket_accessible"


def _health_s3_client(row: ProfileRow, config: StorageConfig, budgets: HealthBudgets):
    return build_s3_client(row, config, connect_timeout=budgets.storage_connect_seconds,
                           read_timeout=budgets.storage_read_seconds, total_max_attempts=1)


def probe_storage(database: Check, row: ProfileRow | None, config: HealthConfig, budgets: HealthBudgets,
                  *, client_factory=None) -> Check:
    """Report the current profile only; the profile row itself stays inside this process."""
    started = time.monotonic()
    try:
        return _within_budget(lambda: _probe_storage(database, row, config, budgets,
                                                     client_factory=client_factory),
                              budgets.storage_seconds)
    except _ProbeBudgetExceeded:
        backend = row.backend if row is not None and row.backend in {"local", "s3"} else None
        return check_result(CHECK_STORAGE, "check_deadline_exceeded", backend=backend,
                            latency_ms=max(0, int((time.monotonic() - started) * 1000)))


def _probe_storage(database: Check, row: ProfileRow | None, config: HealthConfig, budgets: HealthBudgets,
                   *, client_factory=None) -> Check:
    if database.state != "connected":
        # Never guess the current profile from the environment when the database did not answer.
        return check_result(CHECK_STORAGE, "dependency_unavailable")
    if config.storage is None:
        return check_result(CHECK_STORAGE, "invalid_config")
    if row is None:
        return check_result(CHECK_STORAGE, "profile_not_initialized")
    if profile_conflict_code(row, config.storage):
        return check_result(CHECK_STORAGE, "profile_conflict")
    started = time.monotonic()
    if row.backend == "local":
        code = probe_local_directory(row.local_root)
    elif row.backend == "s3":
        code = probe_s3_bucket(row, config.storage, budgets, client_factory=client_factory)
    else:
        return check_result(CHECK_STORAGE, "profile_conflict")
    return check_result(CHECK_STORAGE, code, latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                        backend=row.backend)


Emit = Callable[[str, Check], None]
# The child stops a little before the parent's deadline so it can exit on its own.
_EXIT_MARGIN_SECONDS = 0.2


def probe_storage_after_database(config: HealthConfig, budgets: HealthBudgets, emit: Emit,
                                 *, client_factory=None) -> None:
    database, row = probe_database(config, budgets)
    emit(CHECK_DATABASE, database)
    emit(CHECK_STORAGE, probe_storage(database, row, config, budgets, client_factory=client_factory))


def run_round(config: HealthConfig, budgets: HealthBudgets, emit: Emit, *,
              transport=None, client_factory=None) -> bool:
    """Check the database, storage and every model service; return True when all results were sent."""
    pool = ThreadPoolExecutor(max_workers=1 + len(config.services), thread_name_prefix="mmkb-probe")
    try:
        pending = {pool.submit(probe_storage_after_database, config, budgets, emit,
                              client_factory=client_factory)}
        for probe in config.services:
            pending.add(pool.submit(_emit_model, probe, budgets, emit, transport))
        done, not_done = wait(pending, timeout=max(0.0, budgets.total_seconds - _EXIT_MARGIN_SECONDS))
        for future in done:
            # A probe that escaped its own translation is still only an unknown observation.
            error = future.exception()
            if error is not None:
                logger.warning("health_probe component=probe error_type=%s", type(error).__name__)
        return not not_done
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _emit_model(probe: ServiceProbe, budgets: HealthBudgets, emit: Emit, transport) -> None:
    emit(probe.key, probe_model(probe, budgets, transport=transport))


def _emit_factory(stream, lock: threading.Lock) -> Emit:
    def emit(key: str, check: Check) -> None:
        frame = {"v": 1, "check": key, "code": check.code, "latency_ms": check.latency_ms}
        if key == CHECK_STORAGE and check.backend is not None:
            frame["backend"] = check.backend
        with lock:
            try:
                stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
                stream.flush()
            except (OSError, ValueError):
                # The parent is gone: leave immediately instead of writing into a closed pipe.
                os._exit(0)
    return emit


def _install_stderr_policy() -> None:
    # Deliberately message-free: third-party log lines may carry endpoints or identifiers.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="health_probe level=%(levelname)s logger=%(name)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logging.Formatter("health_probe level=%(levelname)s logger=%(name)s"))


def main(argv=None) -> int:
    _install_stderr_policy()
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    try:
        request = json.loads(sys.stdin.read())
        config = health_config_from_payload(request["health_config"])
        budgets = HealthBudgets.from_payload(request.get("budgets") or {})
    except (ValueError, KeyError, TypeError, OSError):
        logger.error("health_probe component=probe error_type=request_invalid")
        return 2
    watchdog = threading.Timer(budgets.total_seconds + budgets.cleanup_seconds + 0.5,
                               lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    emit = _emit_factory(sys.stdout, threading.Lock())
    complete = run_round(config, budgets, emit)
    watchdog.cancel()
    return 0 if complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
