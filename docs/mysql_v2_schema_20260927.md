# AniVAG Video Parse V2 MySQL 实际 Schema 报告

生成日期：2026-09-27。数据源：MySQL `information_schema` 与对每张业务表执行的只读 `COUNT(*)`。

本报告记录独立开发实例中的实际数据库结构，不以 SQLAlchemy ORM 或迁移脚本反推。结构与行数采集均为只读；采集前已对两套隔离空库执行 Agent Message 请求明细迁移，旧库未动。

## 实例与使用边界

- Schema：`anivag_video_parse_v2_development`
- MySQL：`8.4.11`；本次直连会话 `time_zone=SYSTEM`，全局 `time_zone=SYSTEM`。
- Schema 默认字符集/排序规则：`utf8mb4` / `utf8mb4_0900_ai_ci`。
- 实例只绑定 loopback，端口为 `127.0.0.1:33307`，独立于项目通常使用的 `3306`。
- 实际 datadir 为 `outputs/mysql-v2-refactor-20260927/`，独立于默认实例使用的 `outputs/local-services/mysql`。
- 本实例由独立 `mysqld` 进程运行；`start.sh` 的默认 MySQL 数据目录仍是 `outputs/local-services/mysql`，不能用它代替这套实例的重启方式。
- 这是本地临时隔离实例，未从旧库复制任何数据；23 张表逐表 `COUNT(*)` 均为 0。
- 同实例测试库 `anivag_video_parse_v2_refactor_test` 也有相同的 23 张表、333 个字段；测试数据已清理，逐表 `COUNT(*)` 均为 0。
- 应用连接该实例时需显式设置：

```bash
export ANIVAG_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33307/anivag_video_parse_v2_development?charset=utf8mb4'
```

该连接示例不包含密码，只适用于这套本地 loopback 临时实例。应用通过自身数据库引擎连接时会设置 UTC session time zone；上面记录的 `SYSTEM` 是本报告原始只读连接的实测值。

本机重启后如需恢复这套独立实例，先在仓库根目录确认 33307 未监听，再使用原数据目录启动：

```bash
/opt/homebrew/bin/mysqld --no-defaults \
  --datadir="$PWD/outputs/mysql-v2-refactor-20260927" \
  --bind-address=127.0.0.1 --port=33307 \
  --socket=/private/tmp/anivag-v2-33307.sock \
  --pid-file="$PWD/outputs/mysql-v2-refactor-20260927/mysql.pid" \
  --log-error="$PWD/outputs/mysql-v2-refactor-20260927/mysql.err" \
  --mysqlx=OFF --daemonize
```

## 总体统计

| 项目 | 实测数量 |
| --- | ---: |
| 表 | 23 |
| 字段 | 333 |
| 可空字段 | 96 |
| `TIMESTAMP` 字段 | 47 |
| `BIGINT` 字段 | 40 |
| `JSON` 字段 | 41 |
| 主键约束 | 23 |
| 唯一约束 | 18 |
| CHECK 约束 | 76 |
| 外键约束 | 31 |
| 普通非唯一索引 | 34 |
| 具有非 NULL server default 的字段 | 0 |
| `EXTRA` 非空字段 | 0 |
| 逐表精确总行数 | 0 |

所有表的实测引擎均为 InnoDB，排序规则均为 `utf8mb4_0900_ai_ci`。字段表中的 `Default` 是 `information_schema.COLUMNS.COLUMN_DEFAULT` 原值；当前 333 个字段均返回 NULL。`Key` 使用 MySQL 的 `PRI`、`UNI`、`MUL` 标记，完整键定义以下方约束和索引为准。`agent_message` 的 JSON 表达式唯一索引不对应独立字段，因此不会显示在字段表的 `Key` 栏。

## 实测结构摘要

- 五张 V2 任务/成果表均已存在：`video_parse_task`、`chunk_parse_task`、`chunk_parse_content`、`interval_parse_task`、`interval_parse_content`。
- 实际 `video_parse_task` 有 24 个字段；当前库中没有 `revision` 字段，也没有对应 CHECK 约束。本条仅陈述实际反射结果。
- `interval_parse_task` 存在两列复合外键 `(chunk_parse_task_id, video_parse_task_id)`，指向 `chunk_parse_task(id, video_parse_task_id)`，删除规则为 CASCADE。
- `upload_session.initial_video_parse_task_id`、`parse_usage_attempt.video_parse_task_id`、`parse_model_call.video_parse_task_id` 均已存在，删除 V2 主任务时使用 SET NULL。
- Agent Timing 已合入 `agent_message.detail` JSON；USER 发送幂等键存于 `detail.request.idempotencyKey`，同 Conversation 唯一性由 JSON 表达式索引保证。`agent_message` 不再有 `idempotency_key`、`reply_to_message_id`、`retrieval_query` 三列或回复自外键。
- 三张 ES 投影表 `es_index_build`、`es_index_build_chunk`、`es_video_publication` 存在于实际库中。
- 本报告的空库结论来自逐表精确 `COUNT(*)`，不是 InnoDB 的 `TABLE_ROWS` 估算。

## 核心 V2 任务链关系

1. `video` 是解析业务输入；一次主动解析对应一条 `video_parse_task`。后者通过 `video_id` 物理外键归属 Video，并以 `(video_id, version)` 唯一约束区分历史解析版本。
2. `chunk_parse_task` 通过 `video_parse_task_id` 归属主任务，并通过 `video_chunk_id` 指向固定 Chunk 拓扑；其 `(video_parse_task_id, video_chunk_id)` 唯一。
3. `chunk_parse_content` 通过唯一外键 `chunk_parse_task_id` 与 Chunk 任务一对一；成果版本保存在 `version`。
4. `interval_parse_task` 通过 `video_parse_task_id` 归属主任务。复合外键 `(chunk_parse_task_id, video_parse_task_id)` 强制锚点 Chunk 任务属于同一主任务；`(video_parse_task_id, start_sequence_no)` 唯一。
5. `interval_parse_content` 通过主键兼外键 `interval_parse_task_id` 与 Interval 任务一对一；当前表没有 `version` 列。
6. `upload_session.initial_video_parse_task_id` 保存上传完成时创建的初始 V2 任务；用量/调用表可通过可空外键关联 V2 主任务。

`interval_parse_task.chunk_parse_task_ids` 是有序 JSON 成员列表。MySQL 只把锚点 `chunk_parse_task_id` 与父任务组成复合物理外键；JSON 数组内的其余成员不是物理外键，其成员归属、连续性、首项锚点与顺序由应用层领域校验负责。`ready_mask` 的第 i 位对应 JSON 列表第 i 项，区间语义为左闭右开。

## Agent 消息与 Timing 关系

1. `agent_message.conversation_id` 通过物理外键归属 `agent_conversation`；删除会话时级联删除消息。
2. USER 与 ASSISTANT 由同一 Conversation 内相邻的 `sequence_no` 和角色配对；实际库没有回复自外键。
3. USER 的 `detail.request.idempotencyKey` 由 `(conversation_id, JSON 路径表达式)` 唯一索引约束；ASSISTANT 的该键缺省。Timing 明细保存在同一条消息的 `detail.timing`；数据库不另建 Timing 表，JSON 内部结构由应用层合同校验。

## 表目录

| 表 | 字段数 | 精确行数 | 用途 |
| --- | ---: | ---: | --- |
| `agent_conversation` | 9 | 0 | Agent 会话头信息、工作区归属、幂等键与最近消息时间。 |
| `agent_message` | 22 | 0 | Agent 会话中的用户/助手消息、执行阶段、请求幂等键、检索结果及 Timing 明细。 |
| `chunk_parse_content` | 7 | 0 | V2 Chunk 任务的一对一当前成果、内容版本及 detail 扩展。 |
| `chunk_parse_task` | 15 | 0 | V2 单 Chunk 执行状态、调度时间、重试与租约。 |
| `context_snapshot` | 9 | 0 | 旧解析链按 Video Chunk 位置保存的上下文压缩快照。 |
| `es_index_build` | 12 | 0 | 一次 Elasticsearch 投影构建的父记录及合同版本。 |
| `es_index_build_chunk` | 29 | 0 | ES 构建中的逐 Chunk 投影、向量、写入状态与查询证明。 |
| `es_video_publication` | 8 | 0 | Video 当前期望及已发布 ES build 的 fenced 指针。 |
| `index_sync_intent` | 11 | 0 | 旧索引同步 outbox/intention，记录 UPSERT/DELETE 的重试状态。 |
| `interval_parse_content` | 5 | 0 | V2 Interval 任务的一对一区间级成果。 |
| `interval_parse_task` | 18 | 0 | V2 重叠区间的固定成员、ready mask、Refine 状态与租约。 |
| `parse_model_call` | 22 | 0 | 解析模型调用明细、token、费用快照及 V1/V2 任务归属。 |
| `parse_usage_attempt` | 6 | 0 | 按旧 Job attempt/lease 记录费用采集尝试，并可关联 V2 主任务。 |
| `series` | 10 | 0 | Series 业务根实体、工作区归属、状态与人物设定。 |
| `series_parse_job` | 23 | 0 | 保留的 V1 整集解析 Job、租约、进度及错误。 |
| `upload_segment` | 8 | 0 | 上传会话的分片范围、校验和及暂存 URI。 |
| `upload_session` | 31 | 0 | 上传会话、确认集序、配置快照、完成状态及 V1/V2 任务链接。 |
| `video` | 16 | 0 | Video 基础信息、Series 内集序、源媒体及聚合状态。 |
| `video_chunk` | 14 | 0 | 固定 Video Chunk 拓扑、秒级范围、媒体引用和基础识别数据。 |
| `video_chunk_checkpoint` | 13 | 0 | 保留的 V1 Chunk 连续处理 checkpoint。 |
| `video_chunk_embedding` | 8 | 0 | 旧链持久化的 Chunk 文本/视觉 embedding 与元数据。 |
| `video_chunk_info` | 13 | 0 | 旧链持久化的 Chunk 解析详情、compact 与 provenance。 |
| `video_parse_task` | 24 | 0 | V2 单次 Video 解析身份、固定配置、解析版本、状态与主租约。 |

## 逐表明细

### `agent_conversation`

用途：Agent 会话头信息、工作区归属、幂等键与最近消息时间。

实测：9 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `conversation_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `workspace_id` | `varchar(64)` | 否 | `NULL` | `MUL` | — |
| 3 | `title` | `varchar(255)` | 否 | `NULL` | — | — |
| 4 | `title_is_auto` | `tinyint(1)` | 否 | `NULL` | — | — |
| 5 | `idempotency_key` | `varchar(128)` | 否 | `NULL` | — | — |
| 6 | `next_message_sequence` | `int` | 否 | `NULL` | — | — |
| 7 | `last_message_at` | `bigint` | 是 | `NULL` | — | — |
| 8 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 9 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`conversation_id`)
- 唯一 `uq_agent_conversation_workspace_idempotency`：(`workspace_id`, `idempotency_key`)
- CHECK `ck_agent_conversation_next_message_sequence`：`` (`next_message_sequence` >= 0) ``

普通非唯一索引：

- `ix_agent_conversation_workspace_id`：(`workspace_id`)，类型 `BTREE`

### `agent_message`

用途：Agent 会话中的用户/助手消息、执行阶段、检索请求、结果及同一消息内的 Timing 明细。

实测：22 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `message_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `conversation_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `sequence_no` | `int` | 否 | `NULL` | — | — |
| 4 | `role` | `varchar(16)` | 否 | `NULL` | — | — |
| 5 | `status` | `varchar(16)` | 否 | `NULL` | — | — |
| 6 | `phase` | `varchar(16)` | 是 | `NULL` | — | — |
| 7 | `version` | `int` | 否 | `NULL` | — | — |
| 8 | `reference` | `json` | 是 | `NULL` | — | — |
| 9 | `extra_context` | `json` | 是 | `NULL` | — | — |
| 10 | `detail` | `json` | 是 | `NULL` | — | — |
| 11 | `content` | `longtext` | 是 | `NULL` | — | — |
| 12 | `series_ids` | `json` | 否 | `NULL` | — | — |
| 13 | `model_name` | `varchar(255)` | 是 | `NULL` | — | — |
| 14 | `requested_top_k` | `int` | 是 | `NULL` | — | — |
| 15 | `requested_run_options` | `json` | 是 | `NULL` | — | — |
| 16 | `requested_series_ids` | `json` | 是 | `NULL` | — | — |
| 17 | `search_result` | `json` | 是 | `NULL` | — | — |
| 18 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 19 | `last_error_code` | `varchar(64)` | 是 | `NULL` | — | — |
| 20 | `last_error_message` | `longtext` | 是 | `NULL` | — | — |
| 21 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 22 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`message_id`)
- 唯一 `uq_agent_message_conversation_idempotency`：`conversation_id` 加 `detail.request.idempotencyKey` 的 JSON 提取、NULL 归一化及 `CHAR(128)` 转换表达式；第二键不是物理列，完整定义见 [本次 Agent Message 迁移 SQL](migrations/20260927_agent_message_detail_request.sql)
- 唯一 `uq_agent_message_conversation_sequence`：(`conversation_id`, `sequence_no`)
- CHECK `ck_agent_message_phase`：`` ((`phase` is null) or (`phase` in (_utf8mb4'QUEUED',_utf8mb4'RETRIEVING',_utf8mb4'EVALUATING',_utf8mb4'RESPONDING'))) ``
- CHECK `ck_agent_message_requested_top_k`：`` ((`requested_top_k` is null) or (`requested_top_k` between 1 and 100)) ``
- CHECK `ck_agent_message_role`：`` (`role` in (_utf8mb4'USER',_utf8mb4'ASSISTANT')) ``
- CHECK `ck_agent_message_sequence`：`` (`sequence_no` >= 0) ``
- CHECK `ck_agent_message_status`：`` (`status` in (_utf8mb4'PROCESSING',_utf8mb4'COMPLETED',_utf8mb4'FAILED')) ``
- CHECK `ck_agent_message_version`：`` (`version` > 0) ``
- 外键 `agent_message_ibfk_1`：(`conversation_id`) → `agent_conversation`(`conversation_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_agent_message_conversation_id`：(`conversation_id`)，类型 `BTREE`
- `ix_agent_message_conversation_status`：(`conversation_id`, `status`)，类型 `BTREE`

### `chunk_parse_content`

用途：V2 Chunk 任务的一对一当前成果、内容版本及 detail 扩展。

实测：7 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `chunk_parse_task_id` | `varchar(36)` | 否 | `NULL` | `UNI` | — |
| 3 | `content` | `json` | 是 | `NULL` | — | — |
| 4 | `version` | `int` | 否 | `NULL` | — | — |
| 5 | `detail` | `json` | 是 | `NULL` | — | — |
| 6 | `create_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 7 | `update_time` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`id`)
- 唯一 `uq_chunk_parse_content_task`：(`chunk_parse_task_id`)
- CHECK `ck_chunk_parse_content_version`：`` (`version` > 0) ``
- 外键 `chunk_parse_content_ibfk_1`：(`chunk_parse_task_id`) → `chunk_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

### `chunk_parse_task`

用途：V2 单 Chunk 执行状态、调度时间、重试与租约。

实测：15 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `video_parse_task_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 4 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 5 | `next_execute_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 6 | `retry_count` | `int` | 否 | `NULL` | — | — |
| 7 | `lease_owner` | `varchar(128)` | 是 | `NULL` | — | — |
| 8 | `lease_version` | `int` | 否 | `NULL` | — | — |
| 9 | `lease_expire_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 10 | `start_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 11 | `finish_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 12 | `last_error` | `json` | 是 | `NULL` | — | — |
| 13 | `detail` | `json` | 是 | `NULL` | — | — |
| 14 | `create_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 15 | `update_time` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`id`)
- 唯一 `uq_chunk_parse_task_id_parent`：(`id`, `video_parse_task_id`)
- 唯一 `uq_chunk_parse_task_parent_chunk`：(`video_parse_task_id`, `video_chunk_id`)
- CHECK `ck_chunk_parse_task_lease_state`：`` (((`status` = _utf8mb4'UNDERSTANDING') and (`lease_owner` is not null) and (`lease_expire_time` is not null)) or ((`status` <> _utf8mb4'UNDERSTANDING') and (`lease_owner` is null) and (`lease_expire_time` is null))) ``
- CHECK `ck_chunk_parse_task_lease_version`：`` (`lease_version` >= 0) ``
- CHECK `ck_chunk_parse_task_retry_count`：`` (`retry_count` >= 0) ``
- CHECK `ck_chunk_parse_task_status`：`` (`status` in (_utf8mb4'QUEUED',_utf8mb4'UNDERSTANDING',_utf8mb4'COMPLETE',_utf8mb4'FAILED')) ``
- 外键 `chunk_parse_task_ibfk_1`：(`video_parse_task_id`) → `video_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `chunk_parse_task_ibfk_2`：(`video_chunk_id`) → `video_chunk`(`video_chunk_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_chunk_parse_task_due`：(`status`, `next_execute_time`)，类型 `BTREE`
- `ix_chunk_parse_task_lease`：(`status`, `lease_expire_time`)，类型 `BTREE`
- `ix_chunk_parse_task_parent_status`：(`video_parse_task_id`, `status`, `video_chunk_id`)，类型 `BTREE`
- `video_chunk_id`：(`video_chunk_id`)，类型 `BTREE`

### `context_snapshot`

用途：旧解析链按 Video Chunk 位置保存的上下文压缩快照。

实测：9 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `snapshot_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `engine_snapshot_id` | `varchar(128)` | 否 | `NULL` | — | — |
| 3 | `series_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 4 | `video_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 5 | `after_chunk_sequence_no` | `int` | 否 | `NULL` | — | — |
| 6 | `compact_text` | `longtext` | 否 | `NULL` | — | — |
| 7 | `structured_state` | `json` | 否 | `NULL` | — | — |
| 8 | `created_from_video_chunk_id` | `varchar(36)` | 否 | `NULL` | `UNI` | — |
| 9 | `created_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`snapshot_id`)
- 唯一 `created_from_video_chunk_id`：(`created_from_video_chunk_id`)
- 唯一 `uq_context_snapshot_video_sequence`：(`video_id`, `after_chunk_sequence_no`)
- CHECK `ck_context_snapshot_sequence`：`` (`after_chunk_sequence_no` >= 0) ``
- 外键 `context_snapshot_ibfk_1`：(`series_id`) → `series`(`series_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `context_snapshot_ibfk_2`：(`video_id`) → `video`(`video_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `context_snapshot_ibfk_3`：(`created_from_video_chunk_id`) → `video_chunk`(`video_chunk_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_context_snapshot_series_id`：(`series_id`)，类型 `BTREE`
- `ix_context_snapshot_video_id`：(`video_id`)，类型 `BTREE`

### `es_index_build`

用途：一次 Elasticsearch 投影构建的父记录及合同版本。

实测：12 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `workspace_id` | `varchar(64)` | 否 | `NULL` | — | — |
| 3 | `series_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 4 | `video_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 5 | `video_parse_task_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 6 | `parse_task_version` | `int` | 否 | `NULL` | — | — |
| 7 | `chunk_count` | `int` | 否 | `NULL` | — | — |
| 8 | `index_name` | `varchar(255)` | 否 | `NULL` | — | — |
| 9 | `index_contract_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 10 | `retrieval_contract_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 11 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 12 | `published_at` | `timestamp(3)` | 是 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`id`)
- CHECK `ck_es_index_build_chunk_count`：`` (`chunk_count` > 0) ``
- CHECK `ck_es_index_build_parse_version`：`` (`parse_task_version` > 0) ``

普通非唯一索引：

- `ix_es_index_build_video`：(`video_id`, `created_at`)，类型 `BTREE`

### `es_index_build_chunk`

用途：ES 构建中的逐 Chunk 投影、向量、写入状态与查询证明。

实测：29 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `build_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 3 | `sequence_no` | `int` | 否 | `NULL` | — | — |
| 4 | `start_seconds` | `bigint` | 否 | `NULL` | — | — |
| 5 | `end_seconds` | `bigint` | 否 | `NULL` | — | — |
| 6 | `chunk_parse_task_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 7 | `content_version` | `int` | 否 | `NULL` | — | — |
| 8 | `chunk_content_sha256` | `varchar(64)` | 否 | `NULL` | — | — |
| 9 | `fact_snapshot` | `json` | 否 | `NULL` | — | — |
| 10 | `fact_sha256` | `varchar(64)` | 否 | `NULL` | — | — |
| 11 | `projection_snapshot` | `json` | 否 | `NULL` | — | — |
| 12 | `projection_revision` | `varchar(64)` | 否 | `NULL` | — | — |
| 13 | `source_revision` | `varchar(64)` | 否 | `NULL` | — | — |
| 14 | `projection_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 15 | `encoding_contract` | `json` | 否 | `NULL` | — | — |
| 16 | `text_vector` | `json` | 是 | `NULL` | — | — |
| 17 | `video_vector` | `json` | 否 | `NULL` | — | — |
| 18 | `text_vector_sha256` | `varchar(64)` | 是 | `NULL` | — | — |
| 19 | `video_vector_sha256` | `varchar(64)` | 否 | `NULL` | — | — |
| 20 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 21 | `generation` | `int` | 否 | `NULL` | — | — |
| 22 | `attempt_count` | `int` | 否 | `NULL` | — | — |
| 23 | `available_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 24 | `lease_expires_at` | `timestamp(3)` | 是 | `NULL` | — | — |
| 25 | `query_proof` | `json` | 是 | `NULL` | — | — |
| 26 | `ready_at` | `timestamp(3)` | 是 | `NULL` | — | — |
| 27 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 28 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 29 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`build_id`, `video_chunk_id`)
- 唯一 `uq_es_build_chunk_sequence`：(`build_id`, `sequence_no`)
- CHECK `ck_es_build_chunk_attempts`：`` (`attempt_count` >= 0) ``
- CHECK `ck_es_build_chunk_content_version`：`` (`content_version` > 0) ``
- CHECK `ck_es_build_chunk_generation`：`` (`generation` >= 0) ``
- CHECK `ck_es_build_chunk_range`：`` ((`start_seconds` >= 0) and (`end_seconds` > `start_seconds`)) ``
- CHECK `ck_es_build_chunk_ready_proof`：`` (((`status` = _utf8mb4'WRITING') and (`lease_expires_at` is not null) and (`query_proof` is null) and (`ready_at` is null)) or ((`status` = _utf8mb4'READY') and (`lease_expires_at` is null) and (`query_proof` is not null) and (`ready_at` is not null)) or ((`status` in (_utf8mb4'PENDING',_utf8mb4'RETRY_WAIT')) and (`lease_expires_at` is null) and (`query_proof` is null) and (`ready_at` is null))) ``
- CHECK `ck_es_build_chunk_sequence`：`` (`sequence_no` >= 0) ``
- CHECK `ck_es_build_chunk_status`：`` (`status` in (_utf8mb4'PENDING',_utf8mb4'WRITING',_utf8mb4'RETRY_WAIT',_utf8mb4'READY')) ``
- CHECK `ck_es_build_chunk_text_vector_pair`：`` (((`text_vector` is null) and (`text_vector_sha256` is null)) or ((`text_vector` is not null) and (`text_vector_sha256` is not null))) ``
- 外键 `es_index_build_chunk_ibfk_1`：(`build_id`) → `es_index_build`(`id`)；ON UPDATE NO ACTION，ON DELETE RESTRICT

普通非唯一索引：

- `ix_es_build_chunk_due`：(`status`, `available_at`)，类型 `BTREE`

### `es_video_publication`

用途：Video 当前期望及已发布 ES build 的 fenced 指针。

实测：8 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `desired_build_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 3 | `published_build_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 4 | `fence` | `bigint` | 否 | `NULL` | — | — |
| 5 | `is_deleted` | `tinyint(1)` | 否 | `NULL` | — | — |
| 6 | `published_at` | `timestamp(3)` | 是 | `NULL` | — | — |
| 7 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 8 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_id`)
- CHECK `ck_es_video_publication_fence`：`` (`fence` > 0) ``
- CHECK `ck_es_video_publication_tombstone`：`` (((`is_deleted` = 1) and (`desired_build_id` is null) and (`published_build_id` is null)) or ((`is_deleted` = 0) and (`desired_build_id` is not null))) ``
- 外键 `es_video_publication_ibfk_1`：(`desired_build_id`) → `es_index_build`(`id`)；ON UPDATE NO ACTION，ON DELETE RESTRICT
- 外键 `es_video_publication_ibfk_2`：(`published_build_id`) → `es_index_build`(`id`)；ON UPDATE NO ACTION，ON DELETE RESTRICT

普通非唯一索引：

- `desired_build_id`：(`desired_build_id`)，类型 `BTREE`
- `published_build_id`：(`published_build_id`)，类型 `BTREE`

### `index_sync_intent`

用途：旧索引同步 outbox/intention，记录 UPSERT/DELETE 的重试状态。

实测：11 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `operation` | `varchar(16)` | 否 | `NULL` | — | — |
| 3 | `source_revision` | `varchar(64)` | 否 | `NULL` | — | — |
| 4 | `generation` | `int` | 否 | `NULL` | — | — |
| 5 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 6 | `attempt_count` | `int` | 否 | `NULL` | — | — |
| 7 | `available_at` | `bigint` | 否 | `NULL` | — | — |
| 8 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 9 | `synced_at` | `bigint` | 是 | `NULL` | — | — |
| 10 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 11 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_chunk_id`)
- CHECK `ck_index_sync_intent_attempt_count`：`` (`attempt_count` >= 0) ``
- CHECK `ck_index_sync_intent_generation`：`` (`generation` > 0) ``
- CHECK `ck_index_sync_intent_operation`：`` (`operation` in (_utf8mb4'UPSERT',_utf8mb4'DELETE')) ``
- CHECK `ck_index_sync_intent_status`：`` (`status` in (_utf8mb4'PENDING',_utf8mb4'RETRY_WAIT',_utf8mb4'SYNCED')) ``
- 外键 `index_sync_intent_ibfk_1`：(`video_chunk_id`) → `video_chunk`(`video_chunk_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_index_sync_intent_due`：(`status`, `available_at`)，类型 `BTREE`

### `interval_parse_content`

用途：V2 Interval 任务的一对一区间级成果。

实测：5 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `interval_parse_task_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `content` | `json` | 否 | `NULL` | — | — |
| 3 | `detail` | `json` | 是 | `NULL` | — | — |
| 4 | `create_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 5 | `update_time` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`interval_parse_task_id`)
- 外键 `interval_parse_content_ibfk_1`：(`interval_parse_task_id`) → `interval_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

### `interval_parse_task`

用途：V2 重叠区间的固定成员、ready mask、Refine 状态与租约。

实测：18 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `chunk_parse_task_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `video_parse_task_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 4 | `chunk_parse_task_ids` | `json` | 否 | `NULL` | — | — |
| 5 | `start_sequence_no` | `int` | 否 | `NULL` | — | — |
| 6 | `chunk_count` | `int` | 否 | `NULL` | — | — |
| 7 | `ready_mask` | `bigint` | 否 | `NULL` | — | — |
| 8 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 9 | `next_execute_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 10 | `retry_count` | `int` | 否 | `NULL` | — | — |
| 11 | `lease_owner` | `varchar(128)` | 是 | `NULL` | — | — |
| 12 | `lease_version` | `int` | 否 | `NULL` | — | — |
| 13 | `lease_expire_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 14 | `start_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 15 | `finish_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 16 | `last_error` | `json` | 是 | `NULL` | — | — |
| 17 | `create_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 18 | `update_time` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`id`)
- 唯一 `uq_interval_parse_task_parent_start`：(`video_parse_task_id`, `start_sequence_no`)
- CHECK `ck_interval_parse_task_chunk_count`：`` (`chunk_count` between 1 and 63) ``
- CHECK `ck_interval_parse_task_lease_state`：`` (((`status` = _utf8mb4'REFINING') and (`lease_owner` is not null) and (`lease_expire_time` is not null)) or ((`status` <> _utf8mb4'REFINING') and (`lease_owner` is null) and (`lease_expire_time` is null))) ``
- CHECK `ck_interval_parse_task_lease_version`：`` (`lease_version` >= 0) ``
- CHECK `ck_interval_parse_task_ready_mask`：`` ((`ready_mask` >= 0) and ((`ready_mask` >> `chunk_count`) = 0)) ``
- CHECK `ck_interval_parse_task_retry_count`：`` (`retry_count` >= 0) ``
- CHECK `ck_interval_parse_task_start_sequence`：`` (`start_sequence_no` >= 0) ``
- CHECK `ck_interval_parse_task_status`：`` (`status` in (_utf8mb4'WAITING',_utf8mb4'REFINING',_utf8mb4'COMPLETE',_utf8mb4'FAILED')) ``
- 外键 `fk_interval_parse_task_anchor_parent`：(`chunk_parse_task_id`, `video_parse_task_id`) → `chunk_parse_task`(`id`, `video_parse_task_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `interval_parse_task_ibfk_1`：(`video_parse_task_id`) → `video_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `fk_interval_parse_task_anchor_parent`：(`chunk_parse_task_id`, `video_parse_task_id`)，类型 `BTREE`
- `ix_interval_parse_task_due`：(`status`, `next_execute_time`)，类型 `BTREE`
- `ix_interval_parse_task_lease`：(`status`, `lease_expire_time`)，类型 `BTREE`
- `ix_interval_parse_task_parent_status`：(`video_parse_task_id`, `status`, `start_sequence_no`)，类型 `BTREE`

### `parse_model_call`

用途：解析模型调用明细、token、费用快照及 V1/V2 任务归属。

实测：22 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `call_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `job_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `video_parse_task_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 4 | `attempt_count` | `int` | 否 | `NULL` | — | — |
| 5 | `lease_version` | `int` | 否 | `NULL` | — | — |
| 6 | `workspace_id` | `varchar(64)` | 否 | `NULL` | — | — |
| 7 | `series_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 8 | `video_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 9 | `sequence_no` | `int` | 是 | `NULL` | — | — |
| 10 | `video_chunk_id` | `varchar(36)` | 是 | `NULL` | — | — |
| 11 | `provider` | `varchar(128)` | 否 | `NULL` | — | — |
| 12 | `model` | `varchar(255)` | 否 | `NULL` | — | — |
| 13 | `category` | `varchar(16)` | 否 | `NULL` | — | — |
| 14 | `operation` | `varchar(128)` | 否 | `NULL` | — | — |
| 15 | `started_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 16 | `request_id` | `varchar(512)` | 是 | `NULL` | — | — |
| 17 | `status` | `varchar(16)` | 否 | `NULL` | — | — |
| 18 | `tokens` | `json` | 是 | `NULL` | — | — |
| 19 | `estimated_cost_cny` | `varchar(128)` | 是 | `NULL` | — | — |
| 20 | `price_snapshot` | `json` | 是 | `NULL` | — | — |
| 21 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 22 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`call_id`)
- CHECK `ck_parse_model_call_category`：`` (`category` in (_utf8mb4'parse',_utf8mb4'embedding')) ``
- CHECK `ck_parse_model_call_status`：`` (`status` in (_utf8mb4'completed',_utf8mb4'failed')) ``
- 外键 `parse_model_call_ibfk_1`：(`job_id`) → `series_parse_job`(`job_id`)；ON UPDATE NO ACTION，ON DELETE NO ACTION
- 外键 `parse_model_call_ibfk_2`：(`video_parse_task_id`) → `video_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE SET NULL

普通非唯一索引：

- `ix_parse_model_call_job_started`：(`job_id`, `started_at`)，类型 `BTREE`
- `ix_parse_model_call_video_parse_task_started`：(`video_parse_task_id`, `started_at`)，类型 `BTREE`

### `parse_usage_attempt`

用途：按旧 Job attempt/lease 记录费用采集尝试，并可关联 V2 主任务。

实测：6 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `job_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `video_parse_task_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 3 | `attempt_count` | `int` | 否 | `NULL` | `PRI` | — |
| 4 | `lease_version` | `int` | 否 | `NULL` | `PRI` | — |
| 5 | `started_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 6 | `recording_complete` | `tinyint(1)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`job_id`, `attempt_count`, `lease_version`)
- 外键 `parse_usage_attempt_ibfk_1`：(`job_id`) → `series_parse_job`(`job_id`)；ON UPDATE NO ACTION，ON DELETE NO ACTION
- 外键 `parse_usage_attempt_ibfk_2`：(`video_parse_task_id`) → `video_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE SET NULL

普通非唯一索引：

- `ix_parse_usage_attempt_video_parse_task_id`：(`video_parse_task_id`)，类型 `BTREE`

### `series`

用途：Series 业务根实体、工作区归属、状态与人物设定。

实测：10 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `series_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `workspace_id` | `varchar(64)` | 否 | `NULL` | `MUL` | — |
| 3 | `name` | `varchar(255)` | 否 | `NULL` | — | — |
| 4 | `description` | `longtext` | 是 | `NULL` | — | — |
| 5 | `transcript_language_hint` | `varchar(64)` | 是 | `NULL` | — | — |
| 6 | `status` | `varchar(32)` | 否 | `NULL` | — | — |
| 7 | `important_extra_data` | `json` | 是 | `NULL` | — | — |
| 8 | `character_bible` | `json` | 否 | `NULL` | — | — |
| 9 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 10 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`series_id`)
- 唯一 `uq_series_workspace_name`：(`workspace_id`, `name`)

### `series_parse_job`

用途：保留的 V1 整集解析 Job、租约、进度及错误。

实测：23 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `job_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `series_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `video_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 4 | `pipeline_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 5 | `parse_config` | `json` | 是 | `NULL` | — | — |
| 6 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 7 | `stage` | `varchar(32)` | 否 | `NULL` | — | — |
| 8 | `progress` | `int` | 否 | `NULL` | — | — |
| 9 | `attempt_count` | `int` | 否 | `NULL` | — | — |
| 10 | `max_attempts` | `int` | 否 | `NULL` | — | — |
| 11 | `version` | `int` | 否 | `NULL` | — | — |
| 12 | `available_at` | `bigint` | 否 | `NULL` | — | — |
| 13 | `lease_owner` | `varchar(128)` | 是 | `NULL` | — | — |
| 14 | `lease_version` | `int` | 否 | `NULL` | — | — |
| 15 | `lease_expires_at` | `bigint` | 是 | `NULL` | — | — |
| 16 | `heartbeat_at` | `bigint` | 是 | `NULL` | — | — |
| 17 | `started_at` | `bigint` | 是 | `NULL` | — | — |
| 18 | `completed_at` | `bigint` | 是 | `NULL` | — | — |
| 19 | `processing_duration_ms` | `bigint` | 否 | `NULL` | — | — |
| 20 | `processing_timing_complete` | `tinyint(1)` | 否 | `NULL` | — | — |
| 21 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 22 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 23 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`job_id`)
- 唯一 `uq_series_parse_job_video_pipeline`：(`video_id`, `pipeline_version`)
- CHECK `ck_series_parse_job_attempt_count`：`` (`attempt_count` >= 0) ``
- CHECK `ck_series_parse_job_lease_version`：`` (`lease_version` >= 0) ``
- CHECK `ck_series_parse_job_max_attempts`：`` (`max_attempts` > 0) ``
- CHECK `ck_series_parse_job_processing_duration`：`` (`processing_duration_ms` >= 0) ``
- CHECK `ck_series_parse_job_progress`：`` (`progress` between 0 and 100) ``
- CHECK `ck_series_parse_job_stage`：`` (`stage` in (_utf8mb4'QUEUED',_utf8mb4'PREPARING',_utf8mb4'SPLIT_READY',_utf8mb4'PARSING',_utf8mb4'TEXT_EMBEDDING',_utf8mb4'VISION_EMBEDDING',_utf8mb4'PERSISTING',_utf8mb4'INDEXING',_utf8mb4'COMPLETED')) ``
- CHECK `ck_series_parse_job_status`：`` (`status` in (_utf8mb4'QUEUED',_utf8mb4'RUNNING',_utf8mb4'RETRY_WAIT',_utf8mb4'SUCCEEDED',_utf8mb4'FAILED')) ``
- CHECK `ck_series_parse_job_version`：`` (`version` >= 1) ``
- 外键 `series_parse_job_ibfk_1`：(`series_id`) → `series`(`series_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `series_parse_job_ibfk_2`：(`video_id`) → `video`(`video_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_series_parse_job_due`：(`status`, `available_at`)，类型 `BTREE`
- `ix_series_parse_job_series_id`：(`series_id`)，类型 `BTREE`
- `ix_series_parse_job_video_id`：(`video_id`)，类型 `BTREE`

### `upload_segment`

用途：上传会话的分片范围、校验和及暂存 URI。

实测：8 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `upload_session_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `segment_number` | `int` | 否 | `NULL` | `PRI` | — |
| 3 | `start_byte` | `bigint` | 否 | `NULL` | — | — |
| 4 | `end_byte_exclusive` | `bigint` | 否 | `NULL` | — | — |
| 5 | `actual_size_bytes` | `int` | 否 | `NULL` | — | — |
| 6 | `checksum_sha256` | `varchar(64)` | 否 | `NULL` | — | — |
| 7 | `artifact_uri` | `longtext` | 否 | `NULL` | — | — |
| 8 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`upload_session_id`, `segment_number`)
- CHECK `ck_upload_segment_number`：`` (`segment_number` >= 0) ``
- CHECK `ck_upload_segment_range`：`` ((`start_byte` >= 0) and (`end_byte_exclusive` > `start_byte`)) ``
- CHECK `ck_upload_segment_size`：`` (`actual_size_bytes` = (`end_byte_exclusive` - `start_byte`)) ``
- 外键 `upload_segment_ibfk_1`：(`upload_session_id`) → `upload_session`(`upload_session_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

### `upload_session`

用途：上传会话、确认集序、配置快照、完成状态及 V1/V2 任务链接。

实测：31 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `upload_session_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `workspace_id` | `varchar(64)` | 否 | `NULL` | `MUL` | — |
| 3 | `series_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 4 | `episode_order` | `int` | 否 | `NULL` | — | — |
| 5 | `upload_sequence` | `int` | 否 | `NULL` | — | — |
| 6 | `original_filename` | `varchar(1024)` | 否 | `NULL` | — | — |
| 7 | `media_type` | `varchar(255)` | 否 | `NULL` | — | — |
| 8 | `total_size_bytes` | `bigint` | 否 | `NULL` | — | — |
| 9 | `preferred_segment_size_bytes` | `int` | 否 | `NULL` | — | — |
| 10 | `total_segments` | `int` | 否 | `NULL` | — | — |
| 11 | `content_sha256` | `varchar(64)` | 是 | `NULL` | — | — |
| 12 | `parse_config` | `json` | 是 | `NULL` | — | — |
| 13 | `video_parse_config` | `json` | 是 | `NULL` | — | — |
| 14 | `detail` | `json` | 是 | `NULL` | — | — |
| 15 | `completion_stage` | `varchar(32)` | 是 | `NULL` | — | — |
| 16 | `version` | `int` | 否 | `NULL` | — | — |
| 17 | `status` | `varchar(16)` | 否 | `NULL` | — | — |
| 18 | `idempotency_key` | `varchar(128)` | 否 | `NULL` | — | — |
| 19 | `request_fingerprint` | `varchar(64)` | 是 | `NULL` | — | — |
| 20 | `active_slot_key` | `varchar(96)` | 是 | `NULL` | `UNI` | — |
| 21 | `completion_token` | `varchar(36)` | 是 | `NULL` | — | — |
| 22 | `completion_started_at` | `timestamp(3)` | 是 | `NULL` | — | — |
| 23 | `expires_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 24 | `completed_at` | `timestamp(3)` | 是 | `NULL` | — | — |
| 25 | `video_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 26 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 27 | `last_error_code` | `varchar(64)` | 是 | `NULL` | — | — |
| 28 | `job_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 29 | `initial_video_parse_task_id` | `varchar(36)` | 是 | `NULL` | `MUL` | — |
| 30 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 31 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`upload_session_id`)
- 唯一 `uq_upload_session_active_slot`：(`active_slot_key`)
- 唯一 `uq_upload_session_workspace_idempotency`：(`workspace_id`, `idempotency_key`)
- CHECK `ck_upload_session_completion_stage`：`` ((`completion_stage` is null) or (`completion_stage` in (_utf8mb4'VERIFYING',_utf8mb4'ASSEMBLING',_utf8mb4'PROBING',_utf8mb4'CREATING_JOB'))) ``
- CHECK `ck_upload_session_episode_order`：`` (`episode_order` > 0) ``
- CHECK `ck_upload_session_segment_size`：`` (`preferred_segment_size_bytes` > 0) ``
- CHECK `ck_upload_session_status`：`` (`status` in (_utf8mb4'UPLOADING',_utf8mb4'COMPLETING',_utf8mb4'COMPLETED',_utf8mb4'EXPIRED')) ``
- CHECK `ck_upload_session_total_segments`：`` (`total_segments` > 0) ``
- CHECK `ck_upload_session_total_size`：`` (`total_size_bytes` > 0) ``
- CHECK `ck_upload_session_upload_sequence`：`` (`upload_sequence` > 0) ``
- CHECK `ck_upload_session_version`：`` (`version` > 0) ``
- 外键 `upload_session_ibfk_1`：(`series_id`) → `series`(`series_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE
- 外键 `upload_session_ibfk_2`：(`video_id`) → `video`(`video_id`)；ON UPDATE NO ACTION，ON DELETE SET NULL
- 外键 `upload_session_ibfk_3`：(`job_id`) → `series_parse_job`(`job_id`)；ON UPDATE NO ACTION，ON DELETE SET NULL
- 外键 `upload_session_ibfk_4`：(`initial_video_parse_task_id`) → `video_parse_task`(`id`)；ON UPDATE NO ACTION，ON DELETE SET NULL

普通非唯一索引：

- `ix_upload_session_initial_video_parse_task_id`：(`initial_video_parse_task_id`)，类型 `BTREE`
- `ix_upload_session_job_id`：(`job_id`)，类型 `BTREE`
- `ix_upload_session_series_id`：(`series_id`)，类型 `BTREE`
- `ix_upload_session_video_id`：(`video_id`)，类型 `BTREE`

### `video`

用途：Video 基础信息、Series 内集序、源媒体及聚合状态。

实测：16 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `series_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `episode_order` | `int` | 否 | `NULL` | — | — |
| 4 | `upload_sequence` | `int` | 否 | `NULL` | — | — |
| 5 | `original_filename` | `varchar(1024)` | 否 | `NULL` | — | — |
| 6 | `original_source_uri` | `longtext` | 否 | `NULL` | — | — |
| 7 | `inference_proxy_uri` | `longtext` | 是 | `NULL` | — | — |
| 8 | `content_hash` | `varchar(64)` | 否 | `NULL` | — | — |
| 9 | `total_bytes` | `bigint` | 否 | `NULL` | — | — |
| 10 | `duration_seconds` | `bigint` | 否 | `NULL` | — | — |
| 11 | `upload_status` | `varchar(32)` | 否 | `NULL` | — | — |
| 12 | `parse_status` | `varchar(32)` | 否 | `NULL` | — | — |
| 13 | `upload_progress` | `int` | 否 | `NULL` | — | — |
| 14 | `parse_progress` | `int` | 否 | `NULL` | — | — |
| 15 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 16 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_id`)
- 唯一 `uq_video_series_hash`：(`series_id`, `content_hash`)
- 唯一 `uq_video_series_order`：(`series_id`, `episode_order`)
- CHECK `ck_video_duration`：`` (`duration_seconds` > 0) ``
- CHECK `ck_video_episode_order`：`` (`episode_order` > 0) ``
- CHECK `ck_video_parse_progress`：`` (`parse_progress` between 0 and 100) ``
- CHECK `ck_video_total_bytes`：`` (`total_bytes` >= 0) ``
- CHECK `ck_video_upload_progress`：`` (`upload_progress` between 0 and 100) ``
- 外键 `video_ibfk_1`：(`series_id`) → `series`(`series_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_video_series_id`：(`series_id`)，类型 `BTREE`

### `video_chunk`

用途：固定 Video Chunk 拓扑、秒级范围、媒体引用和基础识别数据。

实测：14 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `video_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `sequence_no` | `int` | 否 | `NULL` | — | — |
| 4 | `start_seconds` | `bigint` | 否 | `NULL` | — | — |
| 5 | `end_seconds` | `bigint` | 否 | `NULL` | — | — |
| 6 | `analysis_window_start_seconds` | `bigint` | 否 | `NULL` | — | — |
| 7 | `analysis_window_end_seconds` | `bigint` | 否 | `NULL` | — | — |
| 8 | `chunk_media_ref` | `longtext` | 否 | `NULL` | — | — |
| 9 | `analysis_window_media_ref` | `longtext` | 否 | `NULL` | — | — |
| 10 | `transcript` | `json` | 否 | `NULL` | — | — |
| 11 | `baseline_visual_caption` | `longtext` | 是 | `NULL` | — | — |
| 12 | `keyframe_uris` | `json` | 否 | `NULL` | — | — |
| 13 | `created_at` | `timestamp(3)` | 否 | `NULL` | — | — |
| 14 | `updated_at` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_chunk_id`)
- 唯一 `uq_video_chunk_video_sequence`：(`video_id`, `sequence_no`)
- CHECK `ck_video_chunk_analysis_range`：`` ((`analysis_window_start_seconds` <= `start_seconds`) and (`analysis_window_end_seconds` >= `end_seconds`)) ``
- CHECK `ck_video_chunk_range`：`` ((`start_seconds` >= 0) and (`end_seconds` > `start_seconds`)) ``
- CHECK `ck_video_chunk_sequence`：`` (`sequence_no` >= 0) ``
- 外键 `video_chunk_ibfk_1`：(`video_id`) → `video`(`video_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_video_chunk_video_id`：(`video_id`)，类型 `BTREE`

### `video_chunk_checkpoint`

用途：保留的 V1 Chunk 连续处理 checkpoint。

实测：13 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `job_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `sequence_no` | `int` | 否 | `NULL` | `PRI` | — |
| 3 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | — | — |
| 4 | `stage` | `varchar(32)` | 否 | `NULL` | — | — |
| 5 | `chunk_start_seconds` | `bigint` | 是 | `NULL` | — | — |
| 6 | `chunk_end_seconds` | `bigint` | 是 | `NULL` | — | — |
| 7 | `chunk_media_uri` | `longtext` | 是 | `NULL` | — | — |
| 8 | `analysis_window_media_uri` | `longtext` | 是 | `NULL` | — | — |
| 9 | `parse_artifact_uri` | `longtext` | 是 | `NULL` | — | — |
| 10 | `text_embedding_artifact_uri` | `longtext` | 是 | `NULL` | — | — |
| 11 | `vision_embedding_artifact_uri` | `longtext` | 是 | `NULL` | — | — |
| 12 | `last_error` | `longtext` | 是 | `NULL` | — | — |
| 13 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`job_id`, `sequence_no`)
- CHECK `ck_video_chunk_checkpoint_range`：`` (((`chunk_start_seconds` is null) and (`chunk_end_seconds` is null)) or ((`chunk_start_seconds` is not null) and (`chunk_end_seconds` is not null) and (`chunk_start_seconds` >= 0) and (`chunk_end_seconds` > `chunk_start_seconds`))) ``
- CHECK `ck_video_chunk_checkpoint_sequence`：`` (`sequence_no` >= 0) ``
- CHECK `ck_video_chunk_checkpoint_stage`：`` (`stage` in (_utf8mb4'PREPARED',_utf8mb4'SPLIT_READY',_utf8mb4'PARSED',_utf8mb4'TEXT_EMBEDDED',_utf8mb4'VISION_EMBEDDED',_utf8mb4'INDEXED')) ``
- 外键 `video_chunk_checkpoint_ibfk_1`：(`job_id`) → `series_parse_job`(`job_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_video_chunk_checkpoint_job_stage`：(`job_id`, `stage`)，类型 `BTREE`

### `video_chunk_embedding`

用途：旧链持久化的 Chunk 文本/视觉 embedding 与元数据。

实测：8 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `text_input` | `longtext` | 否 | `NULL` | — | — |
| 3 | `text_vector` | `json` | 是 | `NULL` | — | — |
| 4 | `vision_vector` | `json` | 是 | `NULL` | — | — |
| 5 | `text_embedding_metadata` | `json` | 是 | `NULL` | — | — |
| 6 | `vision_embedding_metadata` | `json` | 是 | `NULL` | — | — |
| 7 | `created_at` | `bigint` | 否 | `NULL` | — | — |
| 8 | `updated_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_chunk_id`)
- 外键 `video_chunk_embedding_ibfk_1`：(`video_chunk_id`) → `video_chunk`(`video_chunk_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

### `video_chunk_info`

用途：旧链持久化的 Chunk 解析详情、compact 与 provenance。

实测：13 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `video_chunk_id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `all_time_events` | `json` | 否 | `NULL` | — | — |
| 3 | `all_time_event_text` | `longtext` | 否 | `NULL` | — | — |
| 4 | `video_compact` | `longtext` | 否 | `NULL` | — | — |
| 5 | `context_before_compact` | `longtext` | 否 | `NULL` | — | — |
| 6 | `context_after_compact` | `longtext` | 否 | `NULL` | — | — |
| 7 | `window_summary` | `longtext` | 否 | `NULL` | — | — |
| 8 | `model_provider` | `varchar(64)` | 是 | `NULL` | — | — |
| 9 | `model_name` | `varchar(255)` | 是 | `NULL` | — | — |
| 10 | `pipeline_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 11 | `is_fallback` | `tinyint(1)` | 否 | `NULL` | — | — |
| 12 | `parse_provenance` | `json` | 是 | `NULL` | — | — |
| 13 | `parsed_at` | `bigint` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`video_chunk_id`)
- 外键 `video_chunk_info_ibfk_1`：(`video_chunk_id`) → `video_chunk`(`video_chunk_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

### `video_parse_task`

用途：V2 单次 Video 解析身份、固定配置、解析版本、状态与主租约。

实测：24 个字段，精确行数 0，引擎 `InnoDB`，排序规则 `utf8mb4_0900_ai_ci`。

| # | 字段 | 类型 | 可空 | Default | Key | Extra |
| ---: | --- | --- | :---: | --- | --- | --- |
| 1 | `id` | `varchar(36)` | 否 | `NULL` | `PRI` | — |
| 2 | `video_id` | `varchar(36)` | 否 | `NULL` | `MUL` | — |
| 3 | `pipeline_version` | `varchar(128)` | 否 | `NULL` | — | — |
| 4 | `status` | `varchar(16)` | 否 | `NULL` | `MUL` | — |
| 5 | `parse_config` | `json` | 否 | `NULL` | — | — |
| 6 | `chunk_count` | `int` | 否 | `NULL` | — | — |
| 7 | `version` | `int` | 否 | `NULL` | — | — |
| 8 | `next_execute_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 9 | `attempt_count` | `int` | 否 | `NULL` | — | — |
| 10 | `max_attempts` | `int` | 否 | `NULL` | — | — |
| 11 | `lease_owner` | `varchar(128)` | 是 | `NULL` | — | — |
| 12 | `lease_version` | `int` | 否 | `NULL` | — | — |
| 13 | `lease_expire_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 14 | `heartbeat_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 15 | `start_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 16 | `finish_time` | `timestamp(3)` | 是 | `NULL` | — | — |
| 17 | `processing_duration_ms` | `bigint` | 否 | `NULL` | — | — |
| 18 | `processing_timing_complete` | `tinyint(1)` | 否 | `NULL` | — | — |
| 19 | `last_error` | `json` | 是 | `NULL` | — | — |
| 20 | `is_test` | `tinyint(1)` | 否 | `NULL` | — | — |
| 21 | `is_deleted` | `tinyint(1)` | 否 | `NULL` | — | — |
| 22 | `detail` | `json` | 否 | `NULL` | — | — |
| 23 | `create_time` | `timestamp(3)` | 否 | `NULL` | — | — |
| 24 | `update_time` | `timestamp(3)` | 否 | `NULL` | — | — |

约束与关系：

- 主键 `PRIMARY`：(`id`)
- 唯一 `uq_video_parse_task_video_version`：(`video_id`, `version`)
- CHECK `ck_video_parse_task_attempt_count`：`` (`attempt_count` >= 0) ``
- CHECK `ck_video_parse_task_chunk_count`：`` (`chunk_count` > 0) ``
- CHECK `ck_video_parse_task_lease_state`：`` (((`status` in (_utf8mb4'PREPARING',_utf8mb4'RUNNING')) and (`lease_owner` is not null) and (`lease_expire_time` is not null) and (`heartbeat_time` is not null)) or ((`status` in (_utf8mb4'PENDING',_utf8mb4'COMPLETE',_utf8mb4'FAILED')) and (`lease_owner` is null) and (`lease_expire_time` is null) and (`heartbeat_time` is null))) ``
- CHECK `ck_video_parse_task_lease_version`：`` (`lease_version` >= 0) ``
- CHECK `ck_video_parse_task_max_attempts`：`` (`max_attempts` > 0) ``
- CHECK `ck_video_parse_task_processing_duration`：`` (`processing_duration_ms` >= 0) ``
- CHECK `ck_video_parse_task_status`：`` (`status` in (_utf8mb4'PENDING',_utf8mb4'PREPARING',_utf8mb4'RUNNING',_utf8mb4'COMPLETE',_utf8mb4'FAILED')) ``
- CHECK `ck_video_parse_task_version`：`` (`version` > 0) ``
- 外键 `video_parse_task_ibfk_1`：(`video_id`) → `video`(`video_id`)；ON UPDATE NO ACTION，ON DELETE CASCADE

普通非唯一索引：

- `ix_video_parse_task_due`：(`status`, `next_execute_time`)，类型 `BTREE`
- `ix_video_parse_task_lease`：(`status`, `lease_expire_time`)，类型 `BTREE`
- `ix_video_parse_task_video_status`：(`video_id`, `status`, `version`)，类型 `BTREE`

## 外键总表

以下 31 条关系按 constraint 分组；复合外键只计为一条关系。

| # | 子表与约束 | 子表字段 | 父表字段 | ON UPDATE | ON DELETE |
| ---: | --- | --- | --- | --- | --- |
| 1 | `agent_message.agent_message_ibfk_1` | `conversation_id` | `agent_conversation`(`conversation_id`) | NO ACTION | CASCADE |
| 2 | `chunk_parse_content.chunk_parse_content_ibfk_1` | `chunk_parse_task_id` | `chunk_parse_task`(`id`) | NO ACTION | CASCADE |
| 3 | `chunk_parse_task.chunk_parse_task_ibfk_1` | `video_parse_task_id` | `video_parse_task`(`id`) | NO ACTION | CASCADE |
| 4 | `chunk_parse_task.chunk_parse_task_ibfk_2` | `video_chunk_id` | `video_chunk`(`video_chunk_id`) | NO ACTION | CASCADE |
| 5 | `context_snapshot.context_snapshot_ibfk_1` | `series_id` | `series`(`series_id`) | NO ACTION | CASCADE |
| 6 | `context_snapshot.context_snapshot_ibfk_2` | `video_id` | `video`(`video_id`) | NO ACTION | CASCADE |
| 7 | `context_snapshot.context_snapshot_ibfk_3` | `created_from_video_chunk_id` | `video_chunk`(`video_chunk_id`) | NO ACTION | CASCADE |
| 8 | `es_index_build_chunk.es_index_build_chunk_ibfk_1` | `build_id` | `es_index_build`(`id`) | NO ACTION | RESTRICT |
| 9 | `es_video_publication.es_video_publication_ibfk_1` | `desired_build_id` | `es_index_build`(`id`) | NO ACTION | RESTRICT |
| 10 | `es_video_publication.es_video_publication_ibfk_2` | `published_build_id` | `es_index_build`(`id`) | NO ACTION | RESTRICT |
| 11 | `index_sync_intent.index_sync_intent_ibfk_1` | `video_chunk_id` | `video_chunk`(`video_chunk_id`) | NO ACTION | CASCADE |
| 12 | `interval_parse_content.interval_parse_content_ibfk_1` | `interval_parse_task_id` | `interval_parse_task`(`id`) | NO ACTION | CASCADE |
| 13 | `interval_parse_task.fk_interval_parse_task_anchor_parent` | `chunk_parse_task_id`, `video_parse_task_id` | `chunk_parse_task`(`id`, `video_parse_task_id`) | NO ACTION | CASCADE |
| 14 | `interval_parse_task.interval_parse_task_ibfk_1` | `video_parse_task_id` | `video_parse_task`(`id`) | NO ACTION | CASCADE |
| 15 | `parse_model_call.parse_model_call_ibfk_1` | `job_id` | `series_parse_job`(`job_id`) | NO ACTION | NO ACTION |
| 16 | `parse_model_call.parse_model_call_ibfk_2` | `video_parse_task_id` | `video_parse_task`(`id`) | NO ACTION | SET NULL |
| 17 | `parse_usage_attempt.parse_usage_attempt_ibfk_1` | `job_id` | `series_parse_job`(`job_id`) | NO ACTION | NO ACTION |
| 18 | `parse_usage_attempt.parse_usage_attempt_ibfk_2` | `video_parse_task_id` | `video_parse_task`(`id`) | NO ACTION | SET NULL |
| 19 | `series_parse_job.series_parse_job_ibfk_1` | `series_id` | `series`(`series_id`) | NO ACTION | CASCADE |
| 20 | `series_parse_job.series_parse_job_ibfk_2` | `video_id` | `video`(`video_id`) | NO ACTION | CASCADE |
| 21 | `upload_segment.upload_segment_ibfk_1` | `upload_session_id` | `upload_session`(`upload_session_id`) | NO ACTION | CASCADE |
| 22 | `upload_session.upload_session_ibfk_1` | `series_id` | `series`(`series_id`) | NO ACTION | CASCADE |
| 23 | `upload_session.upload_session_ibfk_2` | `video_id` | `video`(`video_id`) | NO ACTION | SET NULL |
| 24 | `upload_session.upload_session_ibfk_3` | `job_id` | `series_parse_job`(`job_id`) | NO ACTION | SET NULL |
| 25 | `upload_session.upload_session_ibfk_4` | `initial_video_parse_task_id` | `video_parse_task`(`id`) | NO ACTION | SET NULL |
| 26 | `video.video_ibfk_1` | `series_id` | `series`(`series_id`) | NO ACTION | CASCADE |
| 27 | `video_chunk.video_chunk_ibfk_1` | `video_id` | `video`(`video_id`) | NO ACTION | CASCADE |
| 28 | `video_chunk_checkpoint.video_chunk_checkpoint_ibfk_1` | `job_id` | `series_parse_job`(`job_id`) | NO ACTION | CASCADE |
| 29 | `video_chunk_embedding.video_chunk_embedding_ibfk_1` | `video_chunk_id` | `video_chunk`(`video_chunk_id`) | NO ACTION | CASCADE |
| 30 | `video_chunk_info.video_chunk_info_ibfk_1` | `video_chunk_id` | `video_chunk`(`video_chunk_id`) | NO ACTION | CASCADE |
| 31 | `video_parse_task.video_parse_task_ibfk_1` | `video_id` | `video`(`video_id`) | NO ACTION | CASCADE |

## 复核结论

- 表目录与逐表字段合计均为 23 张表、333 个字段。
- 外键总表共 31 条，等于 `information_schema.TABLE_CONSTRAINTS` 的 31 条 FOREIGN KEY。
- 主键 23、唯一约束 18、CHECK 76 的数量均直接来自实际实例。
- 23 张表精确总行数为 0，确认该独立开发库是空库；旧库数据未复制。
- 测试库 `anivag_video_parse_v2_refactor_test` 的 23 张同名表、333 个字段也已逐表读取，清理测试数据后精确总行数为 0。
