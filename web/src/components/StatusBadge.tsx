import type { ResourceStatus } from '../api/types'
import { STATUS_HINTS, STATUS_LABELS } from '../state/format'

export function StatusBadge({ status, showHint = false }: { status: ResourceStatus; showHint?: boolean }) {
  return (
    <>
      <span className={`badge status-${status}`} data-status={status}>
        {STATUS_LABELS[status]}
      </span>
      {showHint ? <span className="badge-hint">{STATUS_HINTS[status]}</span> : null}
    </>
  )
}
