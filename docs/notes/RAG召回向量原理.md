# RAG 召回向量与 RRF 融合原理

## 1. 一句话结论

Seekino 的召回不是把多个向量先合成一个向量，而是：

> 各召回通道独立计算相似度、独立产生排行榜；对各榜单 Top K 的并集按 Chunk ID 去重，再用 Weighted RRF 对名次加权投票，得到最终排行。

当前代码实现是 **Transcript/ASR 词法匹配 + Pro Semantic 向量 + Video Vision 向量的 Query-Adaptive 三通道融合**。

## 2. 三个召回通道分别如何匹配

建库时，每个 VideoChunk 会得到用于检索的向量。当前 Qdrant point 中有两个 named vector：

- `text`：来自 Pro 解析文本，主要表达片段摘要、事件、已确认对白和整体语义。
- `vision`：来自视频片段本身，主要表达场景、物体、动作、颜色和构图等视觉信息。

原始 ASR 不作为第三个 Qdrant vector。它保存在 MySQL `VideoChunk.transcript` 中，当前 Transcript 通道会：

1. 只读取当前 READY Series Scope 且处于 SYNCED + UPSERT 的 Chunk；
2. 用 cue 中点所属的 canonical VideoChunk 消除 AnalysisWindow overlap 重复；
3. 对 Query 和 cue 做 Unicode NFKC、大小写、空白与标点规范化；
4. 做连续原话/短语匹配，允许一句话跨相邻 cue；
5. 按 `videoChunkId` 聚合，得到独立 Transcript 排行榜。

这是精确词法召回，不是向量语义匹配；“不要甩锅”不会被当成“别把责任推给别人”的原话命中。近义表达应由 Semantic 通道处理。

查询时，用户问题被编码成 Query Vector：

```text
q = embed_text(query)
```

当前使用共享语义空间的多模态 Embedding 模型，因此同一个文本 Query Vector 可以分别搜索 `text` 和 `vision` 向量。这不是把文本变成了视频，而是文本和视频都被编码到了可比较的向量空间。

两个向量通道分别计算余弦相似度：

$$
\operatorname{cosine}(q,v)
=
\frac{q\cdot v}{\lVert q\rVert\lVert v\rVert}
$$

余弦相似度越高，候选在对应向量通道中的排名越靠前。加上 Transcript 词法榜后，三份榜单通常不同：

```text
Transcript/ASR      Semantic/Text       Video/Vision
1. Chunk E          1. Chunk A          1. Chunk B
2. Chunk A          2. Chunk C          2. Chunk A
                    3. Chunk B          3. Chunk D
```

## 3. RRF 是什么

**RRF 是 Reciprocal Rank Fusion 的缩写**，中文通常叫：

> **倒数排名融合**

它的用途是把多个召回通道产生的排行榜合并成一个总排行榜。

经典 RRF 公式是：

$$
\operatorname{RRFScore}(d)
=
\sum_i\frac{1}{k_{\mathrm{rrf}}+\operatorname{rank}_i(d)}
$$

其中：

- $d$：某个候选片段；
- $i$：召回通道，例如 Transcript、Semantic 或 Vision；
- $\operatorname{rank}_i(d)$：片段在该通道中的名次；
- $k_{\mathrm{rrf}}$：RRF 平滑常数，当前默认为 `60`。

例如某个 Chunk：

- Text 排名第 1；
- Vision 排名第 5。

它的 RRF 分数是：

$$
\frac{1}{60+1}+\frac{1}{60+5}
$$

### 3.1 为什么叫 Reciprocal Rank

因为它使用排名的倒数形式：

```text
第 1 名  → 1 / (k_rrf + 1)
第 2 名  → 1 / (k_rrf + 2)
第 20 名 → 1 / (k_rrf + 20)
```

排名越靠前，对最终分数的贡献越大。

### 3.2 RRF 不直接相加 cosine score

Text 通道的 `0.80` 和 Vision 通道的 `0.80` 不一定表示相同强度的相关性。不同模态、不同索引的分数分布可能不在同一标尺上。

RRF 只使用每个通道中的名次，从而避免在未做分数校准时直接混合不可比的原始分数。

## 4. Weighted RRF 与动态权重

Weighted RRF 会给不同通道的“名次票”乘上不同权重：

$$
\operatorname{Score}(d)
=
\frac{w_{\mathrm{transcript}}}{k_{\mathrm{rrf}}+r_{\mathrm{transcript}}(d)}
+
\frac{w_{\mathrm{text}}}{k_{\mathrm{rrf}}+r_{\mathrm{text}}(d)}
+
\frac{w_{\mathrm{vision}}}{k_{\mathrm{rrf}}+r_{\mathrm{vision}}(d)}
$$

因此：

- 普通 RRF：各通道投票权相同；
- Weighted RRF：不同通道拥有不同投票权；
- Query-Adaptive Weighted RRF：根据当前问题动态决定各通道投票权。

用户问原话或明确措辞时可以偏 Transcript：

```text
Transcript = 0.8
Semantic   = 0.15
Vision     = 0.05
```

用户问剧情、人物关系或对白含义时可以偏 Semantic：

```text
Transcript = 0.1
Semantic   = 0.75
Vision     = 0.15
```

用户问“哪个镜头出现了红色雨伞”：

```text
Transcript = 0.05
Semantic   = 0.15
Vision     = 0.8
```

这三个数值由 Query Agent 为每次 Tool Call 直接提交，Server 校验并归一化；它们是连续权重，不是固定 Profile。原话问题应把 Tool Query 改写成原句或有区分度的连续短语。如果同一个问题还需要整体语义或画面，Agent 可以自主再发起一次 Semantic/Visual-heavy 调用。

权重不会修改 Query Vector、cosine score 或单通道的原始排名。它只会改变各榜单名次对最终排序的贡献。

所有权重同比例放大或缩小不会改变排序。当前 Server 接收 `0..1` 的三路权重并统一归一化，例如 `0.6:0.3:0.1` 与 `0.3:0.15:0.05` 会得到相同的 effective weights 和最终顺序。

## 5. 候选集取并集，不是交集

假设三个通道分别召回：

```text
Transcript Top 20 = {A, E, F, ...}
Semantic Top 20   = {A, B, C, ...}
Vision Top 20     = {B, C, D, ...}
```

候选池是三个 Top K 的并集，并按 Chunk ID 去重：

$$
\operatorname{CandidateSet}
=
\operatorname{TranscriptTopK}
\cup
\operatorname{SemanticTopK}
\cup
\operatorname{VisionTopK}
$$

然后对并集中每个 Chunk 计算：

$$
\operatorname{score}(d)
=
\frac{w_t}{k_{\mathrm{rrf}}+\operatorname{rank}_t(d)}
+
\frac{w_s}{k_{\mathrm{rrf}}+\operatorname{rank}_s(d)}
+
\frac{w_v}{k_{\mathrm{rrf}}+\operatorname{rank}_v(d)}
$$

如果某个 Chunk 没有进入某通道的 Top 20，该通道对它的贡献就是 `0`，不是给它一个“第 21 名”的虚拟分数。

例如：

```text
A：Semantic 第 1、Transcript 第 3、Vision 未入榜
B：Semantic 第 5、Vision 第 2、Transcript 未入榜
D：只在 Vision 排第 1
```

完整融合顺序是：

1. 各通道分别召回 `channel_top_k` 个候选；
2. 对各通道的 Top K 取并集，按 Chunk ID 去重；
3. 为并集中每个 Chunk 计算 Weighted RRF；
4. 按融合分数降序排列；
5. 截取最终 `top_k`，例如最终 Top 10。

不能只取交集，因为这会直接丢掉“单路特别强”的正确结果。例如“红色雨伞在哪个镜头”可能只在 Vision 命中，Transcript 和 Semantic 都没有相关文字；如果取交集，正确结果会被提前删掉。

## Transcript 查询为什么必须使用 ASR 原文语言

Transcript 通道比较的是规范化后的连续字符，而不是跨语言语义。日语 ASR 中保存的是「お前、灯をバンドに誘ったの？」时，中文“是你邀请灯加入乐队的吗”不会因为意思相近而命中；语言翻译属于 Semantic 能力，不属于词法匹配。

Series 的 `transcriptLanguageHint`（例如 `ja`）只提醒 Query Agent 应采用哪种语言和脚本。若用户没有提供真实原文，可靠流程是：

```text
中文释义问题
  → Semantic-heavy 召回相关片段
  → 读取 evidence.transcript 中的真实日语 cue
  → 复制有区分度的连续原文
  → Transcript-heavy 精确复查
```

语言提示不能证明某句台词存在，模型自行翻译出的日语也不能作为答案中的“原话”证据。未配置提示时，不应根据作品名称或文件名猜测语言。

可以把 RRF 直观理解为：

> 各召回通道分别提名候选人，所有被提名者进入候选池；同一候选在多个榜单中获得名次票，最后累计票数并重新排名。

## 6. `rrf_k`、`channel_top_k` 和最终 `top_k` 不是一件事

### 6.1 `rrf_k`

RRF 公式中的平滑常数，当前默认为 `60`。

- `rrf_k` 越小，同一榜单内第 1 名相对第 20 名的优势越大；
- `rrf_k` 越大，同一榜单内名次差距越平，“进入了多少个榜单”相对更重要。

因此，准确的说法是：

> 较大的 `rrf_k` 会压平同一榜单中头部与尾部候选的贡献差距，因此多通道均入榜的候选更容易胜过只在单通道高排名的候选。

例如在两路等权且 `rrf_k=60` 时：

```text
Chunk C：Semantic 第 1，Vision 未入榜
score = 1 / 61 = 0.016393

Chunk D：Semantic 第 20，Vision 第 20
score = 1 / 80 + 1 / 80 = 0.025000
```

Chunk D 会超过 Chunk C，表明当前参数对跨通道共识有明显奖励。

### 6.2 `channel_top_k`

表示每个通道最多提名多少候选。当前默认是每路 20 个。

如果某个 Chunk 没有进入任何通道的 `channel_top_k`，它就不在候选并集中，后续无论怎样调权都无法救回它。

### 6.3 最终 `top_k`

表示所有候选完成 RRF 融合排序后，最终返回多少个结果。它只负责最后截断，不改变前面的向量匹配和融合计算。

## 7. 当前三通道策略

当前策略可以表示为：

```text
Transcript/ASR 排行榜 ──┐
Pro Semantic 排行榜  ──┼─→ Weighted RRF ─→ 最终排行榜
Video Vision 排行榜   ──┘
```

三个通道的职责是：

| 通道 | 主要擅长 | 建议的底层召回 |
|---|---|---|
| Transcript / ASR | 原台词、短句、专有名词、明确措辞 | cue 级规范化连续短语匹配，再聚合到 Chunk |
| Pro Semantic | 剧情、因果、人物关系、事件发展 | Pro 解析文本的 dense 向量召回 |
| Video Vision | 镜头、动作、地点、物体、颜色、构图 | 视频多模态 dense 向量召回 |

Transcript 通道没有把一分钟 ASR 拼成一个长文本向量，因为短台词容易被整个片段语义稀释。当前先做 cue 级词法匹配，再按 `videoChunkId` 聚合；未来可增加专用 ngram/BM25 或 cue dense 候选，但不应把近义匹配伪称为原话命中。

Query Agent 直接提交连续权重；下面只是常见问题的冷启动参考比例，不是固定 Profile 枚举：

| 问题类型 | Transcript | Semantic | Vision |
|---|---:|---:|---:|
| 原话/台词 | 0.70 | 0.20 | 0.10 |
| 剧情/语义 | 0.15 | 0.70 | 0.15 |
| 画面/镜头 | 0.10 | 0.20 | 0.70 |
| 均衡 | 0.34 | 0.33 | 0.33 |

这些只是冷启动值，最终应使用分类评测集调教，而不是把经验值当成永久默认值。

## 8. 这是 Hybrid Retrieval 吗

### 8.1 当前实现

**是 Hybrid Retrieval**。当前同时使用确定性词法召回和两种 dense/multimodal vector retrieval，再做后融合。更精确的名称是：

> **Agent-Routed Query-Adaptive Lexical + Multimodal Hybrid Retrieval with Weighted RRF**
>
> Agent 驱动、基于 Weighted RRF 的词法 + 多模态查询自适应混合召回

在搜索领域的狭义用法中，“Hybrid Search”经常特指 **BM25/关键词稀疏检索 + dense 向量检索**。Seekino 当前 Transcript 是应用层的规范化连续短语匹配，不是 BM25，但已经是真实独立的 lexical channel；因此“词法 + 多模态向量混合召回”是准确表述，不能说成“三向量融合”。

### 8.2 当前不包含精排

Weighted RRF 只负责合并初召回排名。Query Agent 会从真实候选中选择证据，Evaluator 会审核草稿是否被证据支持，但两者都没有实现一个对固定候选池统一打相关性分的 Cross-Encoder/LLM Reranker。因此当前流程没有严格意义上的“精排”。若以后增加，合理位置是三路 RRF 之后、最终候选截断与回答 Agent 之前。

## 9. 当前实现与后续增强对照

| 能力 | 当前实现 | 后续增强 |
|---|---|---|
| 召回通道 | Transcript + Pro Semantic + Video Vision | 保持三路，增强各路候选质量 |
| 原始 ASR | cue 级规范化连续短语匹配；Scope 内 JSON 线性扫描 | 独立 cue 表、ngram/BM25 候选，可选 cue dense |
| 权重 | Query Agent 每次提交三路连续权重，Server 归一化 | 用 held-out 评测校准 Skill 参考比例 |
| 融合 | 三路 Query-Adaptive Weighted RRF | 可评估 learned fusion 或校准分数融合 |
| Agent 参数 | `query`、`top_k`、三路 `channel_weights` | 如有证据再考虑独立 `transcript_query` |
| 候选池 | 三路 Top K 并集 | RRF 后可选独立 reranker |

## 10. 最容易混淆的结论

1. 多个通道的候选取并集，不是交集。
2. RRF 融合的是名次，不是向量，也不是直接相加原始 cosine score。
3. 通道权重只改变最终投票力度，不改变单通道向量相似度和原始排名。
4. `rrf_k` 越大，单榜内名次差距越平，多通道共识的相对作用越明显。
5. `channel_top_k` 决定谁能进候选池，最终 `top_k` 决定融合后录取谁。
6. Text 通道是 Pro Semantic；原始 ASR 使用独立的 Transcript 词法排行榜。
7. Query Agent 可以改写 Query、多次检索，并为每次调用动态设置 Transcript/Semantic/Visual 权重。
8. 融合分数只用于同一次召回内部排序，不是相关概率或置信度。
9. 当前没有 reranker；RRF、Agent 选证据和 Evaluator 审核分别是融合、选择与审核，不能混称精排。
