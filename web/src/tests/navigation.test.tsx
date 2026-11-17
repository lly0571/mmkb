import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import { installFetch, jsonResponse, makeDetail, makeList } from './helpers'

describe('navigation scroll and focus', () => {
  it('resets scroll on route/ID changes, but not detail polling updates', async () => {
    const scroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    let revision = 1
    installFetch((call) => call.path === '/api/resources'
      ? jsonResponse(200, makeList([]))
      : jsonResponse(200, makeDetail({ id: call.path.split('/').at(-1), revision })))
    const view = render(<App />)
    expect(document.activeElement).toBe(screen.getByRole('main'))
    const change = async (hash: string) => {
      await act(async () => { window.location.hash = hash; window.dispatchEvent(new HashChangeEvent('hashchange')) })
    }
    await change('#/import')
    expect(scroll).toHaveBeenLastCalledWith(0, 0)
    expect(document.activeElement).toBe(screen.getByRole('main'))
    const id = '00000000-0000-4000-8000-000000000001'
    await change(`#/resources/${id}`)
    const before = scroll.mock.calls.length
    revision = 2
    view.rerender(<App />)
    expect(scroll).toHaveBeenCalledTimes(before)
    await change('#/resources/00000000-0000-4000-8000-000000000002')
    expect(scroll).toHaveBeenCalledTimes(before + 1)
  })

  it('skip link focuses main without corrupting the hash route', () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    const scrollIntoView = vi.fn()
    window.location.hash = '#/import'
    render(<App />)
    screen.getByRole('main').scrollIntoView = scrollIntoView
    fireEvent.click(screen.getByRole('link', { name: '跳到主要内容' }))
    expect(window.location.hash).toBe('#/import')
    expect(document.activeElement).toBe(screen.getByRole('main'))
    expect(scrollIntoView).toHaveBeenCalledOnce()
  })
})
