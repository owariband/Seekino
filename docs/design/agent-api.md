# AniVAG Multi-Agent API 对接文档

> 状态：首个可运行纵向闭环
>
> 更新时间：2026-09-15
>
> 面向对象：WebUI 对话、历史会话与 Agent 状态联调

本文只描述独立 Agent 会话系统，不重复描述视频上传、解析任务或底层 `/api/search`
契约。所有资源 ID 使用 camelCase Query 参数，不放入 Path；创建、列表、详情和发送动作
分别显式使用 `create`、`list`、`get`、`send`。

## 1. 领域关系

```text
AgentSession
└── AgentConversation（固定 seriesIds Scope）
    └── AgentMessage
        ├── USER
        └── ASSISTANT（modelName + retrievalQuery + searchResult）
```

- `AgentSession`：Workspace 中的一次 Agent 使用上下文，可以包含多个对话。
- `AgentConversation`：连续对话线程，创建时固定 `seriesIds`，追问不会静默扩大范围。
- `AgentMessage`：MySQL 中的消息事实。POST SSE 交付实时正文；GET 快照和列表接口恢复持久化状态。
- LangGraph checkpoint 不是产品聊天记录；本轮产品状态全部由上述 MySQL 表持久化。

## 2. Multi-Agent 协作图

```text
一次 AgentRuntime.kick()：
    Query Agent：SDK 内部模型 / 工具循环，给出计划、草稿和选中片段 ID
        ↓
    Evaluator Agent：complete / continue + advice
        ├── continue 且尚未满 3 轮 → 下一轮 Query，读取原计划和全部工作记录
        ├── 第 3 轮仍 continue → FAILED，最终 Chat 不调用
        └── complete → 最终 Chat 只根据获批材料实时整理正文
                            ↓
                     确定性 Output Node：正文 + 相同获批 Evidence
                            ↓
                     Service 原子保存 Assistant Message
                            ↓
                     answer.completed
```

Retrieve Tool 是进程内工具，直接调用注入的 `VideoRetrievalService.search`。模型参数只有
`query` / `top_k`；Workspace / Series Scope 由当前 Kick 固定。完整实际召回、公开计划、
草稿、审核意见和复用来源保存在各 `LoopTurn`，下一轮显式获得这些信息。

代码职责：`server/service/agent_chat.py` 管会话与消息事务；`server/agent/` 是独立于 HTTP 的
Harness；`gateway/agent_model.py` 使用 LangChain `create_agent` / `ToolStrategy` 管内层
工具循环，`langgraph` 管外层审核循环。最终 Chat 使用 Gateway 的 `stream_text` 读取
原始 Provider delta。审核通过只代表允许进入 Chat，消息完成还要求正文正常结束并成功落库。

## 3. 接口总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/agent/sessions/create` | 创建 Agent Session |
| `GET` | `/api/agent/sessions/list?workspaceId=...` | 列出 Session |
| `GET` | `/api/agent/sessions/get?sessionId=...` | 获取 Session |
| `POST` | `/api/agent/conversations/create?sessionId=...` | 创建 Conversation |
| `GET` | `/api/agent/conversations/list?sessionId=...` | 列出 Conversation |
| `GET` | `/api/agent/conversations/get?conversationId=...` | 获取 Conversation |
| `GET` | `/api/agent/messages/list?conversationId=...` | 列出 Message |
| `POST` | `/api/agent/messages/send?conversationId=...` | 发送用户消息 |
| `GET` | `/api/agent/messages/events?conversationId=...` | 订阅 Message 状态快照 |

三个创建/发送接口均要求 `Idempotency-Key`。同一个 Key 配合相同字段会返回原资源或原
消息；同一个 Key 配合不同字段返回 `409 CONFLICT`。

## 4. Session

### 4.1 创建

```http
POST /api/agent/sessions/create
Idempotency-Key: 82f8c4d0-...
Content-Type: application/json

{
  "workspaceId": "local-default",
  "name": "画面札记"
}
```

响应 `201`：

```json
{
  "sessionId": "5ca10c9d-84c8-4bb6-b70a-1c79775382ca",
  "workspaceId": "local-default",
  "name": "画面札记",
  "conversationCount": 0,
  "createdAt": "2026-09-15T01:00:00",
  "updatedAt": "2026-09-15T01:00:00"
}
```

### 4.2 列表与详情

```http
GET /api/agent/sessions/list?workspaceId=local-default&limit=100
GET /api/agent/sessions/get?sessionId={sessionId}&workspaceId=local-default
```

列表直接返回 `AgentSessionView[]`。

## 5. Conversation

### 5.1 创建

```http
POST /api/agent/conversations/create?sessionId={sessionId}
Idempotency-Key: 80d5cdda-...
Content-Type: application/json

{
  "workspaceId": "local-default",
  "title": "寻找春日影后的质问",
  "seriesIds": [
    "77285f16-3e5b-592e-ba15-8b224b619806"
  ]
}
```

响应 `201`：

```json
{
  "conversationId": "3955f7ee-873d-42b4-9ea1-e3b6555ddc67",
  "sessionId": "5ca10c9d-84c8-4bb6-b70a-1c79775382ca",
  "workspaceId": "local-default",
  "title": "寻找春日影后的质问",
  "seriesIds": [
    "77285f16-3e5b-592e-ba15-8b224b619806"
  ],
  "messageCount": 0,
  "lastMessageAt": null,
  "createdAt": "2026-09-15T01:01:00",
  "updatedAt": "2026-09-15T01:01:00"
}
```

`seriesIds` 至少包含一个当前 Workspace 中存在的 Series。底层正式召回仍要求 Series
已经 `READY`；未 READY 时发送消息返回 `409`，不会偷偷从 Scope 删除该 Series。

### 5.2 列表与详情

```http
GET /api/agent/conversations/list?sessionId={sessionId}&workspaceId=local-default&limit=100
GET /api/agent/conversations/get?conversationId={conversationId}&workspaceId=local-default
```

## 6. Message

### 6.1 发送

```http
POST /api/agent/messages/send?conversationId={conversationId}
Idempotency-Key: 99946c6f-...
Content-Type: application/json

{
  "workspaceId": "local-default",
  "content": "第二段里的她为什么突然哭了？",
  "topK": 8
}
```

`topK` 可省略，默认 `8`；当前服务端上限默认 `20`，同时限制每次召回和最终选中的不同片段数量。
多次召回可以累积更多内部证据，交付结果仍遵守本次 TopK。一次发送先提交 USER Message 和
`PROCESSING` ASSISTANT Message，再由 Service 拥有的执行任务运行 Harness。Query 草稿通过审核后，
最终 Chat 整理正文；Provider 正常结束且数据库事务成功，才更新同一个 ASSISTANT 为 `COMPLETED`。同一 Conversation 同时只允许一个
ASSISTANT Message 处于 `PROCESSING`。

响应 `200`：

```json
{
  "userMessage": {
    "messageId": "5f744756-...",
    "conversationId": "3955f7ee-...",
    "sequenceNo": 0,
    "role": "USER",
    "status": "COMPLETED",
    "content": "第二段里的她为什么突然哭了？",
    "modelName": null,
    "replyToMessageId": null,
    "retrievalQuery": null,
    "searchResult": null,
    "lastError": null,
    "createdAt": "2026-09-15T01:02:00",
    "updatedAt": "2026-09-15T01:02:00"
  },
  "assistantMessage": {
    "messageId": "8166b7b0-...",
    "conversationId": "3955f7ee-...",
    "sequenceNo": 1,
    "role": "ASSISTANT",
    "status": "COMPLETED",
    "content": "当前范围内没有找到足够可信、值得播放核验的候选画面。",
    "modelName": "doubao-seed-2-0-lite-260428",
    "replyToMessageId": "5f744756-...",
    "retrievalQuery": "人物突然哭泣，结合上一轮第二个候选的前后语境",
    "searchResult": {
      "query": "人物突然哭泣，结合上一轮第二个候选的前后语境",
      "seriesIds": ["77285f16-..."],
      "agentReply": {
        "message": "当前范围内没有找到足够可信、值得播放核验的候选画面。",
        "resultCount": 0
      },
      "hits": []
    },
    "lastError": null,
    "createdAt": "2026-09-15T01:02:00",
    "updatedAt": "2026-09-15T01:02:04"
  }
}
```

上面是合法的“无可信候选”响应。存在候选时，`resultCount` 必须始终等于 `hits.length`，
且 `hits` 包含选中的完整 Candidate、SelectionReason 和 VideoChunkEvidence。

### 6.2 列表与 `modelName`

```http
GET /api/agent/messages/list
  ?conversationId={conversationId}
  &workspaceId=local-default
  &afterSequenceNo=-1
  &limit=200
```

响应直接返回按 `sequenceNo` 升序排列的 `AgentMessageView[]`。

`modelName` 是所有 Message 的必返字段，但允许为 `null`：

| Message | `modelName` | 前端语义 |
| --- | --- | --- |
| `USER` | `null` | 用户消息没有生成模型 |
| `ASSISTANT` | 非空字符串 | 生成这条消息时实际使用的 Chat Agent 模型 |

助手 Message 创建为 `PROCESSING` 时即保存模型名。模型配置以后发生变化，不会改写历史
消息。因此前端展示“当前模型”时，应读取最新一条 ASSISTANT Message 的 `modelName`；
不要从本地常量或当前环境配置推测。Evaluator 可以使用另一模型，但不会冒充回答模型；
Message 上显示的是最终回答所属 Chat Agent 的模型。

### 6.3 GET 消息快照 SSE

```http
GET /api/agent/messages/events
  ?conversationId={conversationId}
  &workspaceId=local-default
Accept: text/event-stream
```

连接建立后立即发送完整快照：

```text
event: messages
data: {"conversationId":"...","messages":[...]}
```

Message 新增，或同一 ASSISTANT Message 从 `PROCESSING` 变成 `COMPLETED/FAILED` 时，再发送
新的完整快照。约 15 秒无变化时发送 SSE 注释 keep-alive。断线重连后首帧仍是 MySQL
当前完整状态，所以前端不需要依赖丢失的中间事件恢复。

快照不受 List Message 的单页 500 条上限截断；List 接口仍按原来的分页参数工作。
轮询与保活间隔由 `ANIVAG_SSE_POLL_SECONDS`（默认 1 秒）和
`ANIVAG_SSE_KEEPALIVE_SECONDS`（默认 15 秒）配置，保活按经过时间计算。

GET SSE 只恢复数据库快照，不再次启动 Kick，也不回放未持久化的正文增量。刷新或断线后，
正在执行的消息仍显示 PROCESSING，任务正常完成后从快照得到完整结果。

### 6.4 POST 正文增量 SSE

在同一发送接口增加 `Accept: text/event-stream`，Body 和 `Idempotency-Key` 与 JSON 调用一致：

```http
POST /api/agent/messages/send?conversationId={conversationId}
Accept: text/event-stream
Idempotency-Key: 99946c6f-...
Content-Type: application/json

{"workspaceId":"local-default","content":"找摔杯子的片段","topK":8}
```

每帧有 `event`、`id: {messageId}:{sequence}` 和 JSON `data`；所有事件包含
`conversationId`、助手 `messageId`、本次流内递增的整数 `sequence`。

| event | 时机 | 附加字段 |
| --- | --- | --- |
| `answer.started` | USER / PROCESSING ASSISTANT 已提交 | `userMessage`、`assistantMessage` |
| `answer.evidence` | 审核 complete 后、正文生成前 | `hits`：获批完整 VideoChunkSearchHit |
| `answer.delta` | Provider 仍在生成时 | `delta`：收到的原始正文增量 |
| `answer.completed` | 正常收流且最终结果成功提交后 | `userMessage`、`assistantMessage`：持久化快照 |
| `answer.failed` | 执行或提交失败 | `code`、`message`，以及可取得的消息快照 |

已失败的任务没有 completed。SSE 已开始后，模型 / 提交错误通过 failed 帧报告；开始之前的
参数、归属、幂等冲突仍按 HTTP 错误返回。相同 Key 重放已完成消息只返回 started 和 completed；
相同 Key 对 PROCESSING 请求返回 409，不重复启动执行。

连接断开仅关闭订阅，Service 的任务继续执行。慢消费者导致缓冲溢出时返回
`STREAM_INTERRUPTED` 并停止该订阅；这表示传输中断，不能据此将消息改为 FAILED。
前端继续用 GET 快照等待真正终态。事件序号不是持久化重放游标；本阶段没有跨 Worker
增量回放或进程崩溃后的 PROCESSING 执行恢复。

## 7. 状态与错误

Message 状态：

- `PROCESSING`：执行任务正在进行 Query 工具循环、审核、最终 Chat 生成或结果提交。
- `COMPLETED`：`content`、`retrievalQuery`、`searchResult` 已保存。
- `FAILED`：失败事实和 `lastError` 已保存；使用新 Idempotency-Key 重试。

| HTTP | code | 含义 |
| --- | --- | --- |
| `400` | `INVALID_REQUEST` | ID、正文、Scope 或参数格式错误 |
| `404` | `NOT_FOUND` | Session、Conversation 或 Series 不属于 Workspace |
| `409` | `CONFLICT` | 幂等冲突、同一对话已有消息执行中、Series 未 READY 或检索事实不一致 |
| `502` | `AGENT_MODEL_ERROR` | Query/Evaluator/Chat 调用、输出或执行预算失败，或最多三轮仍未通过审核 |

## 8. 模型配置

```text
ANIVAG_AGENT_QUERY_MODEL
ANIVAG_AGENT_EVALUATOR_MODEL
ANIVAG_AGENT_CHAT_MODEL
ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS=180
ANIVAG_AGENT_HISTORY_MESSAGE_LIMIT=12
ANIVAG_AGENT_DEFAULT_TOP_K=8
ANIVAG_AGENT_MAX_TOP_K=20
ANIVAG_AGENT_MAX_LOOP_TURNS=3
ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN=6
ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN=4
ANIVAG_AGENT_MAX_OUTPUT_TOKENS=4096
ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS=180
ANIVAG_AGENT_MAX_CONCURRENT_KICKS=4
```

Chat 模型名省略时使用 `config.yaml` 的 `video_parser.transcript_model`；Query 和 Evaluator
省略时各自继承 Chat 模型名。三个角色可以单独配置，消息 `modelName` 仍记录最终 Chat 模型。
模型和检索调用在执行边界检查总 deadline / 取消，SDK 每次模型调用前后也检查；正在阻塞的同步
Provider 请求需要等待其网络超时，执行 deadline 不代表线程会被硬终止。生产装配保持 Ark Provider，
Query / Evaluator / Chat 以及召回 Embedding 的 SDK Client 按调用隔离并关闭。
