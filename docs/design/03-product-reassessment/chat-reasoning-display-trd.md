# 1. 问题与现状

**Chat 思考过程展示 TRD。状态：初稿（2026-11-05）。** 分支 `feat/chat-reasoning-display`，PR 待创建。依赖：[chat-answer-shape-trd.md](chat-answer-shape-trd.md)（PR [#51](https://github.com/owariband/Seekino/pull/51)，完成状态行与视频卡版式在该分支调整，本分支基于它，须在 #51 之后合并）；[chat-text-delta-stream-trd.md](chat-text-delta-stream-trd.md)（PR #49，正文增量交付，本需求沿用其事件通路，不改正文合同）。触发：2026-11-06 面向面试官演示。

## 背景与触发场景

学习路线类问题一轮需要 50–110 秒（LOOP-08 重跑：LP01 90.2 s、首字 79.5 s；LP02 108.8 s）。这段时间里页面只有吉祥物和「查找视频资料 进行中」状态行，用户看不到系统在想什么，容易以为卡住。DeepSeek 等产品的做法是把模型的思考过程以可折叠区域展示，默认折叠，想看的用户展开即可。

## 现状证据

1. Supervisor 使用 `deepseek-flash`，开启 thinking（`extra_body.thinking.type=enabled`，`reasoning_effort=low`），Provider 在流中返回 `reasoning_content`。
2. `gateway/streaming/chat.py` 只把 `choice.delta.content` 转成 `ModelTextDelta`；`reasoning_content` 只在完成后被复制进消息历史（`gateway/agent_model/deepagents.py` 的 `_copy_assistant_reasoning`），供下一轮请求使用，从未到达前端。
3. LP01 的 90 s 中，11 轮 Supervisor 模型调用约占 55 s，大部分是工具调用前的思考；这正是用户等待时最想看到的内容。

## 本次边界

- 解决：把 Supervisor 每轮模型调用的思考增量实时推送到前端，在回答上方以「思考中… / 已思考（用时 N 秒）」可折叠区域展示，默认折叠。
- 不解决：思考内容持久化到历史消息（刷新后不再显示，演示后再评估）；缩短思考时间本身（见回合耗时跟进）；子 Agent 的思考。
- 保持：正文增量、视频卡、工具状态行的既有合同；调试模式额外信息照常。

# 2. 影响因素、假设与评价指标

| 因素 | 证据 | 假设 | 验证 |
| --- | --- | --- | --- |
| 思考增量未被转发 | `chat.py` 只读 `content` | 从同一流读取 `reasoning_content` 并沿正文增量的观察者通路发出，不增加请求或延迟 | 单测 + 门禁首个思考事件时间 |
| 前端无展示位 | `AgentProgress` 只有状态 | 折叠区域放在回答上方，默认折叠，展开显示累计思考文本 | 前端单测 + 浏览器截图 |

| 指标 | 定义 | 基线 | 目标 / 护栏 |
| --- | --- | --- | --- |
| 首个可见反馈（结果） | 发送后到首个思考增量到达前端的时间 | 无（首字 LP01 79.5 s） | ≤ 10 s |
| 回合耗时（护栏） | doneSeconds | LP01 90.2 s | 不因本需求增加超过 5% |
| 门禁完成（护栏） | LOOP-08 九门禁 COMPLETED | 9/9 | 9/9 |
| 回归（护栏） | 相关后端测试、`npm --prefix web test`、typecheck | 通过 | 通过 |

# 3. 改造方案与建模

1. **Gateway**：`gateway/streaming/chat.py` 在同一流中读取 `choice.delta` 的 `reasoning_content`，以独立的观察类型（与 `ModelTextDelta` 并列）通过 `gateway/model_events.py` 的上下文观察者发出；嵌套调用的抑制规则与正文一致。
2. **Supervisor**：订阅思考增量，发布 `answer.reasoning.delta` 事件（`callId`、`delta`）；不进入正文、不参与渲染块校验。
3. **Server**：事件类型加入 SSE 视图与事件合并逻辑，按正文增量的方式处理。
4. **前端**：事件合同与会话 hook 按消息累计思考文本；回答上方渲染折叠区域，进行中显示「思考中…」，完成后显示「已思考（用时 N 秒）」，默认折叠；没有思考内容的历史消息只显示一行完成状态。

# 4. 对照实验与结果分析

待测：重跑 LP01/LP02/SE01，记录首个思考事件时间与回合耗时，对照 `gates/loop08-shape-60974360-rerun.json`。

# 5. 试点验证与方案取舍

待测：演示环境浏览器截图，确认默认折叠、展开可读、完成后保留。

# 6. 结果验收与闭环

待实验与试点完成后填写。
