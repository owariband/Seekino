# Parse V1 解析产物现状与 Parse V2 对照基础

状态：基于 2026-09-24 当前源码的只读调查记录。本文先固定 Old（Parse V1）的真实情况；New（Parse V2）只记录由现状暴露出的能力缺口，不在这里提前拍板最终 Schema。

## 1. 结论

Parse V1 的解析产物不是“纯 ASR”，也不是完整的多模态事实库。它实际是以 `VideoChunk` 为单位的复合快照：

```text
候选 ASR
+ 多模态事件时间线
+ Window 摘要
+ 滚动上下文与 Compact
+ Chunk 级 Text / Vision 检索投影
```

V1 已经要求多模态模型结合视频、音频、硬字幕、前文和人物资料纠正 ASR，并尝试识别说话人物；但纠正结果没有形成独立的“校正后正式台词时间线”：

- 候选 ASR 原样保存在 `candidate_transcript`；
- 确认后台词写在通用 `DIALOGUE event.text`；
- 人物归因写在 `event.description` 自然语言中；
- 两者没有稳定 ID 或结构化引用关系。

因此 V1 的核心问题不是“没有综合理解”，而是综合理解后的内容没有被正规化。

## 2. Parse V1 的真实处理链

```text
AnalysisWindow
├── 候选 ASR：TranscriptAnalysis
├── 视频 / 音频 / 硬字幕
├── SeriesBible / 背景参考图
└── 上一个 Chunk 的有界 ContextSnapshot
        ↓
多模态理解模型
        ↓
VideoTimeline
├── summary
└── events[]
        ↓
绝对时间换算 + Chunk 唯一归属 + 轻量去重
        ↓
VideoChunkParseResult
        ↓
Artifact 完整存档
MySQL 在线事实投影
Qdrant Chunk 级派生索引
```

主调用链：

- `engine/parsev1/pipeline.py:91-154`
- `engine/parsev1/understand.py:40-94`
- `engine/parsev1/understand.py:96-213`

## 3. Engine 层实际 Schema

### 3.1 候选 ASR

当前 `TranscriptCue` 只有：

```text
start_seconds: int
end_seconds: int
text: str
```

源码：`engine/parsev1/schemas.py:22-33`。

当前没有：

- `speaker` 或 ASR diarization 标签；
- 人物 ID；
- cue ID；
- confidence；
- 与后续纠正结果的引用关系。

Audio Transcription 适配器也只读取 provider segment 的 `start/end/text`。即使上游返回 `speaker_0` 一类标签，当前 V1 Schema 没有承载位置。源码：`engine/parsev1/audio_transcription.py:79-120`。

ASR Prompt 明确只负责听写，不结合画面纠错，也不识别人物。源码：`engine/parsev1/prompts.py:8-17`。

### 3.2 多模态事件

`TimelineEvent` 是 V1 唯一的高层语义单元：

```text
start_seconds: int
end_seconds: int
event_type:
  SCENE | ACTION | DIALOGUE | EMOTION | RELATION_CHANGE | OTHER
description: str
text: str | null
```

源码：`engine/parsev1/schemas.py:36-64`。

多模态 Prompt 明确要求：

- 候选 ASR 可能有错漏和时间偏差，必须结合视频重新判断；
- 可根据音频和清晰硬字幕纠正对白；
- 确认后台词写入 `DIALOGUE.text`；
- 能确认人物时在 `description` 中写姓名；
- 不能确认时使用匿名外观描述，不强猜真名。

源码：`engine/parsev1/prompts.py:19-34`。

但 `_to_video_evidence()` 会把候选 ASR 原样放回 `candidate_transcript`，同时保存模型生成的 `summary/events`。源码：`engine/parsev1/video_chunk_parse.py:759-781`。

所以 V1 同时保留两条没有统一的内容轨道：

```text
candidate_transcript             DIALOGUE event
----------------------------     ---------------------------------
ASR 候选文本                     多模态确认或纠正后的台词
没有人物                         人物可能写在 description 中
用于 ASR Exact 检索              用于 Event / Text Dense 语义文本
```

### 3.3 每个 Chunk 的完整解析结果

`VideoChunkParseResult` 的完整字段为：

```text
VideoChunkParseResult
├── window
├── context_before
├── candidate_transcript[]
├── video_parse                 # provider/model/input/fallback provenance
├── is_fallback
├── window_summary              # 模型对整个 AnalysisWindow 的摘要
├── observed_events[]
├── events[]                    # 最终归属当前 Chunk 的 OwnedTimelineEvent
├── self_compact
├── context_after
├── context_before_compact
└── context_after_compact
```

源码：`engine/parsev1/schemas.py:280-329`。

其中：

- `observed_events` 不是原始视觉观察，而是模型 Event 完成绝对时间换算后，对“是否归当前 Chunk、是否在去重后保留”的过程记录；
- `events` 才是最终保留的 Chunk 事件；
- `self_compact` 不是新的模型结论，而是 Engine 把 `events` 确定性渲染成文本；
- `context_before/after` 是供相邻窗口消歧的有界滚动上下文，不是全剧事实库。

Event 的时间换算、归属和去重见 `engine/parsev1/understand.py:240-297,419-445`；Compact 渲染见 `engine/parsev1/understand.py:299-394`。

### 3.4 整集产物

```text
VideoParseResult
├── models
├── source_video
├── duration_seconds
├── chunk_duration_seconds
├── context_overlap_seconds
├── chunks[]
└── events[]                    # 所有 Chunk 最终 events 的并集
```

源码：`engine/parsev1/schemas.py:347-376`。

### 3.5 临时视觉观察不是正式产物

Fallback 路径会临时生成 `NeutralVisualObservation`：

```text
evidence_frames
subjects
visual_fact
hard_subtitle
visible_speaker
```

它只用于把匿名逐帧证据交给 refiner，最终不会进入 `VideoChunkParseResult` 或 MySQL；Primary 路径甚至不会生成这一结构。源码：

- `engine/parsev1/schemas.py:66-82`
- `engine/parsev1/video_chunk_parse.py:273-375`
- `engine/parsev1/video_chunk_parse.py:581-666`

因此 V1 没有持久化、可定位、可复核的统一视觉证据层。

## 4. Window、时间与 Overlap

V1 区分两种媒体范围：

- `AnalysisWindow`：可带前后 overlap，供模型理解上下文；
- canonical `VideoChunk`：互不重叠，覆盖整集，用于播放、持久化和 Vision Embedding。

V1 里的 `canonical` 指规范且唯一的媒体分块，不表示“经过校正的正式内容”。源码：`engine/parsev1/analysis_windows.py:1-5`。

当前 Parse Schema 使用整数秒：

- `candidate_transcript` 时间相对 AnalysisWindow；
- 模型 Event 最初也相对 AnalysisWindow；
- Engine 将 Event 换算为整集绝对秒，并按事件中点归属唯一 canonical Chunk；
- ASR cue 在 Parse 阶段没有做相同归属，相邻 overlap Window 可能保存重复 cue；精确检索时才换算绝对时间并按 cue 中点过滤。

ASR 查询时归属处理见 `server/repository/video.py:723-809`。

## 5. 实际持久化不是单一 Schema

V1 由 Artifact、MySQL、Qdrant 三层组成。

### 5.1 Artifact：最完整的恢复产物

每次 Parse lease 下保存：

```text
jobs/<job_id>/leases/lease-XXXXXXXX/
├── chunks/chunk-XXXX.json
├── embeddings/chunk-XXXX.text.json
├── embeddings/chunk-XXXX.vision.json
└── pipeline.parse.json
```

- `chunk-XXXX.json`：模型信息加完整 `VideoChunkParseResult`；
- `pipeline.parse.json`：完整 `VideoParseResult`；
- embedding JSON：Chunk ID、输入 hash 和完整 embedding result。

源码：`server/service/parsev1/parse_persistence.py:168-303`。

Artifact 比 MySQL 投影更完整，包含 `observed_events` 和完整 Context 链。生产路径没有单独的 durable ASR checkpoint；候选 ASR 会在整个 Chunk 理解成功后随 Chunk Artifact 一起保存。

### 5.2 MySQL：在线事实源

`video_chunk` 保存：

- canonical Chunk 与 AnalysisWindow 范围；
- Chunk/Window 媒体引用；
- `transcript JSON`；
- keyframes。

表定义：`server/model/video.py:113-163`。导入时 `transcript` 明确写入 `chunk.candidate_transcript`：`server/repository/video.py:472-523`。

`video_chunk_info` 保存：

- `all_time_events JSON`，实际是当前 Chunk 的最终 owned events；
- `all_time_event_text`；
- `video_compact`；
- before/after context compact；
- window summary；
- provider/model/pipeline/fallback/provenance/parsed time。

表定义：`server/model/video.py:166-191`；写入映射：`server/repository/video.py:538-572`。当前 `all_time_event_text` 和 `video_compact` 都写入同一个 `self_compact`。

`context_snapshot` 保存每个 Chunk 之后的 compact 与 recent events：`server/model/video.py:194-233`。

`video_chunk_embedding` 保存 Text Embedding 输入、Text/Vision vectors 和 metadata：`server/model/video.py:236-248`。

MySQL 当前没有：

- 校正后台词表；
- Speaker / Person 归因表；
- VisualObservation 表；
- 独立 Event 行表；
- Evidence 引用；
- Patch / Reducer 产物；
- Parse Content Revision 或 Published Revision。

### 5.3 Qdrant：Chunk 级派生索引

一个 `VideoChunk` 对应一个 point：

```text
vectors:
  text
  vision

payload:
  seriesId
  videoId
  videoChunkId
  sourceRevision
  isFallback
```

源码：`server/storage/qdrant.py:14-29,44-52,136-174`。

Qdrant 不保存台词、事件、时间、媒体 URI 或 Compact 正文。命中后，Server 使用 `video_chunk_id` 回 MySQL 联表补全整个 Chunk。

当前 `sourceRevision` 只哈希 Qdrant point 的 IDs、操作、`isFallback` 和两条向量，不是业务解析版本。源码：`server/repository/index_sync.py:345-378`。

## 6. V1 检索实际消费哪份产物

| 通道 | 实际输入 | 方法 | 返回单位 |
| --- | --- | --- | --- |
| ASR 原话 | `video_chunk.transcript`，即候选 ASR | 连续 cue 字面子串匹配 | Chunk，附命中 cue 文本与时间 |
| Text Dense | `self_compact + owned event.description + event.text` | 每 Chunk 一条 Text Vector | Chunk |
| Vision Dense | 无 overlap canonical Chunk 的整段视频 | 每 Chunk 一条 Video Vector | Chunk |

检索文本拼装：`engine/retrieval/video_retrieval.py:97-128`；ASR Exact：`engine/retrieval/video_retrieval.py:244-298`；三路 RRF：`engine/retrieval/video_retrieval.py:172-241`。

这造成一个实际不一致：

- ASR Exact 搜的是未被多模态模型正式改写的候选 ASR；
- Text Dense 可能搜到 `DIALOGUE.text` 中的纠正台词和 `description` 中的人物名；
- 两条通道不是基于同一份正式台词；
- Text/Vision 只知道哪个 Chunk 相似，不知道真正命中了哪条 Event、哪句话或哪一帧。

## 7. Old 与 V2 待补能力的准确对照

下表只描述由 V1 事实暴露出的缺口，不等于 V2 最终 Schema 已决定。

| 内容 | Parse V1 实际状态 | Parse V2 需要解决的问题 |
| --- | --- | --- |
| 原始 ASR | 有候选文本和时间，但没有当前 speaker 标签 | 是否保留上游 diarization 与原始证据身份 |
| 校正后台词 | 混在 `DIALOGUE event.text` | 是否成为独立正式台词时间线 |
| 人物归因 | 混在 `event.description` 自然语言 | 如何结构化关联已知人物、稳定匿名人物和 unknown |
| 视觉依据 | Fallback 中临时存在，最终丢弃 | 哪些可定位依据需要持久化以支持纠错和归因 |
| 语义事件 | 有 generic `TimelineEvent` | 如何关联其来源台词或视觉依据 |
| Compact | 有，且为确定性派生文本 | 保持为派生视图，不冒充源事实 |
| Overlap | Event 已做唯一归属；ASR 延迟到查询时处理 | 如何形成统一、无重复的正式时间线 |
| 内容 Revision | 没有 | 如何表达内容修改、物化结果与发布版本 |
| 检索原子 | 只有 Chunk | 是否从正式台词和 Event 生成更细粒度检索对象 |

V2 不应被描述为“V1 没有综合理解，所以重新造一套”。更准确的演进方向是：

```text
ASR 底稿
≠
校正后的正式台词与人物归因
≠
更高层的动作、场景、关系与剧情事件
≠
面向检索系统的派生投影
```

至少需要把 V1 已经隐含存在、但目前混在一起的内容边界拆清；是否引入 `Utterance`、`SpeakerAssertion`、`VisualObservation`、`Shot` 等具体稳定类型，仍属于后续设计决策。

## 8. 当前讨论中的术语约束

- 不再用笼统的 “Canonical Evidence” 同时指代原始证据、校正台词和语义事件；
- 讨论 V1 时，`canonical VideoChunk` 只表示规范、不重叠的媒体 Chunk；
- 需要表达业务结果时，优先使用“候选 ASR”“校正后台词时间线”“语义事件”“检索投影”等直接名称；
- `SpeakerAssertion` 如果存在，应被视为归因过程或冲突判断，不自动等于最终对外解析产物；
- `Shot` 在镜头检测稳定之前，不自动成为第一版核心 Schema。
