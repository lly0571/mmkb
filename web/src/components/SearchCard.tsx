import type { SearchItem } from '../api/types'
import { resourceRoute } from '../app/router'
import { formatScore, KIND_LABELS, truncate } from '../state/format'

/**
 * Search hits carry strictly fewer fields than ResourceDetail: no status, no revision, no
 * updated_at. The score is cosine similarity (not a probability, may be negative).
 */
export function SearchCard({ item, mode }: { item: SearchItem; mode: string }) {
  const href = resourceRoute(item.resource_id)
  return (
    <article className="card" data-resource-id={item.resource_id}>
      <div className="card-thumb" aria-hidden="true">
        <span>{KIND_LABELS[item.input_kind]}</span>
      </div>
      <div className="card-body">
        <h3 className="card-title">
          <a href={href}>{item.title ? item.title : '（无标题）'}</a>
        </h3>
        <p className="card-meta">
          <span>{KIND_LABELS[item.input_kind]}</span>
          <span>模式 {mode}</span>
          <span className="score" title="cosine 相似度得分，不是概率，允许为负">
            得分 {formatScore(item.score)}
          </span>
        </p>
        <p className="card-summary">{item.summary ? truncate(item.summary, 180) : '（无摘要文字）'}</p>
        {item.tags.length > 0 ? (
          <ul className="chips chips-plain" aria-label="标签">
            {item.tags.map((tag) => (
              <li key={tag}>
                <span>{tag}</span>
              </li>
            ))}
          </ul>
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
