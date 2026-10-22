# 解析配置

正式解析只使用 Parse V2。上传前读取 `/api/uploads/parse-config` 获取服务端默认值；自定义配置随上传会话保存，完成上传后成为 `VideoParseTask.parse_config` 快照。模型凭据与服务地址保留在服务端配置中。

任务快照中的主要字段：

- `chunk_duration`、`interval_duration`、`batch_max_size` 决定固定切片、精修窗口和批次大小。
- `visual_input` 保存视频或按序帧输入方式、FPS、最大高度和上下文重叠；实际执行以任务快照为准。修改默认 FPS 不会改变已完成的解析结果。
- 按序帧解析按 `ceil(实际视频时长 × fps)` 均匀抽帧，默认 3 fps：60 秒为 180 帧，包含前后各 3 秒上下文时为 198 帧。旧 `maxFramesPerWindow` / `max_frames_per_chunk` 字段为配置兼容保留，不再限制解析取帧。已有任务仍沿用冻结的 FPS，需显式采用新配置重新解析才能得到 3 fps 的结果。
- Gateway 不再用固定每图 2048 tokens 的猜测提前拒绝多帧输入；实际视觉容量与错误由模型服务返回。
- `models` 保存 ASR、理解、精修、OCR、Embedding 等模型的来源、型号与调用参数。可用来源由 Gateway 配置决定。
- `execution` 保存解析、精修、索引的并发、超时和重试参数；`indexing` 保存索引代次、稀疏检索开关和合同指纹。
- Embedding 模型、维度与预处理合同必须匹配当前 Elasticsearch 索引，不能只改任务参数而继续写入不兼容的索引。

当前任务详情由 `/api/jobs/video-parse/get` 返回完整 `parseConfig`。重试和重新解析通过上传服务的对应接口执行，以服务端返回的 `canRetry`、`canReparse` 及任务版本判断可用操作。旧 V1 Job 接口与离线导入命令已删除。

字段约束见 [Parse V2 输入合同](../agent/video_parse_v2/input.py)，默认值组装见 [解析配置](../server/config/parse_profile.py)，部署与数据库升级见 [Server 文档](../server/README.md)。
