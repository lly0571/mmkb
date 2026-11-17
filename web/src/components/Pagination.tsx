interface PaginationProps {
  total: number
  limit: number
  offset: number
  onPage: (offset: number) => void
}

export function Pagination({ total, limit, offset, onPage }: PaginationProps) {
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  const hasPrevious = offset > 0
  const hasNext = offset + limit < total
  return (
    <nav className="pagination" aria-label="分页">
      <button type="button" disabled={!hasPrevious} onClick={() => onPage(Math.max(0, offset - limit))}>
        上一页
      </button>
      <span>{`第 ${from}–${to} 条 / 共 ${total} 条`}</span>
      <button type="button" disabled={!hasNext} onClick={() => onPage(offset + limit)}>
        下一页
      </button>
    </nav>
  )
}
