先给结论：对 Seekino 来说，下一步不应该只是把“三路 RRF”机械扩成“四路、五路”，而应该建设一套“粗召回 → 细粒度定位 → 重排核验 → 时间证据展开”的分层检索。

稀疏向量/BM25 值得加入，但更适合作为低成本的词法召回实验；结合目前评测结果，真正更可能提高 Precision@1、缩短片段长度的，是 event/utterance/shot 级细粒度索引与候选重排。

## 一、先把整套 RAG 教程放回正确位置

这套教程可以按下面的链路理解：

| 阶段 | 核心问题 | 典型技术 | Seekino 对应物 |
|---|---|---|---|
| 数据准备 | 原始数据怎样变成证据 | 清洗、分块、语义切块 | 视频解析、ASR、TimelineEvent |
| 索引单元 | 什么东西作为一次召回候选 | Chunk、Sentence Window、父子节点 | 60 秒 VideoChunk、Event、Cue、Shot |
| 表示方法 | 候选怎样转成可检索表示 | Dense、Sparse、Multimodal、Multi-vector | text、vision、未来 lexical/event |
| 查询构建 | 用户的问题应怎样拆解 | Rewrite、Multi-query、Metadata Filter、Routing | Query Agent、Series Scope、通道权重 |
| 粗召回 | 怎样尽量不漏 | Dense Search、BM25、Hybrid Search | Transcript/Text/Vision Top-K |
| 融合与精排 | 怎样把正确项推到前面 | RRF、Cross-Encoder、ColBERT、LLM Reranker | Weighted RRF、未来 CandidateVerifier |
| 上下文展开 | 命中小证据后给模型看什么 | Sentence Window、Parent Retrieval | Event 命中后回源完整 Chunk |
| 生成 | 怎样稳定交给程序和前端 | Pydantic、JSON Schema、Function Calling | DraftItem、RenderBlock、Tool Calling |
| 评估 | 到底有没有变好 | Hit@K、MRR、Precision、Faithfulness | 当前 recall smoke 与后续 held-out 集 |

所以，“向量层”只是中间一环。RRF 是融合算法，不是召回模型；HNSW 是加速结构，不会让语义更聪明；格式化生成则是检索完成后的产品协议。

教程的相关章节分别是：[文本分块](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter2/05_text_chunking.md)、[索引优化](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter3/10_index_optimization.md)、[混合检索](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/11_hybrid_search.md)、[查询重构与路由](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/14_query_rewriting.md)、[检索进阶](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/15_advanced_retrieval_techniques.md)。

## 二、稀疏向量到底是什么

一句话：

> 密集向量主要匹配“意思像不像”，稀疏向量主要匹配“关键字有没有出现以及有多重要”。

假设词表中：

```text
18   -> 爽世
731  -> 春日影
1142 -> 演奏
```

文档“爽世质问为什么演奏春日影”可能表示为：

```text
indices = [18, 731, 1142]
values  = [1.7, 3.4, 1.2]
```

词表可能有几十万维，但这条文档只有三个位置非零，所以称为“稀疏”。

### BM25 和稀疏向量是什么关系

两者不是完全同一个概念：

- 稀疏向量是一种表示形式。
- BM25 是一种给词计算权重并对文档排序的算法。
- 倒排索引是让稀疏检索跑得快的数据结构。

BM25 主要考虑：

- TF：这个词在当前文档里出现多少次。
- IDF：这个词在整个库里有多稀有。
- 文档长度：相同关键词出现在短文档里通常比出现在超长文档里更重要。

因此查询“为什么要演奏春日影”时：

- Dense 可能召回“她质问大家为什么唱起那首旧歌”，即使没有“春日影”三个字。
- BM25 会强烈奖励真正含有“春日影”的片段。
- 两路并行，再用 RRF 融合，既覆盖语义改写，也保住专名。

这正是教程所说的 Hybrid Search：[Datawhale 混合检索](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/11_hybrid_search.md)。

### BM25、学习式稀疏和 Dense 的区别

| 表示 | 擅长 | 不擅长 | 维护成本 |
|---|---|---|---|
| BM25 稀疏 | 人名、歌名、原话、型号、OCR、罕见词 | 同义改写、跨语言 | 低，但要维护分词与索引 |
| SPLADE/BGE-M3 Sparse | 词法匹配，并学习词的重要性或扩展 | 模型更重，解释和版本管理更复杂 | 中 |
| Dense Text | 剧情、因果、关系、释义、同义表达 | 容易漏掉罕见专名和精确字面约束 | 中 |
| Vision/Video Dense | 动作、物体、颜色、构图、场景 | 长片段噪声、细台词、OCR | 高 |
| ColBERT Multi-vector | Token 级细粒度匹配、精排 | 存储和计算更大 | 高，适合 Top-K 重排 |

因此第一版 Sparse 建议从 BM25 开始，不要上来就维护 SPLADE。

### BM25 需要维护什么

不需要自己实现 BM25 数学公式，但需要维护：

1. 索引文本由哪些字段组成。
2. 中文、日文、英文怎样分词和归一化。
3. 建库和查询必须使用相同 tokenizer 配置。
4. 索引 revision、幂等同步和重建。
5. BM25 与 Dense/Vision 的融合权重。
6. 单路及融合后的回归评测。

Seekino 包含中文描述、日语原文和人物专名，不能直接使用默认英文分词。Qdrant 当前支持 `multilingual` tokenizer，官方明确建议非空格语言使用它，并要求写入和查询保持相同设置：[Qdrant Full-Text Search](https://qdrant.tech/documentation/search/text-search/full-text-search/)。

## 三、先校正一下当前两种向量的命名

按仓库当前实现，实际情况是：

| 输入 | 编码方式 | 正确叫法 |
|---|---|---|
| `self_compact + owned event description/text` | Text Embedding | 综合语义 Text Dense |
| VLM 生成的 caption，再走 Text Embedding | Text Embedding | Visual-caption Text Dense |
| canonical MP4/帧，直接走 `embed_video` | Video/Multimodal Embedding | Vision/Video Dense |

目前代码中的 `vision` 是第三种：直接对 canonical 视频调用 `embed_video()`，不是 caption 向量。[embed.py](/Users/bytedance/workspace/other-project/Seekino/engine/parse/embed.py:98)

当前 `text` 则由 `self_compact`、owned event description 和台词共同组成。[video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:95)

因此：

- 如果你说的“直接 caption 的 vision 向量”是指当前实现，应该叫“直接视频 Vision Vector”。
- 如果真的是“VLM 先写 caption，再把 caption 向量化”，它本质还是 Text Dense，只不过文本内容描述画面。
- Visual-caption Text 很可能和现有综合语义 Text 高度重合，除非评测证明有独立增益，否则不值得专门再维护一路。

## 四、Seekino 适合建立哪些检索层次

我建议按“路由层、粗召回层、证据原子层、精排层”组织，而不是平铺一堆向量。

### 1. 路由层：Series / Episode Summary

用途：

- 全库搜索时先决定哪一部作品、哪一集。
- Long-horizon 问题先定位相关剧集，再进入细节召回。

表示：

- Episode summary dense。
- Series/episode/title/order 等 metadata filter。

结论：

- 当前搜索本来就要求明确 Series Scope，所以暂时不是优先项。
- 以后做“跨整个动画库搜索”时再加。
- 只能用于路由，不能当作最终视频证据。

### 2. Chunk 粗召回层：当前主体

#### `text`：综合语义 Dense，保留

输入：

```text
self_compact
+ owned event descriptions
+ owned event dialogue text
```

负责：

- 剧情、人物关系、因果。
- 台词释义。
- 用户与原文措辞不同的查询。

当前设计不把 `window_summary` 和相邻 Context 写入向量，这是正确的：邻接上下文适合命中后展开，不适合污染当前 Chunk 的召回语义。

#### `vision`：直接视频 Dense，保留但不要盲目等权

负责：

- 动作、物体、颜色、服装、场景、镜头构图。

但当前一个向量压缩整个 60 秒视频，粒度明显偏粗。现有 smoke 中 Vision Hit@1 只有 25%，等权 RRF 反而低于 Text 单路；文档也已经明确指出整段 60 秒 Vision Vector 对台词和画面标题查询噪声较大。[ep07.recall-smoke.md](/Users/bytedance/workspace/other-project/Seekino/evals/recall/ep07.recall-smoke.md:43)

所以 Vision 应该：

- 按查询类型路由。
- 视觉问题提高权重。
- 台词/剧情问题降低权重。
- 长期方向是下沉到 shot/keyframe，而不是继续调 60 秒向量权重。

#### `lexical`：BM25 Sparse，建议先做实验

第一版可以在每个 60 秒 Chunk 上构建：

```text
self_compact
+ owned event description/text
+ 原始 ASR
+ 后续 OCR
```

但必须继续排除：

- 相邻 Chunk Context。
- `window_summary` 中不属于当前 Chunk 的内容。
- 生成模型推测出来、不能由当前片段证明的知识。

它主要解决：

- 人名、别名、歌名。
- 原话和口头禅。
- 屏幕文字。
- 稀有名词。
- Query 与原文只有部分关键词重合，而不是完整连续短语。

当前 Transcript 通道是规范化后的连续字面短语匹配，不是 BM25。[video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:242)

两者的关系是：

- Transcript exact：整段连续原话命中，精度最高。
- BM25：若干关键词可分散出现，召回更宽。
- Dense：不要求出现相同词，只要求语义接近。

三者可以共存。

### 3. Event / Utterance 证据原子层：最值得建设

这是我认为 Seekino 下一步最有价值的层。

当前解析结果已经有：

```text
event_type
description
text
start_seconds
end_seconds
video_chunk_id
```

可以将每个 `OwnedTimelineEvent` 变成独立检索 point：

```text
人物 + 事件类型 + 自包含动作/台词释义 + 原始台词
```

payload 保存：

```text
eventId
videoChunkId
seriesId
videoId
startSeconds
endSeconds
eventType
sourceRevision
```

然后：

1. 在 Event 上进行精准召回。
2. 将命中的 Event 映射回 `videoChunkId`。
3. 同一 Chunk 的多个 Event 先在通道内部聚合，避免“事件多的 Chunk 被重复投票”。
4. 再和 Chunk Text、Vision、BM25 做跨通道 RRF。
5. 回源时给模型看完整 Chunk 或 Event 前后若干 Cue。

这就是句子窗口思想在视频上的对应版本：

> 用小 Event 命中，用父 VideoChunk 补上下文，用 Event 时间定位最终播放范围。

Datawhale 对 Sentence Window 的核心描述也是“小块索引，大窗口返回”，并且窗口只存在 metadata 中、不参与 embedding：[索引优化章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter3/10_index_optimization.md)。

### 4. Shot / Keyframe Vision 层：视觉查询的长期方案

粒度可以是：

- 镜头切分后的 Shot。
- 3～10 秒视觉段。
- 代表性 Keyframe。
- 短段视频 embedding。

负责：

- 拥抱、离场、拿手机。
- 绿色荧光棒。
- 服装、物体、场所、构图。
- 一闪而过的画面。

命中后同样映射回父 `videoChunkId` 和精确时间。

这比给 60 秒视频生成更多不同模型的整体向量更有意义：问题主要是时间粒度被平均掉，不一定是模型种类太少。

### 5. OCR 层：屏幕文字专用

可以拆成：

- OCR sparse：准确命中标题、短信、路牌、片尾文字。
- OCR dense：按文字含义检索，作为可选补充。

类似 `#8 どうして` 这种片尾标题查询，BM25/OCR 会比整段 Vision Vector 稳定得多。

### 6. 后面再考虑的层

- Character reference image vectors：只有人物误认是主要失败类型时再做；通常先做别名表和人物 metadata。
- Audio/music vectors：只有产品明确支持“哪段响起某首歌/某种音效”再加。
- ColBERT Multi-vector：适合作为 Text/BM25 粗召回后的 Top-K 精排，不宜直接对全集所有 token 做第一层召回。
- Graph/relationship index：适合跨集人物关系和事件链，但它是知识结构层，不应冒充视频证据。
- HyDE：可能扩大语义召回，但会生成假设性内容；只能用于找候选，绝不能作为证据。

## 五、推荐的在线检索流程

```text
用户问题 + 有限对话历史
        │
        ├── 原始词法 Query
        │     保留人名、原话、歌名、OCR 字符
        │
        ├── Semantic Query
        │     补全指代，改写成可独立理解的事件描述
        │
        └── Visual Query
              提取人物外观、动作、物体、颜色、地点、构图
        │
        ▼
Series / Episode / Revision Metadata Filter
        │
        ├── Transcript exact
        ├── BM25 Sparse
        ├── Chunk Text Dense
        ├── Event Text Dense
        ├── Chunk Video Dense
        └── Shot / Keyframe Vision
        │
        ▼
Atom → videoChunkId 聚合、去重
        │
        ▼
Query-aware Weighted RRF
        │
        ▼
Top 20 Candidate Verifier / Reranker
        │
        ▼
MySQL 回源完整 Evidence
        │
        ▼
Event/Cue 时间范围 + 必要上下文展开
        │
        ▼
结构化答案与视频播放块
```

这里最重要的一个变化是：不要强迫同一个 Query 同时服务所有通道。

例如：

- BM25 必须尽可能保留用户原词。
- Semantic Query 可以消歧、补全人物与动作。
- Visual Query 应改写成画面可观察条件。
- 对话历史只用于补全“她、那首歌、刚才那个场景”，不应把整段聊天直接拼进 embedding。

教程的 Query Rewrite、Multi-query、Routing 正是在解决这一层问题：[查询重构与分发](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/14_query_rewriting.md)。

## 六、Qdrant 中怎么落地 Sparse

概念上，Chunk Collection 可以变成：

```python
vectors_config = {
    "text": DenseVectorParams(...),
    "vision": DenseVectorParams(...),
}

sparse_vectors_config = {
    "lexical": SparseVectorParams(modifier=Modifier.IDF),
}
```

写入时为一个 point 同时保存：

```text
text dense
vision dense
lexical sparse
payload
```

查询时：

1. 使用相同配置生成 BM25 Query Sparse Vector。
2. 带 `seriesId` 等 filter 查询 `lexical`。
3. Text、Vision 分别查询。
4. 将各自 Top-K 转成现有 `RankedVideoChunkMatch`。
5. 继续使用 Engine 侧 Weighted RRF。

Qdrant 也支持通过 `prefetch` 在库内执行 Dense + Sparse + RRF：[Qdrant Hybrid Queries](https://qdrant.tech/documentation/search/hybrid-queries/)。不过 Seekino 第一版建议继续保留应用层融合，因为：

- 还要加入 MySQL Transcript exact 通道。
- 要保存每路 rank/score 做诊断。
- 当前项目使用的是 1-based rank、`rrfK=60` 和自己的加权公式。
- Qdrant 当前 RRF 默认参数和加权公式与现实现并不完全相同，迁移需要专门做排序一致性测试。

另外，当前 Qdrant Adapter 只声明和写入 `text`、`vision` 两个 named vector。[qdrant.py](/Users/bytedance/workspace/other-project/Seekino/server/storage/qdrant.py:97)<br>
加入 sparse 后建议使用新的 collection schema/version 并从 MySQL 真源重建，不要偷偷修改现有生产 Collection。

## 七、结合当前结果，建议的落地顺序

### 第一阶段：低成本验证 BM25

只在 eval backend 增加 Chunk 级 `lexical`：

- 比较现有基线与 `+BM25`。
- 专门观察人名、歌名、原话、OCR 类查询。
- 不立刻进生产契约。

### 第二阶段：Event 级 Dense

复用已有 `OwnedTimelineEvent`：

- 每个 Event 建独立 point。
- 命中后回到父 VideoChunk。
- 评估 Precision@1、MRR 和最终片段长度。
- 这是最符合 Sentence Window 原理的一步。

### 第三阶段：加入真正的精排

粗召回 Top 20 后：

- 文本类使用 Cross-Encoder 或 ColBERT。
- 视觉类只对少量候选调用 VLM verifier。
- 跨模态问题再调用较贵的 LLM/VLM Judge。

Datawhale 对 RRF、Cross-Encoder、ColBERT 和 LLM Reranker 的职责区分讲得比较清楚：[检索进阶章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/15_advanced_retrieval_techniques.md)。Qdrant 官方也推荐 Dense + Sparse 先扩大召回，再使用 late interaction 做精排：[Hybrid Search with Reranking](https://qdrant.tech/documentation/tutorials-basics/reranking-hybrid-search/)。

### 第四阶段：细粒度视觉和 OCR

仅针对视觉/OCR bad case 加：

- Shot/Keyframe Vision。
- OCR Sparse。
- 必要时 OCR Dense。

不要先加 Episode、Entity、Audio、ColBERT 等所有层次。

## 八、怎么判断一层向量值不值得保留

目前 12 条 smoke 的 Top-10 已经是 100%，主要问题其实是 Top-1、排序和粒度，而不是完全召回不到。因此每次增加一层，都要做单变量消融：

```text
A：现有 Transcript + Text + Vision
B：A + BM25
C：A + Event Dense
D：C + BM25
E：D + Reranker
F：E + Shot Vision
```

测试集至少按下面类型分层：

- 精确原话。
- 人名、别名、歌名。
- 台词释义。
- 剧情关系和因果。
- 视觉动作。
- 物体、颜色、场景。
- 人物归因。
- OCR/片尾标题。
- 跨 Chunk 长事件。
- 跨集 long-horizon。
- 无答案和错误前提。

指标建议：

- Hit@1/3/5/10、MRR。
- 每类 Query 的分项结果。
- 时间范围 IoU、边界误差、最终片段长度。
- 人物/说话者归因正确率。
- p50/p95 延迟。
- 索引体积、建库时间、单查询模型成本。
- 最终答案 Faithfulness。

Datawhale 的评估章节也强调必须把检索相关性、生成忠实度和答案相关性分开诊断，且每次实验尽量只修改一个变量：[RAG 系统评估](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter6/18_system_evaluation.md)。

## 九、你给的“格式化生成”章节对 Seekino 的意义

这一章不是新的 RAG 召回技术。它解决的是：

> 模型找完证据后，怎样稳定地输出程序能消费的数据。

它介绍了：

- `JsonOutputParser`
- `PydanticOutputParser`
- JSON Schema
- Function/Tool Calling
- llama.cpp 的 GBNF 语法约束

详见 [格式化生成章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter5/16_formatted_generation.md)。

Seekino 其实已经落实得比较好了：

- `RetrievalChannelWeights`
- `DraftItem`
- `SelectedVideoRange`
- `ChatRenderDraft`
- `FinalRenderBlock`
- `RetrievalTraceEntry`

都采用了严格 Pydantic Schema，并校验 ID、时间范围、重复项和字段类型。[state.py](/Users/bytedance/workspace/other-project/Seekino/server/agent/video_search/state.py:27)

后面可以进一步把 Query Agent 的计划也结构化成：

```json
{
  "intent": "lexical | semantic | visual | mixed",
  "lexical_query": "...",
  "semantic_query": "...",
  "visual_query": "...",
  "filters": {},
  "channel_weights": {},
  "top_k": 20
}
```

模型只负责提出计划；Server 继续负责验证 Series Scope、稳定 ID、revision 和时间范围。对于 `llama.cpp` 本地 fallback，可以用 GBNF 强制生成合法 JSON，但 Schema 合法不等于事实正确，Evidence 校验仍然不能省。

最终建议压缩成一句话就是：

> 保留 Chunk Text + Raw Video 两个粗召回基线；先试 BM25 词法补漏，同时把索引粒度下沉到 Event/Shot；候选映射回父 Chunk 后做加权融合和精排，最后按时间证据展开并结构化输出。

本轮按项目门禁只做了只读分析，没有修改文件。预计修改树：无；预计修改量：0 文件、0 行。

---

# 三路召回与 RRF 现状复核（2026-09-24）

不完全是。最关键的纠正是：当前不是“三路 Embedding”，而是“两路 Dense Embedding + 一路 ASR 精确词法匹配”。

```text
Raw ASR cues ──连续短语包含匹配────────────┐
                                          │
self_compact + events + event.text ─Text Dense─┼─ 加权 RRF ─ Top-K Chunk
                                          │
完整 60 秒视频 Chunk ─────────Vision Dense──┘
```

Qdrant 里也确实只有 `text` 和 `vision` 两个向量字段；Transcript 路不在 Qdrant 中。[qdrant.py](/Users/bytedance/workspace/other-project/Seekino/server/storage/qdrant.py:14)

## 一、当前三路实际是什么

### 1. Transcript：原始 ASR 连续短语匹配

这一路不是 Embedding，也不是 BM25。

它会：

1. 对 Query 做 NFKC、大小写归一；
2. 删除所有标点和空格；
3. 把当前 Chunk 的 ASR cues 拼起来；
4. 用字符串 `find()` 搜索连续子串；
5. 每个 Chunk 只保留一个最佳命中。

实现见 [video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:244) 和 [video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:397)。

所以“完全不支持 ASR 原台词召回”是不准确的：

- 精确原话、同语言、ASR 没识别错：支持；
- 部分词匹配、ASR 错字、近似台词、译文查询、同义表达：不支持；
- 谁说的、说了多少次、列出全部说话人：不支持。

### 2. Semantic：一个混合文本 Dense Vector

当前并不存在：

```text
compact vector
+
chunk semantic vector
```

这两条独立通道。

实际只有一个 `text` vector，它的输入是：

```text
self_compact
+ 每个 owned event 的 description
+ 每个 owned event 的 text
```

见 [video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:97)。

而 `self_compact` 也不是另一套独立摘要。它就是把当前 Chunk 的 owned events 格式化成：

```text
[时间范围] EVENT_TYPE description；台词：text
```

见 [understand.py](/Users/bytedance/workspace/other-project/Seekino/engine/parsev1/understand.py:375)。

因此目前的数据关系其实是：

```text
owned events
   ├─ 渲染成 self_compact
   └─ description / text 再追加一次
                 ↓
            一个 Text Embedding
```

也就是说，很多事件描述和台词在 Text Embedding 输入里出现了两遍。只有 `self_compact` 超过 4000 字被截断时，两者才不完全重复。

另外几个容易混淆的字段：

- `self_compact`：进入 Text Embedding；
- `events.description/text`：进入 Text Embedding；
- `window_summary`：不进入 Text Embedding；
- `context_before_compact/context_after_compact`：不进入 Text Embedding；
- 完整 `candidate_transcript`：不进入 Text Embedding，只走精确 Transcript 通道。

所以“compact embedding 没用”还不能直接下结论，但“当前 `self_compact + events` 存在明显冗余，边际价值可疑”是成立的。应该通过消融实验判断，不该凭感觉直接删除。

### 3. Vision：完整 Chunk 的视频向量

这一点你的理解基本正确：每个 canonical Chunk 的整段视频被压成一个 Vision Dense Vector。[embed.py](/Users/bytedance/workspace/other-project/Seekino/engine/parsev1/embed.py:144)

它适合粗粒度查找：

- 绿色荧光棒；
- 某人拿吉他；
- 粉色手机；
- 某个地点或服装。

但不适合可靠表达：

- 镜头先拍吉他，再切观众，再切主唱；
- 某个 0.5 秒短镜头；
- 正反打、推拉摇移、特写转全景；
- 一个 60 秒 Chunk 中第 37 秒出现的某个物体；
- 多个镜头的先后顺序。

因为一个向量只能表达整段视频的总体内容，不能保存完整的镜头序列与精确位置。

## 二、对你提出的三点逐条判断

### 1. “不支持 ASR 原台词召回，急需修改”

更准确地说：

> 已经支持精确原话召回，但缺少可用的通用词法召回。

当前适合：

```text
用户输入原文：“どうして春日影やったの”
ASR 也正好识别成这句话
```

当前不适合：

```text
ASR：どうして春日影やったの
用户：为什么要演奏春日影
```

也不适合：

```text
ASR：你的live是最棒的
用户：有人夸灯的演出很好
```

第一种需要原语言 Query 或翻译；第二种需要 Semantic Dense；ASR 有少量错字时则需要 BM25、字符 n-gram 或模糊匹配。

因此急需补的是：

```text
Exact Phrase：高精度原话定位，保留
BM25 Sparse：关键词、专名、部分匹配，新增
Semantic Dense：译文、释义、同义表达，保留
```

不是把现有 Transcript 通道整个推倒。

### 2. “compact embedding 没有用”

结论应该改成：

> 没有独立的 compact embedding；当前 Text Embedding 存在重复编码，是否有价值必须做消融。

建议至少比较四组：

```text
A. self_compact only
B. owned event description + text only
C. 当前 self_compact + events
D. window_summary + owned events
```

分别比较台词、剧情、人物关系、动作、物体查询的 Hit@1、MRR。

我个人对结果的预期是：

- `events only` 很可能不弱于当前双重拼接；
- `self_compact` 的时间和事件类型格式可能有少量帮助；
- 重复台词和人物名可能让 Dense 表示发生偏置；
- `window_summary` 可能补充整体语义，但也更容易引入概括错误；
- 最终大概率应构造一份明确的 retrieval document，而不是把现有字段机械相加。

但这是需要实验验证的推测，不应该现在就断言 `self_compact` 完全没用。

### 3. “分镜召回和‘有哪些人说了哈？’支持不了”

分镜召回：基本成立。

“有哪些人说了‘哈？’”：只能完成最前半步。

假设 Query Agent 正确提取出：

```text
哈?
```

当前归一化会变成：

```text
哈
```

然后会匹配：

- `哈？`
- `哈哈`
- `哈喽`
- 任何包含“哈”的更长字符串。

即使召回了正确 Chunk，系统仍然不知道谁说的，因为 `TranscriptCue` 只有：

```text
start_seconds
end_seconds
text
```

没有 `speaker_id`。[schemas.py](/Users/bytedance/workspace/other-project/Seekino/engine/parsev1/schemas.py:22)

`TimelineEvent` 同样没有结构化 speaker 字段，只有 description 和 text；说话人如果存在，只是模型写在自然语言 description 里。[schemas.py](/Users/bytedance/workspace/other-project/Seekino/engine/parsev1/schemas.py:36)

此外，当前每个 Chunk 只保留一个最佳 Transcript 命中，最终每路只取 Top 20。因此它不能可靠回答：

- 谁说过；
- 总共说了几次；
- 请列出全部片段；
- 同一个 Chunk 内有哪些不同的人分别说过。

这类问题需要的不是普通 Top-K，而是：

```text
完整词法 occurrence 检索
    ↓
每个命中的 cue 时间范围
    ↓
与 speaker / visible speaker / dialogue event 对齐
    ↓
人物别名归一
    ↓
按人物分组并返回全部证据
```

BM25只能帮助“找到出现‘哈’的地方”，不能解决“是谁说的”。

## 三、当前三路召回 + RRF 还存在什么问题

### 1. 所有通道都被 60 秒 Chunk 粒度锁死

用户通常找的是：

- 一句 2 秒台词；
- 一个 4 秒动作；
- 一个 1 秒镜头；
- 一个跨越 Chunk 边界的连续事件。

当前召回单元却是整段 canonical Chunk。结果是：

- 一个 Text Vector 混入多个事件、人物和台词；
- 一个 Vision Vector 混入几十个镜头；
- 命中了正确 Chunk，也不代表找到了正确时间；
- 相邻 Chunk 可能分别拿到同一个连续事件的一半；
- 返回结果容易停留在 60 秒范围，再交给 Agent 二次收紧。

这其实是当前最核心的瓶颈，比加第四条向量更严重。

### 2. Text Vector 会稀释细节，也会放大解析错误

一个 Chunk 中如果包含：

```text
人物 A 进入
人物 B 说话
人物 C 演奏
观众欢呼
场景切换
```

最后只生成一个向量。Embedding 得到的是平均语义，不保证任何一个细节还能占据足够权重。

更糟糕的是，Text Vector 来自 VLM 解析结果：

- 人物识别错误；
- 说话人归因错误；
- 动作遗漏；
- 关系描述幻觉；

都会直接进入向量。

之前的 `r06` 就是正确台词存在，但解析把乐奈的回答归给了高松灯，最终 Text 和 Vision 都排到第 8。[ep07.recall-smoke.md](/Users/bytedance/workspace/other-project/Seekino/evals/recall/ep07.recall-smoke.md:77)

RRF 不会纠正源数据错误，只会融合多个带错的排序。

### 3. 每次调用的三个通道使用同一条 Query

单次 `retrieve` 调用里，同一个 `query` 同时用于：

- Transcript 精确匹配；
- Text Dense；
- Vision Dense。

例如：

```text
有哪些人说了“哈？”的片段
```

对不同通道真正合适的 Query 应该是：

```text
Transcript：哈
Semantic：人物因惊讶或疑问发出简短回应
Visual：人物惊讶、疑惑的表情与说话镜头
```

但当前接口只有一条 Query。Agent 可以主动拆成多次检索，Skill 也鼓励这么做，但这是依赖 LLM 临场规划的，不是稳定的检索管线。[retrieval/SKILL.md](/Users/bytedance/workspace/other-project/Seekino/agent/chat/video_search/skills/retrieval/SKILL.md:58)

### 4. Transcript 当前是全量线性扫描

每次查询会从 MySQL 加载当前范围内所有可搜索 Chunk 的 Transcript，再在 Python 中逐 Chunk 做字符串查找。[video.py](/Users/bytedance/workspace/other-project/Seekino/server/repository/video.py:723)

复杂度近似：

```text
O(当前 Series 全部 ASR 字符数)
```

一集问题不大，13 集、多次 Agent 循环、长对话反复检索以后，延迟会持续增长。

BM25/倒排索引的价值不仅是“排序更聪明”，还包括不再每次把全部 ASR 拉出来扫描。

### 5. Transcript 对短句和常见词的排序很弱

当前精确匹配分数基本落在 `0.9～1.0`：

- 完整 cue 正好等于 Query：1.0；
- Query 只是 cue 的一部分：约 0.9x。

对于“哈”“嗯”“好”“等一下”这类短句，大量 Chunk 会得到几乎相同的分数。

随后 tie-break 主要依赖时间和 ID，而不是：

- 谁说的；
- 语境是否相关；
- 用户要求的情绪；
- 是否完整覆盖 Query；
- 是否是独立一句话。

这会产生大量无意义候选。

### 6. RRF 完全丢弃原始分数差距

当前融合只使用：

\[
\frac{w}{60 + rank}
\]

见 [video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/engine/retrieval/video_retrieval.py:205)。

它不知道：

```text
Text rank 1：cosine 0.91
Text rank 2：cosine 0.43
```

和：

```text
Text rank 1：cosine 0.51
Text rank 2：cosine 0.50
```

是两种完全不同的置信度结构。

对 RRF 来说，它们都只是第一名和第二名。

更严重的是，Dense 检索即使没有相关结果，通常也会硬返回若干最近邻；当前没有 score threshold，这些“最不差但其实不相关”的结果仍然会参加投票。

### 7. `rrf_k=60` 把名次差距压得非常平

当前配置：

```text
channel_top_k = 20
rrf_k = 60
```

见 [retrieval.py](/Users/bytedance/workspace/other-project/Seekino/server/config/retrieval.py:8)。

单路中：

```text
rank 1  = 1/61 ≈ 0.0164
rank 20 = 1/80 = 0.0125
```

第一名只比第二十名高约 31%。

等权情况下，一个候选如果在两个噪声通道都是第 20：

```text
2 × 1/80 = 0.025
```

会超过另一个只在最可靠通道排第 1 的候选：

```text
1/61 = 0.0164
```

这正是 RRF 的“多路共识奖励”，但前提是各路相对独立、质量相近。你们目前：

- Vision 明显弱于 Text；
- Transcript 和 Text 都可能来自相同台词；
- `self_compact` 又重复 owned events；
- 三路并不独立。

所以“多路投票”不一定代表多份独立证据。

仓库现有实验也已经看到这一点：Text Hit@1 是 58.3%，等权 RRF 反而降到 41.7%。[ep07.recall-smoke.md](/Users/bytedance/workspace/other-project/Seekino/evals/recall/ep07.recall-smoke.md:50)

### 8. Top-20 截断发生在融合之前

每个通道先截取 Top 20，再做 RRF。

因此：

- 正确结果在某路排第 21，就视为完全不存在；
- 后续提高该通道权重也救不回来；
- Agent 和 Evaluator根本看不到这个候选；
- 召回失败以后，后面的多 Agent 再聪明也没用。

这也是为什么需要把“候选召回率”和“最终排序准确率”分开评估。

### 9. 当前没有真正的候选级 Reranker

当前流程大致是：

```text
三路召回
→ RRF
→ 直接截取 Top-K
→ MySQL hydrate
→ Query Agent 逐项精读
→ Evaluator 审核草稿
```

Agent/Evaluator 可以发现候选不对，但它们不是一个稳定的候选级重排器：

- 正确候选必须先进入最终 Top-K；
- 模型要逐项读取，成本和延迟高；
- 不同模型或 Prompt 可能做出不同选择；
- 无法像 cross-encoder/reranker 一样统一比较所有候选。

更理想的是：

```text
各路召回 Top 20～50
→ 候选并集
→ 轻量 Rerank 到 Top 8～12
→ Agent 精读
```

### 10. Top-K 检索天然不适合“全部、哪些人、每一次”

下面几类任务不是普通 Top-K：

- “有哪些人说过‘哈’？”
- “全季所有拿起手机的片段”
- “每次演奏《春日影》分别在哪里？”
- “有没有完全没出现过某人物的一集？”

它们需要：

- exhaustive scan 或可分页 occurrence index；
- 去重；
- 分组；
- 计数；
- 覆盖率证明；
- 明确的终止条件。

当前 `top_k` 和 RRF 只能给“最像的若干片段”，无法证明“这就是全部”。

### 11. 没有稳定的 No-result / Abstention 机制

如果用户搜索一个完全不存在的事件：

- Transcript 可以返回空；
- 但 Text/Visual Dense 通常仍会返回最近的若干 Chunk；
- RRF 没有最低相关性阈值；
- 最终只能依赖 Agent 阅读后判断证据不足。

这容易产生“明明不存在，系统却找了几个勉强相似片段”的体验。

### 12. Fallback 质量只被展示，没有进入排序

`is_fallback` 会被验证并在限制说明里提示“证据粒度可能较粗”，但不会降低融合得分。[video_retrieval.py](/Users/bytedance/workspace/other-project/Seekino/server/service/video_retrieval.py:332)

因此：

- 正常解析片段；
- 审核失败后降级生成的粗粒度片段；

在 RRF 中没有质量区别。

是否应该降权不能拍脑袋，但至少应该进入评测和 reranker 特征。

### 13. 现有评测还不足以证明三路体系

当前签入的真实 smoke：

- 只有一集；
- 12 条 Query；
- 没有负样本；
- 没有 fallback Chunk；
- 没有“全部出现位置”；
- 没有短常见台词；
- 没有说话人枚举；
- 没有分镜顺序查询；
- 报告评估的还是 Text/Vision 双路，而不是现在完整的三路。

该文档自己也明确写了，这是 exploratory smoke，不是统计上有代表性的 benchmark。[ep07.recall-smoke.md](/Users/bytedance/workspace/other-project/Seekino/evals/recall/ep07.recall-smoke.md:71)

## 四、我认为现在真正的优先级

### P0：先补词法和评测，但保留现有 Exact Phrase

```text
ASR Exact Phrase
+ BM25 / 字符 n-gram
+ 原语言 Query
+ 短常见词专项评测
+ no-result 查询
```

BM25 不应该简单作为独立第四票，而应先和 Exact Phrase 合成一个 Lexical Family。

### P0：建立 Utterance 级检索对象

至少需要：

```text
utterance_id
video_chunk_id
start_seconds
end_seconds
raw_text
normalized_text
speaker_id / speaker_candidates
source_revision
```

这样“哈？”才能从 Chunk 检索问题，变成真正的台词 occurrence 检索问题。

### P0：增加候选级 Rerank

优先解决：

- RRF 丢失分数；
- 弱通道制造噪声；
- 多路相关证据重复投票；
- 人物、动作、台词约束无法同时验证。

### P1：Shot / Keyframe 索引

面向分镜召回建立：

```text
shot_id
parent_chunk_id
start/end
keyframe or short-clip embedding
shot type
camera movement
subjects
OCR
```

先召回 Shot，再展开到 Event/Chunk，而不是继续压缩整段 60 秒视频。

### P1：对 Text 文档做消融

不要先认定 `compact` 无用，而是测清楚：

```text
events only
vs self_compact only
vs current duplicated input
vs summary + events
```

### P2：最后才是 Graph

上述问题都是：

- 粒度；
- 词法；
- 说话人；
- 分镜；
- 排序；
- 全量枚举；
- 评测。

GraphRAG 不会自动解决其中任何一个。先把这些做好，再讨论跨集人物关系和因果链，顺序是对的。

一句话概括：

> 当前真正的三路是“ASR 精确短语 + 混合事件文本 Dense + 整段视频 Dense”。原话检索不是没有，而是太脆；`self_compact` 不是独立通道，而是与 events 重复；分镜与“谁说了某句话”则分别缺少 Shot 粒度和 Speaker/Occurrence 数据模型。比继续加召回路数更严重的问题，是 60 秒单向量、RRF 无阈值且过度奖励多路共识、Top-20 提前截断，以及缺少候选 Rerank。

本轮仅做只读调查，没有修改文件、执行测试、迁移或重启；预计及实际修改树均为无，修改量 0 文件、0 行。

---

# 三个 P0 的含义与依赖关系（2026-09-24）

这三个 P0 分别解决三个完全不同的问题：

```text
词法召回：能不能把相关台词找进候选池？
Utterance：被找出来的到底是哪一次发言、在何时、由谁说？
Rerank：候选都找出来以后，哪一个最符合用户这次的问题？
```

三者不是互相替代，而是前后相接。

## 一、P0：补词法和评测，但保留 Exact Phrase

### 它解决什么

它解决的是“候选能不能被找到”。

当前 Exact Phrase 只能在用户原文和 ASR 连续子串基本一致时命中。它的优点是精度高、命中后有直接时间证据；缺点是对错字、分词、同义表达、译文和非连续关键词非常脆弱。

因此建议形成一个词法召回家族：

```text
Exact Phrase
  ├─ 奖励完整、连续、精确的原话
  └─ 保留准确 cue 时间范围

BM25
  ├─ 找专名、关键词和部分词覆盖
  └─ 通过 IDF 奖励作品内的稀有词

字符 n-gram
  ├─ 处理无空格的中日文和轻微 ASR 差异
  └─ 作为受控补漏，避免短字符产生海量误召回
```

原语言 Query 指的是：词法通道应尽量使用 Transcript 的原语言和原脚本。中文译文查日文 ASR 不会因为加入 BM25 就自动成功；译文或剧情释义仍应主要交给 Semantic Dense。

短常见词专项评测用于检验“哈、嗯、好、等等”这类高频、低信息量表达会不会返回大量误匹配。`no-result` 查询则用于检验系统在素材中不存在目标时，能否拒绝勉强相似的 Dense 候选，而不是永远返回几个结果。

### 为什么 Exact Phrase 和 BM25 不能直接各算一票

两者通常都读取同一份 ASR。若把它们作为两个平权 RRF 通道：

```text
Exact 命中一次  ─┐
                  ├─ 同一份台词得到两张票
BM25 又命中一次 ─┘
```

这并不是两份独立证据，只是同一证据被两种算法重复计票。

所以应该先合成一个 `Lexical Family`：

```text
Exact candidates ─┐
                  ├─ 并集、去重、保留 exact/bm25 特征
BM25 candidates ──┘
                         ↓
                  一份 Lexical Ranking
```

然后再让 Lexical、Semantic、Visual 三个证据家族参与上层融合或 Rerank。

### 它不解决什么

它不能解决：

- 谁说了这句话；
- 同一句出现了多少次；
- 哪一次最符合特定剧情语境；
- 画面里是谁、镜头如何切换。

这些分别属于 Utterance 数据模型和候选 Rerank。

## 二、P0：建立 Utterance 级检索对象

### 它解决什么

它解决的是“检索结果的原子单位是什么”。

当前 Transcript 最后仍被折叠成 `video_chunk_id`：一个 60 秒 Chunk 无论出现一次还是五次“哈”，每个 Chunk 都只保留一个最佳命中。这不适合 occurrence 查询。

Utterance 对象应让每一次真实发言都有独立身份：

```text
utterance_id
  这一次发言的稳定 ID；相同文本出现十次，应有十个不同 ID。

video_chunk_id
  指回父 Chunk，用于权限、revision、Evidence hydrate 和视频播放。

start_seconds / end_seconds
  这一次发言的准确媒体范围，而不是整个 60 秒范围。

raw_text
  ASR 原始输出，是可审计事实；展示和证据核验使用它。

normalized_text
  搜索用归一化表示，可以去标点、统一大小写或生成 tokenizer 输入；
  它不是原始事实，不能覆盖 raw_text。

speaker_id / speaker_candidates
  已确认说话人，或带来源/置信信息的候选说话人；无法确认时应保持 unknown，
  不能为了字段非空而猜测。

source_revision
  关联这条 Utterance 来自哪个解析版本；ASR 或说话人修正后可使旧索引失效并重建。
```

有了它以后，“有哪些人说了‘哈？’”才可以变成：

```text
查找所有匹配的 utterance
→ 分页遍历全部 occurrence，而不是只取 Top-K
→ 按时间对齐 speaker / visible speaker / dialogue event
→ 归一化人物别名
→ 按 speaker 分组
→ 每个人返回对应 utterance 和播放范围
```

### `speaker_candidates` 为什么可能比单一 `speaker_id` 更现实

动画里经常存在：

- 画面不展示说话人；
- 镜头拍着听者而不是说话者；
- 多人画外音；
- ASR 没有 diarization；
- VLM 对人物身份判断冲突。

因此解析阶段可以先保存候选及来源，例如“ASR diarization、可见嘴型、相邻 Dialogue Event、VLM 判断”，证据充分后再固化 `speaker_id`。不确定时返回 unknown，比制造错误人物关系更可靠。

### 它不一定一开始就是新的业务主表

Utterance 可以先作为可重建的派生检索对象进行验证，不必第一天就扩成复杂领域模型。但只要要稳定支持 occurrence、speaker 和精确播放范围，就必须最终拥有稳定 ID、revision 和父 Chunk 关系，不能永远停留在临时字符串匹配结果里。

### 它不解决什么

它仍然不能独立判断：

- 用户说的“哈”是惊讶、疑问还是嘲讽；
- 同一句台词的多个命中中，哪一个最符合当前人物和事件约束；
- 某个弱 Vision 候选是否应该排在精确台词候选前。

这些是 Rerank 的职责。

## 三、P0：增加候选级 Rerank

### 它解决什么

它解决的是“粗召回之后，如何选出真正符合完整问题的候选”。

推荐链路是：

```text
Exact / BM25 / Semantic / Visual 各自扩大召回
                  ↓
              候选并集、去重
                  ↓
       候选级 Rerank（统一比较完整约束）
                  ↓
             Top 8～12 给 Agent 精读
```

RRF 可以继续做廉价粗融合，但不再承担最终排序。

### 1. “RRF 丢失分数”是什么意思

RRF 只看每一路的名次，不看原始相关度差距。

例如：

```text
情况 A：rank 1 = 0.92，rank 2 = 0.31
情况 B：rank 1 = 0.52，rank 2 = 0.51
```

情况 A 的第一名明显远强于第二名；情况 B 的前两名几乎打平。但进入 RRF 后，两种情况都只剩下 `rank 1` 和 `rank 2`。

候选级 Rerank 可以重新读取：

- Query；
- ASR 原句与覆盖率；
- Event description；
- 人物、动作、物体和时间约束；
- 原始 Dense/BM25 分数与名次；
- exact phrase 是否命中；
- fallback 和证据质量标记。

然后比较“这个候选是否真正回答完整 Query”，而不只比较名次。

### 2. “弱通道制造噪声”是什么意思

Dense 检索通常无论是否存在正确答案，都会返回一批最近邻。Vision 通道尤其可能只是返回“画面稍微相似”的 Chunk，而不是实际相关证据。

若这些弱候选仍参加 RRF：

- 一个无关候选可能因为在两个弱通道都排中游而被抬高；
- 一个精确台词候选可能因为只在 Lexical 路出现而被压低；
- 不存在答案时，系统仍会得到一批看起来有排序的结果。

Rerank 的作用是对候选做绝对相关性或成对相关性判断，必要时拒绝候选，而不是因为“它是 Vision 第三名”就默认它有价值。

### 3. “多路相关证据重复投票”是什么意思

RRF 默认把每一路当成相对独立的投票者，但 Seekino 的通道并不完全独立：

- Exact Phrase 使用原始 ASR；
- BM25 也将使用原始 ASR；
- Semantic Text 中的 `event.text` 可能再次包含同一句台词；
- `self_compact` 又会重复渲染 `event.text` 和 description。

于是同一句台词可能以不同通道名称获得三次以上奖励。这会把“重复编码”误认为“多模态一致性”。

候选级 Rerank 应保留证据来源，但按证据家族理解它们：

```text
Lexical evidence：Exact + BM25
Semantic evidence：事件和剧情语义
Visual evidence：画面与镜头事实
```

真正有价值的共识是“台词、剧情和画面互相支持”，而不是“同一句 ASR 被三个算法重复看到”。

### Rerank 不等于立刻上昂贵大模型

可以分三步实施：

1. 规则/特征重排：Exact bonus、Query 覆盖率、score threshold、证据质量、同源去重；
2. Text cross-encoder：统一比较 Query 与 ASR/Event 文本；
3. 只对极少量歧义候选调用 VLM/强 LLM 核验画面和人物。

先做第一步就可能修复一批明显的 RRF 排序问题；不必把所有候选都交给昂贵模型。

## 四、三者的工程依赖顺序

三个 P0 虽然同级，但实现上不是彼此孤立：

```text
1. 先补评测类型和标注口径
2. 定义最小 Utterance/Occurrence 表示
3. 在 Utterance 上验证 Exact + BM25/n-gram Lexical Family
4. 映射回父 Chunk，与 Semantic/Visual 形成候选并集
5. 增加轻量候选 Rerank
6. 最后再决定是否引入更昂贵的 cross-encoder / VLM reranker
```

Chunk 级 BM25 可以作为低成本实验，用于快速验证词法召回是否有收益；但生产方案如果目标包含“谁说了、说了几次、全部片段”，就不应该先做一套重型 Chunk BM25，随后再整体迁移到 Utterance。

最短总结：

> Lexical Family 解决“找得到”；Utterance 解决“这是哪一次、何时、谁说”；Rerank 解决“找出的这些里面，哪个真正符合完整问题”。缺少任何一层，“有哪些人说过‘哈？’”都只能得到不完整答案。
