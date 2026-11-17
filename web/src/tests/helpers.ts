import { vi } from 'vitest'

import type { CreatedResource, ResourceDetail, ResourceList, SearchItem, SearchResponse } from '../api/types'

export interface RecordedCall {
  url: string
  path: string
  search: URLSearchParams
  method: string
  headers: Record<string, string>
  body: BodyInit | null
  formData: FormData | null
  json: Record<string, unknown> | null
}

export type FetchHandler = (call: RecordedCall) => Response | Promise<Response>

export interface FetchOptions {
  /**
   * Real fetch rejects on AbortSignal. Some tests need a proxy that ignores cancellation to prove
   * the UI also drops the late payload, so the wiring can be turned off explicitly.
   */
  respectSignal?: boolean
}

function headerRecord(init?: HeadersInit): Record<string, string> {
  const result: Record<string, string> = {}
  if (!init) return result
  if (init instanceof Headers) {
    init.forEach((value, key) => {
      result[key.toLowerCase()] = value
    })
    return result
  }
  if (Array.isArray(init)) {
    for (const [key, value] of init) result[key.toLowerCase()] = value
    return result
  }
  for (const [key, value] of Object.entries(init)) result[key.toLowerCase()] = String(value)
  return result
}

export function installFetch(handler: FetchHandler, options: FetchOptions = {}): RecordedCall[] {
  const { respectSignal = true } = options
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const absolute = new URL(url, 'http://localhost:5173')
    const body = (init?.body ?? null) as BodyInit | null
    let json: Record<string, unknown> | null = null
    if (typeof body === 'string') {
      try {
        json = JSON.parse(body) as Record<string, unknown>
      } catch {
        json = null
      }
    }
    const call: RecordedCall = {
      url,
      path: absolute.pathname,
      search: absolute.searchParams,
      method: (init?.method ?? 'GET').toUpperCase(),
      headers: headerRecord(init?.headers),
      body,
      formData: body instanceof FormData ? body : null,
      json,
    }
    calls.push(call)
    const result = Promise.resolve().then(() => handler(call))
    const signal = init?.signal
    if (!respectSignal || !signal) return result
    return await Promise.race([result, abortRejection(signal)])
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function abortRejection(signal: AbortSignal): Promise<never> {
  return new Promise<never>((_resolve, reject) => {
    const fail = () => reject(new DOMException('The user aborted a request.', 'AbortError'))
    if (signal.aborted) fail()
    else signal.addEventListener('abort', fail, { once: true })
  })
}

export function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
}

export function noContentResponse(): Response {
  return new Response(null, { status: 204 })
}

export function htmlResponse(status: number, body: string): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'text/html' } })
}

export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const ISO = '2026-11-16T08:00:00Z'

export function makeDetail(overrides: Partial<ResourceDetail> = {}): ResourceDetail {
  const id = overrides.id ?? '6f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b'
  return {
    id,
    input_kind: 'text_image',
    workflow: 'text_image',
    title: '金融图表笔记',
    tags: ['金融'],
    user_text: '一张金融图表的配文',
    caption: null,
    ocr_text: null,
    extra: {},
    sha256: 'a'.repeat(64),
    status: 'ready',
    error: null,
    revision: 3,
    created_at: ISO,
    updated_at: ISO,
    original_url: `/api/resources/${id}/original`,
    ...overrides,
  }
}

export function makeCreated(overrides: Partial<CreatedResource> = {}): CreatedResource {
  return { ...makeDetail(overrides), duplicate: false, ...overrides } as CreatedResource
}

export function makeList(items: ResourceDetail[], overrides: Partial<ResourceList> = {}): ResourceList {
  return { items, total: items.length, limit: 20, offset: 0, ...overrides }
}

export function makeSearchItem(overrides: Partial<SearchItem> = {}): SearchItem {
  const id = overrides.resource_id ?? '1f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b'
  return {
    resource_id: id,
    input_kind: 'image',
    title: '相似图表',
    tags: ['金融'],
    summary: '摘要文字',
    score: 0.8123,
    detail_url: `/api/resources/${id}`,
    original_url: `/api/resources/${id}/original`,
    ...overrides,
  }
}

export function makeSearch(items: SearchItem[]): SearchResponse {
  return { mode: 'vector', items }
}

export function pngFile(name = 'chart.png', bytes = 12): File {
  return new File([new Uint8Array(bytes).fill(137)], name, { type: 'image/png' })
}

export function callsTo(calls: RecordedCall[], method: string, pathFragment: string): RecordedCall[] {
  return calls.filter((call) => call.method === method && call.path.includes(pathFragment))
}
