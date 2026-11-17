import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { LibraryPage } from '../pages/LibraryPage'
import { LIBRARY_AUTO_REFRESH_MS } from '../state/constants'
import { cacheDetail, invalidateDetail, writeSearchCache } from '../state/cache'
import { readSearchSession, searchSessionKey, writeSearchSession } from '../state/searchSession'
import { createManualVisibility } from '../state/visibility'
import { deferred, installFetch, jsonResponse, makeDetail, makeList, makeSearch, makeSearchItem } from './helpers'

function okRouter(overrides: { list?: () => unknown; search?: () => unknown } = {}) {
  return installFetch((call) => {
    if (call.method === 'GET' && call.path === '/api/resources') {
      return jsonResponse(200, overrides.list ? overrides.list() : makeList([makeDetail()]))
    }
    if (call.method === 'POST' && call.path === '/api/search') {
      return jsonResponse(200, overrides.search ? overrides.search() : makeSearch([makeSearchItem()]))
    }
    return jsonResponse(500, { code: 'unexpected', message: `${call.method} ${call.path}` })
  })
}

const queryBox = () => screen.getByRole('searchbox')
const tagBox = () => screen.getByLabelText('标签（多个标签按 AND 匹配）')
const firstCard = () => within(screen.getByRole('article'))

describe('library page', () => {
  it('opens in browse mode with pagination params and links cards to the web route', async () => {
    const id = '11111111-2222-3333-4444-555555555555'
    const calls = okRouter({ list: () => makeList([makeDetail({ id, status: 'pending' })], { total: 42 }) })
    render(<LibraryPage visibility={createManualVisibility(true)} />)

    const title = await screen.findByRole('link', { name: '金融图表笔记' })
    expect(title.getAttribute('href')).toBe(`#/resources/${id}`)
    expect(calls[0].path).toBe('/api/resources')
    expect(calls[0].search.get('limit')).toBe('20')
    expect(calls[0].search.get('offset')).toBe('0')
    expect(calls[0].search.has('status')).toBe(false)
    expect(screen.getByText(/^浏览模式 · 共 42 条/)).toBeTruthy()
    expect(firstCard().getByText('待处理')).toBeTruthy()
    expect(firstCard().queryByText('处理失败，未进入检索索引')).toBeNull()
    expect(screen.getByText(/第 1–20 条 \/ 共 42 条/)).toBeTruthy()
  })

  it('sends browse filters including repeated tags and resets the offset', async () => {
    const user = userEvent.setup()
    const calls = okRouter()
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    await screen.findByRole('link', { name: '金融图表笔记' })

    await user.selectOptions(screen.getByLabelText('状态'), 'failed')
    await vi.waitFor(() => expect(calls.at(-1)?.search.get('status')).toBe('failed'))
    expect(calls.at(-1)?.search.get('offset')).toBe('0')

    await user.type(tagBox(), '金融, 图表{Enter}')
    await vi.waitFor(() => expect(calls.at(-1)?.search.getAll('tags')).toEqual(['金融', '图表']))

    await user.click(screen.getByRole('button', { name: '移除标签 金融' }))
    await vi.waitFor(() => expect(calls.at(-1)?.search.getAll('tags')).toEqual(['图表']))
  })

  it('switches to the search endpoint with its own params and shows cosine scores', async () => {
    const user = userEvent.setup()
    const calls = okRouter({ search: () => makeSearch([makeSearchItem({ score: -0.25 })]) })
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    await screen.findByRole('link', { name: '金融图表笔记' })

    await user.selectOptions(screen.getByLabelText('状态'), 'ready')
    await user.type(queryBox(), '金融图表')
    await user.click(screen.getByRole('button', { name: '搜索' }))

    await vi.waitFor(() => expect(calls.some((call) => call.method === 'POST')).toBe(true))
    const post = calls.find((call) => call.method === 'POST')!
    expect(post.path).toBe('/api/search')
    expect(post.formData?.get('text')).toBe('金融图表')
    expect(post.formData?.get('top_k')).toBe('20')
    expect(post.formData?.has('status')).toBe(false)
    expect(post.formData?.has('input_kind')).toBe(false)
    expect(await screen.findByText(/^搜索模式 · mode=vector/)).toBeTruthy()
    expect(firstCard().getByText('得分 -0.2500')).toBeTruthy()
    expect(screen.queryByText(/^浏览模式 ·/)).toBeNull()
  })

  it('requires text or an image before searching and never guesses a query', async () => {
    const user = userEvent.setup()
    const calls = okRouter()
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    await screen.findByText(/^浏览模式 ·/)
    await user.click(screen.getByRole('button', { name: '搜索' }))
    expect(await screen.findByText(/搜索需要文字或图片至少一项/)).toBeTruthy()
    expect(calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('drops a late response from a superseded query instead of overwriting the new result', async () => {
    const user = userEvent.setup()
    const slow = deferred<Response>()
    installFetch((call) => {
      if (call.method === 'POST') {
        if (call.formData?.get('text') === '第一查询') return slow.promise
        return jsonResponse(
          200,
          makeSearch([makeSearchItem({ resource_id: 'bbbbbbbb-0000-4000-8000-000000000000', title: '新查询命中' })]),
        )
      }
      return jsonResponse(200, makeList([]))
    })
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    await screen.findByText(/^浏览模式 ·/)

    await user.type(queryBox(), '第一查询')
    await user.click(screen.getByRole('button', { name: '搜索' }))
    await user.clear(queryBox())
    await user.type(queryBox(), '第二查询')
    await user.click(screen.getByRole('button', { name: '搜索' }))

    expect(await screen.findByText('新查询命中')).toBeTruthy()
    slow.resolve(jsonResponse(200, makeSearch([makeSearchItem({ title: '迟到的旧查询命中' })])))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.queryByText('迟到的旧查询命中')).toBeNull()
  })

  it('keeps a failed search from looking like an empty result', async () => {
    const user = userEvent.setup()
    installFetch((call) =>
      call.method === 'POST'
        ? jsonResponse(503, { code: 'model_config', message: 'Check Embedding service configuration' })
        : jsonResponse(200, makeList([])),
    )
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    await screen.findByText(/^浏览模式 ·/)
    await user.type(queryBox(), '金融')
    await user.click(screen.getByRole('button', { name: '搜索' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('检索失败')).toBeTruthy()
    expect(within(alert).getByText('model_config')).toBeTruthy()
    expect(screen.queryByText(/没有命中任何已就绪资料/)).toBeNull()
    expect(screen.getByRole('button', { name: '重新搜索' })).toBeTruthy()
  })

  it('shows a real empty browse result', async () => {
    installFetch(() => jsonResponse(200, makeList([])))
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    expect(await screen.findByText(/没有找到符合筛选条件的资料/)).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a failed browse request as a failure, not as an empty list', async () => {
    installFetch(() => jsonResponse(503, { code: 'database_unavailable', message: 'Database unavailable' }))
    render(<LibraryPage visibility={createManualVisibility(true)} />)
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('资料列表读取失败')).toBeTruthy()
    expect(within(alert).getByText('database_unavailable')).toBeTruthy()
    expect(screen.queryByText(/没有找到符合筛选条件的资料/)).toBeNull()
    await userEvent.setup().click(within(alert).getByRole('button', { name: '重试' }))
    expect(await screen.findByText(/资料列表读取失败/)).toBeTruthy()
  })

  it('refreshes the visible page while items are in flight and pauses when hidden', async () => {
    vi.useFakeTimers()
    const visibility = createManualVisibility(true)
    const calls = okRouter({ list: () => makeList([makeDetail({ status: 'processing' })]) })
    render(<LibraryPage visibility={visibility} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(calls).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIBRARY_AUTO_REFRESH_MS * 2)
    })
    // One shared interval per page; cards never poll individually.
    expect(calls.length).toBeGreaterThan(1)
    expect(calls.length).toBeLessThanOrEqual(3)

    const before = calls.length
    visibility.setVisible(false)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIBRARY_AUTO_REFRESH_MS * 3)
    })
    expect(calls.length).toBe(before)

    visibility.setVisible(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(calls.length).toBeGreaterThan(before)
  })

  it('reuses cached hits after returning from a detail page without re-running the model', async () => {
    writeSearchSession({ text: '已缓存查询', tags: [], topK: 20, file: null, token: 111 })
    const key = searchSessionKey(readSearchSession())
    writeSearchCache(key, makeSearch([makeSearchItem({ title: '缓存中的命中' })]))
    const calls = installFetch((call) =>
      call.method === 'POST'
        ? jsonResponse(200, makeSearch([makeSearchItem({ title: '不应自动重跑' })]))
        : jsonResponse(200, makeList([])),
    )
    render(<LibraryPage visibility={createManualVisibility(true)} />)

    expect(await screen.findByText('缓存中的命中')).toBeTruthy()
    expect(screen.getByText(/不会自动重跑模型/)).toBeTruthy()
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('removes an edited resource from cached hits', async () => {
    const item = makeSearchItem({ resource_id: 'cccccccc-0000-4000-8000-000000000000', title: '编辑前命中' })
    writeSearchSession({ text: '已缓存查询', tags: [], topK: 20, file: null, token: 222 })
    const key = searchSessionKey(readSearchSession())
    writeSearchCache(key, makeSearch([item, makeSearchItem({ resource_id: 'dddddddd-0000-4000-8000-000000000000', title: '其他命中' })]))
    cacheDetail(makeDetail({ id: item.resource_id }))

    render(<LibraryPage visibility={createManualVisibility(true)} />)
    expect(await screen.findByText('编辑前命中')).toBeTruthy()

    // The detail page calls this after a successful PATCH; the stale hit must disappear.
    await act(async () => {
      invalidateDetail(item.resource_id)
    })
    expect(screen.queryByText('编辑前命中')).toBeNull()
    expect(screen.getByText('其他命中')).toBeTruthy()
  })
})
