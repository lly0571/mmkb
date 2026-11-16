# MMKB

多模态知识库的 Python 基础项目，使用 PostgreSQL、pgvector、SQLAlchemy ORM 和 Alembic 管理资料与检索数据。

目前提供数据库模型、初始迁移、配置加载器和模型适配客户端。模型适配支持文本、图片、图文 Embedding，以及可选视觉服务的 Caption、OCR 和图片分类。

## 数据结构

| 表 | 职责 |
| --- | --- |
| `resources` | 图片、图文和文本资料的元数据、文件位置与处理状态 |
| `resource_indexes` | 每份资料的 2048 维半精度向量和辅助文本检索字段 |
| `storage_profiles` | 本地目录或 S3 兼容存储的公共配置 |

详细字段和迁移约定见 [Schema 说明](docs/schema.md)，模型定义见 [models.py](mmkb/db/models.py)。

## 检索服务

`mmkb.retrieval.search()` 已支持文本、图片字节与图文联合查询、标签 AND 过滤、cosine Top-K 排序及结果摘要。编码器由调用方注入，默认 Top-K 为 20；共享的 `segment_text()` 提供 Jieba 精确分词。调用示例、编码协议、异常约定及隔离数据库测试命令见 [检索服务说明](docs/retrieval.md)。

当前检索未接入 HTTP 搜索接口、真实模型客户端桥接或关键词辅助。数据库结构沿用现有迁移，测试使用事务内的精确扫描。

## 文件存储

文件存储已提供同步 Local/S3 适配器、读写删除接口和显式 profile 初始化入口。先应用迁移并配置存储，再运行 `python -m mmkb.storage init`；配置、调用方式和隔离集成测试见 [文件存储说明](docs/storage.md)。存储配置独立加载，不要求模型服务配置存在。

## 开发环境

需要 Python 3.11 及以上版本，以及已经创建的 PostgreSQL 数据库。目标数据库须预先在 `public` schema 中启用 pgvector 扩展。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

首次配置时，将 `.env.example` 复制为 `.env.local`，填写自己的配置：

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | Alembic 使用的 PostgreSQL 连接字符串 |
| `EMBEDDING_BASE_URL` | 自行部署的 Embedding API 基址 |
| `EMBEDDING_MODEL` | API 实际提供的模型 ID |
| `EMBEDDING_DIMENSIONS` | 固定为 `2048`，与当前 Schema 一致 |

模型地址和模型 ID 没有内置部署默认值。加载模型或检索配置时必须提供 Embedding 地址和模型；操作系统环境变量优先于 `.env.local`。

`.env.local`、本地数据及内部文档目录均被 Git 忽略。公开模板不包含可用的服务地址或凭证。

## 模型适配

Embedding 客户端面向支持 `messages` 多模态扩展的 vLLM Qwen3-VL Embedding 服务；图文使用一次原生联合编码。视觉客户端使用兼容 Chat Completions 的图像模型服务，发送 `chat_template_kwargs.enable_thinking=false` 关闭 Qwen 思考模式，三项能力共用 `CAPTION_BASE_URL` 与 `CAPTION_MODEL`。两个变量均为空时关闭视觉能力，仅填写其中一项会报配置错误。OCR 首版只提取全文，不提供坐标、版面结构或置信度。

基址填写 API 根路径（通常以 `/v1` 结尾），客户端追加 `embeddings`、`chat/completions` 或 `models`。可选的 `EMBEDDING_API_KEY`、`CAPTION_API_KEY` 通过 Bearer 认证发送；为空时不发送认证头。`EMBEDDING_TIMEOUT_SECONDS` 默认 60，`CAPTION_TIMEOUT_SECONDS` 默认 120，表示网络读写与连接池等待超时；连接超时固定为 5 秒，不自动重试。

```python
from pathlib import Path
from mmkb.providers import ImageInput, ProviderError, build_providers

with build_providers() as providers:
    query = providers.embedding.embed(purpose="query", text="查询文字")
    image = ImageInput(Path("sample.png").read_bytes(), "image/png")
    document = providers.embedding.embed(
        purpose="document", image=image, text="图片配文",
    )
    if providers.vision is not None:
        caption = providers.vision.caption(image, structured=True)
        ocr_text = providers.vision.ocr(image)
        image_kind = providers.vision.classify(image)
```

`purpose` 必填，查询与资料共用预处理和模型；调整提示词或更换模型后需要重建已有向量。图片由调用方读取为字节，MIME 支持 `image/png`、`image/jpeg`、`image/webp`，适配器将原始字节编码为 data URL，解码和模型预处理由服务端完成。向量必须为 2048 维有限数值且非零，返回前完成 L2 归一化。输入超限或视觉输出截断会明确报错，不自动裁剪或分块。

调用失败抛出 `ProviderError`，通过 `capability`、`code`、`status_code` 判断原因；错误信息不含原始请求或服务响应正文。常见错误码包括 `invalid_input`、`input_too_large`、`timeout`、`connection_error`、`http_error`、`invalid_response`、`output_truncated`、`model_not_found`。未配置视觉服务时 `providers.vision` 为 `None`；处理模块负责将可选能力失败转为警告或回退整图编码。`check_connection()` 仅检查服务模型列表，不代表推理或检索质量验收通过。

默认离线测试使用 HTTPX MockTransport，不访问模型服务或数据库：

```bash
python -m unittest discover -s tests -p 'test_model_*.py' -v
```

显式启用真实服务测试（会发送短文本和生成的测试图片）：

```bash
RUN_MODEL_INTEGRATION=1 python -m unittest discover -s tests -p 'test_model_services.py' -v
```

可额外设置 `MODEL_TEST_PHOTO`、`MODEL_TEST_CHART`、`MODEL_TEST_OCR` 为本地 PNG 样本路径，验证普通图片、图表和中英混合截图；`MODEL_TEST_OCR_EXPECTED` 可指定必须识别到的文字片段。测试结果应区分协议通过与真实资料上的质量表现。

协议依据：[vLLM 多模态 Embedding](https://docs.vllm.ai/en/stable/models/pooling_models/embed/)、[Qwen3-VL Embedding 示例](https://github.com/QwenLM/Qwen3-VL-Embedding/blob/main/examples/embedding_vllm.ipynb)、[vLLM 思考模式参数](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)。

## 数据库迁移

初始化和后续升级都通过 Alembic 执行，命令会修改 `DATABASE_URL` 指定的数据库：

```bash
alembic upgrade head
```

修改 ORM 后，在已应用当前迁移的开发数据库上生成候选变更，检查生成的 Python 迁移后再应用：

```bash
alembic revision --autogenerate -m "describe_change"
alembic upgrade head
```

仓库仅维护 ORM 与 Python 迁移文件，不维护独立 SQL 文件。已应用的历史迁移不应随当前 ORM 重新生成。
