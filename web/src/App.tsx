import { useLayoutEffect, useRef } from 'react'

import { CONNECTIONS_ROUTE, IMPORT_ROUTE, LIBRARY_ROUTE, useRoute } from './app/router'
import { ConnectionsPage } from './pages/ConnectionsPage'
import { ImportPage } from './pages/ImportPage'
import { LibraryPage } from './pages/LibraryPage'
import { ResourceDetailPage } from './pages/ResourceDetailPage'

export function App() {
  const route = useRoute()
  const active = route.name
  const mainRef = useRef<HTMLElement>(null)
  const routeKey = route.name === 'resource' ? `resource:${route.resourceId}`
    : route.name === 'unknown' ? `unknown:${route.path}` : route.name

  useLayoutEffect(() => {
    const previous = window.history.scrollRestoration
    window.history.scrollRestoration = 'manual'
    return () => { window.history.scrollRestoration = previous }
  }, [])

  useLayoutEffect(() => {
    mainRef.current?.focus({ preventScroll: true })
    window.scrollTo(0, 0)
  }, [routeKey])

  return (
    <div className="app">
      <a className="skip-link" href="#main" onClick={(event) => {
        event.preventDefault()
        mainRef.current?.focus({ preventScroll: true })
        mainRef.current?.scrollIntoView()
      }}>
        跳到主要内容
      </a>
      <header className="app-head">
        <p className="brand">MMKB 资料库</p>
        <nav className="nav" aria-label="主导航">
          <a href={LIBRARY_ROUTE} aria-current={active === 'library' || active === 'resource' ? 'page' : undefined}>
            资料库
          </a>
          <a href={IMPORT_ROUTE} aria-current={active === 'import' ? 'page' : undefined}>
            导入
          </a>
          <a href={CONNECTIONS_ROUTE} aria-current={active === 'connections' ? 'page' : undefined}>
            连接状态
          </a>
        </nav>
        <p className="app-note">单用户本地部署；前端只调用同源 <code>/api</code>，不接触数据库、对象存储与模型凭证。</p>
      </header>
      <main id="main" className="app-main" ref={mainRef} tabIndex={-1}>
        {route.name === 'library' ? <LibraryPage /> : null}
        {route.name === 'import' ? <ImportPage /> : null}
        {route.name === 'connections' ? <ConnectionsPage /> : null}
        {route.name === 'resource' ? <ResourceDetailPage key={route.resourceId} resourceId={route.resourceId} /> : null}
        {route.name === 'unknown' ? (
          <section className="page">
            <h2>页面不存在</h2>
            <p className="empty">
              未识别的哈希路由 <code>{route.path}</code>。可用入口：<code>#/library</code>、
              <code>#/import</code>、<code>#/resources/&lt;id&gt;</code>。
            </p>
          </section>
        ) : null}
      </main>
      <footer className="app-foot">
        状态、得分与错误码均来自后端响应；卡片首版显示文字与类型占位，原件在详情页按需加载。
      </footer>
    </div>
  )
}
