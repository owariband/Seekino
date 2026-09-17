# AniVAG Server

Server 当前实现 M1 的 Upload-to-Search 纵向闭环：分片上传与断点续传、环境隔离的
Artifact Store、MySQL 事实与 DB Queue、带 Lease/Heartbeat/Retry/Reaper 的进程内
ParseJobConsumer、VideoChunk checkpoint、持久化 Index Sync Intent、Qdrant 派生索引，
以及按 `seriesIds` 限定的双路召回、RRF 和 `videoChunkId[]` 批量回源。模型协议仍由
Gateway 实现，AniVAG Server 进程内直接复用 Engine 的解析链路。

Multi-Agent 对话作为独立领域建立在正式检索之上：Workspace 直接拥有
AgentConversation，每个 Conversation 保存聊天历史，AgentMessage 保存每次发送的 Series Scope，
不再增加 Agent Session 容器。独立 Harness 用 LangGraph 编排 Query / Evaluator 最多
3 轮，审核通过后由 Chat 实时输出最终正文，Output 保留结构化视频证据。
完整接口见 [Agent API 对接文档](../docs/design/agent-api.md)。旧 Agent Session
及 Conversation 固定 Scope 数据库结构会被 init-db / create_schema 拒绝；[迁移步骤](../docs/design/agent-api.md#8-已有数据库升级)
保留 Conversation ID 与消息，本次未对真实开发 / 生产库执行 DDL。

Qdrant 的连接、写入和恢复属于产品持久化逻辑，因此放在 `server/storage/`；Engine
只负责生成解析结果、向量与检索算法。Qdrant payload 不保存业务正文；当前
Embedding 的 durable source 是 MySQL，Qdrant 始终只是可重建派生索引。

## HTTP 分层

```text
server/router/      URL、Method、Response Model 和 API Handler 的绑定
server/api/         参数/Header/Body 校验、HTTP 响应映射、SSE、异常转 HTTP
server/service/     业务用例、资源归属与状态校验、跨资源编排
server/bootstrap.py AniVAG Server 的数据库、Storage、Gateway、Queue 和 Service 装配
```

调用方向为 `router → api → service → repository/storage`。Router 不实现 Handler；
API 不查询业务事实后自行判断归属或状态。Content-Range 语法和 Body 长度在 API 校验，
workspace 归属、会话总大小、分片范围、缺片计算、Job 归属和 READY 检索 Scope 由
Service 负责；Repository 保留事务内的状态与并发约束。

HTTP 专用 Request/View 位于 `server/api/schema.py`；API、CLI、Agent 共用的检索证据和
应用报告留在 `server/schema.py`。`server/api/app.py` 只创建应用、配置生命周期与中间件、
注册异常处理和 Router。

## 后台任务与共享代码

`server/mq/` 是消息基础设施边界，当前使用 MySQL DB Queue，不依赖外部消息中间件。
AniVAG Server 启动进程内 ParseJobConsumer 领取任务，再调用业务处理服务；Event 仍只
表达已经发生的事实。API 保存完整视频并提交 ParseJob 后，Consumer 在同一 Server 进程
继续处理，浏览器关闭不影响已经入队的任务。

```text
server/config/                   配置类型、环境变量读取与校验
server/constants/workspace.py    本地默认 Workspace；不承担身份认证
server/utils/                    时间、文本校验、异常摘要等无业务取数的纯函数
server/bootstrap.py              唯一 Composition Root 和应用生命周期资源装配
server/service/video_processing.py
                                 解析、checkpoint、向量化、入库与索引同步编排
server/mq/contracts.py            Queue / Handler 的最小内部协议
server/mq/mysql_queue.py          MySQL Claim、Lease、Retry、Ack 和 Reaper adapter
server/mq/consumer.py             进程内消费循环与 Lease Heartbeat
server/mq/__main__.py             仅保留 --once 单任务诊断入口
```

配置入口为 `ServerSettings.from_environment()`，`from server.config import
ServerSettings` 保持可用。Upload、Agent、VideoProcessing、Retrieval 和 SSE 的配置类型
分别放在对应配置模块；配置模块不依赖 Service。任务状态等领域枚举继续放在
`server/model/`，不集中进无边界的 constants 文件，也不新增空的 common 目录。

部署调优项已补到 `.env.example`：`ANIVAG_SSE_POLL_SECONDS`、
`ANIVAG_SSE_KEEPALIVE_SECONDS`、`ANIVAG_DATABASE_POOL_RECYCLE_SECONDS`、
`ANIVAG_RETRIEVAL_CHANNEL_TOP_K`、`ANIVAG_RETRIEVAL_RRF_K` 和双路权重
`ANIVAG_RETRIEVAL_TEXT_WEIGHT` / `ANIVAG_RETRIEVAL_VISION_WEIGHT`。
Embedding 模型与 FPS 覆盖值由 Settings 读取，API 与进程内 Consumer 使用同一套配置。
Consumer 的 Lease、Heartbeat、Poll 和 Retry 分别使用 `ANIVAG_MQ_*` 配置；旧的
`ANIVAG_WORKER_*` 名称暂时作为兼容回退。Index Sync 的重试间隔已独立为
`ANIVAG_INDEX_SYNC_RETRY_BASE_SECONDS` / `ANIVAG_INDEX_SYNC_RETRY_MAX_SECONDS`，不再
与 Queue delivery retry 共用配置。

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

## Upload-to-Search API 与进程内 Consumer

前端对接所需的完整请求、响应、分片续传、任务 SSE 与检索契约见
[前端 API 对接文档](../docs/design/frontend-api.md)。

先初始化 MySQL，再启动唯一常驻的 AniVAG Server：

```bash
uv run python -m server.cli init-db
uv run python -m server.api
```

Server 默认监听 `127.0.0.1:9621`，OpenAPI 位于 `/docs`。HTTP API 写入 MySQL 中的事实
和任务，进程内 ParseJobConsumer 从 MySQL Claim；任务的持久性来自数据库中的 Job、
Lease 和 Fencing，不依赖进程内内存队列。
`GET /healthz` 会在 `components.parseConsumer.status` 返回 `running`、`stopped` 或
`failed`；只有 Consumer 正常运行时顶层状态才是 `ok`，否则为 `degraded`。本地启动脚本
也以这个契约判断 Server readiness，不再通过 PID 或日志文本猜测消费端是否存活。

需要单步诊断领取链路时，可运行：

```bash
uv run python -m server.mq --once
```

该命令复用 Server 的统一装配并至多处理一个到期任务；它不是常驻服务，不应与
`server.api` 并行启动。

上传顺序由后端成功创建 `UploadSession` 的顺序确定。同一 Series 在行锁内分配下一个
追加序号，新会话的 `episodeOrder` 与 `uploadSequence` 相等；不解析文件名，也不采纳
客户端指定的序号。`uploads/create` 的两个旧序号参数仅保留兼容接收，现已可省略。
幂等重放返回原序号；已分配序号不复用，既有视频不重新编号。

ParseJobConsumer 按固定上传序号领取任务，更早的未过期上传、未成功解析的视频（含重试等待和
失败）会阻塞后续任务；传输完成先后不会改变 Parse 顺序。已过期且未生成 Video 的
上传不再占用解析队列。这一门禁负责视频的执行顺序，跨视频 Context 传递仍未实现。

主要接口：

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
POST /api/uploads/delete?uploadSessionId={uploadSessionId}&workspaceId={workspaceId}
POST /api/uploads/retry-parse?uploadSessionId={uploadSessionId}&workspaceId={workspaceId}&expectedJobVersion={version}
POST /api/uploads/reparse?uploadSessionId={uploadSessionId}&workspaceId={workspaceId}&expectedJobVersion={version}
GET  /api/jobs/list?workspaceId=local-default
GET  /api/jobs/get?jobId={jobId}
GET  /api/jobs/events?jobId={jobId}             text/event-stream
GET  /api/videos/content?videoId={videoId}      支持 Range
POST /api/search
POST /api/agent/conversations/create
GET  /api/agent/conversations/list?workspaceId={workspaceId}
GET  /api/agent/conversations/get?conversationId={conversationId}
GET  /api/agent/messages/list?conversationId={conversationId}
POST /api/agent/messages/send?conversationId={conversationId}
GET  /api/agent/messages/events?conversationId={conversationId}
```

`UploadSession` 是用户任务的聚合根；`Video`、一个或多个 Pipeline 版本的
`SeriesParseJob`、Checkpoint 和 `VideoChunk` 都是同一任务产生的内部记录。上传面板
可删除未完成任务，解析任务页可用同一个 `uploadSessionId` 删除等待解析、解析失败或
已完成的整项任务，不另设 Job 删除接口。

解析失败后的 `retry-parse` 从现有 Checkpoint 继续；`reparse` 则仅允许已完成上传的
终态主任务，清空该 Job 的 Checkpoint 并从整集开头重跑。二者都复用原 Job 并以
`expectedJobVersion` 防止重复提交，attempt 和模型用量历史不会清零。已有成功结果在
新解析完成 MySQL 导入前保持可检索，旧媒体与向量不会在重跑前被删除。

删除分两阶段执行。第一个 MySQL 事务锁定 UploadSession 及其 Video 的全部 ParseJob，
阻止新的分片和 Consumer claim，并写入 `DELETION_PENDING` 墓碑；随后幂等清理上传暂存、
所有 Job 目录、Video 媒体目录和 Qdrant point；第二个事务删除模型调用/用量、Job、
Checkpoint、VideoChunk/Info/Embedding/Context/IndexIntent 和 Video。成功后保留
`EXPIRED` UploadSession 及原序号/幂等键，以拦截迟到请求和保证重复 DELETE 返回同一
结果。进程在中途退出时，API 启动会扫描持久删除标记继续清理。重新上传须使用新幂等键。
`COMPLETING` 或存在 `RUNNING` ParseJob 时返回 409，等当前受租约保护的写入结束后再删。
删除与 IndexSync 对同一环境和 Qdrant Collection 使用跨进程互斥锁；所有索引同步、巡检
和重建查询也会排除 `DELETION_PENDING` 对应的视频。因此即使进程在删完 point、提交
最终 MySQL 清理前退出，旧 UPSERT Intent 也不会把待删 point 写回来。

解析主任务最终 `FAILED` 后，可按同一个 `uploadSessionId` 手动重试。请求必须携带当前
`expectedJobVersion`；服务端按 UploadSession → ParseJob 的锁顺序校验并只把
`UploadSession.job_id` 指向的主 Job 重新排队。重试保留 attempt 历史、Checkpoint、阶段、
进度、用量记录和工作目录；若旧额度已用尽，只把 `maxAttempts` 扩展一个名额，Consumer
实际领取时才增加 `attemptCount`。版本不匹配、删除中或任务已不再 FAILED 均返回 409，
因此并发点击和响应丢失后的同版本重放不会多投递一次解析尝试。

Agent 对话中的既有回答和证据快照属于聊天历史，不在任务级删除范围内；删除后不能再用
其中的 VideoChunk ID 加载实时媒体证据。

创建 Conversation 的 JSON 为 `workspaceId, title`，不需要父容器 ID；
`Idempotency-Key` 在 Workspace 内唯一。Conversation 详情直接返回 `workspaceId`，
同一 Conversation 可以在后续消息选择不同动画。`messages/send` 的 JSON 可传 `seriesIds`，
省略或空数组默认搜索当前 Workspace 全部 READY 动画；显式选择未 READY 动画返回 409。
每次 Kick 冻结实际 Scope，USER / ASSISTANT 消息均公开保存 `seriesIds`。前端复用
`GET /api/series/list?workspaceId=...` 的 `seriesId/name/status` 实现多选；改变选择只影响
下一次发送。消息幂等限定在 Conversation，并按原请求范围比较；原 Key 的“全部”重放
不会因新增动画而扩大旧消息 Scope。完整召回 `hits[].evidence.seriesId` 必填，来自 MySQL join。
`messages/send` 默认 JSON，通过 `Accept: text/event-stream` 返回
started / evidence / delta / completed / failed；`messages/events` 单独恢复数据库消息快照。

`Complete` 会在校验所有分片后流式合并、计算整文件 SHA-256、发布 Original Video、探测
时长，并在同一个 MySQL 事务中创建 `Video + SeriesParseJob`。重复请求复用相同结果。
Artifact URI 形如 `artifact://development/...`，test/development/production 使用独立目录；
Server 内的 API/Consumer 依赖同一个 `ArtifactStore` 边界，当前 M1 adapter 是
`LocalArtifactStore`。

VideoProcessingService 为 Consumer 领取的每个 Job 保存单调 checkpoint：

```text
PREPARED → SPLIT_READY → PARSED → TEXT_EMBEDDED → VISION_EMBEDDED → INDEXED
```

AnalysisWindow 带上下文重叠，仅供模型解析；Vision vector 始终读取无重叠的 canonical
VideoChunk MP4。Text vector 读取同一 VideoChunk 的完整 retrieval document；两路向量以
同一个稳定 `videoChunkId` 入库与建索引。Server 异常退出后复用本地持久化产物续跑，旧
`leaseVersion` 不能提交 checkpoint 或最终 MySQL 事实。SSE 只发送 MySQL 中对 UI 有意义
的状态、阶段、进度或 checkpoint 变化，不把纯 Heartbeat 当成进度事件；断线后仍以 Job
查询结果为准。

## 运行日志

AniVAG Server 在进程启动时配置 Python logging，同时写入终端和轮转文件，默认
`ANIVAG_LOG_LEVEL=info`；也可用 `python -m server.api --log-level debug` 覆盖。
日志文件不依赖 `./start.sh`，直接启动 Server 也会落盘：

```text
outputs/server/logs/development/api.log
```

一次性运行 `python -m server.mq --once` 时会单独写入同目录的 `mq.log`；该文件只属于
诊断入口，不代表存在常驻 MQ 或 Worker 进程。

test / production 使用各自目录。`ANIVAG_LOG_DIRECTORY` 可覆盖日志根目录；
`ANIVAG_LOG_MAX_BYTES` 默认 10485760（10 MB），`ANIVAG_LOG_BACKUP_COUNT` 默认 5。
文件满后轮转为 `api.log.1` 到 `api.log.5`，`.1` 为最新历史文件。
通过 `./start.sh` 启动时，`outputs/local-services/api.log`、`web.log`、
`mysql.log` 和 `qdrant.log` 另存进程输出，包含启动信息；这组脚本输出文件不轮转。

Server 业务日志包含时间、级别、模块和 `request_id`。HTTP 请求记录 method、
path、status 与响应头返回前的 `duration_ms`；上传业务日志记录 `upload_session_id`、
序号、大小、合并/校验/媒体探测/创建任务的阶段、完成后的 `video_id` / `job_id` 和失败
原因与各分片的确认记录。请求编号通过 `X-Request-Id` 与错误体返回，上传面板
的错误提示会显示它。请求日志不打印 query、请求正文或认证 Header；未预期异常记录
异常类型与调用栈。Consumer 的领取、解析进度、重试、失败和完成日志也写入同一
`api.log`，并附带时间与级别。

```bash
tail -f outputs/server/logs/development/api.log
rg 'upload_session_id=会话ID|job=任务ID|request_id=请求编号' outputs/server/logs/development/*.log*
```

目前日志按进程本地落盘，未接入集中检索、Metrics 或 Trace。
上传详细错误同时保存在 `upload_session.last_error`，解析错误保存在任务/checkpoint
记录中；公开接口只返回适合用户阅读的通用提示。日志配置改动在进程重启后生效。

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

历史 VideoChunk 缺少首帧时，先按 workspace/Series 做只读检查，再显式执行回填：

```bash
uv run python -m server.cli backfill-keyframes \
  --workspace-id local-default \
  --series-id 77285f16-3e5b-592e-ba15-8b224b619806 \
  --limit 100 \
  --dry-run

uv run python -m server.cli backfill-keyframes \
  --workspace-id local-default \
  --series-id 77285f16-3e5b-592e-ba15-8b224b619806 \
  --limit 100 \
  --apply
```

该命令没有模型调用，不重跑解析或向量，只从已有受管 canonical VideoChunk 提取并补充
缺失的带原视频绝对时间首帧。找不到 canonical 媒体时默认跳过；不会由 API 请求或服务
启动自动触发。

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
