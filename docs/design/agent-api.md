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
- `AgentMessage`：MySQL 中的消息事实。SSE 只通知消息变化，列表接口仍是恢复真源。
- LangGraph checkpoint 不是产品聊天记录；本轮产品状态全部由上述 MySQL 表持久化。

## 2. Multi-Agent 协作图

```text
Chat Agent：结合历史消息改写 retrievalQuery
    ↓
Scene Retrieval Tool：Text + Vision → RRF → MySQL Hydrate
    ↓
Evaluator Agent：检查 query 和 Evidence 是否覆盖用户意图
    ├── correctedQuery → 最多补查一次 → 再次评估
    └── 通过
         ↓
Chat Agent：只基于完整 VideoChunkEvidence 生成回答与候选 ID
         ↓
Assistant Message：内容、modelName、retrievalQuery、searchResult 原子落库
```

检索 Tool 是进程内 typed Tool，直接调用 `VideoRetrievalService`，不是 Server 对自己的
HTTP 请求。Evaluator 只返回公开校验摘要和可选的 corrected query，不保存或返回模型
私有思维链。

这里的技术分工是明确的：

- `langchain-core` 负责 `StructuredTool` 及其参数 Schema，约束 Agent 与检索能力的边界。
- `langgraph` 负责 `StateGraph`、条件分支和“一次纠错后停止”的协作编排。
- 现有 Gateway `ArkMultimodalModel` 继续通过火山方舟官方 SDK 调模型；它是模型适配层，
  不需要让 Server 通过 HTTP 调用自己的工具接口。

因此当前没有安装未使用的 `langchain` 聚合包；使用的是实际需要的 LangChain Core 与
LangGraph。以后新增更多工具或更换模型适配器时，图和产品持久化协议不需要跟着推倒重写。

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

`topK` 可省略，默认 `8`；当前服务端上限默认 `20`。一次发送先提交 USER Message 和
`PROCESSING` ASSISTANT Message，再运行 LangGraph，成功后更新同一个 ASSISTANT Message。
同一 Conversation 同时只允许一个 ASSISTANT Message 处于 `PROCESSING`。

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

### 6.3 SSE

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

当前 SSE 是持久化 Message 状态流，不是 token delta 流。LangGraph node/token 的细粒度
透传需要独立增加 run/event 序号和断线游标，不能把进程内 token 直接当成可靠聊天记录。

## 7. 状态与错误

Message 状态：

- `PROCESSING`：LangGraph 正在规划、召回、评估或组织回答。
- `COMPLETED`：`content`、`retrievalQuery`、`searchResult` 已保存。
- `FAILED`：失败事实和 `lastError` 已保存；使用新 Idempotency-Key 重试。

| HTTP | code | 含义 |
| --- | --- | --- |
| `400` | `INVALID_REQUEST` | ID、正文、Scope 或参数格式错误 |
| `404` | `NOT_FOUND` | Session、Conversation 或 Series 不属于 Workspace |
| `409` | `CONFLICT` | 幂等冲突、同一对话已有消息执行中、Series 未 READY 或检索事实不一致 |
| `502` | `AGENT_MODEL_ERROR` | Chat/Evaluator 模型调用或结构化输出失败 |

## 8. 模型配置

```text
ANIVAG_AGENT_CHAT_MODEL
ANIVAG_AGENT_EVALUATOR_MODEL
ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS=180
ANIVAG_AGENT_HISTORY_MESSAGE_LIMIT=12
ANIVAG_AGENT_DEFAULT_TOP_K=8
ANIVAG_AGENT_MAX_TOP_K=20
```

两个模型名省略时都使用 `config.yaml` 的 `video_parser.transcript_model`。生产中建议显式设置，
这样 `modelName`、成本归属和模型行为都更容易核对。
