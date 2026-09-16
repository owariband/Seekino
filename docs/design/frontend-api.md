# AniVAG 前端 API 对接文档

> 状态：Upload-to-Search M1 与 Agent Chat 基础接口已实现；真实模型聊天联调与异常恢复仍待验收/补齐
>
> 更新时间：2026-09-15
>
> 面向对象：WebUI、前端状态管理与联调开发
>
> 实现来源：`server/router/`、`server/api/`、`server/api/schema.py`、`server/schema.py`

本文描述当前代码已经提供的 HTTP 契约，覆盖 Series 管理、视频分片上传与断点续传、
解析任务进度、视频播放、正式检索及 Workspace 下的 Agent Conversation / Message。
运行中的 FastAPI OpenAPI 是字段级最终事实源；接口已实现不等于真实模型效果已验收。

创建、列表和详情接口在路径中
显式使用 `create`、`list`、`get`，不依靠 HTTP Method 隐含操作名称；用于定位路由资源的
ID 统一通过 camelCase Query 参数传递，不放在 URL Path 中。检索请求中的 `seriesIds` 是
业务 Scope 集合，随 `/api/search` 或每次 Agent `messages/send` 的 JSON Body 传递。

聊天联调入口、消息类型、模型名与 SSE 规则见本文第 12 节；逐接口示例也可参阅
[Agent API 对接文档](agent-api.md)。本文不新增接口，全部路径与字段均对应当前源码。

- Swagger UI：`http://127.0.0.1:9621/docs`
- OpenAPI JSON：`http://127.0.0.1:9621/openapi.json`
- 默认 API Base URL：`http://127.0.0.1:9621`

## 1. 通用约定

### 1.1 数据格式

- 普通请求与响应使用 JSON，字段统一为 `camelCase`。
- ID 为规范 UUID 字符串。
- 时间为 ISO 8601 字符串；前端不要自行假设展示时区。
- `workspaceId` 当前默认是 `local-default`。
- 上传分片的 Body 是原始二进制，不是 `multipart/form-data`。
- 当前 M1 没有登录认证；`workspaceId` 是数据作用域，不是安全认证凭证。

### 1.2 环境与 CORS

开发环境默认允许：

```text
http://127.0.0.1:5173
http://localhost:5173
```

允许的方法：`GET`、`POST`、`PUT`、`OPTIONS`。

上传相关允许的自定义 Header：

```text
Content-Type
Content-Range
Idempotency-Key
X-Content-SHA256
```

### 1.3 统一领域错误

后端主动识别的领域错误使用：

```json
{
  "code": "CONFLICT",
  "message": "upload cannot complete with missing segments: 2, 3"
}
```

| HTTP 状态 | `code` | 前端含义 |
| --- | --- | --- |
| `400` | `INVALID_REQUEST` | 参数、UUID、Content-Range 或业务输入格式无效 |
| `404` | `NOT_FOUND` | 资源不存在，或不属于指定 `workspaceId` |
| `409` | `CONFLICT` | 幂等冲突、分片范围/总大小与会话不符、缺片、过期、Series 未 READY、索引不一致，或 Conversation 已有处理中消息 |
| `422` | FastAPI 默认格式 | JSON 字段、Header 或 Query 缺失或不满足字段约束，未进入领域服务 |
| `500` | 服务默认错误 | 未预期的服务异常；前端应保留请求上下文并展示可重试提示 |
| `502` | `AGENT_MODEL_ERROR` | Query/Evaluator/Chat 模型调用、输出或执行预算失败，最多 3 轮仍未通过审核，或最终证据 ID/时间/Scope 校验失败 |

不要依赖英文 `message` 做业务分支；当前稳定分支依据是 HTTP 状态和 `code`。

## 2. 接口总览

| 模块 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| Health | `GET` | `/healthz` | API 进程存活检查 |
| Series | `POST` | `/api/series/create` | 创建或幂等取得 Series |
| Series | `GET` | `/api/series/list?workspaceId=...` | Series 列表 |
| Series | `GET` | `/api/series/get?seriesId=...&workspaceId=...` | Series 及其 Video 详情 |
| Upload | `POST` | `/api/uploads/create?seriesId=...` | 创建上传会话 |
| Upload | `GET` | `/api/uploads/get?uploadSessionId=...&workspaceId=...` | 查询确认进度和恢复点 |
| Upload | `PUT` | `/api/uploads/segments/upload?uploadSessionId=...&segmentNumber=...&workspaceId=...` | 上传一个分片 |
| Upload | `POST` | `/api/uploads/complete?uploadSessionId=...&workspaceId=...` | 完成上传并创建解析任务 |
| Upload | `POST` | `/api/uploads/delete?uploadSessionId=...&workspaceId=...` | 删除整个 UploadSessionTask；上传中、等待解析、解析失败和已完成均可删，合并中或正在解析返回 409 |
| Upload | `POST` | `/api/uploads/retry-parse?uploadSessionId=...&workspaceId=...&expectedJobVersion=...` | 将该 UploadSession 的 FAILED 主解析任务重新排队；保留检查点和执行历史，以 Job version 防重复提交 |
| Job | `GET` | `/api/jobs/list?workspaceId=...&limit=100` | 查询任务列表 |
| Job | `GET` | `/api/jobs/get?jobId=...&workspaceId=...` | 查询任务真实状态 |
| Job | `GET` | `/api/jobs/events?jobId=...&workspaceId=...` | SSE 订阅任务变化 |
| Media | `GET` | `/api/videos/content?videoId=...&workspaceId=...` | 播放 Original Video，支持 Range |
| Search | `POST` | `/api/search` | Series Scope 内双路检索并返回完整 Evidence |
| Agent Conversation | `POST` | `/api/agent/conversations/create` | 以 JSON workspaceId/title 创建对话，不绑定动画范围 |
| Agent Conversation | `GET` | `/api/agent/conversations/list?workspaceId=...&limit=100` | Workspace 下的 Conversation 列表 |
| Agent Conversation | `GET` | `/api/agent/conversations/get?conversationId=...&workspaceId=...` | Conversation 详情 |
| Agent Message | `GET` | `/api/agent/messages/list?conversationId=...&workspaceId=...&afterSequenceNo=-1&limit=200` | 按序查询持久化消息，包含 `modelName` 和完整结果 |
| Agent Message | `POST` | `/api/agent/messages/send?conversationId=...` | Query/Evaluator 最多 3 轮，通过后由 Chat 交付；按 Accept 返回 JSON 或实时正文 SSE |
| Agent Message | `GET` | `/api/agent/messages/events?conversationId=...&workspaceId=...` | SSE 订阅消息状态快照，不是 token 流 |

共 19 个路径。不存在 `/api/agent/sessions/*` 或 `/api/agent/turns`；内部 Retrieval Tool 直接调用检索 Service，
不把 Agent 工具调用绕成对自身 HTTP API 的请求。

## 3. Series API

### 3.1 创建 Series

```http
POST /api/series/create
Content-Type: application/json
```

请求：

```json
{
  "workspaceId": "local-default",
  "name": "BanG Dream! It's MyGO!!!!!",
  "description": "动画正片",
  "importantExtraData": {
    "seriesBible": "可选的作品背景信息"
  },
  "characterBible": [
    {
      "name": "长崎爽世",
      "aliases": ["爽世"]
    }
  ]
}
```

约束：

- `workspaceId`：可省略，默认 `local-default`，最大 64 字符。
- `name`：必填，1～255 字符。
- `description`、`importantExtraData` 可为空。
- `characterBible` 可省略，默认空数组。
- Series ID 由 `workspaceId + 规范化名称` 稳定生成。
- 完全相同的创建请求是幂等的；同名但字段不同返回 `409`。

响应 `201`：

```json
{
  "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
  "workspaceId": "local-default",
  "name": "BanG Dream! It's MyGO!!!!!",
  "description": "动画正片",
  "status": "EMPTY",
  "importantExtraData": {
    "seriesBible": "可选的作品背景信息"
  },
  "characterBible": [
    {
      "name": "长崎爽世",
      "aliases": ["爽世"]
    }
  ],
  "videoCount": 0,
  "videos": []
}
```

### 3.2 获取 Series 列表

```http
GET /api/series/list?workspaceId=local-default
```

返回 `SeriesView[]`。列表接口提供 `videoCount`，当前不会展开每个 Series 的 Video；
需要 Video 详情时调用单个 Series 接口。

Agent 搜索页复用该接口的必填 `seriesId/name/status` 提供动画多选，未 READY 项展示状态并
禁用。选中项随下一次 `messages/send.seriesIds` 传递；全部取消选择表示当前 Workspace
的全部 READY 动画，由后端在发送时展开，无须新增只返回 ID 的列表接口。

### 3.3 获取 Series 详情

```http
GET /api/series/get?seriesId={seriesId}&workspaceId=local-default
```

响应中的 `videos` 按 `episodeOrder` 升序排列：

```json
{
  "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
  "workspaceId": "local-default",
  "name": "BanG Dream! It's MyGO!!!!!",
  "description": "动画正片",
  "status": "INDEXING",
  "importantExtraData": null,
  "characterBible": [],
  "videoCount": 1,
  "videos": [
    {
      "videoId": "2a150753-57ae-58c3-a996-d552734bc640",
      "episodeOrder": 7,
      "uploadSequence": 7,
      "originalFilename": "episode-07.mp4",
      "originalSourceUri": "artifact://development/media/.../episode-07.mp4",
      "inferenceProxyUri": null,
      "contentHash": "64-character-sha256",
      "totalBytes": 2147483648,
      "durationMs": 1435520,
      "uploadStatus": "COMPLETED",
      "parseStatus": "RUNNING",
      "uploadProgress": 100,
      "parseProgress": 42
    }
  ]
}
```

Series 状态：

| 状态 | 含义 | UI 建议 |
| --- | --- | --- |
| `EMPTY` | 尚无已完成上传的 Video | 展示上传入口 |
| `INDEXING` | 至少一个 Video 正在解析或索引未收敛 | 展示任务进度，禁用正式检索 |
| `READY` | 全部 Video 已解析，全部最新索引已同步 | 开放正式检索 |

## 4. 视频分片上传与断点续传

### 4.1 上传状态机

```text
UPLOADING
   │
   ├── 上传或重传任意缺失分片
   │
   ├── Complete ──> COMPLETING ──> COMPLETED
   │                    │
   │                    └── 完成失败后回到 UPLOADING
   │
   └── 超过 expiresAt ──> EXPIRED
```

| 状态 | 含义 | 是否还能 PUT 分片 |
| --- | --- | --- |
| `UPLOADING` | 接收分片 | 是 |
| `COMPLETING` | 后端正在合并、校验和探测视频 | 否；稍后查询或重试 Complete |
| `COMPLETED` | Video 与 ParseJob 已创建 | 否；Complete 可幂等重试 |
| `EXPIRED` | Session 已过期 | 否；使用新幂等键创建新 Session |

### 4.2 创建 UploadSession

```http
POST /api/uploads/create?seriesId={seriesId}
Idempotency-Key: <frontend-generated-stable-key>
Content-Type: application/json
```

请求：

```json
{
  "workspaceId": "local-default",
  "episodeOrder": 7,
  "uploadSequence": 7,
  "originalFilename": "episode-07.mp4",
  "mediaType": "video/mp4",
  "totalSizeBytes": 2147483648
}
```

字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `workspaceId` | 否 | 默认 `local-default` |
| `episodeOrder` | 是 | 正整数；Series 内集序唯一 |
| `uploadSequence` | 否 | 正整数；省略时等于 `episodeOrder` |
| `originalFilename` | 是 | 只允许文件名，不允许目录路径 |
| `mediaType` | 是 | `video/mp4`、`video/quicktime` 或 `video/x-msvideo` |
| `totalSizeBytes` | 是 | 完整文件大小；当前默认上限 10 GiB |

`Idempotency-Key` 必须由前端在第一次请求前生成并持久化。推荐 UUID：

```ts
const idempotencyKey = crypto.randomUUID();
```

如果创建请求超时或响应丢失，以同一个 Key 和完全相同的请求字段重试，会返回同一个
UploadSession。相同 Key 搭配不同文件信息会返回 `409`。

响应 `201`：

```json
{
  "uploadSessionId": "8b6ce5d9-d1bf-4b04-b201-4f16cae46cef",
  "workspaceId": "local-default",
  "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
  "episodeOrder": 7,
  "uploadSequence": 7,
  "originalFilename": "episode-07.mp4",
  "mediaType": "video/mp4",
  "totalSizeBytes": 2147483648,
  "preferredSegmentSizeBytes": 8388608,
  "totalSegments": 256,
  "status": "UPLOADING",
  "uploadedSegmentCount": 0,
  "uploadedSizeBytes": 0,
  "missingSegmentNumbers": [0, 1, 2],
  "expiresAt": "2026-09-15T10:00:00",
  "videoId": null,
  "lastError": null,
  "createdAt": "2026-09-14T10:00:00",
  "updatedAt": "2026-09-14T10:00:00"
}
```

示例里的 `missingSegmentNumbers` 为节省篇幅省略了后续编号；真实响应会包含全部缺片。

前端不得写死 8 MiB，必须使用响应中的 `preferredSegmentSizeBytes`。

### 4.3 查询上传状态和恢复点

```http
GET /api/uploads/get?uploadSessionId={uploadSessionId}&workspaceId=local-default
```

返回完整 `UploadSessionView`。断点恢复只认以下服务端字段：

```json
{
  "status": "UPLOADING",
  "uploadedSegmentCount": 83,
  "uploadedSizeBytes": 696254464,
  "totalSizeBytes": 2147483648,
  "missingSegmentNumbers": [12, 84, 85, 86]
}
```

恢复规则：

1. 读取本地持久化的 `uploadSessionId`。
2. 调用 GET 获取 MySQL 已确认的状态。
3. 只上传 `missingSegmentNumbers` 中的分片。
4. 不以浏览器内存中的“已完成列表”作为最终事实。
5. 如果状态为 `COMPLETED`，直接使用 `videoId`，无需重新上传。
6. 如果状态为 `EXPIRED`，创建新 Session；新请求必须使用新的 `Idempotency-Key`。

浏览器刷新后通常无法自动恢复原始 `File` 对象。前端需要采用以下方案之一：

- 让用户重新选择同一个文件，并至少核对文件名和 `size`；
- 使用 File System Access API 持久化授权句柄；
- 桌面壳环境持久化本地文件路径或句柄。

### 4.4 上传一个分片

```http
PUT /api/uploads/segments/upload?uploadSessionId={uploadSessionId}&segmentNumber={segmentNumber}&workspaceId=local-default
Content-Type: application/octet-stream
Content-Range: bytes <start>-<inclusiveEnd>/<totalSize>
X-Content-SHA256: <segment-sha256>

<raw binary body>
```

分片编号从 `0` 开始。边界公式：

Content-Range 语法与 Body 长度由 API 校验；Header 中的总大小和
字节范围是否匹配 UploadSession，由 Service 读取会话后校验，不匹配返回 `409 CONFLICT`。

```ts
const segmentSize = session.preferredSegmentSizeBytes;
const start = segmentNumber * segmentSize;
const endExclusive = Math.min(start + segmentSize, file.size);
const body = file.slice(start, endExclusive);

// Content-Range 的 end 是闭区间。
const contentRange = `bytes ${start}-${endExclusive - 1}/${file.size}`;
```

计算分片 SHA-256：

```ts
async function sha256(blob: Blob): Promise<string> {
  const bytes = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}
```

上传示例：

```ts
const checksum = await sha256(body);
const response = await fetch(
  `${apiBase}/api/uploads/segments/upload` +
    `?uploadSessionId=${encodeURIComponent(session.uploadSessionId)}` +
    `&segmentNumber=${segmentNumber}` +
    `&workspaceId=${encodeURIComponent(workspaceId)}`,
  {
    method: "PUT",
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Range": contentRange,
      "X-Content-SHA256": checksum,
    },
    body,
  },
);
```

响应 `200`：

```json
{
  "uploadSessionId": "8b6ce5d9-d1bf-4b04-b201-4f16cae46cef",
  "segmentNumber": 0,
  "startByte": 0,
  "endByteExclusive": 8388608,
  "actualSizeBytes": 8388608,
  "checksumSha256": "98c5..."
}
```

分片语义：

- 支持乱序上传和前端有限并发。
- 除最后一片外，分片必须严格等于服务端指定大小。
- 后端校验分片编号、范围、实际字节数和 SHA-256。
- 相同编号、范围、内容和 Hash 重传是幂等成功。
- 相同分片编号换成不同内容返回 `409`。
- PUT 请求结果不确定时，先 GET Session；若编号仍在 `missingSegmentNumbers` 中再重传。

建议同一文件使用 3～4 个并发上传 Worker，不建议一次启动数百个请求。

### 4.5 完成上传

```http
POST /api/uploads/complete?uploadSessionId={uploadSessionId}&workspaceId=local-default
```

没有请求体。后端依次执行：

1. 检查所有分片是否齐全；
2. 流式读取并重新验证每片大小和 SHA-256；
3. 按编号合并；
4. 计算完整文件 SHA-256；
5. 发布 Original Video；
6. 使用 `ffprobe` 验证视频并获取时长；
7. 在一个 MySQL 事务中创建 `Video + SeriesParseJob`；
8. 将 Series 置为 `INDEXING`。

响应 `200`：

```json
{
  "uploadSession": {
    "uploadSessionId": "8b6ce5d9-d1bf-4b04-b201-4f16cae46cef",
    "workspaceId": "local-default",
    "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
    "episodeOrder": 7,
    "uploadSequence": 7,
    "originalFilename": "episode-07.mp4",
    "mediaType": "video/mp4",
    "totalSizeBytes": 2147483648,
    "preferredSegmentSizeBytes": 8388608,
    "totalSegments": 256,
    "status": "COMPLETED",
    "uploadedSegmentCount": 256,
    "uploadedSizeBytes": 2147483648,
    "missingSegmentNumbers": [],
    "expiresAt": "2026-09-15T10:00:00",
    "videoId": "2a150753-57ae-58c3-a996-d552734bc640",
    "lastError": null,
    "createdAt": "2026-09-14T10:00:00",
    "updatedAt": "2026-09-14T10:20:00"
  },
  "videoId": "2a150753-57ae-58c3-a996-d552734bc640",
  "jobId": "72d1f97d-7f11-57da-a245-f51442672580"
}
```

Complete 是幂等的。成功响应丢失后，用同一个 `uploadSessionId` 重试，会返回相同的
`videoId` 和 `jobId`。

常见冲突：

| 场景 | 响应 | 前端处理 |
| --- | --- | --- |
| 还有缺片 | `409 CONFLICT` | GET Session 后继续上传缺片 |
| 另一个 Complete 正在执行 | `409 CONFLICT` | 延迟后 GET；不要立即高频重试 |
| Session 过期 | `409 CONFLICT` | 使用新幂等键重新创建 |
| 同一集已有另一 UploadSession 或 Video | `409 CONFLICT` | 回到 Series 详情确认资产 |
| 视频格式无效或 `ffprobe` 失败 | `500` | GET Session 读取 `lastError`，提示重新选择文件或重试 Complete |

### 4.6 前端确认进度

服务端确认进度：

```ts
const confirmedRatio =
  session.uploadedSizeBytes / session.totalSizeBytes;
```

服务端只在一个完整分片通过校验后记录它。要展示正在发送的分片内部进度，可使用
XHR 或支持 `onUploadProgress` 的客户端库；原生 `fetch` 当前没有通用的上传进度事件。

UI 建议区分：

- `confirmedBytes`：来自 GET Session；
- `inFlightBytes`：当前请求的临时进度；
- 页面刷新后只恢复 `confirmedBytes`。

### 4.7 推荐的断点续传伪代码

```ts
async function resumeUpload(file: File, session: UploadSessionView) {
  const latest = await getUploadSession(session.uploadSessionId);

  if (latest.status === "COMPLETED") {
    return { videoId: latest.videoId };
  }
  if (latest.status === "EXPIRED") {
    throw new Error("UPLOAD_SESSION_EXPIRED");
  }
  if (latest.totalSizeBytes !== file.size) {
    throw new Error("SELECTED_FILE_SIZE_MISMATCH");
  }

  const pending = [...latest.missingSegmentNumbers];
  const concurrency = Math.min(4, pending.length);
  await Promise.all(
    Array.from({ length: concurrency }, async () => {
      while (pending.length > 0) {
        const segmentNumber = pending.shift();
        if (segmentNumber === undefined) return;
        await uploadOneSegment(file, latest, segmentNumber);
      }
    }),
  );

  const verified = await getUploadSession(latest.uploadSessionId);
  if (verified.missingSegmentNumbers.length > 0) {
    throw new Error("UPLOAD_NOT_YET_COMPLETE");
  }
  return completeUpload(latest.uploadSessionId);
}
```

## 5. ParseJob API

### 5.1 查询单个 Job

```http
GET /api/jobs/get?jobId={jobId}&workspaceId=local-default
```

响应 `200`：

```json
{
  "jobId": "72d1f97d-7f11-57da-a245-f51442672580",
  "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
  "videoId": "2a150753-57ae-58c3-a996-d552734bc640",
  "seriesName": "BanG Dream! It's MyGO!!!!!",
  "episodeOrder": 7,
  "originalFilename": "episode-07.mp4",
  "pipelineVersion": "m1",
  "status": "RUNNING",
  "stage": "PARSING",
  "progress": 37,
  "attemptCount": 1,
  "maxAttempts": 3,
  "availableAt": "2026-09-14T10:20:00",
  "leaseExpiresAt": "2026-09-14T10:26:00",
  "startedAt": "2026-09-14T10:21:00",
  "completedAt": null,
  "lastError": null,
  "checkpoints": [
    {
      "sequenceNo": 0,
      "videoChunkId": "75f5...",
      "stage": "INDEXED",
      "chunkMediaUri": "artifact://development/media/.../chunk-0000.mp4",
      "analysisWindowMediaUri": "artifact://development/media/.../window-0000.mp4",
      "lastError": null
    },
    {
      "sequenceNo": 1,
      "videoChunkId": "d9a2...",
      "stage": "PARSED",
      "chunkMediaUri": "artifact://development/media/.../chunk-0001.mp4",
      "analysisWindowMediaUri": "artifact://development/media/.../window-0001.mp4",
      "lastError": null
    }
  ],
  "createdAt": "2026-09-14T10:20:00",
  "updatedAt": "2026-09-14T10:23:00"
}
```

Job 状态：

| 状态 | 是否终态 | UI 含义 |
| --- | --- | --- |
| `QUEUED` | 否 | 等待 Worker |
| `RUNNING` | 否 | 正在处理 |
| `RETRY_WAIT` | 否 | 暂时失败，等待自动重试；展示 `lastError` 和尝试次数 |
| `SUCCEEDED` | 是 | 完成 |
| `FAILED` | 是 | 已用尽重试次数 |

Job 阶段顺序：

```text
QUEUED
→ PREPARING
→ SPLIT_READY
→ PARSING
→ TEXT_EMBEDDING
→ VISION_EMBEDDING
→ PERSISTING
→ INDEXING
→ COMPLETED
```

VideoChunk checkpoint 顺序：

```text
PREPARED
→ SPLIT_READY
→ PARSED
→ TEXT_EMBEDDED
→ VISION_EMBEDDED
→ INDEXED
```

### 5.2 查询 Job 列表

```http
GET /api/jobs/list?workspaceId=local-default&limit=100
```

- `limit` 默认 100，范围 1～500。
- 返回 `ParseJobView[]`，按创建时间倒序。
- `seriesName`、`episodeOrder`、`originalFilename` 已由服务端联表补齐，前端不需要再和 Series 详情做 Join。
- 每个 Job 都带 checkpoint，前端不需要再逐个请求 chunk 进度。

### 5.3 SSE 订阅 Job 变化

```http
GET /api/jobs/events?jobId={jobId}&workspaceId=local-default
Accept: text/event-stream
```

服务端事件：

```text
event: job
data: {"jobId":"...","status":"RUNNING","stage":"PARSING",...}

```

前端接入：

```ts
const url =
  `${apiBase}/api/jobs/events?jobId=${encodeURIComponent(jobId)}` +
  `&workspaceId=${encodeURIComponent(workspaceId)}`;
const source = new EventSource(url);

source.addEventListener("job", (event) => {
  const job = JSON.parse((event as MessageEvent).data) as ParseJobView;
  jobStore.upsert(job);
  if (job.status === "SUCCEEDED" || job.status === "FAILED") {
    source.close();
  }
});

source.onerror = async () => {
  // EventSource 会自行重连；同时允许用 GET 恢复 MySQL 真实状态。
  const current = await getJob(jobId);
  jobStore.upsert(current);
};
```

SSE 契约：

- 连接后先发送当前 Job 快照。
- 只在阶段、进度、checkpoint、错误等 UI 状态变化时发送新 `job` 事件。
- Worker 的纯 Heartbeat 不产生 UI 进度事件。
- 约 15 秒无变化时发送 SSE 注释作为 keep-alive，前端无需处理。
- Job 进入 `SUCCEEDED` 或 `FAILED` 后发送最终快照并关闭流。
- SSE 只是通知；断线恢复必须调用 GET，以 MySQL 返回为准。

## 6. Original Video 播放

```http
GET /api/videos/content?videoId={videoId}&workspaceId=local-default
Range: bytes=<start>-<end>
```

后端支持标准 HTTP Range，浏览器 `<video>` 会自动发起 Range 请求：

```tsx
<video
  controls
  preload="metadata"
  src={`${apiBase}/api/videos/content?videoId=${encodeURIComponent(videoId)}` +
    `&workspaceId=${encodeURIComponent(workspaceId)}`}
/>
```

从检索结果跳转到命中时间：

```ts
videoElement.currentTime = evidence.startMs / 1000;
await videoElement.play();
```

前端不要直接解析或访问 `artifact://...`；播放入口始终使用 HTTP Video Content API。

## 7. 正式检索 API

### 7.1 发起检索

```http
POST /api/search
Content-Type: application/json
```

请求：

```json
{
  "workspaceId": "local-default",
  "query": "爽世为什么哭着质问大家要演奏春日影",
  "seriesIds": [
    "77285f16-3e5b-592e-ba15-8b224b619806"
  ],
  "topK": 10
}
```

约束：

- `query` 必填，不能只包含空白。
- `seriesIds` 至少一个；检索只在这个 Scope 内执行，不做全库召回。
- 所有 Series 必须属于指定 workspace 且状态为 `READY`。
- `topK` 默认 10，范围 1～100。
- Series 未 READY 或 Qdrant/MySQL 不一致时返回 `409`，不会静默缩小 Scope。

这是基础 `/api/search` 的非空 Scope 契约；Agent `messages/send` 允许省略 / 空数组，
由 AgentChatService 展开为全部 READY 动画后调用同一检索 Service。

### 7.2 检索响应

```json
{
  "query": "爽世为什么哭着质问大家要演奏春日影",
  "seriesIds": [
    "77285f16-3e5b-592e-ba15-8b224b619806"
  ],
  "agentReply": {
    "message": "我保留了 1 段备选。第一段同时进入文本与视觉召回，可联合核验对白、画面事件与上下文。",
    "resultCount": 1
  },
  "hits": [
    {
      "candidate": {
        "videoChunkId": "75f5...",
        "fusedRank": 1,
        "fusedScore": 0.0325,
        "textRank": 1,
        "textScore": 0.83,
        "visionRank": 2,
        "visionScore": 0.79
      },
      "selectionReason": {
        "summary": "该片段同时进入文本与视觉召回，并在融合排序中位列第 1；保留它用于联合核验对白、画面事件与上下文。",
        "matchedEvidence": [
          {
            "kind": "TRANSCRIPT",
            "text": "为什么要弹《春日影》？",
            "startMs": 1252000,
            "endMs": 1259000
          },
          {
            "kind": "VISUAL_EVENT",
            "text": "爽世眼含泪水冲向成员并质问。",
            "startMs": 1252000,
            "endMs": 1259000
          }
        ],
        "limitations": []
      },
      "evidence": {
        "seriesId": "77285f16-3e5b-592e-ba15-8b224b619806",
        "workspaceId": "local-default",
        "seriesName": "BanG Dream! It's MyGO!!!!!",
        "seriesDescription": "动画正片",
        "seriesStatus": "READY",
        "importantExtraData": null,
        "characterBible": [],
        "videoId": "2a150753-57ae-58c3-a996-d552734bc640",
        "episodeOrder": 7,
        "originalFilename": "episode-07.mp4",
        "originalSourceUri": "artifact://development/media/.../episode-07.mp4",
        "inferenceProxyUri": null,
        "contentHash": "64-character-sha256",
        "durationMs": 1435520,
        "uploadStatus": "COMPLETED",
        "parseStatus": "SUCCEEDED",
        "videoChunkId": "75f5...",
        "sequenceNo": 20,
        "startMs": 1200000,
        "endMs": 1260000,
        "analysisWindowStartMs": 1197000,
        "analysisWindowEndMs": 1263000,
        "chunkMediaRef": "artifact://development/media/.../chunk-0020.mp4",
        "analysisWindowMediaRef": "artifact://development/media/.../window-0020.mp4",
        "transcript": [
          {
            "speaker": null,
            "text": "为什么要弹《春日影》？",
            "startMs": 1252000,
            "endMs": 1259000
          }
        ],
        "baselineVisualCaption": null,
        "keyframeUris": [],
        "allTimeEvents": [],
        "allTimeEventText": "该 VideoChunk 自有事件文本",
        "retrievalText": "生成 Text vector 时使用的完整检索正文",
        "videoCompact": "该 VideoChunk 的紧凑事实摘要",
        "contextBeforeCompact": "前文上下文",
        "contextAfterCompact": "更新后的上下文",
        "windowSummary": "当前解析窗口摘要",
        "modelProvider": "doubao",
        "modelName": "ep-model",
        "pipelineVersion": "m1",
        "isFallback": false,
        "parseProvenance": null,
        "parsedAt": "2026-09-14T10:35:00"
      }
    }
  ]
}
```

数据语义：

- `candidate` 是 Qdrant Text/Vision 双路召回和 RRF 的排序信息。
- `/api/search` 的 `agentReply` 是确定性生成的检索摘要，`resultCount` 始终等于 `hits.length`；它不是 Chat Agent 回答或持久化对话记录。
- 当前 `selectionReason` 是根据通道排名和候选 Evidence 生成的公开摘要，不是模型按 Query 作出的逐候选审核结论，也不包含私有思维链。Agent 选择候选后沿用该字段，前端不要自行编造额外理由。
- `evidence` 是按照 `videoChunkId` 从 MySQL 一次联表 Hydrate 的完整事实。
- `retrievalText` 是生成该 Text vector 时实际使用的完整文本，可供上层回答与分析能力使用。
- Vision vector 绑定的是无重叠 canonical VideoChunk MP4，不是 caption。
- 响应不返回原始 Text/Vision 浮点向量。
- 播放使用 `videoId + startMs/endMs`，不要从模型正文反解析时间。

## 8. 建议前端类型

以下类型是便于对接的摘录；精确约束仍以 OpenAPI 为准。

```ts
type SeriesStatus = "EMPTY" | "INDEXING" | "READY";
type UploadStatus = "UPLOADING" | "COMPLETING" | "COMPLETED" | "EXPIRED";
type ParseJobStatus =
  | "QUEUED"
  | "RUNNING"
  | "RETRY_WAIT"
  | "SUCCEEDED"
  | "FAILED";

interface UploadSessionView {
  uploadSessionId: string;
  workspaceId: string;
  seriesId: string;
  episodeOrder: number;
  uploadSequence: number;
  originalFilename: string;
  mediaType: string;
  totalSizeBytes: number;
  preferredSegmentSizeBytes: number;
  totalSegments: number;
  status: UploadStatus;
  uploadedSegmentCount: number;
  uploadedSizeBytes: number;
  missingSegmentNumbers: number[];
  expiresAt: string;
  videoId: string | null;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

interface VideoChunkCheckpointView {
  sequenceNo: number;
  videoChunkId: string;
  stage:
    | "PREPARED"
    | "SPLIT_READY"
    | "PARSED"
    | "TEXT_EMBEDDED"
    | "VISION_EMBEDDED"
    | "INDEXED";
  chunkMediaUri: string | null;
  analysisWindowMediaUri: string | null;
  lastError: string | null;
}

interface ParseJobView {
  jobId: string;
  seriesId: string;
  videoId: string;
  seriesName: string;
  episodeOrder: number;
  originalFilename: string;
  pipelineVersion: string;
  status: ParseJobStatus;
  stage: string;
  progress: number;
  attemptCount: number;
  maxAttempts: number;
  availableAt: string;
  leaseExpiresAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  lastError: string | null;
  checkpoints: VideoChunkCheckpointView[];
  createdAt: string;
  updatedAt: string;
}

interface VideoChunkCandidate {
  videoChunkId: string;
  fusedRank: number;
  fusedScore: number;
  textRank: number | null;
  textScore: number | null;
  visionRank: number | null;
  visionScore: number | null;
}

interface TranscriptCue {
  speaker: string | null;
  text: string;
  startMs: number;
  endMs: number;
}

interface VideoChunkEvidence {
  seriesId: string;
  workspaceId: string;
  seriesName: string;
  seriesDescription: string | null;
  seriesStatus: SeriesStatus;
  importantExtraData: Record<string, unknown> | null;
  characterBible: Array<Record<string, unknown>>;
  videoId: string;
  episodeOrder: number;
  originalFilename: string;
  originalSourceUri: string;
  inferenceProxyUri: string | null;
  contentHash: string;
  durationMs: number;
  uploadStatus: string;
  parseStatus: string;
  videoChunkId: string;
  sequenceNo: number;
  startMs: number;
  endMs: number;
  analysisWindowStartMs: number;
  analysisWindowEndMs: number;
  chunkMediaRef: string;
  analysisWindowMediaRef: string;
  transcript: TranscriptCue[];
  baselineVisualCaption: string | null;
  keyframeUris: string[];
  allTimeEvents: Array<Record<string, unknown>>;
  allTimeEventText: string;
  retrievalText: string;
  videoCompact: string;
  contextBeforeCompact: string;
  contextAfterCompact: string;
  windowSummary: string;
  modelProvider: string | null;
  modelName: string | null;
  pipelineVersion: string;
  isFallback: boolean;
  parseProvenance: Record<string, unknown> | null;
  parsedAt: string;
}

interface MatchedEvidence {
  kind: "TRANSCRIPT" | "VISUAL_EVENT" | "CONTEXT";
  text: string;
  startMs: number | null;
  endMs: number | null;
}

interface SelectionReason {
  summary: string;
  matchedEvidence: MatchedEvidence[];
  limitations: string[];
}

interface AgentReply {
  message: string;
  resultCount: number;
}

interface VideoChunkSearchHit {
  candidate: VideoChunkCandidate;
  selectionReason: SelectionReason;
  evidence: VideoChunkEvidence;
}

interface VideoChunkSearchResult {
  query: string;
  seriesIds: string[];
  agentReply: AgentReply;
  hits: VideoChunkSearchHit[];
}
```

建议前端从 `/openapi.json` 生成基础 HTTP DTO，再在 UI 层对状态枚举做窄化，不要维护
第二套手写且无法校验的完整 DTO。

## 9. 前端重试与状态恢复规则

| 操作 | 是否幂等 | 请求结果不确定时怎么做 |
| --- | --- | --- |
| 创建 Series | 是，字段必须一致 | 原请求重试 |
| 创建 UploadSession | 是，依赖相同 `Idempotency-Key` 和字段 | 使用原 Key 重试 |
| PUT 分片 | 是，编号、范围、Hash 和内容必须一致 | GET Session，缺失时重传 |
| Complete | 是 | GET Session；未完成时重试 Complete |
| Delete UploadSessionTask | 是 | 原 uploadSessionId 重试；成功返回 EXPIRED 墓碑，会话序号与幂等键保留，派生数据库记录、暂存/媒体/Job 文件与向量索引清除；中断时由持久删除标记续做 |
| Retry Parse for UploadSessionTask | 同一 expectedJobVersion 至多投递一次 | 响应不确定时先 GET Job；仍为 FAILED 且 version 未变时才用原版本重试，QUEUED / RUNNING 表示请求已生效 |
| GET Job | 只读 | 直接重试 |
| SSE | 通知流 | 自动重连并用 GET Job 校准 |
| Search | 只读 | 网络错误可重试；`409` 先处理 READY 或一致性问题 |
| 创建 Agent Conversation | 是，依赖原 `Idempotency-Key` 和相同字段 | 保留原 Key 重试，不另建资源 |
| 发送消息 | 是，依赖原 `Idempotency-Key`、内容和有效 `topK` | 先查询消息；原 Key 重试只读回原结果，处理中返回 `409`，不会重跑 FAILED 消息 |
| POST Answer SSE | 同一次幂等发送的实时输出 | 断流后读取消息/GET 快照；已完成重试不重放原增量 |
| GET Message SSE | 状态通知流 | 重连获取完整快照；按 `messageId` 更新同一条消息 |

上传与检索可自动重试网络错误和明确可重试的 `5xx`；`400/404/409/422` 应先根据业务
状态处理。聊天 `502` 应先读取持久化消息：同 Key 不会重新执行，用户明确再次发送时才
生成新 Key。不要把任意 `5xx` 都当作可以无限重跑的请求。

## 10. 当前尚未提供的前端能力

以下接口不在当前 M1 中，前端不要假设已经存在：

- 独立按 Series、Video 或 VideoChunk ID 删除（当前只提供按 UploadSessionTask 的整任务删除）；
- 按任意 jobId 重跑次要 Pipeline Job（当前只允许按 uploadSessionId 重试该 UploadSession 的 FAILED 主任务）；
- 暂停 Worker 解析；
- 登录、用户鉴权和权限管理；
- 直接读取 `artifact://` URI 的接口。
- Agent 消息撤销/取消、Conversation 删除、跨 Worker 增量重放；
- Agent 执行的 Lease/Reaper 或进程崩溃后自动恢复；
- Agent 异常时自动降级为基础召回；独立 `/api/search` 可用不等于已实现自动回退；
- 模型输出的精确 Clip 范围、相邻 VideoChunk 补查和 Query 相关逐候选解释。

上传“暂停”不需要后端状态切换：停止发送分片请求即可；恢复时重新 GET UploadSession，
根据 `missingSegmentNumbers` 继续。

## 11. 本地联调启动方式

准备好开发 MySQL、Qdrant 和 `.env.local` 后：

```bash
uv run --env-file .env.local python -m server.cli init-db
uv run --env-file .env.local python -m server.api
uv run --env-file .env.local python -m server.mq
```

基础检查：

```bash
curl -sS http://127.0.0.1:9621/healthz
```

API 和 Worker 是两个独立进程：只启动 API 可以创建 Series 和上传视频，但 ParseJob 会
保持 `QUEUED`，直到 Worker 启动并成功 Claim。

`server.mq.worker.ParseWorker` 消费的是 MySQL 视频解析任务，不消费 Agent Message。
聊天由 API 进程中的 AgentChatService 执行线程池运行；HTTP 订阅断开不会终止执行，但进程崩溃后的 Agent Run 恢复尚未实现。
`init-db` 只创建缺失表，不会迁移旧 Schema；现有数据库如含旧命名表，应先执行受控迁移，
不要把启动命令当作自动迁移方案。

## 12. Agent Chat 联调

完整请求示例、错误映射及 SSE 契约见 [Agent API 对接文档](agent-api.md)；本节保留前端
联调所需的资源、模型和状态规则。

### 12.1 资源关系与三 Agent 交付

资源关系为 `Workspace → Conversation → Message`。一个 Conversation 是可以连续追问的
完整对话，保存 workspace 和 title；其下按 `sequenceNo` 排列 USER / ASSISTANT 消息。
动画范围随每次发送选择，不保存在 Conversation。同一对话可以先搜索动画 A，再搜索
动画 B；选择变化只影响下一次发送，不能改变正在运行的 Kick。不同 Conversation 不共享
聊天历史，没有额外的 Agent Session 容器。

```text
当前消息 + 当前 Conversation 近期已完成消息（各自 seriesIds）+ 本次消息固定 Scope
  → Query：规划，调用内部 Retrieval Tool，生成草稿并选择证据
  → Evaluator：基于草稿与召回证据返回 complete / continue
      ├── continue：advice 连同逐轮计划、召回、草稿和审核记录交回 Query
      │    → Query 可以重新召回或复用已有候选，再交 Evaluator
      └── complete：Chat 基于获批草稿与证据实时输出正文
           → Output 校验证据并封装结果 → 提交 ASSISTANT → 交付 completed
```

Query/Evaluator 默认最多迭代 3 轮，包含首轮。任一轮通过即进入 Chat；第三轮仍要求
continue 则消息 `FAILED`，不能把跑满轮数当作审核通过。Query 可以选择当前 Kick 已验证
召回记录中的候选，最终片段数量仍受有效 `topK` 限制，ID、时间范围和 Scope 必须合法。
Evaluator 读取结构化文本 Evidence，并非重新观看原视频；重新召回不等于相邻片段补查。

### 12.2 最小请求顺序

Conversation 创建和消息发送必须带 `Idempotency-Key`，最长 128 字符，不能只包含空白。
同一操作的网络重试复用 Key，不同操作生成新 Key。

1. 查询 Workspace 下的 Conversation，选择已有对话或直接创建：

   ```http
   GET /api/agent/conversations/list?workspaceId=local-default&limit=100
   GET /api/agent/conversations/get?conversationId={conversationId}&workspaceId=local-default
   ```

   ```http
   POST /api/agent/conversations/create
   Content-Type: application/json
   Idempotency-Key: conversation-create-001

   {"workspaceId":"local-default","title":"第七集画面查询"}
   ```

   返回 `201 AgentConversationView`。`title` 可省略，默认“新对话”，范围 1～255 字符。
   创建不传 `seriesIds`，不校验动画。也不传 `sessionId`，不先创建 Session。

2. 加载消息并订阅持久化状态：

   ```http
   GET /api/agent/messages/list?conversationId={conversationId}&workspaceId=local-default&afterSequenceNo=-1&limit=200
   GET /api/agent/messages/events?conversationId={conversationId}&workspaceId=local-default
   ```

3. 从已有列表接口加载动画，维护独立于 Conversation 的多选状态：

   ```http
   GET /api/series/list?workspaceId=local-default
   ```

   使用 `SeriesView[]` 中的 `seriesId/name/status`。未 READY 项不能新选，已选项仍可取消；没有选中项显示
   “全部可检索动画”。切换选择不新建或切换对话，检索结果卡片仍按消息保存各自来源。
   已选动画变成非 READY 或缺失时提示用户调整范围，不静默删成空数组。按钮和 Enter
   共用发送条件，无效提交保留输入正文。

4. 发送当前消息，使用 `Accept` 选择同一路径的交付形式：

   ```http
   POST /api/agent/messages/send?conversationId={conversationId}
   Accept: text/event-stream
   Content-Type: application/json
   Idempotency-Key: message-send-001

   {"workspaceId":"local-default","content":"爽世为什么哭着质问大家要演奏春日影","seriesIds":["77285f16-3e5b-592e-ba15-8b224b619806"],"topK":8}
   ```

   `Accept: application/json` 或默认请求等待执行结束，返回 `200 AgentMessageSendView`。
   `Accept: text/event-stream` 返回 POST SSE，事件及完成条件见第 12.5 节；应使用 `fetch`
   消费 POST 流，`EventSource` 仅用于 GET 状态快照。

   `content` 为 1～20000 字符，不能只包含空白。`topK` 可省略或为 `null`，当前默认 8，
   Service 上限默认 20；分别由 `ANIVAG_AGENT_DEFAULT_TOP_K`、`ANIVAG_AGENT_MAX_TOP_K`
   配置。HTTP Schema 的 1～100 只是外层限制，超过 Service 上限仍返回 `400`。
   最终候选数可能小于 `topK`，也可以为 0。

   `seriesIds` 可省略或为 `[]`，后端展开为当前 Workspace 全部 READY 动画；没有 READY
   动画返回 `409`。显式数组中的 ID 必须属于当前 Workspace 且已 READY，跨 Workspace /
   缺失返回 `404`，未 READY 返回 `409`，不能静默删去选择项。UUID 规范化、去重并排序后用于请求
   幂等比较，选择顺序不影响幂等；实际展开后的范围在启动任务前保存到 USER / ASSISTANT 消息。

`workspaceId` 可省略，默认 `local-default`，范围 1～64 字符。Conversation 列表默认
`limit=100`、上限 500，只有 `limit`、没有翻页游标；Message 列表默认 200、上限 500，
按 `sequenceNo` 升序返回严格大于 `afterSequenceNo` 的消息。列表返回数组，没有 `total` 包装。

### 12.3 消息模型与当前模型名

以下摘录复用第 8 节的 `VideoChunkSearchResult`：

```ts
interface AgentConversationView {
  conversationId: string;
  workspaceId: string;
  title: string;
  messageCount: number;
  lastMessageAt: string | null;
  createdAt: string;
  updatedAt: string;
}

interface AgentMessageView {
  messageId: string;
  conversationId: string;
  sequenceNo: number;
  role: "USER" | "ASSISTANT";
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  content: string | null;
  seriesIds: string[];
  modelName: string | null;
  replyToMessageId: string | null;
  retrievalQuery: string | null;
  searchResult: VideoChunkSearchResult | null;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

interface AgentMessageSendView {
  userMessage: AgentMessageView;
  assistantMessage: AgentMessageView;
}

interface AgentMessageEventView {
  conversationId: string;
  messages: AgentMessageView[];
}

interface AgentAnswerEventBase {
  conversationId: string;
  messageId: string;
  sequence: number;
}

type AgentAnswerEvent =
  | (AgentAnswerEventBase & AgentMessageSendView & {
      type: "answer.started" | "answer.completed";
    })
  | (AgentAnswerEventBase & {
      type: "answer.evidence";
      hits: VideoChunkSearchHit[];
    })
  | (AgentAnswerEventBase & { type: "answer.delta"; delta: string })
  | (AgentAnswerEventBase & {
      type: "answer.failed";
      code: string;
      message: string;
      userMessage?: AgentMessageView;
      assistantMessage?: AgentMessageView;
    });
```

POST SSE 的 `type` 来自 SSE `event:` 行，DTO data 为对应载荷；不要假定 data 自带 `type`。
`messageId` 始终指向该次发送的 ASSISTANT，`sequence` 是该流内正整数事件序号，与消息的
`sequenceNo` 不同。命名流的精确 Schema 见 `server/api/schema.py`。

`messages/list`、JSON `messages/send`、流中的消息对和 GET 快照复用 `AgentMessageView`。
其中 `seriesIds` 必填，为本次消息实际 Scope；同一次发送的 USER / ASSISTANT 范围相同，
省略 / 空选择也返回已展开的 ID 列表。Query 获得带各自范围的聊天历史，上一条动画的
证据不能直接作为本次新召回。每个获批 `hits[].evidence.seriesId` 也必填，来自真实 MySQL
join 回源，必须属于当前消息范围；视频卡片不能从当前多选项推测历史结果来源。

`modelName` 始终返回：USER 为 `null`，ASSISTANT 从 PROCESSING 开始保存实际 Chat 模型名，
完成或失败后保留。它不是 Query/Evaluator 模型名，也不是 `evidence.modelName`；配置切换
不会重写历史记录。页面可显示最近一条 ASSISTANT 的模型名，无记录时显示“尚未调用”。

Chat 使用 `ANIVAG_AGENT_CHAT_MODEL`，未设置时使用 Gateway `config.yaml` 的
`video_parser.transcript_model`；Query 和 Evaluator 分别使用 `ANIVAG_AGENT_QUERY_MODEL`、
`ANIVAG_AGENT_EVALUATOR_MODEL`，未设置时沿用实际 Chat 模型。三个角色可以使用同一模型。

### 12.4 发送、状态机与幂等恢复

服务先在 MySQL 写入已完成 USER 与 `PROCESSING` ASSISTANT，再由 AgentChatService 的
执行线程池运行 Harness，最终更新同一条 ASSISTANT。JSON 请求等待结果；POST SSE
实时消费输出，两者均不返回 `202`。HTTP 订阅断开不会停止服务持有的 Kick。

```text
USER：COMPLETED
ASSISTANT：PROCESSING → COMPLETED / FAILED
```

同一个 Conversation 同时只能有一个 `PROCESSING` ASSISTANT。网络断线、请求超时或
切换页面不代表模型执行失败，应从持久化消息确认状态：

- 原 Key + 相同请求：完成或失败时读回原消息对，不重新调用模型；处理中返回 `409`。
- 原 Key + 不同内容/有效 `topK`/规范化请求 `seriesIds`：返回 `409`，不能在重试时更改参数。
  省略与 `[]` 等价；USER 内部保存原请求 `requested_series_ids`，公开 DTO 不返回它。
  重放优先读旧消息，新增动画不会扩大旧“全部”请求的实际范围；改成显式数组仍属新请求。
- Query/Evaluator/Chat 调用、预算、输出或证据校验失败，以及最多 3 轮仍未通过：JSON 首次
  请求返回 `502 AGENT_MODEL_ERROR`，持久化 `FAILED` 与 `lastError`；POST 流已开始时用
  `answer.failed` 报告错误。原 Key 的终态重试可能返回 `200`，仍须检查消息 `status`。
- 检索业务错误保留 `404/409` 等既有映射；POST SSE 已开始后通过 failed 及消息快照报告。
- 用户明确重新尝试使用新 Key，新增消息对。进程硬退出可能遗留 PROCESSING，目前没有
  Agent Reaper，不能用新 Key 循环发送绕过此状态。

收到 `answer.evidence` 后可渲染获批视频卡片，收到 delta 后显示逐段正文，此时消息仍为
PROCESSING；只有 completed 或持久化 COMPLETED 快照代表完整交付成功。最终候选从
`assistantMessage.searchResult.hits` 读取，播放使用 `evidence.videoId + startMs/endMs`。
不要从模型正文反解析视频时间，也不要把 `/api/search` 摘要当作获批后的 Chat 回答。

### 12.5 POST 正文 SSE、GET 快照与页面恢复

同一 POST 的正常事件顺序为：

```text
answer.started → answer.evidence → answer.delta（多段）→ answer.completed
```

| 事件 | 载荷与前端处理 |
| --- | --- |
| `answer.started` | 已落库的 USER/PROCESSING ASSISTANT 消息对；按 ID 创建或合并消息 |
| `answer.evidence` | Evaluator 放行后的完整 `hits`；渲染视频证据卡片 |
| `answer.delta` | 最终 Chat Provider 的原始文本增量；追加到该 ASSISTANT 正文 |
| `answer.completed` | Provider 正常结束且数据库提交成功后的完整消息对；确认完成 |
| `answer.failed` | `code/message` 与可选持久化消息对；失败后不会发送 completed |

每帧包含 `conversationId/messageId/sequence`，SSE `id:` 为 `messageId:sequence`。按
Conversation、ASSISTANT ID 和单调事件序号拒绝迟到、重复或不相关事件；不把 Query 草稿
或 Evaluator advice 展示为最终正文。终态幂等重试读回原结果，不重放原始 delta。

GET `/api/agent/messages/events` 则发送独立的完整持久化状态快照：

```text
event: messages
data: {"conversationId":"...","messages":[...]}
```

用 `EventSource.addEventListener("messages", ...)` 接收命名事件，首次连接也发送快照，
包括空数组。GET 快照不会启动新 Kick，也不重放 Chat 增量。

- 按 `messageId` 合并并按 `sequenceNo` 排序；PROCESSING 快照不能抹掉已收到的实时正文
  和卡片，旧 PROCESSING 状态不能覆盖已完成/失败状态。
- PROCESSING 到终态不改变消息 sequence，仅用 `afterSequenceNo` 拉新消息会漏掉状态
  变化，需要完整快照或重新拉取覆盖已有消息。
- 切换 Conversation 时关闭旧 GET 连接、终止旧 POST 消费，并校验 Conversation ID，
  旧订阅回调不得污染当前页面。
- POST 断流或 `STREAM_INTERRUPTED` 表示订阅需恢复；没有真实 FAILED 快照时保留
  PROCESSING 和部分正文，等 GET 终态校准，不编造执行失败。
- REST 首次加载超过 500 条时按最后一个 `sequenceNo` 继续分页；GET 完整快照不受该
  REST 上限截断，目前每轮重读全部消息，长对话容量优化仍待做。
- 默认每 1 秒检查变化，每 15 秒空闲发送 `: keep-alive`，可通过
  `ANIVAG_SSE_POLL_SECONDS`、`ANIVAG_SSE_KEEPALIVE_SECONDS` 调整。

数据库是终态事实源。前端可以展示“检索与审核中”和获批后的正文流，不能制造未提供的
百分比进度；进程崩溃后的运行恢复与跨 Worker 增量重放尚未实现。
