import { useState } from 'react'

import { createResource } from '../api/resources'
import { toApiError } from '../api/http'
import type { CreatedResource, ImageWorkflow } from '../api/types'
import { IMAGE_WORKFLOWS } from '../api/types'
import { ErrorNote, InfoNote } from '../components/ErrorNote'
import { ImagePicker } from '../components/ImagePicker'
import { StatusBadge } from '../components/StatusBadge'
import { TagInput } from '../components/TagInput'
import { resourceRoute } from '../app/router'
import { bumpLibrary } from '../state/cache'
import {
  DOCUMENTED_MAX_TEXT_CHARS,
  DOCUMENTED_MAX_UPLOAD_BYTES,
  formatBytes,
  WORKFLOW_AUTO_LABEL,
  WORKFLOW_LABELS,
} from '../state/format'
import { useResourceStatus } from '../state/useResourceStatus'
import { BatchImportPanel } from './BatchImportPanel'

const WORKFLOW_CHOICES = IMAGE_WORKFLOWS

export function ImportPage() {
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [userText, setUserText] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [workflowChoice, setWorkflowChoice] = useState<ImageWorkflow | ''>('')
  const [submitting, setSubmitting] = useState(false)
  const [problem, setProblem] = useState('')
  const [failure, setFailure] = useState<unknown>(null)
  const [created, setCreated] = useState<CreatedResource | null>(null)
  const [batchMode, setBatchMode] = useState(false)

  const hasImage = file !== null
  const hasText = userText.trim().length > 0
  const route: 'text' | 'text_image' | 'image' = !hasImage ? 'text' : hasText ? 'text_image' : 'image'
  const workflowToSend: ImageWorkflow | null = route === 'image' ? (workflowChoice || null) : null
  const statusState = useResourceStatus(created?.id ?? null)

  const routeDescription =
    route === 'text'
      ? '固定路线 text（无图片时要求非空文字）'
      : route === 'text_image'
        ? '固定路线 text_image（图片 + 非空配文，不发送旧的仅图片路线）'
        : workflowToSend
          ? `仅图片，显式路线 ${workflowToSend}（${WORKFLOW_LABELS[workflowToSend]}）`
          : `仅图片，未选路线：省略 workflow 字段，由处理模块${WORKFLOW_AUTO_LABEL}`

  const submit = async () => {
    if (!hasImage && !hasText) {
      setProblem('没有图片时必须填写非空文字，否则后端会以 empty_input 拒绝。')
      return
    }
    setProblem('')
    setFailure(null)
    setCreated(null)
    setSubmitting(true)
    try {
      const resource = await createResource({
        file,
        title,
        userText: hasText ? userText : null,
        tags,
        workflow: workflowToSend,
      })
      setCreated(resource)
      bumpLibrary()
      // Keep tags for the next entry, clear the content so the same image is not imported twice.
      setFile(null)
      setTitle('')
      setUserText('')
    } catch (error) {
      // Draft and image preview stay untouched. A failed POST is never replayed automatically.
      setFailure(error)
    } finally {
      setSubmitting(false)
    }
  }

  const shown = statusState.detail ?? created
  const shownStatus = shown?.status ?? null

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>导入资料</h2>
          <p className="muted">{batchMode ? '按清单逐项导入，支持目录、续传和失败项重试。' : '一次创建一份资料；'}提交后由后台 Worker 推进状态。</p>
        </div>
      </header>

      <div className="actions" role="group" aria-label="导入方式">
        <button type="button" aria-pressed={!batchMode} onClick={() => setBatchMode(false)}>单份导入</button>
        <button type="button" aria-pressed={batchMode} onClick={() => setBatchMode(true)}>批量 / 目录导入</button>
      </div>
      {batchMode ? <BatchImportPanel /> : <>

      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <ImagePicker
          id="import"
          label="图片（可选）"
          file={file}
          onChange={setFile}
          hint={`默认上限 ${formatBytes(DOCUMENTED_MAX_UPLOAD_BYTES)}，接受 PNG/JPEG/WebP；` +
            '浏览器只做提前提示，后端按真实字节校验。'}
        />

        <label className="field">
          <span>标题（可选）</span>
          <input type="text" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="留空则卡片显示（无标题）" />
        </label>

        <label className="field">
          <span>用户文字 / 图片配文（无图片时必填）</span>
          <textarea
            rows={6}
            value={userText}
            onChange={(event) => setUserText(event.target.value)}
            placeholder="纯文本资料填写正文；图片资料填写配文即成为图文资料"
          />
          <span className="field-hint">
            默认上限 {DOCUMENTED_MAX_TEXT_CHARS.toLocaleString('zh-CN')} 字符（服务端 MAX_TEXT_CHARS 可调整），超限返回 413。
          </span>
        </label>

        <TagInput id="import-tags" label="标签（可自由输入）" value={tags} onChange={setTags} />

        <fieldset className="field fieldset" disabled={route !== 'image'}>
          <legend>仅图片时的处理路线</legend>
          <div className="radio-row">
            <label>
              <input
                type="radio"
                name="workflow"
                checked={workflowChoice === ''}
                onChange={() => setWorkflowChoice('')}
              />
              {WORKFLOW_AUTO_LABEL}
            </label>
            {WORKFLOW_CHOICES.map((choice) => (
              <label key={choice}>
                <input type="radio" name="workflow" checked={workflowChoice === choice} onChange={() => setWorkflowChoice(choice)} />
                {WORKFLOW_LABELS[choice]}
              </label>
            ))}
          </div>
          <p className="field-hint">{routeDescription}</p>
          {route !== 'image' ? <p className="field-hint">有文字时路线固定，选择框已禁用；不发送不兼容的 workflow。</p> : null}
        </fieldset>

        {problem ? (
          <p className="field-warning" role="alert">
            {problem}
          </p>
        ) : null}

        <p className="actions">
          <button className="button-primary" type="submit" disabled={submitting}>
            {submitting ? '提交中…' : '创建资料'}
          </button>
        </p>
      </form>

      {failure ? (
        <ErrorNote
          error={failure}
          title="导入失败"
          note={
            toApiError(failure).outcomeUnknown
              ? '网络中断导致创建结果未知：本页不会自动重发，请先回到资料库确认是否已经创建，避免重复导入。'
              : '表单内容与图片预览已保留，可按提示修正后重试。'
          }
        />
      ) : null}

      {created ? (
        <section className="panel result-panel" aria-label="导入结果">
          <h3>创建成功</h3>
          <p>
            资源 ID <code>{created.id}</code>
          </p>
          <p>
            服务端状态：<StatusBadge status={shownStatus ?? 'pending'} showHint />
          </p>
          {created.duplicate ? (
            <InfoNote tone="warn">该图片的 sha256 与已有资料相同；后端仍已单独保存为一份新资料。</InfoNote>
          ) : null}
          {statusState.error ? (
            <ErrorNote error={statusState.error} title="状态刷新失败" note="以下状态可能不是最新，可手动重试。" onRetry={statusState.refresh} />
          ) : null}
          {statusState.stale && !statusState.error ? <InfoNote>正在恢复状态轮询…</InfoNote> : null}
          {shown && (shown.status === 'pending' || shown.status === 'processing') ? (
            <p role="status">
              后台处理中（页面可见时每约 2 秒刷新）。{statusState.exceededWindow ? ' 仍在处理，可稍后查看；本页不会改写服务端状态。' : ''}
            </p>
          ) : null}
          {shown?.status === 'failed' ? <p className="card-error" role="status">处理失败：{shown.error ?? '见详情页错误详情'}</p> : null}
          {shown?.status === 'ready' ? <p role="status">已就绪，可被检索。</p> : null}
          <p className="actions">
            <a className="button button-primary" href={resourceRoute(created.id)}>
              打开详情
            </a>
          </p>
        </section>
      ) : null}
      </>}
    </section>
  )
}
