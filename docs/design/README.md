# 设计演进与主题索引

整理日期：2026-10-28（Asia/Shanghai）。技术设计统一保存在 `docs/design/`，先按发展阶段归属，再通过本页的主题索引查找。每份文档保留独立职责；跨阶段修订保留在原文，由阶段 README 说明时间与背景。

本页的“定位”区分主设计、专项合同、草案和历史参考，不表示功能已实现或已部署。当前问题与完成状态见 [problems.md](../dev/problems.md)，决策、实验与验收记录见 [devlog.md](../dev/devlog.md)，完整文档分工见 [docs/README.md](../README.md)。

## 发展阶段

| 阶段 | 当时主要回答的问题 | 时间背景与入口 | 归入的原文件 |
|---|---|---|---|
| 01 · MVP | 从导入到可播放证据，第一条链能否成立 | [阶段 README](01-mvp/README.md)；主要为 2026 年 9 月上中旬 | 14 份文档/MDV，13 个附件 |
| 02 · Agent Harness | 已有闭环怎样更可靠、可恢复，架构怎样完善 | [阶段 README](02-agent-harness/README.md)；主要为 9 月中旬至 10 月下旬 | 39 份文档，14 个图文件 |
| 03 · 产品复审 | 从产品目标重新判断哪些能力和复杂度有价值 | [阶段 README](03-product-reassessment/README.md)；10 月 23 日面试反馈后的反思起，10 月 28 日归档整理 | 2 份衔接文档 |

阶段可以重叠，不是严格日历切片；初次提出、后续修订、首次入库、此次整理分别记录。阶段归属不等于文档失效：初版 API 仍可能持续维护，历史方案也不因迁入新目录而重新生效。

第二阶段的图统一放在 [diagram/](02-agent-harness/diagram/)。各模块原 report_docs 的 22 份报告与图已集中到 [docs/report/](../report/README.md)，按实际项目模块层级组织。

每篇文档的归类理由、日期证据和未知项见阶段 README。原根目录文件保持文件名，可从下面的主题索引定位；原 diagram/、diagrams/ 和 MDV 外置资源归入 01-mvp，原第二阶段散图归入 02-agent-harness/diagram。

## 分类导航

- [产品与整体架构](#产品与整体架构)
- [视频解析与理解](#视频解析与理解)
- [检索与索引](#检索与索引)
- [Chat 与 Agent](#chat-与-agent)
- [用户、协作与运行形态](#用户协作与运行形态)
- [模型接入、用量与训练](#模型接入用量与训练)
- [接口与字段合同](#接口与字段合同)
- [实验与历史参考](#实验与历史参考)
- [图表与附件](#图表与附件)

## 产品与整体架构

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| 产品定位 | [product.md](02-agent-harness/product.md) | 产品方向草案：本地优先的视频解析、证据召回及 Local / Managed / Cloud 形态。 |
| Seekino 整体规划 | [seekino-trd.md](../dev/seekino-trd.md) | 10 月 24–25 日的产品与架构复盘：同时冷启动产品、策略、后端的背景，以及剧本、分镜与作品制作目标；原文保留在 docs/dev，作为第三阶段背景入口。 |
| 分层解析体验 | [produce_future_1.md](02-agent-harness/produce_future_1.md) | 候选思路：即时陪看、轻量处理与完整解析的产品分层。 |
| 产品阶段目标 | [各个阶段目标.md](01-mvp/各个阶段目标.md) | 早期阶段规划；用于追溯 MVP 范围，不维护当前任务进度。 |
| 工程职责拆分 | [refactor_1.md](02-agent-harness/refactor_1.md) | 历史重构设计：Engine、Server、Gateway 的职责与调用边界。 |
| 综合技术方案 | [technical_solution copy.mdv](01-mvp/technical_solution%20copy.mdv) | MDV 原始方案文档；保留原格式、Reference 与 Document。 |
| 工程边界补充 | [technical_solution copy.mdv.document.working.md](01-mvp/technical_solution%20copy.mdv.document.working.md) | 原方案的工作补充，自第 14 章记录工程结构决策；不替代原 MDV。 |
| 开发计划原稿 | [re0_dev_plan.mdv](01-mvp/re0_dev_plan.mdv) | MDV 计划材料；按原文日期与上下文阅读。 |
| 工程问题构想 | [各种工程问题构想.mdv](01-mvp/各种工程问题构想.mdv) | MDV 构想材料；保留讨论来源，不作为当前任务清单。 |
| 产品宣传 PV | [seekino-pv-design.md](02-agent-harness/seekino-pv-design.md) | 宣传短片创意、音乐分析、制作方案与历史能力验证。 |

## 视频解析与理解

阅读顺序：先看 Harness 的任务与恢复合同，再看 Engine 的结果与算法边界；声纹、摘要和分镜按各自专题阅读。Event 索引的最终合同另见 [sub-chunk-trd.md](02-agent-harness/sub-chunk-trd.md)。

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| Parse V2 任务编排 | [video-parse-harness-trd.md](02-agent-harness/video-parse-harness-trd.md) | 主 TRD：任务与成果、Prepare、调度、租约、Refine、Prefix 和恢复；同一 ChunkTask 承担 Parse / Index。 |
| Parse V2 领域算法 | [video-parse-engine-trd.md](02-agent-harness/video-parse-engine-trd.md) | 领域合同：解析结果 Schema、时间与人物规则、解析和投影边界；旧检索章节结合最新 Event 合同阅读。 |
| Parse 模型输出协议 | [parse-output-xml.md](03-product-reassessment/parse-output-xml.md) | 设计稿：无缩进 XML、标签与正文分行、任务级语言默认值与程序派生字段；尚未接入生产链路。 |
| 声纹与台词归属 | [voiceprint-trd.md](02-agent-harness/voiceprint-trd.md) | 专项 TRD：声纹准备、比较、原子台词及说话人归属；保留历次实验的适用边界。 |
| 整集摘要 | [video-summarize-trd.md](02-agent-harness/video-summarize-trd.md) | 专项 TRD：官方标题、事实简介、封面与真实集数的证据约束、职责及发布流程。 |
| 分镜解析 | [shot_parse_trd.md](02-agent-harness/shot_parse_trd.md) | 草稿占位，目前仅有 background 提示，尚无完整设计。 |
| Parse V2 早期拓扑 | [parse-v2-harness.md](02-agent-harness/parse-v2-harness.md) | 历史算法与拓扑设计；任务、Index 归属和恢复以 Harness 主 TRD 为入口。 |
| Parse V2 存储接入 | [refactor_2.md](02-agent-harness/refactor_2.md) | 存储与调用链重构记录，含阶段约定和历史实施结果。 |
| V1 / V2 对照 | [oldvsnew_chat.md](02-agent-harness/oldvsnew_chat.md) | 历史调查：V1 产物、compact、Timeline 与 V2 需求的差异。 |
| 多模态理解方向 | [多模态理解解析研究.md](02-agent-harness/多模态理解解析研究.md) | 研究与设计讨论：解析 scope、身份归属、声音与画面理解及能力边界。 |

## 检索与索引

Event / Sub-Chunk 的实施入口是 [sub-chunk-trd.md](02-agent-harness/sub-chunk-trd.md)。视觉媒体输入由视觉检索 TRD 说明；早期 Event、ES 迁移和评审材料保留设计演进，不覆盖后续已确认合同。

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| Event / Sub-Chunk 检索 | [sub-chunk-trd.md](02-agent-harness/sub-chunk-trd.md) | 主 TRD：独立 Event ES 索引、词法与文本／视觉向量、抽帧、发布及恢复合同。 |
| 视觉媒体输入与检索 | [visual-retrieval-trd.md](02-agent-harness/visual-retrieval-trd.md) | 专项 TRD：文字与视觉查询分工，图片、GIF、MP4 输入及检索接入。 |
| Event 早期方案 | [event-retrieval-trd.md](02-agent-harness/event-retrieval-trd.md) | 历史方案与后续合同说明；当前实施依据已转到 Sub-Chunk TRD。 |
| 多模态检索需求 | [multimodel-retrieve-trd.md](02-agent-harness/multimodel-retrieve-trd.md) | 目标 Query Set、召回与评测研究记录；检索需求与候选路线的来源。 |
| ES 统一检索迁移 | [refactor_3_es_qdrant.md](02-agent-harness/refactor_3_es_qdrant.md) | ES / Qdrant 迁移设计与评审历史；混合历史提案和后续修订，按文中基线阅读。 |
| Chunk 索引评审 | [es-chunk-index-review.md](02-agent-harness/es-chunk-index-review.md) | Chunk ES 索引样例、查询设计与评审；配套图表见下文。 |
| 检索开发阶段评审 | [es-development-plan-review.md](02-agent-harness/es-development-plan-review.md) | Engine-first 检索的阶段职责、计划与当时实现评审。 |
| 构图标签 Schema | [shot-composition-schema.md](02-agent-harness/shot-composition-schema.md) | 草案：camera、composition、subjects、frame、content 等构图字段。 |
| 构图标注与排序实验 | [shot-tagging-experiment-brief.md](02-agent-harness/shot-tagging-experiment-brief.md) | 实验设计：构图标注、排序与消融评估；不等同于业务效果验收。 |

## Chat 与 Agent

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| Chat 工具与证据交付 | [agent-chat-delivery-contract.md](01-mvp/agent-chat-delivery-contract.md) | 交付合同与演进记录：Query / Evaluator、业务工具、证据核准及答案交付；Event 工具结合 Sub-Chunk TRD 阅读。 |
| 外部 Agent MCP 接入 | [mcp-access-trd.md](03-product-reassessment/mcp-access-trd.md) | 主 TRD：localhost 工具发现、Event 召回、版本化证据与媒体取用；外部 Agent 承担制作。 |
| Agent Runtime | [agent-runtime-design.md](01-mvp/agent-runtime-design.md) | Runtime 结构与执行边界的设计记录，含旧 Session / 多 Agent 历史方案。 |
| ContextCanvas | [context-canvas-design.md](02-agent-harness/context-canvas-design.md) | 专项设计：Parse / Chat 共用上下文工作区、工具协议、读取与修改边界。 |
| 当前播放位置理解 | [realtime-parse-trd.md](02-agent-harness/realtime-parse-trd.md) | 专项 TRD：实时理解、临时解析与 Supervisor 接线；附执行流程图。 |
| Agent Loop 参考 | [agent-loop-references.md](01-mvp/agent-loop-references.md) | 源码与设计参考：循环出口、Output Node 及前端消费。 |
| 陪看与按需解析 | [agent_harness_refactor.md](02-agent-harness/agent_harness_refactor.md) | 候选产品与 Harness 讨论：当前内容解释、历史召回和按需解析。 |
| 上下文隔离修复 | [agent_harness_context修复.md](02-agent-harness/agent_harness_context修复.md) | 历史讨论与修复记录：上下文隔离、证据读取、草稿和工具输出约束。 |
| Harness 调教 | [agent-harness调教log.md](02-agent-harness/agent-harness调教log.md) | 历史故障与调教日志；后续实施记录统一写入 devlog。 |

## 用户、协作与运行形态

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| 用户与租户 | [user-tenant-trd.md](02-agent-harness/user-tenant-trd.md) | 主 TRD：游客、用户、租户身份、资源归属与隔离边界。 |
| Series 共享 | [series-sharing-trd.md](02-agent-harness/series-sharing-trd.md) | 专项 TRD：Series 共享、团队范围与访问合同。 |
| Local / Cloud | [local-cloud-trd.md](02-agent-harness/local-cloud-trd.md) | 运行形态设计：身份、配置归属、本地接入和云端部署边界。 |

## 模型接入、用量与训练

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| Gateway 模型记录 | [gateway-model-observability-trd.md](02-agent-harness/gateway-model-observability-trd.md) | 主 TRD：统一模型调用记录、观测平台、追踪与采集边界。 |
| Server Token 用量 | [model-token-usage.md](02-agent-harness/model-token-usage.md) | Server 账本合同：采集、持久化、SQL 聚合与公开用量接口。 |
| 本地 Agent 接入 | [codex-bridge-trd.md](02-agent-harness/codex-bridge-trd.md) | Bridge 设计与演进记录：Codex / Claude Code、ACP、Supervisor 和会话绑定。 |
| 云端 Pi Host | [pi-host-trd.md](02-agent-harness/pi-host-trd.md) | Host 专项方案：多用户进程、独立对话与协议；按记录日期阅读，运行接线以源码和 devlog 为准。 |
| 模型选型 | [模型调查.md](01-mvp/模型调查.md) | 模型能力、输入方式及历史对照调查；不是实时模型排行榜。 |
| Qwen 微调 | [Qwen调教.md](01-mvp/Qwen调教.md) | 可行性与实验方案：蒸馏、LoRA 及后续训练路线；不表示已完成训练。 |
| 角色 NPC | [character-npc-trd.md](02-agent-harness/character-npc-trd.md) | 原训练目录中的空白 TRD 占位；尚无需求或技术设计。 |

## 接口与字段合同

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| 前端 API | [frontend-api.md](01-mvp/frontend-api.md) | 上传、解析、检索、任务等前后端接口合同。 |
| Agent API | [agent-api.md](01-mvp/agent-api.md) | Workspace / Conversation / Message、聊天事件与历史数据库升级约定。 |
| Series 背景信息 | [series-background-api.md](01-mvp/series-background-api.md) | 背景正文、参考图片、上传与 Worker 输入的接口流程。 |
| 公共字段命名 | [base-model-field-names.md](02-agent-harness/base-model-field-names.md) | BaseModel 审计字段、用户租户字段及命名迁移合同。 |

## 实验与历史参考

| Feature / 主题 | 文档 | 内容与定位 |
|---|---|---|
| 检索优化思路 | [RAG_perf_1.md](02-agent-harness/RAG_perf_1.md) | 分层召回、细粒度定位、重排与时间证据展开的候选分析。 |
| RAG 调教记录 | [RAGretrieve调教.md](02-agent-harness/RAGretrieve调教.md) | 三路召回、RRF、权重和动态策略的历史机制说明与调教记录。 |
| 视频理解模型实验 | [模型调教log.md](01-mvp/模型调教log.md) | 模型故障、抽帧降级、输入方式及候选路线的历史实验记录。 |
| 潜在问题与验收缺口 | [potential_problems.md](02-agent-harness/potential_problems.md) | VP2 历史观察与验收材料；当前待办状态由 problems 维护。 |

## 图表与附件

图表补充对应 TRD。历史图中的模块路径、阶段标记与固定数字保留原记录时点；设计约定按对应文档的最新修订阅读。

| 主题 | 文件 | 用途 |
|---|---|---|
| Parse 总流程 | [parse_big_graph.png](02-agent-harness/diagram/parse_big_graph.png)、[ChunkUnderstandingWorkflow.png](02-agent-harness/diagram/ChunkUnderstandingWorkflow.png) | 原始解析拓扑与单 Chunk 工作流。 |
| 实时理解 | [realtime_understanding_graph.svg](02-agent-harness/diagram/realtime_understanding_graph.svg) | 当前播放位置理解的设计流程图。 |
| Chunk 索引评审 | [es-chunk-index-review.excalidraw](02-agent-harness/diagram/es-chunk-index-review.excalidraw) | 与 Chunk 评审正文配套的可编辑场景。 |
| 检索阶段计划 | [源场景](02-agent-harness/diagram/es-development-plan-review.excalidraw)、[总图](02-agent-harness/diagram/es-development-plan-review.svg)、[P0–P3](02-agent-harness/diagram/es-development-plan-review-p0-p3.svg)、[P4–P7](02-agent-harness/diagram/es-development-plan-review-p4-p7.svg) | 检索开发阶段评审图。 |
| Cloud 架构 | [源场景](02-agent-harness/diagram/seekino-cloud-architecture.excalidraw)、[SVG](02-agent-harness/diagram/seekino-cloud-architecture.svg)、[PNG](02-agent-harness/diagram/seekino-cloud-architecture.png) | Local / Cloud 架构图。 |
| Cloud 部署 | [源场景](02-agent-harness/diagram/seekino-cloud-deployment.excalidraw)、[SVG](02-agent-harness/diagram/seekino-cloud-deployment.svg)、[PNG](02-agent-harness/diagram/seekino-cloud-deployment.png) | 云端部署图。 |
| 系统组件 | [交互图](01-mvp/diagram/seekino-system-components.html)、[规格](01-mvp/diagram/seekino-system-components.architecture.json) | 系统组件及依赖。 |
| 系统架构 | [交互图](01-mvp/diagrams/seekino-system-architecture.html)、[规格](01-mvp/diagrams/seekino-system-architecture.json) | 系统架构视图。 |
| M1 架构 | [交互图](01-mvp/diagrams/seekino-animation-m1.architecture.html)、[规格](01-mvp/diagrams/seekino-animation-m1.architecture.json) | 动画解析与检索 M1 架构。 |
| 索引流程 | [交互图](01-mvp/diagrams/seekino-indexing.sequence.html)、[规格](01-mvp/diagrams/seekino-indexing.sequence.json) | 索引时序。 |
| 中断恢复 | [交互图](01-mvp/diagrams/seekino-recovery.wide.sequence.html)、[规格](01-mvp/diagrams/seekino-recovery.wide.sequence.json) | 恢复时序。 |
| 检索流程 | [交互图](01-mvp/diagrams/seekino-search.compact.sequence.html)、[规格](01-mvp/diagrams/seekino-search.compact.sequence.json) | 检索时序。 |
| MDV 图片 | [.mdv-assets/](01-mvp/.mdv-assets/) | MDV 关联资源，随原文档保留。 |

## 维护约定

1. 新增技术设计放入对应发展阶段，在该阶段 README 记录提出日期、视角和背景，并在本页对应主题登记。正文持续修订时保留原归属，补充后续演进；文件名表达功能，避免新增泛称 `trd.md`。
2. 同一职责维护一份主设计；补充合同和历史方案明确链接主设计。设计被替代时保留来源，在文档开头及本索引说明后续入口。
3. 模块 README 维护模块使用方式，并链接这里的设计；不在模块内另建设计副本。本文档目录中的草案、日志和 MDV 原稿保留其原有性质。
4. 实施状态统一维护在 problems，实施过程与验证统一维护在 devlog。本索引不复制 TODO / DONE 或部署状态。
5. 新调研继续放 [research/](../research/)，UI 设计继续放 [ui-design/](../ui-design/)，原始实验与验收证据保留各自目录。迁移或改名时同步更新正文、图片、阶段图及模块入口中的链接。

<details>
<summary>2026-10-24 迁移前路径对照</summary>

原路径已移除。历史快照、图中文字和历史记录中的原路径可通过下表定位；快照清单与校验值保留原记录。

| 原路径 | 当前文档 |
|---|---|
| `agent/chat/design/实时parse_trd.md` | [realtime-parse-trd.md](02-agent-harness/realtime-parse-trd.md) |
| `agent/design/utils_context_canvas.md` | [context-canvas-design.md](02-agent-harness/context-canvas-design.md) |
| `agent/video_parse_v2/design/trd.md` | [video-parse-harness-trd.md](02-agent-harness/video-parse-harness-trd.md) |
| `agent/video_parse_v2/design/voiceprint-trd.md` | [voiceprint-trd.md](02-agent-harness/voiceprint-trd.md) |
| `engine/design/multimodel-retrieve-trd.md` | [multimodel-retrieve-trd.md](02-agent-harness/multimodel-retrieve-trd.md) |
| `engine/design/shot-composition-schema.md` | [shot-composition-schema.md](02-agent-harness/shot-composition-schema.md) |
| `engine/design/shot-tagging-experiment-brief.md` | [shot-tagging-experiment-brief.md](02-agent-harness/shot-tagging-experiment-brief.md) |
| `engine/design/多模态理解解析研究.md` | [多模态理解解析研究.md](02-agent-harness/多模态理解解析研究.md) |
| `engine/parsev2/design/trd.md` | [video-parse-engine-trd.md](02-agent-harness/video-parse-engine-trd.md) |
| `engine/parsev2/design/es-chunk-index-review.md` | [es-chunk-index-review.md](02-agent-harness/es-chunk-index-review.md) |
| `engine/parsev2/design/es-development-plan-review.md` | [es-development-plan-review.md](02-agent-harness/es-development-plan-review.md) |
| `engine/parsev2/design/oldvsnew_chat.md` | [oldvsnew_chat.md](02-agent-harness/oldvsnew_chat.md) |
| `server/design/local-cloud-trd.md` | [local-cloud-trd.md](02-agent-harness/local-cloud-trd.md) |
| `server/design/model-token-usage.md` | [model-token-usage.md](02-agent-harness/model-token-usage.md) |
| `server/design/series-sharing-trd.md` | [series-sharing-trd.md](02-agent-harness/series-sharing-trd.md) |
| `server/design/sub-chunk-trd.md` | [sub-chunk-trd.md](02-agent-harness/sub-chunk-trd.md) |
| `server/design/user-tenant-trd.md` | [user-tenant-trd.md](02-agent-harness/user-tenant-trd.md) |
| `gateway/design/trd.md` | [gateway-model-observability-trd.md](02-agent-harness/gateway-model-observability-trd.md) |
| `gateway/bridge/design/codex-bridge-trd.md` | [codex-bridge-trd.md](02-agent-harness/codex-bridge-trd.md) |
| `gateway/pi/design/pi-host-trd.md` | [pi-host-trd.md](02-agent-harness/pi-host-trd.md) |
| `docs/video-summarize-trd.md` | [video-summarize-trd.md](02-agent-harness/video-summarize-trd.md) |
| `docs/dev/seekino_trd.md` | [docs/dev/seekino-trd.md](../dev/seekino-trd.md)（当前实际位置，本次未移动） |
| `training/character_NPC/anon_trd.md` | [character-npc-trd.md](02-agent-harness/character-npc-trd.md) |

`agent/chat/design/realtime_understanding_graph.svg` 与 `engine/parsev2/design/` 的 5 个 Excalidraw / SVG 文件也已迁入本目录，文件名保持不变，见上方图表索引。

</details>
