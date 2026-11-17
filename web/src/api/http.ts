// Single HTTP boundary: FormData vs JSON, 204, binary URLs, error shape, cancellation.
// POST/PATCH/DELETE are never replayed automatically; GET retries are explicit caller actions.
import type { ErrorDetail } from './types'

export const API_PREFIX = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: ErrorDetail[]
  readonly rawMessage: string

  constructor(status: number, code: string, message: string, details: ErrorDetail[] = []) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.message = message
    this.details = details
    this.rawMessage = message
  }

  /** Network failure or aborted fetch: the server outcome is unknown. */
  get outcomeUnknown(): boolean {
    return this.status === 0
  }

  get dependencyFailure(): boolean {
    return this.status === 502 || this.status === 503 || this.status === 504
  }

  get isConflict(): boolean {
    return this.status === 409
  }

  get isNotFound(): boolean {
    return this.status === 404
  }
}

const STATUS_HINTS: Record<number, string> = {
  400: '请求无法处理',
  401: '请求未被接受（当前系统范围为单用户本地运行）',
  403: '请求被拒绝',
  404: '资料或原件不存在，可能已被删除',
  409: '版本冲突或当前状态不允许该操作，请重新读取最新详情后再决定',
  413: '内容超出服务端大小或长度限制',
  415: '图片格式不受支持（仅 PNG/JPEG/WebP，按真实内容校验）',
  422: '输入不合法',
  500: '服务端内部错误，请查看后端日志',
  502: '依赖的模型服务返回异常',
  503: '后端依赖（数据库/存储/模型）暂不可用',
  504: '依赖服务响应超时，可稍后手动重试',
}

const CODE_HINTS: Record<string, string> = {
  import_busy: '相同图片或请求正在导入，请先核查回执后重试',
  import_key_required: '批量导入请求缺少提交标识',
  idempotency_conflict: '提交标识对应的图片或参数已变化，请核对清单',
  source_changed: '图片内容与预览清单不一致，请重新核验',
  import_resource_deleted: '该提交原先关联的资料已删除，需要新建导入清单',
  network_error: '无法连接后端，请确认 API 已启动且代理目标正确；提交结果未知时请先核对资料列表',
  parse_error: '响应格式无法解析，请检查代理是否把接口改写成了页面',
  empty_input: '请提供图片或非空文字',
  invalid_input: '输入不合法',
  invalid_tags: '标签必须是字符串列表',
  invalid_title: '标题不能为空值',
  invalid_text: '字段必须是文字或 null',
  invalid_workflow: '所选路线与输入形式不兼容',
  invalid_fields: '包含不支持的编辑字段',
  invalid_status: '未知的状态筛选',
  invalid_input_kind: '未知的输入类型筛选',
  invalid_pagination: '分页参数不合法（limit 1..100，offset ≥ 0）',
  invalid_top_k: 'Top-K 需在 1..100 之间',
  invalid_resource: '资料内容与记录的输入形式不一致',
  invalid_key: '存储 key 不合法',
  text_too_large: '文字字段超出 MAX_TEXT_CHARS',
  file_too_large: '图片超出 MAX_UPLOAD_BYTES',
  unsupported_media_type: '图片格式不受支持',
  revision_conflict: '资料已被修改：请读取最新详情，再决定如何应用本地草稿',
  invalid_state: '当前状态不允许该操作（只有失败资料可以重试）',
  resource_not_found: '资料不存在或已删除',
  original_not_found: '该资料没有可读取的原件（纯文本资料无原件，或文件已被清理）',
  storage_unavailable: '存储不可用，请检查服务端存储配置与对象服务',
  storage_config: '存储配置错误，请检查服务端配置',
  database_unavailable: '数据库不可用，请检查连接与迁移',
  model_config: '模型服务配置缺失或错误，请检查服务端配置',
  model_unavailable: '模型服务暂不可用',
  embedding_timeout: 'Embedding 服务超时，请稍后重试',
  embedding_invalid_response: 'Embedding 服务返回无效结果',
  embedding_connection: '无法连接 Embedding 服务',
  validation_error: '请求字段校验失败',
  http_error: '请求失败',
  worker_lock_lost: '处理 Worker 已失去所有权，需要运维介入恢复',
  aborted: '请求已取消',
}

const PREFIX_HINTS: [string, string][] = [
  ['embedding_', '查询向量编码失败，请稍后重试或检查 Embedding 服务'],
  ['provider_', '模型服务调用失败'],
]

export function explainError(error: unknown): string {
  const apiError = toApiError(error)
  const hint =
    CODE_HINTS[apiError.code] ??
    PREFIX_HINTS.find(([prefix]) => apiError.code.startsWith(prefix))?.[1] ??
    STATUS_HINTS[apiError.status]
  if (hint && apiError.code === 'http_error') return hint
  if (hint && apiError.message && !hint.includes(apiError.message)) return `${hint}（${apiError.message}）`
  return hint ?? apiError.message
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  if (isAbort(error)) return new ApiError(-1, 'aborted', '请求已取消')
  const message = error instanceof Error ? error.message : String(error)
  return new ApiError(0, 'network_error', message || '请求失败')
}

export function isAbortError(error: unknown): boolean {
  return isAbort(error) || (error instanceof ApiError && error.code === 'aborted')
}

function isAbort(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: string }).name === 'AbortError'
  )
}

function errorDetails(payload: Record<string, unknown>): ErrorDetail[] {
  const details = payload['details']
  if (!Array.isArray(details)) return []
  return details
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({ field: String(item['field'] ?? ''), message: String(item['message'] ?? '') }))
}

async function toApiErrorFrom(response: Response): Promise<ApiError> {
  const fallback = STATUS_HINTS[response.status] ?? `请求失败（HTTP ${response.status}）`
  let payload: unknown = null
  let text = ''
  try {
    text = await response.text()
  } catch {
    text = ''
  }
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }
  // Reverse proxies and static servers can answer with HTML for /api paths; keep the status meaning.
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    const snippet = text.trim().slice(0, 120).replace(/\s+/g, ' ')
    return new ApiError(response.status, 'http_error', snippet || fallback)
  }
  const record = payload as Record<string, unknown>
  const code = typeof record['code'] === 'string' ? record['code'] : 'http_error'
  const message = typeof record['message'] === 'string' && record['message'] ? record['message'] : fallback
  return new ApiError(response.status, code, message, errorDetails(record))
}

export interface RequestOptions {
  signal?: AbortSignal
  headers?: Record<string, string>
}

async function send(request: Promise<Response>): Promise<Response> {
  let response: Response
  try {
    response = await request
  } catch (error) {
    if (isAbort(error)) throw error
    throw toApiError(error)
  }
  if (!response.ok) throw await toApiErrorFrom(response)
  return response
}

/** JSON request. FormData must never carry a hand-written Content-Type: the browser adds the boundary. */
async function requestJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await send(fetch(path, init))
  if (response.status === 204 || response.status === 205) return undefined as T
  const text = await response.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError(response.status, 'parse_error', '响应不是有效 JSON')
  }
}

export function getJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, { method: 'GET', headers: { Accept: 'application/json' }, signal: options.signal })
}

export function postJson<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  })
}

export function patchJson<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, {
    method: 'PATCH',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  })
}

export function postForm<T>(path: string, form: FormData, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, { method: 'POST', headers: options.headers, body: form, signal: options.signal })
}

/** DELETE returns 204 with no body; never try to parse JSON from it. */
export async function deleteResource_(path: string, options: RequestOptions = {}): Promise<void> {
  const response = await send(fetch(path, { method: 'DELETE', signal: options.signal }))
  if (response.status === 204) return
  await response.body?.cancel?.().catch(() => undefined)
}

/** Same-origin URL for the binary original; loaded by <img>, never decoded as JSON. */
export function originalPath(resourceId: string): string {
  return `${API_PREFIX}/resources/${resourceId}/original`
}
