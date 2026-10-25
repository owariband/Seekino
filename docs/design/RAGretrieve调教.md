# AniVAG RAG 召回机制与调教

## 1. 文档目的

本文记录三件事：

1. AniVAG 当前已经实现的向量建库、匹配和融合全过程。
2. `channel_top_k`、`rrf_k`、通道权重等参数为什么会改变最终结果。
3. “由 Query Agent 判断更重视原台词、整体语义还是视频画面，并按问题动态调整三路权重”的当前策略与后续调教方向。

必须区分“当前实现”和“后续增强”。当前已经实现 Query Agent 自主调权的三通道召回；Transcript/ASR V1 是确定性的原话词法匹配，不是第三个 dense vector，也还没有专用全文索引或语义精排模型。

## 2. 一句话结论

当前策略是：

> 原始 ASR 通过规范化连续短语匹配形成 Transcript 排行榜；同一个 Query 在 Pro Semantic 与 Video Vision 两个 named vector 中分别做余弦近邻搜索，形成另外两份排行榜；Query Agent 为每次 Tool Call 自主选择三路连续权重，Server 归一化后使用 Weighted RRF 做后融合。

这个策略可称为：

- 中文：**Agent 驱动的查询自适应三路混合召回**。
- 英文：**Agent-routed Query-Adaptive Multi-Channel Hybrid Retrieval**。
- 融合机制：**LLM Soft Routing + Query-Adaptive Weighted RRF Late Fusion**。

三路是并行召回信号，不是依次执行的“三层召回”。RRF 是排名融合，不是语义精排。当前 Evaluator 审核的是候选能否支撑回答，也不是对融合候选逐个打相关分的 Cross-Encoder/LLM Reranker；因此当前严格意义上仍然**没有精排阶段**。

## 3. 当前建库全过程

### 3.1 检索单位

当前检索单位是规范 `VideoChunk`，默认每段 60 秒。一个 VideoChunk 在 Qdrant 中对应一个 point，point ID 是稳定的 `videoChunkId`。

### 3.2 Pro Semantic 文本向量

Lite ASR 先得到候选台词，Pro 视频解析再结合视频、候选 ASR、硬字幕和上下文，输出结构化 Timeline Events。

当前文本检索文档由以下内容拼接：

```text
self_compact
+ 每个 owned event.description
+ 每个 owned event.text（存在时）
```

这里的 `self_compact` 本身也是对 Pro 解析后事件的文本渲染。因此当前代码中的 `text` named vector，本质上更接近 **Pro Semantic Vector**，承载的是对白、动作、场景和分镜语义的混合描述。

原始 `candidate_transcript` 会持久化到 MySQL。它不会加入 Pro Semantic 检索文档，也不进入 Qdrant named vectors；当前由独立的 Transcript 词法通道从 MySQL 的可检索 Chunk 中读取并排名。

### 3.3 Video Vision 向量

同一个 VideoChunk 的媒体文件会直接送入多模态 Embedding 模型，生成 `vision` named vector。它表示模型从视频媒体中得到的画面/视频语义，不依赖 Pro 输出的文字描述。

当前 Text/Vision 均使用 `doubao-embedding-vision-251215`，默认 1024 维；视频 Embedding 默认按 1 FPS 取样。具体模型、维度和 FPS 都属于索引版本的一部分，调整后必须重建对应向量。

### 3.4 Qdrant point 结构

当前一个 point 的核心结构可抽象为：

```json
{
  "id": "videoChunkId",
  "vector": {
    "text": ["Pro Semantic Vector"],
    "vision": ["Video Vision Vector"]
  },
  "payload": {
    "seriesId": "...",
    "videoId": "...",
    "videoChunkId": "...",
    "sourceRevision": "...",
    "isFallback": false
  }
}
```

`text` 和 `vision` 不是拼接成一个大向量，也不是先相加再存储。它们是同一个 point 上的两个独立 named vector，可以分别搜索和排名。

## 4. 当前查询全过程

### 4.1 Query 的词法与向量两条路径

用户或 Query Agent 给出检索文本，例如：

```text
雨天路口红绿灯下行人穿过积水
```

如果 Transcript 权重大于 0，系统先对 Query 做 Unicode NFKC、大小写、空白和标点规范化，在当前 READY + SYNCED + UPSERT Scope 的原始 ASR 中寻找连续原话；一句话可以跨相邻 cue。AnalysisWindow 重叠产生的重复 cue 通过 cue 中点所属的 canonical VideoChunk 归属规则去重。

如果 Semantic 或 Vision 权重大于 0，系统再使用与建库兼容的多模态 Embedding 模型，把 Query 编码成一个文本 Query Vector：

```text
q = embed_text(query)
```

Text Vector 和 Vision Vector 维度必须一致，因此同一个 `q` 可以分别与两个向量空间中的 point 做匹配。若只启用 Transcript，系统完全跳过 Query Embedding 和 Qdrant。

这不表示 Query 真的变成了视频，而是多模态 Embedding 模型把文本和视频编码进了可比较的共享语义空间。

### 4.2 三个通道独立产生排行榜

Qdrant 对每个通道分别计算 Query Vector 与候选向量的余弦相似度：

```text
cosine(q, v) = dot(q, v) / (norm(q) * norm(v))
```

Semantic 与 Vision 会产生两份互相独立的余弦排行榜；Transcript 产生一份词法排行榜：

```text
Transcript/ASR 排行榜
1. Chunk E   literal=1.00
2. Chunk A   literal=0.96
...

Semantic/Text 排行榜
1. Chunk A   cosine=0.83
2. Chunk C   cosine=0.79
3. Chunk B   cosine=0.75
...

Vision 排行榜
1. Chunk B   cosine=0.88
2. Chunk A   cosine=0.77
3. Chunk D   cosine=0.72
...
```

当前每个通道默认最多保留 `channel_top_k=20` 个候选。一个片段可以同时出现在多份榜单，也可以只出现在其中一份。Transcript 的 match score 用于通道内排序和观测，RRF 仍只消费它的 rank。

Series Scope 会作为 Qdrant filter 参与两路向量查询，然后才在该 Scope 内取 Top 20，并不是先取全库 Top 20 再做 Series 过滤。Transcript 同样只读取当前 Workspace、Series Scope 且索引状态有效的 Chunk。正式向量服务默认使用 Qdrant ANN；诊断和评测可以显式要求 exact search。

### 4.3 为什么三个通道会得到不同顺序

三个通道比较的对象和擅长信息不同：

- `transcript` 按原始 ASR 的连续字符短语匹配，适合原话、专名和固定措辞；它不做近义词理解，也不能仅凭当前无 speaker 的 ASR 确认说话人。
- `text` 向量来自 Pro 结构化事件文本，更容易命中人物关系、对白含义、事件因果和剧情概括。
- `vision` 向量来自视频媒体，更容易命中颜色、构图、动作、场景、物体和相似视觉片段。

例如查询“雨天路口红绿灯”：

- Pro 文本中明确写了“雨天城市路口，信号灯切换”，该片段会在 Text 排行较高。
- 视频画面确实包含雨幕、积水和信号灯，即使 Pro 文本描述较简略，也可能在 Vision 排行较高。
- 两边都强的片段会同时获得两路排名贡献。

### 4.4 当前 Tool 与 Service 参数流

Query Agent 当前传给检索工具的参数是：

```json
{
  "query": "检索文本",
  "top_k": 8,
  "channel_weights": {
    "transcript": 0.1,
    "semantic": 0.7,
    "visual": 0.2
  }
}
```

`workspaceId` 和 `seriesIds` 由服务端固定绑定，Agent 不能修改检索范围。Tool 中的 `top_k` 只控制融合后的最终返回数量；三个底层通道仍各自使用固定的 `channel_top_k`。

当前流程是：

```text
Query Agent: {query, top_k, channel_weights}
    ↓
服务端绑定 workspace / Series Scope，限制 top_k，校验并归一化权重
    ↓
Transcript 权重 > 0：读取 scoped ASR，做规范化连续短语匹配
Semantic 或 Vision 权重 > 0：query 生成一次 text embedding
    ↓
Transcript Top 20
Qdrant text named vector Top 20
Qdrant vision named vector Top 20
    ↓
本次调用的 Query-Adaptive Weighted RRF
    ↓
按 fused score 排序
    ↓
截取最终 top_k
    ↓
MySQL hydrate 完整证据
```

## 5. Weighted RRF 如何融合顺序

### 5.1 当前公式

当前融合不直接相加词法分数或两个向量通道的余弦分数，而是使用每个候选在通道内的名次：

```text
fused_score(chunk)
  = transcript_weight / (rrf_k + transcript_rank)
  + text_weight   / (rrf_k + text_rank)
  + vision_weight / (rrf_k + vision_rank)
```

某个片段没有进入某通道的 Top K 时，该通道没有这一项贡献。

非 Agent 调用未显式传权时的启动级默认值：

```text
text_weight   = 1.0
vision_weight = 1.0
rrf_k         = 60
channel_top_k = 20
```

Query Agent 调用不直接沿用这组 `1:1` 向量权重，而是每次提交
`channel_weights.transcript`、`channel_weights.semantic` 与 `channel_weights.visual`；Server 校验后把比例归一化为总和 1。非 Agent 调用保持 `transcript=0`，以兼容原有双向量搜索行为。
`rrf_k` 和 `channel_top_k` 仍是服务端固定参数。

原始 transcript match score 与 cosine score 会保留在结果里用于观察，但当前 RRF 排序不直接使用它们。

### 5.2 为什么使用排名而不是直接混合 cosine score

不同检索算法的原始分数没有共同标尺。Transcript 的 `1.0` 是规范化连续原话完全覆盖，Text/Vision 的 `0.78` 是各自向量空间中的 cosine；它们不能当成同一种置信度直接相加。例如只对两个向量通道直接做：

```text
0.5 * text_cosine + 0.5 * vision_cosine
```

要求两个通道先完成可靠的分数校准，否则一个通道可能仅因为分数分布更高而长期压过另一个通道。

RRF 只使用“第几名”，牺牲部分分数强弱信息，换取跨通道融合的稳定性。

### 5.3 权重为什么能改变最终顺序

权重不会修改：

- Query Vector；
- Qdrant 算出的 cosine；
- 每个通道内部的原始名次。

权重修改的是每个通道的名次在最终投票中值多少钱。

假设 `rrf_k=60`，两个候选的排名相反：

| 候选 | Text Rank | Vision Rank |
|---|---:|---:|
| A | 1 | 20 |
| B | 20 | 1 |

三组权重下的结果：

| 权重 | A 的 fused score | B 的 fused score | 最终顺序 |
|---|---:|---:|---|
| Balanced `0.5 / 0.5` | `0.014447` | `0.014447` | 两者并列 |
| Text-heavy `0.7 / 0.3` | `0.015225` | `0.013668` | A 在前 |
| Vision-heavy `0.3 / 0.7` | `0.013668` | `0.015225` | B 在前 |

这里为了便于比较，把每组权重都归一化为总和 1。未显式传权时的启动配置
`1:1` 会由 Service 归一化为 `0.5:0.5`；最终顺序和 fused score 标尺均与 Agent
显式提交的 `0.5:0.5` 一致。

以 Text-heavy 为例：

```text
A = 0.7 / (60 + 1)  + 0.3 / (60 + 20)
  = 0.015225

B = 0.7 / (60 + 20) + 0.3 / (60 + 1)
  = 0.013668
```

所以“调高 Text 权重”并不是让 A 的文本向量突然更相似，而是让 A 的 Text 第 1 名对最终排序产生更大的贡献。

### 5.4 RRF 会奖励跨通道共识

在默认 `rrf_k=60` 且两路等权时：

```text
Chunk C：Text Rank 1，但没有进入 Vision Top 20
score = 1 / 61 = 0.016393

Chunk D：Text Rank 20，同时 Vision Rank 20
score = 1 / 80 + 1 / 80 = 0.025000
```

Chunk D 虽然两边都只排第 20，仍会排在只由单路强力命中的 Chunk C 前面。这说明当前参数明显奖励“双通道都认可”的候选。

这不一定永远正确：原台词问题可能只在 Transcript/Text 通道很强，纯构图问题也可能只在 Vision 通道很强。因此动态权重才有价值。

### 5.5 各参数分别改变什么

#### `channel_top_k`

控制每个通道进入融合池的候选数量。

- 太小：其他通道无法救回单路排名稍低但综合正确的候选。
- 太大：噪声候选增加，融合和后续 Agent 上下文成本上升。
- 权重无法救回没有进入对应通道 `channel_top_k` 的片段。

#### `rrf_k`

控制同一通道内头部名次之间的差距。

- `rrf_k` 越小，第 1 名相对第 20 名的优势越大。
- `rrf_k` 越大，各名次贡献越接近，更强调“出现在哪几路”而不是“在单路排得多靠前”。
- 当前 `rrf_k=60` 相对温和，跨通道共识影响较明显。

#### 通道权重

控制不同通道之间的相对话语权。

- 正比例放大所有权重不会改变顺序；`7:2:1` 与 `0.7:0.2:0.1` 的排序相同。
- 权重只改变最终融合顺序，不改变通道内部近邻顺序。
- 某通道权重为 0 时，当前 Service 会跳过该通道查询，并在候选说明中明确标记“本次策略未启用”，而不是误写成“查询后未召回”。
- fused score 不是概率、cosine 或可直接展示的置信度，只能用于同一次融合中的相对排序。

#### 最终 `top_k`

只在完成融合排序后截断结果，不改变前面的通道候选和融合计算。

融合分数完全相同的候选，当前以 `videoChunkId` 字典序稳定打破平局；这只是确定性规则，不代表额外的语义判断。三路各取 20 时，进入融合的并集最多有 60 个不同 VideoChunk。

## 6. 当前实现的边界

### 6.1 当前是三路召回，但只有两路是向量

当前正式召回包含：

1. Transcript/ASR 规范化连续原话匹配；
2. Pro Semantic/Text Vector；
3. Video Vision Vector。

Transcript V1 从 MySQL JSON 事实读取 scoped cues，在 Engine 纯函数中完成规范化、跨相邻 cue 连续匹配、Chunk 聚合和排名。它不修改 Qdrant schema，也不需要重跑已有视频解析。当前代价是每次 Transcript-enabled 调用会线性扫描 Scope 内的可检索 transcript；数据规模扩大后应迁移到独立 cue 表及可索引的候选生成器。

Transcript 是词法匹配，不负责翻译。Series 通过 `transcriptLanguageHint` 向 Query Agent 提供 ASR 原文语言提示，例如 MyGO 日语音轨配置为 `ja`。用户直接给出原文时，Agent 应保留原语言短语；用户只给中文译意或剧情释义时，应先做 Semantic-heavy 召回，再从真实 `evidence.transcript` 复制日语 cue 做 Transcript-heavy 精确复查。模型自行翻译的句子只能是检索尝试，不能冒充已验证原台词；未配置提示时也不能根据作品名或文件名断言语言。

### 6.2 当前权重是调用级动态参数

Query Agent 在每次 Tool Call 中传入 `channel_weights.transcript`、`channel_weights.semantic` 和 `channel_weights.visual`。Server 拒绝 bool、NaN、Infinity、负数、全 0 和未知字段，并把权重归一化后作为局部参数传入 `VideoRetrievalService`。它不修改共享 Service 实例状态，所以并发 Kick 之间不会串权重。

启动配置中的 `text_weight` 和 `vision_weight` 仍作为非 Agent 调用或未显式传权时的默认比例。

### 6.3 Query Agent 已有的能力

Query Agent 当前可以：

- 改写 Query；
- 一轮内多次调用检索工具；
- 为每次调用选择不同的 Transcript/Semantic/Visual 连续权重；
- 在 Evaluator 要求继续时重新检索；
- 从真实候选中选择最终片段。

它当前已能动态决定“搜什么、搜几次、三路各信多少、选哪些”。原话检索应把 Query 改写为原句或有区分度的连续短语；自然语言语义 Query 与原话短语冲突时，可以自主分成两次 Tool Call，并使用不同权重。

### 6.4 当前归因边界

Transcript 命中会返回真正参与匹配的 cue 文本和绝对时间，不再用片段第一条 transcript 假装命中原因。Semantic 命中展示 `videoCompact/retrievalText`，Vision 命中展示视觉事件或 caption。

但 Semantic/Visual 当前仍只保留 Chunk 级向量 rank/score，没有 token/frame 级 attribution；展示证据用于核验，不等于模型已经解释了向量相似度的全部来源。

### 6.5 当前没有精排

当前链路是“三路初召回 → Weighted RRF → top_k → hydrate → Query Agent 选择 → Evaluator 审核”。其中：

- RRF 是 rank fusion，不是 reranker；
- Query Agent 会选择证据，但没有对固定 fused Top N 逐项输出统一相关性分；
- Evaluator 判断草稿和证据是否足够支撑回答，不承担 Cross-Encoder/LLM relevance reranking。

若增加精排，最合理的插入点是 RRF 合并得到较大的候选集之后、最终截断和提交 Query Agent 之前。精排输入应使用 Query + 候选的 transcript/compact/visual evidence，输出新的相关性分与可审计理由。

## 7. 当前 Agent 自适应三路混合召回

### 7.1 三个主通道

#### Transcript / ASR

当前用于召回原台词、短句、专有名词和明确措辞。当前内部实现是：

```text
读取 READY + SYNCED + UPSERT Scope 的 ASR JSON
       ↓
Unicode NFKC + casefold + 去空白/标点
       ↓
连续短语匹配（允许跨相邻 cue）
       ↓
按 canonical owner 去重并聚合到 videoChunkId
       ↓
形成一个 Transcript Rank
```

对顶层 Query Agent 只暴露一个 Transcript 权重。V1 没有 cue dense 召回，因此错字、同义改写和语义相近但非原话的 Query 应交给 Semantic；未来可以在 Transcript 通道内部增加 ngram/BM25 或 cue dense 候选，但不能悄悄改变字段语义。

#### Pro Semantic

继续使用 Pro 解析得到的事件描述、确认对白和片段 compact，适合剧情、人物关系、事件发展和整体语义问题。

#### Video Vision

继续使用视频媒体向量，适合画面、动作、颜色、构图、环境、物体和相似镜头问题。

### 7.2 三路融合公式

```text
fused_score(chunk)
  = transcript_weight / (rrf_k + transcript_rank)
  + semantic_weight   / (rrf_k + semantic_rank)
  + visual_weight     / (rrf_k + visual_rank)
```

缺失某一路排名时，该项没有贡献。Transcript 无命中是正常的 empty channel：混合检索仍使用其他通道；Transcript-only 调用返回合法空结果，不伪造匹配，也不当成索引一致性故障。Server 不会在模型不知情时改写 requested weight；因此 Agent 应在无命中后自主改写短语或发起 Semantic-heavy 调用。

### 7.3 Agent 的判断思想

Query Agent 根据当前问题而不是整个会话固定选择侧重点：

| 用户问题特征 | 建议侧重点 |
|---|---|
| 带引号的句子、“谁说过”“原话是什么”、日语台词片段 | Transcript 高 |
| “为什么”“发生了什么”“关系如何变化”“某段剧情” | Pro Semantic 高 |
| 颜色、服装、构图、地点、动作、镜头、物体 | Video Vision 高 |
| 同时问“说了什么并做了什么” | 混合权重，或拆成多次不同侧重点的检索 |

Agent 不必被固定为一次只调用一种策略。一个复合问题可以先用 Transcript-heavy 找台词，再用 Semantic/Visual-heavy 检索动作与场景，最后由 Evaluator 审核联合证据。

### 7.4 建议的初始权重参考

当前 Retrieval Skill 提供以下三路冷启动参考，但 Tool 接收的是模型自主选择的连续权重，不是 Profile 枚举：

| Profile | Transcript | Semantic | Visual |
|---|---:|---:|---:|
| Balanced | 0.34 | 0.33 | 0.33 |
| Dialogue | 0.70 | 0.20 | 0.10 |
| Semantic | 0.15 | 0.70 | 0.15 |
| Visual | 0.10 | 0.20 | 0.70 |

当前三路采用 Agent 直接提交连续权重，服务端执行：

- 各项都是 `0..1` 的有限非负数，拒绝 bool、NaN、Infinity 和负数；
- 至少一项大于 0；
- 服务端统一归一化；
- 非法输入直接拒绝，不静默回退以免实际策略与 Agent 意图不一致；
- 返回并记录 requested weights、effective weights 和 strategy version。

不要同时暴露 Profile 和连续权重，避免出现两套互相冲突的真相。

### 7.5 Tool 示例

当前三路实现：

```json
{
  "query": "不要把责任丢给别人",
  "top_k": 8,
  "channel_weights": {
    "transcript": 0.7,
    "semantic": 0.2,
    "visual": 0.1
  }
}
```

当前已选择“连续权重”接口，不再同时暴露 Profile，避免两套互相冲突的真相。

Agent 不应控制以下参数：

- Workspace / Series Scope；
- Qdrant collection 和过滤条件；
- `rrf_k`；
- 每通道最大候选深度；
- exact/ANN 开关；
- 索引版本。

这些仍由服务端配置和评测结果管理。

## 8. 结果必须可解释和可复现

当前 Agent 最终结果会把所有 Query/Evaluator 轮次中的 Tool Call 按时间顺序持久化为
`retrievalTrace`。每一项记录实际 Query、受限后的 `topK`、requested/effective weights、
返回数量和有序 Chunk ID：

```json
{
  "retrievalTrace": [
    {
      "query": "丰川祥子在练习室宣布退出乐队",
      "topK": 8,
      "strategyVersion": "query-adaptive-three-channel-rrf-v2",
      "requestedWeights": {"transcript": 0.6, "semantic": 0.3, "visual": 0.1},
      "effectiveWeights": {"transcript": 0.6, "semantic": 0.3, "visual": 0.1},
      "resultCount": 2,
      "videoChunkIds": ["chunk-a", "chunk-b"]
    }
  ]
}
```

该 trace 同时通过 Agent 消息 JSON 和 `answer.evidence` SSE 暴露，便于本轮实验直接观察；
普通 `/api/search` 仍保持原有结果契约，不附带 Agent 调用轨迹。

候选自身的 `transcriptRank/transcriptScore`、`textRank/textScore`、
`visionRank/visionScore`、`fusedRank/fusedScore` 以及 `selectionReason` 保存在 `hits` 中。
Transcript selection reason 使用真正命中的 cue 与绝对时间，不能用“第一条 transcript”
假装命中原因。

观测指标至少包含：

- 三路 requested/effective weights；
- 每路候选数和最终命中数；
- 每路 rank 与原始相似度；
- 融合后的 rank/score；
- 真正命中的 cue/event 及时间；
- Query embedding、各路检索、融合、hydrate 的分阶段耗时；
- 策略版本和索引 source revision。

## 9. 调教与评测方法

### 9.1 评测集分类

至少建立四类带标准时间范围或 VideoChunk ID 的 Query：

1. 原台词：完整台词、半句、错字、不同标点、原语言片段。
2. 整体语义：人物关系、事件原因、剧情概括、前后变化。
3. 视频画面：颜色、物体、动作、地点、构图、镜头特征。
4. 混合条件：台词 + 动作、人物 + 场景、剧情 + 画面限制。

### 9.2 指标

召回阶段优先关注：

- Hit@K / Recall@K；
- MRR；
- nDCG@K；
- 原台词 cue 命中率；
- 命中 cue 与标准时间范围的重叠；
- 各类 Query 的失败分布。

回答是否正确属于 Agent/Evaluator 的下游指标，不能用回答质量掩盖底层召回漏检。

### 9.3 调参顺序

建议按以下顺序调教，避免多个变量同时变化后无法归因：

1. 分别评估 Transcript、Semantic、Visual 单通道基线。
2. 固定各路候选深度，搜索三路连续权重比例。
3. 固定权重后调 `rrf_k`。
4. 再调每路 `channel_top_k` 和最终 `top_k`。
5. 最后评估 Agent 是否为不同 Query 选择了接近离线最优的连续权重。

Agent 路由评测要单独统计：

- 权重意图分类准确率；
- Agent 权重与离线最优权重的效果差距；
- 一次混合检索与多次拆分检索的收益和成本；
- Evaluator 要求重检后是否真正改善 Recall@K。

### 9.4 已有探索性基线

仓库已有一组 MyGO 第 7 集的 12 条人工 Query 冒烟评测。候选单位为 60 秒 VideoChunk，每路 Top 20，`rrf_k=60`：

| Channel | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR |
|---|---:|---:|---:|---:|---:|
| Text | 58.3% | 91.7% | 91.7% | 100.0% | 0.747 |
| Vision | 25.0% | 58.3% | 58.3% | 100.0% | 0.441 |
| Equal-weight RRF | 41.7% | 75.0% | 91.7% | 100.0% | 0.623 |

这组数据说明两个问题：

1. 当前双路索引适合作为高召回候选生成器，12 条 Query 都能在 Top 10 找到相关 VideoChunk。
2. 通道质量不同时，等权融合不一定优于强单路；较弱通道可能把正确的 Text 头部候选往后拉。

在同一组 12 条数据上把 Text:Vision 离线重算为 `3:1`，得到 Hit@1 75.0%、Hit@3 91.7%、Hit@10 100.0%、MRR 0.845。这只能证明“权重确实会改变排序，而且存在更优比例”，不能直接把 `3:1` 定为生产默认：它是同一集上的 in-sample 调参，必须用更大且按 episode 隔离的 held-out 数据验证。

原始记录见 `evals/recall/ep07.recall-smoke.md`。

## 10. 实施边界与阶段

### Phase 0：双向量基线（已完成）

- 固化当前 Text/Vision 单路与融合评测结果。

### Phase 1：建立 Transcript 词法通道（当前已完成 V1）

- 直接读取已有 MySQL transcript，无需重跑解析或迁移 Qdrant。
- 已处理 AnalysisWindow overlap、canonical owner 和重复 cue。
- 已支持规范化 exact/substring 与相邻 cue 连续短语匹配。
- 已聚合为稳定的 VideoChunk Transcript Rank，并返回真实命中 cue。

V1 的限制是每次调用线性扫描 Scope 内 transcript JSON。后续规模化版本应建立独立 cue 表与 ngram/BM25 索引；若再增加 cue dense，会产生 Embedding 成本，但它应是 Transcript 内部候选增强，不必破坏现有 Qdrant chunk named-vector schema。

### Phase 2：三路动态融合（已完成）

- 已将动态 Semantic/Visual RRF 推广到 Transcript/Semantic/Visual 三路。
- 已在 `channel_weights` 连续权重结构中增加 `transcript`，不另设 Profile 枚举。
- 服务端按调用校验并生成 effective weights。
- Query Agent 与 Evaluator 都能看到本次策略和三路真实归因。

### Phase 3：离线调权与在线观察（待完成）

- 以分类评测集调连续权重参考区间、`rrf_k` 和候选深度。
- 版本化策略配置。
- 对比静态 Balanced、规则路由和 Agent 路由。
- 统计 Agent 实际提交的 requested/effective weights，并用离线指标与真实案例持续校准 Skill 中的参考比例。

### Phase 4：可选精排（尚未实现）

- 在三路 RRF 的较大候选池之后加入独立 reranker；
- 用 held-out 数据验证 reranker 对 Recall@K、MRR、nDCG 与延迟/成本的真实收益；
- 保留初召回 rank、RRF score、rerank score 与策略版本，不能用最终回答审核代替精排观测。

## 11. 当前代码锚点

- 检索文本构造：`engine/retrieval/video_retrieval.py`
- Transcript 词法排名与 Weighted RRF：`engine/retrieval/video_retrieval.py`
- Scoped Transcript 事实读取：`server/repository/video.py`
- Query embedding、三路检索与融合：`server/service/video_retrieval.py`
- Qdrant named vectors 与 cosine schema：`server/storage/qdrant.py`
- Query Agent Tool 参数：`server/agent/tools.py`
- Query Agent Retrieval Skill：`server/agent/skills/retrieval/SKILL.md`
- Query Agent 多轮编排：`server/agent/runtime.py`
- Query Agent Prompt：`server/agent/query.py`
- 默认检索配置：`server/config/retrieval.py`
- 原始 Transcript 持久化：`server/model/video.py`、`server/repository/video.py`

## 12. 最容易混淆的结论

1. 权重不改变 ASR 词法匹配、向量或单通道内部顺序；它改变的是各通道排名对最终 RRF 分数的贡献。
2. 当前同一个 Query Vector 分别搜索 `text` 和 `vision`，不是把两个字段先合成一个向量。
3. 当前 RRF 使用 rank，不使用原始 cosine score 做融合。
4. Text 通道是 Pro Semantic；原始 ASR 由独立 Transcript 词法通道排名。
5. Query Agent 可以在每次检索中动态设置 Transcript/Semantic/Visual 三路权重。
6. 动态权重只能重排已经进入各通道候选池的片段，不能救回所有通道 Top K 之外的片段。
7. Transcript 命中已绑定真实 cue；Semantic/Visual 仍是 Chunk 级证据，不应夸大为 token/frame 级归因。
8. fused score 不是相关概率；同分时按 VideoChunk ID 排序也不是新的语义判断。
9. 当前没有独立精排模型；RRF、Query Agent 选择和 Evaluator 审核都不能改叫 reranking。
