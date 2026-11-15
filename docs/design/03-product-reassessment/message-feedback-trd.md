# 1. 问题与现状

- 需求：Assistant Message 级用户反馈（message feedback）
- 分支：`feat/message-feedback`
- PR：待创建
- 状态：实现完成，定向验证通过；未提交、未部署，真实试点待进行
- 更新时间：2026-11-15（Asia/Shanghai）

## 1.1 背景与触发场景

Seekino 的 Chat 已经能保存 Conversation、Message、回答正文、证据、模型与失败信息，但用户没有对单条回答表达“有用/没用/哪里不对”的产品入口。当前可用的反馈方式只有页面右上角的 GitHub 仓库入口和 `/docs` 里的 GitHub Issues 链接；这两者都要求用户离开产品、拥有 GitHub 账号，并且无法把反馈关联回具体的 `messageId`、模型、回答阶段和检索证据。

2026-11-14 的线上数据暴露了这个问题：

- 当天 18 条 assistant 消息中 1 条失败（`AGENT_MODEL_ERROR`，`deepseek-flash`，约 196 秒后失败），用户没有可点击的重试或报告入口；
- 近 7 天 86 条 assistant 消息中 8 条失败（9.3%），全部为 `AGENT_MODEL_ERROR`；
- 当天 0 条成功搜索，17 个问答用户，但没有一条结构化反馈能说明“回答是否解决了问题”；
- 失败回答只显示 `模型调用失败，错误：AgentModelInvocationError`，属于内部类名，且没有“报告问题”动作；
- `/docs` 的“交流群”仍是 `[xxx]` 占位符，公开页面没有有效的社区反馈入口。

这不是一个“再加一个按钮”的孤立问题：没有消息级反馈，团队无法区分模型失败、检索证据错误、回答不完整和播放问题，也无法按模型/阶段/证据命中情况做质量比较。

## 1.2 实际与期望行为

当前行为：

- `AgentMessage` 没有用户反馈字段或关联表；
- 消息列表、详情和 SSE 快照都不返回当前用户的反馈；
- `COMPLETED` 回答没有 👍/👎；
- `FAILED` 回答只有错误文案，没有“报告这个问题”；
- 用户只能通过 GitHub Issues 描述问题，缺少消息上下文；
- 反馈没有可查询的评分、原因、模型、阶段和证据快照。

期望行为：

- 用户可以在已结束的 assistant 回答下用一次点击表达正向/负向；
- 负向时可以补充原因和可选说明；
- 失败回答可以直接报告，并自动带上错误码、模型和阶段；
- 刷新页面后仍能看到自己此前的反馈；
- 同一用户同一条消息只有一条反馈，可修改、可撤销；
- 服务端能按消息、模型、状态和原因聚合，供后续 triage 与评测使用。

## 1.3 负面影响

- **用户侧：** 遇到错误回答或失败时没有低成本反馈路径，只能离开产品去 GitHub，或者直接放弃。
- **产品侧：** 无法知道哪类问题最影响使用，无法判断失败是模型、检索还是回答组织导致。
- **工程侧：** 失败率、延迟和证据命中已经可观测，但“用户是否认为回答有用”没有事实来源，优化只能靠猜测。
- **运营侧：** GitHub Issues 是公开、异步、需要账号的渠道，和站内问答没有关联，难以形成稳定闭环。

## 1.4 现状证据

已确认事实：

- `server/model/agent.py` 的 `AgentMessage` 当前没有 feedback 字段或关联表；
- `server/api/agent.py` 的 `_agent_message_view` 只投影消息正文、证据、错误和耗时；
- `web/src/features/search/SearchPage.tsx` 的失败分支只渲染 `message.lastError?.message`，没有操作按钮；
- `web/src/features/docs/content.ts` 的“交流群”使用 `COMMUNITY_PLACEHOLDER = "[xxx]"`；
- 2026-11-14 00:00–20:12（Asia/Shanghai）API 日志与 MySQL 统计：18 条 assistant 消息、1 条失败、0 条用户反馈记录；
- 2026-11-10 至 2026-11-14 的 assistant 消息为 86 条，其中 8 条失败，全部为 `AGENT_MODEL_ERROR`。

尚未确认：

- 用户在面对失败回答时是否愿意点击“报告问题”；
- 有多少比例的用户愿意对正常回答评分；
- 哪些原因码最常被选择；
- 反馈是否能稳定改善后续 triage 和评测效率。

这些属于本次试点要回答的问题，不能写成已实现效果。

## 1.5 本次边界

本次解决：

- 新建 message feedback 持久表；
- 新增写入/清除接口；
- 消息列表、详情和 SSE 快照返回当前用户的反馈；
- 已完成/失败 assistant 消息的反馈 UI；
- 失败回答的“报告问题”入口；
- 迁移脚本、后端测试和前端测试。

本次不解决：

- 管理后台 triage 页面；
- 自动创建 GitHub Issue；
- 自动上传对话正文或模型日志；
- 反馈驱动的自动重试、模型切换或评测集生成；
- 正向原因标签、多级评分、表情回应；
- 对历史消息回填反馈。

必须保持：

- 现有消息发送、SSE、证据和失败语义不变；
- 反馈写入不影响回答生成延迟；
- 旧消息和旧客户端在缺少 feedback 字段时仍可工作；
- 删除 Conversation 时反馈随消息级联删除；
- 只有 Conversation 所有者能读写自己的反馈。

# 2. 影响因素、假设与评价指标

## 2.1 影响因素

| 因素 | 当前证据 | 可能机制 | 本次改变 |
| --- | --- | --- | --- |
| 反馈摩擦 | 目前只能跳 GitHub Issues | 点击次数、是否需要账号、是否要描述上下文，决定反馈率 | 消息下方一键 👍/👎；负向再展开原因 |
| 反馈结构 | 现有渠道只有自由文本 | 固定原因码比自由文本更易聚合和比较 | 稳定 code + 可选 comment |
| 失败上下文 | 失败消息没有操作入口 | 失败时用户最想报告，但缺少入口 | 失败气泡增加“报告这个问题”，自动带错误码 |
| 归属与持久化 | 没有反馈表 | 无归属的反馈无法安全读取和删除 | `(message_id, user_id)` 唯一，会话归属校验 |
| 消息版本 | 消息有 `version`，但反馈没有版本概念 | 回答更新后旧反馈可能误套到新内容 | 保存反馈时的 `message_version` |
| 质量归因 | 只有耗时/失败/证据指标 | 没有用户判断，无法比较模型和阶段 | 保存 model、status、error_code，支持聚合 |
| UI 位置 | 失败文案在回答内部 | 入口太远或太显眼都会影响使用 | 放在回答底部，失败时替代普通评分 |

## 2.2 假设

- **H1：** 消息级 👍/👎 比“跳转 GitHub Issues”获得更高的反馈覆盖率。验证：上线两周后统计 `feedback_coverage`，并与 0 基线比较；若两周内少于 5 条反馈，说明入口或样本不足，不扩大投入。
- **H2：** 失败回答的“报告问题”能捕获一部分原本完全丢失的失败案例。验证：统计 `message_status=FAILED` 的反馈数量及原因；与同期 `agent_message` 失败数对照。
- **H3：** 保存 model、status、error_code 和 message_version 后，反馈可以支持按模型/阶段/证据命中分组。验证：用 SQL 输出分组表，检查能否区分 `deepseek-flash`、失败阶段和无证据回答。
- **H4：** 独立表 + 批量读取不会让消息列表或发送链路出现可感知延迟。验证：反馈写入 p95、消息列表 p95，以及聊天首包/总耗时对照。

## 2.3 指标与验收标准

| 指标 | 定义与统计口径 | 基线 | 目标/允许范围 | 测量方法与条件 | 证据入口 |
| --- | --- | --- | --- | --- | --- |
| 反馈覆盖率 | 有反馈的 terminal assistant 消息数 / terminal assistant 消息数 | 0（无机制） | 试点两周 ≥5%，且至少 5 条反馈 | MySQL 按日统计；排除测试数据 | 本文 §4、§6 |
| 反馈写入成功率 | 2xx feedback 写入 / feedback 写入请求 | 无 | ≥99% | API 日志与测试；本地/试点 | API 日志、测试记录 |
| 反馈写入 p95 | PUT/DELETE 服务端耗时 | 无 | <300 ms（本地 MySQL 基线） | API `duration_ms`，排除客户端网络 | API 日志 |
| 失败反馈数 | `message_status=FAILED` 的反馈数 | 0 | 试点期 ≥1 条真实失败报告 | MySQL 统计 | 本文 §4 |
| 正向率 | `UP / (UP + DOWN)` | 无 | 仅作诊断，样本 <30 时不设结论 | MySQL 分组 | 本文 §4 |
| 负向原因 Top N | 按 `reasons` 展开计数 | 无 | 能列出前 3 个可行动原因 | MySQL JSON 展开或应用聚合 | 本文 §4 |
| 聊天发送退化 | 反馈上线前后 chat TTFB / 总耗时 | 11-14 p50 451 ms、p90 41.5 s | 不因反馈写入增加可感知延迟 | 同期 API 日志对照；不把供应商波动归因于本功能 | 本文 §4 |
| 数据一致性 | 删除 Conversation 后反馈残留数 | 无 | 0 | 集成测试 + SQL 检查 | 测试记录 |

指标解释：

- 反馈覆盖率低不一定代表功能失败，也可能是当前流量太小；因此把“至少 5 条反馈”与覆盖率一起看，避免用百分比掩盖小样本。
- 正向率不作为“满意度”或产品成功指标；它只用于发现异常和辅助 triage。
- 延迟对照必须使用同一时段、同一模型和同一部署条件；供应商侧波动不能归因于反馈功能。

# 3. 改造方案与建模

## 3.1 已有基础与参考

- `AgentMessage` 已有 `id`、`conversation_id`、`version`、`status`、`model`、`last_error_code` 和证据投影，可直接作为反馈的权威关联对象。
- `AgentChatService` 已有会话归属、消息终态和统一 clock，可复用校验与时间来源。
- 前端 `MessageRow` 已有 assistant 气泡、失败文案和 `message.version`，可在不改变消息协议的情况下追加组件。
- 参考实现：
  - LibreChat 的 per-message feedback 使用 `rating`、`tag`、`text`，校验 rating/tag 方向并支持清除；
  - Codex `/feedback` 把分类、可选原因、会话 ID、是否带日志和标签拆开，且日志需要显式同意；
  - Open WebUI 在管理侧展示 rating/reason/comment，并保留反馈快照；
  - Langfuse / LangSmith 的 feedback 是观测或评测层的 score，不是产品消息存储。

本方案借鉴“per-message、二分类、原因码、可撤销”的形态，但不引入 Langfuse/LangSmith SDK，也不上传对话正文或模型日志。

## 3.2 方案取舍

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| GitHub Issues 外链 | 零后端、已有入口 | 需要 GitHub 账号、无消息关联、无法聚合、公开泄露风险 | 保留为补充渠道，不作为主机制 |
| 写入 `agent_message.detail` | 无需新表 | 与 renderBlocks/timing 共用 JSON，并发更新可能互相覆盖；难索引/聚合 | 不采用 |
| 在 `agent_message` 加多列 | 查询简单 | 只支持单评价者；热表加列；未来共享会话受限 | 不采用 |
| 独立 `agent_message_feedback` 表 | 归属清晰、可 upsert、可索引、可级联删除 | 多一次批量查询和迁移 | 采用 |
| Langfuse/LangSmith score | 复用观测/评测 | 需要实例/账号、trace/run 映射、UI 和代理；自托管重 | 后续可镜像，不作主存储 |

选择独立表的原因不是“表更漂亮”，而是反馈需要成为可查询的产品事实，并且要支持未来共享会话下的多用户评价。当前会话是私有的，但 `(message_id, user_id)` 的唯一键不会限制后续演进。

## 3.3 数据模型

```text
agent_conversation 1 ─── n agent_message 1 ─── n agent_message_feedback
                                              (message_id, user_id) 唯一
```

`agent_message_feedback` 关键字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message_id` | VARCHAR(36) | 关联 assistant message；复合主键之一 |
| `user_id` | VARCHAR(64) | 反馈用户；复合主键之一 |
| `conversation_id` | VARCHAR(36) | 冗余用于按会话聚合和索引 |
| `tenant_id` | VARCHAR(64) | 冗余用于租户隔离与统计 |
| `rating` | VARCHAR(8) | `UP` / `DOWN` |
| `reasons` | JSON | 稳定原因码数组；UI 翻译 |
| `comment` | VARCHAR(2000) | 可选自由文本；可为空 |
| `message_version` | INT | 反馈时的消息版本，避免重新生成后误用 |
| `model` | VARCHAR(255) | 反馈时的模型快照 |
| `message_status` | VARCHAR(16) | `COMPLETED` / `FAILED` |
| `phase` | VARCHAR(16) | 反馈时的最后阶段快照（来自 `timing.stages`；可为空） |
| `error_code` | VARCHAR(64) | 失败回答的公开错误码快照 |
| `create_time` / `update_time` | BIGINT | 毫秒时间戳，沿用 BaseModel |
| `create_date` / `update_date` | DATETIME(3) | UTC 日期，沿用 BaseModel |

约束：

- 主键 `(message_id, user_id)`，保证一人一消息一条反馈；重复提交走 upsert。
- `message_id` 外键 `ON DELETE CASCADE`；删除 Conversation 时 MySQL 先删 Message，再级联删反馈。
- `conversation_id`、`tenant_id` 不加外键，避免多级联路径；由 Service 校验并写入。
- `rating`、`message_status`、`message_version > 0` 加 CHECK。
- 索引：`(conversation_id, create_time)`、`(rating, create_time)`、`(user_id, create_time)`。

## 3.4 API

```http
PUT /api/agent/messages/feedback?conversationId=...&messageId=...&tenantId=...
Content-Type: application/json

{
  "rating": "DOWN",
  "reasons": ["WRONG_EVIDENCE"],
  "comment": "片段不是我要的那一段"
}
```

响应 `200`：

```json
{
  "rating": "DOWN",
  "reasons": ["WRONG_EVIDENCE"],
  "comment": "片段不是我要的那一段",
  "createdAt": 1763...,
  "updatedAt": 1763...
}
```

```http
DELETE /api/agent/messages/feedback?conversationId=...&messageId=...&tenantId=...
```

响应 `204`，无正文。

`AgentMessageView` 增加：

```json
"feedback": {
  "rating": "UP",
  "reasons": [],
  "comment": null,
  "createdAt": 1763...,
  "updatedAt": 1763...
}
```

未评价时为 `null`。列表、详情、发送响应和 SSE 快照共用该投影。

校验与错误：

| 情况 | 状态 | 行为 |
| --- | --- | --- |
| 会话不存在 / 不属于当前用户 | 404 | 不泄露其他会话是否存在 |
| 消息不属于该会话 | 404 | `AgentMessageNotFoundError` |
| 消息不是 ASSISTANT | 400 | 只允许回答 |
| 消息仍在 PROCESSING | 409 | 终态后才能反馈 |
| `UP` 携带 `reasons` | 400 | 避免方向矛盾 |
| `FAILED` 回答给 `UP` | 400 | 失败回答不能正向评价 |
| `COMPLETED` 回答使用 `FAILURE` | 400 | 失败原因只用于失败消息 |
| `reasons` 超过 3 个 / 未知 code | 422 或 400 | DTO/Service 校验 |
| `comment` 超过 2000 字符 | 422 | DTO 长度约束 |
| 并发重复 PUT | 200 | 唯一键 + upsert，返回最终状态 |

## 3.5 交互

- `PROCESSING`：不显示反馈控件。
- `COMPLETED`：回答底部显示 👍 / 👎。
  - 👍 直接提交 `UP`；再次点击已选中的 👍 清除反馈。
  - 👎 打开原因浮层；多选原因 + 可选说明；提交 `DOWN`；已评价时预填并允许修改/清除。
- `FAILED`：显示“报告这个问题”；默认 `DOWN` + `FAILURE`，自动带 `error_code`；允许补充说明。
- 提交后乐观更新；失败回滚并显示错误。
- 无障碍：按钮 `aria-pressed`、明确 `aria-label`；浮层可键盘关闭；错误 `role="alert"`。

原因码先固定为：

| code | 中文 |
| --- | --- |
| `IRRELEVANT` | 答非所问 |
| `WRONG_EVIDENCE` | 片段或出处不对 |
| `MISSING_RESULT` | 没找到想要的内容 |
| `INCOMPLETE` | 回答不完整/太笼统 |
| `TOO_SLOW` | 太慢 |
| `PLAYBACK_ISSUE` | 视频/播放有问题 |
| `OTHER` | 其他 |
| `FAILURE` | 回答失败（仅 FAILED） |

数据库只存 code，前端按 locale 翻译。

## 3.6 关键路径与失败路径

写入路径：

```text
UI 点击
  → PUT feedback
    → get_identity / CSRF
    → require_tenant_read
    → AgentChatService 校验枚举、方向、comment
    → Repository 锁 conversation 行
    → 校验 message 属于该 conversation、ASSISTANT、terminal
    → upsert feedback（message_version/model/status/error_code 快照）
    → 返回 view
  → UI 更新本地 feedback
```

失败路径：

- 会话被删除或消息被级联删除：返回 404；
- 消息仍在生成：返回 409，UI 隐藏控件，正常不会触发；
- 数据库写入失败：UI 回滚到上一状态并保留用户输入，允许重试；
- 并发修改：唯一键约束保证只有一条；upsert 返回最后写入。

读取路径：

```text
list/get/SSE
  → Repository 批量查当前用户对该页消息的 feedback
  → _message_state 注入
  → AgentMessageView.feedback
```

## 3.7 实施边界与兼容

- 新表由 `Base.metadata.create_all` 在空库/缺表时创建；生产库另附 `docs/migrations/20261115_agent_message_feedback.sql`，避免依赖运行时 DDL 权限。
- 现有 `agent_message`、`agent_conversation` 不变；旧消息 feedback 为 `null`。
- API 只增加可选字段和新路由；旧客户端忽略 `feedback`。
- 部署顺序：迁移/建表 → 后端 → 前端；前端先上时，旧后端没有路由会返回 404，UI 应显示可恢复错误而不是破坏消息。
- 删除 Conversation 时反馈随 Message 级联删除；若未来需要长期评测集，另做脱敏归档，不在本次范围。
- 不改变发送、SSE、证据和错误语义。

## 3.8 验证顺序

1. 单元/集成：表创建、upsert/clear、归属、终态、方向校验、级联删除。
2. API：PUT/DELETE 的状态码、响应字段、消息列表/详情回读。
3. 前端：👍/👎、原因提交、失败报告、已反馈回显、错误回滚。
4. 回归：现有 Agent API 测试、消息/SSE 测试、Web 类型检查与测试。
5. 试点：真实用户反馈覆盖率和写入延迟；未达到前不宣称效果达标。

# 4. 对照实验与结果分析

本次是低风险产品功能，不做模型/算法 A/B；采用“上线前 0 基线 + 上线后两周观察”的对照。

## 4.1 比较对象与可比条件

- 基线：2026-11-14 当日及之前 7 天，无 message feedback；终端 assistant 消息 18 条/日、86 条/7 天，失败 1/18、8/86。
- 方案：同一部署、同一模型集合、同一 Chat 入口，增加反馈表/接口/UI。
- 可比性限制：流量小、模型供应商波动、用户组成变化；反馈覆盖率受入口曝光和用户意愿影响，不能把变化全部归因于组件。

## 4.2 实验记录（待测）

| 指标 | 基线实测 | 方案实测 | 变化量 | 验收标准 | 判断与证据 |
| --- | --- | --- | --- | --- | --- |
| 反馈覆盖率 | 0 | 未测（需真实流量） | 待计算 | 两周 ≥5% 且 ≥5 条 | 试点待测 |
| 反馈写入成功率 | 无 | 定向 API 测试全部通过；生产未测 | 待计算 | ≥99% | 未验证 |
| 反馈写入 p95 | 无 | 未测 | 待计算 | <300 ms | 未验证 |
| 失败反馈数 | 0 | 合成失败消息可报告；真实未测 | 待计算 | ≥1 条真实失败报告 | 部分验证 |
| 聊天发送延迟 | 11-14 p50 451 ms / p90 41.5 s | 未测 | 待计算 | 无因本功能产生的可感知退化 | 未验证 |
| 删除会话反馈残留 | 无 | SQLite 外键级联测试为 0 | 0 | 0 | 本地已验证 |

本地定向验证记录（2026-11-15）：

| 验证项 | 结果 | 证据/限制 |
| --- | --- | --- |
| 后端反馈 API/仓储 | 4 passed | `tests/server/test_agent_feedback.py`；含 upsert、清除、PROCESSING/非 assistant/外部 owner、失败方向、级联 |
| Agent API 回归 | 139 passed | `tests/server/test_api.py`；新增反馈路径与 query 参数断言 |
| 数据库 schema 回归 | 143 passed | `tests/server/test_database.py`；表数量与 BaseModel 审计列 |
| 前端单测 | 682 passed | `npm --prefix web test -- --run` |
| 前端类型检查/构建 | 通过 | `npm --prefix web run typecheck`、`npm --prefix web run build` |
| 全量 server 套件 | 3041 passed / 141 skipped / 8 failed | 运行后修正 1 项本功能表数量断言；剩余 7 项为既有 Refine/VideoParse 失败，与反馈改动无关 |

## 4.3 结论与行动

初稿阶段不作效果结论。若试点覆盖率过低，先检查入口曝光、按钮文案和失败路径，不直接判定“用户不需要反馈”；若写入延迟或错误率超出护栏，优先回退前端入口或修复事务，不扩大试点。

# 5. 试点验证与方案取舍

## 5.1 试点范围

- 先在本机/测试环境和内部账号验证完整链路；
- 上线后观察 2 周，覆盖真实 Chat 流量；
- 观察窗口内不自动创建 GitHub Issue，不向外部观测平台发送反馈正文。

## 5.2 监测与回退

- 每日 SQL：反馈数、UP/DOWN、原因分布、失败反馈数、写入错误；
- API 日志：`PUT/DELETE /api/agent/messages/feedback` 的状态码和耗时；
- 回退：前端隐藏入口即可停止新增反馈；表和接口保留，已写入数据不删除；若表/事务异常，回退后端并保留迁移（新增表不影响旧代码）。

## 5.3 取舍

- 收益：获得消息级质量信号，失败回答有低成本报告入口，反馈可关联模型/阶段/证据。
- 代价：一张新表、两个接口、一个组件、迁移与测试；消息读取多一次批量查询；需要处理隐私和公开评论。
- 不扩大范围：本次不做管理后台、自动 triage、评测集导出；这些依赖试点数据证明有使用价值。

# 6. 结果验收与闭环

## 6.1 当前状态

- 设计：初稿完成，已按实现结果回填；
- 实现：表、Repository、Service、API、前端组件和迁移已完成；
- 验证：定向后端/前端测试、类型检查和生产构建通过；全量 server 仍有既有视频解析失败；
- 部署：未部署，未对真实数据库执行迁移；
- 推广：未开始，真实反馈覆盖率和延迟待试点；
- 提交：未 commit、未 push（当前任务未获得该授权），PR 待创建。

## 6.2 验收清单

- [x] 新表由 `create_schema` 在空库创建；迁移 SQL 已提供，真实已有库未执行；
- [x] PUT 可新增、修改；DELETE 可清除；重复 PUT 只有一行；
- [x] 列表/详情/发送响应/SSE 投影包含当前用户 feedback；
- [x] 非所有者、非 assistant、PROCESSING、方向矛盾、FAILURE 误用均被拒绝；
- [x] 删除 Conversation 后反馈级联删除（SQLite 外键测试）；
- [x] 前端 👍/👎、原因、comment、失败报告、回显、回滚可用；
- [x] 后端定向测试、前端类型检查/组件测试、生产构建通过；
- [ ] 真实库迁移与生产部署验证；
- [ ] 试点达到覆盖率/成功率/延迟门槛，或明确记录未达标原因。

## 6.3 剩余事项

- 按任务授权创建 commit / PR，并把 TRD 作为首个提交；
- 在目标 MySQL 备份后执行 `docs/migrations/20261115_agent_message_feedback.sql`，再部署后端和前端；
- 真实流量试点与数据回收；
- 管理侧 triage 查询/导出（待试点后决定）；
- 是否镜像到 Langfuse/LangSmith（本次不引入）；
- `/docs` 社区占位符替换（独立小修复，不阻塞本功能）；
- PR 创建后回填链接与工作台状态。
