// Per-resource detail cache plus separate browse and search caches, so a lifecycle action can
// invalidate exactly the views it affects. Search and browse caches never share entries.
import { useSyncExternalStore } from 'react'

import type { ResourceDetail, SearchResponse } from '../api/types'
import { createStore } from './store'

export interface CacheSnapshot {
  details: Record<string, ResourceDetail>
  libraryToken: number
  search: { key: string; response: SearchResponse } | null
  searchToken: number
}

const cacheStore = createStore<CacheSnapshot>({
  details: {},
  libraryToken: 0,
  search: null,
  searchToken: 0,
})

function sameDetail(a: ResourceDetail | undefined, b: ResourceDetail): boolean {
  if (!a) return false
  return a.revision === b.revision && a.status === b.status && a.updated_at === b.updated_at && a.error === b.error
}

/** Return the accepted snapshot so callers never schedule from a rejected stale response. */
export function cacheDetail(detail: ResourceDetail): ResourceDetail {
  const { details } = cacheStore.get()
  const previous = details[detail.id]
  if (previous) {
    if (detail.revision < previous.revision) return previous
    if (detail.revision === previous.revision) {
      if (Date.parse(detail.updated_at) < Date.parse(previous.updated_at)) return previous
      const wasTerminal = previous.status === 'ready' || previous.status === 'failed'
      if (wasTerminal && (detail.status === 'pending' || detail.status === 'processing')) return previous
    }
    if (sameDetail(previous, detail) && JSON.stringify(previous) === JSON.stringify(detail)) return previous
  }
  cacheStore.set({ details: { ...details, [detail.id]: detail } })
  return detail
}

export function getCachedDetail(resourceId: string): ResourceDetail | null {
  return cacheStore.get().details[resourceId] ?? null
}

/** Edit/retry: the stored detail may be stale and old hits must not keep showing the indexed state. */
export function invalidateDetail(resourceId: string): void {
  const { details, search } = cacheStore.get()
  if (!(resourceId in details)) return
  const next = { ...details }
  delete next[resourceId]
  cacheStore.set({
    details: next,
    search: search ? { key: search.key, response: { ...search.response, items: withoutHit(search.response.items, resourceId) } } : null,
    searchToken: search ? cacheStore.get().searchToken + 1 : cacheStore.get().searchToken,
  })
}

function withoutHit<T extends { resource_id: string }>(items: T[], resourceId: string): T[] {
  return items.filter((item) => item.resource_id !== resourceId)
}

/** Delete: drop the detail, every cached hit and force browse lists to reload. */
export function dropResourceEverywhere(resourceId: string): void {
  const state = cacheStore.get()
  const details = { ...state.details }
  delete details[resourceId]
  cacheStore.set({
    details,
    search: state.search
      ? { key: state.search.key, response: { ...state.search.response, items: withoutHit(state.search.response.items, resourceId) } }
      : null,
    libraryToken: state.libraryToken + 1,
  })
}

export function bumpLibrary(): void {
  cacheStore.set((state) => ({ libraryToken: state.libraryToken + 1 }))
}

export function readSearchCache(key: string): SearchResponse | null {
  const entry = cacheStore.get().search
  return entry && entry.key === key ? entry.response : null
}

export function writeSearchCache(key: string, response: SearchResponse): void {
  cacheStore.set((state) => ({ search: { key, response }, searchToken: state.searchToken + 1 }))
}

export function clearSearchCache(): void {
  cacheStore.set((state) => ({ search: null, searchToken: state.searchToken + 1 }))
}

export function useCacheSnapshot(): CacheSnapshot {
  return useSyncExternalStore(cacheStore.subscribe, cacheStore.get, cacheStore.get)
}

export function useCachedDetail(resourceId: string | null): ResourceDetail | null {
  const snapshot = useCacheSnapshot()
  return resourceId ? (snapshot.details[resourceId] ?? null) : null
}

export function useLibraryToken(): number {
  return useCacheSnapshot().libraryToken
}

export function useSearchToken(): number {
  return useCacheSnapshot().searchToken
}

/** Test helper: reset module state between cases. */
export function resetCache(): void {
  cacheStore.set({ details: {}, libraryToken: 0, search: null, searchToken: 0 })
}

export { invalidateDetail as invalidateDetailCache }
