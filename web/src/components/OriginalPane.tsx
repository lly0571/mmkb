import { useEffect, useState } from 'react'

interface OriginalPaneProps {
  resourceId: string
  originalUrl: string | null
}

/**
 * The original is loaded lazily and separately from the text detail: a failed image load offers its
 * own retry and never replaces details that were already read successfully.
 */
export function OriginalPane({ resourceId, originalUrl }: OriginalPaneProps) {
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [zoomed, setZoomed] = useState(false)

  useEffect(() => {
    setAttempt(0)
    setZoomed(false)
    setState(originalUrl ? 'loading' : 'ready')
  }, [resourceId, originalUrl])

  if (!originalUrl) {
    return (
      <section className="pane pane-original" aria-label="原件">
        <h2>原件</h2>
        <p className="muted">纯文本资料没有原件文件。</p>
      </section>
    )
  }

  const src = attempt === 0 ? originalUrl : `${originalUrl}?retry=${attempt}`

  return (
    <section className="pane pane-original" aria-label="原件">
      <div className="pane-head">
        <h2>原件</h2>
        <div className="pane-actions">
          <button type="button" onClick={() => setZoomed((value) => !value)}>
            {zoomed ? '适应窗口' : '放大查看'}
          </button>
          <a href={src} target="_blank" rel="noreferrer">
            单独打开
          </a>
        </div>
      </div>
      {state === 'loading' ? <p className="muted">正在通过后端代理读取原件…</p> : null}
      {state === 'error' ? (
        <div className="note note-error" role="alert">
          <div>
            <strong>原件读取失败</strong>
            <p>图片接口返回错误；文字详情不受影响。可能是文件已被清理、存储不可用或资料已删除。</p>
          </div>
          <div className="note-side">
            <button
              type="button"
              onClick={() => {
                setAttempt((value) => value + 1)
                setState('loading')
              }}
            >
              重新加载原件
            </button>
          </div>
        </div>
      ) : null}
      <img
        key={src}
        className={`original${zoomed ? ' original-zoomed' : ''}`}
        src={src}
        alt={`资料 ${resourceId} 的原件图片`}
        style={state === 'error' ? { display: 'none' } : undefined}
        onLoad={() => setState('ready')}
        onError={() => setState('error')}
      />
    </section>
  )
}
