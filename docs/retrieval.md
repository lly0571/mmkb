# 检索服务

已实现的入口是 `mmkb.retrieval.search()`，支持文本、图片字节和图文联合查询，默认返回 Top-20。多个标签必须全部匹配，标签按原字符串精确匹配；空标签列表不限制结果。仅返回 `ready`、未删除且具有索引的资料。

本轮通过注入编码器运行，不自动加载模型配置或创建模型客户端。HTTP 搜索接口、真实模型桥接、关键词辅助与文本降级尚未接入。

## 直接调用

在 `mmkb` Conda 环境安装项目及测试依赖：

```bash
conda activate mmkb
python -m pip install -e '.[test]'
```

调用方提供数据库地址和实现 `mmkb.embedding.EmbeddingProvider` 协议的编码器：

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mmkb.embedding import EmbeddingProvider
from mmkb.retrieval import SearchResult, search


def find_resources(database_url: str, embedder: EmbeddingProvider) -> SearchResult:
    engine = create_engine(database_url, hide_parameters=True)
    try:
        sessions = sessionmaker(engine)
        return search(
            sessions,
            embedder,
            text="金融图表",
            tags=["金融", "图表"],
            top_k=20,
        )
    finally:
        engine.dispose()
```

图片查询传入 `image=图片字节`；图文查询同时传入 `text` 与 `image`。检索服务不读取图片路径、不创建临时文件，图片格式和大小由调用入口或编码器校验。

`search()` 先校验请求、编码并归一化查询向量，再创建 Session。Session 由本次搜索关闭，Engine 和编码器由调用方管理。搜索不写入资源、不提交事务。

结果为 `SearchResult(items, mode="vector")`。每项包含 `resource_id`、`input_kind`、`title`、`tags`、`summary`、`score`。摘要依次选取非空用户文字、Caption、OCR，去除首尾空白后取前 200 字符。`score = 1 - cosine_distance`，越大越相似，可为负值；同分顺序不保证。无匹配时 `items` 为空。后续 API 可通过资源 ID 生成详情与原件入口。

## 编码协议与分词

编码器提供同步方法：

```python
embed(*, text=None, image=None, purpose) -> Sequence[float]
```

`image` 为非空 `bytes`；`purpose` 支持 `query` 和 `document`。图文查询只调用一次 `embed()`，编码器必须进行联合编码。查询与资料必须使用相同模型及预处理约定。

`mmkb.embedding` 定义的是可注入协议；`mmkb.providers` 中的模型客户端使用自己的 `ImageInput`。后续接线时由桥接器补充 MIME 并转换图片输入类型，当前不应直接将该客户端作为图片查询的编码器传入。

`normalize_embedding()` 可供资料处理复用，检查 2048 维、有限数值和非零向量并返回单位向量。非法请求抛出 `ValueError`；编码失败或无效向量抛出 `EmbeddingError`；数据库异常向上传递。失败不返回伪造的空结果，不执行关键词降级。

`segment_text(text)` 使用 Jieba 默认精确模式，去掉空白 token 后以空格拼接，不修改大小写或标点。资料处理负责组合标题、标签、用户文字、OCR、Caption 后调用，再写入 `segmented_text`；`search_tsv` 仍由数据库生成。当前向量搜索不使用分词结果。

## 测试与索引

不依赖模型服务或数据库的检索测试：

```bash
python -m pytest -q tests/test_retrieval.py
```

集成测试必须显式设置 `MMKB_TEST_DATABASE_URL`，不会回退使用 `.env.local` 或 `DATABASE_URL`。目标应为独立、空的测试数据库，预先在 `public` 启用 pgvector；迁移通过已有 Alembic 入口执行：

```bash
export MMKB_TEST_DATABASE_URL='postgresql+psycopg2://user:password@localhost/mmkb_test'
DATABASE_URL="$MMKB_TEST_DATABASE_URL" python -m alembic upgrade head
python -m pytest -q tests/test_retrieval.py tests/test_retrieval_integration.py
```

未设置测试库地址时集成测试跳过；设置后连接失败或结构未迁移会使测试失败。测试数据在外层事务内写入，每例结束回滚；搜索会话通过 SAVEPOINT 使用该事务。

测试覆盖三类输入、编码失败、向量校验、距离与得分、Top-K、状态过滤、多标签 AND、摘要和 Session 关闭。

检索 SQL 使用 `ORDER BY embedding <=> 查询向量 LIMIT K`，保持 HNSW 可用的查询形式。初始迁移已经定义 HNSW 索引，测试在事务内使用 `SET LOCAL enable_indexscan = off` 强制精确扫描。业务代码不固定扫描方式；后续可由数据库构建、选择和配置 HNSW。正式启用近似检索时，仍需验证过滤后的召回数量和质量。参见 [pgvector 官方说明](https://github.com/pgvector/pgvector#monitoring)。
