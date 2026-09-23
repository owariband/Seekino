# 解析配置

上传前可选择服务端当前默认配置，或展开自定义配置。自定义参数随上传会话保存，完成上传时复制到解析任务；排队、失败重试和重新解析沿用该任务的配置。旧任务的 `parseConfig` 为 `null`，表示当时的参数没有被记录，不能用当前默认值冒充历史值。

## 画面与帧数

- `window.analysisMaxHeight` 控制送往 ASR 与主视觉模型的分析视频高度。可以试用 480p，不会同时改变回放素材。
- `window.canonicalMaxHeight` 控制回放片段、关键帧及视频向量化使用的视频高度。
- `vision.fps` 控制主模型的取帧频率；`window.chunkDurationSeconds` 和 `window.contextOverlapSeconds` 决定每次调用的视频时间范围。界面显示的帧数是估计值，实际 Token 和费用以服务商返回值为准。
- `vision.inputMode=video` 保留视频输入；这个模式没有独立的硬帧数上限。
- `vision.inputMode=ordered_frames` 使用已有的均匀抽帧路径，此时 `maxFramesPerWindow` 是每段帧数硬上限。主视觉模型接收图片序列和上游 ASR 文本，不接收该段视频的原始音轨；应单独评测其效果。
- 内容过滤兜底另有 `fallback.frameFps`、`groupDurationSeconds`、`maxFramesPerGroup` 和 `maxHeight`。兜底参数只在触发时生效。

## 其他参数

ASR 模式、模型、FPS、并发与输出上限，主视觉模型及输出上限，前序事件和文字预算，背景文字与参考图的输入限额，视频 embedding FPS，以及请求超时和最大尝试次数，均可按任务配置。背景参考图最大边长不能超过已存储的模型图片大小。

主解析与 ASR 的模型 ID 走 Ark，内容过滤兜底模型走 DeepSeek。这里配置的是现有接入方式的模型参数；填写本地 Ollama 模型名不会自动切换接入方式。界面的参数上限是建议范围，若服务端原有默认值更高，会允许保留该默认值。

Embedding 模型和维度必须与当前 Qdrant 索引一致；界面显示这两个值但不允许按任务混用。模型凭据与服务地址留在服务端配置中，不进入任务快照。

任务详情接口返回完整的 `parseConfig`，方便核对成本和效果。当前重新解析复用同一任务并清空检查点；若要同时保留同一集两套结果，还需要独立的实验任务与隔离的输出存储。

## 升级

已有 MySQL 安装需先应用 `docs/migrations/20260922_parse_config_snapshot.sql`，再启动读取新字段的服务版本。迁移只加两个可空 JSON 列，不回填不可信的历史配置。新建测试数据库通过 SQLAlchemy 模型直接获得这两列。
