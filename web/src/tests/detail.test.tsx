import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ResourceDetailPage } from '../pages/ResourceDetailPage'
import { getCachedDetail } from '../state/cache'
import { installFetch, jsonResponse, makeDetail, noContentResponse, pngFile } from './helpers'

const ID = 'aaaaaaaa-0000-4000-8000-000000000000'

interface RouterOptions {
  detail?: () => ReturnType<typeof makeDetail>
  patch?: () => Response
}

function resourceRouter(options: RouterOptions = {}) {
  let patchCalls = 0
  let retryCalls = 0
  let deleteCalls = 0
  let getCalls = 0
  const calls = installFetch((call) => {
    if (call.path.endsWith('/original')) return new Response(pngFile('original', 512), { status: 200 })
    if (call.method === 'GET') {
      getCalls += 1
      return jsonResponse(200, options.detail ? options.detail() : makeDetail({ id: ID }))
    }
    if (call.method === 'PATCH') {
      patchCalls += 1
      return options.patch ? options.patch() : jsonResponse(200, makeDetail({ id: ID, status: 'pending', revision: 6 }))
    }
    if (call.method === 'POST') {
      retryCalls += 1
      return jsonResponse(200, makeDetail({ id: ID, status: 'pending', revision: 6 }))
    }
    if (call.method === 'DELETE') {
      deleteCalls += 1
      return noContentResponse()
    }
    return jsonResponse(500, { code: 'unexpected', message: call.path })
  })
  return {
    calls,
    get patchCalls() {
      return patchCalls
    },
    get retryCalls() {
      return retryCalls
    },
    get deleteCalls() {
      return deleteCalls
    },
    get getCalls() {
      return getCalls
    },
  }
}

const openDetail = async () => {
  await screen.findByText(/资源 ID/)
}

describe('resource detail page', () => {
  it.each(['edit', 'retry'])('restarts stopped polling after %s and observes ready without navigation', async (action) => {
    vi.useFakeTimers()
    let detail = makeDetail({ id: ID, status: action === 'edit' ? 'ready' : 'failed',
      error: action === 'retry' ? 'embedding_timeout: retry the resource' : null, revision: 3 })
    let gets = 0
    installFetch((call) => {
      if (call.method === 'GET') {
        gets += 1
        return jsonResponse(200, detail)
      }
      detail = { ...detail, revision: 4, status: 'pending' }
      return jsonResponse(200, detail)
    })
    render(<ResourceDetailPage resourceId={ID} />)
    await act(async () => {})
    if (action === 'edit') {
      fireEvent.click(screen.getByRole('button', { name: '编辑' }))
      fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'changed' } })
      fireEvent.click(screen.getByRole('button', { name: '提交编辑' }))
    } else {
      fireEvent.click(screen.getByRole('button', { name: '重试（使用 revision 3）' }))
    }
    await act(async () => {})
    expect(gets).toBe(2)
    detail = { ...detail, status: 'ready' }
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(screen.getByText('已就绪')).toBeTruthy()
    const settled = gets
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(gets).toBe(settled)
  })

  it('shows stored fields, processing warnings with codes and the proxied original', async () => {
    const detail = makeDetail({
      id: ID,
      status: 'ready',
      extra: { mime_type: 'image/png', width: 800, height: 600, processing: { warnings: [{ stage: 'caption', code: 'unavailable' }] } },
    })
    resourceRouter({ detail: () => detail })
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()

    expect(screen.getByRole('img', { name: `资料 ${ID} 的原件图片` })).toBeTruthy()
    expect(screen.getByText('已就绪')).toBeTruthy()
    expect(screen.getByText('一张金融图表的配文')).toBeTruthy()
    expect(screen.getAllByText('null（未生成）').length).toBeGreaterThan(0)
    const warnings = screen.getByRole('region', { name: '处理警告' })
    expect(within(warnings).getByText('图像描述（Caption）：可选模型服务未启用，本步骤已跳过')).toBeTruthy()
    expect(within(warnings).getByText('caption/unavailable')).toBeTruthy()
    expect(screen.getByText(/状态为已就绪的资料仍已进入检索索引/)).toBeTruthy()
  })

  it('does not offer an image entry for plain-text resources', async () => {
    resourceRouter({
      detail: () => makeDetail({ id: ID, input_kind: 'text', workflow: 'text', original_url: null, sha256: null }),
    })
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()
    expect(screen.getByText(/纯文本资料没有原件文件/)).toBeTruthy()
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('does not offer retry while the resource is ready', async () => {
    resourceRouter({ detail: () => makeDetail({ id: ID, status: 'ready', revision: 4 }) })
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()
    expect(screen.queryByRole('button', { name: /^重试/ })).toBeNull()
  })

  it('offers retry for failed resources and posts the current revision', async () => {
    const router = resourceRouter({
      detail: () => makeDetail({ id: ID, status: 'failed', error: 'embedding_timeout: upstream', revision: 4 }),
    })
    render(<ResourceDetailPage resourceId={ID} />)
    await screen.findByText('处理失败', { exact: true })
    expect(router.retryCalls).toBe(0)
    await userEvent.setup().click(screen.getByRole('button', { name: '重试（使用 revision 4）' }))
    await vi.waitFor(() => expect(router.retryCalls).toBe(1))
    expect(router.calls.find((call) => call.method === 'POST')?.json).toEqual({ revision: 4 })
    expect(screen.getByText(/已重新排队：revision 4 → 6/)).toBeTruthy()
  })

  it('keeps the draft after a 409, reloads the detail and never auto-resends', async () => {
    let serverRevision = 5
    const user = userEvent.setup()
    const router = resourceRouter({
      detail: () => makeDetail({ id: ID, revision: serverRevision }),
      patch: () => {
        // The remote edit lands while the PATCH is in flight: the next read shows revision 9.
        serverRevision = 9
        return jsonResponse(409, { code: 'revision_conflict', message: 'Resource changed; reload and retry' })
      },
    })
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()

    await user.click(screen.getByRole('button', { name: '编辑' }))
    const titleInput = screen.getByLabelText('标题') as HTMLInputElement
    await user.clear(titleInput)
    await user.type(titleInput, '人工新标题')
    await user.click(screen.getByRole('button', { name: '提交编辑' }))

    await vi.waitFor(() => expect(router.patchCalls).toBe(1))
    expect(router.calls.find((call) => call.method === 'PATCH')?.json).toEqual({ revision: 5, title: '人工新标题' })

    serverRevision = 9
    await vi.waitFor(() => expect(screen.getByText(/远端已变化/)).toBeTruthy())
    expect(screen.getByText(/版本冲突（未提交）/)).toBeTruthy()
    expect(screen.getByText(/服务端已是 9/)).toBeTruthy()
    expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('人工新标题')

    // The conflict path re-reads the resource (proven by the 远端已变化 notice) but never resends.
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(router.patchCalls).toBe(1)
    expect(router.getCalls).toBeGreaterThanOrEqual(2)

    // Re-basing is an explicit user action; it does not send anything by itself.
    await user.click(screen.getByRole('button', { name: '以最新详情为基准比较草稿' }))
    expect(router.patchCalls).toBe(1)
    expect(screen.queryByText(/远端已变化/)).toBeNull()
  })

  it('does not send a PATCH when nothing changed', async () => {
    const user = userEvent.setup()
    const router = resourceRouter()
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()
    await user.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByText(/没有改动：不会发送 PATCH/)).toBeTruthy()
    const submit = screen.getByRole('button', { name: '提交编辑' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: '放弃草稿' }))
    expect(screen.queryByRole('button', { name: '提交编辑' })).toBeNull()
    expect(router.patchCalls).toBe(0)
  })

  it('sends "" for a manual empty caption and null only for regeneration', async () => {
    const user = userEvent.setup()
    const router = resourceRouter()
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()

    await user.click(screen.getByRole('button', { name: '编辑' }))
    const captionGroup = screen.getByRole('radiogroup', { name: 'Caption（图像描述）处理方式' })
    await user.click(within(captionGroup).getByRole('radio', { name: '人工填写（可为空字符串）' }))
    const value = screen.getByLabelText('人工值') as HTMLTextAreaElement
    await user.clear(value)
    await user.click(screen.getByRole('button', { name: '提交编辑' }))
    await vi.waitFor(() => expect(router.patchCalls).toBe(1))
    expect(router.calls.find((call) => call.method === 'PATCH')?.json).toEqual({ revision: 3, caption: '' })
  })

  it('sends explicit null to clear OCR and explains that automatic OCR is deferred', async () => {
    const user = userEvent.setup()
    const router = resourceRouter()
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()
    await user.click(screen.getByRole('button', { name: '编辑' }))
    const ocrGroup = screen.getByRole('radiogroup', { name: 'OCR 文字处理方式' })
    await user.click(within(ocrGroup).getByRole('radio', { name: '清空（提交 null，首阶段不自动生成）' }))
    expect(screen.getByText(/首阶段所有路线均不自动生成 OCR/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '提交编辑' }))
    await vi.waitFor(() => expect(router.patchCalls).toBe(1))
    const body = router.calls.find((call) => call.method === 'PATCH')?.json
    expect(body).toEqual({ revision: 3, ocr_text: null })
  })

  it('refuses to blank out the only text of a plain-text resource', async () => {
    const user = userEvent.setup()
    const router = resourceRouter({
      detail: () => makeDetail({ id: ID, input_kind: 'text', workflow: 'text', original_url: null, user_text: '唯一正文' }),
    })
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()
    await user.click(screen.getByRole('button', { name: '编辑' }))
    const body = screen.getByLabelText('正文文字（纯文本不可清空）') as HTMLTextAreaElement
    await user.clear(body)
    expect(await screen.findByText(/纯文本资料不能清空正文/)).toBeTruthy()
    expect((screen.getByRole('button', { name: '提交编辑' }) as HTMLButtonElement).disabled).toBe(true)
    expect(router.patchCalls).toBe(0)
  })

  it('confirms the title before deleting, then drops every local copy of the resource', async () => {
    const user = userEvent.setup()
    const router = resourceRouter()
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()

    await user.click(screen.getByRole('button', { name: '删除' }))
    const dialog = screen.getByRole('alertdialog', { name: '确认删除' })
    expect(within(dialog).getByText(/《金融图表笔记》/)).toBeTruthy()
    expect(within(dialog).getByText(/当前 UI 不提供恢复/)).toBeTruthy()
    expect(router.deleteCalls).toBe(0)

    await user.click(within(dialog).getByRole('button', { name: '确认删除' }))
    await vi.waitFor(() => expect(router.deleteCalls).toBe(1))
    expect(window.location.hash).toBe('#/library')
    expect(getCachedDetail(ID)).toBeNull()
  })

  it('keeps readable text details when the original fails to load', async () => {
    const calls = installFetch((call) => {
      if (call.path.endsWith('/original')) return jsonResponse(404, { code: 'original_not_found', message: 'Original file not found' })
      if (call.method === 'GET') return jsonResponse(200, makeDetail({ id: ID }))
      return jsonResponse(404, { code: 'http_error', message: 'unexpected' })
    })
    const originalCalls = () => calls.filter((call) => call.path.endsWith('/original')).length
    render(<ResourceDetailPage resourceId={ID} />)
    await openDetail()

    const image = screen.getByRole('img', { name: `资料 ${ID} 的原件图片` })
    act(() => {
      image.dispatchEvent(new Event('error'))
    })
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('原件读取失败')).toBeTruthy()
    expect(within(alert).getByText(/文字详情不受影响/)).toBeTruthy()
    expect(screen.getByText('一张金融图表的配文')).toBeTruthy()
    expect(originalCalls()).toBe(0)
    await userEvent.setup().click(within(alert).getByRole('button', { name: '重新加载原件' }))
    // jsdom never fetches <img> sources, so the retry affordance is asserted on the new request URL.
    expect(screen.getByRole('img', { name: `资料 ${ID} 的原件图片` }).getAttribute('src')).toContain('retry=1')
  })
})
