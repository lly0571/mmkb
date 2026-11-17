import type { TextIntent } from '../state/editDraft'

interface TriFieldProps {
  id: string
  legend: string
  baseline: string | null
  intent: TextIntent
  onChange: (intent: TextIntent) => void
  hint?: string
  autoGenerate?: boolean
}

/**
 * Three explicit states so “keep”, “manual value (possibly empty)” and “regenerate” never collapse
 * into the same request: omit / string / explicit null.
 */
export function TriField({ id, legend, baseline, intent, onChange, hint, autoGenerate = true }: TriFieldProps) {
  const select = (action: TextIntent['action']) => {
    if (action === 'keep') onChange({ action: 'keep' })
    else if (action === 'regenerate') onChange({ action: 'regenerate' })
    else onChange({ action: 'manual', value: baseline ?? '' })
  }

  return (
    <fieldset className="field fieldset">
      <legend>{legend}</legend>
      <p className="field-current">
        当前值：
        {baseline === null ? (
          <em className="muted">{autoGenerate ? 'null（未生成，等待自动处理）' : 'null（未生成）'}</em>
        ) : baseline === '' ? (
          <em className="muted">空字符串（人工置空）</em>
        ) : (
          <span className="current-value">{baseline}</span>
        )}
      </p>
      <div className="radio-row" role="radiogroup" aria-label={`${legend}处理方式`}>
        <label>
          <input
            type="radio"
            name={`${id}-intent`}
            checked={intent.action === 'keep'}
            onChange={() => select('keep')}
          />
          保留现值（不提交该字段）
        </label>
        <label>
          <input
            type="radio"
            name={`${id}-intent`}
            checked={intent.action === 'manual'}
            onChange={() => select('manual')}
          />
          人工填写（可为空字符串）
        </label>
        <label>
          <input
            type="radio"
            name={`${id}-intent`}
            checked={intent.action === 'regenerate'}
            onChange={() => select('regenerate')}
          />
          {autoGenerate ? '清空并请求重新生成（提交 null）' : '清空（提交 null，首阶段不自动生成）'}
        </label>
      </div>
      {intent.action === 'manual' ? (
        <label className="sub-field">
          <span>人工值</span>
          <textarea
            rows={4}
            value={intent.value}
            onChange={(event) => onChange({ action: 'manual', value: event.target.value })}
          />
        </label>
      ) : null}
      {hint ? <p className="field-hint">{hint}</p> : null}
    </fieldset>
  )
}
