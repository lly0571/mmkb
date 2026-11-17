# 图片路由与首阶段编码

纯图片且 `workflow=null` 时自动分类。已指定或上次保存的路线直接复用；`PATCH workflow=null` 可让纯图片重新分类。图文与纯文本始终使用 `text_image`、`text`。

## 四类图片

| workflow | 判断口径 | 首阶段处理 |
| --- | --- | --- |
| `visdoc` | 完整 PDF/论文/报告页面截图，页面内可包含表格、图表 | 仅原图编码，不自动 OCR 或 Caption |
| `structured_image` | 独立图表、数值对比表、流程图、关系图 | 原图与结构化 Caption 一次原生联合编码 |
| `info_rich_image` | 代码、日志、报错或关键文字为主的信息密集 UI 截图 | 原图与文字感知 Caption 一次原生联合编码 |
| `visual_image` | 风景、人物、插画、视觉设计等视觉内容为主的图片 | 仅原图编码，不自动 Caption 或 OCR |

依次判断文档页、独立结构图表、信息密集截图、普通图片。少量文字不改变普通图片分类。两种分类器共用版本化分类定义；目前版本为 `four-image-v1`。

Info Rich Caption 用简短中文概括用途和关系，原样引用关键可见错误码、命令、参数名和标识符，不能推断型号或版本。它是文字感知描述，不承担全文 OCR。生成失败、截断或超限时保留类别，使用原图编码并记录警告。结构化 Caption 沿用同样的可降级契约。

非 null Caption/OCR 始终保留，包括人工空字符串；清空 OCR 在首阶段不会触发自动识别。仅结构图片与 Info Rich 的有效 Caption 参与向量编码。所有已有文字仍参与共享分词。每份资料保持一个归一化 2048 维向量。

## 两种可切换的实现

```dotenv
IMAGE_ROUTING_BACKEND=systemone
ROUTING_MIN_PROBABILITY=0.80
ROUTING_MIN_CONFIDENCE=0.80
ROUTING_MIN_LABEL_MASS=0.90
```

- `systemone` 为默认后端，调用 `{DECISION_BASE_URL}/systemone`。基址通常包含 `/v1`，复用 `DECISION_MODEL`、API key、超时和 profile；`DECISION_ENABLED=false` 显式禁用。
- `llm` 使用支持图片输入的基础 LLM，经 `{CAPTION_BASE_URL}/chat/completions` 四分类，复用 `CAPTION_*` 及其启用开关。此模式完全绕过决策请求，无须配置决策模型。
- 默认链路：SystemOne → LLM → `visual_image`。决策未配置、禁用、请求失败或可信度不足时尝试 LLM；LLM 失败后保底整图编码。直接 LLM 模式失败后同样保底，但不反向调用 SystemOne。
- LLM 仅返回合法的类别标识；没有概率时保持为空，不生成或采信口述置信度。SystemOne 校验胜出类别、类别概率、confidence、已有 label_mass 和 argmax 诊断。诊断缺失时按已有指标判断并记录缺项，`argmax_is_label=false` 一律回退。

默认阈值来自首轮四类小样本校准，可按部署语料覆盖；它不是分类准确率。label_mass 与类别内部概率含义不同，见 [SystemOne 协议](systemone.md)。配置错误启动时暴露；网络请求沿用服务超时，不自动重试。修改配置需重启 Worker；API 与 Worker 使用统一启动入口时一起重启。

## 处理记录与版本

资源详情的 `extra.processing.routing` 包含 `policy_version`、`revision`、`configured_backend`、最终 `workflow`、`source`、实际阈值、尝试结果/耗时，以及可用决策诊断。`source` 为 `input_kind / preset / systemone / llm / default`；`preset` 表示本轮复用已有路线，不证明原路线由人工指定。

`extra.processing.encoding` 为 `image_only / image_caption / image_user_text / text_only`。警告仍为 `extra.processing.warnings` 中的 `{stage, code}`，包括低置信、低 label mass、非法响应、超时与描述截断。日志不包含图片、完整响应、凭证或内部地址。

模型调用在事务外执行；Worker 只在 ID、revision、processing 和未删除条件匹配时原子提交处理记录与索引。存量资料采用新路线时显式重新排队；人工字段默认保留，需要重新生成 Caption 时同时置为 null。升级前执行 `alembic upgrade head`，新增 `0003_image_routing` 只更新 workflow 注释，不改写资料值。

## 小样本对照与复现

准备现有九张图片和公开 ColPali v6 PDF 的完整第 2、3、4 页，共四类各三张；每类一张校准、两张冻结验收。PDF 页面渲染使用 Poppler，准备工具校验已核对 PDF 的 SHA，原样保留历史清单。

```bash
python scripts/acceptance/prepare_routing_samples.py \
  --existing-manifest docs_hide/acceptance-runs/20261116a/samples/manifest.json \
  --pdf data/routing-v1/colpali-2407.01449v6.pdf --output data/routing-v1

# 明确配置 DECISION_*、CAPTION_* 和 EMBEDDING_* 后执行真实请求。
python -m mmkb.routing_eval capture --manifest data/routing-v1/manifest.json \
  --output docs_hide/routing-runs/RUN_ID/capture.json --process
python -m mmkb.routing_eval evaluate --manifest data/routing-v1/manifest.json \
  --capture docs_hide/routing-runs/RUN_ID/capture.json \
  --output docs_hide/routing-runs/RUN_ID/evaluation.json
```

公开 PDF：[ColPali v6](https://arxiv.org/pdf/2407.01449v6)。样本、响应片段与报告放入忽略目录，代码和离线测试不依赖这些文件。使用自有清单时需为每张图片提供 ID、相对文件名、SHA、预期类别、calibration/validation 划分，两组均覆盖四类。`key_text` 可预先声明 Caption 必须保留的文字。

capture 对每张图分别请求两个分类器；`--process` 另以显式标注路线检查编码，不把它冒充自动分类端到端证据。每张完成后保存，可在相同输入/模型/提示词下继续；更换条件须另建报告。离线 evaluate 不再调用模型，仅在校准组比较预定阈值网格，按零误采纳、最多直接采纳、严格阈值优先选参，再报告冻结组结果。未配置或失败服务不能计为在线验收通过；合法 Caption 降级单列 `degraded` 和警告，不计为完整增强成功。

只修改编码提示词时，可对新的 capture 传入 `--reuse-classification PREVIOUS_CAPTURE`；工具核对样本、模型和分类提示词指纹后复用原分类，避免重新调用模型或用新结果替换冻结分类。真实自动路由、持久化、查询及迁移回滚的独立冒烟入口为：

```bash
python scripts/acceptance/run_routing_smoke.py --manifest data/routing-v1/manifest.json \
  --output docs_hide/routing-runs/RUN_ID/smoke.json --regression
```

此入口需要创建数据库的权限，只创建并清理本次随机命名的 `mmkb_test_routing_*` 数据库和临时原件；`--regression` 另外执行资源、Worker 与检索数据库回归。图片查询附精确样本标签以验证链路，不作为大规模语义召回指标。

首阶段检验分类正确性、关键文字、编码与失败降级。第二阶段另在同一文档和冻结查询上比较 VisDoc 的整图、整图加 OCR 联合编码、独立编码后归一化加权聚合；选型后再交付生产策略与存量重建。
