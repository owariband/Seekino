# seekino Server

Server 的正式 Upload-to-Search 链路只使用 V2、MySQL 和 Elasticsearch：分片上传与
环境隔离的 Artifact Store 提供媒体，VideoParseTask/Chunk/Interval/Prefix 状态机保存恢复事实，
ES 保存同版本的检索投影。API、Agent Chat 和 CLI 共用 V2 检索 Service；
`SEEKINO_PARSE_EXECUTION_MODE` 只接受 `v2`，启动必须显式配置 ES。
模型协议由 Gateway 实现，Server 进程内复用 Engine 的领域转换、索引投影和融合能力。
V1 解析、旧 Consumer、Qdrant 客户端及离线导入工具已删除。旧 `SeriesParseJob`／`VideoChunkCheckpoint` 和专用用量模型也已退出；已有库按[退役迁移说明](../docs/migrations/20261031_retired_parse_models.md)处理，新库不再创建这三张旧表。

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

## Chat 正文流式与计时证据

Deep Agents 的实时正文仍通过持久化后的累积 `answer.snapshot` 发送，`textStream` 可选字段携带模型调用和增量来源；非流式、恢复快照及完成后补发不伪造来源。新增 `answer.model.completed` 仅表示一次模型调用结束，整轮完成仍等待原业务校验和持久提交。

`service/agent_chat_stream.py` 在写库前合并普通正文快照：每次模型调用首段立即保存，后续在增量到达时按 50 ms 间隔选择最新快照。业务边界刷新暂存正文，模型完成后的刷新不带实时来源；取消/失败也保存已接收的合法前缀。该策略由 Server 执行，Supervisor 只组装并校验回答。断开 SSE 不自动重跑请求；现有权限、版本与恢复合同继续生效，无数据库 Schema 迁移。

Server 可通过 `SEEKINO_CHAT_TRACE_DIR` 开启 Chat 诊断，在调用作用域内向 Gateway 注入目录；Gateway 不通过 Chat 消息身份选择采集策略。有效 FTT 需关联供应商 trace 与浏览器实际展示，不能用服务端快照耗时替代。设计与验收见 [Chat 流式 TRD](../docs/design/03-product-reassessment/chat-streaming-trd.md)。

## Parse 模型正文草稿

Server 在 Parse 调用作用域内观察 HTTP Chat Completions / Responses 正文，按调用 UUID 在内存累积；每新增至少 1,024 个 UTF-8 字节保存一次累计快照，调用退出时保存余量及终态。`parse_model_output_draft` 独立于父任务和正式解析结果，不包含指向父任务的外键；草稿读取以 tenant、Parse task 和 call ID 限定。非流式调用只在完整响应后保存，未暴露通用正文观察的 SDK/CLI 路径不提供中途草稿。

`streaming` / `completed` / `failed` / `cancelled` 描述模型调用草稿；`completed` 不等于 Parse 业务结果通过校验或已提交。reasoning、工具参数和 prompt 不写入正文草稿。写库异常保留原模型处理路径并记录告警，本调用暂停中途重试、退出时最多再尝试一次。新增表通过显式 `init-db` 注册，运行库建表与服务加载单独验收；本次不会自动恢复暂停任务。设计、验证与运行边界见 [Parse 草稿 TRD](../docs/design/03-product-reassessment/parse-output-drafts-trd.md)。

模型流中的 `check_control` 仅检查本地取消状态；租约仍由独立周期心跳续期，调用接纳、工具边界和正式提交继续保留持久检查。草稿保存不续租，也不作为任务恢复的已完成事实。

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
V1 用量不并入新统计；本轮退役迁移删除旧 Job 用量，现行 V2 用量和 `model_usage` 保留。
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

请求参数与跨层数据快照定义于 `server/model/dto/`，HTTP 响应 View 定义于 `server/model/vo/`，
`server/api/schema.py` 保留兼容聚合；API、CLI、Agent 共用的检索证据和
应用报告留在 `server/schema.py`。`server/api/app.py` 只创建应用、配置生命周期与中间件、
注册异常处理和 Router。

`python -m server.api` 收到停服信号后停止接受连接，最多等待 5 秒让现有 HTTP 请求收尾，
再取消仍打开的 SSE 等请求并进入应用生命周期关闭。Consumer、Agent 等业务服务先收尾，
随后释放数据库、ES 等 Caller 资源；这 5 秒不截断业务服务的关闭过程。

## 本机 MCP：供 Codex 等外部 Agent 调用

`SEEKINO_AUTH_MODE=local` 时，正常 API 进程同时提供 Streamable HTTP MCP：`http://127.0.0.1:9621/mcp`。若 API 端口不同，使用实际端口。它随 API 进程启动。Cloud 部署提供同路径的 HTTPS 服务。

同一编辑客户端通过这个地址取用表演和手法范例。可从 [#50](https://github.com/owariband/Seekino/pull/50) 检出整个功能，在本机用[整体验收命令](../docs/design/03-product-reassessment/mcp-edit-references-trd.md#6-结果验收与闭环)从两个普通上传开始，验证最终能取得两段原片。

| 工具 | 参数与结果 |
|---|---|
| `list_series` | `offset=0, limit=20`，分页列出本地系列与状态，`next_offset` 用于续页 |
| `retrieve` | `query` 必填，可选 `series_ids`、`top_k=5`、`event_types`、`speaker`；返回 Event 证据及 `evidence_ref`。使用相同条件和 `next_cursor` 继续读取排序结果 |
| `browse_events` | 指定 `series_id`，按集和时间翻完已发布事件。可用 `visible_names` 筛选出镜角色，自动展开明确的系列别名。单页 `limit` 为 1–50，传回 `next_cursor` 直到为空 |
| `read_evidence` | 接收正片事件或参考片的 `evidence_ref`，核对来源及发布版本。成对传入 `start_ms`、`end_ms` 可下载自选区间的 `media.clip_url` |
| `find_edit_templates` | 用 `query` 找已发布的手法观察。可传 `series_ids`、`top_k`。每项给出步骤、原片区间、证据引用及可直接播放的 `media.play_url` |
| `get_style_card` | 传 `video_id` 读取当前全片 Refine 卡。`anchored_signals` 的例子含 `segment_id`、`at_ms`、画面依据和 `evidence_ref`。早期卡标作 `legacy_technique_groups`，各代表段也能从 `exemplars` 打开 |

参考片通过上传或重解析的 `parseConfig.profile = "edit_reference"` 选择。普通剧集可逐集发布，`retrieve` 在部分集已就绪时返回已有事件及覆盖提示。参考片发布后，编辑通过手法命中或全片卡进入 `read_evidence`，默认可播放、下载该代表段，并查原生帧时间和分步观察。引证时刻属于原视频坐标，独立 MP4 的播放时钟从零开始。人物分离、手法取舍和出片由编辑在本机完成。[双召回主设计 #50](https://github.com/owariband/Seekino/pull/50)

使用剪辑素材策略解析后，可以搜索“镜头向右横移，灯塔从左边出画，小船在右边入画”。命中的 `SCENE.camera_ref` 给出镜头方向、速度类别、起幅、落幅和画面依据。对这条事件调用 `read_evidence`，适合二维复现的运镜会附带 `camera_replay`，其中有版本化 ES 模块、入口 `cameraAt(elapsedMs, params, world, viewport)` 和原片时长。返回值是新画面中的裁取矩形，可直接用于 Canvas 的 `drawImage`。

`params.progress_samples` 存在时，横移曲线使用带原片时间的背景位移采样。每对数字分别是镜头时长占比和行程占比，`travel_in_view_widths` 以一屏宽为单位。新画幅和起止锚点由剪辑端选择。未测得行程时，`travel_basis` 明示构图默认值。环绕等无法用此模板表达的动作仍能按描述检索。旧素材需按 `clip` 重新解析才能生成运镜记录。[设计与适用范围](../docs/design/03-product-reassessment/clip-camera-reference-trd.md)

`top_k` 是不同 Chunk 的数量（最多 20），不是 Event 数量；同一 Chunk 可以包含多条命中事件。默认搜索本地 READY 系列，超过 50 个时先用 `list_series` 选择范围。Event 索引未覆盖会返回 `EVENT_INDEX_NOT_READY`，不会伪装成“没有匹配”。

视频时间全部使用原片整数毫秒，区间左闭右开。`next_evidence_ref` 续读同一来源版本的上下文；`EVIDENCE_STALE` 表示需要重新检索。本机不传时间时仍返回 `original_url` 和可用的 `playback_url`。传入时间可以跨同一原片的事件或分块；下载时重验权限和出处，使用本机 FFmpeg 从原片生成 H.264 MP4，保留首条音轨（若有）。源文件缺失时不返回有效下载链接。

给编辑 Agent 的提示：**当手势或表情的变化承载剧情时，回看命中前后，从起势到收势选一段。把原片起止毫秒和这条 `evidence_ref` 交给 `read_evidence`，下载 `media.clip_url`。可以直接剪这段原片；需要换背景时，用你本地的工具抠出连续动作。需要强调某一拍时再定格。** 取片不需要人物名。

Cloud 模式的片段下载沿用媒体访问权限，每次最多 **60,000 ms（含）**；超限会报错，不静默截断。本地没有这一分钟限制。[取片合同](../docs/design/03-product-reassessment/mcp-source-clips-trd.md)与官方 Cloud MCP 的连接、鉴权交付分开；本分支不新增公网 MCP 入口。

在已恢复正常 API/数据库/ES/媒体的本机，可为一次 Codex 会话临时配置：

```bash
codex -c 'mcp_servers.seekino.url="http://127.0.0.1:9621/mcp"' \
  -c 'mcp_servers.seekino.required=true'
```

然后让 Codex“调用 Seekino 的 list_series；选择已可检索的系列，retrieve 一个实际内容，再 read_evidence 核对来源并取得媒体”。如需持久保存连接，使用 `codex mcp add seekino --url http://127.0.0.1:9621/mcp`；该命令会修改用户的 Codex MCP 配置，本轮实现没有代为执行。连接方式见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

MCP SDK 根据服务端函数签名、类型和说明生成工具 JSON Schema，通过 `tools/list` 提供给客户端；实际请求通过 `tools/call` 调用现有 Service。协议客户端测试不等于真实素材可用，正式演示还需要已解析并完成 Event 索引的视频和对应媒体。

本机端点校验回环来源和 Host/Origin，拒绝转发头。连接本机 API 时直接使用其监听端口。主合同与验收边界见 [MCP 接入 TRD](../docs/design/03-product-reassessment/mcp-access-trd.md)。

## 长任务查询与调度排序

任务列表先在授权范围内分页取 ID，再读取该页正文；活动 prefix 只投影对应状态。MySQL 的 JSON 筛选也可能使 filesort 携带大字段，因此 Queue 的候选扫描读取轻量标量后在 Python 保持原优先级和轮转，领取仍由原事务/租约校验决定。Consumer 外层失败会记录堆栈，不无限重试。设计、同任务恢复及验证边界见[长任务查询与恢复 TRD](../docs/design/03-product-reassessment/parse-task-query-recovery-trd.md)。

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

Understanding 子图各节点按冻结配置对暂态错误有限重试；最终 Parse 异常由 Consumer
记录 `PARSE_NODE_FAILED` 并通知任务订阅，不再重排整个 Chunk。该视频停止新派发，
兄弟任务失去续租资格，迟到提交被拒绝；任务 API/SSE 返回失败阶段与受控错误原因。
原生 ASR 已进入的阻塞推理需返回控制点后才响应取消。进程崩溃恢复及 Index/Refine 重试独立保留。

现行任务模型统一放在 `model/task.py`，任务进度计算位于 `model/task_progress.py`；Video／Chunk／Interval 成果统一放在 `model/parse_content.py`。旧 `legacy_parse.py` 已删除。DTO 按任务、成果、素材、检索分别归入 `taskDTO.py`、`parseContentDTO.py`、`libraryDTO.py`、`searchDTO.py`，任务 VO 位于 `vo/taskVO.py`；调用方直接引用具体模块。单纯文件归类保持原合同；随后退出 V1 兼容会移除旧 `job_id` 数据库列及 HTTP `jobId`／`latestJobId`，前后端须同步升级。现行 `videoParseTaskId` 与任务版本／租约合同保留。

2026-10-31 本机测试默认已切换到 whisper.cpp Turbo Q5_0＋Metal；新任务保持整片 ASR，WhisperX batch_size 不参与该路径。启动入口支持 `--require-metal`，具体配置、CPU 线程与时间来源边界见[本地模型配置](../docs/model-config.md#本机-apple-gpu-测试whispercpp-turbo2026-10-31)。这次纯转写测试不执行声学强制对齐。

完成成果由 `video_parse_content` 保存，每个解析任务至多一行；`asr`、可选 `diarization` 与 `summary` 为独立 JSON 列，任务 detail 保留调度与完成状态。准备入口通过 `service/video_parse_results.py` 校验媒体／冻结模型／Schema／摘要和租约，有效 ASR 在创建 Provider 前复用；完成提交后清理内存不影响持久结果。`repository/video_parse_content.py` 分别读取元数据或所需正文，普通任务查询与心跳不加载 ASR。Summary 成果、Video 当前展示字段和完成回执同事务提交。旧库须先执行[受控迁移](../docs/migrations/20261031_video_parse_content.md)，历史缺失 ASR 不自动回填。

启用冻结的 `models.diarization` 时，准备链先提交原始 ASR，再调用本地 pyannote 并独立提交
分组；失败只重做缺失阶段，门禁须等待两项成果。Engine 仅在内存副本上合并匿名编号，
以任务 ID 标识整片作用域，Chunk evidence 记录本次实际输入。新解析版本可在同一 Video
内查找身份、Schema 和摘要均匹配的既有成果，复制时仍使用当前合法租约并记录来源任务；
旧 Chunk 成果保留在旧版本，不能自动充当新输入的解析结果。

V2 Task SSE 忽略纯心跳变化，并在发送终态快照后关闭。快照中的
`canRetry` / `canReparse` / `canDelete` 只代表该次读取时的精确租约门禁；终态后租约变化
不会延长 SSE，客户端应重新 GET/list。Web TasksPage 每 5 秒刷新列表，UploadProvider
则订阅当前 V2 Task 的单任务 SSE。Agent SSE 仍返回完整 Message 快照，包含
500 条之后的消息以及同一条消息的状态变化。保活按单调时钟的经过时间计算，
不是固定轮询次数。完整快照仍有随聊天长度增长的读取和传输成本，后续如改增量协议，
须另行设计状态更新游标，不能只按 sequenceNo 跳过已有的 PROCESSING 消息。

Refine 执行在读取成员快照前注册进程内订阅。事务成功后仅通知正文确实变更且成员相交的执行；
通知只减少旧快照计算，不能作为完成事实或替代租约/版本校验。当前请求独立取消并等待旧流退出，
最近完整候选与具体冲突返回 Agent，再读取新版本继续；原尝试期限不重置。更新不会重跑音频或
Understanding。相关实现与未完成的真实并发验收见
[Refine 冲突恢复报告](../docs/report/server/refine-conflict-recovery-20261102.md)。

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
`IndexSyncIntent`，没有 `Episode` 表。旧 `SeriesParseJob/VideoChunkCheckpoint` 已退出。所有
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

Server 默认监听 `127.0.0.1:9621`，OpenAPI 位于 `/api/docs`。HTTP API 写入 MySQL 中的事实
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

已排队或已停止的非终态任务也可通过 `reparse` 采用修正后的配置，但父任务及所有
Interval/Chunk 执行租约必须先完成回收。新版本创建后，旧非终态版本撤销调度资格和容量，
保留原快照与历史成果。仅修改 YAML 或调用 `retry-parse` 不会改变已提交任务的 OCR 开关。
共享媒体尚未发布时，Chunk 同时检查父任务是否仍在原租约下准备，防止等待已经停止的生产者。

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
公开系列由管理者显式发布和撤回，原片和索引各自决定每集的播放与检索能力。普通聊天允许已授权的零索引课程。指定一门课时，`POST /api/search` 可用 `episodeOrder` 限定单集，Agent 的检索工具使用 `episode`。部分就绪的响应带实际 `searchedVideoIds`、`partial` 与集数提示。零就绪和服务离线分别返回 `SERIES_NOT_READY`（409）与 `RETRIEVAL_UNAVAILABLE`（503）。[设计和验收](../docs/design/03-product-reassessment/public-course-readiness-trd.md)

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
V2 的索引阶段属于 `chunk_parse_task`：相关 Interval Refine 成果提交后放行 Chunk 的 INDEX 阶段，
ES 写入与就绪检查成功后该 Chunk 才进入 `COMPLETE`；索引失败保留成果，恢复该 Chunk
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

### 解析暂停与继续

任务控制使用 `POST /api/jobs/video-parse/control`，参数为 `tenantId`、`videoParseTaskId`、`paused`、`expectedJobVersion`、`expectedLeaseVersion`。仅当前正式活动任务可以操作，冲突返回 409；读取投影提供 `isPaused/canPause/canResume/activeStage`。暂停保留版本与已提交结果，撤销在途租约并释放容量；原生模型调用可能在返回到取消检查点后才真正退出。详见[设计](../docs/design/03-product-reassessment/parse-pause-resume-trd.md)。

## Cloud MCP

在 Cloud 网页的 `/docs/agents` 可复制带本站地址的命令。也可以在剪辑终端把下方示例的域名换成部署地址，取得游客只读凭据。已有网页登录身份可用相同接口和当前 CSRF 凭据换取令牌。令牌随该会话过期，身份失效时撤销。

```sh
SEEKINO_CLOUD='https://your-seekino-cloud.example'
SEEKINO_MCP_TOKEN=$(curl -fsS -X POST "$SEEKINO_CLOUD/api/auth/mcp-token" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])') && export SEEKINO_MCP_TOKEN
```

在同一个终端为所用客户端添加服务，随后启动客户端。

```sh
claude mcp add --transport http seekino_cloud "$SEEKINO_CLOUD/mcp" --header 'Authorization: Bearer ${SEEKINO_MCP_TOKEN}'
```

```sh
codex mcp add seekino_cloud --url "$SEEKINO_CLOUD/mcp" --bearer-token-env-var SEEKINO_MCP_TOKEN
```

`list_series` 返回平台公开租户已发布且 READY 的系列。`browse_events` 返回 Event 引用，`retrieve` 用自然语言检索。先 `read_evidence(evidence_ref)` 核对来源，再给出 `start_ms` 与 `end_ms` 获取 `media.clip_url`。区间使用整数毫秒、左闭右开，每段最多 60 秒。链接最长有效 5 分钟，下载器可直接获取该 MP4，下载时服务端重新核对发布状态、证据版本和用户身份。检索与转码共用既有云端请求和并发额度。以不同名称添加本机端点即可在同一剪辑会话调用两个库。[服务端设计](../docs/design/03-product-reassessment/cloud-public-mcp-trd.md)


### 剪辑 Skill、风格参考与 series 共享模板

连接后可用 `resources/read` 读取 `skill://seekino-editing-materials/SKILL.md`。支持 MCP Skills 扩展的客户端可通过 `skills/list` 和 `skills/get` 发现指引与随附的本机分层脚本。也可以给本机 Claude Code 和 Codex 一次安装。

```sh
npx skills add https://github.com/owariband/Seekino/tree/main/server/skills/seekino-editing-materials -a claude-code -a codex -g -y
```

用 `find_edit_templates` 搜索剪辑技法和可见 series 的共享模板，`get_style_card` 读取整片风格并回看例证。用 `get_edit_template` 和 `list_edit_template_versions` 读取所属 series 的版本、来源和反馈。通过 `save_edit_template_version` 指定 `series_id` 保存完整代码与来源，`append_edit_template_note` 追加反馈或本机检查，`set_default_edit_template_version` 明确指定默认版本。

模板归属 series。任何有权查看该 series 的用户都可以读取模板、历史版本和反馈，作者字段用于署名与追溯。游客凭据换新后仍可读取同一公开 series 的模板。写入沿用 series 现有编辑权限。Cloud 编辑者可在账号菜单选择“复制剪辑写入凭据”，或用 `POST /api/auth/mcp-token?scope=personal`、当前会话 Cookie 和 CSRF 请求头领取写入凭据。本机沿用本地 series 权限。客户端首次调用时可先请它读取剪辑 Skill。

从旧版升级时，先停止 API 写入并备份数据库，运行 `uv run python -m scripts.migrate_edit_template_series` 预览，再用 `--apply --writers-stopped` 应用，随后按原方式执行 `init-db`。API 镜像也包含同一脚本，可在容器中用 `python -m scripts.migrate_edit_template_series` 执行预览和升级。迁移给现有模板表增加 `series_id` 和索引。历史未绑定模板会保留，管理者须经确认后将其 `series_id` 与 `tenant_id` 显式绑定到目标 series；绑定前不会出现在共享读取中。模板引用的原片不能作为自动公开旧代码的依据。


### 本机制作 Skill 与镜头运动参考

MCP 分发 `skill://motion-showreel/SKILL.md`、`skill://video-shotcraft/SKILL.md` 及各自的许可证和固定版本信息。在剪辑机上完整安装后，Agent 才能读取各自的参考资料、素材和制作脚本。

两份 MCP 正文由 Seekino 按固定上游版本改写为中文，保留原有操作规则。元数据分别记录上游原文和改写正文的 SHA-256，并标明修改范围；下面的安装命令取得对应版本的上游完整包。

motion-showreel 适用于代码驱动的图形、文字、界面动画及支持的人物分层合成，用确定性的 `render(t)`、共享时间线和逐帧渲染制作视频。它原生要求先在工作区预览、确认后导出，除非用户明确免除预览。常规原片选取与裁剪沿用 Seekino 剪辑指引。

```sh
npx skills add https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel -a claude-code -a codex -g -y
```

video-shotcraft 提供 Remotion 镜头配方、demo、模板和工作台，供 Agent 按任务需要参考。

```sh
npx skills add https://github.com/Vincentwei1021/video-shotcraft/tree/6700db2ba80572ebc777b99d72049679fed9840c -a claude-code -a codex -g -y
```

Skill 提供镜头运动的参考和建议，Agent 根据镜头意图、节奏与素材，在剪辑工程里自行编写速度、位移及形变代码。钟形速度与积分得到的 S 形位移可以作为平移或推拉的参考；拉伸、拖影等可按瞬时速度和方向联动。Agent 可以复用或调整上游辅助函数，并回看实际效果。原片观测与实测轨迹继续用于核对来源。

[video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) 的代码与文档使用 Apache-2.0，MCP 分发中保留许可证、来源与修改说明。喜欢这套镜头库，欢迎给上游项目 Star。完整包中的媒体和渲染依赖按上游各自的授权说明使用。

[motion-showreel](https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel) 来自 ConardLi/garden-skills，使用 MIT，分发中保留原版权与许可证。觉得有帮助，欢迎给上游项目 Star。
