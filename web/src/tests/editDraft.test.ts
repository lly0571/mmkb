import { describe, expect, it } from 'vitest'

import { diffDraft, effectiveRoute, initialDraft, type EditDraft } from '../state/editDraft'
import { makeDetail } from './helpers'

function draftWith(resource: ReturnType<typeof makeDetail>, overrides: Partial<EditDraft> = {}): EditDraft {
  return { ...initialDraft(resource), ...overrides }
}

describe('edit draft diffing', () => {
  it('sends nothing when the draft equals the baseline', () => {
    const resource = makeDetail({ caption: '原描述', ocr_text: null })
    const diff = diffDraft(resource, draftWith(resource))
    expect(diff.hasChanges).toBe(false)
    expect(diff.patch).toEqual({})
    expect(diff.labels).toEqual([])
  })

  it('omits caption and ocr_text when the user keeps the stored value', () => {
    const resource = makeDetail({ caption: '原描述', ocr_text: '原 OCR' })
    const diff = diffDraft(resource, draftWith(resource, { title: '新标题' }))
    expect(diff.patch).toEqual({ title: '新标题' })
    expect('caption' in diff.patch).toBe(false)
    expect('ocr_text' in diff.patch).toBe(false)
  })

  it('sends an empty string as a manual value, which is not the same as null', () => {
    const resource = makeDetail({ caption: null })
    const diff = diffDraft(resource, draftWith(resource, { caption: { action: 'manual', value: '' } }))
    expect(diff.patch.caption).toBe('')
    expect(diff.patch.caption === null).toBe(false)
    expect(diff.labels.join()).toContain('人工置空')
  })

  it('treats a manual value identical to the stored one as no change', () => {
    const resource = makeDetail({ caption: '同一描述' })
    const diff = diffDraft(resource, draftWith(resource, { caption: { action: 'manual', value: '同一描述' } }))
    expect(diff.hasChanges).toBe(false)
  })

  it('sends explicit null only for the regenerate action', () => {
    const resource = makeDetail({ caption: '原描述', ocr_text: '原 OCR' })
    const diff = diffDraft(resource, draftWith(resource, { caption: { action: 'regenerate' }, ocrText: { action: 'regenerate' } }))
    expect(diff.patch.caption).toBeNull()
    expect(diff.patch.ocr_text).toBeNull()
    expect(diff.notes.join()).toContain('只有在 structured_image/info_rich_image 路线下才会自动生成')
  })

  it('keeps manual edits to an already empty field and to a filled field both visible', () => {
    const resource = makeDetail({ caption: '' })
    const fill = diffDraft(resource, draftWith(resource, { caption: { action: 'manual', value: '补上的描述' } }))
    expect(fill.patch.caption).toBe('补上的描述')
    const keepEmpty = diffDraft(resource, draftWith(resource, { caption: { action: 'manual', value: '' } }))
    expect(keepEmpty.hasChanges).toBe(false)
  })

  it('submits workflow null for reclassification and a concrete workflow when chosen', () => {
    const resource = makeDetail({ input_kind: 'image', workflow: 'visual_image', user_text: null })
    const auto = diffDraft(resource, draftWith(resource, { workflow: { action: 'auto' } }))
    expect(auto.patch.workflow).toBeNull()
    const chosen = diffDraft(resource, draftWith(resource, { workflow: { action: 'manual', value: 'structured_image' } }))
    expect(chosen.patch.workflow).toBe('structured_image')
    const unchanged = diffDraft(resource, draftWith(resource, { workflow: { action: 'manual', value: 'visual_image' } }))
    expect(unchanged.hasChanges).toBe(false)
  })

  it('never sends a workflow for captioned images or plain text', () => {
    const withCaption = makeDetail({ input_kind: 'text_image', workflow: 'text_image' })
    const diffCaption = diffDraft(withCaption, draftWith(withCaption, { workflow: { action: 'auto' } }))
    expect(diffCaption.hasChanges).toBe(false)

    const text = makeDetail({ input_kind: 'text', workflow: 'text', original_url: null })
    const diffText = diffDraft(text, draftWith(text, { workflow: { action: 'manual', value: 'visdoc' } }))
    expect(diffText.hasChanges).toBe(false)
  })

  it('blocks clearing the only text of a plain-text resource before the backend 422', () => {
    const text = makeDetail({ input_kind: 'text', workflow: 'text', original_url: null, user_text: '正文' })
    const diff = diffDraft(text, draftWith(text, { userText: '   ' }))
    expect(diff.patch.user_text).toBe('   ')
    expect(diff.problems.join()).toContain('不能清空正文')
  })

  it('reports a route switch when the caption is removed from an image resource', () => {
    const resource = makeDetail({ input_kind: 'text_image', workflow: 'text_image', user_text: '配文' })
    const diff = diffDraft(resource, draftWith(resource, { userText: '' }))
    expect(diff.patch.user_text).toBe('')
    expect(effectiveRoute(resource, draftWith(resource, { userText: '' })).label).toBe('自动识别（等待处理模块判断）')
    expect(diff.problems).toEqual([])
  })

  it('normalises tags and ignores reorder-insensitive duplicates', () => {
    const resource = makeDetail({ tags: ['A', 'B'] })
    const same = diffDraft(resource, draftWith(resource, { tags: ['A', 'B', 'A', '  '] }))
    expect(same.hasChanges).toBe(false)
    const changed = diffDraft(resource, draftWith(resource, { tags: [' A '] }))
    expect(changed.patch.tags).toEqual(['A'])
  })
})
