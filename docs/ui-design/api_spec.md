# Seekino UI API Specification

> 文件：api_spec.md  
> 对应 UI：A「青瓷札记」及正式 Web 的资料库、传输案台、解析任务、Agent 工作台、证据窗口和设置  
> 版本：UI Target Contract v2 · 待 Review  
> 更新日期：2026-09-15  
> 状态：**目标接口契约，不是现有后端接口清单，也不代表接口已经实现。**

## 1. 规格边界与验收原则

本文件从 UI 的完整任务出发，规定前端期望的路由、请求、响应、字段语义、状态转换、实时事件和恢复行为。不得因为当前后端缺少某能力而把对应 UI 要求删掉或降级为静态展示。

设计依据：

- [已确认的 A 版方向](direction-approved.md)。
- [UI 功能与交互规范](design-spec.md)。
- 正式 Web 中的系列书架、逐文件并行上传、任务阶段与检查点、连续聊天、候选切换、选择理由、原始证据、前后语境、播放器、日夜主题。
- 已确认的要求：2–3 个视频真实并行；上传与建库进度分开；Agent 按当前问题逐候选解释；支持基于已选片段追问。

下文路由和 TypeScript schema 是目标契约。响应字段除标记 `?` 的字段外均为 **MUST**；`null` 表示字段必须存在但值未知／不可用，`?` 表示可省略。请求中标记 `?` 的字段可以省略。

- **MUST**：完整 UI 验收的必要条件。
- **MUST NOT**：禁止造成错误展示、状态伪装或数据边界破坏。
- 后端实现后的 OpenAPI MUST 与本文件一致，不能用当前 OpenAPI 覆盖 UI 需求。
- 文档内示例仅用于解释契约，MUST NOT 注入运行中的页面作为默认数据。
- 这里只规定公开行为，不指定数据库表、队列、模型框架或内部文件布局。
- 单用户本地产品，不引入登录、会员、计费、多租户权限后台、永久公网分享等没有 UI 要求的能力。

资源关系固定为：

~~~text
Workspace → Series → Video → VideoChunk
Workspace → UploadSession → Video + ParseJob
Workspace → Conversation → Message
~~~

**不设计 Agent Session 层。** Conversation 是完整多轮聊天，不是一次提问；USER 与 ASSISTANT 分别是一条 Message。

### 1.1 本次实现范围（2026-09-15）

用户确认以逐消息范围为准，已修正第 7 / 8 节旧的 Conversation 固定范围约定。本次补齐现有业务的请求参数与公开 VO，并接入前端；第 3 节 30 条路由仍是完整目标清单，不能据此宣称全部已实现。

请求参数与业务传输对象位于 `server/model/dto/`，前端公开返回视图位于 `server/model/vo/`，按资源使用 `xxxDTO.py` / `xxxVO.py` 分组。返回模型包含 Common、Library、Upload、Job、Agent、Search、Health，提供统一分页、毫秒时间、结构化错误、消息版本 / 阶段 / 引用、系列与视频统计 / 媒体、上传合并阶段 / 后续 Job、完整 SearchResult。新增只读 uploads/list、jobs/summary、agent/messages/get、video-chunks/get、media/images/content，供队列、问答和媒体恢复。

保留与完整目标的差异：

- 未实现本规格的取消、手动重跑、settings 修改、system/status，以及 Workspace 级 jobs SSE；当前 jobs/events 仍要求 jobId。
- JSON send 仍同步等待后返回 200；SSE 继续真实 Provider delta。此处没有把 JSON 改成目标的首次 202 后台接管行为。
- contentSha256 请求暂可省略；提供时固定并执行全文件合并校验。历史或尚未计算的值为 null，完成后返回真实 SHA。前端仍使用分段校验，尚未强制全文件 Worker hash。
- 不改变现有 READY Series 检索规则，不开放 PARTIAL Series 范围；ready / failed / pending 统计来自真实状态。
- 当前解析未持久化关键帧原视频时间位置，所以 Evidence.thumbnail=null、keyframes=[] 并提供 limitations/warnings；不猜 atMs。不兼容的原视频不假称 READY，没有新增转码业务。
- estimatedRemainingMs / completionProgress 在无法可靠度量时为 null，canRetry / canCancel=false；离线导入没有 Job 时 latestJobId=null。
- 旧数据库必须按新增 VO 状态字段执行受控迁移；init-db 会拒绝缺列结构，不自动修改真实数据库。

实际接口与迁移说明见 [公开 UI VO 契约](../api-ui-views.md)。下面其他能力要求仍保留为后续完整 UI 验收目标。

## 2. 通用 HTTP、时间与数据约定

### 2.1 路由与 Workspace

- 开发 Base URL：`http://127.0.0.1:9621`。前端可以通过同源代理或 `VITE_API_BASE_URL` 访问。
- 路由沿用 `create/list/get/update/retry/cancel` 等动作命名；资源 ID 使用 camelCase Query，不使用路径参数。
- 所有业务请求 MUST 显式携带 `workspaceId`；无 JSON Body 的请求放 Query，有 JSON Body 的请求放 Body。
- 初始 Workspace 为 `local-default`，可由部署配置替换；不得在业务逻辑中写死素材、系列或视频 ID。
- 所有读取、写入、图片、视频、候选引用和 SSE MUST 校验 Workspace 归属。不存在和归属不符统一 `404 NOT_FOUND`。
- ID 是不透明字符串，前端不得推断 ID 中的名称、时间或排序含义。
- JSON 使用 camelCase；业务 JSON 请求／响应使用 `application/json`。

### 2.2 时间统一为 Unix 毫秒时间戳

**所有业务绝对时间均为 JSON integer，单位为毫秒，自 1970-01-01 UTC 起算。** 不使用 Unix 秒，不使用字符串数字，不返回日期字符串，不混用单位。

~~~ts
type UnixTimeMs = number;    // JSON integer，UTC epoch milliseconds，>= 0
type MediaOffsetMs = number; // JSON integer，从原视频开头起算，>= 0
type DurationMs = number;    // JSON integer，时长／剩余时长，>= 0
~~~

| 分类 | 字段示例 | 类型与单位 |
| --- | --- | --- |
| 创建、更新、完成、检查时间 | createdAt、updatedAt、completedAt、checkedAt | UnixTimeMs |
| 到期、可运行、下次重试时间 | expiresAt、availableAt、retryAt | UnixTimeMs 或 null |
| 最近搜索／消息时间 | searchedAt、lastMessageAt、lastSearch.searchedAt | UnixTimeMs 或 null |
| SSE 事件发生时间 | occurredAt | UnixTimeMs |
| 视频／对白／事件／关键帧位置 | startMs、endMs、atMs、analysisWindowStartMs | MediaOffsetMs，**不是 Unix 时间** |
| 视频时长、ETA | durationMs、estimatedRemainingMs | DurationMs 或 null |

例如 `createdAt: 1789437600000` 是绝对时间；`startMs: 1252000` 表示视频第 20 分 52 秒。视频偏移绝不能作为日期传入 `new Date()`。

前端用 `new Date(createdAt)` 按用户本地时区展示；不额外乘 1000，不手工补时区。所有整数 MUST 在 JavaScript 安全整数范围内。未知时间使用 `null`，不使用 `0` 或空字符串冒充未知值。

HTTP 协议本身的标准日期头不属于业务 schema；JSON 和 SSE data 中的业务时间统一遵守以上规则。

### 2.3 分页与精确统计

所有列表返回统一的 `Page<T>`，不是裸数组。`limit` 默认 50，范围 1–100；`cursor` 可省略。

~~~ts
interface Page<T> {
  items: T[];
  total: number;             // integer >= 0，当前筛选范围全部记录数，不是本页数量
  hasMore: boolean;
  nextCursor: string | null; // opaque cursor
}
~~~

- 空列表：`items: []`、`total: 0`、`hasMore: false`、`nextCursor: null`。
- `hasMore` 为 true 时 `nextCursor` MUST 非空；最后一页 MUST 为 null。
- cursor MUST 绑定 Workspace、筛选条件和排序；不能拿另一组筛选条件的 cursor 继续翻页。
- 列表必须有稳定 ID 作为排序的次级键；前端按 ID 去重，实时变化后重新同步首屏。
- 任务全局统计由 summary 接口提供，不能通过最近 50／100 条记录计算“全部任务”。
- 所有数组字段无数据时返回 `[]`，不返回 null。

### 2.4 幂等与并发

下文所有业务 POST 写入接口 MUST 携带 `Idempotency-Key` 请求头，长度 1–128；前端首次提交前生成并保存，网络重试复用原键与原始参数。

- 幂等键作用域为 workspaceId + HTTP 方法 + 路由；Query 中的目标资源 ID 和 Body 中的有效业务参数均纳入请求指纹。Accept 只决定 JSON／SSE 返回方式，不改变业务动作。
- 同一 Workspace、动作、目标资源、幂等键及相同有效请求：返回同一业务结果，不产生重复系列、上传、任务、消息或取消操作。
- 同一作用域内，同一幂等键对应不同有效参数：`409 IDEMPOTENCY_CONFLICT`。
- 幂等结果必须覆盖资源生命周期；上传键至少保存至会话到期后 24 小时，消息键在对话保留期间可查。
- 异步接口在任务受服务端接管且初始状态持久化后才返回 202 或发送 started 事件。
- 请求断开、切换页面和取消 SSE 订阅 **不等于取消业务任务**。
- 一个文件或 Conversation 的失败不得阻塞其他文件／Conversation。
- 有 `version` 的资源，其版本为单资源单调递增整数；更新时间碰巧相同也不得产生版本歧义。前端拒绝较低版本快照。

### 2.5 错误 schema

所有非成功业务 JSON 响应，包括参数校验失败，均使用统一结构：

~~~ts
interface Failure {
  code: string;    // 稳定程序分支，必须支持下方约定的 code
  message: string; // 面向用户的可读错误，不含密钥、内部连接串或私有思考过程
}
interface ApiError extends Failure {
  requestId: string;
  fieldErrors: Array<{ field: string; message: string }>;
  retryAt: UnixTimeMs | null;
}
~~~

~~~json
{
  "code": "VALIDATION_ERROR",
  "message": "请检查上传文件信息",
  "requestId": "request-example-01",
  "fieldErrors": [
    { "field": "episodeOrder", "message": "集数必须是大于 0 的整数" }
  ],
  "retryAt": null
}
~~~

| HTTP | 必须支持的 code | UI 行为 |
| --- | --- | --- |
| 400 | INVALID_REQUEST、REFERENCE_INVALID | 修正输入／解除无效候选引用 |
| 403 | PERMISSION_DENIED | 展示本地文件／目录权限不足，不伪装为网络离线 |
| 404 | NOT_FOUND | 资源失效；重新同步列表 |
| 409 | CONFLICT、IDEMPOTENCY_CONFLICT、VERSION_CONFLICT | 不自动另建资源；同步当前状态 |
| 409 | DUPLICATE_EPISODE、DUPLICATE_CONTENT | 标出具体文件／集数冲突，保留其他文件上传 |
| 409 | UPLOAD_EXPIRED、UPLOAD_CANCELLED、UPLOAD_IN_PROGRESS、MISSING_SEGMENTS | 按上传快照恢复；失效会话使用新键重新加入 |
| 409 | SERIES_NOT_READY、INDEX_NOT_READY | 提示未就绪，不静默改动检索范围 |
| 409 | MESSAGE_IN_PROGRESS、JOB_IN_PROGRESS | 恢复已有任务，不重复提交 |
| 409 | MEDIA_NOT_READY | 媒体准备中，刷新对应媒体／任务状态 |
| 410 | MEDIA_MISSING | 显示“原媒体已丢失”，其他证据仍可阅读 |
| 422 | VALIDATION_ERROR、CHECKSUM_MISMATCH | 指明字段或分段错误，不自动重试错误内容 |
| 429 | RATE_LIMITED | 按 retryAt 等待，不并发重放 |
| 503 | SERVICE_UNAVAILABLE | 展示依赖故障并允许安全恢复 |
| 500 | INTERNAL_ERROR、PROCESSING_FAILED | 保留输入与业务 ID，查询是否已受理 |

`fieldErrors` 无字段级问题时为 []；`retryAt` 无可靠重试时间时为 null。异步 DTO 中的 `lastError` 使用 `Failure | null`；错误不再仅是无 code 的字符串。

CORS MUST 允许所需 GET／POST／PUT／OPTIONS 和 Accept、Content-Type、Idempotency-Key、Range、If-Range、If-None-Match、Last-Event-ID、Content-Range、X-Content-SHA256；暴露 Content-Range、Content-Length、Accept-Ranges、ETag、X-Request-Id。SSE 与媒体读取必须能从正式 Web 的 origin 使用。

## 3. UI → 目标路由总表

以下 **30 条路由** 是完整 UI 目标契约，不表示已实现。通用 Workspace、幂等键、分页约定不在每行重复。

| UI / 动作 | 方法 | 路由 | 成功响应 |
| --- | --- | --- | --- |
| API 进程连接 | GET | /healthz | 200 HealthView |
| 服务、模型、Worker、索引与上传能力 | GET | /api/system/status | 200 SystemStatusView |
| 设置、存储位置与检索偏好 | GET | /api/settings/get | 200 SettingsView |
| 修改可写设置 | POST | /api/settings/update | 200 SettingsView |
| 新建系列 | POST | /api/series/create | 201 SeriesDetail |
| 系列书架 | GET | /api/series/list | 200 `Page<SeriesSummary>` |
| 系列、剧集与待上传资料 | GET | /api/series/get | 200 SeriesDetail |
| 为单个文件建立上传 | POST | /api/uploads/create | 201 UploadSessionView |
| 恢复服务端上传队列 | GET | /api/uploads/list | 200 `Page<UploadSessionView>` |
| 查询上传确认点与后续 Job | GET | /api/uploads/get | 200 UploadSessionView |
| 上传／重传分段 | PUT | /api/uploads/segments/upload | 200 UploadSegmentReceipt |
| 提交校验合并与建库 | POST | /api/uploads/complete | 202 / 200 UploadCompleteResult |
| 真正取消未完成上传 | POST | /api/uploads/cancel | 202 / 200 UploadSessionView |
| 任务列表与筛选 | GET | /api/jobs/list | 200 `Page<ParseJobView>` |
| 单任务详情与恢复 | GET | /api/jobs/get | 200 ParseJobView |
| 全范围任务统计 | GET | /api/jobs/summary | 200 JobSummary |
| 全局／单任务实时状态 | GET | /api/jobs/events | SSE |
| 手动重跑失败／取消任务 | POST | /api/jobs/retry | 201 ParseJobView |
| 真正取消建库任务 | POST | /api/jobs/cancel | 202 / 200 ParseJobView |
| 新建完整聊天 | POST | /api/agent/conversations/create | 201 ConversationView |
| 历史对话与系列筛选 | GET | /api/agent/conversations/list | 200 `Page<ConversationView>` |
| 恢复单个对话 | GET | /api/agent/conversations/get | 200 ConversationView |
| 消息历史 | GET | /api/agent/messages/list | 200 `Page<MessageView>` |
| 查询一轮问答／幂等恢复 | GET | /api/agent/messages/get | 200 MessageSendResult |
| 发消息与实时回答 | POST | /api/agent/messages/send | 202 / 200 JSON，或 200 SSE |
| 对话持久化消息快照 | GET | /api/agent/messages/events | SSE |
| 停止生成回答 | POST | /api/agent/messages/cancel | 202 / 200 MessageSendResult |
| 原视频／浏览器可播放媒体 | GET | /api/videos/content | 200 / 206 binary |
| 封面／关键帧图片 | GET | /api/media/images/content | 200 image |
| 刷新片段证据、媒体 URL 与时间书签 | GET | /api/video-chunks/get | 200 VideoChunkEvidence |

`POST /api/search` 可以保留为兼容性的单次检索入口，但**不属于以上 30 条 UI 必需路由，不能用它替代连续聊天**。

纯本地行为不新增 API：主题切换、导航、弹窗开关、待提交文件队列、拖拽、上传暂停／继续、并行槽位调度、候选点选、复制时间点。点选状态在下一条消息中作为 reference 提交。

## 4. 系列、剧集与部分就绪

### 4.1 Schema

~~~ts
interface ImageAsset {
  imageId: string;
  url: string;                  // HTTP(S) 或相对于 API origin 的 URL
  expiresAt: UnixTimeMs | null;  // URL 到期时间，稳定 URL 为 null
}
interface Character {
  name: string;
  aliases: string[];
}
type SeriesStatus = "EMPTY" | "INDEXING" | "PARTIAL" | "READY" | "FAILED";
type IndexStatus = "NOT_READY" | "SYNCING" | "READY" | "FAILED";
type PlaybackStatus = "PREPARING" | "READY" | "FAILED" | "MISSING";

interface SeriesSummary {
  seriesId: string;
  workspaceId: string;
  name: string;
  description: string | null;
  status: SeriesStatus;
  videoCount: number;            // 已发布 Video 数，不含未合并上传
  readyVideoCount: number;       // 解析和索引均已就绪的视频数
  failedVideoCount: number;      // 当前最新建库任务最终失败的视频数
  pendingUploadCount: number;    // 服务端未完成上传数，不是当前活跃网络请求数
  cover: ImageAsset | null;
  lastSearch: {
    conversationId: string;
    query: string;               // 最近一次成功受理检索的用户问题
    searchedAt: UnixTimeMs;
  } | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}
interface VideoSummary {
  videoId: string;
  workspaceId: string;
  seriesId: string;
  episodeOrder: number;          // integer > 0
  uploadSequence: number;        // integer > 0，上传顺序，不是集数
  originalFilename: string;
  totalBytes: number;
  durationMs: DurationMs;
  uploadStatus: "COMPLETED";
  uploadProgress: 100;
  parseStatus: ParseJobStatus;
  parseProgress: number;         // integer 0..100，建库进度
  indexStatus: IndexStatus;
  playbackStatus: PlaybackStatus;
  playbackUrl: string | null;
  playbackUrlExpiresAt: UnixTimeMs | null;
  thumbnail: ImageAsset | null;
  latestJobId: string;
  lastError: Failure | null;
}
interface SeriesDetail extends SeriesSummary {
  importantExtraData: Record<string, unknown> | null;
  characterBible: Character[];
  videos: VideoSummary[];             // 全部已发布视频，按 episodeOrder、uploadSequence 升序
  pendingUploads: UploadSessionView[]; // 全部非终态上传，按 uploadSequence 升序
}
interface SeriesCreateRequest {
  workspaceId: string;
  name: string;                      // trim 后 1..255 字符
  description?: string | null;
  importantExtraData?: Record<string, unknown> | null;
  characterBible?: Character[];
}
~~~

### 4.2 路由与参数

| 路由 | Query / Body | 排序与行为 |
| --- | --- | --- |
| POST /api/series/create | Body: SeriesCreateRequest | 返回新系列详情；相同有效名称和资料幂等返回原系列，同名资料冲突 409 |
| GET /api/series/list | Query: workspaceId；可选 cursor、limit、status、query | query 搜索名称；createdAt、seriesId 升序 |
| GET /api/series/get | Query: workspaceId、seriesId | 详情不使用列表摘要冒充；视频与待上传项均完整返回 |

~~~json
{
  "workspaceId": "local-default",
  "name": "BanG Dream! It's MyGO!!!!!",
  "description": "动画正片",
  "importantExtraData": null,
  "characterBible": [
    { "name": "长崎爽世", "aliases": ["爽世"] }
  ]
}
~~~

### 4.3 状态与检索范围

| Series 状态 | 精确含义 | UI 检索行为 |
| --- | --- | --- |
| EMPTY | 无已发布视频，也无未完成上传 | 引导上传 |
| INDEXING | 没有可检索视频，仍有上传／建库工作 | 展示进度，不能提交检索 |
| PARTIAL | 至少一个视频可检索，仍有未就绪、失败或待上传内容 | **允许检索**，明确显示“已就绪 N / 已导入 M 集”及待上传数 |
| READY | 至少一个已发布视频，全部解析与索引就绪，且没有未完成上传 | 允许检索 |
| FAILED | 没有可检索视频、没有在进行的工作，但已导入视频仍未就绪，例如任务失败、取消或索引异常 | 展示真实原因及可用的重跑入口 |

- `videoCount === videos.length`；未合并文件属于 pendingUploads，不能生成虚假的 videoId。
- 资料库按 seriesId + episodeOrder 合并展示视频和上传行，上传未完成时可使用 uploadSessionId 作为行 ID。
- `readyVideoCount` 以当前索引事实为准，不以“上传 100%”或“解析进度 100%”推断。
- 若第 7 集已就绪、第 8–10 集在上传／建库，MUST 允许搜索第 7 集；不得锁住整个系列。
- 不允许暗中移除请求的 Series；未就绪视频可以排除，但 SearchResult MUST 回传实际 searchedVideoIds、partial 和 warnings。
- 非空且媒体可用的系列需要真实代表帧封面；无素材时 cover 可以为 null，UI 使用明确的空态，不使用另一部作品的图片。

## 5. 并行上传、恢复与真实取消

### 5.1 Schema

~~~ts
type UploadStatus =
  | "UPLOADING" | "COMPLETING" | "COMPLETED"
  | "CANCELLING" | "CANCELLED" | "EXPIRED";
type UploadCompletionStage = "VERIFYING" | "ASSEMBLING" | "PROBING" | "CREATING_JOB";

interface UploadSessionCreateRequest {
  workspaceId: string;
  episodeOrder: number;
  uploadSequence?: number;       // 不提供时由服务端原子分配，不能强制等于 episodeOrder
  originalFilename: string;      // 仅文件名，不含本地路径
  mediaType: "video/mp4" | "video/quicktime" | "video/x-msvideo";
  totalSizeBytes: number;        // integer > 0，最大值取 system.status 的能力字段
  contentSha256: string;         // 原文件完整内容的 SHA-256，64 个小写十六进制字符
}
interface UploadSessionView {
  uploadSessionId: string;
  workspaceId: string;
  seriesId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
  contentSha256: string;         // 创建时固定，恢复上传与最终合并校验使用同一值
  preferredSegmentSizeBytes: number;
  totalSegments: number;
  status: UploadStatus;
  uploadedSegmentCount: number;
  uploadedSizeBytes: number;     // 已校验且持久化确认的字节
  missingSegmentNumbers: number[];
  completionStage: UploadCompletionStage | null;
  completionProgress: number | null; // integer 0..100；不能可靠估计时为 null
  estimatedRemainingMs: DurationMs | null; // 服务端合并阶段 ETA，上传网络 ETA 由客户端算
  expiresAt: UnixTimeMs;
  videoId: string | null;
  jobId: string | null;          // 建库受理后非空，用于页面刷新后恢复解析追踪
  lastError: Failure | null;
  version: number;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}
interface UploadSegmentReceipt {
  uploadSessionId: string;
  segmentNumber: number;         // zero-based
  startByte: number;
  endByteExclusive: number;
  actualSizeBytes: number;
  checksumSha256: string;        // 64 个小写十六进制字符
}
interface UploadCompleteResult {
  uploadSession: UploadSessionView;
  videoId: string | null;
  jobId: string | null;
}
~~~

### 5.2 创建、列出与查询

| 路由 | 参数 | 行为 |
| --- | --- | --- |
| POST /api/uploads/create | Query: seriesId；Body: UploadSessionCreateRequest | 201；每个文件单独创建会话 |
| GET /api/uploads/list | Query: workspaceId；可选 seriesId、status、cursor、limit | 200 Page；默认列出全部非终态会话，uploadSequence、uploadSessionId 升序 |
| GET /api/uploads/get | Query: workspaceId、uploadSessionId | 200；服务端确认点与完成阶段的权威快照 |

`status` 筛选可以使用逗号分隔的 UploadStatus；显式 `status=all` 查询全部状态。集数在同一系列中唯一；不同有效上传不能占用同一集数。同一系列已导入或被有效会话占用的相同 contentSha256 返回 DUPLICATE_CONTENT，不静默改集数或另造视频；最终发布前仍须根据服务端校验后的真实内容再次检查冲突。

~~~json
{
  "workspaceId": "local-default",
  "episodeOrder": 8,
  "originalFilename": "episode-08.mp4",
  "mediaType": "video/mp4",
  "totalSizeBytes": 25165824,
  "contentSha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
~~~

对应的完整上传快照示例：

~~~json
{
  "uploadSessionId": "8b6ce5d9-d1bf-4b04-b201-4f16cae46cef",
  "workspaceId": "local-default",
  "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
  "episodeOrder": 8,
  "uploadSequence": 14,
  "originalFilename": "episode-08.mp4",
  "mediaType": "video/mp4",
  "totalSizeBytes": 25165824,
  "contentSha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "preferredSegmentSizeBytes": 8388608,
  "totalSegments": 3,
  "status": "UPLOADING",
  "uploadedSegmentCount": 1,
  "uploadedSizeBytes": 8388608,
  "missingSegmentNumbers": [1, 2],
  "completionStage": null,
  "completionProgress": null,
  "estimatedRemainingMs": null,
  "expiresAt": 1789524000000,
  "videoId": null,
  "jobId": null,
  "lastError": null,
  "version": 2,
  "createdAt": 1789437600000,
  "updatedAt": 1789437601000
}
~~~

- missingSegmentNumbers 必须为真实、无重复、升序、完整缺段集合，不得为了减小响应返回部分编号。
- totalSegments = ceil(totalSizeBytes / preferredSegmentSizeBytes)。
- 未完成且会话有效时，uploadedSegmentCount + missingSegmentNumbers.length = totalSegments。
- COMPLETED 后 staging 可以清理，但确认进度仍为完整文件、missingSegmentNumbers 仍为 []；不能因为 staging 删除而倒退进度。
- expiresAt 只控制未完成上传的有效期，不使已发布 Video 或 Job 到期。
- COMPLETED 时 videoId 与 jobId 非空；其他状态若尚未发布均为 null。
- contentSha256 在创建时固定；前端用流式／Worker 计算完整文件 hash，不能仅用文件名、大小或修改时间识别原文件。校验阶段作为本地“校验文件”状态，不占用活跃上传轨道，不把文件完整读入内存后再复制多份。
- 浏览器文件句柄不会由 API 自动恢复。刷新后从 list/get 重建会话列表，提示重新选择原文件，再恢复缺段；不能显示正在传输的假轨道。

### 5.3 分段 PUT

~~~http
PUT /api/uploads/segments/upload?workspaceId=local-default&uploadSessionId=8b6ce5d9-d1bf-4b04-b201-4f16cae46cef&segmentNumber=1
Content-Type: application/octet-stream
Content-Range: bytes 8388608-16777215/25165824
X-Content-SHA256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
~~~

Body 为原始二进制，不使用 multipart。PUT 通过会话、编号、范围和内容 hash 幂等，不额外要求 Idempotency-Key。

- startByte = segmentNumber × preferredSegmentSizeBytes。
- endByteExclusive = min(startByte + preferredSegmentSizeBytes, totalSizeBytes)。
- Content-Range 的结束字节是 endByteExclusive - 1。
- 必须验证状态、归属、编号、总大小、实际长度、范围、SHA-256。
- 支持乱序和重复请求；相同编号相同内容重传成功，不增加确认字节两次；不同内容返回 409。
- 校验不符返回 CHECKSUM_MISMATCH，不能提交该段。
- 必须支持 3 个文件 × 每文件 3 段，即至少 9 个受限并发 PUT；一个会话的锁不得串行化所有文件。
- 响应是完整 UploadSegmentReceipt；前端只在成功确认后将该段计入 confirmedBytes。

### 5.4 提交完成与后台衔接

`POST /api/uploads/complete?workspaceId=...&uploadSessionId=...`，无 Body，带 Idempotency-Key。

- 若仍有缺段：409 MISSING_SEGMENTS；前端重新 GET 并补缺段。
- 首次受理：202 UploadCompleteResult，status 为 COMPLETING，完成校验、合并、媒体探测和创建 Job 均由服务端接管。
- VERIFYING 必须核验合并后完整文件的 SHA-256 等于会话 contentSha256；单段校验成功不能代替完整源文件一致性校验。不一致时记录 CHECKSUM_MISMATCH，不能发布 Video 或创建可运行 Job。
- 前端查询会话直到 COMPLETED，取得 videoId/jobId，立即读取 Job 并订阅事件。
- 已完成的幂等重放：200，返回同一 videoId/jobId；不能新建解析任务。
- 若工作已经在进行，不能另开合并任务；相同有效请求返回现有完成快照。
- COMPLETED 与 Job 可查询性必须一起生效，不能返回 jobId 后暂时 404。
- 可重试的完成失败恢复为 UPLOADING，保留已确认段和 lastError；用户复用同一提交动作重试可重新受理，MUST NOT 永久缓存瞬时失败。
- 不可修复的文件问题必须返回具体错误，并允许真正取消该会话后重新加入。
- 任一文件字节传完即可释放客户端上传槽位，合并／建库不占据其他文件的网络上传槽位。
- 单个文件完成后独立进入自己的 Job，不等待同批其他文件。

### 5.5 暂停、恢复、取消与进度

- 暂停／全部暂停：客户端 abort 该文件当前 PUT 并停止调度。后台合并／解析需要调用各自 cancel 接口，不能用 abort 冒充取消。
- 恢复／全部恢复：GET 最新会话，核对大小并计算所选文件 contentSha256，与会话值完全一致后才重传缺段。仅文件名、大小相同不足以恢复，不能把不同文件混入同一会话。
- 本地“收起已完成”只隐藏 UI 记录，不删除 Video、Job 或服务器文件。
- **真正取消**：`POST /api/uploads/cancel?workspaceId=...&uploadSessionId=...`，无 Body，带 Idempotency-Key。
- 取消受理返回 202 CANCELLING；完成后为 CANCELLED，后续 PUT／complete 不得成功，需释放该集数占用并清理本会话 staging。
- 重复取消已 CANCELLED／EXPIRED 的会话返回 200 当前快照。
- 若发布 Video + Job 已先成功提交，返回 409 并提示进入 Job 取消；不得删除已发布资产冒充取消。
- EXPIRED／CANCELLED 会话不能恢复；重新加入文件使用新幂等键。

进度口径：

~~~text
单文件确认上传进度 = uploadedSizeBytes / totalSizeBytes × 100
批次确认上传进度   = sum(confirmedBytes) / sum(totalSizeBytes) × 100
~~~

批次为客户端本次选中的固定文件集合，不新增 Batch 资源。已完成上传文件保留其完整字节；明确移除／取消文件后才从当前集合移除。

XHR in-flight bytes 可驱动临时动画，但必须与服务端确认字节区分；不能作为已持久化进度。上传网络速度与 ETA 由客户端真实传输样本计算，不可靠时为 null／“计算中”。上传百分比、合并百分比和 Job.progress 分别展示，不能把三者相加或平均，不能上传 100% 后用解析 5% 覆盖上传轨道。

全局导航仅展示正在发 PUT 的真实文件轨道及真实占用槽位；暂停、等待重选文件、合并中和解析中有独立状态，不能占用三条上传轨道而遮住其他活跃上传。

## 6. 解析任务、统计、重跑与取消

### 6.1 Schema

~~~ts
type ParseJobStatus =
  | "QUEUED" | "RUNNING" | "RETRY_WAIT" | "SUCCEEDED" | "FAILED"
  | "CANCELLING" | "CANCELLED";
type ParseJobStage =
  | "QUEUED" | "PREPARING" | "SPLIT_READY" | "PARSING"
  | "TEXT_EMBEDDING" | "VISION_EMBEDDING" | "PERSISTING"
  | "INDEXING" | "COMPLETED";
type ChunkStage =
  | "PREPARED" | "SPLIT_READY" | "PARSED"
  | "TEXT_EMBEDDED" | "VISION_EMBEDDED" | "INDEXED";

interface ChunkCheckpoint {
  sequenceNo: number;
  videoChunkId: string;
  stage: ChunkStage;
  lastError: Failure | null;
}
interface ParseJobView {
  jobId: string;
  workspaceId: string;
  seriesId: string;
  videoId: string;
  seriesName: string;           // 列表、详情、SSE 直接提供，不要求前端补 Join
  episodeOrder: number;
  originalFilename: string;
  pipelineVersion: string;
  retryOfJobId: string | null;  // 手动重跑的来源 Job，初次任务为 null
  status: ParseJobStatus;
  stage: ParseJobStage;
  progress: number;             // integer 0..100，覆盖解析到索引就绪
  attemptCount: number;         // 当前 Job 内的真实自动尝试次数
  maxAttempts: number;
  availableAt: UnixTimeMs | null;
  estimatedRemainingMs: DurationMs | null;
  startedAt: UnixTimeMs | null;
  completedAt: UnixTimeMs | null;
  lastError: Failure | null;
  checkpoints: ChunkCheckpoint[];
  canRetry: boolean;            // 来源可读取，且没有冲突中的新任务时才允许
  canCancel: boolean;
  version: number;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}
interface JobSummary {
  workspaceId: string;
  seriesId: string | null;      // null 表示整个 Workspace
  counts: {
    queued: number;
    running: number;
    retryWait: number;
    cancelling: number;
    completed: number;
    failed: number;
    cancelled: number;
  };
  checkpointCount: number;      // 当前范围所有 Job 的检查点总数
  version: number;              // 当前 Workspace／Series 统计范围内单调递增
  updatedAt: UnixTimeMs;
}
interface JobRetryRequest {
  workspaceId: string;
  expectedVersion: number;      // 防止基于过期 Job 状态操作
}
interface JobsSnapshotEvent {
  type: "jobs.snapshot";
  workspaceId: string;
  seriesId: string | null;
  jobId: string | null;
  jobs: ParseJobView[];
  summary: JobSummary;
  occurredAt: UnixTimeMs;
}
~~~

### 6.2 查询与筛选

| 路由 | Query | 返回与用途 |
| --- | --- | --- |
| GET /api/jobs/list | workspaceId；可选 seriesId、status、cursor、limit | `Page<ParseJobView>`，createdAt、jobId 倒序 |
| GET /api/jobs/get | workspaceId、jobId | 完整 Job 与全部检查点 |
| GET /api/jobs/summary | workspaceId；可选 seriesId | 全范围统计，不受列表分页或状态标签筛选影响 |

`status` 可用逗号分隔的 ParseJobStatus；省略或 `all` 表示全部。

任务页“进行中”筛选包括 QUEUED／RUNNING／RETRY_WAIT／CANCELLING，“异常”包括 FAILED，“已完成”包括 SUCCEEDED，“已取消”包括 CANCELLED。

UI 顶部“处理中” = queued + running + retryWait + cancelling；“已完成” = completed；“切片检查点” = checkpointCount。这些值只能来自 JobSummary，不能用当前列表页推断。

### 6.3 进度与终态

- stage 必须匹配上述枚举，不能返回前端未知的内部阶段字符串。
- 每个 Job 内 progress 对外不倒退；自动重试保留已经确认的检查点，不伪装成全新上传。
- 只有解析、文本／视觉向量持久化与索引一致性全部确认后才能 SUCCEEDED、COMPLETED、progress=100。
- 视频上传结束或模型返回文本不代表建库完成。
- checkpoints 按 sequenceNo 升序，不出现重复 videoChunkId；格阵颜色由 ChunkStage 决定。
- RETRY_WAIT 提供真实 availableAt 和 lastError；没有可靠 ETA 时 estimatedRemainingMs 为 null。
- FAILED／CANCELLED 保留最后有效阶段、进度与检查点，不强制置 100%。
- completedAt 对所有终态表示本次 Job 结束时间；非终态为 null。
- Job 终态提交时，Video 的最新 parseStatus、indexStatus 和 Series 的就绪计数必须同步更新。
- 单个视频失败不能将其他已就绪视频变成不可检索。

### 6.4 手动重跑

`POST /api/jobs/retry?jobId=...`，Body 为 JobRetryRequest，带 Idempotency-Key：

~~~json
{
  "workspaceId": "local-default",
  "expectedVersion": 12
}
~~~

- 仅 canRetry=true 的 FAILED／CANCELLED Job 可手动重跑。
- 返回 201 **新的 ParseJobView**，jobId 不同，retryOfJobId 指向原 Job，status=QUEUED、attemptCount=0。
- 原 Job 保持终态用于历史与诊断，不重置其 version 或冒充原失败已消失。
- 相同幂等键重放必须返回同一新 Job；同一视频已有在进行的最新建库任务时返回 JOB_IN_PROGRESS。
- 后端负责判断可复用的切片／分析／向量检查点；不得只调用 uploads/complete 返回原失败 Job。
- 来源媒体丢失时 canRetry=false，并提供具体 lastError；不要求用户重复传输仍然可读取的原视频。
- Video.latestJobId 切换到新 Job；UI 订阅新 jobId，避免原 Job 的迟到 SSE 污染新任务。

### 6.5 真正取消 Job

`POST /api/jobs/cancel?workspaceId=...&jobId=...`，无 Body，带 Idempotency-Key。

- canCancel=true 时返回 202 CANCELLING；服务端停止继续调度、抑制迟到结果提交，最终返回／推送 CANCELLED。
- 取消不是暂停 Worker，也不删除原 Video、已确认检查点或其他 Job。
- 重复取消已结束任务返回 200 当前终态；若任务先 SUCCEEDED，返回 SUCCEEDED，UI 不能谎称已取消。
- CANCELLING 期间不能再发起新重跑；CANCELLED 后可通过 retry 创建新 Job。
- 浏览器停止订阅 events 不得取消 Job。

### 6.6 SSE：全局与单任务

`GET /api/jobs/events?workspaceId=...`；可选 `seriesId` 或 `jobId`，二者不能同时使用。Accept 为 text/event-stream。

| event | data schema | 语义 |
| --- | --- | --- |
| jobs.snapshot | JobsSnapshotEvent | 连接后的首个完整状态；全局／系列连接包含全部非终态 Job，单任务连接包含指定 Job，即使已终态 |
| job | ParseJobView | 新任务创建或阶段、进度、检查点、错误、状态发生变化时的完整资源快照 |
| jobs.summary | JobSummary | 计数或检查点总数变化后的权威统计 |

- Job 事件 id 使用 `jobId:version`；首帧和后续事件必须使用相同 DTO。
- 任务页使用一个 Workspace／Series SSE，不为每个历史任务建立连接。
- 单任务连接在推送最终终态后关闭；全局连接持续接收新任务。
- 心跳使用 SSE 注释，不伪造进度事件；最长约 15 秒一次。
- 本契约不要求回放所有旧事件。断线／重连后 MUST 同步 list/get/summary；只靠“当前活跃 Job 首帧”不能恢复断线期间已经结束的任务。
- GET 快照与 event 可能交错；按资源 version 合并，不让低版本覆盖高版本。
- JobSummary.version 按 workspaceId + seriesId 的统计范围比较；切换范围后不能沿用另一范围的版本。统计快照也拒绝低版本覆盖，不能靠毫秒更新时间解决同一毫秒内的变化。
- 设置正确的 Cache-Control: no-cache 与禁止代理缓冲，及时发送完整事件帧。

## 7. Conversation、消息、候选引用与回答流

### 7.1 Schema

~~~ts
interface ConversationView {
  conversationId: string;
  workspaceId: string;
  title: string;
  messageCount: number;
  lastMessagePreview: string | null;
  lastMessageAt: UnixTimeMs | null;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}
interface ConversationCreateRequest {
  workspaceId: string;
  title?: string;               // 省略时“新对话”，首条问题后自动形成可辨认标题
}
interface EvidenceReference {
  messageId: string;            // 当前 Conversation 中提供候选的 ASSISTANT 消息
  videoChunkId: string;         // 该消息实际 hits 中的片段
}
type MessageStatus = "PROCESSING" | "COMPLETED" | "FAILED" | "CANCELLING" | "CANCELLED";
type AnswerPhase = "QUEUED" | "RETRIEVING" | "EVALUATING" | "RESPONDING";

interface MessageView {
  messageId: string;
  conversationId: string;
  seriesIds: string[];          // 此消息实际冻结的检索范围，非空
  sequenceNo: number;           // zero-based，单对话持久化、严格递增
  role: "USER" | "ASSISTANT";
  status: MessageStatus;
  phase: AnswerPhase | null;    // 公开工作阶段，不包含内部推理正文
  content: string | null;
  modelName: string | null;
  replyToMessageId: string | null;
  reference: EvidenceReference | null;
  retrievalQuery: string | null; // 实际检索 query，不冒充用户原始 content
  searchResult: SearchResult | null;
  lastError: Failure | null;
  version: number;
  createdAt: UnixTimeMs;
  updatedAt: UnixTimeMs;
}
interface MessageSendRequest {
  workspaceId: string;
  content: string;              // trim 后 1..20000 字符
  seriesIds?: string[];         // 每次发送多选；省略或 [] 展开为当前 Workspace 全部 READY Series
  topK?: number;                // 默认 settings.retrieval.defaultTopK，上限 system.capabilities.maximumTopK
  reference?: EvidenceReference | null;
}
interface MessageSendResult {
  userMessage: MessageView;
  assistantMessage: MessageView;
}
interface MessagesSnapshotEvent {
  type: "messages";
  conversationId: string;
  messages: MessageView[];
  occurredAt: UnixTimeMs;
}
interface AnswerEventBase {
  conversationId: string;
  messageId: string;            // 本次 ASSISTANT ID
  sequence: number;             // 本次回答流内严格递增正整数，不是时间戳
  occurredAt: UnixTimeMs;
}
interface AnswerStartedEvent extends AnswerEventBase, MessageSendResult {
  type: "answer.started";
}
interface AnswerProgressEvent extends AnswerEventBase {
  type: "answer.progress";
  phase: AnswerPhase;
}
interface AnswerEvidenceEvent extends AnswerEventBase {
  type: "answer.evidence";
  searchResult: SearchResult;   // 完整检索结果，不仅是 hits
}
interface AnswerDeltaEvent extends AnswerEventBase {
  type: "answer.delta";
  delta: string;                // 真实回答增量，不是前端拆分整段文本
}
interface AnswerCompletedEvent extends AnswerEventBase, MessageSendResult {
  type: "answer.completed";
}
interface AnswerFailedEvent extends AnswerEventBase, MessageSendResult {
  type: "answer.failed";
  error: Failure;
}
interface AnswerCancelledEvent extends AnswerEventBase, MessageSendResult {
  type: "answer.cancelled";
}
interface AnswerInterruptedEvent extends AnswerEventBase {
  type: "answer.interrupted";
  error: Failure;               // 传输中断，不表示业务 FAILED
}
type AnswerEvent =
  | AnswerStartedEvent | AnswerProgressEvent | AnswerEvidenceEvent | AnswerDeltaEvent
  | AnswerCompletedEvent | AnswerFailedEvent | AnswerCancelledEvent | AnswerInterruptedEvent;
~~~

### 7.2 对话创建、历史与范围

| 路由 | Query / Body | 返回与行为 |
| --- | --- | --- |
| POST /api/agent/conversations/create | Body: ConversationCreateRequest | 201；每次“新建聊天”创建独立 Conversation |
| GET /api/agent/conversations/list | Query: workspaceId；可选 seriesId、query、cursor、limit | Page；updatedAt、conversationId 倒序；seriesId 匹配包含该系列的对话，query 搜索标题 |
| GET /api/agent/conversations/get | Query: workspaceId、conversationId | 完整 Conversation 元数据 |

~~~json
{
  "workspaceId": "local-default",
  "title": "爽世与春日影"
}
~~~

- Conversation 创建不绑定 Series，也不要求素材已就绪。
- 每次 send 的 seriesIds 决定该次 Kick 的范围；同一 Conversation 可以在下一条消息修改多选。
- 省略或空数组默认当前 Workspace 全部 READY Series；显式选中的 Series 必须属于 Workspace 且 READY，不能静默替换或缩小。
- 后端规范化、去重并冻结实际范围到本轮 USER / ASSISTANT.seriesIds，Retrieve Tool 只能使用该范围；运行期间新建素材不扩大范围。
- 幂等重放使用原请求与已冻结范围，不因此后新增 Series 改变旧消息或重新执行。
- 标题不能让所有历史对话都永久叫“画面问询”；未自定义标题时，首条用户问题后由后端生成短标题。
- 历史对话来自服务端 list，不以浏览器保存的一条 conversationId 代替完整历史。
- localStorage 可缓存当前选择，但清空浏览器缓存后仍能重新列出已有对话。
- Conversation 之间不自动共享聊天上下文；同一系列可有多个独立 Conversation。

### 7.3 消息历史与单轮恢复

`GET /api/agent/messages/list`：

- Query：workspaceId、conversationId；可选 cursor、limit、afterSequenceNo。
- 默认 afterSequenceNo=-1；范围 integer >= -1，筛选 sequenceNo > afterSequenceNo。
- 返回 `Page<MessageView>`，按 sequenceNo 升序；total 是该条件下消息总数。
- PROCESSING 消息的内容／状态会原位更新而不增加 sequenceNo；afterSequenceNo 只能用于新增消息，不能用于恢复旧处理中消息的最新内容。
- 全量恢复与翻页使用 cursor；进行中的已知消息通过 get 和 events 校准。

`GET /api/agent/messages/get`：

- Query：workspaceId、conversationId，并且 **messageId 与 idempotencyKey 恰好提供一个**。
- 返回 MessageSendResult：查询消息所在一轮的 USER + ASSISTANT；按幂等键查询发送请求对应的同一轮。
- 用于发送响应丢失、页面刷新、时间书签恢复、查询生成中消息和取消后的最终状态。
- 尚未受理的幂等键返回 404；只在确认未受理后才重放发送，仍复用原键。

同一轮 USER 与 ASSISTANT 初始记录必须一起持久化。USER 的 status 为 COMPLETED；ASSISTANT 的 replyToMessageId 必须等于本轮 USER.messageId。USER.reference 保存用户当时实际提交的候选引用，ASSISTANT.reference 为 null。

### 7.4 发消息与“这段／上一条第二段”的指代

`POST /api/agent/messages/send?conversationId=...`，Body 为 MessageSendRequest，带 Idempotency-Key。

~~~json
{
  "workspaceId": "local-default",
  "content": "这段里她为什么哭？再往前发生了什么？",
  "seriesIds": ["77285f16-3e5b-592e-ba15-8b224b619806"],
  "topK": 10,
  "reference": {
    "messageId": "52d1e4fc-384e-42fd-bf02-9b7c66a1ec00",
    "videoChunkId": "75f5a5f0-d2d1-4bf3-bc37-5df29b640ec3"
  }
}
~~~

- 前端点选候选后，下一条有关该候选的问题 MUST 提交 reference；切换 Conversation 时清除旧 reference。
- 用户可以解除引用；不允许浏览器悄悄把旧片段带入另一条新对话。
- 后端验证 reference.messageId 属于当前对话、role=ASSISTANT，且 reference.videoChunkId 确实存在于该消息返回的候选中，属于当前 scope。
- 引用的是事实证据，不是模型内部推理；后端自行加载原片段与上下文，前端不提交可篡改的整份 Evidence。
- 普通多轮提问使用已完成聊天历史；“上条第二段”等指代还需要后端读取上一轮有序候选 ID 和结构化 Evidence，不能只传 role/content 文本给 Agent。
- 只使用已确认的检索证据；失败／取消回答中已经审核并保存的证据仍可被引用，但必须保留其来源状态。
- 不合法引用返回 REFERENCE_INVALID，保留用户输入，不静默改引用。
- topK 的校验上限与公开 capabilities 必须一致；不能 OpenAPI 宣称 100，而实际只允许 20。
- 同一 Conversation 最多一个 PROCESSING／CANCELLING ASSISTANT；并发冲突返回 MESSAGE_IN_PROGRESS。
- 不同 Conversation 可以独立工作，不通过全局单例 scope 串扰。

Accept 的目标行为：

| Accept | 首次受理 | 幂等重放 |
| --- | --- | --- |
| application/json / 未指定 | 202 MessageSendResult；后台生成，前端用 get/events 获取终态 | 已终态返回 200 同一消息对；仍在进行返回 409 MESSAGE_IN_PROGRESS，再按幂等键恢复 |
| text/event-stream | 200 SSE，先发 started，再发送真实增量与终态 | 已终态发 started + 原终态快照，不重新调用模型；仍在进行返回 409 并通过快照恢复 |

前端不能通过发送新幂等键自动重试一次“结果未知”的发送。先查询原请求；用户明确要求重试最终失败的提问时才产生新键和新消息对，旧失败记录仍保留。

### 7.5 回答 SSE 的顺序、证据与终态

正常回答顺序：

~~~text
answer.started
→ answer.progress（真实工作阶段，可重复）
→ answer.evidence（已审核完整结果，包括 partial/warnings/query）
→ answer.delta（0..N 个真实 Provider 文本增量）
→ answer.completed
~~~

澄清可以不检索、不发送 evidence；无结果检索必须发送 hits=[] 的完整 SearchResult，不能用异常或上一次候选伪装当前结果。

- 每个 data 都有 type、conversationId、messageId、sequence、occurredAt，type 与 SSE event 一致。
- SSE id 为 messageId:sequence；前端只处理当前 Conversation、当前回答 ID 的递增 sequence。
- sequence 表示一条流的事件顺序，不承担跨连接的持久化版本；资源快照按 MessageView.version 合并。
- evidence 必须携带完整 SearchResult，避免前端从用户文本猜 retrievalQuery、自己拼 agentReply 或丢失 warnings。
- evidence 为本轮候选的完整替换，不是列表追加；未审核草稿和中间候选不得冒充最终公开证据。
- delta 必须在 Provider 产生后及时发送，不能等完整回答生成后再拆字模拟流式。
- SSE 失败前若已受理业务消息，必须通过 get/events 可恢复；不能凭 HTTP 断开把 PROCESSING 改成 FAILED。
- completed 必须在最终完整正文、候选、理由和终态持久化后发送；快照必须与随后 GET 一致。
- failed 表示持久化业务 FAILED，带完整消息对和公开 error；允许保留真实部分正文，但必须标为未完成。
- interrupted 只表示当前流传输中断，业务可能仍运行；客户端继续查询／订阅，不添加伪造的失败消息。
- 终态事件发送后关闭本次回答流；如果终态持久化失败，不得发送 completed／failed 假快照。
- 不返回私有 Chain-of-Thought 或 Evaluator 内部评语，公开 phase 只是“召回／核验／回复”等 UI 工作状态。

合法 delta 事件示例：

~~~text
id: 52d1e4fc-384e-42fd-bf02-9b7c66a1ec00:4
event: answer.delta
data: {"type":"answer.delta","conversationId":"0af395ca-f9e8-4a28-b57c-61c9a31b5463","messageId":"52d1e4fc-384e-42fd-bf02-9b7c66a1ec00","sequence":4,"occurredAt":1789437603000,"delta":"第一段同时命中了人物、质问动作与曲名。"}

~~~

### 7.6 消息快照 SSE 与恢复

`GET /api/agent/messages/events?workspaceId=...&conversationId=...`：

- event 名为 messages，data 为 MessagesSnapshotEvent。
- 首帧包含当前对话的完整消息列表，包括 [] 和 PROCESSING；消息新增、版本变化或终态变化时发送完整快照。
- 允许批量保存生成中 content 检查点，不要求每个 token 写数据库；最终完整正文必须持久化。生成中的查询可能只返回最近已保存的部分正文。
- PROCESSING 快照中较短的持久化正文不能擦掉本地已收到的真实 delta；终态完整 content 是最终权威值，允许覆盖部分流内容。
- snapshot 不增加客户端自行生成的 messageId，不通过文本匹配把消息凑成历史。
- 切换对话后关闭旧 events 与回答流，旧回调必须按 conversationId/messageId 拒绝。
- 断开订阅不停止服务端生成；再次打开原对话可以恢复同一业务消息。
- 最长约 15 秒注释心跳；此流在 Conversation 仍存在时保持打开，不因单条回答结束而关闭。

### 7.7 真正停止生成

`POST /api/agent/messages/cancel?workspaceId=...&conversationId=...&messageId=...`，无 Body，带 Idempotency-Key。

- messageId 必须为该对话中本次正在生成的 ASSISTANT。
- 返回 202 MessageSendResult，ASSISTANT 为 CANCELLING；最终保存 CANCELLED，并发送 answer.cancelled／messages 快照。
- 已生成的真实正文和已审核候选可以保留，但 UI 必须标注“已停止”，不能展示为正常完成。
- 取消受理后 Provider 的迟到结果不能把 CANCELLED 覆盖为 COMPLETED。
- 若完成提交先发生，返回 200 原 COMPLETED 快照；重复取消其他终态也返回 200 原终态。
- 取消不会删除消息历史或中断其他对话。

## 8. 候选、选择理由、原始证据与前后语境

### 8.1 Schema

~~~ts
interface TranscriptCue {
  speaker: string | null;
  text: string;
  startMs: MediaOffsetMs;
  endMs: MediaOffsetMs;
}
interface VisualEvent {
  description: string;
  startMs: MediaOffsetMs;
  endMs: MediaOffsetMs;
}
interface Keyframe extends ImageAsset {
  atMs: MediaOffsetMs;
  alt: string | null;
}
interface MatchedEvidence {
  kind: "TRANSCRIPT" | "VISUAL_EVENT" | "CONTEXT";
  text: string;
  startMs: MediaOffsetMs | null;
  endMs: MediaOffsetMs | null;
}
interface SelectionReason {
  summary: string;              // 针对当前问题，逐候选说明为何入选
  matchedEvidence: MatchedEvidence[];
  limitations: string[];
}
interface VideoChunkCandidate {
  videoChunkId: string;
  rank: number;                 // 最终候选显示序号，integer 1..N
  fusedRank: number;            // 原检索融合排名，不冒充 Agent 最终顺序
  fusedScore: number;           // finite number > 0，不是概率
  textRank: number | null;
  textScore: number | null;
  visionRank: number | null;
  visionScore: number | null;
}
interface VideoChunkEvidence {
  workspaceId: string;
  seriesId: string;
  seriesName: string;
  seriesStatus: SeriesStatus;
  videoId: string;
  episodeOrder: number;
  originalFilename: string;
  durationMs: DurationMs;
  videoChunkId: string;
  sequenceNo: number;
  startMs: MediaOffsetMs;
  endMs: MediaOffsetMs;
  analysisWindowStartMs: MediaOffsetMs;
  analysisWindowEndMs: MediaOffsetMs;
  playbackStatus: PlaybackStatus;
  playbackUrl: string | null;
  playbackUrlExpiresAt: UnixTimeMs | null;
  thumbnail: Keyframe | null;
  keyframes: Keyframe[];
  transcript: TranscriptCue[];
  baselineVisualCaption: string | null;
  allTimeEvents: VisualEvent[];
  allTimeEventText: string;
  videoCompact: string;
  contextBeforeCompact: string | null;
  contextAfterCompact: string | null;
  windowSummary: string;
  isFallback: boolean;
  modelProvider: string | null;
  modelName: string | null;
  pipelineVersion: string;
  parsedAt: UnixTimeMs;
}
interface SearchHit {
  candidate: VideoChunkCandidate;
  selectionReason: SelectionReason;
  evidence: VideoChunkEvidence;
}
interface SearchResult {
  query: string;                // 真实有效检索 query；用户原文在 USER.content
  seriesIds: string[];           // 必须与此轮 USER / ASSISTANT 已冻结的实际范围一致
  searchedVideoIds: string[];    // 实际纳入本次搜索的视频范围，不只是命中视频
  partial: boolean;
  warnings: string[];
  agentReply: {
    message: string;             // 终态与 ASSISTANT.content 一致
    resultCount: number;        // 必须等于 hits.length
  };
  hits: SearchHit[];
}
~~~

### 8.2 UI 字段消费与不变量

| UI 区域 | 必须使用的字段 |
| --- | --- |
| 候选序号／集数／时间 | candidate.rank、evidence.episodeOrder、startMs、endMs |
| 候选图与主画面关键帧 | evidence.thumbnail、keyframes、playbackStatus |
| 候选标题与对白 | videoCompact、windowSummary、transcript |
| “为什么选它” | selectionReason.summary |
| 命中事实 | selectionReason.matchedEvidence |
| Text／Vision 来源标签 | candidate.textRank／visionRank，matchedEvidence.kind |
| 仍需核验／弱匹配 | selectionReason.limitations、evidence.isFallback |
| 原始证据 | transcript、baselineVisualCaption、allTimeEvents、allTimeEventText |
| 前后语境 | contextBeforeCompact、videoCompact、contextAfterCompact、分析窗口范围 |
| 当前检索范围与部分结果提示 | seriesIds、searchedVideoIds、partial、warnings |
| 视频播放器 | playbackStatus、playbackUrl、durationMs、片段和分析窗口范围 |

强制不变量：

~~~text
agentReply.resultCount = hits.length
candidate.videoChunkId = evidence.videoChunkId
candidate.rank = hits 中的位置 + 1
0 <= analysisWindowStartMs <= startMs < endMs <= analysisWindowEndMs <= durationMs
0 <= 每条有定位的对白、事件、命中事实 startMs < endMs <= durationMs
~~~

- hits 中 videoChunkId 不重复，每个 Evidence 的 Workspace、Series 与本次范围匹配。
- 所有 textRank／visionRank 非空时为正整数；未召回的通道 rank 与 score 均为 null。
- matchedEvidence 的 startMs/endMs 必须成对为 null 或成对有效。
- 当检索只覆盖已就绪视频或某个通道降级时 partial=true，并有非空 warnings，逐候选 limitations 也需指出其影响。
- 完整正常结果 partial=false、warnings=[]；fusedScore 不得转成“置信度 97%”。
- 生成中 evidence 事件里的 agentReply.message 可以是已审核的摘要；completed 时必须替换为与最终 ASSISTANT.content 一致的正文。
- 无结果返回完整 SearchResult、hits=[]、resultCount=0 和明确可读回复；不能展示上轮 hits 为这轮结果。
- 不需要检索的澄清回答可 searchResult=null；若 UI 保留上一轮证据，必须标明其来源消息，不冒充本轮召回。
- 正常候选必须有至少一帧真实 keyframe 和 thumbnail；帧的 atMs 位于分析窗口内。playbackStatus 只表示视频可播放性，不能代替图片可用性检查。
- 图片确实不可用时 thumbnail=null、keyframes=[]，并在 limitations/warnings 中说明；不能用别的候选或素材图替换。

### 8.3 选择理由的内容要求

每个候选必须给出“当前问题的条件 → 命中的事实 → 为何保留 → 仍缺什么”，且理由与当前问题及 reference 对齐。

- summary 必须是非空的逐候选解释，不是所有候选相同的“相似度较高／双路召回第 N 名”。
- matchedEvidence 必须至少一项，能在返回的对白、视觉事件或上下文中核验。
- 不得无条件取首条对白当作“命中事实”；应选择与当前问题相关的证据。
- 人物、台词、动作、曲名、情绪等要求没有证据时必须承认未知或不符，不能从模型常识补剧情事实。
- 低序候选必须说明排序／保留依据；确有不足时指出相对首选缺少什么，例如“命中哭泣氛围，但人物不符、目标曲名未出现”。同样满足条件的候选不得仅因排名较低就编造不足。
- 单通道候选、降级解析、无法精确定位的上下文等限制必须显式说明。
- 理由须由后端绑定最终审核候选生成；前端只展示／排版，不从通道分数拼造 Agent 解释。
- 公开可核验理由与私有思维链严格分开；不输出隐藏 prompt、推理轨迹或审核内部文本。

下面是可直接展示的理由示例；nearby 候选也必须具有同样完整结构：

~~~json
{
  "summary": "这段命中了哭着挽留的情绪与关系冲突，但说话的人是灯，也未出现《春日影》，因此只保留为情绪近邻，而非首选。",
  "matchedEvidence": [
    {
      "kind": "TRANSCRIPT",
      "text": "不要走……",
      "startMs": 1184000,
      "endMs": 1187000
    }
  ],
  "limitations": ["人物与问题中的爽世不符", "目标曲名未命中"]
}
~~~

### 8.4 刷新证据与时间书签

`GET /api/video-chunks/get?workspaceId=...&videoChunkId=...` 返回完整 VideoChunkEvidence，用于：

- 关键帧／视频 URL 到期后获取新的公开 URL。
- 恢复复制的片段时间书签。
- 媒体丢失后重新确认 playbackStatus，保留可读文本证据。
- 从历史回答选择候选后刷新媒体状态，不改写原消息的选择理由。

选择理由属于具体问题／消息，不属于永久 VideoChunk。get 返回事实 Evidence；问题对应理由通过 messages/get/list 获取。签名 URL 更新不能改变片段 ID、原视频时间轴或已保存回答。

## 9. 视频与图片公开访问

### 9.1 视频 HTTP Range

`GET /api/videos/content?workspaceId=...&videoId=...&variant=playback`：

- variant 可选 playback／original，默认 playback。
- playback 返回浏览器兼容的视频媒体；MOV／AVI 等原文件不能直接假定可被所有目标浏览器播放。
- 服务端可以生成可播放代理，但必须保持原视频的时间原点与时间轴，startMs/endMs 不因转码改变。
- original 返回上传原文件；该选项用于原媒体访问，不是前端可播放性承诺。
- playbackStatus=PREPARING 时返回 409 MEDIA_NOT_READY，MISSING 时返回 410 MEDIA_MISSING，FAILED 时返回具体公开错误。
- Workspace 归属错误／资源不存在为 404；不能读取任意本地文件路径。

二进制响应必须满足：

| 请求情况 | 响应 |
| --- | --- |
| 无 Range | 200，正确 Content-Type、Content-Length |
| 单个有效字节 Range | 206，正确 Content-Range、Content-Length |
| 不可满足的字节 Range | 416，Content-Range: bytes */完整大小 |
| 成功播放 | Accept-Ranges: bytes、Content-Disposition: inline |
| 条件请求 | 稳定 ETag，正确处理 If-Range，不能把旧版本字节拼到新媒体 |

浏览器根据 playbackUrl 或此接口进行 Range 请求；前端不直接访问 originalSourceUri、chunkMediaRef、analysisWindowMediaRef、artifact://、file:// 或 s3://。

播放模式：

- “播放此片段”：定位 startMs，默认在 endMs 停止／循环。
- “核验前后文”：允许播放 analysisWindowStartMs 到 analysisWindowEndMs。
- 切换候选必须重新定位，不能在同一原视频中沿用上一候选的起点。
- 复制时间点由前端生成文字或应用内 URL，包含 seriesId、videoChunkId、atMs；若需还原当时选择理由，额外携带 conversationId/messageId。
- 不新增公网 Share 资源或把原视频路径放入复制内容。

### 9.2 封面与关键帧图片

`GET /api/media/images/content?workspaceId=...&imageId=...`：

- imageId 为后端已登记图片的 opaque ID，不接受任意 URI／服务器路径。
- 返回真实 JPEG／PNG／WebP 等图片、正确 Content-Type、Content-Length 和 ETag；可使用条件缓存 304。
- 检查图片归属；缺失为 410 MEDIA_MISSING，图片正在准备为 409 MEDIA_NOT_READY。
- ImageAsset.url 和 Keyframe.url 必须是浏览器可直接读取的公开 HTTP 地址，不只是“返回一个内部 keyframeUris 数组”。
- expiresAt 是 URL 到期时间；本地稳定地址为 null。到期刷新资源 DTO，不由前端猜签名参数。
- 前端图片／video 的 onerror 必须转换成媒体错误状态，允许刷新证据或查看对应任务，不静默保留看似有效的占位画面。

## 10. 设置、服务、模型、索引与上传能力

### 10.1 Schema

~~~ts
type ServiceState = "CHECKING" | "READY" | "DEGRADED" | "UNAVAILABLE";
interface DependencyStatus {
  state: ServiceState;
  checkedAt: UnixTimeMs | null;
  lastError: Failure | null;
}
interface ModelStatus extends DependencyStatus {
  provider: string | null;
  modelName: string | null;
}
interface IndexChannelStatus extends DependencyStatus {
  indexedChunkCount: number;
  pendingChunkCount: number;
}
interface HealthView {
  status: "ok";                  // 仅表示 API 进程可接受请求
  serverTime: UnixTimeMs;
}
interface SystemStatusView {
  workspaceId: string;
  overallState: ServiceState;
  services: {
    api: DependencyStatus;
    mysql: DependencyStatus;
    qdrant: DependencyStatus;
    worker: DependencyStatus;
    mediaStorage: DependencyStatus;
  };
  models: {
    query: ModelStatus;
    evaluator: ModelStatus;
    chat: ModelStatus;
    embedding: ModelStatus;
    vision: ModelStatus;
  };
  index: {
    text: IndexChannelStatus;
    vision: IndexChannelStatus;
    readyVideoCount: number;
    pendingVideoCount: number;
    lastUpdatedAt: UnixTimeMs | null;
  };
  capabilities: {
    maximumParallelFiles: number;     // 本 UI 目标至少支持 3
    maximumParallelSegments: number;  // 本 UI 目标至少支持每文件 3
    maximumUploadSizeBytes: number;
    supportedMediaTypes: string[];
    maximumTopK: number;              // 与真实请求校验一致
  };
  serverTime: UnixTimeMs;
}
interface SettingsView {
  workspaceId: string;
  version: number;
  storage: {
    mediaDirectory: string;           // 展示本地保存位置，不作为媒体读取 URL
    allowedMediaDirectories: string[]; // 后端允许切换的目录，空数组时不可切换
    writable: boolean;
    totalUsedBytes: number;           // 当前 Workspace 的 Seekino 受管媒体／产物用量
    freeBytes: number | null;         // 当前写入目录文件系统剩余容量，未知时 null
  };
  retrieval: {
    defaultTopK: number;
  };
  autoCheckServices: boolean;
  updatedAt: UnixTimeMs;
}
interface SettingsUpdateRequest {
  workspaceId: string;
  expectedVersion: number;
  mediaDirectory?: string;
  defaultTopK?: number;
  autoCheckServices?: boolean;
}
~~~

### 10.2 GET /healthz

进程连接检查，不假装 Worker、模型和索引均可用：

~~~json
{
  "status": "ok",
  "serverTime": 1789437600000
}
~~~

API 无法连接是客户端网络状态；依赖故障使用 system/status 呈现。

### 10.3 GET /api/system/status

Query：workspaceId；返回 SystemStatusView。

- CHECKING 只能在实际检查中出现；尚无结果的 checkedAt 为 null。
- READY 必须有真实检查依据，不能只因配置了模型名称而标为服务在线。
- 查询 Worker 存活、数据库／向量存储、媒体目录、实际模型可达性与索引事实，不能由前端静态填“已连接”。
- 缺少某服务或模型的失败应体现在该项 state/lastError，不令其他状态丢失。
- overallState 不能以 /healthz 成功代替所有依赖健康；前端按相关服务决定上传／检索／播放入口状态。
- 单个查询失败若仍可提供其余项，返回 200 完整状态并标记降级；无法获得 Workspace 状态则返回标准错误。
- 模型只返回公开 provider/modelName 与健康状态，不能返回 API Key、认证信息、私有地址配置或 prompt。
- Text／Vision 索引计数来自当前 Workspace 的事实，不拿另一个环境的 collection 数据冒充。
- capabilities 提供真实当前限制，前端以此校验文件大小、类型、topK，并在 UI 并行上限 3 内调度。
- autoCheckServices=true 时前端打开应用与定期检查；false 时保留手动“重新检查”，不隐藏已发生的请求错误。

### 10.4 GET /api/settings/get

Query：workspaceId；返回 SettingsView。用来展示本地媒体位置、磁盘用量、当前检索偏好和自动检查开关。

日夜主题属于设备 UI 偏好，保存在浏览器本地，不写入 SettingsView，也不要求外观设置 API。

### 10.5 POST /api/settings/update

Body：SettingsUpdateRequest，带 Idempotency-Key；至少提供一个可写设置字段。

~~~json
{
  "workspaceId": "local-default",
  "expectedVersion": 3,
  "defaultTopK": 10,
  "autoCheckServices": true
}
~~~

- 所有字段原子更新，返回完整新 SettingsView；version 递增，updatedAt 为 Unix 毫秒。
- expectedVersion 不匹配返回 VERSION_CONFLICT，前端刷新设置后让用户重新确认；幂等重放原成功请求不受当前版本已推进影响。
- defaultTopK 为 1..maximumTopK 的整数；API 与 UI 不得使用不同上限。
- mediaDirectory 只能选 allowedMediaDirectories 中属于本 Workspace 的可写目录，后端检查真实目录和权限。
- 有活跃上传、合并、建库或存储切换工作时，目录变更返回 409，不能破坏运行中的任务。
- 目录变更默认只改变后续写入位置；既有媒体与图片必须继续可读，公开 URL 不失效。UI 确认时明确说明“仅影响后续写入，不自动搬迁已有文件”。
- 此接口不执行递归迁移、删除旧目录或任意路径操作；需要数据搬迁的产品能力应独立评审，不能由“更改目录”暗中触发。
- 设置保存失败保留表单输入并给出字段级错误，不能在前端先展示成功的新路径。

## 11. 完整 UI 工作流与恢复约定

### 11.1 应用启动与资料库

1. 读取本地主题与当前导航；调用 healthz、system/status、settings/get。
2. 调用 series/list 获取真实书架；选中后调用 series/get。
3. 同步 uploads/list 与 jobs/summary；恢复待上传会话和后台建库指示。
4. 空列表展示引导，错误展示具体错误，不注入样例系列、文件或任务。
5. 再次进入页面／断线恢复时重新读取权威快照，不用陈旧缓存永久显示“API 已连接”。

### 11.2 三文件并行导入

1. 前端接受多选／拖入文件，逐项确认 episodeOrder，计算完整文件 contentSha256；校验与等待队列是本地状态。
2. 每个文件创建独立 UploadSession，并持久化幂等键和会话 ID。
3. 最多 3 个文件发 PUT，每文件最多 3 个分段；每条独立进度、暂停、恢复、取消。
4. 单文件字节完成即调用 complete，释放网络槽位；不等待其他文件。
5. 查询其完成快照，取得 videoId/jobId；合并和解析使用独立阶段。
6. 订阅／同步 Job，失败后显示正确操作：上传失败补缺段，合并失败重试完成，解析最终失败使用 jobs/retry。
7. 刷新浏览器后列出原会话，提示重选文件；不是从 0 新建同一集上传。
8. 取消前端 PUT 只暂停发送；真正停止服务端任务调用对应 cancel 接口。

### 11.3 任务中心

1. list 分页显示全部任务，summary 显示真实总量。
2. 一个全局／系列 SSE 更新任务列表、检查点和 summary。
3. 重跑创建新 Job，以新 jobId 订阅，原失败留在历史。
4. 取消等待最终 CANCELLED，不把关闭页面显示为“已取消”。
5. 断线恢复后重新 list/get/summary，避免漏掉断线期间结束的 Job。

### 11.4 连续 Agent 聊天与证据

1. 根据 Series 范围读取历史 Conversation，用户可以选择历史或新建。
2. 打开 Conversation，分页加载消息，建立 messages 快照 SSE。
3. 发送前保存幂等键与原文；SSE started 建立真实消息 ID。
4. 接收公开阶段、完整审核候选、真实 delta；先展示证据，再以最终完整正文校准。
5. 点选候选、查看“选择理由／原始证据／前后语境”，播放片段或分析窗口。
6. 追问已选片段时提交 reference；普通追问继承服务端历史与上一轮候选。
7. 无结果／澄清／部分结果／降级解析分别有明确提示，不隐藏不确定性。
8. 停止生成调用 messages/cancel；切换页面仅取消传输订阅。
9. 发送结果未知时按原幂等键查询消息对，不能自动使用新键重复问一次。
10. URL 到期／媒体错误时刷新 chunk Evidence；历史理由不由前端重写。

## 12. 验收清单

### 12.1 契约与时间

- [ ] 总表 30 条 UI 路由与后端 OpenAPI、请求／响应类型逐项一致。
- [ ] 所有业务绝对时间均为 Unix 毫秒 integer／null；没有 Unix 秒、字符串数字或日期字符串。
- [ ] 片段、对白、关键帧和 ETA 都有明确相对毫秒类型，不被解析为日历时间。
- [ ] 所有必须字段存在；未知可空值为 null，无数据数组为 []。
- [ ] 列表 Page 的 total／hasMore／nextCursor 正确，任务统计覆盖完整范围。
- [ ] 非成功响应包括 422 参数校验统一 ApiError；前端只按 code 分支。
- [ ] Workspace 归属验证覆盖普通读写、媒体、SSE 与 reference。
- [ ] 幂等重放与并发失败不会产生重复消息、视频、解析任务或取消操作。

### 12.2 素材与并行上传

- [ ] 空系列不展示虚构资料；正常素材使用真实封面和关键帧。
- [ ] 第 7 集就绪且第 8–10 集未完成时仍可查询第 7 集，范围与 partial/warnings 准确。
- [ ] 3 个文件／9 个分段真实并行，互不串行阻塞；第 4 个文件在槽位释放后接入。
- [ ] 两文件时显示两条活跃轨道，三文件时三条；暂停／合并／解析不遮住活跃上传。
- [ ] 上传、合并和解析的进度分开；总体上传进度按字节加权。
- [ ] 分段乱序、重复、断线重传正确；确认字节不会重复增加。
- [ ] 创建或完成响应丢失后复用原键可恢复；completed 的 staging 清理不倒退进度。
- [ ] 刷新后能恢复上传队列与 jobId，重选原文件后只传缺段。
- [ ] 恢复前验证完整文件 contentSha256；相同文件名和大小的不同内容不能混入原会话，合并后仍须核验完整内容。
- [ ] 真正取消后不再接受分段／合并，释放集数占用，不删除已发布 Video。
- [ ] 文件名、大小、类型、重复集数、重复内容、过期和校验错误逐项显示，其他文件继续工作。

### 12.3 任务中心

- [ ] 任务名称、集数、文件、阶段、进度、检查点、尝试次数和错误均直接来自 DTO。
- [ ] summary 与完整任务范围一致，不随当前页大小改变。
- [ ] SSE 首帧、更新、心跳和最终终态正确，不把轮询文案写成实时 SSE。
- [ ] 失败／取消后重跑创建新 jobId，旧终态与迟到事件不覆盖新任务。
- [ ] 取消最终为 CANCELLED；完成优先的竞态如实返回 SUCCEEDED。
- [ ] 断线期间完成的任务通过快照重新同步，不留在永久“处理中”。

### 12.4 Agent 与证据

- [ ] 清空浏览器缓存后能列出服务端历史，不需要 Agent Session。
- [ ] 同一系列可以有多个聊天，标题可辨认，消息按 sequenceNo 恢复。
- [ ] “这段”和“上次第二段”使用正确候选与结构化上下文，不只继承 role/content。
- [ ] reference 的消息、候选 ID、Conversation 和 Workspace 归属验证正确。
- [ ] 每候选理由针对当前问题，低序候选说明不足，不靠第一条对白或排名模板充当理由。
- [ ] delta 在真实 Provider 生成过程中到达，最终 GET 与 completed 正文一致。
- [ ] 传输中断与业务失败区分；部分 PROCESSING 快照不抹掉已收到正文。
- [ ] 无结果、澄清和部分结果不冒充上轮完整命中，不伪造概率或私有思维链。
- [ ] 取消后迟到模型结果不能改成 COMPLETED，其他 Conversation 不受影响。

### 12.5 媒体与设置

- [ ] 视频 Range、错误 MIME、不可满足范围、媒体准备／丢失及 Workspace 错误均正确处理。
- [ ] 片段播放与前后文播放可分别定位，同视频候选切换不会沿用旧起点。
- [ ] 封面／关键帧是公开 HTTP 图片；到期可刷新，不能直读内部 artifact URI。
- [ ] 时间书签可恢复同一片段和必要的历史理由，不暴露服务器路径。
- [ ] Worker、模型、索引和存储状态来自实际检查，不以 healthz 代替。
- [ ] 文件大小、类型、并发与 topK 的 UI 校验使用公开能力，与后端一致。
- [ ] 保存设置按版本检查，失败不显示成功；目录切换不损坏既有媒体。
- [ ] 日夜主题真实切换并在本地保存，不依赖后端。

## 13. Review 与实现交接

这份文档是 UI 对接口的完整预期。Review 应确认路由、字段、时间单位、状态、取消／重跑语义与安全边界，再按此实现和生成 OpenAPI／前端类型。

前后端实现位置仅作交接参考：

- `server/router/`：路由注册。
- `server/api/`：HTTP handler 与 schema。
- `web/src/api/`：生成／维护类型与请求。
- `web/src/features/`：页面状态、上传调度、聊天与证据交互。

不允许通过硬编码数据、删除功能入口、猜字段或解析错误文本掩盖接口不一致。本次文档更新不表示以上接口、时间转换或完整 UI 行为已经在代码中实现。
