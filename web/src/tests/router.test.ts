import { describe, expect, it } from 'vitest'

import { parseHash, RESOURCE_ID_PATTERN } from '../app/router'

describe('hash routing', () => {
  it('treats an empty hash and #/ as the library so refresh works on a static host', () => {
    expect(parseHash('')).toEqual({ name: 'library' })
    expect(parseHash('#')).toEqual({ name: 'library' })
    expect(parseHash('#/')).toEqual({ name: 'library' })
    expect(parseHash('#/library')).toEqual({ name: 'library' })
  })

  it('parses the four entry points', () => {
    expect(parseHash('#/import')).toEqual({ name: 'import' })
    expect(parseHash('#/connections')).toEqual({ name: 'connections' })
    expect(parseHash('#/resources/6f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b')).toEqual({
      name: 'resource',
      resourceId: '6f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b',
    })
  })

  it('decodes encoded ids and ignores query suffixes', () => {
    expect(parseHash('#/resources/a%2Fb?x=1')).toEqual({ name: 'resource', resourceId: 'a/b' })
  })

  it('reports unknown routes instead of silently rendering the library', () => {
    expect(parseHash('#/settings')).toEqual({ name: 'unknown', path: '/settings' })
  })

  it('only treats UUIDs as resource ids', () => {
    expect(RESOURCE_ID_PATTERN.test('6f1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b')).toBe(true)
    expect(RESOURCE_ID_PATTERN.test('not-a-uuid')).toBe(false)
  })
})
