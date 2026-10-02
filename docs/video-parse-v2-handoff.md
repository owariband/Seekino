# V2 解析 pipeline：check-out 式交接

## 0. 2026-10-02 R6 / 08A 产品集成停点（下一 session 先读本节）

本节是 2026-09-28 原始开工交接及 2026-09-30 R5 / 07A 收口之后的最新实现快照，覆盖下文“尚未开始实现”和“产品入口尚未切换”等旧进度描述。下文 §1～§10 仍保留原始合同、完整修改树和历史验收依据；判断当前完成度和续接顺序时以本节与 R6 阶段图为准。

### 0.1 当前职责、方向、下一步和未决项

1. **主要职责**：当前产品集成阶段为 **08A**，负责让正式 Search API / Chat、Web 任务观察与操作、业务 CLI 和 recall eval 使用同一 V2 任务 / ES / 同版证据合同。**02 检索合同评审**仍是并行主线；**07B 真实 MySQL / ES 后端综合验收**仍未通过，不能因 08A 本地接线而移除。
2. **开发方向**：07A 七项可靠性修复保持本地完成；P8 已把产品入口接到现有 V2 能力。V2 查询只使用 ES 候选并由 MySQL hydrate / 校验 latest、未删除、COMPLETE 任务及同版身份；不回退 Qdrant，也不另建 pipeline。
3. **分层边界**：本轮不负责开发 Engine 层查询策略。Server / Agent / Web / CLI 只调用 Engine 已公开的版本化融合与索引函数；`server/service/video_retrieval.py` 已留 `TODO(video-parse-v2/P8)`，等 Engine 发布 query-policy 抽象后再改接，不在宿主层复制策略。
4. **下一步**：R6 本地开发、全量回归与本轮交付已经收口。跨 session 的下一步是：有隔离真实环境后补 07B 的 MySQL / ES 证据与 08A 产品演示，同时由用户收敛 02 检索合同及质量门槛。不要重做 interval、scope、INDEXING、短窗口或 Prefix 规则，也不要把本地通过写成真实验收。
5. **需要用户收敛的关键边界**：02 中的 mapping / 文字与视觉编码 / 严格字面规则及完整候选身份；08A 固定评测集与效果、定位、延迟、成本阈值；rerank 的 scorer、预算、失败策略和可选扩展的本版范围。公开 Agent Message breaking change 的调用方兼容、非空历史库回填 / 迁移方案也必须在发布前确认。目标环境、真实 URL、运行窗口与发布负责人尚未提供，因此真实验收保持未通过。

本轮用户已明确授权完成核心 P8 代码后在 `main` 延续既有风格 commit、push，不需要新开发分支。该授权不包含实际数据库迁移、ES 重建、服务重启、发布或付费模型调用；这些动作仍须另行确认。

### 0.2 工作区与队列决定

- 当前目录仍为 `/Users/bytedance/workspace/other-project/AniVAG`。最初开工基线是 `main@a60268d`；R5 期间曾被外部操作切到 `feat/es-v2-retrieval@de1c987`。R6 开工时已经回到 `main`，核对的 HEAD / `origin/main` 均为 `20e178c`，其后 P8 代码仍与此前跨批次成果共处同一工作区。最终 commit / push 身份以本轮交付记录为准。
- 工作区仍有大量 tracked、untracked 和被忽略的有效成果，且包含 P7、P8 与其他主线历史，不可全部归因于本批。不得整理或覆盖无关改动；提交时只暂存经范围审计的相关路径，不纳入无关 RAGFlow 图。
- 必须继承当前工作目录。不要 reset、clean、stash、整库暂存或从当前提交重新 checkout；单独 checkout `a60268d` 会丢失最新 TRD、V2 新文件及大量未提交成果。
- 当前 durable queue 和事实源是 **MySQL**。V2 Consumer 已通过队列接口调用领取、心跳、重试和回收，业务 Agent 不直接依赖 Redis；本阶段不引入 Redis。当前 Bootstrap 和类型仍显式装配 `MySqlVideoParseQueue`，而任务状态、租约 fencing、预算及成果事务也有意留在 MySQL，所以未来接 Redis 更适合作为通知 / wake-up transport，而不是直接替换持久真相。该演进规模可控，但不是简单改一个连接字符串。
- 索引纯逻辑已位于 `engine/retrieval/index_pipeline.py` / `embedding_reuse.py`，Server 通过 `server/service/video_parse_index_coordinator.py` 承担 Repository、ES 和事务协调；原 Server 纯策略实现已删除，不得恢复或继续引用。

### 0.3 已落地的主链

| 范围 | 当前实现 |
| --- | --- |
| Engine | 固定 Chunk 拓扑、完整滑窗和唯一短窗规划；成员、时间、实际字段修改及 Prefix 结果校验；正式 ES Projection、纯索引管线和版本化 BM25 / 文字 Dense / 视觉 Dense 融合。Engine 尚未发布 query-policy 抽象，宿主层只留 TODO，不代写策略。 |
| Agent | Prepare、90% 分批 Scheduler、Chunk ASR / 视觉 / OCR / Understanding、Interval Refine、Listener、Prefix Agent 和持久阶段恢复均已有具体实现；配置会拒绝关闭 Understanding、ASR 和 OCR 后伪造空成功。 |
| Gateway | 请求前模型调用控制、取消传播、用量采集、结构化 Schema 规范化，以及主 / fallback 各自冻结的 timeout、输出 token 和 reasoning 配置已接入。 |
| DB Queue / Repository | Video、Chunk、Interval、Prefix 的领取、续租、提交、重试、过期回收和 fence；Prepare 幂等建子任务；基础成果 / ready mask、Refine / scope 合并、索引完成、Video 汇总和模型调用预算均进入事务。 |
| Worker / Runtime | `VideoParseConsumer` 与 `VideoParseRuntimeService` 已在 V2 模式由 Bootstrap 装配；包含 bounded 并发、异常回收、提交后心跳竞态处理、`--once` 诊断入口和 media reaper。 |
| 媒体 | 临时媒体按 execution kind、task ID 和 lease version 隔离；V1 代理分片作为输入时仍保存 whole-video source ref，V2 按全局时间从整集媒体裁剪；失效目录按精确租约身份回收，DB 检查失败时 fail closed。 |
| ES / Indexing | Interval Refine 提交后按逐 Chunk 放行；Engine 纯管线生成计划，Server coordinator 执行 Repository / ES 副作用。使用稳定文档 ID、外部版本保护、bulk 逐项检查、refresh 后可见性确认和相容 embedding 复用；上传删除会精确清理对应 V2 文档并验证不存在。 |
| 正式 Search / Chat | `ANIVAG_PARSE_EXECUTION_MODE=v2` 时由 Bootstrap 装配 `VideoParseRetrievalService` 与 lexical AgentRuntime，并把同一个 active query `EncoderContract` 交给正式 retrieval 与 UploadService；候选只来自 ES，MySQL hydrate 校验 latest、未删除、COMPLETE、内容版本、receipt / generation、合同与 query encoder identity，不相符即 fail closed。V1 transcript tool / prompt 与 V2 lexical tool / prompt 保持隔离；部分就绪 Series 返回 `partial / warnings`，V2 不回退 Qdrant。 |
| 上传 / 任务 API | `v1` / `v2` execution mode 已接；V2 上传只建立和消费 V2 任务。配置 ES 时，create、旧会话 complete、PENDING takeover 与普通 reparse 还要求任务 embedding source / model / dimensions 精确匹配 active query `EncoderContract`，同维不同 model 也在建任务 / 接纳占位前拒绝。`/api/jobs/video-parse/{get,list,events}` 暴露 workspace 隔离的公开任务投影和 exact-lease action capability；未接纳 PENDING 只在 V2 mode 允许 takeover，terminal SSE 后由 GET / list 刷新能力。 |
| Web | UploadProvider 支持 V1 / V2 watcher 与 fallback GET；TasksPage 显示 V2 历史、状态、用量与公开错误，并分别服从 `canRetry / canReparse / canDelete`，支持普通 reparse、PENDING 接管和删除。 |
| Agent Message | 公开 Message 合同移除 `replyToMessageId`、`retrievalQuery` 和 `searchResult.query`，请求身份归入 `detail.request`。这是已确认的 breaking change；`20260927_agent_message_detail_request.sql` 只允许空 legacy 表，非空历史库不能直接执行，发布前须确认调用方兼容与回填 / 迁移方案。 |
| CLI / Eval | `search` / `rebuild-index` 支持 V1 / V2 mode；V2 search 走正式 ES + MySQL 服务，V2 rebuild 要求明确 task ID。Recall eval 保留 local 对照并把外部后端迁到隔离 ES dataset，数据集身份覆盖解析输入、视觉媒体和 `max_height`。 |
| 用量与迁移 | V2 attempt 和 model call 已携带 `execution_kind` / `execution_task_id`，可区分 Video、Chunk、Interval、Prefix；`20260928_v2_worker_runtime.sql` 已同步相应列、约束和索引草案。 |

P8 已完成上述本地接线，但不等于 08A 签收。正式验收仍要求隔离真实 ES / MySQL / 模型环境、02 获批合同、固定评测集与事前阈值；只有选定 latest `COMPLETE` Video 任务及同版证据可以对外返回。

### 0.4 R5 / 07A 已完成的可靠性收口（历史事实继续有效）

1. **Series Prefix 入口原子失效**：普通 reparse 及未接纳 PENDING 接管在同一事务调用共享 invalidator；三集、compact-disabled 中间集、活跃 / 过期 lease、旧 worker fence、历史 task 和 child content 保留已有直接回归。真实 MySQL E1 / E2 仍属07B。
2. **半迁移 Schema gate**：`create_schema()` 在 `create_all()` 前按结构语义比较 usage attempt / model call 的列、nullable、PK、unique、CHECK、FK `ondelete` 与索引列组；名称和 server default 不参与；补 `video_parse_usage_attempt.started_at` UTC 映射。
3. **exact lease identity**：Chunk 增加 parent / `video_chunk_id`，Interval 增加 parent / phase / members / start / count 精确匹配；真实 row ID 配伪身份的读和提交均拒绝。
4. **手动 retry 锁序**：按 Chunk → Interval → parent 锁定，并在变更前拒绝全部活跃 child / interval lease；SQLite直接回归已过，双连接 MySQL 无循环等待证明留07B。
5. **Prefix 取消释放**：新增 fenced `release_prefix()`；只清 owner / expiry / execution ID 并立即可重领，不增加 retry / error；Runtime 取消分支释放后继续传播 `CancelledError`。
6. **episode order 变化失效**：顺序真实变化时在同一事务从 `min(old,new)` 失效 enabled 后缀；同值重放不失效，活跃 Prefix lease 令 Video / Upload / Prefix 整笔回滚。
7. **V2 ES 准入**：R5 要求 create、旧会话 complete、PENDING takeover 和普通 reparse 入队前的 V2 embedding 维度同时匹配启动 ES store；R6 又绑定正式 retrieval 共用的 active query `EncoderContract`，要求 source / model / dimensions 精确一致。同维不同 model 的 create / reparse 也在建任务前拒绝。

`invalidate_series_prefixes_from()` 仍保留 `prefix.committed_leases`。本轮已证明失效后旧 worker 写入被拒绝；“游标再次推进后的旧 committed marker / ABA”尚未单独验证，只是待查观察，不得在没有证据时升级为缺陷或重做架构。

### 0.5 R6 / 08A 已实现范围与验收限制

1. **正式检索 / Chat**：V2 mode 下 Search API 与 Agent Chat 共用 ES 候选 + MySQL 同版 hydrate；Bootstrap 将同一个 active query `EncoderContract` 交给 retrieval 与 UploadService，使建任务时的 source / model / dimensions 准入和查询时的 encoder identity / generation 校验闭合。只返回 latest、未删除、COMPLETE 任务，正文版本、receipt 与投影合同任一不符也 fail closed。AgentRuntime 按 execution mode 选择 V1 transcript 或 V2 lexical tool / prompt；Series 仅部分视频可检索时明确返回 `partial / warnings`，不把局部结果伪装成完整结果。
2. **任务观察 / 操作**：任务 Repository 按当前 main / child / interval / Prefix lease 计算独立 `canRetry / canReparse / canDelete`；活跃 child 可以阻止 retry / delete 而不阻止普通 reparse，后序 Prefix lease 阻止受影响早集 reparse。公开错误不泄露内部 runtime；terminal SSE 发送最终快照后关闭，TasksPage 继续轮询 list 取得 lease 释放后的 capability。
3. **Web / CLI / Eval / Message**：UploadProvider 与 TasksPage 已消费 V2 合同；CLI `search` / `rebuild-index` 按 mode 分发且保留 V1 兼容；recall eval 的外部执行后端已迁到专用 ES index / dataset，并在任何 embedding 前校验包含视觉真实来源与 `max_height` 的数据集身份。公开 Agent Message 已移除 `replyToMessageId`、`retrievalQuery` 与 `searchResult.query`；这是 breaking change，现有 SQL 仅适用空 legacy 表，非空历史数据与调用方须在发布前另行确认。
4. **Engine 边界**：当前查询只调用 `engine.retrieval.fuse_versioned_video_chunk_rankings`；Server 内 `_v2_selection_reason` 属于展示适配，不是 Engine 策略实现。未来 channel selection / query policy 等 Engine 关键抽象只通过 TODO 等待并调用，不在本批新增 Engine 决策层。
5. **07B 真实存储**：仍须补 E1 / E2 reparse、retry / worker 同时竞争、锁序和整笔回滚的双连接 MySQL 证据，以及 ES bulk 部分失败、旧写、删除交错、refresh / 查询可见性和恢复证据；SQLite / fake ES 不能证明这些性质。
6. **08A 产品验收**：仍须在同一隔离真实环境演示上传 → 观察 V2 进度 → Search / Chat 同版证据 → retry / reparse / PENDING takeover / delete → CLI rebuild，并以已确认评测集和效果 / 定位 / 延迟 / 成本阈值判断。02 未决合同或质量门槛没有确认时，08A 保持未通过。
7. **操作边界**：本轮 commit / push 已授权；实际数据库迁移、ES 重建、服务重启、发布和付费模型调用仍未授权。08A 本地完成不启动 08B。

### 0.6 R5 历史证据与 R6 已运行验证

| 范围 | 结果 | 能证明 / 不能证明 |
| --- | --- | --- |
| R5 / 07A 统一 Server 定向集 | `283 passed`，1条既有Starlette deprecation warning | 覆盖 database、usage、bootstrap、repository、queue、worker、upload、library、fencing和API的本地直接行为；是历史局部证据，不是 R6 最终全量。 |
| R6 任务 API / Repository / SSE | `97 passed`，1条既有 Starlette warning | 覆盖 V2 list / get / events、公开投影、action capability、terminal SSE 和 API 入口。 |
| R6 V2 retrieval / storage | `88 passed`，1条既有 Starlette warning | 覆盖 ES 候选、同版 MySQL hydrate、latest COMPLETE gate、partial / warnings、Bootstrap 与 ES adapter 的本地行为。 |
| R6 Agent / Chat | `174 passed, 1 skipped` | 覆盖 Chat 工具 / runtime 与服务接线；skip 不得解释为真实模型验证。 |
| R6 CLI | `190 passed` | 覆盖 mode 分发、V2 search / rebuild 参数与 config / keyframe 兼容。 |
| R6 Web 全量 | `201 passed`；typecheck 与 production build 通过 | 覆盖 V2 watcher、TasksPage 动作和 Search partial / warnings；build 保留一条既有 Vite 大 chunk 提示。 |
| R6 核心同版定向集 | `289 passed, 1 skipped`，1条既有 Starlette warning | 在修复 V1 transcript / V2 lexical 合同泄漏并补齐 V2 query encoder identity / generation fail-closed 后运行；证明本地核心接线，不替代真实环境或最终 Python 全量。 |
| R6 Python 最终全量 | `1646 passed, 17 skipped`，1条既有 Starlette warning（26.24s） | 同一最终工作区 `.venv/bin/pytest -q`；证明本地 Python 全量无未解释失败，17 个 skip 不等于真实环境通过。 |
| R6 最终静态检查 | `compileall`、`git diff --check` 通过 | 与 Web typecheck / build 一起证明语法、类型、构建和 whitespace 收口；不证明运行竞争或模型质量。 |
| 真实环境集成入口 | R5 为 `12 skipped`；R6 环境变量仍未配置 | `ANIVAG_TEST_DATABASE_URL`、`ANIVAG_TEST_ELASTICSEARCH_URL`、`ANIVAG_ELASTICSEARCH_URL`均未配置；没有证明真实 MySQL / ES。 |

以上定向分组存在测试交集，不能相加；最终 Python / Web 数字是本轮同版收口结果。此前各批次的 Gateway、Agent / Engine、Web 和 ES 子计划测试数仍只支持各自当时版本与范围。未运行真实付费模型、实际迁移、ES重建、服务重启或发布。

### 0.7 R6 / 08A 收口与恢复顺序

1. 读取本节、R6 主图 / 05–08 分图 / evidence、最新 Agent / Engine TRD，并重新核对实际分支 / HEAD、工作区和三个真实环境变量；不要假定仍是 `main@a60268d` 或只凭文件存在判断完成。
2. R6 已固定 Python `1646 passed / 17 skipped`、Web `201 passed`、typecheck / build、compileall 和 diff check 结果；后续若代码继续变化，按影响范围重跑并更新同版证据。Engine 策略缺口只留已指明 TODO。
3. 本轮范围审计、`main` commit / push 及未纳入文件以最终交付记录为准；下一 session 不重复提交本轮范围，也不纳入无关 RAGFlow 图或创建新开发分支。
4. 若后续提供隔离 MySQL / ES / 模型环境，分别补 07B 真实竞争 / 可见性与 08A 产品场景，保存同版配置、预期、实际、恢复结果和事前阈值；缺环境或阈值时保持未通过。
5. 02 检索合同继续独立 review。确认后先回写有效合同和同一 plan-map，再调整查询 / mapping / eval；不借实现现状替用户决定。
6. 实际数据库迁移、ES 重建、服务重启、发布和付费模型调用只在新的明确授权后执行；08A 不直接推进 08B。

可给下一 session 的续接指令：

> 阅读 `docs/video-parse-v2-handoff.md` §0、R6 阶段图及证据索引，继承当前工作目录全部成果。当前产品集成位置是 08A，检索合同评审仍在 02，07B 真实 MySQL / ES 验收仍未通过。先核对实际分支 / HEAD、工作区与环境；不要重做 07A，不擅自定稿 02，不在宿主层实现 Engine 查询策略。正式 V2 Search / Chat、Web、CLI、eval 已有本地接线；缺真实环境和质量阈值时保持未验收。实际迁移、ES 重建、服务重启、发布和付费模型调用另行确认。

以下从“更新日期：2026-09-28”开始的 §1～§10 是原始历史交接，保留用于合同与修改树追溯；其中实现进度、授权状态和旧路径如与 §0 冲突，以 §0 与 R6 阶段图为准。

更新日期：2026-09-28。接手工作区：`/Users/bytedance/workspace/other-project/AniVAG`。
生成时分支为 `main`，HEAD 为 `a60268d`；大量成果仍在未提交工作区中。

## 1. 接手后首先明确的四件事

1. **职责**：完成 V2 视频解析 pipeline 的真实执行接线，负责 Agent / Engine / Gateway / Server 的交接、MySQL 任务与成果事务、Worker、ES 索引、失败恢复和完成汇总。
2. **开发阶段**：已有状态合同、领域 Schema、Projection、ES 适配器及 Agent 框架；下一阶段是具体逻辑实现和运行接入。框架中的 `NotImplementedError` 尚未消除，整条 V2 pipeline 尚未跑通。
3. **交付物**：后端 `上传建任务 → Prepare → Scheduler → Understanding → Interval Refine → INDEXING → Video COMPLETE` 可验证；索引失败只恢复索引；启用时的 Prefix 后台链可独立恢复；提供 V2 任务查询和直接覆盖这些行为的测试。
4. **待用户决定的边界**：上述主链**无关键未决项**。区间、scope、写回顺序、索引放行和短视频处理均已确认，不要重新询问。局部提前对外检索仍未选择；本批继续采用已写明的选定 Video 任务 `COMPLETE` 门槛，不阻塞后端实现。

### 授权状态

- 用户已经授权并完成的最近动作：更新 Agent / Engine TRD 的关键位置；随后要求生成本文供新 session 接手。
- 完整执行接线的修改树已展示，尚未获得执行该整批实现的明确开工指令。本文保存上下文与方案，本身不扩大授权。
- 新 session 如收到“按 handoff 实现”等指令，即按实际授权执行，不要再次询问已经确认的设计问题。
- commit、push、服务重启和实际数据库迁移按新 session 的明确授权处理；本轮未执行这些动作。

## 2. 必读顺序与资料有效性

1. [Agent TRD：主链与索引放行](../agent/video_parse_v2/design/trd.md#03-已确认的主链与索引放行2026-09-28)。重点继续读“Interval 区间、scope 与索引放行”、状态机、Listener、Scheduler、Prefix 和输入配置。
2. [Engine TRD](../engine/parsev2/design/trd.md)：重点读 §2.3.10、§2.3.11，明确当前有效内容、Projection、索引完成与恢复边界。
3. [Agent 框架 README](../agent/video_parse_v2/README.md)：理解组件及集成缺口。它尚未同步最近全部 scope / 索引规则；冲突处采用最新 TRD。
4. [领域 Schema](../engine/parsev2/schemas.py)、[已有 Projection](../engine/retrieval/index_projection.py)、[任务模型](../server/model/job.py)、[重试入口](../server/repository/upload.py)、[ES 适配器](../server/storage/elasticsearch.py)。用代码判断哪些能力已实现。
5. 按 §7 TODO 定向读取 Gateway、Server 同层既有实现，遵循项目分层和编码惯例。

资料优先级：新 session 的用户指令 → 本次已确认决定和更新后的两份 TRD → 代码实际实现情况。旧图、旧 README 与历史草稿不能覆盖新决定。

- `docs/design/parse-v2-harness.md` 的原始 scope 定义可参考，但旧“归并待评审 / 短窗待定”等段落已经被本次决定替代。
- `agent/video_parse_v2/design/architecture_draft.md`、`engine/parsev2/README.md` 仍可能含旧 Canonical / Proposal / Patch / publication 方案。不要据此重建一套 reducer 或发布体系。
- `docs/design/parse_big_graph.png` 中的 left/mid/right 是旧示意；当前不存在中心 / mid Chunk，统一使用 `[start,end)`。
- `docs/design/video-parse-v2-architecture.excalidraw` 及 SVG 尚需同步本次新放行规则。
- 实际 `schemas.py` 已使用人物人名；个别文档的实现进度说明过期，不需要再做一轮 person ID → 人名重构。

### 工作区必须保留

`**/design/` 被 `.gitignore` 忽略，两份最新 TRD 和架构图不会出现在普通 `git status` 中；很多 V2 代码仍未跟踪。新 session 应继承当前目录。若另开 worktree / clone，必须同时带上本次相关的未提交、未跟踪及忽略文件，单独 checkout HEAD 得不到这些成果。

先检查 `git status --short`、staged/unstaged 差异和相关未跟踪文件，再建立本批自己的差异基线。保护其他工作区改动，避免整库回滚、整库暂存或把既有改动归到本批。

## 3. 已完成与尚未完成

| 项目 | 已有成果 | 当前限制 |
| --- | --- | --- |
| 三张 ES 发布表的代码清理 | 已移除旧模型、Repository、发布/清理/已发布查询 Service、对应 CLI 接入和建表注册 | 未连接数据库核验或执行物理 DROP；不能说数据库实体表已删 |
| 状态与手动恢复 | `server/model/job.py` 已有 Chunk / Interval / Video 阶段；`repository/upload.py` 已有按子任务恢复的 retry / reparse | 生产 Worker 仍为 V1；短窗口完整性检查需适配新规则 |
| 数据库迁移 | 已有 `docs/migrations/20260928_task_indexing_contract.sql` 和建库检查 | 未执行；脚本仅支持空 V2 任务/成果表及空或不存在的旧 ES 发布表 |
| Engine | 人名版 `VideoParseResult`、已有 Chunk Projection 和检索合同 | 固定拓扑规划、宿主成员范围校验及实际接线待实现 |
| ES | 已有适配器、mapping 与按解析任务过滤/稳定文档 ID 的合同 | Interval 索引执行未接线；需要 ES 写入本身拒绝迟到旧版本覆盖 |
| Agent | LangGraph 拓扑、输入配置、Workflow、Listener、Refine、Prefix 骨架及编排测试 | 外部数据库、媒体、模型、索引、汇总仍有大量 `NotImplementedError` |
| 最新 TRD | 两文件关键段落已更新，最近一批共 +160 / -69 行并完成一致性复审 | README、架构图、代码旧注释仍需在实现中同步 |

已清理的旧代码包括 `server/model/index_publication.py`、`server/repository/index_publication.py`、`server/service/es_index_build.py`、`es_index_cleanup.py`、`es_published_search.py`。不要恢复它们来实现本次索引。

旧 `20260927_es_index_publication.sql` 属于历史迁移，不是新建目标库的方案。实际迁移前需重新核对库状态；已有非空数据不能套用只支持空表的脚本。MySQL DDL 会隐式提交，这份多步迁移不能当成可整体回滚的原子事务。

前次清理批次的会话记录为 **298 passed / 12 skipped**，跳过涉及未配置的 MySQL / ES 集成环境；这不是本次完整 V2 执行链的验收结果。最近 TRD 更新只做文档差异、代码块与链接检查，没有运行业务测试。

## 4. 已确认的 pipeline

```text
VideoParsePrepare
  固定 VideoChunk 拓扑、任务配置快照，幂等建立 Chunk / Interval 子任务
  n >= interval_size：完整滑窗；0 < n < interval_size：唯一 [0,n)
        ↓
ChunkScheduler
  按 sequence_no 分批，stream 式分发，由 Worker 原子领取
  本批 90% 基础解析 COMPLETE 后开放下一批，遗留任务继续运行
        ↓
ChunkUnderstanding
  ASR + 视觉输入 + 可选 OCR → Understanding → Engine 校验
  基础成果 + 初始 scope + Chunk COMPLETE + Interval ready_mask 同事务提交
        ↓
Interval Refine
  自身成员全部基础就绪即可独立执行，读取 [start,end) 内容及成员 scope
  实际正文修改 + scope 并集 + Interval 成果 + INDEXING 同事务提交
        ↓
逐 Chunk 检查
  同一解析任务中，所有包含它的 Interval 都已经提交 Refine？
        ├─ 否：等待后续提交通知 / 数据库扫描
        └─ 是：读取当前有效 Chunk 正文及版本
                    ↓
          Projection → embedding / 相容结果复用 → ES
                    ↓
          逐项成功、来源版本、查询可见性检查
                    ↓
Interval 全部有效成员的索引就绪 → COMPLETE
                    ↓
Video 根据持久子任务状态汇总

可选 PrefixContextAgent：独立后台租约与游标
  目标全部关联 Refine 已提交 + 前序背景就绪 → compact / 当前目标微调
  修改检索内容时增加版本并唤醒相关 Interval 重索引
```

### 4.1 区间与 scope

- 区间成员为 `start_sequence_no <= sequence_no < start_sequence_no + chunk_count`。末端由现有字段计算。
- TRD 建议以 `interval_size = ceil(interval_duration / chunk_duration)` 作为实现默认，现有 bitmap 配置约束为 1～63。短窗 `chunk_count=n`，bitmap 按实际成员数判断。
- 完整滑窗已覆盖首尾，`n >= interval_size` 时不再添加额外尾部短窗。
- Interval 谁先基础就绪谁先执行，不按 start 排队；Prefix 的升序处理规则不能套到 Refine。
- `chunk_parse_content.detail.scope: list[int]` 是有序、去重、单调增加的 **Chunk 序号列表**，初始 `[sequence_no]`。
- 区间 `[s,e)` 提交时，对每个成员使用数据库当前值计算 `ordered_unique(scope ∪ [s,s+1,...,e-1])`。
- scope 用于提示 Agent 已参考过的范围，不保存 Interval ID，不是 ready bitmap，也不能证明所有关联窗口完成。
- 并行 Agent 可以读到相同旧 scope；scope 扩大或另一窗写入不要求重新调用 Refine。
- 仅 scope 更新不递增检索内容版本、不触发重索引。
- `ChunkIndexInput.scope` 是另一种可信身份/版本对象，不能与上述序号列表混淆。

### 4.2 Refine 提交合同

校验本 Interval 的租约和未提交状态、Engine 允许的目标及字段；短事务中按固定顺序锁定成果行。
只把本次相对输入实际修改的业务字段应用到数据库当前正文；同一字段由后提交的值生效，未修改字段保留当前值。
scope 从当前值原子并集，正文实际变化时以当前 version 递增；同事务保存 Interval 产物和 `INDEXING`。

固定锁行顺序只避免死锁，不规定窗口的业务执行顺序。不能把旧成员快照整份覆盖当前正文，也不能仍以“所有输入版本必须相同”为理由强制其他窗口重跑模型。租约失效、重复提交、越界写入仍应拒绝。

### 4.3 索引放行与完成

- 查询范围必须限定同一 `video_parse_task_id`，覆盖关系由固定区间成员范围确定。
- 关联 Interval 的 `INDEXING / INDEXING_FAILED / COMPLETE` 均表示 Refine 已提交；`WAITING / REFINING / PARSING_FAILED` 不满足。
- 不等待关联 Interval 全部 `COMPLETE`；否则索引和完成条件会相互等待。
- 一个 Interval 可先索引已放行成员，再释放租约等其余成员；等待不是失败，不增加 retry_count。
- 复用 Refine 提交后的 Listener 通知，加数据库扫描补偿；不增加第二套 bitmap、callback 或 Chunk 索引状态。
- 索引及重试读取 **chunk_parse_content 当前有效正文及版本**。interval_parse_content 保存窗口产物，不能用早先窗口快照重放覆盖后续修正。
- 稳定 ES `_id` 使用已有 `parse_task_id:video_chunk_id` 合同；多个 Interval 涉及同一 Chunk 时检查并复用相符表示。
- ES bulk 检查逐项结果；完成前检查编码合同、正文版本和查询可见性。ES 写入自身需要防止迟到旧版本覆盖，MySQL 完成前校验不能代替它。
- 本 Interval 全部有效成员满足条件才 `COMPLETE`。提前写 ES 不等于提前开放对外检索。

例：只有 `[0,3)`、`[1,4)` 两窗时，Chunk 0 等前窗、Chunk 3 等后窗、Chunk 1/2 等两窗 Refine 提交；都不等待两窗索引完成。

### 4.4 状态、重试与版本

| 对象 | 主路径 | 失败与恢复 |
| --- | --- | --- |
| Chunk | QUEUED → UNDERSTANDING → COMPLETE | FAILED → QUEUED；只负责基础解析 |
| Interval | WAITING → REFINING → INDEXING → COMPLETE | PARSING_FAILED → WAITING；INDEXING_FAILED → INDEXING |
| Video | PENDING → PREPARING → PARSING → INDEXING → COMPLETE | 汇总 PARSING_FAILED / INDEXING_FAILED；按实际子任务阶段恢复 |

- MySQL Refine 成功、ES 失败：保留正文、scope、Interval 成果；索引阶段内重试未能恢复后记录 INDEXING_FAILED，后续重试只索引。
- Video 同时有两类失败时，解析失败优先作为汇总状态；不能仅凭父状态统一回滚所有子任务。
- PARSING 期间局部索引可推进；父 INDEXING 表示全部必需解析已就绪、只剩索引。允许 PARSING 直接到 COMPLETE。
- 失败重试复用任务 ID、解析版本、配置快照；主动重解析创建新任务/版本。
- 手动重新入队不能被累计 retry_count 的领取硬限制永久挡住；区分一次手动尝试与自动重试预算。
- 不增加百分比 progress、逐内容 indexed bool、独立 build/publication 表、独占中心 Chunk。
- 用户已撤销“重建期间旧版持续可检索、整版原子发布切换”的要求。

### 4.5 Prefix 与检索开放

- Prefix 可选，不参与主解析完成屏障；有独立租约和持久游标，同 Series 按集序/Chunk 序号推进，不跳过前序缺口。
- 当前目标须全部关联 Interval 已提交 Refine，前序背景也就绪；不等待 ES 成功。短视频同样等待其唯一短窗。
- compact 放既有成果 detail；Prefix 的 cursor/lease 可用现有任务 detail 独立命名空间，属于宿主实现方案。
- Prefix 改检索正文时递增版本，同事务恢复包含目标且已提交 Refine 的 Interval 为 INDEXING、清除 finish_time，提交后通知索引。
- 只改 compact 元数据不重索引；不能丢失现有 scope。尚未提交 Refine 的任务不能因重建被跳到 INDEXING。
- 对外检索继续限定选定 Video 解析任务 COMPLETE，并校验相符任务/内容版本。局部提前召回和整个前端/Chat 检索切换属于后续范围。

## 5. 分层与可复用能力

| 层 | 接手职责 |
| --- | --- |
| Agent | Graph、阶段编排、提示词、工具、Listener；运行依赖通过注入提供 |
| Engine | 正式结果 Schema、拓扑/区间规划、成员/时间/修改范围校验、已有 Projection |
| Gateway | 模型协议、编码、错误、用量事件、实际请求控制 |
| Server Repository | 原子领取、租约检查、成果事务、状态聚合、持久预算与后台游标 |
| Server Service / Worker | 宿主装配、运行/恢复、媒体生命周期、索引和通知/扫描 |

直接复用人名版 Schema、`project_chunk_index`、现有 embedding 复用判断及 Gateway ASR/视觉/编码能力。V1 Engine 的 Schema 和生命周期不同，不要导入整个 V1 Pipeline 充当 V2 实现。

DB Session、模型实例、媒体临时路径放运行依赖，不能进入可持久化 RunState。模型或 ES 请求期间不持有数据库行锁。

## 6. 当前代码的具体缺口

| 入口 | 当前事实 / 必要调整 |
| --- | --- |
| `agent/video_parse_v2/agent.py` | 总是从 Prepare 开始且完成处理为空；按持久阶段恢复，纯索引重试不能重跑解析 |
| `video_parse_prepare.py` / `chunk_scheduler.py` | 准备、查询、分发、等待皆为空；接实际 DB queue 和批次规则 |
| `chunk_understanding/workflow.py` | Graph 已有 ASR/视觉/OCR 汇合；外部步骤为空，需实例依赖和真实事务提交 |
| `refine/input.py` | 只有 member_contents/member_versions，缺少给 Agent 的 member_scopes |
| `refine/interval_refine.py` | 仍写冲突重读重算；改按最新 TRD，仅实际修改字段与 scope 并集提交 |
| `chunk_interval_listener.py` | 仍为空；不能让 update_interval_mask 另开事务破坏原子提交 |
| `precompact/*` | 模型、工具、加载和保存为空；实现独立租约/游标恢复 |
| `server/bootstrap.py` | 仍装配 V1 EngineVideoParseRuntime / MySqlParseJobQueue / ParseJobConsumer |
| `server/repository/upload.py` | complete_upload 同时建立 V1 job 与 V2 PENDING 占位；接新 Worker 前必须明确执行归属 |
| `server/service/upload.py` | 已完成上传的幂等返回仍强制 V1 job_id |
| `server/repository/job.py` | 已有 V2 get/latest/list/input 读取可复用，避免再造读仓储 |
| `server/api/jobs.py` / `service/job.py` | 公开状态查询仍为 V1，已有 VideoParseTaskView 可复用 |
| `server/model/parse_usage.py` | model call 已有 video_parse_task_id，但 job_id 仍强制关联 V1；不能创建假 V1 job 来满足它 |
| `gateway/usage.py` | 当前 observer 在请求结束后执行且吞 observer 错误；预算需请求前门禁 |
| `gateway/schema_normalization.py` | 内联 $defs 后 discriminator.mapping 的字符串引用可能悬空，需用真实 V2 联合 Schema 验证/修复使用路径 |
| `server/storage/elasticsearch.py` | 普通 bulk 写入中的版本字段不等于 ES 写入版本保护 |

## 7. 执行 TODO 与推荐顺序

以下是同一完整交付中的实施顺序，不意味着完成一个骨架步骤即可结束任务。

### A. Prepare、任务事务和领取

- [ ] 增加纯 Engine 拓扑/窗口规划，首次与 retry 共用；实现完整窗口、唯一短窗与配置兼容校验。
- [ ] Server Repository 幂等建立/恢复子任务；基础内容、初始 scope、mask 和 COMPLETE 原子提交。
- [ ] 原子领取、续租、失效回收，所有提交检查现有 lease_owner / lease_version / lease_expire_time 与阶段；重复通知和旧 Worker 不能重复提交。
- [ ] 将租约版本校验与内容版本校验区分：Refine 不要求成员输入内容版本保持未变；索引和 Prefix 按各自合同检查当前内容版本。
- [ ] Scheduler 的 90% 门槛只统计基础 COMPLETE，活跃数覆盖所有开放批次；Refine/索引不阻塞下一批。

### B. 媒体、模型和共享调用控制

- [ ] 宿主根据固定且不含凭据的有效模型配置快照装配依赖；恢复不能偷偷换成今天的全局默认模型。
- [ ] 从原视频生成 Task/Lease 隔离的临时媒体，音频/视频/均匀帧处理完成或取消时清理；失效目录由 Reaper 回收。
- [ ] 接 ASR、视觉、可选 OCR、Understanding 和 Engine 校验；时间转换遵循正式 Schema 与实际视频边界。
- [ ] understanding_enabled=false 仅在已有合法构造/校验方式时接受，否则模型调用前拒绝，不能用空结果伪装完成。
- [ ] 真实 provider 请求前原子预留累计调用次数和并发名额，fallback/失败/重试也计数；恢复不归零，结束仅释放并发。
- [ ] 复用 Gateway call_id；预算与 Prefix 在 detail 中修改各自命名空间，锁行合并，避免覆盖或 ORM 未检测到原地 JSON 改动。
- [ ] 取消/丢租约后禁止新请求，尝试关闭本次在途 transport，禁止迟到写回；仅取消 asyncio.to_thread 的外层协程不够。
- [ ] 接真实 V2 用量身份；保持 V1 attempt 历史合同，避免扩展为新预算表/假 V1 job。

模型控制需核验实际支持的调用器：Codex/Claude SDK 内部多轮请求未必全部暴露在当前 track_model_call 中。不能把一次 Agent 调用冒称为一个真实模型请求，也不能无证据承诺已精确控制内部所有轮次；按实际采用的调用路径落实并说明限制。

### C. Refine、scope 与索引放行

- [ ] Refine 输入/提示词携带每个成员 scope，独立执行；宿主计算实际修改字段并提交，不引入一套通用 Patch/reducer 框架。
- [ ] 原子合并数据库当前 scope，保存区间产物和 INDEXING；重复提交不再次改内容/版本。
- [ ] 全部关联 Interval Refine 提交后逐 Chunk 放行；scope 不参与此判断。
- [ ] 提交后 Listener 唤醒相关索引；扫描补偿丢通知；等待无失败、无额外重试、无长期租约占用。

### D. INDEXING、恢复和 Video 汇总

- [ ] 当前 Chunk 正文/版本 → 已有 Projection → 相容向量复用/编码 → ES，固定本次编码合同。
- [ ] ES 稳定 ID、逐项失败处理、写入版本保护与可查询检查；同版本重放、旧版本迟到有可验证行为。
- [ ] 只处理未满足当前表示要求的成员，局部成功可复用；所有有效成员满足条件后 Interval COMPLETE。
- [ ] 聚合优先级：解析失败 → 尚有解析 → 解析已齐且索引失败 → 仅剩索引 → 全部 COMPLETE。
- [ ] 混合失败按子任务恢复，纯索引重试不调用 ASR/Understanding/Refine；进程中断按已提交阶段继续。

### E. 宿主入口与 Prefix

- [ ] 建议 V1/V2 执行模式互斥；V2 上传只建真实 V2 执行任务，兼容默认行为在配置中明确。
- [ ] 历史 V1 上传已留下 V2 PENDING 占位，不能启动 V2 后无条件全量领取；用现有 detail 标记已接纳任务，明确 reparse/接管入口。
- [ ] 上传幂等、删除保护包含 V2 活跃任务/租约；V2 返回 videoParseTaskId，jobId 可空；提供有 workspace 校验的任务查询。
- [ ] 现有上传 UI 仍以 jobId 监听 V1。后端接口是本批验收边界，部署前另需页面切换，不宣称页面已适配。
- [ ] Prefix 独立持久游标/租约、前序背景与工具范围，正文更改后索引恢复；不将后台完成作为主解析屏障。
- [ ] 同步 README、历史 scope 文档中被替代段落、Excalidraw 源文件及预览；两份 TRD 已更新，不重复当成未完成设计。

## 8. 预计修改树

沿用已展示的预计范围。`[M]` 表示当前工作区文件已存在（可能未跟踪），`[A]` 是预计新增；两份 TRD 的本次合同更新已完成。新增名是实施建议，不要求为了凑树强行新建包装层。接手者按实际必要性收敛，发生实质范围变化时先报告。

```text
AniVAG/
├── [M] .env.example                         ← V1/V2 执行模式说明
├── agent/
│   └── video_parse_v2/
│       ├── [M] agent.py                     ← 依赖注入、持久阶段恢复、完成汇总
│       ├── [A] runtime.py                   ← 精简宿主依赖合同
│       ├── [A] prompts.py                   ← 阶段提示词与 scope 输入
│       ├── [M] video_parse_prepare.py       ← 幂等准备
│       ├── [M] chunk_scheduler.py           ← 实际批次和分发
│       ├── [M] chunk_interval_listener.py   ← 唤醒与逐 Chunk 放行
│       ├── [M] README.md                    ← 实现进度及验证
│       ├── chunk_understanding/
│       │   └── [M] workflow.py              ← 真实媒体/模型/提交
│       ├── refine/
│       │   ├── [M] input.py                 ← member_scopes
│       │   └── [M] interval_refine.py       ← 独立执行及新写回规则
│       ├── precompact/
│       │   ├── [M] prefix_context_agent.py  ← 后台执行和恢复
│       │   └── [M] tools.py                 ← 目标绑定的受限工具
│       └── design/
│           └── [M] trd.md                   ← 合同已更新；实现中仅核对必要差异
├── engine/
│   └── parsev2/
│       ├── [A] planning.py                  ← 固定拓扑、完整窗口及短窗
│       ├── [A] validation.py                ← 时间/成员/修改范围
│       ├── [M] README.md                    ← 纠正旧架构及进度
│       └── design/
│           └── [M] trd.md                   ← 合同已更新；实现中仅核对必要差异
├── gateway/
│   ├── [M] usage.py                         ← 请求前预算及结束释放
│   ├── [M] schema_normalization.py          ← 联合 Schema 引用处理
│   ├── ark/
│   │   ├── [M] multimodal.py                ← 实际请求/fallback 控制
│   │   ├── [M] sse.py                       ← 关闭本次在途流
│   │   └── [M] embedding.py                 ← 编码期限/取消
│   └── openai/
│       ├── [M] audio.py                     ← ASR 请求控制
│       └── [M] vision.py                    ← 视觉请求控制
├── server/
│   ├── [M] bootstrap.py                     ← V2 宿主与 Worker 装配
│   ├── [M] database.py                      ← 用量 Schema 门禁
│   ├── config/
│   │   ├── [M] settings.py                  ← 执行模式
│   │   └── [M] parse_profile.py             ← 有效配置快照
│   ├── model/
│   │   ├── [M] parse_usage.py               ← 纯 V2 用量关联
│   │   ├── dto/
│   │   │   ├── [M] jobDTO.py                ← V2 执行/租约快照
│   │   │   ├── [M] parseUsageDTO.py          ← V2 用量身份
│   │   │   └── [M] uploadDTO.py              ← 仅 V2 上传完成返回
│   │   └── vo/
│   │       └── [M] uploadVO.py              ← nullable jobId
│   ├── mq/
│   │   ├── [A] video_parse_queue.py         ← 领取/续租/回收/阶段恢复
│   │   └── [A] video_parse_consumer.py      ← 异步生命周期与扫描
│   ├── repository/
│   │   ├── [A] video_parse.py               ← 原子成果/状态/scope/预算/Prefix
│   │   ├── [M] upload.py                    ← 执行路由、短窗恢复、删除保护
│   │   └── [M] parse_usage.py               ← V2 调用记录
│   ├── service/
│   │   ├── [A] video_parse.py               ← 唯一宿主装配/执行入口
│   │   ├── [A] video_parse_media.py         ← 媒体准备和清理
│   │   ├── [A] video_parse_model_calls.py   ← 共享请求预算/并发/取消
│   │   ├── [A] video_parse_index_coordinator.py ← 执行 Engine 计划、编码/ES/完成检查
│   │   ├── [M] upload.py                    ← 仅 V2 幂等返回
│   │   ├── [M] job.py                       ← V2 查询与鉴权
│   │   └── [M] parse_usage.py               ← V2 用量接线
│   ├── storage/
│   │   └── [M] elasticsearch.py             ← 幂等与旧版本覆盖防护
│   ├── api/
│   │   ├── [M] jobs.py                      ← V2 状态查询
│   │   └── [M] uploads.py                   ← View 复用及内部 detail 过滤
│   └── router/
│       └── [M] jobs.py                      ← V2 查询路由
├── docs/
│   ├── design/
│   │   ├── [M] parse-v2-harness.md           ← 同步被替代规则
│   │   ├── [M] video-parse-v2-architecture.excalidraw ← 新索引放行与短窗
│   │   └── [M] video-parse-v2-architecture.svg       ← 同步预览
│   └── migrations/
│       └── [A] 20260928_v2_worker_runtime.sql ← 用量对 V1 job 的非空依赖升级
└── tests/
    ├── agent/
    │   └── video_parse_v2/
    │       ├── [M] test_framework.py        ← 编排和 scope
    │       └── [A] test_runtime.py          ← 运行依赖接线
    ├── engine/
    │   ├── parsev2/
    │   │   ├── [A] test_planning.py         ← 拓扑和短窗
    │   │   └── [A] test_validation.py       ← 时间/成员/修改范围
    │   ├── [A] test_index_pipeline.py       ← 存储中立索引计划与逻辑文档
    │   └── [A] test_embedding_reuse.py      ← 相容性复用策略
    ├── gateway/
    │   ├── [M] test_usage.py                ← 请求前预算/失败计数
    │   ├── [M] test_openai_media.py         ← 媒体请求控制
    │   ├── [M] test_video_embedding.py      ← 编码控制
    │   ├── [M] test_models.py               ← 多模态/fallback
    │   ├── [M] test_sse_proxy.py            ← 流关闭
    │   └── [A] test_schema_normalization.py ← 真实联合 Schema
    └── server/
        ├── [A] test_video_parse_repository.py ← 事务/scope/预算/汇总
        ├── [A] test_video_parse_queue.py      ← 领取/租约/恢复
        ├── [A] test_video_parse_worker.py     ← 完整执行与故障恢复
        ├── [M] test_bootstrap.py              ← 模式及生命周期
        ├── [M] test_config.py                 ← 配置合同
        ├── [M] test_database.py               ← Schema 门禁
        ├── [M] test_upload_service.py         ← 上传/幂等/重试/删除
        ├── [M] test_api.py                    ← V2 查询和权限
        ├── [M] test_parse_usage.py            ← 纯 V2 用量
        ├── [M] test_mysql_integration.py      ← 真实事务/竞争/fence
        ├── [M] test_elasticsearch_storage.py  ← 部分失败/版本保护
        ├── [M] test_elasticsearch_integration.py ← 实际查询可见
        └── [A] test_video_parse_index_coordinator.py ← Engine 计划的物理执行与恢复
```

上一轮预计 78 文件：59 M / 19 A，生产 46、测试 23、文档/样例/迁移 9；其中两份 TRD 的合同更新已完成。生产新增粗估 2,500～4,500 行，测试新增 1,000～2,000 行。本文独立交接文件不计入那次估算。树是预期范围，不是要求改满每个文件；避免无必要的格式化、包装层或测试扩张。

实际用量升级预计放宽 `parse_model_call.job_id`，使用已有 `video_parse_task_id`；不改 V1 attempt 复合主键，不新增预算表。源码中现有 V2 任务状态和字段已够用，别为接线再加 progress / indexed bool。

## 9. 验收清单与验证方式

- [ ] 真实 Agent / Engine / Repository / Worker 贯通；只在网络边界替换模型和 ES，不能 monkeypatch 掉所有关键提交后声称已接通。
- [ ] 多 Worker 不重复领取/提交同一租约；过期 Worker 无法落成果；丢通知可由扫描推进。
- [ ] 完整/短窗口均经过 Refine；90% 批次推进不会等待跨批索引而死锁。
- [ ] 两窗任意提交顺序下 scope 不丢元素；只改 scope 不重算/不重索引；同字段符合提交顺序语义。
- [ ] Chunk 0/1/2/3 的两窗例子准确放行；未就绪等待不记失败、不耗 retry、不长期占 lease。
- [ ] ES 部分失败、ES 成功但 MySQL 完成前中断、旧写入迟到，均有明确可恢复行为。
- [ ] 纯索引恢复时 ASR/Understanding/Refine 调用次数不增加；混合失败各回自己的阶段。
- [ ] 跨 Worker 模型预算/并发有效，失败和重试计数，重启不清零；取消尝试关闭本次请求并阻止迟到提交。
- [ ] Prefix 独立恢复、只改当前目标、scope 保留；正文变化触发正确索引恢复，compact-only 不触发。
- [ ] V2 上传只实际解析一次；历史 PENDING 占位不会自动全量执行；API workspace 隔离且不泄露内部预算/租约 detail。
- [ ] 对外查询门槛继续使用 COMPLETE Video，不把提前写 ES 当成已开放检索。

先做直接保护本次行为的单元/组件检查，随后验证真实 MySQL 和 ES。示例命令（接手时按已创建文件调整）：

```bash
.venv/bin/python -B -m pytest tests/agent/video_parse_v2 tests/engine/parsev2 tests/engine/test_index_projection.py -q
.venv/bin/python -B -m pytest tests/server/test_video_parse_repository.py tests/server/test_video_parse_queue.py tests/server/test_video_parse_worker.py -q
.venv/bin/python -B -m pytest tests/server/test_mysql_integration.py tests/server/test_elasticsearch_integration.py -q
```

MySQL 集成依赖 `ANIVAG_TEST_DATABASE_URL`；ES 集成依赖 `ANIVAG_TEST_ELASTICSEARCH_URL`，按环境可使用 `ANIVAG_TEST_ELASTICSEARCH_API_KEY`。使用隔离测试实例。缺失环境时记录 skipped 及未证明的性质，SQLite/模拟测试不能证明 MySQL 锁竞争或 ES 查询可见性。真实付费模型调用按会话授权执行。

## 10. 新 session 开始时的操作

1. 读取本文和最新两份 TRD，确认当前用户的执行授权；若已要求实现，简短报告任务理解后开始，不重复做设计问卷。
2. 核对当前分支/HEAD、工作区差异与忽略资料，保留用户已有改动。上述路径/估算基于交接时工作区，遇到已被实现的部分先核实再复用。
3. 阅读适用的 check-out、anivag-change-gate、hypnos-code 指引；沿已有同层实现确认分层。
4. 从 §7 A 开始，按具体事务和阶段逐段完成，持续更新实际进度；主链和可选后台都按承诺范围验收。
5. 完成时区分：已运行验证、跳过/受环境阻塞的验证、尚未接通的能力，以及迁移/部署状态。不要用框架测试通过替代整链已运行。

可给下一 session 的开场指令：

> 阅读 `docs/video-parse-v2-handoff.md` 和其中引用的最新 Agent / Engine TRD，继承当前工作目录里的未提交成果。先简要核对任务理解、实现范围与验收标准；按本次消息明确的授权继续。已经确认的 scope、区间、INDEXING 和短窗规则不要再次询问，也不要恢复旧发布表体系。
