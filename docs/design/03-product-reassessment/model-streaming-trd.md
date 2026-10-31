# 1. 问题与现状

需求：将模型流式传输作为 Gateway 的共享协议能力，供 Chat、结构化工具调用和 Parse 复用。日期：2026-10-31；当前为设计初稿，实施和验证待完成。

- 分支：`refactor/model-streaming`；目标分支：`dev`；PR 创建后回填。首提交只包含本 TRD。
- 上游：[Kimi 流式接入 TRD](kimi-streaming-trd.md)、[PR #20](https://github.com/owariband/Seekino/pull/20)，已合并 `dev`。本次基于该实现收敛共享能力，不重复真实素材调用。
- 当前问题状态由 [problems.md](../../dev/problems.md) 维护；实验与决策写入 [devlog.md](../../dev/devlog.md)。

用户指出，`stream=True` 不应只属于 Kimi，Chat 和 Parse 应复用同一能力。现状核查确认：`GatewayClient._build_generative` 仅在 `profile == "kimi"` 时启用 `OpenAIVisionModel` 流式；OpenAI/Ark 的普通 Agent 调用、LangChain 结构化 Agent 调用仍使用非流式；Ark Responses 已有 SSE 收集器，DeepSeek Responses 尚未接入；本地 Ollama/llama.cpp 的结构化生成和文本流各有接收代码。前端 SSE 传递业务事件，不能证明模型 HTTP 连接已经流式化。

这导致相同协议重复实现、入口行为分裂，后续模型仍可能出现长请求等待完整响应的问题。上游单个 Kimi Chunk 已证明流式请求可以完成，但不能据此保证所有供应商都兼容或所有长请求都成功。

本次范围为已实现的 Chat Completions / Responses 生成协议，包括 Chat、结构化工具、视觉 JSON/工具轮次及本地兼容生成接口。保留 SDK/CLI 原生协议、Embedding/ASR、Prompt、Schema、模型参数、工具合同、事务和重试策略。前端逐 token 展示、服务重启、正式任务/数据库变更和新一轮真实素材请求不属于本次验收。

# 2. 影响因素、假设与评价指标

流式是传输协议能力，不由模型显示名或业务入口决定。Chat Completions 和 Responses 的终止事件、工具增量结构不同，应共享各自协议实现，不能把两种格式混合拼接。供应商特有的参数仍由适配器负责。

| 指标 | 基线 | 验收标准 | 验证方法 |
| --- | --- | --- | --- |
| 正式入口接入 | Kimi 视觉与 Ark Responses 流式，其余多个入口非流式 | 同协议所有已实现生成入口使用同一收集器；默认 true、显式 false，主模型/fallback 独立生效；无 Kimi 开关 | 工厂、Chat/工具、结构化生成实际请求测试及调用链检查 |
| 完整性 | 各路径已有 JSON/工具校验 | 终止事件缺失、断流、截断不能变成成功；完整后才执行工具 | 正常/故障 SSE 与多轮工具测试 |
| 参数与计量 | provider/profile 分别装配参数 | token 参数、Schema、工具、reasoning、末尾 usage 保留 | 请求内容及返回结果断言 |
| 控制与回退 | 各入口已有取消、超时、fallback | 连接关闭、取消优先、失败原因及 fallback 合同保留；不增加自动重试 | 控制/网络故障注入及既有回归 |
| 实际效果边界 | PR #20 单 Kimi Chunk 成功 | 本次证明共享接入和协议合同；其他供应商在线兼容性待测 | 自动化测试与历史真实证据分别报告 |

# 3. 改造方案与建模

1. 将现有 Chat Completions 收集器和 Ark Responses SSE 收集器移入 `gateway/streaming/`，按协议组织；复用 SDK 的 SSE 解码与 Chat 增量合并，不自己再写一套字节解析器。
2. 通用 `ModelBinding.stream` 使用严格布尔值，默认 `true`，允许 YAML `models.<role>.stream: false` 或创建模型时显式传入 `ModelBinding(stream=False)`。旧 Agent 配置投影、主模型与 fallback 分别保留配置；工厂把值传给所有 HTTP 生成适配器，共享请求函数只执行该值。删除 Kimi profile 开关，不在失败时自动改成非流式重放。
3. Chat、Deep Agents 和结构化 Agent 复用收集后的完整响应，继续执行各自 finish reason、Schema 和工具合同。推理字段跟随完整 assistant message 回传，由不接收该字段的供应商适配器处理差异。
4. Responses 路径保留终止事件、序号和最终正文一致性校验；DeepSeek 与 Ark 复用。显式保留 usage、诊断、取消和失败分类。
5. 本地 OpenAI-compatible 生成接口复用 SDK 与同一 Chat 收集器；文本流也复用同协议接收。无事件期间仍受 HTTP read timeout 限制，逐事件预算检查不声称能替代独立硬截止时间。

没有数据库迁移。旧 YAML 未配置 stream 时按新默认 true 执行；显式 false 保留非流式兼容入口。原生 CLI/Embedding/ASR 不发送 HTTP 生成协议 stream 参数，不支持的显式配置需明确说明或拒绝，不能假装执行。

# 4. 对照实验与结果分析

待实施。测试先覆盖共享协议与正式入口，再跑相关 Gateway/Parse 回归。真实 SDK + HTTP MockTransport 验证请求形状和 SSE 合并，不仅 mock 最终对象；现有工具合同测试的模拟服务同步改为返回真实分片形状。

基线已知失败：`test_fallback_media_is_checked_against_the_active_context_budget` 在上游改动前同样失败，详见 PR #20 报告。若本次仍失败，单独列出，不将其写成通过。

# 5. 试点验证与方案取舍

本次使用隔离 worktree 和模拟 HTTP 响应，不读取密钥、不发送用户媒体、不改运行中服务。共享收集器降低遗漏入口的风险，但流式本身不降低上下文占用，也不能保证首包之前不超时。远端实际兼容性、性能和整集质量须用独立运行证据评价。

回退可以撤销本 PR，不影响数据格式与已提交结果；不通过自动非流式重放隐藏供应商协议错误。

# 6. 结果验收与闭环

待完成。交付需记录具体入口覆盖、测试结果、未验证的供应商/部署边界，并更新 Gateway README、设计索引、问题状态和 devlog。通过 PR 集成 `dev`；不将代码集成等同于服务加载或真实任务恢复。

## 2026-10-31 决策澄清

用户明确要求 stream 属于模型通用配置，创建模型时决定 true/false；因此从“全路径固定流式”修订为“统一默认 true，显式 false 可用”。原测试把 false 固定为默认的设计错误不得继续保留；协议、工具合同等旧测试可以显式选择 false 覆盖兼容分支，但必须新增所有正式入口的默认流式和显式关闭验收。

归因证据：`862c2699` 同次加入 Chat 工具循环的 stream=False 与对应测试断言，Git 作者/提交者均为 Hypnos；`1ab13600` 引入结构化 Agent 的 streaming=False，同样署名 Hypnos；`71a1343e` 引入视觉适配器的 stream=False，署名 yyu03。无 Agent 署名或用户决策记录，不能仅凭 Git 身份认定由用户亲自决定。`f82fabc2` / PR #20 的 Kimi 专属开关和其他 profile 非流式测试由本会话 Codex 实施，责任明确。
