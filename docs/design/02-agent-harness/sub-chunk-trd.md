# Seekino Sub-Chunk / Event 检索 TRD

日期：2026-10-17；更新：2026-10-21。当前文本投影版本：v3。状态：代码、定向回归及真实 ES 多向量合同验证完成；MyGO 存量回填及全库效果实验尚未执行。

本文件记录本轮用户确认的最终合同，取代先前 Event 方案中的 MySQL Event 表、`source_content_id`、`publication_key`、独立 Event 版本以及仅词法检索的提案。实际实现进展和验证在文末记录。

## 1. 目标与边界

保留 Chunk 整体检索，增加直接搜索细节的 Event 池。Query / Evaluator 可直接细搜，也可先召回 Chunk 再限制范围细搜；不强制先粗后细，不新增意图路由模型。

- MySQL `ChunkParseContent.content.timeline` 仍是完整、有序的唯一权威事实。
- 不新增 Event / Frame SQL 表、Frame Index、Event 队列或任务状态机。
- 在现有 Chunk `INDEX` 阶段从已正式提交的 Timeline 派生 Event ES 文档。
- 每 Event 一个 text vector 集合、一条 vision vector；集合内独立编码各段文本，vision 来自抽帧 capture，不生成 VLM caption。
- 图片是租约临时文件，编码后清理；时间及输入摘要保留。
- 完整事件、时间、视频信息和上下文通过现有 MySQL 关系批量回源取得。
- 通用 Canvas 和业务 Canvas 均继续使用现有 Chunk Evidence，不承载索引算法或新增 Event 集合。

## 2. Event Index Schema

新增独立物理索引，当前开发环境默认名称为 `seekino_video_events_development_v3`。复用现有 ES client；原 Chunk 索引名称、mapping、正文与编码指纹不变。

Mapping 使用 `dynamic: strict`，字段如下：

| 字段 | ES 类型 | 合同 |
|---|---|---|
| `video_chunk_id` | keyword | 业务父 Chunk |
| `parse_task_id` | keyword | 父 Chunk 的解析来源，不是新增业务归属 |
| `version` | integer | 直接复制 `ChunkParseContent.version`，不独立递增 |
| `timeline_index` | integer | 权威 Timeline 原始数组下标，过滤后不重新编号 |
| `event_type` | keyword | 复用 SCENE / ACTION / DIALOGUE / EMOTION / RELATION_CHANGE / ON_SCREEN_TEXT / OTHER |
| `speaker` | keyword，可空 | 只从 DIALOGUE 的可靠 speaker 投影；不使用 participants / visible_persons 代替 |
| `lexical_fields` | nested | 仅 `field / raw_text / language / role`，不重复 speaker、时间或下标 |
| `semantic_vectors` | nested | 各项包含 source / language / role / vector；vector 为 dense_vector，维度取冻结文本 EncoderContract |
| `vision_vector` | dense_vector | 当前 Event 抽帧视觉表示，维度取冻结视觉 EncoderContract |
| `detail` | object，enabled:false | 本 Event 的抽帧记录及必要执行扩展信息，不参与搜索 |

`lexical_fields.field` 取 `event_description / person_names / dialogue / screen_text`；`raw_text` 沿用 CJK 分析合同。Dialogue 保留语言与 ORIGINAL / TRANSLATION，屏幕文字保留已有语言。文字、语言及 role 条件在同一 nested 记录内匹配。

每个 Event 先独立编码 `description`。DIALOGUE 再按 `text_variants` 原有顺序，逐条独立编码每个 ORIGINAL / TRANSLATION 的 `text`；没有译文时不生成译文。索引中 `source=description` 的 language / role 为 null，`source=dialogue` 保留实际语言及 role。所有元数据只标记向量来源，不拼入 embedding 文本。不同文本不合并、不求平均，原始 Timeline schema、description 含义和 text_variants 均保持原样。其他事件仍只编码 description，词法字段和视觉路径不变。输入构造属于固定投影合同 `timeline-event-index-v3`。

语义得分为 `max(cosine(query, vector_i))`，向量必须属于同一冻结文本 EncoderContract。ANN 查询 `semantic_vectors.vector`，由 ES nested kNN 按父 Event 返回不同候选、取最近子向量打分；精确路径在父级授权过滤内使用 nested `score_mode=max` 和子向量 `script_score`。沿用现有分值表示：ANN `_score=(1+cosine)/2`，exact `_score=1+cosine`，调试对比时需要还原；两者的 max 与排序语义相同。上层仍只有 lexical / semantic / vision 三通道，不为各语言新增融合权重。ANN 是近似召回，max 规则不保证完整的精确 TopK。

description 也参与 max，因此描述产生的高分仍可能决定事件排序。此次明确保留该能力；多向量解决文本拼接相互稀释的问题，并不自动消除所有描述误匹配。

ES `_id` 根据 `(parse_task_id, video_chunk_id, version, timeline_index)` 确定性生成，对外返回为 `event_id`；`_source` 不再重复保存 event_id。没有 `source_content_id`、`publication_key` 或独立 event_version。

一物理 Index 固定完整投影、encoder、采样、预处理和聚合合同，由 `_meta` 校验。异合同使用新物理 Index，避免每文档增加一套合同/版本字段。

## 3. 抽帧与配置

配置只有一处可调整入口；首次进入 INDEX 时冻结有效 Event 目标到现有 VideoParseTask.detail。显式补建在事务内接受指定目标。Index `_meta` 是兼容校验快照，不是另一处可修改配置来源。

`resolve_event_indexing_contract` 是唯一解析入口：`SEEKINO_EVENT_FRAME_FPS` / `SEEKINO_EVENT_MAX_FRAMES` 覆盖 Engine 的默认采样参数，尺寸使用当前服务共同的媒体 max-height。Bootstrap 只构造一份有效 Event 合同供查询与 INDEX 使用；CLI 初始化/补建使用相同解析入口。不能按每个旧 Task 的 `visual_input.max_height` 分别创建 Event 合同，否则同一物理索引与查询会发生配置分叉。原 Task 的 Chunk 配置、编码指纹不变；已冻结的 Event 目标继续沿用，改合同须显式切换物理索引并补建。

首版工程基线：均匀中点、1 fps、最多 3 帧；尺寸沿用现有媒体配置；`l2-mean-v1` 为逐帧 L2 归一化、等权均值、再 L2 归一化。它不保留运动顺序，也不保证短暂细节不会稀释，效果由实验验证。参数不是图片工具展示预算。

对与 canonical Chunk 相交的 Event 派生索引；保留完整 Event 事实时间，抽帧使用交集范围，不能改写权威事件。每张实际参与编码的图片按输入顺序记录：

```json
{"capture":{"frames":[{"requested_at_ms":54950,"actual_at_ms":null,"image_sha256":"..."}]}}
```

时间是原视频绝对整数毫秒。现有 FFmpeg seek 未取得实际 PTS，`actual_at_ms` 必须为 null，不能以请求时间代填。每 Event detail 不重复整份配置；完整可恢复编码缓存及执行 manifest 继续放父 Chunk.detail。

Gateway 已有 embed_image，不增加生成 caption 或新 embedding 模型前置条件。新 image request/artifact 验证模型、模态、维度、输入与非零有限向量；不能把 image 塞入旧 expected modality=video 的 vision request。

## 4. INDEX 发布与恢复

```text
Refine 正式提交且覆盖依赖完成
→ 原 INDEX 领取租约
→ bind_event_index_target：锁父任务、校验租约、首次冻结或返回既有目标
→ 使用返回的目标构造 Coordinator（不使用绑定前的旧 task_input.detail）
→ 读取同一快照中的 task/chunk/version/Timeline
→ Engine 生成 Event 投影、文本向量集合及视觉向量计划
→ 恢复缓存，抽临时帧并只编码缺失表示
→ 发布 Chunk 与 Event 文档
→ 验证预期集合、合同、来源和搜索可见性
→ 在租约、正文版本和目标未变时提交完成凭据
```

- 现有 Chunk ES 快速复用路径也必须验收并补齐 Event。
- Writer 沿原 Chunk 写入惯例在首次 INDEX 时创建或验证 Event 索引；Query 只验证既有索引，不隐式建库。`init-event-index` 可提前显式验证完整 mapping / `_meta`，只报告 Event `contractId`，不为 Event 引入独立 Chunk generation。
- 逐 Event 缓存内部合并，不能浅层覆盖其他 Event 结果。
- 文本缓存按来源（description 或 dialogue 的 language / role）绑定输入 SHA 与冻结合同；中途失败可复用已完成的表示，完整文档验收要求所有预期表示齐全且与计划逐项对应。
- receipt 记录来源、目标合同、数量及集合摘要；零 Event 也有明确完成证明。
- 部分写入或超时先 inspect / verify 实际状态，成功编码不因发布重试重算。
- 写入、完成和清理受租约及来源保护；旧版本仅有界清理，不能误删新版。
- 删除素材沿原删除/补偿链清理相应 task/chunk 的所有 Event 版本并验证不可见。
- 旧 COMPLETE 没有 Event receipt 表示未覆盖，不等于零命中。回填复用原 INDEX，不重跑 Parse / Refine，不抬高正文版本或修改原 Chunk generation。
- 复用现有 INDEX rebuild 会暂时使对应 Video 退出最新 COMPLETE 查询范围；本轮不承诺无间断回填。

## 5. 查询与 Agent 合同

Server 先用 SQL 确定可信授权范围和已完成 Event 发布的来源，再将 `(parse_task_id, video_chunk_id, version)` 配对组合交给 ES。必须按元组 OR 过滤，不得拆成三个独立 terms 集合。

词法、text vector、vision vector 都在 topK 前应用同一来源范围及 event_type / root speaker 条件。候选先按 Event 融合，随后按 distinct Chunk 批量回源，核对原始 Timeline 的下标、类型、speaker、内容与版本，再按 Chunk 组织结果。

新增业务工具 `search_video_events(query, event_types?, speaker?, video_chunk_ids?, top_k, channel_weights, cursor?)`：

- event_types 可选多选，不传不限；speaker 有值而类型未传时限定 DIALOGUE，显式混入其他类型则反馈参数冲突。
- speaker=null 不满足指定人筛选；人物名称检索与说话人条件保持不同含义。
- 模型传入的 Chunk 子范围只能缩小授权范围。
- top_k 表示最多返回多少个 Chunk 组，保留实际命中的 Event 及其字段/语言/通道来源。
- Query / Evaluator 可直接使用；首轮真实检索规则认可两种工具。
- 回源校验后才登记现有 Chunk Evidence，之后使用原 Canvas 精读、草稿和审核流程。
- 视觉主张仍使用已有 inspect_video_frames 核验；离线索引抽帧不代表 Agent 已经看图。
- Event 索引尚未覆盖时返回可恢复反馈，Agent 可继续 Chunk 检索，不将未覆盖解释为无匹配。

## 6. Trace、分层与范式

完整业务 trace 保留查询条件、有效范围、各通道候选与分数/排名、融合、截断、回源剔除原因、命中 Event/Chunk，以及 Query/Evaluator 的实际调用。RetrievalCall 与最终 trace 传递完整记录，Agent context 仅使用预算内摘要；日志摘要不能冒充完整 trace。

- Engine：纯模型、投影、计划、编码校验、融合与事实核验；不导入 Server、Gateway、ORM 或 ES SDK。
- Server：配置解析、事务/租约、媒体/Gateway/ES IO、发布及检索编排。
- Agent：工具参数和策略选择、证据精读、草稿及审核；不复制检索算法。
- 复用现有租约、队列、缓存、分页、调用预算和批量回源，不新增无实际用途的包装层或状态。
- 遵守 hypnos-code；每批按 hypnos-review 检查冗余、上下游职责、硬编码及配置真实消费，修复后重验至本次范围收敛。
- 保留工作区既有声纹、解析和前端改动，只审查本次增量。

## 7. 实施批次与验收

1. Engine 模型/算法、Event Store、INDEX 构建与恢复、配置及删除/回填接线。
2. Event 查询、SQL 回源、Query/Evaluator 工具与完整 trace。
3. 定向单元/集成验证与 hypnos-review 修复，真实效果和运行回填按环境授权执行。

直接验证包含：最终字段、七类事件/稀疏speaker、稳定身份和原始下标、图片模态及时间、缓存、快路径补齐、零集合、部分失败、旧租约/正文变化、固定_meta、删除恢复、来源元组过滤、Event-only起手、分页及完整trace、旧Chunk兼容。

MyGO 实验使用“高高在上／居高临下”、指定 speaker、已确认 RELATION_CHANGE、动作/情绪及视觉描述；分别报告索引就绪、召回质量、耗时和编码调用量。

## 8. 实施记录

- 2026-10-21：用户确认 description、原文及已有译文分别编码到同一 `semantic_vectors` 集合，Event 语义检索取 max；投影合同和默认物理索引改为 v3。此前 v2 的纯 description 方案由此替代，Timeline 字段拆分与 interpretation 提案未实施。保留已有正文和 Event 身份，部署须显式绑定 v3 目标并补建，不能复用旧混合文本向量代替各独立表示。
- 2026-10-21：多向量定向组合回归 **514 passed**，覆盖 Engine、Event / Chunk Coordinator、Event Store、检索服务、SQL→索引→Agent Canvas、CLI、配置与调用装配。真实本机 ES 隔离测试 **2 passed**：精确与 ANN 均验证 max 而非 sum、父 Event TopK 去重、父级授权与 speaker 过滤，以及版本可见性和删除清理。测试仅创建随机 test 索引并清理，没有操作运行视频索引或调用真实模型；本轮没有执行 MyGO 重建、服务切换、commit 或 push。
- 2026-10-17：持久化上述最终设计，用户授权按 check-out 范围直接开发。当前开始实现；尚未创建运行索引、回填或重启服务。
- 2026-10-17：实现 Event 纯模型/投影、image 编码与池化、独立 Event ES Store、INDEX 冻结目标/缓存/发布验收、显式补建及删除补偿；接入三通道 Event 查询、SQL 回源和 Query / Evaluator 的 `search_video_events`，复用原 Chunk Canvas。
- 2026-10-17：按 hypnos-code / hypnos-review 进行实现、自审与跨层交叉检查，已修正：旧 Chunk 快路径漏补 Event 的风险、Writer 使用只读索引绑定导致首次创建失败、旧 Task 画面高度与查询合同分叉、Event/Chunk 策略版本混用、失败调用未进入最终 trace、检索不必要拉取完整向量、ES 部分响应被当作完整发布证明，以及大量 Chunk 来源条件的 bool 膨胀。来源范围优化仍按同 `(task, version)` 分组，不拆散授权元组。
- 2026-10-17：一次统一定向回归 **1477 passed / 9 skipped**，覆盖 Engine Event/旧 Chunk、Server INDEX/Repository/Media/配置/CLI/Bootstrap/Upload/ES/检索和 Agent 工具/运行闭环。9 项跳过依赖未配置的 `SEEKINO_TEST_ELASTICSEARCH_URL`；仅有既有 Starlette 测试依赖弃用提示。该数字是一次运行结果，不累加重叠分组测试。
- 2026-10-17：最终 ES/CLI 补丁与 Supervisor/抽帧回归 **202 passed**；补充真实 SQLite Repository → INDEX 双索引发布 → Event 查询/批量回源 → 原业务 Canvas 的纵向用例，并与 Coordinator/Bootstrap 复测 **9 passed**。这些运行存在重叠，不累计为独立用例总数。纵向用例仅替换模型、FFmpeg 命令和 ES I/O，未连接真实外部服务。最终本次范围的 hypnos-review 未再发现必须修正项，`git diff --check` 通过。

### 运行入口与验证边界

现有服务启动不主动访问 Event 索引。新 INDEX Worker 会按冻结目标创建/验证 Event Index，再构建文本向量集合与视觉向量；Query 在未覆盖时给可恢复反馈。运维入口沿用项目现有 `python -m server.cli`：

```bash
# 可选：提前创建或验证当前环境固定合同的 Event 索引；不读 MySQL。
python -m server.cli init-event-index --environment development

# 只读检查现有任务持久化的 Event target 和 Chunk receipts；不代表实时 ES 健康。
python -m server.cli event-index-status --environment development --video-parse-task-id <task-id>

# 显式接受当前 Event 目标并复用旧 INDEX 补建；需要对应环境的 DB / ES 配置。
python -m server.cli request-es-index-rebuild --environment development --video-parse-task-id <task-id> --with-events
```

同一物理 Index 不能接纳不同合同；调整采样/encoder/max-height 时应选择新 Event Index 并同步配置。补建会暂时移出该 Video 的 COMPLETE 检索范围；保留原正文、Timeline 顺序和版本，不重跑 Parse / Refine。本次没有执行这些运行命令、付费模型、MyGO 回填、服务重启或提交/推送，因此测试通过不能表述为现有 MyGO Event 数据已就绪或召回效果已经提升。

v1 / v2 → v3 时，API、Worker、初始化和补建命令必须使用同一个新 Event 索引；若环境中显式设置了 `SEEKINO_ELASTICSEARCH_EVENT_INDEX`，应将其指向新索引。保留旧索引，不能只修改其 `_meta` 来冒充 v3。未完成补建的任务仍持有旧 target / receipt，不能供 v3 Event 查询使用。无需变更 SQL Schema 或重新解析已有视频，但各独立文本必须补编码。

Event 缓存先核对整份 `contract_id` 和媒体来源，再核对各文本来源的输入哈希。合同升级会使旧 Event 缓存失效，因此常规补建除了文本 embedding，还会重新抽帧和调用图像 embedding；不是仅重算文本的迁移。跨合同复用已验证的视觉向量属于后续缓存优化，本次未实现。
