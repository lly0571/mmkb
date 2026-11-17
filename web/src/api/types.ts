// Hand-written contract types mirroring mmkb/api.py. Re-check against a running
// instance's /openapi.json whenever the backend changes.

export type Workflow = 'structured_image' | 'visual_image' | 'visdoc' | 'text_image' | 'text'
export type ImageWorkflow = 'structured_image' | 'visual_image' | 'visdoc'
export type ResourceStatus = 'pending' | 'processing' | 'ready' | 'failed'
export type InputKind = 'image' | 'text_image' | 'text'

export interface ResourceDetail {
  id: string
  input_kind: InputKind
  workflow: Workflow | null
  title: string
  tags: string[]
  user_text: string | null
  caption: string | null
  ocr_text: string | null
  extra: Record<string, unknown>
  sha256: string | null
  status: ResourceStatus
  error: string | null
  revision: number
  created_at: string
  updated_at: string
  original_url: string | null
}

export interface CreatedResource extends ResourceDetail {
  duplicate: boolean
}

export interface ResourceList {
  items: ResourceDetail[]
  total: number
  limit: number
  offset: number
}

// Search hits carry strictly fewer fields than ResourceDetail: no status, no revision.
export interface SearchItem {
  resource_id: string
  input_kind: InputKind
  title: string
  tags: string[]
  summary: string
  score: number
  detail_url: string
  original_url: string | null
}

export interface SearchResponse {
  mode: 'vector'
  items: SearchItem[]
}

export interface ProcessingWarning {
  stage: string
  code: string
}

export interface ListQuery {
  limit: number
  offset: number
  status?: ResourceStatus | ''
  input_kind?: InputKind | ''
  tags?: string[]
}

export interface CreateResourceInput {
  file?: File | Blob | null
  filename?: string
  title?: string
  userText?: string | null
  tags?: string[]
  // null / undefined means "let the server classify": the workflow field is then omitted,
  // never sent as the literal string "null".
  workflow?: ImageWorkflow | null
}

export interface CreateSearchInput {
  file?: File | Blob | null
  filename?: string
  text?: string | null
  tags?: string[]
  topK?: number
}

/**
 * PATCH body. `null` for caption/ocr_text means "clear and let the worker regenerate";
 * omitting the field keeps the stored value; a string (including "") is a manual value.
 */
export interface ResourcePatchInput {
  revision: number
  title?: string
  tags?: string[]
  user_text?: string
  caption?: string | null
  ocr_text?: string | null
  workflow?: ImageWorkflow | null
}

export interface ErrorDetail {
  field: string
  message: string
}

export const IMAGE_WORKFLOWS: ImageWorkflow[] = ['structured_image', 'visual_image', 'visdoc']

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Read extra.processing.warnings without assuming any other extra keys exist. */
export function readWarnings(extra: Record<string, unknown> | null | undefined): ProcessingWarning[] {
  if (!extra) return []
  const processing = extra['processing']
  if (!isRecord(processing)) return []
  const warnings = processing['warnings']
  if (!Array.isArray(warnings)) return []
  return warnings.filter(
    (item): item is ProcessingWarning => isRecord(item) && typeof item['stage'] === 'string' && typeof item['code'] === 'string',
  )
}

export function resourceSummaryText(resource: ResourceDetail): string {
  for (const value of [resource.user_text, resource.caption, resource.ocr_text]) {
    if (value && value.trim()) return value.trim()
  }
  return ''
}
