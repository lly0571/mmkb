"""Fixed vocabulary for the connection status report: keys, states, codes, scopes and summary.

Everything a probe may say is whitelisted here. Messages are constant and free of addresses,
identifiers, bucket names and remote text, so a probe child cannot leak deployment details and the
parent never has to sanitise free-form strings.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CHECK_API = "api"
CHECK_DATABASE = "database"
CHECK_EMBEDDING = "embedding"
CHECK_STORAGE = "storage"
CHECK_VISION = "vision"
CHECK_DECISION = "decision"
CHECK_WORKER = "worker"

# The probe child reports these five; ``api`` and ``worker`` belong to the parent process.
DEPENDENCY_CHECKS = (CHECK_DATABASE, CHECK_EMBEDDING, CHECK_STORAGE, CHECK_VISION, CHECK_DECISION)
CHECK_KEYS = (CHECK_API, CHECK_DATABASE, CHECK_EMBEDDING, CHECK_STORAGE, CHECK_VISION, CHECK_DECISION,
              CHECK_WORKER)
# Required means the full import and retrieval loop needs it, not that the resource API may start.
REQUIRED_CHECKS = frozenset({CHECK_API, CHECK_DATABASE, CHECK_EMBEDDING, CHECK_STORAGE})
OPTIONAL_MODEL_CHECKS = (CHECK_VISION, CHECK_DECISION)

STATES = frozenset({"connected", "down", "not_configured", "disabled", "unknown"})
STATUS_VALUES = frozenset({"healthy", "degraded", "unavailable", "unknown"})
BACKENDS = frozenset({"local", "s3"})

# code -> (default scope, operator-facing message, state the code may carry)
_CODE_CONTRACT: dict[str, tuple[str, str, str]] = {
    # HTTP entrypoint and configuration.
    "api_reachable": ("http_response", "API 已响应", "connected"),
    "required_not_configured": ("configuration", "服务端缺少该项的必需配置", "not_configured"),
    "optional_not_configured": ("configuration", "未配置该可选服务", "not_configured"),
    "invalid_config": ("configuration", "服务端配置不合法，请检查对应环境变量", "down"),
    "service_disabled": ("configuration", "该项已显式禁用", "disabled"),
    # Database: one short-lived read-only connection plus version and structure checks.
    "database_ready": ("connection_and_schema", "连接与预期结构检查通过", "connected"),
    "database_unreachable": ("connection", "无法在预算内连接或查询数据库，请检查连接与服务负载", "down"),
    "database_auth_failed": ("connection", "数据库认证失败，请检查服务端账号配置", "down"),
    "database_permission_denied": ("connection", "数据库账号缺少必要的结构或表读取权限", "down"),
    "schema_missing": ("connection_and_schema", "缺少 MMKB 表结构或迁移版本记录，请执行 alembic upgrade head",
                       "down"),
    "schema_outdated": ("connection_and_schema", "数据库迁移版本落后于当前应用，请执行 alembic upgrade head",
                        "down"),
    "schema_incompatible": ("connection_and_schema", "数据库迁移版本或关键结构与当前应用不兼容", "down"),
    # Model services: the model list only; no inference request is ever sent.
    "model_list_verified": ("model_list", "配置模型已在服务列表中发现，未执行推理", "connected"),
    "model_unreachable": ("model_list", "无法连接模型服务，请检查服务端地址与网络", "down"),
    "model_timeout": ("model_list", "模型服务列表检查超时", "down"),
    "model_auth_failed": ("model_list", "模型服务认证失败，请检查服务端 API Key 配置", "down"),
    "model_http_error": ("model_list", "模型服务未接受列表检查，或不支持模型列表接口", "down"),
    "model_not_found": ("model_list", "配置的模型未出现在服务列表中，请核对模型 ID", "down"),
    "model_invalid_response": ("model_list", "模型服务列表响应格式不正确", "down"),
    # Current storage profile only; historical profiles are out of scope.
    "dependency_unavailable": ("dependency", "数据库未就绪，无法确定当前存储 profile", "unknown"),
    "profile_not_initialized": ("profile", "当前存储 profile 尚未在服务端初始化，请显式执行 storage init",
                                "down"),
    "profile_conflict": ("profile", "已登记的存储 profile 与当前配置不一致，请改用新的 STORAGE_PROFILE_NAME",
                         "down"),
    "local_root_unavailable": ("local_directory", "本地存储根目录不存在或不可用", "down"),
    "local_directory_accessible": ("local_directory", "本地存储目录可访问，未验证可写、磁盘余量与原件完整性",
                                    "connected"),
    "bucket_accessible": ("bucket_head", "当前 S3 Bucket 检查通过，未验证对象读写", "connected"),
    "storage_access_denied": ("bucket_head", "存储访问被拒绝，请检查服务端凭证与权限", "down"),
    "bucket_unavailable": ("bucket_head", "无法访问 Bucket，请确认 Bucket 存在且服务端凭证可用", "down"),
    "storage_timeout": ("bucket_head", "存储检查超时，请稍后重新检查", "down"),
    # Scheduling faults: the observation failed, which does not prove the dependency is offline.
    "check_deadline_exceeded": ("probe", "检查未在预算内完成，无法判断该项", "unknown"),
    "probe_start_failed": ("probe", "探测进程无法启动或上一轮探测未回收", "unknown"),
    "probe_protocol_error": ("probe", "探测结果不完整或不符合契约", "unknown"),
    "heartbeat_not_implemented": ("not_monitored", "尚无 Worker 心跳，无法判断是否运行", "unknown"),
}

# Codes allowed per check: storage cannot report model failures and vice versa.
_CHECK_CODES: dict[str, frozenset[str]] = {
    CHECK_API: frozenset({"api_reachable"}),
    CHECK_WORKER: frozenset({"heartbeat_not_implemented"}),
    CHECK_DATABASE: frozenset({
        "invalid_config", "database_ready", "database_unreachable", "database_auth_failed",
        "database_permission_denied", "schema_missing", "schema_outdated", "schema_incompatible",
        "check_deadline_exceeded",
    }),
    CHECK_STORAGE: frozenset({
        "invalid_config", "dependency_unavailable", "profile_not_initialized", "profile_conflict",
        "local_root_unavailable", "local_directory_accessible", "bucket_accessible", "storage_access_denied",
        "bucket_unavailable", "storage_timeout", "check_deadline_exceeded",
    }),
    CHECK_EMBEDDING: frozenset({
        "required_not_configured", "invalid_config", "model_list_verified", "model_unreachable",
        "model_timeout", "model_auth_failed", "model_http_error", "model_not_found",
        "model_invalid_response", "check_deadline_exceeded",
    }),
    CHECK_VISION: frozenset({
        "optional_not_configured", "required_not_configured", "service_disabled", "invalid_config",
        "model_list_verified", "model_unreachable", "model_timeout", "model_auth_failed",
        "model_http_error", "model_not_found", "model_invalid_response", "check_deadline_exceeded",
    }),
    CHECK_DECISION: frozenset({
        "optional_not_configured", "required_not_configured", "service_disabled", "invalid_config",
        "model_list_verified", "model_unreachable", "model_timeout", "model_auth_failed",
        "model_http_error", "model_not_found", "model_invalid_response", "check_deadline_exceeded",
    }),
}

# A round that never reported proves nothing about the dependency, so every fault stays unknown.
for _key in DEPENDENCY_CHECKS:
    _CHECK_CODES[_key] |= {"check_deadline_exceeded", "probe_start_failed", "probe_protocol_error"}

# Scope of the storage check follows the backend that was actually probed.
_BACKEND_SCOPES = {"storage_access_denied", "bucket_unavailable", "storage_timeout"}

SCOPES = frozenset(scope for scope, _, _ in _CODE_CONTRACT.values()) | _BACKEND_SCOPES


def error_code_catalog() -> dict[str, frozenset[str]]:
    """Codes accepted per check, for probe implementations and contract tests."""
    return dict(_CHECK_CODES)


def message_for(code: str) -> str:
    return _CODE_CONTRACT[code][1]


def state_for(code: str) -> str:
    return _CODE_CONTRACT[code][2]


def scope_for(code: str, backend: str | None = None) -> str:
    if code in _BACKEND_SCOPES:
        return "local_directory" if backend == "local" else "bucket_head"
    return _CODE_CONTRACT[code][0]


def is_allowed(key: str, code: str, state: str) -> bool:
    return code in _CHECK_CODES.get(key, frozenset()) and state == state_for(code)


@dataclass(frozen=True)
class Check:
    """One component result; scope and message come from the code, never from the probe."""

    state: str
    code: str
    latency_ms: int | None = None
    backend: str | None = None

    def __post_init__(self) -> None:
        if self.code not in _CODE_CONTRACT:
            raise ValueError(f"Unexpected health code: {self.code}")
        # A code carries exactly one state, so a probe cannot label a failure as connected.
        if self.state != state_for(self.code):
            raise ValueError(f"Health code {self.code} cannot carry state {self.state}")
        if self.backend is not None and self.backend not in BACKENDS:
            raise ValueError("Health backend must be local or s3")
        if self.latency_ms is not None and (isinstance(self.latency_ms, bool)
                                            or not isinstance(self.latency_ms, int) or self.latency_ms < 0):
            raise ValueError("Health latency must be a non-negative integer or None")

    @property
    def message(self) -> str:
        return message_for(self.code)

    @property
    def scope(self) -> str:
        return scope_for(self.code, self.backend)


def default_check(key: str, code: str) -> Check:
    """Build a check from its code, so state and code can never disagree."""
    return check_result(key, code)


def check_result(key: str, code: str, *, latency_ms: int | None = None,
                 backend: str | None = None) -> Check:
    """Probe-side constructor: the code fixes the state, scope and message."""
    if code not in _CHECK_CODES.get(key, frozenset()):
        raise ValueError(f"Code {code} is not allowed for {key}")
    return Check(state_for(code), code, latency_ms, backend)


def check_payload(key: str, check: Check) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": check.state,
        "required": key in REQUIRED_CHECKS,
        "code": check.code,
        "message": check.message,
        "latency_ms": check.latency_ms,
        "scope": check.scope,
    }
    # backend is a storage-only field: other components must not invent one.
    if key == CHECK_STORAGE and check.backend is not None:
        payload["backend"] = check.backend
    return payload


def summarize_status(checks: Mapping[str, Check]) -> str:
    """Aggregate with the documented priority; optional services only ever degrade."""
    for key in (CHECK_API, CHECK_DATABASE, CHECK_EMBEDDING, CHECK_STORAGE):
        state = checks[key].state
        # A required component that is switched off is treated as unavailable, never as healthy.
        if state in {"down", "not_configured", "disabled"}:
            return "unavailable"
    if any(checks[key].state == "unknown"
           for key in (CHECK_API, CHECK_DATABASE, CHECK_EMBEDDING, CHECK_STORAGE)):
        return "unknown"
    for key in OPTIONAL_MODEL_CHECKS:
        check = checks[key]
        if check.state in {"down", "unknown"}:
            return "degraded"
        # An absent optional service is expected; one the operator asked for explicitly is not.
        if check.state == "not_configured" and check.code == "required_not_configured":
            return "degraded"
    return "healthy"
