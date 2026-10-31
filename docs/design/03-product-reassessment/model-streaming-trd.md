# 1. 问题与现状

需求：修复模型流式开关被入口写死的问题，将其作为通用模型配置和共享协议能力，供 Chat、结构化工具调用和 Parse 复用。日期：2026-10-31；实现及隔离接入验证完成，PR 集成、服务加载和新增线上验证分别验收。

- 分支：`refactor/model-streaming`；目标分支：`dev`；[PR #22](https://github.com/owariband/Seekino/pull/22)。首提交只包含本 TRD。
- 上游：[Kimi 流式接入 TRD](kimi-streaming-trd.md)、[PR #20](https://github.com/owariband/Seekino/pull/20)，已合并 `dev`。本次基于该实现收敛共享能力，不重复真实素材调用。
- 当前问题状态由 [problems.md](../../dev/problems.md) 维护；实验与决策写入 [devlog.md](../../dev/devlog.md)。

用户指出，`stream=True` 不应只属于 Kimi，Chat 和 Parse 应复用同一能力。修复前核查确认：`GatewayClient._build_generative` 仅在 `profile == "kimi"` 时启用 `OpenAIVisionModel` 流式；OpenAI/Ark 的普通 Agent 调用、LangChain 结构化 Agent 调用仍使用非流式；Ark Responses 已有 SSE 收集器，DeepSeek Responses 尚未接入；本地 Ollama/llama.cpp 的结构化生成和文本流各有接收代码。前端 SSE 传递业务事件，不能证明模型 HTTP 连接已经流式化。实施时还确认 Server Parse profile / Harness 快照有字段白名单，必须同时保存和恢复开关，单独改工厂会漏掉显式 false。

这导致相同协议重复实现、入口行为分裂，后续模型仍可能出现长请求等待完整响应的问题。上游单个 Kimi Chunk 已证明流式请求可以完成，但不能据此保证所有供应商都兼容或所有长请求都成功。

本次范围为已实现的 Chat Completions / Responses 生成协议，包括 Chat、结构化工具、视觉 JSON/工具轮次、本地兼容生成接口和使用 Ark 生成接口的视频转写。保留 SDK/CLI 原生协议、Embedding/独立音频 ASR、Prompt、Schema、模型参数、工具合同、事务和重试策略。前端逐 token 展示、服务重启、正式任务/数据库变更和新一轮真实素材请求不属于本次验收。

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
6. Server Parse 默认配置、上传配置和 Harness 任务快照保留开关；理解、OCR、Refine 等生成绑定及 fallback 恢复后传给 Gateway。Ark 视频转写也传递配置。旧快照缺少字段时按 true 执行，序列化时保持缺省形状以保护历史指纹；新任务明确保存有效值。

没有数据库迁移。旧 YAML 和任务快照未配置 stream 时按新默认 true 执行；显式 false 保留非流式兼容入口。原生 CLI 沿用事件协议并拒绝显式 false；Embedding/独立音频 ASR 不转发 HTTP 生成协议 stream 参数。独立 Query Hook 的本地改写器使用 `query_hook.rewriter_stream`，默认 true。

# 4. 对照实验与结果分析

实现后的入口验收使用真实 OpenAI / Ark SDK 和 HTTP MockTransport：标准兼容、Kimi、DeepSeek、Ark × 默认 / true / false × JSON / Agent Loop / Structured / Deep Agents Supervisor 共 48 个组合；另覆盖主模型和 fallback 独立配置、同名 Ark fallback、视频转写、本地模型和 Query Hook。实际检查发送的 stream、返回值和末尾 usage，不只检查构造参数。

Parse 配置、OCR、Harness 输入和正式 runtime 模型创建相关回归 397 passed，证明配置经过保存、恢复和工厂创建，且旧快照序列化保持原状。既有完整响应合同测试显式选择 false，默认 SSE 由独立矩阵覆盖；不再把 false 断言作为默认行为。

配置 HTTP 路由与上传服务的隔离 SQLite 回归另有 113 passed，覆盖读取默认配置、提交 true / false、存储并取回主模型和 fallback 的值及既有幂等合同。前端配置类型同步可选字段，typecheck 通过；没有新增 UI 开关或逐 token 展示。

协议故障测试覆盖截断、缺失终止 choice / event、异常序号、最终文本不一致、取消、读取超时、工具半截参数与 usage。Gateway 全量回归和命令见 [实施记录](../../report/gateway/model-streaming-20261031.md)。全量发现的 5 项失败均在未改业务代码的 `dev` / `580df6cd` 同环境重现：3 项默认配置旧断言、1 项 Langfuse header 断言、1 项既有 fallback context 用例；未夹带修复，不记为通过。

# 5. 试点验证与方案取舍

本次使用隔离 worktree 和模拟 HTTP 响应，不读取密钥、不发送用户媒体、不改运行中服务。共享收集器降低遗漏入口的风险，但流式本身不降低上下文占用，也不能保证首包之前不超时。远端实际兼容性、性能和整集质量须用独立运行证据评价。

相对继续为 Kimi 特判，统一配置消除了新模型遗漏流式入口的策略分叉；相对无条件固定 true，显式 false 保留供应商兼容选项。LangChain 结构化适配器内部禁止回调绕过完整轮次校验，但实际 HTTP 请求读取 `request_stream`；该内部开关不能误当成模型 stream=false。

运行新版本后可以通过模型绑定显式 false 选择完整响应；不会自动非流式重放。新任务快照增加 stream 字段，回滚到尚不认识该字段的旧版本前需要评估新任务恢复兼容性，不能声称任意代码回滚都没有数据格式影响。

# 6. 结果验收与闭环

代码和隔离验证已完成，当前实现不再按 provider/profile 写死开关；正式 Parse 的保存、恢复和创建边界已接入。Gateway README、模型配置、设计索引、问题和 devlog 同步维护。交付通过原有 PR #22 进入 dev，不另拆 PR；合并与运行中服务加载另列，不能视为真实任务恢复。

未验证：本次没有供应商在线请求、真实 Parse HTTP/数据库提交、服务重启或整集验收，不能由离线矩阵推断各模型线上成功率。原 Kimi 单 Chunk 真实结果保留在上游报告。问题状态仅维护在 [B24](../../dev/problems.md#b24模型-stream-被入口写死且正式-parse-丢失开关)。

## 2026-10-31 决策澄清

用户明确要求 stream 属于模型通用配置，创建模型时决定 true/false；因此从“全路径固定流式”修订为“统一默认 true，显式 false 可用”。原测试把 false 固定为默认的设计错误不得继续保留；协议、工具合同等旧测试可以显式选择 false 覆盖兼容分支，但必须新增所有正式入口的默认流式和显式关闭验收。

归因证据：`862c2699` 同次加入 Chat 工具循环的 stream=False 与对应测试断言，Git 作者/提交者均为 Hypnos；`1ab13600` 引入结构化 Agent 的 streaming=False，同样署名 Hypnos；`71a1343e` 引入视觉适配器的 stream=False，署名 yyu03。无 Agent 署名或用户决策记录，不能仅凭 Git 身份认定由用户亲自决定。`f82fabc2` / PR #20 的 Kimi 专属开关和其他 profile 非流式测试由本会话 Codex 实施，责任明确。
