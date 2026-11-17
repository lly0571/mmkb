# 结构化决策适配（`/v1/systemone`）

`SystemOneProvider` 封装 vLLM 的结构化决策接口：一次请求针对同一个 `state` 提出若干个带类型的问题，服务端为每个允许的答案返回概率与置信度，常用于分类与路由，不产出自由文本。适配器只负责请求协议、限制校验与响应解析，不承担工作流分支或持久化。

与视觉分类的区别：`vision.classify` 走 Chat Completions，返回单个类别字符串；`providers.decision` 返回每个选项的概率、置信度与可选诊断，可以一次问多个问题，但要求服务端提供受支持的读取策略。Worker 自动图片路由默认使用 SystemOne 四分类，不可信或不可用时回退基础 LLM；也支持显式选择 LLM，见 [图片路由](routing.md)。

## 启用与配置

决策服务是可选项：`DECISION_BASE_URL` 与 `DECISION_MODEL` 同时为空时 `providers.decision` 为 `None`，只填写其中一项会报配置错误。以本地 `d3-mini` 为例（`.env.local`）：

```bash
DECISION_BASE_URL=http://localhost:8004/v1
DECISION_MODEL=vllm-sr/d3-mini
```

服务端启动方式：

```bash
vllm serve vllm-sr/d3-mini --served-model-name vllm-sr/d3-mini
```

基址填 API 根路径（通常以 `/v1` 结尾），客户端追加 `systemone` 或 `models`。`DECISION_API_KEY` 为空时不发送认证头；`DECISION_TIMEOUT_SECONDS` 默认 60，连接超时固定 5 秒，不自动重试。

`DECISION_MODEL_PROFILE` 选择请求方言，默认 `auto`：

| 取值 | 适用模型 | 差异 |
| --- | --- | --- |
| `d3` | Decision 3.0 系列（`vllm-sr/d3`、`d3-flash`、`d3-mini`、`d3-nano`、`d3-lite`、`d3-edge`） | 提示词由 checkpoint 固定：`instructions` 返回 400；`choice` 最多 255 个选项 |
| `clef` | `Cloudflare/clef-flash`、`Cloudflare/clef-omni` | 提示词由 checkpoint 固定：`instructions` 返回 400；`choice` 最多 26 个选项 |
| `generic` | 具备读取策略的生成模型（Qwen3 系列、Liquid d1 等） | 允许 `instructions`；`choice` 按 A–Z 标注，最多 26 个选项 |

`auto` 在模型 ID 中出现独立的 `d3` 词元时选 `d3`，出现 `clef-flash` 或 `clef-omni` 词元时选 `clef`，其余按 `generic`。自行改名部署（例如 `my-decision-4b`）时请显式指定 `d3`、`clef` 或 `generic`。

## 调用方式

```python
from mmkb.providers import ImageInput, build_providers

with build_providers() as providers:
    result = providers.decision.decide(
        {"ticket": "我的银行卡在同一笔订单里被扣了两次款。"},
        {
            "team": ChoiceQuestion({"billing": "支付与退款", "shipping": "物流配送", "security": "账号登录"}),
            "urgent": NoulQuestion(true="涉及资金损失或账号无法登录"),
            "tone": ScoreQuestion(["平静", "不满", "愤怒"]),
        },
    )
    team = result["team"]              # 按问题 ID 取答案，顺序与请求一致
    team.value                        # 'billing'
    team.probabilities                # {'billing': 0.97, 'shipping': 0.01, 'security': 0.02}
    team.confidence                   # 胜出标签在全词表上的占比
    team.label_mass                   # 模型分给标签的概率总和；偏低说明答案不可靠
    result["urgent"].value            # bool
    result["tone"].level_name         # 期望档位对应的档位名
    result.usage.input_tokens         # 跨所有读取的实际 token 统计

    # 单问题快捷入口：choose / noul / rate 直接返回 Answer
    answer = providers.decision.choose("这张图片属于哪一类？", ["chart", "document", "photo"],
                                       images=[ImageInput(open("a.png", "rb").read(), "image/png")])
```

`state` 是字符串、Mapping 或 Sequence；容器递归转换为 JSON 对象和数组（支持 `UserDict`、`UserList`、元组、`range` 等）。对象键必须是字符串，嵌套值只接受 JSON 标量与上述容器；二进制、非有限数值、循环引用及其他不支持的内容在本地报 `invalid_input`。图片可选，`state` 用空串表示“只有图片/视频”。媒体一次读取、请求内所有问题共用。`images` 接受 `ImageInput`（编码为 data URL）或 HTTP(S)/data URL，`videos` 只接受 URL；音频不支持，`joint` 联合读取不下发。

## 三种问题类型

| 类型 | `criteria` | `Answer` 字段 |
| --- | --- | --- |
| `choice` | 选项名 → 描述或 `null`，至少 1 项 | `choice` 为胜出选项名，`probabilities` 按选项名索引 |
| `noul` | `null`，或含可选 `true`、`false` 描述的对象 | `noul` 为 `probability(yes) >= 0.5`，`probabilities` 键为 `yes`/`no` |
| `score` | 档位名列表，2–10 项 | `score` 为按概率加权的 0 基档位期望值，`legend` 为请求中的档位名，`probabilities` 键为档位索引字符串 |

限制：一次请求最多 64 个问题；`score` 最多 10 档；`choice` 选项上限取决于方言（`d3` 为 255，`clef` 和 `generic` 为 26）。问题 ID、选项名、档位名不得为空或带首尾空白，档位名不可重复。超限在本地即报 `invalid_input`，不发请求。

## 响应校验

`answers` 必须与请求的问题集合一一对应，`type` 与请求一致；每个答案的 `probabilities` 必须覆盖全部允许答案、取值落在 `[0, 1]` 且和约等于 1（不静默重归一化），`confidence` 必填。`score` 答案还要求 `legend` 覆盖每个档位，`score` 落在 `[0, 档位数-1]`。

`id`、`model`、`created`、`usage`、`diagnostics` 属于可选元信息：缺失或类型异常时对应字段为 `None`，不影响答案解析；`diagnostics.label_mass` 存在但非法会按 `invalid_response` 报错。

概率不是完全可复现的：同一请求的概率取决于同批请求与前缀缓存命中情况。需要严格复现时，服务端设置 `VLLM_BATCH_INVARIANT=1` 并关闭前缀缓存，代价是吞吐下降。

## 错误与验证

沿用 `ProviderError`，`capability` 固定为 `decision`：

| 场景 | `code` |
| --- | --- |
| 本地限制校验不通过（问题数、选项数、档位、ID、媒体格式、固定提示词下传 `instructions`） | `invalid_input` |
| 服务端判定输入超限（`max_model_len` 等） | `input_too_large` |
| 服务端不支持该能力（无读取策略，HTTP 501） | `unsupported_model` |
| 答案缺失、概率不归一、类型不符 | `invalid_response` |
| 超时、连接失败、其他 HTTP 错误 | `timeout` / `connection_error` / `http_error` |

默认离线测试不接触服务，使用 HTTPX MockTransport：

```bash
python -m unittest discover -s tests -p 'test_model_*.py' -v
```

真实服务验证（需先启动 `d3-mini` 并配置 `DECISION_*`）：

```bash
RUN_MODEL_INTEGRATION=1 python -m unittest discover -s tests -p 'test_model_services.py' -v
```

该用例检查模型列表、三类问题的概率归一性、置信度区间与 token 统计，以及一次带图片的决策；它只验证协议与契约，不代表业务分类质量，后者需要在真实语料上另行评估。

协议依据：[vLLM Structured Decisions Models](https://docs.vllm.ai/en/latest/models/structured_decisions_models)。

## OpenRouter Decisions 兼容性

OpenRouter 同时提供 [Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)（`/api/alpha/decisions`）和 [System One API](https://openrouter.ai/docs/client-sdks/go/sdks/systemone/README.md)（`/api/v1/systemone`）。当前适配器可通过 `DECISION_BASE_URL=https://openrouter.ai/api/v1` 直接命中后者，无需修改请求路径；但 `noul` 响应与图片快捷参数尚未完全适配：

| 项目 | 当前 vLLM 适配器 | OpenRouter |
| --- | --- | --- |
| 请求地址 | 在基址后追加 `systemone` | 可用 `POST https://openrouter.ai/api/v1/systemone`；另有 `/api/alpha/decisions` |
| 图片 | `images=` 转为顶层 `images` 字段 | 图片是 `state` 数组中的 `image_url` 项；可手工构造该数组，但现有图片快捷参数不转换为此格式 |
| `noul` 响应 | 必须包含 `noul`、`confidence` 和 `probabilities.yes/no` | Jev 可只返回 `type` 和 `noul`；当前解析器会报 `invalid_response` |

2026-11-16 使用 `typesafe/jev-1.14`、公开示例的自行车描述、512×512 纯色 PNG 和三个问题进行了真实请求，HTTP 200，返回模型为 `typesafe/jev-1.14-20261014`。逐题交给当前解析器时，`choice` 和 `score` 通过，`noul` 因缺少 `confidence` 被拒绝。该检查验证协议兼容性，不评估图片分类质量。原始 64×64 测试图片被服务端以 HTTP 400 拒绝；Jev 的图片尺寸限制见 [Multimodal Decisions](https://openrouter.ai/docs/guides/community/multimodal-decisions)。

随后针对 `/api/v1/systemone` 使用同一请求再次验证，HTTP 200：`noul=0.04`，`choice=bikes`，`score=0.99`。当前 `generic` 方言可解析 `choice` 和 `score`；`noul` 仍因相同的响应字段差异被拒绝，所以一个包含三类问题的完整 `decide()` 调用仍会失败。

完整支持需要增加 `noul` 响应映射和图片快捷参数转换，无需为 System One API 增加新路径。这里的基址以 `/api/v1` 结尾，是因为本适配器只追加 `/systemone`；[TypeSafe SDK 指南](https://openrouter.ai/docs/guides/community/typesafe-sdk)中的 `/api` 基址适用于会自行追加 `/v1/systemone` 的官方 TypeSafe SDK。
