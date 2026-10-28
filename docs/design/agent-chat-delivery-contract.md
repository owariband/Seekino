# Agent Runtime：Query / Evaluator Loop 与 Chat 工具交付

> **Event 业务工具最终合同（2026-10-17）**：以 [Sub-Chunk / Event TRD](sub-chunk-trd.md) 为唯一实施依据。Event ES 独立提供词法、文本及抽帧视觉向量检索，不新增 Event / Frame SQL 表；完整 Timeline 与既有 Chunk Evidence 保持权威。Query / Evaluator 可直接细搜或限制已召回 Chunk 再搜，不增加意图路由器。下文 2026-09-18 的模型、状态及 SSE 描述保留原记录时点，不作为当前实现状态的证明。

日期：2026-09-18。状态：三 Agent 代码已按用户确认的结构完成本地开发，已通过仿真模型、SQLite、ASGI 与 DOM 回归，并完成真实 DeepSeek Query / Evaluator / Chat + 开发 MySQL / Qdrant 联调；浏览器媒体回放仍需单独验收。Query Agent 与 Evaluator Agent 最多循环 3 轮；事实性结论必须提交非空 `selected_video_ranges`。Evaluator complete 后，存在 approved evidence 时 Chat Agent 首次以 `tool_choice=required` 进入有边界自主循环，并至少调用一次 `submit_video_item({video_chunk_id, claim})`；成功提交后恢复 `auto`。服务端从 Evaluator 已放行的 `selected_video_ranges` 绑定播放范围，完整校验并提交后才交付成功结果。这一实现替代此前强制批量提交视频、默认一次返工和直接交付审核草稿的旧基线。

本次删除没有独立产品需求的 Agent Session 层。当前资源关系为 Workspace → Conversation → Message，Conversation 直接保存 workspaceId；旧 Session 表、CRUD、DTO 与前端创建流程均不再属于当前契约。本次未对真实开发 / 生产库执行 DDL；create_schema 会拒绝旧结构，完整迁移步骤见 [Agent API 第 8 节](agent-api.md#8-已有数据库升级)，迁移保留 Conversation ID 与全部消息。

检索范围改为每次 messages/send 的 seriesIds：省略或 [] 表示当前 Workspace 全部 READY 动画，显式选择未 READY 返回 409。Conversation 不再保存 seriesIds；每次 Kick 启动前展开实际范围，USER / ASSISTANT 消息都公开保存该快照。动画多选复用已有 GET /api/series/list?workspaceId=... 的 seriesId / name / status，改变选择只影响下一次发送。

当前检索 Runtime 入口：[video_search/runtime.py](../../agent/chat/video_search/runtime.py)。旧离线示例已因导入路径失效于 2026-10-24 清理，探索结论见[归档总结](../dev/design-experiment-cleanup.md#过时架构图和代码示例)。完整结构与历史提案见 [agent-runtime-design.md](agent-runtime-design.md)，参考项目源码见 [agent-loop-references.md](agent-loop-references.md)。当前没有跨进程 delta 重放、崩溃遗留 PROCESSING 恢复或独立 Clip 端点。

## 1. 三个 Agent 的职责

| 角色 | 输入 | 输出 | 执行次数 |
| --- | --- | --- | --- |
| Query Agent | 用户问题、固定 Scope、原计划、历轮计划 / 检索 / 草稿 / review / advice | 本轮计划、必要召回、draft output 与真实检索证据 | 最多 3 轮；SDK 内部自行调用模型 / 工具，最终返回 QueryDraft |
| Evaluator Agent | Query 草稿与完整检索结果，以及用户问题和上下文 | action: complete / continue；advice | 每个 Query 轮次一次，最多 3 次 |
| Chat Agent | 用户问题与对话上下文、最终获批的 Query 草稿和证据 | 自主编排 Markdown；有 approved evidence 时至少提交一个 `submit_video_item({video_chunk_id, claim})` | 审核放行后一个交付阶段，不计入 Query / Evaluator 轮数；内部模型调用受统一次数与 deadline 限制 |

continue 携 advice 回到 Query，由 Query 决定重新召回还是复用已有证据。Evaluator 不通过 correctedQuery 直接控制工具。第 3 轮仍 continue 则失败结束，不启动最终 Chat。

Chat 负责正文表述、视频块的取舍/顺序和每个已提交片段的 `claim`；可选范围只限 Query/Evaluator 已获批 Evidence。Chat 不重新检索或引入获批范围外的视频，也不能填写时间、URI 或标题。Server 拒绝重复/越权 ID，从 Evaluator 已批准的 `selected_video_ranges` 原样绑定时间，并从可信 Evidence 补齐其余媒体字段。Evaluator 审核的是 Query 草稿及证据；最终 Chat 正文没有再次经过审核，不能宣称逐字正文已经二次放行。

Query / Evaluator 的输入包含历轮工作记录；最终 Chat 的工作材料只传当前获批草稿与选中证据，避免将已拒绝草稿、未选中检索片段继续传给整理节点。用户问题与对话历史作为回答背景保留。

## 2. 图的流程与输出

```text
START
  → 创建本轮 LoopTurn
  → Query：结合原计划、此前各轮记录与 advice 生成本轮计划
      ├── 重新检索 → Retrieval Tool → 完整 Evidence
      └── 复用已有 Evidence
  → Query：生成 draft output，选择真实 videoChunkId
  → 代码：校验 Scope / ID / 时间并绑定完整 Evidence
  → Evaluator：审核草稿与完整检索结果
      ├── continue + advice
      │   ├── 尚未到第 3 轮 → 创建下一轮 → Query
      │   └── 已完成第 3 轮 → 失败，无 Chat 正文输出
      └── complete
          → Chat：读取获批材料；有 approved evidence 时首次 tool_choice=required
          → 模型：至少提交一个视频；成功后恢复 auto，自主输出 Markdown、继续调用或结束
          → 代码：逐次校验未重复的获批 ID，用可信 Evidence 填充 URI / 标题 / 时间并返回确认
          → 模型：读取 acknowledgement 后自主继续，直到正常结束
          → 代码：要求至少一个非空 Markdown 块，形成有序 renderBlocks
          → Output Node：有序 Markdown / video 块 + 服务端可信 hits → final_output
          → END

Service：在一次完成写入中保存 message.content 与 evidence-only searchResult
  → 提交成功后发送 HTTP 完成事件 / 消息快照
```

三个 Agent 是三个模型角色。检索、校验、Output Node、事务与 SSE 封装属于代码职责。保留确定性 Output Node，用于封装 Chat 正文与已获批 Evidence；它不调用第四个模型。

内部 final_output 复用现有 VideoChunkSearchResult：

- 内部 agentReply.message 与 assistantMessage.content 是所有 Markdown blocks 以空行连接的兼容正文；真实显示顺序以 `renderBlocks` 为准。Agent HTTP/SSE 的公开 `searchResult` 使用 evidence-only 投影，不再返回 `agentReply`。独立 `/api/search` 仍返回其确定性 `agentReply`。
- hits 是 Chat 实际提交的获批证据子集，顺序与 video blocks 的提交顺序一致；ID / 时间仍来自可信 Evidence。
- renderBlocks 是有序轻量数组：Markdown 块携带文本，video 块只携带 videoChunkId，由前端与同消息 hits 关联。
- resultCount 等于 hits.length。
- query 为按选择顺序最后一个 hit 的真实来源 query；不作事实性结论时允许空选择，并使用最后实际检索 query。多次检索可以合并选择，单个公开 query 不代表所有 hit 来自同次召回；完整调用和逐片段来源保留在 LoopTurn。
- 公开助手消息 modelName 表示最终 Chat 的主模型；发生 Lite fallback 时只记录 warning，不改写该字段。USER / ASSISTANT 消息的 seriesIds 表示本次实际 Scope；hits[].evidence.seriesId 必填，来自批量 MySQL join 回源，并属于此范围。

## 3. Harness 的 Loop / Kick / Turn

### 3.1 已确定的三个业务层级

设计来源：2026-09-15 用户讨论并要求持久化。采用 Loop 定义运行规则、kick() 启动一次完整任务、turn() 执行一轮业务工作的层级；Agent 内部模型 / 工具 Step 由所选 Agent 框架封装。

| 层级 | 一次的含义 | 职责与数据归属 |
| --- | --- | --- |
| Loop | 可复用的运行规则与 Graph 定义 | 定义 Query / Evaluator 的连接、continue / complete 路由、最多 3 轮、最终 Chat 与 Output Node；依赖可共享，不保存跨请求共用的可变工作状态 |
| Kick | 一次 kick() 启动的用户请求 | 固定用户输入、Conversation / Message 身份、历史与 workspace / Series Scope；独立保存全部业务 Turn、最终输出及这次执行的交付生命周期 |
| Turn | Kick 内一次 Query → Evaluator | 保存本轮计划、完整检索结果与复用来源、draft output、review / advice、状态；内部可以包含多次模型或工具调用 |

这里的“一次对话”指用户发起一次请求。同一 Conversation 的后续消息会再次 kick()，创建新的独立执行状态；不会把整个 Conversation 或每次模型调用都算成一个 Kick。公开资源为 Conversation / Message，不新增公开 Kick / Turn 资源。

Kick 对应此前的 Run，正式执行状态继续使用 RunState；Turn 对应 LoopTurn。[AgentRuntime](../../server/agent/runtime.py) 已提供 kick(inputs, emit=..., cancellation=...) 与 turn(state)。编译 Graph 的节点为 turn → 条件回边 / chat → output；turn 节点内部执行 Query、真实证据绑定和 Evaluator。没有另建 KickState / TurnState 包装。

一次 Kick 的生命周期：创建独立状态 → 执行第 1 至第 3 个业务 Turn → 获批后进入最终 Chat 自主工具循环 → 生成有序 Markdown / video blocks → 完整校验 → Output Node 封装 → Service 成功提交 → 发送完成事件。第 3 个 Turn 仍 continue 时失败，不调用最终 Chat。最终 Chat 属于本次 Kick 的交付阶段，不增加第 4 个 Query / Evaluator Turn；获批 LoopTurn 只保存完整校验后的 render draft，不保存失败循环的半成品。

### 3.2 Kick 的执行状态与各轮上下文

Conversation 是已有持续对话；每次 Kick 创建一个 RunState；其 turns 是本次执行内的 LoopTurn，最多 3 轮。RunState 不是新增数据库表。

```text
RunState（一次 Kick / Graph 执行，未持久化）
├── inputs：RunInputs，固定身份 / 问题 / 历史 / Scope / requested_top_k
├── turns：已完成业务轮次的 LoopTurn[]
│   └── LoopTurn
│       ├── number / status：轮数与 complete / continue
│       ├── plan_context：QueryDraft 公开返回的简洁计划
│       ├── retrieval_calls：每次实际 query / top_k / channel_weights / 完整 SearchResult
│       ├── retrieval_result：本次 Kick 已实际召回、当前可用的完整证据
│       ├── evidence_source_turns：所选 videoChunkId → 实际召回轮次
│       ├── query_draft：plan_context / message / selected_video_ranges
│       ├── draft_output：草稿正文与确定性绑定的完整选中 hits
│       ├── review：Evaluator action / advice
│       └── rendered_answer：已完整校验的有序 Markdown / video blocks
├── control：本次 deadline、取消 Event 与 emit callback
└── final_output：最终 Chat 正文 + 同一份获批 hits
```

共享输入在 kick 时深复制；原计划从第一轮 plan_context 取得。Query 的下一轮 Prompt 显式包含输入、原计划和先前全部轮次计划、每次检索的完整结果、草稿与 review / advice。模型未公开返回的隐藏推理不被当作已保存信息。本轮调用记录先在 turn 的局部列表累计，Query 与评审成功后创建 LoopTurn 写回 Graph；没有向前端宣称逐节点进度，也没有持久化失败中间轮次。

编译 Graph 和依赖可共享；每次 kick 的 RunState、工具闭包、记录与正文缓冲独立。节点返回新的 turns 列表，已完成轮次不被后轮或 Chat 修改。LoopTurn 用 frozen dataclass，检索结果与选中证据使用深复制，未使用 checkpoint 或会重复追加整轮的 reducer。

### 3.3 Agent 内部 Step、预算与停止

[SDKQueryAgentModel](../../gateway/agent_model.py) 使用已安装的 LangChain create_agent、StructuredTool、ToolStrategy 与 ModelCallLimitMiddleware 封装内部模型 / 工具循环。Query 最终返回 QueryDraft(plan_context, message, selected_video_ranges)，不再另调用旧 RetrievalIntent / ChatAnswer。事实性结论要求该范围集合非空；不作事实性结论时可以明确返回空集合并表达不确定性。第一轮必须实际检索一次；后续轮可再次检索、多次检索，或根据历史记录复用真实旧证据。SDK 的这些 Step 属于同一个业务 Turn，不增加 Query / Evaluator 轮数。[LangChain 官方接口](https://reference.langchain.com/python/langchain/agents/factory/create_agent)

约束分开控制，默认值来自 [AgentServiceConfig](../../server/config/agent.py)：

| 约束 | 默认 | 实际执行位置 |
| --- | --- | --- |
| Query / Evaluator 业务 Turn | 最多 3 | Runtime 路由；最后仍 continue 则失败 |
| Query 内部模型调用 | 每 Turn 最多 6 | SDK ModelCallLimitMiddleware，超限报错 |
| 实际 Retrieve Tool 调用 | 每 Turn 最多 4 | tools.py 计数，包括失败尝试；超限不再检索 |
| top_k / 最终选中 hits | 默认 8，上限 20 | 每次 Tool 和最终去重选择均不超过 min(请求 TopK, 配置上限)；多次召回不能绕过最终数量预算 |
| 单次模型输出 token | 4096 | Query / Evaluator / Chat 的 Provider 参数 |
| Kick 总执行时间 | 180 秒 | monotonic deadline；模型、工具、Chat 工具循环和 Output 边界检查 |

SDK 在每次模型调用前后执行 check_control；工具在调用前后检查，Chat 在每个 `submit_video_item`、工具确认、最终正文和 Output 边界再次检查。取消 Event 停止后续步骤。总 deadline 和取消都是协作检查，不能硬终止正在阻塞的同步网络调用；模型单次请求另配置有限 timeout，实际结束时间可能超过总 deadline。每次 Provider 调用会采集返回的 usage，但这里仍没有跨角色累计 token 用量或统一总 token 账本，输出上限和调用次数共同控制开销。

SDK 每次 run_query 创建新的 Agent / messages，不使用共享 checkpoint。正式默认装配中，Query、Evaluator、Chat 都以 `config.yaml` 的 `agent_model.model`（DeepSeek Flash）为主模型，并允许通过各角色环境变量覆盖；`agent_model.reasoning_effort` 固定为 `none`，请求显式关闭 thinking。Query 使用 LangChain Tool Calling，Evaluator 使用 text-only Chat Completions 的单个 required function call，Chat 使用有边界的 function-calling loop；Parser 的多模态 Responses 路径与此独立。`video_parser.transcript_model`（Ark Doubao Lite）是默认 fallback。Provider 请求或模型响应异常时，失败的单次模型调用在当前 Agent 消息 / Tool 状态上原位切换到 Ark Lite；此前完成的业务 Tool 结果与 acknowledgement 继续复用，不重新调用业务 Tool。业务 Tool 错误、取消 / deadline / 预算等控制错误和本地输入校验错误不会触发模型降级。Chat 只向模型公开 `submit_video_item({video_chunk_id, claim})`；存在 approved evidence 时首次 `tool_choice=required`，成功提交至少一个后恢复 `auto`。每轮可以产生 content、一个/多个 tool calls 或 stop；tool acknowledgement 返回模型后继续下一轮。Query / Chat 的自有 Provider client 按 Kick 内调用创建并关闭；Evaluator client 在服务装配时创建，并在执行线程退出后沿 ApplicationServices → AgentChatService → AgentRuntime 关闭链释放。显式注入测试 client 时由调用方拥有其生命周期。Agent 工具 Schema 和构造器统一在 `server/agent/tools.py`；角色模块只绑定当前 Kick / Turn 状态并把 `BaseTool` 列表交给 Gateway。

### 3.4 DeepSeek Harness 与 Codex 的术语对应

2026-09-15 只读核对本地 ../deepseek-harness：ReactLoopAgent 确有 private async kick(): Promise<void>，内部为 while (await this.turn()) {}。kick 是驱动器一次从 running 到 idle 的运行；turn 结束后如果 Inbox 仍有排队输入，会返回 true，使同一次 kick 继续处理下一个用户 Turn。因此它的 kick 不固定对应一条用户消息，其 turn 是用户任务边界。[kick 源码](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L210)、[turn 与队列判断](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L246)

其 kick 是私有零参数方法，外部通过 followup / steer / send 唤醒驱动器，输入存于 Inbox。其 step 方法也在该仓库自己执行模型流和工具调用，并未将内层循环交给 LangChain。[输入入口](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L113)、[内部 Step](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L332)

Seekino 借鉴 kick → turn 的组织方式，同时按本项目约定：一次 Kick 固定一个用户请求，内部 Turn 固定一轮 Query / Evaluator。Codex 的 Thread 对应 Conversation，Turn 对应这里一次 Kick 的完整任务；Codex 内部再进行模型请求和工具执行。[本地 Codex 实现](../../../codex/codex-rs/core/src/session/turn.rs#L142)、[Codex 官方术语](https://learn.chatgpt.com/docs/app-server#core-primitives)

### 3.5 AgentChat 应用层与 Harness 执行层

已按用户确认的结构迁移。旧 server/service/agent.py 已删除，业务和测试导入已更新；没有空转发兼容模块、BaseAgent、Registry 或插件框架。server/agent 是底层逻辑目录，不依赖 FastAPI、Request、StreamingResponse、app.state 或 Conversation Repository，可由 HTTP、CLI 或测试调用。

```text
server/
├── service/
│   ├── agent_chat.py        会话归属 / 历史 / 消息事务 / 执行任务与订阅
│   └── video_retrieval.py   正式 READY / 双路召回 / RRF / MySQL Hydrate
├── agent/
│   ├── __init__.py
│   ├── runtime.py           AgentRuntime：Graph / kick / turn / 停止与预算
│   ├── state.py             RunInputs / RunState / LoopTurn / QueryDraft / EvalDecision
│   ├── query.py             完整工作上下文 → SDK 工具循环 → QueryDraft
│   ├── evaluator.py         草稿 / 完整调用和证据 → complete / continue + advice
│   ├── chat.py              当前获批草稿与选中 hits → 有序视频工具调用 → Markdown
│   ├── tools.py             统一 Agent Tool Schema / 构造入口；固定 Scope、每 Turn 记录 / 预算 / 局部锁
│   └── output.py            真实 ID / Scope / 时间校验、证据绑定和最终封装
├── api/agent.py             JSON / SSE 协商、DTO 白名单编码、完整快照
├── config/agent.py          Turn / 内部调用 / top_k / token / deadline / 并发配置
└── bootstrap.py             装配三个模型角色、检索依赖、Runtime 与 AgentChatService

gateway/
├── agent_model.py           SDK Query 适配与文本 / 工具循环、DeepSeek → Ark Lite fallback
├── models.py                Evaluator 的 StructuredAgentModel 适配与跨 Provider fallback
└── usage.py                 各 Provider 调用 usage 采集
```

[AgentChatService](../../server/service/agent_chat.py) 校验请求与 Workspace 归属，优先查询原请求幂等消息；新请求解析 / 校验本次 Series Scope，在短事务检查幂等和 PROCESSING 门禁并提交 USER / PROCESSING ASSISTANT，然后由 Service 自有 ThreadPoolExecutor 执行任务。默认最多 4 个 Kick 同时执行，线程池等待队列未持久化；准备当前 Conversation 的历史与已保存的消息 Scope 后调用 Runtime.kick，历史逐条包含原有 seriesIds。JSON send 等待这个任务结果，POST SSE 订阅其公开事件。连接断开只 detach，不取消或重启 Kick。关闭自有服务时拒绝新执行、通知取消，并等待已有任务结束后才关闭数据库 / Qdrant；外部注入服务由调用方关闭。

Conversation 直接保存 conversationId / workspaceId / title / idempotencyKey / nextMessageSequence / lastMessageAt / 时间，不存 seriesIds；所有绝对时间统一为 signed INT64 / `BIGINT` Unix epoch 毫秒，媒体偏移和耗时为相对毫秒。Message 按 conversationId 挂载并保存实际 seriesIds。创建使用 POST /api/agent/conversations/create，JSON 为 workspaceId / title，无 Query 资源 ID；列表使用 GET /api/agent/conversations/list?workspaceId=...&limit=...。创建幂等唯一键为 (workspace_id, idempotency_key)，发送消息幂等仍为 (conversation_id, idempotency_key)。USER 内部保存原请求 requested_series_ids（[] 为全部），与展开实际 Scope 分开；ASSISTANT 对应字段为 NULL，公开 DTO 不暴露该字段。Repository 直接按 Conversation.workspace_id 校验 get / messages / start 的归属，不经 Session join。

[Retrieve Tool](../../server/agent/tools.py) 仅向模型公开 query / top_k / channel_weights。workspace_id / series_ids 取自本次 Kick 的固定 Scope，直接调用注入的 VideoRetrievalService.search，不请求自己的 /api/search。每 Turn 的工具闭包和局部 Lock 保证多个工具调用的预算及记录顺序；每次完整 SearchResult 记入 RetrievalCall。工具返回与固定 Scope 相同的 Series 集合，同次重复 chunk ID 拒绝；同一 Kick 的后续调用或后续 Turn 若再次命中已交给模型的 chunk，只返回 candidate 与摘要型 `reusedHits`，不重放完整 ASR / compact。Server 仍保留每次完整真实结果，并在绑定时按最新结果合并。Evaluator 读取当前全部检索调用、可用证据、选中证据、来源轮次和历史。

Runtime 只编排执行和返回 final_output；角色调用 Gateway 协议；state 不反向导入角色或 Service。Output 从实际召回结果绑定模型选择，保持 ID / 时间不被最终 Chat 改写。Query 上下文明确提供 selection_limit；最终去重选择超限在评审前失败，不事后截断已获批材料。Service 保存最终 content / searchResult 或 FAILED；只有数据库提交成功后才产生 answer.completed，API 负责 HTTP 编码。

异常职责分开：模型调用、审核拒绝、输出违规与预算停止使用 AgentModelInvocationError。检索依赖的业务异常仅在工具边界包装为 AgentToolInvocationError，保留原 cause 并穿过 SDK / Query；AgentChatService 在 Kick 边界解包，保持已有 READY / Scope / 索引一致性异常的 HTTP 映射，JSON 仍可返回原 404 / 409，不误报为模型 502。POST SSE 开始后发生这些错误，发送 AGENT_EXECUTION_ERROR 和可获得的 FAILED 数据库快照；不重试或降级绕过错误。

## 4. Chat 视频工具协议与 HTTP SSE

2026-10-17 最终检索合同：`search_video_events(query, event_types?, speaker?, video_chunk_ids?, top_k, channel_weights, cursor?)`。
本轮可信授权范围由 Server 注入，模型的可选 Chunk 集合只缩小范围；“必须真实调用检索”的规则应认可该工具。
top_k 按不同 Chunk 组计数，分页卡展示一个命中 Event，保留字段/语言及通道来源；服务端先核验来源版本，Agent 再读取完整 Timeline 与必要画面。speaker 只筛可靠 DIALOGUE；视觉索引抽帧不代表 Agent 已看图。
最终继续走已核验的 Chunk 视频交付，不以裸 Event ID 或分数代替证据，也不要求新增 HTTP 路径、消息表或前端 DTO。
完整候选/筛选和成功/失败调用进入后端 trace；未覆盖可改用 Chunk 检索，但失败调用不能满足首轮真实检索。模型上下文与前端工具状态沿各自通道处理，通用 Canvas 不加入 Event 集合或检索算法。
当前最终合同及运行覆盖验收见上述 TRD；下述历史工具参数、Runtime 或协议保留原记录时点。

Query 使用 SDK 工具循环；Evaluator 使用 `generate(inputs, schema_name, result_type)` 获取完整结构化判断。最终 Chat 默认使用 text-only DeepSeek Chat Completions，并只向模型公开下列业务工具：

```text
submit_video_item({video_chunk_id, claim})
```

存在 approved evidence 时，首轮使用 `tool_choice=required`，模型必须至少提交一个获批 hit；第一次成功提交后恢复 `auto`，后续可选择其任意子集并自主决定顺序。同一片段最多调用一次。`claim` 是该片段在最终回答中的简洁证据说明。服务端拒绝重复或未获批 ID，不接受模型提供的时间、URI、标题、集数或其他媒体元数据。Evaluator complete 只有在 `selected_video_ranges` 与获批 Evidence 一一对应且范围合法时才接受；Output 从该获批范围原样绑定时间，不使用 Chat proposed range 或 canonical fallback。每次接受调用后，服务端从获批 Evidence 构造可信视频项，再把 acknowledgement 返回 Chat。

普通 assistant `content` 可以出现在工具调用之前或同一轮中，每段非空 content 和每个已接受工具调用都依次形成 render block。Gateway 会在触发任何 callback 前校验当轮完整 envelope 和所有参数 Schema，采集每次 Provider usage，并要求循环正常 stop 且整体至少有一段非空 Markdown。`content_filter`、非法 tool call、调用次数耗尽或无可见文本都作为错误处理。

实际交接为：确认当前 review.complete → Chat 自主生成 content / tool calls → 服务端校验并返回 acknowledgement → Chat 继续至 stop → Runtime 对有序块和可信 hits 做整体校验 → 形成单个不可变交付结果。整体校验完成前不向前端产生 evidence、block 或 delta；中途错误没有成功 payload、final_output 或 completed，Service 保存失败状态并通知 answer.failed。

成功路径保持原子语义：Runtime 只向 Service 交出同一个完整的 blocks + 视频 hits 快照。Service 先用一次完成写入同时保存兼容 `assistantMessage.content` 与内含 `renderBlocks` 的 `searchResult`；提交成功后，POST SSE 才依次发布一条 evidence、N 条有序 block、一条完整 Markdown delta 和 completed。不会出现客户端收到成功块但数据库完成写入失败，也不会把 Provider token 或半截 Markdown 当作成功结果。

当前 Harness 使用直接注入的 emit callback，不使用 get_stream_writer 或 Graph messages 流来获取 Gateway token。LangGraph 承担状态和路由，HTTP 不直接暴露 Graph 内部 state。[LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)

| SSE 事件 | 发送时机 | 关键数据 |
| --- | --- | --- |
| answer.started | USER / PROCESSING ASSISTANT 已提交，订阅初始化 | conversationId、messageId、sequence、完整消息对 |
| answer.progress | 执行阶段切换 | 相同身份 / sequence、RETRIEVING / EVALUATING / RESPONDING |
| answer.evidence | 完整交付结果已在数据库提交后 | 相同身份 / sequence、从已提交快照读取的 evidence-only `searchResult`，含可信 URI / 标题 / 时间，不含 `agentReply` |
| answer.block | evidence 后按逻辑顺序发送 | Markdown 块，或只含 `videoChunkId` 的视频引用 |
| answer.delta | 与 evidence 相同的完成写入提交后 | 所有 Markdown blocks 的一次性兼容汇总；与已提交的 `assistantMessage.content` 相同 |
| answer.completed | 数据库提交成功后 | 相同身份 / sequence、COMPLETED 完整消息对 |
| answer.failed | 执行失败且 FAILED 状态已保存 | 相同身份 / sequence、error 与完整消息对 |
| answer.interrupted | 当前连接缓冲溢出或终态暂时无法确认 | 相同身份 / sequence、error；转用 GET 快照恢复 |

POST /api/agent/messages/send?conversationId=... 保持 Idempotency-Key 和原 Body，通过 Accept: text/event-stream 协商 SSE；默认 JSON 返回原 AgentMessageSendView，不新增路径或 202 语义。JSON 与 SSE 类型按 Accept 质量值选择。事件 id 为 messageId:sequence，sequence 是当前订阅的内存顺序，不是持久化 Message revision，不能用于跨 Worker 重放。

Service 每个执行的订阅队列最多 256 条。慢客户端溢出时停止缓存，发送 `answer.interrupted(error.code=STREAM_INTERRUPTED)` 并结束当前订阅；任务继续执行及提交。该 code 只说明连接中断，不表示数据库消息 FAILED。前端保持原消息事实，通过快照恢复；没有失败数据库快照时也不自行伪造终态。

GET /api/agent/messages/events?conversationId=...&workspaceId=... 保留 event: messages，默认每秒读取数据库完整 Conversation 快照、15 秒保活。刷新、重连与切换返回原 Conversation 可以恢复已持久化消息；订阅从不启动 Graph。PROCESSING 状态更新不改变 sequenceNo，恢复不能仅取 afterSequenceNo 新消息。相同 Key / 相同参数：PROCESSING 返回 409，终态读取原消息对，不重跑；改变内容、有效 TopK 或规范化原请求 seriesIds 返回 409。省略与 [] 等价；重放先读原消息，不重新展开全部动画，新增素材不会改变旧请求范围。

前端 [useAgentConversation](../../web/src/features/search/useAgentConversation.ts) 按 Workspace 创建和恢复 Conversation，动画多选与聊天身份独立。切换动画只更新下一次发送的 seriesIds，不自动切换 Conversation。切换 Conversation 时 abort 旧 POST 消费并关闭旧快照订阅，捕获的 active 实例拒绝旧回调；按 conversationId / messageId / sequence 关联正文与视频，终态不被旧 PROCESSING 覆盖。GET 恢复完整最终状态；若唯一正文 delta 在断线期间丢失，最终消息快照仍恢复完整正文。

当前任务、delta 队列和 Graph 中间状态都在单进程内；没有跨 Worker live 交接 / Last-Event-ID replay、执行租约、持久化 checkpoint 或崩溃 PROCESSING 扫描。硬退出仍可能留下 PROCESSING 并阻止该 Conversation 后续发送，不把快照恢复描述成任务恢复。

## 5. 视频如何交付和播放

Query 至少可读取 videoChunkId、videoId、seriesId、seriesName、episodeOrder、startMs、endMs、对白与事件、retrievalText、videoCompact 与来源信息，并返回 `selected_video_ranges`。事实性结论要求范围集合非空；seriesId 是真实完整 Evidence 的必填字段，不依赖模型补齐。

代码根据这些 ID 从真实检索结果绑定完整 VideoChunkSearchHit：

- 校验 videoChunkId 属于固定 Scope 的有效结果。
- 按选择顺序去重，确保 Candidate 与 Evidence 使用相同 ID。
- 校验 workspace / Series 归属、0 <= startMs < endMs <= durationMs，并确保分析窗口包围片段且不超过视频时长；MatchedEvidence 起止时间成对且合法。
- 将选中 Evidence 与 Query 草稿一起交 Evaluator，完整检索结果也供其审核。

通过后，有 approved evidence 时 Chat 必须至少提交一个获批项的 `video_chunk_id` 与 `claim`。服务端按 ID 从获批 `selected_video_ranges` 绑定时间，并从 Evidence 填充每个视频项的 URI、标题等可信字段。前端按 `renderBlocks` 定位视频块，并只从同消息 `searchResult.hits` 解析 `videoChunkId`；不靠自然语言反解析时间或 URI。Agent 的这些 `searchResult` 都是 evidence-only，不包含独立搜索摘要 `agentReply`。

原视频播放路径已经存在：

```text
GET /api/videos/content?videoId={videoId}&workspaceId={workspaceId}
```

解析每个 canonical VideoChunk 时会抽取该片段首帧，登记带原视频媒体偏移 `atMs` 的 keyframe；公开 Evidence 提供 `thumbnail` 和 `keyframes` 的 HTTP 图片 URL。旧数据若没有有效登记关键帧，会返回 warning 并允许无缩略图展示。候选列表只渲染首帧 `<img loading="lazy">`；用户点击候选后才挂载 [EvidencePlayer](../../web/src/features/search/EvidencePlayer.tsx) 和 `<video preload="metadata">`，因此未点击时不会为每张卡加载视频。

EvidencePlayer 在 loadedmetadata 后定位到 startMs / 1000；提前点击播放会等待 metadata，timeupdate 达到 endMs / 1000 时暂停并定位到终点，重播从范围起点开始。切换同一视频的片段也重新定位，切换时清理旧监听与播放请求。原始 file: / artifact: URI 不作为浏览器播放地址。Chat 正文由安全 Markdown 渲染器展示，不执行内嵌 HTML，并限制链接 / 图片协议。上述行为已由 DOM 媒体事件仿真验证，真实媒体回放仍待浏览器验收。

解析链已有规范 VideoChunk 的 chunkMediaRef，但片段 HTTP 访问尚未实现。后续候选端点（当前没有该路由或调用）：

```text
GET /api/video-chunks/content?videoChunkId={videoChunkId}&workspaceId={workspaceId}
```

MediaService 校验正式片段归属并解析现有媒体引用，复用 videoChunkId。直接片段媒体的播放坐标为 0 到 endMs - startMs，卡片仍展示相对原视频起点的媒体偏移；不能将原视频 startMs 用于已裁剪短片的 seek。

事件级更短范围、按需裁剪 / 压缩 Clip、邻片核验均未实现；不是这次三 Agent 命名即可取得的能力。

## 6. 当前实现与验证边界

本次最终回归：后端 699 passed / 8 skipped / 3 warnings，前端 14 个文件 / 120 项通过，
typecheck / build 通过。8 个跳过项均来自未配置真实 MySQL 测试环境的
`tests/server/test_mysql_integration.py`；更早的测试数字仅是开发阶段基线。

| 位置 | 当前已实现 | 仍需独立验收 / 开发 |
| --- | --- | --- |
| server/service/agent_chat.py / server/agent/ | 会话与 Harness 分离，三角色、SDK Query 工具循环、最多 3 Turn、完整工作记录、Output | 真实多轮模型质量、崩溃恢复、累计 token 与长任务容量 |
| gateway/agent_model.py / models.py / usage.py | DeepSeek Query / Evaluator / Chat、required → auto 视频工具循环、Ark Lite 单次调用原位 fallback、Tool 状态保留、单次输出预算、usage、Schema 与终态检查 | 真实跨 Provider fallback 与非空证据的三角色端到端运行 |
| bootstrap / settings / config | Query / Evaluator / Chat 注入、预算、每调用自有 client | 实际密钥 / 模型配置、开发 MySQL 当前 Schema |
| api/agent.py / router/agent.py | 同一路径 JSON / SSE、提交后完成、独立快照、幂等、断线任务继续 | 真实网络与多 Worker 部署的 live 交接 / delta replay |
| web client / useAgentConversation / SearchPage / EvidencePlayer | 正式消息与完整正文 delta、安全 Markdown、首帧缩略图、按需视频挂载、切换隔离、快照恢复、原视频范围播放 | 浏览器真实媒体加载、seek / endMs 交互与长对话 |

已运行的正式测试取代此前 /private/tmp 的 8 项草案验证：

| 测试 | 证明的范围 |
| --- | --- |
| tests/server/test_agent_runtime.py | complete 第 1 / 2 / 3 轮、三次 continue 零 Chat、原计划 / 全部召回 / advice、多调用 / 复用来源、事实性结论非空范围、approved evidence 至少一次视频工具调用、required → auto、重复 / 越权拒绝、可信字段绑定、有序 `renderBlocks`、最终 Markdown、并发 Graph / Tool 隔离、调用和最终片段预算 / 取消 / deadline |
| tests/server/test_agent_chat_service.py / test_api.py / test_sse_events.py | SQLite 持久化、JSON / SSE DTO、幂等、断线继续、commit 前不 completed、保存失败、缓冲溢出、消息归属；默认 READY 全集与显式多选、Scope 冻结、素材变化后旧 Key 重放 / 新 Kick 刷新、同 Conversation 换 Scope、历史范围 provenance；ASGI / 直接事件消费仿真 |
| tests/gateway/test_agent_model.py / test_models.py / test_sse_proxy.py | 实际 SDK 配合 HTTP MockTransport / 假 ChatModel 工具调用、模型调用预算、DeepSeek 主请求与 Ark Lite 原位 fallback、业务 Tool 不重放、视频工具请求 / acknowledgement / 最终文本、required 契约、usage / Schema / finish reason / 错误 |
| web 的 client / useAgentConversation / SearchPage / EvidencePlayer 测试 | SSE 分段解析、逐消息动画多选 / [] 全部、切换选择保留 Conversation 与历史、乱序保护、快照恢复、Markdown、缩略图与 DOM metadata / 范围暂停 |

SQLite、ASGI、实际 SDK 配合 HTTP MockTransport / 仿真 Provider 事件与 jsdom 仍不代表全部真实模型质量或浏览器解码播放已验收。2026-09-18 已用开发 MySQL / Qdrant 和 MyGO 真实数据完成一条双 Turn E2E：Query、Evaluator、Chat 均由 DeepSeek Flash 主路径完成，无 Lite fallback，交付 3 个视频块并持久化 completed；这只覆盖一个代表性查询，不替代完整场景矩阵。MySQL 集成测试需要 SEEKINO_TEST_DATABASE_URL，未配置时跳过。Evaluator 目前审核结构化文本，没有再次看视频 / 关键帧独立核验人物；最终 Chat 正文没有第二次 Evaluator 审核。

## 7. 可复现命令与后续顺序

以下保留 2026-09-18 时点的命令记录，测试路径应按当前目录核对；已移除失效的旧离线示例命令：

```bash
.venv/bin/pytest -q
.venv/bin/pytest tests/server/test_agent_runtime.py tests/server/test_agent_chat_service.py tests/server/test_api.py tests/server/test_sse_events.py -q
.venv/bin/pytest tests/gateway/test_agent_model.py tests/gateway/test_models.py tests/gateway/test_sse_proxy.py -q
```

从 web/ 运行 npm run typecheck、npm test、npm run build。旧离线示例曾使用非空假证据，演示 first continue → advice → 第二轮复用 → complete → evidence / delta → final_output；没有 SQL / HTTP，因此没有 answer.completed。该探索的结论已收录于[归档总结](../dev/design-experiment-cleanup.md#过时架构图和代码示例)。

接下来的验收顺序：

1. 真实 Query / Evaluator / Chat + 当前开发 MySQL / Qdrant 联调白话、对白、视觉动作、人物歧义、多轮指代和无证据；核对实际模型名、选择质量、来源与 ID / 时间。
2. 浏览器验证结构化回答完成后唯一正文 delta、切换 / 断线恢复、首帧缩略图与点击后才加载视频，以及原视频 metadata、Range seek 和 endMs 暂停。仅验证原视频范围播放，不声称已生成动作级短 Clip。
3. 按明确的新需求设计 Agent PROCESSING 崩溃恢复、跨 Worker 增量交接和可标识的基础候选降级；现有视频 ParseJob Lease / Reaper 不适用于聊天。
4. 逐候选 Query 相关解释、邻片核验、anchor / Clip 端点和累计 token / 长对话容量另行实施，不绕过第三轮审核拒绝的失败边界。
