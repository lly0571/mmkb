// Page visibility source, injectable so polling behaviour can be tested without a real browser.
export interface VisibilitySource {
  isVisible(): boolean
  subscribe(listener: () => void): () => void
}

export const browserVisibility: VisibilitySource = {
  isVisible: () => (typeof document === 'undefined' ? true : !document.hidden),
  subscribe(listener) {
    if (typeof document === 'undefined') return () => undefined
    const handler = () => listener()
    document.addEventListener('visibilitychange', handler)
    return () => document.removeEventListener('visibilitychange', handler)
  },
}

export interface ManualVisibility extends VisibilitySource {
  setVisible(visible: boolean): void
}

export function createManualVisibility(initial = true): ManualVisibility {
  let visible = initial
  const listeners = new Set<() => void>()
  return {
    isVisible: () => visible,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    setVisible(next) {
      if (next === visible) return
      visible = next
      for (const listener of [...listeners]) listener()
    },
  }
}
