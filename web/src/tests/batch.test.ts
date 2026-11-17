import { beforeEach, describe, expect, it, vi } from 'vitest'
import { IDBFactory } from 'fake-indexeddb'

import { ApiError } from '../api/http'
import { resolveImports, uploadImport } from '../api/imports'
import type { ImportOptions, ImportReceipt, ImportedResource } from '../api/imports'
import { BatchController } from '../state/batchController'
import { batchSummary, fileHash, previewBatch } from '../state/batchManifest'
import type { BatchManifest, BatchOptions } from '../state/batchManifest'
import { listBatches, saveBatch } from '../state/batchStorage'
import { deferred, installFetch, jsonResponse, makeCreated } from './helpers'

const LIMITS: ImportOptions = { schema_version: 1, formats: ['PNG', 'JPEG', 'WEBP'], image_workflows: ['visual_image'], max_upload_bytes: 1024, max_text_chars: 100 }
const OPTIONS: BatchOptions = { tags: ['标签'], title_mode: 'stem', workflow: 'auto', duplicates: 'skip' }
function image(name = 'image.png', extra = '') {
  return new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), extra], name, { type: 'image/png' })
}
function receipt(key: string, fields: Partial<ImportReceipt> = {}): ImportReceipt {
  return { key, state: 'missing', outcome: null, resource_id: null, resource_status: null,
    resource_workflow: null,
    resource_error: null, duplicate: false, error: null, ...fields }
}
function created(fields: Partial<ImportedResource> = {}): ImportedResource {
  return { ...makeCreated({ status: 'ready' }), import_outcome: 'created', replayed: false, ...fields }
}
async function fixture(files = [image()]) {
  const result = await previewBatch(files, LIMITS, OPTIONS, true, window.location.origin)
  const persisted: BatchManifest[] = []
  const save = vi.fn(async (manifest: BatchManifest) => {
    const next = JSON.parse(JSON.stringify({ ...manifest, local_revision: manifest.local_revision + 1 })) as BatchManifest
    persisted.push(next)
    return next
  })
  const resolve = vi.fn(async (keys: string[]) => keys.map((key) => receipt(key)))
  const upload = vi.fn<typeof uploadImport>(async () => created())
  const controller = new BatchController({ save, resolve, upload, changed: vi.fn() })
  await controller.setManifest(result.manifest, result.files, true)
  return { ...result, controller, persisted, save, resolve, upload }
}

describe('file manifests', () => {
  it('hashes exact bytes on ordinary HTTP and handles recursive relative paths consistently', async () => {
    expect(await fileHash(new File(['abc'], 'text'))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    const first = image('z.jpg')
    const nested = image('nested.png')
    Object.defineProperty(nested, 'webkitRelativePath', { value: 'root/nested/nested.png' })
    const direct = image('a.png')
    Object.defineProperty(direct, 'webkitRelativePath', { value: 'root/a.png' })
    const shallow = await previewBatch([first, nested, direct], LIMITS, OPTIONS, false, window.location.origin)
    expect(shallow.manifest.items.map((item) => item.relative_path)).toEqual(['a.png', 'z.jpg'])
    expect(shallow.manifest.items[1]!.mime_type).toBe('image/png')
    const recursive = await previewBatch([first, nested, direct], LIMITS, OPTIONS, true, window.location.origin)
    expect(recursive.manifest.items.map((item) => item.relative_path)).toEqual(['a.png', 'nested/nested.png', 'z.jpg'])
  })

  it('reports unsupported, empty and oversized input individually without trusting MIME', async () => {
    const files = [new File(['plain text'], 'fake.png', { type: 'image/png' }), new File([], 'empty.png'),
      new File([new Uint8Array(1025)], 'large.png'), image('real.bin')]
    const { manifest } = await previewBatch(files, LIMITS, OPTIONS, false, window.location.origin)
    expect(manifest.items.map((item) => [item.relative_path, item.status])).toEqual([
      ['empty.png', 'failed'], ['fake.png', 'skipped'], ['large.png', 'failed'], ['real.bin', 'pending'],
    ])
  })
})

describe('durable queue', () => {
  it('saves the key and in-flight state before POST, then resolves a lost response without replay', async () => {
    const f = await fixture()
    let committed = false
    f.resolve.mockImplementation(async (keys) => keys.map((key) => receipt(key, committed
      ? { state: 'completed', outcome: 'created', resource_id: 'resource', resource_status: 'ready', resource_workflow: 'visdoc' } : {})))
    f.upload.mockImplementation(async () => {
      expect(f.persisted.at(-1)!.items[0]!.status).toBe('uploading')
      expect(f.persisted.at(-1)!.items[0]!.key).toBe(f.manifest.items[0]!.key)
      committed = true
      throw new ApiError(0, 'network_error', 'response lost')
    })
    await f.controller.run()
    expect(f.controller.getSnapshot().manifest!.items[0]!.status).toBe('created')
    expect(f.controller.getSnapshot().manifest!.items[0]!.resource_workflow).toBe('visdoc')
    await f.controller.run()
    expect(f.upload).toHaveBeenCalledTimes(1)
  })

  it('keeps the submission key when a missing receipt is retried after a network failure', async () => {
    const f = await fixture()
    const key = f.manifest.items[0]!.key
    f.upload.mockRejectedValueOnce(new ApiError(0, 'network_error', 'lost'))
    await f.controller.run()
    expect(f.controller.getSnapshot().manifest!.items[0]!.status).toBe('unknown')
    await f.controller.run()
    expect(f.upload.mock.calls.map((call) => call[0].key)).toEqual([key, key])
  })

  it('pauses scheduling while allowing the sent request to complete and persist', async () => {
    const f = await fixture([image('a.png'), image('b.png', 'different')])
    const pending = deferred<ImportedResource>()
    f.upload.mockReturnValueOnce(pending.promise)
    const running = f.controller.run(1)
    await vi.waitFor(() => expect(f.upload).toHaveBeenCalledTimes(1))
    f.controller.pause()
    pending.resolve(created())
    await running
    expect(f.upload).toHaveBeenCalledTimes(1)
    expect(f.controller.getSnapshot().manifest!.items.map((item) => item.status)).toEqual(['created', 'pending'])
  })

  it('stops before uploading if local persistence fails', async () => {
    const f = await fixture()
    f.save.mockRejectedValueOnce(new Error('quota exceeded'))
    await f.controller.run()
    expect(f.upload).not.toHaveBeenCalled()
    expect(f.controller.getSnapshot().paused).toBe(true)
    expect(f.controller.getSnapshot().error).toContain('quota exceeded')
  })

  it('requires reselected files, detects changed bytes and gives repaired failures a new key', async () => {
    const f = await fixture()
    const originalKey = f.manifest.items[0]!.key
    await f.controller.setManifest(f.controller.getSnapshot().manifest!)
    await f.controller.run()
    expect(f.upload).not.toHaveBeenCalled()
    expect(f.controller.getSnapshot().manifest!.items[0]!.error).toBe('file_missing')
    await f.controller.attach([image('image.png', 'fixed')])
    await f.controller.run(2, true)
    expect(f.upload).toHaveBeenCalledTimes(1)
    const item = f.controller.getSnapshot().manifest!.items[0]!
    expect(item.key).not.toBe(originalKey)
    expect(item.attempts[0]!.key).toBe(originalKey)
  })

  it('never changes a key for changed bytes while an earlier request is unresolved', async () => {
    const f = await fixture()
    const originalKey = f.manifest.items[0]!.key
    f.resolve.mockImplementation(async (keys) => keys.map((key) => receipt(key, { state: 'reserved' })))
    const manifest = f.controller.getSnapshot().manifest!
    manifest.items[0]!.status = 'failed'
    await f.controller.setManifest(manifest, new Map([['image.png', image('image.png', 'changed')]]))
    await f.controller.run(2, true)
    expect(f.upload).not.toHaveBeenCalled()
    expect(f.controller.getSnapshot().manifest!.items[0]!.key).toBe(originalKey)
    expect(f.controller.getSnapshot().manifest!.items[0]!.status).toBe('unknown')
  })

  it('counts model processing failures separately and deduplicates resource IDs', async () => {
    const f = await fixture()
    const item = f.manifest.items[0]!
    item.status = 'created'; item.resource_id = 'same'; item.resource_status = 'failed'
    f.manifest.items.push({ ...item, status: 'skipped' })
    const summary = batchSummary(f.manifest)
    expect(summary.failed).toBe(0)
    expect(summary.resources).toEqual({ failed: 1 })
  })

  it('ignores an older receipt response that arrives after a newer status snapshot', async () => {
    const f = await fixture()
    const old = deferred<ImportReceipt[]>()
    const key = f.manifest.items[0]!.key
    f.resolve.mockReturnValueOnce(old.promise).mockResolvedValueOnce([receipt(key, {
      state: 'completed', outcome: 'created', resource_id: 'same', resource_status: 'ready', resource_workflow: 'visdoc',
    })])
    const earlier = f.controller.refresh()
    await f.controller.refresh()
    old.resolve([receipt(key, { state: 'completed', outcome: 'created', resource_id: 'same', resource_status: 'pending' })])
    await earlier
    expect(f.controller.getSnapshot().manifest!.items[0]!.resource_status).toBe('ready')
    expect(f.controller.getSnapshot().manifest!.items[0]!.resource_workflow).toBe('visdoc')
  })
})

describe('IndexedDB and HTTP boundaries', () => {
  beforeEach(() => vi.stubGlobal('indexedDB', new IDBFactory()))

  it('persists metadata without file bytes and rejects stale writes from another tab', async () => {
    const { manifest } = await fixture()
    const saved = await saveBatch(manifest)
    expect(saved.local_revision).toBe(1)
    expect((await listBatches())[0]).toEqual(saved)
    await expect(saveBatch(manifest)).rejects.toThrow('另一页面')
    expect(JSON.stringify(saved)).not.toContain('data:image')
  })

  it('sends multipart idempotency fields and resolves in bounded read-only chunks', async () => {
    const f = await fixture()
    const calls = installFetch((call) => call.path.endsWith('resolve')
      ? jsonResponse(200, { items: (call.json!['keys'] as string[]).map((key) => receipt(key)) })
      : jsonResponse(201, created()))
    await uploadImport(f.manifest.items[0]!, image())
    expect(calls[0]!.headers['idempotency-key']).toBe(f.manifest.items[0]!.key)
    expect(calls[0]!.headers['content-type']).toBeUndefined()
    expect(calls[0]!.formData!.get('expected_sha256')).toBe(f.manifest.items[0]!.sha256)
    await resolveImports(Array.from({ length: 201 }, (_, index) => String(index)))
    expect(calls.slice(1).map((call) => (call.json!['keys'] as string[]).length)).toEqual([100, 100, 1])
  })
})
