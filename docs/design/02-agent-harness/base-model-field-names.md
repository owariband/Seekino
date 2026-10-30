# BaseModel 与业务字段命名合同

本轮把 24 张业务表统一到 `BaseModel`，清理重复的实体前缀，沿用 RAGFlow 的 User 资料字段名称。现有 323 列调整为 390 列：38 个旧审计列替换为 96 个公共审计列，User 增加 7 列、Tenant 增加 2 列。另有 77 个现有业务／身份字段名称变化，其中 75 个是业务字段更名，2 个是 `is_guest → is_anonymous`、`role → is_superuser`。

实现入口为 [BaseModel](../../../server/model/base.py)、[User](../../../server/model/user.py)、[Tenant](../../../server/model/tenant.py)。受控迁移见 [20261010_base_model_field_names.sql](../../migrations/20261010_base_model_field_names.sql)，应用启动不会自动执行它。

## 公共时间字段

所有 24 张表继承以下四列。抽象 BaseModel 自身不产生数据库表。

| 字段 | MySQL 类型 | 可空 | 索引 | 合同 |
|---|---|---|---|---|
| `create_time` | BIGINT | 是 | 普通索引 | Unix 毫秒 |
| `create_date` | DATETIME(3) | 是 | 普通索引 | 与 `create_time` 对应的 UTC 日期 |
| `update_time` | BIGINT | 是 | 普通索引 | Unix 毫秒 |
| `update_date` | DATETIME(3) | 是 | 普通索引 | 与 `update_time` 对应的 UTC 日期 |

四列允许 NULL 是为了保留历史未知值。新 ORM 记录正常填满四列，初始 `update_time` 等于 `create_time`；日期列从整数列派生，使用同一个毫秒时刻。仓储只显式写入整数 `create_time/update_time`，接收 datetime 时先转换成 Unix 毫秒。SQLAlchemy ORM 和 Core／批量 INSERT、UPDATE 共用列 default/onupdate 维护逻辑。日期类型在数据库存 UTC 无时区值，读取时恢复为 UTC aware datetime。

这些维护规则是 SQLAlchemy 写入合同，数据库没有添加审计触发器。外部原生 SQL 更新必须同时维护对应整数与日期列，不能假定数据库会自动维护。Agent timing-only checkpoint 显式保留已有 `update_time`，仅同步其日期表示，继续保留原先的更新时间语义。

四列分别建索引，总计增加 96 个公共时间普通索引；User 另有 nickname、last_login_time 两个索引。它们增加存储和写入成本，批量迁移窗口应覆盖建索引时间。

业务事件时间继续保持原类型，仅统一名称。例如旧任务的 `started_at BIGINT → start_time BIGINT`，模型调用的 `started_at TIMESTAMP(3) → start_time TIMESTAMP(3)`。`*_time` 后缀本身不保证类型是 BIGINT；只有 BaseModel 的 create_time、update_time 固定为 Unix 毫秒。

## PK、FK 与命名

独立实体自身 PK 使用 `id`，关联键使用目标短名或明确角色名。ID 原有 VARCHAR 长度和值保留，不重新生成 UUID，不把 36 字符身份改为 RAGFlow 的 32 字符 ID。

13 个已有独立实体 PK 改为 id；video_parse_task、chunk_parse_task、chunk_parse_content、interval_parse_task 原本已经是 id。其余 7 张表保持关联键或复合键结构。业务字段保留有意义的限定，例如 `engine_snapshot_id`、`requested_*`、`end_byte_exclusive`。

现有 30 个 FK 约束包含 31 条列引用，其中 interval_parse_task 有两列复合 FK。迁移保持原 ondelete/onupdate、关联值和约束含义；不补建当前不存在的业务 FK。旧的 tenant.owner_user_id 变为 owner_id，继续引用 user.id；其它用户角色字段同样保持既有归属含义。

下面是完整的 77 个名称变化；公共四时间列的转换另见历史迁移表。

| 表 | 现有字段 → 目标字段 |
|---|---|
| `agent_conversation` | `owner_user_id → owner_id`<br>`last_message_at → last_message_time`<br>`conversation_id → id` |
| `model_usage` | `actor_user_id → user_id`<br>`started_at → start_time`<br>`call_id → id` |
| `series` | `series_id → id` |
| `user` | `user_id → id`<br>`is_guest → is_anonymous`<br>`role → is_superuser` |
| `agent_message` | `model_name → model`<br>`message_id → id` |
| `tenant` | `owner_user_id → owner_id`<br>`tenant_id → id` |
| `video` | `original_filename → filename`<br>`original_source_uri → source_uri`<br>`inference_proxy_uri → proxy_uri`<br>`content_hash → hash`<br>`total_bytes → size_bytes`<br>`duration_seconds → duration_ms`<br>`video_id → id` |
| `series_parse_job` | `created_by_user_id → creator_id`<br>`available_at → next_execute_time`<br>`lease_expires_at → lease_expire_time`<br>`heartbeat_at → heartbeat_time`<br>`started_at → start_time`<br>`completed_at → finish_time`<br>`job_id → id` |
| `video_chunk` | `video_chunk_id → id`<br>`start_seconds → start_ms`<br>`end_seconds → end_ms`<br>`analysis_window_start_seconds → window_start_ms`<br>`analysis_window_end_seconds → window_end_ms` |
| `video_parse_task` | `created_by_user_id → creator_id` |
| `chunk_parse_task` | `video_parse_task_id → video_task_id`<br>`video_chunk_id → chunk_id` |
| `context_snapshot` | `created_from_video_chunk_id → source_chunk_id`<br>`snapshot_id → id` |
| `index_sync_intent` | `video_chunk_id → chunk_id`<br>`available_at → next_execute_time`<br>`synced_at → sync_time` |
| `parse_model_call` | `video_parse_task_id → video_task_id`<br>`video_chunk_id → chunk_id`<br>`started_at → start_time`<br>`call_id → id` |
| `parse_usage_attempt` | `video_parse_task_id → video_task_id`<br>`started_at → start_time` |
| `upload_session` | `upload_session_id → id`<br>`created_by_user_id → creator_id`<br>`original_filename → filename`<br>`total_size_bytes → size_bytes`<br>`preferred_segment_size_bytes → segment_size_bytes`<br>`total_segments → segment_count`<br>`content_sha256 → sha256`<br>`completion_started_at → completion_start_time`<br>`expires_at → expire_time`<br>`completed_at → finish_time`<br>`initial_video_parse_task_id → initial_task_id` |
| `video_chunk_checkpoint` | `video_chunk_id → chunk_id`<br>`chunk_start_seconds → start_ms`<br>`chunk_end_seconds → end_ms` |
| `video_chunk_embedding` | `video_chunk_id → chunk_id` |
| `video_chunk_info` | `video_chunk_id → chunk_id`<br>`model_provider → provider`<br>`model_name → model`<br>`parsed_at → parse_time` |
| `video_parse_usage_attempt` | `video_parse_task_id → video_task_id`<br>`started_at → start_time`<br>`attempt_id → id` |
| `chunk_parse_content` | `chunk_parse_task_id → chunk_task_id` |
| `interval_parse_task` | `chunk_parse_task_id → chunk_task_id`<br>`video_parse_task_id → video_task_id`<br>`chunk_parse_task_ids → chunk_task_ids` |
| `upload_segment` | `upload_session_id → session_id`<br>`actual_size_bytes → size_bytes`<br>`checksum_sha256 → sha256` |
| `interval_parse_content` | `interval_parse_task_id → interval_task_id` |

## User 与 Tenant

User 共 17 列：13 个自身字段，以及 BaseModel 的 4 个时间字段。

| 字段 | MySQL 类型 | 可空 | 含义／ORM 默认 |
|---|---|---|---|
| `id` | VARCHAR(36) | 否 | PK；原 user_id 值保留 |
| `email` | VARCHAR(254) | 是 | UNIQUE；游客必须 NULL，本地身份允许 NULL |
| `is_anonymous` | BOOLEAN | 否 | 原 is_guest；默认 true |
| `status` | VARCHAR(16) | 否 | ACTIVE / DISABLED；默认 ACTIVE |
| `auth_version` | INTEGER | 否 | 默认 1，必须 ≥ 1；撤销旧登录凭据 |
| `is_superuser` | BOOLEAN | 否 | 原全局 role：admin→true，user→false |
| `nickname` | VARCHAR(100) | 否 | 昵称，普通索引；创建入口设置明确名称 |
| `avatar` | TEXT | 是 | 头像资料，使用 RAGFlow 字段名称与 TEXT 形态 |
| `language` | VARCHAR(32) | 是 | ORM 默认 Chinese |
| `color_schema` | VARCHAR(32) | 是 | ORM 默认 Bright |
| `timezone` | VARCHAR(64) | 是 | ORM 默认 Asia/Shanghai |
| `last_login_time` | DATETIME(3) | 是 | 最近成功登录的 UTC 日期，普通索引 |
| `login_channel` | VARCHAR(32) | 是 | 登录渠道；当前成功邮箱登录记为 email |

这轮没有新增 password、access_token、is_authenticated 等不同认证机制的字段。现有邮箱验证码、令牌撤销和状态校验继续使用既有认证链路；DTO 的游客、全局 user/admin 角色由新 ORM 字段映射，接口合同继续保留。

成功邮箱登录在同一身份事务中更新目标 User 的 last_login_time、login_channel；失败验证码、普通请求鉴权和退出登录不写最近成功登录记录。本地模式没有把每次请求算作登录。

历史 nickname：固定 local-user 使用“本地用户”，游客使用“游客”，其他身份使用邮箱 @ 前缀，最多 100 字符；没有可用前缀时使用“用户”。新增 avatar、language、color_schema、timezone、last_login_time、login_channel 的历史值保持 NULL。历史资料不根据请求日志伪造，ORM 默认只在后续新记录写入时应用。

Tenant 共 11 列：原业务字段 id、owner_id、type、status、name，加 description TEXT、avatar TEXT，以及 BaseModel 的 4 个时间字段。public Tenant 的 owner_id 必须 NULL；local/personal 必须有 owner_id，继续通过 FK 引用 User。type、status、PK 长度及归属规则保留。新增 description、avatar 的历史值为 NULL。

本轮提供持久化字段和现有登录链路写入，不新增资料编辑、头像上传或偏好设置 API／前端交互。

## 历史时间迁移

每个已有时间事实独立迁入对应时间／日期对。不从 start_time、finish_time、parse_time、心跳等业务时间猜测创建时间；仅有创建时间的历史记录保留更新两列 NULL，仅有更新时间的记录保留创建两列 NULL。

| 源审计列 | 目标与转换 |
|---|---|
| BIGINT `created_at/updated_at` | 更名为 create_time/update_time，原整数值保持；使用 UTC epoch + 整数微秒派生 date |
| TIMESTAMP(3) `created_at/updated_at` | 迁为 DATETIME(3) create_date/update_date，UTC 会话保留毫秒；TIMESTAMPDIFF(MICROSECOND, epoch, date) DIV 1000 派生 time |
| V2 TIMESTAMP(3) `create_time/update_time` | 先 CHANGE 为 DATETIME(3) create_date/update_date，再 ADD 新 BIGINT create_time/update_time，避免同名类型冲突 |
| 源没有对应创建／更新列 | 对应的整数、日期两列均 NULL |

旧 Unix 毫秒必须是非负值且能够由 DATETIME(3) 表示；不满足范围时预检拒绝迁移。date 列不经过浮点秒转换，保留已有毫秒精度。

以下清单列出 24 表的源审计事实和最终列数：

| 表 | 原有创建事实 | 原有更新事实 | 新列数 |
|---|---|---|---:|
| `agent_conversation` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 12 |
| `model_usage` | `created_at`，UTC TIMESTAMP(3) | 无，历史保留 NULL | 12 |
| `series` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 13 |
| `user` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 17 |
| `agent_message` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 24 |
| `tenant` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 11 |
| `video` | `created_at`，UTC TIMESTAMP(3) | `updated_at`，UTC TIMESTAMP(3) | 18 |
| `series_parse_job` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 26 |
| `video_chunk` | `created_at`，UTC TIMESTAMP(3) | `updated_at`，UTC TIMESTAMP(3) | 16 |
| `video_parse_task` | `create_time`，UTC TIMESTAMP(3) | `update_time`，UTC TIMESTAMP(3) | 27 |
| `chunk_parse_task` | `create_time`，UTC TIMESTAMP(3) | `update_time`，UTC TIMESTAMP(3) | 17 |
| `context_snapshot` | `created_at`，Unix 毫秒 | 无，历史保留 NULL | 12 |
| `index_sync_intent` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 13 |
| `parse_model_call` | `created_at`，UTC TIMESTAMP(3) | `updated_at`，UTC TIMESTAMP(3) | 26 |
| `parse_usage_attempt` | 无，历史保留 NULL | 无，历史保留 NULL | 10 |
| `upload_session` | `created_at`，UTC TIMESTAMP(3) | `updated_at`，UTC TIMESTAMP(3) | 34 |
| `video_chunk_checkpoint` | 无，历史保留 NULL | `updated_at`，Unix 毫秒 | 16 |
| `video_chunk_embedding` | `created_at`，Unix 毫秒 | `updated_at`，Unix 毫秒 | 10 |
| `video_chunk_info` | 无，历史保留 NULL | 无，历史保留 NULL | 17 |
| `video_parse_usage_attempt` | 无，历史保留 NULL | 无，历史保留 NULL | 12 |
| `chunk_parse_content` | `create_time`，UTC TIMESTAMP(3) | `update_time`，UTC TIMESTAMP(3) | 9 |
| `interval_parse_task` | `create_time`，UTC TIMESTAMP(3) | `update_time`，UTC TIMESTAMP(3) | 20 |
| `upload_segment` | `created_at`，UTC TIMESTAMP(3) | 无，历史保留 NULL | 11 |
| `interval_parse_content` | `create_time`，UTC TIMESTAMP(3) | `update_time`，UTC TIMESTAMP(3) | 7 |

媒体时间已在此前受控迁移中变为整数毫秒。本轮只把 duration_seconds、start_seconds、end_seconds 等顶层列改为 *_ms，数值不再乘 1000。video.duration_ms 继续带 `media_time_milliseconds` COMMENT。配置中的分段时长等秒单位也保持现有语义。

JSON 中的 at_seconds、保存的消息证据、请求参数和媒体 artifacts 不做递归字段替换。现有 DTO/API 继续使用原来的字段名与原类型，并由仓储显式映射；旧的媒体 *_seconds DTO 字段数值仍遵循既有毫秒合同。对数据库的直接查询需要使用新列名。

## 迁移执行与验证

1. 在隔离 MySQL 8.0.16+ 库恢复生产备份副本，先执行本脚本并验证。确认旧身份、V2/runtime 和媒体毫秒迁移均已完成，24 表源 Schema 与本脚本 source manifest 一致。
2. 停止应用、Worker、队列及其它数据库写入，释放任务租约。备份数据与 Schema，并验证恢复流程。迁移 advisory lock 只防止重复迁移进程，不能代替停止写入。
3. 使用 utf8mb4 客户端显式选择目标库，保持 foreign_key_checks=1，group_concat_max_len 至少 1024。用遇到错误即停止的 SQL 客户端执行，禁止 mysql --force。脚本设置 UTC、严格 SQL 模式，并在成功／SQL 异常时恢复这两项会话设置。
4. 脚本先检查完整旧列集合、数据类型、可空性、PK/UNIQUE/声明索引、CHECK 名称／表达式／启用状态，以及所有 FK 的来源和目标列、规则、关键类型及字符集／排序规则。还逐项检查现有数据是否满足 78 个旧 CHECK 谓词和 30 条 FK 关系，拒绝旧实例曾关闭约束检查时遗留的不合法数据。检查媒体毫秒标记、合法 User role/guest、审计时间范围及租约；任何预检失败都发生在首个业务表 ALTER 之前。
5. 捕获实际旧 FK 名称并删除约束，保持 FK 检查开启；移除旧 CHECK，逐表更名、增加公共审计列、精确转换旧时间和 User role，保留既有 PK/UNIQUE/索引定义并更名受影响的 4 个声明索引。随后增加公共／资料索引，重建全部目标 CHECK、FK。FK 重建时 MySQL 验证已有数据。
6. 末尾核对精确的 390 列目标类型／可空性、PK/UNIQUE/索引定义、CHECK 启用状态、30 个 FK 约束／31 条引用、各表行数和每一对 time/date 相等关系。只有输出 `BaseModel migration verified` 后才部署并启动新的应用代码，再检查上传、解析、聊天、用量接口。

迁移不关闭 FK 检查，不重建或清空业务表，不生成新资源 ID。InnoDB 为 FK 自动建立的普通辅助索引允许保留；声明索引与逻辑约束必须匹配目标模型。原 user.role 文本转换为 boolean 后移除原列，语义映射严格为 user→0、admin→1；旧 guest 管理员等不合法数据在预检阶段拒绝。

重复执行会因旧 Schema manifest 不匹配而明确拒绝。SQL DDL 隐式提交，步骤 5 中途异常无法靠 ROLLBACK 撤回已完成 ALTER，也不能把脚本当成幂等修复器。异常后保持写入停止，检查已经完成的步骤，按已验证备份恢复；只有经单独 Review 的恢复方案才能继续处理部分状态。回滚应用代码前必须同步恢复旧 Schema，旧版本直接连接新 Schema 不兼容。

隔离验证应覆盖：完整旧 Schema、有数据的成功迁移；媒体毫秒原值及 ID/FK 值不变；UTC 毫秒 round trip；缺失历史时间两列 NULL；合法角色映射及 nickname；完整目标结构；重复运行拒绝；缺表／缺列／列类型不符、目标列冲突、错误 FK、旧秒标记和非法角色的预检拒绝。生产数据库执行与应用重启分别按明确授权进行。

## 本轮验证记录

2026-10-10 在隔离的 MySQL 8.4.11 实例（关闭 TCP，仅临时 Unix socket）完成验证，没有执行业务数据库迁移或重启应用。

| 验证 | 结果 |
|---|---|
| 全量 `tests/server` | 1433 通过、80 跳过、12 失败；12 个失败与完整 HEAD 基线逐用例一致 |
| 最终 BaseModel、Auth、数据库守卫回归 | 168 通过、7 跳过；包括全量回归后补充的 3 个毫秒精度边界用例 |
| 最终真实 MySQL 集成 | 25 通过 |
| MySQL 审计时间插入／普通与批量更新 | 配对、UTC、毫秒精度通过，额外验证 1970／2514／9999 年边界 |
| 有数据的旧结构迁移 | 旧 24 表／323 列／29 行迁入 24 表／390 列，目标 `create_schema()` 校验通过 |
| 全行值保留 | 315 个原业务字段、所有 PK/FK、JSON、媒体毫秒数值不变；58 组审计映射与 24 个新可选资料 NULL 值正确 |
| 重复执行 | 预检明确拒绝，重新核对目标结构和完整数据通过 |
| 异常预检 | 18／18 通过；每次拒绝前后全部业务表结构与完整行快照相同 |

异常案例覆盖缺表／缺列、VARCHAR 长度与可空性错误、目标字段冲突、FK 规则／PK／声明与功能索引漂移、旧秒标记、非法 User 值、FK 孤儿、禁用 CHECK 后的坏数据、审计越界／负数、活动租约和新审计索引名冲突。MySQL 会阻止对已有违规行重新启用 CHECK，因此 CHECK 坏数据用例验证禁用约束的元数据拒绝，没有伪造“已启用但包含违规行”的状态。

职责外 TODO：`test_api.py` 的 12 个旧 Agent 用例仍使用不存在的 `get_draft_catelog`／`read_trieve_item` 工具替身；完整 HEAD 基线为同样 12 失败、51 通过。由 Agent／ContextCanvas 测试维护方更新替身，这轮字段迁移保留其现状。

## 本轮文件范围

实际修改与已确认的 50 文件范围一致：23 个生产文件、25 个测试文件、2 个文档／迁移文件，共新增 4 个、修改 46 个。

```text
[A] docs/design/base-model-field-names.md
[A] docs/migrations/20261010_base_model_field_names.sql
[M] server/database.py
[M] server/model/agent.py
[A] server/model/base.py
[M] server/model/index_sync.py
[M] server/model/job.py
[M] server/model/parse_usage.py
[M] server/model/tenant.py
[M] server/model/upload.py
[M] server/model/user.py
[M] server/model/video.py
[M] server/mq/mysql_queue.py
[M] server/mq/video_parse_queue.py
[M] server/repository/agent.py
[M] server/repository/identity.py
[M] server/repository/index_sync.py
[M] server/repository/job.py
[M] server/repository/library.py
[M] server/repository/model_usage.py
[M] server/repository/parse_usage.py
[M] server/repository/upload.py
[M] server/repository/video.py
[M] server/repository/video_parse.py
[M] server/service/auth.py
[M] tests/server/test_agent_chat_service.py
[M] tests/server/test_agent_system_integration.py
[M] tests/server/test_api.py
[M] tests/server/test_auth.py
[A] tests/server/test_base_model.py
[M] tests/server/test_cloud_access.py
[M] tests/server/test_cloud_library_access.py
[M] tests/server/test_database.py
[M] tests/server/test_elasticsearch_integration.py
[M] tests/server/test_index_sync_repository.py
[M] tests/server/test_index_sync_service.py
[M] tests/server/test_job_repository.py
[M] tests/server/test_keyframe_backfill.py
[M] tests/server/test_library_views.py
[M] tests/server/test_model_usage_repository.py
[M] tests/server/test_mq_consumer.py
[M] tests/server/test_mysql_integration.py
[M] tests/server/test_mysql_queue.py
[M] tests/server/test_realtime_understanding.py
[M] tests/server/test_repository.py
[M] tests/server/test_upload_service.py
[M] tests/server/test_video_parse_queue.py
[M] tests/server/test_video_parse_repository.py
[M] tests/server/test_video_parse_worker.py
[M] tests/server/test_worker_fencing.py
```
