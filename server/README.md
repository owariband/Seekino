# AniVAG Server

Server 当前实现 M1 的 Upload-to-Search 纵向闭环：分片上传与断点续传、环境隔离的
Artifact Store、MySQL 事实与 DB Queue、带 Lease/Heartbeat/Retry/Reaper 的解析 Worker、
VideoChunk checkpoint、持久化 Index Sync Intent、Qdrant 派生索引，以及按 `seriesIds`
限定的双路召回、RRF 和 `videoChunkId[]` 批量回源。模型协议仍由 Gateway 实现，Worker
直接复用 Engine 的解析链路。

Multi-Agent 对话作为独立领域建立在正式检索之上：MySQL 保存 AgentSession、
AgentConversation 和 AgentMessage，LangGraph 编排 Chat Agent、Scene Retrieval Tool 与
Evaluator Agent。完整接口见 [Agent API 对接文档](../docs/design/agent-api.md)。

Qdrant 的连接、写入和恢复属于产品持久化逻辑，因此放在 `server/storage/`；Engine
只负责生成解析结果、向量与检索算法。Qdrant payload 不保存业务正文；当前
Embedding 的 durable source 是 MySQL，Qdrant 始终只是可重建派生索引。

## HTTP 分层

```text
server/router/      URL、Method、Response Model 和 API Handler 的绑定
server/api/         参数/Header/Body 校验、HTTP 响应映射、SSE、异常转 HTTP
server/service/     业务用例、资源归属与状态校验、跨资源编排
server/bootstrap.py API 进程的数据库、Storage、Gateway 和 Service 装配
```

调用方向为 `router → api → service → repository/storage`。Router 不实现 Handler；
API 不查询业务事实后自行判断归属或状态。Content-Range 语法和 Body 长度在 API 校验，
workspace 归属、会话总大小、分片范围、缺片计算、Job 归属和 READY 检索 Scope 由
Service 负责；Repository 保留事务内的状态与并发约束。

HTTP 专用 Request/View 位于 `server/api/schema.py`；API、CLI、Agent 共用的检索证据和
应用报告留在 `server/schema.py`。`server/api/app.py` 只创建应用、配置生命周期与中间件、
注册异常处理和 Router。

## 后台任务与共享代码

`server/worker/` 是后台任务执行入口，不是 Event：Event 表达发生了什么，Worker
领取并执行任务。当前任务保存在 MySQL，不依赖事件总线。API 保存完整视频并创建
ParseJob 后，Worker 即可独立运行；浏览器关闭不影响已经入队的解析任务。

```text
server/config/                   配置类型、环境变量读取与校验
server/constants/workspace.py    本地默认 Workspace；不承担身份认证
server/utils/                    时间、文本校验、异常摘要等无业务取数的纯函数
server/service/video_processing.py
                                 解析、checkpoint、向量化、入库与索引同步编排
server/worker/__main__.py         Worker 进程装配和启动
server/worker/runtime.py          任务领取、Lease、Heartbeat、Retry、Reaper
```

配置入口为 `ServerSettings.from_environment()`，`from server.config import
ServerSettings` 保持可用。Upload、Agent、VideoProcessing、Retrieval 和 SSE 的配置类型
分别放在对应配置模块；配置模块不依赖 Service。任务状态等领域枚举继续放在
`server/model/`，不集中进无边界的 constants 文件，也不新增空的 common 目录。

部署调优项已补到 `.env.example`：`ANIVAG_SSE_POLL_SECONDS`、
`ANIVAG_SSE_KEEPALIVE_SECONDS`、`ANIVAG_DATABASE_POOL_RECYCLE_SECONDS`、
`ANIVAG_RETRIEVAL_CHANNEL_TOP_K`、`ANIVAG_RETRIEVAL_RRF_K` 和双路权重
`ANIVAG_RETRIEVAL_TEXT_WEIGHT` / `ANIVAG_RETRIEVAL_VISION_WEIGHT`。
Embedding 模型与 FPS 覆盖值由 Settings 读取，API 与 Worker 使用同一套配置。

Job SSE 忽略纯心跳变化，终态发送后关闭；Agent SSE 仍返回完整 Message 快照，包含
500 条之后的消息以及同一条消息的状态变化。保活按单调时钟的经过时间计算，
不是固定轮询次数。完整快照仍有随聊天长度增长的读取和传输成本，后续如改增量协议，
须另行设计状态更新游标，不能只按 sequenceNo 跳过已有的 PROCESSING 消息。

## 数据库隔离

开发、测试和正式环境必须使用不同的数据库。CLI 会强制检查数据库名：

- `development`：数据库名以 `_dev` 或 `_development` 结尾；
- `test`：数据库名以 `_test` 结尾；
- `production`：拒绝上述非正式环境后缀；
- SQLite 只允许用于隔离测试。

Qdrant Collection 使用相同的物理隔离规则：测试以 `_test` 结尾，开发以 `_dev` 或
`_development` 结尾，正式环境拒绝这两类后缀。

创建开发库后初始化表结构：

```sql
CREATE DATABASE anivag_development
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

```bash
export ANIVAG_ENVIRONMENT=development
export ANIVAG_DATABASE_URL='mysql+pymysql://anivag:password@127.0.0.1:3306/anivag_development?charset=utf8mb4'
export ANIVAG_QDRANT_URL='http://127.0.0.1:6333'
export ANIVAG_QDRANT_COLLECTION='anivag_video_chunks_development'
export ANIVAG_TEXT_VECTOR_DIMENSIONS=1024
export ANIVAG_VISION_VECTOR_DIMENSIONS=1024

uv sync
uv run python -m server.cli init-db
```

上述 `init-db` 只负责初始化空库或校验已经使用新命名的库。若数据库仍包含旧
`video_segment*` 表，或 `context_snapshot/index_sync_intent` 仍使用旧列名，Server 会在
创建任何新表前抛出 `SchemaUpgradeRequiredError`，防止形成新旧 Schema 混合状态。本次
术语重构没有自动修改现有 MySQL；旧开发库必须先受控重命名表、列、外键和约束并核对
原有 24 行数据，再运行新代码。

当前 Schema 包含六个视频领域模型：`Series`、`Video`、
`VideoChunk`、`VideoChunkInfo`、`ContextSnapshot`、
`VideoChunkEmbedding`；另有 `UploadSession/UploadSegment`、
`SeriesParseJob/VideoChunkCheckpoint` 和 `IndexSyncIntent`，没有 `Episode` 表。所有
MySQL 表使用 InnoDB。

## Upload-to-Search API 与 Worker

前端对接所需的完整请求、响应、分片续传、任务 SSE 与检索契约见
[前端 API 对接文档](../docs/design/frontend-api.md)。

先初始化 MySQL，再分别启动 API 与 Worker：

```bash
uv run python -m server.cli init-db
uv run python -m server.api
uv run python -m server.worker
```

API 默认监听 `127.0.0.1:9621`，OpenAPI 位于 `/docs`。API 与 Worker 是两个进程；API
写入 MySQL 中的事实和任务，Worker 从 MySQL Claim，不依赖进程内队列。主要接口：

```text
POST /api/series/create
GET  /api/series/list?workspaceId=local-default
GET  /api/series/get?seriesId={seriesId}&workspaceId=local-default
POST /api/uploads/create?seriesId={seriesId}       Idempotency-Key
GET  /api/uploads/get?uploadSessionId={uploadSessionId}
PUT  /api/uploads/segments/upload?uploadSessionId={uploadSessionId}&segmentNumber={segmentNo}
      Content-Range: bytes START-END/TOTAL
      X-Content-SHA256: <64 hex chars>
POST /api/uploads/complete?uploadSessionId={uploadSessionId}
GET  /api/jobs/list?workspaceId=local-default
GET  /api/jobs/get?jobId={jobId}
GET  /api/jobs/events?jobId={jobId}             text/event-stream
GET  /api/videos/content?videoId={videoId}      支持 Range
POST /api/search
POST /api/agent/sessions/create
GET  /api/agent/sessions/list
GET  /api/agent/sessions/get?sessionId={sessionId}
POST /api/agent/conversations/create?sessionId={sessionId}
GET  /api/agent/conversations/list?sessionId={sessionId}
GET  /api/agent/conversations/get?conversationId={conversationId}
GET  /api/agent/messages/list?conversationId={conversationId}
POST /api/agent/messages/send?conversationId={conversationId}
GET  /api/agent/messages/events?conversationId={conversationId}
```

`Complete` 会在校验所有分片后流式合并、计算整文件 SHA-256、发布 Original Video、探测
时长，并在同一个 MySQL 事务中创建 `Video + SeriesParseJob`。重复请求复用相同结果。
Artifact URI 形如 `artifact://development/...`，test/development/production 使用独立目录；
API/Worker 依赖 `ArtifactStore` 边界，当前 M1 adapter 是 `LocalArtifactStore`。

Worker 为每个 Job 保存单调 checkpoint：

```text
PREPARED → SPLIT_READY → PARSED → TEXT_EMBEDDED → VISION_EMBEDDED → INDEXED
```

AnalysisWindow 带上下文重叠，仅供模型解析；Vision vector 始终读取无重叠的 canonical
VideoChunk MP4。Text vector 读取同一 VideoChunk 的完整 retrieval document；两路向量以
同一个稳定 `videoChunkId` 入库与建索引。Worker 崩溃后复用本地持久化产物续跑，旧
`leaseVersion` 不能提交 checkpoint 或最终 MySQL 事实。SSE 只发送 MySQL 中对 UI 有意义
的状态、阶段、进度或 checkpoint 变化，不把纯 Heartbeat 当成进度事件；断线后仍以 Job
查询结果为准。

## 导入第七集已有产物

`import-parse` 是保留给已有离线产物的过渡入口。原视频、AnalysisWindow 和规范 VideoChunk
必须真实存在；导入器会把媒体保存为稳定的 `file:` URI。省略显式 ID 时，
Series ID 由 workspace 与规范化名称生成，Video ID 由 Series ID 与视频内容哈希生成，
VideoChunk ID 再由 Video ID 与序号生成稳定、不可反解关系的 UUID。`episodeOrder` 不参与
Video 身份计算，调整集序不会产生另一个业务 ID。

```bash
uv run python -m server.cli import-parse \
  --parse-result outputs/engine/ep07-full-60s/pipeline.parse.json \
  --canonical-chunk-dir outputs/engine/ep07-recall-smoke/eval-only-canonical-media/bfa4a91901efd2b7-h720 \
  --embedding-cache-dir outputs/engine/ep07-recall-smoke/embedding-cache/segments \
  --series-name 'BanG Dream! It’s MyGO!!!!!' \
  --series-description 'MyGO 动画测试数据' \
  --episode-order 7 \
  --pipeline-version 50661d1 \
  --series-bible-file assets/mygo_background_referrence/characters.md
```

整个 Video 快照在一个事务中写入。相同命令重复执行会复用相同的 `videoId` 与
`videoChunkId` 并更新解析器拥有的数据，不会增加记录；并发首导入遇到唯一键竞争时
会回滚后重试。重导不会清空已有的 Series/Video 管理元数据、Canonical VideoChunk
媒体引用或下游 enrichment，也不会用改变后的文本继续配用旧 Text Embedding。
同一 ID 对应到另一源视频或另一套 VideoChunk 边界时会明确报冲突。

上例的 `embedding-cache/segments` 是现有第七集历史缓存目录；导入器只把其中的
`segment-NNNN.*.json` 当兼容输入读取，新领域模型与新产物统一使用 VideoChunk/chunk。

Artifact Store 负责原视频、Proxy、VideoChunk、AnalysisWindow、关键帧和 Clip 等媒体或
二进制产物；当前实现是按环境隔离的本地文件 adapter。Text/Vision 向量仍持久化在
MySQL `VideoChunkEmbedding`，未来是否外置及外置到哪类存储是独立决策。

事实与对应的 `IndexSyncIntent` 在同一个 MySQL 事务中提交。双向量完整时意图为
`UPSERT`；向量不完整或新事实要求清除旧索引时为 `DELETE`。导入不直接访问 Qdrant，
因此 Qdrant 故障不会回滚已经提交的业务事实。

## 索引同步与恢复

启动本地 Qdrant 后消费当前到期的 Intent：

```bash
docker compose -f deploy/qdrant/compose.yaml up -d
uv run python -m server.cli sync-index --limit 100
```

正式 point ID 直接使用稳定的 `videoChunkId` UUID；Collection 只包含 `text`、
`vision` 两个 named vector，payload 只包含 `seriesId`、`videoId`、
`videoChunkId`、`sourceRevision`、`isFallback`。相同意图重复执行是覆盖写，不会增加
point。两个向量维度是启动时明确给定的 Collection Schema；任意首条异常向量都不能
反向决定或污染正式 Collection 的维度。

写入失败时 Intent 进入 `RETRY_WAIT`，保存错误、尝试次数和指数退避后的下次执行时间；
再次运行 `sync-index` 会继续消费已到期任务。校准命令检查每个当前 revision，发现缺失、
过期或应删除但仍存在的 point 后立即走同一幂等路径修复：

```bash
uv run python -m server.cli reconcile-index
```

如果 Collection 被清空或丢失，使用全量重放恢复。该命令不会主动删除 Collection，
只以 MySQL `VideoChunkEmbedding` 中的当前向量事实为准重放全部期望状态：

```bash
uv run python -m server.cli rebuild-index
```

只有一个 Series 下的全部 Video 都解析成功且产生 VideoChunk，并且所有 VideoChunk 都
具备 text/vision 向量、最新 UPSERT Intent 均已确认同步，Series 才进入 `READY`；其他
情况保持 `INDEXING`。

## 正式检索闭环

`search` 只接受完整、已进入 `READY` 的 Series Scope。每个 named vector 都把
`seriesId` 的 Qdrant filter 放在 TopK 查询内执行；随后由 Engine 做确定性 RRF，并保留
Text/Vision 各自的 rank 与 score。最终 `videoChunkId[]` 通过一条联表 SQL 同时 Hydrate
Evidence 和当前 Index Sync revision，若发现 Qdrant 孤儿 Point、旧 revision、跨 Series/
Video 或 payload 契约不一致，会明确抛出一致性错误，不会静默丢弃。

```bash
uv run python -m server.cli search \
  --query '爽世为什么哭着质问大家要演奏春日影' \
  --series-id 77285f16-3e5b-592e-ba15-8b224b619806 \
  --top-k 5
```

多 Series 检索时重复传入 `--series-id`。默认使用 Text/Vision `1:1` 权重、每路 Top 20、
`rrfK=60`；`--exact` 只用于确定性诊断。输出的每个 `hit` 包含一个
`VideoChunkCandidate`（融合与双路分数）和一个同 ID 的 `VideoChunkEvidence`（Series、
集数、时间范围、媒体引用、解析正文，以及生成 Text 向量时使用的完整
`retrievalText`），不包含只供 ANN 计算使用的原始浮点向量。

批量回源接受一个或多个正式 ID，并通过一条联表 SQL 返回 Series、Video、VideoChunk 和
解析信息。返回字段使用工程 API 风格的 `retrievalText`、`videoCompact` 与
`isFallback`，不返回原始向量：

```bash
uv run python -m server.cli hydrate \
  00000000-0000-0000-0000-000000000000 \
  11111111-1111-1111-1111-111111111111
```
