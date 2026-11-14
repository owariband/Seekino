# 项目报告索引

整理日期：2026-10-28（Asia/Shanghai）。

各模块原 report_docs 中的阶段图、实施证据与交付说明统一移到 docs/report/。目录沿用仓库根目录下实际模块的层级，去掉末尾 report_docs；报告和配套图就近保存，便于一起核对。

| 原目录 | 现目录 | 文件数 |
|---|---|---|
| agent/video_parse_v2/report_docs/ | [agent/video_parse_v2/](agent/video_parse_v2/) | 7 |
| gateway/report_docs/ | [gateway/](gateway/) | 7 |
| gateway/bridge/report_docs/ | [gateway/bridge/](gateway/bridge/) | 3 |
| server/report_docs/ | [server/](server/) | 5 |

设计按发展阶段进入 [设计索引](../design/README.md)，报告按负责模块进入本目录。报告中的时间、分支、测试数量及“已完成/待验收”是原记录的快照；今天的状态看 [管理工作台](../dev/workbench.html)，问题分析看 [problems](../dev/problems.md)，决策与验证沿革看 [devlog](../dev/devlog.md)。

## Docs / dev / workbench

[2026-11-13 Chat 模型窗口与同步压缩本地对照](gateway/agent_model/chat-context-compaction-20261113/README.md)：模型窗口自动发现、85% 触发接线、冻结真实模型边界的对照方法与验证范围；真实秒数与整轮效果按报告证据理解。

[2026-11-03 管理工作台与课程闭环地图](docs/dev/workbench/README.md)：同步 Kimi 工作区 revision 4，新增里程碑概览、闭环地图、验收门槛与四列看板，36 项浏览器检查。门槛结果引用已有 QA 报告，未重跑业务验收。

## Docs / dev / kanban

[2026-11-02 单文件项目看板设计与验收](docs/dev/kanban/README.md)：真实条目导入、唯一状态入口迁移、桌面/移动端与离线保存重开验证。历史业务状态未重新验收。

## Agent / chat

[2026-11-14 JEV-only 默认配置生产上线](agent/chat/query-intent-jev-only-20261113/production-rollout-20261114.md)：dev #155 / main #157 合并、兼容镜像、Cloud API 配置与真实 HTTP／检索 hook 探针；区分部署操作耗时与用户停机，保留未验证范围。

[2026-11-13 关闭独立改写器后的 Chat 三路径复测](agent/chat/query-intent-jev-only-20261113/README.md)：三组全部重跑，B 为 JEV 单独分类，实际 9 次成功分类、零独立改写；完整 FTT／TT／token、输出及卡片证据。原实验没有改 Cloud；后续上线见上一项。

[2026-11-13 Chat 三路径意图判断完整对照](agent/chat/query-intent-routing-20261113/README.md)：Cloud 恢复 JEV 的配置证据，及 DeepSeek 前置决策、JEV＋改写器、Supervisor 直接判断的相同模型实验；首字、完成耗时、用量与内容分别核对。

## Agent / video_parse_v2

- [Refine 只接收逐句声纹预填摘要（2026-11-14）](agent/video_parse_v2/refine-voiceprint-prefill-context-20261114/README.md)：复用已有绑定结果，移除模型输入中的完整参考资料；167 项回归及合成输入字符对照，真实整集另验。
- [剪辑参考模型观察的过度校验修复（2026-11-12）](agent/video_parse_v2/reference-model-judgment-20261112/README.md)：12 条真实失败响应对照、来源边界与独立上传到 MCP 验证；真实批次运行另行记录。

11 月 9 日，片主在《Weekend》两支匿名成片中选了 02。两组收到同样的八段 P5R 笔记，02 的导演多拿到一张自动风格卡。[成片盲评与动态试剪](agent/video_parse_v2/film-refine-ab-20261109/README.md) · [同日文字选片评分](agent/video_parse_v2/refine-ab-20261109/README.md)

剪辑素材的镜头方向、起止构图与回放误差，见[四段控制样例及 10 秒灯塔片实测](agent/video_parse_v2/clip-camera-reference-20261110.md)。

最新实验：[2026-10-29 精简字段与 SHOT 的 JSON/XML 对照](agent/video_parse_v2/parse-compact-shot-20261029.md)。复用本地 Whisper、关闭独立 OCR，同字段 8 次未显示 XML 性能优势；最终选择精简 JSON，不换 XML，不再追求单 Chunk 30 秒，质量优先并沿用既有并行策略。此前的[首轮完整 JSON/XML 对照](agent/video_parse_v2/parse-protocol-20261029.md)独立保留，均未接入生产。

质量复核：[豆包 2.1 Pro non-thinking 的业务问题清单](agent/video_parse_v2/doubao-nonthinking-quality-review-20261029.md)。逐份核对两轮八份原文，区分身份/分段/时间/构图错误、文字遗漏、合同失败与已改善项；无新增模型调用。

背景：Parse V2 的迁移阶段、职责与验收证据。正文版本为 R6 / 2026-10-02 checkpoint，含此前多次基线；本次搬迁不改变阶段完成判断。

- [video-parse-v2-migration-stages-acceptance-01-04.excalidraw](agent/video_parse_v2/video-parse-v2-migration-stages-acceptance-01-04.excalidraw)
- [video-parse-v2-migration-stages-acceptance-01-04.svg](agent/video_parse_v2/video-parse-v2-migration-stages-acceptance-01-04.svg)
- [video-parse-v2-migration-stages-acceptance-05-08.excalidraw](agent/video_parse_v2/video-parse-v2-migration-stages-acceptance-05-08.excalidraw)
- [video-parse-v2-migration-stages-acceptance-05-08.svg](agent/video_parse_v2/video-parse-v2-migration-stages-acceptance-05-08.svg)
- [video-parse-v2-migration-stages-evidence.md](agent/video_parse_v2/video-parse-v2-migration-stages-evidence.md)
- [video-parse-v2-migration-stages.excalidraw](agent/video_parse_v2/video-parse-v2-migration-stages.excalidraw)
- [video-parse-v2-migration-stages.svg](agent/video_parse_v2/video-parse-v2-migration-stages.svg)

## Gateway

[2026-11-03 Flash逐块费用、耗时与质量审计](gateway/deepseek-flash-chunk-audit-20261103.md)，附[117块及115个Refine区间明细](gateway/deepseek-flash-chunk-audit-20261103.html)。只读真实任务；区分none／low、共享Refine费用、原话丢失和时间错配，不把结构通过写成质量通过。

[2026-11-03 DeepSeek Flash 整课费用与恢复](gateway/deepseek-flash-parse-20261103.md)：按同一任务记录协议接线、网络恢复、纯排序与完整 ASR 对白权限修复；保留真实提交／索引身份证据、分段用时及未知用量，不以运行快照替代整课验收。

本机 ASR 性能与恢复边界：[2026-10-30 GPT 整集 Metal 实测](gateway/local/whisper-metal-benchmark-20261030.md)，附[可审阅指标 JSON](gateway/local/whisper-metal-benchmark-20261030.json)。完整 116 分钟音轨转写约 5 分钟；明确区分 CPU WhisperX 的 VAD/对齐、实验落盘与未完成的正式持久恢复。

[2026-11-02 DeepSeek 网页搜索接入](gateway/deepseek-web-search-20261102.md)：独立 dev 基线、官方 HTTP 接入、离线边界与真实页面样本；保留回答质量失败案例。

流式接入：[2026-10-30 正式 Kimi 生成与 Parse 入口验证](gateway/kimi-streaming-20261030.md)。工厂、直接 JSON 与视觉工具 SSE 覆盖；合成图和原 189 帧长 Chunk 均完成，部署与整集效果单独验收。

API 并发：[2026-10-30 Kimi Code 10 路并发实测](gateway/kimi-concurrency-20261030.md)。两轮各 10 路短文本请求全部通过，关闭重试，保留串行对照、逐请求时间与用量；不等于长视频解析吞吐验收。

本机部署：[2026-10-29 WhisperX 三语部署与 Karpathy 解析启动](gateway/local/whisperx-deployment-20261029.md)，包含实际权重、样本、配置切换、验证边界与原始证据位置。

背景：2026-10-07 起的统一模型记录与观测实施。保留当时 G01–G05 的证据、限制和图中状态。

- [model-observability-stages-01-03.excalidraw](gateway/model-observability-stages-01-03.excalidraw)
- [model-observability-stages-01-03.svg](gateway/model-observability-stages-01-03.svg)
- [model-observability-stages-04-05.excalidraw](gateway/model-observability-stages-04-05.excalidraw)
- [model-observability-stages-04-05.svg](gateway/model-observability-stages-04-05.svg)
- [model-observability-stages-evidence.md](gateway/model-observability-stages-evidence.md)
- [model-observability-stages.excalidraw](gateway/model-observability-stages.excalidraw)
- [model-observability-stages.svg](gateway/model-observability-stages.svg)

## Gateway / bridge

背景：正文日期 2026-10-10 的本地 Agent Bridge 分批方案及后续适用范围修订；旧阶段图不代表最新运行架构。

- [agent-bridge-stages.excalidraw](gateway/bridge/agent-bridge-stages.excalidraw)
- [agent-bridge-stages.md](gateway/bridge/agent-bridge-stages.md)
- [agent-bridge-stages.svg](gateway/bridge/agent-bridge-stages.svg)

## Server

- [Event 查询有界内容读取（2026-11-13）](server/service/video_event_retrieval/bounded-query-20261113.md)：生成数据验证 ES 前只读元数据、候选正文有界，完整 detail 延迟加载且仅投影 compact / camera_hints；相关八模块隔离回归 248 passed、4 skipped，摘要与运镜提示保留已验证。

11 月 7 日，micrograd v5 在隔离实例跑完 146 个索引块，用时 90 分 04.884 秒，414 份终态草稿都能对上模型调用。[草稿写入、整集计时与合并后的数据库回放](server/service/parse-output-drafts-20261107.md#micrograd-v5)

11 月 8 日，MyGO 第 3 集单独冷跑，14 分 58.684 秒可检索，562 个事件逐一对上。[同集四次运行和对照条件](server/mq/parse-ready-dispatch/animation.md#dev-ep03-reruns)

[PR #18 范围收口与现有任务交接](server/pr18-scope-handoff-20261103.md)：已验证的解析修复收口，后端查询与 Consumer 转 PR #30；保留同一 v11 任务、音频与 Chunk 成果。

[长任务 MySQL 排序与 Consumer 恢复验收](server/parse-task-query-recovery-20261103.md)：PR #30 的 1038 根因、645 项分组回归、原 v11 持久成果核对及隔离 API 接管证据。

PR 交付核对：[2026-11-01 PR #18 实现提交与整合复核](server/pr18-delivery-20261101.md)，区分已提交代码、基线失败、服务加载与未完成的整课验收。

[2026-11-01 Refine 中断候选的旧实现基线](server/refine-interruption-baseline-20261101.md)：固定 GPT v4 最终进度、成功/失败及含重试等待的中位数、冲突和用量覆盖率；只读留证，新增方案尚未实施。

Chat 正文流式与计量：[2026-11-01 实施与隔离验收](server/chat-streaming-20261101.md)。供应商完成前正文持久化、非流式/缓冲反例、浏览器真实增长和 FTT 采证；真实课程模型与部署仍待试点。

背景：2026-10-08–09 的模型用量交接与验收、2026-10-10 的用户/租户交付。不同报告的版本、环境和测试集合分别阅读。

- [model-token-usage-stages-evidence.md](server/model-token-usage-stages-evidence.md)
- [model-token-usage-stages.excalidraw](server/model-token-usage-stages.excalidraw)
- [model-token-usage-stages.svg](server/model-token-usage-stages.svg)
- [model-usage-handoff-20261008.md](server/model-usage-handoff-20261008.md)
- [user-tenant-handoff-20261010.md](server/user-tenant-handoff-20261010.md)

## Web

[2026-11-12 移动端文档入口与选集密度](web/mobile-docs-episodes-20261112/README.md)：指定Claude Opus 5.5的mini源码指导、DS Flash实施及9场景同条件截图；第二轮采用纯图标入口与240/180ms导航过渡，12项浏览器回归通过；390px下13集总高减少67.67%，真实Cloud与真机效果待发布后验证。

[2026-11-12 聊天页重复解析与连接状态验收](web/chat-frontend-performance-20261112.md)：同生产构建和固定 SSE 下对照历史 Markdown 执行次数、输入与 DOM 观察延迟、Kino 状态和滚动；包含 658 项单测及一个在基线同样失败的布局断言，真实线上效果另验。

## 维护约定

1. 新报告放到与实际负责模块对应的路径，如 docs/report/gateway/bridge/；不要在代码模块下重新建立 report_docs。
2. 文首说明报告时点、范围、代码或环境基线，以及尚未验证的事项；整理日期与实验/提出日期分开。
3. 可编辑场景和导出图与报告共同维护；跳转链接指向现有设计、代码或证据。图中的旧目录文字和历史修改树保留原记录含义。
4. 不在报告中再维护一套实时任务状态；与看板、设计、problems、devlog 建立链接。
5. 历史迁移清单、来源校验值及基准快照仍保留原路径，本索引提供原 report_docs 到新目录的对照。
