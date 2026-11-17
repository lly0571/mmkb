import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { importOptions } from '../api/imports'
import type { ImportOptions } from '../api/imports'
import { IMAGE_WORKFLOWS, type ImageWorkflow } from '../api/types'
import { resourceRoute } from '../app/router'
import { TagInput } from '../components/TagInput'
import { batchController } from '../state/batchController'
import { batchSummary, previewBatch } from '../state/batchManifest'
import type { BatchItem, BatchManifest, BatchOptions, UploadState } from '../state/batchManifest'
import { listBatches } from '../state/batchStorage'
import { formatBytes, WORKFLOW_LABELS } from '../state/format'

const LABELS: Record<UploadState, string> = {
  pending: '待上传', uploading: '请求已发出', unknown: '提交结果未知', created: '已导入', skipped: '已跳过', failed: '导入失败',
}
const ERRORS: Record<string, string> = {
  unsupported_format: '非受支持图片', empty_file: '空文件', file_too_large: '文件超限',
  invalid_image: '图片损坏', read_failed: '读取失败', file_missing: '请重选该文件', source_changed: '文件内容已变化，请仅重试失败项',
  previous_request_unresolved: '先前请求尚未确定结果，保留原标识核查', import_busy: '服务端正在导入相同内容',
}

function classificationLabel(item: BatchItem): string {
  if (item.resource_status === 'deleted') return '资料已删除'
  if (item.resource_workflow) return WORKFLOW_LABELS[item.resource_workflow]
  if (item.resource_id) {
    if (item.resource_status === 'processing') return '分类中…'
    if (item.resource_status === 'pending') return '等待分类'
    return item.resource_status === 'failed' ? '暂无分类结果' : '等待更新'
  }
  if (item.status === 'skipped') return '不适用'
  if (item.status === 'failed') return '未分类'
  if (item.workflow) return `已指定：${WORKFLOW_LABELS[item.workflow]}`
  return '待分类'
}

export function BatchImportPanel() {
  const snapshot = useSyncExternalStore(batchController.subscribe, batchController.getSnapshot)
  const { manifest, busy, paused } = snapshot
  const [limits, setLimits] = useState<ImportOptions | null>(null)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [selected, setSelected] = useState<File[]>([])
  const [recursive, setRecursive] = useState(false)
  const [tags, setTags] = useState<string[]>([])
  const [titleMode, setTitleMode] = useState<BatchOptions['title_mode']>('stem')
  const [workflow, setWorkflow] = useState<ImageWorkflow | 'auto'>('auto')
  const [duplicates, setDuplicates] = useState<'skip' | 'create'>('skip')
  const [concurrency, setConcurrency] = useState(2)
  const [preparing, setPreparing] = useState(false)
  const [history, setHistory] = useState<BatchManifest[]>([])
  const [historyId, setHistoryId] = useState('')
  const [page, setPage] = useState(0)
  const filesInput = useRef<HTMLInputElement>(null)
  const directoryInput = useRef<HTMLInputElement>(null)
  const attachInput = useRef<HTMLInputElement>(null)
  const attachDirectory = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    void importOptions().then((value) => { if (active) setLimits(value) }).catch(() => {
      if (active) setError('无法读取服务端导入限制，请检查连接后重新打开批量入口')
    })
    void listBatches().then((values) => {
      if (active) { setHistory(values); setHistoryId(values[0]?.run_id ?? '') }
    }).catch(() => { if (active) setError('无法读取本地清单，检查浏览器存储权限') })
    return () => { active = false; batchController.pause() }
  }, [])

  const runId = manifest?.run_id
  const observe = manifest?.items.some((item) => ['unknown', 'uploading'].includes(item.status)
    || ['pending', 'processing'].includes(item.resource_status ?? '')) ?? false
  const hasLinkedItems = manifest?.items.some((item) => item.resource_id) ?? false
  useEffect(() => {
    // Read terminal receipts once too, so restored older manifests get classification data.
    if (!runId || (!observe && !hasLinkedItems)) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let controller: AbortController | null = null
    let delay = 5000
    const clear = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined }
    const poll = async () => {
      if (disposed || document.hidden || controller) return
      clear()
      controller = new AbortController()
      const signal = controller.signal
      try {
        await batchController.refresh(signal)
        if (!disposed && !signal.aborted) { delay = 5000; setPollError('') }
      } catch {
        if (!disposed && !signal.aborted) { delay = Math.min(delay * 2, 30000); setPollError('状态刷新失败，显示上次结果，稍后自动重试') }
      } finally {
        controller = null
        if (!disposed && !document.hidden && observe) timer = setTimeout(() => void poll(), delay)
      }
    }
    const visibility = () => {
      clear()
      if (document.hidden) controller?.abort()
      else void poll()
    }
    void poll()
    document.addEventListener('visibilitychange', visibility)
    return () => { disposed = true; clear(); controller?.abort(); document.removeEventListener('visibilitychange', visibility) }
  }, [runId, observe, hasLinkedItems])

  const preview = async () => {
    if (!limits || !selected.length) return
    setPreparing(true); setError('')
    try {
      const result = await previewBatch(selected, limits, {
        tags, title_mode: titleMode, workflow, duplicates,
      }, recursive, window.location.origin)
      await batchController.setManifest(result.manifest, result.files, true)
      setPage(0)
      setHistory(await listBatches())
      setHistoryId(result.manifest.run_id)
    } catch (problem) { setError(problem instanceof Error ? problem.message : '预览失败') }
    finally { setPreparing(false) }
  }

  const attach = async (files: File[]) => {
    setPreparing(true); setError('')
    try { await batchController.attach(files) }
    catch (problem) { setError(problem instanceof Error ? problem.message : '文件核验失败') }
    finally { setPreparing(false) }
  }

  const exportManifest = () => {
    if (!manifest) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = `mmkb-import-${manifest.run_id}.json`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const count = manifest ? batchSummary(manifest) : null
  const disabled = busy || preparing
  return <section className="panel" aria-label="目录批量导入">
    <h3>目录批量导入</h3>
    <p className="muted">浏览器上传所选文件。默认只取目录的直接文件；后端按真实内容校验，模型处理在入库后继续。</p>
    <div className="actions">
      <button type="button" disabled={disabled} onClick={() => filesInput.current?.click()}>多选文件</button>
      <button type="button" disabled={disabled} onClick={() => directoryInput.current?.click()}>选择目录</button>
    </div>
    <input aria-label="批量图片文件" type="file" multiple hidden ref={filesInput}
      onChange={(event) => { setSelected(Array.from(event.target.files ?? [])); event.target.value = '' }} />
    <input aria-label="批量本地目录" type="file" multiple hidden ref={directoryInput} {...{ webkitdirectory: '' }}
      onChange={(event) => { setSelected(Array.from(event.target.files ?? [])); event.target.value = '' }} />
    <p>已选择 {selected.length} 个文件。目录选择不可用时可使用多选。</p>
    <fieldset disabled={disabled} className="field fieldset">
      <legend>新清单设置</legend>
      <label><input type="checkbox" checked={recursive} onChange={(event) => setRecursive(event.target.checked)} />包含子目录</label>
      <TagInput id="batch-tags" label="统一标签" value={tags} onChange={setTags} />
      <label className="field">标题规则<select value={titleMode} onChange={(event) => setTitleMode(event.target.value as BatchOptions['title_mode'])}>
        <option value="stem">文件名去扩展名</option><option value="relative-path">相对路径</option><option value="empty">留空</option>
      </select></label>
      <label className="field">图片路线<select value={workflow} onChange={(event) => setWorkflow(event.target.value as ImageWorkflow | 'auto')}>
        <option value="auto">自动选择</option>
        {(limits?.image_workflows ?? IMAGE_WORKFLOWS).map((value) => <option value={value} key={value}>{WORKFLOW_LABELS[value]}</option>)}
      </select></label>
      <label className="field">重复图片<select value={duplicates} onChange={(event) => setDuplicates(event.target.value as 'skip' | 'create')}>
        <option value="skip">跳过已有内容，不合并标签或路线</option><option value="create">新建独立资料</option>
      </select></label>
    </fieldset>
    <button type="button" disabled={disabled || !limits || !selected.length} onClick={() => void preview()}>
      {preparing ? '核验文件中…' : '生成预览清单'}
    </button>
    {history.length > 0 && <div className="actions">
      <label>已保存清单 <select aria-label="已保存清单" value={historyId} disabled={disabled} onChange={(event) => setHistoryId(event.target.value)}>
        {history.map((value) => <option key={value.run_id} value={value.run_id}>{new Date(value.created_at * 1000).toLocaleString()} · {value.items.length} 项</option>)}
      </select></label>
      <button type="button" disabled={disabled} onClick={() => {
        const saved = history.find((value) => value.run_id === historyId)
        if (saved) void listBatches().then((fresh) => {
          const latest = fresh.find((value) => value.run_id === saved.run_id)
          if (latest) return batchController.setManifest(latest)
        }).catch((problem: unknown) => setError(problem instanceof Error ? problem.message : '恢复失败'))
      }}>恢复清单</button>
    </div>}
    {(error || snapshot.error) && <p role="alert" className="field-warning">{error || snapshot.error}</p>}
    {manifest && count && <>
      <p role="status">发现 {count.discovered} · 有效 {count.valid} · 跳过 {count.skipped} · 导入成功 {count.created} · 导入失败 {count.failed} · 待上传 {count.pending} · 结果未知 {count.unknown + count.uploading}</p>
      <p>资源处理（按资源 ID 去重）：{Object.entries(count.resources).map(([state, number]) => `${state} ${number}`).join(' · ') || '暂无已关联资源'}</p>
      <p className="field-hint">清单已保存在浏览器，只含文件指纹与结果。刷新后先恢复清单，再重选原文件或目录；已成功项不会重传。</p>
      <div className="actions">
        <button type="button" disabled={disabled} onClick={() => attachInput.current?.click()}>重选文件以续传</button>
        <button type="button" disabled={disabled} onClick={() => attachDirectory.current?.click()}>重选目录以续传</button>
        <label>上传并发 <input aria-label="上传并发" type="number" min={1} max={8} value={concurrency} disabled={disabled}
          onChange={(event) => setConcurrency(Math.max(1, Math.min(8, Number(event.target.value) || 1)))} /></label>
        <button type="button" disabled={disabled} onClick={() => void batchController.run(concurrency)}>开始 / 继续上传</button>
        <button type="button" disabled={!busy || paused} onClick={batchController.pause}>暂停新上传</button>
        <button type="button" disabled={disabled || !count.failed} onClick={() => void batchController.run(concurrency, true)}>仅重试失败项</button>
        <button type="button" onClick={exportManifest}>导出清单与结果</button>
        <button type="button" onClick={() => void batchController.refresh().catch(() => setPollError('状态刷新失败，请稍后重试'))}>刷新状态</button>
      </div>
      <input aria-label="续传文件" type="file" multiple hidden ref={attachInput}
        onChange={(event) => { void attach(Array.from(event.target.files ?? [])); event.target.value = '' }} />
      <input aria-label="续传目录" type="file" multiple hidden ref={attachDirectory} {...{ webkitdirectory: '' }}
        onChange={(event) => { void attach(Array.from(event.target.files ?? [])); event.target.value = '' }} />
      {busy && <p role="status">{paused ? '已暂停领取新项，正在核查已发出的请求…' : '正在逐项上传…'}</p>}
      {pollError && <p role="alert">{pollError}</p>}
      <div className="batch-table-wrap"><table className="batch-table">
        <thead><tr><th>相对路径</th><th>大小</th><th>上传结果</th><th>分类结果</th><th>处理状态</th><th>资源 / 原因</th></tr></thead>
        <tbody>{manifest.items.slice(page * 100, (page + 1) * 100).map((item) => <tr key={item.relative_path}>
          <td>{item.relative_path}</td><td>{formatBytes(item.size_bytes)}</td><td>{LABELS[item.status]}</td>
          <td>{classificationLabel(item)}</td>
          <td>{item.resource_status ?? '—'}</td><td>{item.resource_id && <a href={resourceRoute(item.resource_id)}>打开资料</a>}
            {item.error && <span>{ERRORS[item.error] ?? '导入未完成'} <code>{item.error}</code></span>}
            {item.resource_error && <span>{item.resource_error}</span>}</td>
        </tr>)}</tbody>
      </table></div>
      {manifest.items.length > 100 && <div className="actions">
        <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
        <span>第 {page + 1} 页</span>
        <button type="button" disabled={(page + 1) * 100 >= manifest.items.length} onClick={() => setPage(page + 1)}>下一页</button>
      </div>}
    </>}
  </section>
}
