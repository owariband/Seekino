# AniVAG 前端 API 对接文档

> 状态：M1 已实现
>
> 更新时间：2026-09-15
>
> 面向对象：WebUI、前端状态管理与联调开发
>
> 实现来源：`server/router/`、`server/api/`、`server/api/schema.py`、`server/schema.py`

本文描述当前代码已经提供的 HTTP 契约，覆盖 Series 管理、视频分片上传与断点续传、
解析任务进度、视频播放和正式检索。运行中的 FastAPI OpenAPI 是字段级最终事实源：

本文只描述 Upload-to-Search，不包含聊天或对话系统协议。创建、列表和详情接口在路径中
显式使用 `create`、`list`、`get`，不依靠 HTTP Method 隐含操作名称；用于定位路由资源的
ID 统一通过 camelCase Query 参数传递，不放在 URL Path 中。检索请求中的 `seriesIds` 是
业务 Scope 集合，继续随 `/api/search` 的 JSON Body 传递。

独立的 Multi-Agent Session、Conversation、Message 和 SSE 契约见
[Agent API 对接文档](agent-api.md)。

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
| `409` | `CONFLICT` | 幂等冲突、分片范围/总大小与会话不符、缺片、过期、Series 未 READY 或索引不一致 |
| `422` | FastAPI 默认格式 | JSON 字段、Header 或 Query 缺失或不满足字段约束，未进入领域服务 |
| `500` | 服务默认错误 | 未预期的服务异常；前端应保留请求上下文并展示可重试提示 |

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
| Job | `GET` | `/api/jobs/list?workspaceId=...&limit=100` | 查询任务列表 |
| Job | `GET` | `/api/jobs/get?jobId=...&workspaceId=...` | 查询任务真实状态 |
| Job | `GET` | `/api/jobs/events?jobId=...&workspaceId=...` | SSE 订阅任务变化 |
| Media | `GET` | `/api/videos/content?videoId=...&workspaceId=...` | 播放 Original Video，支持 Range |
| Search | `POST` | `/api/search` | Series Scope 内双路检索并返回完整 Evidence |

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
- `agentReply` 是本轮检索结果的公开摘要，`resultCount` 始终等于 `hits.length`；它不是持久化对话记录。
- `selectionReason` 只整理当前响应中可核验的 Evidence、召回通道和限制，不包含模型私有思维链。
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
| GET Job | 只读 | 直接重试 |
| SSE | 通知流 | 自动重连并用 GET Job 校准 |
| Search | 只读 | 网络错误可重试；`409` 先处理 READY 或一致性问题 |

建议只自动重试网络错误和明确的 `5xx`；`400/404/409/422` 应先根据业务状态处理，避免
无意义的快速重试。

## 10. 当前尚未提供的前端能力

以下接口不在当前 M1 中，前端不要假设已经存在：

- 取消或删除 UploadSession；
- 删除 Series、Video 或 VideoChunk；
- 用户主动重跑一个已失败 ParseJob；
- 暂停 Worker 解析；
- 登录、用户鉴权和权限管理；
- 直接读取 `artifact://` URI 的接口。

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
