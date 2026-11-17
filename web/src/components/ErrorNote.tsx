import { explainError, toApiError } from '../api/http'

interface ErrorNoteProps {
  error: unknown
  title?: string
  onRetry?: () => void
  retryLabel?: string
  /** Extra guidance, e.g. that a failed POST outcome is unknown. */
  note?: string
}

/** Failures are never presented as “no results”; the code stays visible for log correlation. */
export function ErrorNote({ error, title, onRetry, retryLabel = '重试', note }: ErrorNoteProps) {
  const apiError = toApiError(error)
  return (
    <div className="note note-error" role="alert">
      <div>
        <strong>{title ?? '请求失败'}</strong>
        <p>{explainError(apiError)}</p>
        {apiError.details.length > 0 ? (
          <ul className="note-details">
            {apiError.details.map((detail, index) => (
              <li key={`${detail.field}-${index}`}>
                <code>{detail.field}</code> {detail.message}
              </li>
            ))}
          </ul>
        ) : null}
        {note ? <p className="note-extra">{note}</p> : null}
      </div>
      <div className="note-side">
        <code className="note-code">{apiError.code}</code>
        {apiError.status > 0 ? <code className="note-code">HTTP {apiError.status}</code> : null}
        {onRetry ? (
          <button type="button" onClick={onRetry}>
            {retryLabel}
          </button>
        ) : null}
      </div>
    </div>
  )
}

export function InfoNote({ children, tone = 'info' }: { children: React.ReactNode; tone?: 'info' | 'warn' }) {
  return <p className={`note note-${tone}`}>{children}</p>
}
