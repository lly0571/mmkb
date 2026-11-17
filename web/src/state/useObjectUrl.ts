// Object URL lifecycle: previews are released on change and on unmount.
import { useEffect, useState } from 'react'

export function useObjectUrl(file: File | Blob | null): string | null {
  const [url, setUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!file) {
      setUrl(null)
      return
    }
    let active = true
    let created: string | null = null
    try {
      created = URL.createObjectURL(file)
    } catch {
      created = null
    }
    if (active) setUrl(created)
    return () => {
      active = false
      if (created) URL.revokeObjectURL(created)
    }
  }, [file])

  return url
}
