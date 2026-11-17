import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

import { resetCache } from '../state/cache'
import { resetSearchSession } from '../state/searchSession'

// jsdom does not implement object URLs; previews are still exercised for lifecycle calls.
if (typeof URL.createObjectURL !== 'function') {
  let counter = 0
  URL.createObjectURL = () => {
    counter += 1
    return `blob:local/${counter}`
  }
}
if (typeof URL.revokeObjectURL !== 'function') {
  URL.revokeObjectURL = () => undefined
}

beforeEach(() => {
  window.location.hash = ''
  resetCache()
  resetSearchSession()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.location.hash = ''
  document.body.innerHTML = ''
})
