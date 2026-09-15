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
Workspace
└── AgentConversation（聊天历史）
    └── AgentMessage
        ├── USER（本次实际 seriesIds）
        └── ASSISTANT（同一 seriesIds + modelName + retrievalQuery + searchResult）
```

- `AgentConversation`：直接属于 Workspace 的连续对话线程，保存标题和历史，不保存固定检索范围。
- `AgentMessage`：MySQL 中的消息事实，USER / ASSISTANT 都保存本次发送解析后的实际 `seriesIds`。同一 Conversation 可以在后续发送选择不同动画；一次 Kick 内范围保持不变。POST SSE 交付实时正文；GET 快照和列表接口恢复持久化状态。
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
| `GET` | `/api/series/list?workspaceId=...` | 复用已有 Series 列表，为动画多选提供 seriesId / name / status |
| `POST` | `/api/agent/conversations/create` | 在 Workspace 中创建 Conversation |
| `GET` | `/api/agent/conversations/list?workspaceId=...` | 列出 Workspace 的 Conversation |
| `GET` | `/api/agent/conversations/get?conversationId=...` | 获取 Conversation |
| `GET` | `/api/agent/messages/list?conversationId=...` | 列出 Message |
| `POST` | `/api/agent/messages/send?conversationId=...` | 发送用户消息 |
| `GET` | `/api/agent/messages/events?conversationId=...` | 订阅 Message 状态快照 |

Conversation 创建和消息发送均要求 `Idempotency-Key`。同一个 Key 配合相同字段会返回
原资源或原消息；不同字段返回 `409 CONFLICT`。Conversation 创建 Key 的唯一范围是
当前 Workspace，消息发送 Key 的唯一范围是当前 Conversation。

## 4. Conversation

### 4.1 创建

```http
POST /api/agent/conversations/create
Idempotency-Key: 80d5cdda-...
Content-Type: application/json

{
  "workspaceId": "local-default",
  "title": "寻找春日影后的质问"
}
```

响应 `201`：

```json
{
  "conversationId": "3955f7ee-873d-42b4-9ea1-e3b6555ddc67",
  "workspaceId": "local-default",
  "title": "寻找春日影后的质问",
  "messageCount": 0,
  "lastMessageAt": null,
  "createdAt": "2026-09-15T01:01:00",
  "updatedAt": "2026-09-15T01:01:00"
}
```

创建 Conversation 不传 `seriesIds`，不校验或绑定动画。动画选择属于每次发送的请求。

### 4.2 列表与详情

```http
GET /api/agent/conversations/list?workspaceId=local-default&limit=100
GET /api/agent/conversations/get?conversationId={conversationId}&workspaceId=local-default
```

## 5. Message

### 5.1 发送

```http
POST /api/agent/messages/send?conversationId={conversationId}
Idempotency-Key: 99946c6f-...
Content-Type: application/json

{
  "workspaceId": "local-default",
  "content": "第二段里的她为什么突然哭了？",
  "seriesIds": ["77285f16-3e5b-592e-ba15-8b224b619806"],
  "topK": 8
}
```

`seriesIds` 可省略或传 `[]`，表示当前 Workspace 中全部 `READY` 动画；没有 READY 动画
时返回 `409`，不会执行空 Scope 召回。显式数组去重并规范化，所有 ID 必须属于当前
Workspace 且已 READY；UUID 规范化、去重并排序，选择顺序不影响幂等。缺失 / 跨 Workspace
返回 `404`，未 READY 返回 `409`，不能静默
移除显式选择项。前端复用 `GET /api/series/list?workspaceId=...` 获取 `SeriesView[]`，
读取必填 `seriesId/name/status` 展示多选，非 READY 项不能新选，已选项仍可取消；空选择显示“全部可检索动画”。

Service 在启动任务前展开并保存实际 Scope，USER / ASSISTANT 均返回这一 `seriesIds` 快照。
Retrieve Tool 的闭包只使用当前 Kick 的 Scope，模型、advice 和前端后续选择不能扩大它。
聊天历史传给 Query 时携带各消息 `seriesIds`，旧范围证据不能直接当作本次实际召回。

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
    "seriesIds": ["77285f16-..."],
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
    "seriesIds": ["77285f16-..."],
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
每个 `hits[].evidence.seriesId` 都必填，来自 MySQL 对 Series / Video / VideoChunk 的批量
join 回源，必须属于本次消息实际 `seriesIds`，不是由模型生成或从正文推断。

消息幂等比较正文、有效 `topK` 和规范化原请求 `seriesIds`，省略与 `[]` 等价。USER 内部
独立保存 `requested_series_ids`；它不进入公开 DTO。相同 Key 的重放在重新展开当前动画
之前读回原消息，因此新增动画不会改变旧“全部”请求的结果。相同 Key 改为显式范围，
即使该范围恰好等于旧展开结果也返回 `409`。

### 5.2 列表与 `modelName`

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

### 5.3 GET 消息快照 SSE

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

### 5.4 POST 正文增量 SSE

在同一发送接口增加 `Accept: text/event-stream`，Body 和 `Idempotency-Key` 与 JSON 调用一致：

```http
POST /api/agent/messages/send?conversationId={conversationId}
Accept: text/event-stream
Idempotency-Key: 99946c6f-...
Content-Type: application/json

{"workspaceId":"local-default","content":"找摔杯子的片段","seriesIds":[],"topK":8}
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

## 6. 状态与错误

Message 状态：

- `PROCESSING`：执行任务正在进行 Query 工具循环、审核、最终 Chat 生成或结果提交。
- `COMPLETED`：`content`、`retrievalQuery`、`searchResult` 已保存。
- `FAILED`：失败事实和 `lastError` 已保存；使用新 Idempotency-Key 重试。

| HTTP | code | 含义 |
| --- | --- | --- |
| `400` | `INVALID_REQUEST` | ID、正文、Scope 或参数格式错误 |
| `404` | `NOT_FOUND` | Conversation 或 Series 不属于 Workspace |
| `409` | `CONFLICT` | 幂等冲突、同一对话已有消息执行中、Series 未 READY 或检索事实不一致 |
| `502` | `AGENT_MODEL_ERROR` | Query/Evaluator/Chat 调用、输出或执行预算失败，或最多三轮仍未通过审核 |

## 7. 模型配置

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

## 8. 已有数据库升级

新数据库只创建 `agent_conversation` 和 `agent_message` 两张 Agent 表。已有数据库需要停写并
备份后受控升级，不能依赖 `create_all()` 自动删表或改列；MySQL DDL 也不能靠事务自动回滚。
`create_schema()` 检测到旧 `agent_session`、Conversation 的 `session_id` 或缺失的
`workspace_id` 时拒绝初始化；Conversation 仍有 `series_ids`，或 Message 缺少 `series_ids`
/ `requested_series_ids` 时也拒绝初始化，要求受控 message-scope migration。

先用 `SHOW CREATE TABLE` 核对两张 Agent 表。下文按最早的 Session + 固定 Conversation
Scope 结构写迁移顺序；已完成 Session 删除的数据库跳过归属复制、Session 外键 / 列 / 表
和旧唯一键操作，只做消息 Scope 回填及 Conversation Scope 删除。字段或约束已存在时
不重复添加，不能将整段 SQL 未经核对直接执行。

仍有 Session 层时，先核对旧对话的 Workspace 归属，以下查询必须没有结果；有孤立对话时
先修复归属，不能删除对话：

```sql
SELECT c.conversation_id
FROM agent_conversation AS c
LEFT JOIN agent_session AS s ON s.session_id = c.session_id
WHERE s.workspace_id IS NULL;
```

将归属复制到 Conversation，保留原 Conversation ID 和全部消息：

```sql
ALTER TABLE agent_conversation ADD COLUMN workspace_id VARCHAR(64) NULL;
UPDATE agent_conversation AS c
JOIN agent_session AS s ON s.session_id = c.session_id
SET c.workspace_id = s.workspace_id;
```

在建立新的 Workspace 幂等唯一键前检查旧 Session 之间的 Key 冲突：

```sql
SELECT workspace_id, idempotency_key, COUNT(*) AS conversation_count
FROM agent_conversation
GROUP BY workspace_id, idempotency_key
HAVING COUNT(*) > 1;
```

有冲突时为冲突对话分配新的无冲突迁移 Key，并保留映射记录；不要删对话或修改消息 ID。

在删除 Conversation 的 `series_ids` 前，将其固定 Scope 回填到所有历史消息。先新增
可空列，完成回填 / 核对后再收紧实际 Scope 为 NOT NULL：

```sql
ALTER TABLE agent_message
    ADD COLUMN series_ids JSON NULL,
    ADD COLUMN requested_series_ids JSON NULL;

UPDATE agent_message AS m
JOIN agent_conversation AS c ON c.conversation_id = m.conversation_id
SET m.series_ids = c.series_ids,
    m.requested_series_ids = CASE
        WHEN m.role = 'USER' THEN c.series_ids
        ELSE NULL
    END;
```

上述 SQL 先复制历史范围。随后由受控迁移脚本按当前 Service 的 `_canonical_scope` 规则，
将每条 USER 的 `requested_series_ids` 做 UUID 规范化、去重并排序；例如 Python 使用
`sorted({str(UUID(value)) for value in old_series_ids})`，遇到非法 ID 应停止迁移并修复来源。
不能仅复制旧数组的选择顺序，否则新服务重试同 Key 会误报请求范围冲突。

旧 USER 原本使用固定的显式 Conversation 范围，故 `requested_series_ids` 保存规范化后的
该数组，不能写成 `[]` 并冒充历史“全部”请求；ASSISTANT 为 SQL NULL。两条消息的实际
`series_ids` 均保留旧范围。检查以下查询无结果，再核对 USER / ASSISTANT 消息对范围相同、原消息 ID /
序号 / content / status / search_result 不变，并检查历史 hits 的 evidence.seriesId 在其范围内：

```sql
SELECT message_id
FROM agent_message
WHERE series_ids IS NULL
   OR JSON_TYPE(series_ids) <> 'ARRAY'
   OR JSON_LENGTH(series_ids) = 0
   OR (role = 'USER' AND (requested_series_ids IS NULL
       OR JSON_TYPE(requested_series_ids) <> 'ARRAY'
       OR JSON_LENGTH(requested_series_ids) = 0))
   OR (role = 'ASSISTANT' AND requested_series_ids IS NOT NULL);

ALTER TABLE agent_message
    MODIFY COLUMN series_ids JSON NOT NULL;
```

复核所有 `workspace_id` 非空、对话和消息数量及内容不变，再用 `SHOW CREATE TABLE agent_conversation`
确认指向旧 Session 的实际外键。以下查询生成对应的删除语句，核对并执行返回的语句后继续：

```sql
SELECT CONCAT('ALTER TABLE agent_conversation DROP FOREIGN KEY `',
              CONSTRAINT_NAME, '`;') AS migration_statement
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'agent_conversation'
  AND COLUMN_NAME = 'session_id'
  AND REFERENCED_TABLE_NAME = 'agent_session';
```

完成外键移除后执行：

```sql
ALTER TABLE agent_conversation
    DROP INDEX uq_agent_conversation_session_idempotency,
    DROP COLUMN session_id,
    MODIFY COLUMN workspace_id VARCHAR(64) NOT NULL,
    ADD CONSTRAINT uq_agent_conversation_workspace_idempotency
        UNIQUE (workspace_id, idempotency_key),
    ADD INDEX ix_agent_conversation_workspace_id (workspace_id);
DROP TABLE agent_session;
```

消息回填已核对成功后，移除 Conversation 的旧检索范围：

```sql
ALTER TABLE agent_conversation DROP COLUMN series_ids;
```

最后复核 Conversation 和 Message 数据及约束，再重新初始化并启动服务。以上是迁移步骤，
本次代码重构没有对已有开发或生产数据库执行 DDL。
