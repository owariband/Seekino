# 1. 问题与现状

- 分支：`fix/watch-answer-delivery`；PR：[#33](https://github.com/owariband/Seekino/pull/33)。
- 更新日期：2026-11-04。状态：实现完成，真实模型效果待测。
- 相关问题：C22（观看追问尝试不可用的视频提交）、C25（回答超长并暴露内部取证术语）。

**背景与触发场景：** 课程 QA 的观看模式（Q01–Q03），学习者在 01:30 暂停讲座后连续追问。客户端冻结了播放位置，`realtime_available` 为真；该课程尚未 READY、未进入可检索范围，`video_available = bool(inputs["series_ids"])` 为假，两者可在同一会话内同时成立（源码 `agent/chat/supervisor.py` 约 233/249 行）。

**实际行为：** 当回答依赖 `understand_current_video` 的实时证据时，模型仍调用 `submit_video_item` 提交卡片。实时证据 Chunk 不是检索命中，`submit_video_item` 因此抛出 `UNKNOWN_ITEM` 或 `VIDEO_EVIDENCE_UNAVAILABLE`。两组 Q01–Q03 共 5 次卡片提交失败、1 次无范围检索失败；模型随后对用户说“没有可播放片段”，而正文中的 Markdown 时间码链接实际能正确跳转播放（PB01 通过），该说法与事实冲突。同时 Q01 要求简短、Q02 要求“总共三句话”，回答均超长，Q02 还在三点之后追加来源段；Q01 把 `partial`、Chunk、`visual_summary`、`PARSING_FAILED`、`omitted_evidence` 等内部术语写给了用户。

**负面影响：** 无效卡片调用浪费模型轮次、制造错误反馈，失败后误导用户“无法回看”；超长回答违背用户的长度要求、增加阅读负担；内部术语暴露取证实现细节。三者都属于用户交付缺陷，不能因为 HTTP 最终完成而放行。

**现状证据：** [补齐 captions 后的 QA 报告](../../../outputs/brief-context/course-qa-deepseek-caption-20261103.md)、[C22](../../../outputs/brief-context/problems-C22.md)、[C25](../../../outputs/brief-context/problems-C25.md)。守卫本身正确（拒绝非检索命中的提交），不能为消除错误而绕过来源与版本校验。

**本次边界：** 只改 Supervisor 的工具注册与提示词，以及对应测试与 TRD。不改 gateway/、server/、engine/、检索工具，不改实时 runtime 的证据格式。必须保持：普通（非观看）对话仍照常注册卡片工具；观看模式下时间码链接仍可跳转（PB01）；检索命中后仍可正常提交视频卡。

# 2. 影响因素、假设与评价指标

**因素与假设：**

- 工具可用性：`submit_video_item`/`remove_video_item` 目前无条件注册。观看且无可检索系列时，实时证据不可能成为检索命中，任何卡片提交只能失败。假设：这类会话不注册卡片工具，即可消除“必然失败”的提交，同时不影响有检索系列或普通对话。
- 交付路径提示：模型未被告知实时证据只能以时间码链接交付、`submit_video_item` 只接受本轮检索工具返回的 `video_chunk_id`。假设：明确该合同可避免把实时 Chunk 当候选提交。
- 失败表述：提示未说明卡片提交失败不等于无可回看来源。假设：提示“时间码链接仍是可回看的依据”可纠正“没有可播放片段”的误导。
- 长度与内部术语：提示未约束用户长度要求，也未禁止把内部字段名/状态码写入回答。假设：明确“整条回复（含来源）控制在内、来源写进句内时间码链接、不得为缩短删来源”以及“内部术语不得对用户输出”，可同时压缩长度且不丢来源。

**指标与验收标准：**

| 指标 | 定义与统计口径 | 基线 | 目标 | 测量方法与条件 | 证据入口 |
| --- | --- | --- | --- | --- | --- |
| Q01–Q03 单轮失败卡片提交数 | 每轮三问内 `submit_video_item` 报错次数 | 两组共 5 次 | 0 | 真实模型重跑 Q01–Q03（隔离课程、01:30 暂停、既有 Chunk） | QA 报告，待测 |
| Q02 句数 | 完整回复（含来源标注）的句子数 | 三点+追加来源段 | ≤3（含来源） | 真实模型重跑，人工计数 | QA 报告，待测 |
| 用户可见文本中的内部术语数 | partial/Chunk/visual_summary/omitted_evidence/PARSING_FAILED/after_anchor 等出现次数 | >0 | 0 | 真实模型重跑，人工检查 | QA 报告，待测 |
| 时间码链接仍存在且跳转正确（护栏） | PB01 跳转通过 | 通过 | 保持通过 | 真实模型重跑 | QA 报告，待测 |
| 普通对话卡片提交不变（护栏） | 非观看、有 series 范围时 `submit_video_item` 仍注册且可用 | 可用 | 保持 | 单元测试 + 既有回归 | `tests/agent/chat/test_supervisor.py` |

# 3. 改造方案与建模

**已有基础：** 工具注册、`video_delivery` 与 `prompt` 已按 `video_available`/`realtime_available` 分支组装，无需新增依赖或跨层改动。

**方案与取舍：** 采用“条件注册 + 提示约束”的最小组合。

- 工具注册：把 `submit_video_item`/`remove_video_item` 抽成 `video_card_tools`，仅在 `video_available or not realtime_available` 时并入 `tools`。即：普通（非观看）对话保持现状照常注册；观看会话仅在有可检索系列时保留卡片工具。观看且无可检索系列时，模型拿不到卡片工具，从机制上杜绝“只能失败”的卡片提交。这是比“提示引导绕开”更小的确定性方案，且不改非观看对话契约。**不采用**“等检索工具返回命中后再动态注册/删除”的运行时增删规则：需要在模型循环中反复变更工具集，成本更高且本次问题（观看且无检索系列）不需要检索命中才成立。
- 提示：在 `realtime_available` 分支补齐三条规则——(a) 实时证据一律以正文中的 Markdown 时间码链接交付，`submit_video_item` 只接受本轮检索工具返回的 `video_chunk_id`；(c) 时间码链接仍是可回看依据，卡片提交失败不得说“没有可播放片段”；(d) 用户要求简短或限定句数时整条回复（含来源）控制在内，来源写成句内时间码链接、不另起段、不得为缩短删来源；(e) `chunks/frames/visual_summary/partial/omitted_evidence/after_anchor/PARSING_FAILED/NO_VIDEO_SCOPE` 等是内部术语，不得写入对用户回答，覆盖范围用普通中文说明。另在 `video_delivery` 的提交句补一句“video_chunk_id 只能是本轮检索工具返回的值”，把检索提交合同统一。
- 失败反馈：`submit_video_item` 的 `VIDEO_EVIDENCE_UNAVAILABLE`/`UNKNOWN_ITEM` 提示改为指向“实时证据以时间码链接交付、submit 只接受检索命中”，供极少数仍被调用的场景给出正确下一步。

**关键设计：** 工具集按 `(video_available, realtime_available)` 判定；实时证据与检索命中是两条交付路径（时间码链接 vs 视频卡），互不混用。

**实施边界：** 仅 `agent/chat/supervisor.py` 与 `tests/agent/chat/test_supervisor.py` 及本 TRD。不改检索工具、实时 runtime、gateway/server/engine，无数据迁移。

**验证顺序：** 先加单元测试证明注册与提示合同，再交真实模型重跑 Q01–Q03（待测）。单元测试通过不代替真实模型效果。

# 4. 对照实验与结果分析

**比较对象：** 同一仓库修改前后；单元测试覆盖注册与提示合同，真实模型指标另行重跑。

**可比条件：** 单元测试复用既有 fake（`FakeVideoRuntime`、`FakeWebSearcher`、自定义 `Model`）；真实模型重跑沿用既有隔离课程、01:30 暂停点、Chunk 0/1/2，由 lead 执行。

**可复核记录：** `uv`/`.venv` 环境，命令 `.venv/bin/python -m pytest tests/agent/chat -q -m "not live_agent"`。

**实际结果（单元测试）：** 修改前 `tests/agent/chat` 388 passed；修改后 391 passed（新增 3 条用例，8.41 秒）。新增用例断言：(1) 观看轮 + 实时证据 + 无检索命中时不提供 `submit_video_item`/`remove_video_item`，且 `understand_current_video` 可用、证据可用、正文时间码链接交付、`video_result is None`；(2) 普通对话带 series 范围仍注册卡片工具；(3) `realtime_available` 提示段包含时间码链接交付规则、长度规则与禁止内部术语规则，以及失败不得称“没有可播放片段”。

**真实模型结果：** 待测。由 lead 重跑 Q01–Q03，按上表记录四项主指标与护栏。

# 5. 试点验证与方案取舍

本次为提示与注册合同的小范围改动，风险低，以代表性集成验证（单元测试 + 真实模型重跑 Q01–Q03）替代独立试点。真实模型重跑前不宣称效果达标。若重跑显示卡片失败仍出现（例如有检索系列时仍提交实时证据），则回到第 3 节补强提示或考虑更窄的工具集；若普通对话卡片提交回归，则回退注册条件。

# 6. 结果验收与闭环

**原问题验收：** 单元测试已证明注册与提示合同（如上）；真实模型的失败卡片数、Q02 句数、内部术语数、时间码链接跳转四项仍待测，暂不标记“问题已解决”。

**最终决定：** 实现完成、单元测试通过；真实模型效果待测，待 lead 重跑后闭环。

**项目实际状态：** `agent/chat/supervisor.py` 已改，`tests/agent/chat/test_supervisor.py` 已加 3 条用例；未部署、未合并。

**剩余事项：** lead 用真实模型重跑 Q01–Q03，记录四项主指标与 PB01 护栏；确认后回填本 TRD 第 4/6 节并更新 problems.md 的 C22/C25 状态。当前 PR 待创建。

## 决策记录

| 日期 | 原判断或方案 | 新证据 | 调整与原因 | 对目标、验收及后续工作的影响 |
| --- | --- | --- | --- | --- |
| 2026-11-04 | 去掉视频卡工具即可避免失败提交 | QA 的观看会话带有课程库系列范围（`video_available` 为真），工具门控在该场景不生效 | 门控只覆盖“仅有当前播放、无可检索系列”的情况；QA 场景依赖 Prompt 规则与修正后的工具反馈 | 必须以真实模型复测 Q01–Q03，统计失败的卡片提交次数；若仍出现，再评估按“本轮是否有检索命中”动态暴露工具 |
