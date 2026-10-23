> **Event 检索最终合同（2026-10-17）**：以 [Sub-Chunk / Event TRD](../../../server/design/sub-chunk-trd.md) 为准。完整、有序的 Chunk Timeline 保持唯一权威；不新增 Event / Frame SQL 表或 Frame Index。现有 Chunk INDEX 派生独立 Event ES 的词法、文本向量及抽帧视觉向量，首次 INDEX 在任务 detail 冻结目标，不改变 ParseConfig。Agent 可直接细搜；旧研究不替代本轮运行验收。
> 任务与恢复以 [Parse Agent TRD 当前基线](../../../agent/video_parse_v2/design/trd.md) 为准：Interval 仅 Refine，现有 ChunkTask 的 INDEX 阶段承担 Chunk/Event 索引。本文件 §2.3 中遗留的 Interval 索引状态与调度描述被该基线取代；历史样例及“当前使用 Qdrant”的文字保留其原记录时点，不表示现在的实现状态。

# 零、整体原则和架构设计

> **文字、图片与 GIF 共享同一条外层检索流程（目标设计，尚未全部实现）：**
> 把输入变成检索信号 → 多路召回候选 → 归并到 Chunk → 核验命中证据和时间。
> **图片、GIF 不应固定套用文字检索的所有通道，也不必先“识别成文字”。**

| 输入 | 合适的召回方式 |
| --- | --- |
| 文字 | 默认使用 BM25、Chunk 文字综合语义 Dense 和 Chunk 视频 Dense；学习式文本 Sparse 可选，默认关闭。两条 Dense 路独立召回，不合成一个向量。 |
| 图片 | 用视觉向量寻找语义相似画面；同源截图或局部裁剪另需帧级或局部特征匹配，并核验定位依据。 |
| GIF | 解出多帧，综合视觉信息召回；同源片段定位还要核验多帧的先后顺序和时间位置，语义相似检索可放宽时序要求。 |

多帧综合召回不等于把 GIF 压成单帧，也不等于必须为每帧建立 ES 文档。
上述内容定义检索目标与外层编排；ES Index 的字段、粒度和各路实现仍需结合
RAGFlow 的分片与索引链路另行设计，不能把本表当作已落地的索引结构。

一整个agent for：一个video解析
要求：engine提供内部schema
不管engine怎么换策略，我们同一套agent harness都可以被复用填写同样的schema内容

因此我们的engine主要起到这么一个指引harness体系收敛方向 + embedding indexing的作用

职责归属：为harness提供收敛target（解析结果本质为了更好的RAG服务），并针对更好的更有效的RAG提出相应的embedding 策略（例如提出要向量化的内容？——就是设计相关的schema）

> so, Engine 所拥有的稳定领域合同最重要；Schema 是这个合同的主体，但不是全部。


# 一、解析schema设计

> 状态：本节为 Parse V2 的设计基线。按 2026-09-27 的明确约定，人物字段直接填写人名。
> 现有 [`engine/parsev2/schemas.py`](../schemas.py) 仍保留旧的人物 ID 字段，尚待同步；
> 本节的人名合同不应被旧实现覆盖。本次只修正文档，不表示运行时代码已经完成同步。

## 1.1 Schema 的归属与执行边界

`engine/parsev2/schemas.py` 负责承载 Video Parse V2 结构化解析合同，人物字段待按本节同步。Engine 使用
Pydantic 定义领域模型、不变量和可导出的 JSON Schema；通用 Harness 在运行时接收该
Schema，组织证据并调用模型填写结果，不硬编码任何视频领域字段。

当前主链路是：

```text
Raw Evidence（ASR / OCR / 画面 / 上下文）
    ↓
Schema-driven Harness + Model
    ↓
VideoParseResult
    ↓
Engine Pydantic 校验
    ↓
Retrieval Projection（下一阶段单独定义）
    ↓
Exact / Text / Vision / Storage
```

Harness 可以随 Engine 提供的 Schema 变化而复用。模型直接产出正式的结构化解析结果，
Engine 负责领域合同和确定性校验；V2 不再把 Proposal / Patch / Canonical 或固定三阶段
流程作为主线。

Parse Schema 与 Retrieval Projection 是两个合同。Engine 负责二者的定义及派生策略，
但不负责调用具体 embedding provider、写向量数据库或持久化任务状态。

## 1.2 顶层最小结果

设计中的最小 `VideoParseResult` 只有两个必填字段：

```text
VideoParseResult
├── persons: string[]                # 直接保存人名，例如“千早爱音”“要乐奈”
└── timeline: TimelineEvent[]
```

`persons` 直接保存本次结果中确认的人名。Timeline 的 `speaker`、`participants`、
`visible_persons` 也直接填写相同的人名，不维护人物 ID 到名称的映射。
人物名称在同一次解析中保持写法一致；无法确认姓名时保留事件描述，说话人无法确认则填 `null`。

当前顶层不再并行维护 `speakers`、`actors`、`transcript` 等重复事实源；这些都是 Timeline
的派生视图。当前也不包含 `story_summary`，需要时应先明确消费方和不重复投影的规则。

## 1.3 TimelineEvent：共享基类 + 具体事件联合类型

`TimelineEventBase` 是实际承载公共字段和校验逻辑的 Pydantic 基类，不是一个只声明行为
的 interface。每种事件是具体子类；`TimelineEvent` 则是以 `event_type` 为 discriminator
的联合类型：

| `event_type` | 具体类型 | 额外字段 |
| --- | --- | --- |
| `SCENE` | `SceneEvent` | 无 |
| `ACTION` | `ActionEvent` | 无 |
| `DIALOGUE` | `DialogueEvent` | `speaker`、`text_variants` |
| `EMOTION` | `EmotionEvent` | 无 |
| `RELATION_CHANGE` | `RelationChangeEvent` | 无 |
| `ON_SCREEN_TEXT` | `OnScreenTextEvent` | `language`、`text` |
| `OTHER` | `OtherEvent` | 无 |

所有事件共享：

| 字段 | 语义 |
| --- | --- |
| `start_seconds` | 源视频中的绝对开始时间，严格整数秒，包含该秒 |
| `end_seconds` | 源视频中的绝对结束时间，严格整数秒，不包含该秒 |
| `description` | 对该原子事件的语义描述，不承担原台词字段的职责 |
| `participants` | 在语义上直接参与该事件的人物 |
| `visible_persons` | 该时间范围内画面中可见的相关人物 |

时间统一使用源视频绝对整数秒半开区间 `[start_seconds, end_seconds)`，并要求
`0 <= start_seconds < end_seconds`。整数秒是 V2 明确采用的统一时间单位。

Timeline 是稀疏事件序列，不是互斥分段。同一时间可以同时存在 Dialogue、Emotion、
Action 等不同事件；结果只要求按 `start_seconds` 非递减排序，不要求事件无重叠，也不
要求无缝覆盖整段视频。Event 不按中点唯一归属某个 Chunk；Event 与 Chunk 的关系由后续
Projection 派生。

`participants` 与 `visible_persons` 不能合并：speaker 可以在画外，画面中出现的
人物也不一定参与当前事件。

## 1.4 Dialogue 与多语言文本

一个 `DialogueEvent` 表示一个逻辑 utterance，而不是一种采集模态或一种语言。它的专属
字段是：

```text
speaker: string | null
text_variants: DialogueTextVariant[1..n]

DialogueTextVariant
├── language: string                 # BCP 47 风格，例如 ja、zh-Hans
├── role: ORIGINAL | TRANSLATION
└── text: string                     # 对应语言的原文，不改写为统一语言
```

`speaker` 是必填但可为 `null` 的字段：能够可靠归因时直接填写人名，无法可靠归因
时显式填写 `null`，不能通过缺失字段表达不确定性。

`text_variants` 本身至少包含一项。“语言字段可选”指哪些语言条目存在是动态的，而不是为
日语、中文、英语分别设计固定的 nullable 字段。当前不强制同时存在 `ORIGINAL` 和
`TRANSLATION`；只保存证据足以确认的语言版本。

例如，日语原声与 OCR 识别出的中文字幕表达同一句对白时，应形成同一个 Dialogue：

```yaml
event_type: DIALOGUE
start_seconds: 12
end_seconds: 16
description: 爱音否认自己在难过
speaker: 千早爱音
participants: [千早爱音, 要乐奈]
visible_persons: [千早爱音, 要乐奈]
text_variants:
  - language: ja
    role: ORIGINAL
    text: 大丈夫、本当だよ。
  - language: zh-Hans
    role: TRANSLATION
    text: 我没事，真的。
```

如果画面字幕本身是日中双语，模型识别语言及原文/译文关系后，仍然填入同一个 Dialogue
的不同 `text_variants`。日语原台词就保存日语，中文译文就保存中文，不能用译文覆盖原
台词。

ASR 与 OCR 是 Evidence / Artifact 层的不同证据来源，不是两套平行的正式事实源，也不
分别生成两个 Dialogue。正式结果只保存模型综合证据后认可的逻辑 utterance 及其语言
变体；原始候选、置信度和采集来源留在 Evidence / Artifact 中。

`language` 按 BCP 47 风格填写。当前 Schema 确保它非空，并按大小写不敏感的
`(language, role)` 组合去重，但尚未实现完整的 BCP 47 语法规范化。

普通对白字幕归入 Dialogue；标题、UI、路牌、手机消息等非对白视觉文字才使用
`ON_SCREEN_TEXT`。当前 `OnScreenTextEvent.text` 必填，`language` 必填但可以显式为
`null`。

## 1.5 重叠事件的表达

Dialogue、Emotion、Action 等是不同的原子事实。一个人物边说话边表现出情绪时，不增加
`EMOTIONAL_DIALOGUE`，也不把 `event_type` 改成数组，而是使用时间重叠的两条事件：

```yaml
- event_type: EMOTION
  start_seconds: 11
  end_seconds: 17
  description: 爱音强忍眼泪，显得十分难过
  participants: [千早爱音]
  visible_persons: [千早爱音]

- event_type: DIALOGUE
  start_seconds: 12
  end_seconds: 16
  description: 爱音否认自己在难过
  speaker: 千早爱音
  participants: [千早爱音, 要乐奈]
  visible_persons: [千早爱音, 要乐奈]
  text_variants:
    - language: ja
      role: ORIGINAL
      text: 大丈夫、本当だよ。
```

是否生成独立 Emotion 的判断是：移除对白文本后，这个情绪是否仍然是独立成立、影响理解
或值得检索的事实。普通语气修饰可以留在 Dialogue 的 `description` 中，避免 Timeline
过密。

P0 不增加显式 `linked_event_id`。后续 Projection 可以使用“时间重叠 + 同一视频内相同人名”组合
相关事件，不能只凭时间重叠，否则可能把同一画面中 A 的对白和 B 的情绪错误关联。

## 1.6 确定性不变量

Engine 应执行以下结构校验；其中人物字段待实现直接使用人名的合同：

- 所有模型拒绝未知字段，嵌套模型在组装结果时重新校验；
- 所有字符串字段都必须至少包含一个非空白字符，台词内容不会被自动 trim 或改写；
- 时间必须是严格整数，且结束时间大于开始时间；
- `persons` 中的人名必须唯一；
- `participants` 与 `visible_persons` 各自不得重复；
- Timeline 人物字段中填写的人名必须存在于 `persons`；
- Dialogue 的 `speaker` 非空时必须属于该事件的 participants；
- `DialogueEvent.text_variants` 至少一项，且 `(language, role)` 组合按语言大小写不敏感地
  保持唯一；
- 非 Dialogue 事件不能携带 Dialogue 专属字段；未知或缺失的 `event_type` 会被拒绝；
- Timeline 只按开始时间排序，合法的重叠事件不会被合并或拒绝。

这些都是 Engine 可以确定性检查的结构不变量。台词是否正确、语言关系是否成立、speaker
归因是否可信等语义判断由模型结合证据完成，不伪装成 Pydantic 能证明的规则。

当前顶层结果不包含可信的视频总时长，因此 Schema 只能验证 `0 <= start < end`，不能独立
验证 `end_seconds <= source_video_duration`。该约束应由持有真实媒体元数据的 Host 在结果
校验或 Projection 前执行，而不是让模型生成一个可自证的 `duration_seconds`。

## 1.7 派生视图与单一事实源

以下内容直接从 Timeline 派生，不再由模型并行维护：

```text
speakers
  = 所有非空 DialogueEvent.speaker 的并集

actors / 出镜人物
  = 所有 visible_persons 的并集

participants
  = 所有 participants 的并集

transcript
  = 按时间排列 DialogueEvent.text_variants
    再由调用方按 language / role 选择所需视图
```

这避免 `speaker[]`、`actor[]`、`utterances[]`、`transcript` 和 `timeline[]` 成为多套可能漂移
的事实源。`description` 与 `text_variants` 也不是同一字段：前者描述事件语义，后者保存可
逐字使用的台词。

## 1.8 当前明确不进入 Parse Schema 的内容

当前 Parse Schema 不包含：

- 原始 ASR / OCR 候选、采集模态或相互竞争的 transcript；
- Proposal、Patch、Canonical 三阶段中间对象；
- Chunk、`self_compact` 或 Event 与 Chunk 的固定归属；
- `event_id`、`video_id`、`chunk_id` 和数据库主键；
- `embedding_text`、向量、维度、模型名、RRF 权重；
- Elasticsearch 索引文档、MySQL 记录、任务状态等基础设施结构；
- Exact、Event、Interval、Chunk、Vision、Storage 的投影结果；
- 尚无明确消费方的 confidence、provenance、显式因果边和 Revision 字段。

这些字段不能因为“以后可能需要”就提前搬入模型输出。2026-10-17 修订：持久化 Event ID
由宿主按 `(parse_task_id, video_chunk_id, version, timeline_index)` 确定性生成；version 复制 Chunk 正文版本，只保证版本内
稳定；检索和存储字段属于 [Event 最终合同](../../../server/design/sub-chunk-trd.md)，不要求模型维护身份。

## 1.9 与 Projection 和 Harness 接线的后续工作

下一步以该 `VideoParseResult` 为唯一正式事实来源，定义 Projection V0，以及 Exact / Event /
Interval / Chunk / Vision / Storage 各自消费哪些字段、如何拆分多语言 Dialogue、如何处理跨
Chunk Event 和检索结果去重。

Engine 可以通过 `VideoParseResult.model_json_schema()` 向 Harness 提供原始 JSON Schema。
当前 Gateway 的部分 Provider 适配会内联 `$defs`，但尚未同步改写 discriminator mapping；
因此在真正接入 Harness 前，需要在 Provider schema normalization 层修复并增加集成测试，
不能为了适配单一 Provider 反向修改本领域模型。

现有实现和直接验证用例分别位于以下位置，人物相关部分待同步为本节的人名合同：

- [`engine/parsev2/schemas.py`](../schemas.py)
- [`tests/engine/parsev2/test_schemas.py`](../../../tests/engine/parsev2/test_schemas.py)




# 二、检索策略


## 2.1 ragflow已有的召回、解析机制拆解

> 调查基线：2026-09-26，本地 `../ragflow`，commit
> `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
> 以下记录源码中的实际机制；Python 普通文档入库、可配置 Pipeline、Go 实现和
> Agentic RAG 的行为分别说明。对 AniVAG 的借鉴放在最后，不直接改变第一章的 Parse Schema。

### 2.1.1 先统一术语：召回、融合、精筛

- **召回**：从整个检索范围内找出有限候选。向量召回和全文召回本身也会产生分数。
- **融合**：把多路结果组合起来；可以融合名次，也可以融合分数，具体看算法。
- **精筛 / 重排**：针对已召回的候选，再结合当前问题计算相关性，重新排序并选出结果。

**Reranker 打分 + 排序 + 阈值过滤 / Top N，已经构成精筛。**
“打分后再精筛”如果只是指排序和截取，不代表后面还缺一次模型调用。
是否属于精筛，取决于它在检索流程中的作用；不能仅凭“有没有分数”区分阶段。

本次核实的策略如下：

| 策略 | RAGFlow 中的实际位置 |
| --- | --- |
| Dense 向量召回 | Query 编码后查询 Chunk 的 `q_<dim>_vec`，使用余弦相似度 |
| 全文 / 关键词召回 | 查询正文、标题、关键词、生成问题等多个字段，字段权重不同 |
| 混合检索 | 普通 Chunk 路径使用向量与全文能力；具体融合、重排行为随后端变化 |
| RRF | 已核实用于 Agentic RAG 的 Claim 检索；普通 Chunk 主路径不能统一描述成 RRF |
| 模型 Reranker | 可选，对 Query 与候选自然语言正文打分，再参与最终排序 |
| 智能元数据过滤 | 支持 `manual`、`auto`、`semi_auto`，主要先圈定文档范围 |
| 关键词、问题、摘要增强 | 可选，生成不同的检索表示；不等于每个字段都有独立向量 |
| 父子 Chunk | 可选，命中子块后展开父内容 |
| RAPTOR | 可选，构造更高层的摘要及其检索表示；另有结构树输出分支 |

ColBERT、RankLLM 不是本次所查普通主链路固定调用的算法。
该链路调用配置的 Reranker 适配器，外部服务采用什么模型需另外核实。

### 2.1.2 解析输出与最终索引记录是不同合同

可以把对应阶段整理为：

```text
原文 / PDF / 图片 / 表格
    ↓ Parser：提取正文、布局、图片、位置等内容
结构化解析输出
    ↓ Chunker：根据策略组织 Chunk
Chunk + 可选的关键词 / 问题 / 摘要 / 元数据
    ↓ Tokenizer / Embedding：构造检索字段和向量
索引记录
    ↓ 查询侧消费这些字段
候选召回 → 融合 / 重排 → 过滤与截取 → 返回证据
```

Go 的 `ParserOutputs` 承载文件名、类型、结构化 JSON 等；`ChunkDoc` 则显式定义
`text`、`questions`、`keywords`、`summary`、上下文、父块、位置等公共字段，
动态增强字段通过 `Extra` 承载。

Python Pipeline 的上游合同使用 Pydantic 包装，但内部 `chunks` 仍是 `list[dict]`。
因此，不能把 RAGFlow 描述为“所有解析模型填写同一个严格的最终索引 Schema”。

#### 数据实例、阶段 Schema、字段转换表分别是什么？

Schema 规定字段、类型、必填性和约束；填写了具体内容的 JSON 是数据实例。
RAGFlow 在不同阶段使用不同的合同，不能把所有字段合并成一份让解析模型填写的对象。

| 对象 | 它表达什么 | 怎样查到真实定义 |
| --- | --- | --- |
| 下方的设备过热 JSON | 人工编写的 Chunk 增强结果示例 | 用于说明字段用途，不是完整 Schema，也不是一次模型调用的输出保证 |
| Go `ChunkDoc` | Chunker / Tokenizer 边界的实际类型合同 | `internal/ingestion/component/schema/chunk_types.go` |
| `2.1.3` 字段转换表 | 上游字段怎样加工、由哪个查询步骤消费 | 对应 Tokenizer、索引写入和检索代码，表中字段横跨多个阶段 |
| 最终索引记录 | 检索后端实际保存、查询的数据 | 由写入映射、ES mapping 等后端定义及查询代码共同约束 |

例如，真实 `ChunkDoc` 中有下面这些字段（只摘录相关部分）：

```go
Text      string                     `json:"text,omitempty"`
Questions string                     `json:"questions,omitempty"`
Keywords  string                     `json:"keywords,omitempty"`
Summary   string                     `json:"summary,omitempty"`
Extra     map[string]json.RawMessage  `json:"-"`
```

`metadata` 等动态增强字段通过 `Extra` 的自定义序列化逻辑承载。
完整结构也包含 token、位置和布局等字段；它不是纯粹的 LLM 语义输出类。

下面是用于理解字段的**中间数据实例**，不是从源码复制的完整 Schema。
各增强字段按配置产生，并非全部必填，也不要求一次模型调用同时生成：

```json
{
  "text": "设备过热时，请先断电，等待冷却后再重新启动。",
  "keywords": "设备过热,断电,冷却,重新启动",
  "questions": "设备过热后应该怎么办？\n什么时候可以重新启动？",
  "summary": "设备过热后的断电、冷却与重启步骤。",
  "metadata": {"设备类型": "控制器"}
}
```

进入 Go 索引边界时要求存在字符串 `text`，随后映射为 `content_with_weight`。
原始 `questions`、`keywords`、`summary` 等字段在被消费后可能被删除；索引记录保留的是
正文、派生字段、向量和定位信息。这里的字符串检查也不等于语义正确性检查。

源码：[ParserOutputs](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/component/schema/parser.go:24)、
[ChunkDoc](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/component/schema/chunk_types.go:98)、
[Python Pipeline 合同](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/schema.py:20)、
[索引边界映射](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/task/indexdoc/process.go:68)。

### 2.1.3 字段 → 索引加工 → 查询消费

这张表是**转换与消费关系表**，不是第二份候选 Schema。
左列包含多个阶段的字段；右两列解释怎样把内容变成可检索表示，以及查询如何使用它。

| 解析 / 增强字段 | 索引侧怎样处理 | 查询侧怎样使用 |
| --- | --- | --- |
| `text` | 正文保存到 `content_with_weight`；按路径构造正文 token 和向量 | 提供语义召回表示、重排正文与返回证据 |
| 文件名 / 标题 | `docnm_kwd`、`title_tks`、`title_sm_tks`；可与内容向量加权 | 标题全文匹配；影响向量表示 |
| `keywords` | 拆成 `important_kwd`，另生成 `important_tks` | 在全文查询及后续词项匹配中使用 |
| `questions` | 拆成 `question_kwd`，另生成 `question_tks` | 匹配类似问法；普通 Python 入库还可能用它替代正文作为内容 Embedding 输入 |
| `summary` | Pipeline Tokenizer 优先用摘要构造 `content_ltks` / `content_sm_ltks` | 全文匹配可以针对摘要；是否参与 Dense 由该路径的 Embedding 规则决定 |
| `context_above` / `context_below` | Go Tokenizer 的 `text` 表示可包含上下文与本块正文 | 补充上下文；不等于最终存储正文也自动变成相同的拼接文本 |
| `metadata` | 所查 Pipeline 会汇入文档元数据，随后从 Chunk 中移除 | 先过滤文档，再限制 Chunk 检索范围 |
| `positions` 等位置数据 | 转换为 `page_num_int`、`top_int`、`position_int` | 定位原文和展示引用 |
| `doc_id` / `kb_id` / Chunk ID | 标识归属和索引记录 | 范围过滤、结果关联、回查 |
| `q_<dim>_vec` | Embedding 阶段生成的主向量字段 | Dense KNN 召回 |
| `mom` / `mom_with_weight` | 构造父内容记录与子块的 `mom_id` | 子块命中后展开父内容 |
| `tag_feas` / `pagerank_fea` | 可选的排名特征 | 作为额外分数；与硬性元数据过滤是不同机制 |

这里要学的是：**同一份内容可以拥有不同的检索表示，每种表示都有明确的消费方。**
正文、分词字段、生成问题和向量的内容不一定完全相同。

源码：[Python 字段加工](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/tokenizer.py:140)、
[Go 字段加工](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/component/tokenizer.go:764)、
[元数据汇总](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/task/indexdoc/process.go:170)、
[排名特征](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:501)。

### 2.1.4 Embedding 策略：必须分清入口

#### A. Python 普通文档入库

内容 Embedding 的输入优先级是：

```text
存在非空 question_kwd → 将多个问题用换行拼接
否则                 → content_with_weight
```

标题和内容分别编码，再按权重合成一个主向量：

```text
chunk_vector = w × title_vector + (1 - w) × content_vector
默认 w = 0.1
```

所以，开启问题生成以后，内容向量可能表达“这一块能够回答哪些问题”。
原始正文仍可供重排和证据展示使用；多个问题在这个分支中合并编码，并非每个问题一个点。

源码：[内容选择与加权](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_utils.py:160)、
[普通 Embedding 执行](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_service.py:77)。

#### B. 可配置 Pipeline / Tokenizer

- Python Tokenizer 通过 `fields` 选择参与 Embedding 的字段，默认是 `text`。
- Go Tokenizer 对 `text` 使用 `context_above + text + context_below` 的内容表示。
- 文件名向量可以按配置参与加权；不能把普通入库的“问题优先”直接套到这些入口。
- 存在摘要时，摘要可以替代正文构造全文 token；它不会因此自动替代所有路径的 Dense 输入。

Python Dataflow 另有一个兜底分支：当流程输出没有向量时，以
`questions → summary → text → content_with_weight` 的优先级补算向量。
这也不能当成所有 Pipeline 的默认规则。

源码：[Python Tokenizer](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/tokenizer.py:37)、
[Go 内容拼接](/Users/bytedance/workspace/other-project/ragflow/internal/ingestion/component/tokenizer.go:864)、
[Dataflow 兜底入口](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/dataflow_service.py:144)、
[兜底内容选择](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_utils.py:85)。

### 2.1.5 查询怎样与这些索引字段对应？

**Dense 路径**：使用知识库对应的 Embedding 模型编码 Query，查询 `q_<dim>_vec`。

**全文路径**：将 Query 分词、组织查询表达式，查询下面这些字段；`^` 后面的数是字段 boost：

```text
title_tks^10       title_sm_tks^5
important_kwd^30  important_tks^20
question_tks^20
content_ltks^2    content_sm_ltks
```

这些权重说明：解析时提取关键词、生成问题，在查询时有明确的匹配入口。
部分 Query 分支还使用同义词和相邻词短语来组织查询。

在 ES 中，部分文本字段使用默认相似度，部分 `*_tks` 字段配置了 `scripted_sim`。
因此，“向量 + BM25”可以帮助理解混合检索，但不足以精确描述这一版的全部评分行为。

ES 适配器还把包含全文条件的 `bool_query` 用作 KNN filter。
这条路径不能一概理解成“两路完全独立召回，再对两个列表做 RRF”。

源码：[Query 向量](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:104)、
[全文字段与查询](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28)、
[ES 字段映射](/Users/bytedance/workspace/other-project/ragflow/conf/mapping.json:71)、
[ES 查询组装](/Users/bytedance/workspace/other-project/ragflow/rag/utils/es_conn.py:244)。

#### 普通 ES 路径：没有配置模型 Reranker 时

候选取得后，通过限定候选 ID 的 KNN 请求取得向量分数，再与本地计算的词项匹配分数融合：

```text
final_score = (1 - w) × T + w × V + B

T：本地 token_similarity 计算的词项匹配分数
V：ES KNN-only 查询返回的 _score
B：可选的标签、pagerank 等排名特征分数
```

`T` 使用正文、标题、关键词、生成问题等字段构造的 token 序列。
它不是直接取 ES 的 BM25 分数。`V` 的读取函数直接返回 `_score`，没有额外执行原始余弦值的
反变换；不能仅根据函数注释把它写成已还原的原始 cosine。候选获取阶段的融合参数，
也不能直接等同于这里的权重。

Infinity 的普通路径有区别：使用后端的 `weighted_sum` 融合，并配置 `atan` 归一化；
没有模型重排时，检索层可以直接消费后端分数。

源码：[ES 候选再评分](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:533)、
[ES 分数读取](/Users/bytedance/workspace/other-project/ragflow/common/doc_store/es_conn_base.py:297)、
[加权公式](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:604)、
[Infinity 归一化](/Users/bytedance/workspace/other-project/ragflow/rag/utils/infinity_conn.py:264)。

#### RRF 出现在哪里？

Agentic RAG 的 Claim 检索分别取得 Dense 和全文结果，再调用 `_rrf_fuse`：

```text
RRF(candidate) = Σ 1 / (60 + rank)
rank 从 1 开始
```

它融合的是排名。Claim 记录通过 `source_chunk_ids` 关联原始 Chunk，便于回到证据。
“RAGFlow 有 RRF”成立；把普通 Chunk、Claim 和不同后端都画成同一条 RRF 流程会失真。

源码：[RRF 实现](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/tools/navigation.py:1529)、
[Claim 召回调用](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/tools/navigation.py:1617)。

### 2.1.6 模型 Reranker：对候选再次打分，就是精筛的核心

公共接口可以理解为：

```python
scores, used_tokens = rerank_model.similarity(query, candidate_texts)
# scores[i] 对应 candidate_texts[i]
```

普通 Chunk 主链路优先将 `content_with_weight` 中的自然语言正文传入模型。
正文不存在时，才回退到拼接正文 token、标题、关键词、生成问题等字段。
它不会自动把所有 Schema 字段、元数据和图片一起传给模型。

开启模型重排以后，最终评分变成：

```text
final_score = (1 - w) × T + w × R + B

R：Reranker 对 Query 与候选正文的相关性分数
```

这里复用了名为 `vector_similarity_weight` 的参数，但这一步权重乘的是 `R`。
随后代码执行降序排序、阈值过滤和 Top N 截取，完成这一轮精筛。

补充几个接口细节：

- 检索函数的 `rerank_candidates_count` 默认是 64，调用方可以覆盖。
- 基类处理空输入、长度限制和分数范围；已在 `[0, 1]` 内的分数保留，超范围时按规则处理。
- `[0, 1]` 分数不等于“答案正确概率”。精筛是处理阶段，实际精度仍需评测。
- Jina、Cohere / VLLM、HuggingFace 等通过不同适配器接入；用量可能来自响应或本地估算。
- 接口约定的是相关性评分，不限定模型必须是 Encoder-only，也不要求生成聊天回答。
- 模型只能重排传入的候选，未被召回的内容不在本次评分范围内。

源码：[构造正文与模型重排](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:664)、
[排序、过滤和截取](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:842)、
[Reranker 公共接口与适配器](/Users/bytedance/workspace/other-project/ragflow/rag/llm/rerank_model.py:58)。

### 2.1.7 元数据过滤：先把问题变成结构化检索范围

所查 Pipeline 将 Chunk 提取出的 `metadata` 汇总到文档元数据。
查询时按条件得到 `doc_ids`，再把它们传给 Chunk 检索。

- `manual`：使用明确配置的字段、运算符和值。
- `auto`：让 LLM 根据问题及可用元数据生成条件。
- `semi_auto`：限制可选字段或运算符，再由 LLM 生成条件。

因此，它实现的是“解析时产生元数据 → 查询时生成条件 → 按条件圈定文档”。
不能直接推导为任意 Chunk 的所有元数据都原样留在向量 payload 中。

一个会影响结果的实际行为：Python `auto` / `semi_auto` 分支无匹配时会返回 `None`；
普通调用链收到 `doc_ids=None` 时不添加文档 ID 限制，可能回到更宽的知识库检索。
`manual` 在存在条件但无命中时使用 `['-999']` 表达空结果。
所以，自动元数据过滤不能直接承担必须严格满足的业务约束。

源码：[过滤模式与空结果处理](/Users/bytedance/workspace/other-project/ragflow/common/metadata_utils.py:239)、
[调用方传入检索范围](/Users/bytedance/workspace/other-project/ragflow/api/db/services/dialog_service.py:757)、
[检索条件构造](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:230)。

### 2.1.8 父子块、RAPTOR，以及不同材料的表示

#### 父子 Chunk：召回单元与返回证据可以不同

子块用于召回，保存 `mom_id`；命中后可以回查更完整的父内容。
所查普通入库路径中，父记录保存正文并设置 `available_int=0`，且不保留主向量字段，
避免它作为普通可检索子块重复参与召回。

这说明“小单元适合匹配，大单元适合提供上下文”可以通过显式关联实现。
展开父内容本身不会再调用一次 Reranker。

源码：[父内容构造](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/chunk_service.py:341)、
[子块展开父内容](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:1085)。

#### RAPTOR：额外构造更高层的摘要表示

RAPTOR 对内容聚类、生成摘要，并对摘要编码；检索可以命中更高层的主题表示。
它增加了派生内容和模型计算，不是所有 Chunk 的必经步骤。

本版代码需要区分两个输出分支：普通 `is_tree=False` 会生成带正文和向量的摘要记录；
`is_tree=True` 会保存结构树记录，并设为 `available_int=0`。
结构树不能直接等同于普通可召回的摘要点。

源码：[RAPTOR 输出分支](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/raptor_service.py:344)。

#### 表格与图片

- 表格解析器可以把一行组织成一个 Chunk，将参与检索的列格式化为“字段名：值”的正文；
  另一些列按配置保留为结构化数据。不是每一列分别生成向量。
- 所查图片解析路径先做 OCR，在相应条件下调用视觉模型描述图片，再把文字组织成可索引正文。
  这条路径使用文字表示，不能据此断言普通 Chunk 重排正在直接读取图片。

源码：[表格行构造](/Users/bytedance/workspace/other-project/ragflow/rag/app/table.py:615)、
[图片 OCR 与描述](/Users/bytedance/workspace/other-project/ragflow/rag/app/picture.py:78)。

### 2.1.9 对 AniVAG 的借鉴：把解析和检索的对应关系写明确

下面是从上述实现得到的设计启发，尚未作为本项目的新增实现：

1. **正式事实与检索表示分别定义。** 保持第一章的 `VideoParseResult` 作为正式解析合同，
   从 `persons + timeline` 派生检索输入；不让 Harness 同时填写一套容易漂移的索引正文。
2. **每个投影字段写清消费方。** 例如 Dialogue 的不同 `text_variants` 怎样进入精确匹配，
   事件 `description` 怎样进入 Dense / Reranker，人物与时间怎样用于过滤、关联和定位。
3. **不同用途可以采用不同内容表示。** Dense 输入、关键词字段、重排正文、返回证据分别确定；
   借鉴生成问题或摘要前，应先决定它们替代哪个表示，还是增加独立的检索单元。
4. **召回粒度和证据粒度可以分别选择。** Event / Interval / Chunk 的关联由 Projection 派生，
   固定 VideoChunk 边界继续作为已有拓扑；不据此恢复第一章已排除的固定 Event 归属规则。
5. **精筛使用明确的候选合同。** 保留候选对应的视频、绝对时间和证据，决定 Reranker 实际接收
   哪些正文、对白、图片或视频；模型分数由 Engine 的排序策略消费，Provider 调用由 Gateway 承接。

现有 V1 已有 ASR、文本、视觉三路融合和检索正文构造；这些不是本次才提出的新能力。
V2 要解决的是让各路检索从同一份正式事实派生，并明确新增精筛怎样消费证据。

#### 把示例中的五个字段对应到我们的设计

| RAGFlow 示例中的概念 | 对 AniVAG 的启发 | 由谁产生 |
| --- | --- | --- |
| `text`：可阅读的内容 | 按所选策略，将事件描述与所需语言的对白组织成文本检索 / 重排输入；Video Source 不要求独立的 `text` 字段 | Engine 定义投影规则，消费已经确认的 Timeline |
| `keywords`：重要词语 | 人物名、动作词、屏幕文字等可以成为词项检索输入 | 先复用正式字段；需要额外提取时，再定义可选增强步骤 |
| `questions`：这段内容能回答什么 | 可以生成更接近用户问法的检索表示，例如“哪一段中她说没事后转身擦眼泪？” | 基于正式事实的可选模型增强，是否采用需评测 |
| `summary`：压缩内容 | 较长 Interval / Chunk 可以有摘要表示，原视频与结构化解析结果继续保留 | 按检索消费需求派生；涉及语义概括时可能需要模型 |
| `metadata`：过滤和定位依据 | 事件类型、人物名称、语言、时间可直接用于结构化过滤；视频归属由 Host 提供 | 从正式字段和可信业务上下文确定性映射 |

对应的合同关系是：

```text
Parse Schema：视频里发生了什么（当前 persons + timeline）
    ↓ Engine 定义投影规则；可选模型增强通过 Harness / Gateway 执行
Retrieval Projection：怎样表述这些事实，以便匹配问题和重排
    ↓ Embedding Provider 编码；Storage Adapter 映射后端字段
Index Schema：检索后端怎样保存检索表示、词项、向量和定位信息
```

因此，不需要因为 RAGFlow 有 `keywords/questions/summary`，就把它们全部加入当前
`VideoParseResult`。这些检索增强是否存在，应由明确的消费方与效果验证决定。
生成问题也不能补写事实中不存在的因果或情绪，原始台词不能被“更好检索的说法”覆盖。

当策略只是重新组织已有事实或更换编码方式时，可以重建 Projection / 索引；
当新需求依赖正式结果没有记录的事实时，仍需补充解析合同或证据处理。
通用 Harness 通过接收 Engine 提供的 Schema 复用，不把这五个字段硬编码成固定解析流程。

参考：[现有检索文本构造](/Users/bytedance/workspace/other-project/AniVAG/engine/retrieval/video_retrieval.py:97)、
[现有三路融合](/Users/bytedance/workspace/other-project/AniVAG/engine/retrieval/video_retrieval.py:172)。

#### Chunk 前后上下文的粒度

当前 Source 以固定 VideoChunk 为主体，通过 Chunk 关联原视频及固定时间边界。
视频没有必须填写的“正文”，Source 不要求独立的 `text` 字段。
TimelineEvent 保存对视频事件的结构化解析；摘要、关键词、问题属于可选增强。
文本召回或文本重排需要的输入，由 Engine 按所选策略从这些已有结果派生。

`context_before` 和 `context_after` 分别来自前一个、后一个 VideoChunk。
上下文合同按 Chunk 关联，不使用 `context_before_event_indices` / `context_after_event_indices`。

```text
Chunk N-1 的视频 / 已有解析结果 → context_before
Chunk N 的视频、Timeline、可选增强 → 当前内容
Chunk N+1 的视频 / 已有解析结果 → context_after
```

邻块关系由固定拓扑确定，解析结果从同一次 Parse Job 中装配。
上下文关联通过 Chunk ID 定位；给模型使用时，按任务需要加载相邻视频、Timeline、
已有摘要或 compact 等实际背景内容，不要求统一成一个必填的 `text` 字段。
媒体按 Task / Lease 临时生成，临时路径不写入 Source。
视频首尾没有相应邻块时，上下文为空；任务需要邻块解析结果而邻块尚未完成时，属于任务就绪状态。

以上是 Source 的 Chunk 消费视图。正式事实继续复用第一章的 `persons + timeline`，
本块事件视图由 Projection 派生，不新增一套可以独立修改的事件事实。

#### Video Source Schema 提案

以下给出完整的 Source 视图类型；人物直接使用人名字符串，`TimelineEvent` 沿用第一章定义。
该提案描述 Engine 提供给解析增强与检索流程的消费合同，尚未改动 Python Schema。

```typescript
type VideoSource = {
  video_id: string;

  // 同一次解析中的人名列表，事件内直接填写这些人名
  persons: string[];

  // 按固定视频拓扑顺序排列
  chunks: ChunkSource[];
};

type ChunkSource = {
  // 关联 VideoChunk，读取原视频及固定时间边界
  chunk_id: string;

  // 与当前 Chunk 时间范围相交的事件，保留原事件的绝对时间
  timeline: TimelineEvent[];

  // 当前 Chunk 的可选检索增强；未启用时为 []
  enrichments: SourceEnrichment[];

  // 前一个、后一个 Chunk 的已有内容
  context_before: ChunkContext | null;
  context_after: ChunkContext | null;
};

type ChunkContext = {
  // 相邻 Chunk 的 ID，也用于按需读取相邻视频
  chunk_id: string;

  // 复用该 Chunk 已有的解析结果
  timeline: TimelineEvent[];
  enrichments: SourceEnrichment[];
};

type SourceEnrichment = {
  // 本组 summary、keywords、questions 的输出语言，例如 zh-Hans、ja
  language: string;

  // 对当前 Chunk 的概括；未生成时为 null
  summary: string | null;

  // 便于词项检索的关键词；未生成时为 []
  keywords: string[];

  // 当前 Chunk 能回答的问题；未生成时为 []
  questions: string[];
};
```

- `video_id`、`chunk_id`、Chunk 顺序和邻块关联由系统装配。Engine 定义内容字段与校验，
  Harness 按任务填充人物、事件或启用的增强；模型不生成视频拓扑。
- Chunk 的 `timeline` 从同一份正式 Timeline 按时间交集派生；跨边界事件可出现在多个
  Chunk 视图中，保留完整事件时间，不赋予排他归属。事件内人名与 `VideoSource.persons` 保持一致。
- `context_before/after` 在读取时从同一次 Parse Job 的邻块结果装配，不分别持久化一份
  可以独立修改的邻块内容，也不递归携带邻块自己的上下文。首尾无邻块时为 `null`；
  所需邻块解析未完成由任务状态表达，不伪装成已完成的空结果。
- 视频输入通过固定 Chunk 定位，由 Worker 按需生成临时媒体；Source 不保存临时路径。
  文本 Embedding / 文本重排的输入由 Engine 按策略派生，Source 无独立的必填 `text`。

##### 语言字段的来源与选择

`SourceEnrichment.language` 标记这一组生成文本使用的语言。没有对白的视频也可以生成
中文摘要、关键词和问题，此时填写 `zh-Hans`。它不描述动作、情绪或画面本身的语言，
也不代表整个视频的原声语言。

| 位置 | 语言如何确定 |
| --- | --- |
| `DialogueTextVariant.language` | 根据该条对白文本的实际语言填写；原文与译文分别保留语言和 `role` |
| `OnScreenTextEvent.language` | 根据识别出的画面文字填写；无法可靠判断时为 `null` |
| `SourceEnrichment.language` | Engine 根据检索产品需要，在任务配置中指定目标语言；Harness 按该语言生成本组增强 |
| `TimelineEvent.description` | 按 Engine 指定的解析描述语言生成；当前沿用已有字段，不为每个动作或场景增加语言标签 |

建议初版将解析描述和检索增强统一输出为 `zh-Hans`，并在任务配置中记录选择；
对白原文与画面文字继续保留实际识别出的语言。确有多语言检索需求时，再为同一 Chunk
增加其他语言的 `SourceEnrichment` 条目，不因视频原声包含多种语言而自动复制所有增强。
`summary`、`keywords`、`questions` 遵循该组目标语言；人名、品牌名、代码等可保留原写法。

### 2.1.10 视频多模态精筛的补充调查

本小节记录可供 AniVAG 后续评估的模型能力，不能当成 RAGFlow 普通文本重排已经实现的行为。

- **视觉 Encoder 已有成熟实现。** CLIP / SigLIP 类图文模型支持图文表示匹配；
  BLIP 的图文匹配分支会把图片特征交给文本模型，并通过打分头判断匹配程度。
- **视频也有专用多模态 Reranker。** Qwen3-VL-Reranker 提供 2B / 8B 模型，
  接收 Query 与候选的文本、图片、视频或混合输入，输出相关性分数。
- **我们可以评估的接法**：现有召回与融合 → 有限候选的视频 / 有序帧与文本证据 →
  多模态 Reranker 打分、排序、截取。这个阶段本身就是精筛，无须因命名再增加一轮模型判断。
- 对“说完某句台词之后发生某个动作”这样的查询，应保留画面顺序和时间信息，并明确提供对白文本；
  单张截图不足以完整表达动作顺序，也不能把支持视频输入理解为已经处理了音轨。
- 候选数量、帧数、画面分辨率和文本长度都影响成本；是否采用此路径，要评估我们自己的查询质量与延迟。

来源：[SigLIP 文档](https://huggingface.co/docs/transformers/main/model_doc/siglip)、
[BLIP 图文匹配源码](https://github.com/salesforce/BLIP/blob/main/models/blip_itm.py)、
[Qwen3-VL-Reranker 输入合同](https://github.com/QwenLM/Qwen3-VL-Embedding#model-input-specification)、
[Qwen3-VL-Reranker 打分源码](https://github.com/QwenLM/Qwen3-VL-Embedding/blob/main/src/models/qwen3_vl_reranker.py)。



## 2.2 给我们的启示

本节整理从 RAGFlow 学到的检索设计原则，以及这些原则对视频场景的启发。
具体解析流程、召回组合和第一版实施范围放在 2.3。

### 2.2.1 每种信息都要有明确的检索消费方

RAGFlow 的全文查询区分标题、关键词、生成问题和内容 token，并分别设置权重。
这说明新增解析字段时，应同时说明它怎样加工、由哪种查询消费。

对应到视频：对白服务原话查询，画面文字服务文字线索查询，事件描述服务语义查询，
关键词强化词项匹配，生成问题帮助匹配用户问法。一个字段可以服务多个策略，
但不需要因为它存在就单独建立一个向量。

源码：[字段与查询](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28)。

### 2.2.2 召回表示与重排证据可以不同

RAGFlow 的普通 Python 入库路径在存在生成问题时，可能将问题拼接后作为内容 Embedding
输入；模型重排仍优先使用原始自然文本。

给视频的启示是：生成问题可以帮助找到候选，Timeline 和原视频用来确认候选。
例如“哪个片段中有人说没事后转身擦眼泪？”可以成为召回线索，返回结果时仍应检查
实际对白、动作、人物与时间。生成问题不能补写原始事实中没有的原因或剧情。

源码：[Embedding 选择](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_utils.py:186)、
[重排输入](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:664)。

### 2.2.3 用小线索命中，返回完整证据

RAGFlow 可以用子块命中，再关联更完整的父内容。召回线索的粒度与返回证据的粒度可以不同。

视频中，一句对白、一个动作或一处画面文字都可以成为命中线索；返回时可以展开成对应
Chunk 或包含前后过程的连续片段，同时保留真正命中的时间。
这不要求把前后上下文改成 Event 粒度，也不要求给每个 Event 建立向量点。

源码：[父内容构造](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/chunk_service.py:341)。

### 2.2.4 上下文按用途与预算加入

所查 RAGFlow Python 分块器会在配置启用时，为图片、表格附加邻近文字，并控制上下文数量。
视频中也可以根据任务选择相邻 Chunk 的视频、Timeline 或摘要，控制时间范围与输入长度。

上下文可用于解析消歧、命中后展开和重排，但需要保留内容来源。例如前一个 Chunk 有
奔跑，当前 Chunk 只携带了这段背景，最终应把证据定位到奔跑实际发生的位置。

源码：[上下文处理](/Users/bytedance/workspace/other-project/ragflow/rag/flow/chunker/token_chunker.py:191)。

### 2.2.5 融合与精筛需要结合自己的查询评测

RAGFlow 的融合规则随入口和后端变化，Claim 路径可见 RRF。可借鉴的是为多种检索表示
明确融合与精筛方式；具体采用哪些通道、怎样设权重和候选数，需要用自己的查询评测。
对视频中的人物、先后顺序和跨块条件，还应检查实际事件与视频证据，相关性分数本身
不能证明这些关系成立。

源码：[RAGFlow Claim RRF](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/tools/navigation.py:1529)、
[模型重排](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:664)。

### 2.2.6 词项检索与问题增强是两个维度

语义文本、生成问题和视频向量都是相似度检索表示。语义文本与问题还可能来自相同解析
内容，因此不能仅凭向量种类更多就认为召回能力足够互补。词项检索仍有独立用途：
原话、画面文字、专名、数字等字面线索继续通过分字段全文索引和短语匹配查找。

`questions` 是内容侧的增强结果，可以同时服务两种检索：

```text
Chunk 能回答的问题
  → 分词后参与词项检索
  → 按需生成问题向量，参与语义检索
```

RAGFlow 将 questions 加工成 `question_kwd / question_tks`，并在 Query 中查询问题字段；
普通 Python 入库也可以使用生成问题作为内容 Embedding 输入。
[字段加工](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/tokenizer.py:148)、
[查询字段](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28)、
[Embedding 输入](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_utils.py:186)。

### 2.2.7 查询侧也可以增强检索

以下是在同一本地 RAGFlow 版本中继续核对到的能力。内容侧生成 questions 与查询侧改写
用户问题是不同步骤；后者不要求给 Source 新增字段。

| 能力 | RAGFlow 的实际行为与依据 | 对视频的启发 |
| --- | --- | --- |
| 词项归一、同义词与短语 | [Query](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:42) 做字符归一、同义词扩展和短语组织；[词典](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/synonym.py:79) 优先查自定义词典，再对英文尝试 WordNet | 为已确认人物别名、动作近义表达建立可控扩展；原话查询保留原字面 |
| 多轮问题补全与跨语言查询 | [调用入口](/Users/bytedance/workspace/other-project/ragflow/api/db/services/dialog_service.py:736) 按配置调用 full_question / cross_languages；[实现](/Users/bytedance/workspace/other-project/ragflow/rag/prompts/generator.py:271) 用对话补全问题，并可生成目标语言查询 | 将“她后来呢”补成明确人物与条件；按需要用中日等查询变体覆盖原文词项 |
| 缺失证据驱动的补搜 | [Agentic Graph](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/agentic_rag_graph.py:1562) 根据不足项与已尝试查询生成新查询，再调用 _fanout_search；[模式配置](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/config.py:46) 控制迭代预算 | 已找到台词却缺后续动作时，只针对人物、时间窗口和缺少的动作继续查 |

第一版先完成基础三路检索。上表的查询补全、跨语言改写与迭代补搜按需要后续接入；
RAPTOR 的层级表示与 GraphRAG 的关系检索整理到第三章，作为未来 TODO。

## 2.3 我们的设计

2026-10-17 最终增补：本节的 Chunk 三路检索保留，另增加独立 Event 池的类型/speaker 过滤、词法、文本向量与抽帧视觉向量检索。
Engine 定义纯投影、编码/融合及事实核验合同；Server 执行租约、ES IO、授权范围及既有 MySQL 关系批量回源，不新增 Event / Frame SQL 表。
只投影与 canonical Chunk 相交的条目，保留完整事件时间与原数组下标；新工具时间单位为整数毫秒。
每 Event 一条 text vector 与一条基于 capture 的 vision vector，不生成 VLM caption；采样参数首个 INDEX 冻结到任务 detail，不改变 ParseConfig。
具体字段、索引完成判定、回填及版本边界统一见 [Event 最终 TRD](../../../server/design/sub-chunk-trd.md)，不在本节维护第二份定义。

以下是基于已确认 Source 结构的解析与召回提案，尚未实现。RAGFlow 的具体行为以 2.1
的本地源码调查为准；这里对 AniVAG 的选择不代表 RAGFlow 所有路径都采用相同策略。

已确定统一使用 Elasticsearch：MySQL 保存正式解析结果、Chunk 基础解析状态、Interval Refine / 索引状态及主任务汇总；Elasticsearch
承接词项检索、Chunk 文本语义向量与视频向量检索。Qdrant 从目标架构中移除。
当前运行代码仍使用 Qdrant，替换尚未实现；Engine / Server 的接入边界见 2.3.11。
本节原有的“基础三路”指 BM25 词项、Chunk 文字综合语义 Dense 和 Chunk 视频 Dense：
Dense 内含文字与视频两条独立向量路。可选的文本 Sparse 与截图查询另见 2.3.12。

### 2.3.1 怎样得到 Source

| 工作 | 输入与处理 | 产出 |
| --- | --- | --- |
| 准备 | 读取固定 Chunk 拓扑，按任务需要生成临时视频、音频或有序帧 | 已知的 Chunk ID、时间边界与模型证据 |
| 单块理解 | 综合画面、音轨，以及按需取得的 ASR / OCR 候选；理解场景、动作、对白、情绪、人物关系和非对白画面文字 | 人物与 Timeline；同一对白的 ASR / 字幕证据汇成语言变体 |
| 邻域纠偏与前缀背景 | 沿用已规划的 Interval Refine / PrefixContextAgent，读取邻块已完成结果及必要视频，校正人物名称、指代和跨边界事件 | 同一次解析中的一致结果；当前事件仍需对应实际证据 |
| 检索增强 | 从通过校验的结果生成启用的摘要、关键词、问题；目标语言由 Engine 指定 | `SourceEnrichment[]`；增强受已有事实约束 |
| 装配与索引 | 从本次任务的解析成果装配 Chunk 视图和邻块上下文，再按策略构造检索表示 | 可追溯到解析任务、内容版本、Chunk 和事件时间的索引输入 |

初次单块理解不等待邻块的最终增强，避免相邻任务互相等待；Refine 在所需基础结果就绪后执行。
上下文在使用时装配，不要求每个解析请求一次性生成整个 `VideoSource`。
事件中的人名与本次 Video 的 `persons` 保持一致，模型输出先经过 Engine 的 Schema 与领域校验。
这些工作复用同一 Harness；不要求每一种事件类型或每一个增强字段各发一次模型请求。

### 2.3.2 Schema 字段怎样产生检索表示

| 检索方向 | Source / 业务输入 | 索引与查询策略 |
| --- | --- | --- |
| 原话、画面文字、专名 | Dialogue 的 `text_variants`、OnScreenText 的 `text`、事件中的人名、keywords | 保留原文与语言，生成分词 / 全文字段；短语匹配服务原话，BM25 等词项排序服务关键词。生成问题中的用词不冒充原台词命中 |
| 事件与语义 | 按时间组织的 `timeline.description`、必要对白、summary | Engine 构造文本编码输入，由 Gateway 调用编码模型；Query 用兼容的编码策略查询，召回动作、场景、情绪和剧情描述 |
| 画面与镜头 | `chunk_id` 定位的原视频 / 有序帧 | 视觉或视频 Embedding；文本 Query 使用与视频向量对齐的编码器，模型、采样与版本须匹配 |
| 问法增强 | `questions` | 先参与词项检索；有评测收益时再增加独立问题向量，基础事实向量继续保留。问题命中后回到真实事件与视频验证 |
| 条件过滤 | 可信的视频归属、解析任务及内容版本，以及事件类型、人物名称、语言、时间 | 先限定允许搜索的集合；用户明确条件与模型推测分开处理，不确定推测用于扩大候选或加分 |
| 时间关系 | 事件起止时间、说话人、参与者、可见人物 | 将多个条件的候选按视频、人物和时间关系关联；顺序、重叠、间隔分别检查，必要时展开相邻 Chunk 验证 |

第一版以 Chunk 作为候选归并单位，事件时间保留在命中证据中；词项或事件命中可以返回
更完整的 Chunk / 连续片段。不要求每个字段或每个 TimelineEvent 都建立单独的向量点。
语义输入直接使用事件中已有的人名。长内容按模型预算组织，保留检索具体细节所需
的事实；关键词与问题可有独立全文字段和权重，Source 无须增加必填 `text`。

`context_before/after` 优先用于解析消歧、候选展开和重排。默认 Chunk 语义输入以本块
事实为主体；若策略加入邻块摘要，必须标注来源，并回查真正发生的位置。
不能因为前块出现奔跑，就把只因携带前块背景而命中的本块标记成奔跑发生处。

### 2.3.3 查询侧执行顺序

```text
Query + 可信搜索范围
  → 提取原话、语义描述、视觉条件、明确筛选条件与时间关系
  → 词项召回 / 文本向量召回 / 视频向量召回（按需求启用）
  → 各路先按 Chunk 归并，再执行现有加权 RRF
  → 需要时在指定时间窗口内展开邻块、关联多事件条件
  → 可选重排：启用后按问题选择文本或多模态输入
  → 校验原话、人物与时间约束，返回 Chunk 和实际证据时间
```

明确引用的台词保留原始查询字面；查询重写可以用于语义通道，不覆盖精确匹配条件。
多事件查询可分别召回各子条件，再检查同一视频内的关联；不依赖一个向量编码所有顺序约束。
例如“她说完‘没事’后转身擦眼泪”，先找对白，再在局部 Timeline / 邻块中检查动作、
人物与时间顺序；只命中对白的候选不能被当成满足整句查询。

同一通道的多个问题、语言版本或细分命中先归并，第一版可用最佳排名形成 Chunk 排名，
避免同一 Chunk 因增强条目更多而获得额外 RRF 票数。人名按视频 / 作品范围解释，跨视频关联须有依据。
Reranker 消费可读的实际事件、对白或视频证据；生成问题只作为召回线索。
明确过滤条件和权限约束持续生效，模型分数不能覆盖它们；RRF 分数也不视为相关性概率。

初始融合基线继续复用 AniVAG 已有的三路加权 RRF；权重和召回数通过视频查询评测调整。
源码：[现有融合](/Users/bytedance/workspace/other-project/AniVAG/engine/retrieval/video_retrieval.py:172)。

### 2.3.4 第一版范围与成本控制

- 基线：同一正式 Timeline 派生分字段词项与文本向量，沿用现有视频编码合同；三路索引
  统一写入 Elasticsearch，支持明确条件过滤、Chunk 归并、加权 RRF、原话与证据时间校验。
  具体加工步骤见 2.3.10。
- 增强：消费 Harness 已产出的摘要、关键词和问题；questions 先进入词项字段。
  独立问题向量、查询改写、复杂补搜与新模型重排按收益后续接入，不作为基础三路的前置条件。
  RAPTOR 与 GraphRAG 见第三章 TODO；逐事件向量和更多语言表示由实际漏召回需求驱动。
- 对本次任务已持久化的解析成果构建索引。上游事实改变时，检查相关增强是否失效，并重建
  受影响表示；视频、时间范围和编码合同未变时复用视觉向量。索引执行与重试由 Interval 任务状态管理，主任务汇总。
- 组合生成增强可复用一次模型请求；先做有预算的候选召回，再对有限候选重排。
  候选数、采样、语言数和字段生成数量都纳入配置与已有 Gateway 用量统计。
- 以原话、画面、动作 / 情绪、跨块时间关系、多语言、无匹配问题构建人工标注集，
  分别观察召回覆盖、最终结果质量、时间定位、空结果行为、成本与延迟。
  新策略逐项对比基线，避免无法判断是哪项变化带来收益。

### 2.3.5 具体向量清单与索引输入

第一版默认构建两类向量：Chunk 语义向量与视频向量；问题向量是可选增量。
下表是 Engine 从 Source 派生的索引表示，不向已确认的 Source 增加必填文本字段。

| 表示 | 具体输入 | 默认数量与消费方 |
| --- | --- | --- |
| `semantic_vector` | 本块事件按时间组织的 description、事件中的人名、对白语言变体、相关画面文字，以及启用的本块 summary | 有内容时通常每 Chunk 一个，供文本语义 ANN 召回 |
| `vision_vector` | 固定 Chunk 时间范围内的实际视频，按固定编码合同采样 | 每个可编码 Chunk 一个，供跨模态 ANN 召回 |
| `question_vector` | 同一语言的非空 questions 用换行拼接 | 默认不启用；启用时每 Chunk / 语言组一个，供问法召回 |
| 词项字段 | 对白、画面文字、人物名、事件描述、keywords、summary、questions 分字段加工 | 不另做独立 Dense 向量，供短语 / 全文排序；保留原文与命中位置 |
| 过滤与定位字段 | 可信业务范围、解析任务及内容版本、Chunk、事件类型、人物名称、语言及时间 | 用于过滤、关联和证据定位，不做 Embedding |

语义向量的渲染规则固定在 Projection 版本中：按时间组织事实，直接使用事件中的人名，
摘要可作为概括附加；对白与画面文字保留可核验内容。邻块内容默认用于理解、展开和重排，
不无界拼入本块向量。编码器长度不够时，按连续时间段拆分派生表示，仍关联同一 Chunk；
不静默截去尾部事实，不改变固定 Chunk 拓扑或 Chunk 粒度上下文。
所有表示携带来源解析任务、内容版本与 Chunk 关联；同一候选的各路表示和事实回查须保持来源一致。

Query 使用与各索引兼容的查询编码器：语义和问题路使用其文本编码合同，视觉路使用与
视频向量对齐的文本编码合同。不同向量空间分别检索，后续融合排名。
已有视频编码调用见 [Gateway](/Users/bytedance/workspace/other-project/AniVAG/gateway/ark/embedding.py:40)。

### 2.3.6 多路候选、融合与防漏规则

默认让词项、语义、视觉三路独立检索，使用相同的可信范围和解析任务约束；普通词项命中
不成为语义 / 视觉路的强制前提。查询规划主要调整预算和权重，避免一次意图判断就关闭
可能有用的通道。明确的原话要求保留原字面，并在返回前验证实际对白或画面文字。

以下数字是便于启动评测的参数建议，尚无 AniVAG 评测结论，也不是 RAGFlow 的默认配置；
重排与视频精读参数仅在相应能力启用后生效：

| 参数 | 起步建议 |
| --- | --- |
| 基础三路候选数 | 每路 Top 50 个不同 Chunk |
| 问题路候选数 | 启用时 Top 20 个不同 Chunk |
| RRF 常数 | 60，基础三路先用等权重 |
| 重排候选上限 | 60 个不同 Chunk |
| 各路保留名额 | 每个启用通道先保留前 5 个，去重后按 RRF 补足候选上限 |
| 视频精读预算 | 从短名单中按视觉 / 时序需求分配，单独限制候选数与采样量 |

分段表示、多语言或多个问题在同一通道内先按 Chunk 归并，再产生一份排名。
这里的候选数按不同 Chunk 计数；后端先取表示条目，再在扫描预算内补取和归并，
不能把 50 个向量点直接当成 50 个不同 Chunk。
初版可按该 Chunk 最佳命中排名归并，保留全部命中线索；表示条目更多不额外增加 RRF 票数。
启用问题路时单独评测其权重与成本，不因其与语义路同源就视为独立事实佐证。

```text
RRF(chunk) = Σ channel_weight / (60 + rank_in_channel)
candidate_pool = union(lexical_hits, semantic_hits, visual_hits, optional_question_hits)
rerank_pool = 每路保留名额的并集，再按 RRF 补足剩余名额
```

候选并集用于覆盖互补命中；RRF 用于整合不同分数量纲与多路排名；保留名额用于防止
某一路独有的高排名候选在精筛前被挤掉。固定 Top K 下的最终召回仍可能下降，需要评测。
重排只改变给定候选的排序；它无法找回未进入候选池的内容。

多条件查询分别检索各条件，再在指定视频和时间窗口内关联。邻块展开使用同一解析任务及
相符的内容版本，人物与时间条件由 Timeline 检查，信息不足时按预算读取原视频验证。
候选不足时允许一次有预算的补召回，例如扩大单路候选数或补充语义改写；权限、用户明确
搜索范围和原话条件持续生效。仍不满足条件时返回空结果，不能仅因相似而当作匹配。

### 2.3.7 RAGFlow 依据与视频扩展边界

本轮依据本地 RAGFlow commit `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
源码可以证明机制存在；对 AniVAG 视频查询的效果需要由本项目的评测证明。

| 设计点 | RAGFlow 中的直接依据 | 我们的选择 |
| --- | --- | --- |
| 全文与向量并存，字段有不同用途 | [Tokenizer](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/tokenizer.py:140) 将 keywords / questions / summary 加工成检索字段；[Query](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28) 配置字段权重 | 对白、画面文字、事件和增强分字段加工，语义向量由视频事实构造 |
| 问法参与 Embedding | [内容选择](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_utils.py:186) 优先拼接 question_kwd；[执行调用](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/embedding_service.py:77) 实际编码 | 基础语义向量保留，问题向量可选；这是对视频的设计选择 |
| 独立多路与 RRF | [Claim 召回](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/tools/navigation.py:1617) 分别查询 Dense / 全文；[RRF](/Users/bytedance/workspace/other-project/ragflow/rag/advanced_rag/harness/tools/navigation.py:1529) 按排名融合 | 扩展到词项 / 语义 / 视觉；三路预算与保留名额属于我们的方案 |
| 候选重排 | [rerank_by_model](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:664) 用自然文本打分；[retrieval](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:710) 限定候选窗口，默认 64 | 文本问题消费实际事件与对白；视觉问题按预算使用视频证据 |
| 命中后展开证据 | [retrieval_by_children](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:1085) 按 mom_id 读取父内容；[上下文](/Users/bytedance/workspace/other-project/ragflow/rag/flow/chunker/token_chunker.py:191) 控制邻近文本预算 | 展开 Chunk / 相邻片段，同时保留真正命中时间 |
| 空结果补召回 | [search](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:337) 的空结果分支尝试调整词项条件或 Dense-only 查询 | 采用有上限的补召回，保留所有必须满足的条件 |
| 视频向量、人物时序校验 | 上述 RAGFlow 普通文本链路没有提供对应的视频实现依据 | 视频向量复用 AniVAG 已有 Gateway，人物与时序校验是本项目的扩展 |

普通 ES 混合路径还会把全文条件放进 KNN filter，不能概括为所有 RAGFlow 检索都做
独立两路加 RRF；参见 [ES 查询组装](/Users/bytedance/workspace/other-project/ragflow/rag/utils/es_conn.py:244)。

### 2.3.8 怎样验证召回收益

先固定人工标注的真实查询、相关片段和时间命中判据，按原话、动作 / 情绪、画面、跨块
时序、多语言、无结果问题分组。分别记录：

- 候选并集的 Recall@K，以及每一路独有的相关命中。
- 候选预算截取后的 Recall，判断融合和精筛配额是否丢掉有效结果。
- 最终 Precision@5 / MRR、时间定位准确性与无结果问题的误报情况。
- 查询耗时、索引成本、每次查询的模型调用和用量。

在固定候选与成本预算下逐项对比：语义基线 → 加词项 → 加视觉 → 加问题增强 →
加邻块与时序处理 → 重排。区分“解析没记录”“检索没找到”“融合截掉”“重排排错”，
据此决定修改解析、表示、召回预算还是排序。问题向量等增量只有在收益值得成本时才默认启用。

### 2.3.9 新增增强的接入顺序

1. **第一版先完善词项路。** 对白、画面文字、人物名、事件描述及增强分字段检索；统一
   索引与查询的字符归一、分词规则，原话短语匹配保留独立判据。已有 questions 参与词项
   检索，不必等待问题向量启用；已有且可信的别名可以受控使用。
2. **后续按需增强用户查询。** 多轮指代不清时补全问题，跨语言词项匹配不足时生成目标语言
   查询变体；保留原始查询。多个变体仍在所属通道内按 Chunk 归并，共享查询预算。
3. **后续对复杂问题按缺失条件补搜。** 从首轮候选明确缺少哪个人物、动作或时间条件，再做一次
   有预算的查询或邻块视频检查；无新证据、达到预算或条件已满足时停止。

基础词项 + 语义 + 视觉三路先形成可评测的完整流程。其余增强分别评估增量收益和成本。

### 2.3.10 Engine 收到 Harness 结果后的基础加工流程

本节是拟议处理合同，尚未实现。Engine 输入是通过校验的正式解析结果、Chunk Source
消费视图，以及宿主提供的固定 Chunk 拓扑、解析任务及内容版本和编码配置。
其中业务标识与版本由宿主注入，人物、事件及启用的增强由 Harness 按 Engine Schema 填充。

```text
Harness：Interval Refine 提交有效 Chunk 成果（人物 + Timeline + 已启用的增强）
  → 宿主：逐 Chunk 确认所有包含它的 Interval 均已提交 Refine，再读取该 Chunk 当前有效成果及内容版本
  → Engine：校验结果，按固定拓扑装配当前 Chunk 视图
  → Engine：提取分字段词项记录、构造语义编码文本、声明视频编码输入
  → 宿主：经 Gateway 编码，经存储适配器写入词项 / 向量索引
  → 宿主：确认本 Interval 负责的有效 Chunk 当前版本的必需表示全部就绪，将 Interval 从 INDEXING 更新为 COMPLETE
  → 宿主：汇总各 Chunk 与必需 Interval，更新 Video 主任务状态
```

单 Chunk Understanding 的完成只提供基础解析结果；上面的索引加工在 Interval 上下文 Refine
之后执行。Interval 的成员范围为 `[s, e)`：`s = start_sequence_no`，`e = s + chunk_count`，
所有 `s <= sequence_no < e` 的成员共同参与 Refine，不定义中心 Chunk。`e` 由已有字段计算，
不新增表字段。设 `n` 为本 Video 的 Chunk 数、`k = interval_size`：`n >= k` 时创建完整滑窗，
`0 < n < k` 时创建唯一短窗 `[0, n)`，仍执行 Refine 后再索引。

索引逐 Chunk 放行：同一解析任务中，所有包含该 Chunk 的 Interval 都处于 `INDEXING`、
`INDEXING_FAILED` 或 `COMPLETE`，才表示其所需 Refine 均已提交；不等待这些 Interval 全部
`COMPLETE`。一个 Interval 可以先处理已放行的部分成员，余下成员等待相关 Refine 提交。
等待不算失败、不消耗重试次数，也不持有执行租约。Refine 提交后宿主通知主协调器，DB Queue
扫描并领取已满足放行条件的索引工作，不增加 bitmap 或另一套 callback。

重叠 Interval 的基础成果就绪后即可执行和提交，不按 `start_sequence_no` 排队，也不因其他
窗口写入而重跑模型。每次提交只把本次实际修改的字段应用到当前有效 Chunk 内容；同字段
采用后提交的值，未修改字段保留当前值，不能以旧窗口快照整份覆盖当前内容。

宿主在 `chunk_parse_content.detail.scope` 保存已参与上下文处理的 Chunk 序号列表：初始为
`[i]`，每次 Interval Refine 提交时，对每个成员原子合并当前窗口的全部序号，保持有序、去重
及单调增加。它仅用于提示 Agent 已参考的范围，不保存 Interval ID，不作为 Refine 或索引
放行条件；scope 增长本身不触发 Refine 重算或重建索引。范围、写回和放行合同见
[Agent TRD](../../../agent/video_parse_v2/design/trd.md#interval-区间scope-与索引放行)。

#### 第一步：校验并固定输入快照

依照正式 Schema 设计校验时间顺序、事件类型和人名一致性，再核对视频范围、固定 Chunk
关联与 Source 所属解析任务及内容版本。索引从当前有效 Chunk 内容读取，不使用旧 Interval
快照覆盖或代替它。一次索引执行固定所读成果及其版本；Refine / Prefix
后续修改事实时，重新读取并更新受影响表示。回查发现内容版本不一致时丢弃过期候选并触发索引更新，
不能将旧的事件下标套到新的 Timeline；不为此另设不可变 build 快照表。

来源回查使用具体的索引定位字段，不新增含义未定义的 `source_ref` 字段：

- 外层 `ChunkIndexInput.scope` 携带解析任务、内容版本及 Chunk 关联，每条词项记录复用该作用域；
  它与上面的 `chunk_parse_content.detail.scope` 序号列表是不同合同。
- 从事件提取的词项记录携带 `timeline_index`：它是对应内容版本中，完整正式 Timeline
  数组的下标，从 0 开始。它不是当前 Chunk 视图内重新编号的下标。
- 对白的 `language + role` 进一步定位该事件中的语言变体；其他字段通过记录的 field 定位。
- `timeline_index` 由 Engine 在投影时生成，Harness 只填写正式内容。同一来源事件进入多个
  Chunk 视图时仍指向同一个正式条目；新内容版本内的下标只能与该版本一起使用。
- Chunk 增强通过外层 Chunk 作用域和增强条目位置回查，不伪装成 Timeline 事件。

例如，下列是 `ChunkIndexInput.lexical_fields` 中的一条对白词项记录，外层作用域已确定
解析任务、内容版本与 Chunk。一个 Chunk 还可以有其他对白、事件描述、问题等词项记录，以及独立
生成的语义 / 视频编码输入；这条示例只展示其中一个局部产物。

```json
{
  "field": "dialogue",
  "raw_text": "我没事。",
  "language": "zh-Hans",
  "role": "ORIGINAL",
  "speaker": "千早爱音",
  "start_seconds": 240,
  "end_seconds": 243,
  "timeline_index": 17
}
```

该记录回查对应内容版本的 `timeline[17]`，再按 language / role 取对白变体。
这里的 17 仅为数组下标示例，属于索引派生信息；原事件的内容与绝对时间保持不变。

#### 第二步：按含义提取词项字段

以下名称是拟议的逻辑索引字段；它们由现有 Source 派生，不是新的模型输出字段或业务表。

| 逻辑字段 | 从哪里取 | 怎么加工、怎样命中 |
| --- | --- | --- |
| `dialogue` | DIALOGUE.text_variants | 每条变体保留原文、language、ORIGINAL / TRANSLATION、说话人及事件引用；支持对白全文与原话短语匹配 |
| `screen_text` | ON_SCREEN_TEXT.text | 保留原文、language 及事件引用；支持画面文字全文与短语匹配，和对白分别检索 |
| `person_names` | 当前 Chunk 的 speaker、participants、visible_persons 中直接填写的人名 | 去重并构造专名匹配值及分词表示；保留所在事件，不把整部视频所有人物放进每个 Chunk |
| `event_description` | 当前 Chunk 的 timeline.description | 按事件保留描述、类型、人物和时间引用；分词后参与全文排序 |
| `keywords` | 每组 enrichments.keywords | 按组保留语言，去空与去重，生成词项 / 全文表示 |
| `questions` | 每组 enrichments.questions | 按组保留语言和问题边界，单独参与全文排序；命中表示该块可能回答问题 |
| `summary` | 每组非空 enrichments.summary | 按组保留语言，作为补充全文字段；不覆盖对白、画面文字或事件细节 |

每条文本保留原始值，另有按合同归一的匹配表示。索引与查询使用同版本的语言分词与归一
规则，专名、数字不随意丢弃。分词器可由索引后端执行，Engine 定义可观察的匹配合同。
普通全文允许词项匹配与字段加权；原话判据不使用近义词替换，并明确严格匹配的归一规则。
对白与画面文字沿用各自语言，description 使用本次解析配置的描述语言，增强使用每组 language。

对白、画面文字和事件描述有事件级来源时间；增强只有 Chunk 级来源时，返回 Chunk 范围，
不凭关键词或生成问题伪造一句原话及其发生秒数。事件时间也不冒充模型未提供的逐词时间。

例如对白变体为“我没事”，questions 中另有“她说我没事之后做了什么？”：两者分别进入
dialogue 与 questions。“说了‘我没事’”只能由对应对白证据满足；译文命中需标记译文，
要求原声原话时限定 ORIGINAL。不同说话人的独立事件不因拼接字段而形成一句虚假的原话；
第一版按单条对白变体验证，后续跨句匹配需显式定义连续性规则。

#### 第三步：派生编码输入

- **语义输入**：按事件时间组织 description、事件中的人名、对白及画面文字；启用的
  summary 可补充概括。Engine 用确定的渲染规则构造输入，Gateway 执行 Embedding。
  这一步复用已有事实，不为每个字段再发一次理解请求。
- **视频输入**：声明固定 Chunk 及采样 / 编码合同。宿主从原视频生成 Task / Lease 隔离的
  临时媒体交给 Gateway；临时路径不进入 Source、持久化索引合同或 Checkpoint。
- **问题增强**：第一版消费已有 questions 做词项检索；问题向量默认关闭，后续独立评测。

Engine 的逻辑产物可概括为一个 `ChunkIndexInput`：

```text
ChunkIndexInput（派生产物，名称为提案）
  scope               可信视频 / Chunk / 解析任务及内容版本
  lexical_fields      上表各字段的文本、语言、类型和 timeline_index / 增强条目位置
  semantic_inputs     待编码文本及来源事件下标，按模型长度预算组织
  vision_input        固定 Chunk 关联与采样 / 编码合同
```

##### 一条对白记录与各路检索的关系

各路表示从同一 Chunk 的事实、增强和原视频分别派生。上面的 dialogue JSON 是词项路
中的一条文本记录；语义编码也可以使用其中的对白内容，但无须以该 JSON 作为中间输入。
Engine 直接从同一 Source 提取各路所需数据，保持版本一致。

| 能力 | Engine 怎样准备输入 | 这条对白记录在其中的作用 |
| --- | --- | --- |
| 文本直接检索 | 按 dialogue、screen_text、event_description、person_names、keywords、questions、summary 分字段建立词项表示 | raw_text 进入对白全文 / 短语匹配；speaker 可用于人名查询或条件检查，其他来源信息用于定位 |
| 文本语义向量 | 把本块 Timeline 的事件描述、人名、对白、画面文字及启用的摘要组织成编码文本 | “千早爱音说：我没事”可以成为编码文本的一部分，再与同块其他事实一起编码 |
| 视觉 / 视频向量 | 根据 chunk_id 定位实际视频，按固定采样与编码合同准备媒体 | 使用同一 Chunk 的原视频；媒体输入与该对白记录通过 Chunk 及版本关联 |
| question 增强 | Harness 按 Engine 合同，基于本块事实生成该块能够回答的问题，写入 enrichments.questions | 可以围绕这句对白及相关事件生成问题；第一版把问题写入 questions 全文字段，独立问题向量后续可选 |

例如，假设同块 timeline[17] 是“千早爱音说：我没事”，timeline[18] 是“千早爱音转身擦眼泪”：

```text
词项输入：
  dialogue = 我没事
  event_description = 千早爱音转身擦眼泪
  questions = 千早爱音说“我没事”后做了什么？（启用增强时）

语义编码输入：
  千早爱音说“我没事”，随后转身擦眼泪。

视频编码输入：
  当前固定 Chunk 对应的实际视频
```

生成问题提供额外问法，不增加新的事实。它和普通词项内容一起服务第一版的词项通道，
问题向量默认关闭；这仍然是词项、语义、视频三路检索。

`timeline_index` 用于命中后的来源回查，不参与 BM25 词项或向量编码。语义输入覆盖多个
事件时可以保留多个来源下标，但这些下标只说明输入来源，不能证明相似度由某条事件贡献。
视频向量关联 Chunk 及媒体范围；Chunk 级问题关联增强条目，不强制填写某个单一事件下标。
三路候选在同一解析任务内按 Chunk 及内容版本归并和融合，再按需要回查事件或展开邻块。

当前 Chunk 的 before / after 继续以 Chunk 关联，优先用于消歧和命中后展开；基础索引
从本块事实派生，邻块中的事件不会仅因出现在背景里就变成本块的原话或动作证据。

#### 第四步：写入索引并在查询时消费

Engine 负责字段提取、编码输入、查询规则、证据校验及融合；Harness 负责模型任务编排，
Gateway 负责模型调用，Server Service / Worker 组织成果持久化及索引任务执行，存储适配器执行索引读写。
BM25 等全文排序需要词项后端实际建立倒排索引及相应统计；只把字符串写入 JSON 或向量
payload 并不会自动获得这些能力。词项后端已选择 Elasticsearch，由 ES 适配器落实分字段
查询、过滤、语言分析器和短语候选能力；逻辑检索字段无需各自增加一张 MySQL 表。

查询侧由 Engine 根据用户查询与可信范围形成各路输入：词项路查询上述字段，语义 / 视频
路使用兼容的 Query 编码器。三路返回带来源的候选，Engine 按 Chunk 去重并融合排名。
原话等硬条件在进入最终短名单前校验；普通全文命中和生成问题不能代替对白证据。
读取来源、约束校验和返回结果使用相同的解析任务及内容版本，不混用不同来源的成果。

现有 V1 的 [台词匹配](/Users/bytedance/workspace/other-project/AniVAG/engine/retrieval/video_retrieval.py:244)
对已读取的 ASR 文本做匹配；[服务编排](/Users/bytedance/workspace/other-project/AniVAG/server/service/video_retrieval.py:143)
调用它并检索文本 / 视频向量。上述分字段词项索引属于待补齐能力，不能把现有三路称为
已经实现了这里的 V2 全文方案。

RAGFlow 可直接学习的依据是 [Tokenizer 的字段加工](/Users/bytedance/workspace/other-project/ragflow/rag/flow/tokenizer/tokenizer.py:148)
与 [Query 的分字段权重](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28)。
具体到视频的对白角色、事件来源时间和视频编码输入，是 AniVAG 的设计。

### 2.3.11 Elasticsearch 统一检索：Engine 与存储适配器的职责

以下是已选的 MySQL + Elasticsearch 接入设计，尚未实现或部署：

| 层 / 组件 | 职责 |
| --- | --- |
| Parse Harness | 按 Engine Schema 填写人物名称、Timeline 与启用的增强 |
| Engine | 定义 Source 与派生索引内容；决定词项字段、字段权重、原话条件、语言处理合同；构造文本 / 视频编码输入；按 Chunk 归并、融合排名并校验证据 |
| Gateway | 执行文本、视频及 Query 的模型编码，沿用统一用量、超时与取消管理 |
| Server Service / Worker | 读取可信范围和解析成果，组织索引、模型调用、三路查询与结果回查；按 Interval 任务维护 Refine 后的索引执行与重试，并汇总主任务状态 |
| ES 存储适配器 | 管理 ES 客户端、连接与索引；保存词项字段、Chunk 文本语义向量和视频向量；把 Engine 的要求落实为 mapping、analyzer、全文与 KNN 查询，执行读写并转换响应 |
| MySQL Repository | 保存正式解析成果、内容版本及主任务 / 解析子任务状态 |

Engine 定义三路检索合同与策略。ES SDK、连接参数、索引物理名称、网络读写及响应解析，
由 Server 的 ES 适配器承载。语言和匹配语义由 Engine 的检索合同确定，适配器负责落实
对应配置；索引与查询共用相容的配置版本。Harness 继续消费领域 Schema。

#### 任务阶段与失败重试（2026-09-28）

状态机以 [Agent TRD 的解析任务状态机](../../../agent/video_parse_v2/design/trd.md#解析任务状态机) 为准：
`chunk_parse_task` 执行 `QUEUED → UNDERSTANDING → COMPLETE`，只负责基础解析；
`interval_parse_task` 执行 `WAITING → REFINING → INDEXING → COMPLETE`，负责上下文纠偏及有效 Chunk 的索引；
`video_parse_task` 汇总 `PENDING / PREPARING / PARSING / INDEXING / COMPLETE` 及失败状态。
主任务仍在 `PARSING` 时，Interval 可以索引已满足关联 Refine 提交条件的 Chunk；主任务 `INDEXING`
只表示必需解析均完成、仅剩索引工作。
不维护百分比 `progress`，也不等待整集解析结束后才统一发起索引。

Chunk 基础解析成果与其 `COMPLETE`、Interval ready bitmap 同事务提交。Interval 成员的基础
成果就绪后即可读取上下文并执行 Refine，不按窗口起点排队，不因其他窗口提交而重算。
Refine 提交只写本次实际修改字段，同字段后提交值生效；未修改字段保留当前值。
正式成果、成员 `detail.scope` 的有序原子集合并与自身 `INDEXING` 同事务提交。

随后逐 Chunk 检查本任务中所有包含它的 Interval 是否均已提交 Refine（状态为 `INDEXING`、
`INDEXING_FAILED` 或 `COMPLETE`），满足后读取当前有效 Chunk 内容和版本，执行 Projection、
向量生成或复用、ES 写入及查询就绪检查。不要求相关 Interval 的索引先完成。
同一 Interval 可以先处理部分就绪成员；等待剩余成员时保持 `INDEXING`，不持租约、不记失败、
不消耗重试次数。复用 Refine 提交后的宿主通知和数据库扫描重新检查，不新增 bitmap
或 callback。就绪 Chunk 可以组成 embedding / ES bulk 小批次，逐项检查结果；任务的索引成功
或失败记录在 Interval 上。

Interval 按 `[s, e)` 处理全部成员，不定义中心 Chunk。`n >= interval_size` 时创建完整滑窗，
首尾 Chunk 均被覆盖；`0 < n < interval_size` 时创建唯一 `[0, n)` 短窗，同样执行 Refine 和索引。
`detail.scope` 仅提示 Agent 已参考哪些 Chunk，不控制上述就绪条件，增长本身不触发重算或重建索引。

Chunk 基础解析失败记为 `FAILED`；Interval 的 Refine 失败记为 `PARSING_FAILED`，索引失败记为
`INDEXING_FAILED`。重试按子任务的失败阶段恢复，不能仅凭 Video 汇总状态选择恢复入口。
索引重试重新检查关联 Refine 提交条件，并读取当前有效 Chunk 成果及内容版本，不读取旧 Interval
快照作为写入来源，也不重跑 Understanding / Refine；任务 ID、解析版本及
配置快照保持不变，ES 部分成功不回滚 MySQL 成果。

目标设计删除 `es_index_build`、`es_index_build_chunk`、`es_video_publication` 三张 MySQL 表。
索引状态及重试入口归入已有 `interval_parse_task`，`video_parse_task` 负责汇总；
重建来源是既有解析成果表，检索文档保存在 ES；
不增加逐 Chunk 的“已写入 ES”布尔字段或另一张替代发布表。表结构边界见
[Agent TRD](../../../agent/video_parse_v2/design/trd.md#索引持久化边界)。

#### ES 中保存哪些检索表示

基础情况下，同一 Chunk 的词项内容与两类向量保存在同一索引文档中，携带来源
`video_parse_task_id`、Chunk 标识及内容版本。MySQL 中的正式结果是重建来源，ES 保存可重建的检索投影。

| ES 字段内容 | 查询消费 |
| --- | --- |
| 对白、画面文字、事件描述、摘要、keywords、questions | 按字段分析与全文排序；原话走对应字段的短语候选，再回查正式证据 |
| `semantic_vector` | 使用与文本编码合同相容的 Query 向量执行 KNN |
| `vision_vector` | 使用与视频编码合同对齐的 Query 向量执行 KNN |
| 可信业务范围、版本、Chunk、人名、语言与时间 | 范围过滤、候选关联和证据定位；人名直接使用名称 |

两类向量各自配置维度与相似度；不能混成同一向量空间。需要分段表示时沿用 2.3.5 的
规则，仍按 Chunk 归并。questions 默认只参与词项路，独立问题向量仍关闭。

三路查询由 ES 执行，返回各自排名，再由 Engine 复用现有加权 RRF 融合。第一版不依赖
ES 内置融合功能；ES 版本、语言分析器和向量 mapping 在适配器实现时固定并验证。

#### 现有代码的替换落点

当前运行路径仍使用 Qdrant，后续沿现有职责边界替换：

- [Engine 检索算法](/Users/bytedance/workspace/other-project/AniVAG/engine/retrieval/video_retrieval.py:97)
  已有文本投影与 RRF；V2 的词项投影、查询策略在 Engine 的检索侧增加。
- [Server 检索服务](/Users/bytedance/workspace/other-project/AniVAG/server/service/video_retrieval.py:45)
  已组织 MySQL、Query 编码和 Qdrant 查询；替换后从 ES 获取三路候选，交回 Engine 融合。
- [现有存储适配器](/Users/bytedance/workspace/other-project/AniVAG/server/storage/qdrant.py:72)
  的向量读写由拟新增的 `server/storage/elasticsearch.py` 接替，并增加词项检索；新文件尚未创建。
- [依赖装配](/Users/bytedance/workspace/other-project/AniVAG/server/bootstrap.py:124)
  改为创建 ES 客户端并注入 Service；配置、CLI、部署、评测入口与相关测试同步替换 Qdrant 依赖。

索引同步、重试、对账和删除清理由 Server 继续负责，改为面向 ES 执行。迁移时优先复用
输入与编码合同相容的已有向量，避免仅因更换存储就重新调用模型。

第一版复用已讨论的 ChunkIndexInput 与三路排名合同，按实际需要补最小的词项查询输入和
命中证据字段，不照搬 RAGFlow 的完整多后端表达式体系。人物始终直接使用人名。

#### 从 RAGFlow 学习的具体边界

RAGFlow 的 [FulltextQueryer](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/query.py:28)
决定字段与查询表达，[MatchTextExpr](/Users/bytedance/workspace/other-project/ragflow/common/doc_store/doc_store_base.py:58)
承载文本查询要求，[Dealer](/Users/bytedance/workspace/other-project/ragflow/rag/nlp/search.py:79)
接收存储连接并组织检索，[ESConnection](/Users/bytedance/workspace/other-project/ragflow/rag/utils/es_conn.py:179)
将要求转换成 ES 查询并执行。[settings](/Users/bytedance/workspace/other-project/ragflow/common/settings.py:398)
选择存储实现并装配检索器。

所查默认 ES 路径同时处理全文与 Dense KNN，见
[MatchDenseExpr 分支](/Users/bytedance/workspace/other-project/ragflow/rag/utils/es_conn.py:253)。
AniVAG 同样采用 ES 统一保存词项与向量，额外保留独立的视频向量字段。上述策略与适配
分工可供学习；RAGFlow 的目录名和 Dealer 内部编排不要求原样迁入我们的纯 Engine。

#### 索引完成与失败恢复

Server 按 Interval 确定成员，逐 Chunk 检查所有关联 Interval 已提交 Refine，再读取当前有效
Chunk 成果及内容版本构建索引。同一 Interval 的就绪成员可以先完成索引；剩余成员尚未放行
属于等待，不算索引失败、不消耗重试次数，也不持有租约。后续由 DB Queue 扫描领取继续处理，
宿主提交后通知用于主协调器唤醒，不用相关 Interval 的 `COMPLETE` 互相等待。
将该 Interval 的 `INDEXING` 更新为 `COMPLETE` 前必须确认：

1. 本 Interval 全部成员均已满足关联 Refine 提交条件，且当前有效 Chunk 所需的词项 / 向量表示完整，
   内容与编码合同相符；提交 `COMPLETE` 前校验
   来源内容版本仍有效。Prefix 等后台处理已改写成果时，重新处理受影响内容后再完成。
2. ES 批量写入逐项成功；失败项完成重试，不能只检查请求的 HTTP 状态。
3. 必需记录已对检索可见，不能把写入确认直接当成查询就绪。

失败且阶段内未能恢复时，该 Interval 记为 `INDEXING_FAILED`，其 MySQL Refine 成果保留，其他任务可继续。
重试读取当前有效 Chunk 成果及内容版本，使用稳定 Chunk 文档 ID 重放写入；已有 ES 部分记录
不代表 Interval 索引完整。写入需防止迟到的旧版本覆盖新版本，不能只在 MySQL 完成提交前校验版本。
重试仍按逐 Chunk 放行条件处理，不能用旧 Interval 快照覆盖更新后的成果。
Worker 中断后按 Chunk / Interval 的持久化阶段恢复，不重新解析已经成功的内容。
基础解析、必需 Interval 及全部要求的有效 Chunk 索引完成后，Video 主任务才汇总为 `COMPLETE`。

MySQL 与 ES 不做跨库事务，不维护独立 build ID、发布指针或逐 Chunk 索引进度表。
2026-09-28 用户明确撤销“旧版持续服务、新版整版原子切换”的要求。
当前选定任务进入重解析/重建状态后，只要其状态不是 `COMPLETE`，即使 ES 仍保留旧文档或向量，
也不得返回该任务的数据；旧值存在不能绕过任务状态检查。
当前对外检索仍限定到选定的 `COMPLETE` Video 解析任务，各通道与事实回查使用同一任务及相符的内容版本；
这是现有文字中的检索开放条件，不阻止各 Interval 提前写入 ES；架构图在局部 Refine、embedding 后
标注“可以进入召回”，是否改为局部开放仍待收敛，不能由任务职责的调整直接推定。
后续仅重建索引时，负责目标有效 Chunk 的 Interval 回到 `INDEXING`，复用成果及相容向量，
主任务重新汇总；Chunk 基础解析状态不变。Refine 实际修改正文时递增内容版本，首次索引等待
目标 Chunk 的全部关联 Refine 提交。Prefix 后续修改检索内容时，在同一事务递增版本，
将包含目标 Chunk 且已提交 Refine 的 Interval 恢复为 `INDEXING` 并清除 `finish_time`，提交后唤醒索引；
重建不改变尚未提交 Refine 的任务阶段。仅 `detail.scope` 或 compact 元数据变化不触发重建。
索引失败同样记录在 Interval 上。

幂等写入、失败重试和按任务 / 视频范围清理 ES 记录仍由 Server 负责，不依赖被删除的三表。
当前 [IndexSyncService](/Users/bytedance/workspace/other-project/AniVAG/server/service/parsev1/index_sync.py:20)
只面向现有 Qdrant 路径。现有 ES 三表及配套代码也需要改接主任务流程；本文的设计调整
不代表已完成 ORM、Worker、CLI、建库校验或数据库迁移。

### 2.3.12 文本 Sparse 与截图检索：2026-09-28 确认的需求

以下是用户确认的目标需求，不代表现有代码已实现，也不替代上文的基础三路定义。

| 范围 | 已确认的需求 |
| --- | --- |
| 文本查询 | BM25 默认使用；Dense 默认包含 Chunk 文字综合语义向量与 Chunk 视频向量两路，两路分别检索，不合成一个向量。学习式文本 Sparse 可启用或关闭，默认关闭；它是额外的文本召回方式，不取代视频 Dense。 |
| 截图查询 | 支持用原视频截图定位，也支持局部裁剪的截图；还要能找语义相似的画面，不限制为同一人物或同一视频。截图检索是独立的图片输入需求，不能直接等同于文本 Sparse 检索。 |
| 定位结果 | 至少返回对应 Chunk；能够可靠定位时，进一步给出帧级位置（帧号或视频时间点），否则回退到 Chunk 定位。 |

尚待设计的是 Sparse 启用时的编码与建索引策略、默认关闭时是否预先构建表示，
以及截图同源匹配与语义相似匹配的索引、融合、抽帧覆盖和帧级定位依据。
这些实现选择不能从上述需求直接推定；落地前需与 ES 重构实施计划对齐。

# 三、未来拓展

本章记录高级检索 TODO，不纳入第一版基础三路的交付前提。以下均尚未实现。

## 3.1 TODO：学习 RAPTOR 的层级摘要检索

目的：为跨多个 Chunk 的剧情或主题问题提供更高层概括，命中后回到原始片段取证。

- [ ] 学习 RAGFlow 从已有内容与向量进行聚合、生成摘要、再次编码并组织层级的流程；
  评估连续场景 / Interval 摘要作为视频第一种层级表示的收益。
- [ ] 为每个摘要显式维护来源 Chunk、事件引用及时间范围。聚类不保证时间连续；涉及
  不连续片段时保留各段范围，不把首尾时间之间的所有视频都当成主题证据。
- [ ] 设计摘要召回到源 Chunk 的展开流程，保留基础 Chunk 通道；重排与最终引用回查
  正式 Timeline 和原视频，摘要不能替代原话或具体事件的证据。
- [ ] 确定摘要构建预算、解析变更后的失效与重建规则、来源任务及内容版本关联；使用长剧情查询
  测量新增召回、额外成本与定位质量，再决定是否启用。

源码依据：[RAPTOR 生成](/Users/bytedance/workspace/other-project/ragflow/rag/svr/task_executor_refactor/raptor_service.py:344)。
所查 is_tree=False 分支保存摘要文本、向量、分词字段和层级；is_tree=True 分支保存结构树。
不能由输入携带来源就推断所有落库分支都完整保存了来源：当前非树摘要物化分支未写入
source_chunk_ids，AniVAG 需要显式落实自己的来源映射。

## 3.2 TODO：学习 GraphRAG 的实体与关系检索

目的：在人物对应与关系事实可靠后，支持跨片段的人物互动、关系变化和多跳关联查询。

- [ ] 学习 RAGFlow 的可选 KG 入口，以及实体、关系、N-hop 路径和社区信息的检索方式；
  用实际视频关系问题确定需要哪些能力。
- [ ] 先定义人名对应规则。现有合同直接保存人名；按视频 / 作品范围解释名称，同名不能
  自动跨作品合并，译名或别名的关联须有依据，不确定对应保留不确定性。
- [ ] 从可核验 Timeline 派生实体与关系，记录方向、时间及来源事件 / Chunk；区分可见
  事实与推断。图的更新、删除和来源内容版本需与来源变更对应。
- [ ] 设计关系命中到真实片段的回查，并评估其在基础三路之上的独有召回、误关联、
  模型成本和维护成本；获得收益证据后再确定具体存储与实现范围。

源码依据：[可选 KG 调用](/Users/bytedance/workspace/other-project/ragflow/agent/tools/retrieval.py:226)、
[实体与关系检索](/Users/bytedance/workspace/other-project/ragflow/rag/graphrag/search.py:139)。
