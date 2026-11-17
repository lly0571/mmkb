# 连接状态 API

`GET /api/health` 为 Web「连接状态」页和运维人员提供一份只读的依赖快照：API 自身、PostgreSQL、Embedding、当前存储 profile、可选视觉模型、可选决策模型与 Worker。它回答「本轮检查到底观察了什么、结论能推到哪一步」，不承担健康探针编排、不代替业务接口、也不修复任何问题。

实现分布在 `mmkb/health_contract.py`（词表与聚合）、`mmkb/health_probes.py`（探测）、`mmkb/health_runner.py`（子进程调度）、`mmkb/health.py`（缓存与报告）、`mmkb/schema_contract.py`（随包安装的结构预期）。

## 1. 边界

- 只读：不建表、不迁移、不初始化 storage profile、不创建目录、不写对象、不改任何业务状态。
- 模型检查只请求 `GET {BASE_URL}/models`，从不发起推理，因此不证明生成质量、Embedding 维度一致性或检索效果。
- 存储只检查 `STORAGE_PROFILE_NAME` 指向的当前 profile，不检查历史 profile，也不读写对象。
- Worker 尚无心跳，固定返回「未监测」，页面与接口都不得据此推断队列深度或处理能力。
- 探测失败按证据分级：检查未跑完记为 `unknown`，不写成「服务离线」。
- 报告不含服务地址、模型 ID、API Key、Bucket 名、profile 名、对象 key、本地路径、数据库 DSN 或远端错误正文；文案由错误码固定，探测进程无法自由输出。
- 依赖故障仍返回 `200` 与完整报告；只有 API 自身逻辑异常才返回 `5xx`。响应带 `Cache-Control: no-store`。

## 2. 请求与响应

无参数、无请求体、不分页。首版沿用单用户部署，与其余 `/api/*` 一致不鉴权，因此响应只应在受信网络内暴露。

```bash
curl -s http://127.0.0.1:8010/api/health
```

```json
{
  "schema_version": 1,
  "status": "healthy",
  "checked_at": "2026-11-17T09:39:02Z",
  "duration_ms": 412,
  "cached": false,
  "age_ms": 0,
  "ttl_ms": 10000,
  "checks": {
    "api": {"state": "connected", "required": true, "code": "api_reachable",
            "message": "API 已响应", "latency_ms": null, "scope": "http_response"},
    "database": {"state": "connected", "required": true, "code": "database_ready",
                 "message": "连接与预期结构检查通过", "latency_ms": 23, "scope": "connection_and_schema"},
    "embedding": {"state": "connected", "required": true, "code": "model_list_verified",
                  "message": "配置模型已在服务列表中发现，未执行推理", "latency_ms": 31, "scope": "model_list"},
    "storage": {"state": "connected", "required": true, "code": "bucket_accessible",
                "message": "当前 S3 Bucket 检查通过，未验证对象读写", "latency_ms": 45,
                "scope": "bucket_head", "backend": "s3"},
    "vision": {"state": "not_configured", "required": false, "code": "optional_not_configured",
               "message": "未配置该可选服务", "latency_ms": null, "scope": "configuration"},
    "decision": {"state": "disabled", "required": false, "code": "service_disabled",
                 "message": "该项已显式禁用", "latency_ms": null, "scope": "configuration"},
    "worker": {"state": "unknown", "required": false, "code": "heartbeat_not_implemented",
               "message": "尚无 Worker 心跳，无法判断是否运行", "latency_ms": null, "scope": "not_monitored"}
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 报告契约版本，当前固定 `1`；前端遇到未知版本直接判为契约不符 |
| `status` | `healthy`、`degraded`、`unavailable`、`unknown` 四值汇总 |
| `checked_at` | 本轮探测完成时刻（UTC，秒级） |
| `duration_ms` | 生成该快照的探测耗时；命中缓存时保留原值 |
| `cached` / `age_ms` / `ttl_ms` | 是否复用上一轮结果、该结果已存在多久、缓存有效期 |
| `checks.<key>.state` | `connected`、`down`、`not_configured`、`disabled`、`unknown` |
| `checks.<key>.required` | 完整导入与检索闭环是否依赖该项；`required` 不等于「资源管理 API 启动必需」 |
| `checks.<key>.code` | 固定错误码，决定 `state`、`scope` 与文案，前后端据此给下一步建议 |
| `checks.<key>.scope` | 本轮实际观察范围，例如 `model_list`、`bucket_head`、`not_monitored` |
| `checks.storage.backend` | 仅存储项返回 `local` 或 `s3`，其他项不得出现该字段 |

检查项固定七项，缺一即视为契约不符。`api` 与 `worker` 由 API 进程给出，其余五项来自探测进程。

### 汇总规则

按优先级取第一个命中：

1. `api`、`database`、`embedding`、`storage` 任一项为 `down`、`not_configured` 或 `disabled` → `unavailable`（必需项被关闭同样算不可用）。
2. 上述四项任一为 `unknown` → `unknown`。
3. `vision`、`decision` 为 `down`、`unknown`，或因显式开启而缺配置 → `degraded`；未配置的可选服务不降级。
4. 其余情况 → `healthy`。

`worker` 为 `unknown` 不影响汇总，因为它从未被观察。

## 3. 每项检查实际证明什么

| 检查项 | 必需 | 本轮执行 | 不证明 |
| --- | --- | --- | --- |
| `api` | 是 | 请求已被 FastAPI 处理并准备返回 | Worker、队列、依赖可用性 |
| `database` | 是 | 单条短连接内执行 `SELECT 1`、读取迁移版本、零行读取关键表、检查 pgvector 与 `halfvec(2048)` 列类型 | 数据量、索引性能、资源内容正确性 |
| `embedding` | 是 | `GET /models` 中是否包含配置的模型 | 推理可用、向量维度与质量、检索效果 |
| `storage` | 是 | 读取当前 profile 记录并比对配置；`local` 打开目录，`s3` 执行 HeadBucket | 对象读写权限、磁盘余量、存量文件完整性、历史 profile |
| `vision`（读 `CAPTION_*`） | 否 | 同 `embedding` 的模型列表检查 | Caption/OCR 质量与耗时 |
| `decision`（读 `DECISION_*`） | 否 | 同 `embedding` 的模型列表检查 | 是否实际执行决策路由；实际调用由输入与服务端路由配置决定 |
| `worker` | 否 | 无 | 一切 |

### 数据库结构预期

`mmkb/schema_contract.py` 保存随包安装的迁移历史与关键结构：迁移版本表 `public.mmkb_alembic_version`、必需表、pgvector 扩展以及 `mmkb.resource_indexes.embedding` 的 `halfvec(2048)` 类型。版本比较只按契约内的显式历史排序，不做字符串或数字比较：

- 版本表缺失或为空 → `schema_missing`（提示执行 `alembic upgrade head`）。
- 版本属于已知历史但不是头版本 → `schema_outdated`。
- 多个版本行、未知版本或关键结构不符 → `schema_incompatible`。

数据库错误优先按 SQLSTATE 分类；驱动在连接握手阶段抛出的错误可能不带 SQLSTATE（psycopg2 的密码错误即如此），此时只按 libpq 的稳定措辞判定认证失败或权限不足。原始错误文本只用于分类，不进入响应，也不写进日志。

新增迁移时同步更新该契约，`tests/test_health.py` 会在契约与仓库迁移头不一致时失败。检查只读，不会自动迁移。

## 4. 错误码与建议动作

| 检查项 | 错误码 | 含义 |
| --- | --- | --- |
| `database` | `database_unreachable` / `database_auth_failed` / `database_permission_denied` | 连接、认证或权限不足 |
| `database` | `schema_missing` / `schema_outdated` / `schema_incompatible` | 未迁移、落后或与当前应用不兼容 |
| `embedding` / `vision` / `decision` | `model_unreachable` / `model_timeout` / `model_auth_failed` / `model_http_error` / `model_invalid_response` / `model_not_found` | 连不上、超时、认证失败、拒绝列表检查、响应格式异常、模型 ID 不在列表 |
| `storage` | `profile_not_initialized` | 服务端没有该 profile 记录，需显式 `python -m mmkb.storage init` |
| `storage` | `profile_conflict` | 已登记 profile 与当前配置不一致，应改用新的 `STORAGE_PROFILE_NAME` |
| `storage` | `local_root_unavailable` / `bucket_unavailable` / `storage_access_denied` / `storage_timeout` | 目录不可用、Bucket 不可达、凭证或权限被拒、超时 |
| `storage` | `dependency_unavailable` | 数据库未就绪，本轮无法确定当前 profile |
| 配置类 | `required_not_configured` / `optional_not_configured` / `invalid_config` / `service_disabled` | 缺配置、可选未配置、配置非法、显式禁用 |
| 调度类 | `check_deadline_exceeded` / `probe_start_failed` / `probe_protocol_error` | 本轮没跑完、探测进程无法启动或上一轮未回收、结果不完整；一律 `unknown` |

`database`、`storage` 与模型检查各自只允许上表内的错误码，探测无法跨界报错；`storage` 的 `scope` 跟随实际后端在 `local_directory` 与 `bucket_head` 间切换。

## 5. 时间预算、缓存与进程隔离

预算在代码内固定（`HealthBudgets`），不新增环境变量，测试通过注入缩短它：

| 预算 | 默认值 | 说明 |
| --- | --- | --- |
| `total_seconds` | 5s | 一整轮探测的硬期限 |
| `model_seconds` | 2s | 单个模型列表检查总时限，包含客户端创建、DNS、响应读取与解析；另分连接 0.5s、读取 1s、取连接 0.2s |
| `database_seconds` | 2s | 数据库组件总时限，包含引擎创建、连接、全部结构/profile 查询及清理；另有连接 1s、单 SQL 500ms、锁等待 200ms、事务空闲 2s 限制 |
| `storage_seconds` | 2s | 获得数据库结果后开始，限制配置比对、目录访问或 S3 凭证发现/客户端创建/HeadBucket/清理的总耗时；另有连接 0.5s、读取 1s 限制 |
| `cleanup_seconds` | 0.5s | 一轮进程退出、terminate 与 kill 共用的额外清理预算，不按步骤重复增加 |
| `ttl_seconds` | 10s | 报告缓存有效期 |
| `model_response_limit_bytes` | 256KB | 模型列表响应上限，超出即截断并判为 `model_invalid_response` |

同一进程内并发请求共享一轮探测，后到者等待同一结果；`ttl_seconds` 内直接复用报告并置 `cached=true` 与 `age_ms`，页面必须显示「使用最近检查结果（N 秒前）」。

数据库、模型与存储探测在独立子进程（`python -m mmkb.health_probes`）中运行：父进程经 stdin 传入配置，子进程经 stdout 逐帧返回结果，stderr 丢弃。整轮硬期限从启动子进程前开始计时；超过期限先 `terminate` 再 `kill` 并回收，父进程关闭时同样处理。

子进程内每个同步组件使用独立的守护线程执行，调度线程只等待该组件预算；到期即报告 `unknown / check_deadline_exceeded` 并丢弃晚到结果。数据库到期后不会继续检查存储，而是返回 `dependency_unavailable`；其余模型仍独立检查。守护线程无法中断正在进行的底层调用，剩余操作随一次性探测子进程退出结束，因此这些探测函数不能放在常驻 API 或 Worker 进程里执行。驱动明确返回的网络超时仍使用对应的故障错误码，预算耗尽则只表示本轮无法完成观察。

## 6. 配置项

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 无 / 无 / 空 | 必需项，无 `EMBEDDING_ENABLED` 开关，缺配置即 `required_not_configured` |
| `CAPTION_*` + `CAPTION_ENABLED` | 关闭 | `vision` 检查项；留空且未显式开启为「未配置」，`false` 为「已禁用」，`true` 则要求配齐 |
| `DECISION_*` + `DECISION_ENABLED` | 关闭 | `decision` 检查项，规则同上 |
| `STORAGE_BACKEND` / `STORAGE_PROFILE_NAME` | `local` / 空 | 决定检查哪个 profile；空白时按 `local-default` 或 `s3-default` |
| `LOCAL_STORAGE_ROOT` / `S3_ENDPOINT_URL` / `S3_BUCKET` 等 | 见 `.env.example` | 仅用于比对当前 profile，值本身不出现在报告里 |

配置在进程启动时解析一次，改环境需重启后端；单个组件配置非法只影响它自己那一项，其余检查照常返回。

数据库与存储设置取自当前应用实例的配置：`create_app` 传入 `AppConfig` 时，`/api/health` 探测的就是这套数据库与存储 profile，不会退回进程环境变量去探测另一套部署；模型服务不属于 `AppConfig`，仍按环境变量与 `.env.local` 解析，与 Worker 加载模型的来源一致。

## 7. 前端展示

检查范围只描述本轮看了什么，不预判结果：`数据库连接`、`模型列表接口`、`S3 Bucket` 等中性名称在成功与失败下都成立，成功结论只出现在检查通过的边界说明里。

`#/connections` 进入时发起一次检查，「重新检查」按钮手动重跑，不轮询、不自动重试；浏览器等待上限 7 秒，超时报「等待健康检查超时」。检查中禁用按钮、保留上一次报告并标注「正在重新检查」；离开页面会中止请求，晚到响应按请求序号丢弃。

响应需严格满足契约：HTML、未知 `schema_version`、缺字段、类型错误或 `latency_ms` 非整数一律判为契约不符，页面显示失败而不会猜测为绿色。状态词固定区分「检查通过」「检查失败」「未配置」「已禁用」「无法判断」「未监测」，每项附 `scope`、耗时、边界说明与建议动作。刷新失败时保留旧报告并明确标注「历史结果，未获最新状态」。

## 8. 测试

- 离线：`tests/test_health_config.py`（配置解析与开关语义）、`tests/test_health.py`（聚合、缓存、期限、子进程回收）、`tests/test_health_probes.py`（错误码分类、只读语义）、`tests/test_health_runner.py`（IPC、超期、僵尸进程）、`tests/test_health_api.py`（HTTP 契约、`no-store`、依赖全红仍 `200`）。
- 集成（`integration` 标记，需 `MMKB_TEST_DATABASE_URL` 指向可丢弃的 `mmkb_test_*` 库）：`tests/test_health_integration.py` 覆盖真实驱动错误、迁移版本表、目录自省、权限与 HeadBucket；S3 部分需 `MMKB_TEST_S3_ENDPOINT_URL` 与 `MMKB_TEST_S3_BUCKET`。
- 前端：`web/src/tests/health.test.ts`（解析与边界文案）、`web/src/tests/connections.test.tsx`（一次请求、按钮锁、晚到响应、契约拒绝、失败保留旧报告）。
