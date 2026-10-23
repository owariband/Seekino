# Event 台词定位与耗时实测

最新补测：**中文改写与记忆偏差的两条真实 Chat 已完成**，并保存三条固定中文查询的 Event/Chunk 对照，见 [中文偏义测试报告](EP04-ZH.md)。包含原始检索、排序、首屏展示、精读、审核和最终回答的区别。

用户随后澄清希望测试已完成索引的第 4 集。**EP04 原台词已真实命中 Event，完整结果见 [第 4 集原台词报告](EP04.md)**。下文保留此前正式 MyGO 13 集未覆盖时的测试，两个 scope 的结论分别适用。

日期：2026-10-18。测试对象：当前本地运行的 Seekino，正式 MyGO Series。只运行一次自然问题 Chat，以及只读检索对比；没有修改生产代码、回填索引、修改配置或重启服务。

## 结论

回答成功，**本轮不能证明 Event 提高命中或加速**。Query 已主动选择 Event 工具，但正式 MyGO 13 集尚无 Event 发布覆盖，工具返回 `EVENT_INDEX_NOT_READY`；最终通过原 Chunk 检索找到真实出处。

当前 Event 索引已有 430 条文档，但全部来自独立的 EP04 speaker-review 素材，不属于正式 MyGO Series。正式 13 集的 312 个 Chunk 已发布，Event receipt 数为 0。详见 [完整 SQL/ES 覆盖快照](environment-coverage.json)。未覆盖发生在召回之前，不是 Event 检索得到零匹配。

## 自然问题 Chat

- 原始输入：`真是高高在上的，出于哪一集？`
- 固定 Series：`836fa9f0-2bf4-50fb-9c64-75526fa1dea9`；未额外传入 episode、speaker、当前播放位置、参考材料或使用 Event 的提示。
- 使用运行服务的默认模型、提示和预算；没有强制工具选择。
- Conversation：`b41eb77f-3a7a-46e7-9bf9-ddfdaeb8aa6e`。
- Assistant message：`3adc2411-714e-4110-a522-bc9b6f6cdf39`。
- 最终状态：COMPLETED；服务端总耗时 **103.399 秒**，客户端收到终态 **103.5569 秒**，首次收到可见文字快照 **12.0676 秒**。前端快照传送正文，本轮没有 `answer.delta`；不能以首个 delta 计算首次文字时间。

最终答案确认：**第 13 集，00:54.600–00:56.000，豊川祥子对若葉睦说「いいご身分ですわね。」；正式解析中的简中译文为「真是居高临下啊」。** 视频卡播放范围 00:46.400–01:00.000。

目标来源是 `video_chunk_id=ad1c7419-c11c-5280-9e2e-89456355bfe7`，正文 version=1，原始 timeline_index=18，canonical Chunk 为 00:00–01:00。预期 Event ID `fa396affd78007f80e57e85c7cac1a92011bdb7bcea4ec1273a5a6e005c9d53b`，当前 ES 中不存在。

## 实际调用过程

1. Supervisor 先调用 `understand_current_video`。请求没有播放上下文，11 ms 后返回 `INVALID_PLAYBACK_CONTEXT`；随后改为委派全 Series 检索。这次不必要的工具尝试保留在日志中。
2. Query 首次检索调用 `search_video_events`，类型筛选为 DIALOGUE，权重 lexical=0.65、semantic=0.35、visual=0。query 为：`中文台词 真是高高在上的 还真是居高临下 一副高高在上的样子 傲慢 俯视他人`。352 ms 后返回 `EVENT_INDEX_NOT_READY`，没有 Event 候选或 Event query embedding。
3. Query 正常回退 `retrieve`，query 为：`中文台词“真是高高在上的”或“还真是居高临下”“一副高高在上的样子”，人物讽刺对方态度傲慢、居高临下的对话`。权重 lexical=0.55、semantic=0.45、visual=0，top_k=10。工具耗时 **760 ms**，目标 Chunk 排名 **2**。
4. Query 精读候选，提交目标 Chunk 草稿；Evaluator 核验并通过；Chat 整理回答，Supervisor 再读审核与目标 transcript 后提交视频卡。最终依赖的是 Chunk 证据，而非 Event 命中。

本轮工具调用共 40 次：Supervisor 7、Query 18、Evaluator 5、Chat 10。共 6 次错误均有后续恢复：播放上下文缺失 1、Event 未覆盖 1、Chat 的 Canvas 范围请求错误 4。它们是实际运行观察，本轮没有修复这些相邻行为。

API 阶段计时如下；QUEUED 标签还包含初始 Supervisor 工作，不能直接视为纯排队时间：

| 阶段标签 | 秒 |
|---|---:|
| QUEUED | 15.762 |
| RETRIEVING | 34.956 |
| EVALUATING | 21.433 |
| RESPONDING | 31.248 |

Event 未覆盖检查与 Chunk 检索工具本次合计约 **1.112 秒**。其余时间不能归因于 ES 查询，主要涉及多轮模型调用、证据精读往返和角色交接。Event 提高定位精度后是否减少这些往返，仍需要覆盖完成后的对照。

## 直接检索对比

使用与运行服务一致的 production factory、配置和正式 Series，保留每阶段 wall time、模型调用、原始通道候选与完整结果。没有绕过 Event coverage、临时加入目标文档或提前提供 EP13/speaker。顺序重复仅用于观察本机本次表现，样本不足以作稳定延迟分位数结论。

| 输入与策略 | 次数 | 服务耗时 | 目标 Chunk 排名 |
|---|---:|---|---|
| 原问句，Event，仅限 DIALOGUE | 3 | 0.324 / 0.296 / 0.303 s | 未执行召回，全部 INDEX_NOT_READY |
| 原问句，Chunk 当前默认三路权重 | 3 | 0.543 / 0.385 / 0.431 s | 全部未进入目标 Top 10 |
| 正式译文「真是居高临下啊」，Chunk 默认权重 | 1 | 0.400 s | 6 |
| Query 实际扩写词与 0.55/0.45/0 权重，Chunk | 3 | 0.552 / 0.421 / 0.501 s | 全部为 2 |

“原问句未进入目标 Top 10”指目标 Chunk，不表示候选中没有其他第 13 集片段。扩写与权重同时发生变化，因此本轮不能分离两者各自的贡献。对正式译文的测试是事后诊断，未注入自然 Chat 的输入。

## 原始证据与复现

- [覆盖、索引 mapping/meta、任务/版本聚合、完整目标 Event](environment-coverage.json)
- [统一分析数据](analysis.json)
- [自然 Chat 请求与标识](chat-natural/manifest.json)、[完整 SSE](chat-natural/sse.raw.log)、[带接收时间的事件](chat-natural/events.jsonl)
- [最终 SQL 消息视图](chat-natural/persisted-messages.json)、[完整业务 retrievalTrace](chat-natural/retrieval-trace.json)
- [服务日志时间窗原文](chat-natural/server.window.raw.log)：保留该窗口全部日志，可能含并发请求；[本会话日志](chat-natural/server.scoped.log)、[逐工具记录](chat-natural/tool-events.json)
- [检索环境](retrieval/environment.json)、[原问题结果摘要](retrieval/summary.json)、[Query 实际策略重复摘要](retrieval/actual-query-summary.json)。对应逐次 JSON 保存完整各通道候选、结果、阶段耗时和 usage。
- [真实 Chat driver](run_chat.py)、[检索 driver](run_retrieval.py)。Chat driver 会创建真实会话并调用模型，不应为了读日志再次运行。

可复现检索：

```bash
uv run --no-sync --env-file .env.local -- python engine/design/event-retrieval-eval-20261018/run_retrieval.py
uv run --no-sync --env-file .env.local -- python engine/design/event-retrieval-eval-20261018/run_retrieval.py --actual-query
```

全模型原始输入/输出与业务检索 trace 是不同证据。当前 Chat 默认没有启用完整模型 transcript 捕获；原生 Codex rollout 的可用性、对应关系及缺失部分另见 `chat-natural/native-session-manifest.json`，真实 usage 另存 `chat-natural/model-usage.json`。不把摘要日志冒充缺失的模型原文。

已核验保存 [Supervisor 原生 rollout](chat-natural/native-sessions/supervisor.rollout.jsonl)，389,354 字节、72 条记录，与源文件逐字节一致；thread `01a14d5e-aa3d-7c70-b9b8-0198cf02e711` 与本会话 SQL 绑定对应。Query / Evaluator / Chat 使用 ephemeral session，本轮没有保留下来的完整原生 transcript，manifest 明确标记 `not_preserved`。[model-usage.json](chat-natural/model-usage.json) 含本 assistant run_id 的 5 条记录：4 条 Codex SDK run 级聚合记录和 1 条 embedding；这不等于整个流程只进行了 5 轮内部模型请求。

## 下一次有效对照的前提

需要先为**正式 MyGO Series 的目标搜索范围**补齐 Event 发布，再用同一原问句、不提示集数或说话人重跑。当前 Event coverage 要求所选范围完整发布，仅其他 Series 有 Event 或仅补一个已知目标 Chunk 都不能代表全 Series 搜索已就绪。

本次没有执行这项回填。当前结果证明了工具可见、Query 主动选用、未覆盖反馈和 Chunk 回退正常；尚未验证 Event 的实际召回排名、命中率或端到端加速，不能把旧实验与本次耗时差异解释为 Event 的收益。
