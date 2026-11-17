// Edit draft rules. The three states must stay distinguishable on the wire:
// omit (keep), string (manual value, including ""), explicit null (clear + regenerate).
import { normalizeTags } from '../api/resources'
import type { ImageWorkflow, ResourceDetail, ResourcePatchInput, Workflow } from '../api/types'
import { WORKFLOW_AUTO_LABEL, WORKFLOW_LABELS } from './format'

export type TextIntent =
  | { action: 'keep' }
  | { action: 'manual'; value: string }
  | { action: 'regenerate' }

export type WorkflowIntent =
  | { action: 'keep' }
  | { action: 'auto' }
  | { action: 'manual'; value: ImageWorkflow }

export interface EditDraft {
  title: string
  tags: string[]
  userText: string
  caption: TextIntent
  ocrText: TextIntent
  workflow: WorkflowIntent
}

export interface DraftDiff {
  /** Everything except revision, which the caller attaches from the edit baseline. */
  patch: Omit<ResourcePatchInput, 'revision'>
  labels: string[]
  /** Client-side checks for requests the backend will certainly reject; the backend stays authoritative. */
  problems: string[]
  notes: string[]
  hasChanges: boolean
}

export function initialDraft(resource: ResourceDetail): EditDraft {
  return {
    title: resource.title ?? '',
    tags: [...(resource.tags ?? [])],
    userText: resource.user_text ?? '',
    caption: { action: 'keep' },
    ocrText: { action: 'keep' },
    workflow: { action: 'keep' },
  }
}

export function hasImageOriginal(resource: ResourceDetail): boolean {
  return resource.original_url !== null && resource.original_url !== ''
}

/** Route preview after the draft is applied; mirrors backend input_route ordering. */
export function effectiveRoute(resource: ResourceDetail, draft: EditDraft): { workflow: ImageWorkflow | null; label: string } {
  const hasImage = hasImageOriginal(resource)
  const hasText = Boolean(draft.userText.trim())
  if (!hasImage) return { workflow: null, label: 'text' }
  if (hasText) return { workflow: null, label: 'text_image' }
  if (draft.workflow.action === 'auto') return { workflow: null, label: WORKFLOW_AUTO_LABEL }
  const candidate: Workflow | null = draft.workflow.action === 'manual' ? draft.workflow.value : resource.workflow
  if (!candidate || candidate === 'text' || candidate === 'text_image') return { workflow: null, label: WORKFLOW_AUTO_LABEL }
  return { workflow: candidate, label: WORKFLOW_LABELS[candidate] }
}

function sameTags(a: readonly string[], b: readonly string[]): boolean {
  const left = normalizeTags(a)
  const right = normalizeTags(b)
  return left.length === right.length && left.every((value, index) => value === right[index])
}

export function diffDraft(baseline: ResourceDetail, draft: EditDraft): DraftDiff {
  const patch: Omit<ResourcePatchInput, 'revision'> = {}
  const labels: string[] = []
  const problems: string[] = []
  const notes: string[] = []

  if (draft.title !== (baseline.title ?? '')) {
    patch.title = draft.title
    labels.push('标题')
  }

  if (!sameTags(baseline.tags ?? [], draft.tags)) {
    patch.tags = normalizeTags(draft.tags)
    labels.push('标签')
  }

  const baselineText = baseline.user_text ?? ''
  if (draft.userText !== baselineText) {
    patch.user_text = draft.userText
    labels.push(hasImageOriginal(baseline) ? '图片配文' : '正文文字')
    if (!hasImageOriginal(baseline) && !draft.userText.trim()) {
      problems.push('纯文本资料不能清空正文：后端会拒绝空内容，请保留至少一个非空白字符。')
    }
  }

  applyTextField(patch, labels, notes, baseline, 'caption', draft.caption, 'Caption（图像描述）', 'structured_image/visual_image')
  applyTextField(patch, labels, notes, baseline, 'ocr_text', draft.ocrText, 'OCR 文字', 'visdoc')

  const canChooseWorkflow = hasImageOriginal(baseline) && !draft.userText.trim()
  if (canChooseWorkflow && draft.workflow.action !== 'keep') {
    if (draft.workflow.action === 'auto') {
      if (baseline.workflow !== null) {
        patch.workflow = null
        labels.push('路线：重新自动分类')
      } else {
        notes.push('路线已经是自动识别；仍需其他改动才会重新排队。')
      }
    } else {
      const value = draft.workflow.value
      if (baseline.workflow !== value) {
        patch.workflow = value
        labels.push(`路线：${WORKFLOW_LABELS[value]}`)
      }
    }
  }

  return { patch, labels, problems, notes, hasChanges: Object.keys(patch).length > 0 }
}

function applyTextField(
  patch: Omit<ResourcePatchInput, 'revision'>,
  labels: string[],
  notes: string[],
  baseline: ResourceDetail,
  field: 'caption' | 'ocr_text',
  intent: TextIntent,
  label: string,
  usedBy: string,
): void {
  const current = baseline[field]
  if (intent.action === 'keep') return
  if (intent.action === 'regenerate') {
    patch[field] = null
    labels.push(`${label}：清空并请求重新生成`)
    notes.push(`清空后只有在 ${usedBy} 路线下才会自动生成；其他路线会保持为空。`)
    return
  }
  if (intent.value !== current) {
    patch[field] = intent.value
    labels.push(intent.value === '' ? `${label}：人工置空（保持为空）` : `${label}`)
  }
}

export function describeTextIntent(intent: TextIntent): string {
  if (intent.action === 'keep') return '保留现值'
  if (intent.action === 'regenerate') return '清空并重新生成'
  return intent.value ? '人工修正' : '人工置空'
}
