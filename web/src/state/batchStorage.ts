import type { BatchManifest } from './batchManifest'

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('mmkb-imports', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('manifests', { keyPath: 'run_id' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(new Error('无法打开导入清单存储'))
  })
}

/** Persist only metadata. A revision conflict prevents two tabs from losing each other's work. */
export async function saveBatch(manifest: BatchManifest): Promise<BatchManifest> {
  const db = await database()
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction('manifests', 'readwrite')
      const store = transaction.objectStore('manifests')
      const read = store.get(manifest.run_id)
      const next = { ...manifest, local_revision: manifest.local_revision + 1 }
      let conflict = false
      read.onsuccess = () => {
        const previous = read.result as BatchManifest | undefined
        if ((previous?.local_revision ?? 0) !== manifest.local_revision) {
          conflict = true
          transaction.abort()
        } else store.put(next)
      }
      transaction.oncomplete = () => resolve(next)
      transaction.onerror = () => reject(new Error('清单保存失败，上传已暂停'))
      transaction.onabort = () => reject(new Error(conflict ? '另一页面更新了清单，请重新加载后继续' : '清单保存失败，上传已暂停'))
    })
  } finally { db.close() }
}

export async function listBatches(): Promise<BatchManifest[]> {
  const db = await database()
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction('manifests').objectStore('manifests').getAll()
      request.onsuccess = () => resolve((request.result as BatchManifest[]).sort((a, b) => b.created_at - a.created_at))
      request.onerror = () => reject(new Error('无法读取导入清单'))
    })
  } finally { db.close() }
}
