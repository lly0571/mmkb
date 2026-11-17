import { API_PREFIX, postForm } from './http'
import { normalizeTags } from './resources'
import type { CreateSearchInput, SearchResponse } from './types'

export const DEFAULT_TOP_K = 20
export const TOP_K_MIN = 1
export const TOP_K_MAX = 100

/**
 * Search uses multipart/form-data like the backend endpoint. A query image is used only for
 * this request: it is never imported, so no resource is created here.
 */
export function searchForm(input: CreateSearchInput): FormData {
  const form = new FormData()
  if (input.file) {
    form.append('file', input.file, input.filename ?? (input.file instanceof File ? input.file.name : 'image'))
  }
  if (input.text && input.text.trim()) form.append('text', input.text.trim())
  for (const tag of normalizeTags(input.tags)) form.append('tags', tag)
  const topK = input.topK ?? DEFAULT_TOP_K
  form.append('top_k', String(Math.min(TOP_K_MAX, Math.max(TOP_K_MIN, Math.trunc(topK)))))
  return form
}

export function searchResources(input: CreateSearchInput, signal?: AbortSignal): Promise<SearchResponse> {
  return postForm<SearchResponse>(`${API_PREFIX}/search`, searchForm(input), { signal })
}
