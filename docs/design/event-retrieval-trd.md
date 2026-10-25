# Event 细粒度检索：当前合同与历史方案

修订：2026-10-17。当前唯一实施依据为 [Seekino Sub-Chunk / Event 检索最终 TRD](sub-chunk-trd.md)。实施记录与运行索引覆盖分别核对，不以设计说明代替运行验收。

最终合同取代本文件早期的 MySQL Event 表、仅词法细搜和 Event 向量另行评测方案：

- MySQL `ChunkParseContent.content.timeline` 保持完整、有序的唯一权威事实；不新增 Event / Frame SQL 表、Frame Index 或 Event 队列。
- 复用现有 Chunk INDEX 阶段，从正式 Timeline 派生独立 Event ES 索引。每 Event 一条文本向量和一条抽帧视觉向量，不生成 VLM caption；Chunk 索引合同保持不变。
- Event 身份绑定 `(parse_task_id, video_chunk_id, version, timeline_index)`；version 复制 Chunk 正文版本，原始下标不重编。固定物理索引的完整投影/编码/采样合同由 `_meta` 验收，不另建 Event 业务版本。
- 首次进入 INDEX 在现有 VideoParseTask.detail 冻结有效 Event 目标；不改变 ParseConfig。均匀中点、1 fps、最多 3 帧和 `l2-mean-v1` 是本轮基线，实际帧 PTS 未取得时保留 null。
- 新 Event 目标的视觉尺寸复用当前服务统一媒体 max-height 配置，查询与 INDEX 使用同一合同，不从旧任务的 `visual_input.max_height` 另取配置；已绑定目标继续沿用其冻结值，原任务与 Chunk ParseConfig 保持不变。
- Server 按可信来源元组过滤所有通道，Event 融合后批量回源现有 MySQL 关系，核对同一版本的原始 Timeline，再返回 Chunk Evidence；speaker 只筛可靠 DIALOGUE 说话人。
- Query / Evaluator 可直接 `search_video_events(query, event_types?, speaker?, video_chunk_ids?, top_k, channel_weights, cursor?)`，也可先粗搜再细搜。top_k 按不同 Chunk 组计数，分页卡按 Event 展示，不新增 Canvas 集合。视觉主张仍需 inspect_video_frames 核验。
- 完整成功/失败调用、有效筛选、候选与分数、截断及回源原因保留业务 trace；模型只收到预算内摘要。未覆盖不是零命中，可继续 Chunk 检索；失败调用不能满足首轮真实检索。
- 旧 COMPLETE 无 Event receipt 表示未覆盖，回填复用原 INDEX，不重跑 Parse/Refine、不提高正文版本；零 Event、部分发布、旧租约、删除与恢复均按最终 TRD 验收。

以下保留早期提案和当时的只读统计，用于追溯决策；其中的 SQL Event 表、独立 Event 版本及“首版仅词法”不再是有效开发指令。

<details>
<summary>已由 2026-10-17 最终合同替代的早期方案（历史记录）</summary>

# Event 细粒度检索 TRD

修订：2026-10-17，v1。状态：已确认的设计方向与实施合同，待实现；本文不表示 Event 表、ES 索引或 Agent 工具已经接通。具体表名与工具名是工作名，实施时沿用项目命名惯例。

本文集中维护 Event 持久化、投影同步和检索工具的跨层合同。[Engine TRD](video-parse-engine-trd.md) 维护解析与算法边界，[Parse Agent TRD](video-parse-harness-trd.md) 维护任务、租约和恢复，[Chat 交付合同](agent-chat-delivery-contract.md) 维护业务工具与答案交付。[视觉媒体检索 TRD](visual-retrieval-trd.md) 维护媒体输入；输入模态与检索粒度分别演进。

## 1. 问题与本次目标

Chunk 综合文字向量用于匹配整体事件和上下文；短台词、局部动作等细节可能被整块内容稀释。现有词法索引虽保留事件来源，候选预算及后续融合仍按 Chunk 组织，不能把“有词法命中”等同于最终一定返回对应 Chunk。

本次增加可直接搜索的 Event 池，让 Agent 按事件类型、关键词和可选 Chunk 范围定位细节，再回到完整 Chunk 核验证据。不强制先粗召回 Chunk，也不以新增意图路由器为前置条件。现有 Chunk 词法、文字与视频向量检索继续服务整体语义问题。

2026-10-17 本地 MyGO 当前 COMPLETE 任务的只读样本：13 集、312 个已索引 Chunk，共 7,103 条 Timeline 条目，平均每 Chunk 22.77 条，中位数 23，范围 10–43。它们是按来源 Chunk 计数的条目，不代表跨 Chunk 语义去重后的事件；这组统计只用于估算本地存储规模，不能证明新工具的召回效果。

## 2. 权威事实、实体与身份

### 2.1 Chunk Timeline 保持权威

- `chunk_parse_content.content` 中完整、有序的 `VideoParseResult.timeline` 保持唯一权威事件事实；不从 Chunk 删除 Timeline，也不把它改成仅保存 Event ID 的引用数组。
- MySQL Event 行是持久化的检索投影，ES Event 文档是搜索投影。两者都能从当前正式 Chunk Timeline 重建，不提供独立编辑后反写 Timeline 的入口。
- Parse 的 `TimelineEvent` 输出不增加数据库主键、Chunk ID 或 `event_id`。业务检索的身份绑定由宿主根据正式来源完成。
- 保留权威数组的原始 `timeline_index`；筛选后不重新编号、不按类型重排。Dialogue 的原文与译文是同一 Event 的语言变体，不拆成多个逻辑 Event。

### 2.2 MySQL Event 模型草案

新增独立派生实体，工作名 `video_timeline_event`。表自身 PK 依项目合同使用 `id`，工具对外称 `event_id`；时间与审计字段沿用 [BaseModel 命名合同](base-model-field-names.md)。以下只列业务必需信息，不是可执行 DDL：

| 信息 | 字段或来源 | 消费目的 |
| --- | --- | --- |
| 版本内事件身份 | `id` | 幂等落库、命中定位与回查 |
| 正式来源 | `chunk_task_id` | 关联解析任务、固定 Chunk、Video 与授权归属 |
| 正文版本 | `content_version` | 与正式 Chunk 内容一致性校验 |
| 原始位置 | `timeline_index` | 精确定位权威 Timeline 条目 |
| 类型与事件时间 | `event_type`、`start_ms`、`end_ms` | 类型过滤、事件定位及范围核验 |
| 单条事件内容 | `content`，包含该条事件的原始字段 | 投影对白变体、描述、人物或画面文字，保留匹配依据 |

唯一来源键为 `(chunk_task_id, content_version, timeline_index)`；`id` 由该键确定性生成，其身份等价于解析任务、来源 Chunk、正文版本与数组位置的组合。同版本重试使用同一 ID；Refine、Prefix 或重解析形成新版本时，不承诺语义相同的事件维持原 ID。不额外建立跨 Chunk 的全局语义身份。

首版只保留当前正式版本的有效投影；新版本集合替换、任务/视频删除清理均由 Repository 管理。完整历史事实的保留沿用现有解析成果策略，本表不新增一套版本历史系统。来源关系和唯一键必须受约束，不能允许调用方任意拼接不相属的任务与 Chunk。

### 2.3 时间与 Chunk 归属

当前 Parse Schema 的 `start_seconds / end_seconds` 实际为原视频绝对整数毫秒；新检索合同使用 `start_ms / end_ms`，不做乘以 1,000 的转换。区间为半开区间 `[start_ms, end_ms)`。

正式内容可能包含邻域背景。与现有 Chunk 投影一致，只将与 canonical Chunk 区间相交的 Timeline 条目放入该 Chunk 的可检索 Event 集合：`event.start < chunk.end && event.end > chunk.start`。保留原始数组位置与完整事件时间，不裁剪或改写权威事实。没有交集的邻段内容仍保存在完整 Timeline 中，不能冒充本 Chunk 发生的事件。

同一跨界事件可能存在于不同来源 Chunk 的投影中，返回时保留来源身份；首版不承诺语义去重。命中时间是定位线索，最终播放范围仍通过既有 Chunk/视频证据边界校验。

## 3. Refine 后的建立与同步

“Refine 后建立 Event”落在现有 Chunk `INDEX` 阶段，不在 Agent 的模型/工具回调里直接写库，不新增 EventTask、EventQueue 或 Event 同步状态机。

```text
Interval Refine 正式提交
  → 同事务检查各成员：全部覆盖该 Chunk 的 Refine 已提交
  → 同一 ChunkTask 进入 INDEX / QUEUED
  → Index Coordinator 领取租约，读取当前正文、版本及目标 generation
  → Engine 纯函数生成 Chunk 投影与 Event 集合
  → Server 在租约及来源版本校验下幂等保存 MySQL Event 集合
  → 同步 Chunk ES 文档与 Event ES 集合，核验逐项成功及查询可见性
  → 再次验证来源版本、租约、generation 与预期集合，提交 INDEX / COMPLETE
```

权威成果及阶段推进仍由既有事务保证；通知仅加速唤醒，DB Queue 扫描独立保证中断恢复。MySQL 与 ES 不做跨库事务：Event 落库或 ES 部分成功后失败，由同一 Index 阶段幂等恢复，不重跑已成功的 Understanding / Refine。

同步必须满足：

1. Event MySQL 集合替换与完成提交都在事务内核对当前 Chunk 来源版本、Index 租约及目标 generation；失去执行权的旧 Worker 不能提交新成果或完成状态。
2. ES Event 文档携带来源任务、Chunk、正文版本、投影合同与目标 generation；权限范围由 Server 可信来源注入。写入和清理都要防止旧 Worker 覆盖或删除新版本；清理限定到明确的来源及过期版本，不对 Chunk 无条件宽泛删除。
3. 核验当前版本预期的 Event ID 集合与内容指纹、必需字段、查询可见性，并处理旧版本残留；只比较数量或只看 bulk HTTP 成功都不足以判定完整。跨库操作期间的新旧混合由查询回源校验拒绝。
4. 零条可检索 Event 是合法结果。复用现有 Index receipt/checkpoint 承载来源版本、投影合同、集合摘要/数量的完成证明；空集合也要验证并清理旧投影，不能用“查不到行”区分未执行和已完成。新版本从非空变空须能重试收敛。
5. Chunk ES 已存在同版时也必须核验并补齐 Event MySQL/ES 集合；当前仅检查 Chunk 的复用分支不能直接认定新增 Event 能力已完成。
6. Prefix 或后续 Refine 实际改写正式内容时递增正文版本，沿既有失效/重排 Index 路径同步重建 Event。仅 `scope` 扩大、Event 回填或索引策略更新不伪造事实变化，不递增 `content_version`。

Index 的目标交付增加 Event 投影后，`INDEX / COMPLETE` 必须同时证明该代次要求的 Chunk 与 Event 表示完整。沿用当前选定 Video 解析任务 COMPLETE 的对外开放规则，不因新增 Event 擅自开放部分解析结果。

## 4. Event 检索算法与工具

### 4.1 首版检索

直接在 Event 池内查询，先应用可信任务/范围及可选事件类型、Chunk 限制，再按事件内容做词法匹配。`event_types` 可选且支持多选；不传表示不限类型，不能要求 Agent 先猜中唯一事件类型。

检索字段来自真实事件：描述、人物、Dialogue 原文/译文及 OnScreenText 文字。对白匹配保留 `language / role / speaker` 与所属 Event 关系，不把不同 Event 的人物和台词拼成一个命中；生成问题或背景摘要不能伪装成原台词。

首版不强制新增 Event 向量或替换 embedding 模型。词法的分词、短语/关键词策略及候选预算由 Engine 合同和既有配置机制维护，ES adapter 落实查询；避免 Agent、Service 与 ES 各藏一份权重或默认值。综合 Chunk 向量保留；Event 语义向量、本地 embedding 模型和混合排序属于后续独立评测项。

按 Event 评分后按 canonical Chunk 归组返回，保留真正命中的条目；同一 Chunk 多语言变体不应靠重复计数挤占全部返回预算。首版工具的 `top_k` 指最多返回的 Chunk 组数，组内匹配条目有独立的服务端配置预算，并在截断时说明结果不完整。Event 排名不直接并入现有 Chunk 三路 RRF，先让 Agent 自主组合两个工具的结果。

### 4.2 Agent 工具合同

工作名与最小参数示意，尚未注册生产工具：

```text
search_video_events(
    query="真是高高在上 / 居高临下",
    event_types=["DIALOGUE"],     # 可省略或多选
    video_chunk_ids=[...],         # 可省略，直接搜索当前授权范围
    top_k=...
)
```

Tenant、Series/Video 的实际授权 Scope 和可检索任务由 Server 从本轮上下文注入；模型传入的 Chunk 列表只能缩小范围。可选 Video 子范围若后续暴露，同样必须与授权范围取交集。非法/越权身份不得被当作宽范围查询。

每个 Chunk 组至少提供 `video_chunk_id` 与 `matches`；每条 match 提供 `event_id`、`timeline_index`、`event_type`、`start_ms / end_ms`、实际匹配文本及其字段/语言来源。后端内部结果还必须携带解析任务、正文版本及投影/索引身份供一致性回查，不能仅靠 Chunk ID 绑定事件。回源校验在结果交给模型前完成。

不只返回裸 Event ID 或 Chunk ID，否则 Agent 仍不知道为何命中、是否满足原话/人物要求。模型可收到预算内的简明命中片段，完整候选、筛选和打分过程保留在后端 trace；展示摘要不替代全量排障记录。

### 4.3 Query / Evaluator 的消费

- Query 可直接细搜、先粗搜后限制 Chunk 细搜，或在失败后放宽类型/关键词；没有固定的粗到细必经顺序。已有“必须检索”的规则应认可真实 Event 检索调用，不能仍只识别旧工具名。
- 命中用于选择精读对象，人物关系、说话人、前后文和播放范围仍从正式 Chunk Timeline 核验；视觉结论继续使用已有画面检查工具。相关性分数不等于事实证据。
- Evaluator 可使用同一业务检索能力补查，并核对草稿结论与实际命中/正式来源是否一致；不能把 Query 的推测背景当成已经证实的前提。
- Event 候选通过 Chat 业务适配层关联已有 Chunk Evidence，最终继续提交已核验的视频片段。通用 Canvas 仅承载通用内容与引用，不引入 Event ORM、索引同步、召回策略或身份生成规则，也不修改其设计。
- 实际工具调用沿既有消息展示、模型上下文与 trace 通道分别处理；模型上下文的工具结果、前端展示和后端 trace 不因新增工具而混成同一载荷。

工具扩展不要求新增 HTTP endpoint、前端结果 DTO 或数据库消息表。若实施时确需公开 Event 级 UI，再单独设计消费合同。

## 5. 分层与依赖

| 层 | 本次新增职责 | 依赖边界 |
| --- | --- | --- |
| Server Model / Repository | Event 派生行、来源关联、版本约束、幂等替换、删除/回填 | MySQL 事务和租约在此执行 |
| Server Service / Index Coordinator / ES adapter | 装配可信快照与 Scope，编排 Chunk/Event 同步、检索、回源和 trace | ES 客户端、mapping、查询 DSL 与 IO 留在 Server |
| Engine | Event 投影、检索输入输出合同、匹配/排序/归组策略与证据规则 | 接收宿主身份及数据，不依赖 ORM、ES SDK、HTTP 或 Agent |
| Agent 业务工具与 Query / Evaluator | 注册工具、组织推测关键词、读取候选并核验 | 消费注入的业务检索接口；通用 utils 不反向依赖检索业务 |

投影规则与结果预算各自只有所属层的一个有效来源。Agent 不另行扫描数据库生成 Event，Server 不通过隐藏的生成模型调用替 Agent 猜测意图。

## 6. 版本、回填与实施边界

- Event 的 MySQL 派生模型、ES mapping、投影及检索算法建立各自首版合同。实际常量名和索引物理名在实施时确定，本次不改运行配置。
- 当前 Chunk ES `anivag-video-chunk-es-6`、Chunk 投影 `parse-v2-chunk-index-v1`、来源 `parse-v2-video-result-v1`、Chunk RRF `query-adaptive-three-channel-rrf-v2` 不因新增 Event 自动升级；只有相应结构或语义改变时才提升对应合同。不把 Parse V2 整体改名为 V3。
- 部署需新增 Event 表的增量迁移，并为现有正式 Chunk 执行受控 Index 重建/回填；保留完整 Timeline 和已有消息。历史 SQL、数据库实测快照及检索报告不改写成“当时已有 Event”。本文不生成或执行迁移。
- 回填从当前正式版本生成，复用成功的 Parse/Refine，不调用模型重新解析。即使 Chunk ES 可复用，也须验证 Event 集合；旧任务的 COMPLETE 不能直接当作新 Event 能力就绪。
- Event 纳入必需索引交付时，通过现有目标 generation/兼容合同及受控 Index 重排识别新要求；不得修改不可变任务配置后悄悄声称旧完成凭据满足新要求。具体升级入口在实施前与现有 rebuild 合同对齐，不能靠增加正文版本掩盖覆盖缺口。
- 新工具仅搜索已完成 Event 回填且满足来源/合同校验的数据；尚未具备细搜能力时返回明确的不可用信息，既有 Chunk 检索可继续使用，不能把未建索引伪装成“没有相关台词”。
- 若本次实际查询范围（应用可选 Chunk 子范围后）仅部分完成 Event 回填，首版细搜整次返回明确不可用并说明缺失范围，由 Agent 继续使用已有 Chunk 检索；不得静默缩小范围，也不能据此对未覆盖内容作否定性结论。

## 7. 验收与未决事项

实现必须覆盖：

1. 同一正式版本重复投影/重试，ID 和集合一致；保持原始下标、多语变体关系和毫秒时间。跨边界与邻域背景按交集规则处理，Chunk Timeline 完整不变。
2. 多个覆盖窗口仅部分 Refine 完成时不提前索引；全部提交后才生成可发布 Event；Index 失败不重跑 Refine。
3. Prefix 改写、旧租约迟到、ES 部分成功/超时、空集合及删除场景能够恢复；旧 Worker 不误删新版，不能混用不同任务或内容版本的事实。
4. 存量 Chunk ES 已就绪但无 Event 的复用路径能补齐；回填不提高正文版本，完成判定核验预期集合而非只数行。
5. 直接 Event 搜索、限制 Chunk 搜索、多类型/不限类型及越权请求都有明确行为；部分范围未回填不能静默漏搜，重复译文不挤占 Chunk 返回预算。
6. 用 MyGO“高高在上／居高临下”及原话、动作、情绪等样例，对比现有 Chunk 检索与 Event 词法召回的命中位置、遗漏、误召、耗时及后续核验。词面不同可能仍漏召，不能预先宣称词法已解决同义表达问题。
7. Trace 可追溯查询参数、有效 Scope、过滤及候选数量、命中 Event/Chunk、排序与截断、回源剔除原因，以及 Query/Evaluator 的实际工具读取；最终仍交付符合现有合同的可播放片段。

实施前需确定的具体项：新表/工具最终命名、ES 分析器及短语/关键词策略、集合摘要规范与预算默认值、现有任务 generation 的升级/回填入口。它们按所属层配置与既有重建机制收敛，不为这些细节另建队列、发布表或业务状态机。Event 向量是否值得增加，由首版真实漏例与成本评测决定。


</details>
