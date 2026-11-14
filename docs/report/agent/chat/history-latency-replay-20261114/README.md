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

## 9. 后续只读排查：历史 ID、首字等待与 Query 的旧压缩路径

本节复查同一批回放，没有新增 Chat 生成、修改产品代码或部署。逐调用时间、错误计数、配置和压缩元数据见 [diagnostics.json](diagnostics.json)；原始日志仍只在私有目录。三个 Flash reader 分别整理错误、流式和 Query 停止路径，主 Agent 打开关键源码，并以私有日志、SSE 和用量记录复核结论。

`UNKNOWN_ITEM` 表示传入的 `video_chunk_id` 不在当前 kick 的成功检索结果列表里。[检索命中查找](../../../../../agent/chat/video_search/output.py#L387)只遍历内存列表；[抽帧工具](../../../../../agent/chat/video_search/tools/frames.py#L45)在读帧之前就执行这个检查。它不是数据库中视频不存在，也不是帧源故障；异常会转成可重试的工具反馈。Supervisor 每个 turn 都重新创建 `retrieval_calls`，历史消息保留的 ID 不会自动成为本轮可用证据。

T02 首次读取和抽帧分别用 8ms、9ms 返回 `UNKNOWN_ITEM`，T03 首次抽帧用 7ms 返回该错误。两个目标 ID 都已经存在于原请求前的 native state，重新检索后，同一个 ID 均成功抽帧。这与正常下一轮的空注册表契约一致，不能归咎于复制丢了素材；T02 的 SQL 历史本身没有该 ID，但 native state 有，不能只检查展示历史。这里暴露的是历史上下文与当前工具可用证据之间的衔接问题。后续若支持按历史 ID 自动恢复，也须按当前授权范围和版本回源核对，不能把历史文字直接当作当前可信证据。

首字等待可定位到以下真实时点，均从公网客户端发送计时。工具日志只有秒精度，因此用模型调用起点标出边界，不伪造逐毫秒的工具完成时间。

| 事件 | T02 秒 | T03 秒 |
| --- | ---: | ---: |
| 收到 `answer.started` | 0.819 | 0.824 |
| JEV provider 请求完成 | 2.133 | 2.395 |
| 第一段思考事件到达 | 3.310 | 3.819 |
| 第一次 Supervisor 调用结束，随后旧 ID 调用失败 | 5.021 | 4.709 |
| 恢复检索后，第 3 次 Supervisor 调用开始 | 8.199 | 8.023 |
| 成功抽帧后，第 4 次 Supervisor 调用开始 | 11.095 | 11.076 |
| 第一段正文到达，发生在第 4 次调用内 | 14.817 | 17.577 |

因此不是 SSE 把全部事件压到最终完成才发送：约 3–4 秒已有思考事件。正文前先走了旧 ID 失败、恢复检索、再次决定抽帧，再进入看图后的模型调用。第 4 次调用从开始到首正文分别还有 3.722／6.501 秒，该次总 reasoning 为 402／748 token。high 会影响这段生成，但本轮没有同输入的 low/high 对照，不能把全部 FTT 归因于它。数据库持久化和传输开销也未做独立消融。

[Supervisor prompt](../../../../../agent/chat/supervisor.py#L1259)明确禁止“我先看一下”等过程说明，要求直接调用工具；同一 prompt 又允许 mixed 请求先给实际回应。[正文回调](../../../../../agent/chat/supervisor.py#L446)与 [DeepAgents 模型后回调](../../../../../gateway/agent_model/deepagents.py#L351)已经支持正文先于工具执行，正文会保留到后续快照。因此可以在同一个 Agent 循环里先确认当前任务／时间点、继续调用工具、再补证据结论；无需额外增加一个串行 ack 模型调用。这里只确认现有能力，尚未改 prompt 或实测新 FTT。固定“收到”可缩短反馈等待，但不能代替有用答案 FTT，也不能缩短真实取证时间。

T01 的进一步证据是两个并行 Query 共同展开，而非单个 Query 的 45 轮：日志有两条子任务启动，45 次结构化生成合计产生 30025 reasoning token。43 次 Event 搜索中 41 次有非空结果、2 次参数错误，查询文本哈希为 37 种；哈希日志没有所有 filter，不能把重复哈希直接当作完全相同的请求。49 次证据读取中 41 次成功、5 次 `INVALID_EVIDENCE_CURSOR`、3 次被预算拒绝。同一个 Chunk 被尝试读取 22 次，其中 9 次不带 cursor，均成功返回 15 页证据的第一页；这些计数合并两个子任务，不臆造各子任务的精确分配。T01 没有观察到 `UNKNOWN_ITEM`。

还有此前漏查的压缩接线：Supervisor 已按实际 1048576 容量、891289 阈值运行；Query 的 agent 绑定却仍是 262144／200000／150000、`auto_compact_ratio=null`，与 runtime 合并后实际是 **64000／48000／24000**，另有 200000 字符上限。[DeepAgents 构建](../../../../../gateway/agent_model/factory.py#L354)会调用动态容量解析，[structured 构建](../../../../../gateway/agent_model/factory.py#L413)没有调用它。当前配置哈希与回放时一致。

T01 时间窗发现 9 次 `agent_context_window_compacted`，window ID 构成两条独立链；每次事件都与一条已记录的 T01 Query 模型调用起点落在同一秒。它们是本地上下文替换，不是额外的 Supervisor 摘要模型调用，因此“Supervisor summary 为 0”不能写成“所有 Agent 都没压缩”。[Query 的恢复 checkpoint](../../../../../agent/chat/video_search/context.py#L328)保存已检索 ID 和抽帧摘要，没有保存每个 Chunk 的分页位置，恢复说明要求重读已知 ID。这是压缩后丢失阅读进度、放大重读的机制风险，但没有完整模型响应和实际 cursor 字符串，仍不能认定每次游标错误或重读都由压缩造成。

运行时没有按已读页去重或按无新增进展自动终止；模型正常提交 `QueryCompletion` 才会完成报告，硬上限是最后护栏。[失败处理](../../../../../agent/chat/supervisor.py#L736)只合并成功 Query 报告，失败 child 已取得的证据不会作为部分报告返回。两个 Query 最后都预算失败，Supervisor 再尝试读取时仍使用同一个已耗尽的计数器，整轮退出。已确认的是持续探索、重读、旧压缩路径和失败恢复共同存在；尚未证明结构化结果反复校验失败，也没有测量单独修正压缩后的因果收益。

后续优先核对 Query 的动态容量、字符上限和分页进度恢复，再设计有界停止与部分报告；首字体验可独立调整为有内容的前置回应。以上均为后续候选，本轮没有调预算、降 high、改 prompt 或再次回放模型。
