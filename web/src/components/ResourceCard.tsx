import type { ResourceDetail } from '../api/types'
import { resourceSummaryText } from '../api/types'
import { resourceRoute } from '../app/router'
import { formatDateTime, KIND_LABELS, truncate } from '../state/format'
import { StatusBadge } from './StatusBadge'

interface ResourceCardProps {
  resource: ResourceDetail
  /** Browse cards expose server status; search hits have no status field and must not fake one. */
  showStatus?: boolean
}

/** First version shows text plus a type placeholder: originals load on the detail page only. */
export function ResourceCard({ resource, showStatus = true }: ResourceCardProps) {
  const summary = resourceSummaryText(resource)
  const href = resourceRoute(resource.id)
  return (
    <article className="card" data-resource-id={resource.id}>
      <div className="card-thumb" aria-hidden="true">
        <span>{KIND_LABELS[resource.input_kind]}</span>
      </div>
      <div className="card-body">
        <h3 className="card-title">
          <a href={href}>{resource.title ? resource.title : '（无标题）'}</a>
        </h3>
        <p className="card-meta">
          <span>{KIND_LABELS[resource.input_kind]}</span>
          <span>{resource.workflow ? resource.workflow : '路线待识别'}</span>
          <span>更新于 {formatDateTime(resource.updated_at)}</span>
          {showStatus ? <StatusBadge status={resource.status} /> : null}
        </p>
        <p className="card-summary">{summary ? truncate(summary, 180) : '暂无可读文字（等待处理或仅有原件）'}</p>
        {resource.tags.length > 0 ? (
          <ul className="chips chips-plain" aria-label="标签">
            {resource.tags.map((tag) => (
              <li key={tag}>
                <span>{tag}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {showStatus && resource.status === 'failed' && resource.error ? (
          <p className="card-error" role="status">
            处理失败：{resource.error}
          </p>
        ) : null}
        <p className="card-actions">
          <a className="button" href={href}>
            查看详情
          </a>
        </p>
      </div>
    </article>
  )
}
