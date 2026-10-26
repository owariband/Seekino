# Chat Behavior · 对话理解与应变

这就是围绕“真是高高在上呢 哪里出现过”与“那又咋了，你说话怎么这么无趣啊”建立的 **10 个端到端对话场景**。它衡量 Chat 是否理解当前请求、接住反馈并完成任务，不把 multi-agent 工具覆盖或前置分类准确率当成对话质量。

维护入口：[cases.json](cases.json)。当前输入逐字沿用 2026-10-24 `query-hook-chat-model-ab` 正式实验冻结版：每例包含稳定 ID、`state.query`、完整 `state.recent_messages`、参考 `expected`。这里的 `expected` 只是意图标签；语义通过还需按下表检查实际回答、调用顺序与播放卡。

## 用例与验收

| ID | 场景 | 预期动作 | 业务验收重点 |
| --- | --- | --- | --- |
| `retrieve_original_quote` | 真是高高在上呢 哪里出现过 | retrieve | 检索出处，定位 EP13 00:54.6–00:56 祥子对睦的「いいご身分ですわね。」；正确说明字幕差异，正文与卡片相符。 |
| `c19_original_history` | 已给出处后，用户说“那又咋了，你说话怎么这么无趣啊” | respond | 直接回应用户对助手表达的反馈，不继续找台词、不复述旧剧情、不把反馈改写成给用户用的回复示例。 |
| `respond_existing_evidence` | 只根据刚贴的句子解释拒绝邀请的含义 | respond | 遵守“只根据”；说明已有安排与讨厌邀请者之间的证据差别，不补检索或臆测心理。 |
| `mixed_feedback_and_search` | 先回应吐槽，再找首次登台 | mixed | 实质回应反馈在先，检索与交付在后，两项都完成；不把“我将搜索”算作回应吐槽。 |
| `respond_stop_retrieval` | 停止找视频，把解释压成一句话 | respond | 停止新增检索，给出一句压缩后的内容，不能只承诺“会简短”。 |
| `retrieve_correct_negation` | 不用解释过程，不等于不用检索 | retrieve | 修正误解，继续找首次登台；不能被“不用”关键词误导成停止任务。 |
| `mixed_reference_and_feedback` | 第二件先回答，第一件继续找 | mixed | 解析历史中的两件事：先回答为何把吐槽当台词，再找雨中等人；不交换顺序或漏项。 |
| `retrieve_feedback_quote` | 明确要求找角色说“你说话怎么这么无趣啊” | retrieve | 执行台词检索，不误当用户反馈；“无聊的女生”等近似候选不能宣称逐字命中。 |
| `holdout_feedback` | 出处已知，只谈表达机械 | respond | 自然、简洁地回应表达问题，不重复出处或启动检索。 |
| `holdout_mixed` | 先把出处压成一句，再找 MyGO 五人首次登台 | mixed | 先实际完成一句话摘要，再交付明确目标的演出片段；不要重新按上文角色猜另一支乐队。 |

首次 live 的已知正例在 EP07，607.7–610.8 秒的首次 live 自述是一个参考锚点；能证明同场首次登台的其他连续片段也可通过，不强制只交付这一句。雨中等人和“说话无趣”没有全库穷尽负例金标：没有找到不能断言原片不存在，近似候选要标出差异。纯回复题中的拒绝邀请对话是固定文本 fixture，不冒充已核实的原片台词。

统一评审：`pass` 为本轮全部任务、顺序、证据与交付均成立；`partial` 为方向正确但只有近似候选、陈旧复述、无依据推断或错误时间；`fail` 为错误意图／对象、漏掉独立任务、无证据的精确命中、超时／调用失败；`unassessed` 为输入歧义或证据不足，不能算通过。评审时单列意图／顺序与业务质量，不能仅凭“没有调用搜索”就认定回答好。

## 历史结果

| 文件 | 对照条件 | 正式观测 |
| --- | --- | --- |
| [jev-vs-direct-20261024.jsonl](baselines/jev-vs-direct-20261024.jsonl) | old：旧 Prompt + Jev/Qwen；new：v4 Prompt + 主 Chat 直接处理，无 hook | 10 × 2 arms × 2 repeats = 40 |
| [hook-models-20261024.jsonl](baselines/hook-models-20261024.jsonl) | 相同 v4 Chat 下，Jev/Qwen 与同 Chat 模型 low / none 前置决策比较 | 10 × 3 arms × 2 repeats = 60 |

每个 JSONL 首行 `kind=run` 保存**当轮完整 fixture**、协议、原汇总及来源路径／SHA；后续逐例保存最终回答、实际卡片、render blocks、工具名与参数、耗时／用量、原评审。精简产物不含完整工具返回和召回正文；原本地实验仍保留：[Jev 与直接 Chat](../../../outputs/verification/jev-chat-e2e-ab-20261024/)、[前置模型对照](../../../outputs/verification/query-hook-chat-model-ab-20261024/)。

两轮只有 `holdout_mixed` 输入不同：前轮为“她们第一次登台”，后轮正式运行前改为明确的“MyGO!!!!! 五人第一次登台”。旧轮该题保留 `unassessed`，没有拿新题给旧答案重新判分。原 native history 注入告警、失败和缺失用量也保留在结果中。

原评审计数（pass / partial / fail / unassessed）：第一轮 old **10 / 8 / 0 / 2**，new **12 / 2 / 4 / 2**；第二轮 Jev **15 / 5 / 0 / 0**，low **13 / 6 / 1 / 0**，none **11 / 7 / 2 / 0**。这些是当时模型、Prompt 和索引条件下的结果，不是当前 Deep Agents + Kimi 的验收。

协议中的历史 GPT 名称是记录，不是以后运行的默认值。两轮 Prompt、题目版本有差异，不能合并为一个比较分数；每题两次不足以报告显著性或 P95。`holdout` 仅指当时新增措辞，如今已公开，应当作固定回归题。

## 当前如何使用

2026-10-26 已完成 **Kimi `k3.1-pro` × Deep Agents / Pi / Codex × 每题两次**的 60 条主观测，见 [三框架基线](baselines/harnesses-kimi-20261026.jsonl) 和 [完整对照报告](../reports/chat-harness-kimi-20261026.md)。本轮使用上述完整冻结历史，前置 Jev/Qwen、业务 Prompt、Query 与工具共用，仅替换 Supervisor harness；没有调用 GPT 生成模型。

“高高在上→说话无趣”原场景中，Deep Agents 和 Codex 均两次接住当前反馈，Pi 两次仍复述旧剧情。通过仅表示遵守该场景的反馈／意图要求，不代表独立用户评测证明更有趣。两条未明确乐队名的首次 Live 题保留对象歧义；当前固定 `holdout_mixed` 已明确 MyGO 五人。

新基线分别保存实际交付正文、Pi 原始增量的语义诊断拼接、动作顺序、用量、Kimi 原评分和执行代理的明确证据修订。Pi 的增量断段不能用诊断拼接掩盖；未找到精确台词的诚实回答也不能冒充完成目标。失败、未知用量、时钟异常与评分失败的补评记录保留，同模型评分不是独立人工金标。分维度分数和延迟配对口径以报告为准。

先编辑／评审 `cases.json` 与本文件的验收表，离线完整性检查执行：

```bash
.venv/bin/pytest -q tests/evals/test_benchmark.py tests/evals/test_harness_comparison.py
```

[compare.py](../compare.py) 是当前三框架复跑入口，默认只输出离线计划。真实运行需显式 `--live`，并准备报告列出的 Kimi 配置、SQL/ES、素材、改写服务及隔离 Pi 依赖。入口将完整历史写入新会话，再调用真实 ASGI HTTP；使用固定 MyGO scope `836fa9f0-2bf4-50fb-9c64-75526fa1dea9`，记录回答、工具顺序、卡片和终态。不能把历史拼成一条普通用户消息或跳过历史来声称复现。

生成式模型按约定使用 `k3.1-pro`，`expected` 与验收标准不进入被测模型输入。模型与 harness 的结论须分开，并记录 Jev/Qwen 是否仍存在、索引可用范围与预算；每题两次只能支持这批固定回归观察，不报告显著性、P95 或通用智能排名。
