# AniVAG Video Parse V2：当前重构总结与接入清单

更新日期：2026-09-27。本文记录**当前已落地的代码与隔离 MySQL 空库状态**，供后续接入者继续开发。它不是新的架构提案，也不代表 Video Parse V2 已经能够执行完整解析。`design/` 被 Git 忽略；阅读本文及两份 TRD 时须直接打开文件，不以 `git diff` 或默认搜索结果判断文件是否存在。

目标行为以用户最新明确约定、[Video Parse V2 TRD](trd.md) 和 [Engine Parse V2 TRD](../../../engine/parsev2/design/trd.md) 为准。[批次计划与实施记录](../../../docs/design/refactor_2.md) 记录批次 0～2 的原始验收；涉及文档与源码差异时，以实际代码和本次实测为准。[Harness README](../README.md) 是当前编排框架的入口说明，[MySQL 逐表实测报告](../../../docs/mysql_v2_schema_20260927.md) 是隔离库的字段、约束与关系明细。

## 一、开发前置背景

旧链路是上传完成后创建 `series_parse_job`，由 V1 Worker 调用 `engine.parsev1`，使用连续前缀 checkpoint 和整集导入。V2 的身份和成果粒度不同：一次 Video 解析对应一个 `video_parse_task`，其下有可独立执行的 Chunk/Interval 任务及成果。旧 Job 不能直接改名或把 checkpoint 当作 V2 子任务。

用户已经明确：`episode_order` 是实际业务输入且在同 Series 内唯一；Interval 用最左侧 Chunk 任务作锚点，有序成员固定，范围左闭右开；compact 成果放在 `chunk_parse_content.detail`；系统绝对时间使用 `TIMESTAMP` 和内部 UTC `datetime`，视频内部时间为秒；失败重试复用任务 ID，主动重解析创建新任务和解析版本，不删除历史任务或成果。`video_parse_task.version` 仅表示同一 Video 的第几次解析；当前没有 `revision` 字段，也不能拿解析版本冒充 UI/SSE 的更新序号。

另一次表结构审核确认：Agent 单消息耗时不需要独立 `agent_message_timing` 表；USER 发送幂等键属于该消息的请求明细，应存入 `agent_message.detail.request.idempotencyKey`。一次 Agent 回答可以有多次检索，不应保留一个误导性的 `retrieval_query`/公开顶层 `query`；USER/ASSISTANT 固定消息对可由同一会话内的相邻序号及角色找回，不必保存 `reply_to_message_id` 自外键。这些 Agent 表调整与 Video Parse V2 Worker 接线是不同工作流，不能互相充当完成证明。

## 二、已调查的事实与差异

| 调查入口 | 已确认事实及影响 |
| --- | --- |
| [上传 API](../../../server/api/uploads.py) → [service](../../../server/service/upload.py) → [repository](../../../server/repository/upload.py) | 创建会话、分片、完成、重试和重解析沿用现有事务边界；上传完成可在同一事务中关联 V1 Job 与 V2 Task。 |
| [V2 模型](../../../server/model/job.py)、[上传模型](../../../server/model/upload.py)、[建表门禁](../../../server/database.py) | V2 已有五张任务/成果表及任务版本、外键、租约字段；`create_all()` 不会替已有库做列迁移，旧 Schema 有显式升级门禁。 |
| [Harness 入口](../agent.py)、[Prepare](../video_parse_prepare.py)、[Scheduler](../chunk_scheduler.py)、[Listener](../chunk_interval_listener.py) | ParseAgent 主图、Prepare 单节点、Chunk 子图及共享 Listener 已有可测试的编排骨架；默认执行在 `_load_input` 等业务步骤抛 `NotImplementedError`。 |
| [Engine Schema](../../../engine/parsev2/schemas.py) 与 Engine TRD 第一章 | 当前 `VideoParseResult` 已是 `persons: string[]` 与稀疏 `timeline`，人物字段直接存人名，时间为整数秒半开区间；Engine 拥有结构与领域校验。历史进度记录曾称“仍使用人物 ID”，现以源码为准；剩余问题是模型、存储与 Worker 接线。 |
| [旧 Worker 装配](../../../server/bootstrap.py)、[旧队列](../../../server/mq/mysql_queue.py) 与前端任务页 | 当前 Worker 仍执行 V1 runtime；V2 `PENDING` 不会由它自动执行。旧任务页还把 V1 Job 更新版本传给 V2 retry/reparse 接口，并按 V1 Job 响应消费。 |
| [Agent 消息模型](../../../server/model/agent.py)、[Repository](../../../server/repository/agent.py)、[Agent API 设计](../../../docs/design/agent-api.md) | `detail.timing` 和 `detail.request.idempotencyKey` 已取代耗时附表与 USER 的独立幂等列；配对依赖消息顺序，检索轨迹保留每次实际 query。会话级 `agent_conversation.idempotency_key` 仍是独立列。 |
| 本地 `../codex` 的 `thread_turns`/`thread_items` 与上下文拼接实现 | 对照实现按有序 turn/item 保存上下文，不依赖每条 assistant 消息的 `reply_to_message_id`；工具调用使用 `call_id`。这是对照证据，不把 Codex 的存储结构照搬到 AniVAG。 |
| [隔离 MySQL 实测报告](../../../docs/mysql_v2_schema_20260927.md) | `127.0.0.1:33307` 的开发库与测试库各 23 表、333 字段，逐表精确行数为 0；旧默认 MySQL 数据目录未动。V1 专用表仍保留原名。 |

原 TRD 的个别字段/时间写法和历史交接文件早于上述明确约定，不能直接当作当前 DDL。尤其 `video_parse_task` 没有 `revision`；`interval_parse_content` 当前没有 `version`；新空库中的旧 V1 表还未加 `_v1` 后缀。检索/embedding 的其它并行代码变更也不算本批 V2 解析任务接入。

## 三、已 refactor 的文件树

以下是**本主题的主要生产文件与文档**，`M/A` 表示当前相对于仓库基线的修改/新增。它不是整个工作区的 `git status` 清单；其它并行的 ES、检索和评测改动不计入这里。被 Git 忽略的设计文档单独标注。

```text
AniVAG/
├── agent/
│   ├── chat/
│   │   └── video_search/
│   │       ├── [M] context.py             Agent 历史证据投影
│   │       ├── [M] output.py              多次检索轨迹输出
│   │       └── [M] state.py               无顶层 query 的最终证据合同
│   └── video_parse_v2/
│       ├── [M] README.md                  框架导航与未实现范围
│       ├── [M] __init__.py                ParseAgent 公开入口
│       ├── [A] agent.py                   主图
│       ├── [A] input.py                   固定输入配置
│       ├── [A] state.py                   主图瞬时状态
│       ├── [A] errors.py                  错误合同
│       ├── [A] video_parse_prepare.py     Prepare 步骤
│       ├── [A] chunk_scheduler.py         调度步骤
│       ├── [A] chunk_interval_listener.py Listener 回调
│       ├── chunk_understanding/
│       │   ├── [A] __init__.py
│       │   ├── [A] state.py
│       │   └── [A] workflow.py            Chunk 子图
│       ├── refine/
│       │   ├── [A] __init__.py
│       │   ├── [A] input.py
│       │   └── [A] interval_refine.py
│       ├── precompact/
│       │   ├── [A] __init__.py
│       │   ├── [A] context.py
│       │   ├── [A] tools.py
│       │   └── [A] prefix_context_agent.py
│       └── design/
│           └── [A, ignored] refactor_1.md  本总结
├── engine/
│   ├── parsev2/
│   │   └── [M] schemas.py                 V2 领域结果和校验
│   └── retrieval/
│       └── [M] contracts.py               Agent 证据与单次 Search 分型
├── server/
│   ├── [M] database.py                    Schema 门禁
│   ├── api/
│   │   ├── [M] uploads.py
│   │   ├── [M] agent.py
│   │   ├── [M] search.py
│   │   └── [M] schema.py
│   ├── config/
│   │   └── [M] parse_profile.py
│   ├── model/
│   │   ├── [M] job.py                     V1/V2 任务与成果
│   │   ├── [M] video.py                   Video 与 Chunk 拓扑
│   │   ├── [M] upload.py                  上传快照/关联
│   │   ├── [M] parse_usage.py             V2 任务费用关联
│   │   ├── [M] agent.py                   消息 detail 与唯一索引
│   │   ├── dto/
│   │   │   ├── [M] jobDTO.py
│   │   │   ├── [M] uploadDTO.py
│   │   │   ├── [M] parseUsageDTO.py
│   │   │   └── [M] agentDTO.py
│   │   └── vo/
│   │       ├── [M] jobVO.py
│   │       ├── [M] uploadVO.py
│   │       ├── [M] agentVO.py
│   │       └── [M] searchVO.py
│   ├── repository/
│   │   ├── [M] upload.py                  完成、retry、reparse 事务
│   │   ├── [M] job.py                     V2 Task 读取
│   │   ├── [M] library.py                 集序/时间兼容
│   │   ├── [M] parse_usage.py             费用归属
│   │   └── [M] agent.py                   JSON 幂等键与消息配对
│   ├── service/
│   │   ├── [M] upload.py
│   │   ├── [M] parse_usage.py
│   │   └── [M] agent_chat.py
│   ├── router/
│   │   └── [M] uploads.py
│   ├── mq/
│   │   └── [M] mysql_queue.py            旧队列集序/时间兼容
│   └── utils/
│       └── [M] time.py                   UTC 时间边界
├── docs/
│   ├── design/
│   │   ├── [M] agent-api.md
│   │   └── [M, ignored] refactor_2.md    批次计划与进度
│   ├── migrations/
│   │   ├── [A] 20260927_video_parse_v2_tasks.sql
│   │   └── [A] 20260927_agent_message_detail_request.sql
│   └── [A] mysql_v2_schema_20260927.md
├── tests/
│   ├── agent/
│   │   └── video_parse_v2/
│   │       ├── [A] test_framework.py
│   │       └── [A] test_input.py
│   ├── engine/
│   │   └── parsev2/
│   │       └── [M] test_schemas.py
│   └── server/
│       ├── [M] test_upload_service.py
│       ├── [M] test_job_repository.py
│       ├── [M] test_mysql_integration.py
│       ├── [M] test_database.py
│       ├── [M] test_agent_chat_service.py
│       ├── [M] test_api.py
│       └── [M] test_sse_events.py
└── web/
    └── src/
        ├── api/
        │   └── [M] contracts.ts
        └── features/search/
            ├── [M] SearchPage.test.tsx
            └── [M] AgentProgress.test.tsx
```

树中 Harness 文件是编排框架，不代表其中的 `NotImplementedError` 已被填完。`20260927_video_parse_v2_tasks.sql` 是**已有库**的受控升级草案，未在旧库执行；两套隔离空库先由当前 ORM 建表，再针对空 `agent_message` 应用了请求明细迁移。目标文件和 `refactor_2.md` 均被 `**/design/` 忽略，需要显式保留/交付。

## 四、综合起来已经实现的功能

1. **上传到 V2 任务身份。** `episode_order` 从 DTO 经 API、service 到 repository 生效，校验正整数和同 Series 冲突，同次上传重放复用确认值。上传完成原子创建 Video、V1 Job、一个 V2 `PENDING` Task 和上传关联；重复完成不重复建 V2 Task。V1 Job 保留，使现有 V1 Worker 路径仍可继续处理初次上传。
2. **配置快照与版本。** 创建上传会话时规范化并保存 V2 有效配置；Repository 已能把 Task、Video 与固定快照一起读出，创建时遇到快照缺失或与既有 Chunk 拓扑不兼容会报错。后续 Worker 恢复还需接入该读取能力。按视频时长与 `chunk_duration` 得到正整数 `chunk_count`。失败 retry 沿用同一 V2 Task ID、解析版本与成果；主动 reparse 创建新 ID 和递增解析版本，旧任务及成果保留。当前过渡参数 `expectedJobVersion` 实际比较 V2 **解析版本**。
3. **V2 任务/成果 Schema。** 已建立主任务、Chunk 任务与成果、Interval 任务与成果五张表。主任务有执行领取/租约字段；Chunk 内容一任务一行并有内容版本；Interval 有最左成员锚点、同父任务复合外键、有序成员 JSON、连续范围和 ready-mask 校验。成员列表中非锚点 ID 的归属/连续性仍由应用层校验。compact 的预定保存位置为 `chunk_parse_content.detail`，后台游标与租约尚未落地。
4. **时间与对外投影。** 本批触及的绝对时间列采用 `TIMESTAMP(3)` 和 UTC `datetime`，VO 前转 Unix 毫秒；视频内部区间仍用秒，duration 保持数值。没有改公共时间基类来隐式迁移所有旧业务表。
5. **Engine 与 Harness 基础。** Engine V2 Schema 已实现直接人名、事件联合类型、整数秒半开时间、多语言对白和确定性校验。Harness 已有 ParseAgent、Prepare、Chunk 子图、Scheduler、共享 Listener、Refine 和 PrefixContext 的调用骨架与输入配置验证；这些组件尚未连接真实 DB 租约、模型、媒体与提交。
6. **Agent 消息表收敛。** Assistant 耗时进入 `detail.timing`，USER 幂等键进入 `detail.request.idempotencyKey`；同会话 JSON 表达式唯一索引保留物理去重，重复发送可找回原消息对。配对使用 `(conversation_id, sequence_no)` 的相邻序号与角色；删除了 `reply_to_message_id` 自外键和单值 `retrieval_query`。Agent 公开 `searchResult` 不再给一个伪“最终 query”，保留 `retrievalTrace[].query`；独立 `/api/search` 仍保留其单次 query 合同。
7. **隔离 MySQL 空库。** 开发库 `anivag_video_parse_v2_development` 与测试库 `anivag_video_parse_v2_refactor_test` 在独立 MySQL 8.4、端口 33307 上实测各 23 表、333 字段，`agent_message` 为 22 列，无旧三列/回复自外键，JSON 唯一表达式索引存在。两库逐表行数为 0；原有库及其数据目录未动。完整字段/外键见实测报告，不能以 SQLite 建表代替这项验证。

本轮 Agent 消息与结果链的定向验证：真实 MySQL 的 `agent_message_detail_request`、V2 retry、V2 reparse 三类集成用例 `3 passed`；Agent 结果链后端定向集合 `310 passed, 1 skipped`；前端类型检查通过，相关前端测试 `76 passed`；数据库 Schema 门禁测试 `36 passed`。两套隔离库已再次逐表计数，总行数各为 0。真实 MySQL 定向命令如下：

```bash
env ANIVAG_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33307/anivag_video_parse_v2_refactor_test?charset=utf8mb4' \
  .venv/bin/pytest -q tests/server/test_mysql_integration.py \
  -k 'agent_message_detail_request or manual_parse_retry or reparse_allocates'

.venv/bin/pytest -q tests/agent/chat tests/server/test_api.py \
  tests/server/test_agent_chat_service.py tests/server/test_sse_events.py \
  tests/server/test_search_lexical_contract.py tests/engine/test_video_retrieval.py

# 以下两条在 web/ 目录运行：
npm run typecheck
npm test -- src/features/search/AgentProgress.test.tsx \
  src/features/search/SearchPage.test.tsx \
  src/features/search/useAgentConversation.test.ts src/app/App.test.tsx
```

此前批次 0～2 有服务端测试 `769 passed, 3 skipped`、Harness 框架测试 `28 passed`，以及 V2 上传/重试/重解析的真实 MySQL 集成测试 `9 passed`；这些是此前轮次的记录，不能当作当前所有代码的新全量结果。上述通过项均不证明 V2 Worker、模型调用或历史库升级。

## 五、TODO 与待实现功能

| 优先级/批次 | 待做事项 | 当前接入点与验收边界 |
| --- | --- | --- |
| 近期：旧表风格收敛 | 用户要求保留 V1 专用表但表名明确带 `_v1`；当前 `series_parse_job`、`video_chunk_checkpoint` 等仍是旧名。`agent_message` 的 `requested_top_k`、`requested_run_options`、`requested_series_ids`、`last_error`、`last_error_code`、`last_error_message` 仍为独立列；请求字段去留与错误统一为 `last_error JSON` 还未完成。 | 先核对每列生产者、消费者、外键、索引与已有数据，再给可审阅修改树及空库迁移；不能因为空库可重建就直接修改有数据的旧库。会话级幂等键本轮仍保持独立列。 |
| 批次 3：真正执行 | 复用既有队列和 Consumer 领取 V2 主任务，完成 Prepare 的固定拓扑和子任务幂等初始化、租约 fencing、Chunk 并发调度、Listener mask 与提交后通知、Interval Refine 和主任务完成判定。 | [Worker/队列](../../../server/mq/mysql_queue.py)、[装配](../../../server/bootstrap.py)、[Harness](../agent.py)；真实 DB 事务、失败恢复、并发和模型费用必须验证。当前 V2 `PENDING` 不会自行跑。 |
| 批次 3：结果提交 | 将模型和媒体证据接到 Engine Schema，由 Engine 校验；按任务/租约原子保存 Chunk、Interval 内容及版本，避免在 Harness 中硬编码视频结果字段。 | [Chunk workflow](../chunk_understanding/workflow.py)、[Engine Schema](../../../engine/parsev2/schemas.py)、持久化层；模型调用应在事务外，提交时校验租约及输入版本。 |
| 批次 4：后台 compact/费用 | 建立独立后台游标与租约，在 `chunk_parse_content.detail` 保存 compact，接 PrefixContext 微调和费用归属；失败不能误判主解析失败。 | [PrefixContextAgent](../precompact/prefix_context_agent.py)、usage repository/service；后台执行元数据不与 compact 内容混放。 |
| 批次 5：查询与前端 | `/api/jobs`、SSE、Library 和旧任务页从 V1 Job 过渡到 V2 Task；定义独立 UI/SSE 更新令牌，解决延迟 retry 的 ABA。集序自动识别与人工修正入口也未实现。 | 旧前端传入的 V1 Job `version` 不等于 V2 解析版本，当前按钮可能 409 或误读响应；切换时保留历史成功成果的可见性。 |
| 批次 6：已有数据升级/旧链清理 | 在旧库副本验证时间列、Agent JSON 回填、V1 表改名和消费者切换；决定旧成果过渡读取与最终清理。 | 两套新空库的 DDL 成功不等于旧库升级成功；不伪造 V1→V2 COMPLETE 成果，也不在仍有消费者时删旧列。 |

本总结的完成点是**任务模型、上传创建链、Agent 消息收敛与隔离空库实测**。下一个使 V2 真正“能跑”的最小闭环是 Worker 领取一个现有 `video_parse_task_id`，Prepare 恢复固定配置并建子任务，Chunk 经 Engine 校验后在租约保护下提交成果，最后正确结束主任务。
