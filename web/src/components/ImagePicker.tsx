import { useRef, useState } from 'react'
import type { DragEvent, ClipboardEvent } from 'react'

import { useObjectUrl } from '../state/useObjectUrl'
import {
  ACCEPT_ATTRIBUTE,
  ACCEPTED_IMAGE_TYPES,
  DOCUMENTED_MAX_UPLOAD_BYTES,
  formatBytes,
  isAcceptedImageType,
} from '../state/format'

interface ImagePickerProps {
  id: string
  label: string
  file: File | null
  onChange: (file: File | null) => void
  hint?: string
  /** Preview stays local: nothing here uploads anything or creates a resource. */
  previewAlt?: string
}

function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  return target instanceof HTMLTextAreaElement || (target instanceof HTMLInputElement && target.type !== 'file')
}

export function ImagePicker({ id, label, file, onChange, hint, previewAlt = '图片预览' }: ImagePickerProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const previewUrl = useObjectUrl(file)

  const accept = (candidate: File | null | undefined) => {
    if (!candidate) return
    onChange(candidate)
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    accept(event.dataTransfer.files?.[0])
  }

  const onPaste = (event: ClipboardEvent<HTMLDivElement>) => {
    // Pasting text into a text field must keep doing text editing.
    if (isTextEntry(event.target)) return
    const pasted = event.clipboardData?.files?.[0] ?? null
    if (!pasted) return
    event.preventDefault()
    accept(pasted)
  }

  const warnings: string[] = []
  if (file) {
    if (!isAcceptedImageType(file)) {
      warnings.push(`浏览器标记类型为 ${file.type || '未知'}；后端按真实字节判定格式（PNG/JPEG/WebP）。`)
    }
    if (file.size > DOCUMENTED_MAX_UPLOAD_BYTES) {
      warnings.push(
        `文件大小 ${formatBytes(file.size)} 超过默认上限 ${formatBytes(DOCUMENTED_MAX_UPLOAD_BYTES)}；` +
          '实际限制由服务端 MAX_UPLOAD_BYTES 决定，仍会提交由后端校验。',
      )
    }
  }

  return (
    <div className="field">
      <label htmlFor={`${id}-file`}>{label}</label>
      <div
        onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onPaste={onPaste}
      >
        <button
          type="button"
          className={`dropzone${dragging ? ' dropzone-active' : ''}`}
          aria-label={`${label}：选择图片`}
          aria-describedby={`${id}-hint`}
          onClick={() => inputRef.current?.click()}
        >
          <span>点击选择、拖放文件到此区域，或聚焦后粘贴图片</span>
          <span className="dropzone-formats">接受 {ACCEPTED_IMAGE_TYPES.join(' / ')}；仅在本页预览，不自动上传</span>
        </button>
        <input
          id={`${id}-file`}
          ref={inputRef}
          type="file"
          hidden
          accept={ACCEPT_ATTRIBUTE}
          onChange={(event) => {
            accept(event.target.files?.[0] ?? null)
            if (!event.target.files?.length) return
            event.target.value = ''
          }}
        />
      </div>
      {file ? (
        <div className="preview">
          {previewUrl ? <img src={previewUrl} alt={previewAlt} /> : <p className="muted">此环境不支持本地预览</p>}
          <div className="preview-meta">
            <span className="preview-name">{file.name || '粘贴的图片'}</span>
            <span className="muted">{formatBytes(file.size)}</span>
            <button type="button" onClick={() => {
              onChange(null)
              if (inputRef.current) inputRef.current.value = ''
            }}>
              移除图片
            </button>
          </div>
        </div>
      ) : null}
      {warnings.map((warning) => (
        <p className="field-warning" key={warning}>
          {warning}
        </p>
      ))}
      <p className="field-hint" id={`${id}-hint`}>{hint ?? '不使用图片时留空即可。'}</p>
    </div>
  )
}
