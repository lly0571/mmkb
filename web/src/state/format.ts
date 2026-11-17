// Display labels and formatting. Values shown here never come from user content.
import type { InputKind, ProcessingWarning, ResourceStatus, Workflow } from '../api/types'

export const STATUS_LABELS: Record<ResourceStatus, string> = {
  pending: '待处理',
  processing: '处理中',
  ready: '已就绪',
  failed: '失败',
}

export const STATUS_HINTS: Record<ResourceStatus, string> = {
  pending: '后台处理中，尚未进入检索索引',
  processing: '后台处理中，尚未进入检索索引',
  ready: '已建立索引，可被检索',
  failed: '处理失败，未进入检索索引',
}

export const KIND_LABELS: Record<InputKind, string> = {
  image: '仅图片',
  text_image: '图文',
  text: '纯文本',
}

export const WORKFLOW_LABELS: Record<Workflow, string> = {
  structured_image: '结构化图片（图表/表格）',
  visual_image: '视觉图片',
  visdoc: '图文文档（OCR）',
  text_image: '图文（以配文为主）',
  text: '纯文本',
}

export const WORKFLOW_AUTO_LABEL = '自动识别（等待处理模块判断）'

export const WARNING_STAGE_LABELS: Record<string, string> = {
  classify: '自动分类',
  caption: '图像描述（Caption）',
  ocr: '文字识别（OCR）',
}

export const WARNING_CODE_LABELS: Record<string, string> = {
  unavailable: '可选视觉服务未配置，本步骤已跳过',
  invalid_response: '视觉服务返回了无效结果',
  input_too_large: '生成的文字超出服务端长度上限',
  timeout: '视觉服务响应超时',
  connection: '无法连接视觉服务',
  rate_limited: '视觉服务限流',
  invalid_image: '图片无法被视觉服务解析',
  provider_error: '视觉服务调用失败',
}

export function warningLabel(warning: ProcessingWarning): string {
  const stage = WARNING_STAGE_LABELS[warning.stage] ?? warning.stage
  const reason = WARNING_CODE_LABELS[warning.code] ?? '未记录的警告原因'
  return `${stage}：${reason}`
}

export function warningCode(warning: ProcessingWarning): string {
  return `${warning.stage}/${warning.code}`
}

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  return dateFormatter.format(new Date(timestamp))
}

/** Cosine similarity: comparable ranking, not a probability; it may be negative. */
export function formatScore(score: number): string {
  if (!Number.isFinite(score)) return '—'
  return score.toFixed(4)
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(1)} ${units[index]}`
}

export function truncate(value: string, max: number): string {
  const single = value.replace(/\s+/g, ' ').trim()
  return single.length <= max ? single : `${single.slice(0, max)}…`
}

export const ACCEPTED_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp']
export const ACCEPT_ATTRIBUTE = ACCEPTED_IMAGE_TYPES.join(',')

/** Deployment defaults from MAX_UPLOAD_BYTES / MAX_TEXT_CHARS; the server is authoritative. */
export const DOCUMENTED_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
export const DOCUMENTED_MAX_TEXT_CHARS = 100_000

export function isAcceptedImageType(file: File | Blob): boolean {
  if (!file.type) return false
  return ACCEPTED_IMAGE_TYPES.includes(file.type)
}
