# 02 · Agent Harness：架构形成后的完善

整理日期：2026-10-28（Asia/Shanghai）。归类依据：文档最初要解决的问题、正文日期、MDV 版本元数据及 Git 历史；文件修改时间不作为提出时间。

阶段归属与当前有效性分开记录。跨阶段修订的文档保留一份正文；表中“后续”说明演进，不把旧文中的“当前”“已完成”解释为今天的状态。实施状态仍由 [problems](../../dev/problems.md) 维护，决策和验证见 [devlog](../../dev/devlog.md)。

## 当时的视角与背景

视频到证据的初步闭环已经形成，问题转向“怎样让这套系统可靠、可恢复、可扩展”：控制上下文，明确任务与成果，改造 Parse V2，完善检索、身份、运行形态与用量观测。

主要材料覆盖 2026 年 9 月中旬至 10 月下旬。这个阶段也有产品定位、陪看、视觉查询和云端体验的探索；将它们留在提出时的阶段，能让第三阶段看清每个设想的来源。

### 当时的个人处境：工程经验在增长，产品取舍还没有跟上

hypnos 于 **2026-04-27 入职实习**，这一阶段覆盖实习约四个半月至接近半年的时期。当时 hypnos 仍在一边实习、一边推进 Seekino；首个闭环跑通后，开始直接面对上下文溢出、任务恢复、数据一致性、检索质量等真实工程问题，技术设计也逐渐从搭骨架变成维护一套相互关联的系统。入职日期由 hypnos 在 2026-10-28 补充，阶段内实习时长为估算。

但从 [10 月 23–24 日的 reflection](../../dev/reflection.md) 回看，hypnos 当时没有充分把个人时间当成有限的人力：一个人同时承担产品、研发、测试、运维等工作，又不断加入计划外的新功能，缺少稳定的交付期限和外部反馈。实习中的事务切换让个人时间更碎片化，文档、表达和思路整理也没有跟上开发。这些是 hypnos 事后的复盘，不是当时已经建立好的工作方法。

因此，本阶段的技术补强有实际问题来源，也夹杂了范围扩张和方向探索。此前已经写过产品定位草案，但还没有持续用用户体验、整体成本和可交付结果判断每项改动。10 月的二面反馈促使 hypnos 重新审视一个反差：同行围绕剧本和分镜做得更收敛，Seekino 的复杂度却持续增加。这个问题引向 [第三阶段的产品复审](../03-product-reassessment/README.md)，并不意味着此前所有技术设计都要推倒。

## 文档时间与提出背景

| 文档 | 时间归属与证据 | 提出时的视角与背景 | 后续演进或阅读边界 |
|---|---|---|---|
| [refactor_1.md](refactor_1.md) | 2026-09-16–17 的正文与 Git 记录 | 收敛 Engine、Server、Gateway 职责和调用边界 | 从首个闭环转向架构完善的衔接材料；历史目录和命令保留 |
| [refactor_2.md](refactor_2.md) | 正文 2026-09-27，含 09-24 背景 | 为 Parse V2 重构存储模型与调用链 | 含历史与后续修订，任务合同结合 Harness 主 TRD 阅读 |
| [refactor_3_es_qdrant.md](refactor_3_es_qdrant.md) | 正文 2026-09-28，含 09-27 背景、10-17 修订 | 统一 ES 检索，重新划分索引与发布职责 | 旧提案与最终 Event 合同分开 |
| [agent-harness调教log.md](agent-harness调教log.md) | 正文 2026-09-17、09-19 | 已能检索后暴露 context overflow、工具输出和规模问题 | 第二阶段在初版闭环期间已开始，日期与 MVP 重叠 |
| [agent_harness_context修复.md](agent_harness_context修复.md) | 正文 2026-09-19 | 通过上下文隔离、有界读取与独立证据空间控制膨胀 | 保留讨论和修复背景，不把方案全文当作现行实现 |
| [agent_harness_refactor.md](agent_harness_refactor.md) | 未署日期；Git 首见 2026-10-23，提出日不详 | 从片段搜索扩展到课程暂停答疑、历史回忆和按需解析 | 早期产品探索；第三阶段复审时作为背景引用 |
| [context-canvas-design.md](context-canvas-design.md) | 正文更新 2026-10-10；含 10-22–23 演进 | 为 Parse/Chat 提供共用上下文工作区与工具协议 | 正文已说明 Chat Canvas 后来移除；Parse/通用能力另有边界 |
| [parse-v2-harness.md](parse-v2-harness.md) | 正文收敛日期 2026-09-28；10-14/17 后续基线 | 将 V2 解析拓扑、并发、区间状态和恢复规则固化 | 旧 Interval INDEX 设计被后续主合同替代 |
| [video-parse-harness-trd.md](video-parse-harness-trd.md) | 含 2026-10-06 记录；10-14 设计基线，10-17 补充 | 定义任务、租约、调度、Prepare/Refine/Index 与恢复 | 持续维护的主合同；整理目录不代表完成其验收 |
| [video-parse-engine-trd.md](video-parse-engine-trd.md) | 含 2026-09-26–28 记录；10-17 Event 合同修订 | 建立事件 Schema、时间人物规则与领域算法边界 | 历史检索章节与后续 Harness/Event 合同对照 |
| [oldvsnew_chat.md](oldvsnew_chat.md) | 正文调查 2026-09-24 | 比较 V1 产物、compact 与 V2 所需 Timeline | 历史迁移依据 |
| [voiceprint-trd.md](voiceprint-trd.md) | 正文可见 2026-10-18–22 讨论、实验及后续修订 | 解决原子台词、说话人归属和跨片段身份 | 按每次实验版本阅读；部分证据已退役 |
| [video-summarize-trd.md](video-summarize-trd.md) | 未单列提出日；含 20261021 迁移引用，Git 首见 10-23；10-29 标题规则修订 | 基于解析证据生成本集标题、简介、封面与真实集数；正式标题优先，无标题时生成内容短标题；未看系列选 Summary 封面、已看显示上次画面 | 初版精确提出日仍不详；10-29 本机 12 秒片 Kimi 与页面验收见正文 |
| [realtime-parse-trd.md](realtime-parse-trd.md) | 正文含 2026-10-10 记录 | 复用当前播放位置附近的解析，支持临时理解与缓存 | 由早期陪看需求引出的专项技术合同 |
| [多模态理解解析研究.md](多模态理解解析研究.md) | 初次整理 2026-10-17；声纹更新 10-18；10-24 退役说明 | 拆解视觉、声音、身份与构图理解的能力边界 | 研究、实验、候选方案分开；原实验恢复条件看清理记录 |
| [RAG_perf_1.md](RAG_perf_1.md) | 含 2026-09-24 复核；首段提出日不详 | 评估多路召回、细粒度定位、重排和时间证据展开 | 技术候选及机制分析，供第三阶段做收益复审 |
| [RAGretrieve调教.md](RAGretrieve调教.md) | 未署日期；Git 首见 2026-10-23，提出日不详 | 解释已有三路召回、RRF、权重和动态调整 | 按机制与架构背景归类；当前通道收益须另做实验 |
| [multimodel-retrieve-trd.md](multimodel-retrieve-trd.md) | 正文含 2026-10-13–17；目标 Query Set 为 10-16 | 用真实目标问题检查召回能力与评测路线 | 已有产品需求视角；仍是本轮产品复审之前的需求来源 |
| [visual-retrieval-trd.md](visual-retrieval-trd.md) | 正文 2026-10-15，10-16/17 补充 | 定义图片、GIF、MP4 与文字查询的职责和接入 | 目标设计与已实现能力分开 |
| [event-retrieval-trd.md](event-retrieval-trd.md) | 正文 2026-10-17 | 弥补 Chunk 粒度下细节证据的检索缺口 | 保留早期 Event 方案，最终合同另见 Sub-Chunk TRD |
| [sub-chunk-trd.md](sub-chunk-trd.md) | 正文 2026-10-17；更新 10-21 | 确定独立 Event ES、词法/语义/视觉及发布恢复合同 | 这是第三阶段 Event-first 复审的已有技术基础 |
| [es-chunk-index-review.md](es-chunk-index-review.md) | 正文 2026-09-28–29；10-17 后续合同 | 从 Chunk 样本审查 ES 索引和查询设计 | 附件移到 diagram；旧方案按日期阅读 |
| [es-development-plan-review.md](es-development-plan-review.md) | 正文 2026-10-01–02 | 分解 Engine-first 检索的职责、计划与实施证据 | 附件移到 diagram；阶段标记是当时快照 |
| [shot-composition-schema.md](shot-composition-schema.md) | 草案 2026-10-19；10-24 归档说明 | 实验性刻画 camera/composition/subjects 等镜头信息 | 不是生产合同；实验资产另有恢复条件 |
| [shot-tagging-experiment-brief.md](shot-tagging-experiment-brief.md) | 实验任务 2026-10-19；10-24 归档说明 | 用标注、排序与消融判断构图字段是否有价值 | 历史实验设计，不自动成为新的执行任务 |
| [product.md](product.md) | 正文标注产品方向草案 2026-09-20 | 探索本地优先、视频证据召回与 Local/Managed/Cloud | 保留早期定位；本次产品复审通过引用评估其中假设 |
| [produce_future_1.md](produce_future_1.md) | 未署日期；Git 首见 2026-10-23，提出日不详 | 把解析拆成即时陪看、快速索引及更完整的理解层次 | 候选产品思路，不因整理而视为已采纳的路线图 |
| [seekino-pv-design.md](seekino-pv-design.md) | 正文含 2026-10-09–10 | 通过宣传片叙事梳理产品能力与制作方式 | 旧名称为 anivag-pv-design.md；品牌更名不改变提出时期 |
| [local-cloud-trd.md](local-cloud-trd.md) | 正文更新 2026-10-24；早期提出日不详 | 区分地址、凭证、数据源和本地/云端运行责任 | 连续演进的技术与产品合同，最新判断仍要核对证据 |
| [user-tenant-trd.md](user-tenant-trd.md) | 正文更新 2026-10-10；含 10-09 背景 | 为公开体验和登录用户定义身份、归属、额度与隔离 | 部署和迁移状态按原记录理解 |
| [series-sharing-trd.md](series-sharing-trd.md) | 正文 2026-10-19；后续实施修订 | 定义个人公开/私有分享，讨论后续团队范围 | 阶段二团队方案仍按正文提案边界阅读 |
| [codex-bridge-trd.md](codex-bridge-trd.md) | 历史目标 2026-10-10；10-21 等后续接入记录 | 复用本地 Agent runtime，并明确会话和工具接线 | 旧 ACP 阶段图是历史预算；报告现归 docs/report/gateway/bridge |
| [pi-host-trd.md](pi-host-trd.md) | 正文 2026-10-21；10-24 退役说明 | 探索云端多用户运行和独立会话职责 | 已退役路线；不因归入 Harness 而恢复为实施方案 |
| [gateway-model-observability-trd.md](gateway-model-observability-trd.md) | 正文 2026-10-07 | 统一模型调用记录、追踪和采集边界 | 配套验收报告现归 docs/report/gateway |
| [model-token-usage.md](model-token-usage.md) | 正文更新 2026-10-09 | 建立 Server 侧 Token 账本、聚合与公开接口 | 历史运行成绩保留范围；配套报告现归 docs/report/server |
| [base-model-field-names.md](base-model-field-names.md) | 正文与 Git 记录 2026-10-10 | 统一审计字段、时间、用户和租户命名 | 工程字段合同，非本轮产品能力变更 |
| [potential_problems.md](potential_problems.md) | 正文记录 2026-10-02–06 | 记录 V2 实施中暴露的锁序、索引及验收缺口 | 历史问题材料；今天的状态由 problems 维护 |
| [character-npc-trd.md](character-npc-trd.md) | 空文件；Git 最早可追溯 2026-10-18 | 原训练目录中的占位，背景无法从正文判断 | 暂存本阶段、归属待核；不是已确认的 NPC 产品需求 |
| [shot_parse_trd.md](shot_parse_trd.md) | 仅有 background；Git 最早可追溯 2026-10-25 | 分镜解析占位，缺少可判断提出视角的正文 | 暂存本阶段、归属待核；不推定第三阶段已有完整设计 |

## 图统一放在 diagram

本阶段 14 个图文件集中在 diagram/，正文保留在阶段目录。可编辑场景与导出的 SVG/PNG 同目录、同名关联；移动不重新绘制图、不更改图内历史阶段或实验数字。

图的精确制作日期若未单独注明，以关联正文的记录时点作为阅读背景，不将正文日期冒充制图日期。

| 主题与时间背景 | 文件 |
|---|---|
| Parse V2 拓扑与单 Chunk 工作流；参照 V2 设计演进 | [parse_big_graph.png](diagram/parse_big_graph.png)、[ChunkUnderstandingWorkflow.png](diagram/ChunkUnderstandingWorkflow.png) |
| 实时理解；参照 2026-10-10 的正文记录 | [realtime_understanding_graph.svg](diagram/realtime_understanding_graph.svg) |
| Chunk 索引评审；参照 2026-09-28–29 的正文 | [es-chunk-index-review.excalidraw](diagram/es-chunk-index-review.excalidraw) |
| 检索阶段评审；参照 2026-10-01–02 的正文 | [es-development-plan-review.excalidraw](diagram/es-development-plan-review.excalidraw)、[es-development-plan-review.svg](diagram/es-development-plan-review.svg)、[es-development-plan-review-p0-p3.svg](diagram/es-development-plan-review-p0-p3.svg)、[es-development-plan-review-p4-p7.svg](diagram/es-development-plan-review-p4-p7.svg) |
| Local/Cloud 架构；正文持续修订至 2026-10-24 | [seekino-cloud-architecture.excalidraw](diagram/seekino-cloud-architecture.excalidraw)、[seekino-cloud-architecture.svg](diagram/seekino-cloud-architecture.svg)、[seekino-cloud-architecture.png](diagram/seekino-cloud-architecture.png) |
| Local/Cloud 部署；正文持续修订至 2026-10-24 | [seekino-cloud-deployment.excalidraw](diagram/seekino-cloud-deployment.excalidraw)、[seekino-cloud-deployment.svg](diagram/seekino-cloud-deployment.svg)、[seekino-cloud-deployment.png](diagram/seekino-cloud-deployment.png) |

## 与另外两个阶段的关系

- 起点见 [MVP](../01-mvp/README.md)：首个闭环中暴露的问题逐步形成这里的技术设计。
- 当前复审见 [产品复审](../03-product-reassessment/README.md)：技术能力和候选通道是否值得保留，按产品任务和实验收益重新判断。
- 实施阶段图、验收证据和交付说明统一见 [报告索引](../../report/README.md)，按实际项目模块路径组织。

[返回设计总索引](../README.md)
