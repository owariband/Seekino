# Video Parse V2：存储模型与调用链重构计划

更新时间：2026-09-27。状态：批次 0～2 已实施，并完成代码级验证、隔离 MySQL 8.4 空库建表验证及真实 MySQL 集成测试；随后按用户确认将 Agent 消息耗时附表并入 `agent_message.detail`，两套新空库已同步调整。Agent Message 的进一步字段收敛也已在两套隔离空库执行 DDL，实测结构见下文。已有库升级迁移脚本未执行，Video Parse V2 整体仍未完成。

本文交给后续实现模型使用，目标是把现有上传、任务、Worker、成果持久化接到 V2 Harness。不是重建一套上传或队列系统，也不是只把 `SeriesParseJobRecord` 全局替换成新名字。

## 1. 阅读顺序、依据与本轮边界

1. 本文中的用户最新约定优先于旧文档、旧测试及现有实现。
2. 阅读 [Video Parse V2 TRD](video-parse-harness-trd.md)，重点是 1.3.1、1.4、1.6～1.9。
3. 阅读 [Engine TRD](video-parse-engine-trd.md) 第一章及 [现有 Engine Schema](../../engine/parsev2/schemas.py)，明确目标结果合同与实际实现的差距；Harness 不自行定义视频业务结果。
4. 阅读 [Harness README](../../agent/video_parse_v2/README.md) 和实际实现；框架名称与文件布局已整理过，不要重新设计入口命名。
5. 历史交接文件位于 `/private/tmp/seekino-video-parse-v2-harness-handoff-2026-09-27.md`，仅作背景，不能覆盖本文的新约定。
6. 按下面的源码导航读取本批实际调用链，再进入第 5 节的修改树；不能只读目标表结构就开始全局替换名称。
7. 实现前遵循 [seekino-change-gate](../../.agents/skills/seekino-change-gate/SKILL.md) 与本机 [hypnos-code](/Users/bytedance/.codex/skills/hypnos-code/SKILL.md)；以用户当次授权和已确认批次为准，不重复索取已有授权。

本文初稿阶段仅维护了本文件；此后批次 0～2 已按第 7 节记录实施。两份 TRD 和 Git 跟踪规则仍未修改；新空库已按当前 ORM 建表，但已有库升级迁移仍未执行。本文及 TRD 都被 `.gitignore` 的 `**/design/` 忽略，后续模型必须直接打开，不能依赖 `git diff` 或默认 `rg` 找到它们。

### 1.1 资料根目录与冲突处理

本机项目根目录为 `/Users/bytedance/workspace/other-project/Seekino`。下表中的仓库路径均相对此根目录；Markdown 链接相对本文件，可直接打开。迁移工作区后使用新的项目根目录，不能把本机用户名写进业务代码。

- **目标行为依据**：用户最新明确决定，以及对应领域的当前 TRD。本文第 2 节记录本轮约定及具体建议，第 4 节仍标为建议/待定的内容不是用户已经批准的设计。
- **现状证据**：当前源码和测试用于判断现在能做什么、哪里需要改；旧实现不能反过来否定已确认的新约定。
- **阅读辅助**：README、流程图、交接和历史讨论帮助理解背景；与已确认约定冲突时，记录差异，不照旧说明恢复被否定的结构。
- Harness 的编排/任务合同读 Harness TRD；Engine 的结果字段/校验合同读 Engine TRD。两份 TRD 的同一事项若冲突且本轮没有明确覆盖，应指出具体条目，不把“文件更新较晚”当作唯一判断依据。
- 背景资料中的检索、embedding、发布策略不自动扩大本次任务存储重构范围。可以引用边界，不因此提前实施后续检索工程。

### 1.2 必读设计资料与历史背景路径

| 资料路径 | 读取用途 | 当前效力与注意事项 |
| --- | --- | --- |
| [docs/design/video-parse-harness-trd.md](video-parse-harness-trd.md) | Harness 主设计：任务/成果表、Prepare、调度、Listener、Refine、PrefixContext 与输入配置 | 编排与存储设计依据；第 2 节的新约定覆盖其尚未同步的字段描述。重点读 1.3.1、1.4、1.6～1.9 |
| [docs/design/video-parse-engine-trd.md](video-parse-engine-trd.md) | Engine 结果 Schema、时间/人物/Timeline 不变量，以及解析和 Retrieval Projection 的边界 | 第一章是本次接入需要的领域合同；第二章的检索规划不是当前批次的实现清单 |
| [agent/video_parse_v2/README.md](../../agent/video_parse_v2/README.md) | 当前 Harness 文件布局、入口签名、注入边界、事务/通知语义及占位范围 | 当前框架导航；实际能力仍需对照源码，不等于 DB/模型业务已经完成 |
| [engine/parsev2/schemas.py](../../engine/parsev2/schemas.py) 与 [tests/engine/parsev2/test_schemas.py](../../tests/engine/parsev2/test_schemas.py) | 已存在的 Engine Pydantic 类型、校验和测试入口 | 当前 `VideoParseResult.persons` 和 Timeline 人物引用已直接使用人名、正式时间为原视频整数秒；结果合同仍需接入实际 Harness/Worker 提交链 |
| [docs/design/parse_big_graph.png](parse_big_graph.png) 与 [docs/design/ChunkUnderstandingWorkflow.png](ChunkUnderstandingWorkflow.png) | 用户原始总流程图、单 Chunk 并行分支与汇合图 | 帮助理解拓扑；图中文字和固定数字可能早于后续决定，数量/开关/命名以当前合同核对 |
| [/private/tmp/seekino-video-parse-v2-harness-handoff-2026-09-27.md](/private/tmp/seekino-video-parse-v2-harness-handoff-2026-09-27.md) | 前置讨论、被否定的设计、框架完成范围、历史验证和工作区背景 | 本机临时历史快照，可能消失；仍有更名前路径，以当前 README/磁盘为准，不能作为唯一依赖 |
| [docs/design/refactor_1.md](refactor_1.md) 与 [engine/README.md](../../engine/README.md) | 理解为什么复用 Server 内部 Consumer，以及 Engine/Server/Gateway 的职责边界 | 主要说明已有 V1 架构；refactor_1 开头已标历史快照，不复制旧路径、旧表结构或旧索引目标作为 V2 方案 |
| [docs/design/oldvsnew_chat.md](oldvsnew_chat.md) | 查 V1 产物、旧 compact、Timeline 与新需求的背景差异 | 2026-09-24 的只读调查；用于理解旧成果转换难点，不代替当前 V2 Schema |
| [docs/design/parse-v2-harness.md](parse-v2-harness.md) 与 [engine/parsev2/README.md](../../engine/parsev2/README.md) | 补充早期 Harness/Engine 分层讨论，追溯旧设计名词 | 含历史设计或未同步描述，仅作辅助；不能据此恢复旧主线，具体差异见下文 |
| [.agents/skills/seekino-change-gate/SKILL.md](../../.agents/skills/seekino-change-gate/SKILL.md) 与 [/Users/bytedance/.codex/skills/hypnos-code/SKILL.md](/Users/bytedance/.codex/skills/hypnos-code/SKILL.md) | 实施范围、批次修改树、验证与最小改动要求 | 前者位于项目内但也被 Git 忽略，后者是本机 skill 路径；换环境后按可用 skills 定位，不能假设仅克隆仓库即可获得全部资料 |

已核对的资料差异，接手时特别注意：

1. Engine TRD 第一章明确采用 Schema-driven Harness → VideoParseResult → Engine 校验，且说明 Proposal/Patch/Canonical 不再是主线；Engine README 仍有旧主线叙述。接入时读 TRD 和现有 Schema 的差异，不照 README 创建整套旧架构。
2. Engine TRD 明确人物字段直接使用人名、正式 Timeline 使用原视频绝对整数秒；当前 `schemas.py` 已按此定义 `persons`、`participants`、`visible_persons` 和 `speaker`。正式结果不能因通用时间描述而放宽为小数秒；原始模型证据的精度处理由 Engine 合同确定。
3. 旧 handoff 仍提到 listener/listener.py、chunk_understanding/nodes.py 等路径；当前对应入口是 chunk_interval_listener.py、chunk_understanding/workflow.py，不要把已整理的目录改回去。
4. 本计划、两份 TRD 和流程图可能被 Git 忽略；历史 refactor_1.md 已被跟踪。不能从目录名推断所有 design 文件都未跟踪；必要时用 `rg --files --hidden --no-ignore` 和 `git check-ignore -v` 核对。

### 1.3 当前调用链与实现证据入口

下面不是额外修改清单，而是接手时应先读的源码入口。按函数/类定位，行号可能随重构变化。

| 要回答的问题 | 重要资料路径与入口 |
| --- | --- |
| 上传输入、配置快照和任务在哪创建？ | [server/api/uploads.py](../../server/api/uploads.py)、[server/service/upload.py](../../server/service/upload.py)、[server/repository/upload.py](../../server/repository/upload.py)：从 create_upload_session/create_session 跟到 complete_upload，并读 retry_parse/reparse |
| 当前真实表结构与外键是什么？ | [server/model/job.py](../../server/model/job.py)、[video.py](../../server/model/video.py)、[upload.py](../../server/model/upload.py)、[parse_usage.py](../../server/model/parse_usage.py)；建表与旧库门禁在 [server/database.py](../../server/database.py) |
| 已有 Worker 如何领取、续租和重试？ | [server/mq/contracts.py](../../server/mq/contracts.py)、[mysql_queue.py](../../server/mq/mysql_queue.py)、[consumer.py](../../server/mq/consumer.py)；写入时的租约条件在 [server/repository/job.py](../../server/repository/job.py) |
| 配置和模型怎样装配到实际执行？ | [server/config/parse_profile.py](../../server/config/parse_profile.py)、[server/bootstrap.py](../../server/bootstrap.py)、[server/service/parsev1/video_processing.py](../../server/service/parsev1/video_processing.py)；对照 V2 [agent/video_parse_v2/input.py](../../agent/video_parse_v2/input.py) 和 [agent.py](../../agent/video_parse_v2/agent.py) |
| 为什么旧 checkpoint 不能只改名？ | [server/service/parsev1/parse_persistence.py](../../server/service/parsev1/parse_persistence.py)、[server/service/parsev1/parse_import.py](../../server/service/parsev1/parse_import.py)、[server/repository/video.py](../../server/repository/video.py)；V1 算法入口与持久化端口见 [engine/parsev1/pipeline.py](../../engine/parsev1/pipeline.py)、[persist.py](../../engine/parsev1/persist.py) |
| V2 在哪里提交 Chunk、Interval 和 compact？ | [chunk_understanding/workflow.py](../../agent/video_parse_v2/chunk_understanding/workflow.py)、[chunk_interval_listener.py](../../agent/video_parse_v2/chunk_interval_listener.py)、[refine/interval_refine.py](../../agent/video_parse_v2/refine/interval_refine.py)、[precompact/prefix_context_agent.py](../../agent/video_parse_v2/precompact/prefix_context_agent.py)；当前均有待接入的业务步骤 |
| 改表会怎样影响查询、前端和系统时间？ | [server/repository/library.py](../../server/repository/library.py)、[server/api/jobs.py](../../server/api/jobs.py)、[server/model/vo/jobVO.py](../../server/model/vo/jobVO.py)、[server/utils/time.py](../../server/utils/time.py)、[web/src/api/contracts.ts](../../web/src/api/contracts.ts) |
| 现有测试到底验证到了哪里？ | [tests/agent/video_parse_v2/test_framework.py](../../tests/agent/video_parse_v2/test_framework.py)、[test_input.py](../../tests/agent/video_parse_v2/test_input.py) 只保护框架/配置；真实存储与并发入口是 [tests/server/test_mysql_integration.py](../../tests/server/test_mysql_integration.py)、[test_worker_fencing.py](../../tests/server/test_worker_fencing.py)；其余按第 6 节选择 |

### 1.4 数据迁移的历史依据与分批依赖

| 历史资料路径 | 本次需要从中确认什么 |
| --- | --- |
| [docs/migrations/20260918_unix_time_ms.sql](../migrations/20260918_unix_time_ms.sql) | 旧系统时间为何采用 BIGINT，以及更早的 UTC/本地时区来源差异；仅用于查来源，不能把旧转换方向当作本次 TIMESTAMP 目标，也不能原样反向执行 |
| [docs/migrations/20260922_parse_config_snapshot.sql](../migrations/20260922_parse_config_snapshot.sql) | upload_session 与旧 Job 已有配置快照列；历史记录可能为 NULL，不能用今天的默认配置伪造其历史参数 |
| [docs/migrations/20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) | processing_duration_ms 等耗时字段的来源；耗时不随绝对时间一起变成 TIMESTAMP |
| [docs/migrations/20260923_video_timeline_seconds.sql](../migrations/20260923_video_timeline_seconds.sql) | 视频时间已按秒迁移的背景，以及旧成果/索引依赖；历史脚本含当时的数据重建操作，不是本次升级可直接复用的脚本 |

脚本存在不证明目标数据库已经执行过；本轮未连接数据库，实际库结构与数据版本需在迁移准备阶段核对。

- 批次 0～2：先读两份 TRD 的相关合同、现有 ORM/database、上传创建链和历史迁移；先确定新身份、字段及旧数据边界。
- 批次 3～4：在上述基础上读 Harness 入口/回调、Worker 租约、V1 提交端口、Engine Schema 和费用记录；复用执行通道但不复制旧成果结构。
- 批次 5～6：再读 API/VO、前端 contracts、Library/检索读取方和相应测试，核对旧字段的最后一个消费者后才安排清理。
- 历史 RAGFlow/LightRAG 对照不是完成本批重构的必备外部依赖；前因可从 handoff 和 Engine TRD 的已有研究章节了解，无需在开工前重新做一轮外部调研。

### 已有能力与真实缺口

- 已有上传创建、有效配置快照、数据库队列、原子领取、租约、续租、过期回收、失败重试和 Worker 执行入口。
- 当前上传完成在 `server/repository/upload.py::complete_upload()` 创建 `SeriesParseJobRecord`；`server/bootstrap.py` 装配的执行实现仍是 `engine.parsev1.runtime.EngineVideoParseRuntime`。
- 当前 V2 是框架：此前 28 项测试只验证框架合同；交接时仍有 33 个业务占位。实施时重新核对数量，不把历史测试通过当作真实存储与模型链已通。
- 旧持久化按“整集 Job 租约 + 连续前缀 checkpoint + 整集结果导入”工作；V2 需要“本次 Video 任务 + Chunk/Interval 独立状态与成果”。
- 现有 `server.config.parse_profile.ParseConfig` 的版本号不能当作 V2 Harness 已接入的证据。V2 目标类型在 `agent/video_parse_v2/input.py`，两者的字段与配置来源必须明确适配。

## 2. 用户最新约定与建议的具体落点

### 2.1 episode_order 是有效业务输入，不能忽略

用户明确：现在 `episode_order` 不 ignore。

- 创建上传会话时，DTO → API → service → repository 必须传递并使用确认后的实际集数。
- 校验正整数、Series 归属及同 Series 集序唯一性；检查已有 Video 和仍占用集序的上传会话。
- 同一次上传重试复用已确认集数；幂等请求携带不同集数应报告冲突，不能悄悄改回服务端分配值。
- 移除 `upload_sequence`，展示与执行排序按 `episode_order`；集序冲突不再用 `next_order` 自动递增掩盖。
- TRD 原有流程仍成立：创建会话前由 Agent 识别集数，无法确定时由用户填写。当前正则猜测加递增兜底不等于已经实现 Agent 识别。
- “允许使用已确认集数”与“接入 Agent 识别入口”可分步交付，但最终必须移除无法识别时自动编造集数的行为。
- 本次检查的代码仍存在 deprecated/ignored 描述及忽略行为；用户这次说明是目标约定，不能写成代码已经改好。

### 2.2 Interval 用最左侧 Chunk 作身份锚点，范围左闭右开

用户已澄清，`interval_parse_task.chunk_parse_task_id` 不是多余的成员引用，而是该 Interval 最左侧 Chunk 的解析任务 ID。

```text
chunk_parse_task_id  -> chunk_parse_task.id  （最左侧成员、身份锚点）
video_parse_task_id  -> video_parse_task.id  （所属本次解析）
start_sequence_no   = 最左侧成员对应的 video_chunk.sequence_no
区间成员序号范围     = [start_sequence_no, start_sequence_no + chunk_count)
```

- 保留 Interval 自身 `id` 主键，以及 TRD 的 `(video_parse_task_id, start_sequence_no)` 唯一约束；“锚点”不等于擅自把两种任务的主键设成同一 ID。
- 补齐锚点外键，并校验锚点属于同一个父解析任务。重试和恢复必须找回同一个 Interval。
- 右端不属于本 Interval。`[3, 6)` 包含序号 3、4、5，不包含 6。
- 对应的视频时间覆盖为 `[首个 Chunk.start_seconds, 最后一个 Chunk.end_seconds)`，使用原视频绝对秒，右端不包含；不要把成员序号当时间，也无需额外持久化可推导的时间列。
- `chunk_count` 保持 1～63；成员不足一个完整窗口时不创建 Interval。窗口生成规则按 TRD 和 Engine，不因增加列表改变。

### 2.3 建议增加有序成员列表：chunk_parse_task_ids

用户推荐增加成员 Chunk ID list；本计划赞成，建议具体字段为：

```text
chunk_parse_task_ids JSON NOT NULL  本次 Interval 的成员 Chunk 解析任务 ID，按 sequence_no 升序
```

使用 `chunk_parse_task_ids` 而不是含义不明的 `chunk_ids`：它关联本次执行与成果，直接对应 RefineInput 的成员 key；固定拓扑 ID 仍叫 `video_chunk_id`。这是对用户建议的字段命名细化，并非旧 TRD 已存在的列。

此列表可由父任务、起点和数量推导，确实存在冗余。保留它的理由是显式记录固定成员身份，使 Refine 取数、回调核对和排障更直接；不宣称它消除了所有查询或天然提高性能。每个窗口最多 63 个成员，创建后不可变，可以把一致性校验集中在初始化事务。

创建与迁移时必须验证：

1. 数组长度等于 `chunk_count`，ID 唯一，所有成员存在。
2. `chunk_parse_task_ids[0] == chunk_parse_task_id`。
3. 所有成员属于同一个 `video_parse_task_id`。
4. 第 i 项的 `video_chunk.sequence_no == start_sequence_no + i`，无缺口、无乱序。
5. 重试、恢复和 Refine 不增删成员、不重排列表；数据库查询结果必须按该顺序或 ID 映射整理。

JSON 元素没有普通外键约束；应用层必须在创建事务验证。不能让列表和起点范围成为两套独立可编辑的成员定义，也不额外新增 Interval-Member 表。

`ready_mask` 的映射固定：第 i 位对应列表第 i 项，亦即序号 `start_sequence_no + i`。满位条件为 `(1 << chunk_count) - 1`。重复完成通知仅重复按位 OR，不能重复增加完成数量。

### 2.4 PrefixContext 的 compact 成果放在 Chunk 成果 detail

用户明确 compact 成果放在 Chunk 的 `detail`。在任务/成果已拆表的设计下，本计划将其具体落实到：

```text
chunk_parse_content.detail
```

- `video_chunk` 只存固定拓扑，没有 detail；不能为了 compact 把解析版本相关成果放回拓扑表。
- `chunk_parse_task.detail` 是任务扩展数据；compact 是成果，存入 `chunk_parse_content.detail`。
- 建议在该 JSON 中给 compact 留明确的命名空间，合并写入，不覆盖原有模型配置、原始识别信息等键。最终 compact 结构和校验仍由 Engine 定义，Harness 不另造结果 Schema。
- `series_prefix_compact`、`video_prefix_compact` 的覆盖范围必须明确：输入是当前目标之前的背景；处理后的滚动产物供后续目标使用。不能把包含当前 Chunk 的产物反过来当当前 Chunk 的前置输入。
- 保存 compact、可选内容修正及推进处理位置必须同一事务完成，校验独立后台租约及读取时的内容版本。需定义 detail 更新所用的并发版本规则，禁止全量替换 JSON 导致并发丢字段。
- compact 关闭或失败不阻止主 Video 任务完成；后台按 Series 内 `(episode_order, sequence_no)` 升序处理，不跳过缺口。

文档核对结论：旧 TRD 写过“维护 compact 字段”、两类 compact 和保存要求，也定义了 content.detail，但没有明确把两者连起来。本文补上对应关系，不再把“compact 成果存在哪里”列为未决问题。

后台游标、租约、固定前序任务集合属于执行元数据，不等于 compact 成果。它们仍需落实到持久化字段，但不要因此新建一套 checkpoint 系统；具体落点见第 4 节待定项。

### 2.5 系统时间用 TIMESTAMP，视频内部用秒

用户明确：系统级时间存 `TIMESTAMP`，只在传给前端的 VO 边界转 Unix 毫秒；视频内部时间用秒。

| 数据 | 数据库与内部处理 | 前端边界 |
| --- | --- | --- |
| 创建、更新、开始、完成、租约到期、下次执行等绝对时间 | `TIMESTAMP`；建议统一毫秒精度 `TIMESTAMP(3)`，内部使用 UTC datetime | VO 输出 Unix 毫秒整数，保留可空语义 |
| 视频时长、Chunk 起止、ASR/OCR/Event 时间位置 | 秒；按对应 Schema 使用整数或允许的小数 | 仍为秒，不转成 Unix 时间 |
| 重试延迟、分发间隔、处理耗时 | 时长，沿用明确单位或 timedelta | 不是绝对时间，不能改成 TIMESTAMP |

- 这条最新约定覆盖旧 TRD 中 Video 的 BIGINT 系统时间描述；不能再沿用“Video 必须保留毫秒列”的旧结论。
- 迁移按具体表和列执行，覆盖本次 Video、Upload、任务、成果及调用记录中实际切换的系统时间。未列入批次的 Chat、Series 等域另行对齐，不靠修改共享 `TimestampedRecord` 隐式连带迁移。
- 本次内部 DTO、repository、queue 的 `now`、到期比较及时间运算一并改为 datetime/timedelta；不能只改 SQL 类型，内部仍写毫秒整数。
- 数据库连接、读取归一化和 VO 转换都明确 UTC；MySQL 返回 naive datetime 时，在受控边界按数据库 UTC 约定归一化，不能依赖机器本地时区。
- 复用 `server/utils/time.py::datetime_to_unix_time_ms()` 在 VO 投影处转换；没有新职责就不新增时间包装层。
- 历史毫秒值迁移需要显式转换，保留 NULL 与毫秒精度，检查目标数据库支持的日期范围。系统时间列名是否保留原名按目标表定义处理，不全局机械改名。
- `server/database.py` 对毫秒列的类型门禁必须按已迁移表改成 TIMESTAMP 检查，未迁移域仍保留原检查；不能删除整套 Schema 校验来让启动通过。

## 3. 目标模型、命名与不可混淆的版本

| 表 | ORM 建议名 | 职责 |
| --- | --- | --- |
| `video` | `VideoRecord` | 原视频基础信息、实际集数；关联多次解析任务 |
| `upload_session` | `UploadSessionRecord` | 上传状态、确认集数、配置及解析任务关联；detail.progress 可选 |
| `video_chunk` | `VideoChunkRecord` | 可复用的固定拓扑，只有序号和秒级边界等基础字段 |
| `video_parse_task` | `VideoParseTaskRecord` | 本次解析身份、固定配置、解析版本及整体状态 |
| `chunk_parse_task` | `ChunkParseTaskRecord` | 单 Chunk 执行状态、重试、租约 |
| `chunk_parse_content` | `ChunkParseContentRecord` | 原始/最终内容及内容版本，detail 保存 compact 等成果扩展 |
| `interval_parse_task` | `IntervalParseTaskRecord` | 锚点、固定成员、mask、Refine 执行状态与租约 |
| `interval_parse_content` | `IntervalParseContentRecord` | 区间级成果 |

暂按现有 `server/model/job.py` 及 `server/repository/job.py` 标记改造点；实现批次可以明确改为 `parse_task.py`，但不把所有同名 service/API/DTO/页面顺手全局改名。新增具体文件必须补到该批修改树，不能用一个大 Manager 隐藏原有职责。

三类版本必须分开：

- `video_parse_task.version`：同 Video 第几次解析；新任务增加，领取、续租、进度变化不增加。
- 内容 `version`：Chunk 内容的并发写入版本，Refine 和后台微调读取后据此校验。
- UI/SSE 更新版本：现有 Job 用于事件更新、条件重试的变化标识；不能直接复用解析版本。具体承载方式在切换查询前确定。

`pipeline_version` 表示实现/配置相关版本，不等于解析次数。`derive_parse_job_id(video_id, pipeline_version)` 会折叠同一视频的重复解析，不能原样保留为新任务生成规则。

失败重试继续同一个 `video_parse_task_id`，保留已完成成果。用户主动重新解析或更改本次配置时新建任务和配置快照，不能清空旧任务 checkpoint 后当作新解析。新任务版本分配需在事务中处理并发请求与唯一约束。

主状态按 TRD：Video 为 `PENDING/PREPARING/RUNNING/COMPLETE/FAILED`；Chunk 为 `QUEUED/UNDERSTANDING/COMPLETE/FAILED`；Interval 为 `WAITING/REFINING/COMPLETE/FAILED`。可重试任务的回退、退避和租约清理必须定义，不能照搬旧 `stage` 枚举硬套。

## 4. 真正仍需决定的事项

以下仅阻挡依赖它们的批次。不要把已明确的集序、Interval 锚点、compact 成果位置和时间单位重复拿来询问用户，也不能把下面的建议冒充已确认设计。

| 事项 | 缺口与推荐处理 | 何时必须明确 |
| --- | --- | --- |
| 主任务执行权与恢复 | 新主任务 DDL 没列父租约/重试字段。建议复用现有主执行领取与续租机制，明确字段去向；Chunk/Interval 的独立租约不能自动替代 Prepare 和 Scheduler 的执行权 | 创建可运行 Schema、切换 Worker 前 |
| PENDING 的 chunk_count | TRD 要求 >0，Prepare 才固定拓扑。建议创建任务时从已有拓扑取数量，或按已知时长与配置计算规划数量，Prepare 按同一规则验证；不可随意填 1 或悄悄取消约束 | 任务创建前 |
| content 行数与版本 | TRD 未列一任务单行还是多版本历史。建议每个 Chunk 任务一条当前成果、唯一 task FK、version 作并发控制；跨解析历史由新任务保存。若需同任务内容历史，另明确唯一约束和读取规则 | Content DDL 和 Refine 提交前 |
| Prefix 执行元数据 | 固定前序 task IDs、next_chunk_sequence_no、独立租约尚未指定字段。优先明确放到已有任务的结构化字段/detail 中；如使用 JSON，也必须支持原子领取与条件更新，不新增 checkpoint 系统 | 后台 compact 开启前 |
| SSE / 条件修改版本 | 决定独立 revision 等变化标识如何保存/生成；保留 heartbeat 不触发无意义 UI 更新等现有行为 | API 与 SSE 切换前 |
| 旧数据转换策略 | 确定保留历史、转换旧成果、过渡读取及旧表最终清理的批次。不存在可自动推导的 V1→V2 Engine 内容映射；不允许伪造 COMPLETE 数据或擅自清库 | 编写可执行迁移、删除旧结构前 |

模型配置快照还需检查：默认值补齐后保存的是本次有效配置；恢复不能偷偷使用最新默认值。模型实例由应用装配层注入，模型选择的固定信息沿用上游已有机制，不能序列化活实例。旧 V1 配置是否可转换要显式验证，不能因名字都叫 ParseConfig 就直接反序列化。

## 5. 分批实施与完成条件

每批开工前更新实际修改树、行数估算与验证命令；下列文件是依据当前源码确认的影响范围，M 是预计修改，A 是拟新增，均不代表已经实施。跨批文件只在首次出现处完整列出，后续说明复用。

### 批次 0：冻结剩余存储合同与迁移边界

- 解决第 4 节中本批依赖项，补齐各表字段、类型、唯一约束、外键、状态迁移和兼容关系。
- 明确保留/转换/清理哪些旧行，迁移后旧 API 的 jobId 指向什么；主任务删除或软删除时如何处理子任务、旧 Worker 与后台调用。
- 时间迁移列清单包括所触及上传分片和 usage 记录，不以“基类改了”代替逐表核对。
- 将本文件的新约定同步到 TRD 应作为后续明确文档步骤；本轮没有修改 TRD。
- 验收：不靠猜测即可写出主任务领取、创建、重试及 Interval 初始化事务。

### 批次 1：任务/成果模型、身份与迁移脚本

```text
Seekino/
├── server/
│   ├── [M] database.py                 ← 注册新模型；按迁移范围校验 TIMESTAMP
│   ├── [M] identity.py                 ← 新解析任务身份；保留已有稳定 Video/Chunk ID
│   └── model/
│       ├── [M] job.py                  ← Video/Chunk/Interval 任务、成果和状态定义
│       ├── [M] video.py                ← 基础信息/拓扑与版本化成果分离
│       ├── [M] upload.py               ← 新任务关联、detail、集序和系统时间
│       └── [M] parse_usage.py          ← 新任务外键、执行归属和系统时间
└── docs/
    └── migrations/
        └── [A] 20260927_video_parse_v2_tasks.sql ← 建议名；按最终迁移批次拆分
```

预期影响：新模型可建表且约束正确。调整被引用的旧表/主键时，上传、checkpoint、usage 的外键必须同时处理，不能只改一个 __tablename__。已有库的升级不由 `Base.metadata.create_all()` 自动完成。新增迁移不编辑历史 SQL；写脚本不等于已获授权执行真实数据库迁移。

若批次需要先并存新旧结构，必须明确哪条链仍读旧模型；不能中间就删旧列再等后续批次修读者。schema 切换和依赖代码应作为可运行的一组交付。

### 批次 2：上传输入、配置快照、任务创建与重解析

```text
Seekino/
└── server/
    ├── config/
    │   └── [M] parse_profile.py         ← 核对旧配置与 V2 固定配置的边界
    ├── model/
    │   └── dto/
    │       ├── [M] uploadDTO.py         ← episode_order 有效；上传返回关联新任务
    │       └── [M] jobDTO.py            ← 新任务状态、版本、datetime 执行数据
    ├── api/
    │   └── [M] uploads.py               ← 传入已确认集数，完成/重试/重解析投影
    ├── service/
    │   └── [M] upload.py                ← 验证集序、固定配置和创建语义
    └── repository/
        ├── [M] upload.py                ← 原子完成上传、新建任务、重解析与幂等
        └── [M] job.py                   ← 新任务取数、最新版本和重试查询
```

预期影响：上传完成创建 PENDING 任务；失败重试复用 ID，主动重解析返回新 ID。新任务失败不能让历史已成功成果被覆盖或消失。配置快照缺失、与已有固定拓扑不兼容时明确拒绝，不能临时改拓扑或静默换默认值。

先让已确认 episode_order 真正贯穿调用链；Agent 识别入口与人工补填在批次 5 交付。不得把无法识别的上传用递增集数悄悄放行。

### 批次 3：复用 Worker 通道，接入 V2 与事务持久化

```text
Seekino/
├── server/
│   ├── [M] bootstrap.py                ← 装配模型、Engine、DB 操作与共享 Listener
│   ├── mq/
│   │   ├── [M] contracts.py             ← 主任务及子任务执行/租约合同
│   │   ├── [M] mysql_queue.py           ← 原子领取、续租、退避、回收与实际集序
│   │   └── [M] consumer.py              ← 调用 V2、取消及失败处理
│   ├── repository/
│   │   └── [M] video.py                 ← 拓扑复用及逐任务成果事务
│   └── service/
│       ├── [M] video_processing.py      ← 现有执行入口接 ParseAgent
│       └── [M] parse_persistence.py     ← 替换连续前缀恢复与整集一次性提交
└── agent/
    └── video_parse_v2/
        ├── [M] agent.py                ← 主任务完成/失败收尾与依赖注入
        ├── [M] video_parse_prepare.py  ← 读取固定输入、校验执行权、固定拓扑与子任务
        ├── [M] chunk_scheduler.py      ← 按 DB 状态恢复、90% 批次推进和全局活跃上限
        ├── [M] chunk_interval_listener.py ← 同事务 mask 回调与提交后通知
        ├── chunk_understanding/
        │   └── [M] workflow.py         ← 输入准备、模型/Engine 校验、原子成果提交
        └── refine/
            └── [M] interval_refine.py  ← 按固定成员读取版本、模型纠偏及事务提交
```

预期影响与硬性边界：

- ParseAgent 仍是入口；Prepare 仍是主图单节点，ChunkUnderstandingWorkflow 仍是子图。仅有 RunState 与 ChunkState 是 Graph State。
- `_load_input(video_parse_task_id)` 接已有任务和执行上下文；不新建平行上传通道，不为了接入造独立服务进程。
- 应用层依赖注入保持明确：共享 Listener 不能保存某个任务的可变 session/事务字段。mask 操作必须使用本次提交事务，同时保证并发任务不串事务；具体事务传递方式在实现前说明。
- Chunk 成果、相关 Interval mask 以及 Chunk COMPLETE 在同一事务提交；提交后通知 Worker。通知失败由扫描补偿，不回退已完成状态或再次调用模型。
- Interval 列表、起点范围和锚点在 Prepare 中幂等初始化。Refine 读取所有成员内容版本；提交时校验租约及全部版本，原子保存成员更新、区间成果和 Interval COMPLETE；冲突后重读重算。
- Chunk 可乱序完成，恢复按 DB 状态，不再要求完成结果形成连续前缀。有效 UNDERSTANDING 租约占用并发名额。
- 批次达到实际成员数量的 90% 完成才开放下一批；总活跃上限覆盖所有已开放批次，不能只看 active_count 下降。
- 模型调用在数据库事务外执行；所有调用经过共享预算、并发和取消控制。Engine 定义并校验业务结果 Schema。
- 临时媒体属于 Task/Lease 运行时，临时路径不得进入 DB、Checkpoint 或 Graph State，包括 content.detail。媒体消费结束、完成、取消或租约丢失时主动清理；强杀遗留由 Worker 启动及周期 Reaper 确认租约失效后清理。无需为了该规则构造多层 Runtime/Manager 包装。
- 主任务 COMPLETE 取决于要求执行的 Chunk 和 Interval；不依赖 compact、embedding 或索引完成。

实际 Engine/模型适配文件需根据已找到的同类实现另列最小修改树。若相应 Engine 合同还不存在，先报告缺口；不能让 Harness 猜结果结构来绕过它，也不能把存储联通冒充完整模型解析已完成。

### 批次 4：Prefix compact 与费用归属

```text
Seekino/
├── agent/
│   └── video_parse_v2/
│       └── precompact/
│           ├── [M] prefix_context_agent.py ← 读取前置成果、独立租约、同事务写回
│           ├── [M] context.py              ← 仅在已确认输入合同需要时调整
│           └── [M] tools.py                ← Engine 允许的当前目标微调
└── server/
    ├── model/
    │   └── dto/
    │       └── [M] parseUsageDTO.py     ← 主解析/子任务/执行租约的归属
    ├── repository/
    │   └── [M] parse_usage.py           ← 新任务关联、去重和汇总
    └── service/
        └── [M] parse_usage.py           ← 调用记录与完整性口径
```

复用前述 model、repository、queue、bootstrap 和 Listener 改造点。费用 FK 与基本写入在批次 1～3 就必须可用，不能运行真实收费调用后等本批才补记录；本批完善子任务聚合与后台调用口径。

compact 遵循 2.4：成果入 `chunk_parse_content.detail`；后台执行位置、独立租约和固定前序任务先明确承载字段。保存后唤醒下一位置，漏通知靠扫描恢复；同 Series 串行，不同 Series 可并行。

费用记录不能只把 job_id 改名：原先 attempt_count/lease_version 是整集执行身份，新调用需记录实际 Chunk/Interval/Prefix 执行归属，并能按父解析任务汇总。失败、取消或失去租约后已发生的模型调用仍需可追踪，重试不能重复计费记录。

### 批次 5：查询、VO 时间转换与前端上传/任务展示

```text
Seekino/
├── server/
│   ├── model/
│   │   ├── [M] job_progress.py          ← 新任务 detail/完成计数的展示投影
│   │   ├── dto/
│   │   │   └── [M] libraryDTO.py        ← 新任务关联、实际集序、内部 datetime
│   │   └── vo/
│   │       ├── [M] jobVO.py             ← 新状态与进度，系统时间输出毫秒
│   │       ├── [M] uploadVO.py          ← 移除 uploadSequence，时间转换
│   │       ├── [M] libraryVO.py         ← 基础信息与聚合状态，时间转换
│   │       └── [M] parseUsageVO.py      ← 仅在新任务归属展示合同确需变化时调整
│   ├── repository/
│   │   └── [M] library.py              ← 最新任务与可用历史成果、状态和封面来源
│   ├── service/
│   │   └── [M] job.py                  ← 任务查询、重试和详情
│   └── api/
│       ├── [M] jobs.py                 ← 新任务投影、独立 SSE 更新标识
│       ├── [M] library.py              ← Video 展示投影及时间转换
│       └── [M] parse_usage.py          ← 仅在费用展示合同确需变化时调整投影
└── web/
    └── src/
        ├── api/
        │   ├── [M] contracts.ts        ← 集序、新状态和已明确的响应合同
        │   └── [M] client.ts           ← 上传输入、识别入口及条件重试合同
        ├── shared/
        │   └── [M] format.ts           ← 新状态文案；移除错误的集序兜底调用
        └── features/
            ├── uploads/
            │   ├── [M] UploadDialog.tsx    ← 识别/人工确认集序后再建会话
            │   ├── [M] UploadProvider.tsx  ← 重试复用集序、新任务终态和 SSE
            │   └── [M] upload-types.ts     ← 未确认集序和上传阶段的实际类型
            ├── tasks/
            │   ├── [M] TasksPage.tsx        ← 新任务状态与重解析身份
            │   └── [M] ParseUsageDetails.tsx ← 费用与执行归属展示
            ├── usage/
            │   └── [M] UsagePage.tsx        ← 新任务状态及费用聚合
            ├── library/
            │   └── [M] LibraryPage.tsx      ← 若直接使用旧状态枚举则适配
            └── player/
                ├── [M] WatchPage.tsx        ← 只按实际 episodeOrder 排序
                └── [M] WatchHomePage.tsx    ← 移除 uploadSequence 排序兜底
```

预期影响：episode_order 不再被服务端忽略；识别不确定时阻止创建并允许填写；同一次上传重试复用确认值。识别 API 的新增文件/路由必须在这批开工树中具体列出，不能将正则函数包装后宣称 Agent 识别完成。

`server/api/uploads.py` 在批次 2 已列，VO 组装的时间转换同批补齐。若 `server/utils/time.py` 的现有转换已足够，复用即可，不为凑结构修改文件。

当前 ParseUsageVO 是聚合视图，没有调用时间字段；不要为时间统一新增没有消费者的字段。若保留 jobId 映射且展示合同不变，上述费用 API/VO 可不改。

接口原则：

- `/api/jobs` URL 不因换表自动改名，jobId 可暂作为新解析任务 ID 的产品投影；若决定连 wire name 一起统一，先明确合同并列全调用者，不只改后端。
- 新状态直接暴露时，前端终态判断必须包含 COMPLETE；不能只更新文字，轮询/SSE 仍等待旧 SUCCEEDED。
- Video 表不再存状态/进度，但 VO 可以从上传与解析任务聚合展示；不是删除页面进度条。
- 最新解析任务不一定是最新可用成果。新一轮失败不能让已成功的旧结果失去展示与查询入口，选择规则需明确。
- Video 的 filename/source_uri 列改名不等于全局替换上传输入 originalFilename；原视频 URI 继续通过既有媒体权限与服务处理。

### 批次 6：旧成果读取兼容及最终清理

```text
Seekino/
├── server/
│   ├── [M] cli.py                      ← 旧 import-parse 的兼容或明确停用路径
│   ├── model/
│   │   └── dto/
│   │       ├── [M] parseDTO.py          ← 旧整集导入合同，按切换方案调整
│   │       └── [M] keyframeDTO.py       ← 旧持久化媒体引用的替代输入
│   ├── repository/
│   │   └── [M] index_sync.py            ← 仅处理旧表读取兼容，不开发 V2 索引
│   └── service/
│       ├── [M] parse_import.py          ← V1 导入与新成果存储边界
│       ├── [M] keyframe_backfill.py     ← 从原视频和秒级边界准备所需媒体
│       └── [M] media.py                 ← 仅在基础信息 DTO 字段变化时适配
└── engine/
    └── retrieval/
        └── [M] contracts.py             ← 仅在旧 evidence 字段实际被替换时调整
```

`server/repository/video.py` 和 `library.py` 的读取也必须一起核对：ASR 检索、evidence hydrate、封面仍依赖旧 Chunk transcript/keyframes、Info/Embedding 等结构。不能一边删列，一边用“索引以后开发”跳过当前读取方。

本批可在明确保留旧结构时后置；若批次 1 就物理删旧字段/表，本批兼容工作必须前移。V2 embedding/index 仍不在本计划的实现目标内，不创建 TRD 可选的三张索引业务表来扩大范围。旧 Checkpoint/Info/Snapshot 的删除时间取决于迁移与读者切换证据，不预先批量删除。

## 6. 验证范围与验收标准

测试必须保护行为变化，不能只把旧类名、字段名替换后继续断言旧语义。以下是预计需要检查和按批次修改的现有测试；仅被路径搜索命中的 fixture，不应顺便重构测试架构。

```text
Seekino/
├── tests/
│   ├── agent/
│   │   └── video_parse_v2/
│   │       ├── [M] test_framework.py
│   │       └── [M] test_input.py
│   ├── server/
│   │   ├── [M] conftest.py
│   │   ├── [M] test_database.py
│   │   ├── [M] test_config.py
│   │   ├── [M] test_bootstrap.py
│   │   ├── [M] test_upload_service.py
│   │   ├── [M] test_job_repository.py
│   │   ├── [M] test_mysql_queue.py
│   │   ├── [M] test_mq_consumer.py
│   │   ├── [M] test_worker_fencing.py
│   │   ├── [M] test_parse_persistence.py
│   │   ├── [M] test_video_processing_service.py
│   │   ├── [M] test_repository.py
│   │   ├── [M] test_parse_usage.py
│   │   ├── [M] test_library_views.py
│   │   ├── [M] test_api.py
│   │   ├── [M] test_sse_events.py
│   │   ├── [M] test_media_views.py
│   │   ├── [M] test_mysql_integration.py
│   │   ├── [M] test_video_retrieval_service.py
│   │   ├── [M] test_keyframe_backfill.py
│   │   ├── [M] test_index_sync_repository.py
│   │   └── [M] test_index_sync_service.py
│   └── engine/
│       └── [M] test_video_retrieval.py
└── web/
    └── src/
        ├── app/
        │   └── [M] App.test.tsx
        ├── api/
        │   └── [M] client.test.ts
        ├── shared/
        │   └── [M] format.test.ts
        └── features/
            ├── uploads/
            │   ├── [M] UploadProvider.test.tsx
            │   └── [M] upload-types.test.ts
            ├── tasks/
            │   ├── [M] TasksPage.test.tsx
            │   └── [M] ParseUsageDetails.test.tsx
            ├── usage/
            │   └── [M] UsagePage.test.tsx
            └── player/
                ├── [M] WatchPage.test.tsx
                ├── [M] WatchHomePage.test.tsx
                └── [M] VideoPlayer.test.tsx
```

必须覆盖的验收场景：

1. 上传实际使用指定集序；同 Series 冲突失败；并发创建、幂等重放和修改集序均不偷偷分配新序号。
2. 上传完成幂等创建任务；失败重试 ID 不变；主动重解析新建 ID 与 version，旧成果保留；恢复使用原配置快照。
3. Interval `[3,6)` 仅含 3/4/5；列表与锚点、父任务、数量和顺序不一致时拒绝；不足窗口不创建；重复 Prepare 不改变成员。
4. Chunk 成果/mask/COMPLETE 同时提交或同时回滚；重复回调不重复计数；未提交结果不能使 Interval 就绪。
5. 并发 Worker 只有一个领取成功；旧租约不能提交；回收后按退避恢复；乱序完成及进程重启不要求连续前缀。
6. 重叠 Interval 的内容版本冲突不会造成部分提交或旧结果覆盖；回调失败不会重新运行已完成模型步骤。
7. compact 进入 content.detail 且保留其他键；不会把当前内容当成自身前置背景；失败保留位置，恢复不跳过缺口，独立租约与内容版本失效时不能提交。
8. 系统时间经数据库 UTC TIMESTAMP 往返后，VO 毫秒值和 NULL 正确；租约边界比较不受本机时区影响；视频起止秒数不变。
9. SSE 能观察同一解析任务的多次状态变化；heartbeat 不制造无意义更新；前端 COMPLETE 后停止等待；新解析失败仍可使用旧成果。
10. 模型调用归属于正确的父任务和子任务执行；重试去重，失败/取消后的实际调用仍可追踪。
11. Schema 门禁能识别未升级旧库；迁移前后记录、外键、版本归属可核对，不能仅凭 SQLite 单测宣称 MySQL 租约和 TIMESTAMP 已验证。

按批次选择现有测试。例如在项目根目录运行：

```bash
.venv/bin/python -B -m pytest tests/agent/video_parse_v2 -q
.venv/bin/python -B -m pytest tests/server/test_database.py tests/server/test_upload_service.py tests/server/test_job_repository.py -q
.venv/bin/python -B -m pytest tests/server/test_mysql_queue.py tests/server/test_mq_consumer.py tests/server/test_worker_fencing.py -q
.venv/bin/python -B -m pytest tests/server/test_parse_persistence.py tests/server/test_video_processing_service.py tests/server/test_parse_usage.py -q
.venv/bin/python -B -m pytest tests/server/test_api.py tests/server/test_library_views.py tests/server/test_sse_events.py -q
npm --prefix web run typecheck
npm --prefix web test
git diff --check
```

MySQL 集成测试使用既有 `SEEKINO_TEST_DATABASE_URL`，部分还需要索引环境；只能使用明确的测试环境。未配置时报告 skipped/未验证，不能当作通过，也不能自动借用用户业务库。以上命令是各批次的验证入口；批次 0～2 的实际执行结果见第 7 节。

## 7. 范围、估算与交付记录

- 本计划是多批次路线，不是一次性全仓库改名授权。预计数十个生产文件与测试文件受影响；完整任务存储切换及业务接入为数千行量级，不能沿用纯命名重构的工作量。
- 文件树中的 conditional 项应在对应批次逐文件确认；若不改即可完成该批目标，则移出执行范围。代码行数、M/A/D/R 数量和文档/迁移数量在每批冻结实际树后单独估算，不拿全路线粗估当精确承诺。
- 对数据库 Schema、持久化数据、配置兼容、API 状态和前端交互都有影响。部署和实际迁移须作为单独动作确认；本计划不执行迁移、重启、commit 或 push。
- 暂无计划批量删除生产文件。新模型文件拆分、识别入口、Engine 适配等尚未固定的文件，必须在各自批次补上具体路径与必要性，不能凭本文自行扩建框架。

每批完成后在此追加：实际文件树和行数、实现了哪些行为、测试通过/失败/跳过数量、是否使用真实 DB 或模型、剩余占位与真实缺口。没有真实模型与持久化验收，不能写“V2 全链路完成”。

### 7.1 2026-09-27 批次 0～2 实际交付记录

本批完成任务/成果 Schema、上传创建链、固定配置读取以及 retry/reparse 身份语义。初次上传完成仍同时创建既有 V1 `SeriesParseJobRecord`，使当前 V1 Worker 链可继续工作；同一事务新增 V2 `VideoParseTaskRecord`，作为后续 V2 执行接入的稳定身份。本批没有把 V2 任务交给 Worker 或 Harness 真正执行，因此不能据此宣称 Video Parse V2 全链路完成。

#### 已冻结并落地的存储合同

- 主任务拥有独立的领取与恢复字段：`next_execute_time`、`attempt_count`、`max_attempts`、`lease_owner`、`lease_version`、`lease_expire_time`、`heartbeat_time`，并通过状态/租约约束避免运行态与租约信息互相矛盾。Chunk 与 Interval 仍拥有各自的执行状态、重试和租约字段；批次 3 再接真实领取、续租与提交。
- `video_parse_task.chunk_count` 在创建任务时根据 Video 时长和本次固定 `chunk_duration` 规划为正整数。Video 已存在固定 `video_chunk` 拓扑时，创建或重解析必须与其数量及秒级边界完全一致，否则拒绝；不会填占位值或静默重写拓扑。
- 一个 `chunk_parse_task` 最多有一行当前 `chunk_parse_content`，由唯一任务外键保证；`chunk_parse_content.version` 用作同一成果行的并发版本。不同解析轮次的历史由不同 `video_parse_task` 及其子任务保存。
- `video_parse_task.version` 只表示同一 Video 的解析轮次，内容 `version` 只负责内容并发控制。本批没有给 V2 Task 引入独立 `revision`，也没有把解析版本兼作 UI/SSE 更新标识；该更新标识仍按第 3、4 节保留为后续接口切换前必须解决的事项。
- V1/V2 采用并存切换。初次上传保留旧 Job ID 和旧执行链，同时记录 `initial_video_parse_task_id`；V2 retry/reparse 不重置旧 Job、不删除 checkpoint，也不覆盖旧 V2 任务或成果。历史 V1 Job/成果没有可靠的 V2 Engine 映射，因此迁移脚本和运行时都不合成伪 V2 COMPLETE 任务；历史已完成上传可以没有 V2 任务关联。
- Interval Schema 已落地最左成员锚点、同父任务复合外键、有序 `chunk_parse_task_ids`、左闭右开序号范围及 ready-mask 校验函数。它们保护批次 3 初始化和提交所需合同；本批没有创建或执行 Interval 子任务。compact 的目标列为 `chunk_parse_content.detail`，Prefix 后台游标和租约仍留到批次 4，不把执行元数据伪装成 compact 成果。
- 本批触及的 Video、Upload、V2 Task/Content 和 usage 绝对时间列显式使用 `TIMESTAMP(3)`/UTC datetime，MySQL 会话固定为 `+00:00`，VO 边界才转换为 Unix 毫秒。Series、旧 V1 Job/checkpoint 等未列入范围的表继续沿用原类型，没有修改共享时间基类实施隐式全库迁移；视频内部起止值仍是整数秒，时长仍是数值。

#### 已实现行为

- `episode_order` 已贯穿 DTO、API、service、repository 和排序调用者，校验正整数及 32 位上限，并检查同 Series 的 Video/有效上传占位冲突。幂等重放必须使用首次确认值，修改集序会返回冲突；兼容列 `upload_sequence` 暂时与确认值保持一致，未再用 `next_order` 掩盖冲突。
- 上传会话保存规范化后的 `video_parse_config`。调用者可以显式传入完整配置；没有显式值时，代码从当前运行配置生成一份可见的 V2 运行默认快照。这些默认值只是当前接入策略，不是已确认的产品默认。完成上传时必须使用已持久化快照；快照缺失、V1/V2 `chunk_duration` 不一致或固定拓扑不兼容都会明确失败，不会在恢复时读取新的默认值。
- 上传完成在一个事务内创建 Video、一个 V2 PENDING 任务、既有 V1 Job 及上传关联；重复完成返回原任务，不会重复创建。Repository 可按 ID 读取 V2 Task，并一次取得 Task、Video 和固定配置，也可读取同一 Video 的最新任务及版本历史。
- 失败 retry 依靠任务状态和事务行锁校验，只接受最新 FAILED V2 任务，复用任务 ID、解析 `version`、固定配置与既有成果，并清理本次失败/租约状态；它不增加解析版本，也不维护另一套 task revision。当前 wire 仍保留名为 `expectedJobVersion` 的过渡参数，retry 和主动 reparse 都将它与最新任务的解析 `version` 比较，用于拒绝跨解析轮次到达的旧请求。reparse 成功时在 `(video_id, version)` 唯一约束下创建新任务 ID 和递增的解析 `version`，复制旧快照或保存调用者显式提供的新快照，同时保留旧任务、Chunk content、V1 Job、checkpoint 和历史成果；过期解析版本不会创建额外任务。
- 上述状态检查和行锁只序列化当前事务，没有解决同一任务多次进入 FAILED 时的 ABA：第一次失败产生的延迟 retry 请求，在任务重试后再次失败时仍可能命中相同解析 `version` 并重新触发。Worker 和 V2 条件修改接口接入前，必须按第 4 节确定独立的条件令牌及生成规则；不能用解析 `version` 冒充该令牌，也不能把本批 retry 校验描述为已经解决延迟请求去重。
- 上传完成、retry 和 reparse 的 V2 投影已经在上传 API/VO 中提供，并在 VO 边界输出毫秒时间。已有 `/api/jobs` 列表、SSE 和前端任务页仍以 V1 Job/wire `jobId` 为主，见下方剩余接入点。

#### 实际修改范围与改动量

本批生产改动集中在 22 个 `server/` 文件：

- Schema、时间与模型：`server/database.py`、`server/utils/time.py`、`server/model/job.py`、`server/model/video.py`、`server/model/upload.py`、`server/model/parse_usage.py`。
- 配置、DTO 与 VO：`server/config/parse_profile.py`、`server/model/dto/jobDTO.py`、`server/model/dto/uploadDTO.py`、`server/model/dto/parseUsageDTO.py`、`server/model/vo/jobVO.py`、`server/model/vo/uploadVO.py`。
- 上传及任务调用链：`server/api/schema.py`、`server/api/uploads.py`、`server/router/uploads.py`、`server/service/upload.py`、`server/repository/upload.py`、`server/repository/job.py`。
- 现有调用者的时间/集序兼容：`server/mq/mysql_queue.py`、`server/repository/library.py`、`server/repository/parse_usage.py`、`server/service/parse_usage.py`。

测试修改集中在 10 个 `tests/server/` 文件：`test_api.py`、`test_database.py`、`test_index_sync_service.py`、`test_job_repository.py`、`test_library_views.py`、`test_mysql_integration.py`、`test_mysql_queue.py`、`test_parse_usage.py`、`test_upload_service.py`、`test_utils.py`。新增迁移草案为 `docs/migrations/20260927_video_parse_v2_tasks.sql`，本文件同步记录实际结果。按记录完成时的工作树统计，22 个生产文件约 `+1725/-329`，10 个服务端测试约 `+1137/-382`，迁移脚本 556 行；被忽略的本设计文档不在 Git diff 统计中。开工前已经存在的 Harness/README 工作区修改予以保留，不计入本批生产改动量。

#### 验证结果与限制

| 命令 | 结果 | 说明 |
| --- | --- | --- |
| `env SEEKINO_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33307/seekino_video_parse_v2_refactor_test?charset=utf8mb4' .venv/bin/python -B -m pytest tests/server -q` | 769 passed, 3 skipped, 1 warning | Agent Timing 合表后重跑完整服务端测试；真实 MySQL 门禁已打开，3 项其他环境门禁仍跳过。此总数包含共享工作区中新增、未计入批次 0～2 改动量的测试 |
| `.venv/bin/python -B -m pytest tests/agent/video_parse_v2 -q` | 28 passed | 只验证现有 Harness 框架/输入合同，不代表真实模型或持久化执行已接通 |
| `.venv/bin/python -B -m server.cli init-db --database-url 'mysql+pymysql://root@127.0.0.1:33307/seekino_video_parse_v2_development?charset=utf8mb4' --environment development` | 通过；重复执行也通过 | 在新隔离 MySQL 8.4 实例（`127.0.0.1:33307`）的空开发交付库由当前 ORM 成功创建全部表；对同一交付库重复 `init-db` 也通过 Schema 门禁 |
| `.venv/bin/python -B -m server.cli init-db --database-url 'mysql+pymysql://root@127.0.0.1:33307/seekino_video_parse_v2_refactor_test?charset=utf8mb4' --environment test` | 通过 | 空测试库由当前 ORM 成功创建全部表 |
| `env SEEKINO_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33307/seekino_video_parse_v2_refactor_test?charset=utf8mb4' .venv/bin/python -B -m pytest tests/server/test_mysql_integration.py -q` | 9 passed in 2.92s | Agent Timing 合表后在独立测试库重跑；覆盖并发 V2 retry/reparse、同 Series 集序并发约束、分片上传 claim/lease fencing，以及既有导入回滚、删除和跨连接锁路径 |
| `env SEEKINO_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33307/seekino_video_parse_v2_refactor_test?charset=utf8mb4' .venv/bin/python -B -m pytest tests/server/test_agent_chat_service.py::test_mysql_agent_timing_roundtrips_through_message_detail -q` | 1 passed in 3.39s | 最后一次定向复跑：真实 MySQL 验证 Assistant 消息创建、纯耗时 checkpoint 不改消息 version/updated_at、中间阶段、完成后的 `detail.timing` 持久化往返及清理 |
| `.venv/bin/python -B -m pytest -q` 加下列 6 个定向用例 | 6 passed, 1 warning | 补足重复完成、固定快照读取、历史保留和时间绑定的代码级证据；这些用例不使用真实 MySQL，不折算为上行 9 项集成测试 |
| `.venv/bin/python -m compileall -q server agent/video_parse_v2 engine/parsev2` | 通过 | Python 模块编译检查通过 |
| `git diff --check` | 通过 | 未发现空白错误 |

额外 6 项定向用例的准确命令为：

```bash
.venv/bin/python -B -m pytest -q \
  tests/server/test_upload_service.py::test_complete_creates_video_and_durable_parse_job_once \
  tests/server/test_upload_service.py::test_selected_parse_config_survives_upload_completion_and_job_claim \
  tests/server/test_upload_service.py::test_full_reparse_creates_new_v2_version_and_retains_history \
  tests/server/test_job_repository.py::test_v2_task_repository_reads_frozen_config_video_and_parse_versions \
  tests/server/test_database.py::test_utc_timestamp_bind_normalizes_timezone_and_millisecond_precision \
  tests/server/test_parse_usage.py::test_parse_usage_persists_aware_utc_and_keeps_unix_ms_at_the_event_boundary
```

隔离 MySQL 8.4 已验证当前 ORM 能在两个空库建表、交付库重复 `init-db` 可通过现有 Schema 门禁，并由 9 项 V2/上传集成测试和 1 项 Agent Timing 集成测试验证真实 MySQL 路径。测试没有执行 V2 Worker/Harness、真实模型调用，也没有演练已有库时间列转换及历史数据升级；不能把它解释成完整 V2 执行链或升级迁移已经验收。`20260927_video_parse_v2_tasks.sql` 面向已有库升级，本次没有执行该脚本或任何数据清理；旧库未改动，旧数据也没有复制到两个新库。本批仍未重启业务服务、commit 或 push。

用户审核表结构后，确认 `agent_message_timing` 是没有独立查询或生命周期需求的一对一附表；它已并入 `agent_message.detail.timing`，`extra_context` 继续只保存用户请求上下文。Agent API/SSE 的 `timing` 输出保持原合同。新建的开发库和测试库原有 Agent 表均为 0 行；两库均已显式 `ALTER TABLE agent_message ADD COLUMN detail JSON NULL` 并 `DROP TABLE agent_message_timing`，复跑 `init-db` 通过。`create_schema()` 现在拒绝仍有旧附表或缺少 `detail` 的已有库；旧库有数据时须另做备份、回填和受控切换，不能套用本次空库 DDL。

调整后的新库实际表、逐字段类型/可空性/默认值、约束和 31 条物理外键见 [MySQL Schema 实测报告](../mysql_v2_schema_20260927.md)。交付库和测试库当前各有 23 张表、333 个字段，均已逐表 `COUNT(*)`，全部为 0；报告按真实 `information_schema` 生成，不将旧库升级 SQL 当作空库初始化步骤。

Agent Message 字段收敛已按 [Agent API 设计说明](agent-api.md) 在两套隔离空库执行 DDL：USER 的发送幂等键放入 `agent_message.detail.request.idempotencyKey`，用同 Conversation 的 JSON 路径唯一索引保持数据库约束；USER / ASSISTANT 以相邻 `sequence_no` 和角色配对，移除 `reply_to_message_id` 及其自外键；检索查询按每次实际调用保留在 `search_result.retrievalTrace[].query`，移除单独的 `retrieval_query` 列和公开顶层查询字段。两库的 `agent_message` 均实测为 22 列，`idempotency_key`、`reply_to_message_id`、`retrieval_query` 三列不存在。上述 DDL 只针对本次两套空库；有数据的旧库仍需单独设计回填和切换。

#### 下一批接入点与当前限制

- 批次 3 需要让现有队列领取 V2 主任务，令 Worker 调用 `ParseAgent`，由 Prepare 校验/初始化固定 Chunk 与 Interval 子任务，并实现租约 fencing、逐任务事务持久化、共享 Listener/mask 与提交后通知。当前 V2 retry/reparse 产生的 PENDING 任务不会被 V1 Worker 执行；初次上传能够继续运行，是因为同时保留了既有 V1 Job。
- 真正模型调用、Engine 结果 Schema 校验、乱序恢复和 Chunk/Interval 成果提交尚未接通。当前 `engine/parsev2/schemas.py` 已使用直接人名和原视频绝对整数秒；后续接入仍须由 Engine 校验结果，Harness 不能自行硬编码替代 Schema。
- Prefix compact 后台处理、独立游标/租约、费用按子任务归属属于批次 4；embedding、索引和检索后端迁移不在本批。
- `/api/jobs`、SSE、Library 最新可用成果选择和前端仍主要消费 V1 Job 的状态与 `jobId`/version。旧任务页目前把 V1 Job `version` 传入上传 retry/reparse 的 `expectedJobVersion`，并将实际返回的 `VideoParseTaskView` 当作 `ParseJobView` 使用；这两个版本无等价关系，按钮在 V1 Job 版本变化后可能返回 409，即使请求成功也不能正确消费响应。批次 5 需切到 V2 Task，并先确定独立于解析 `version` 的 UI/SSE 更新标识，同时保留旧成功成果的展示入口；在该切换完成前，本批上传 API 中的 V2 投影不能视为全局前端合同已经迁移。
- 历史 V1 数据不自动合成 V2 任务；本次只验证新空库由 ORM 建表，没有复制旧数据或演练已有库升级。对旧库执行真实迁移前，仍需在其副本核对现有列类型、数据范围、外键与并发升级方案。当前从 V1 profile 派生的 V2 默认配置只是实现层运行默认，后续若产品确认不同值，应以新上传/主动 reparse 的新快照生效，不能回写旧任务。

## 8. 给后续实现模型的开场指令

> 先直接读取 docs/design/refactor_2.md，并按第 1 节资料索引阅读 docs/design/video-parse-harness-trd.md、docs/design/video-parse-engine-trd.md、engine/parsev2/schemas.py 和当前 Harness README；design 文件被 Git 忽略不代表不存在。两份 TRD 分别负责编排/存储与结果 Schema，留意索引中已标注的历史文档和实现差异。按 refactor_2.md 的最新约定解释旧 TRD：episode_order 必须生效；Interval 以最左 chunk_parse_task_id 作锚点、左闭右开，并按计划增加有序 chunk_parse_task_ids；compact 成果放 chunk_parse_content.detail；系统时间用 TIMESTAMP，VO 才转 Unix 毫秒，视频时间用秒。复用现有上传与 Worker 通道，按批次对齐任务/成果模型再接 ParseAgent。先查第 4 节真正未决的合同，再列本批最小修改树和验收；遵守用户已授予的范围，不重复确认已决定的设计。保留现有工作区修改，不全局重命名 Job，不开发本阶段以外的 embedding/index，不重新设计已确定的 Harness 结构。不要把文档中的建议或历史测试结果冒充已完成实现。
