// Request helper with cancellation and stale-response suppression: a later query must never be
// overwritten by an earlier response that arrives afterwards.
import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError, toApiError, type ApiError } from '../api/http'

export type RequestStatus = 'idle' | 'loading' | 'success' | 'error'

export interface RequestResult<T> {
  data: T | null
  error: ApiError | null
  status: RequestStatus
  /** A refetch is running while an older payload is still displayed. */
  isFetching: boolean
  reload: () => void
}

export interface UseRequestOptions {
  enabled?: boolean
  /** Keep the previous payload visible while refetching (used by list refresh). */
  keepData?: boolean
}

/** Stable string key for a request; changing it aborts the previous call and starts a new one. */
export function queryKey(...parts: unknown[]): string {
  return parts
    .map((part) => {
      if (typeof part === 'string') return part
      if (part === null || part === undefined) return '-'
      try {
        return JSON.stringify(part) ?? String(part)
      } catch {
        return String(part)
      }
    })
    .join('|')
}

interface InternalState<T> {
  status: RequestStatus
  error: ApiError | null
  data: T | null
  isFetching: boolean
}

export function useRequest<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: UseRequestOptions = {},
): RequestResult<T> {
  const { enabled = true, keepData = true } = options
  const fetcherRef = useRef(fetcher)
  const callId = useRef(0)
  const [reloadToken, setReloadToken] = useState(0)
  const [state, setState] = useState<InternalState<T>>({
    status: enabled ? 'loading' : 'idle',
    error: null,
    data: null,
    isFetching: enabled,
  })

  // Declared before the request effect so the effect always sees this render's closure.
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  const reload = useCallback(() => setReloadToken((token) => token + 1), [])

  useEffect(() => {
    const current = ++callId.current
    if (!enabled) {
      setState({ status: 'idle', error: null, data: null, isFetching: false })
      return
    }
    setState((previous) => ({
      status: previous.status === 'success' && keepData ? 'success' : 'loading',
      error: null,
      data: keepData ? previous.data : null,
      isFetching: true,
    }))
    const controller = new AbortController()
    const run = async () => {
      try {
        const data = await fetcherRef.current(controller.signal)
        // A superseded call keeps its payload out of the view even if the fetch was not aborted.
        if (current !== callId.current) return
        setState({ status: 'success', error: null, data, isFetching: false })
      } catch (error) {
        if (current !== callId.current || isAbortError(error)) return
        setState((previous) => ({
          status: 'error',
          error: toApiError(error),
          data: keepData ? previous.data : null,
          isFetching: false,
        }))
      }
    }
    void run()
    return () => controller.abort()
  }, [enabled, keepData, key, reloadToken])

  return {
    data: state.data,
    error: state.status === 'error' ? state.error : null,
    status: state.status,
    isFetching: state.isFetching,
    reload,
  }
}
