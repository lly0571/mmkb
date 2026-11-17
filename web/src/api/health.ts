// The only place the browser learns about dependencies. The server answers HTTP 200 for every
// completed report, so dependency failures arrive as data, not as HTTP errors.
import { API_PREFIX, ApiError, getJson, isAbortError } from './http'
import {
  HEALTH_CHECK_KEYS,
  HEALTH_SCHEMA_VERSION,
  HEALTH_STATES,
  HEALTH_STATUSES,
} from './types'
import type { HealthCheck, HealthCheckKey, HealthReport, HealthState, HealthStatus } from './types'

export const HEALTH_ENDPOINT = `${API_PREFIX}/health`
/** The browser must stop waiting on a wedged backend; the server enforces its own shorter budget. */
export const HEALTH_CLIENT_TIMEOUT_MS = 7_000

const SCOPES = [
  'http_response',
  'connection',
  'connection_and_schema',
  'configuration',
  'model_list',
  'profile',
  'dependency',
  'local_directory',
  'bucket_head',
  'not_monitored',
  'probe',
]

function fail(message: string): never {
  throw new ApiError(200, 'health_invalid_response', message)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readString(source: Record<string, unknown>, key: string, where: string): string {
  const value = source[key]
  if (typeof value !== 'string' || !value) fail(`${where} 缺少 ${key}`)
  return value as string
}

function readBoolean(source: Record<string, unknown>, key: string, where: string): boolean {
  const value = source[key]
  if (typeof value !== 'boolean') fail(`${where} 的 ${key} 不合法`)
  return value
}

function readNonNegativeInt(source: Record<string, unknown>, key: string, where: string): number {
  const value = source[key]
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) fail(`${where} 的 ${key} 不合法`)
  return value
}

function parseCheck(key: HealthCheckKey, value: unknown): HealthCheck {
  const where = `checks.${key}`
  if (!isRecord(value)) fail(`${where} 不是对象`)
  const state = readString(value, 'state', where)
  if (!HEALTH_STATES.includes(state as HealthState)) fail(`${where} 的 state 未知：${state}`)
  if (typeof value['required'] !== 'boolean') fail(`${where} 的 required 不合法`)
  const latency = value['latency_ms']
  if (latency !== null && (typeof latency !== 'number' || !Number.isInteger(latency) || latency < 0)) {
    fail(`${where} 的 latency_ms 不合法`)
  }
  const scope = readString(value, 'scope', where)
  if (!SCOPES.includes(scope)) fail(`${where} 的 scope 未知：${scope}`)
  const code = readString(value, 'code', where)
  const check: HealthCheck = {
    state: state as HealthState,
    required: value['required'] as boolean,
    code,
    message: readString(value, 'message', where),
    latency_ms: (latency ?? null) as number | null,
    scope,
  }
  // backend is storage-only: any other component claiming one means an unknown schema.
  if (value['backend'] !== undefined) {
    if (key !== 'storage' || (value['backend'] !== 'local' && value['backend'] !== 's3')) {
      fail(`${where} 的 backend 不合法`)
    }
    check.backend = value['backend']
  }
  return check
}

/**
 * Validate one report. A reverse proxy may answer with HTML, a newer backend with an unknown schema
 * version, and a half-broken one with missing checks; none of those may be rendered as “normal”.
 */
export function parseHealthReport(value: unknown): HealthReport {
  if (!isRecord(value)) fail('健康检查响应不是 JSON 对象')
  if (value['schema_version'] !== HEALTH_SCHEMA_VERSION) {
    fail(`健康检查契约版本不受支持：${String(value['schema_version'])}`)
  }
  const status = readString(value, 'status', 'report')
  if (!HEALTH_STATUSES.includes(status as HealthStatus)) fail(`健康检查汇总状态未知：${status}`)
  const checks = value['checks']
  if (!isRecord(checks)) fail('health 缺少 checks')
  const parsed = {} as Record<HealthCheckKey, HealthCheck>
  for (const key of HEALTH_CHECK_KEYS) {
    if (!(key in checks)) fail(`health 缺少 ${key} 检查项`)
    parsed[key] = parseCheck(key, checks[key])
  }
  return {
    schema_version: HEALTH_SCHEMA_VERSION,
    status: status as HealthStatus,
    checked_at: readString(value, 'checked_at', 'report'),
    duration_ms: readNonNegativeInt(value, 'duration_ms', 'report'),
    cached: readBoolean(value, 'cached', 'report'),
    age_ms: readNonNegativeInt(value, 'age_ms', 'report'),
    ttl_ms: readNonNegativeInt(value, 'ttl_ms', 'report'),
    checks: parsed,
  }
}

/** One GET with a client-side wait limit; caller cancellation stays distinguishable from a timeout. */
export async function fetchHealthReport(signal?: AbortSignal): Promise<HealthReport> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, HEALTH_CLIENT_TIMEOUT_MS)
  const forward = () => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', forward, { once: true })
  }
  const stopWaiting = () => {
    clearTimeout(timer)
    if (signal) signal.removeEventListener('abort', forward)
  }
  try {
    const payload = await getJson<unknown>(HEALTH_ENDPOINT, { signal: controller.signal })
    const report = parseHealthReport(payload)
    stopWaiting()
    return report
  } catch (error) {
    stopWaiting()
    if (timedOut && isAbortError(error)) {
      throw new ApiError(
        408,
        'health_timeout',
        `等待健康检查超过 ${HEALTH_CLIENT_TIMEOUT_MS / 1000} 秒，后端可能仍在探测依赖`,
      )
    }
    throw error
  }
}

export const HEALTH_SUMMARY_LABELS: Record<HealthStatus, string> = {
  healthy: '连接检查通过',
  degraded: '部分可选服务异常',
  unavailable: '必需服务不可用',
  unknown: '无法判断',
}

export const HEALTH_STATE_LABELS: Record<HealthState, string> = {
  connected: '检查通过',
  down: '检查失败',
  not_configured: '未配置',
  disabled: '已禁用',
  unknown: '无法判断',
}

export const HEALTH_CHECK_LABELS: Record<HealthCheckKey, string> = {
  api: 'API 服务',
  database: '数据库',
  embedding: 'Embedding 服务',
  storage: '当前存储 profile',
  vision: '可选视觉服务',
  decision: '可选决策服务',
  worker: '处理 Worker',
}

/** Neutral names for what a scope looked at; they must not claim the check succeeded. */
export const HEALTH_SCOPE_LABELS: Record<string, string> = {
  http_response: 'HTTP 入口应答',
  connection: '数据库连接',
  connection_and_schema: '数据库连接、迁移版本与关键结构',
  configuration: '服务端配置解析',
  model_list: '模型列表接口',
  profile: '存储 profile 记录',
  dependency: '依赖数据库的 profile 判定',
  local_directory: '本地存储目录',
  bucket_head: 'S3 Bucket',
  not_monitored: '未接入监测',
  probe: '本轮探测',
}

/** What the check proves — and what it deliberately does not. */
export function healthBoundaryText(key: HealthCheckKey, check: HealthCheck): string {
  if (key === 'worker') return '未监测：尚无 Worker 心跳，无法判断是否运行。'
  const parts: string[] = []
  if (check.state === 'connected') {
    if (key === 'embedding' || key === 'vision' || key === 'decision') {
      parts.push('模型列表检查通过，未执行推理，不代表生成质量或检索效果。')
    } else if (key === 'storage') {
      parts.push(
        check.backend === 's3'
          ? '仅验证 Bucket 可访问，未验证对象读写权限与存量文件。'
          : '仅验证目录可打开，未验证可写、磁盘余量与原件完整性。',
      )
    } else if (key === 'database') {
      parts.push('检查连接、迁移版本与关键结构，未读取任何资料正文或向量。')
    }
  } else if (check.state === 'disabled') {
    parts.push('已显式禁用：本轮未发起任何网络请求。')
  } else if (check.state === 'not_configured') {
    parts.push('未配置：该项不参与本轮检查。')
  } else if (check.state === 'unknown') {
    parts.push('本轮未能完成观察，不能据此判断依赖离线。')
  }
  if (key === 'decision') parts.push('是否实际调用决策路由取决于输入与服务端路由配置，本检查不验证路由执行。')
  return parts.join('')
}

export function healthAdviceText(key: HealthCheckKey, check: HealthCheck): string {
  if (check.state === 'connected' || key === 'worker') return ''
  if (check.code === 'profile_not_initialized') return '在服务端执行 python -m mmkb.storage init 后重新检查。'
  if (check.code === 'profile_conflict') return '保持存储后端不变，或改用新的 STORAGE_PROFILE_NAME 后初始化。'
  if (check.code === 'schema_missing' || check.code === 'schema_outdated') {
    return '在服务端执行 alembic upgrade head 后重新检查。'
  }
  if (check.code === 'schema_incompatible') return '核对数据库迁移版本与关键结构；本接口不会自动修复。'
  if (check.state === 'down' && check.code.startsWith('model_')) {
    return '检查服务端模型地址、API Key 与模型 ID；本页不展示这些配置值。'
  }
  if (check.state === 'down') return '按错误码检查服务端配置或对应服务，然后手动重新检查。'
  return ''
}
