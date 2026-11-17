// Hash routing keeps refresh working behind a plain static server without SPA fallback rules.
import { useMemo, useSyncExternalStore } from 'react'

export type Route =
  | { name: 'library' }
  | { name: 'import' }
  | { name: 'resource'; resourceId: string }
  | { name: 'connections' }
  | { name: 'unknown'; path: string }

export const LIBRARY_ROUTE = '#/library'
export const IMPORT_ROUTE = '#/import'
export const CONNECTIONS_ROUTE = '#/connections'

export function resourceRoute(resourceId: string): string {
  return `#/resources/${encodeURIComponent(resourceId)}`
}

export const RESOURCE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function parseHash(hash: string): Route {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const path = raw.split('?')[0] ?? ''
  const segments = path.split('/').filter(Boolean)
  if (segments.length === 0 || segments[0] === 'library') return { name: 'library' }
  if (segments[0] === 'import') return { name: 'import' }
  if (segments[0] === 'connections') return { name: 'connections' }
  if (segments[0] === 'resources' && segments[1]) {
    let resourceId = segments[1]
    try {
      resourceId = decodeURIComponent(segments[1])
    } catch {
      resourceId = segments[1]
    }
    return { name: 'resource', resourceId }
  }
  return { name: 'unknown', path }
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function currentHash(): string {
  return window.location.hash
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, currentHash, () => '')
  return useMemo(() => parseHash(hash), [hash])
}

export function navigate(route: string): void {
  const target = route.startsWith('#') ? route : `#${route}`
  if (window.location.hash === target) return
  window.location.hash = target
}
