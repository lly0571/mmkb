# MMKB Web 前端

`web/` 是 MMKB 的单用户资料管理界面：React 19 + TypeScript + Vite 8 的 SPA，使用哈希路由，只调用同源 `/api` 接口，不直接连接 PostgreSQL、对象存储或模型服务，也不保存任何服务端凭证。

页面与后端能力的对应关系、请求与状态约定、静态部署方式见 [Web 界面说明](../docs/web.md)。

## 运行条件

| 项 | 本次开发实测值 |
| --- | --- |
| Node.js | `v26.13.1`（Vite 8 要求 `^20.19.0 \|\| >=22.12.0`，`engines` 已写入 `package.json`） |
| npm | `12.2.2` |
| 依赖锁文件 | `package-lock.json`（已提供） |

```bash
cd web
npm ci          # 或 npm install
npm run dev     # http://127.0.0.1:5173
```

若 `npm ci` 后 `vite`/`vitest` 报 `Cannot find native binding`（npm 可选依赖的已知问题），补装与 `rolldown` 同版本的平台包后重试：

```bash
npm i -D @rolldown/binding-linux-x64-gnu@<rolldown 版本>
```

## 开发代理

`vite.config.ts` 把 `/api` 原样代理到 API（后端路由自带 `/api` 前缀，因此不重写路径）。代理目标只读自 `process.env.MMKB_API_PROXY_TARGET` 或 `web/.env.local`，不进入前端包：

```bash
cp .env.example .env.local   # MMKB_API_PROXY_TARGET=http://127.0.0.1:8010
```

先确认本机端口占用（例如 8000 常被 vLLM 使用），再启动后端：

```bash
uvicorn mmkb.api:create_app --factory --host 127.0.0.1 --port 8010
```

浏览器对 5173 发起同源请求，原件的相对地址同样走代理，因此后端无需配置 CORS。`vite dev` 的代理只在开发时存在，构建产物请使用 [部署说明](../docs/web.md#静态部署) 的同源入口。

## 命令

```bash
npm run typecheck   # tsc -b，含测试代码
npm run test        # vitest run（jsdom + Testing Library）
npm run test:watch
npm run build       # tsc -b && vite build → dist/
npm run preview     # 本地预览构建产物（不代理 /api）
npm run lint        # oxlint
```

`npm run lint` 目前保留 4 条 `react(set-state-in-effect)` 提示，分别来自对象 URL、原件面板重置、请求生命周期与状态轮询；`react/rules-of-hooks` 保持为 error。

## 测试覆盖重点

`src/tests/` 只测关键契约与交互，不给纯展示组件写实现镜像测试：

| 文件 | 覆盖 |
| --- | --- |
| `http.test.ts` | JSON/FormData 头约定、204 不解析正文、`{code,message,details}` 与非 JSON 代理错误、网络失败不自动重放、取消 |
| `apiContract.test.ts` | 创建/搜索 FormData 字段（workflow 省略、重复 `tags`、Top-K 收敛）、浏览查询串、PATCH 只发变更字段、`null` 与省略的区别、retry 请求体 |
| `editDraft.test.ts` | 保留/人工字符串（含 `""`）/显式 `null` 三态、无改动不发请求、路线切换、纯文本不可清空 |
| `polling.test.ts` | 约 2 秒轮询、终态停止、隐藏取消/恢复、并发与取消失效后的旧响应隔离、StrictMode、失败退避、卸载停止 |
| `cache.test.ts` | 低 revision、旧更新时间、终态回退保护，以及高 revision 重新排队 |
| `navigation.test.tsx` | 页面/资源切换回页首与主内容焦点、同页更新不重置、skip link 保留 hash |
| `imagePicker.test.tsx` | 原生按钮与隐藏 input 分离，键盘激活、拖放/粘贴、重复选择、不提交表单 |
| `library.test.tsx` | 浏览/搜索参数互不混用、标签重复参数、Top-K、得分与模式提示、晚到响应不覆盖新结果、空结果与失败区分、共享刷新定时器与隐藏暂停、缓存命中复用与编辑后失效 |
| `detail.test.tsx` | 字段与警告渲染、纯文本无原件、仅 `failed` 可重试、409 保留草稿且只读不重发、三态字段提交、删除确认与本地失效、原件失败不影响文字详情 |
| `import.test.tsx` | 三类输入与路线参数、重复提示、校验失败保留草稿与预览、结果未知不重发、创建后轮询到 ready |
| `router.test.ts` | 哈希路由解析与 UUID 校验 |

## 已知限制（与后端进度一致）

- 卡片不加载缩略图：后端尚未提供 `thumbnail_url`（T06），首版只显示文字与类型占位，原件在详情页按需读取。
- “连接状态”页只提供说明与待接入标记：`GET /api/health`（T05）尚未实现，页面不虚构任何绿色状态，也不推断 Worker 心跳。
- 检索只有 `vector` 模式，结果无分页与 total；关键词辅助（T09）交付前不显示相关控件。
- 一次只创建一份资料；多选批量（T10）交付前不做顺序静默提交。
