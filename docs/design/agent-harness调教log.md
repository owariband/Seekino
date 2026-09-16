# Agent Harness 调教日志

## 2026-09-17：Agent Evaluator Context Overflow

### 异常现象

用户提问“下雨天 MyGO 里面都发生了什么？”时，检索流程已经完成候选片段召回，但对话停在“核验候选”阶段，前端显示“回答未完成”和“这次对话未能完成，请稍后重试”。

### 日志链路

```text
POST /api/agent/messages/send                 200
Query Agent /chat/completions                 200
multimodal embeddings                        200
Qdrant text query                            200
Qdrant vision query                          200
Query Agent /chat/completions                 200
Evaluator Agent /api/v3/responses            400 Bad Request
```

数据库中记录的对外错误为 `AGENT_MODEL_ERROR`；进一步按实际证据规模复现后，Ark 返回的原始错误为：

```text
InvalidParameter: Total tokens of multi-modal content and text exceed max message tokens.
```

### 量化根因

- 本次检索返回 10 个 hits，完整检索结果约为 `260,149` 字符。
- Evaluator 同时注入了 `retrieval_calls`、`retrieval_result` 和 `selected_output`，三者携带高度重复的完整证据，最终 prompt 约为 `780,531` 字符。
- MyGO 的 `important_extra_data` 单份约为 `12,586` 字符，它被嵌入每个 hit，因而同一系列背景在 10 个 hits 中重复 10 次；系列级 `series_context` 又另行注入一次。
- 因此，异常不是“普通对话历史过长”，而是将面向 Server / API 的完整检索 DTO 直接当作 Agent 之间的上下文协议，且在同一次模型请求中重复传递。

### 已排除项

- 检索本身成功：Embedding、Qdrant 文本/视觉检索和 Query Agent 均成功返回。
- 非模型全局不可用：同模型、同 schema 的最小 Evaluator 请求可正常完成。
- 重启不能修复此异常：重启可以加载最新代码，但不会改变 prompt 的重复结构和体积。
- 与 Parse refactor 无关：故障发生在 Agent Evaluator 组装及调用模型的链路，不在视频解析或 Parse Job 执行链路。

### 架构判断

完整的检索 DTO 应继续保留在 Server 的 canonical state 中，用于 DB 持久化、API/SSE 输出、前端展示及后续按 ID 回读。模型不应接收该完整 DTO，而应接收根据当前 Agent 职责生成的 deterministic projection。

具体原则：

- 系列背景只在系列级注入一次，不再复制到每个 hit。
- 检索证据按 `videoChunkId` 去重，Evaluator 只接收一份 compact evidence ledger。
- `retrieval_calls` 仅保留 query、top-k、hit IDs 等调用元数据；`selected_output` 仅保留草稿与所选 IDs，不再复制完整证据。
- 这一层应为 Server Agent Runtime 内的 `ContextProjector / ContextBudgeter`，是普通、确定性的工程代码。
- 不新增 LLM Compactor，也不新增进程。使用 LLM 二次总结会额外引入事实改写、调用费用、延迟和新的失败点。
- `AgentRuntime` 已经拥有外层 LangGraph，负责 `turn → continue/complete → chat → output`；Query 模型内部另有 LangChain 的模型/Tool 子循环。本次只修正两层进入模型前的数据投影，不新增第三套 Graph。

### 修复方案

1. 新增 Agent 上下文投影与预算模块，定义 Tool Result、Evaluator Evidence、Loop Turn 和 History 的 compact view。
2. Tool 执行层保留完整结果，但给 Query Agent 只返回候选 ID、排名/分数、集数与时间范围、命中片段、窗口摘要等必要字段。
3. Evaluator 输入收敛为“调用元数据 + 一份去重 compact evidence + 草稿/所选 IDs”，移除三份完整证据的重复。
4. 后续 Loop Turn 仅携带计划、query、hit IDs、草稿、selected IDs 和 Evaluator advice；完整 evidence 始终在 Server 状态内保留一份。
5. 历史消息不再将完整 `searchResult` 回填到新一轮 prompt；只保留稳定 ID、时间范围以及有界的 `videoCompact` / matched evidence 来解析指代，并要求本轮 Tool 重新核验事实。
6. Provider 调用前执行应用层字符预算校验；Query 除了预检 Server 注入的初始 prompt 和 Tool Result，还在 LangChain `before_model` 边界检查完整 messages、Tool Call 参数和 Tool/结构化输出契约；Evaluator/Chat 分别在调用前检查。本地超限时返回明确的 `AGENT_CONTEXT_BUDGET_EXCEEDED`。该预算是可配置的工程保护，不冒充 Provider tokenizer 给出的精确 token 上限。
7. 在服务端保留 Provider 原始错误的 code 和 request ID 用于排障，对外仍返回稳定、脱敏的错误协议。

### 验收标准

- 10 个 hits，且每个原始结果包含 20k–30k 字符大字段时，Evaluator prompt 仍保持在明确的预算上限内。
- 同一系列背景只出现一次，同一 `videoChunkId` 的证据在 Evaluator 输入中只出现一次。
- Query Agent、Evaluator 和 Chat Agent 各自只接收完成本职责所需的字段。
- 多轮 `continue` 不随轮数重复累积完整 Tool Result。
- 对外 API/SSE 的完整检索结果、稳定 ID、时间范围和前端展示能力不丢失。
- 超限请求在 Provider 调用前被拦截，日志可定位到具体超限 section，不再只记录为泛化的 `AGENT_MODEL_ERROR`。

### 运行态备注

发现异常时，本地运行的仍是重构前启动的 API 进程和独立 Worker，尚未重启到新拓扑。这是需要单独处理的运行态一致性问题，但不是本次 Evaluator context overflow 的根因；即使重启，未修改的上下文组装仍会在同等证据规模下失败。

### 落地结果

- 新增纯函数式模型上下文投影；完整 `VideoChunkSearchResult` 仍保留在 Runtime canonical state，并继续用于校验、DB、API、SSE 和最终输出。
- Query Tool 不再返回每个 hit 中重复的 Series 背景和工程元数据；Evaluator 只接收一份 `available_evidence`，历史 Turn 只保留调用与审核元数据。
- Series 背景按 Series 注入一次；历史结果保留有界的 ID、时间、`videoCompact` 和 matched evidence，用于解析“上条第二段”等指代，但不重放完整 Evidence。
- Reference Evidence 只在 Query 阶段作为检索依据出现；Evaluator/Chat 使用本轮重新召回并核验后的唯一 Evidence，避免相同 Chunk 再复制一份。
- 新增 `ANIVAG_AGENT_MAX_PROMPT_CHARS`，默认字符预算为 `200,000`；超限在本地终止并输出可操作错误，不再等待 Provider 返回泛化 400。
- Agent 执行失败现在记录异常链和稳定业务 ID，不记录原始 Prompt、用户正文或背景载荷，能够保留 Provider code/request ID 供排障。
