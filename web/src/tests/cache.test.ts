import { describe, expect, it } from 'vitest'
import { cacheDetail, getCachedDetail } from '../state/cache'
import { makeDetail } from './helpers'

describe('detail cache version protection', () => {
  it('rejects lower revisions and older timestamps within the same revision', () => {
    const latest = makeDetail({ revision: 3, status: 'processing', updated_at: '2026-11-17T02:00:02Z' })
    cacheDetail(latest)
    expect(cacheDetail(makeDetail({ revision: 2 }))).toBe(latest)
    expect(cacheDetail({ ...latest, title: 'older', updated_at: '2026-11-17T02:00:01Z' })).toBe(latest)
    expect(getCachedDetail(latest.id)).toBe(latest)
  })

  it('keeps terminal states within a revision, but accepts a retry with a higher revision', () => {
    const terminal = makeDetail({ revision: 3, status: 'failed' })
    cacheDetail(terminal)
    expect(cacheDetail({ ...terminal, status: 'pending', updated_at: '2026-11-18T00:00:00Z' })).toBe(terminal)
    const retry = { ...terminal, revision: 4, status: 'pending' as const }
    expect(cacheDetail(retry)).toBe(retry)
  })
})
