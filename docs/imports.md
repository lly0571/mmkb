# 目录与批量导入

CLI 和 Web 共用逐项 HTTP 导入协议。客户端负责枚举、预览和上传队列，后端复用单份资源校验与创建逻辑；入库后由现有单 Worker 处理。每张图片独立成功或失败，整批不回滚。

## 先迁移，再启动

在项目根目录的 `mmkb` Conda 环境执行：

```bash
alembic upgrade head
python -m mmkb.storage init
python -m mmkb.server
```

`0002_import_items` 新增持久回执表。运行中的新版本要求数据库升级到当前 head，启动不会自动迁移。CLI 只需要 API 地址，不读取服务端数据库、存储或模型凭证；源目录位于执行 CLI 的主机，可以与 API 分处不同主机。

## CLI

```bash
# 预览：读取 API 的实际限制，生成清单，不创建资源
python -m mmkb.importer plan /path/to/images \
  --manifest /path/to/import-state.json \
  --api-url http://127.0.0.1:8010 --tag 项目资料

# 阅读清单后执行；默认两个并发上传
python -m mmkb.importer run /path/to/import-state.json

# 中断或网络结果未知后继续，先核查回执
python -m mmkb.importer resume /path/to/import-state.json

# 只重试失败项；修复损坏文件后也使用此命令
python -m mmkb.importer retry-failed /path/to/import-state.json

# 查询入库后的模型处理状态
python -m mmkb.importer status /path/to/import-state.json --watch --window 120
```

`plan` 默认不递归，`--recursive` 包含子目录；两种模式均跳过符号链接和特殊文件。普通隐藏文件也参与枚举。清单按相对路径排序，包含非图片、损坏、超限及读取失败项；格式按实际内容判定，PNG/JPEG/WebP 的扩展名可以不准确。源文件不会被修改。

| 选项 | 默认 | 行为 |
| --- | --- | --- |
| `--api-url` | `MMKB_API_URL` 或 `http://127.0.0.1:8010` | API origin，不携带凭证、路径或查询参数；恢复必须使用原 origin |
| `--tag` | 无 | 可重复，统一标签 |
| `--title-mode` | `stem` | `stem` 文件名去扩展名；`relative-path` 相对路径含扩展名；`empty` 空标题 |
| `--workflow` | `auto` | 自动选择，或当前支持的图片路线；以命令帮助和服务端返回为准 |
| `--duplicates` | `skip` | `skip` 跳过已有未删除内容；`create` 新建独立副本 |
| `--concurrency` | `2` | run/resume/retry-failed 可设为 1–8，不改变 Worker 并发 |

清单固定本次文件集合，续跑不会自动纳入后来新增的文件。请把清单放在源目录之外，以免下一次计划把清单本身列为非图片。已有清单不能被 `plan` 覆盖；同一清单禁止两个 CLI 进程同时执行。

每项提交标识及 `uploading` 状态在请求发出前通过原子替换和 fsync 保存。未知请求继续沿用原标识；相同内容在同一清单内顺序上传。已成功项不重发。`retry-failed` 对修复后的文件重新预检，内容变化时生成新标识并保留原尝试；先前回执仍为 `reserved` 时不会更换标识。正常续跑遇到源内容变化会单独失败，需显式执行失败项重试。

退出码：`0` 上传阶段无失败，`1` 有已知失败，`2` 命令、目录、API 或清单错误，`3` 仍有结果未知项或观察超时，`130` 用户中断。非图片及正常重复跳过不算失败。`status` 的模型处理失败单独统计，不计入“导入失败”。SIGINT 停止领取新项，已发请求结束并核查后保存清单。

## Web

在导入页切换到“批量 / 目录导入”，选择多个文件或本地目录，设置递归、统一标签、标题、路线和重复策略，再生成预览清单并开始上传。目录控件不可用时保留多选入口。Web 的文件签名、大小和 SHA-256 检查是预检，完整解码校验仍由服务端负责。

浏览器上传实际选中的文件，不把电脑上的目录字符串发给服务端读取。选择目录默认只取根目录直接文件；开启“包含子目录”后重新生成清单即可纳入嵌套文件。

IndexedDB 只保存清单元数据和结果，不缓存原图。刷新或重启后选择“恢复清单”，再“重选文件以续传”或“重选目录以续传”。内容不同或文件缺失会逐项提示；已成功项不用重传。旧清单保留在历史选择器中，并可导出 JSON 结果。CLI 与 Web 使用相同版本的条目结构，但 Web 导出不含 CLI 所需的绝对源根目录。

默认上传并发 2，可设 1–8。“暂停新上传”停止领取新文件，已发送请求继续核查。存储配额耗尽或同一清单被其他页面更新时暂停上传；重新恢复最新清单后再继续。内容散列使用随应用打包的 SHA-256 实现，兼容普通 HTTP 部署。

每项展示“分类结果”，取关联资源的实际 workflow；自动分类完成后随状态刷新，重复跳过项使用已有资源的分类。尚未分类、分类中、已指定但未上传、非图片和已删除项分别显示对应提示。旧清单恢复后会查询一次最新分类，不把本次请求的路线当作重复项的实际分类。

处理状态按资源 ID 去重统计。页面可见且仍有处理中或结果未知项时，整批共用约 5 秒轮询器，每次最多查询 100 个标识；故障退避至 30 秒，隐藏后暂停，重新可见时刷新。上传成功和模型处理成功分别显示。

## HTTP 契约

`GET /api/imports/options` 返回 `schema_version=1`、`formats`、`image_workflows`、`max_upload_bytes` 和 `max_text_chars`。

`POST /api/resources` 保留原有 multipart 字段，新增：

| 参数 | 规则 |
| --- | --- |
| `Idempotency-Key` 请求头 | UUID；批量客户端必须先持久保存，每项独立使用 |
| `duplicate_policy` 表单字段 | `skip/create`；后端缺省 `create`，批量客户端显式传 `skip` |
| `expected_sha256` 表单字段 | 图片预览的 64 位小写摘要，后端按实际字节核验 |

不传标识时，现有单份创建行为和响应字段保持兼容。使用批量选项必须带标识。新建返回 `201`；跳过或重放已完成请求返回 `200`。带标识的成功响应额外包含 `import_outcome=created/skipped` 与 `replayed`；`duplicate` 是原提交的重复结果，重放本身不改变它。

跳过只比较原件 SHA-256，与标题、标签和 workflow 无关，也包括已有的 pending/processing/failed 资源。它返回已有资源，不合并字段、不自动重试模型；有多个副本时按创建时间及 ID 选择。`create` 允许独立副本，因此 SHA-256 索引不是唯一索引。

`POST /api/imports/resolve` 是只读查询：

```json
{"keys":["c54952fb-2ffb-4ce2-93db-ea1185213895"]}
```

请求支持 1–100 个 UUID。返回 `items`，每项包含 `key/state/resource_id/outcome/duplicate/error/resource_status/resource_workflow/resource_error`。`resource_workflow` 是当前实际分类，无结果或已删除时为 null。`state` 为 `missing/reserved/completed/failed`；资源状态为四种处理状态、`deleted` 或 `null`。`missing` 也只能使用原标识重试，因为第一次请求可能尚未到达；`reserved` 可能是进行中或先前中断，同标识重试由后端互斥保护。接口不返回对象 key、存储配置或请求指纹。

关键错误：`409 idempotency_conflict` 表示同标识的内容或参数改变；`409 source_changed` 表示实际图片与预览不符；`409 import_busy` 表示同项或同内容仍在提交；`410 import_resource_deleted` 表示原提交关联资源已删除，旧标识不能重新创建。资源 retry 接口仍只负责已入库资源的模型处理重试。

## 失败与恢复边界

原件写入前，服务端回执保存预分配的资源 ID、profile 快照和对象 key；资源与成功回执同事务提交。相同内容的所有创建路径共享单文件事务锁，锁忙时返回可核查的错误；整批不持有一个大事务。

响应丢失或数据库提交结果未知时，先通过新连接核实，不能直接删除可能已提交的原件。失败或崩溃可能留下可定位的保留对象；后续同标识重试覆盖原位置，转为重复跳过时清理该位置。清理失败继续保留失败回执，供重试定位。profile 切换后恢复仍使用首次保留的位置。

回执不随资源物理删除，也不自动过期。备份恢复应把回执表与资源表一起纳入一致性备份；全库对账和备份演练由对应维护功能负责。

## 验证

```bash
python -m pytest -q tests/test_importer.py
# TEST_DATABASE_URL 必须指向独立 mmkb_test_* PostgreSQL/pgvector 数据库
python -m pytest -q tests/test_imports_integration.py
cd web
npm run test
npm run typecheck
npm run build
npm run lint
```

数据库用例覆盖并发、原件写入后崩溃、COMMIT 回执丢失、删除后的幂等记录、存储故障与清理重试。前端覆盖 IndexedDB 写入冲突、上传前持久化失败、刷新续传、暂停和结果未知的核查。
