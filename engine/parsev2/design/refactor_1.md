# Parse V2 与 Elasticsearch 检索：调查及阶段修改总结

更新日期：2026-09-27。本文是**当前工作区的事实快照**，用于架构 Review 和后续交接；不是新的实现方案，也不是 ES 切换验收报告。工作区同时存在多个会话的未提交修改，下文的“已修改”表示源码中可见，**不将这些修改全部归因于本次文档任务**。本次只填写本文件。

## 1. 开发前置背景与当前范围

- [Parse V2 存储与调用链计划](../../../docs/design/refactor_2.md)要求在旧 V1 链路旁建立 V2 任务、成果和 Worker；[ES 重构主计划](../../../docs/design/refactor_3_es_qdrant.md)提出长期目标：MySQL 保存正式解析成果和任务，ES 承接词项、Chunk 文本语义向量及 Chunk 视频向量，Qdrant 退出目标架构。Engine 定义 Schema、检索投影和融合；Server 承担存储与运行；Harness 消费 Engine Schema。
- 用户随后收窄**当前阶段**：先学习 `../ragflow` 的表和文档→分片→索引→检索链路，建立 ES 在 AniVAG 中替代 Qdrant 定位的架构框架，先审查数据库设计。具体索引、持久化和发布逻辑由用户设计并 Review。已有工作区中的 ES 三表和运行服务不能因“代码已写”就视为已经批准的架构，后续设计决策先做 grill-critical 审查。
- 原计划的最终要求仍需记录：同一视频重新解析或重建索引时，旧版继续可检索；新版完整且可查询后，索引与 MySQL 正式事实按**同一个固定版本**切换。解析轮次、内容并发版本、同步 generation、索引发布版本是不同身份。单独切 ES alias 不构成跨库发布。这是**待实现目标**，不是当前已上线能力。
- 已确认的内容约束：人物直接用人名；默认向量粒度为 Chunk；questions 先进入词项检索，生成问题命中不能伪装成真实对白；`context_before` / `context_after` 指相邻 Chunk；相容的已有向量应优先复用。RAPTOR、GraphRAG 和复杂新重排仍属后续事项。

判断现状应以源码和实际调用链为准。[Engine TRD](trd.md)和[Harness TRD](../../../agent/video_parse_v2/design/trd.md)描述目标，不等于已经运行。[Engine README](../README.md)仍写 Proposal/Patch/Canonical 旧路线，Engine TRD 第一章已改为模型直接产出 `VideoParseResult`；[refactor_2](../../../docs/design/refactor_2.md)中“Schema 仍有 `person_id`”的文字也已过时，当前 [Schema](../schemas.py)已使用人名。

## 2. 已调查

### 2.1 RAGFlow 的数据库和全链路

已按本地 `../ragflow` 源码、[Engine TRD 的 RAGFlow 调查](trd.md#21-ragflow已有的召回解析机制拆解)核对，并整理成可编辑的[全链路图](../../../docs/ragflow-document-ingestion-search.excalidraw)与[ER 图](../../../docs/ragflow-document-er.excalidraw)。核心实体和职责如下；Go ingestion 表属于另一条可选实现路径，不能和默认 Python 路径混画为一套表。

| 位置 | 主要对象 | 解决的问题 |
| --- | --- | --- |
| MySQL，默认 Python 路径 | `tenant`、`knowledgebase`、`document`、`file`、`file2document`、`task` | 租户和知识库范围、文件与文档关系、解析配置/状态、任务领取与进度；不是逐 Chunk 的正文检索库 |
| MySQL，可选 Go ingestion 路径 | `ingestion_task`、`ingestion_task_log`、`pipeline_operation_log` | 记录 Go pipeline 的执行及日志；不要当作默认 Python 解析必经表 |
| Elasticsearch | `ragflow_{tenant_id}` 的 Chunk 文档；另有 `ragflow_doc_meta_{tenant_id}` 文档元数据索引 | 保存可搜索正文、词项/关键词等检索字段、向量和过滤定位信息；文档元数据有独立索引 |

默认 Python 链路可概括为：上传文件并创建文档/任务 → Worker 解析和分片 → 对 Chunk 加工词项、问题等检索表示并调用 embedding → 写入 ES → 按知识库/文档范围做全文与向量召回、融合、回查展示。ES 存的不是“只有一个向量”，而是 Chunk 检索文档。所查主路径没有单独的关系型 Chunk 正文表，也没有 AniVAG 目标所需的跨 MySQL/ES 构建发布表。RAGFlow 重解析会清除旧 Chunk 再重建，不能直接满足 AniVAG 的“旧版持续可检索”要求。以上是**对照观察**，不是建议照搬 RAGFlow 的 ER 结构。

### 2.2 AniVAG 当前调用链与关键缺口

- 上传侧已在同一事务创建 V1 Job 和 V2 `PENDING` 主任务；`video_parse_task`、`chunk_parse_task`、`chunk_parse_content`、`interval_parse_task`、`interval_parse_content` 五张任务/成果表的 ORM 合同已存在。但队列当前仍领取 V1 Job，[Bootstrap](../../../server/bootstrap.py)装配 V1 `EngineVideoParseRuntime`，默认 [ParseAgent](../../../agent/video_parse_v2/agent.py)会停在 Prepare 未实现步骤。`chunk_parse_content` 是可变当前行；已有主任务 ID 或解析轮次不能单独固定一份旧事实。
- 生产 [搜索 API](../../../server/api/search.py)继续调用 V1 `VideoRetrievalService` / Qdrant；Bootstrap 中 ES 客户端仅可选构造，未替换生产检索与同步。Chat Tool、Library 可检索状态和历史证据读取也仍依赖 V1 合同。前端与 `/api/jobs`、SSE 主要消费 V1 Job。
- 新的 Engine 投影摘要已覆盖词项内容及语义输入；但 V1 [index source revision](../../../server/repository/index_sync.py)未覆盖该新投影。`transcript` 仍专指 ASR，词项检索另有 lexical 合同；在生产真正接线前，不能把 ASR 命中当作一般全文命中。
- ES 适配器区分 bulk 写入结果与 search 查询可见性；独立构建服务据此判断 READY。这个机制当前只在独立模块和测试中，真实 ES 行为及生产调度尚未验证。

## 3. 已 Refactor：当前工作区的主要修改树

以下树列出**与 Parse V2 / ES 主题直接有关、当前磁盘可见的主要文件**，不是主计划旧的“预计 60 文件”清单，也不是全部工作区 diff。`[M]` 表示已跟踪文件被修改，`[A]` 表示当前未跟踪新增，`[D]` 表示已跟踪文件被删除。并行的 Agent Chat 等无关变更不归入本主题；完整动态状态以 `git status --short` 为准。

```text
AniVAG/
├── agent/video_parse_v2/
│   ├── [M] README.md、__init__.py                 当前 Harness 框架说明与入口
│   ├── [A] agent.py、input.py、state.py、errors.py  ParseAgent 与配置/状态骨架
│   ├── [A] video_parse_prepare.py、chunk_scheduler.py、chunk_interval_listener.py
│   ├── [A] chunk_understanding/{workflow.py,state.py}
│   ├── [A] refine/{interval_refine.py,input.py}
│   └── [A] precompact/{prefix_context_agent.py,context.py,tools.py}
├── engine/
│   ├── parsev2/
│   │   └── [M] schemas.py                          直接人名、Timeline/整数秒合同
│   └── retrieval/
│       ├── [A] index_projection.py                Chunk 检索投影与版本摘要
│       ├── [M] contracts.py                       lexical 候选与来源合同
│       └── [M] video_retrieval.py                 独立词项通道的归并/融合
├── server/
│   ├── model/
│   │   ├── [M] job.py、video.py、upload.py          V2 任务/成果及上传关系
│   │   ├── vo/
│   │   │   └── [M] searchVO.py                    可选 lexical 结果投影
│   │   └── [A] index_publication.py               ES 构建/发布表草案，待 Review
│   ├── repository/
│   │   ├── [M] upload.py、job.py、library.py        V2 创建/重试/重解析与读取基础
│   │   └── [A] index_publication.py               冻结构建、发布指针仓库草案
│   ├── service/
│   │   ├── [M] upload.py                           上传事务及 V2 任务创建
│   │   ├── [A] embedding_reuse.py                  相容向量复用判定
│   │   ├── [A] es_index_build.py、es_index_cleanup.py
│   │   └── [A] es_published_search.py             固定 build 的独立检索核心
│   ├── storage/
│   │   └── [A] elasticsearch.py                    ES mapping、写入、查询与可见性检查
│   ├── config/
│   │   ├── [M] settings.py、parse_profile.py
│   │   └── [A] elasticsearch.py
│   ├── mq/
│   │   └── [M] mysql_queue.py                    当前仍领取 V1 Job
│   ├── [M] bootstrap.py、cli.py、database.py
│   └── api/
│       ├── [M] uploads.py、schema.py               上传侧 V2 投影
│       └── [M] search.py                         仍由旧生产检索链服务
├── evals/recall/
│   ├── [M] cli.py、recall_eval.py
│   ├── [D] qdrant_index.py
│   └── [A] elasticsearch_index.py                 离线 ES 双向量适配
├── deploy/
│   └── [M] README.md                              可选 ES 环境说明
├── docs/
│   ├── migrations/
│   │   ├── [A] 20260927_video_parse_v2_tasks.sql  旧库升级草案，未执行
│   │   └── [A] 20260927_es_index_publication.sql   ES 发布表草案，未执行
│   ├── [A] ragflow-document-ingestion-search.excalidraw
│   └── [A] ragflow-document-er.excalidraw
├── scripts/
│   └── [M] local_services.py                     显式 ES 配置时的本地检查
├── tests/
│   ├── agent/video_parse_v2/
│   │   ├── [A] test_framework.py
│   │   └── [A] test_input.py
│   ├── engine/
│   │   ├── parsev2/
│   │   │   └── [M] test_schemas.py
│   │   ├── [A] test_index_projection.py
│   │   └── [M] test_video_retrieval.py
│   ├── server/
│   │   ├── [M] test_upload_service.py、test_mysql_integration.py
│   │   ├── [A] test_index_publication_repository.py、test_es_index_build_service.py
│   │   ├── [A] test_es_index_cleanup.py、test_es_published_search.py
│   │   └── [A] test_elasticsearch_storage.py、test_cli_elasticsearch.py
│   └── evals/
│       ├── [A] test_elasticsearch_index.py
│       └── [D] test_qdrant_index.py
├── web/src/
│   ├── api/
│   │   └── [M] contracts.ts                       可选 lexical 输出合同
│   └── features/search/
│       └── [M] explain.ts                         命中来源说明
├── [M] .env.example
├── [M] compose.yaml
├── [M] pyproject.toml
└── [M] uv.lock
```

V2 批次 0～2 的逐文件历史交付记录见[refactor_2 第 7.1 节](../../../docs/design/refactor_2.md#71-2026-09-27-批次-02-实际交付记录)；ES 分批记录见[主计划第 13 节](../../../docs/design/refactor_3_es_qdrant.md#13-2026-09-27-实施状态与交接)。本树中的 ES 三表、迁移和相关 Service 是**当前工作区中的待评审实现草案**；列出它们不代表用户已经批准其表结构或运行策略。

## 4. 综合起来，当前代码已具备什么

| 范围 | 已有代码能力 | 尚不能宣称 |
| --- | --- | --- |
| Parse V2 批次 0～2 | `episode_order` 贯穿上传；固定解析配置快照；上传完成创建 V2 `PENDING` 主任务并保留 V1 Job；retry 沿用任务、主动 reparse 创建新解析轮次且保留历史；五张 V2 任务/成果表有 ORM/约束 | V2 Worker 已消费任务、模型已产出正式成果、Chunk/Interval 业务成果已写入 |
| Engine 与检索合同 | 当前 Schema 使用 `persons + timeline`、直接人名和整数秒；Chunk 词项/语义投影及其摘要、lexical 融合、生成问题与原话证据区分已有纯逻辑；前端和 VO 有可选 lexical 说明 | 生产已经做三路 ES 检索；旧 V1 source revision 已覆盖新词项；Chat 的 live Tool 已切 lexical |
| ES 独立基础 | ES mapping、bulk 逐项错误、search 可见性核对、构建与固定 build 查询核心、手动 `init/sync/repair/cleanup/search-es-published` CLI、可选配置/Compose profile、离线双向量评测适配均有代码 | ES 三表设计已批准；正式 V2 Writer/自动调度/业务 Evidence 已接；默认部署、生产 API 或数据已切 ES |
| 版本与恢复草案 | 冻结构建快照、desired/published 指针、租约/fence、READY 条件发布、tombstone 枚举及相容向量复用判定有独立代码和替身测试 | 旧版持续服务和新版完整后跨库切换已经过真实并发/故障演练；删除/对账/回收形成闭环 |

**验证边界：**本次总结过程仅做只读审计和文档写入；定向执行的 Harness/Engine Schema 测试为 **55 passed**，只证明框架和 Schema。`refactor_2` 记录了此前隔离 MySQL 8.4 空库建表和 V2 上传/任务集成测试；那不包含已有业务库升级。主计划记录了 ES 替身/单元测试及静态检查，未完成真实 ES + 发布 MySQL 联合验证。上述历史数字不代表本次重跑后的全量回归。两份 2026-09-27 迁移 SQL 均未用于已有业务库；未进行数据切换、服务重启、commit 或 push。

| 验收层次 | 当前状态 |
| --- | --- |
| 代码基础 | **部分完成**：V2 上传/任务基础与 ES 独立组件已写，生产主链未切换 |
| 真实环境验证 | **局部完成**：V2 空库/上传任务历史集成有记录；ES 与 ES 发布表的真实联合验证未完成 |
| 业务数据迁移和切换 | **未执行**；Qdrant 仍服务当前生产检索 |
| V2 整版发布 | **未完成**；Harness、正式成果 Writer、生产读取/索引/查询未贯通 |

## 5. TODO 与待实现功能

### 5.1 先交用户做架构 Review 的问题

1. 以两张 RAGFlow 图和当前源码为材料，确定 AniVAG 需要哪些 MySQL 事实/任务/索引状态表、ES Chunk 文档字段，以及两边的身份和关系。现有 `es_index_build`、`es_index_build_chunk`、`es_video_publication` 是待审草案，可保留、调整或撤销；不能把它们自动算成定稿。
2. 明确 V2 正式成果的组装、不可变快照/读取合同，以及解析轮次、内容并发版本、同步 generation、索引发布版本的边界；规定新构建如何绑定同一固定事实版本。`chunk_parse_content` 当前行会被后续 Refine/Prefix 更新，不能直接充当旧版事实。
3. Review 切换、失败与回收策略：何时证明 ES **可查询**、何时允许 MySQL 发布指针切换；部分写入、超时、过期回调、删除交错和旧版保留期如何处理。仅有 ES bulk 成功或 alias 切换不够。这里列的是必须回答的问题，不代替用户决定实现方案。

### 5.2 已知的后续工程缺口（不属于本次文档改动）

1. **V2 解析主链：**让 Worker 领取 V2 主任务；完成 Prepare、Chunk/Interval 初始化、媒体/ASR/OCR/模型调用、Engine 校验、按租约原子提交、Listener/Refine/Prefix compact、费用与取消管理。再将 `/api/jobs`、SSE、Library 和前端切到正确的 V2 身份/状态；修复过渡期 `expectedJobVersion` 混用 V1 Job 与 V2 解析版本的问题。
2. **正式成果与索引：**在用户确认持久化合同后实现 V2 Writer/Repository，按正式成果生成投影和可信事实快照；把词项投影纳入实际索引源版本；只在编码器、输入、维度、采样等合同相容时复用向量。
3. **生产查询与发布：**按一次查询固定发布 build map，三路 ES 召回和 MySQL 事实回查使用同一版本；把 opaque 快照装配成业务 Evidence；让搜索 API、Chat Tool/历史引用与可检索性门槛使用新合同，同时保持 transcript=ASR、生成问题仅作召回线索。补齐自动同步/对账、删除 tombstone、重试与旧构建回收。
4. **评测、CLI、部署和切换：**离线 V2 词项+双向量评测、生产/CLI/测试/部署的统一接入仍需做；现有离线 ES 适配只覆盖旧双向量场景。隔离环境验证真实 ES analyzer/KNN/refresh/bulk 故障及 MySQL 并发/迁移，再做已有库升级和数据切换。未切换前保留 Qdrant 运行依赖和旧数据。

本文件位于 Git 忽略的 `**/design/` 路径，普通 `git status` 不会显示它；交接或 Review 时需直接打开该文件。主计划和两份 TRD 也应按磁盘内容读取，不能从 Git 状态推断设计目标已经实现。
