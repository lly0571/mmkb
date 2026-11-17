import { useCallback, useMemo, useState } from 'react'

import {
  HEALTH_CHECK_LABELS,
  HEALTH_CLIENT_TIMEOUT_MS,
  HEALTH_SCOPE_LABELS,
  HEALTH_STATE_LABELS,
  HEALTH_SUMMARY_LABELS,
  fetchHealthReport,
  healthAdviceText,
  healthBoundaryText,
} from '../api/health'
import { HEALTH_CHECK_KEYS } from '../api/types'
import type { HealthCheck, HealthCheckKey, HealthReport } from '../api/types'
import { explainError, toApiError } from '../api/http'
import { formatDateTime } from '../state/format'
import { useRequest } from '../state/useRequest'
import { InfoNote } from '../components/ErrorNote'

/**
 * Read-only dependency view backed by `GET /api/health`. The server reports every completed round
 * with HTTP 200, so a red item here is a real dependency failure and not a transport error. Nothing
 * is polled, and Worker liveness is never inferred from the API answering.
 */
export function ConnectionsPage() {
  const [round, setRound] = useState(0)
  const fetcher = useMemo(() => (signal: AbortSignal) => fetchHealthReport(signal), [])
  const request = useRequest(`health:${round}`, fetcher, { keepData: true })
  const report = request.data as HealthReport | null
  const stale = report !== null && request.error !== null
  const checking = request.status === 'loading' || request.isFetching

  const refresh = useCallback(() => setRound((value) => value + 1), [])

  const announcement = checking
    ? '正在重新检查连接状态'
    : request.error
      ? '本次检查失败，页面保留的是历史结果'
      : report
        ? `检查完成：${HEALTH_SUMMARY_LABELS[report.status]}${report.cached ? '（使用最近检查结果）' : ''}`
        : '正在检查连接状态'

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>连接状态</h2>
          <p className="muted">
            只读检查服务端依赖：不写入、不迁移、不触发资料处理，也不发送推理请求。
          </p>
        </div>
        <div className="health-actions">
          <button type="button" onClick={refresh} disabled={checking}>
            {checking ? '检查中…' : '重新检查'}
          </button>
        </div>
      </header>

      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      {request.error ? (
        <div className="note note-error" role="alert">
          <div>
            <strong>无法获取最新连接状态</strong>
            <p>{explainError(request.error)}</p>
            {request.error.code === 'health_timeout' ? (
              <p className="note-extra">
                浏览器等待上限 {HEALTH_CLIENT_TIMEOUT_MS / 1000} 秒：后端可能仍在等待慢依赖，可稍后重试。
              </p>
            ) : null}
          </div>
          <div className="note-side">
            <code className="note-code">{toApiError(request.error).code}</code>
            {toApiError(request.error).status > 0 ? (
              <code className="note-code">HTTP {toApiError(request.error).status}</code>
            ) : null}
          </div>
        </div>
      ) : null}

      {report ? (
        <>
          <div className={`health-summary health-${report.status}`} data-status={report.status}>
            <span className={`badge health-badge-${report.status}`}>{HEALTH_SUMMARY_LABELS[report.status]}</span>
            <p className="health-meta">
              检查时间 {formatDateTime(report.checked_at)} · 本轮耗时 {report.duration_ms} ms ·{' '}
              {report.cached
                ? `使用最近检查结果（${Math.round(report.age_ms / 1000)} 秒前，有效期 ${Math.round(
                    report.ttl_ms / 1000,
                  )} 秒）`
                : '本轮已重新探测依赖'}
            </p>
            {stale ? <span className="badge health-badge-unknown">历史结果，未获最新状态</span> : null}
          </div>
          {stale ? (
            <InfoNote tone="warn">
              下方为上一次成功获取的报告，仅用于参考；本次刷新未取回新结果，不能当作当前状态。
            </InfoNote>
          ) : null}

          <h3 id="health-checks-heading" className="health-heading">
            逐项检查结果
          </h3>
          <ul className="health-list" aria-labelledby="health-checks-heading">
            {HEALTH_CHECK_KEYS.map((key) => (
              <HealthRow key={key} checkKey={key} check={report.checks[key]} />
            ))}
          </ul>

          <section className="panel">
            <h3>这些结果不说明什么</h3>
            <ul className="pending-list">
              <li>模型列表通过不等于推理或检索质量通过；本页不会发起 embeddings、chat 或决策请求。</li>
              <li>存储检查只覆盖当前 profile：Local 尝试打开目录、S3 尝试 Bucket 检查，两者都不验证对象读写。</li>
              <li>数据库检查读取迁移版本与关键结构，未扫描资料正文或向量数据，也不会自动修复结构。</li>
              <li>“无法判断”表示本轮未完成观察，不等于依赖离线；“未配置”“已禁用”则由服务端配置决定。</li>
            </ul>
            <p className="muted">
              服务端配置在进程启动时读取；修改环境变量后需重启后端再重新检查。本页不显示地址、凭证、Bucket、
              profile 名称或对象 key。
            </p>
          </section>
        </>
      ) : request.status === 'loading' ? (
        <p className="empty">正在检查 API、数据库、Embedding、存储与可选服务…</p>
      ) : null}
    </section>
  )
}

function HealthRow({ checkKey, check }: { checkKey: HealthCheckKey; check: HealthCheck }) {
  const boundary = healthBoundaryText(checkKey, check)
  const advice = healthAdviceText(checkKey, check)
  return (
    <li className="health-item" data-state={check.state}>
      <div className="health-row-head">
        <strong>{HEALTH_CHECK_LABELS[checkKey]}</strong>
        <span className={`badge health-badge-${check.state}`}>{HEALTH_STATE_LABELS[check.state]}</span>
        <span className="health-required">{check.required ? '必需' : '可选'}</span>
      </div>
      <p className="health-message">{check.message}</p>
      <p className="health-meta">
        范围：{HEALTH_SCOPE_LABELS[check.scope] ?? check.scope}
        {check.backend ? ` · 后端 ${check.backend}` : ''}
        {check.latency_ms !== null ? ` · 耗时 ${check.latency_ms} ms` : ' · 本轮未发起该检查'}
      </p>
      <p className="health-meta health-code">
        <code>{check.code}</code>
      </p>
      {boundary ? <p className="health-boundary">{boundary}</p> : null}
      {advice ? <p className="health-advice">下一步：{advice}</p> : null}
    </li>
  )
}
