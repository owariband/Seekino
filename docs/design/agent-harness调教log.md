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

## 2026-09-19：5–6 集范围仍反复爆 Context，13 集扩容风险

### 当前状态

- 优先级：`P0`
- 状态：未解决，不应将 5–6 集的偶发成功视为容量已验证。
- 影响：多集 Series Scope 下 Agent 回答可在检索后因上下文过大而失败，无法稳定扩展到 13 集。
- 运行结论：在完成 token-aware 装箱和分层取证之前，13 集整体范围应视为高风险场景，必须用真实数据压测，不能直接推定可用。

### 已发生的问题

#### 1. 2026-09-17 的 Evaluator Context Overflow

已有日志确认：10 个 hits 的完整检索结果约为 `260,149` 字符，由于重复注入，Evaluator prompt 增长到约 `780,531` 字符，最终被 Provider 以超过最大消息 token 数拒绝。这是已经量化和定位的事实。

#### 2. 5–6 集范围下反复爆 Context

用户确认已经反复遇到多次 context overflow，并非单次偶发；当检索范围扩大到 5–6 集时尤为明显。

当前这一轮报告尚没有对应请求的精确 `input_tokens`、各 section 字符数、hit 数与调用次数，因此不伪造具体数值。但“5–6 集范围下已多次爆 context”作为已发生现象记录，后续需用稳定的请求 ID 和 token 构成补齐量化证据。

#### 3. 先前修复降低了重复，但没有封住总量

2026-09-17 的修复消除了明显的 Series 背景重复和同一完整 DTO 在 Evaluator 中的三份复制，这一方向正确，但仍留有两个无界因素：

- 每个“首次出现的新 hit”仍会向 Query Agent 返回完整 evidence projection，包含 `transcript`、`allTimeEvents`、`baselineVisualCaption`、`videoCompact`、`contextBeforeCompact`、`contextAfterCompact` 和 `windowSummary`。
- `available_evidence` 是当前及之前所有检索调用的唯一 `videoChunkId` 并集，后续 Query/Evaluator 会再次投影这个并集。去重只避免了相同 Chunk 重复返回，并没有限制不同 Chunk 的总 evidence 体积。

### 当前上下文的增长链路

```text
单次模型输入
= 固定 Prompt / Tool Schema
+ Series Context（所有入选 Series 的背景、角色表和视频列表）
+ Conversation History / Reference Context
+ 当前及历史轮次元数据
+ 所有唯一检索候选的完整 Evidence Projection
+ 输出预留和 Provider 内部契约
```

按当前配置上限，最多有 3 个 Query/Evaluator 轮次、每轮最多 4 次检索、每次最多 20 个 hits。在所有检索结果都不重复的理论最坏情况下，`available_evidence` 可累积至：

```text
3 轮 × 4 次检索/轮 × 20 hits/次 = 240 个唯一 Chunk
```

实际运行通常会先被字符预算或 Provider token 上限拦住，不会真正走到 240 个完整证据；但这说明当前设计只是“超限失败”，而不是“始终装进固定预算”。

### 13 集为什么是高风险，但不能简单按集数线性估算

`top_k` 是固定的，因此从 6 集增加到 13 集，单次检索返回的 hit 数并不会必然变成原来的两倍，不应声称 context 一定按 `13/6` 线性增长。但风险仍会显著上升，原因包括：

- `series_context` 会携带每个选中 Series 的 description、`importantExtraData`、`characterBible` 和完整视频列表。如果 13 集是同一 Series 下的 13 个视频，直接线性增长的主要是视频列表；如果还扩展了 Series 数，其他背景字段也会一起增长。
- 更大的素材范围更容易产生多义问题，促使 Query Agent 执行更多次改写检索、返回更多不重复 Chunk。
- 即使 hit 数不变，不同 Chunk 的 transcript 和 event 体积差异很大；单个极肥 Chunk 也可击穿预算。
- 召回范围扩大后，为了保证覆盖率而提高 `top_k` 只会进一步放大上下文，它不是可持续解法。

### 尚未发生或尚未单独证实的同源隐患

| ID | 隐患 | 触发方式 | 预期后果 | 当前状态 |
|---|---|---|---|---|
| `CTX-R01` | 13 集 Scope 元数据本身增长 | 同一 Series 下视频列表扩到 13 集，或同时扩展 Series 数并注入更多背景 | 尚未召回证据就占用更多输入预算 | 未压测 |
| `CTX-R02` | 多检索/多轮唯一 Evidence 并集无硬上限 | Query 改写问题并反复召回不同 Chunk | Evaluator 输入跨轮线性增长，最终整轮失败 | 设计上已存在 |
| `CTX-R03` | 单个超大 Chunk 突破预算 | 长 transcript、高密度事件、较长前后 compact 同时出现 | `top_k=1` 仍可 overflow | 未设 per-hit token cap |
| `CTX-R04` | 历史消息与历史检索摘要累积 | 长对话达到 12 条历史，每条有多个候选摘要 | 新问题留给当前 evidence 的预算不足 | 只按消息数限制，不按 token 限制 |
| `CTX-R05` | Reference Context 注入完整证据 | 用户引用一个异常肥的历史 Chunk | Query 尚未新检索就超预算 | 无独立 reference token cap |
| `CTX-R06` | Chat Agent 重新接收已选完整 Evidence | Query/Evaluator 成功选出多个高体积 Chunk | 在最后渲染阶段才 overflow，前面检索和审核计算全部浪费 | Chat 仍使用 `project_search_result` |
| `CTX-R07` | 字符预算与真实 token 上限失配 | 中文/日文、JSON、ID、Tool Schema 或 Provider tokenizer 变化 | 本地检查通过，Provider 仍拒绝；或过早拒绝本可完成的请求 | 当前仅有 char budget |
| `CTX-R08` | 不同模型共用同一 `max_prompt_chars` | Query/Evaluator/Chat 切换到 context window 或 tokenizer 不同的模型 | 一个模型可用的预算对另一个模型不安全 | 未按 provider/model 配置 |
| `CTX-R09` | 超预算时只会失败，不会降级 | 新 Tool Result 加入后超过字符预算 | 用户只得到“缩小范围后重试”，已有候选和计算丢失 | 未实现 budget-aware packing |
| `CTX-R10` | 未超硬上限但出现 lost-in-the-middle | 大量候选勉强装入上下文 | 模型忽略中间证据、选错片段或产生不稳定回答 | 未建立容量-质量联合评测 |
| `CTX-R11` | 并发 Tool Call 在单次模型响应中一次生成多个大结果 | 模型同时请求多组查询 | 尽管服务端串行执行，所有结果仍会进入后续 messages | 已串行计数，未做联合 token 装箱 |

### 根因判断

根因不是“向量库召回的候选不能多”，而是将候选排名、详细取证和最终模型上下文绑成了同一个大 DTO。

当前流程是：

```text
检索 top-k
→ hydrate 每个候选的完整 Evidence
→ 将完整 Evidence 作为 Tool Result 交给 Query
→ 将所有唯一 Evidence 并集交给 Evaluator
→ 将已选完整 Evidence 再交给 Chat
```

目标流程应改为：

```text
服务端可以召回较多候选
→ 向模型只返回有界 Candidate Cards
→ 模型选择少量 videoChunkId
→ 按 ID + 字段 + 时间范围 + token 预算展开 Evidence
→ Evaluator 只审查所选详细证据和其他候选的轻量排名元数据
→ Chat 只接收获批结论、ID、时间范围和 claim
```

即把 Agent context 视为有界工作集，而不是完整证据仓库。

### 紧急防护措施

在分层召回改造完成前，临时措施只用于降低失败概率，不标记为根治：

1. 13 集不默认一次全选，优先先按集数或时间段缩小 Scope。
2. 多集范围继续使用较小 `top_k`，不用提高 `top_k` 解决召回不确定性。
3. 检索调用次数和 `top_k` 同时受总 evidence 预算约束，不只分别受数量上限约束。
4. 在 Query、Evaluator、Chat 每次 Provider 调用前记录脱敏的 section 大小、估算 tokens、hit 数、unique hit 数、Series 数和历史消息数。
5. 字符预检仍保留作为最后一层 fail-fast，但不将“更早失败”当作“问题已解决”。

### 根治任务

#### P0：将候选召回与详细取证拆开

- 定义严格有界的 `CandidateCard`，只包含 ID、排名/分数、集数、时间范围、有界摘要、少量命中片段和可展开字段。
- `search_video_chunk_evidence` 只返回 Candidate Cards，不再返回全量 transcript/events/context。
- 新增 `get_video_chunk_evidence`，必须传入有界 `videoChunkIds`、字段集、可选时间范围和 `maxTokens`。
- 详细 evidence 只能按 cue、event 和段落边界裁剪，返回 `partial`、`omittedFields`、`omittedCueCount` 等不完整性信号。

#### P0：从 char budget 切换为 token-aware packing

- 按 provider/model 维度配置 context window、输出预留和 safety margin。
- 优先使用对应 tokenizer；没有精确 tokenizer 时使用对中文、日文和 JSON 安全的保守估算，不使用固定的英文字符比例。
- 分别设置 `maxToolResultTokens`、`maxEvidenceTokensPerTurn`、`maxHistoryTokens` 和 `outputTokenReserve`。
- 超预算时按候选排名和证据优先级装箱，返回 partial 结果，不将本轮整体失败作为正常流程。

#### P1：分离 Agent 状态、历史记忆和 API DTO

- 完整 Evidence 继续保留在 DB/Runtime canonical state，模型上下文仅保留稳定引用和当前工作集。
- 历史记忆保留用户目标、约束、已确认 claim 及其 `videoChunkId`/时间范围、未解问题和最近计划，不回放历史全量检索结果。
- Evaluator 只接收已选中详细 Evidence，未选候选只接收 Candidate Card/调用元数据。
- Chat 只接收获批草稿、claim、`videoChunkId` 和获批时间范围，不再接收原始 transcript/events/context。

### 验收标准

- 使用真实 13 集数据执行单一集数定位、跨集检索、多义人物/事件和长对话四类场景，Query、Evaluator 和 Chat 均不超过各自 token 预算。
- 同一请求在 1、6、13 集 Scope 下均有可解释的预算构成；集数或 Series 扩容不能使模型输入无界增长。
- 单个异常肥 Chunk 和“3 轮 × 4 次检索 × 20 hits”的合成极端用例均不得让 Tool Result 突破配置的 token 上限。
- 超出详细 Evidence 预算时返回有明确标记的 partial 结果，可继续答题或发起更精确的取证，不再整轮报 `AGENT_CONTEXT_BUDGET_EXCEEDED`。
- 通过埋点可直接看到每阶段 `input_tokens`、Tool Result tokens、Series Context tokens、History tokens、unique hit 数、expanded hit 数和 omitted 数。
- 容量压测不只验证“不爆”，还要比较召回覆盖率、证据选择正确率和 lost-in-the-middle 退化。

### 待补证据

下次复现 5–6 集 overflow 时，保留并回填以下信息，但不记录用户原文或原始 Evidence 正文：

- conversation ID、message ID、request ID、阶段和 Provider error code；
- Scope 中的 Series/视频数量；
- 调用次数、每次 `top_k`、各次 hit 数和全局 unique hit 数；
- 固定 Prompt、Tool Schema、Series Context、History、Reference、Candidate/Evidence 的分项字符数和估算 token 数；
- Provider 返回的实际 input/output token usage（如可用）。

## 2026-09-19：Codex 对标后的两层 Tool 设计定稿

### 决策状态

- 状态：设计已确认并完成 Agent 接入实现；真实 13 集容量压测与观测指标仍待执行。
- 决策来源：用户明确指定；并已对照 `../codex` 的 `skills.list` / `skills.read`、Tool 自有输出上限和 Harness compact 机制。
- 本节覆盖本日志前文中 `search_video_chunk_evidence + get_video_chunk_evidence` 等临时命名与任何三层 Tool 提案。历史故障事实保留，但实现目标以本节 Tool 清单为准。
- Tool 名称按用户原文固定，包括 `read_trieve_item`、`update_draft_tem`、`get_draft_catelog`；未经用户确认不得自行纠正拼写或增加 Tool。

### Long-horizon 不变量

1. 只设置 Catalog 与 Read 两层 Tool。
2. Catalog 只返回有界 description、稳定 handle/`chunkId` 和必要元数据。
3. Read 按 handle/`chunkId` 精读一个 item；小对象可完整返回，大对象在同一个 Tool 内通过 `next_cursor` 分页。分页不是第三层。
4. 每个 Tool 自己保证序列化后的单次输出不超过硬上限；Agent/Skill 不负责修补无界 Tool Result。
5. 完整 Retrieval Item、Draft Item 和最终 Video Item 保存在外部 canonical store；messages 只保存当前工作集。
6. Skill 只编排 Tool 使用顺序；Harness 负责累计历史的 token 水位、compact 与换窗。

### Tool 清单（不得擅自增删或改名）

| Agent | Tool | 语义 |
|---|---|---|
| Query Agent | `retrieve` | 返回有界 Retrieval Catalog |
| Query Agent | `read_trieve_item` | 按 `chunkId` 精读 Retrieval Item，可在 Tool 内分页 |
| Query Agent | `update_draft_tem` | 以 `chunkId` 为去重键执行 Draft upsert：不存在则 insert，存在则 update |
| Query Agent | `remove_draft_item` | 按 `chunkId` 删除 Draft Item |
| Query Agent | `get_draft_catelog` | 返回有界 Draft Catalog |
| Query Agent | `read_draft_item` | 精读一个 Draft Item，可在 Tool 内分页 |
| Evaluator | `retrieve` | 与 Query 相同的 Retrieval Catalog Tool |
| Evaluator | `read_trieve_item` | 与 Query 相同的 Retrieval Item Read Tool |
| Evaluator | `get_draft_catelog` | 只读 Draft Catalog |
| Evaluator | `read_draft_item` | 只读 Draft Item |
| Chat Agent | `remove_video_item` | 按 `chunkId` 删除最终 Video Item |
| Chat Agent | `update_video_item` | 按 `chunkId` 新增或修改最终 Video Item，并去重 |
| Chat Agent | `get_draft_catelog` | 只读 Draft Catalog |
| Chat Agent | `read_draft_item` | 只读 Draft Item |

明确不实现 `submit_draft`、`submit_video_item`、第三层 Evidence/Draft Tool、Evaluator Draft mutation Tool 和“控制 Gateway Tool”。

### `videoChunkId` 核验结果

- `videoChunkId` 是 canonical UUID，也是 `video_chunk.video_chunk_id` 的全局主键。
- 当前通过 `UUIDv5(videoId, sequenceNo)` 确定性生成；准确 seed 为 `anivag:video-segment`。
- `sequenceNo` 才是单个 Video 内的切片顺序号；数据库另有 `(video_id, sequence_no)` 唯一约束。
- 因此 Draft 与最终 Video Item 以 `videoChunkId` 去重符合当前领域模型。

代码证据：`server/identity.py:30-36`、`server/model/video.py:113-143`。

### Evaluator advice 与放行链路核验结果

Evaluator 当前不通过 Tool 控制 Gateway/审批闸门。旧链路继续保留：

```text
Evaluator 输出 EvalDecision(action, advice)
→ Runtime 写入 LoopTurn.review
→ action=continue：LangGraph 回到 Query，Query 读取 review.advice
→ action=complete：LangGraph 进入 Chat
→ Chat 完成后 Runtime 产出 final output
→ AgentChatService 持久化并发布 SSE
```

代码证据：`server/agent/state.py::EvalDecision`、`server/agent/runtime.py:149-187`、`server/service/agent_chat.py:583-600`。

### 尚未发生但必须提前防止的同源隐患

| ID | 隐患 | 约束 |
|---|---|---|
| `CTX-R12` | Read Tool 名为“全量读取”而实现成单次无界返回 | `read_trieve_item` / `read_draft_item` 必须按序列化大小硬封顶并支持 cursor |
| `CTX-R13` | Catalog description 自身随候选数无界增长 | `retrieve` / `get_draft_catelog` 必须有最大条目数、响应预算和 `next_cursor` |
| `CTX-R14` | `update_draft_tem` 对相同 Chunk 重复 insert | Store 对 `videoChunkId` 建唯一约束或原子 upsert |
| `CTX-R15` | Skill 中出现未注册或旧名称 Tool | 三个 Agent 的 Skill 与注册 Tool 清单做一致性测试 |
| `CTX-R16` | 把 Evaluator 的审批路由误实现成 Tool Call | 保留结构化 `EvalDecision` 与 LangGraph conditional edge |
| `CTX-R17` | Chat 继续接收完整 selected evidence 快照 | Chat 只能通过 `get_draft_catelog` / `read_draft_item` 按需读取 |

### 2026-09-19 实现回填

- 新增每 Kick 隔离的 `AgentWorkspace`；完整 Retrieval、Draft、获批 Draft 与 Chat Render 状态不再进入跨角色 Prompt。
- Query 注册且仅注册：`retrieve`、`read_trieve_item`、`update_draft_tem`、`remove_draft_item`、`get_draft_catelog`、`read_draft_item`。
- Evaluator 注册且仅注册：`retrieve`、`read_trieve_item`、`get_draft_catelog`、`read_draft_item`；Server 通过 Draft read audit 阻止“未读完整草稿却 complete”。
- Chat 注册且仅注册：`remove_video_item`、`update_video_item`、`get_draft_catelog`、`read_draft_item`；旧 `submit_video_item` 已从业务代码删除。
- Tool 单次输出统一执行 UTF-8 字节硬上限；Catalog 和 Read 分别通过 item cursor 与 content cursor 续读。
- Query / Evaluator 的 LangChain 完整消息由 `max_input_chars` 检查；Chat 自定义 function loop 已补齐同等的逐请求累计消息检查。
- 三个 Agent Skill 均加入“作品无关”约束，禁止具体动画、角色、集数、剧情、台词或镜头特例。
- 新增配置 `max_tool_result_bytes=16384`、`max_catalog_items_per_page=20`，Tool-call 默认预算调整为 24。
- 已覆盖 Tool 白名单、分页与字节上限、Workspace scope、Draft upsert、获批快照、Evaluator 读审计、Chat 累计 context 拦截以及 API/持久化回归。

## 2026-09-19：Codex-style Context Window / compact 已实现

### 模块边界

| 文件 | 唯一职责 |
|---|---|
| `gateway/context_window.py` | Provider 无关的 token ledger、阈值判断、replacement history、window identity 与硬上限恢复 |
| `gateway/agent_model.py` | 把相同 Controller 接入自定义 function loop 和 LangChain/LangGraph middleware，并采集真实 Provider usage |
| `server/agent/context.py` | 将 AniVAG canonical Workspace 投影成有界 navigation checkpoint |
| `server/agent/workspace.py` | 保存模型消息之外的 Retrieval/Draft/approved Draft/render canonical state |
| `server/agent/query.py` / `evaluator.py` / `chat.py` | 定义角色固定锚点、一次性初始快照和 checkpoint callback |
| `server/config/agent.py` / `settings.py` | 保存窗口、预留、软阈值和目标阈值配置 |
| `server/bootstrap.py` | 构造不可变 ContextWindowPolicy 并注入三个生产模型 |

不新增 `utils.py` 或 `common.py`。只服务于 compaction wire format 的常量保留为 `gateway/context_window.py` 私有常量；现有 `gateway/constants.py` 只继续保存跨模块 Provider 常量。

### 已实现行为

1. 每次模型请求都在发送前计量完整 messages 与 Tool schema，而不是只计量最初 Prompt。
2. 第一请求使用 UTF-8 byte/3 的保守估算；收到 Provider usage 后，用真实 `input_tokens` 修正下一请求的累计水位。
3. 默认 48K tokens 自动换窗；64K context 中预留 4096 output 与 4096 safety，硬输入边界为 55,808 tokens，目标压回 24K。
4. compact 创建新的 `window_id` / `previous_window_id` / `window_number`，并替换 LangGraph 或自定义 loop 中的真实消息状态，不做只影响单次请求的临时遮罩。
5. 固定 System/Skill 与当前任务锚点保留；已消费的旧 history、Tool batch 和中间轨迹替换为活动索引与有界 Workspace checkpoint。
6. 最新 Tool batch 在软 compact 时保留一次，避免刚检索的 Evidence 在下一次推理前被释放。
7. 硬边界无法满足时，优先把最大 Tool Result 替换为可恢复 receipt；receipt 指引使用 Catalog/Read Tool 检查 canonical state，并禁止盲目重放 mutation。
8. Query、Evaluator、Chat 分别取得自己角色的 Workspace checkpoint；checkpoint 仅含 ID、数量、范围和 render outline，不含完整 Evidence 或 Draft claim。
9. Tool 名称与权限清单没有变化，没有新增 compact Tool、submit Tool 或 Gateway Tool。

### 新增配置

| 环境变量 | 默认值 | 语义 |
|---|---:|---|
| `ANIVAG_AGENT_CONTEXT_WINDOW_TOKENS` | `64000` | Provider context window |
| `ANIVAG_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS` | `4096` | 输入侧安全余量 |
| `ANIVAG_AGENT_AUTO_COMPACT_TOKENS` | `48000` | 自动换窗软阈值 |
| `ANIVAG_AGENT_COMPACT_TARGET_TOKENS` | `24000` | replacement history 目标水位 |
| `ANIVAG_AGENT_MAX_OUTPUT_TOKENS` | `4096` | 输出预留，同时参与硬输入预算 |

### 已发生问题与新增防线

| ID | 问题 | 本次处理 |
|---|---|---|
| `CTX-R18` | 只限制单次 Tool，累计 ToolMessage 仍随轮次增长 | 每次 Provider 请求前自动计量并换窗 |
| `CTX-R19` | 只看字符数，中文/JSON/Tool schema 与真实 token 使用错位 | Tool schema 纳入估算，并用 Provider `input_tokens` 反馈校准 |
| `CTX-R20` | compact 只修改临时请求，LangGraph state 下一轮又恢复旧历史 | 使用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 安装 replacement history |
| `CTX-R21` | 原始 Prompt 是一个不可拆 user message，旧 history 被永久固定 | 拆为固定 System/Skill、稳定任务锚点、可回收初始 snapshot |
| `CTX-R22` | compact 在模型消费前释放刚返回的 Retrieval Evidence | 软阈值始终保留最新完整 Tool batch一次 |
| `CTX-R23` | checkpoint 把完整 Evidence/Draft 再注入，换窗后立刻重新膨胀 | checkpoint 只允许 navigation metadata；自身再受 12K 字符硬限制 |

### 尚未发生但必须防止的同源隐患

| ID | 隐患 | 当前约束 |
|---|---|---|
| `CTX-R24` | 不同并发 Kick 共享 token ledger/window ID，互相污染 | Controller 与 Workspace 都按单次 invocation 创建 |
| `CTX-R25` | Provider 未返回 usage 时错误地认为没有预算风险 | 回退到保守 UTF-8 byte/3 估算，不关闭 compact |
| `CTX-R26` | Tool schema 扩大后吞掉输入空间，但预算只看 messages | `measure_context` 同时序列化 messages 与 contract |
| `CTX-R27` | 硬恢复释放 mutation receipt 后模型重复执行写操作 | receipt 明确禁止因释放而重放 mutation，状态从 Catalog/Read 恢复 |
| `CTX-R28` | 固定指令或单条用户输入本身已超过窗口，却伪称 compact 成功 | 无可安全压缩对象时在 Provider 调用前 fail closed |
| `CTX-R29` | 把本地 checkpoint 当作原始证据，摘要失真后污染结论 | checkpoint 明示仅用于导航；事实必须重新通过 Read Tool 获取 |
| `CTX-R30` | 多次换窗没有身份链，线上无法还原何时丢失上下文 | 日志记录 window number、current/previous ID、前后 token/字符数与 released Tool 数，不记录 Evidence 正文 |

### 验证状态

- Context/Gateway/Agent/Tool/配置聚焦回归全部通过；包含六轮多 Tool batch 压测、Provider usage 校准、最新 batch 保留、硬边界 release 和固定前缀 fail-closed。
- 全量回归：`831 passed, 8 skipped, 9 failed`。
- 9 个失败全部在 `tests/server/test_sse_events.py`，原因是另一组未完成的 Parse Job timing 改动为 `ParseJobState` 新增两个必填字段而旧 fixture 未同步；本次实现未修改该范围。
- 尚待真实 1/6/13/26 集质量与容量实验；单元/合成测试证明 Harness 有界，不替代真实召回覆盖率和答案质量评测。
