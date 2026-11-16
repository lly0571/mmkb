# 文件存储

存储基础模块提供同步 Local/S3 适配器、配置加载、工厂函数和 profile 初始化命令。两个后端使用相同的读写删除契约，S3 兼容性以 RustFS 上的真实集成测试为准。业务入口可以通过 `StorageResolver` 调用存储，缩略图生成、自动对账、分片上传和版本清理另行实现。

## 配置与初始化

所有命令在项目根目录的 `mmkb` Conda 环境中执行。先通过 Alembic 应用迁移，再配置并初始化存储：

```bash
conda activate mmkb
python -m pip install -e '.[test]'
alembic upgrade head
python -m mmkb.storage init
```

`load_storage_config()` 独立于数据库和模型配置。进程环境变量优先于项目根目录的 `.env.local`；只有初始化命令额外要求 `DATABASE_URL`。

| 配置 | 说明 |
| --- | --- |
| `STORAGE_BACKEND` | `local`（默认）或 `s3`，用于初始化 profile |
| `STORAGE_PROFILE_NAME` | 留空时使用 `local-default` 或 `s3-default`；资源入口据此选择新资料使用的 profile |
| `LOCAL_STORAGE_ROOT` | 默认 `./data/assets`；相对项目根目录解析，初始化时以绝对路径入库 |
| `S3_ENDPOINT_URL` | S3 兼容服务的 HTTP(S) endpoint；留空使用 SDK 标准 endpoint |
| `S3_BUCKET` | S3 初始化必填；须提前创建，且从未启用版本控制 |
| `S3_CREDENTIAL_PROFILE` | Boto3 命名凭证 profile，保存为 `credential_ref`；留空使用 SDK 默认凭证链 |
| `S3_REGION` | 默认 `us-east-1` |
| `S3_ADDRESSING_STYLE` | 默认 `path`；允许 `auto / path / virtual` |
| `S3_SIGNATURE_VERSION` | 首版固定 `s3v4` |

初始化先验证目录可写或 Bucket 可访问，再开启短数据库事务。成功输出 profile ID、名称和后端；同名且配置一致时复用，配置不同则退出并提示使用新名称。命令不执行迁移、不创建 Bucket、不覆盖旧 profile。数据库写入失败时，本地已创建的空目录可能保留。

S3 凭证保存在服务端 AWS 凭证配置中，或通过 SDK 支持的环境变量、实例身份提供。API 与 Worker 都须能解析数据库保存的凭证引用；缺失的命名 profile 会报错，不切换到默认凭证。密钥不写入数据库或应用响应。客户端使用 5 秒连接超时、30 秒读取超时，标准重试模式最多执行 3 次总尝试。

新资料按默认名称选择 profile 并记录其 ID。旧资料始终使用自己的 profile，读取根目录、endpoint、Bucket 与凭证引用；切换环境中的默认 profile 只影响新导入。API 与 Worker 统一使用 region、addressing style 和签名配置，本地后端须共享同一份目录。

旧配置若使用 `STORAGE_PROFILE_NAME=default`，继续显式保留该名称。`S3_PREFIX` 从公开模板移除；业务调用方如需前缀，应在生成 key 时写入完整路径。存储适配器不拼接前缀，读取旧 key 不依赖当前前缀配置。

## 接口与调用

| 接口 | 返回 | 行为 |
| --- | --- | --- |
| `put(key, data, content_type=...)` | `PutResult(size, sha256)` | 完整覆盖同 key 内容，返回大小与客户端计算的 SHA-256 |
| `open(key)` | 支持 `read/close/with` 的二进制读取流 | 不保证随机定位，调用方须关闭 |
| `stat(key)` | `FileStat(size)` | 只查询大小，不下载内容或重新计算哈希 |
| `delete(key)` | 无 | 只删除指定对象，重复删除成功 |

`put` 主要接收 `bytes`，也兼容现有资源模块传入的二进制流：从当前位置读到末尾，不定位、不关闭输入。上传内容整体进入内存，业务调用方负责限制上传大小。

```python
from mmkb.config import load_storage_config
from mmkb.storage import create_storage

# profile 为调用方从数据库读取的 StorageProfile。
storage = create_storage(profile, load_storage_config())
try:
    result = storage.put("resources/example/original.png", image_bytes,
                         content_type="image/png")
    print(result.size, result.sha256)
    print(storage.stat("resources/example/original.png").size)
    with storage.open("resources/example/original.png") as stream:
        content = stream.read()
    storage.delete("resources/example/original.png")
finally:
    storage.close()
```

`create_storage()` 不查询数据库或修改 profile。应用入口可以使用 `StorageResolver` 复用适配器，并在关闭时调用 `resolver.close()` 释放 S3 客户端。

所有 key 都是完整相对路径，支持中文和空格；拒绝绝对路径、反斜杠、冒号、NUL，以及空、`.`、`..` 路径段。Local 还拒绝 key 中的符号链接和根目录越界，写入使用同目录临时文件原子替换，失败时清理临时文件。根目录须由应用管理，避免其他进程并发修改路径结构。

存储异常继承 `StorageError`。非法 key 抛出 `InvalidKeyError`，无效配置抛出 `StorageConfigError`，缺失对象的 `open/stat` 抛出 `ObjectNotFoundError`（兼容旧名称 `ObjectNotFound`）。权限错误、缺失 Bucket、连接失败和读流失败均作为存储故障报告。S3 对象请求返回含糊的 404 时，会检查 Bucket 是否可访问；不能据此确认缺失对象时继续报告故障。

## 业务接入约定

资源管理负责校验文件类型和大小、生成 key、控制资源状态和数据库事务。新资料使用服务端生成的资源 ID，例如 `resources/{uuid}/original.jpg`；原文件名只作为展示元数据。纯文本不调用存储。

文件先保存，再提交资源记录；返回的 SHA-256 写入 `Resource.sha256`，大小和校验后的 MIME 写入 `Resource.extra`。数据库提交失败时按已知 key 补偿删除；S3 超时可能发生在服务端已写入之后，失败路径同样需要清理。补偿失败须记录资料 ID 和错误类型，留待后续对账。

原件读取前由资源管理检查删除状态，再按资料自己的 profile 打开流。删除原件与已知缓存全部成功后才物理删除资料，清理失败保留记录供重试。未来缩略图使用固定 key 缓存并复用本模块接口，不增加派生文件表。

## 验证

默认测试不连接应用数据库或远程 S3，使用临时目录、独立内存数据库和 Botocore Stubber：

```bash
python -m pytest tests/test_storage.py tests/test_storage_config.py -q
```

真实集成测试只读取显式的测试配置，不回退到应用数据库、endpoint 或 Bucket：

| 测试配置 | 用途 |
| --- | --- |
| `MMKB_TEST_DATABASE_URL` | 已应用迁移的独立 PostgreSQL 测试库 |
| `MMKB_TEST_S3_ENDPOINT_URL`、`MMKB_TEST_S3_BUCKET` | RustFS/S3 测试服务及独立测试 Bucket |
| `MMKB_TEST_S3_CREDENTIAL_PROFILE` | 可选测试凭证 profile；留空使用 SDK 默认凭证链 |
| `MMKB_TEST_S3_REGION` | 可选，默认 `us-east-1` |

```bash
python -m pytest tests/test_storage_integration.py -q -ra
python -m pytest -q -ra
```

缺失测试配置时显示跳过原因。S3 集成测试先确认 Bucket 从未启用版本控制，只写入随机 UUID 前缀，并清理本次创建的 key；PostgreSQL 集成测试仅创建和删除随机命名的 profile。测试覆盖写入读回、大小与 SHA-256、覆盖写、流关闭、重复删除、路径边界、错误映射、初始化复用与冲突，以及默认配置切换后的旧 profile 访问。

SDK 模拟通过不能代替 RustFS 兼容性验收，目标服务上的真实契约测试通过后才能确认该部署可用。
