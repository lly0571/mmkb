import { InfoNote } from '../components/ErrorNote'

/**
 * Connection status is intentionally read-only and unimplemented: the health endpoint is task T05
 * and does not exist yet. Nothing here probes or infers Worker liveness, and no item is shown as
 * healthy without a contract that says so.
 */
export function ConnectionsPage() {
  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>连接状态</h2>
          <p className="muted">待接入：后端尚未交付健康检查接口（T05）。</p>
        </div>
      </header>

      <InfoNote tone="warn">
        本页不提供状态灯。没有 <code>GET /api/health</code> 契约时，逐项显示“正常”会伪造后端结论，因此相关检查项保持未完成。
      </InfoNote>

      <section className="panel">
        <h3>接口交付后计划展示</h3>
        <ul className="pending-list">
          <li>API 进程可达性 —— 仅说明 HTTP 入口可访问，不能推出模型推理通过。</li>
          <li>数据库连接与迁移版本 —— 有界超时的显式探测。</li>
          <li>Embedding 服务 —— 区分未配置、可连接（列表检查）与故障；列表可达不等于质量验收。</li>
          <li>当前存储 profile —— Local / S3 端点与 Bucket 可访问性。</li>
          <li>可选视觉 / 决策服务 —— 明确区分未配置、已禁用与故障。</li>
        </ul>
        <p className="muted">
          Worker 没有心跳接口，因此不会显示“Worker 正常运行”。本页只读，不在浏览器保存或编辑数据库、Bucket 与模型凭证。
        </p>
      </section>

      <section className="panel">
        <h3>当前可用的事实核查方式</h3>
        <ul className="pending-list">
          <li>资料处理是否推进：在详情页观察服务端返回的 <code>status</code>（待处理 / 处理中 / 已就绪 / 失败）。</li>
          <li>存储是否可用：详情页“通过后端代理打开”原件，成功即该资料的 profile 可读。</li>
          <li>Embedding 是否可用：执行一次检索，失败会返回带 <code>embedding_*</code> 或 <code>model_config</code> 的错误码。</li>
        </ul>
      </section>
    </section>
  )
}
