import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'
import type { ImageWorkflow, ResourceStatus, Workflow } from '../api/types'
import type { ImportOptions } from '../api/imports'

export type UploadState = 'pending' | 'uploading' | 'unknown' | 'created' | 'skipped' | 'failed'
export interface BatchItem {
  relative_path: string
  key: string
  title: string
  tags: string[]
  workflow: ImageWorkflow | null
  duplicate_policy: 'skip' | 'create'
  status: UploadState
  sha256: string | null
  size_bytes: number
  modified_ms: number
  mime_type: string | null
  error: string | null
  resource_id: string | null
  resource_status: ResourceStatus | 'deleted' | null
  // Optional for manifests saved before classification results were included.
  resource_workflow?: Workflow | null
  resource_error?: string | null
  duplicate?: boolean
  outcome?: 'created' | 'skipped'
  receipt_state?: string
  attempts: { key: string; sha256: string | null; error: string | null }[]
}

export interface BatchOptions {
  tags: string[]
  title_mode: 'stem' | 'relative-path' | 'empty'
  workflow: ImageWorkflow | 'auto'
  duplicates: 'skip' | 'create'
}

export interface BatchManifest {
  schema_version: 1
  run_id: string
  local_revision: number
  api_url: string
  root: string
  recursive: boolean
  created_at: number
  import_options: ImportOptions
  options: BatchOptions
  items: BatchItem[]
  last_upload_seconds?: number
}

export function submissionKey(): string {
  // getRandomValues is available on plain HTTP too; randomUUID may require HTTPS.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 15) | 64
  bytes[8] = (bytes[8]! & 63) | 128
  const hex = bytesToHex(bytes)
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function buffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === 'function') return blob.arrayBuffer()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as ArrayBuffer)
    reader.onerror = () => reject(new Error('read_failed'))
    reader.readAsArrayBuffer(blob)
  })
}

export async function fileHash(file: File): Promise<string> {
  const digest = sha256.create()
  for (let offset = 0; offset < file.size; offset += 64 * 1024) {
    digest.update(new Uint8Array(await buffer(file.slice(offset, offset + 64 * 1024))))
  }
  return bytesToHex(digest.digest())
}

export function relativePath(file: File): string {
  const path = file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name
  if (!path || path.startsWith('/') || path.split('/').some((part) => !part || part === '.' || part === '..')) {
    throw new Error('文件相对路径无效')
  }
  return path
}

export async function inspectFile(file: File, limits: ImportOptions): Promise<{
  sha256: string; mime_type: string
}> {
  if (!file.size) throw new Error('empty_file')
  if (file.size > limits.max_upload_bytes) throw new Error('file_too_large')
  const header = new Uint8Array(await buffer(file.slice(0, 12)))
  const signature = [...header].map((value) => value.toString(16).padStart(2, '0')).join('')
  const mime = signature.startsWith('89504e470d0a1a0a') ? 'image/png'
    : signature.startsWith('ffd8ff') ? 'image/jpeg'
    : signature.startsWith('52494646') && signature.slice(16, 24) === '57454250' ? 'image/webp' : null
  if (!mime) throw new Error('unsupported_format')
  return { sha256: await fileHash(file), mime_type: mime }
}

export async function previewBatch(files: File[], limits: ImportOptions, options: BatchOptions,
  recursive: boolean, origin: string): Promise<{ manifest: BatchManifest; files: Map<string, File> }> {
  const entries = files.map((file) => ({ file, path: relativePath(file) }))
    .filter(({ path }) => recursive || !path.includes('/'))
    .sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
  const attached = new Map<string, File>()
  const items: BatchItem[] = []
  for (const { file, path } of entries) {
    if (attached.has(path)) throw new Error(`文件路径重复：${path}`)
    attached.set(path, file)
    const basename = path.split('/').at(-1)!
    const item: BatchItem = {
      relative_path: path, key: submissionKey(), title: options.title_mode === 'stem'
        ? basename.replace(/\.[^.]+$/, '') : options.title_mode === 'relative-path' ? path : '',
      tags: options.tags, workflow: options.workflow === 'auto' ? null : options.workflow,
      duplicate_policy: options.duplicates, status: 'pending', sha256: null,
      size_bytes: file.size, modified_ms: file.lastModified, mime_type: null,
      error: null, resource_id: null, resource_status: null, resource_workflow: null, attempts: [],
    }
    try {
      Object.assign(item, await inspectFile(file, limits))
      if ([...item.title].length > limits.max_text_chars) throw new Error('text_too_large')
    } catch (error) {
      item.error = error instanceof Error ? error.message : 'read_failed'
      item.status = item.error === 'unsupported_format' ? 'skipped' : 'failed'
    }
    items.push(item)
  }
  return { manifest: {
    schema_version: 1, run_id: submissionKey(), local_revision: 0, api_url: origin, root: '',
    recursive, created_at: Date.now() / 1000, import_options: limits, options, items,
  }, files: attached }
}

export function batchSummary(manifest: BatchManifest) {
  const states = { pending: 0, uploading: 0, unknown: 0, created: 0, skipped: 0, failed: 0 }
  const resources = new Map<string, string | null>()
  for (const item of manifest.items) {
    states[item.status]++
    if (item.resource_id) resources.set(item.resource_id, item.resource_status)
  }
  const processing: Record<string, number> = {}
  for (const status of resources.values()) processing[status ?? 'unknown'] = (processing[status ?? 'unknown'] ?? 0) + 1
  return { discovered: manifest.items.length, valid: manifest.items.filter((item) => item.sha256
    && !['invalid_image', 'unsupported_format', 'file_too_large', 'empty_file'].includes(item.error ?? '')).length,
    ...states, resources: processing }
}
