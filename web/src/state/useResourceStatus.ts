// Server-status polling: ~2s while the tab is visible, stops on ready/failed, pauses when hidden,
// backs off on failures while keeping the last successful detail on screen.
import { useCallback, useEffect, useState } from 'react'

import { isAbortError, toApiError, type ApiError } from '../api/http'
import { getResource } from '../api/resources'
import type { ResourceDetail } from '../api/types'
import { cacheDetail, useCachedDetail } from './cache'
import { browserVisibility, type VisibilitySource } from './visibility'
import { POLL_INTERVAL_MS, POLL_MAX_INTERVAL_MS, POLL_OBSERVE_WINDOW_MS } from './constants'

export { POLL_INTERVAL_MS, POLL_MAX_INTERVAL_MS, POLL_OBSERVE_WINDOW_MS }

export interface PollOptions {
  intervalMs?: number
  maxWaitMs?: number
  visibility?: VisibilitySource
  enabled?: boolean
}

export interface ResourceStatusState {
  detail: ResourceDetail | null
  error: ApiError | null
  isFetching: boolean
  /** The last refresh failed: the displayed detail may not match the server. */
  stale: boolean
  /** The server still reports pending/processing. */
  inFlight: boolean
  /** Still in flight after the observation window; the server state is unchanged by this. */
  exceededWindow: boolean
  refresh: () => void
}

export function isActiveStatus(status: ResourceDetail['status'] | undefined): boolean {
  return status === 'pending' || status === 'processing'
}

function beyondWindow(detail: ResourceDetail | null, maxWaitMs: number): boolean {
  if (!detail || !isActiveStatus(detail.status)) return false
  const since = Date.parse(detail.updated_at)
  return Number.isFinite(since) && Date.now() - since > maxWaitMs
}

export function useResourceStatus(resourceId: string | null, options: PollOptions = {}): ResourceStatusState {
  const {
    intervalMs = POLL_INTERVAL_MS,
    maxWaitMs = POLL_OBSERVE_WINDOW_MS,
    visibility = browserVisibility,
    enabled = true,
  } = options
  const detail = useCachedDetail(resourceId)
  const [state, setState] = useState<{
    error: ApiError | null
    stale: boolean
    isFetching: boolean
    exceededWindow: boolean
  }>({ error: null, stale: false, isFetching: false, exceededWindow: false })
  const [refreshToken, setRefreshToken] = useState(0)
  const refresh = useCallback(() => setRefreshToken((token) => token + 1), [])

  useEffect(() => {
    if (!resourceId || !enabled) {
      setState({ error: null, stale: false, isFetching: false, exceededWindow: false })
      return
    }
    const source = visibility
    const id = resourceId
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let delay = intervalMs
    let shouldPoll = true
    let generation = 0
    let active: AbortController | null = null

    const clearTimer = () => {
      if (timer !== undefined) clearTimeout(timer)
      timer = undefined
    }

    const cancelRequest = () => {
      generation += 1
      active?.abort()
      active = null
    }

    const schedule = () => {
      clearTimer()
      if (cancelled || !shouldPoll || !source.isVisible()) return
      timer = setTimeout(() => void load(), delay)
    }

    async function load() {
      if (cancelled || active || !source.isVisible()) return
      clearTimer()
      const controller = new AbortController()
      active = controller
      const requestGeneration = ++generation
      const isCurrent = () => !cancelled && requestGeneration === generation
      setState((previous) => ({ ...previous, isFetching: true }))
      try {
        const fresh = await getResource(id, controller.signal)
        // Cancellation alone is insufficient: transports may still finish JSON decoding.
        if (!isCurrent()) return
        const accepted = cacheDetail(fresh)
        delay = intervalMs
        shouldPoll = isActiveStatus(accepted.status)
        // The observation window is evaluated when the server timestamp is read, not during render.
        setState({ error: null, stale: false, isFetching: false, exceededWindow: beyondWindow(accepted, maxWaitMs) })
      } catch (error) {
        if (!isCurrent() || isAbortError(error)) return
        delay = Math.min(delay * 2, POLL_MAX_INTERVAL_MS)
        // Last successful detail stays visible; mark it as possibly outdated instead of guessing.
        setState((previous) => ({
          error: toApiError(error),
          stale: true,
          isFetching: previous.isFetching,
          exceededWindow: previous.exceededWindow,
        }))
      } finally {
        if (isCurrent()) {
          active = null
          setState((previous) => ({ ...previous, isFetching: false }))
          schedule()
        }
      }
    }

    void load()
    const unsubscribe = source.subscribe(() => {
      if (cancelled) return
      clearTimer()
      if (source.isVisible()) {
        void load()
      } else {
        cancelRequest()
        setState((previous) => ({ ...previous, isFetching: false }))
      }
    })
    return () => {
      cancelled = true
      clearTimer()
      cancelRequest()
      unsubscribe()
    }
  }, [resourceId, enabled, intervalMs, maxWaitMs, refreshToken, visibility])

  return {
    detail,
    error: state.error,
    isFetching: state.isFetching,
    stale: state.stale,
    inFlight: isActiveStatus(detail?.status),
    exceededWindow: state.exceededWindow,
    refresh,
  }
}
