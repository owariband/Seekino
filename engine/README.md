# anivag Engine

Engine 是 anivag 的领域算法边界。现有解析实现完整冻结在 `parsev1/`；新的
Video Parse V2 算法策略只进入 `parsev2/`，其 Agent／LangGraph 运行程序位于
`agent/video_parse_v2/`。`rag/` 独立拥有索引表示、输入处理、召回、融合与证据核验；
V1 专用检索文档构造归入 `parsev1/retrieval.py`。

Engine 不拥有生产 Job、Lease、Checkpoint、Artifact、MySQL 或 Qdrant；这些执行与
持久化生命周期仍由 Server 负责。外部模型请求由根目录下的 `gateway/` 实现。
`parsev1` 与 `parsev2` 不互相导入，也不通过兼容模块转发实现。

## V1 生产契约

生产 Parse 通过一个显式 Pipeline 进入 Engine：

```text
ParsePipeline
  → transcribe(VideoChunk[])                  # ASR 有界并发、按序交付
  → understand(Context, Transcript, VideoChunk) # 按序推进 Context
  → embed(VideoParseResult)                   # 构造检索文档与 Text/Vision 向量
  → persist(ParsePipelineResult, ParsePersistence) # 只调用持久化端口
```

`transcribe`、`understand` 和 `embed` 是算法阶段，不是 Server Service。ASR 可以跨
VideoChunk 有界并发，但必须按序把 Transcript 交给理解阶段；理解阶段串行推进
`ContextSnapshot`，并负责 ownership、去重和 fallback；Embedding 阶段从正式 Parse
结果构造检索文档并生成 Text/Vision 向量。provider、model 与 tier 只属于配置和
provenance，不用来定义 Pipeline 拓扑。

`persist` 表示 Pipeline 的提交边界，不表示 Engine 直接连接数据库。Engine 只向中立的
`ParsePersistence` 提交完整、已验证的 `ParsePipelineResult`；Server 实现该端口，并在
实现内部执行 Lease/Fencing 校验、Parse Artifact／Checkpoint 更新和 MySQL 事务。Qdrant
仍是 Server 在 Pipeline 返回后同步的派生索引，不属于该端口。
Engine 的输入输出只包含算法 DTO、`Path` 和 `sequence_no`，不得引用 Server 的 Job、
Lease、Repository、持久化 URI 或 DTO。

生产调用关系为：

```text
ParseJobConsumer
  → Server Job Adapter
      → Engine ParsePipeline
          transcribe → understand → embed → persist(ParsePersistence)
      ← Server ParsePersistence
          Lease/Fencing → Parse Artifact/Checkpoint → MySQL
      → Server Qdrant index sync
```

Server Job Adapter 负责加载任务、恢复已提交前缀、校验原视频和提供当前执行所需的
持久化端口；它不重新描述算法阶段。模型调用完成后的结果只有通过 Server 的当前
Lease/Fencing 校验后才能成为 durable state。内容审核 fallback 是 understand 阶段内部的
一次策略切换：它复用 transcribe 阶段产出的同一份 Transcript，不会因 Transcript 为空
而补跑 ASR。

Pipeline State 只属于当次算法执行，不是跨 Attempt 的恢复真相。异常交给
ParseJobConsumer 和 MySQL Queue 发起下一 Attempt；恢复仅信任 MySQL Checkpoint、已提交
Artifact 与 Lease/Fencing。生产 ASR 结果目前没有独立 durable checkpoint；理解失败后，
下一 Attempt 会恢复已完成的连续 PARSED 前缀，并为其余窗口重新执行 ASR。

Engine 内部仍负责单个 VideoChunk 的 Prompt、显式 Transcript 融合、审核 fallback、
事件时间换算、ownership、去重和 `ContextSnapshot` 演进。`contextAfter`
只来自当前及更早 VideoChunk 的已确认事件，不会吸收 AnalysisWindow
后置重叠区里的未来事件。

## 代码边界

算法代码按版本和职责组织：

- `parsev1/`：当前生产解析实现，只接受兼容性修复和明确的 V1 维护；
- `parsev2/`：Video Parse V2 的领域策略、状态不变量和 Patch／投影算法；不放
  LangGraph 调度、Server 恢复逻辑，也不导入 `parsev1`；
- `rag/`：存储与模型中立的索引、查询与检索策略，消费 `parsev2` 正式 Schema，
  不依赖解析运行程序或 Agent／Server／Gateway／存储 SDK；

调用方直接导入 `engine.rag` 或具体能力模块；旧 `engine/retrieval/` 已删除。
`rag/` 不导入 `parsev1`，也不借旧路径转发实现。

`parsev1/` 的主路径按现有 Pipeline 阶段命名：

- `parsev1/pipeline.py`：V1 生产整集 Pipeline 入口，显式连接 transcribe、understand、embed 和 persist；
- `parsev1/transcribe.py`：ASR 调用、有界并发和按序交付；
- `parsev1/understand.py`：Context、ownership、去重、结果构造和审核 fallback；
- `parsev1/embed.py`：检索文档构造、Text/Vision Embedding 和向量校验；
- `parsev1/persist.py`：中立 `ParsePersistence` 提交契约，不实现数据库或对象存储；
- `parsev1/runtime.py`：装配模型实现与 V1 Parse Pipeline；
- `parsev1/frame_sampler.py`：实现审核 fallback 所需的帧时间规划与 FFmpeg 抽帧；
- `parsev1/prompts.py`：实现 Transcript 与事件时间线的证据约束；
- `parsev1/analysis_windows.py`：提供视频窗口规划与物化原语。

RAG 能力按处理职责分开：

```text
rag/
├── contracts.py              # Engine／Agent／Server 共享的检索领域合同
├── encoding.py               # 编码合同、响应校验、输入身份和向量复用
├── query_pipeline.py         # 查询上下文校验与输入处理编排
├── indexing/
│   ├── projection.py         # 正式 Timeline → 来源词项与文字语义输入
│   └── pipeline.py           # Chunk Source → Index Plan → Document／Receipt
├── nlp/
│   ├── query.py              # 文字查询 → 编码信号及结果校验
│   └── transcript.py         # 通用 ASR cue 短语匹配及真实时间范围
├── vision/inputs.py          # 视频片段／有序帧输入、采样与实际 manifest
└── retrieval/
    ├── contracts.py          # 查询、可信任务范围、候选与证据数据结构
    ├── pipeline.py           # 编码信号 → 召回请求 → Chunk 候选归并
    ├── fusion.py             # 共享排名与 RRF
    └── evidence.py           # 同版正式事实、相邻来源、人物／对白／时序核验
```

索引入口是 `plan_chunk_index`／`finalize_chunk_index`；查询先由
`plan_retrieval_query` 准备输入信号，再由 `build_recall_requests`、
`fuse_retrieval_candidates` 和 `verify_retrieval_evidence` 处理召回与真实来源。
调用方提供可信 COMPLETE 任务及正式事实，执行媒体读取、模型调用和 ES 查询；
Engine 只产出中立计划、校验返回值并执行策略。文字输入可以产生兼容的文字或视觉
Dense 信号，输入模态不与召回通道一一绑定。

当前 `nlp/query.py` 实现文字查询处理，`vision/inputs.py` 承接已实现的索引视觉输入
规则；图片／GIF 查询处理及视觉证据定位仍属于 P4 后续开发。新查询 pipeline 的
产品接入与真实协议验收也需分别完成，目录迁移不代表这些能力已接线。

V1 `VideoChunkRetrievalDocument` 构造位于 `parsev1/retrieval.py`，消费 V1 正式成果；
通用 ASR cue 短语匹配位于 `rag/nlp/transcript.py`，不依赖解析版本。
V1 和 V2 调用方直接消费 `rag/retrieval/fusion.py` 的共享排名与 RRF。
投影版本常量仍参与 `rag/indexing/projection.py` 的指纹计算。存储读写和正式事实
回查由 Server 实现，模型执行由 Gateway 实现，召回评测入口仍在 `evals/recall/`。

## 本地实验兼容层

生产任务与本地实验复用 transcribe／understand 原语和模型装配；完整的
`transcribe → understand → embed → persist` 只由生产 `ParsePipeline` 编排。
`parsev1/cli.py` 的本地适配器保留独立 JSON Checkpoint 恢复路径，不执行生产 embed／persist；
Server 则为 `ParsePipeline` 提供生产 `ParsePersistence`，提交受 Lease 保护的 Parse
Artifact 和 MySQL 事实，并在 Pipeline 返回后同步索引。两种执行路径不能把各自的恢复
状态泄漏进算法 DTO；本地 JSON 不是生产任务事实，生产 DB Checkpoint 也不是算法 DTO。

本地 V1 `parse-video` CLI 物化的 AnalysisWindow 不是上传传输分片。上传断点续传使用
`Segment`，它是字节传输分片，与 VideoChunk 无关。

## 本地实验运行

在仓库根目录执行：

```bash
uv sync

uv run --env-file .env.local python -m engine.parsev1.cli \
  --config config.yaml parse-video \
  --video '/absolute/path/to/episode.mp4' \
  --chunk-duration-seconds 60 \
  --context-overlap-seconds 3 \
  --max-height 720 \
  --asr-mode none \
  --asr-concurrency 4 \
  --fallback \
  --series-bible-file /absolute/path/to/characters.md \
  --output-dir outputs/engine/episode
```

该本地兼容入口使用并发受限的 ASR 流水线：当前 VideoChunk 的 Transcript 就绪后会立即写入
`transcripts/chunk-XXXX.json` 并交给理解流水线，同时 ASR Worker 继续准备后续
VideoChunk；理解仍按 VideoChunk 顺序串行执行，并在每段完成后写入
`chunks/chunk-XXXX.json`。使用相同参数和输出目录重跑时，
Pipeline 会验证模型、窗口和上下文链，然后从第一个未完成 VideoChunk 继续。全部完成
后写入 `pipeline.parse.json`。每个 `VideoChunkParseResult` 包含该时间片的
`context_before_compact → self_compact → context_after_compact`，便于直接检查
上下文是否真的按 VideoChunk 传递。

读取已有实验目录时仍兼容历史 `segments/segment-XXXX.json` 与
`transcripts/segment-XXXX.json` 文件名；新产物只写 `chunks/chunk-XXXX.json` 与
`transcripts/chunk-XXXX.json`。这些旧路径只是兼容格式，不恢复旧领域语义。

如果不需要生成 ASR，可显式传入 `--asr-mode none`。该模式会为每个 VideoChunk
保存带 `transcript_source=none` 的空 Transcript 检查点，并把这个显式空 DTO 交给主路径
和可能触发的 fallback。fallback 不会把空 Transcript 解释成“尚未运行”，也不会按需
补跑 ASR。`--asr-mode lite` 只是选择 ASR Agent 的具体配置，不改变 Agent 或 fallback
拓扑。

## 内容审核 fallback

fallback 只响应主理解模型明确返回的 `ContentFilterError`，每个 VideoChunk 最多
执行一次。网络错误、Schema 错误和 DeepSeek 错误不会误触发或形成循环重试。单个
VideoChunk 的算法降级链路为：

1. understand 阶段使用 transcribe 阶段传入的 Transcript 解析 AnalysisWindow；
2. 主模型被内容审核拒绝后，fallback 复用完全相同的 Transcript，不运行 ASR；
3. Engine 把窗口细分为不超过 30 秒的组，以 0.5 fps 抽帧，每组最多 15 帧；
4. Gateway 调用配置的 fallback 视觉模型，只根据该组图片生成匿名、可定位到帧的视觉证据；
   这一阶段不接收 ASR、SeriesBible 或 `contextBeforeCompact`；
5. Engine 将帧编号映射回窗口内秒时间并去重；
6. fallback 整理模型再结合传入的 Transcript、映射后的视觉证据、SeriesBible 与
   `contextBeforeCompact`，整理为最终 `VideoTimeline`。

默认参数由 `config.yaml` 管理：

```yaml
video_parser:
  fallback:
    enabled: true
    frame_fps: 0.5
    group_duration_seconds: 30
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

运行前需在 `.env.local` 中提供 `DEEPSEEK_API_KEY`；fallback 使用官方 API，不要求
启动 Ollama 或 llama.cpp。`--fallback` 强制启用配置链路，`--no-fallback` 强制关闭，
两者都不传时使用 `video_parser.fallback.enabled`（也可由
`ANIVAG_FALLBACK_ENABLED` 覆盖）。

本地实验恢复检查会核对实际 provider、model 与 input mode。fallback 成功的 VideoChunk 会在
`video_parse` provenance 中保存 Transcript、视觉与整理模型以及抽帧时间，并写入
`is_fallback: true`，供后续重生成和质量修复筛选；未触发 fallback 的 VideoChunk 保持
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

不选择 Elasticsearch 后端时，命令使用进程内精确余弦排名，适合作为对照组：

```bash
uv run --env-file .env.local python -m evals.recall.cli \
  --parse-result outputs/engine/episode/pipeline.parse.json \
  --fixture evals/recall/ep07.recall-smoke.json \
  --output-dir outputs/engine/episode-recall-local \
  --channel-top-k 20 \
  --rrf-k 60 \
  --concurrency 1
```

Elasticsearch 是 `evals/recall/` 私有的可选实验后端，不依赖 MySQL，也不复用
`server/storage/` 的正式持久化 adapter。必须使用 `anivag_recall_eval_*` 命名的专用
索引，避免误写正式业务索引。

```bash
ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL=http://127.0.0.1:9200 \
ANIVAG_RECALL_EVAL_INDEX=anivag_recall_eval_test \
uv run --env-file .env.local python -m evals.recall.cli \
  --parse-result outputs/engine/episode/pipeline.parse.json \
  --fixture evals/recall/ep07.recall-smoke.json \
  --output-dir outputs/engine/episode-recall-elasticsearch \
  --retrieval-backend elasticsearch \
  --dataset-id test:mygo:ep07:60s:v1 \
  --elasticsearch-search-mode exact \
  --text-weight 3 \
  --vision-weight 1 \
  --channel-top-k 20 \
  --rrf-k 60 \
  --concurrency 1
```

同一索引内以必填的 `dataset-id` 隔离数据。每个 dataset 的摘要同时覆盖 VideoChunk
文档、embedding 模型/维度/FPS，以及 embedding cache 已使用的视觉输入哈希；后者包含
源片指纹、canonical media 指纹和 `max-height`。因此同一 ID 下即使文档不变，只要源片、
物化媒体或高度发生变化也会在 embedding 前 fail closed，必须换用新的 dataset ID。

索引保存 Text/Vision 两个 dense vector 及定位字段；前后 Context 不会落入召回文本。
`exact` 使用 dataset 过滤后的 `script_score` 精确扫描，`approximate` 使用过滤后的 kNN。
评测结果会记录后端、索引、dataset、搜索模式和 Text/Vision 权重，做 A/B 时应使用不同
输出目录，避免覆盖结果。

该入口只评估 raw candidate recall，尚不包含 CandidateVerifier，不能把 TopK 命中率当成
最终回答正确率。第七集 12 条人工 smoke 的标签来源、限制和首轮结果记录在
`evals/recall/ep07.recall-smoke.md`。
