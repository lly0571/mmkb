import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ImportPage } from '../pages/ImportPage'
import { installFetch, jsonResponse, makeCreated, pngFile } from './helpers'

const CREATED_ID = '99999999-0000-4000-8000-000000000000'
const TEXTAREA = /用户文字 \/ 图片配文/
const IMAGE_INPUT = '图片（可选）'
const SUBMIT = { name: '创建资料' } as const

function importRouter(options: { duplicate?: boolean; create?: () => Response; status?: () => unknown } = {}) {
  let statusCalls = 0
  const calls = installFetch((call) => {
    if (call.method === 'POST' && call.path === '/api/resources') {
      return options.create
        ? options.create()
        : jsonResponse(201, makeCreated({ id: CREATED_ID, status: 'pending', duplicate: options.duplicate ?? false }))
    }
    if (call.method === 'GET' && call.path === `/api/resources/${CREATED_ID}`) {
      statusCalls += 1
      const payload = options.status ? options.status() : makeCreated({ id: CREATED_ID, status: 'pending' })
      return jsonResponse(200, payload)
    }
    return jsonResponse(404, { code: 'unexpected', message: `${call.method} ${call.path}` })
  })
  return {
    calls,
    get statusCalls() {
      return statusCalls
    },
    createCall: () => calls.find((call) => call.method === 'POST'),
  }
}

describe('import page', () => {
  it('requires text when no image is chosen and never posts an empty resource', async () => {
    const user = userEvent.setup()
    const router = importRouter()
    render(<ImportPage />)
    await user.click(screen.getByRole('button', SUBMIT))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/必须填写非空文字/)).toBeTruthy()
    expect(router.createCall()).toBeUndefined()
  })

  it('posts plain text with the fixed text route, no workflow field and no manual content type', async () => {
    const user = userEvent.setup()
    const router = importRouter()
    render(<ImportPage />)
    await user.type(screen.getByLabelText(/标题（可选）/), '读书笔记')
    await user.type(screen.getByLabelText(TEXTAREA), '待保存的文字')
    await user.type(screen.getByLabelText('标签（可自由输入）'), '笔记{Enter}')
    await user.click(screen.getByRole('button', SUBMIT))

    await vi.waitFor(() => expect(router.createCall()).toBeDefined())
    const form = router.createCall()!.formData!
    expect(form.get('title')).toBe('读书笔记')
    expect(form.get('user_text')).toBe('待保存的文字')
    expect(form.getAll('tags')).toEqual(['笔记'])
    expect(form.has('file')).toBe(false)
    expect(form.has('workflow')).toBe(false)
    expect(router.createCall()!.headers['content-type']).toBeUndefined()

    expect(await screen.findByText(/创建成功/)).toBeTruthy()
    expect(screen.getByText(CREATED_ID)).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('后台处理中')
    expect(screen.getByRole('link', { name: '打开详情' }).getAttribute('href')).toBe(`#/resources/${CREATED_ID}`)
  })

  it('keeps the route fixed at text_image for image plus caption and disables the image-only choice', async () => {
    const user = userEvent.setup()
    const router = importRouter()
    render(<ImportPage />)
    await user.upload(screen.getByLabelText(IMAGE_INPUT), pngFile())
    await user.type(screen.getByLabelText(TEXTAREA), '图片配文')

    expect(screen.getByText(/固定路线 text_image/)).toBeTruthy()
    expect((screen.getByRole('group', { name: '仅图片时的处理路线' }) as HTMLFieldSetElement).disabled).toBe(true)

    await user.click(screen.getByRole('button', SUBMIT))
    await vi.waitFor(() => expect(router.createCall()).toBeDefined())
    const form = router.createCall()!.formData!
    expect(form.get('file')).toBeInstanceOf(File)
    expect(form.has('workflow')).toBe(false)
    expect(form.get('user_text')).toBe('图片配文')
  })

  it('omits workflow when the image-only route is left to automatic classification', async () => {
    const user = userEvent.setup()
    const router = importRouter()
    render(<ImportPage />)
    await user.upload(screen.getByLabelText(IMAGE_INPUT), pngFile())
    expect(screen.getByText(/省略 workflow 字段/)).toBeTruthy()
    expect((screen.getByRole('group', { name: '仅图片时的处理路线' }) as HTMLFieldSetElement).disabled).toBe(false)
    await user.click(screen.getByRole('button', SUBMIT))
    await vi.waitFor(() => expect(router.createCall()).toBeDefined())
    expect(router.createCall()!.formData!.has('workflow')).toBe(false)
  })

  it.each([
    ['文档页（整图编码）', 'visdoc'],
    ['信息密集图片（关键文字描述 + 整图）', 'info_rich_image'],
  ])('sends the chosen image-only workflow %s', async (label, workflow) => {
    const user = userEvent.setup()
    const router = importRouter()
    render(<ImportPage />)
    await user.upload(screen.getByLabelText(IMAGE_INPUT), pngFile())
    await user.click(screen.getByRole('radio', { name: label }))
    await user.click(screen.getByRole('button', SUBMIT))
    await vi.waitFor(() => expect(router.createCall()).toBeDefined())
    expect(router.createCall()!.formData!.get('workflow')).toBe(workflow)
  })

  it('keeps the draft and the preview on a validation failure', async () => {
    const user = userEvent.setup()
    installFetch((call) =>
      call.method === 'POST'
        ? jsonResponse(415, { code: 'unsupported_media_type', message: 'Only PNG, JPEG and WebP images are accepted' })
        : jsonResponse(404, { code: 'unexpected', message: call.path }),
    )
    render(<ImportPage />)
    await user.upload(screen.getByLabelText(IMAGE_INPUT), pngFile('坏了的.png'))
    await user.type(screen.getByLabelText(TEXTAREA), '配文保留')
    await user.click(screen.getByRole('button', SUBMIT))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('导入失败')).toBeTruthy()
    expect(within(alert).getByText('unsupported_media_type')).toBeTruthy()
    expect((screen.getByLabelText(TEXTAREA) as HTMLTextAreaElement).value).toBe('配文保留')
    expect(screen.getByRole('img', { name: '图片预览' })).toBeTruthy()
  })

  it('does not replay a POST whose outcome is unknown', async () => {
    const user = userEvent.setup()
    let posts = 0
    installFetch((call) => {
      if (call.method === 'POST') {
        posts += 1
        throw new TypeError('Failed to fetch')
      }
      return jsonResponse(404, { code: 'unexpected', message: call.path })
    })
    render(<ImportPage />)
    await user.type(screen.getByLabelText(TEXTAREA), '文字')
    await user.click(screen.getByRole('button', SUBMIT))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/不会自动重发/)).toBeTruthy()
    expect(within(alert).getByText(/请先回到资料库确认/)).toBeTruthy()
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(posts).toBe(1)
  })

  it('keeps polling the created resource while pending and stops at ready', async () => {
    vi.useFakeTimers()
    let stage: 'pending' | 'ready' = 'pending'
    const router = importRouter({ status: () => makeCreated({ id: CREATED_ID, status: stage }) })
    render(<ImportPage />)

    fireEvent.change(screen.getByLabelText(TEXTAREA), { target: { value: '文字' } })
    fireEvent.click(screen.getByRole('button', SUBMIT))
    await vi.waitFor(() => expect(router.calls.some((call) => call.method === 'POST')).toBe(true))
    await vi.waitFor(() => expect(router.statusCalls).toBeGreaterThanOrEqual(1))
    expect(screen.getByText('待处理')).toBeTruthy()

    stage = 'ready'
    await vi.waitFor(async () => {
      await vi.advanceTimersByTimeAsync(2000)
      expect(screen.getByText('已就绪')).toBeTruthy()
    })
    const settled = router.statusCalls
    await vi.advanceTimersByTimeAsync(10_000)
    expect(router.statusCalls).toBe(settled)
  })

  it('flags a duplicate import without pretending it was blocked', async () => {
    const user = userEvent.setup()
    importRouter({ duplicate: true })
    render(<ImportPage />)
    await user.upload(screen.getByLabelText(IMAGE_INPUT), pngFile())
    await user.click(screen.getByRole('button', SUBMIT))
    expect(await screen.findByText(/sha256 与已有资料相同/)).toBeTruthy()
    expect(screen.getByText(/仍已单独保存为一份新资料/)).toBeTruthy()
  })
})
