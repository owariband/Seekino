# Video Retrieval Skill

检索采用严格的两层读取协议：先用 `retrieve` 获得有界 Catalog，再只对确有价值的候选调用 `read_trieve_item` 精读。不要假设 Catalog 已包含完整证据，也不要为了“保险”把全部候选都展开。

## 第一层：retrieve

`retrieve` 在当前固定 Series Scope 内检索真实视频证据，只返回候选描述、排序信号、稳定 `video_chunk_id` 和分页游标，不返回完整 Transcript、视觉事件或语义上下文。

- `query`：可独立理解的检索语句。写入人物、对白含义、事件、动作、物体、场景或画面约束。
- `top_k`：本次最多召回多少个候选。问题明确时使用较小值；人物、事件或时间表达不确定时再扩大。
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

- `video_chunk_id`：候选稳定 ID，后续精读和草稿修改都使用它。
- `description`：仅供筛选的短描述，不足以独立支撑事实结论。
- `episode_order`、`start_seconds`、`end_seconds`：候选所在集数与 canonical Chunk 范围。
- `fused_rank`、各通道 rank/score：排序与诊断信号，不是事实或置信度百分比。
- `next_cursor`：Catalog 下一页；为空表示本次召回已读完。

## 第二层：read_trieve_item

对 Catalog 中可能直接支撑答案的 `video_chunk_id` 调用 `read_trieve_item`。先用 `section="overview"` 查看可读 section 和长度，再用同一 `video_chunk_id`、所需 `section` 和起始位置读取内容，必要时指定结束位置。可读 section 为 `candidate`、`selection_reason`、`evidence.transcript`、`evidence.events`、`evidence.text`、`evidence.series_context`、`evidence.metadata`。Transcript、视觉事件、语义上下文、来源信息和 canonical 时间范围都应从相关 section 核验。

精读不使用 cursor。读取 `overview` 时只需传 `video_chunk_id` 和 `section="overview"`，可省略 `start`、`end`；它列出有限、互不重叠的 section 及字符长度。读取其他 section 时可指定字符半开范围 `[start, end)`，返回值包含 `total_chars`。若请求范围过大，按工具提示缩小范围并再次调用；需要更多内容时用相邻范围继续读。同一 ID 可以按显式范围反复读取，不依赖此前返回的书签。Catalog 的 `next_cursor` 仍只用于 `retrieve` 翻页。

要读到当前 section 末尾可省略 `end`；要读完整 section 时先用 `start=0` 且省略 `end`。如果工具返回 `RANGE_TOO_LARGE`，按 `suggested_end` 分段读取。显式填写 `end` 时必须满足 `start < end <=` 当前 `video_chunk_id`、当前 section 的 `total_chars`；不同片段的长度不能互用。

只允许读取当前 Workspace 中已经由 `retrieve` 发现的 ID。最终结论、claim 和播放范围必须来自精读后的证据，不能只根据 Catalog 描述决定。

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

`retrieve` 只发现 canonical VideoChunk，`read_trieve_item` 负责核验。需要形成播放候选时，应遵守：

- `video_chunk_id` 必须来自本轮或此前真实召回；
- `start_seconds`、`end_seconds` 必须位于精读证据的 canonical 范围内；
- 范围要完整覆盖支撑结论的事件、动作或对白，同时去掉明显无关的前后内容；
- 对该片段的事实描述必须准确说明 Evidence 实际支持什么，不得把推测写成事实。

不要截断一句对白或连续动作，也不要为了追求短而移除理解结论所需的上下文。如何保存或审核结果由当前角色自己的 Skill 和已注册 Tool 决定；不得调用当前角色未注册的 Tool。
