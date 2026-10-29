# seekino Server

Server 的正式 Upload-to-Search 链路只使用 V2、MySQL 和 Elasticsearch：分片上传与
环境隔离的 Artifact Store 提供媒体，VideoParseTask/Chunk/Interval/Prefix 状态机保存恢复事实，
ES 保存同版本的检索投影。API、Agent Chat 和 CLI 共用 V2 检索 Service；
`SEEKINO_PARSE_EXECUTION_MODE` 只接受 `v2`，启动必须显式配置 ES。
模型协议由 Gateway 实现，Server 进程内复用 Engine 的领域转换、索引投影和融合能力。
V1 解析、旧 Consumer、Qdrant 客户端及离线导入工具已删除；历史表的 ORM 映射仅供已有数据清理使用。

Multi-Agent 对话作为独立领域建立在正式检索之上：Workspace 直接拥有
AgentConversation，每个 Conversation 保存聊天历史，AgentMessage 保存每次发送的 Series Scope，
不再增加 Agent Session 容器。独立 Harness 用 LangGraph 编排 Query / Evaluator 最多
3 轮，审核通过后由 Chat 实时输出最终正文，Output 保留结构化视频证据。
完整接口见 [Agent API 对接文档](../docs/design/01-mvp/agent-api.md)。旧 Agent Session
及 Conversation 固定 Scope 数据库结构会被 init-db / create_schema 拒绝；[迁移步骤](../docs/design/01-mvp/agent-api.md#8-已有数据库升级)
保留 Conversation ID 与消息，本次未对真实开发 / 生产库执行 DDL。

Engine 构造不依赖媒体、数据库或模型的 ASR/OCR 正式事件，并提供索引投影、embedding
复用和带版本身份的融合函数。Agent 定义 OCR Prompt、理解与 Refine 流程；Server Host
准备媒体、执行模型并持久化结果。Repository 在一个事务中提交 Chunk 成果和 Interval mask，
DB Queue 按持久状态领取可执行任务；提交后的通知用于唤醒 Video 主协调器；DB Queue/Consumer 独立扫描并推进后续领取。

检索评分桥接位于 `server/service/retrieval_rerank.py`：它把候选转换成领域评分 Prompt，
调用 Gateway 的通用模型接口，并交由 Engine 校验计划和结果。Gateway 不依赖 Engine RAG
合同，也不读取嵌套的视频候选结构。

Elasticsearch 的连接、写入和恢复位于 `server/storage/`；业务正文与任务版本的 durable
source 始终是 MySQL，ES 是可按当前合同重建的派生数据。

## 模型 Token 用量

`GET /api/usage/overview?workspaceId=...` 从 `model_usage` 查询最近 30 天已记录调用。
可用 `fromStartTime`/`toStartTime`（Unix 毫秒）、`seriesId`、`runId` 缩小范围；最大窗口 31 天。
SQL 先按 workspace、半开 UTC 窗口、精确 run/系列筛选，再稳定排序读取最多 20,000 条。
响应固定 `source=local`，状态为 `available` 或 `unavailable`；只包含 Token，不返回费用或货币。

V2 解析/index 使用真实视频解析任务 ID；助手 direct、Web 和检索共用真实助手消息 ID。
Chat 在实际执行线程绑定 owner，内部 embedding/rerank 继承它，不创建独立 retrieval run。
Gateway 最终事件触发一次短事务，写入失败记录 warning 并保留业务结果。
SDK 明细替代父用量，缓存和推理子集不重复加入总量；未知、main_loop 或截断保持完整总量 null，
已知数字通过 `knownTokens` 单独报告。读取失败不会返回 available 的全零结果。

新表沿用现有模型登记和 init-db，真实建表须先核实操作授权。默认 `tracing.enabled=false`，
bootstrap 不装配 Langfuse writer/reader；可选模块和空凭据字段保留。
冻结的 V1 旧链不注入新统计；旧表、价格历史不删除、不回填、不与新统计合并。
`model_usage` 没有业务对象外键，删除视频/消息不级联删除已记录用量。
预算、租约、heartbeat、取消和模型控制仍由原执行管理器负责。
详细合同见 [模型 Token 用量](../docs/parse-model-usage.md)。

## HTTP 分层

```text
server/router/      URL、Method、Response Model 和 API Handler 的绑定
server/api/         参数/Header/Body 校验、HTTP 响应映射、SSE、异常转 HTTP
server/cli.py       终端命令参数、命令分发和 JSON 输出
server/service/     业务用例、资源归属与状态校验、跨资源编排
server/bootstrap.py 完整应用装配、Queue 和应用生命周期
server/common/      CLI 与完整应用共用的 Storage、检索依赖装配
```

调用方向为 `router → api → service → repository/storage`。Router 不实现 Handler；
API 不查询业务事实后自行判断归属或状态。Content-Range 语法和 Body 长度在 API 校验，
workspace 归属、会话总大小、分片范围、缺片计算、Job 归属和 READY 检索 Scope 由
Service 负责；Repository 保留事务内的状态与并发约束。

HTTP 专用 Request/View 定义于 `server/model/dto/` 和 `server/model/vo/`，
`server/api/schema.py` 保留兼容聚合；API、CLI、Agent 共用的检索证据和
应用报告留在 `server/schema.py`。`server/api/app.py` 只创建应用、配置生命周期与中间件、
注册异常处理和 Router。

`python -m server.api` 收到停服信号后停止接受连接，最多等待 5 秒让现有 HTTP 请求收尾，
再取消仍打开的 SSE 等请求并进入应用生命周期关闭。Consumer、Agent 等业务服务先收尾，
随后释放数据库、ES 等 Caller 资源；这 5 秒不截断业务服务的关闭过程。

## 后台任务与共享代码

`server/mq/` 是消息基础设施边界，当前使用 MySQL DB Queue，不依赖外部消息中间件。
seekino Server 启动进程内 V2 Consumer 领取任务，再调用业务处理服务；Event 仍只
表达已经发生的事实。API 保存完整视频并提交 VideoParseTask 后，Consumer 在同一 Server
进程继续处理，浏览器关闭不影响已经入队的任务。

```text
server/config/                   配置类型、环境变量读取与校验
server/constants/workspace.py    本地默认 Workspace；不承担身份认证
server/utils/                    时间、文本校验、异常摘要等无业务取数的纯函数
server/model/identifiers.py       集中维护业务实体的稳定 UUID5 生成规则
server/common/storage.py         DB、ES、Artifact 依赖构建与短期资源关闭
server/common/retrieval.py       Encoder、查询模型、评分模型与检索 Service 装配
server/bootstrap.py              完整应用的 Composition Root 和生命周期资源装配
server/service/video_parse.py    V2 Host：媒体、模型执行与成果提交
server/repository/video_parse.py V2 事务、Lease/Fencing、成果与 Interval mask 原子提交
server/mq/video_parse_queue.py   基于持久状态的 Claim、Retry 和恢复扫描
server/mq/video_parse_consumer.py 进程内 V2 消费循环与 Lease Heartbeat
tests/manual/server_mq_once.py    --once 单任务手动诊断入口
```

配置入口为 `ServerSettings.from_environment()`，`from server.config import
ServerSettings` 保持可用。CLI 在选中命令后按需调用对应配置模块：`init-db` 只读取
数据库配置，`init-es-index` 只读取 ES 配置与维度，`search` 再读取模型与检索配置。
显式命令参数优先于对应环境变量，未指定时使用配置默认值。Upload、Agent、VideoParse、
Retrieval 和 SSE 的配置类型分别放在对应配置模块；配置模块不依赖 Service。
任务状态和业务 ID 生成规则留在 `server/model/`；通用 UUID 校验位于
`server/utils/validation.py`。所有包初始化保持轻量，调用方直接导入具体模块。

CLI 与 HTTP 是并列的调用入口，CLI 直接调用既有 Service/Repository。
它复用 `common` 的 Storage 和检索构建，不启动完整聊天运行图或 Consumer。
CLI 自建 DB/ES 在命令成功或失败后关闭，查询与评分 SDK 按每次调用创建并关闭；
完整应用继续由 `ApplicationServices` 持有资源并按既有顺序释放。

部署调优项已补到 `.env.example`：`SEEKINO_SSE_POLL_SECONDS`、
`SEEKINO_SSE_KEEPALIVE_SECONDS`、`SEEKINO_DATABASE_POOL_RECYCLE_SECONDS`、
`SEEKINO_RETRIEVAL_CHANNEL_TOP_K`、`SEEKINO_RETRIEVAL_RRF_K` 和非 Agent 向量双路默认权重
`SEEKINO_RETRIEVAL_TEXT_WEIGHT` / `SEEKINO_RETRIEVAL_VISION_WEIGHT`。
Embedding 模型与 FPS 覆盖值由 Settings 读取，API 与进程内 Consumer 使用同一套配置。
Consumer 的 Lease、Heartbeat、Poll 和 Retry 分别使用 `SEEKINO_MQ_*` 配置；旧的
`SEEKINO_WORKER_*` 名称暂时作为兼容回退。冻结 Index Sync 的配置不再由正式 Settings 暴露。

V1 Job 与 V2 Task SSE 都忽略纯心跳变化，并在发送终态快照后关闭。快照中的
`canRetry` / `canReparse` / `canDelete` 只代表该次读取时的精确租约门禁；终态后租约变化
不会延长 SSE，客户端应重新 GET/list。Web TasksPage 每 5 秒刷新列表，UploadProvider
则订阅当前 V1 Job 或 V2 Task 的单任务 SSE。Agent SSE 仍返回完整 Message 快照，包含
500 条之后的消息以及同一条消息的状态变化。保活按单调时钟的经过时间计算，
不是固定轮询次数。完整快照仍有随聊天长度增长的读取和传输成本，后续如改增量协议，
须另行设计状态更新游标，不能只按 sequenceNo 跳过已有的 PROCESSING 消息。

## 数据库隔离

开发、测试和正式环境必须使用不同的数据库。CLI 会强制检查数据库名：

- `development`：数据库名以 `_dev` 或 `_development` 结尾；
- `test`：数据库名以 `_test` 结尾；
- `production`：拒绝上述非正式环境后缀；
- SQLite 只允许用于隔离测试。

Elasticsearch 索引使用相同的物理隔离规则：测试以 `_test` 结尾，开发以 `_dev` 或
`_development` 结尾，正式环境拒绝这两类后缀。

创建开发库后初始化表结构：

```sql
CREATE DATABASE seekino_development
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

```bash
export SEEKINO_ENVIRONMENT=development
export SEEKINO_DATABASE_URL='mysql+pymysql://seekino:password@127.0.0.1:3306/seekino_development?charset=utf8mb4'
export SEEKINO_ELASTICSEARCH_URL='http://127.0.0.1:9200'
export SEEKINO_ELASTICSEARCH_INDEX='seekino_video_chunks_development'
export SEEKINO_TEXT_VECTOR_DIMENSIONS=1024
export SEEKINO_VISION_VECTOR_DIMENSIONS=1024

uv sync
uv run python -m server.cli init-db
```

上述 `init-db` 只负责初始化空库或校验已经使用新命名的库。若数据库仍包含旧
`video_segment*` 表，或 `context_snapshot/index_sync_intent` 仍使用旧列名，Server 会在
创建任何新表前抛出 `SchemaUpgradeRequiredError`，防止形成新旧 Schema 混合状态。本次
术语重构没有自动修改现有 MySQL；旧开发库必须先受控重命名表、列、外键和约束并核对
原有 24 行数据，再运行新代码。

历史 Schema 保留六个视频领域模型：`Series`、`Video`、
`VideoChunk`、`VideoChunkInfo`、`ContextSnapshot`、
`VideoChunkEmbedding`；另有 `UploadSession/UploadSegment`、
`SeriesParseJob/VideoChunkCheckpoint` 和 `IndexSyncIntent`，没有 `Episode` 表。所有
MySQL 表使用 InnoDB。正式 V2 状态由 VideoParseTask、ChunkParseTask、IntervalParseTask
及对应成果表承载；Prefix 的 lease/cursor 保存在 VideoParseTask.detail，
索引缓存与 receipt 保存在 Chunk 成果 detail。

## Upload-to-Search API 与进程内 Consumer

前端对接所需的完整请求、响应、分片续传、任务 SSE 与检索契约见
[前端 API 对接文档](../docs/design/01-mvp/frontend-api.md)。

先初始化 MySQL，再启动唯一常驻的 seekino Server：

```bash
uv run python -m server.cli init-db
uv run python -m server.api
```

Server 默认监听 `127.0.0.1:9621`，OpenAPI 位于 `/docs`。HTTP API 写入 MySQL 中的事实
和任务，进程内 V2 Consumer 从 MySQL Claim；任务的持久性来自数据库中的 Task、
Lease 和 Fencing，不依赖进程内内存队列。
`GET /healthz` 会在 `components.parseConsumer.status` 返回 `running`、`stopped` 或
`failed`；只有 Consumer 正常运行时顶层状态才是 `ok`，否则为 `degraded`。本地启动脚本
也以这个契约判断 Server readiness，不再通过 PID 或日志文本猜测消费端是否存活。

需要单步诊断领取链路时，可运行：

```bash
uv run python -m tests.manual.server_mq_once --once
```

该命令复用 Server 的统一装配并至多处理一个到期任务；它不是常驻服务，不应与
`server.api` 并行启动。

上传顺序由后端成功创建 `UploadSession` 的顺序确定。同一 Series 在行锁内分配下一个
追加序号，新会话的 `episodeOrder` 与 `uploadSequence` 相等；不解析文件名，也不采纳
客户端指定的序号。`uploads/create` 的两个旧序号参数仅保留兼容接收，现已可省略。
幂等重放返回原序号；已分配序号不复用，既有视频不重新编号。

V2 DB Queue 按到期时间和持久任务状态扫描并领取，每次执行受 Lease/Fencing 保护。
同系列跨集 Prefix 根据 `episode_order` 和已提交 frontier 推进；上传序号保留为业务顺序，
调度不使用旧 V1 的整集失败阻塞规则。

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
GET  /api/jobs/video-parse/list?workspaceId=local-default
GET  /api/jobs/video-parse/get?videoParseTaskId={taskId}&workspaceId={workspaceId}
GET  /api/jobs/video-parse/events?videoParseTaskId={taskId}&workspaceId={workspaceId}  text/event-stream
GET  /api/videos/content?videoId={videoId}      支持 Range
POST /api/search
POST /api/agent/conversations/create
GET  /api/agent/conversations/list?workspaceId={workspaceId}
GET  /api/agent/conversations/get?conversationId={conversationId}
GET  /api/agent/messages/list?conversationId={conversationId}
POST /api/agent/messages/send?conversationId={conversationId}
GET  /api/agent/messages/events?conversationId={conversationId}
```

`UploadSession` 是上传任务的聚合根；`Video`、V2 VideoParseTask、Chunk/Interval 成果
以及保留的历史 V1 记录都是关联数据。上传面板和解析任务页使用同一个
`uploadSessionId` 删除等待解析、解析失败或已完成的整项任务。

V2 `retry-parse` 复用失败的当前任务，从已提交成果和失败阶段恢复；`reparse` 创建新的
Task 和内容版本。请求仍使用 `expectedJobVersion` 这个参数名，V2 下它承载当前 Task 的
`version`，在事务内防止重复或过期操作。检索只选择当前最新、未删除且 `COMPLETE` 的
Task；新版本重解析期间不会静默回退到旧 Task。

删除分两阶段执行。第一个 MySQL 事务锁定 UploadSession 和关联任务，校验正在执行的
Lease，将 UploadSession 标记为 `EXPIRED`，以 `last_error_code=DELETION_PENDING` 保存墓碑，
阻止新的写入和 claim。先清理上传暂存，再按索引事实删除 V2
ES Document，使用 `refresh="wait_for"` 并核验文档不可见，再清理媒体目录。
ES 删除失败、删除验证失败或有文档待删但未配置 ES 时，保留墓碑和数据库记录，供重试恢复。
第二个事务清理 Video、V2 任务/成果及历史 V1 Chunk/Checkpoint/IndexIntent 等关联记录。
数据库待删 Chunk 与 ES 待删 Document 各自保留用途，不再向 Qdrant 发出请求。
成功后保留 `EXPIRED` UploadSession 及原序号/幂等键；重复 `POST /api/uploads/delete` 返回同一结果。
API 启动扫描持久墓碑继续清理，重新上传须使用新幂等键。


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
时长，并在同一个 MySQL 事务中创建 `Video + VideoParseTask`。重复请求复用相同结果。
Artifact URI 形如 `artifact://development/...`，test/development/production 使用独立目录；
Server 内的 API/Consumer 依赖同一个 `ArtifactStore` 边界，当前 M1 adapter 是
`LocalArtifactStore`。

宿主机的持久媒体根目录默认是 `assets/library/`，由 `SEEKINO_ARTIFACT_ROOT` 配置；
Docker 全栈继续使用持久卷中的 `/data/artifacts`。原片、帧图与附件需要和 MySQL 配套备份。
旧环境显式配置的 `outputs/server/artifacts` 等路径不会自动迁移；更换根目录时保留
`development/` 等环境子目录及其内部结构，数据库中的 `artifact://...` URI 可以保持不变。
迁移步骤见 [部署说明](../deploy/README.md#persistent-media-root)。

## 运行日志

seekino Server 在进程启动时配置 Python logging，同时写入终端和轮转文件，默认
`SEEKINO_LOG_LEVEL=info`；也可用 `python -m server.api --log-level debug` 覆盖。
日志文件不依赖 `./start.sh`，直接启动 Server 也会落盘：

```text
outputs/server/logs/development/api.log
```

一次性运行 `python -m tests.manual.server_mq_once --once` 时会单独写入同目录的 `mq.log`；该文件只属于
诊断入口，不代表存在常驻 MQ 或 Worker 进程。

test / production 使用各自目录。`SEEKINO_LOG_DIRECTORY` 可覆盖日志根目录；
`SEEKINO_LOG_MAX_BYTES` 默认 10485760（10 MB），`SEEKINO_LOG_BACKUP_COUNT` 默认 5。
文件满后轮转为 `api.log.1` 到 `api.log.5`，`.1` 为最新历史文件。
通过 `./start.sh` 启动时，`outputs/local-services/api.log`、`web.log`、
`mysql.log` 保存脚本启动的原生数据库输出，包含启动信息；这组脚本输出文件不轮转。

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

`hydrate` 只回查当前 V2 事实，必须传 `--tenant-id` 和片段 ID；旧的 `import-parse`、
`backfill-keyframes` 与 V1 Job 查询/SSE 路由已移除。上传和解析任务统一使用 `/api/jobs/video-parse/*`。

## V2 索引与恢复

索引写入由 V2 Worker 推进。`sync-index`、`reconcile-index` 以及 V1 `rebuild-index`
入口已退役；历史 IndexSyncIntent 表保留用于兼容旧数据，正式流程不消费它。

### ES 初始化与 V2 接入进度

`init-es-index` 创建或校验显式配置的 ES 8.19 物理索引。
V2 的索引阶段属于 `interval_parse_task`：Refine 成果提交后进入 `INDEXING`，
ES 写入与就绪检查成功后才进入 `COMPLETE`；索引失败保留成果，恢复该 Interval
的索引阶段。ES 文档按 `parse_task_id:video_chunk_id` 稳定覆盖，保留来源内容版本。

```bash
uv run python -m server.cli init-es-index --help
```

初始化要求显式配置隔离环境的 ES URL、索引名及向量维度。正式模式为
`SEEKINO_PARSE_EXECUTION_MODE=v2`，上传只建立带接纳标记的 V2 任务，DB Queue
Consumer 会执行 Prepare、Chunk Understanding、Interval Refine、逐 Chunk 索引放行、
ES 写入与恢复扫描。缺少 ES 配置或选择 `v1` 时 Server 会拒绝启动。
`sync-es-index`、`repair-es-published`、`cleanup-es-deleted` 和
`search-es-published` 已随独立索引发布实现移除。

已有 V2 数据库需应用
[`20260928_task_indexing_contract.sql`](../docs/migrations/20260928_task_indexing_contract.sql)，
更新状态/租约约束并移除三张 ES 发布表。该脚本只接受空的 V2 任务/成果及 ES 发布表，
非空旧数据需单独制定迁移；`init-db` 会拒绝旧任务约束。旧建表 SQL 保留为迁移历史，
从旧版本升级时需继续执行上述后续脚本。V2 Worker 用量还需随后应用
[`20260928_v2_worker_runtime.sql`](../docs/migrations/20260928_v2_worker_runtime.sql)。
迁移不会由应用启动自动执行；新库直接使用当前 ORM 建表。

ES mapping 合同升级为 `seekino-video-chunk-es-4`，旧物理索引无法通过合同检查，
需另行重建。当前初始化命令不执行数据迁移。`search` 和 `rebuild-index` 的
`--execution-mode` 只接受 `v2`。`rebuild-index` 必须指定当前 `--video-parse-task-id`，
只重置该任务的索引阶段，再由 Worker 从已提交成果恢复写入，不直接创建外部索引客户端。
API/Chat 使用 ES 三路候选和同版本 MySQL 回源；Web 已接入 V2 Task 列表、单任务事件及
retry/reparse/delete 门禁。

## 正式检索闭环

Composition Root 只装配 V2 正式检索 Service。
V2 只选择每个 Video 最新、未删除且 `COMPLETE` 的 VideoParseTask，并用 ES lexical /
semantic / vision 三路候选调用 Engine 的 versioned fusion。MySQL 回源逐项核对 task、
content version、index generation、source/projection revision、retrieval contract 与两个
encoder identity；任一候选不一致即 fail closed。Series 中只有部分 Video 满足条件时，
结果明确返回 `partial=true`、实际 `searchedVideoIds` 和 `warnings`，不会把尚未完成或失败
任务的旧版本静默纳入。

```bash
uv run python -m server.cli search \
  --query '爽世为什么哭着质问大家要演奏春日影' \
  --series-id 77285f16-3e5b-592e-ba15-8b224b619806 \
  --top-k 5
```

多 Series 检索时重复传入 `--series-id`。CLI 和 `/api/search` 使用配置的
Lexical/Text/Vision 默认权重以及相同的模型源、Endpoint 与实际编码模型身份解析。
CLI 的 `--channel-top-k`、`--rrf-k`、`--text-weight`、`--vision-weight`
只覆盖显式指定的值，其余检索策略继承配置。Query Agent 获得 Lexical 工具合同，
并为每次 Tool Call 自主提交三路连续权重。
每路 Top 20、`rrfK=60`；`--exact` 只用于向量检索的确定性诊断。输出的每个 `hit` 包含一个
`VideoChunkCandidate`（融合与三路 rank/score）和一个同 ID 的 `VideoChunkEvidence`（Series、
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
