# Kimi K3.1 Pro：Deep Agents、Pi、Codex Chat 对照

本轮完成了当前四类 benchmark：**99 条 Chat 主计划记录**（98 次实际请求、1 次前置依赖阻塞），另有 13 条直接检索和 14 条路由／协议组件检查。两个补充多轮请求单独保存，不替换主成绩。所有被测可配置业务生成角色及后置评分均使用 Kimi `k3.1-pro`。

| 框架 | 请求完成／主计划 | 对话行为任务与证据通过 | 查询覆盖任务与证据通过 |
| --- | ---: | ---: | ---: |
| Deep Agents | 32/33 | **16/20** | **5/13** |
| Pi | 31/33 | 14/20 | 4/13 |
| Codex | 29/33 | 14/20 | **5/13** |

这里的通过只统计 `task_and_evidence=pass`；部分完成、失败和未评均保留，实际交付另评。Codex 的主计划含一次供应商过载和由此前置失败导致的追问阻塞，不能由它们推断模型不懂多轮。Pi 完成回答全部存在复原适配链的增量断段，因此其综合交付通过为零；这不表示其内容理解通过为零。

**继续用 Deep Agents 作为默认 Chat 架构是合理的工程选择。**它在这批行为题和请求完成数上较好，原 C19 反馈两次通过；Codex 同题也两次通过，Pi 两次仍复述旧剧情。但查询内容通过数与 Codex 相同，Deep Agents 还出现了明确的说话人错误，没有“全面更聪明”的证据。去 Pi 所减少的部署／维护复杂度来自此前生产变更，本轮没有重新计算或再次宣称那些删减收益。

下一步应优先修复本轮已有失败证据的公共问题：T05 前置路由与检索任务冲突、Query 超时和查证收敛、暂停边界的正文／卡片检查、同 chunk 更新卡片的语义。若要进一步比较 Pi 的框架能力，应先修正其文本适配合同，再另开冻结实验；本轮原结果继续保留。

## 主要结果

下表按 suite 分开，P / 部分 / F / 未评分别为 pass / partial / fail / unassessed。逐例列展示任务与证据分、实际执行终态和原始请求耗时；行为题两次结果按轮次排列。延迟表仅使用同一用例三套全部完成且无显著时钟差的配对子集，失败仍保留在前面的分母中。

### 对话理解与应变

| 框架 | 完成／计划 | 任务与证据 P / 部分 / F / 未评 | 交付 P / 部分 / F / 未评 | 综合 P / 部分 / F / 未评 |
| --- | ---: | --- | --- | --- |
| Deep Agents | 20/20 | 16 / 4 / 0 / 0 | 19 / 1 / 0 / 0 | 16 / 4 / 0 / 0 |
| Pi | 20/20 | 14 / 6 / 0 / 0 | 0 / 0 / 20 / 0 | 0 / 0 / 20 / 0 |
| Codex | 19/20 | 14 / 6 / 0 / 0 | 18 / 1 / 1 / 0 | 14 / 5 / 1 / 0 |

| 场景 | Deep Agents | Pi | Codex |
| --- | --- | --- | --- |
| `c19_original_history` | pass (COMPLETED, 16.6s)；pass (COMPLETED, 12.8s) | partial (COMPLETED, 16.0s)；partial (COMPLETED, 19.1s) | pass (COMPLETED, 17.8s)；pass (COMPLETED, 20.6s) |
| `retrieve_original_quote` | pass (COMPLETED, 51.8s)；pass (COMPLETED, 51.3s) | pass (COMPLETED, 52.8s)；pass (COMPLETED, 66.8s) | pass (COMPLETED, 54.5s)；pass (COMPLETED, 56.4s) |
| `respond_existing_evidence` | pass (COMPLETED, 11.0s)；pass (COMPLETED, 9.2s) | pass (COMPLETED, 5.7s)；pass (COMPLETED, 8.8s) | pass (COMPLETED, 11.3s)；pass (COMPLETED, 8.2s) |
| `mixed_feedback_and_search` | pass (COMPLETED, 51.8s)；pass (COMPLETED, 273.1s) | pass (COMPLETED, 121.5s)；pass (COMPLETED, 78.4s) | pass (COMPLETED, 62.5s)；pass (COMPLETED, 100.2s) |
| `respond_stop_retrieval` | pass (COMPLETED, 6.0s)；pass (COMPLETED, 11.7s) | pass (COMPLETED, 7.7s)；pass (COMPLETED, 9.0s) | pass (COMPLETED, 7.9s)；pass (COMPLETED, 8.0s) |
| `retrieve_correct_negation` | pass (COMPLETED, 129.8s)；pass (COMPLETED, 321.2s) | pass (COMPLETED, 82.5s)；pass (COMPLETED, 38.4s) | pass (COMPLETED, 194.1s)；pass (COMPLETED, 70.7s) |
| `mixed_reference_and_feedback` | partial (COMPLETED, 220.3s)；partial (COMPLETED, 241.3s) | partial (COMPLETED, 262.0s)；partial (COMPLETED, 77.5s) | partial (COMPLETED, 101.1s)；partial (COMPLETED, 241.2s) |
| `retrieve_feedback_quote` | partial (COMPLETED, 91.8s)；partial (COMPLETED, 226.8s) | partial (COMPLETED, 246.8s)；partial (COMPLETED, 88.3s) | partial (COMPLETED, 325.5s)；partial (COMPLETED, 314.8s) |
| `holdout_feedback` | pass (COMPLETED, 4.9s)；pass (COMPLETED, 8.6s) | pass (COMPLETED, 5.2s)；pass (COMPLETED, 7.4s) | pass (COMPLETED, 9.4s)；pass (COMPLETED, 14.4s) |
| `holdout_mixed` | pass (COMPLETED, 125.8s)；pass (COMPLETED, 238.3s) | pass (COMPLETED, 172.1s)；pass (COMPLETED, 230.7s) | partial (COMPLETED, 116.1s)；partial (HTTP_ERROR, 480.2s) |

| 框架 | 可比较配对数 | 平均耗时 | 中位耗时 |
| --- | ---: | ---: | ---: |
| Deep Agents | 17 | 92.9s | 51.8s |
| Pi | 17 | 74.6s | 52.8s |
| Codex | 17 | 94.9s | 56.4s |

### 查询能力覆盖

| 框架 | 完成／计划 | 任务与证据 P / 部分 / F / 未评 | 交付 P / 部分 / F / 未评 | 综合 P / 部分 / F / 未评 |
| --- | ---: | --- | --- | --- |
| Deep Agents | 12/13 | 5 / 6 / 2 / 0 | 8 / 3 / 2 / 0 | 4 / 6 / 3 / 0 |
| Pi | 11/13 | 4 / 8 / 1 / 0 | 0 / 0 / 13 / 0 | 0 / 0 / 13 / 0 |
| Codex | 10/13 | 5 / 5 / 2 / 1 | 8 / 2 / 3 / 0 | 5 / 4 / 4 / 0 |

| 场景 | Deep Agents | Pi | Codex |
| --- | --- | --- | --- |
| `chat-T01` | partial (COMPLETED, 133.7s) | partial (COMPLETED, 49.0s) | fail (HTTP_ERROR, 8.5s) |
| `chat-T02` | partial (COMPLETED, 147.3s) | partial (COMPLETED, 104.7s) | unassessed (BLOCKED) |
| `chat-T03` | pass (COMPLETED, 444.5s) | partial (HTTP_ERROR, 480.2s) | pass (COMPLETED, 402.3s) |
| `chat-T04` | pass (COMPLETED, 184.3s) | partial (COMPLETED, 219.3s) | partial (COMPLETED, 162.3s) |
| `chat-T05` | partial (COMPLETED, 61.9s) | partial (COMPLETED, 61.4s) | partial (HTTP_ERROR, 480.3s) |
| `chat-T06` | partial (COMPLETED, 44.9s) | partial (COMPLETED, 56.1s) | partial (COMPLETED, 87.9s) |
| `chat-T07` | fail (HTTP_ERROR, 484.0s) | partial (HTTP_ERROR, 480.1s) | partial (COMPLETED, 110.6s) |
| `chat-T08` | partial (COMPLETED, 250.4s) | partial (COMPLETED, 170.1s) | fail (COMPLETED, 234.7s) |
| `chat-T09` | partial (COMPLETED, 170.1s) | fail (COMPLETED, 210.0s) | partial (COMPLETED, 383.0s) |
| `chat-event-original` | pass (COMPLETED, 47.7s) | pass (COMPLETED, 50.0s) | pass (COMPLETED, 63.0s) |
| `chat-event-jp` | pass (COMPLETED, 32.2s) | pass (COMPLETED, 38.4s) | pass (COMPLETED, 46.7s) |
| `chat-event-drift` | pass (COMPLETED, 57.7s) | pass (COMPLETED, 42.8s) | pass (COMPLETED, 70.0s) |
| `chat-event-premise` | fail (COMPLETED, 136.2s) | pass (COMPLETED, 210.2s) | pass (COMPLETED, 168.3s) |

| 框架 | 可比较配对数 | 平均耗时 | 中位耗时 |
| --- | ---: | ---: | ---: |
| Deep Agents | 8 | 115.4s | 96.9s |
| Pi | 8 | 124.6s | 113.1s |
| Codex | 8 | 152.0s | 125.1s |

### 已记录用量

| 集合／框架 | Kimi 已知 total tokens | Kimi 用量未知调用数 | Kimi 记录调用数 |
| --- | ---: | ---: | ---: |
| chat_behavior / Deep Agents | 4,878,624 | 3 | 169 |
| chat_behavior / Pi | 3,433,702 | 1 | 37 |
| chat_behavior / Codex | 3,985,976 | 4 | 52 |
| query_capabilities / Deep Agents | 3,947,645 | 5 | 133 |
| query_capabilities / Pi | 3,772,745 | 4 | 53 |
| query_capabilities / Codex | 4,202,946 | 9 | 68 |

计入 Supervisor 和内部 Query 等生成调用；SDK aggregate 与 provider-request 粒度不同。未知调用未记零，已知数值是观测下界，不换算金额。

## 比较范围与方法

本次评测针对 AniVAG 的 Chat Supervisor 集成。共计划 **99 条主要观测**：10 个对话行为场景 × 3 个 harness × 2 次，以及 13 个查询覆盖任务 × 3 个 harness × 1 次。依赖轮次失败时，追问题按协议记 `BLOCKED`，不能冒充实际向模型发送过。另执行 13 个直接检索配置、14 个意图路由／结构化协议用例，分别报告，不合并分母。

三套的 Supervisor、Query 等可配置业务生成角色以及后置评分均使用 **Kimi `k3.1-pro`**，reasoning effort 为 `low`，没有 GPT 调用或业务生成 fallback。共同的前置 Jev 路由、Qwen 改写与 Doubao embedding 保持不变；独立路由组件测试使用 Kimi，不能把它当作端到端路径已经替换 Jev/Qwen 的证据。Codex 在这里指通过自定义 provider 接 Kimi 的执行框架。

| 条件 | 本轮设置 |
| --- | --- |
| 业务入口 | 实际 FastAPI ASGI HTTP 路由及生产 service，连接已有 SQL、ES、模型服务 |
| 可替换部分 | 仅 Supervisor harness；Query、检索、卡片校验、数据与业务工具共用 |
| 输入 | 完整固定历史及当前 query；参考答案、评分要求不进入被测模型输入 |
| 会话 | 每例新会话；T01→T02 共用会话，两个请求在不同 worker 进程中执行 |
| 调度 | 顶层串行；轮换三套先后顺序，行为第二轮反转用例顺序；内部业务并行保持原配置 |
| 预算 | 配置上下文 128,000、输出 8,192；Supervisor 总预算 480 秒，外部 worker watchdog 570 秒 |
| 自动重试 | 三套 transport 重试为 0，Pi agent auto retry 关闭；模型主动换工具补查单独记录 |
| 实际版本 | Deep Agents 0.7.23、LangGraph 1.2.11、LangChain 1.4.4、openai-codex 0.156.1；Pi SDK 1.3.0 |
| 代码 | 起始 HEAD `c8ee5261ee57308ac7617c748c2d0adc32c74344`；326 个相关 Python 文件、配置及 fixture SHA 固定并逐 worker 核对 |

同一业务 prompt 和工具 schema 不代表 SDK 的全部内部指令一致。Deep Agents、Pi、Codex 的原生指令、内置工具、压缩策略和输入协议仍有差别：Deep Agents/Pi 使用 Chat Completions，Codex 使用 Responses；Codex 的输出配置没有与 API `max_output_tokens=8192` 完全等价的硬上限。Deep Agents 配置在 90k 压缩并保留 60k；Pi 使用其原生 compaction 默认保留预算；Codex 使用 90k 压缩配置及 95% effective context。具体配置见 [native-policy-notes.json](../../../outputs/verification/chat-harness-kimi-recovery-20261026/native-policy-notes.json)。

因此，本轮衡量的是**框架与适配器在当前产品中的组合效果**，不能把全部差异归因于框架算法。每请求新 worker 也没有测 Pi 常驻 Host 的多会话复用收益。计时从消息 POST 开始，应用装配在计时外；模型侧原生进程在请求中启动的部分计入。内部首文本回调不是 HTTP SSE 首字指标。本机为共享工作站，没有清空模型缓存，也没有云端 4 CPU / 8 GB 容量实验。

## Pi 复原与交付合同

Pi 从退役前提交 `798307a91d67f3dfbad651e6f27e6051da166cb1` 提取到独立实验目录，没有重新引入生产代码。保留原 runtime，并为已移除的配置类型恢复实验侧 import。未修改版本在 Kimi 预检收到 `developer` role 不被接受的 HTTP 400，因此实验侧补了 `supportsDeveloperRole=false`，此兼容调整明确记入协议。

复原链路还暴露了流式文本合同不一致：Pi 把每个 text delta 传给上层，而 Chat 消费端按段落拼接，使大量短增量被分成独立段落。HTTP 返回与数据库保存都存在同一问题，不能归咎于报告渲染。评审同时保留实际交付文本，以及原始增量直接拼接的诊断读法；后者只用于理解语义，不算产品已经修好。该问题属于本次复原的 Pi 适配链，不能泛化为 Pi SDK 的固有能力。

31 条完成的 Pi 请求均在实际交付文本中确认此问题。按双换行分段后，每条至少 10 段且至少一半段落不超过 4 字；这个计数只核对已知的 delta→段落合同，不作为通用文风评分器。见 [Pi 交付核对](../../../outputs/verification/chat-harness-kimi-recovery-20261026/pi-delivery-audit.json)。

## 数据与环境中断

两批之间的 SQL 解析状态、索引 UUID 和文档数快照相同：14 个视频、44 个解析任务，状态摘要 SHA 为 `624e46ee0a65fe51eef59c7d219aab393b0cee8a271beab5c70766d6e90941e6`。正式 MyGO 13 集中，EP02 最新任务为 `INDEXING_FAILED`，其余 12 集可用；另有独立 EP04 review 素材。没有重解析或修改检索数据；此快照不等于媒体或整个 ES 内容的逐字节哈希。

首批运行遇到跨系统时间间隔、DNS/连接失败和响应超时。原批次留下 74 条记录，其中前 60 条为完整的行为实验，后 14 条查询覆盖记录为 1 条完成、11 条调用失败、2 条依赖阻塞。保留全部原件，整组重跑 39 条查询覆盖，不选择性覆盖失败，也不把原先成功的查询结果混入恢复批次。恢复前重新验证三套 Kimi 工具／历史冒烟、SQL/ES 状态，并在计时期间临时阻止空闲休眠。

原行为第 48、49、50、58 条还存在 UTC 与 `time.monotonic()` 的显著差异。原回答和质量证据保留；延迟另列全部原始计时与“相同用例、三套全部完成、无 >20 秒时钟差”的配对子集。行为子集为每套 17 条，剔除了两个受时钟影响的完整对照组，以及一个存在正式失败的完整对照组；它不是全量成功率，也没有隐藏第 31 条 Codex 的 480 秒失败。恢复批次按相同办法核对时钟。

恢复后的 Codex T01 又收到一次供应商 `engine overloaded / rate limit exceeded`，T02 因依赖失败阻塞。这两条留在主要结果中，单列供应商错误原因。主对照结束后，在新会话补一组 T01→T02 诊断，单独保存，不覆盖主要分数，不据阻塞状态推断 Codex 的历史恢复能力。

补充诊断两题分别在 **61.5／121.1 秒**完成。T02 在新 worker 中承接并核对上轮四项主张，纠正“灯带她去天文部”为原片只表现两人随后在活动室，交付三张卡；仍未满足冻结标准要求的午餐回述。它证明该次跨进程追问能够工作，不能覆盖主实验的供应商失败或证明普遍恢复可靠性。原件及独立评审见 [Codex 多轮诊断](../../../outputs/verification/chat-harness-kimi-recovery-20261026/codex-followup-diagnostic/)。

原始证据：[首批中断记录](../../../outputs/verification/chat-harness-kimi-20261025/interrupted-run.json)、[原始时钟核对](../../../outputs/verification/chat-harness-kimi-20261025/clock-audit.json)、[恢复协议](../../../outputs/verification/chat-harness-kimi-recovery-20261026/protocol.json)。

## 关键案例与归因

T05 全剧五片段回顾中，三套收到完全相同的公共 Jev hook 说明：“本轮不涉及新的检索任务”“不新增视频检索”。Deep Agents 和 Pi 分别在 61.9／61.4 秒完成剧情推荐文案，但零工具、零播放卡；Codex 仍尝试查证，执行 31 次业务工具调用，最终在 480.3 秒超时，未完成交付。三套均未满足五片段任务。这里同时暴露前置路由与任务要求的冲突、冲突恢复方式，以及查证收敛问题，不能简单归因为某个框架的检索智力。

独立路由集的 13/13 正确并未覆盖这个全剧回顾输入，不能用组件通过率豁免端到端缺口。后续应将此输入纳入路由回归，并明确“推荐片段”何时必须交付可播放来源。本轮冻结题面、路由及业务代码，不边测边修。

公共 Query 也是明显瓶颈：24 次已结束的 `query_video` 外层工具调用中，20 次抛出了包含 `VIDEO_MODEL_TIMEOUT` 的错误，分别为 Deep Agents **8/9**、Pi **5/6**、Codex **7/9**。这里数的是业务工具调用，不是内部模型请求，也不包含只在成功返回体内报告的子项失败。Query 实际有效预算为 120 秒，Supervisor 总预算为 480 秒；一些请求随后改用事件检索恢复，另一些耗尽总预算。应同时检查公共子任务预算与搜索收敛，不能把所有慢请求归因于外层 harness。

T07 中 Codex 在 110.6 秒完成，正确标注 EP02 不可用并交付了 EP04 已知正例，但把同一 chunk 的多次提交误当作区间合并。14 次提交最终为 11 张卡，后一次范围覆盖前一次，正文列出的 EP04 12:09、12:33 和 EP05 03:49 不在最终卡中。需核对“提交动作”与“最终卡片状态”，不能只数成功工具调用。

T08 的 Pi 虽交付了全部位于暂停点之前的卡片，正文仍把 900000–909200 ms 的车站／扶梯进展视为当前已发生。公共 `understand_current_video` 把这个从锚点开始、结束在锚点后的事件标为 `after_anchor=false`；只看标志或卡片检查会漏掉正文边界。工具还返回了尚未加入检索目录的 chunk，引发可恢复的 `UNKNOWN_ITEM`。这些都须区分公共工具合同问题与 Supervisor 的边界核对能力。

同题 Codex 的四张卡也全部位于暂停前，但正文明确引用 EP10 **16:14、16:18、16:25** 的未来对话“证实目的”，越过 15:00 锚点。这属于实质性的时间约束失败，不能因为来源真实或机械卡片检查通过而判成功。

T09 明写场景与 15:00 限制后，Deep Agents 交付三段过去证据，并明确不提前解释后续具体谈话，但“特意等”“准备当面谈开”等动机没有始终标为推断，最终仍评 partial。Codex 仍在正文引用“进入15:00后”的对白；Pi 的当前卡实际延至 907.5 秒，机械检查也检出了越界。单次对照只能说明这些观察，不能据此推出明确题面的普遍提速或稳健性收益。

近义台词题的两处候选都允许成立。Codex 发现 07:02 前后的人物标签冲突后查看了帧，最终确认祥子回应爽世；执行代理复看同一检查的 420.812／425.687 秒帧，支持该归属，错误的是前一 chunk 的爱音标注。Deep Agents 则同时给出 09:08 “无关人士闭嘴”与 07:02 “别再来找我”，说明两者的含义和场景差别。见[静帧补核对](../../../outputs/verification/chat-harness-kimi-recovery-20261026/drift-frame-review.json)。

错误前提题中，Deep Agents 正确纠正“别来指点”不是原句，却把已知参考的爱音改成爽世。共享 Query 子任务明确看到 `speaker=null`，仍根据“爽世持贝斯、面向立希”等人物位置做声音归属；Supervisor 最终确认了它。补看的两张帧也可见坐着抱吉他的爱音，人物位置不足以推翻固定说话人参考。这是子任务证据推断与外层确认共同造成的错误，见[补核对记录](../../../outputs/verification/chat-harness-kimi-recovery-20261026/premise-frame-review.json)。

Pi 与 Codex 在同题均正确识别爱音对立希，说明反驳只针对吉他和弦，并指出原句没有“别来指点”；两套实际卡均为 1202.88–1211.44 秒。该例保留 Deep Agents 的明确质量回归，不以整体架构简化收益抵消。

## 评分边界

执行完成、旧来源检查、意图与顺序、任务与证据、实际交付分别报告。Kimi 在计时结束后依据冻结用例 rubric 及记录在评分协议中的版本／工具合同说明评分，样本打乱并隐藏 arm 标签；同模型裁判不是独立人工金标。执行代理再逐条核对回答、动作顺序、实际卡片和工具原文，并针对人物冲突补看四张已有检查帧，保留裁判原始判断及有证据的修订，不能用主观印象静默替换结果。

后置评分说明包含本轮观测后确认的工具合同、数据版本和故障规则，因此也不声称预注册独立盲测。文字评分包不附原始图片；模型已实际看图而文字包无法核实时，明确引用同一检查帧的补核对，保留原扣分及修订理由。

99 条主记录首轮评分中，97 条完成，2 条触及评分端 2,500 token 输出上限；只对这两份相同输入用 8,192 输出、180 秒上限补评，均完成，没有重跑被测 Chat。2 条诊断也完成独立评分，均为 partial。补评后有 13 条评分与执行代理初审存在维度分歧；逐条复核后，只有 4 条改变维度分，另 1 条仅修正评分理由。其余保留 Kimi 原分。见 [原评分与补评](../../../outputs/verification/chat-harness-kimi-recovery-20261026/judgments/)、[明确复核决定](../../../outputs/verification/chat-harness-kimi-recovery-20261026/adjudications.json) 和 [最终评审](../../../outputs/verification/chat-harness-kimi-recovery-20261026/final-assessments.json)。

后置评分共 103 次 Kimi 调用（99 主记录 + 2 诊断 + 2 次失败补评），已记录 **2,578,735 total tokens**，无缺失 usage；这部分单列于 [评分用量摘要](../../../outputs/verification/chat-harness-kimi-recovery-20261026/grading-summary.json)，不混入前面的被测框架用量。

评分脚本请求的 1,048,576 上下文和 2,000,000 字符上限被项目 binding 实际限制为 **128,000／400,000**，按 [实际评分预算](../../../outputs/verification/chat-harness-kimi-recovery-20261026/effective-review-budgets.json) 记录，不以调用参数冒充生效值。101 个评分包没有额外截断单条工具记录；原工具自身的分页／截断仍保留。用同一生产上下文控制器离线重放全部评分输入，确认初评和补评两种预算均保留完整原始用户载荷，长包只增加 checkpoint 提示，未清除工具记录，见 [上下文核对](../../../outputs/verification/chat-harness-kimi-recovery-20261026/review-context-audit.json)。

- 雨中等人与“说话无趣”没有穷尽负例金标。诚实地交付近似候选或说明未找到，与无依据声称精确命中分开；目标未核实仍不算完全通过。
- C19 通过只表示接住当前表达反馈、不复述旧剧情、不误开检索；没有独立用户偏好或幽默感评分，不能把通过写成“更有趣”的用户实验结论。
- 两条首次登台题没有明确乐队名，保留对象歧义；明确 MyGO 五人的 `holdout_mixed` 没有此问题。公开的 `holdout` 已是固定回归题。
- T02 固定标准要求午餐回述，但题面只要求回查 T01 刚才的结论。T01 没提午餐时，这项要求与字面请求存在争议。本轮保留冻结判分，同时单列意图处理是否正确；后续应把要求写入题面或改为条件化标准。
- EP01 初遇当前把「何か落とした？」标为 247.6–249.3 秒，旧参考将前置「あ」合在 247.2–249.2 秒。EP04 也存在时间、标点、事件切分和 speaker 缺失的版本差异；旧机械检查失败不自动等于语义召回失败。
- T06 连续坐下→起身→离开、T07 短促“哈？”的原声音型，仍需连续看片／回听。索引原文或稀疏帧不足以证明，本轮不冒充完成了独立人工音视频核验。
- 推荐与全剧回顾没有唯一金标。检查证据、数量、顺序、已审阅范围与取舍，不能把卡片数量当作全剧覆盖率。

总预算失败按未完成请求保留，但不抹掉过程中已经写入的卡片。例如 T07 中 Deep Agents 在超时前提交 7 张，Pi 提交 2 张；这些属于部分产物，不能改成终态完成。主观测来自 JSON HTTP 接口，本轮没有验收失败时浏览器／SSE 实际展示了哪些中间内容。

分维度评分允许已完成的部分内容获得 `task_and_evidence=partial`，同时因请求超时或缺交付而 `delivery/overall=fail`。因此表中的“任务 partial”不能读成请求成功；通过数只统计 pass，执行状态和综合结果始终另列。

用量按 `call_id` 去重，区分 SDK aggregate 与 provider-request 粒度；缺失使用量记未知，已知 token 为观测下界。cached/reasoning 是子集，不再次加到 total。Query 等内部生成调用也使用 Kimi，不因其 `context.stage` 与 Supervisor 相同而混淆职责。没有金额口径，不换算费用或给出“更便宜”的排名。

主要用量表只包含 99 条计划记录对应的实际生成调用，不包含被排除的中断查询批次、组件测试、预检、补充诊断与后置评分。主要 Codex T02 为 BLOCKED，其余实际 Codex 主请求均为各自原生会话首轮，不存在该表跨 T01/T02 累计量重复求和；补充多轮诊断的原生累计用量原样保存、不混入主表。详见 [用量口径](../../../outputs/verification/chat-harness-kimi-recovery-20261026/accounting-notes.json)。

## 独立组件结果

这两组是公共检索或 Kimi 分类组件的检查，不是三个 Supervisor 各跑一次，也不计入 99 条主要观测。

| 项目 | 已执行结果 | 能说明什么 |
| --- | --- | --- |
| 直接检索 | 13/13 执行完成，9/13 通过旧来源机械检查 | 保留召回集合与已知来源检查；不等于 69% 语义准确率 |
| Kimi 意图路由 | 13/13 标签正确 | respond/retrieve/mixed 分类通过；不等于 Chat 能把任务完整做好 |
| 结构化协议 smoke | 1/1 通过 | 独立的结构化返回协议检查 |

检索的四条机械未通过中，`retrieval-full-chunk-original` 确实未召回 EP13 目标；EP04 Event 原句和近义两条已把目标放在首位，但时间与标点版本不同；EP04 Event premise 则涉及原合并事件被拆分及 speaker 缺失，不能直接当成完全没召回，也不能未经身份核验就改判全对。旧规则保留，原因另注。

组件原件：[检索结果](../../../outputs/verification/chat-harness-kimi-recovery-20261026/retrieval-quality.jsonl)、[Kimi 路由结果](../../../outputs/verification/chat-harness-kimi-recovery-20261026/intent-routing-kimi.jsonl)。这些路由调用没有执行业务动作；主要端到端实验仍使用共同的 Jev/Qwen 前置链路。

## 运行与复核入口

主要原始目录：[chat-harness-kimi-recovery-20261026](../../../outputs/verification/chat-harness-kimi-recovery-20261026/)。其中前 60 条链接到首批原件，39 条查询覆盖为恢复后的独立记录。精简基线已提升到 [60 条行为观测](../chat_behavior/baselines/harnesses-kimi-20261026.jsonl) 和 [39 条查询观测](../query_capabilities/harnesses-kimi-20261026.jsonl)，完整工具返回、原生会话、源码快照和依赖留在被忽略的本地实验目录。

`compare.py` 默认只输出离线计划，真实运行必须显式 `--live`，并提供 Kimi 配置及已有数据／服务。复跑需要准备退役 Pi runtime 的隔离依赖，不能把当前仓库单独克隆视为已包含视频、索引与全部运行环境。

```bash
# 离线查看 99 条调度计划，不调用 API。
.venv/bin/python -m evals.benchmark.compare \
  --config outputs/verification/chat-harness-kimi-recovery-20261026/model-config.yaml \
  --out outputs/verification/新的运行目录

# 以下才是新一次真实运行；需要原素材/索引、SQL/ES、Qwen 改写服务和凭据。
benchmark_out=outputs/verification/chat-harness-kimi-new
.venv/bin/python -c 'from pathlib import Path; import sys; from evals.benchmark.harnesses import prepare_pi; prepare_pi(Path(sys.argv[1]))' "$benchmark_out/legacy-pi"
npm --prefix "$benchmark_out/legacy-pi/host" ci
npm --prefix "$benchmark_out/legacy-pi/host" run build
UV_CACHE_DIR=/private/tmp/anivag-eval-uv-cache uv run --no-sync --env-file .env.local \
  python -m evals.benchmark.compare \
  --config outputs/verification/chat-harness-kimi-recovery-20261026/model-config.yaml \
  --out "$benchmark_out" --live

.venv/bin/pytest -q tests/evals/test_benchmark.py tests/evals/test_harness_comparison.py
```

长上下文压缩保真、多会话并发、RSS、常驻 Host 复用、进程崩溃／跨节点恢复、sandbox 生命周期与权限隔离均未由这组 Chat fixture 完整验证。不能从 10 个对话场景和 13 个查询任务推出通用智能排名或全部架构验收。

## 变更与审计范围

本轮生产代码和业务默认配置修改均为 **0**。新增的两个 Python 入口只用于评测，Pi 依赖留在隔离实验目录，不增加生产 Node Host 或服务依赖。新成本是维护三套版本适配、冻结协议及结果档案；后置 Kimi 评分产生额外调用，但没有接入产品请求链路。

```text
evals/benchmark/
├── [A] compare.py
├── [A] harnesses.py
├── [M] README.md
├── chat_behavior/
│   ├── [M] README.md
│   └── baselines/[A] harnesses-kimi-20261026.jsonl
├── query_capabilities/
│   ├── [M] README.md
│   └── [A] harnesses-kimi-20261026.jsonl
└── reports/[A] chat-harness-kimi-20261026.md
tests/evals/[A] test_harness_comparison.py
docs/dev/[M] devlog.md
```

合计 10 个文件：6 新增、4 修改；评测源码 2、测试 1、文档 5、数据 2。按本轮范围核对为 **+924／-13 行**，包含 101 行精简 JSONL；评测源码与测试新增 494 行，生产代码 0。既有及并行产生的无关工作区改动保留。

离线检查：`tests/evals/test_benchmark.py` 与 `test_harness_comparison.py` 共 **22 passed**；两个评测入口及新增测试通过编译；默认入口输出 99 条计划并确认 `api_called=false`。新增测试保护三个 arm 的用例／重复次数与 T01→T02 顺序，并拒绝非 Kimi 或带 fallback 的生成配置。它们不取代真实 99 条观测或逐例质量评审。

两份提升后的 JSONL 校验通过：60／39 条唯一观测、用例版本及逐条原件 SHA 一致、维度聚合一致、无 GPT 调用记录，通过凭据值与签名 URL 检查。查询基线离线 replay 为 **39 条、33 执行完成、24 通过旧机械来源检查**，`overall_accuracy=null`、无 API 调用；旧来源检查不替代本文证据评分。见 [基线校验](../../../outputs/verification/chat-harness-kimi-recovery-20261026/promoted-validation.json) 与 [离线检查](../../../outputs/verification/chat-harness-kimi-recovery-20261026/validation-checks.json)。

最终运行审计核对 326 个相关 Python 文件、包版本、配置和两个 fixture 均未变。98 条实际主要请求保存为 92 COMPLETED／6 FAILED，另 1 条为未发送的依赖 BLOCKED；96 个主实验会话。被排除批次、预检和补充诊断另核对 19 条自有消息，均为终态；无 PROCESSING、无返回正文与 SQL 不一致、无原生历史注入失败告警。见 [最终审计](../../../outputs/verification/chat-harness-kimi-recovery-20261026/final-audit.json)。

数据后快照仍为 14 个视频、44 个任务，状态摘要与起始一致。本次启动的 Qwen、主调度、诊断、评分、补评及对应防休眠进程均已退出，没有本实验遗留的 Pi/Codex 原生进程。原有 SQL 3306、ES 9200、API 9621 保持运行，测试专用 18841 已关闭。测试会话、原生 checkpoint、检查帧及完整实验档案保留以便复核；没有迁移、重解析、commit 或 push。退出状态保存于 [资源清理核对](../../../outputs/verification/chat-harness-kimi-recovery-20261026/resource-cleanup-audit.json)。
