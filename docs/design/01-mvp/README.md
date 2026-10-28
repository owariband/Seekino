# 01 · MVP：从 0 到 1

整理日期：2026-10-28（Asia/Shanghai）。归类依据：文档最初要解决的问题、正文日期、MDV 版本元数据及 Git 历史；文件修改时间不作为提出时间。

阶段归属与当前有效性分开记录。跨阶段修订的文档保留一份正文；表中“后续”说明演进，不把旧文中的“当前”“已完成”解释为今天的状态。实施状态仍由 [problems](../../dev/problems.md) 维护，决策和验证见 [devlog](../../dev/devlog.md)。

## 当时的视角与背景

核心问题是“这条链能不能成立”：导入一组视频，得到解析与索引，再用自然语言找回可播放、可定位的证据。优先验证算法可用性和最小工程闭环，再讨论规模化、长期运行与更丰富的业务。

主要可核对材料集中在 2026 年 9 月上中旬；与下一阶段在 9 月中旬重叠。这里保留首版 API 和 Agent 交付合同，即使它们后来继续维护，也不按最近修改日期重新分期。

### 当时的个人处境：一边实习，一边冷启动完整项目

我在 **2026-04-27 入职实习**；到 9 月上中旬形成这些 MVP 材料时，已实习约四个半月。项目是在实习之外独立推进的，开发主要依靠自己与 AI 协作，完整产品的设计、开发和交付经验还在积累。入职日期由我在 2026-10-28 整理时补充，实习时长据此估算。

按 [10 月 23 日的 reflection](../../dev/reflection.md) 对最初动机的回顾，我想做的是“自动解析本地素材，再由 Codex 完成剪视频的一条龙服务”。早期先打通解析、召回和证据播放，是为了让这个想法有一条能实际运行的基础链路；当时还没有把最终制作体验、用户需求与技术范围梳理清楚。

在后来的 [整体规划复盘](../../dev/seekino-trd.md) 中，我把这段经历概括为同时冷启动产品、策略和后端：产品价值定位要摸索，后端系统要从零搭，RAG 与视频解析的任务定义也在探索。因此这里的文档既记录项目从 0 到 1，也记录我如何靠具体问题建立知识和工程经验。这个解释来自后来的复盘，不代表写第一份方案时已经意识到了所有问题。

## 文档时间与提出背景

| 文档 | 时间归属与证据 | 提出时的视角与背景 | 后续演进或阅读边界 |
|---|---|---|---|
| [各个阶段目标.md](各个阶段目标.md) | 未署日期；Git 首见 2026-10-23，提出日不详 | 从本地单用户端到端闭环确定 MVP 范围 | 按内容归属；早期阶段规划，不维护今天的任务状态 |
| [re0_dev_plan.mdv](re0_dev_plan.mdv) | 未署日期；提出日不详 | 在尚缺 Server、Worker 和前端时，以纵向切片搭建第一条链 | 计划主要保存在 Reference；Document 为空，保留原容器 |
| [technical_solution copy.mdv](technical_solution%20copy.mdv) | Reference 初始版本 2026-09-10；M1 基线 09-14；当前正文另有 09-19 修订 | 确定导入、解析、双路检索、时间证据与持久化的最小系统 | 日期取 MDV 内部版本元数据；后续内容不改写初始归属 |
| [technical_solution copy.mdv.document.working.md](technical_solution%20copy.mdv.document.working.md) | 未署日期；Git 首见 2026-10-23，提出日不详 | 补足 M1 工程目录、持久化、入口及实施记录 | 续接原方案第 14 章；是工作补充，不替代 MDV |
| [各种工程问题构想.mdv](各种工程问题构想.mdv) | 未署日期；提出日不详 | 围绕 M1 本地部署讨论 MySQL/Qdrant、队列与恢复 | 保留原 Reference/Document，后续存储变更另见第二阶段 |
| [模型调查.md](模型调查.md) | 正文最后更新 2026-09-13 | 为最初的视频证据解析选择可用模型和输入方式 | 历史模型比较与实验，按当时版本阅读 |
| [模型调教log.md](模型调教log.md) | 正文更新 2026-09-13 | 处理原生视频失败、抽帧降级和模型组合的实际阻塞 | 记录早期可运行链的故障与实验，不作当前排行榜 |
| [Qwen调教.md](Qwen调教.md) | 正文更新 2026-09-15 | 本地视觉模型可靠性不足，研究蒸馏和微调可行性 | 方案与训练完成分开；正文未声称完成训练 |
| [frontend-api.md](frontend-api.md) | Git 最早可追溯 2026-09-15；正文含 09-18、10-08 修订 | 让 Web 与上传、任务、检索及播放闭环对接 | 持续维护的接口文档；早期归属不表示接口已经退役 |
| [agent-api.md](agent-api.md) | Git 最早可追溯 2026-09-15；正文更新 09-18 | 首个可运行纵向闭环的对话、消息与事件对接 | 后续会话结构演进保留在同一文档 |
| [series-background-api.md](series-background-api.md) | 未署日期；Git 最早可追溯 2026-09-16 | 向解析流程提供 Series 背景文字和参考图片 | 以 Git 日期作存在证据，不冒充精确提出日 |
| [agent-runtime-design.md](agent-runtime-design.md) | 正文 2026-09-18；保留 09-15 早期背景 | 设计首版 Query/Evaluator/Chat 的执行和交付 | 旧 Session、两 Agent、恢复提案在正文中有历史边界 |
| [agent-chat-delivery-contract.md](agent-chat-delivery-contract.md) | 初版正文 2026-09-18；后续 Event 合同 10-17 | 让初版检索、核验与 Chat 交付形成可验证闭环 | 跨阶段文档；后续 Event 合同链接到第二阶段 |
| [agent-loop-references.md](agent-loop-references.md) | 正文核对 2026-09-15；10-24 清理说明 | 为初版 Agent 循环出口和前端消费寻找实现参考 | 源码阅读记录；不是新一轮运行架构定案 |

## 图与原始附件

以下系统组件、M1 架构和时序图按内容关联到初步搭建阶段；精确制图日期未从正文确认。文件迁入 Git 或品牌更名的日期不视为原始设计日期。图中未来能力是当时的规划，不能当作当前实现证明。

| 附件 | 关联背景 |
|---|---|
| [9756a880ada5866c41ae5e120e3b99f3042bb52471679e92061b63343c30aee7.png](.mdv-assets/d_3d59b8f638d76fe7fbffb8764f6f01c2/9756a880ada5866c41ae5e120e3b99f3042bb52471679e92061b63343c30aee7.png) | 原始 MDV 的外置图片；与原文保持相对目录关系 |
| [seekino-system-components.architecture.json](diagram/seekino-system-components.architecture.json) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-system-components.html](diagram/seekino-system-components.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-animation-m1.architecture.html](diagrams/seekino-animation-m1.architecture.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-animation-m1.architecture.json](diagrams/seekino-animation-m1.architecture.json) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-indexing.sequence.html](diagrams/seekino-indexing.sequence.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-indexing.sequence.json](diagrams/seekino-indexing.sequence.json) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-recovery.wide.sequence.html](diagrams/seekino-recovery.wide.sequence.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-recovery.wide.sequence.json](diagrams/seekino-recovery.wide.sequence.json) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-search.compact.sequence.html](diagrams/seekino-search.compact.sequence.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-search.compact.sequence.json](diagrams/seekino-search.compact.sequence.json) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-system-architecture.html](diagrams/seekino-system-architecture.html) | M1 系统组件、架构或建库/检索/恢复时序 |
| [seekino-system-architecture.json](diagrams/seekino-system-architecture.json) | M1 系统组件、架构或建库/检索/恢复时序 |

## 如何接着读

初版闭环的技术限制进入 [02 · Agent Harness](../02-agent-harness/README.md)。今天对成本、用户价值和作品交付的重新判断进入 [03 · 产品复审](../03-product-reassessment/README.md)；不要用后来的目标反写初版设计的动机。

[返回设计总索引](../README.md)
