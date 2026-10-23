# Multilingual RAG 对 Dialogue 多语言文本版本的处理研究

日期：2026-09-25

状态：设计研究，不是已批准的 Parse V2 Schema，也不是实现说明。

## 结论

对 AniVAG 当前问题，最稳妥的结论是：

> 同一段语音、同时间出现的原文字幕和翻译字幕，应归属于同一个
> `DIALOGUE` Timeline Event；但不应被拼成一个无法区分语言的字符串。

更准确的分层是：

```text
一个领域事实：Dialogue Event
  ├── 日语原台词文本
  └── 中文字幕文本

多个检索表示：Retrieval Children
  ├── 日语 Exact / lexical / dense representation
  └── 中文 Exact / lexical / dense representation

一个检索结果身份：event_id
```

Parse 模型负责把 ASR 与 OCR 证据对齐、识别语言、纠错和去重；Engine
Projection 负责把一个 Dialogue 的多语言文本拆成多个检索表示；检索端在
`event_id` 层折叠，只返回一次 Dialogue，避免双语内容重复投票。

这不是某个开源项目已经定义好的“视频双语字幕 Schema”。它是把现有项目中已经成熟的三类
能力组合到 AniVAG：

1. multilingual retrieval；
2. multiple representations per parent；
3. search-result grouping/collapse。

## 研究问题

本次具体调查以下问题：

- 日语 ASR、日语 OCR 字幕和中文 OCR 字幕是否应成为三条领域事实；
- 多语言文本是否应直接拼进一个字符串；
- 传统 RAG 如何处理不同语言语料或同一内容的多个表示；
- multilingual embedding、query translation、语言专用 lexical retrieval 各自解决什么；
- 多个语言版本命中时如何避免重复结果和重复计分。

## 现成项目实际做了什么

### NAVER BERGEN：把语料语言作为显式检索策略

BERGEN 是一个公开的 RAG benchmarking 项目，提供 multilingual query 和 multilingual
datastore 实验。它为不同语言的 Wikipedia 分别建立 BGE-M3 索引，并明确比较：

- `retrieve_en`：只查英文语料；
- `retrieve_${lang}`：只查用户语言语料；
- `retrieve_en_${lang}`：合并英文和用户语言语料；
- `retrieve_all`：合并所有支持语言语料；
- 可选 NLLB query translation。

它没有把所有语言先拼进一个字段。其日语实验中，不同数据集上的最佳 retrieval-language
策略也不同，说明不能先验规定“只查原文”“只查译文”或“查全部语言”一定最优。

来源：

- [BERGEN multilingual guide](https://github.com/naver/bergen/blob/main/documentation/multilingual.md)
- [Retrieval-augmented generation in multilingual settings](https://arxiv.org/abs/2407.01463)
- [BERGEN repository](https://github.com/naver/bergen)

边界：BERGEN 研究的是不同语言语料库，不负责把同一条视频 Dialogue 的翻译版本折叠为一个
事件。因此它直接支持“语言表示应独立评测”，但不直接规定 AniVAG 的 parent/event Schema。

### RAGFlow：通过查询翻译扩展跨语言召回

RAGFlow 的 Cross-language search 使用 chat model 把 query 翻译到指定目标语言，然后把结果
作为扩展 query 进入普通 retrieval；模型失败时回退原 query。

来源：

- [RAGFlow cross-language glossary](https://github.com/infiniflow/ragflow/blob/main/docs/references/glossary.mdx)
- [`cross_languages` implementation](https://github.com/infiniflow/ragflow/blob/main/rag/prompts/generator.py)
- [retrieval call site](https://github.com/infiniflow/ragflow/blob/main/agent/tools/retrieval.py)

边界：这是 query-side translation，不是多语言文档版本存储，也没有为多个语言 child 定义
parent collapse。它证明 query translation 是一条真实采用的路线，但不能拿来证明所有原文都
应该先翻译或覆盖。

一个 RAGFlow issue 还报告：即使使用 multilingual embedding，Thai query 仍可能被 lexical
tokenizer／hybrid 候选路径挡掉；加入英文 query expansion 后才恢复召回。这是用户报告，不是
官方保证，但能说明 multilingual dense 并不会自动修复整个 lexical pipeline。

来源：[RAGFlow issue #17041](https://github.com/infiniflow/ragflow/issues/17041)

### LangChain：多个 child embedding 指向一个 parent document

`MultiVectorRetriever` 明确支持一个 parent document 对应多个 embeddings。它先搜索 child
documents，再从 child metadata 取 `doc_id`，按首次出现顺序去重 parent IDs，最后从 docstore
读取 parent documents。

来源：

- [LangChain `MultiVectorRetriever` source](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/retrievers/multi_vector.py)
- [LangChain `ParentDocumentRetriever` source](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/retrievers/parent_document_retriever.py)

边界：源码中的标准例子是 chunk-to-parent，而不是 multilingual version management。将日语和
中文 representation 作为 children、将 Dialogue Event 作为 parent，是 AniVAG 对通用原语的
映射。不过“child 命中后只返回一次 parent”本身是源码已有的行为，不是新发明。

### Qdrant、Elasticsearch 和 Weaviate：按逻辑身份折叠结果

Qdrant Grouping API 明确面向“同一个 item 有多个 points，避免结果冗余”的情况，可按
`document_id` 等 payload 分组；每组保留 best N points，组的排序由组内 top point 决定。

Qdrant 的 multi-representation 教程也把不同检索信号存成独立 representation，查询时做
prefetch、RRF，并按 `document_id` group。官方将它总结为一个 schema decision，而不是仅靠
某个 embedding model 自动完成的事情。

来源：

- [Qdrant Grouping API](https://qdrant.tech/documentation/search/)
- [Qdrant multi-representation search](https://qdrant.tech/documentation/tutorials-search-engineering/multi-representation-search/)

其他引擎也有对应原语：

- [Elasticsearch field collapse](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/collapse-search-results)
- [Weaviate `groupBy`](https://docs.weaviate.io/weaviate/api/graphql/get)

边界：这些 grouping/collapse 功能是通用搜索能力，不是字幕专用功能。把所有语言 child 的
group key 设置为同一 `event_id` 是 AniVAG 的设计推导。

## 四类多语言 RAG 策略不是互斥方案

### 1. Multilingual dense embedding

跨语言 embedding 把不同语言的相近语义映射到可比较的向量空间。BGE-M3 支持 100 多种语言、
dense/sparse/multi-vector retrieval；multilingual-e5 也支持约 100 种语言，但其模型卡明确
提醒低资源语言可能退化。

来源：

- [BGE-M3 project](https://github.com/FlagOpen/FlagEmbedding/blob/master/research/BGE_M3/README.md)
- [multilingual-e5-large model card](https://huggingface.co/intfloat/multilingual-e5-large)
- [SentenceTransformers multilingual models](https://sbert.net/docs/sentence_transformer/pretrained_models.html)

它适合回答“中文 query 能否语义命中只有日语文本的事件”，但不能替代：

- 原台词的精确字符串搜索；
- 人名、术语和罕见实体的 lexical match；
- 原文与翻译文本的保存；
- language-specific tokenizer；
- parent/event 去重。

### 2. Query translation

把 query 翻译成一个或多个目标语言，可以改善 BM25 或单语索引的跨语言召回。RAGFlow 已采用
这一方式；BERGEN 将 NLLB query translation 作为可选实验分支。

但翻译可能损坏人名、专有名词、双关或剧中造词。因此对 AniVAG，它更适合作为基于评测启用的
recall fallback，而不是 P0 的唯一检索路径，也不应覆盖用户原 query。

相关一手研究：

- [Document Translation vs. Query Translation for Cross-Lingual Information Retrieval in the Medical Domain](https://aclanthology.org/2020.acl-main.613/)
- [XOR QA: Cross-lingual Open-Retrieval Question Answering](https://aclanthology.org/2021.naacl-main.46/)
- [Multilingual Retrieval-Augmented Generation for Knowledge-Intensive Task](https://arxiv.org/abs/2504.03616)

### 3. Language-specific lexical retrieval

BM25、Exact 和 n-gram 负责保住原词、原句、人名、术语与字幕精确内容。日语、中文等不以空格
稳定分词的语言需要适当 tokenizer/analyzer，不能假定英文 tokenizer 足够。

来源：

- [Elasticsearch Japanese Kuromoji analyzer](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-kuromoji)
- [Elasticsearch language analyzers](https://www.elastic.co/docs/reference/text-analysis/analysis-lang-analyzer)
- [Elasticsearch multi-fields](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/multi-fields)

AniVAG 已计划 Exact、BM25 与字符 n-gram；具体 tokenizer 和权重必须用项目自己的日中查询集
评测，不能仅凭模型宣称决定。

### 4. Hybrid retrieval and reranking

Dense 主要补跨语言语义 recall，lexical 主要补精确词项和实体。两者候选可用 RRF 等方法融合，
再用 multilingual reranker 排序。BGE-M3 官方也推荐 hybrid retrieval + reranking。

融合时必须先处理“同一 Dialogue 的多个语言表示”。否则一条有日中双语字幕的事件可能比只有
一种文本的事件多两张票，造成结构性偏置。

## 对 AniVAG 的建议

### 领域层：只有一个 Dialogue Event

以下三份输入不是三条事实：

```text
Japanese ASR candidate
Japanese OCR subtitle
Chinese OCR subtitle
```

它们是模型用来构造同一 Dialogue Event 的多模态 Evidence。普通对白字幕不应额外生成
`ON_SCREEN_TEXT`；只有标题卡、手机消息、UI、路牌等非对白视觉文字才属于
`ON_SCREEN_TEXT`。

### 最小候选表达

字段名仍需后续逐字段签字；本研究建议的最小语义形状是：

```python
class DialogueTextVariant:
    language: str
    role: Literal["ORIGINAL", "TRANSLATION"]
    text: str


class TimelineEvent:
    start_seconds: int
    end_seconds: int
    event_type: Literal[...]
    description: str
    text_variants: list[DialogueTextVariant]
    speaker_person_id: str | None
    participant_person_ids: list[str]
    visible_person_ids: list[str]
```

示例：

```yaml
start_seconds: 12
end_seconds: 16
event_type: DIALOGUE
description: 爱音否认自己有事
text_variants:
  - language: ja
    role: ORIGINAL
    text: "大丈夫、本当に。"
  - language: zh-Hans
    role: TRANSLATION
    text: "我没事，真的。"
speaker_person_id: p_anon
participant_person_ids: [p_anon]
visible_person_ids: [p_anon]
```

这里的关键约束是：

- 时间仍使用源视频绝对整数秒半开区间；
- `DIALOGUE` 是唯一领域事件；
- `ORIGINAL` 保留视频原语言，不统一翻译成中文；
- `TRANSLATION` 可以保存视频中实际出现的中文字幕；
- 日语 ASR 与日语 OCR 如果是在陈述同一原台词，由模型核验、纠错并收敛为一个日语
  `ORIGINAL` variant；
- 中文 OCR 成为同一 Event 的中文 `TRANSLATION` variant；
- 如果没有可靠的日语原文，不能根据中文字幕反向编造日语；
- 原始 ASR/OCR candidates 和时间证据继续保留在 Evidence/Artifact 中，不需要把
  `ASR`/`OCR` 硬编码成最终领域类型；
- 字幕若不是对白，则仍按 `ON_SCREEN_TEXT` 处理。

`role` 是否用 enum、是否改成 `original_text + translations[]`，是后续 Schema 命名选择；真正
需要保住的是“一条 Dialogue、多份有语言身份的文本”这个语义。

### Projection V0

以同一个 Dialogue `event_id=e42` 为例：

```text
Dialogue e42
  ├── variant ja / ORIGINAL
  │     ├── Exact child: e42#ja#exact
  │     ├── lexical child: e42#ja#lexical
  │     └── dense child: e42#ja#dense
  └── variant zh-Hans / TRANSLATION
        ├── Exact child: e42#zh-Hans#exact
        ├── lexical child: e42#zh-Hans#lexical
        └── dense child: e42#zh-Hans#dense
```

所有 child 至少携带：

```text
event_id
parse_result_id
timeline_index
language
text_role
projection_version
```

这些是 Engine 生成的 projection metadata，不让模型生成，也不要求 Generic Harness 理解。

### 检索与防重复投票

建议顺序：

1. 各语言／各 retrieval channel 独立召回 children；
2. 在同一 channel 内先按 `event_id` collapse；
3. event 组分数先采用 best/top child，而不是对语言版本无上限求和；
4. 保留 strongest matched variant 作为命中解释；
5. 再把已折叠的 Event 与 Interval、Chunk、Vision 等真正不同粒度／模态结果融合；
6. 最终 hydrate 一次正式 Dialogue Event。

这样：

- 日语原句 query 可精确命中日语 child；
- 中文字幕 query 可精确命中中文 child；
- 中文语义 query 仍可能通过 multilingual dense 命中日语 child；
- 日中两个 child 同时命中时只返回一次 e42；
- 有双语字幕的事件不会天然获得两倍票数。

### Transcript 派生

Transcript 不再是另一套顶层事实源，应从 Dialogue Events 派生，并带目标语言语义：

```text
transcript(ja)      = 所有 Dialogue 的 ja ORIGINAL variants
transcript(zh-Hans) = 所有 Dialogue 的 zh-Hans variants
```

“双语展示”可以在 UI 或响应层把两个 transcript 对齐展示，但不应把日中两行永久拼成一个不可
解析的 canonical transcript 字符串。

## 不建议的默认方案

### 把日中两行直接拼到 `text: str`

问题：

- 无法确定原语言；
- 无法按语言派生 transcript；
- lexical analyzer 无法明确选择；
- Exact 命中解释变差；
- embedding 把不同语言和标记一起编码；
- 后续无法只重建某种语言 projection。

### 把字幕建成独立 `SUBTITLE` Timeline Event

问题：它把证据模态当成领域事实类型，会让同一句对白成为 ASR Dialogue 与 OCR Subtitle 两套
事实源。普通对白字幕应属于 Dialogue；非对白视觉文字才属于 On-screen Text。

### 只保存中文翻译

问题：丢失原台词、角色语气、专有名词和可验证性，也无法支持原文 Exact retrieval。

### 只保存日语，再依赖 multilingual embedding

问题：会主动丢弃视频中已经存在的中文检索入口；multilingual models 也不是所有 query、实体、
语言对上都等价稳定。

### 默认对所有 query 做翻译

问题：增加延迟和费用，并可能破坏人物名、剧中术语或双关。应先以原 query + multilingual
retrieval 为 P0，再通过召回评测决定是否增加 query expansion fallback。

## 需要用 AniVAG 数据验证的事项

这次研究不能替代项目级 evaluation。实现前至少需要建立以下查询切片：

- 日语原句查日语；
- 中文字幕原句查中文；
- 中文意译查只有日语的 Dialogue；
- 日文人物名与中文译名；
- 片假名、罗马字、中文译名混合实体；
- 双语字幕事件与单语事件的公平排序；
- ASR 与同语言 OCR 轻微不一致；
- OCR 只有中文字幕、原语言无法可靠恢复；
- 非对白屏幕文字与对白字幕的区分。

至少比较：

```text
single mixed string
vs. per-language variants

multilingual dense only
vs. lexical only
vs. hybrid

original query
vs. translated-query expansion

no event collapse
vs. best-child event collapse
```

评测指标除 Recall@K、MRR/nDCG 外，还应单独记录：

- logical event duplicate rate；
- 双语事件相对单语事件的曝光偏置；
- matched variant language；
- exact-line hit rate；
- person/entity preservation；
- query translation failure rate。

## 最终判断

用户提出的“归为同一个 Dialogue，交给模型识别”是领域建模上的正确方向。需要补上的只是：

> 模型识别后不要把所有语言压扁；保留同一 Dialogue 下的语言 variants。Engine 再把 variants
> 拆成检索 children，并在 `event_id` 层折叠。

因此，本研究建议把 Parse V2 的最小 Schema 从单值 `DIALOGUE.text` 重新评估为一个可表达多语言
版本的结构；但不新增 `SUBTITLE` 事件类型，不让 Harness 理解 ASR/OCR，也不把 query translation
固化为必经阶段。
