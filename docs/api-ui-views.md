# UI 公开 DTO / VO

本次按 UI 规格补齐现有接口的输入与返回数据。Conversation 不绑定动画范围：每次 `messages/send` 提交 `seriesIds`，省略或空数组展开为当前 Workspace 全部 READY Series。后端保存本轮实际范围并注入 Retrieve Tool；后续多选改变不会修改历史消息，也不会改变正在执行的 Kick。

模型按职责分目录：`server/model/dto/` 存放请求参数和业务传输对象，`server/model/vo/` 存放前端公开视图，`server/model/` 下保留 ORM 与业务枚举。两类模型均按资源分文件，如 `agentDTO.py` / `agentVO.py`、`libraryDTO.py` / `libraryVO.py`、`uploadDTO.py` / `uploadVO.py`。Repository、Service 直接引用 DTO，不再各自定义跨层数据类。请求沿用原类名、字段、默认值、校验与 camelCase JSON 别名；内部状态类继续使用 dataclass 和 snake_case 字段，不直接作为 HTTP 返回结构。持久化和跨层传递的绝对时间统一为 Unix epoch 毫秒整数，转换与校验收口在 `server/utils/time.py`。

解析导入、索引同步与核对的结果报告也分别放在 `parseDTO.py` / `indexSyncDTO.py`；它们是业务传输对象，沿用原 Pydantic 字段校验。`server/schema.py` 保留通用契约基类和 Harness / 检索共享证据契约。

VO 按 `commonVO.py`、`libraryVO.py`、`uploadVO.py`、`jobVO.py`、`agentVO.py`、`searchVO.py`、`systemVO.py` 分组；投影函数留在对应 API 模块。旧 `server/api/schema.py` 作为兼容导入入口，分别引用 DTO 请求与 VO 返回模型。Harness 与原始检索契约在 `server/schema.py` / `server/agent/`，不依赖 HTTP VO。公开投影在 API 边界明确转换，内部媒体 URI、执行租约和模型审核意见不出现在公开 Evidence / Message 中。

| 类型分组 | 公开模型 | UI 使用的数据 |
| --- | --- | --- |
| common | Page、Failure、FieldError、ApiError、ImageAsset | 全范围 total、下一页游标、稳定错误 code、请求编号、公开图片 URL |
| library | Character、SeriesSummary、SeriesDetail、VideoSummary、LastSearchView | 视频 ready / failed 数量、待完成上传、最近检索、ASR 原文语言提示、视频索引与播放状态、实际 latestJobId |
| upload | UploadSessionCreateRequest、UploadSessionView、UploadSegmentView、UploadCompleteView | 完整上传确认点、缺失分段、真实合并阶段、源 SHA、后续 Job、资源 version |
| job | ParseJobView、VideoChunkCheckpointView、JobCounts、JobSummary | 实际阶段 / 进度 / 检查点、结构化错误、单调 version、全范围权威统计 |
| parseUsage | ParseUsageVO、ParseUsageRowVO | 真实视频与动画身份、按模型和职责汇总的 Token／调用数、人民币预估金额、消耗记录完整性 |
| agent | Conversation / Message 请求与 View、EvidenceReference、各类回答事件 | 历史 preview、毫秒时间、消息 phase / version、候选引用、Evaluator 审核后的精确视频范围、恢复同一轮消息对 |
| search | VideoChunkEvidenceView、VideoChunkCandidateView、SearchHitView、SearchResultView、Keyframe | 正文与证据、视频定位、完整检索范围、最终 rank、partial / warnings、可读取媒体 |
| system | HealthView | API 存活与真实 serverTime |

所有业务请求显式传 `workspaceId`：有 JSON Body 时放 Body，否则放 Query。所有公开和持久化的绝对时间用 Unix epoch 毫秒整数，数据库使用 signed `BIGINT`，Python / JSON 使用整数，未知时间为 null。视频 `startMs` / `endMs` / `atMs` 是相对于原视频起点的媒体偏移，`durationMs` 类字段是时长；二者都是相对毫秒，不当作日期或 Unix 时间戳。所有列表使用 `Page<T>`，参数默认 limit=50，范围 1–100；cursor 绑定 Workspace、筛选与固定排序。

```ts
interface Page<T> {
  items: T[];
  total: number;
  hasMore: boolean;
  nextCursor: string | null;
}
interface Failure { code: string; message: string }
interface ApiError extends Failure {
  requestId: string;
  fieldErrors: Array<{field: string; message: string}>;
  retryAt: number | null;
}
```

Series list 返回轻量 `SeriesSummary`，get / create 返回 `SeriesDetail`；列表不会假装包含完整剧集信息。Job list 的 total 与 jobs/summary 均从完整 SQL 范围统计，不用首屏数量冒充全局统计。索引状态由 Chunk / Embedding / Sync Intent 事实推导，不将解析完成视为索引 READY。

`SeriesDetail.transcriptLanguageHint` 是用户配置的 ASR 原文语言提示，例如日语 `ja`。它进入 Query Agent 的 `series_context`，用于判断 Transcript-heavy Query 应采用哪种语言和脚本；它不是界面 locale，不证明具体台词存在，更新后也不会重新解析视频。

ParseJob 的 overallProgress 以语义解析为主：canonical VideoChunk 全部完成媒体准备后到 1%，随后按已 PARSED 的真实视频时长从 1% 推进到 99%；文本/视觉向量化、持久化和索引同步不再额外瓜分权重，所有片段完成语义解析后保持 99%，只有任务成功并可检索时才到 100%。completedDurationMs、completedThroughMs、completedChunkCount 和 currentChunk 始终描述 PARSED 覆盖：前者是全部已解析片段的时长和，连续水位只统计从零开始无缺口的已解析范围，两者不能混用。stageProgress 仅保留为内部阶段诊断字段，任务页不再展示第二个百分比。范围缺失或不连续的历史任务返回 null，不回退到旧的估算百分比。

ParseJob 另返回 `videoDurationMs`（原视频时长）和兼容别名 `totalDurationMs`；`processingDurationMs` 是 worker attempt 的已知单调计时总和，不是视频时长。运行中的 Attempt 每当一个 VideoChunk 到达 PARSED checkpoint，就在现有 checkpoint 事务内更新一次任务级累计耗时；`acknowledge` / `retry` 再结算该 Attempt 剩余时间，因此前端会随解析片段推进显示“...（进行中）”，而不是等任务结束后才出现数值。运行期间 `processingTimingComplete=false`，此时累计值是截至最新 PARSED checkpoint 的当前下界；任务结束后若该字段仍为 `false`，则表示 lease 过期或历史缺数，前端显示“至少 ...（不完整）”而不伪装精确。该补充没有新增表、字段或事务，也不为 SPLIT / ASR / PARSE / TEXT_EMBEDDING / VISION_EMBEDDING / INDEX 持久化阶段明细。

新增和补充的接口：

| 路由 | 参数 | 返回 |
| --- | --- | --- |
| GET /api/uploads/list | workspaceId；可选 seriesId、status、limit、cursor；默认非终态，status=all 或逗号分隔状态 | Page&lt;UploadSessionView&gt; |
| POST /api/uploads/delete | workspaceId、uploadSessionId；删除整个 UploadSessionTask，包括上传分片及已产生的 Video、全部 ParseJob/Checkpoint/VideoChunk、模型用量、媒体文件和向量点；COMPLETING / RUNNING 返回 409 | UploadSessionView（EXPIRED 墓碑） |
| POST /api/uploads/retry-parse | workspaceId、uploadSessionId、expectedJobVersion；仅重排 UploadSession.job_id 指向的 FAILED 主任务，保留 Checkpoint / attempt / 用量，版本冲突或删除中返回 409 | ParseJobView |
| POST /api/uploads/reparse | workspaceId、uploadSessionId、expectedJobVersion；对 SUCCEEDED / FAILED 主任务清空 Checkpoint 后整集从头解析，保留 attempt / 用量历史及已成功的可检索快照，版本冲突或活动任务返回 409 | ParseJobView |
| GET /api/jobs/summary | workspaceId；可选 seriesId | JobSummary |
| GET /api/jobs/usage/get | workspaceId、jobId | ParseUsageVO：指定任务全部执行尝试，按模型与职责展示真实 Token、人民币预估费用和记录完整性；详见 [Parse 模型消耗统计](parse-model-usage.md) |
| POST /api/agent/conversations/delete | workspaceId、conversationId；存在 PROCESSING 回答时返回 409 | 删除前的 AgentConversationView；终态对话及 Message / Timing 级联删除 |
| GET /api/agent/messages/get | workspaceId、conversationId；messageId 与 idempotencyKey 恰好一个 | 同一 USER / ASSISTANT 消息对 |
| GET /api/video-chunks/get | workspaceId、videoChunkId | 可刷新媒体状态的事实 Evidence |
| GET /api/media/images/content | workspaceId、imageId | 经过资源归属与受管路径验证的真实图片 |

已有 series / jobs / conversations / messages 列表同步改为 Page。series/list 支持 status、query；jobs/list 支持 seriesId、status（逗号分隔）；conversations/list 支持 query、历史消息实际 seriesId；messages/list 保留 afterSequenceNo，并增加 cursor / limit。

Parse 用量由前端 `getParseUsage(jobId)` 读取，Workspace 与 Job ID 放 Query，返回模型对应 `server/model/vo/parseUsageVO.py` 和 `web/src/api/contracts.ts`。任务页每张卡片包含 `ParseUsageDetails`：展开后按模型、parse/embedding 分类及 operation 展示用量，金额 `estimatedCostCny` 是十进制字符串，以 `¥` 显示并保留小额精度；Token 和金额的 null 分别显示“未记录”和“未估算”，不能转成零。coverage=unknown 明确提示无法还原历史费用；partial 展示已采集明细并说明缺口；只有已完整跟踪且零次请求才能显示零费用。统计单独调用只读接口，运行中每 5 秒刷新，终态 partial 每 15 秒刷新，收起后停止。

创建与发送示例：

```http
POST /api/agent/conversations/create
Idempotency-Key: <创建键>
Content-Type: application/json

{"workspaceId":"local-default"}
```

省略标题时，后端根据首条问题生成短标题。自定义标题保留；默认创建请求的幂等重放不会因自动标题变化而失效。

```http
POST /api/agent/messages/send?conversationId=<conversationId>
Idempotency-Key: <本轮发送键>
Accept: text/event-stream
Content-Type: application/json

{
  "workspaceId":"local-default",
  "content":"这段里发生了什么？",
  "seriesIds":["<seriesId>"],
  "topK":5,
  "reference":{"messageId":"<来源assistantId>","videoChunkId":"<真实候选Id>"}
}
```

reference 可省略 / 解除；后端检查同 Conversation、来源 ASSISTANT、本轮有效范围及原回答的实际候选，自己读取结构化 Evidence，不接受前端提交的整份证据。`messageId + videoChunkId` 会在服务端恢复该来源回答已审核的精确播放范围，无需让前端回传时间；旧消息缺失或非法范围不再扩大为 canonical Chunk，引用校验失败。完整聊天历史同时保留原回答有序候选，供多轮指代使用。

SSE 正常顺序为 started → 真实阶段 progress → 审核通过的完整 evidence → Chat 真实 Provider delta → 数据库提交后的 completed。每条回答事件有 type、conversationId、messageId、sequence、occurredAt；evidence.searchResult 不再仅有 hits，还包含可审计的 retrievalTrace（每次 Query、受限 topK、requested/effective Transcript/Semantic/Visual 权重、策略版本和返回 Chunk ID）。候选同时暴露 transcriptRank/transcriptScore、textRank/textScore、visionRank/visionScore；Transcript 命中说明使用真正匹配的 ASR cue 与媒体时间偏移。同一 Kick 的后续检索若再次命中已经交给模型的 Chunk，只回传轻量 `reusedHits`；完整 ASR 与 canonical 时间线引用首次结果，Server 仍保存每次检索的完整事实。事实性结论必须包含非空 `selected_video_ranges`；Query Agent 根据 ASR 和事件时间线为每个真实 Chunk 提出候选范围，Evaluator 审核越界、截断或前后冗余，不合理时通过 advice 要求 Query 修订。Evaluator 放行时，`selected_video_ranges` 必须与获批 Evidence 的 ID 集合完全一致，且每个范围都被其 canonical Evidence 包含；非法 complete 转为 continue，不产生降级范围。Agent 工具的 Schema 和构造统一收口在 `server/agent/tools.py`。Chat 的 `submit_video_item` 只接受 `video_chunk_id + claim`；存在 approved evidence 时首次使用 `tool_choice=required`，成功提交至少一个后恢复 `auto`。Output 按 ID 从获批 `selected_video_ranges` 原样绑定 `startMs/endMs`，不存在 Chat proposed range、approved range、canonical range 三层 fallback。Query、Evaluator、Chat 默认使用 `config.yaml` 的 `agent_model.model`（DeepSeek Flash）；Provider 请求或模型响应异常时，失败的单次模型调用在原消息 / Tool 状态上切换到 Ark Doubao Lite。已经完成的业务 Tool 及 acknowledgement 会保留，不重新执行；业务 Tool 错误、取消 / deadline / 预算等控制错误以及本地输入校验错误均不触发模型降级。旧消息的视频块缺少精确范围或范围非法时不渲染，不扩大为 canonical Chunk；Chunk ID 无法与同消息可信 hit 关联时同样不渲染。消息快照有 phase 与单调 version，前端拒绝旧版本覆盖新状态。phase 仅有 QUEUED / RETRIEVING / EVALUATING / RESPONDING，终态为 null，不包含私有推理。

持久化 FAILED 时，answer.failed 返回公开 error 与真实消息对，并保留已经确认的证据 / 部分正文。队列溢出或无法确认终态提交时返回 answer.interrupted，不谎称后台 FAILED。恢复时读取 messages/get / list / events。断开流不取消后台任务。不同 Conversation 的流、上下文和候选引用隔离。

SearchResult 同时包含 query、seriesIds、searchedVideoIds、agentReply、hits、partial、warnings。searchedVideoIds 来自实际索引范围，Harness 汇总各轮真实检索的视频范围，不能从命中候选反推。旧记录没有该事实时返回 [] 并说明缺少原始范围记录。candidate.rank 是最终候选位置+1；fusedRank 保留原检索排序，不把融合分数显示成置信度。

videos/content 新增 variant=playback|original，默认 playback；原文件可读取不等于兼容浏览器。播放器使用后端提供的 playbackUrl 加载原视频，但交互时间轴以 render block 的精确范围为零点和总长度；拖动位置映射回原视频媒体坐标，并在 endMs 停止。媒体丢失返回 MEDIA_MISSING / 410，未准备好或不兼容返回 MEDIA_NOT_READY / 409。Range、If-Range 与 ETag 等浏览器读取头通过 CORS 放行并暴露。

当前与完整 UI 目标的差异：

- 保留 JSON send 同步返回 200，没有改成首次受理 202；SSE 是真实增量输出。
- Series 创建仍按 Workspace + name 的业务身份幂等，upload complete 仍按 uploadSessionId 幂等，未增加覆盖规格所有写入路由的通用幂等记录。
- 没有新增取消、settings 写入或 system/status；jobs/events 仍是指定 jobId 的流。只有仍处于 COMPLETED 的 UploadSession 所关联的 FAILED 主 Job 返回 canRetry=true；canCancel=false，retryOfJobId=null。
- contentSha256 请求暂可省略；提供时固定并在合并后真实校验全文件 SHA。历史 / 未计算值为 null，完成后保存真实值。前端没有新增全文件 Worker hash。
- completionStage 来自真实合并步骤；无法可靠度量的 completionProgress / estimatedRemainingMs 为 null。Upload / Message / Job 的 version 由实际可见变化递增，Job 心跳不伪造新版本。
- 当前没有新增视频转码，不将 MOV / AVI 或无法确认 codec 的原文件标为可播放。解析没有保存关键帧的原视频 atMs，Evidence.keyframes=[] / thumbnail=null，并给出明确限制；系列封面仅使用可读取的已登记真实图片。
- 没有开放 PARTIAL Series 检索，仍遵守当前 READY 范围规则。离线导入没有 ParseJob 时 latestJobId=null，不填假的 Job。
- 真实模型、开发 MySQL / Qdrant、浏览器视频回放仍需单独联调；本地 Mock / SQLite 回归不代替该验收。

旧数据库需要受控迁移，不能把 init-db 当作 ALTER TABLE。新增列：

| 表 | 新增列 |
| --- | --- |
| series | transcript_language_hint VARCHAR(64) NULL |
| agent_conversation | title_is_auto BOOLEAN NOT NULL DEFAULT FALSE |
| agent_message | phase VARCHAR(16) NULL、version INT NOT NULL DEFAULT 1、reference JSON NULL、last_error_code VARCHAR(64) NULL、last_error_message LONGTEXT NULL |
| series_parse_job | version INT NOT NULL DEFAULT 1 |
| video_chunk_checkpoint | chunk_start_ms BIGINT NULL、chunk_end_ms BIGINT NULL |
| upload_session | content_sha256 VARCHAR(64) NULL、completion_stage VARCHAR(32) NULL、version INT NOT NULL DEFAULT 1、job_id VARCHAR(36) NULL、last_error_code VARCHAR(64) NULL、request_fingerprint VARCHAR(64) NULL |

所有旧绝对时间列需受控迁移为 Unix epoch 毫秒 `BIGINT`，见
`docs/migrations/20260918_unix_time_ms.sql`。解析任务的累计 worker 耗时和
计时完整性保存在 `series_parse_job.processing_duration_ms` 与
`series_parse_job.processing_timing_complete`，见
`docs/migrations/20260919_parse_job_processing_timing.sql`。

为 Message / Job / Upload 添加 version>0 检查；Message phase 与 Upload completionStage 添加合法值检查。upload_session.job_id 关联实际 series_parse_job.job_id。旧上传完成记录应从 Video / 对应 pipeline Job 回填实际 SHA / Job；无法确认的值保留 null，禁止猜测关联。旧错误只能回填公开通用 Failure，不直接复制含内部诊断的信息到 last_error_message。已有 PROCESSING phase 不能凭空猜测，保持 null，直到真实运行阶段写入。init-db 会检查这些列并拒绝缺列结构；本次没有执行真实数据库 DDL。

request_fingerprint 只保存原始规范化创建请求的摘要，不包含服务端分段配置、后续分配的顺序或计算出的实际 SHA；因此同键不同可选参数会冲突。历史上传无法从结果反推原始可选参数意图时保留 null，继续按旧字段校验；不填伪造指纹。新增上传均固定真实请求指纹。

video_chunk_checkpoint 的 chunk_start_ms / chunk_end_ms 保存 canonical VideoChunk 相对于原视频起点的媒体范围，用于按真实视频时长计算解析进度。AnalysisWindow 允许重叠，不能用于累计完成时长。两列必须同时为空或同时有值；已有值不可改写。已持久化的历史任务从 video_chunk.start_ms / end_ms 确定性回填，无法确认的旧任务保持 null，并在下次从检查点恢复时补齐。对应受控 SQL 见 `docs/migrations/20260916_video_chunk_checkpoint_ranges.sql`。

Series 的 `transcript_language_hint` 由用户确认并用于 Query Agent 的 Transcript 检索改写；已有行保持 null，不从作品名或文件名猜测。对应受控 SQL 见 `docs/migrations/20260917_series_transcript_language_hint.sql`。
