import { act, renderHook } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useResourceStatus } from '../state/useResourceStatus'
import { createManualVisibility } from '../state/visibility'
import { cacheDetail } from '../state/cache'
import { deferred, installFetch, jsonResponse, makeDetail } from './helpers'

const ID = '6f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b'

function resourceHandler(detail: () => ReturnType<typeof makeDetail>) {
  return installFetch((call) => {
    if (call.path.endsWith('/original')) return new Response(new Uint8Array([1]), { status: 200 })
    return jsonResponse(200, detail())
  })
}

const flush = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

describe('status polling', () => {
  it('ignores an old response even when cancellation is ignored after hiding and resuming', async () => {
    vi.useFakeTimers()
    const old = deferred<Response>()
    let count = 0
    const calls = installFetch(() => {
      count += 1
      return count === 2 ? old.promise : jsonResponse(200, makeDetail({
        id: ID, revision: count === 1 ? 2 : 3, status: count === 1 ? 'pending' : 'ready',
      }))
    }, { respectSignal: false })
    const visibility = createManualVisibility()
    const { result } = renderHook(() => useResourceStatus(ID, { visibility }))
    await flush()
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(calls).toHaveLength(2)
    act(() => visibility.setVisible(false))
    act(() => visibility.setVisible(true))
    await flush()
    expect(result.current.detail?.revision).toBe(3)
    await act(async () => old.resolve(jsonResponse(200, makeDetail({ id: ID, revision: 2, status: 'pending' }))))
    expect(result.current.detail?.revision).toBe(3)
    expect(result.current.detail?.status).toBe('ready')
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(calls).toHaveLength(3)
  })

  it('does not overlap timer requests and decides whether to stop from the accepted cache', async () => {
    vi.useFakeTimers()
    const slow = deferred<Response>()
    const calls = installFetch(() => slow.promise)
    const visibility = createManualVisibility()
    const { result } = renderHook(() => useResourceStatus(ID, { visibility }))
    await flush()
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(calls).toHaveLength(1)
    // A PATCH completed while this GET was running.
    act(() => { cacheDetail(makeDetail({ id: ID, revision: 5, status: 'ready' })) })
    await act(async () => slow.resolve(jsonResponse(200, makeDetail({ id: ID, revision: 4, status: 'processing' }))))
    expect(result.current.detail?.revision).toBe(5)
    expect(result.current.inFlight).toBe(false)
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(calls).toHaveLength(1)
  })

  it('invalidates in-flight results on manual refresh and on resource changes', async () => {
    const old = deferred<Response>()
    let count = 0
    installFetch((call) => {
      count += 1
      if (count === 1) return old.promise
      return jsonResponse(200, makeDetail({ id: call.path.split('/').at(-1), revision: 4 }))
    }, { respectSignal: false })
    const { result, rerender } = renderHook(({ id }) => useResourceStatus(id), { initialProps: { id: ID } })
    await act(async () => {})
    act(() => result.current.refresh())
    await act(async () => {})
    expect(result.current.detail?.revision).toBe(4)
    const other = '00000000-0000-4000-8000-000000000002'
    rerender({ id: other })
    await act(async () => {})
    await act(async () => old.resolve(jsonResponse(200, makeDetail({ id: ID, revision: 1 }))))
    expect(result.current.detail?.id).toBe(other)
    expect(result.current.error).toBeNull()
  })

  it('does not apply aborted StrictMode or unmounted responses', async () => {
    const slow = deferred<Response>()
    let count = 0
    installFetch(() => ++count === 1 ? slow.promise : jsonResponse(200, makeDetail({ id: ID, revision: 6 })), { respectSignal: false })
    const { result, unmount } = renderHook(() => useResourceStatus(ID), { wrapper: StrictMode })
    await act(async () => {})
    expect(result.current.detail?.revision).toBe(6)
    unmount()
    await act(async () => slow.resolve(jsonResponse(200, makeDetail({ id: ID, revision: 1, status: 'pending' }))))
    expect(result.current.detail?.revision).toBe(6)
  })

  it('refreshes about every two seconds while pending and stops when ready', async () => {
    vi.useFakeTimers()
    let status: 'pending' | 'ready' = 'pending'
    const calls = resourceHandler(() => makeDetail({ id: ID, status, updated_at: new Date().toISOString() }))
    const visibility = createManualVisibility(true)

    const { result } = renderHook(() => useResourceStatus(ID, { visibility, intervalMs: 2000 }))
    await flush()
    expect(calls).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(calls).toHaveLength(3)
    expect(result.current.inFlight).toBe(true)

    status = 'ready'
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(result.current.detail?.status).toBe('ready')
    expect(result.current.inFlight).toBe(false)

    const settled = calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(calls).toHaveLength(settled)
  })

  it('stops polling for failed resources and exposes the server error', async () => {
    vi.useFakeTimers()
    const calls = resourceHandler(() => makeDetail({ id: ID, status: 'failed', error: 'embedding_timeout: upstream' }))
    const visibility = createManualVisibility(true)
    const { result } = renderHook(() => useResourceStatus(ID, { visibility, intervalMs: 2000 }))
    await flush()
    expect(result.current.detail?.status).toBe('failed')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(calls).toHaveLength(1)
  })

  it('makes no requests while hidden and reloads as soon as the tab is visible again', async () => {
    vi.useFakeTimers()
    const calls = resourceHandler(() => makeDetail({ id: ID, status: 'processing' }))
    const visibility = createManualVisibility(false)
    renderHook(() => useResourceStatus(ID, { visibility, intervalMs: 2000 }))
    await flush()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000)
    })
    expect(calls).toHaveLength(0)

    act(() => visibility.setVisible(true))
    await flush()
    expect(calls).toHaveLength(1)
  })

  it('keeps the last good detail, marks it stale and backs off on failures', async () => {
    vi.useFakeTimers()
    let fail = false
    const calls = installFetch((call) => {
      if (call.path.endsWith('/original')) return new Response(new Uint8Array([1]), { status: 200 })
      if (fail) throw new TypeError('Failed to fetch')
      return jsonResponse(200, makeDetail({ id: ID, status: 'processing' }))
    })
    const visibility = createManualVisibility(true)
    const { result } = renderHook(() => useResourceStatus(ID, { visibility, intervalMs: 2000 }))
    await flush()
    expect(result.current.detail?.status).toBe('processing')

    fail = true
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(result.current.stale).toBe(true)
    expect(result.current.error?.code).toBe('network_error')
    // Backoff: the next attempt waits 4s, not 2s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(calls.filter((call) => call.method === 'GET')).toHaveLength(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(calls.filter((call) => call.method === 'GET')).toHaveLength(3)
    expect(result.current.detail?.status).toBe('processing')
  })

  it('says “still processing” once the observation window passes without changing server state', async () => {
    vi.useFakeTimers()
    const old = new Date(Date.now() - 300_000).toISOString()
    resourceHandler(() => makeDetail({ id: ID, status: 'pending', updated_at: old }))
    const visibility = createManualVisibility(true)
    const { result } = renderHook(() =>
      useResourceStatus(ID, { visibility, intervalMs: 2000, maxWaitMs: 120_000 }),
    )
    await flush()
    expect(result.current.inFlight).toBe(true)
    expect(result.current.exceededWindow).toBe(true)
  })

  it('stops when the view unmounts so route changes do not accumulate requests', async () => {
    vi.useFakeTimers()
    const calls = resourceHandler(() => makeDetail({ id: ID, status: 'pending' }))
    const visibility = createManualVisibility(true)
    const { unmount } = renderHook(() => useResourceStatus(ID, { visibility, intervalMs: 2000 }))
    await flush()
    unmount()
    const afterUnmount = calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    expect(calls).toHaveLength(afterUnmount)
  })
})
