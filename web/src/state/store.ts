// Minimal observable store: enough for a shared resource cache without a state framework.
export interface Store<T extends object> {
  get(): T
  set(next: Partial<T> | ((previous: T) => Partial<T>)): void
  subscribe(listener: () => void): () => void
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state = initial
  const listeners = new Set<() => void>()
  return {
    get: () => state,
    set(next) {
      const partial = typeof next === 'function' ? next(state) : next
      const current = state as Record<string, unknown>
      const changed = Object.entries(partial).some(([key, value]) => !Object.is(current[key], value))
      if (!changed) return
      state = { ...state, ...partial }
      for (const listener of [...listeners]) listener()
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
  }
}
