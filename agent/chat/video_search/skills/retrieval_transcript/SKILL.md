# Video Retrieval Skill

`retrieve` 和已注册的 `search_video_events` 直接返回有界真实证据。证据充分即可使用；截断或缺少必要上下文时，再用 `read_retrieval_evidence` 按片段 ID 和游标续读。

默认采用贪心原则：具体台词、动作、情绪或局部事件优先用 `search_video_events`，不先粗搜 Chunk；台词保留用户给出的原话或译文短语。Event 工具不可用、索引未覆盖、候选不足或需要更广上下文时，再用 `retrieve`。命中后先读已有 Chunk 的必要上下文；证据充分即结束，不为保险重复检索或展开全部材料。未覆盖或部分覆盖不能解释成没有匹配。

## retrieve

`retrieve` 在当前固定 Series Scope 内检索真实视频证据，返回来源证据摘录、排序信号、稳定 `video_chunk_id`、canonical 时间和分页游标。

- `query`：可独立理解的检索语句。写入人物、对白含义、事件、动作、物体、场景或画面约束。
- `top_k`：由你决定本次最多召回多少个候选，必须是正整数。通常从 8–10 条压缩卡片开始；问题明确时可调小，人物、事件或时间表达不确定时可扩大。这是检索策略建议，不是固定预算；工具按你传入的数量检索，不受旧消息或全局候选预算裁剪。
- `channel_weights.transcript`：ASR 原话词法匹配。原句、短语、专名或口头禅应提高它。
- `channel_weights.semantic`：剧情、因果、人物关系、事件和对白语义检索。
- `channel_weights.visual`：可见动作、物体、颜色、服装、地点和构图检索。
- `cursor`：同一次召回的续页游标。存在 `next_cursor` 时，继续调用同一个 `retrieve` 并只传该游标；不要同时重传初始检索参数。

Skill 必须保持作品无关：不得写入或依赖任何具体动画、角色、集数、剧情、台词或镜头特例。初始调用示例只能使用抽象占位描述：

```json
{
  "query": "目标人物在目标地点完成目标事件",
  "top_k": 8,
  "channel_weights": {
    "transcript": 0.1,
    "semantic": 0.7,
    "visual": 0.2
  }
}
```

三个权重都必须是 `0` 到 `1` 的有限数字，且至少一个大于 `0`。Retrieve Tool 会调用 Engine 策略归一化权重；权重为 `0` 会禁用对应通道。

Catalog 中常用字段：

- `requested_top_k`：本次工具调用请求的候选数量，也是实际传给检索服务的数量。
- `result_count`：本次检索实际找到的候选总数；少于 `requested_top_k` 表示返回的候选不足该数量，不代表已穷尽作品中的相关证据。结合 `partial`、`warnings` 判断检索是否完整。
- `items`、`total`：当前页的压缩卡片和本次检索的候选总数。单页条数或字节限制只影响分页，不减少候选总数。
- `video_chunk_id`：候选稳定 ID，后续读取和最终选择都使用它。
- `description`：仅供筛选的短描述，不足以独立支撑事实结论。
- `evidence`：来自正式 Evidence 的原文摘录；`evidence_truncated` 表示还有未展示内容，不能把摘录之外的事实视为已核验。
- `episode_order`、`start_seconds`、`end_seconds`：候选所在集数与 canonical Chunk 范围。
- `fused_rank`、各通道 rank/score：排序与诊断信号，不是事实或置信度百分比。
- `next_cursor`：Catalog 下一页；为空表示本次召回已读完。

## read_retrieval_evidence

只接受当前真实检索发现的 `video_chunk_id`。首次读取只传 ID，续读传相同 ID 和返回的 `next_cursor`；不填写 section、字符范围或 revision。只读取当前结论所需的上下文。搜索分页的 cursor 和证据续读的 cursor 分属各自工具，不可混用。


## Transcript 语言

Transcript 通道不会翻译 Query。`inputs.series_context[].transcriptLanguageHint` 是用户配置的原文语言提示，只用于检索路由，不证明某句台词存在，也不决定最终回答语言。

- 用户给出原文时，保留原语言和原脚本，抽取连续短语进行 Transcript-heavy 检索。
- 用户只给出译文或剧情释义时，先做 Semantic-heavy 检索；从精读结果复制真实原语言短语，再按需做 Transcript-heavy 复查。
- 没有语言提示时，不得从作品名或文件名臆断语言。

## 检索与精读策略

- 原话、短句、专名：Transcript-heavy，例如 `0.8 / 0.15 / 0.05`。
- 剧情、因果、人物关系：Semantic-heavy，例如 `0.1 / 0.75 / 0.15`。
- 可见动作、物体、地点或构图：Visual-heavy，例如 `0.05 / 0.15 / 0.8`。
- 同时核验对白、语义和画面：使用较均衡权重，必要时拆成两次独立检索。

数值只是示例。同一个 `query` 同时服务三个通道；自然语言语义和原话短语冲突时，应拆成不同调用。候选在所有启用通道中都未进入候选深度时，仅提高权重不能将它救回。

## Evidence 核验结果

检索结果中的原始证据或后续读取的证据用于核验。需要形成播放候选时，应遵守：

- `video_chunk_id` 必须来自本轮或此前真实召回；
- `start_seconds`、`end_seconds` 必须位于精读证据的 canonical 范围内；
- 范围要完整覆盖支撑结论的事件、动作或对白，同时去掉明显无关的前后内容；
- 对该片段的事实描述必须准确说明 Evidence 实际支持什么，不得把推测写成事实。

不要截断一句对白或连续动作，也不要为了追求短而移除理解结论所需的上下文。如何报告和提交最终选择由当前角色自己的 Skill 和已注册 Tool 决定；不得调用当前角色未注册的 Tool。


## Event 细节检索

`search_video_events` 是具体线索的默认起点，无需先 retrieve；已有可靠 Chunk 线索时可用 `video_chunk_ids` 缩小 Event 搜索范围。参数为 `query`、`top_k`、`channel_weights`、可选 `event_types`、`speaker`、`video_chunk_ids` 和 `cursor`。Event 的权重键始终是 `lexical / semantic / visual`，不使用 `transcript`。

- `event_types` 不传表示不限，按工具 schema 的事件类型筛选。`speaker` 是可靠对白说话人，不是出镜人物；只传 speaker 会限定 DIALOGUE，显式同时选择其他类型属于参数冲突。查人物出现或关系时应写入 query，不冒充 speaker。
- `top_k` 和 `result_count` 计数不同 Chunk 组；`event_count` 是命中 Event 数。每张卡包含一个 `matched_event`，同一 Chunk 可以出现多张卡；分页 `total` 是 Event 卡数量。存在 `next_cursor` 时继续调用同一 Event 工具且只传 cursor。
- Event 卡提供原始 Timeline 下标、类型、绝对毫秒时间、说话人、字段/语言/原文或译文角色与通道排名。卡片的 `event_evidence` 是权威 Timeline 对应事件的原文，命中字段和排名是筛选线索；事件原文截断或需要更广上下文时，使用 `read_retrieval_evidence`。
- `EVENT_INDEX_NOT_READY` 表示当前范围尚未覆盖索引，可改用 retrieve；不能把未覆盖当作没有匹配。
- Event 视觉索引使用离线抽帧向量，不代表你已看图。视觉主张继续调用 `inspect_video_frames` 核验；稀疏帧也不证明连续动作或声音强度。
