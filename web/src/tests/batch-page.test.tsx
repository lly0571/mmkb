import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { IDBFactory } from 'fake-indexeddb'
import { expect, it, vi } from 'vitest'
import { ImportPage } from '../pages/ImportPage'
import { BatchImportPanel } from '../pages/BatchImportPanel'
import { batchController } from '../state/batchController'
import { previewBatch } from '../state/batchManifest'
import { WORKFLOW_LABELS } from '../state/format'
import { installFetch, jsonResponse, makeCreated } from './helpers'

it('previews a batch and uploads each file using the durable protocol', async () => {
  vi.stubGlobal('indexedDB', new IDBFactory())
  const calls = installFetch((call) => {
    if (call.path.endsWith('/options')) return jsonResponse(200, {
      schema_version: 1, formats: ['PNG', 'JPEG', 'WEBP'], image_workflows: ['visual_image'],
      max_upload_bytes: 1024, max_text_chars: 100,
    })
    if (call.path.endsWith('/resolve')) return jsonResponse(200, { items: (call.json!['keys'] as string[]).map((key) => ({
      key, state: 'missing', resource_id: null, outcome: null, resource_status: null, error: null, duplicate: false,
    })) })
    return jsonResponse(201, { ...makeCreated({ status: 'ready', workflow: 'visual_image' }), import_outcome: 'created', replayed: false })
  })
  const user = userEvent.setup()
  render(<ImportPage />)
  await user.click(screen.getByRole('button', { name: '批量 / 目录导入' }))
  const file = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], 'chart.png', { type: 'image/png' })
  fireEvent.change(screen.getByLabelText('批量图片文件'), { target: { files: [file] } })
  await waitFor(() => expect((screen.getByRole('button', { name: '生成预览清单' }) as HTMLButtonElement).disabled).toBe(false))
  await user.click(screen.getByRole('button', { name: '生成预览清单' }))
  await screen.findByRole('cell', { name: 'chart.png' })
  expect(screen.getByRole('columnheader', { name: '分类结果' })).toBeTruthy()
  expect(screen.getByRole('cell', { name: '待分类' })).toBeTruthy()
  expect(calls.filter((call) => call.path === '/api/resources')).toHaveLength(0)
  await user.click(screen.getByRole('button', { name: '开始 / 继续上传' }))
  await screen.findByRole('cell', { name: '已导入' })
  expect(screen.getByRole('cell', { name: WORKFLOW_LABELS.visual_image })).toBeTruthy()
  expect(calls.filter((call) => call.path === '/api/resources')).toHaveLength(1)
  expect(calls.find((call) => call.path === '/api/resources')!.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/)
  expect(screen.getByRole('link', { name: '打开资料' }).getAttribute('href')).toContain('#/resources/')
  await waitFor(() => expect(batchController.getSnapshot().busy).toBe(false))
})

it('refreshes restored classifications and distinguishes pending, skipped, failed and deleted items', async () => {
  vi.stubGlobal('indexedDB', new IDBFactory())
  const limits = { schema_version: 1 as const, formats: ['PNG'], image_workflows: ['visual_image' as const],
    max_upload_bytes: 1024, max_text_chars: 100 }
  const names = ['legacy.png', 'duplicate.png', 'processing.png', 'failed.png', 'nonimage.png', 'deleted.png', 'preset.png']
  const files = names.map((name) => new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], name))
  const { manifest } = await previewBatch(files, limits, {
    tags: [], title_mode: 'stem', workflow: 'auto', duplicates: 'skip',
  }, false, window.location.origin)
  const byName = new Map(manifest.items.map((item) => [item.relative_path, item]))
  const legacy = byName.get('legacy.png')!
  Object.assign(legacy, { status: 'created', resource_id: 'legacy', resource_status: 'ready' })
  delete legacy.resource_workflow // Older persisted records have no result field.
  Object.assign(byName.get('duplicate.png')!, { status: 'skipped', resource_id: 'duplicate', resource_status: 'ready', workflow: 'visdoc' })
  Object.assign(byName.get('processing.png')!, { status: 'created', resource_id: 'processing', resource_status: 'processing' })
  Object.assign(byName.get('failed.png')!, { status: 'failed', error: 'invalid_image' })
  Object.assign(byName.get('nonimage.png')!, { status: 'skipped', sha256: null, error: 'unsupported_format' })
  Object.assign(byName.get('deleted.png')!, { status: 'created', resource_id: 'deleted', resource_status: 'deleted', resource_workflow: 'visual_image' })
  Object.assign(byName.get('preset.png')!, { workflow: 'visdoc' })
  await batchController.setManifest(manifest, new Map(), true)
  let processed = false
  const calls = installFetch((call) => {
    if (call.path.endsWith('/options')) return jsonResponse(200, limits)
    return jsonResponse(200, { items: (call.json!['keys'] as string[]).map((key) => {
      const item = manifest.items.find((value) => value.key === key)!
      return { key, state: item.resource_id ? 'completed' : 'missing', outcome: item.status === 'skipped' ? 'skipped' : 'created',
        resource_id: item.resource_id, resource_status: item.relative_path === 'processing.png' && processed ? 'ready' : item.resource_status,
        resource_workflow: item.relative_path === 'legacy.png' ? 'structured_image'
          : item.relative_path === 'duplicate.png' ? 'visual_image'
          : item.relative_path === 'processing.png' && processed ? 'visual_image' : null,
        resource_error: null, duplicate: item.status === 'skipped', error: null }
    }) })
  })
  const user = userEvent.setup()
  render(<BatchImportPanel />)
  const row = (name: string) => within(screen.getByRole('cell', { name }).closest('tr')!)
  await waitFor(() => expect(row('legacy.png').getByRole('cell', { name: WORKFLOW_LABELS.structured_image })).toBeTruthy())
  expect(row('duplicate.png').getByRole('cell', { name: WORKFLOW_LABELS.visual_image })).toBeTruthy()
  expect(row('processing.png').getByRole('cell', { name: '分类中…' })).toBeTruthy()
  expect(row('failed.png').getByRole('cell', { name: '未分类' })).toBeTruthy()
  expect(row('nonimage.png').getByRole('cell', { name: '不适用' })).toBeTruthy()
  expect(row('deleted.png').getByRole('cell', { name: '资料已删除' })).toBeTruthy()
  expect(row('preset.png').getByRole('cell', { name: `已指定：${WORKFLOW_LABELS.visdoc}` })).toBeTruthy()
  processed = true
  await user.click(screen.getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(row('processing.png').getByRole('cell', { name: WORKFLOW_LABELS.visual_image })).toBeTruthy())
  expect(calls.every((call) => call.path !== '/api/resources')).toBe(true)
})
