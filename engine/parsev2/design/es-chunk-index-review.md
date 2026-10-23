# AniVAG：一个 Chunk 的 ES 索引与检索设计评审

> **2026-10-17 范围补充**：本稿的“一 Chunk 一条 ES 文档”样例继续描述 Chunk 索引。[Event TRD](../../../docs/design/event-retrieval-trd.md) 另增可重建的 MySQL/ES Event 投影，保留 Chunk 完整 Timeline，不改写本稿人工样例或据此宣称新工具已验证。索引任务归属以 [Parse Agent TRD](../../../agent/video_parse_v2/design/trd.md) 当前基线为准：Interval 只 Refine，ChunkTask INDEX 承担 Chunk/Event 同步，取代本文 §8 的旧 Interval 索引流程。新 Event 合同使用绝对整数毫秒；本稿历史示例不作为时间单位依据。

状态：**设计提案，待用户 review；没有据此修改运行代码、创建 ES index 或数据库表。**

配套可编辑图：[es-chunk-index-review.excalidraw](./es-chunk-index-review.excalidraw)。打开 Excalidraw 后从 01 到 05 阅读。

职责：本批把现有 TRD 的检索合同落成具体样本；方向是 ES Index 与检索投影设计，下一步由用户 review 字段组织与输入配方，再决定实现。样本中的视频内容、身份、版本和排名均为人工构造，不是一次真实解析或检索结果。

本稿依据 2026-09-28 当前 [Engine TRD](./trd.md) §1、§2.1.9、§2.3.2–§2.3.12，以及 [Agent TRD](../../../agent/video_parse_v2/design/trd.md) 的任务状态与逐 Chunk 索引放行规则。TRD 仍是设计基线；本文提出的细节不自动升级为已确认合同。

2026-09-29 修订：明确视频 Dense 可使用**视频片段或带时间信息的有序抽帧**；补回融合后的**可选 encoder / LLM / 多模态相关性精筛（rerank）**。这两项在 TRD 已有依据，上一版图示表达不完整，不是用户新增需求。当前编码阶段、实际接线与验证结果统一见 [开发阶段评审报告](./es-development-plan-review.md)，不能用本样本的目标链路推断生产能力。

## 1. 本次推荐的物理方案

**一个 Chunk 一条 ES 文档。按类别组织全文字段，保存两路独立 Dense；结构化来源留在 `_source`，最终条件回查 MySQL 正式成果。**

| 已确认，直接沿用 | 本文具体化，待 review |
| --- | --- |
| 人名、Timeline、固定 Chunk；正式事实与检索投影分离 | 全文采用 `lexical.<类别>` 字符串数组，原文/译文对白物理分栏 |
| BM25 + 文字 Dense + 视频 Dense；Sparse 默认关闭 | 类别字段的权重、CJK 分析器基线、严格原话归一规则 |
| questions 先入词项；不能作为对白证据 | `lexical_fields` 只保存来源，不建立 nested 索引 |
| 默认 Chunk 粒度；相邻 Chunk 关联；两路 Dense 独立 | 具体文本模板、示例启用 summary、源摘要规范化与 SHA-256 |
| 当前选定 Video COMPLETE 才对外开放；同任务、相符版本回查 | 当前待完善的物理字段、写入版本保护不能当已完成能力 |

当前实验代码仍用 `lexical_fields: nested` 和 `score_mode: max`。本文是其替代候选方案，不是现有代码行为说明。本文新增的评审字段 `projection_contract_id` 表示投影规则版本；它不复用现有代码中表示逐文档内容哈希的 `projection_revision`，避免混淆合同版本与内容指纹。普通 `object` 数组不能用来在 ES 内保证“同一句里的说话人与文本”；本方案明确不索引来源数组，从同版正式事实核验此关系。

| 比较 | nested 逐记录方案 | 本稿推荐的类别字段方案 |
| --- | --- | --- |
| 评分 | 单记录评分后合并 Chunk；现有 max 只保留最强记录 | 每个类别计算该 Chunk 的 BM25，跨类别按查询加权组合 |
| 关联 | 可以在同一 nested 记录内限制人物、语言和时间 | 候选阶段可能错配，最终必须回查同一事件 |
| 命中解释 | `inner_hits` 提供命中记录 | 从来源记录与正式 Timeline 重新核验；不把高亮当证据 |
| 代价 | 隐藏 Lucene 文档较多，查询更复杂 | 索引结构直接，但回查量、错误候选与补召回成本需评测 |

若实际评测表明人物/对白误配导致大量候选浪费，再 review nested。不能把“查询更简单”当成已经证明效果更好。

## 2. 同一个样本：正式 Timeline → ChunkSource

宿主提供可信范围 `ws1 / s1 / v1`、选定 Video 任务 `t7`、当前 Chunk `c012`、内容版本 `12`。ES 的 `parse_task_id` 就是这里的 `video_parse_task_id`，不是另一类任务。版本 12 是内容版本，不是第 12 次解析、同步 generation 或索引发布版本。

拓扑：`c011=[220,240)`，`c012=[240,260)`，`c013=[260,280)`。时间为原视频绝对整数秒半开区间。

下面是用于这个投影样本的一份完整正式 `VideoParseResult`，不是 MySQL 表定义。宿主应把这份正式数组及版本固定后交给 Engine；ORM 如何装配它仍遵循既有 Repository 合同，不新增整集成果表。

```json
{
  "persons": ["千早爱音", "要乐奈"],
  "timeline": [
    {"event_type": "ACTION", "start_seconds": 236, "end_seconds": 239, "description": "千早爱音跑进走廊。", "participants": ["千早爱音"], "visible_persons": ["千早爱音"]},
    {
      "event_type": "DIALOGUE",
      "start_seconds": 240,
      "end_seconds": 243,
      "description": "千早爱音表示自己没事。",
      "participants": ["千早爱音", "要乐奈"],
      "visible_persons": ["千早爱音", "要乐奈"],
      "speaker": "千早爱音",
      "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "大丈夫。"}, {"language": "zh-Hans", "role": "TRANSLATION", "text": "我没事。"}]
    },
    {"event_type": "ACTION", "start_seconds": 244, "end_seconds": 247, "description": "千早爱音转身擦眼泪。", "participants": ["千早爱音"], "visible_persons": ["千早爱音"]},
    {
      "event_type": "ON_SCREEN_TEXT",
      "start_seconds": 248,
      "end_seconds": 250,
      "description": "门上显示练习室标识。",
      "participants": [],
      "visible_persons": [],
      "language": "zh-Hans",
      "text": "练习室"
    },
    {"event_type": "SCENE", "start_seconds": 258, "end_seconds": 264, "description": "千早爱音与要乐奈离开走廊。", "participants": ["千早爱音", "要乐奈"], "visible_persons": ["千早爱音", "要乐奈"]}
  ]
}
```

正式事件中没有 `timeline_index`、视频 ID、向量或 ES 字段。图中标出的 `[0]` 至 `[4]` 是数组位置，由 Engine 投影时使用。

`c012` 消费 `timeline[1,2,3,4]`；`timeline[0]` 只在邻块背景里。相交条件为 `event.start < chunk.end && event.end > chunk.start`。跨块的 `[258,264)` 事件仍保留完整时间，也可进入 c013；不能裁改事实或把下标重排为 `[0,1,2,3]`。

本样本的 ChunkSource：

```json
{
  "chunk_id": "c012",
  "timeline": [
    {
      "event_type": "DIALOGUE",
      "start_seconds": 240,
      "end_seconds": 243,
      "description": "千早爱音表示自己没事。",
      "participants": ["千早爱音", "要乐奈"],
      "visible_persons": ["千早爱音", "要乐奈"],
      "speaker": "千早爱音",
      "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "大丈夫。"}, {"language": "zh-Hans", "role": "TRANSLATION", "text": "我没事。"}]
    },
    {"event_type": "ACTION", "start_seconds": 244, "end_seconds": 247, "description": "千早爱音转身擦眼泪。", "participants": ["千早爱音"], "visible_persons": ["千早爱音"]},
    {
      "event_type": "ON_SCREEN_TEXT",
      "start_seconds": 248,
      "end_seconds": 250,
      "description": "门上显示练习室标识。",
      "participants": [],
      "visible_persons": [],
      "language": "zh-Hans",
      "text": "练习室"
    },
    {"event_type": "SCENE", "start_seconds": 258, "end_seconds": 264, "description": "千早爱音与要乐奈离开走廊。", "participants": ["千早爱音", "要乐奈"], "visible_persons": ["千早爱音", "要乐奈"]}
  ],
  "enrichments": [{"language": "zh-Hans", "summary": "爱音表示自己没事，随后转身擦眼泪。", "keywords": ["我没事", "转身", "擦眼泪"], "questions": ["千早爱音说“我没事”后做了什么？"]}],
  "context_before": {
    "chunk_id": "c011",
    "timeline": [{"event_type": "ACTION", "start_seconds": 236, "end_seconds": 239, "description": "千早爱音跑进走廊。", "participants": ["千早爱音"], "visible_persons": ["千早爱音"]}],
    "enrichments": []
  },
  "context_after": {
    "chunk_id": "c013",
    "timeline": [
      {
        "event_type": "SCENE",
        "start_seconds": 258,
        "end_seconds": 264,
        "description": "千早爱音与要乐奈离开走廊。",
        "participants": ["千早爱音", "要乐奈"],
        "visible_persons": ["千早爱音", "要乐奈"]
      }
    ],
    "enrichments": []
  }
}
```

`context_before/after` 是读取后装配的消费视图，不是要把邻块复制持久化到当前 Chunk。索引默认不消费邻块背景；c011 的奔跑不能成为 c012 的奔跑证据。邻块展开还需核对它自己的内容版本，不能把所有 Chunk 的版本号都假定为 12。

`enrichments[0]` 是已启用的人工示例增强。未启用时为 `[]`，不因此阻塞基础索引。这里没有生成任何“为什么哭”的因果事实。

## 3. Engine 从同一来源并行派生三路输入

### 3.1 词项内容与来源

逻辑字段保持 TRD 的七类；物理存储把 dialogue 按 ORIGINAL / TRANSLATION 分成两个全文字段，便于原声限定时圈定候选。语言仍保留在来源记录中；本稿不按语言无限增加动态字段。

| 逻辑来源 | ES 检索字段 | 样本值 / 用途 |
| --- | --- | --- |
| Dialogue 原文 | lexical.dialogue_original | 大丈夫。；原声对白候选 |
| Dialogue 译文 | lexical.dialogue_translation | 我没事。；译文候选，返回时标明译文 |
| 非对白画面文字 | lexical.screen_text | 练习室；不与对白字幕重复存事实 |
| 本块事件中的人名 | lexical.person_names / .exact | 千早爱音、要乐奈；全文 / 专名过滤 |
| 本块事件描述 | lexical.event_description | 本块四条 description，含跨界事件完整描述 |
| 增强关键词 / 问题 / 摘要 | lexical.keywords / questions / summary | 独立类别字段，不能伪装为原话 |

原文保留；词项由 ES 分析器生成，不要求 Engine 先拼出 token 字符串。下面是完整来源记录清单。`item_index` 是本文提出的增强数组位置字段，原有实验类型还没有它；无需为它新增 SQL 列。

```json
[
  {"field": "event_description", "raw_text": "千早爱音表示自己没事。", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
  {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
  {"field": "person_names", "raw_text": "要乐奈", "language": null, "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
  {"field": "dialogue", "raw_text": "大丈夫。", "language": "ja", "role": "ORIGINAL", "speaker": "千早爱音", "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
  {
    "field": "dialogue",
    "raw_text": "我没事。",
    "language": "zh-Hans",
    "role": "TRANSLATION",
    "speaker": "千早爱音",
    "start_seconds": 240,
    "end_seconds": 243,
    "timeline_index": 1
  },
  {"field": "event_description", "raw_text": "千早爱音转身擦眼泪。", "language": "zh-Hans", "start_seconds": 244, "end_seconds": 247, "timeline_index": 2},
  {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 244, "end_seconds": 247, "timeline_index": 2},
  {"field": "event_description", "raw_text": "门上显示练习室标识。", "language": "zh-Hans", "start_seconds": 248, "end_seconds": 250, "timeline_index": 3},
  {"field": "screen_text", "raw_text": "练习室", "language": "zh-Hans", "start_seconds": 248, "end_seconds": 250, "timeline_index": 3},
  {"field": "event_description", "raw_text": "千早爱音与要乐奈离开走廊。", "language": "zh-Hans", "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
  {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
  {"field": "person_names", "raw_text": "要乐奈", "language": null, "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
  {"field": "summary", "raw_text": "爱音表示自己没事，随后转身擦眼泪。", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0},
  {"field": "keywords", "raw_text": "我没事", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 0},
  {"field": "keywords", "raw_text": "转身", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 1},
  {"field": "keywords", "raw_text": "擦眼泪", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 2},
  {"field": "questions", "raw_text": "千早爱音说“我没事”后做了什么？", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 0}
]
```

每条事件来源复用外层 `(task, chunk, content_version)`；对白再用 `timeline_index + language + role` 定位同一变体。增强用 `enrichment_index + field + item_index` 定位（summary 无 item_index）。增强的 `[240,260)` 只说明 Chunk 归属范围，不是逐句事件时间。

### 3.2 文字 Dense 的确切输入

推荐按正式数组的时间顺序确定性渲染，保留对白原文/译文、人物、事件描述、画面文字，附加已启用摘要。这次没有额外调用理解模型。以下为完整输入：

```text
[240,243) DIALOGUE：千早爱音表示自己没事。参与人物：千早爱音、要乐奈。
说话人：千早爱音。原文(ja)：大丈夫。译文(zh-Hans)：我没事。
[244,247) ACTION：千早爱音转身擦眼泪。参与人物：千早爱音。
[248,250) ON_SCREEN_TEXT：门上显示练习室标识。画面文字(zh-Hans)：练习室。
[258,264) SCENE：千早爱音与要乐奈离开走廊。参与人物：千早爱音、要乐奈。
本块摘要(zh-Hans)：爱音表示自己没事，随后转身擦眼泪。
```

来源下标 `[1,2,3,4]` 留作元数据，不进入编码正文。questions、keywords、相邻 Chunk 背景默认不再拼入该向量。摘要增强可关闭，关闭后输入指纹随之改变。示例全部内容能放入一次模型输入；超限时按 TRD 按连续时间段拆分表示并归并 Chunk，禁止静默截尾；该多表示物理扩展尚需单独 review，本稿的一文档双向量只覆盖通常的一表示样本。

### 3.3 视频 Dense 的确切输入

这里的“视频 Dense”描述视觉检索通道，**不要求编码器只能接收视频文件**。媒体来源都是本 Chunk 的原视频范围，交给编码器时允许两种输入：

```text
c012 的原视频范围 [240,260)
  ├─ 视频片段 → 支持视频的 Encoder
  └─ 按合同抽帧 → 有序帧 + 各帧绝对时间 → 支持该输入的视觉 Encoder
      ↓
  Chunk 级 vision_vector（或经评审的多表示，再归并到 Chunk）
```

抽帧输入的教学示例是 `[(240.0, frame_A), (245.0, frame_B), (250.0, frame_C), (255.0, frame_D)]`；这只是表达时间和顺序，不是在确定 5 秒采样间隔。帧数、采样、尺寸、预处理、多帧联合编码或帧表示聚合方式属于编码合同，待 review；本稿不擅自采用平均池化，也不自动建立一帧一条 ES 文档。用于 Dense 的采样帧本身不等于足以证明帧级同源定位。

下面的 JSON 是两种输入共用的媒体来源描述；具体输入形态和采样规则由 `sampling_contract` 指向的合同明确，临时帧路径不作为索引身份：

```json
{
  "video_id": "v1",
  "chunk_id": "c012",
  "start_seconds": 240,
  "end_seconds": 260,
  "media_identity": "sample-immutable-media-v1",
  "sampling_contract": "existing-video-sampling-example-v1"
}
```

这描述 c012 对应的真实原视频 `[240,260)`，并允许由它抽取有序帧进行编码。`media_identity` / `sampling_contract` 是评审占位值，实际复用现有媒体身份与编码合同；复用身份应包含输入形态、采样时间、预处理及编码规则。宿主生成 Task / Lease 隔离的临时片段或帧，交 Gateway 编码；临时路径不写入 Source 或 ES。

截至 2026-09-29，Understanding 已支持有序帧，但当前 `VideoParseIndexingService` 固定裁剪视频并调用 `embed_video`；抽帧 Dense 仍是待接入能力。不能把解析抽帧接线当作 Dense 抽帧接线已经完成。

Query 文字分别经文字语义编码器与视频空间相容的文字编码器，查询各自的 Dense 字段。维度相同也不能证明向量空间相容。本稿不选新模型、不重新调用付费模型。

## 4. ES JSON 与 mapping 提案

示例物理索引名：`anivag_chunks_dev_review_v1`，文档 `_id=t7:c012`。此 ID 不包含 Interval ID，避免重叠窗口产生重复文档；不包含内容版本，重建替换同一任务的同一 Chunk。索引名只表示物理配置，不能当作跨库发布状态。

**下方向量数值为手写示意，4/6 维只是让完整 JSON 可阅读，不是实际模型维度或部署配置。** 落地必须用各路确定的模型维度替换 mapping 与输入。两路独立采用 cosine / HNSW 是此评审基线，不做 HNSW 参数调优。

完整 `_source`（没有省略来源记录）：

```json
{
  "workspace_id": "ws1",
  "series_id": "s1",
  "video_id": "v1",
  "video_chunk_id": "c012",
  "parse_task_id": "t7",
  "content_version": 12,
  "chunk_start_seconds": 240,
  "chunk_end_seconds": 260,
  "projection_contract_id": "chunk-projection-review-v1",
  "retrieval_contract_id": "chunk-retrieval-review-v1",
  "semantic_encoder_id": "text-encoder-example-v1",
  "vision_encoder_id": "video-encoder-example-v1",
  "source_revision": "sha256:37e5b015765c252e192c26bdd2d370939bcbed576ce48a892d19ee0def9cd8ad",
  "lexical": {
    "dialogue_original": ["大丈夫。"],
    "dialogue_translation": ["我没事。"],
    "screen_text": ["练习室"],
    "person_names": ["千早爱音", "要乐奈"],
    "event_description": ["千早爱音表示自己没事。", "千早爱音转身擦眼泪。", "门上显示练习室标识。", "千早爱音与要乐奈离开走廊。"],
    "keywords": ["我没事", "转身", "擦眼泪"],
    "questions": ["千早爱音说“我没事”后做了什么？"],
    "summary": ["爱音表示自己没事，随后转身擦眼泪。"]
  },
  "lexical_fields": [
    {"field": "event_description", "raw_text": "千早爱音表示自己没事。", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
    {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
    {"field": "person_names", "raw_text": "要乐奈", "language": null, "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
    {"field": "dialogue", "raw_text": "大丈夫。", "language": "ja", "role": "ORIGINAL", "speaker": "千早爱音", "start_seconds": 240, "end_seconds": 243, "timeline_index": 1},
    {
      "field": "dialogue",
      "raw_text": "我没事。",
      "language": "zh-Hans",
      "role": "TRANSLATION",
      "speaker": "千早爱音",
      "start_seconds": 240,
      "end_seconds": 243,
      "timeline_index": 1
    },
    {"field": "event_description", "raw_text": "千早爱音转身擦眼泪。", "language": "zh-Hans", "start_seconds": 244, "end_seconds": 247, "timeline_index": 2},
    {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 244, "end_seconds": 247, "timeline_index": 2},
    {"field": "event_description", "raw_text": "门上显示练习室标识。", "language": "zh-Hans", "start_seconds": 248, "end_seconds": 250, "timeline_index": 3},
    {"field": "screen_text", "raw_text": "练习室", "language": "zh-Hans", "start_seconds": 248, "end_seconds": 250, "timeline_index": 3},
    {"field": "event_description", "raw_text": "千早爱音与要乐奈离开走廊。", "language": "zh-Hans", "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
    {"field": "person_names", "raw_text": "千早爱音", "language": null, "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
    {"field": "person_names", "raw_text": "要乐奈", "language": null, "start_seconds": 258, "end_seconds": 264, "timeline_index": 4},
    {"field": "summary", "raw_text": "爱音表示自己没事，随后转身擦眼泪。", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0},
    {"field": "keywords", "raw_text": "我没事", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 0},
    {"field": "keywords", "raw_text": "转身", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 1},
    {"field": "keywords", "raw_text": "擦眼泪", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 2},
    {"field": "questions", "raw_text": "千早爱音说“我没事”后做了什么？", "language": "zh-Hans", "start_seconds": 240, "end_seconds": 260, "enrichment_index": 0, "item_index": 0}
  ],
  "semantic_text": "[240,243) DIALOGUE：千早爱音表示自己没事。参与人物：千早爱音、要乐奈。\n说话人：千早爱音。原文(ja)：大丈夫。译文(zh-Hans)：我没事。\n[244,247) ACTION：千早爱音转身擦眼泪。参与人物：千早爱音。\n[248,250) ON_SCREEN_TEXT：门上显示练习室标识。画面文字(zh-Hans)：练习室。\n[258,264) SCENE：千早爱音与要乐奈离开走廊。参与人物：千早爱音、要乐奈。\n本块摘要(zh-Hans)：爱音表示自己没事，随后转身擦眼泪。",
  "vision_input": {
    "video_id": "v1",
    "chunk_id": "c012",
    "start_seconds": 240,
    "end_seconds": 260,
    "media_identity": "sample-immutable-media-v1",
    "sampling_contract": "existing-video-sampling-example-v1"
  },
  "semantic_vector": [0.12, -0.03, 0.44, 0.71],
  "vision_vector": [0.21, 0.51, -0.12, 0.31, 0.17, 0.61]
}
```

`lexical_fields`、semantic_text、vision_input 是可重建的检索输入及来源副本，不是第二份可独立编辑的正式事实。根层不额外复制 COMPLETE 状态，检索资格从 MySQL 选定任务取得。

完整 mapping 配置草案：

```json
{
  "settings": {"analysis": {"analyzer": {"anivag_cjk_review": {"type": "cjk", "stopwords": "_none_"}}}},
  "mappings": {
    "dynamic": "strict",
    "_meta": {"status": "DESIGN_REVIEW_ONLY", "vector_values": "synthetic; dims 4/6 are illustrative, not a model choice"},
    "properties": {
      "workspace_id": {"type": "keyword"},
      "series_id": {"type": "keyword"},
      "video_id": {"type": "keyword"},
      "video_chunk_id": {"type": "keyword"},
      "parse_task_id": {"type": "keyword"},
      "projection_contract_id": {"type": "keyword"},
      "retrieval_contract_id": {"type": "keyword"},
      "semantic_encoder_id": {"type": "keyword"},
      "vision_encoder_id": {"type": "keyword"},
      "source_revision": {"type": "keyword"},
      "content_version": {"type": "long"},
      "chunk_start_seconds": {"type": "long"},
      "chunk_end_seconds": {"type": "long"},
      "lexical": {
        "type": "object",
        "dynamic": "strict",
        "properties": {
          "dialogue_original": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "dialogue_translation": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "screen_text": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "person_names": {
            "type": "text",
            "analyzer": "anivag_cjk_review",
            "search_analyzer": "anivag_cjk_review",
            "similarity": "BM25",
            "position_increment_gap": 100,
            "fields": {"exact": {"type": "keyword"}}
          },
          "event_description": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "keywords": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "questions": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100},
          "summary": {"type": "text", "analyzer": "anivag_cjk_review", "search_analyzer": "anivag_cjk_review", "similarity": "BM25", "position_increment_gap": 100}
        }
      },
      "lexical_fields": {"type": "object", "enabled": false},
      "semantic_text": {"type": "text", "index": false},
      "vision_input": {"type": "object", "enabled": false},
      "semantic_vector": {"type": "dense_vector", "dims": 4, "index": true, "similarity": "cosine", "index_options": {"type": "hnsw"}},
      "vision_vector": {"type": "dense_vector", "dims": 6, "index": true, "similarity": "cosine", "index_options": {"type": "hnsw"}}
    }
  }
}
```

关键选择：

- `lexical` 是普通字段命名空间，其值为纯文本数组；它不是承载人物/台词对象关联的数组。每个类别有自己的 BM25 统计，查询显式加权组合，不称为“全内容一个 BM25 分数”。
- `lexical_fields: object, enabled:false` 只留 `_source`。ES 不验证/索引内部结构，Engine 必须先校验来源合同；不能对它做 nested query、过滤或 inner_hits。[ES enabled](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/enabled.html)
- 评审基线使用 CJK 分析器并关闭停用词删除，索引/查询共用配置。它是可验证的最小起点，中文、日文专名、混合字符的 `_analyze` 和真实召回尚未验证，不能宣称效果最优。[语言分析器](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/analysis-lang-analyzer.html#cjk-analyzer)
- 每句话/问题保留独立数组项，显式 `position_increment_gap=100`，短语候选 `slop=0`；不把不同数组项拼出一句假台词。短语匹配仍不等于原字符串匹配。[位置间隔](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/position-increment-gap.html)
- `person_names.exact` 是人名 keyword，不是 person_id。它只能证明本块出现该人，不能证明这个人说过目标句。[数组语义](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/array.html)
- `dense_vector` 两字段分别固定维度和相似度。Sparse 与独立问题向量关闭时，本样本不写对应字段。[Dense mapping](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/dense-vector.html)

若采用本稿方案，现有 nested mapping 不能通过给原字段原地改类型完成迁移，需要另建相容物理索引并从正式成果重建。本批只提出设计，未执行建索引或数据切换。

## 5. 索引源指纹与向量复用

依据主计划 §4.2，本文建议：

```text
source_revision = SHA256(canonical_json(
  scope + 全部 lexical_fields + 实际 lexical 字段 + semantic_text
  + vision_input + projection / retrieval / 两路 encoder 合同
))
```

本样本规范化为 UTF-8 JSON、key 排序、无多余空格、数组顺序固定、禁止 NaN；原文不做语义改写。算法和规范化细节是提案。词项、来源位置、人物、问题、投影与分析合同均参与，不只计算 semantic_text。

编码器合同必须标识模型版本、维度、输入处理和采样等有效参数；同名但实际行为不同不算相容。媒体身份必须标识不可变内容，不使用临时 URL。

各路复用单独比较实际输入与编码合同，不能把整个 source_revision 变化当成双路重新花钱编码的理由：

| 改动 | ES 投影 | 文字 Dense | 视频 Dense |
| --- | --- | --- | --- |
| 只改 questions / keywords | 重写词项与 source_revision | 输入不变则复用 | 媒体与合同不变则复用 |
| 改 Dialogue / ACTION 描述 | 重建受影响表示 | 文字改变需重编码 | 媒体与合同不变则复用 |
| 改采样 / 视频编码模型 | 更新合同与投影 | 输入与合同不变则复用 | 重新编码 |
| 只改 detail.scope / compact 元数据 | 若实际检索输入未变，不重建 | 复用 | 复用 |

source_revision 是相等性指纹，没有先后顺序；content_version 只说明正式内容变化，两者都不是同步 generation 或发布版本。向量复用检查由 Server 用已有存储路径承接，本稿不建立新缓存表。

## 6. 同一个 Query 如何查三路

查询样本：**千早爱音说“我没事”后转身擦眼泪。** 本例允许用已确认译文满足引号线索，并在结果中明确 TRANSLATION；若用户限定“原声原话”，只允许 ORIGINAL。

入口从 MySQL 取得可信工作区/作品范围与当前选定 COMPLETE 任务，固定 `v1→t7`。三路共享同一任务映射和兼容合同。示例只有一个视频；多视频时按 `(video_id, parse_task_id)` 配对过滤，不能分别做两个 terms 后形成错误组合。候选的 content_version 是各 Chunk 自己的版本，不把“12”当作全库版本过滤。

下面是三条独立 ES 请求草案。权重和 Top 50 / num_candidates 100 是起步评测参数，不代表已测最优；query_vector 仍为手写示意。全文条件不进入 Dense 的强制 filter。

词项路：

```json
{
  "size": 50,
  "query": {
    "bool": {
      "filter": [
        {"term": {"workspace_id": "ws1"}},
        {"terms": {"series_id": ["s1"]}},
        {"bool": {"should": [{"bool": {"filter": [{"term": {"video_id": "v1"}}, {"term": {"parse_task_id": "t7"}}]}}], "minimum_should_match": 1}},
        {"term": {"retrieval_contract_id": "chunk-retrieval-review-v1"}},
        {"term": {"projection_contract_id": "chunk-projection-review-v1"}}
      ],
      "should": [
        {"match": {"lexical.dialogue_original": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 3}}},
        {"match": {"lexical.dialogue_translation": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 3}}},
        {"match": {"lexical.screen_text": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 3}}},
        {"match": {"lexical.person_names": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 2}}},
        {"match": {"lexical.event_description": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 1}}},
        {"match": {"lexical.keywords": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 1}}},
        {"match": {"lexical.questions": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 0.5}}},
        {"match": {"lexical.summary": {"query": "千早爱音说“我没事”后转身擦眼泪", "boost": 0.5}}},
        {"match_phrase": {"lexical.dialogue_translation": {"query": "我没事", "slop": 0, "boost": 6}}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": {"excludes": ["semantic_vector", "vision_vector"]}
}
```

文字语义路：

```json
{
  "size": 50,
  "knn": {
    "field": "semantic_vector",
    "query_vector": [0.22, 0.08, 0.31, 0.71],
    "k": 50,
    "num_candidates": 100,
    "filter": {
      "bool": {
        "filter": [
          {"term": {"workspace_id": "ws1"}},
          {"terms": {"series_id": ["s1"]}},
          {"bool": {"should": [{"bool": {"filter": [{"term": {"video_id": "v1"}}, {"term": {"parse_task_id": "t7"}}]}}], "minimum_should_match": 1}},
          {"term": {"retrieval_contract_id": "chunk-retrieval-review-v1"}},
          {"term": {"projection_contract_id": "chunk-projection-review-v1"}},
          {"term": {"semantic_encoder_id": "text-encoder-example-v1"}}
        ]
      }
    }
  },
  "_source": {"excludes": ["semantic_vector", "vision_vector"]}
}
```

视频语义路（文字经视频空间相容的 Query Encoder）：

```json
{
  "size": 50,
  "knn": {
    "field": "vision_vector",
    "query_vector": [0.14, 0.53, -0.02, 0.35, 0.18, 0.69],
    "k": 50,
    "num_candidates": 100,
    "filter": {
      "bool": {
        "filter": [
          {"term": {"workspace_id": "ws1"}},
          {"terms": {"series_id": ["s1"]}},
          {"bool": {"should": [{"bool": {"filter": [{"term": {"video_id": "v1"}}, {"term": {"parse_task_id": "t7"}}]}}], "minimum_should_match": 1}},
          {"term": {"retrieval_contract_id": "chunk-retrieval-review-v1"}},
          {"term": {"projection_contract_id": "chunk-projection-review-v1"}},
          {"term": {"vision_encoder_id": "video-encoder-example-v1"}}
        ]
      }
    }
  },
  "_source": {"excludes": ["semantic_vector", "vision_vector"]}
}
```

人物+对白+动作是三类信号。候选阶段允许宽召回，后面的硬条件核验不可省略。明确原声查询时，短语候选改查 dialogue_original；不靠 questions 的匹配证明原声说过。

每路先按 Chunk 与来源版本归并；三路融合前保留 `(task, chunk, content_version, source_revision, compatible_contract)`，不同来源指纹不能合成一个“完整命中”。同一通道的多字段、多语变体只贡献一份 Chunk 排名。

例如人工演示排名：c012 在词项路第 2、文字 Dense 第 1、视频 Dense 第 4；等权 RRF 得分为 `1/62 + 1/61 + 1/64 ≈ 0.04815`。这不是运行结果，也不是正确概率。融合产出的是有限候选池，启用 rerank 时不能先按最终返回条数裁掉其候选。

### 6.1 可选相关性精筛 / rerank（必须保留的阶段）

```text
各路 Chunk 归并 → 加权 RRF / 有限候选池
  → 校验候选身份与版本，取同版真实文本 / 视频片段 / 有序帧
  → 按需要展开邻块（保留各自来源与时间）
  → 可选 encoder / LLM / 专用多模态 reranker 打相关性分
  → 按策略重排、筛选，得到待核验候选
  → 核验原话、人物、动作与时间等硬条件 → 最终 Top N
```

关闭 rerank 时从候选池进入证据核验；启用时由 Engine 组织 Query 和候选证据、消费评分并排序，Gateway 承担模型调用、预算、超时与取消，Server 负责同版事实和媒体读取。其他 Encoder、LLM 或专用 Reranker 都是可评审的评分实现，不要求复用召回的编码器。

精筛输入使用实际事件、对白或真实画面；questions 只说明召回线索，不能包装成真实对白。带先后关系的 Query 应保留帧时间、顺序及对白；普通语义相似与同源定位仍使用不同证据标准。模型相关性分数不能覆盖权限、COMPLETE、版本一致性或明确硬条件，也不等同于命中概率。

当前源码尚无检索专用 rerank 合同与接线。待 review 的策略包括输入组合、模型、候选预算、分数语义、排序/阈值、超时或失败处理；上一版直接从 RRF 连到最终核验漏画了这一步。本修订只恢复 TRD 的设计位置，不自行确定模型或阈值。

## 7. 证据回查：通过与拒绝都要明确

对样本候选回查 `(t7,c012,content_version=12)`。宿主必须能返回索引时同一份正式数组；不能把过滤后数组下标当正式下标。若目前只存可变当前值，发现版本变化就丢弃候选，不能用最新事实去填旧候选，也不为本稿新增历史快照表。

依次校验：任务仍是当前选定 COMPLETE；Chunk 仍存在且范围可信；来源版本与合同相符；`timeline[1]` 是同一句 Dialogue 的同一语言变体；`speaker=千早爱音`；字面包含“我没事”；`timeline[2].event_type=ACTION` 且描述/相应证据实际支持“千早爱音转身擦眼泪”；`243 <= 244` 满足对白结束后开始动作。参与人物名单与时间先后本身不能证明动作类型或执行者；本例可由明确字面描述核验，通用语义核验沿用后续有预算的证据策略。多事件关系只证明这两个已记录事件的先后，不自动证明因果或情绪。

本文建议的严格引号规则：对 query 和单条变体做 Unicode NFC 后，检查保留字面和标点的连续子串；不做同义改写、不转简繁、不拼多句。引号本身由查询解析剥离，不随意删正文字符。普通 BM25 的宽松分词与这一最终判据分离；具体归一细节待 review。

通过后返回的内部结果示例（不是新 API 契约）：

```json
{
  "video_id": "v1",
  "video_chunk_id": "c012",
  "parse_task_id": "t7",
  "content_version": 12,
  "chunk_range_seconds": [240, 260],
  "matched_range_seconds": [240, 247],
  "evidence": [
    {"timeline_index": 1, "event_type": "DIALOGUE", "speaker": "千早爱音", "language": "zh-Hans", "role": "TRANSLATION", "text": "我没事。", "range_seconds": [240, 243]},
    {"timeline_index": 2, "event_type": "ACTION", "participants": ["千早爱音"], "description": "千早爱音转身擦眼泪。", "range_seconds": [244, 247]}
  ],
  "localization": "event_seconds"
}
```

| 边界样本 | 正确结果 |
| --- | --- |
| 中文“我没事”命中本样本 | 可以提供译文证据，标 zh-Hans / TRANSLATION；不能说原声为中文 |
| 用户限定中文原声“我没事” | 本例只有日语 ORIGINAL，拒绝该硬条件 |
| 仅 questions 中出现“后做了什么”，实际对白没有 | 不能当作说过这句话；问题仅提供 Chunk 级线索 |
| 同块爱音出现，但台词由另一个人说 | 人名全文命中不算 speaker 一致，拒绝 |
| 只找到对白，未找到同一人的后续动作 | 不满足完整查询；在预算内展开邻块/补搜，仍缺证据则不返回为成功 |
| ES v12，MySQL 已 v13 | 丢弃旧候选并安排更新，不用旧 timeline_index 读取 v13 |
| 查询期间任务变为 INDEXING 或被删除 | 返回前重新校验，丢弃；旧 ES 残留不能绕过资格 |
| 只看本块却命中跨界 [258,264) 离开动作 | 保留原事件时间；播放 c012 只覆盖到 260，完整证据需展开 c013 |

返回前状态/版本检查是应用层验证时点，不是 MySQL 与 ES 的跨库事务，也不承诺检查结束以后永不发生变化。真实并发、读事务和删除交错还需实现与集成验证。

本样本只能支持整数秒事件级定位；没有逐词时间、帧号或像素级同源证据，不能把 `[240,247)` 标成帧级定位结果。

## 8. 写入时序、图片/GIF 边界与未决点

索引路径沿用最新 TRD：所有包含 c012 的 Interval 均已提交 Refine（INDEXING / INDEXING_FAILED / COMPLETE）→ 读取当前有效 Chunk 及版本 → 派生三路/复用向量 → 完整文档写入 → 逐项成功与查询可见 → 校验来源仍有效 → Interval 成员全部就绪后 COMPLETE → Video 汇总 COMPLETE。

一个 Interval 可先处理已放行的成员；等待余下成员不算失败、不持租约。首建/重建均不读取旧 Interval 快照覆盖当前有效 Chunk。基础 Chunk COMPLETE 仅表示 Understanding 成果已提交。

稳定 `_id` 只解决文档身份，不解决迟到旧写覆盖新内容。2026-09-29 源码已存在 `external_gte`、generation/content_version 组合版本、bulk 逐项检查、refresh 后完整来源比对及删除校验；此前“防迟到旧写仍待实现”的表述已过期。它们尚未完成真实 ES/MySQL 联合验收。同内容版本的投影合同重建如何获得正确写入顺序、未知成功超时和删除交错仍需专项核验；不能把无序的 source hash 当作有序写入合同。本稿不新造索引表。

| 输入 | 与本样本共用 | 本样本没有自动实现 |
| --- | --- | --- |
| 文字 | 已描述的 BM25 / 两路 Dense → Chunk → 证据核验 | 实际效果、延迟和预算还未评测 |
| 图片 | 兼容视觉 Query Encoder 召回 Chunk；复用归并与回查 | 裁剪同源匹配、帧级定位需帧/局部特征及精确时间来源 |
| GIF | 多帧综合视觉召回，复用 Chunk 候选 | 定位原片段仍需先后顺序/时间对齐；语义相似可放宽 |

不把图片/GIF 强制识别成文字，也不假定两个 Chunk Dense 字段已经实现帧级检索。Sparse 默认关；启用策略、帧级物理存储及长文本多表示扩展另行设计。

已有文档仍有局部 embedding 后“可进入召回”的图示措辞；当前 TRD 正文明确保留 selected Video COMPLETE 门槛，本稿沿用该门槛，不自行改为局部对外开放。

## 9. 请优先 review 的三个点

1. **字段组织：**是否采用本稿“类别全文数组 + 来源只存 `_source` + 正式事实核验”的主案？重点看错误候选的代价；若要求 ES 内完成同句人物/时间绑定，应转回 nested。
2. **文字 Dense 配方：**本块事实按时间渲染，保留原文/译文，附加已启用 summary；questions / keywords / 邻块背景不默认再次拼入。重点看是否代表你希望检索的综合语义。
3. **原话与结果证据：**译文可作为明确标注的译文证据；“原声原话”只接受 ORIGINAL；生成问题不能提供事件时间。重点看严格字面规则与返回粒度是否符合产品预期。

另外请结合开发阶段图 review **抽帧 Dense 的输入边界与可选 rerank 的输入/输出位置**；具体模型与参数随后由你决定。分词、字段权重、候选数、真实向量模型维度由后续针对性验证收敛。本批仅修订评审材料，没有据此修改生产代码或迁移数据。

## 10. 本批验证与后续验收

已做：正式示例的结构/时间/人物关系核对，派生来源下标与增强位置核对，ES 文档字段与 mapping 的静态一致性检查，双路示意向量维度检查，source_revision 对词项/投影合同变化的敏感性与向量复用输入隔离检查，以及 Excalidraw JSON/布局检查。

未做：真实模型调用、真实 ES 建索引/_analyze/召回、真实 MySQL 状态恢复与并发测试、网站内导入验证。人工示例排名不能代替检索评测。

后续验收至少覆盖：词项与原话/译文边界、人物跨事件错配、问题命中无事实、跨 Chunk 时间、仅改词项触发重写但复用向量、三路来源不混合、bulk 部分失败/可见性、超时重试/旧写/删除交错。当前批次仅完成评审材料。
