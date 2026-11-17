import { useState } from 'react'

import { deleteResource, patchResource, retryResource } from '../api/resources'
import { toApiError, type ApiError } from '../api/http'
import type { ResourceDetail } from '../api/types'
import { IMAGE_WORKFLOWS, readWarnings } from '../api/types'
import { LIBRARY_ROUTE, RESOURCE_ID_PATTERN, navigate, resourceRoute } from '../app/router'
import { ErrorNote, InfoNote } from '../components/ErrorNote'
import { OriginalPane } from '../components/OriginalPane'
import { StatusBadge } from '../components/StatusBadge'
import { TagInput } from '../components/TagInput'
import { TriField } from '../components/TriField'
import { WarningList } from '../components/WarningList'
import { cacheDetail, dropResourceEverywhere, invalidateDetail } from '../state/cache'
import {
  formatBytes,
  formatDateTime,
  KIND_LABELS,
  WORKFLOW_AUTO_LABEL,
  WORKFLOW_LABELS,
} from '../state/format'
import { diffDraft, effectiveRoute, hasImageOriginal, initialDraft, type EditDraft } from '../state/editDraft'
import { useResourceStatus } from '../state/useResourceStatus'

export function ResourceDetailPage({ resourceId }: { resourceId: string }) {
  const isValidId = RESOURCE_ID_PATTERN.test(resourceId)
  const statusState = useResourceStatus(isValidId ? resourceId : null)
  const detail = statusState.detail

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<EditDraft | null>(null)
  const [baseline, setBaseline] = useState<ResourceDetail | null>(null)
  const [conflict, setConflict] = useState<ApiError | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  if (!isValidId) {
    return (
      <section className="page">
        <h2>资料详情</h2>
        <p className="empty">地址中的资源 ID 不是有效 UUID，未发起请求。</p>
      </section>
    )
  }

  if (statusState.error && !detail) {
    return (
      <section className="page">
        <h2>资料详情</h2>
        <ErrorNote error={statusState.error} title="详情读取失败" onRetry={statusState.refresh} />
      </section>
    )
  }

  if (!detail) {
    return (
      <section className="page">
        <h2>资料详情</h2>
        <p className="muted">正在读取详情…</p>
      </section>
    )
  }

  const warnings = readWarnings(detail.extra)
  const remoteChanged = editing && baseline !== null && detail.revision !== baseline.revision
  const diff = editing && baseline && draft ? diffDraft(baseline, draft) : null

  const startEdit = () => {
    setBaseline(detail)
    setDraft(initialDraft(detail))
    setConflict(null)
    setActionError(null)
    setNotice('')
    setEditing(true)
  }

  const cancelEdit = () => {
    setEditing(false)
    setDraft(null)
    setBaseline(null)
    setConflict(null)
  }

  /** Remote moved while editing: re-base the comparison, keep the local draft, never auto-resend. */
  const rebaseDraft = () => {
    setBaseline(detail)
    setConflict(null)
  }

  const save = async () => {
    if (!baseline || !draft) return
    const changes = diffDraft(baseline, draft)
    if (changes.problems.length > 0 || !changes.hasChanges) return
    setBusy(true)
    setActionError(null)
    try {
      const updated = await patchResource(resourceId, { revision: baseline.revision, ...changes.patch })
      // Old hits are dropped first so the edited resource cannot keep showing an indexed state.
      invalidateDetail(baseline.id)
      cacheDetail(updated)
      statusState.refresh()
      setNotice(
        `已提交：${changes.labels.join('、')}。revision ${baseline.revision} → ${updated.revision}，` +
          '旧索引已删除并重新排队（处理中不会进入检索结果）。',
      )
      cancelEdit()
    } catch (error) {
      const apiError = toApiError(error)
      if (apiError.isConflict) {
        // Read the newest state, keep the draft untouched, and let the user decide how to apply it.
        setConflict(apiError)
        statusState.refresh()
      } else {
        setActionError(error)
      }
    } finally {
      setBusy(false)
    }
  }

  const retry = async () => {
    setBusy(true)
    setActionError(null)
    try {
      const updated = await retryResource(resourceId, detail.revision)
      invalidateDetail(resourceId)
      cacheDetail(updated)
      statusState.refresh()
      setNotice(`已重新排队：revision ${detail.revision} → ${updated.revision}。`)
    } catch (error) {
      const apiError = toApiError(error)
      setActionError(error)
      if (apiError.isConflict) statusState.refresh()
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    setActionError(null)
    try {
      // 204 has no body; nothing is parsed from the response.
      await deleteResource(resourceId)
      dropResourceEverywhere(resourceId)
      navigate(LIBRARY_ROUTE)
    } catch (error) {
      setActionError(error)
      setBusy(false)
      setConfirmDelete(false)
    } finally {
      setBusy(false)
    }
  }

  const routeLabel = detail.workflow ? WORKFLOW_LABELS[detail.workflow] : WORKFLOW_AUTO_LABEL

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>{detail.title ? detail.title : '（无标题）'}</h2>
          <p className="card-meta">
            <span>{KIND_LABELS[detail.input_kind]}</span>
            <span>路线 {routeLabel}</span>
            <StatusBadge status={detail.status} showHint />
          </p>
        </div>
        <div className="head-actions">
          <a className="button" href={LIBRARY_ROUTE}>
            返回列表
          </a>
        </div>
      </header>

      {statusState.error && detail ? (
        <ErrorNote
          error={statusState.error}
          title="状态刷新失败"
          note="下方为最后一次成功读取的详情，可能不是最新状态。"
          onRetry={statusState.refresh}
        />
      ) : null}
      {statusState.stale ? <InfoNote>轮询暂时失败，已保留最后成功数据；状态可能滞后。</InfoNote> : null}
      {notice ? <InfoNote>{notice}</InfoNote> : null}
      {actionError ? <ErrorNote error={actionError} title="操作失败" onRetry={statusState.refresh} retryLabel="重新读取" /> : null}

      {detail.status === 'failed' && detail.error ? (
        <div className="note note-error" role="alert">
          <div>
            <strong>处理失败</strong>
            <p>{detail.error}</p>
            <p className="note-extra">失败资料未进入检索索引；可重新排队处理。</p>
          </div>
          <div className="note-side">
            <button type="button" disabled={busy} onClick={() => void retry()}>
              重试（使用 revision {detail.revision}）
            </button>
          </div>
        </div>
      ) : null}

      <div className="detail-grid">
        <OriginalPane resourceId={resourceId} originalUrl={detail.original_url} />

        <section className="pane pane-content" aria-label="资料内容">
          <div className="pane-head">
            <h2>资料内容</h2>
            <div className="pane-actions">
              {editing ? null : (
                <button type="button" onClick={startEdit}>
                  编辑
                </button>
              )}
              {confirmDelete ? null : (
                <button type="button" className="button-danger" onClick={() => setConfirmDelete(true)} disabled={busy}>
                  删除
                </button>
              )}
            </div>
          </div>

          {confirmDelete ? (
            <div className="note note-warn" role="alertdialog" aria-label="确认删除">
              <div>
                <strong>确认删除《{detail.title ? detail.title : '（无标题）'}》？</strong>
                <p>
                  删除后详情页与原件立即不可访问，检索结果也会移除；当前 UI 不提供恢复。物理对象清理由后端负责，
                  失败时由维护命令重试。
                </p>
              </div>
              <div className="note-side">
                <button type="button" onClick={() => setConfirmDelete(false)} disabled={busy}>
                  取消
                </button>
                <button type="button" className="button-danger" onClick={() => void remove()} disabled={busy}>
                  {busy ? '处理中…' : '确认删除'}
                </button>
              </div>
            </div>
          ) : null}

          {editing && draft && baseline && diff ? (
            <EditForm
              baseline={baseline}
              draft={draft}
              diff={diff}
              busy={busy}
              remoteChanged={remoteChanged}
              currentRevision={detail.revision}
              conflict={conflict}
              onChange={setDraft}
              onSave={() => void save()}
              onCancel={cancelEdit}
              onRebase={rebaseDraft}
              onReload={statusState.refresh}
            />
          ) : (
            <ReadOnly resource={detail} />
          )}

          <WarningList warnings={warnings} />

          <dl className="meta-grid">
            <div>
              <dt>资源 ID</dt>
              <dd>
                <code>{detail.id}</code>{' '}
                <a href={resourceRoute(detail.id)}>本页面地址</a>
              </dd>
            </div>
            <div>
              <dt>输入形式 / 路线</dt>
              <dd>
                {KIND_LABELS[detail.input_kind]} / {routeLabel}
              </dd>
            </div>
            <div>
              <dt>revision</dt>
              <dd>{detail.revision}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{formatDateTime(detail.updated_at)}</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{formatDateTime(detail.created_at)}</dd>
            </div>
            <div>
              <dt>sha256</dt>
              <dd>{detail.sha256 ? <code>{detail.sha256}</code> : '—'}</dd>
            </div>
            <div>
              <dt>原件</dt>
              <dd>
                {detail.original_url ? (
                  <a href={detail.original_url} target="_blank" rel="noreferrer">
                    通过后端代理打开
                  </a>
                ) : (
                  '无（纯文本资料）'
                )}
              </dd>
            </div>
            {detail.extra && Object.keys(detail.extra).length > 0 ? (
              <div>
                <dt>图片元数据</dt>
                <dd>
                  <MetadataSummary extra={detail.extra} />
                </dd>
              </div>
            ) : null}
          </dl>
        </section>
      </div>
    </section>
  )
}

function MetadataSummary({ extra }: { extra: Record<string, unknown> }) {
  const entries = Object.entries(extra).filter(([key]) => key !== 'processing')
  if (entries.length === 0) return <span className="muted">仅包含处理元数据</span>
  return (
    <span>
      {entries.map(([key, value]) => {
        const text = typeof value === 'number' && key.includes('bytes') ? formatBytes(value) : String(value)
        return (
          <span key={key} className="meta-pair">
            {key}={text}
          </span>
        )
      })}
    </span>
  )
}

function ReadOnly({ resource }: { resource: ResourceDetail }) {
  return (
    <>
      <Field label="标签" empty={resource.tags.length === 0 ? '未设置标签' : undefined}>
        <ul className="chips chips-plain" aria-label="标签">
          {resource.tags.map((tag) => (
            <li key={tag}>
              <span>{tag}</span>
            </li>
          ))}
        </ul>
      </Field>
      <Field label="用户文字" empty={resource.user_text ? undefined : '无'}>
        <pre className="prose">{resource.user_text ?? ''}</pre>
      </Field>
      <Field label="Caption（图像描述）" empty={resource.caption === null ? 'null（未生成）' : resource.caption === '' ? '空字符串（人工置空）' : undefined}>
        <pre className="prose">{resource.caption ?? ''}</pre>
      </Field>
      <Field label="OCR 文字" empty={resource.ocr_text === null ? 'null（未生成）' : resource.ocr_text === '' ? '空字符串（人工置空）' : undefined}>
        <pre className="prose">{resource.ocr_text ?? ''}</pre>
      </Field>
    </>
  )
}

function Field({ label, empty, children }: { label: string; empty?: string; children: React.ReactNode }) {
  return (
    <div className="content-field">
      <h3>{label}</h3>
      {empty ? <p className="muted">{empty}</p> : children}
    </div>
  )
}

interface EditFormProps {
  baseline: ResourceDetail
  draft: EditDraft
  diff: ReturnType<typeof diffDraft>
  busy: boolean
  remoteChanged: boolean
  currentRevision: number
  conflict: ApiError | null
  onChange: (draft: EditDraft) => void
  onSave: () => void
  onCancel: () => void
  onRebase: () => void
  onReload: () => void
}

function EditForm({
  baseline,
  draft,
  diff,
  busy,
  remoteChanged,
  currentRevision,
  conflict,
  onChange,
  onSave,
  onCancel,
  onRebase,
  onReload,
}: EditFormProps) {
  const imageOnly = hasImageOriginal(baseline) && !draft.userText.trim()
  const route = effectiveRoute(baseline, draft)
  return (
    <form
      className="edit-form"
      onSubmit={(event) => {
        event.preventDefault()
        onSave()
      }}
    >
      <p className="field-hint">
        只提交变更字段与进入编辑时的 revision（当前 {baseline.revision}）。任何有效编辑都会增加 revision、
        删除旧索引并重新排队；没有修改时不会发送请求。
      </p>

      {remoteChanged ? (
        <div className="note note-warn" role="alert">
          <div>
            <strong>远端已变化</strong>
            <p>
              编辑基准 revision {baseline.revision}，服务端已是 {currentRevision}。本地草稿与编辑基准均未被覆盖。
            </p>
          </div>
          <div className="note-side">
            <button type="button" onClick={onReload}>
              重新读取
            </button>
            <button type="button" onClick={onRebase}>
              以最新详情为基准比较草稿
            </button>
          </div>
        </div>
      ) : null}

      {conflict ? (
        <ErrorNote
          error={conflict}
          title="版本冲突（未提交）"
          note="草稿已保留，不会自动以新 revision 重发。读取最新详情后，由你决定是否重新应用。"
        />
      ) : null}

      <label className="field">
        <span>标题</span>
        <input type="text" value={draft.title} onChange={(event) => onChange({ ...draft, title: event.target.value })} />
      </label>

      <TagInput id="edit-tags" label="标签" value={draft.tags} onChange={(tags) => onChange({ ...draft, tags })} />

      <label className="field">
        <span>{hasImageOriginal(baseline) ? '图片配文（清空即成为仅图片资料）' : '正文文字（纯文本不可清空）'}</span>
        <textarea
          rows={5}
          value={draft.userText}
          onChange={(event) => onChange({ ...draft, userText: event.target.value })}
        />
      </label>

      <fieldset className="field fieldset" disabled={!imageOnly}>
        <legend>处理路线</legend>
        <div className="radio-row">
          <label>
            <input type="radio" name="edit-workflow" checked={draft.workflow.action === 'keep'} onChange={() => onChange({ ...draft, workflow: { action: 'keep' } })} />
            保持当前（{baseline.workflow ?? '自动识别'}）
          </label>
          <label>
            <input type="radio" name="edit-workflow" checked={draft.workflow.action === 'auto'} onChange={() => onChange({ ...draft, workflow: { action: 'auto' } })} />
            重新自动分类（提交 workflow: null）
          </label>
          {IMAGE_WORKFLOWS.map((choice) => (
            <label key={choice}>
              <input
                type="radio"
                name="edit-workflow"
                checked={draft.workflow.action === 'manual' && draft.workflow.value === choice}
                onChange={() => onChange({ ...draft, workflow: { action: 'manual', value: choice } })}
              />
              {WORKFLOW_LABELS[choice]}
            </label>
          ))}
        </div>
        {!imageOnly ? <p className="field-hint">有配文或纯文本资料不能指定图片路线：后端会在 image / text_image 间自动更新路线。</p> : null}
      </fieldset>

      <TriField
        id="edit-caption"
        legend="Caption（图像描述）"
        baseline={baseline.caption}
        intent={draft.caption}
        onChange={(caption) => onChange({ ...draft, caption })}
      />
      <TriField
        id="edit-ocr"
        legend="OCR 文字"
        autoGenerate={false}
        baseline={baseline.ocr_text}
        intent={draft.ocrText}
        onChange={(ocrText) => onChange({ ...draft, ocrText })}
      />

      <div className="change-summary" aria-live="polite">
        <h3>将提交的改动</h3>
        {diff.hasChanges ? (
          <ul>
            {diff.labels.map((label) => (
              <li key={label}>{label}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">没有改动：不会发送 PATCH（后端任何有效编辑都会增加 revision 并重新排队）。</p>
        )}
        <p className="muted">应用后路线预览：{route.label}</p>
        {diff.notes.map((note) => (
          <p className="field-hint" key={note}>
            {note}
          </p>
        ))}
        {diff.problems.map((problem) => (
          <p className="field-warning" key={problem} role="alert">
            {problem}
          </p>
        ))}
      </div>

      <p className="actions">
        <button className="button-primary" type="submit" disabled={busy || !diff.hasChanges || diff.problems.length > 0}>
          {busy ? '提交中…' : '提交编辑'}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          放弃草稿
        </button>
      </p>
    </form>
  )
}
