// Contract parsing for GET /api/health: HTML pages, unknown versions and half-filled reports must
// never render as a healthy connection status.
import { describe, expect, it, vi } from 'vitest'

import { HEALTH_CLIENT_TIMEOUT_MS, HEALTH_SCOPE_LABELS, fetchHealthReport, healthBoundaryText, parseHealthReport } from '../api/health'
import { HEALTH_CHECK_KEYS } from '../api/types'
import { ApiError } from '../api/http'
import { deferred, installFetch, jsonResponse, makeHealthReport } from './helpers'

function expectRejected(value: unknown): string {
  try {
    parseHealthReport(value)
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return (error as ApiError).message
  }
  throw new Error('expected parseHealthReport to reject the payload')
}

describe('health scope vocabulary', () => {
  it('names the checked object without claiming the check passed', () => {
    // Scope names render in every state, so none of them may read as a success verdict.
    for (const [scope, label] of Object.entries(HEALTH_SCOPE_LABELS)) {
      expect(label, scope).not.toMatch(/已核对|可访问|可打开|已读取|连接可用|检查通过|已响应/)
    }
    expect(Object.keys(HEALTH_SCOPE_LABELS).length).toBeGreaterThan(8)
  })

  it.each(['connected', 'disabled', 'down'] as const)('does not infer decision routing from a %s list check', (state) => {
    const report = parseHealthReport(makeHealthReport({ checks: { decision: { state } } }))
    const text = healthBoundaryText('decision', report.checks.decision)
    expect(text).toContain('输入与服务端路由配置')
    expect(text).toContain('本检查不验证路由执行')
    expect(text).not.toContain('未接入 Worker')
  })
})

describe('health report parsing', () => {
  it('accepts the documented full report and keeps every check key', () => {
    const report = parseHealthReport(makeHealthReport())
    expect(report.status).toBe('healthy')
    expect(Object.keys(report.checks)).toEqual(HEALTH_CHECK_KEYS)
    expect(report.checks.storage.backend).toBe('s3')
    expect(report.checks.worker.latency_ms).toBeNull()
  })

  it('rejects HTML, arrays and null instead of guessing', () => {
    for (const value of ['<html><body>502 Bad Gateway</body></html>', null, [], 7, '{"checks":']) {
      expectRejected(value)
    }
  })

  it('rejects an unknown schema version', () => {
    expect(expectRejected(makeHealthReport({ schema_version: 2 }))).toMatch(/契约版本/)
    expect(expectRejected(makeHealthReport({ schema_version: '1' }))).toMatch(/契约版本/)
  })

  it('rejects unknown summary and check states', () => {
    expect(expectRejected(makeHealthReport({ status: 'green' }))).toMatch(/汇总状态/)
    expect(
      expectRejected(
        makeHealthReport({ checks: { embedding: { state: 'fine' } } }),
      ),
    ).toMatch(/state/)
  })

  it('requires every check key to be present', () => {
    const payload = makeHealthReport() as Record<string, Record<string, unknown>>
    delete payload['checks']['database']
    expect(expectRejected(payload)).toMatch(/database/)
  })

  it('rejects wrong field types that would break the display', () => {
    expect(expectRejected(makeHealthReport({ duration_ms: -1 }))).toMatch(/duration_ms/)
    expect(expectRejected(makeHealthReport({ cached: 'true' }))).toMatch(/cached/)
    expect(
      expectRejected(makeHealthReport({ checks: { api: { required: 'yes' } } })),
    ).toMatch(/required/)
    expect(
      expectRejected(makeHealthReport({ checks: { database: { latency_ms: '30ms' } } })),
    ).toMatch(/latency_ms/)
    expect(
      expectRejected(makeHealthReport({ checks: { embedding: { scope: 'everything' } } })),
    ).toMatch(/scope/)
    expect(expectRejected(makeHealthReport({ checks: { vision: { backend: 's3' } } }))).toMatch(/backend/)
    expect(
      expectRejected(makeHealthReport({ checks: { storage: { backend: 'ceph' } } })),
    ).toMatch(/backend/)
  })

  it('fetches /api/health once and returns the validated report', async () => {
    const calls = installFetch(() => jsonResponse(200, makeHealthReport()))
    const report = await fetchHealthReport()
    expect(report.status).toBe('healthy')
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe('GET')
    expect(calls[0].path).toBe('/api/health')
    expect(calls[0].headers['accept']).toBe('application/json')
  })

  it('surfaces a proxy HTML answer as an invalid health response, not as healthy', async () => {
    installFetch(() => new Response('<html>nginx</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }))
    await expect(fetchHealthReport()).rejects.toBeInstanceOf(ApiError)
  })

  it('turns a wedged backend into a client timeout after the wait limit', async () => {
    vi.useFakeTimers()
    try {
      const pending = deferred<Response>()
      const calls = installFetch(() => pending.promise)
      const request = fetchHealthReport()
      let failure: unknown = null
      void request.catch((error: unknown) => {
        failure = error
      })
      await vi.advanceTimersByTimeAsync(HEALTH_CLIENT_TIMEOUT_MS - 1)
      expect(failure).toBeNull()
      await vi.advanceTimersByTimeAsync(2)
      await expect(request).rejects.toMatchObject({ code: 'health_timeout', status: 408 })
      expect(calls).toHaveLength(1)
      expect(failure).toBeInstanceOf(ApiError)
      expect(calls[0].signal?.aborted).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps caller cancellation distinguishable from a timeout', async () => {
    installFetch(() => deferred<Response>().promise)
    const controller = new AbortController()
    const request = fetchHealthReport(controller.signal)
    controller.abort()
    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
  })
})
