# 资源管理

已实现三类资料导入、分页查询、详情、编辑、失败重试、原件代理读取及删除清理。HTTP 层使用 FastAPI，业务层直接使用 SQLAlchemy ORM。结构沿用现有三张表，无新增迁移。

本模块只将资料置为 `pending`，不调用编码或检索服务。独立运行的 [处理 Worker](processing.md) 负责推进到 `ready` 或 `failed`；未启动 Worker 时资料保持 `pending`。当前不提供 Web、缩略图、替换原图或删除恢复。

## 配置与启动

统一在 `mmkb` Conda 环境、项目根目录执行：

```bash
conda activate mmkb
python -m pip install -e '.[test]'
alembic upgrade head
```

环境变量优先于 `.env.local`。资源管理仅依赖数据库和存储配置，不要求模型服务配置。

优先使用 RustFS：在服务端配置 `STORAGE_BACKEND=s3`、`S3_ENDPOINT_URL`、`S3_BUCKET`；凭证使用 `AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY`，或由 `S3_CREDENTIAL_PROFILE` 指定 AWS credentials profile。默认使用 `path` addressing、`s3v4` 签名和 `us-east-1`。Bucket 须事先存在，应用不创建 Bucket，也不内置凭证。

Local 使用 `STORAGE_BACKEND=local` 和 `LOCAL_STORAGE_ROOT`。`STORAGE_PROFILE_NAME` 指定默认 profile；未设置时按后端使用 `local-default` 或 `s3-default`。

```bash
python -m mmkb.storage init
uvicorn mmkb.api:create_app --factory --host 127.0.0.1 --port 8000
```

初始化命令验证存储后创建或校验 profile。已有同名配置不一致时报错；切换存储位置应使用新的 profile 名称。旧资料始终使用其自身的 profile 和完整 key，配置切换只影响新资料。API 启动不自动修改 profile 或数据库结构。

浏览器打开 `http://127.0.0.1:8000/docs` 查看并试用完整接口。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `MAX_UPLOAD_BYTES` | `20971520` | 单张图片字节上限，20 MiB |
| `MAX_TEXT_CHARS` | `100000` | 标题、用户文字、Caption、OCR 各字段字符上限 |
| `S3_PREFIX` | 空 | 资源服务生成新文件 key 时使用的前缀；存储适配器不自动追加 |

图片支持 PNG、JPEG、WebP，按真实内容识别 MIME、格式和尺寸，不信任文件扩展名。原件字节保持不变；每份资料独立保存，重复文件允许导入并返回 `duplicate=true`。

## 接口

统一前缀 `/api/resources`。创建使用 `multipart/form-data`，纯文本也接受普通表单；编辑和重试使用 JSON。

| 方法与路径 | 输入与行为 |
| --- | --- |
| `POST /api/resources` | 可选 `file/title/user_text/workflow`，重复 `tags`；文件和非空文字至少提供一个；返回 `201` |
| `GET /api/resources` | `limit` 默认 20，最大 100；`offset` 默认 0；可选 `status/input_kind/tags`，多个标签按 AND 过滤 |
| `GET /api/resources/{id}` | 返回详情、状态、revision、解析字段、元数据及原件入口 |
| `PATCH /api/resources/{id}` | 必须携带 revision；可修改 `title/tags/user_text/caption/ocr_text/workflow` |
| `POST /api/resources/{id}/retry` | 必须携带 revision，只允许失败资料重新排队 |
| `DELETE /api/resources/{id}` | 幂等删除，返回 `204`；删除后立即禁止查询和原件访问 |
| `GET /api/resources/{id}/original` | 按资源自己的存储配置代理读取图片，无文件的纯文本返回 `404` |

创建、编辑、重试及详情返回同一组资料字段；创建另含 `duplicate`。列表返回 `items/total/limit/offset`，按创建时间和 ID 倒序排列。API 不返回内部文件 key、存储位置和凭证引用。

```bash
# 纯文本
curl -X POST http://127.0.0.1:8000/api/resources \
  --form-string 'title=读书笔记' --form-string 'user_text=待保存的文字' \
  --form-string 'tags=笔记'

# 图片和配文；去掉 user_text 即为仅图片
curl -X POST http://127.0.0.1:8000/api/resources \
  -F 'file=@sample.png' --form-string 'user_text=图片配文' \
  --form-string 'tags=图表' --form-string 'tags=项目资料'

# 使用真实 ID 和最新 revision 替换示例值
curl -X PATCH http://127.0.0.1:8000/api/resources/RESOURCE_ID \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"title":"更新标题","caption":"人工修正的描述"}'
```

纯文本路线固定为 `text`，图片与配文固定为 `text_image`。仅图片可选 `structured_image/visual_image/visdoc`，默认留空，等待处理模块判断。添加或删除图片配文会自动更新输入形式和路线；不兼容的显式 workflow 报错。

编辑会递增 revision、删除旧索引、清除处理错误并回到 `pending`；处理中也可编辑。Caption/OCR 未提交时保留；字符串表示修正值（包括空字符串），显式 `null` 表示清空。Worker 保留非 null 值，仅在当前路线需要时为 null 字段生成内容，并校验 revision、状态和删除标记后提交结果。视觉分析警告位于 `extra.processing.warnings`，不影响成功资料进入 `ready`。

版本冲突或不可重试状态返回 `409`；参数错误返回 `422`；内容超限 `413`；格式不支持 `415`；资料、原件不存在或已删除 `404`；数据库或存储不可用 `503`。错误包含 `code/message`，请求校验错误另含不带原始内容的字段信息。

## 删除与异常补偿

删除先在短事务内标记 `deleted_at`、递增 revision 并移除索引，再尝试删除原件。成功后物理删除资料；存储清理失败时保留软删除记录及错误，HTTP 删除仍返回 `204`。可重复执行删除，或用维护命令重试：

```bash
python -m mmkb.resources cleanup-deleted --limit 100
```

每批输出 `scanned/cleaned/failed`，有清理失败时退出码为 1。不存在的文件视为清理成功。运行中的处理 Worker 每轮也会复用此清理能力，最多扫描 100 条。

导入时先保存文件，再提交资源记录；数据库提交失败会补偿删除刚保存的文件。补偿再次失败会记录资源 ID 和错误类型，后续需要对账清理，本次不实现自动存储对账。

## 验证

```bash
# 存储契约与资源测试，数据库测试默认跳过
python -m pytest tests/test_storage.py tests/test_resources.py -q

# 仅使用独立、可清空的 mmkb_test_* 数据库
TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/mmkb_test_resources' \
  python -m pytest tests/test_resources.py -q
```

PostgreSQL 测试库需预先启用 pgvector。测试会通过 Alembic 升级，并清空该测试库的资源和 profile，拒绝使用名称不以 `mmkb_test_` 开头的库。真实 RustFS 验证方式见 [存储说明](storage.md)。

覆盖三类输入、三种图片格式、内容与尺寸校验、重复提示、并发编辑及 revision 冲突、旧索引移除、Caption/OCR 保留、重试状态限制、导入补偿、删除失败重试、切换 profile 后读取旧原件和响应流关闭。
