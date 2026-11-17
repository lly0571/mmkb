"""Connection status contract, aggregation and caching for ``GET /api/health``.

This module owns the public vocabulary (check keys, states, codes, scopes) and turns per-component
probe results into one report. Blocking work lives in :mod:`mmkb.health_probes`, which runs in a
child process started by :mod:`mmkb.health_runner`; nothing here writes, migrates or probes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
import inspect
import logging
import math
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from mmkb.config import HealthConfig, health_config_payload
from mmkb.health_contract import (  # noqa: F401  (re-exported for probe and API callers)
    CHECK_API, CHECK_DATABASE, CHECK_DECISION, CHECK_EMBEDDING, CHECK_KEYS, CHECK_STORAGE, CHECK_VISION,
    CHECK_WORKER, DEPENDENCY_CHECKS, OPTIONAL_MODEL_CHECKS, REQUIRED_CHECKS, SCOPES, STATES, STATUS_VALUES,
    Check, check_payload, check_result, default_check, error_code_catalog, summarize_status,
)

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1

__all__ = [
    "Check", "HealthBudgets", "HealthReport", "HealthService", "ProbeRound", "SCHEMA_VERSION",
    "parse_check_payload", "summarize_status",
]


@dataclass(frozen=True)
class HealthBudgets:
    """One injected budget set; tests shorten these values, deployments do not set new variables.

    HTTPX's connect/read/write/pool and Botocore's connect/read values bound individual phases, so
    ``total_seconds`` is enforced separately by the parent that owns the probe process.
    """

    total_seconds: float = 5.0
    model_seconds: float = 2.0
    model_connect_seconds: float = 0.5
    model_read_seconds: float = 1.0
    model_pool_seconds: float = 0.2
    database_seconds: float = 2.0
    database_connect_seconds: float = 1.0
    database_statement_ms: int = 500
    database_lock_ms: int = 200
    database_idle_ms: int = 2_000
    storage_seconds: float = 2.0
    storage_connect_seconds: float = 0.5
    storage_read_seconds: float = 1.0
    cleanup_seconds: float = 0.5
    ttl_seconds: float = 10.0
    model_response_limit_bytes: int = 256 * 1024
    ipc_frame_limit_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        seconds = (self.total_seconds, self.model_seconds, self.model_connect_seconds, self.model_read_seconds,
                   self.model_pool_seconds, self.database_seconds, self.database_connect_seconds,
                   self.storage_seconds, self.storage_connect_seconds, self.storage_read_seconds,
                   self.cleanup_seconds, self.ttl_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in seconds):
            raise ValueError("Health budgets must be finite and positive")
        if any(not isinstance(value, int) or value <= 0 for value in (
            self.database_statement_ms, self.database_lock_ms, self.database_idle_ms,
            self.model_response_limit_bytes, self.ipc_frame_limit_bytes,
        )):
            raise ValueError("Health budget sizes and millisecond limits must be positive integers")
        if self.total_seconds <= self.cleanup_seconds:
            raise ValueError("The probe deadline must leave room for child cleanup")

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "HealthBudgets":
        """Rebuild the probe-side budgets; parent-only fields keep their defaults."""
        owned = {item.name for item in fields(cls)} - {"cleanup_seconds", "ttl_seconds", "ipc_frame_limit_bytes"}
        return cls(**{name: value for name, value in dict(data).items() if name in owned})

    @property
    def payload(self) -> dict[str, float | int]:
        return {
            "total_seconds": self.total_seconds,
            "model_seconds": self.model_seconds,
            "model_connect_seconds": self.model_connect_seconds,
            "model_read_seconds": self.model_read_seconds,
            "model_pool_seconds": self.model_pool_seconds,
            "database_seconds": self.database_seconds,
            "database_connect_seconds": self.database_connect_seconds,
            "database_statement_ms": self.database_statement_ms,
            "database_lock_ms": self.database_lock_ms,
            "database_idle_ms": self.database_idle_ms,
            "storage_seconds": self.storage_seconds,
            "storage_connect_seconds": self.storage_connect_seconds,
            "storage_read_seconds": self.storage_read_seconds,
            "model_response_limit_bytes": self.model_response_limit_bytes,
        }


@dataclass(frozen=True)
class ProbeRound:
    """What one probe attempt observed; unfinished components are reported as unknown."""

    checks: Mapping[str, Check] = field(default_factory=dict)
    outcome: str = "complete"  # complete, deadline, protocol_error, start_failed, occupied, cancelled
    unreaped_pid: int | None = None


class HealthCheckModel(BaseModel):
    state: Literal["connected", "down", "not_configured", "disabled", "unknown"]
    required: bool
    code: str
    message: str
    latency_ms: int | None
    scope: str
    backend: Literal["local", "s3"] | None = None


class HealthReport(BaseModel):
    """Every completed report answers HTTP 200; dependency failures are structured, not 5xx."""

    schema_version: Literal[1]
    status: Literal["healthy", "degraded", "unavailable", "unknown"]
    checked_at: str
    duration_ms: int = Field(ge=0)
    cached: bool
    age_ms: int = Field(ge=0)
    ttl_ms: int = Field(ge=0)
    checks: dict[str, HealthCheckModel]


@dataclass
class _Snapshot:
    checks: dict[str, Check]
    checked_at: datetime
    duration_ms: int
    moment: float


class HealthService:
    """Serve one aged dependency snapshot to concurrent requests with a bounded probe process."""

    def __init__(self, config: HealthConfig, *, budgets: HealthBudgets | None = None, runner=None,
                 clock: Callable[[], float] = time.monotonic):
        if runner is None:
            raise ValueError("A probe runner is required so health checks stay injectable")
        self.config = config
        self.budgets = budgets or HealthBudgets()
        self.runner = runner
        # Injected so tests can move the cache age without sleeping.
        self._clock = clock
        self._snapshot: _Snapshot | None = None
        self._task: asyncio.Task | None = None
        self._unreaped_pid: int | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def report(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("The health service is closed")
        if self._unreaped_pid is not None:
            # One lingering probe is enough to refuse new rounds; never let children accumulate.
            recovered = await self._reap()
            logger.error("health probe_unavailable pid=%s recovered=%s", self._unreaped_pid, recovered)
            if recovered:
                self._unreaped_pid = None
            # Faults are reported immediately and never cached, so recovery is visible at once.
            return self._payload(self._fault_snapshot("probe_start_failed"), cached=False)
        cached = self._valid_cache()
        if cached is not None:
            return self._payload(cached, cached=True)
        return self._payload(await self._join_round(), cached=False)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
                pass
        await self._call_runner("aclose")

    async def _join_round(self) -> _Snapshot:
        task = self._task
        if task is None or task.done():
            task = asyncio.ensure_future(self._execute())
            self._task = task
        # shield(): one browser cancelling its GET must not cancel the round other waiters share.
        return await asyncio.shield(task)

    async def _execute(self) -> _Snapshot:
        started = self._clock()
        round_result = await self.runner.run(
            {"v": SCHEMA_VERSION, "budgets": self.budgets.payload,
             "health_config": health_config_payload(self.config)},
            timeout=self.budgets.total_seconds,
        )
        duration = max(0, int((self._clock() - started) * 1000))
        checks = {key: self._resolve(key, round_result) for key in CHECK_KEYS}
        if round_result.unreaped_pid is not None:
            self._unreaped_pid = round_result.unreaped_pid
        snapshot = _Snapshot(checks=checks, checked_at=datetime.now(timezone.utc),
                             duration_ms=duration, moment=self._clock())
        # Stale rounds from a superseded close must not overwrite a newer snapshot.
        if not self._closed:
            self._snapshot = snapshot
        self._log_report(checks, duration, round_result.outcome)
        return snapshot

    def _resolve(self, key: str, round_result: ProbeRound) -> Check:
        if key == CHECK_API:
            # Answering this request already proves the HTTP entrypoint; nothing self-requested.
            return default_check(key, "api_reachable")
        if key == CHECK_WORKER:
            return default_check(key, "heartbeat_not_implemented")
        observed = round_result.checks.get(key)
        if observed is not None:
            return observed
        if round_result.outcome in {"deadline", "cancelled"}:
            return default_check(key, "check_deadline_exceeded")
        if round_result.outcome in {"start_failed", "occupied"}:
            return default_check(key, "probe_start_failed")
        return default_check(key, "probe_protocol_error")

    def _fault_snapshot(self, code: str) -> _Snapshot:
        checks = {key: self._resolve(key, ProbeRound(checks={}, outcome="occupied")) for key in CHECK_KEYS}
        if code != "probe_start_failed":  # defensive: the only parent-side fault today
            for key in DEPENDENCY_CHECKS:
                checks[key] = default_check(key, code)
        return _Snapshot(checks=checks, checked_at=datetime.now(timezone.utc), duration_ms=0,
                         moment=self._clock())

    def _valid_cache(self) -> _Snapshot | None:
        snapshot = self._snapshot
        task = self._task
        # A finished task object is not an in-flight round; a running one is.
        if snapshot is None or (task is not None and not task.done()):
            return None
        if self._clock() - snapshot.moment >= self.budgets.ttl_seconds:
            return None
        return snapshot

    async def _reap(self) -> bool:
        result = await self._call_runner("reap", default=False)
        return bool(result)

    async def _call_runner(self, name: str, default=None):
        method = getattr(self.runner, name, None)
        if method is None:
            return default
        result = method()
        if inspect.isawaitable(result):
            return await result
        return default if result is None else result

    def _payload(self, snapshot: _Snapshot, *, cached: bool) -> dict[str, Any]:
        age_ms = max(0, int((self._clock() - snapshot.moment) * 1000))
        return {
            "schema_version": SCHEMA_VERSION,
            "status": summarize_status(snapshot.checks),
            "checked_at": snapshot.checked_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "duration_ms": snapshot.duration_ms,
            "cached": cached,
            "age_ms": age_ms,
            "ttl_ms": int(self.budgets.ttl_seconds * 1000),
            "checks": {key: check_payload(key, snapshot.checks[key]) for key in CHECK_KEYS},
        }

    def _log_report(self, checks: Mapping[str, Check], duration_ms: int, outcome: str) -> None:
        # Codes and states only: no addresses, identifiers, bucket names or remote payloads.
        summary = " ".join(f"{key}={checks[key].state}/{checks[key].code}" for key in CHECK_KEYS)
        logger.info("health outcome=%s duration_ms=%s %s", outcome, duration_ms, summary)


def parse_check_payload(data: Any) -> Check | None:
    """Validate one child frame against the contract; anything unexpected is rejected."""
    if not isinstance(data, dict) or set(data) - {"v", "check", "code", "latency_ms", "backend"}:
        return None
    if data.get("v") != SCHEMA_VERSION or data.get("check") not in DEPENDENCY_CHECKS:
        return None
    key = data["check"]
    code = data.get("code")
    if code not in error_code_catalog()[key]:
        return None
    latency = data.get("latency_ms")
    if latency is not None and (isinstance(latency, bool) or not isinstance(latency, int) or latency < 0):
        return None
    backend = data.get("backend")
    if key != CHECK_STORAGE and backend is not None:
        return None
    if backend is not None and backend not in {"local", "s3"}:
        return None
    return check_result(key, code, latency_ms=latency, backend=backend)
