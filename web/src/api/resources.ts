import { API_PREFIX, deleteResource_, getJson, patchJson, postForm, postJson, originalPath } from './http'
import type {
  CreateResourceInput,
  CreatedResource,
  ListQuery,
  ResourceDetail,
  ResourceList,
  ResourcePatchInput,
} from './types'

/** Mirrors backend normalize_tags: trim, drop blanks, de-duplicate preserving order. */
export function normalizeTags(tags: readonly string[] | null | undefined): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const tag of tags ?? []) {
    if (typeof tag !== 'string') continue
    const value = tag.trim()
    if (!value || seen.has(value)) continue
    seen.add(value)
    result.push(value)
  }
  return result
}

export function resourcePath(resourceId: string): string {
  return `${API_PREFIX}/resources/${resourceId}`
}

export function createResourceForm(input: CreateResourceInput): FormData {
  const form = new FormData()
  if (input.file) {
    form.append('file', input.file, input.filename ?? (input.file instanceof File ? input.file.name : 'image'))
  }
  // The backend defaults title to ""; sending a blank title is equivalent and harmless.
  form.append('title', input.title?.trim() ?? '')
  if (input.userText && input.userText.trim()) form.append('user_text', input.userText)
  if (input.workflow) form.append('workflow', input.workflow)
  for (const tag of normalizeTags(input.tags)) form.append('tags', tag)
  return form
}

export function listResourcePath(query: ListQuery): string {
  const params = new URLSearchParams()
  params.set('limit', String(query.limit))
  params.set('offset', String(query.offset))
  // Empty filters are omitted: the backend types status/input_kind as strict literals.
  if (query.status) params.set('status', query.status)
  if (query.input_kind) params.set('input_kind', query.input_kind)
  for (const tag of normalizeTags(query.tags)) params.append('tags', tag)
  return `${API_PREFIX}/resources?${params.toString()}`
}

export function createResource(input: CreateResourceInput, signal?: AbortSignal): Promise<CreatedResource> {
  return postForm<CreatedResource>(`${API_PREFIX}/resources`, createResourceForm(input), { signal })
}

export function listResources(query: ListQuery, signal?: AbortSignal): Promise<ResourceList> {
  return getJson<ResourceList>(listResourcePath(query), { signal })
}

export function getResource(resourceId: string, signal?: AbortSignal): Promise<ResourceDetail> {
  return getJson<ResourceDetail>(resourcePath(resourceId), { signal })
}

export function patchResource(
  resourceId: string,
  patch: ResourcePatchInput,
  signal?: AbortSignal,
): Promise<ResourceDetail> {
  return patchJson<ResourceDetail>(resourcePath(resourceId), patch, { signal })
}

export function retryResource(resourceId: string, revision: number, signal?: AbortSignal): Promise<ResourceDetail> {
  return postJson<ResourceDetail>(`${resourcePath(resourceId)}/retry`, { revision }, { signal })
}

export function deleteResource(resourceId: string, signal?: AbortSignal): Promise<void> {
  return deleteResource_(resourcePath(resourceId), { signal })
}

export { originalPath }
