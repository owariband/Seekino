# 项目报告索引

整理日期：2026-10-28（Asia/Shanghai）。

各模块原 report_docs 中的阶段图、实施证据与交付说明统一移到 docs/report/。目录沿用仓库根目录下实际模块的层级，去掉末尾 report_docs；报告和配套图就近保存，便于一起核对。

| 原目录 | 现目录 | 文件数 |
|---|---|---|
| agent/video_parse_v2/report_docs/ | [agent/video_parse_v2/](agent/video_parse_v2/) | 7 |
| gateway/report_docs/ | [gateway/](gateway/) | 7 |
| gateway/bridge/report_docs/ | [gateway/bridge/](gateway/bridge/) | 3 |
| server/report_docs/ | [server/](server/) | 5 |

设计按发展阶段进入 [设计索引](../design/README.md)，报告按负责模块进入本目录。报告中的时间、分支、测试数量及“已完成/待验收”是原记录的快照；今天的状态看 [problems](../dev/problems.md)，决策与验证沿革看 [devlog](../dev/devlog.md)。

## Agent / video_parse_v2

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

背景：2026-10-08–09 的模型用量交接与验收、2026-10-10 的用户/租户交付。不同报告的版本、环境和测试集合分别阅读。

- [model-token-usage-stages-evidence.md](server/model-token-usage-stages-evidence.md)
- [model-token-usage-stages.excalidraw](server/model-token-usage-stages.excalidraw)
- [model-token-usage-stages.svg](server/model-token-usage-stages.svg)
- [model-usage-handoff-20261008.md](server/model-usage-handoff-20261008.md)
- [user-tenant-handoff-20261010.md](server/user-tenant-handoff-20261010.md)

## 维护约定

1. 新报告放到与实际负责模块对应的路径，如 docs/report/gateway/bridge/；不要在代码模块下重新建立 report_docs。
2. 文首说明报告时点、范围、代码或环境基线，以及尚未验证的事项；整理日期与实验/提出日期分开。
3. 可编辑场景和导出图与报告共同维护；跳转链接指向现有设计、代码或证据。图中的旧目录文字和历史修改树保留原记录含义。
4. 不在报告中再维护一套实时任务状态；与设计、problems、devlog 建立链接。
5. 历史迁移清单、来源校验值及基准快照仍保留原路径，本索引提供原 report_docs 到新目录的对照。
