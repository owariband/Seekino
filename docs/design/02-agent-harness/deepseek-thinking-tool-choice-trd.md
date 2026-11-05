# 1. 问题与现状

**DeepSeek 思考模式工具选择 TRD。状态：实现完成，真实运行验证通过（2026-11-05）。** 分支 `fix/deepseek-thinking-tool-choice`，PR [#42](https://github.com/owariband/Seekino/pull/42)。关联：[video-outline-trd.md](video-outline-trd.md)（PR [#41](https://github.com/owariband/Seekino/pull/41)，需要该修复才能比较 Summarize 的思考强度）。

## 背景与触发场景

Summarize（以及 Parse 视觉）通过 Gateway 的多模态工具循环 `generate_with_tools`（`gateway/agent_model/multimodal.py`）调用模型；Chat 的 Supervisor 与 Query 走 `gateway/openai/agent.py` / `structured.py`。11-04 视频大纲实验中，GPT 视频的 Summarize 在 `reasoning_effort: none` 下反复因证据引用被拒而循环（单次租约 49 轮），因此尝试把 Summarize 的思考强度调为 `high` 对比。

## 实际与期望行为

期望：任一角色把 DeepSeek 的 `reasoning_effort` 设为 `low` / `high` / `max` 都能正常运行。实际：多模态工具循环固定发送 `tool_choice: "required"`，DeepSeek 在思考模式下直接拒绝：`400 Thinking mode does not support this tool_choice`，三次租约在两分钟内全部失败（`outputs/verification/video-outline-20261104/api.log`）。

## 现状证据

- **最小对照（`outputs/verification/deepseek-thinking-tool-choice-20261104/`）：** 同一模型 `deepseek-flash`、同一 Prompt 与单个工具，只改变思考强度与 `tool_choice`：`none`+`required`、`none`+`auto`、`high`+`auto` 均返回 200 且调用工具；仅 `high`+`required` 返回 400。`high`+`auto` 的输出包含 `reasoning` 条目，`none` 不含任何推理输出。
- **其他路径已有处理：** `gateway/openai/agent.py:253-256` 与 `gateway/openai/structured.py:224-229` 在 DeepSeek 开启思考时把 `tool_choice` 降为 `auto`，同时 `agent.py:293` 仍按原始 `required` 要求本轮必须有工具调用。多模态循环缺少同样的处理（`gateway/agent_model/multimodal.py` 的 `_request_turn` 与 `_request_chat_turn` 直接发送 `choice`）。
- **为何此前未暴露：** 11-04 各次运行的日志中，`agent_model.multimodal` 共 237 次请求均为 `required` + `none`；`openai.agent` / `openai.structured` 的 32 次请求均为 `auto`。多模态路径此前从未在思考模式下运行。

## 负面影响

多模态工具循环的任何角色都无法使用 DeepSeek 思考模式，思考强度这一调优手段在 Summarize 与 Parse 上不可用；配置看似合法，运行时却每次 400。

## 本次边界

解决：多模态工具循环在 DeepSeek 思考模式下发送 `auto`，并保持“本轮必须调用工具”的既有校验。不解决：Summarize 是否应默认开启思考（由 #41 的对照实验决定）；其他 Provider 的行为（不变）。必须保持：`reasoning_effort: none` 与非 DeepSeek Provider 的请求完全不变。

# 2. 影响因素、假设与评价指标

| 因素 | 证据 | 假设 | 验证方法 | 影响的决策 |
| --- | --- | --- | --- | --- |
| DeepSeek 思考模式不接受强制工具选择 | 2×2 对照中仅 `high`+`required` 为 400 | H1：发送 `auto` 即可运行，模型仍会调用工具 | 单测断言请求内容；真实 Summarize 以 `high` 运行 | 降级条件 |
| 降级后模型可能直接结束而不调用工具 | `auto` 允许无工具结束 | H2：沿用原始 `required` 的“必须有工具调用”校验即可兜住 | 单测：降级后无工具调用的回合仍按原有错误处理 | 是否需要额外重试 |

| 指标 | 定义与口径 | 基线 | 目标 / 护栏 | 测量方法 | 证据入口 |
| --- | --- | --- | --- | --- | --- |
| 思考模式可运行（结果） | Summarize 以 `high` 运行时 400 的次数 | 3/3 次租约 400 | 0 | api.log | 0；GPT 视频首个租约即完成，8 次调用均含推理输出 |
| 请求不变（护栏） | `none` 与非 DeepSeek 的 `tool_choice` | `required` | 保持 | 单测 | 单测通过；`tests/gateway` 仅余 `dev` 上已有的 6 个失败 |

# 3. 改造方案与建模

在 `gateway/agent_model/multimodal.py` 增加一个小函数：当 `endpoint.provider == DEEPSEEK_PROVIDER` 且 `adapter.reasoning_effort` 为 `low` / `high` / `max`，且原选择不是 `auto` / `none` 时，发送 `auto`；否则原样发送。Responses 与 Chat 两个请求构造点都使用它。回合校验继续使用原始 `choice`（`require_tool_call=enforce_final_schema and choice == "required"`），与 `gateway/openai/agent.py` 的做法一致。

**实施边界：** `gateway/agent_model/multimodal.py` 与 `tests/gateway/` 中对应测试。无配置、数据库或接口变更。

# 4. 对照实验与结果分析

1. 单测：DeepSeek + `high` 发送 `auto`；DeepSeek + `none`、Ark + `high` 保持 `required`；降级后无工具调用的回合仍被拒绝。
2. 真实运行：在 #41 的克隆库实验中以 `high` 运行 GPT 视频 Summarize，记录 400 次数；思考强度对 Summarize 质量与耗时的比较记录在 #41。

| 指标 | 基线实测 | 方案实测 | 验收标准 | 判断与证据 |
| --- | --- | --- | --- | --- |
| `high` 下 400 次数 | 3/3 | 0 | 0 | 达标（`outputs/verification/video-outline-20261104/`，本地合并 #41 + #42 的 `9ea22f39`） |

# 5. 试点验证与方案取舍

试点即 #41 的 `high` 对照运行。代价：思考模式下模型可以选择不调用工具，由既有校验转为该回合失败；若实测频繁出现，再考虑在循环内对该情形追加提示后重试。

# 6. 结果验收与闭环

两项指标达标。降级后未出现“无工具调用即结束”的回合。Summarize 是否默认开启思考由 [video-outline-trd.md](video-outline-trd.md) 的对照结论决定。

## 调整记录

| 日期 | 原判断或方案 | 新证据 | 调整与原因 | 影响 |
| --- | --- | --- | --- | --- |
