# 第 4 集：中文改写与记忆偏差测试

日期：2026-10-18。使用本机 Seekino 当前运行的 Chat、默认模型和预算。沿用已发布 Event 的 `mygo-ep04-speaker-review-20261016` Series（24 Chunk、430 Event），不修改生产代码、Prompt、配置或索引。

## 结果与边界

**两条中文偏义问题都完成了真实 Chat，找到了有原始对白支持的片段，并说明了记忆措辞与原句的差别。** 两轮 Query 都自主使用 `search_video_events`，没有调用 Chunk `retrieve` 回退。输入没有日文原句、说话人、时间或 Event ID；测试预期只留在本地 case 文件中。Series 名称和元数据包含 EP04，所以这仍然是单集片段定位测试，不是跨全集判断出处。

两轮完整耗时约 154–156 秒，检索工具单次约 0.7–0.9 秒。**本轮不能证明端到端加速。** 两轮 Chat 并行执行；没有运行同问题的 Chunk-only Chat，也没有控制缓存、网络及模型负载。与此前日文精确问题 226 秒的差异不能归因于 Event，问题、审核轮数、抽帧和模型行为均不同。

另有三条固定中文查询的 Event/Chunk 直接对照，用于区分召回、排序和 Agent 行为，不把它们冒充完整 Chat 的实际查询。

## 实际发送的问题与最终回答

### B：语气与措辞偏差

> 我记得有人嫌另一个人多管闲事，差不多说“这又不关你的事，你少掺和”，帮我找那一段，确认是谁说的。

最终选择 **第 4 集 09:08–09:09，椎名立希对千早爱音说「部外者は黙ってて。」**。回答解释为“局外人闭嘴”，并说明不是逐字说“别插手”，而是语境上接近“少插嘴／少掺和”。视频卡为 **09:00.000–09:14.200**。

- Conversation：`ae105562-4b64-4067-b482-c4871beebfe0`。
- Assistant：`cff08757-4a11-4f0c-bd18-1fc02595bf9f`。
- 首次可见文字 **7.3754 秒**，工具条 **9.2568 秒**，服务端总耗时 **156.105 秒**。
- 1 轮 Query/Evaluator；42 次工具调用，Query 12、Evaluator 9、Chat 12、Supervisor 9。
- Query 1 次、Evaluator 3 次抽帧均实际供图；后 3 次命中缓存，合计只有 4 张独立 JPG，并非 16 张新图。
- 6 次 Canvas `INVALID_RANGE` 均恢复；这些错误原样保留，没有从统计或日志删除。

[完整最终响应](ep04-zh-drift-chat/terminal-event.json)、[分析](ep04-zh-drift-chat/analysis.json)、[检索 trace](ep04-zh-drift-chat/retrieval-trace.json)、[审核意见](ep04-zh-drift-chat/review-events.json)、[抽帧原图索引](ep04-zh-drift-chat/frame-evidence/index.json)。

这个问题存在其他合理近义候选：07:02.5 的「関係ありませんわ。」和 07:04.5 的「もう、私に関わらないで。」。前句所回应的是“转学是否因为 CRYCHIC”，不能仅凭中文 event_description 就断言其意思是“与你无关”。本次最终片段有证据支持，但回答开头“你记得的是这一处”仍比证据允许的唯一性更强；更准确的表达应是“找到一处很接近的场景”。因此评价为**有效近义定位，措辞确定性仍可改善**，不评价为唯一出处已证明。

### C：加入了原句没有的指责

> 我记得有人说“不懂音乐就别来指点”，帮我找这段，是谁说的？原话可能记偏了。

最终选择 **20:03.2–20:08.7 千早爱音对椎名立希**的这句：

> ギターはコードに始まり コードに終わるんです。ドラムのあんたには分からないだろうけど。

回答译意为“吉他始于和弦、终于和弦——不过你这个打鼓的应该不会懂”，**明确说明原话没有“不懂音乐”，也没有直接说“别指点”**。没有把这些新增前提塞回原始证据。视频卡为 **20:01.7–20:11.2**，包含立希先说「それコードだし。」、爱音回敬、立希叹气和爱音追问。

- Conversation：`a33927a7-362a-45ee-824a-bf213fe64398`。
- Assistant：`1b65aa2b-7582-484a-af0d-3c9a759de38e`。
- 首次可见文字 **8.079 秒**，工具条 **9.8641 秒**，服务端总耗时 **154.078 秒**。
- 2 轮 Query/Evaluator；39 次工具调用，Query 14、Evaluator 12、Chat 7、Supervisor 6。
- **没有调用抽帧工具**。本例是 canonical transcript/events 的文字核验，不宣称重新核验了画面或声音。
- 2 次 Canvas `INVALID_READ` 恢复。第一次审核要求修正播放起点，第二次通过。

[完整最终响应](ep04-zh-premise-chat/terminal-event.json)、[分析](ep04-zh-premise-chat/analysis.json)、[检索 trace](ep04-zh-premise-chat/retrieval-trace.json)、[审核意见](ep04-zh-premise-chat/review-events.json)、[无抽帧记录](ep04-zh-premise-chat/frame-evidence/index.json)。

这里同样只能确认是一个有支持的近义场景，无法证明就是用户心中那一段。最终回答虽然纠正了具体措辞，但“你记得的是”仍可以降低确定性。

## 自然 Chat 内部发生了什么

### B 的链路

1. Supervisor 先输出可见说明，再调用 `search_video_graph`，委派核对原话、说话人、对象、前后文和片段范围。
2. Query 把中文意图展开为日语近义表达「あなたには関係ない」「余計なお世話」「口を出さないで」「放っておいて」。只筛 DIALOGUE，不筛 speaker。实际权重 **词法 0.25 / 语义 0.70 / 视觉 0.05**，top_k=8。
3. Event 工具 **731 ms** 成功，三个通道共 60 条候选，融合为 47 个 Event，回源后保留 8 个父 Chunk 内的 37 个 Event。首屏实际展示 15 张 Event 目录卡，不能把完整 trace 的 37 条都说成 Agent 已看过。
4. 最终目标 Event `37b02b…` 的融合排名 **13**，语义排名 **13**、视觉排名 **3**，没有词法命中；父 Chunk 排名 **7**。它位于首屏目录中。第 1 名实际是祥子“别再和我扯上关系”，没有机械取第 1 名作为答案。
5. 14:55:54 读取目标 Chunk overview，14:55:57 读取 transcript/events/text/metadata，14:56:02 抽帧，14:56:29 写草稿。日志证明它精读了最终目标 Chunk；没有目标以外 Chunk 的精读记录。
6. Evaluator 复核同一 Chunk 和帧证据，14:57:17 返回 complete。随后 Chat/Supervisor 完成回答和视频卡提交。审核原文明确区分静帧旁证与对白 speaker 标签。

### C 的链路

| 实际检索 | 权重：词法/语义/视觉 | 核心 Event 融合排名 | 工具耗时 | 用途与结果 |
|---|---|---:|---:|---|
| 第一次：日语表达“不懂音乐就别插嘴/评价” | 0.45 / 0.50 / 0.05 | 22 | 671 ms | 语义通道 rank=12 已召回；保留目录有 40 个 Event，目标在此集合的位置是第 20，但首屏只有 14 张卡，目标未在首屏展示 |
| 第二次：多种“不懂就别插嘴”表达，并猜测立希打断爱音 | 0.65 / 0.35 / 0 | 11 | 919 ms | 目标通过词法 rank=9 命中日文 dialogue，进入首屏 13 张卡，随后精读 |
| 第三次：围绕“这很难/那是和弦/吉他”的已知对白 | 0.75 / 0.20 / 0.05 | 4 | 668 ms | 第一次审核之后，寻找相邻前块及跨边界台词，属于播放范围修订 |

第二次检索完整 query 是：

> 「音楽分からないくせに」「何も分からないくせに」「分からないなら口出すな」「知らないくせに」「勝手なこと言わないで」立希が愛音の意見や評価を遮る

**这里仍有过早人物假设**：用户没提供谁指责谁，query 却写入“立希遮断爱音的意见”，而最终证据对应的是爱音回敬立希。它没有把这个假设变成 speaker filter，最终精读也纠正了方向，因此本轮没有形成最终事实错误；但不能因此说“Query 从未预设背景”。从日志无法还原生成该假设的完整模型上下文，不推断它必然来自既有背景知识或某条候选。

14:55:57 Query 才开始读取 `0044de20…` 的 overview，14:56:00 精读 transcript/events，之后写草稿。第一次 Evaluator 已认可核心说话人和原句，要求处理从 20:00 起播截断跨 Chunk 台词的问题。Query 第三次检索找到前一 Chunk `9cfc7164…` 并精读，但最终采用另一条建议：从立希完整台词开始，删除对前一句的依赖。第二次 Evaluator 通过，最终播放起点改为 20:01.7。

这条链路说明本次效果来自**模糊展开、多候选目录、canonical 精读和纠偏审核共同作用**。初始召回、首屏展示、精读与最终选择是不同阶段，不能把最终答对直接等同于第一次检索 Top 1 正确。

## 固定中文查询的 Event / Chunk 对照

直接调用同生产 factory/config，固定同 scope、同 top_k=10、同权重 **0.65 / 0.35 / 0**；Event 使用 DIALOGUE 筛选。每句各执行一次，顺序 Event→Chunk，没有人为共享跨调用 query vector。这些参数**不是**上面自然 Agent 的实际参数。

| 中文输入 | 语义上要核对的具体 Event | Event 排名 / 父 Chunk 排名 | 直接 Chunk 对应父块排名 | Event / Chunk 耗时 |
|---|---|---|---:|---|
| A 不相干的人别插话 | 09:08 立希“局外人闭嘴” | 8 / 6 | 10 | 1.408 / 0.588 秒 |
| B 这又不关你的事，你少掺和 | 07:02.5 / 07:04.5 的拒绝来往候选 | 5、8 / 4 | 4 | 0.643 / 0.537 秒 |
| B 同上 | 原先 09:08 目标 | 12 / 8 | 前十没有 | 同上 |
| C 不懂音乐就别来指点 | 20:03 爱音挤兑鼓手不懂和弦 | 2 / 2 | 5 | 0.585 / 0.552 秒 |

Event 的 top_k 约束返回的父 Chunk 数，不等于只允许前十个 Event，所以 Event rank=12 与成功返回并不矛盾。Chunk 只能证明父块被召回，不能证明某句 Event 已被模型看到。C 中原 09:08 父块虽被 Chunk 排在第 9，但它不支持“音乐能力”的前提，不将其算成正确目标。

本组三句里实际词法命中的字段都是中文 `event_description`，并非日文 `dialogue`。`lexical_fields` 是可搜索材料，`matched_fields` 才是本次实际词法命中。A 的正确 Event 只靠语义 rank=5 召回，融合后降为 rank=8；B 的原目标同样只有语义通道。固定 0.65/0.35 的 RRF 使一些词法弱相关候选排在语义单通道结果之前。C 的第一名是“只报乐队名我听不懂”的干扰项，音乐挤兑场景在第二名。

因此目前证据支持“Event 能提供更细的候选与定位信息，部分相关父块排名改善”，同时暴露了融合排序、首屏展示和查询假设的问题。**不能据这三次单样本声称稳定的命中率或延迟收益。** 未在本次实验调整权重或生产排序。

[全部直接结果与阶段耗时](ep04-zh-retrieval/summary.json)、[独立分析及具体 Event 身份/字段/分数](experiment/ep04-zh-retrieval-analysis.json)、[发送前的事实与评分边界](experiment/ep04-chinese-grading-notes.json)。

## 原始证据与可复现范围

每个 Chat 目录都保存：`manifest.json`、原始 SSE、带接收时间的 `events.jsonl`、最终 SQL/API 消息视图、完整服务日志窗口、按 conversation 过滤的日志、所有工具开始/结束记录、全部 evaluator advice、完整业务 retrievalTrace、实际抽帧原图和 manifest（若有）、模型 usage 及可取得的 native Supervisor rollout。两轮并发，`server.window.raw.log` 可能同时包含另一轮；`server.scoped.log` 按 conversation 分开。

- [B 原始 SSE](ep04-zh-drift-chat/sse.raw.log)、[B 服务日志](ep04-zh-drift-chat/server.scoped.log)、[B 工具记录](ep04-zh-drift-chat/tool-events.json)、[B 原始窗口](ep04-zh-drift-chat/server.window.raw.log)。
- [C 原始 SSE](ep04-zh-premise-chat/sse.raw.log)、[C 服务日志](ep04-zh-premise-chat/server.scoped.log)、[C 工具记录](ep04-zh-premise-chat/tool-events.json)、[C 原始窗口](ep04-zh-premise-chat/server.window.raw.log)。
- [B 模型 usage](ep04-zh-drift-chat/model-usage.json)、[B native 会话索引](ep04-zh-drift-chat/native-session-manifest.json)；[C 模型 usage](ep04-zh-premise-chat/model-usage.json)、[C native 会话索引](ep04-zh-premise-chat/native-session-manifest.json)。
- [B 输入](ep04-zh-drift-case.json)、[C 输入](ep04-zh-premise-case.json)、[固定检索输入](ep04-zh-retrieval-case.json)。

B 的 usage ledger 有 5 条记录（4 个 Codex SDK 聚合运行 + 1 次 embedding），C 有 9 条（6 个 SDK 聚合运行 + 3 次 embedding），不把 SDK 聚合运行数量说成其内部模型请求总次数。Supervisor 原生日志分别为 407,052 / 391,317 字节，均核验原问题、会话绑定、终态、源文件与复制件 SHA。

保留系统实际产生且能够取得的记录；Query/Evaluator/Chat 的 ephemeral 完整模型转录未持久化，工具摘要不能还原完整模型上下文，更不等同于内部思维。抽帧工具未记录实际视频 PTS，备份索引明确保留 `actual_frame_pts_ms=null`，不把请求时间冒充实际帧 PTS。未补造缺失日志，未为了报告重新抽帧或重跑问题。

本轮仅扩展实验脚本的 case 查询循环，新增日志提取脚本和实验记录；未修改业务实现。语法、JSON/JSONL、报告链接和备份 SHA 分别验证。工作区其他已有修改不归为本轮改动；未执行重启、回填、迁移、commit 或 push。
