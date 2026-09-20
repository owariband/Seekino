# AniVAG Engine

Engine 是 AniVAG 的算法边界：它负责单个 VideoChunk 的模型调用、证据解析和
Context 演进，不负责生产 Job 的调度与持久化生命周期。外部模型请求仍由根目录下的
`gateway/` 负责。Engine 当前与 Server 在同一进程内装配；是否独立部署不改变这个边界。

## 生产契约

产品层的算法契约仍然只有 Parse 和 Retrieve：

```text
parse_chunk(Compact, VideoChunk) -> VideoChunkParseResult
retrieve(...) -> RetrievalResult
```

在当前 Python 实现中，Parse 对应
`EngineVideoParseRuntime.parse_chunk(compact, video_chunk, ...)`。`Compact` 对应调用当下已确认的
`ContextSnapshot`；候选 Transcript、SeriesBible 和背景图是该次单 Chunk 调用的显式输入。
它不接收 Job、Lease、Repository、DB Checkpoint、工作目录或 UploadSession。

`EngineVideoParseRuntime.describe_models(...)` 和 `transcribe_chunk(video_chunk)` 是当前模型
Runtime 为 Server 编排候选 ASR 提供的辅助能力，不是新的 Algorithm PSM、Queue
或任务生命周期接口。Retrieve 继续由 `retrieval/video_retrieval.py` 提供纯排序/融合算法。

生产整集编排属于 `server/service/video_processing.py`，包括：

- 规划整集 AnalysisWindow / VideoChunk，并决定下一个待处理 Chunk；
- 以 MySQL Checkpoint 为任务恢复事实；
- 管理有界 ASR 预取和按序解析；
- 在算法调用返回后检查 Lease / Fencing；
- 管理 Artifact、Embedding、MySQL 事实与 Index 发布。

Engine 内部仍负责单个 VideoChunk 的 Prompt、候选 ASR 融合、审核 fallback、
事件时间换算、ownership、去重和 `ContextSnapshot` 演进。`contextAfter`
只来自当前及更早 VideoChunk 的已确认事件，不会吸收 AnalysisWindow
后置重叠区里的未来事件。

## 代码边界

算法代码按职责分为 `parse/` 和 `retrieval/` 两个目录：

- `parse/runtime.py`：装配模型，提供单 Chunk Parse 契约及候选 ASR 辅助能力；
- `parse/video_parse.py`：实现单 Chunk 的 Context、ownership、去重与结果构造；
- `parse/video_chunk_parse.py`：实现 Prompt、候选 ASR 融合、模型选择与审核降级；
- `parse/frame_sampler.py`：实现审核 fallback 所需的帧时间规划与 FFmpeg 抽帧；
- `parse/prompts.py`：实现 Transcript 与事件时间线的证据约束；
- `parse/analysis_windows.py`：提供视频窗口规划与物化原语，由 Server 在生产整集编排中调用。

Retrieve 边界保持不变：`retrieval/video_retrieval.py` 只负责
`VideoChunkRetrievalDocument` 构造与 Text/Vision RRF 等纯算法。Query Embedding、
Qdrant 查询和 MySQL hydrate 由 `server/service/video_retrieval.py` 负责，正式 Qdrant
持久化由 `server/storage/` 承担；不为了形式另增 `RetrieveClient` 或空 facade。
会组合 Engine、Gateway 与实验 Qdrant 数据集的召回评测工具位于
`evals/recall/`。

## 本地实验兼容层

`ParsePipeline.run(...)`、`EngineVideoParseRuntime.run(...)`、`parse/checkpoints.py` 和
`parse/cli.py` 继续支持本地整集实验。它们会从完整视频规划窗口、运行有界 ASR
预取、写入 JSON Checkpoint 并从本地输出目录恢复。这些文件只是 CLI / local
experiment 兼容能力，不是 Server 的生产任务事实，Server 也不通过整集 `run(...)`
接口执行解析。

本地 `parse-video` CLI 物化的 AnalysisWindow 不是上传传输分片。上传断点续传使用
`Segment`，它是字节传输分片，与 VideoChunk 无关。

## 本地实验运行

在仓库根目录执行：

```bash
uv sync

uv run --env-file .env.local python -m engine.parse.cli \
  --config config.yaml parse-video \
  --video '/absolute/path/to/episode.mp4' \
  --chunk-duration-ms 60000 \
  --context-overlap-ms 3000 \
  --max-height 720 \
  --asr-mode none \
  --asr-concurrency 4 \
  --fallback \
  --series-bible-file /absolute/path/to/characters.md \
  --output-dir outputs/engine/episode
```

该本地兼容入口使用并发受限的 ASR 流水线：当前 VideoChunk 的 Transcript 就绪后会立即写入
`transcripts/chunk-XXXX.json` 并交给 Pro，同时 Lite 继续准备后续 VideoChunk；Pro 仍按
VideoChunk 顺序串行解析并在每段完成后写入 `chunks/chunk-XXXX.json`。使用相同参数和输出目录重跑时，
Pipeline 会验证模型、窗口和上下文链，然后从第一个未完成 VideoChunk 继续。全部完成
后写入 `pipeline.parse.json`。每个 `VideoChunkParseResult` 包含该时间片的
`context_before_compact → self_compact → context_after_compact`，便于直接检查
上下文是否真的按 VideoChunk 传递。

读取已有实验目录时仍兼容历史 `segments/segment-XXXX.json` 与
`transcripts/segment-XXXX.json` 文件名；新产物只写 `chunks/chunk-XXXX.json` 与
`transcripts/chunk-XXXX.json`。这些旧路径只是兼容格式，不恢复旧领域语义。

如果主路径不需要提前生成候选 ASR，可显式传入 `--asr-mode none`。该模式会为每个 VideoChunk
保存带 `transcript_source=none` 的空候选检查点，再由 Pro 直接读取视频音频、画面与
硬字幕。它与 fallback 兼容：只有 Pro 明确触发输出内容审核时，fallback 才为当前 VideoChunk
按需调用 Lite ASR；没有触发的 VideoChunk 不会额外调用 Lite。若使用 `--asr-mode lite`，
fallback 会复用已经生成的非空 Lite 结果。

## 内容审核 fallback

fallback 只响应 Ark/Doubao Pro 明确返回的 `ContentFilterError`，每个 VideoChunk 最多
执行一次。网络错误、Schema 错误和 DeepSeek 错误不会误触发或形成循环重试。单个
VideoChunk 的算法降级链路为：

1. Doubao Pro 直接解析 AnalysisWindow；
2. Pro 被内容审核拒绝后，为当前窗口按需运行 Doubao Lite ASR；
3. Engine 把窗口细分为不超过 30 秒的组，以 0.5 fps 抽帧，每组最多 15 帧；
4. Gateway 调用官方 DeepSeek Flash，只根据该组图片生成匿名、可定位到帧的视觉证据；
   这一阶段不接收 ASR、SeriesBible 或 `contextBeforeCompact`；
5. Engine 将帧编号映射回窗口内毫秒时间并去重；
6. 同一个 DeepSeek Flash 再结合 Lite ASR、映射后的视觉证据、SeriesBible 与
   `contextBeforeCompact`，整理为最终 `VideoTimeline`。

默认参数由 `config.yaml` 管理：

```yaml
video_parser:
  video_thinking: disabled
  fallback:
    enabled: true
    frame_fps: 0.5
    group_duration_ms: 30000
    max_frames_per_group: 15
    max_height: 720
    deepseek:
      provider: deepseek
      model: deepseek-flash
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY
      reasoning_effort: none
      max_output_tokens: 8192
      request_timeout_seconds: 1200
```

`video_thinking` 只控制主 `video_model` 的 Ark thinking 模式；默认配置显式关闭，
避免逐片结构化时间线把大部分生成预算消耗在隐藏 reasoning 上。Lite 候选转写不受
这个字段影响。当前不限制主模型的 `max_output_tokens`，防止繁忙片段的合法 JSON
被截断后重新引入结构化解析错误。

运行前需在 `.env.local` 中提供 `DEEPSEEK_API_KEY`；fallback 使用官方 API，不要求
启动 Ollama 或 llama.cpp。`--fallback` 强制启用配置链路，`--no-fallback` 强制关闭，
两者都不传时使用 `video_parser.fallback.enabled`（也可由
`ANIVAG_FALLBACK_ENABLED` 覆盖）。

本地实验恢复检查会核对实际 provider、model 与 input mode。fallback 成功的 VideoChunk 会在
`video_parse` provenance 中保存 Lite、视觉与整理模型以及抽帧时间，并写入
`is_fallback: true`，供后续重生成和质量修复筛选；普通 Pro VideoChunk 保持
`is_fallback: false`。

## 整集 raw recall 评测

`python -m evals.recall.cli` 是仓库级的独立召回评测入口，不属于 Engine，也不参与
VideoChunk 解析。它要求完整且连续的 `VideoParseResult`，把每个 canonical VideoChunk
作为候选单元：

- Text 只索引当前 VideoChunk 的 `self_compact` 与 owned events，不读取 `window_summary` 或相邻
  Context；
- Vision 从原片物化零 overlap 的 canonical 视频，仅供本地评测；
- 分别计算 Text/Vision 排名与可调权重 RRF，输出 Hit@1/3/5/10 和 MRR；
- embedding 按模型、维度、FPS、源文件指纹和输入哈希原子缓存，可安全续跑。

不传 Qdrant 参数时，命令继续使用进程内精确余弦排名，适合作为对照组：

```bash
uv run --env-file .env.local python -m evals.recall.cli \
  --parse-result outputs/engine/episode/pipeline.parse.json \
  --fixture evals/recall/ep07.recall-smoke.json \
  --output-dir outputs/engine/episode-recall-local \
  --channel-top-k 20 \
  --rrf-k 60 \
  --concurrency 1
```

这里的 Qdrant 是 `evals/recall/` 私有的可选实验后端，不依赖 MySQL，也不复用
`server/storage/` 的正式持久化 adapter。仓库提供了固定版本的本地服务：

```bash
docker compose -f deploy/qdrant/compose.yaml up -d
```

使用不同的 Collection 隔离测试与正式环境；同一 Collection 内再以必填的
`dataset-id` 隔离每一套解析结果。当前只建立测试数据，建议让 ID 同时包含环境、视频、
VideoChunk 边界方案和版本；解析结果或边界改变时应换新 ID，不复用旧实验数据。

```bash
ANIVAG_QDRANT_URL=http://127.0.0.1:6333 \
ANIVAG_QDRANT_COLLECTION=anivag_video_chunks_test \
uv run --env-file .env.local python -m evals.recall.cli \
  --parse-result outputs/engine/episode/pipeline.parse.json \
  --fixture evals/recall/ep07.recall-smoke.json \
  --output-dir outputs/engine/episode-recall-qdrant \
  --retrieval-backend qdrant \
  --dataset-id test:mygo:ep07:60s:v1 \
  --qdrant-search-mode exact \
  --text-weight 3 \
  --vision-weight 1 \
  --channel-top-k 20 \
  --rrf-k 60 \
  --concurrency 1
```

Collection 使用 `text`、`vision` 两个命名向量。每个点只保存定位和后续筛选需要的
payload：`dataset_id`、`video_chunk_id`、时间范围、`video_compact` 与
`is_fallback`；前后 Context 不会落入召回文本。`exact` 会要求使用原始向量精确扫描，
用于小数据集基线；`approximate` 则允许 Qdrant 按 Collection 配置选择 ANN 或全扫描，
因此不能仅凭参数名断言实际走了 HNSW。评测结果会记录后端、Collection、dataset、
搜索模式和 Text/Vision 权重，做 A/B 时应使用不同输出目录，避免覆盖结果。

该入口只评估 raw candidate recall，尚不包含 CandidateVerifier，不能把 TopK 命中率当成
最终回答正确率。第七集 12 条人工 smoke 的标签来源、限制和首轮结果记录在
`evals/recall/ep07.recall-smoke.md`。
