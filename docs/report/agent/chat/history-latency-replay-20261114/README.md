# 2026-11-14 实时 JEV 接入与三条历史 Chat 完整回放

日期：2026-11-14（本地 UTC+8；指标时间戳保留 UTC）。分支 `perf/chat-history-latency-replay`，[记录 PR #166](https://github.com/owariband/Seekino/pull/166)。设计与上线边界见[实时意图路由 TRD](../../../../design/03-product-reassessment/chat-realtime-intent-routing-trd.md)，默认 JEV 的发布背景见[JEV-only 上线 TRD](../../../../design/03-product-reassessment/jev-only-hook-rollout-trd.md)。

本报告记录两件事：实时播放请求不再绕过 JEV 的接线修复，以及原 T01–T03 三条历史请求的完整 HTTP/SSE 回放。修复和部署已完成；回放只有每条一次，且第一条失败、后两条虽完成却有时间对齐问题，不能据此宣布整体 Chat 延迟或回答质量已经改善。

## 1. 范围与结论

修复范围只有实时路由：带 `extra_context.playback` 的请求进入既有 JEV 分类，分类器收到 Server 已构造的 `current_video` 身份与播放位置（`positionSeconds` 按整数毫秒解释），不再因为打开了播放器而直接返回原文。原 query 和 playback 不变，Supervisor 仍拿原文和原始上下文执行工具；默认独立改写保持关闭，普通文字请求的 provider state 形状不变，分类器也没有被描述成已经看过画面。

三条真实请求的新 JEV 都返回 `retrieve` 并完成，每条一次，独立改写调用为 0。这是路由接线的验收结果，不是整条 Chat 的验收结果。T01 没有可见回答，终态 `AGENT_MODEL_ERROR`；T02、T03 完成，但 T02 的视频卡和取帧没有覆盖原播放位置，T03 的卡片覆盖了 17:36，实际取帧却不是 17:36。三条只各跑一次，不能给出总体加速、p95/p99 或一般准确率。

## 2. 回放协议与来源

回放的是最初的 T01、T02、T03 三条真实慢请求，不是 2026-11-13 的 12.012 秒、27 turn 基准。每条都新建游客身份和新会话，复制原请求前的 6／10／12 条 SQL 历史消息，以及实际 pre-turn 原生状态：26／49／60 条消息，0／1／1 个文件，并保留已有 summary。发送前核对 messages、files、summary 哈希一致；最终审计确认原会话消息与 checkpoint 未被修改。

SQL 历史 ID 是新生成的，原 prompt 也没有完整留存，因此这不是字节级重放。原始 query、历史正文、图片、checkpoint 和完整输出只保存在被 Git 忽略的私有验收目录，本目录只保留场景标签、哈希与脱敏指标。

旧请求与本次保持相同的 query、历史和前缀，但当前 prompt、动态 compact、Event 内存／排序修复以及自然缓存已经不同。旧基线只作为描述性前后对照，不能把差值全部归因于 JEV；本轮没有回退旧生产配置，也没有做 JEV 因果消融。原有的 27 turn 实验指标未改动。

## 3. 整轮时间与终态

服务端整轮时间取 assistant `timing.totalDurationMs`，公网 FTT（首个非空可见正文）和客户端 TT（自然终止 SSE）分开记录。旧请求没有同口径 FTT／TT，留为未知。第一条的新耗时是失败持续时间，不是成功首答时间；不能把三条放在一起平均，也不能只挑成功条目算加速。

| 用例 | 旧终态／服务端秒 | 新终态／服务端秒 | 服务端变化 | 新公网 FTT 秒 | 新客户端 TT 秒 |
| --- | --- | --- | ---: | ---: | ---: |
| T01 桥上场景所在集数定位 | COMPLETED / 75.659 | FAILED / 196.567 | +159.807% | 无 | 197.331248 |
| T02 核对当前播放位置的牵手动作 | COMPLETED / 47.880 | COMPLETED / 17.176 | −64.127% | 14.817273 | 19.011393 |
| T03 核验指定 17:36 时刻 | COMPLETED / 88.877 | COMPLETED / 19.562 | −77.990% | 17.577218 | 21.282713 |

旧三条全部 COMPLETED，耗时 75.659／47.880／88.877 秒。新三条中 2 条 COMPLETED、1 条 FAILED；完成数是执行状态，不是正确率。T02、T03 的服务端时间下降来自本次前后对照。虽然保留了原输入与请求前状态，当前 prompt、compact、缓存与修复组合已有差异，不能写成“JEV 让 Chat 快了 64%／78%”。

## 4. 各阶段调用与 token

| 用例 | 时段 | 调用数（JEV／Supervisor／summary／Query／embedding） | input | output | total | cached input（缺失调用） | reasoning（缺失调用） |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| T01 | 旧 | 12（0／8／2／0／2） | 347377 | 12719 | 360096 | 233216（2） | 5497（2） |
| T01 | 新 | 101（1／3／0／45／52） | 1554624 | 46212 | 1600836 | 1133056（53） | 32086（53） |
| T02 | 旧 | 7（0／5／1／0／1） | 188479 | 7452 | 195931 | 119040（1） | 4086（1） |
| T02 | 新 | 7（1／5／0／0／1） | 204068 | 1510 | 205578 | 167168（2） | 743（2） |
| T03 | 旧 | 10（0／7／2／0／1） | 236789 | 14848 | 251637 | 153600（1） | 8865（1） |
| T03 | 新 | 7（1／5／0／0／1） | 183464 | 1798 | 185262 | 151296（2） | 1049（2） |

新三条共 115 次 provider 调用、1991676 total token；旧三条为 29 次、807664。T01 的 101 次里 52 次是 embedding，45 次是 Query 子 Agent，不能把 101 次说成 101 次模型推理回合。T02 和 T03 没有 Query 子 Agent，各为 JEV 1 次、Supervisor 5 次、embedding 1 次。reasoning 已包含在 output 中，不重复相加；cached input 与 reasoning 的部分调用没有回报，按未知保留，不补 0。

旧三条的 Supervisor summary 调用分别为 2／1／2 次，耗时 45.191／30.194／51.932 秒；新三条均为 0。三条 JEV 路由耗时分别为 1.5391／1.5107／1.8469 秒，均为 `completed` 的 `retrieve`，没有独立改写请求。Supervisor 固定为 deepseek-flash／high／8192，动态容量仍为 1048576 context、891289 触发、24000 最近消息尾部目标；24000 是尾部目标，不是整条压缩后请求的长度。

逐行 144 条脱敏调用记录见 [usage.jsonl](usage.jsonl)，各用例、角色的 input／output／total／cache／reasoning 与缺失调用见 [metrics.json](metrics.json)。

## 5. T01 为什么失败

“工具预算”指每条用户请求允许的受控工具调用次数上限，由 Supervisor 和其 Query 子 Agent 共用。检索、读取证据等受控操作执行前会消耗次数；每条请求重新计数，超过上限就抛异常。三条原请求本身都带 `max_tool_calls_per_turn=100`，本次回放沿用原值，另有独立的 `max_model_calls_per_turn=100`。一个模型回复可以发起多个工具调用，embedding 也会单独记 provider 用量，因此工具次数、模型调用数和 provider 请求数不能混为同一个计数。

T01 的 `query_video` 工具占用 170.069 秒，约占 196.567 秒整轮时间的 86.5%。Query 反复检索和读取证据，最终耗尽共享的 100 次工具预算，返回 `VIDEO_LOOP_LIMIT_EXCEEDED`；Supervisor 随后直接读取证据，也因同一预算失败，整轮以 `AGENT_MODEL_ERROR` 结束。所有 provider 调用都完成了，这是应用层工具预算与循环停止问题，不是 provider 超时。

调用记录里 Query 有 43 次 `search_video_events`、10 次 `retrieve`、49 次 `read_retrieval_evidence` 的完成／尝试记录，另有 Supervisor 的 2 次检索和 2 次读取。嵌套或并行工具日志不是可直接相加的实际耗时，也不能据此编造一个确定的重复 query 次数。Cloud bootstrap 会强制 `limits_enabled=true`，即使 YAML 写 false 也不能关闭实际生效的共享预算；`max_loop_turns=3` 不是这次 Query 循环的停止条件。

本轮没有把 100 次预算调大，没有降低 high，也没有改动当前工具逻辑。需要另立任务调查 Query 的有界循环、去重和停止条件，并考虑为 Supervisor 保留恢复预算或提供部分失败反馈。

## 6. T02／T03 的时间对齐与交付

T02 的目标播放位置为 1058310ms（17:38.310），最终卡片为 1030500–1041166ms（17:10.5–17:21.166），没有覆盖目标。实际成功的取帧请求为 1034737／1036212／1037687／1039162ms，看的是更早的场景；第一次 `inspect_video_frames` 以 `UNKNOWN_ITEM` 失败，之后重新取到证据并完成。完成和拿到画面不等于核对了当前播放位置。

T03 的卡片为 1054800–1063500ms，覆盖 1056000ms（17:36），但实际取帧请求是 1055000／1057000／1059000／1061000ms，对应 17:35／17:37／17:39／17:41，没有恰好 17:36 的帧。回答声称提取了 17:36，这一说法没有得到请求证据支持。不能因此把整条回答判为幻觉，也没有独立盲评去确认画面真实内容；能确认的是确定性的时间对齐与表述问题。T03 同样先遇到一次 `UNKNOWN_ITEM`，随后重新检索并完成，失败尝试保留。

两条都没有做独立视觉盲评，也不计算整体准确率。2/3 完成只说明执行终态，不说明视频内容正确。

## 7. 部署、检查与发布

dev 修复为 [PR #162](https://github.com/owariband/Seekino/pull/162)，合并提交 `16d32ff4`；正式发布为 [PR #163](https://github.com/owariband/Seekino/pull/163)，合并提交 `7b990dd87303f576e91f87078a0f9414f0005bad`。main 本次只改 3 个文件：两个运行时文件和一条直接回归测试。本地发布树 808 个路径／8 个资产与 GitHub `release-files` 检查通过。主 Agent 在 dev 和 main 各跑同一组 250 项测试，均通过；两组范围重叠，不能相加成 500 项唯一测试。没有顺带修改无关业务。

API 部署从 2026-11-14T03:44:08Z 开始，03:44:25Z 完成，即本地 11:44，操作耗时 16.943 秒。这是停止／重建到 ready 的操作时间，不是测得的用户停机。新镜像 `seekino-api:jev-realtime-7b990dd8` 从既有 `seekino-api:hotfix-event-memory-2bebd9af` 派生，只覆盖 2 个运行时文件，保留原有内存、排序、embedding 补丁和依赖；另外 5 个容器在部署期间未变。没有模型配置变更或数据迁移，最终健康 200、Parse Consumer 为 running，模型配置 SHA 仍为 `547729bafeb922a0e1f0c25916996513f40b1f507a0e4373ce51943ce48067d2`。环境与发布快照见 [rollout.json](rollout.json)、[source-and-runtime-audit.json](source-and-runtime-audit.json)。

## 8. 未验证与后续

这是一条原请求跑一次的前后记录，不是分布，也不是 JEV 因果实验；没有旧生产回退对照，没有 p95/p99，没有一般模型准确率。旧请求缺少同口径首字数据，当前 prompt、compact、内存／排序修复和缓存也与旧时点不同。T01 的工具预算失败和 T02／T03 的播放时间锚定问题只是被诊断，未在本次修复；原 27 turn 指标仍只适用于那组实验。

实时 JEV 接线已按本次范围完成，原 Chat 的延迟与质量仍未闭环。下一步候选是给 Query 增加有界、去重的循环与明确停止条件，并为 Supervisor 预留恢复预算或返回部分失败；播放位置锚定和精确取帧仍需单独验证。这些尚未实现。本轮对 Query 工具链与取帧业务只做诊断记录，未修改其实现；不以调大预算或降低 high 代替这些问题的修复。当前任务状态见[工作台](../../../../dev/workbench.html)的 `CHAT-JEV-REALTIME-01`、`CHAT-QUERY-BUDGET-01` 与 `CHAT-COMPACT-01`。
