# Video Parse V2：阶段图引用与证据索引

版本：**R6 / 2026-10-02 验收补证 checkpoint**。R6 继承 R5 的 07A 事实及此前 P8 产品接线证据，**本批实际基线为 `main@64ce654` 加继承的 dirty workspace**。最初 `main@a60268d`、R5 的分支切换、此前 R6 在 `main@20e178c` 之后的产品整合及大量未提交 / 未跟踪 / 忽略成果均保留为历史背景；不将历史测试或代码存在推导为本批最终验证、真实环境验收、数据切换或发布通过。

先读[阶段开发主图](./video-parse-v2-migration-stages.svg)，或分组阅读[01–04节点](./video-parse-v2-migration-stages-acceptance-01-04.svg)、[05–08节点](./video-parse-v2-migration-stages-acceptance-05-08.svg)。分组图仅复用主图同版节点，不是另一套验收规则；对应源场景与SVG同名。

**每个图中节点已包含功能、预计改动影响、用户验收和Agent指示。** 本文只索引长依据、关键约束与证据，不另设E/F式评审或执行计划。01–08身份延续；07A/07B、08A/08B分别表达原阶段的修复/验收和集成/发布子阶段。

## 总体范围与当前位置

- 总体终点是新表、解析、恢复、ES及同版证据、API/Chat/Web/CLI/评测/部署共同迁移，并完成受控数据切换与发布；07后端验收不是终点。
- **▶ 当前·产品集成：08A**；正式 API / Chat 检索、Web 观察与操作、业务 CLI 和召回评测已完成本地接线，但真实 MySQL / ES / 模型环境与质量门槛未验收。**▶ 当前·检索合同评审：02**（ES 子计划 P2）仍是另一条主线，未被 P8 实现擅自定稿。图中以实心徽标和粗边框定位，不代表当前阶段已通过。
- 07A 本地代码已完成；本批阶段定向及当前工作区 Python 全量均已通过，07B 仍缺真实 MySQL 竞争、真实 ES 可见性和真实环境同版综合证据。08A 独立接线允许与 07B 验收并行；这不移除 07B 门槛，也不等于 08B 发布。
- 基础三路优先；OCR/Prefix按配置验收。可选rerank、Sparse、图片/裁剪/GIF和抽帧Dense的具体本版必需范围及质量指标在08A待确认，不能擅自删除或默认纳入。RAPTOR/GraphRAG按TRD留后续研究。
- 依赖：01→02；03/04依适用合同并行；03+04支撑05/06；07A修复后进入07B。08A设计及独立接入可并行，综合签收需适用上游证据；08B必须等待07B+08A适用必需项和实际授权。

## 有效资料与冲突处理

读取图中所属节点 → 本文对应索引 → [handoff §0](../../../video-parse-v2-handoff.md:3) → 适用TRD / ES子计划 → 当前调用链与验证记录。最新用户决定与有效合同优先；源码用于认定实现差距，不自动批准提案。

新 session 续接时，从 **08A 产品集成**进入，并保留 02 检索合同评审及 07B 真实环境验收两项未关闭事项：先核对实际分支 / HEAD 与 handoff §0，不重做 07A、不擅自定稿 02。本批 Agent 职责仅 Parse V2 Agent / Server pipeline 及直接综合回归，Engine 能力只留调用期望 TODO，Chat 重构延期；并发 Engine diff 和此前 Agent 结构重排必须保留，不归因本批。整阶段实现及直接回归已获授权，**本批未 commit / push**，此前核心 P8 的历史提交授权不自动延续；实际数据库迁移、ES 重建、服务重启、发布和付费模型调用仍须另行确认。

- handoff §1后的旧“框架未接”、TRD的“Projection / Worker尚未实现”等进度不覆盖当前源码；业务合同仍按有效版本使用。
- handoff §0.4对invalidator的“只锁enabled parent”描述不精确：当前helper先锁选中的最新parent，再只修改enabled项。不能据旧描述缩小锁集合。
- Engine TRD保留局部开放的未来问题；当前仍只检索选定 COMPLETE 任务，提前写 ES 不意味着提前开放查询。P8 只调用 Engine 已有的版本化融合能力，Server 查询路径保留了等 Engine 发布 query-policy 抽象后再改接的 TODO。
- `**/design/`被忽略；跨机器/worktree须移交设计与未提交文件并核对差异，不能只凭相同HEAD认定同一状态。

## 阶段引用索引

以下锚点与图中节点一致；旧R2的C/D/B锚点保留为就近引用入口，不再形成独立模块。

<a id="stage-01"></a>
<a id="c01"></a>
### 01｜架构与迁移边界

状态：核心边界已确认｜不是部署验收。

MySQL 保存任务、租约、预算与正式成果，ES 是可重建投影。本阶段不引 Redis，不恢复旧 build / publication 体系。

Ref：[Agent TRD §0.3 / §1.3](../../../design/02-agent-harness/video-parse-harness-trd.md) · [Engine TRD §1.1 / §2.3.11](../../../design/02-agent-harness/video-parse-engine-trd.md) · [队列与工作区决定](../../../video-parse-v2-handoff.md:19)

<a id="stage-02"></a>
<a id="c03"></a>
<a id="c04"></a>
<a id="c06"></a>
<a id="d01"></a>
<a id="d02"></a>
<a id="d03"></a>
<a id="r01"></a>
### 02｜领域、状态与检索合同 · ▶ 当前·检索合同评审

状态：解析核心已确认｜检索合同待 review。

解析合同保持已确认语义。待确认的是 mapping（nested / 类别字段）、文字 / 视觉编码、具体字面规则及完整候选身份；样本的 NFC 连续子串仍是提案。原声与译文区分、人物与对白同事件、questions 不冒充对白已确定。未决不阻塞独立后端修复。

Ref：[Agent TRD §1.3.1 / §1.4 / §1.9](../../../design/02-agent-harness/video-parse-harness-trd.md) · [Engine TRD §1.2–1.7 / §2.3.10–12](../../../design/02-agent-harness/video-parse-engine-trd.md) · [ES 子计划 P2](../../../design/02-agent-harness/es-development-plan-review.md:113) · [Chunk 评审样本（提案）](../../../design/02-agent-harness/es-chunk-index-review.md)

<a id="stage-03"></a>
<a id="c02"></a>
### 03｜持久任务与上传入口

状态：入口 / Schema / 准入的07A本地修复完成｜真实迁移待07B验。

retry 复用任务 ID / 解析版本 / 冻结配置；普通 reparse 新 ID、解析版本+1；未接纳 PENDING 接管保留 ID / 版本。新表和上传 / 查询接口已有，不等于真实库迁移已通过。

启动 ES store 时，UploadService 的 create、旧会话 complete、PENDING takeover 与普通 reparse 共用 Bootstrap 为正式检索装配的同一 active query `EncoderContract`，任务 embedding 的 source / model / dimensions 必须精确一致；同维但不同 model 也在入队 / 接管前拒绝，避免产生 COMPLETE 后永远无法被当前查询编码器检索的任务。

2026-10-02 的 short-window 上传 / HTTP retry 组合场景复现了合法唯一短窗口被旧准备校验拒绝的 409。本批最小修复让 `server/repository/upload.py::_parse_preparation_complete()` 直接消费已有 Engine `plan_intervals()` 的实际 start / count，不修改 Engine 或短窗口合同；直接短窗矩阵及最终同版验证在[本批 checkpoint](#checkpoint-20261002)登记。

Ref：[五表模型](../../../../server/model/job.py:219) · [任务入口 / retry / reparse](../../../../server/repository/upload.py:252) · [半迁移 gate](../../../../server/database.py:170) · [V2用量模型](../../../../server/model/parse_usage.py:45) · [Agent TRD §1.3.1 / §1.9.1](../../../design/02-agent-harness/video-parse-harness-trd.md)

<a id="stage-04"></a>
<a id="c05"></a>
<a id="c08"></a>
### 04｜视频解析主链

状态：Agent / Engine / Gateway 已接｜最终联调未验。

90% 基础 COMPLETE 开下一批；短视频仍有唯一短窗；Refine 只提交实际修改字段，scope 原子并集，不按窗序串行。Prefix 独立游标与租约，不作为主完成屏障；启用和关闭路径分别验收。

Ref：[Agent TRD §0.3 / §1.1–1.2 / §1.5–1.8](../../../design/02-agent-harness/video-parse-harness-trd.md) · [Engine TRD §1.6 / §2.3.10](../../../design/02-agent-harness/video-parse-engine-trd.md) · [六 handler 装配](../../../../server/bootstrap.py:478) · [基础提交](../../../../server/repository/video_parse.py:501) · [Refine提交](../../../../server/repository/video_parse.py:647)

<a id="stage-05"></a>
<a id="c09"></a>
### 05｜Worker 恢复与资源隔离

状态：精确身份 / 锁序 / 取消释放的07A本地修复完成｜真实竞争待07B验。

正文版本与执行租约版本互不替代；exact identity 须覆盖 parent / media / phase / member / range 等适用字段。模型请求前预留累计预算，恢复不归零；媒体按 execution kind / task / lease 隔离。取消释放不应增加 retry / error。

Ref：[Agent TRD §1.3 / §1.4 / §1.9.6](../../../design/02-agent-harness/video-parse-harness-trd.md) · [DB Queue](../../../../server/mq/video_parse_queue.py:69) · [Consumer](../../../../server/mq/video_parse_consumer.py:160) · [Runtime预算与媒体](../../../../server/service/video_parse.py:185) · [预算事务](../../../../server/repository/video_parse.py:949)

<a id="stage-06"></a>
<a id="c07"></a>
<a id="normal-case"></a>
### 06｜ES 索引与完成汇总

状态：写入 / 复用 / 汇总已接｜合同与真实 ES 待验。

逐 Chunk 等全部关联 Refine 提交（INDEXING / INDEXING_FAILED / COMPLETE），不等全部关联窗口 COMPLETE；scope 不是放行凭据。读当前正文，稳定 ES ID = parse_task_id:video_chunk_id，bulk 逐项、当前来源版本、查询可见性都满足才完成。ES 失败只恢复索引。图中双窗口是验收场景，不是本轮实测。

Ref：[Agent TRD：Interval 区间、scope 与索引放行](../../../design/02-agent-harness/video-parse-harness-trd.md:485) · [Engine TRD §2.3.10–11](../../../design/02-agent-harness/video-parse-engine-trd.md:1086) · [Engine 索引管线](../../../../engine/retrieval/index_pipeline.py) · [Server 索引协调器](../../../../server/service/video_parse_index_coordinator.py) · [ES旧写保护 / 可见性](../../../../server/storage/elasticsearch.py:273) · [ES子计划 P2 / P3](../../../design/02-agent-harness/es-development-plan-review.md:113)

<a id="stage-07a"></a>
<a id="b01"></a>
<a id="b02"></a>
<a id="b03"></a>
<a id="reparse-case"></a>
### 07A｜现有主链可靠性收口

状态：**本地实现与直接回归完成**｜真实竞争 / 存储验收交07B。

本阶段修补03 / 05 / 06已有组件，没有另建 pipeline。Series Prefix reparse / PENDING 原子失效、半迁移 gate、exact lease identity、retry锁序、Prefix取消中性释放、集序变化失效和V2 ES准入均已落地并有直接回归；parent-only、不清 child content、冲突整笔回滚及历史成果保留继续成立。统一定向集为 **283 passed**；真实环境集为 **12 skipped**，所以这里只关闭07A，不推导07B或整版通过。R6 又将原有维度准入收紧为与正式查询共享的 active encoder source / model / dimensions 精确合同。

Ref：[handoff 七项与顺序](../../../video-parse-v2-handoff.md:47) · [共享 invalidator](../../../../server/repository/video_parse.py:88) · [reparse / PENDING 入口](../../../../server/repository/upload.py:373) · [集序更新](../../../../server/repository/library.py:193) · [取消分支](../../../../server/service/video_parse.py:835) · [V2 配置准入现状](../../../../server/service/upload.py:140)

<a id="stage-07b"></a>
<a id="b04"></a>
### 07B｜后端综合验收

状态：**本地代码及当前工作区本地验证完成，真实验收尚未通过**｜缺真实 MySQL / ES 和真实环境同版综合证据。

缺环境的必需场景保持未通过，用例存在或测试文件名不能证明真实运行。按合同版本登记签收；现有路径的可靠性验证不等于06 / ES P3完整通过。未决检索合同影响本阶段必需项时，不能以“以后补验”跳过门槛。

本批新增 9 个参数化 MySQL 场景和 3 个 ES 场景已补齐部分验收入口；当前分别为 **19 skipped / 6 skipped**，并未取得真实竞争或可见性证据。Prefix ABA 已有本地组合结果，但历史 committed marker 不等于当前写权限。阶段状态不因这些局部补证移动。

Ref：[handoff §0.8 本批 checkpoint](../../../video-parse-v2-handoff.md#08-2026-10-02-本批验收补证-checkpoint) · [handoff §0.7 恢复顺序](../../../video-parse-v2-handoff.md) · [MySQL 集成入口](../../../../tests/server/test_mysql_integration.py) · [ES 集成入口](../../../../tests/server/test_elasticsearch_integration.py) · [Worker 综合入口](../../../../tests/server/test_video_parse_worker.py)

<a id="stage-08a"></a>
<a id="c10"></a>
<a id="d04"></a>
<a id="d05"></a>
### 08A｜产品入口与工程验收 · ▶ 当前·产品集成

状态：**本地产品接线已实现，尚未验收通过**｜缺真实环境、02合同与质量门槛。

正式 Search API 与 Chat 在 V2 mode 下使用 ES 候选和 MySQL 同版 hydrate，只允许 latest、未删除、COMPLETE 任务，且对内容版本、receipt、generation、合同和编码器身份做校验；部分就绪 Series 显式返回 `partial / warnings`。任务 list / get / SSE、Web V2 watcher / 历史与 retry / reparse / delete、CLI V1/V2 mode 的 search / rebuild-index，以及隔离 ES 数据集的 recall eval 均已接线。本地代码仍只使用 Engine 已有版本融合函数；未实现 Engine 所属的未来查询策略。

本批 Worker full / short-window 综合场景把正式上传、索引失败 / HTTP retry、COMPLETE、正式 receipt / provenance hydrate、Search HTTP、reparse 拒旧 task 和删除失败 / reconcile 串成可执行本地链；使用 SQLite 与媒体 / 模型 / ES 替身，最终运行记录见[本批 checkpoint](#checkpoint-20261002)，不覆盖真实产品演示、Chat 重构或质量门槛。

AgentRuntime 已按 execution mode 隔离 V1 transcript 与 V2 lexical tool / prompt；V2 query encoder identity / generation 不相符时 fail closed。公开 Agent Message 同时移除 `replyToMessageId`、`retrievalQuery` 和 `searchResult.query`；这是已确认但会影响调用方与历史数据的 breaking change，现有 SQL 只支持空表迁移，非空历史库必须在 08B 前另行确认兼容与回填方案。

基础三路的固定评测集与效果 / 定位 / 延迟 / 成本阈值，以及 rerank 的 scorer、预算、失败策略和可选扩展的本版范围 / 质量门槛仍待用户确认；默认关闭不等于完成或获准延期。

Ref：[ES 子计划 P4–P6](../../../design/02-agent-harness/es-development-plan-review.md:131) · [Engine TRD §2.3.3 / §2.3.11–12](../../../design/02-agent-harness/video-parse-engine-trd.md) · [正式检索装配](../../../../server/bootstrap.py) · [V2 检索与 Engine TODO](../../../../server/service/video_retrieval.py) · [任务投影与 action gate](../../../../server/repository/job.py) · [业务 CLI](../../../../server/cli.py) · [Web 任务观察 / 操作](../../../../web/src/features/tasks/TasksPage.tsx) · [ES 评测入口](../../../../evals/recall/cli.py)

<a id="stage-08b"></a>

### 08B｜数据切换与整版发布

状态：未执行 / 未证明｜依赖07B + 08A适用必需项。

先明确目标环境、数据范围、负责人、切换窗口和恢复方案。适用07B / 08A通过及操作授权是前置；Agent Message breaking contract 还要求调用方兼容和非空历史回填方案，不能对非空库直接执行只支持空表的现有 SQL。未进行实际迁移、ES重建、重启或发布；生产旧链退场与发布证据不能由代码存在推出。

Ref：[ES子计划 P7](../../../design/02-agent-harness/es-development-plan-review.md:159) · [当前环境与未执行记录](../../../video-parse-v2-handoff.md:19) · [迁移草案目录（不是直接执行批准）](../../../migrations/)

<a id="source-evidence"></a>
## 七项缺口：R5 实现与直接证据

这张表只记录07A落点及直接证据；用户验收仍以03 / 05 / 06及07A节点四项为准。真实存储与综合验收属于07B。

| 07A 项目 | R5 实现与直接证据 |
| --- | --- |
| Series Prefix入口 | [reparse / PENDING](../../../../server/repository/upload.py)在同一事务复用[共享 invalidator](../../../../server/repository/video_parse.py)，活跃后序lease整笔回滚，过期lease重置并fence；三集、disabled中间集、旧worker与child content均有直接回归。 |
| 半迁移Schema gate | [create_schema](../../../../server/database.py)在`create_all()`前按结构语义核对两张usage表的列 / nullable / PK / unique / CHECK / FK ondelete / 索引列组，忽略名称与server default；`video_parse_usage_attempt.started_at`进入UTC映射。 |
| exact identity | [Chunk / Interval matcher](../../../../server/repository/video_parse.py)核对parent、media、phase、成员、start和count；真实row ID配伪身份的读 / 提交路径均拒绝。 |
| retry锁序 | [retry_parse](../../../../server/repository/upload.py)先锁Chunk、再Interval、后parent，并在变更前拒绝全部活跃执行lease；SQLite直接回归已过，双连接MySQL证明留07B。 |
| Prefix取消释放 | [release_prefix](../../../../server/repository/video_parse.py)按当前fence清owner / expiry / execution并立即可领，不增加retry / error；[CancelledError分支](../../../../server/service/video_parse.py)释放后继续传播取消。 |
| 集序变化失效 | [update_video_episode_order](../../../../server/repository/library.py)在同一事务从`min(old,new)`失效enabled后缀；上移 / 下移 / 同值重放 / active lease回滚和关联upload原子性均有直接回归。 |
| V2 ES维度准入 | R5 的 [upload / complete / takeover / reparse边界](../../../../server/service/upload.py)要求V2 embedding维度同时匹配启动ES store的semantic / vision维度；不相容配置在入队前拒绝。R6 在此基础上要求 source / model / dimensions 精确匹配正式查询的 active `EncoderContract`，同维不同 model 的 create / reparse 回归均证明拒绝发生在建任务前。 |

## 08A：R6 本地产品接线与直接证据

这张表记录 P8 / 08A 已落地的本地调用链；它不把替身回归升级成真实环境、效果门槛或发布证明。

| 08A 范围 | R6 实现与直接证据 |
| --- | --- |
| 正式 Search / Chat | [Bootstrap](../../../../server/bootstrap.py)按 execution mode 装配 retrieval 与 Agent runtime，并把同一个 active query `EncoderContract` 交给 UploadService 与正式 V2 retrieval；[VideoParseRetrievalService](../../../../server/service/video_retrieval.py)只从 ES 取候选，再以 MySQL 校验 latest、未删除、COMPLETE、正文版本、receipt / generation、投影 / 查询 encoder 身份并 hydrate 正式证据，不相符即 fail closed。AgentRuntime 在 V1 使用 transcript tool / prompt，在 V2 使用 lexical tool / prompt；API 与 Chat 共用结果合同，部分就绪 Series 返回 `partial / warnings`，V2 不回退 Qdrant。 |
| 任务 API / SSE | [Repository](../../../../server/repository/job.py)与 [API](../../../../server/api/jobs.py)提供 workspace 隔离的 list / get / events、公开错误投影和基于当前 exact lease 的 `canRetry / canReparse / canDelete`；未接纳 PENDING 只在 V2 mode 暴露 takeover。SSE 在 terminal 快照后关闭，能力随后的 lease 变化由 GET / list 刷新，不伪装成长连接保证。 |
| Web 观察 / 操作 | [UploadProvider](../../../../web/src/features/uploads/UploadProvider.tsx)支持 V1 / V2 watcher 与 GET fallback；[TasksPage](../../../../web/src/features/tasks/TasksPage.tsx)展示 V2 历史并按服务端独立 capability 执行 retry、普通 reparse、PENDING 接管和 delete，列表轮询补 terminal 后的 capability 变化。 |
| Agent Message 合同 | 公开 `AgentMessageView` 已移除 `replyToMessageId`、`retrievalQuery`，公开 `searchResult` 不再回传原 query；请求身份归入 `detail.request`。这是已确认的 breaking 收敛，[现有迁移](../../../migrations/20260927_agent_message_detail_request.sql)只允许空 `agent_message` / legacy timing 表，不能直接用于非空历史库；08B 前必须确认调用方兼容和回填 / 迁移方案。 |
| 业务 CLI | [CLI](../../../../server/cli.py)为 `search` / `rebuild-index` 增加 V1 / V2 mode；V1 保留 Qdrant，V2 search 使用正式 ES + MySQL 服务，V2 rebuild 要求明确 task identity，不创建另一套查询策略。 |
| Recall eval | [评测入口](../../../../evals/recall/cli.py)保留 local 对照并把外部评测后端迁到隔离 Elasticsearch index / dataset；[数据集适配器](../../../../evals/recall/elasticsearch_index.py)校验 mapping、逐项写入与查询可见性，dataset identity 覆盖解析输入、真实视觉媒体指纹和 `max_height`，避免旧视觉向量被静默复用。Qdrant 只保留旧报告读取兼容，不再是执行选项。 |
| Engine 边界 | Engine 提供[版本化候选融合](../../../../engine/retrieval/video_retrieval.py)和纯[索引管线](../../../../engine/retrieval/index_pipeline.py)；Server / Agent / Web / CLI 只消费公开能力并承担 IO / presentation。当前 Server 查询路径有 `TODO(video-parse-v2/P8)`：Engine 发布 query-policy 抽象后再改接；本阶段不在宿主层复制或发明 Engine 策略。 |

<a id="checkpoint-20261002"></a>
## 2026-10-02 验收补证 checkpoint

本批继承 `main@64ce654` 及原工作区，只记录职责内实际补证，不另建阶段或计划；精确验证命令、下一步与限制见[handoff §0.8](../../../video-parse-v2-handoff.md#08-2026-10-02-本批验收补证-checkpoint)。唯一生产修复是 Server 上传准备校验调用既有 `plan_intervals()`；此前 Agent 结构重排、Engine 并发实现及其他无关文件保留为既有 / 外部成果。本批问题统一引用[既有问题日志](../../../design/02-agent-harness/potential_problems.md)，现有 6 项；该目录被 `**/design/` 忽略，交接须按需使用 `--hidden --no-ignore` 核对并保留。

| 范围 | 本批实现 / 当前验证 | 限制 |
| --- | --- | --- |
| 唯一短窗 retry | [Upload Repository](../../../../server/repository/upload.py)按 Engine 规划的实际窗口 start / count 校验准备完整性；[UploadService 直接集](../../../../tests/server/test_upload_service.py) **66 passed**，HTTP 直接集 **2 passed**，Worker full / short 组合 **2 passed**。 | 不改变领域规划、任务身份、Schema 或接口；各集合与阶段 / 全量有交集，不能相加。 |
| 同版产品链与恢复 | [Worker 综合回归](../../../../tests/server/test_video_parse_worker.py)的 full / short 两场景通过，覆盖上传 → 部分索引失败 → 同一连续 Worker event loop 上 HTTP retry → COMPLETE → 正式 retrieval receipt / provenance → Search HTTP → reparse 拒绝旧 task → 删除失败 / reconcile。 | SQLite 开启 FK、媒体 / 模型 / ES 为替身，不能证明真实存储或模型质量；本地并行 row lock / ready mask 观察不升级为 MySQL 故障。 |
| Prefix ABA | [Repository 组合回归](../../../../tests/server/test_video_parse_repository.py)直接集 **16 passed**；失效后重新越过旧位置时历史 committed 检测可恢复为 True，但旧 heartbeat / retry / release / commit 继续拒绝。每次旧操作后分别快照比较完整 parent detail / update_time 与两块 Chunk 成果，保护新 lease 到期／调度时间、compact、正文和版本；增强后阶段及全量重跑仍通过。 | marker 不是写权限，未据此修改生产 Prefix 或清理历史；真实 MySQL 仍待验。 |
| MySQL 新出口 | [MySQL integration](../../../../tests/server/test_mysql_integration.py)新增 9 个参数化场景：普通 reparse / PENDING × active / expired Prefix 的跨集事务；retry 与 Chunk / Refine / Index 领取，以及 Chunk / Refine 提交的双连接竞争。当前 **19 skipped**。 | TEST DB URL 缺失，持锁 Event / 有限等待 / 整笔回滚只是可运行验收入口；锁序观察未证实死锁，不预先修生产。 |
| ES 新出口 | [ES integration](../../../../tests/server/test_elasticsearch_integration.py)新增 3 个场景：同 generation 旧正文拒绝、部分 bulk 恢复只补缺项并 COMPLETE / 正式查询可见、删除响应丢失与即时迟到写 / reconcile。6 collected，当前 **6 skipped**；新增 3 个函数的注入边界离线编排通过。 | TEST ES URL 缺失；coordinator 使用 SQLite Repository，不是 MySQL + ES 真整链。即时 delete 版本拒绝不等于超出 `gc_deletes` 后永久拒旧写。 |
| Runtime 生命周期观察 | VP2-006 记录同一 Runtime 跨 event loop 重用时的本地观察；本批组合场景在同一连续 Worker event loop 内 HTTP retry 已通过。 | 是否正式支持同实例 `start / close / start` 或多次 `run_once_sync` 仍待核验，不称为已证实生产故障，也未扩为本批生产生命周期修复。 |
| 本批阶段定向集 | 10 个 Server 测试文件：**207 passed, 25 skipped**，1 条既有 Starlette warning（20.43s），精确命令见 handoff §0.8。 | MySQL 19 / ES 6 skipped 不等于真实通过；直接组与阶段集有交集，不能相加。 |
| 当前 dirty workspace Python 全量 / 静态 | `.venv/bin/pytest -q --tb=short`：**1690 passed, 29 skipped**，1 条既有 Starlette warning（36.25s）；1 个生产文件 + 5 个测试文件 + Parse V2 Agent 的 `compileall` 及全工作区 `git diff --check` 通过。 | 全量包含此前 Agent 结构重排与外部 Engine 成果，不将增量归因本批；历史 Python **1646 / 17** 和 Web **201** 不是当前结果。02 未批准、07B / 08A 真验收未通过，本批无外部操作或 commit / push。 |

主图与 05–08 分图的源场景及官方导出 SVG 已同步，07B / 08A 卡片通过视觉核验；**只说明地图渲染完成，不代表阶段验收**。01–04 不涉及本批变化，保留已有 R6 snapshot。当前位置仍为 08A，02 未批准；下一步按 handoff §0.7 在具备隔离真实环境、质量门槛与必要操作授权后运行真实场景，Engine TODO-only、Chat 重构延期，不另造计划或推进 08B。

## 证据限制与更新

R5 统一定向命令覆盖 database / usage / bootstrap / repository / queue / worker / upload / library / fencing / API，共 **283 passed，1条既有Starlette deprecation warning**；涉及文件的`py_compile`与`git diff --check`通过。该结果证明本地直接行为，不是最终全量、真实锁竞争或真实ES可见性。

此前 R6 产品接线历史工作区分组执行了直接回归：任务 API / Repository / SSE **97 passed**；V2 retrieval / repository / bootstrap / library / ES storage **88 passed**；Agent / Chat **174 passed、1 skipped**；CLI / config / keyframe backfill **190 passed**。修复 V1 transcript / V2 lexical 合同泄漏并补齐 V2 query encoder identity / generation 门禁后的历史核心定向集为 **289 passed、1 skipped、1条既有 Starlette warning**。这些集合存在交集，不能相加或代替本批验证，只记录当时收敛过程。

此前 R6 的历史 Python 全量 `.venv/bin/pytest -q` 为 **1646 passed、17 skipped、1条既有 Starlette warning**（26.24s）；历史 Web `npm test -- --run` 为 **201 passed**，当时 typecheck / build、`compileall` 与 diff check 通过，build 只有既有 `>500 kB` chunk warning。它们只证明当时版本的本地收口，**不是本批最终结果**；17 个 skip、SQLite / fake ES 或替身也从未代表真实存储、付费模型效果或产品质量通过。

[handoff §0.6 历史记录](../../../video-parse-v2-handoff.md)中的分批数量和ES子计划205项记录只支持各自当时范围，不能与R5相加或冒充更高层验收。

此前 `real_agent_repository` Worker 用例只证明 SQLite 与替身的组件协作；旧 MySQL 用例主要覆盖同视频版本竞争、旧 ES 用例未覆盖全部恢复出口，这是本批补证前的调查事实。现在已经新增跨集 Prefix、retry / Worker、新 ES 恢复出口及同版 HTTP 产品组合场景，不能继续把它们列作尚未实现，也不能因为用例存在就宣称真实验证通过；07B 仍按场景和同版运行证据签收。

本批重新核对时，`SEEKINO_TEST_DATABASE_URL`、`SEEKINO_TEST_ELASTICSEARCH_URL`、`SEEKINO_ELASTICSEARCH_URL` 仍均未配置，未打印凭据或连接外部环境。R5 的 **12 skipped** 是历史记录；本批入口当前分别为 MySQL **19 skipped**、ES **6 skipped**。SQLite / fake ES 不能替代真实锁竞争、真实查询可见性或故障语义；固定评测集与质量 / 定位 / 延迟 / 成本阈值尚未确认，02 未获批准，因此 07B / 08A 仍未通过。数据切换和发布未证明，本批也没有 commit / push、迁移、ES 重建、服务重启、发布或付费模型调用。

`invalidator` 当前仍保留 `prefix.committed_leases`。R5 的“游标重新推进后的旧 marker / ABA 尚未单独验证”已被本批组合结果补齐：历史检测为 True → 失效后 False → 新链重推后 True，而旧写入 / heartbeat / retry / release 继续拒绝，新链正常提交。本地结果没有发现旧 Worker 写权限复活，也不构成清理历史或重做架构的依据；真实 MySQL 竞争证明仍留 07B。

后续更新按所属阶段进行：记录代码/配置/时间基线、调用入口、操作或测试、预期与实际、替身和未覆盖项；用户决定先同步有效合同，再更新受影响节点、引用和验收。只有适用必需项全部满足才标阶段通过；待确认/未实现/未验证/获准延期分开。图、分组阅读页和源场景保持同版，不另建下一批行动区。
