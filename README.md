# MMKB

多模态知识库的 Python 基础项目，使用 PostgreSQL、pgvector、SQLAlchemy ORM 和 Alembic 管理资料与检索数据。

目前提供数据库模型、初始迁移和检索配置加载器。导入、模型调用、检索 API、存储适配器和 Web 界面尚未实现。

## 数据结构

| 表 | 职责 |
| --- | --- |
| `resources` | 图片、图文和文本资料的元数据、文件位置与处理状态 |
| `resource_indexes` | 每份资料的 2048 维半精度向量和辅助文本检索字段 |
| `storage_profiles` | 本地目录或 S3 兼容存储的公共配置 |

详细字段和迁移约定见 [Schema 说明](docs/schema.md)，模型定义见 [models.py](mmkb/db/models.py)。

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

模型地址和模型 ID 没有内置部署默认值。加载检索配置时必须提供它们；操作系统环境变量优先于 `.env.local`。Caption 与存储变量目前是后续 Provider 的配置约定。

`.env.local`、本地数据及内部文档目录均被 Git 忽略。公开模板不包含可用的服务地址或凭证。

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
