import { describe, expect, it } from 'vitest'

import { ApiError, deleteResource_, explainError, getJson, originalPath, patchJson, postForm, postJson } from '../api/http'
import { callsTo, deferred, htmlResponse, installFetch, jsonResponse, noContentResponse } from './helpers'

describe('http layer', () => {
  it('parses JSON GET responses without a request body', async () => {
    const calls = installFetch(() => jsonResponse(200, { ok: true }))
    await expect(getJson<{ ok: boolean }>('/api/resources/x')).resolves.toEqual({ ok: true })
    expect(calls[0].method).toBe('GET')
    expect(calls[0].body).toBeNull()
    expect(calls[0].headers['content-type']).toBeUndefined()
  })

  it('sets application/json only for JSON bodies', async () => {
    const calls = installFetch(() => jsonResponse(200, {}))
    await postJson('/api/resources/x/retry', { revision: 4 })
    await patchJson('/api/resources/x', { revision: 4, title: 'a' })
    expect(calls[0].headers['content-type']).toBe('application/json')
    expect(calls[0].json).toEqual({ revision: 4 })
    expect(calls[1].method).toBe('PATCH')
  })

  it('never hand-writes the FormData content type: the browser must add the boundary', async () => {
    const calls = installFetch(() => jsonResponse(201, {}))
    const form = new FormData()
    form.append('title', '标题')
    await postForm('/api/resources', form)
    expect(calls[0].headers['content-type']).toBeUndefined()
    expect(calls[0].formData?.get('title')).toBe('标题')
  })

  it('treats 204 as success without parsing a body', async () => {
    const calls = installFetch(() => noContentResponse())
    await expect(deleteResource_('/api/resources/x')).resolves.toBeUndefined()
    expect(calls[0].method).toBe('DELETE')
  })

  it('reads the {code,message,details} error shape', async () => {
    installFetch(() =>
      jsonResponse(422, {
        code: 'validation_error',
        message: 'Invalid request fields',
        details: [{ field: 'body.top_k', message: 'Input should be less than or equal to 100' }],
      }),
    )
    const error = await getJson('/api/resources').catch((cause: unknown) => cause) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(422)
    expect(error.code).toBe('validation_error')
    expect(error.details[0].field).toBe('body.top_k')
    expect(explainError(error)).toContain('请求字段校验失败')
  })

  it('keeps the status meaning when a proxy answers with HTML', async () => {
    installFetch(() => htmlResponse(502, '<html><body>Bad Gateway</body></html>'))
    const error = await getJson('/api/search').catch((cause: unknown) => cause) as ApiError
    expect(error.code).toBe('http_error')
    expect(error.status).toBe(502)
    expect(error.dependencyFailure).toBe(true)
    expect(explainError(error).length).toBeGreaterThan(0)
  })

  it('does not replay POST/PATCH/DELETE after a network failure', async () => {
    const calls = installFetch(() => Promise.reject(new TypeError('Failed to fetch')))
    const error = await postJson('/api/resources', { a: 1 }).catch((cause: unknown) => cause) as ApiError
    expect(callsTo(calls, 'POST', '/api/resources')).toHaveLength(1)
    expect(error.status).toBe(0)
    expect(error.outcomeUnknown).toBe(true)
    expect(explainError(error)).toContain('无法连接后端')
  })

  it('maps 409 conflicts to an actionable message', async () => {
    installFetch(() => jsonResponse(409, { code: 'revision_conflict', message: 'Resource changed; reload and retry' }))
    const error = await patchJson('/api/resources/x', { revision: 1 }).catch((cause: unknown) => cause) as ApiError
    expect(error.isConflict).toBe(true)
    expect(explainError(error)).toContain('资料已被修改')
  })

  it('exposes the original as a same-origin binary path, never a JSON call', async () => {
    const calls = installFetch(() => new Response(new Uint8Array([1]), { status: 200 }))
    const path = originalPath('abc')
    expect(path).toBe('/api/resources/abc/original')
    // Nothing in the JSON layer touches that path unless the caller asks for it.
    expect(calls).toHaveLength(0)
  })

  it('surfaces cancellation as an abort, not a failure message', async () => {
    const gate = deferred<Response>()
    installFetch(() => gate.promise)
    const controller = new AbortController()
    const pending = getJson('/api/resources', { signal: controller.signal })
    controller.abort()
    gate.resolve(jsonResponse(200, {}))
    await expect(pending).rejects.toThrowError()
    const error = await pending.catch((cause: unknown) => cause)
    expect((error as { name: string }).name).toBe('AbortError')
  })
})
