import type { ProcessingWarning } from '../api/types'
import { warningCode, warningLabel } from '../state/format'

export function WarningList({ warnings }: { warnings: ProcessingWarning[] }) {
  if (warnings.length === 0) return null
  return (
    <section className="warnings" aria-label="处理警告">
      <h3>处理警告</h3>
      <p className="muted">警告说明部分解析步骤被跳过；状态为已就绪的资料仍已进入检索索引。</p>
      <ul>
        {warnings.map((warning) => (
          <li key={warningCode(warning)}>
            <span>{warningLabel(warning)}</span> <code>{warningCode(warning)}</code>
          </li>
        ))}
      </ul>
    </section>
  )
}
