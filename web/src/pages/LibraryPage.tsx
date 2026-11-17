import { useEffect, useState } from 'react'

import { listResources } from '../api/resources'
import { DEFAULT_TOP_K, searchResources } from '../api/search'
import type { InputKind, ResourceList, ResourceStatus, SearchResponse } from '../api/types'
import { ErrorNote } from '../components/ErrorNote'
import { ImagePicker } from '../components/ImagePicker'
import { Pagination } from '../components/Pagination'
import { ResourceCard } from '../components/ResourceCard'
import { SearchCard } from '../components/SearchCard'
import { TagInput } from '../components/TagInput'
import { LIBRARY_AUTO_REFRESH_MS, LIBRARY_PAGE_SIZES } from '../state/constants'
import { IMPORT_ROUTE } from '../app/router'
import { readSearchCache, useCacheSnapshot, useLibraryToken, writeSearchCache } from '../state/cache'
import { browserVisibility, type VisibilitySource } from '../state/visibility'
import { isActiveStatus } from '../state/useResourceStatus'
import { queryKey, useRequest } from '../state/useRequest'
import { readSearchSession, resetSearchSession, searchSessionKey, writeSearchSession } from '../state/searchSession'

interface SubmittedSearch {
  text: string
  tags: string[]
  topK: number
  file: File | null
  token: number
  /** Restored sessions show cached hits; a model-backed search is never re-fired automatically. */
  run: boolean
}

export function LibraryPage({ visibility = browserVisibility }: { visibility?: VisibilitySource }) {
  const [queryText, setQueryText] = useState(() => readSearchSession().text)
  const [queryImage, setQueryImage] = useState<File | null>(() => readSearchSession().file)
  const [tags, setTags] = useState<string[]>(() => readSearchSession().tags)
  const [topK, setTopK] = useState(() => readSearchSession().topK)
  const [submitted, setSubmitted] = useState<SubmittedSearch | null>(() => {
    const session = readSearchSession()
    if (!session.text.trim() && !session.file) return null
    const restored: SubmittedSearch = {
      text: session.text,
      tags: session.tags,
      topK: session.topK,
      file: session.file,
      token: session.token,
      run: false,
    }
    // Keep search mode across navigation only when the previous hits are still cached.
    return readSearchCache(searchSessionKey(restored)) ? restored : null
  })
  const [statusFilter, setStatusFilter] = useState<ResourceStatus | ''>('')
  const [kindFilter, setKindFilter] = useState<InputKind | ''>('')
  const [limit, setLimit] = useState(DEFAULT_TOP_K)
  const [offset, setOffset] = useState(0)
  const [inputProblem, setInputProblem] = useState('')

  const libraryToken = useLibraryToken()
  const snapshot = useCacheSnapshot()

  const searching = submitted !== null
  const currentSearchKey = submitted ? searchSessionKey(submitted) : ''
  const cachedSearch =
    searching && snapshot.search && snapshot.search.key === currentSearchKey ? snapshot.search.response : null

  const searchResult = useRequest<SearchResponse>(
    currentSearchKey,
    (signal) =>
      searchResources(
        { text: submitted?.text, file: submitted?.file, tags: submitted?.tags, topK: submitted?.topK },
        signal,
      ).then((response) => {
        writeSearchCache(currentSearchKey, response)
        return response
      }),
    { enabled: searching && submitted.run, keepData: false },
  )

  const listKey = queryKey('list', limit, offset, statusFilter, kindFilter, tags, libraryToken)
  const listResult = useRequest<ResourceList>(
    listKey,
    (signal) => listResources({ limit, offset, status: statusFilter, input_kind: kindFilter, tags }, signal),
    { enabled: !searching },
  )

  const items = listResult.data?.items ?? []
  const hasActive = items.some((item) => isActiveStatus(item.status))
  const reloadList = listResult.reload

  useEffect(() => {
    if (searching || !hasActive) return
    const source = visibility
    const timer = setInterval(() => {
      if (source.isVisible()) reloadList()
    }, LIBRARY_AUTO_REFRESH_MS)
    const unsubscribe = source.subscribe(() => {
      if (source.isVisible()) reloadList()
    })
    return () => {
      clearInterval(timer)
      unsubscribe()
    }
  }, [searching, hasActive, reloadList, visibility])

  const submitSearch = () => {
    if (!queryText.trim() && !queryImage) {
      setInputProblem('搜索需要文字或图片至少一项；只想按条件浏览请清空查询区。')
      return
    }
    setInputProblem('')
    const next: SubmittedSearch = { text: queryText, tags, topK, file: queryImage, token: Date.now(), run: true }
    writeSearchSession({ text: queryText, tags, topK, file: queryImage, token: next.token })
    setSubmitted(next)
  }

  const reRunSearch = () => {
    if (!submitted) return
    const next = { ...submitted, token: Date.now(), run: true }
    writeSearchSession({ token: next.token })
    setSubmitted(next)
  }

  const clearSearch = () => {
    setQueryText('')
    setQueryImage(null)
    setSubmitted(null)
    setInputProblem('')
    resetSearchSession()
  }

  const applyFilters = () => {
    // Browsing filters never leak into the search request: the search endpoint has no such params.
    setOffset(0)
    if (searching) setSubmitted(null)
  }

  const searchResponse = searchResult.data ?? cachedSearch

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>资料库与检索</h2>
          <p className="muted">浏览走分页接口；搜索走向量 Top-K 接口，两者参数互不混用。</p>
        </div>
        <a className="button button-primary" href={IMPORT_ROUTE}>
          导入资料
        </a>
      </header>

      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault()
          submitSearch()
        }}
      >
        <div className="query-row">
          <label className="grow">
            <span>查询文字</span>
            <input
              type="search"
              value={queryText}
              placeholder="按语义检索已就绪资料，例如：金融图表"
              onChange={(event) => setQueryText(event.target.value)}
            />
          </label>
          <label>
            <span>Top-K</span>
            <select value={topK} onChange={(event) => setTopK(Number(event.target.value))}>
              {[10, 20, 50, 100].map((value) => (
                <option key={value} value={value}>
                  {value === DEFAULT_TOP_K ? `${value}（默认）` : value}
                </option>
              ))}
            </select>
          </label>
          <button className="button-primary" type="submit">
            搜索
          </button>
          <button type="button" onClick={clearSearch} disabled={!searching && !queryText && !queryImage}>
            清空查询
          </button>
        </div>
        <ImagePicker
          id="query"
          label="查询图片（仅本次请求使用，不会创建资料）"
          file={queryImage}
          onChange={setQueryImage}
          hint="与查询文字可同时提供；图片只在请求期间使用。"
        />
        <TagInput
          id="query-tags"
          label="标签（多个标签按 AND 匹配）"
          value={tags}
          onChange={setTags}
          hint="无标签目录接口，此处为手工输入。"
        />
        {inputProblem ? (
          <p className="field-warning" role="status">
            {inputProblem}
          </p>
        ) : null}
        <div className="filter-row">
          <span className="filter-label">仅浏览模式生效</span>
          <label>
            <span>状态</span>
            <select
              value={statusFilter}
              onChange={(event) => {
                setStatusFilter(event.target.value as ResourceStatus | '')
                applyFilters()
              }}
            >
              <option value="">全部状态</option>
              <option value="pending">待处理</option>
              <option value="processing">处理中</option>
              <option value="ready">已就绪</option>
              <option value="failed">失败</option>
            </select>
          </label>
          <label>
            <span>输入类型</span>
            <select
              value={kindFilter}
              onChange={(event) => {
                setKindFilter(event.target.value as InputKind | '')
                applyFilters()
              }}
            >
              <option value="">全部类型</option>
              <option value="image">仅图片</option>
              <option value="text_image">图文</option>
              <option value="text">纯文本</option>
            </select>
          </label>
          <label>
            <span>每页</span>
            <select
              value={limit}
              onChange={(event) => {
                setLimit(Number(event.target.value))
                setOffset(0)
              }}
            >
              {LIBRARY_PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
        </div>
      </form>

      <section aria-live="polite" aria-busy={listResult.isFetching || searchResult.isFetching}>
        {searching ? (
          <SearchResults
            response={searchResponse}
            topK={submitted?.topK ?? DEFAULT_TOP_K}
            loading={searchResult.status === 'loading'}
            error={searchResult.error}
            onRetry={reRunSearch}
            cachedOnly={searchResult.data === null && cachedSearch !== null}
          />
        ) : (
          <BrowseResults
            data={listResult.data}
            loading={listResult.status === 'loading'}
            error={listResult.error}
            onRetry={listResult.reload}
            refreshing={listResult.isFetching && listResult.data !== null}
            hasActive={hasActive}
            limit={limit}
            offset={offset}
            onPage={setOffset}
          />
        )}
      </section>
    </section>
  )
}

function BrowseResults({
  data,
  loading,
  error,
  onRetry,
  refreshing,
  hasActive,
  limit,
  offset,
  onPage,
}: {
  data: ResourceList | null
  loading: boolean
  error: unknown
  onRetry: () => void
  refreshing: boolean
  hasActive: boolean
  limit: number
  offset: number
  onPage: (offset: number) => void
}) {
  if (error && !data) return <ErrorNote error={error} title="资料列表读取失败" onRetry={onRetry} />
  return (
    <>
      <p className="mode-hint">
        浏览模式 · 共 {data?.total ?? '—'} 条 · 按创建时间倒序
        {refreshing ? ' · 正在刷新…' : ''}
        {hasActive ? ' · 有资料在处理中，本页会定期刷新' : ''}
      </p>
      {error && data ? <ErrorNote error={error} title="列表刷新失败，以下为上次成功结果" onRetry={onRetry} /> : null}
      {loading && !data ? <p className="muted">正在读取资料列表…</p> : null}
      {data && data.items.length === 0 ? (
        <p className="empty">没有找到符合筛选条件的资料。可调整筛选，或到“导入资料”新增。</p>
      ) : null}
      <div className="card-list">
        {(data?.items ?? []).map((resource) => (
          <ResourceCard key={resource.id} resource={resource} />
        ))}
      </div>
      {data ? <Pagination total={data.total} limit={limit} offset={offset} onPage={onPage} /> : null}
    </>
  )
}

function SearchResults({
  response,
  topK,
  loading,
  error,
  onRetry,
  cachedOnly,
}: {
  response: SearchResponse | null
  topK: number
  loading: boolean
  error: unknown
  onRetry: () => void
  cachedOnly: boolean
}) {
  if (error && !response) {
    return (
      <ErrorNote
        error={error}
        title="检索失败"
        note="失败不会被显示成“没有结果”，也不会自动降级为关键词检索。"
        onRetry={onRetry}
        retryLabel="重新搜索"
      />
    )
  }
  return (
    <>
      <p className="mode-hint">
        搜索模式 · mode={response?.mode ?? 'vector'} · 返回 {response?.items.length ?? 0} 条（Top-K {topK}；
        该接口无分页与 total）
        {loading ? ' · 正在检索…' : ''}
      </p>
      {error && response ? (
        <ErrorNote error={error} title="重新检索失败，以下为上次结果" onRetry={onRetry} retryLabel="重新搜索" />
      ) : null}
      {cachedOnly ? (
        <p className="mode-hint">显示的是本标签页内上次搜索结果；编辑或删除过的资料已从结果中移除，不会自动重跑模型。</p>
      ) : null}
      {loading && !response ? <p className="muted">正在编码查询并检索…</p> : null}
      {response && response.items.length === 0 ? (
        <p className="empty">没有命中任何已就绪资料。处理中的资料不会进入检索结果。</p>
      ) : null}
      <div className="card-list">
        {(response?.items ?? []).map((item) => (
          <SearchCard key={item.resource_id} item={item} mode={response?.mode ?? 'vector'} />
        ))}
      </div>
      {response ? (
        <button type="button" className="button" onClick={onRetry}>
          重新搜索
        </button>
      ) : null}
    </>
  )
}
