import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ConnectionsPage } from '../pages/ConnectionsPage'
import { ApiError } from '../api/http'
import { deferred, installFetch, jsonResponse, makeHealthReport } from './helpers'

function healthFetch(payload?: unknown) {
  return installFetch(() => jsonResponse(200, payload ?? makeHealthReport()))
}

const list = () => screen.getByRole('list', { name: '逐项检查结果' })
const row = (label: string) => screen.getByText(label).closest('li') as HTMLElement
const summaryRow = () => expect(screen.getByRole('status').textContent).toBeTruthy()

describe('connections page', () => {
  it('checks once on entry and reports what each dependency check proves', async () => {
    const calls = healthFetch()
    render(<ConnectionsPage />)

    expect(await screen.findByText('连接检查通过')).toBeTruthy()
    expect(screen.getByText(/本轮已重新探测依赖/)).toBeTruthy()
    expect(within(list()).getAllByRole('listitem')).toHaveLength(7)
    expect(screen.getByText('Embedding 服务')).toBeTruthy()
    expect(screen.getByText(/范围：模型列表接口 · 耗时 180 ms/)).toBeTruthy()
    // An unconfigured optional service must not borrow the “model list verified” wording.
    expect(row('可选视觉服务').textContent).not.toContain('模型列表检查通过')
    expect(within(row('可选决策服务')).getByText(/本检查不验证路由执行/)).toBeTruthy()
    expect(screen.queryByText(/决策服务当前未接入 Worker 路由/)).toBeNull()
    expect(
      within(row('Embedding 服务')).getByText('模型列表检查通过，未执行推理，不代表生成质量或检索效果。'),
    ).toBeTruthy()
    expect(within(row('当前存储 profile')).getByText('仅验证 Bucket 可访问，未验证对象读写权限与存量文件。')).toBeTruthy()
    expect(screen.getByText(/不说明什么/)).toBeTruthy()
    expect(screen.queryByText(/Worker 正常/)).toBeNull()
    expect(calls.filter((call) => call.path === '/api/health')).toHaveLength(1)
  })

  it('keeps the Worker unmonitored instead of inferring it from the API answer', async () => {
    healthFetch()
    render(<ConnectionsPage />)
    await screen.findByText('连接检查通过')
    expect(screen.getByText('处理 Worker')).toBeTruthy()
    expect(screen.getByText('尚无 Worker 心跳，无法判断是否运行')).toBeTruthy()
    expect(within(row('处理 Worker')).getByText('未监测：尚无 Worker 心跳，无法判断是否运行。')).toBeTruthy()
    expect(screen.getByText(/范围：未接入监测 · 本轮未发起该检查/)).toBeTruthy()
    expect(screen.getByText('本轮已重新探测依赖', { exact: false })).toBeTruthy()
    // The API answering must not turn into a claim about the Worker or the queue.
    expect(screen.queryByText(/Worker 正在运行|队列正常/)).toBeNull()
  })

  it('names 检查失败, 已禁用, 未配置 and 无法判断 separately', async () => {
    healthFetch(
      makeHealthReport({
        status: 'degraded',
        checks: {
          database: {
            state: 'down',
            code: 'schema_outdated',
            message: '数据库迁移版本落后于当前应用，请执行 alembic upgrade head',
            latency_ms: 20,
            scope: 'connection_and_schema',
          },
          storage: {
            state: 'unknown',
            code: 'dependency_unavailable',
            message: '数据库未就绪，无法确定当前存储 profile',
            latency_ms: null,
            scope: 'dependency',
            backend: undefined,
          },
          vision: {
            required: false,
            state: 'down',
            code: 'model_unreachable',
            message: '无法连接模型服务，请检查服务端地址与网络',
            latency_ms: 500,
            scope: 'model_list',
          },
          decision: {
            required: false,
            state: 'disabled',
            code: 'service_disabled',
            message: '该项已显式禁用',
            latency_ms: null,
            scope: 'configuration',
          },
          embedding: {
            state: 'not_configured',
            code: 'required_not_configured',
            message: '服务端缺少该项的必需配置',
            latency_ms: null,
            scope: 'configuration',
          },
        },
      }),
    )
    render(<ConnectionsPage />)
    await screen.findByText('部分可选服务异常')

    expect(screen.getAllByText('检查失败')).toHaveLength(2)
    expect(screen.getByText('已禁用')).toBeTruthy()
    expect(screen.getByText('未配置')).toBeTruthy()
    // storage (dependency unknown) and the unmonitored worker are both “cannot tell”
    expect(screen.getAllByText('无法判断')).toHaveLength(2)
    expect(within(row('Embedding 服务')).getByText('未配置：该项不参与本轮检查。')).toBeTruthy()
    expect(within(row('当前存储 profile')).getByText('本轮未能完成观察，不能据此判断依赖离线。')).toBeTruthy()
    expect(within(row('可选决策服务')).getByText(/已显式禁用：本轮未发起任何网络请求。/)).toBeTruthy()
    // Fixed error summaries come with an actionable next step, and the stable code stays visible.
    expect(screen.getByText(/下一步：在服务端执行 alembic upgrade head/)).toBeTruthy()
    expect(screen.getByText('下一步：检查服务端模型地址、API Key 与模型 ID；本页不展示这些配置值。')).toBeTruthy()
    expect(screen.getByText('schema_outdated')).toBeTruthy()
    expect(screen.getByText(/范围：依赖数据库的 profile 判定/)).toBeTruthy()
  })

  it('marks a required dependency failure as unavailable and offers the initialization step', async () => {
    healthFetch(
      makeHealthReport({
        status: 'unavailable',
        checks: {
          database: {
            state: 'down',
            code: 'database_unreachable',
            message: '无法在预算内连接或查询数据库，请检查连接与服务负载',
            latency_ms: 1_000,
            scope: 'connection',
          },
          storage: {
            state: 'down',
            code: 'profile_not_initialized',
            message: '当前存储 profile 尚未在服务端初始化，请显式执行 storage init',
            latency_ms: null,
            scope: 'profile',
            backend: undefined,
          },
        },
      }),
    )
    render(<ConnectionsPage />)
    await screen.findByText('必需服务不可用')
    expect(screen.getByText(/下一步：在服务端执行 python -m mmkb.storage init/)).toBeTruthy()
    expect(screen.getByText(/范围：数据库连接 · 耗时 1000 ms/)).toBeTruthy()
    expect(screen.getByText(/范围：存储 profile 记录/)).toBeTruthy()
    // A failed check must not be described with wording that claims the dependency answered.
    expect(screen.queryByText(/范围：连接可用/)).toBeNull()
    expect(screen.queryByText(/已核对/)).toBeNull()
  })

  it('keeps failing model and storage checks from sounding successful', async () => {
    healthFetch(
      makeHealthReport({
        status: 'unavailable',
        checks: {
          embedding: {
            state: 'down',
            code: 'model_timeout',
            message: '模型服务列表检查超时',
            latency_ms: 2_000,
            scope: 'model_list',
          },
          storage: {
            state: 'down',
            code: 'bucket_unavailable',
            message: '无法访问 Bucket，请确认 Bucket 存在且服务端凭证可用',
            latency_ms: 300,
            scope: 'bucket_head',
            backend: 's3',
          },
        },
      }),
    )
    render(<ConnectionsPage />)
    await screen.findByText('必需服务不可用')

    expect(within(row('Embedding 服务')).getByText(/范围：模型列表接口/)).toBeTruthy()
    expect(within(row('当前存储 profile')).getByText(/范围：S3 Bucket/)).toBeTruthy()
    // Success phrasing belongs to the connected branch only.
    expect(screen.queryByText(/已核对|Bucket 可访问|目录可打开/)).toBeNull()
    expect(within(row('Embedding 服务')).getByText('模型服务列表检查超时')).toBeTruthy()
  })

  it('says so when the report is a cached snapshot instead of a fresh probe', async () => {
    healthFetch(makeHealthReport({ cached: true, age_ms: 4_200, ttl_ms: 10_000 }))
    render(<ConnectionsPage />)
    await screen.findByText(/使用最近检查结果（4 秒前，有效期 10 秒）/)
    expect(screen.queryByText(/本轮已重新探测依赖/)).toBeNull()
  })

  it('refreshes on demand, blocks duplicate clicks and keeps the button focused', async () => {
    const user = userEvent.setup()
    const first = deferred<Response>()
    const second = deferred<Response>()
    let index = 0
    installFetch(() => [first, second][index++].promise)
    render(<ConnectionsPage />)

    const pending = screen.getByRole('button', { name: '检查中…' }) as HTMLButtonElement
    expect(pending.disabled).toBe(true)
    first.resolve(jsonResponse(200, makeHealthReport()))
    await screen.findByText('连接检查通过')

    const ready = screen.getByRole('button', { name: '重新检查' }) as HTMLButtonElement
    await user.click(ready)
    expect(screen.getByRole('button', { name: '检查中…' })).toBeTruthy()
    expect(document.activeElement).toBe(ready)
    // While re-checking the previous report stays visible, labelled as pending rather than current.
    expect(screen.getByText('连接检查通过')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/正在重新检查/)

    second.resolve(jsonResponse(200, makeHealthReport({ status: 'degraded' })))
    await screen.findByText('部分可选服务异常')
    expect(screen.getAllByRole('list', { name: '逐项检查结果' })).toHaveLength(1)
    expect((screen.getByRole('button', { name: '重新检查' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('keeps the last report visible but flags it as historical when a refresh fails', async () => {
    const user = userEvent.setup()
    let mode = 'ok'
    installFetch(() =>
      mode === 'ok'
        ? jsonResponse(200, makeHealthReport())
        : jsonResponse(502, { code: 'http_error', message: 'bad gateway' }),
    )
    render(<ConnectionsPage />)
    await screen.findByText('连接检查通过')

    mode = 'fail'
    await user.click(screen.getByRole('button', { name: '重新检查' }))
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('无法获取最新连接状态')).toBeTruthy()
    expect(screen.getByText(/历史结果，未获最新状态/)).toBeTruthy()
    expect(screen.getByText(/下方为上一次成功获取的报告/)).toBeTruthy()
    expect(screen.getByText('连接检查通过')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/本次检查失败/)
  })

  it('shows a failure without inventing a green report when the API is unreachable', async () => {
    installFetch(() => Promise.reject(new TypeError('Failed to fetch')))
    render(<ConnectionsPage />)
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('无法获取最新连接状态')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/本次检查失败/)
    expect(screen.queryByRole('list', { name: '逐项检查结果' })).toBeNull()
    expect(screen.queryByText('连接检查通过')).toBeNull()
    expect(screen.getByText(/无法连接后端/)).toBeTruthy()
    summaryRow()
  })

  it('rejects an HTML page or a broken contract from the health endpoint', async () => {
    const broken: Array<() => Response> = [
      () => new Response('<html>nginx</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
      () => jsonResponse(200, makeHealthReport({ schema_version: 2 })),
      () => {
        const payload = makeHealthReport() as Record<string, Record<string, unknown>>
        delete payload['checks']['database']
        return jsonResponse(200, payload)
      },
      () => jsonResponse(200, makeHealthReport({ status: 'green' })),
    ]
    for (const handler of broken) {
      installFetch(handler)
      const { unmount } = render(<ConnectionsPage />)
      const alert = await screen.findByRole('alert')
      expect(within(alert).getByText('无法获取最新连接状态')).toBeTruthy()
      expect(screen.queryByRole('list', { name: '逐项检查结果' })).toBeNull()
      unmount()
    }
  })

  it('explains the browser wait limit when the backend never answers', async () => {
    installFetch(() =>
      Promise.reject(new ApiError(408, 'health_timeout', '等待健康检查超过 7 秒，后端可能仍在探测依赖')),
    )
    render(<ConnectionsPage />)
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/等待健康检查超过 7 秒/)).toBeTruthy()
    expect(within(alert).getByText(/浏览器等待上限 7 秒/)).toBeTruthy()
    expect(within(alert).getByText('health_timeout')).toBeTruthy()
    expect(within(alert).getByText('HTTP 408')).toBeTruthy()
  })

  it('aborts the request that the page stopped waiting for', async () => {
    vi.useFakeTimers()
    try {
      const calls = installFetch(() => deferred<Response>().promise)
      render(<ConnectionsPage />)
      // Microtasks still run under fake timers, so the request is issued before the wait limit.
      await act(async () => {
        await Promise.resolve()
      })
      expect(calls).toHaveLength(1)
      expect(calls[0].signal?.aborted).toBe(false)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(7_001)
      })
      // The abandoned request is aborted, so its late payload can never reach the page.
      expect(calls[0].signal?.aborted).toBe(true)
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(screen.getByText(/浏览器等待上限 7 秒/)).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('aborts the in-flight request when the page unmounts', async () => {
    const pending = deferred<Response>()
    const calls = installFetch(() => pending.promise)
    const { unmount } = render(<ConnectionsPage />)
    await vi.waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0].signal?.aborted).toBe(false)
    unmount()
    expect(calls[0].signal?.aborted).toBe(true)
    pending.resolve(jsonResponse(200, makeHealthReport()))
  })
})
