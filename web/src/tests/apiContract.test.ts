import { describe, expect, it } from 'vitest'

import { createResourceForm, listResourcePath, normalizeTags, patchResource, retryResource } from '../api/resources'
import { searchForm } from '../api/search'
import { installFetch, jsonResponse, makeDetail, pngFile } from './helpers'

describe('resource and search request contracts', () => {
  it('trims, drops blanks and de-duplicates tags like the backend', () => {
    expect(normalizeTags([' 金融 ', '金融', '', '  ', '图表'])).toEqual(['金融', '图表'])
  })

  it('omits workflow for image-only auto classification instead of sending "null"', () => {
    const auto = createResourceForm({ file: pngFile(), workflow: null })
    expect(auto.has('workflow')).toBe(false)
    expect([...auto.getAll('workflow')]).toEqual([])

    const explicit = createResourceForm({ file: pngFile(), workflow: 'visdoc' })
    expect(explicit.get('workflow')).toBe('visdoc')
  })

  it('sends repeated tags, the file only when present and skips blank user_text', () => {
    const form = createResourceForm({ file: pngFile('a.png'), tags: ['金融', '图表'], userText: '   ', title: ' x ' })
    expect([...form.getAll('tags')]).toEqual(['金融', '图表'])
    expect(form.has('user_text')).toBe(false)
    expect(form.get('title')).toBe('x')
    const file = form.get('file')
    expect(file).toBeInstanceOf(File)
    expect((file as File).name).toBe('a.png')

    const textOnly = createResourceForm({ userText: '文字', title: '笔记' })
    expect(textOnly.has('file')).toBe(false)
    expect(textOnly.get('user_text')).toBe('文字')
  })

  it('builds browse query strings with repeated tags and no empty filters', () => {
    const path = listResourcePath({ limit: 20, offset: 40, status: 'failed', input_kind: '', tags: ['A', 'B'] })
    expect(path).toContain('/api/resources?')
    const params = new URL(path, 'http://localhost:5173').searchParams
    expect(params.get('limit')).toBe('20')
    expect(params.get('offset')).toBe('40')
    expect(params.get('status')).toBe('failed')
    expect(params.has('input_kind')).toBe(false)
    expect(params.getAll('tags')).toEqual(['A', 'B'])
  })

  it('keeps search params separate from browse filters and clamps Top-K', () => {
    const form = searchForm({ text: ' 金融图表 ', file: pngFile(), tags: ['A'], topK: 250 })
    expect(form.get('text')).toBe('金融图表')
    expect(form.get('top_k')).toBe('100')
    expect(form.has('status')).toBe(false)
    expect(form.has('input_kind')).toBe(false)
    expect(searchForm({ text: 'x', topK: 0 }).get('top_k')).toBe('1')
    expect(searchForm({ text: 'x' }).get('top_k')).toBe('20')
  })

  it('sends a PATCH with only the changed fields plus the baseline revision', async () => {
    const calls = installFetch(() => jsonResponse(200, makeDetail({ revision: 4 })))
    await patchResource('id-1', { revision: 3, caption: '' })
    expect(calls[0].method).toBe('PATCH')
    expect(calls[0].json).toEqual({ revision: 3, caption: '' })
  })

  it('keeps explicit null distinct from an omitted field on the wire', async () => {
    const calls = installFetch(() => jsonResponse(200, makeDetail()))
    await patchResource('id-1', { revision: 7, ocr_text: null, workflow: null })
    const body = calls[0].json as Record<string, unknown>
    expect(body).toEqual({ revision: 7, ocr_text: null, workflow: null })
    expect('caption' in body).toBe(false)
    expect('title' in body).toBe(false)
  })

  it('posts retry with the revision only', async () => {
    const calls = installFetch(() => jsonResponse(200, makeDetail()))
    await retryResource('id-1', 9)
    expect(calls[0].path).toBe('/api/resources/id-1/retry')
    expect(calls[0].json).toEqual({ revision: 9 })
  })
})
