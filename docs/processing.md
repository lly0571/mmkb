# 资料处理与 Worker

后端处理已接通导入、索引和搜索：资源 API 返回 `pending`，独立单 Worker 推进到 `processing → ready`，必要处理失败进入 `failed`。详情与列表接口直接返回状态，无任务 ID。结构沿用现有三张表，无新增迁移。

## 启动与恢复

在项目根目录激活 `mmkb` Conda 环境，先完成 Alembic 迁移、pgvector 启用和存储 profile 初始化，具体步骤见 [README](../README.md)。API 与 Worker 使用相同的数据库、资料存储和 Embedding 配置；配置优先级为进程环境高于 `.env.local`。

```bash
conda activate mmkb
python -m mmkb.worker run
```

运行命令可加 `--poll-seconds 2` 调整无待处理资料时的等待间隔，必须是有限正数。有待处理资料时逐份连续处理；每轮还清理最多 100 条软删除资料。单份资料失败不会终止常驻进程。

```bash
python -m mmkb.worker run --once
```

单次模式至多领取一份资料，并执行一批删除清理，输出 JSON `claimed/outcome/cleanup`。`outcome` 为 `idle/ready/failed/stale`；资料失败或删除清理失败时退出码为 1。`stale` 表示用户已编辑、重试、恢复或删除，旧结果被丢弃。

每个数据库使用会话级 advisory lock 保证一个 Worker，固定键为 `(0x4D4D4B42, 1)`，恢复命令也使用同一把锁。另一个实例占用时立即报 `worker_running`。持锁连接独立保留，业务事务仍然短暂；使用直接数据库连接或保持会话绑定的连接池。持锁连接丢失、数据库读取或提交失败时停止进程，不自动重连继续处理。

SIGINT/SIGTERM 会停止后续领取，等待当前资料及本轮清理完成，再释放客户端、数据库连接和锁。强制结束、断电或数据库故障可能留下 `processing`：

```bash
# 先确认旧进程已经停止，再执行
python -m mmkb.worker recover-processing
python -m mmkb.worker run
```

恢复不需要模型配置，不调用模型；将未删除的 `processing` 资料递增 revision、移除旧索引、清除错误并置为 `pending`，输出 `{"requeued": 数量}`。客户端应重新读取 revision。恢复不会清空 Caption/OCR，也不会改动 `failed` 资料；失败资料通过资源 API 的 retry 接口重试。启动不自动恢复，不根据任务耗时回收任务。

## 五条路线

| 路线 | Embedding 输入 | 自动视觉调用 |
| --- | --- | --- |
| `structured_image` | 原图与非空 Caption 原生联合编码；无有效描述时为原图 | 结构化 Caption |
| `visual_image` | 原图 | 普通 Caption，仅用于展示和分词 |
| `visdoc` | 原图 | OCR 全文，仅用于展示和分词 |
| `text_image` | 原图与完整用户配文原生联合编码 | 无 |
| `text` | 完整用户文字 | 无 |

仅图片输入且路线为空时调用分类，分类失败或未配置视觉服务时回退 `visual_image`。已指定路线或上一次保存的路线直接复用；可用 `PATCH workflow=null` 让仅图片资料重新分类。OCR 与 Caption 分别保存，均不覆盖用户文字。

Caption/OCR 为非 null 时始终保留，包括人工设置的空字符串。只有字段为 null 且当前路线需要时才调用视觉服务；图文路线即使清空这两项，也不会自动分析。生成文字超过 `MAX_TEXT_CHARS` 时丢弃并记录警告，不裁剪后保存。

视觉能力缺失、超时、输出截断或非法响应均可降级，警告保存为 `extra.processing.warnings` 中的 `{stage, code}`。每次成功提交替换该警告列表，并保留其他元数据。Embedding、原件读取或必要输入校验失败会进入 `failed`；错误只包含安全错误类型和处理建议，不包含原始内容、服务响应、地址或凭证。

每份资料只保存一条 2048 维有限、非零、归一化向量；不自动分块。标题、标签、用户文字、OCR、Caption 按顺序组合后调用共享 Jieba 分词函数，数据库生成 `search_tsv`；当前检索仍为纯向量模式。

## 事务与版本

1. 领取按创建时间及 ID 排序，在短事务中锁定未删除 `pending` 资料，复制快照并提交 `processing`。
2. 原件按资料自己的 storage profile 读取并关闭，模型调用和分词全部在业务事务外完成。
3. 提交重新锁定资料，确认 ID、revision、`processing` 状态和未删除条件仍匹配，才原子写入解析字段、索引和 `ready`。
4. 失败写入遵循同样的版本条件。编辑、删除或恢复后的旧成功和旧失败均被丢弃；数据库提交异常回滚整次提交并停止进程，保留待恢复的状态。

日志记录资源 ID、revision、阶段、耗时和错误类型。运维需同时检查 Worker 进程状态、处理错误及删除清理警告。

## 验证

```bash
python -m pytest tests/test_processing.py tests/test_worker.py tests/test_search_api.py -q

# 仅指向可清空的、预先启用 pgvector 的 mmkb_test_* 测试库
TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/mmkb_test_worker' \
  python -m pytest tests/test_worker_integration.py -q
```

Worker 集成测试通过 Alembic 升级，并清空测试库的资源和 storage profile，拒绝其他数据库名。覆盖五条路线的导入、三类查询、原件和删除闭环，以及人工字段保留、旧结果隔离、事务外模型调用、索引提交回滚、单实例锁、锁会话终止、恢复、清理重试和 CLI。

真实模型测试仍需显式设置 `RUN_MODEL_INTEGRATION=1`，RustFS/S3 契约需独立测试配置，分别见 [README](../README.md) 和 [存储说明](storage.md)。模拟模型端到端测试验证业务协议与状态，不代表真实素材上的召回质量已验收。
