import { ApiError } from '../api/http'
import { resolveImports, uploadImport } from '../api/imports'
import type { ImportReceipt } from '../api/imports'
import { bumpLibrary } from './cache'
import { inspectFile, relativePath, submissionKey } from './batchManifest'
import type { BatchItem, BatchManifest } from './batchManifest'
import { saveBatch } from './batchStorage'

interface Snapshot { manifest: BatchManifest | null; busy: boolean; paused: boolean; error: string; attached: number }
interface Dependencies {
  save: typeof saveBatch
  resolve: typeof resolveImports
  upload: typeof uploadImport
  changed: () => void
}

function applyReceipt(item: BatchItem, receipt: ImportReceipt) {
  item.receipt_state = receipt.state
  if (receipt.state === 'completed') {
    item.status = receipt.outcome!
    item.outcome = receipt.outcome!
    item.resource_id = receipt.resource_id
    item.resource_status = receipt.resource_status
    item.resource_workflow = receipt.resource_workflow ?? null
    item.resource_error = receipt.resource_error
    item.duplicate = receipt.duplicate
    item.error = null
  } else if (receipt.state === 'failed') {
    item.status = 'failed'
    item.error = receipt.error ?? 'import_failed'
  }
}

export class BatchController {
  private snapshot: Snapshot = { manifest: null, busy: false, paused: true, error: '', attached: 0 }
  private listeners = new Set<() => void>()
  private files = new Map<string, File>()
  private writes: Promise<void> = Promise.resolve()
  private refreshGeneration = 0
  private dependencies: Dependencies

  constructor(dependencies: Partial<Dependencies> = {}) {
    this.dependencies = { save: saveBatch, resolve: resolveImports, upload: uploadImport, changed: bumpLibrary, ...dependencies }
  }
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private emit(values: Partial<Snapshot>) {
    this.snapshot = { ...this.snapshot, ...values }
    this.listeners.forEach((listener) => listener())
  }
  pause = () => this.emit({ paused: true })

  private update(change: (manifest: BatchManifest) => void): Promise<void> {
    const next = this.writes.then(async () => {
      if (!this.snapshot.manifest) return
      const manifest = { ...this.snapshot.manifest,
        items: this.snapshot.manifest.items.map((item) => ({ ...item, attempts: [...item.attempts] })) }
      change(manifest)
      const saved = await this.dependencies.save(manifest)
      this.emit({ manifest: saved })
    })
    this.writes = next.catch((error: unknown) => {
      this.emit({ paused: true, error: error instanceof Error ? error.message : '清单保存失败' })
    })
    return next
  }

  async setManifest(manifest: BatchManifest, files = new Map<string, File>(), persist = false) {
    if (this.snapshot.busy) throw new Error('请先暂停并等待已发请求结束')
    await this.writes
    if (manifest.api_url !== window.location.origin) throw new Error('清单属于其他 API 入口')
    const saved = persist ? await this.dependencies.save(manifest) : manifest
    this.refreshGeneration++
    this.files = files
    this.emit({ manifest: saved, paused: true, error: '', attached: files.size })
  }

  async attach(files: File[]) {
    if (this.snapshot.busy) throw new Error('请先等待已发请求结束')
    const attached = new Map<string, File>()
    for (const file of files) {
      const path = relativePath(file)
      if (attached.has(path)) throw new Error(`文件路径重复：${path}`)
      attached.set(path, file)
    }
    this.files = attached
    // Verify reselected bytes now; run() repeats verification before sending and the server
    // verifies expected_sha256 again, covering edits made while a request is uploading.
    const checks = new Map<string, string | null>()
    for (const item of this.snapshot.manifest?.items ?? []) {
      if (item.resource_id || item.status === 'skipped') continue
      const file = attached.get(item.relative_path)
      try {
        if (!file) throw new Error('file_missing')
        const fresh = await inspectFile(file, this.snapshot.manifest!.import_options)
        checks.set(item.key, item.sha256 && fresh.sha256 !== item.sha256 ? 'source_changed' : null)
      } catch (error) { checks.set(item.key, error instanceof Error ? error.message : 'read_failed') }
    }
    await this.update((manifest) => {
      for (const item of manifest.items) {
        const error = checks.get(item.key)
        if (error && !item.resource_id) {
          item.error = error
          if (item.status !== 'unknown' && item.status !== 'uploading') item.status = 'failed'
        }
      }
    })
    this.emit({ attached: attached.size, error: '' })
  }

  async refresh(signal?: AbortSignal, keys?: string[]) {
    const manifest = this.snapshot.manifest
    if (!manifest) return
    const generation = ++this.refreshGeneration
    const selected = keys ?? manifest.items.filter((item) => item.sha256).map((item) => item.key)
    const receipts = await this.dependencies.resolve(selected, signal)
    if (signal?.aborted || generation !== this.refreshGeneration || this.snapshot.manifest?.run_id !== manifest.run_id) return
    await this.update((current) => {
      if (generation !== this.refreshGeneration || signal?.aborted) return
      const byKey = new Map(receipts.map((receipt) => [receipt.key, receipt]))
      for (const item of current.items) {
        const receipt = byKey.get(item.key)
        if (receipt) applyReceipt(item, receipt)
      }
    })
  }

  async run(concurrency = 2, retryFailed = false) {
    if (this.snapshot.busy || !this.snapshot.manifest) return
    this.emit({ busy: true, paused: false, error: '' })
    const started = Date.now()
    try {
      const selected = this.snapshot.manifest.items.filter((item) => retryFailed ? item.status === 'failed'
        : ['pending', 'unknown', 'uploading'].includes(item.status))
      await this.refresh(undefined, selected.map((item) => item.key))
      const paths = new Set(selected.map((item) => item.relative_path))
      const queue = this.snapshot.manifest!.items.filter((item) => paths.has(item.relative_path)
        && !['created', 'skipped'].includes(item.status)).map((item) => item.relative_path)
      const active = new Map<string, Promise<void>>()
      const activeHashes = new Set<string | null>()
      while (queue.length || active.size) {
        for (const path of [...queue]) {
          if (this.snapshot.paused || active.size >= Math.max(1, Math.min(8, concurrency))) break
          const item = this.snapshot.manifest!.items.find((value) => value.relative_path === path)!
          if (activeHashes.has(item.sha256)) continue
          activeHashes.add(item.sha256)
          const hash = item.sha256
          const task = this.send(path, retryFailed).catch((error: unknown) => {
            this.emit({ paused: true, error: error instanceof Error ? error.message : '批量上传已暂停' })
          }).finally(() => { active.delete(path); activeHashes.delete(hash) })
          active.set(path, task)
          queue.splice(queue.indexOf(path), 1)
        }
        if (!active.size) break
        await Promise.race([...active.values()])
      }
      const unknown = this.snapshot.manifest!.items.filter((item) => ['unknown', 'uploading'].includes(item.status))
      if (unknown.length) await this.refresh(undefined, unknown.map((item) => item.key))
      await this.update((manifest) => { manifest.last_upload_seconds = (Date.now() - started) / 1000 })
    } catch (error) {
      this.emit({ error: error instanceof Error ? error.message : '回执查询失败，请稍后继续' })
    } finally { this.emit({ busy: false, paused: true }) }
  }

  private async send(path: string, retryFailed: boolean) {
    let item = this.snapshot.manifest!.items.find((value) => value.relative_path === path)!
    const file = this.files.get(path)
    try {
      if (!file) throw new Error('file_missing')
      const fresh = await inspectFile(file, this.snapshot.manifest!.import_options)
      const changed = fresh.sha256 !== item.sha256
      if (changed && !retryFailed) throw new Error('source_changed')
      if (changed && item.receipt_state === 'reserved') throw new Error('previous_request_unresolved')
      await this.update((manifest) => {
        const current = manifest.items.find((value) => value.relative_path === path)!
        if (changed) {
          current.attempts.push({ key: current.key, sha256: current.sha256, error: current.error })
          current.key = submissionKey()
        }
        Object.assign(current, fresh, { status: 'uploading', error: null,
          size_bytes: file.size, modified_ms: file.lastModified })
      })
    } catch (error) {
      const code = error instanceof Error ? error.message : 'read_failed'
      await this.update((manifest) => {
        const current = manifest.items.find((value) => value.relative_path === path)!
        const uncertain = ['unknown', 'uploading'].includes(current.status) || current.receipt_state === 'reserved'
        Object.assign(current, { status: uncertain ? 'unknown' : 'failed', error: code })
      })
      return
    }
    item = this.snapshot.manifest!.items.find((value) => value.relative_path === path)!
    try {
      const result = await this.dependencies.upload(item, file!)
      if (!result.id || !['created', 'skipped'].includes(result.import_outcome)) throw new Error('响应无法确认提交结果')
      this.refreshGeneration++
      await this.update((manifest) => {
        const current = manifest.items.find((value) => value.key === item.key)!
        Object.assign(current, { status: result.import_outcome, outcome: result.import_outcome,
          resource_id: result.id, resource_status: result.status, resource_workflow: result.workflow,
          duplicate: result.duplicate, error: null })
      })
      this.dependencies.changed()
    } catch (error) {
      const known = error instanceof ApiError && error.status >= 400 && error.status < 500
        && !['import_busy', 'parse_error'].includes(error.code)
      await this.update((manifest) => {
        const current = manifest.items.find((value) => value.key === item.key)!
        current.status = known ? 'failed' : 'unknown'
        current.error = error instanceof ApiError ? error.code : 'request_outcome_unknown'
      })
    }
  }
}

// Keep a single queue across navigation; page unmount pauses scheduling, while sent requests
// finish and save their receipts. Reloading the browser restores metadata from IndexedDB.
export const batchController = new BatchController()
