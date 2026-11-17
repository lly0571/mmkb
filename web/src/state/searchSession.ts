// Search inputs and results survive navigation inside the tab so that returning from a detail page
// shows the previous hits (which lifecycle actions can invalidate) without silently re-running a
// model-backed search. The picked image, if any, stays in memory only.
import { queryKey } from './useRequest'

export interface SearchSessionState {
  text: string
  tags: string[]
  topK: number
  file: File | null
  /** Bumped by an explicit submit or “重新搜索”; part of the request key. */
  token: number
}

const empty: SearchSessionState = { text: '', tags: [], topK: 20, file: null, token: 0 }

let session: SearchSessionState = { ...empty }

export function readSearchSession(): SearchSessionState {
  return session
}

export function writeSearchSession(next: Partial<SearchSessionState>): SearchSessionState {
  session = { ...session, ...next }
  return session
}

export function resetSearchSession(): void {
  session = { ...empty }
}

export function searchSessionKey(sessionState: SearchSessionState): string {
  return queryKey(
    'search',
    sessionState.text.trim(),
    sessionState.tags,
    sessionState.topK,
    sessionState.file ? `${sessionState.file.name}:${sessionState.file.size}` : '',
    sessionState.token,
  )
}
