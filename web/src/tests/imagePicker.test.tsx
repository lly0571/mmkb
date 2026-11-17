import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ImagePicker } from '../components/ImagePicker'
import { pngFile } from './helpers'

describe('accessible image picker', () => {
  it('uses one native button with a hidden sibling input; keyboard activation opens it once', async () => {
    const user = userEvent.setup()
    render(<ImagePicker id="test" label="测试图片" file={null} onChange={vi.fn()} />)
    const button = screen.getByRole('button', { name: '测试图片：选择图片' })
    const input = screen.getByLabelText('测试图片') as HTMLInputElement
    expect(button.contains(input)).toBe(false)
    expect(input.hidden).toBe(true)
    const click = vi.spyOn(input, 'click').mockImplementation(() => {})
    button.focus()
    await user.keyboard('{Enter}')
    expect(click).toHaveBeenCalledTimes(1)
    await user.keyboard(' ')
    expect(click).toHaveBeenCalledTimes(2)
  })

  it('accepts drop, paste and repeat selection without submitting a form', async () => {
    const user = userEvent.setup()
    const change = vi.fn()
    const submit = vi.fn((event) => event.preventDefault())
    render(<form onSubmit={submit}><ImagePicker id="test" label="测试图片" file={null} onChange={change} /></form>)
    const button = screen.getByRole('button', { name: '测试图片：选择图片' })
    const file = pngFile()
    fireEvent.drop(button, { dataTransfer: { files: [file] } })
    fireEvent.paste(button, { clipboardData: { files: [file] } })
    await user.upload(screen.getByLabelText('测试图片'), file)
    await user.upload(screen.getByLabelText('测试图片'), file)
    expect(change).toHaveBeenCalledTimes(4)
    expect(submit).not.toHaveBeenCalled()
  })
})
