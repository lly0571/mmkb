import { useState } from 'react'

import { normalizeTags } from '../api/resources'

interface TagInputProps {
  id: string
  label: string
  value: string[]
  onChange: (tags: string[]) => void
  hint?: string
}

/**
 * Free-text tags only: there is no tag catalogue endpoint, so suggestions are local history and
 * must not be presented as the whole-library tag list.
 */
export function TagInput({ id, label, value, onChange, hint }: TagInputProps) {
  const [draft, setDraft] = useState('')

  const commit = (raw: string) => {
    const next = normalizeTags([...value, ...raw.split(/[,，;；]/)])
    if (next.length !== value.length || next.some((tag, index) => tag !== value[index])) onChange(next)
    setDraft('')
  }

  const remove = (tag: string) => onChange(value.filter((item) => item !== tag))

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="tag-input">
        <input
          id={id}
          type="text"
          value={draft}
          placeholder="输入后回车添加，可用逗号分隔"
          onChange={(event) => {
            const text = event.target.value
            if (/[,，;；]/.test(text)) commit(text)
            else setDraft(text)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              if (draft.trim()) commit(draft)
            } else if (event.key === 'Backspace' && !draft && value.length > 0) {
              onChange(value.slice(0, -1))
            }
          }}
          onBlur={() => draft.trim() && commit(draft)}
        />
        <button type="button" onClick={() => draft.trim() && commit(draft)} disabled={!draft.trim()}>
          添加
        </button>
      </div>
      {value.length > 0 ? (
        <ul className="chips" aria-label="已添加标签">
          {value.map((tag) => (
            <li key={tag}>
              <span>{tag}</span>
              <button type="button" onClick={() => remove(tag)} aria-label={`移除标签 ${tag}`}>
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="field-empty">未设置标签</p>
      )}
      {hint ? <p className="field-hint">{hint}</p> : null}
    </div>
  )
}
