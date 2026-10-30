# Series 背景文字与参考图片

背景编辑默认使用 Markdown，也可以切换到 JSON 并粘贴 JSON 正文。后端将格式和原始正文保存在现有 `Series.important_extra_data` JSON 字段，不限制角色、关系、术语等自定义字段。JSON 模式只校验 JSON 语法，可以是对象、数组或其他合法 JSON 值；它不是用于校验正文的 JSON Schema。

这条链路是编辑正文、单独上传图片、保存正文、Worker 准备参考输入。粘贴 Markdown 或 JSON 文字不会自动上传其中的本地文件或网络图片。

## 前端接入流程

1. 先创建 Series，或通过 `GET /api/series/get` 读取现有背景。
2. 用户粘贴图片、选择图片文件时，编辑器向图片上传接口发送图片二进制。
3. 上传成功后，编辑器在光标位置插入 `![用户填写的说明](asset://<imageId>)`。JSON 模式可在任意字符串字段中保存 `asset://<imageId>`。
4. 用户可以移动引用、删除引用并补充图片附近的角色名或说明。保存调用 Series 更新接口。
5. 上传失败时，不插入一个未上传的图片引用。正文保存会再次检查受管图片的归属与原图、模型图是否都存在。

图片与正文位置的对应关系通过同一个 `asset://<imageId>` 标识建立。后端保留完整背景文字，提取所有明确标识，按第一次出现的顺序去重；发送模型时，每张图片之前都有写明相同标识的文字。这种关联不依赖 Markdown 渲染，也不依赖模型访问编辑器的预览 URL。图片所代表的人物仍需要用户的标题、说明及图片内容提供语义。

## 更新 Series

`POST /api/series/update`

```json
{
  "workspaceId": "local-default",
  "seriesId": "<series UUID>",
  "description": "系列简介",
  "transcriptLanguageHint": "ja",
  "importantExtraData": {
    "format": "markdown",
    "content": "# 背景\n\n## 角色A\n![角色A](asset://<imageId>)\n\n角色说明。"
  }
}
```

`workspaceId` 和 `seriesId` 必填。`description`、`transcriptLanguageHint`、`importantExtraData` 可分别更新：省略表示保留原值，显式 `null` 表示清空。只有这三个字段可更新；该接口不修改 Series 名称、视频的集数或上传顺序。简介去除首尾空白，空简介按 `null` 保存；背景正文按原文保存。

`transcriptLanguageHint` 使用常见 BCP-47 形式，例如日语 `ja`、简体中文 `zh-Hans`。它立即用于 Query Agent 组织同语言 Transcript-heavy 检索，不会改变已保存 ASR、触发重新解析或证明具体原台词存在。

`importantExtraData.format` 支持 `markdown`、`json`，省略时使用 `markdown`；`content` 必须为字符串。容器只接受这两个字段。任意自定义字段应该放到 JSON 正文内：

```json
{
  "workspaceId": "local-default",
  "seriesId": "<series UUID>",
  "importantExtraData": {
    "format": "json",
    "content": "{\"人物\":[{\"名字\":\"角色A\",\"参考图\":\"asset://<imageId>\"}],\"我自己的字段\":{\"术语\":[\"示例\"]}}"
  }
}
```

成功返回 `200` 和现有 `SeriesDetail`，包含完整 `importantExtraData`、简介与视频列表。`GET /api/series/get?workspaceId=local-default&seriesId=<series UUID>` 使用同一读取结构，JSON 正文的空格、换行和自定义字段保留，便于继续编辑。更新在所属 workspace 的 Series 行锁事务中提交，任一背景校验失败均不会写入该次简介或背景改动。

旧数据兼容：创建接口的任意 `importantExtraData` JSON、旧 `seriesBible` 文本和 `characterBible` 仍可读取。Worker 准备背景时使用新容器正文，并兼容旧字段；旧 JSON 的其他自定义信息也会进入背景文字。

## 上传参考图片

`POST /api/series/images/upload?workspaceId=local-default&seriesId=<series UUID>`

请求体是单张图片的原始二进制，可以发送 `Content-Type: image/png`、`image/jpeg`、`image/webp` 或 `application/octet-stream`。不使用 multipart，不上传整份 Markdown/JSON 文件。

```sh
curl -X POST \
  'http://127.0.0.1:9621/api/series/images/upload?workspaceId=local-default&seriesId=<series UUID>' \
  -H 'Content-Type: image/png' \
  --data-binary @reference.png
```

成功返回 `201` 和现有 `ImageAsset`：

```json
{
  "imageId": "<managed image ID>",
  "url": "/api/media/images/content?workspaceId=local-default&imageId=<managed image ID>",
  "expiresAt": null
}
```

正文引用使用 `asset://<imageId>`；`url` 用于前端预览。原图预览复用 `GET /api/media/images/content`，该接口按 workspace 检查所属 Series，不能借用其他 workspace 的图片。

后端按实际文件内容解码，支持静态 PNG、JPEG、WebP，拒绝动画图片、空文件及无效图片。默认原图上限为 10 MiB、4,000 万像素；配置由 `server/config/media.py` 的 `ImageUploadConfig` 与服务器设置管理。上传时保存原图和用于模型的 JPEG 副本，模型副本保持比例并将最长边限制在 1024 像素。同一个 Series 中相同图片内容返回稳定 ID，不同 Series 的图片 ID 不共享。存储文件按内容固定，删除正文引用不会删除原图或模型图，以便已有任务重试。

接口在读取请求体前验证 Series 归属；同时检查 `Content-Length` 和实际流字节数，未声明长度的请求也受同一上传限额约束。最终限制、文件解码和缩放由 MediaService 及图片工具处理，HTTP 层只负责接收、限额和返回结构。

## 受管引用与错误

后端只把 `asset://<imageId>` 作为受管参考图片。普通 HTTPS 图片链接、相对路径、`file://`、`data:` 文字不会触发后端抓图或变成模型图片输入。用户希望模型看到图片时，必须通过上述上传动作取得受管 ID。

保存和创建背景时，出现的受管 ID 必须能在当前 Series 解析到原图与模型副本。不存在的 ID、非法 ID、其他 Series 的 ID 或丢失文件都拒绝保存。JSON 中的图片引用也按相同规则检查。

| 状况 | HTTP 状态 | 统一错误码 |
| --- | --- | --- |
| JSON 正文语法错误、无效图片、超限、无效或不属于此 Series 的图片引用 | 400 | `INVALID_REQUEST` |
| 请求字段类型错误、不支持的格式、容器多余字段 | 422 | `VALIDATION_ERROR` |
| Series 不存在或不属于请求 workspace，图片读取跨 workspace | 404 | `NOT_FOUND` |
| 原图预览的文件已丢失 | 410 | `MEDIA_MISSING` |

错误沿用 `ApiError` 的 `code`、`message`、`requestId`，字段校验错误使用现有 `fieldErrors`。

## Worker 与模型输入

Worker 在解析前固定 job 首次准备完成的背景快照，包括背景文字、参考图 ID 与内容指纹。该 job 重试使用同一个快照和固定图片文件；用户后续修改 Series 只供后续新 job 使用，不在同一个视频解析中混用多个背景版本。修改背景不会自动重新解析已经完成的视频。

没有快照的旧 job：若所有窗口已经解析完成，继续恢复既有结果，不依赖当前 Series 的图片；若只完成了一部分解析，明确报错并保留缓存，避免将未知的旧背景和当前背景混在一起。不会自动清除旧缓存并重新调用收费模型。

每个需要实际模型解析的窗口都获得完整背景文字，以及按 ID 标记的参考图输入：

```text
背景全文：正文包含 asset://<imageId>
文字输入：背景参考图 asset://<imageId>，只用于身份与背景消歧
图片输入：该 ID 对应的模型 JPEG 文件
当前窗口的模型输入
```

Ark 主路径获得背景文字、参考图片和当前窗口视频。DeepSeek fallback 的第一阶段只读取当前窗口的视频帧，形成中立视觉观察；第二阶段 refiner 接收背景文字、参考图片、视觉观察与 ASR。图片引用在两条路径均转换成实际 `ImageInput`，现有 gateway 读取文件字节，构造 base64 `input_image` 后发送给 provider。

参考图片没有视频时间戳，不进入当前窗口的视频帧序列、允许的对白或事件证据。后续 Timeline、向量构建、MySQL/Qdrant 写入沿用现有流程。

代码分层：共同的图片类型标识在 `server/constants`，限制在 `server/config`，图片 ID 编码/解码及解码缩放在 `server/utils`，文件归属与持久化在 `server/storage`，JSON 和背景引用的业务检查在 LibraryService，V1 Parse 的背景输入组装在 `engine/parsev1/background.py`。

## 验证范围

本地测试覆盖上传后回读原图、Markdown/任意 JSON 保存、原文保留、字段省略与清空、错误时不写库、图片与 workspace/Series 归属、流读取限额、各 Parse 分支的图片 ID/输入映射、每窗口传递和重试快照。

gateway 的请求测试可以验证实际图片字节转换和 wire 格式；模拟模型测试可以验证分支与输入边界。它们不代表真实 Ark/DeepSeek 模型已经完成端到端识别效果验收；真实模型验收需要单独执行付费请求，并检查请求成功及人物识别结果。
