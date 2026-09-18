# Video Retrieval Skill

使用 `search_video_chunk_evidence` 在当前固定 Series Scope 内检索真实视频证据。当前有三个独立排行榜：Transcript/ASR 原话词法匹配、Pro Semantic 语义向量和 Video Vision 视觉向量。

## 输入字段

- `query`：可独立理解的检索语句。写入相关人物、对白含义、事件、动作、物体、场景或画面约束，不要把工具指令写进该字段。
- `top_k`：本次调用期望返回的最大融合候选数。明确、窄范围的问题使用较小值；人物、事件或时间表达不确定时可适当增大。Server 会根据当前回答与 Runtime 上限继续收紧。
- `channel_weights.transcript`：原始 ASR 连续原话的词法匹配排行榜。原话、短句、专名、口头禅或明确措辞问题应提高它。它会做 Unicode、大小写、空白和标点规范化，并支持一句话跨相邻 cue；它不理解近义词，也不能仅凭 ASR 判断说话人。
- `channel_weights.semantic`：Pro Semantic 排行榜的相对影响力。该通道来自 Pro 解析的片段摘要、事件和已确认对白含义。剧情、因果、人物关系、事件和对白语义问题应提高它。
- `channel_weights.visual`：真实 Video Vision 排行榜的相对影响力。可见动作、物体、颜色、服装、地点、构图和镜头外观问题应提高它。

三个权重都必须是 `0` 到 `1` 的有限数字，且至少一个大于 `0`。Server 会将权重和归一化为 `1`，因此真正改变 Weighted RRF 顺序的是权重比例。权重为 `0` 会禁用并跳过对应通道，而不是先查询再忽略结果。

一次完整调用示例：

```json
{
  "query": "丰川祥子在练习室宣布退出乐队",
  "top_k": 8,
  "channel_weights": {
    "transcript": 0.1,
    "semantic": 0.7,
    "visual": 0.2
  }
}
```

## Transcript 语言

Transcript 是对已保存 ASR 原文的词法匹配，通道自身不会翻译 Query。`inputs.series_context[].transcriptLanguageHint` 是用户为 Series 配置的原文语言提示（BCP-47，例如日语 `ja`），只用于选择检索语言；它不证明某句台词真实存在，也不要求最终回答使用同一种语言。

- 用户给出原文时，保留原语言和原脚本，抽取连续短语进行 Transcript-heavy 检索。
- 用户只给出译文或剧情释义时，不要把自行翻译的句子冒充原台词。先用用户语言做 Semantic-heavy 检索，从返回的 `evidence.transcript` 复制真实原语言短语，再做 Transcript-heavy 精确复查。
- 没有语言提示时，不得从 Series 名称、文件名或作品常识断言语言；仍可执行上述“Semantic 召回 → 真实 cue 复查”流程。
- Scope 中存在多个语言提示时，按各语言分别组织必要的 Transcript 调用。日语 ASR 必须使用日语原文而不是中文译意。

## 权重选择

- 原话、短句、专名或明确措辞：使用 Transcript-heavy，例如 `transcript=0.8`、`semantic=0.15`、`visual=0.05`，并把 `query` 改写为与已保存 ASR 相同语言的原句或有区分度的连续短语，不要传整段问题。
- 剧情、事件、人物关系或对白含义：先尝试 Semantic-heavy，例如 `transcript=0.1`、`semantic=0.75`、`visual=0.15`。
- 可见物体、动作、颜色、地点或构图：先尝试 Visual-heavy，例如 `transcript=0.05`、`semantic=0.15`、`visual=0.8`。
- 同时需要核验“说了什么、表达了什么、画面展示了什么”：使用较均衡比例，例如 `transcript=0.4`、`semantic=0.3`、`visual=0.3`。
- 同一个 `query` 同时服务三个通道。自然语言语义问题与原话短语冲突时，应在预算内调用两次工具：一次用原话短语和 Transcript-heavy 权重，一次用自然语言描述和 Semantic/Visual-heavy 权重。

以上数值只是示例，不是固定 Profile。应为当前 Query 自主选择连续权重；同一轮中的不同调用可以使用不同 Query 和权重。

## 融合行为

每个启用的通道独立检索，Server 对各通道候选取并集，再执行 Weighted Reciprocal Rank Fusion。权重只改变该通道名次对最终融合顺序的贡献，不会改变 Query Vector、cosine 相似度或通道内原始顺序。一个候选如果在所有通道中都超出候选深度，仅提高权重无法将它救回。

## 常用返回字段

- `retrievalStrategy.version`：本次融合策略版本，用于复现和比较实验。
- `retrievalStrategy.requestedWeights`：本次 Tool Call 传入的原始权重。
- `retrievalStrategy.effectiveWeights`：归一化后真正参与融合的权重。
- `resultCount`：本次调用实际返回的融合候选数量。
- `candidate.videoChunkId`：候选的稳定 ID，选择证据或提交视频窗口时必须使用。
- `candidate.fusedRank`：多通道融合后的最终名次。
- `candidate.fusedScore`：本次调用内部的 Weighted RRF 分数，不是概率或置信度百分比。
- `candidate.transcriptRank` 和 `candidate.transcriptScore`：ASR 原话词法匹配的名次和匹配分。它不是概率，也不是向量 cosine score。
- `candidate.textRank` 和 `candidate.textScore`：Pro Semantic 的名次和原始 cosine score。
- `candidate.visionRank` 和 `candidate.visionScore`：Video Vision 的名次和原始 cosine score。
- `evidence.episodeOrder`、`startMs`、`endMs`：所属集数与可播放的 canonical Chunk 时间范围。
- `evidence.transcript`：用于证据核验的 ASR cues；只有 `candidate.transcriptRank` 非空才表示该候选进入了 ASR 原话排行榜。
- `evidence.allTimeEvents`、`videoCompact`、`windowSummary`：用于核验结论的视觉事件与语义上下文。
- `selectionReason`：Server 生成的候选保留说明。必须与 evidence 交叉核验，不得将它当作新事实。
- `newResultCount`、`reusedResultCount`、`reusedHits`：仅在后续调用再次命中已返回的 VideoChunk 时出现。`hits` 只携带新 Chunk 的完整 evidence；`reusedHits` 只更新候选排序与摘要，完整 ASR 和 canonical 时间范围继续引用前一次 Tool 结果，不代表证据缺失。
- `partial` 和 `warnings`：检索完整性与限制，不得隐藏。

## 迭代策略

当清晰 Query 和一组权重已经产生充分证据时，一次调用即可。当结果较弱，或问题包含明确分离的语义与画面部分时，可在预算内用改写后的独立 Query 和有意调整的权重比例再检索。只能选择当前或已记录检索调用真实返回的 `videoChunkId`。不得更改 Workspace、Series Scope、`rrf_k`、单通道候选深度、collection、ANN/exact 模式或索引版本。

## 最终片段范围

retrieve 只召回 canonical VideoChunk，不根据最终播放小区间再次检索或裁剪。最终草稿使用 `selected_video_ranges` 选择证据，每项包含真实的 `video_chunk_id`，以及根据 ASR 时间线、事件或视觉证据提出的原视频绝对 `start_ms` 和 `end_ms`。这个小区间是播放建议，不参与 retrieve 是否成立。

时间范围应满足对应候选的 `evidence.startMs <= start_ms < end_ms <= evidence.endMs`，完整覆盖支撑结论的事件、动作或对白，并去掉无关前后内容。不得从一句对白或连续动作中间截断，也不得为了追求短而移除理解结论所需的上下文。Evaluator 会审核范围边界、完整性和精确性；范围不合理时通过 advice 交给 Query 修订，不把它升级成 retrieval 失败。Chat 使用 `submit_video_item` 显式提交获批的 `video_chunk_id`、`start_ms`、`end_ms` 和 `claim`；Server 对异常提交降级并告警，不让可恢复的播放范围问题破坏文字回答。
