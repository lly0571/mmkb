import { getJson, postForm, postJson } from './http'
import { createResourceForm } from './resources'
import type { CreatedResource, ImageWorkflow, ResourceStatus, Workflow } from './types'

export interface ImportOptions {
  schema_version: 1
  formats: string[]
  image_workflows: ImageWorkflow[]
  max_upload_bytes: number
  max_text_chars: number
}

export interface ImportReceipt {
  key: string
  state: 'missing' | 'reserved' | 'completed' | 'failed'
  outcome: 'created' | 'skipped' | null
  resource_id: string | null
  resource_status: ResourceStatus | 'deleted' | null
  resource_workflow: Workflow | null
  resource_error: string | null
  duplicate: boolean
  error: string | null
}

export interface ImportedResource extends CreatedResource {
  import_outcome: 'created' | 'skipped'
  replayed: boolean
}

export function importOptions(): Promise<ImportOptions> {
  return getJson<ImportOptions>('/api/imports/options')
}

/** This POST only reads receipts; retrying it never creates resources. */
export async function resolveImports(keys: string[], signal?: AbortSignal): Promise<ImportReceipt[]> {
  const items: ImportReceipt[] = []
  for (let index = 0; index < keys.length; index += 100) {
    const batch = keys.slice(index, index + 100)
    const result = await postJson<{ items: ImportReceipt[] }>('/api/imports/resolve', { keys: batch }, { signal })
    if (batch.some((key) => !result.items.some((item) => item.key === key))) {
      throw new Error('回执响应不完整')
    }
    items.push(...result.items)
  }
  return items
}

export function uploadImport(item: {
  key: string; title: string; tags: string[]; workflow: ImageWorkflow | null
  duplicate_policy: 'skip' | 'create'; sha256: string | null
}, file: File): Promise<ImportedResource> {
  const form = createResourceForm({ file, title: item.title, tags: item.tags, workflow: item.workflow })
  form.append('duplicate_policy', item.duplicate_policy)
  form.append('expected_sha256', item.sha256 ?? '')
  return postForm<ImportedResource>('/api/resources', form, { headers: { 'Idempotency-Key': item.key } })
}
