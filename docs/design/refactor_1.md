# user排查到的问题

算法层的核心契约其实非常简单：

```text
parse(Compact, VideoChunk) -> Result
retrieve(...) -> RetrievalResult
```

算法是否作为独立 PSM、独立进程运行并不是关键。当前只有 AniVAG Server 一个调用方，算法层本质上只是 Server 调用的 `parse` 和 `retrieve` 两个函数，因此完全可以与 Server 运行在同一个进程中。算法层需要保持清晰的函数边界，但不应仅仅为了边界而被强制拆成独立服务。

当前架构真正的问题，是把 DB Queue、Queue Consumer 和算法接口混在了一起：上传工程被当作 Producer，算法接口被当作 Consumer，又额外启动了一个 `server.mq` 进程放在二者之间。这样相当于把本应属于 Server 内部的后台消费循环提升成了独立 MQ/Worker 服务，同时把算法运行时也装进了这个进程，导致持久化、任务消费、业务编排和算法调用的职责边界全部混乱。

DB Queue 没有必要单独做成一个进程。Queue 中的任务已经存放在数据库里，事务提交后天然具备持久性；任务能否在 Server 重启后恢复，取决于数据库中的 Job 状态、Claim、Lease 和 Fencing，而不是取决于是否存在一个独立 Worker 进程。Producer 和 Consumer 都可以属于同一个 AniVAG Server：上传接口负责写入任务，Server 内部的后台线程负责领取任务，再直接调用 `parse` 或 `retrieve`。

因此需要重新区分三个概念：

```text
DB Queue  = MySQL 表、事务和任务状态，不是一个进程
Consumer  = AniVAG Server 内部的后台消费循环，不是一个独立 PSM
Algorithm = parse / retrieve 两个清晰的算法接口，不要求独立部署
```

问题的本质不是算法是否应该独立，而是当前实现人为制造了一个并不存在的服务边界，并把 DB Queue Consumer、后端任务生命周期和算法调用错误地打包进了同一个 `server.mq` 进程。

# 架构问题背景和处理

明白了。前面我把三件完全不同的东西混成了一件：

1. DB Queue 的持久化载体；
2. 消费 Queue 的后台执行循环；
3. `parse / retrieve` 算法接口。

然后错误地把它们都包装成了一个独立的 `Worker/MQ/Algorithm PSM`。这确实把架构画复杂了。

最准确的定义应该是：

```text
DB Queue     = MySQL 表 + Claim 事务 + Lease/Fencing
Consumer     = Server 进程里的一个后台线程
Algorithm    = Server 调用的 parse / retrieve 函数
```

它们都不需要因此成为新的 PSM 或进程。

## 正确架构

```text
                        MySQL
            UploadSession / ParseJob / Checkpoint
                   持久化 DB Queue
                  ▲                 │
                  │ enqueue         │ claim
                  │                 ▼

┌──────────────── AniVAG Server：唯一 PSM / 单进程 ────────────────┐
│                                                                 │
│  HTTP API                                                       │
│  ├── Upload API                                                 │
│  ├── Job API                                                    │
│  ├── Delete / Cancel API                                        │
│  └── Search / Agent API                                         │
│                                                                 │
│  Background Threads                                             │
│  ├── ParseJob Consumer                                          │
│  │     ├── claim                                                 │
│  │     ├── execute                                               │
│  │     ├── heartbeat                                             │
│  │     ├── retry                                                 │
│  │     └── finish / discard                                      │
│  │                                                              │
│  └── Retention Timer                                            │
│        └── 清理到期 CANCELLED 任务                               │
│                                                                 │
│  Application Services                                           │
│  ├── UploadService                                              │
│  ├── ParseJobService                                            │
│  ├── VideoProcessingService                                     │
│  └── IndexSyncService                                           │
│                                                                 │
│  Algorithm Module                                               │
│  ├── parse(Compact, VideoChunk) -> Result                        │
│  └── retrieve(...) -> RetrievalResult                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

这里根本没有“算法 PSM”。只有算法层或算法模块。

算法是否未来拆成独立进程，只是一个可选部署方式，不能反过来污染现在的代码抽象。当前只有 Server 会调用它，而且就是两个函数，直接调用完全合理。

## DB Queue 不需要自己的进程

对，DB 已经决定了它天然持久化。

更准确地说，持久性来自：

```text
事务提交成功的 ParseJob 记录
+ 明确的状态转换
+ lease expiration
+ fencing/version
```

不是来自某个常驻 Worker 进程。

进程崩溃以后：

```text
Server 重启
→ 后台 Consumer 线程重新启动
→ 从 MySQL 读取 PENDING
→ 回收 lease 已过期的 RUNNING
→ 继续处理
```

Job 并不会因为 Consumer 线程或 Server 进程退出而丢失。

所以：

- DB Queue 不是一个进程；
- DB Queue 也不是一个 PSM；
- 它就是 Repository 背后的持久化调度数据结构；
- Consumer 只需要是 Server 内部的后台循环。

真正的 MQ Broker 才需要单独进程，因为它本身要负责消息存储、投递和消费协议。我们的 MySQL 已经承担了持久存储和原子 Claim，不需要再人为制造一个 `server.mq` 进程。

## 当前 `server.mq` 的问题

当前 [server/mq/__main__.py](/Users/bytedance/workspace/other-project/AniVAG/server/mq/__main__.py:54) 实际做了三件事：

```text
启动 DB Queue Consumer
+ 装配后端 Repository / Service
+ 装配并直接运行算法
```

这产生了很多本来不需要的问题：

- API 和 Consumer 要分别启动；
- 两边分别创建数据库、Qdrant、配置和模型依赖；
- 删除必须跨进程通知；
- cancellation token 不能直接共享；
- 为了防止旧 Worker 回写，不得不完全依赖 DB lease；
- Server shutdown 和 Worker shutdown 要分别管理；
- 本来普通的函数调用被误解成了算法服务边界；
- `server.mq` 看起来像 MQ Broker，实际上只是一个 MySQL polling loop。

也就是说，真正的问题不是“算法和 Worker 耦合”，而是：

> 我们把本应属于 Server 内部的 DB Queue Consumer，错误地提升成了一个独立进程，然后又把算法调用一起塞了进去。

## 应该怎么收回来

目标应该是：

- 删除独立的 `python -m server.mq` 进程入口；
- 把 `ParseWorker.run_forever()` 改成 API 生命周期管理的后台线程；
- Worker 可以改名为 `ParseJobConsumer` 或 `ParseJobRunner`；
- `VideoProcessingService` 继续直接调用 `parse`；
- 检索路径继续直接调用 `retrieve`；
- MySQL Job、Lease、Retry、Checkpoint 全部保持不变；
- Retention Purger 也是 Server 内部的定时任务；
- `scripts/local_services.py` 不再单独启动 Worker。

物理结构变成：

```text
一个 Server 进程
├── HTTP 线程 / Event Loop
├── Agent ThreadPool
├── ParseJob Consumer Thread
└── Retention Scheduler Thread
```

软删除也因此更自然：

```text
DELETE 请求
→ 同一 Server 更新 DB 状态
→ 设置当前执行的 cancellation Event
→ Consumer 不再派发后续工作
→ 正在执行的函数返回后检查 fencing
→ 结果有效则提交，无效则丢弃
→ 到期后定时物理清理
```

数据库状态仍然是事实来源；内存 cancellation event 只是同进程快速通知。即使 Server 正好崩溃，重启后也会从数据库的 `CANCELLING/CANCELLED` 状态恢复，不依赖那份内存事件。

所以最终收敛结论就是：

```text
一个 Backend PSM
一个 Server 进程
一套持久化 DB Queue
一个进程内 Consumer
两个普通算法接口：parse / retrieve
```

是否把算法拆进程留作未来部署选项，但当前架构完全不需要。此前将 DB Queue Consumer、MQ、Worker 进程和算法 PSM 混为一谈，确实制造了不存在的服务边界。



# refactor计划

本节是后续实现的执行口径。若与上文记录的探索性描述存在冲突，以本节为准。

其中最重要的修正是：

```text
删除独立常驻 Worker 进程：是
删除 server/mq package：否
保留 server/mq 作为消息基础设施边界：是
ParseJobConsumer 在 AniVAG Server 进程内运行：是
```

`server/mq` 与“是否单独部署一个进程”是两个不同维度。当前用 MySQL 实现持久化 Queue，未来接入 RocketMQ 时也继续由 `server/mq` 承接消息投递和消费适配；Algorithm、Checkpoint、Artifact 和索引业务不能因此进入 MQ 层。

当前重构基线为 `fe56a7a feat(parse): pipeline ASR with semantic video parsing`。开始第一阶段前，`main` 与 `origin/main` 一致，工作区应保持干净。

## 总体目标

最终运行结构收敛为：

```text
AniVAG Server：唯一 Backend PSM / 单进程
│
├── FastAPI / Control Plane
│   ├── UploadSession API
│   ├── Job API
│   ├── Delete / Cancel API
│   └── Search / Agent API
│
├── server.mq
│   ├── MySqlParseJobQueue
│   └── ParseJobConsumer Thread
│       ├── claim
│       ├── heartbeat
│       ├── ack / retry
│       └── reap expired lease
│
├── VideoProcessingService
│   ├── 整集执行顺序
│   ├── Checkpoint
│   ├── Artifact
│   ├── 数据库事实
│   └── Index
│
├── RetentionPurger Thread
│   └── 清理 purge_after <= now 的 CANCELLED 聚合
│
└── Algorithm Module
    ├── parse(Compact, VideoChunk) -> Result
    └── retrieve(...) -> RetrievalResult

MySQL
├── UploadSession / ParseJob / Checkpoint
├── Job 状态与 Lease/Fencing
└── 当前 DB Queue 的持久化事实来源
```

整个改造分四个阶段。每个阶段必须独立完成、独立测试、独立提交；当前阶段没有通过验收前，不进入下一阶段。

---

## 第一阶段：净化 MQ 边界，将 Consumer 收回 Server 进程

### 目标

只改变运行拓扑和 MQ 代码边界，不改变 Parse 业务、Job 状态机、Checkpoint 含义和删除语义：

```text
当前：API Process + 独立 server.mq Worker Process
目标：一个 AniVAG Server Process + 进程内 ParseJobConsumer Thread
```

MySQL Claim、Lease、Heartbeat、Retry、Reaper 和 Fencing 全部保留。进程合并不等于删除这些机制，它们仍负责重启恢复、多实例误启动保护和迟到结果隔离。

### 预计文件调整

```text
M  .gitignore
   增加 !docs/design/refactor_1.md，使四个阶段的实际结果可以随提交持续更新。

A  docs/design/refactor_1.md
   Git 视角新增；保存本计划，并在每个阶段完成后记录实际结果与偏差。

A  server/config/mq.py
   MQ Consumer 的 consumer_id、lease、heartbeat、poll、retry、shutdown 配置。

A  server/config/index_sync.py
   将 IndexSync retry 从 MQ retry 中拆开，消除当前错误的配置耦合。

A  server/mq/contracts.py
   只定义最小 ParseJobQueue 和 ParseJobHandler 协议。

A  server/mq/mysql_queue.py
   当前 MySQL-backed Queue adapter：
   claim / heartbeat / ack / retry / reap expired lease。

A  server/mq/consumer.py
   ParseJobConsumer、LeaseHeartbeat、run_once、start/stop/join、健康状态。

M  server/mq/__init__.py
   明确 MQ package 是消息基础设施边界，不是独立算法 PSM。

M  server/mq/__main__.py
   保留 python -m server.mq --once 调试入口；
   复用统一 bootstrap，不再自行装配 DB、Qdrant、Algorithm 和 Service。

D  server/mq/worker.py
   代码迁入 consumer.py，删除 ParseWorker 这一错误命名。

M  server/repository/job.py
   移出 Queue delivery 生命周期 SQL；
   保留 Job 查询、汇总、Checkpoint、进度和业务写入的 Fencing。

M  server/bootstrap.py
   成为唯一 Composition Root；只创建一套 DB/Qdrant/Artifact/Algorithm；
   装配 MySqlParseJobQueue、ParseJobConsumer 和 VideoProcessingService。

M  server/api/app.py
   FastAPI lifespan 启动 ApplicationServices，关闭时停止并等待 Consumer。

M  server/api/system.py
M  server/model/vo/systemVO.py
   /healthz 暴露 Consumer running/stopped/failed，避免线程退出而 API 假健康。

M  server/config/settings.py
M  server/config/__init__.py
M  .env.example
   使用新的内部 MQ 配置对象；第一版继续兼容 ANIVAG_WORKER_* 环境变量。

M  scripts/local_services.py
   删除 worker_pid、独立 launch/status/stop 和 worker.log readiness；
   status 改为读取 Server health。

A  tests/server/test_mq_consumer.py
   覆盖 run_once、start/stop、retry、lease lost、fatal health。

A  tests/server/test_mysql_queue.py
   覆盖 claim、heartbeat、ack、retry、reap 和并发领取。

D  tests/server/test_worker_runtime.py
   用例迁入 test_mq_consumer.py。

M  tests/server/test_bootstrap.py
M  tests/server/test_config.py
M  tests/server/test_local_services.py
M  tests/server/test_api.py
M  tests/server/test_job_repository.py
M  tests/server/test_mysql_integration.py
M  tests/server/test_parse_usage.py
M  tests/server/test_upload_service.py
M  tests/server/test_video_processing_service.py
M  tests/server/test_worker_fencing.py
   统一改为通过 MySqlParseJobQueue 建立和推进 Queue delivery；
   Checkpoint 和业务持久化仍通过 Repository。
```

### 启动与关闭顺序

```text
startup
→ 构造一套共享依赖
→ 启动 ParseJobConsumer Thread
→ API ready

shutdown
→ Consumer 停止领取新 Job
→ 等当前处理与 Heartbeat 安全退出
→ Consumer 完全退出
→ 关闭 Agent executor
→ 关闭 Qdrant Client
→ dispose DB Engine
```

禁止在线程仍可能使用连接时先关闭 Qdrant 或 DB。

### 本阶段明确不做

- 不修改 `EngineVideoParseRuntime` 的算法契约；
- 不增加 `CANCELLED`；
- 不修改前端删除按钮；
- 不新增 RocketMQ SDK、`rocketmq_queue.py` 或 `outbox_relay.py`；
- 不删除 `server/mq/__init__.py` 或 `server/mq/__main__.py`；
- 不新增新的 Backend PSM。

### 验收条件

- 本地启动后只有一个 AniVAG Server 进程，不再有常驻 Worker 子进程；
- Upload complete 创建的 QUEUED Job 能由进程内 Consumer 正常处理；
- Retry、Reaper、Heartbeat 和 Fencing 行为与改造前一致；
- API shutdown 不出现后台线程继续使用已关闭资源；
- Consumer fatal 后 `/healthz` 不返回假健康；
- 全量后端测试通过。

### 实际结果（2026-09-16）

阶段 1 的实现与测试已经完成：

- `server/mq` 保留为消息基础设施边界，并拆为 `contracts.py`、`mysql_queue.py` 和
  `consumer.py`；
- Queue delivery 的 Claim、Heartbeat、Acknowledge、Retry 和 Reaper SQL 已从
  `ParseJobRepository` 迁入 `MySqlParseJobQueue`；
- `ParseJobRepository` 只保留任务查询、Checkpoint、进度和业务写入 Fencing；
- 独立 `ParseWorker` 已删除，`ParseJobConsumer` 由 `ApplicationServices` 在 Server
  lifespan 内启动和停止；
- `bootstrap.py` 成为 API 与单任务诊断入口共用的唯一 Composition Root；
- `/healthz` 已暴露 `components.parseConsumer.status`，fatal 时返回 `degraded`，且不公开
  数据库或 Provider 异常正文；
- `scripts/local_services.py` 只启动 AniVAG Server，不再管理 Worker PID、进程、readiness
  或 `worker.log`；
- `python -m server.mq` 只允许 `--once`，写独立的 `mq.log`，不再提供常驻运行方式；
- MQ 与 IndexSync retry 配置已经解耦；新配置使用 `ANIVAG_MQ_*` 和
  `ANIVAG_INDEX_SYNC_*`，旧 `ANIVAG_WORKER_*` 继续作为兼容回退；
- `server/mq/worker.py` 和 `tests/server/test_worker_runtime.py` 已删除，并新增独立的
  Consumer 与 MySQL Queue 测试。

与原计划相比有三项受控偏差：

1. 为避免阶段中间态继续指导用户启动已经删除的常驻 Worker，根 README、Server README、
   前端联调文档和 Parse 用量文档提前在本阶段同步；第四阶段仍负责最终整体校对；
2. `server.mq --once` 不再保留 `--init-db`，Schema 初始化继续由唯一正式入口
   `python -m server.cli init-db` 负责，避免在统一 Bootstrap 建立 Service 后才建表；
3. Shutdown 不设置超时后强制关闭共享资源。当前会完整等待 Consumer 和 Heartbeat 退出，
   避免后台线程继续访问已关闭的 Qdrant/DB；受控取消和更快的安全退出留到第三阶段。

验证结果：

```text
.venv/bin/pytest -q tests/server
399 passed, 8 skipped

.venv/bin/pytest -q
590 passed, 8 skipped, 3 warnings
```

另外，`python -m compileall -q server tests/server`、`python -m server.mq --help` 和
`git diff --check` 均通过。8 个跳过项全部是因为未配置 `ANIVAG_TEST_DATABASE_URL` 的
真实 MySQL 集成测试；SQLite Queue、Consumer、Fencing、Bootstrap、API 生命周期和健康检查
测试均已执行。本阶段没有修改数据库 Schema、Job 状态机、前端删除行为或 Algorithm
契约，也没有接入 RocketMQ。

---

## 第二阶段：收紧 Algorithm 边界

### 目标

将整集任务编排从 Engine 收回 Server，使生产调用真正满足：

```text
parse(Compact, VideoChunk) -> Result
```

Algorithm 只理解当前输入和输出，不理解 Job、Lease、Checkpoint、已经完成的 Chunk、工作目录或 UploadSession 状态。

Retrieve 当前边界基本正确：`engine/retrieval/video_retrieval.py` 是纯排序/融合算法，`server/service/video_retrieval.py` 负责 Embedding、Qdrant 和 MySQL hydrate。本阶段不为了形式新增 `RetrieveClient` 或空 facade。

### 职责重新分配

```text
VideoProcessingService
├── 读取 DB Checkpoint
├── 决定下一个 VideoChunk
├── 管理 ASR 就绪顺序
├── 准备 Compact + VideoChunk
├── 调用一次 Algorithm
├── 返回后检查 Lease/Fencing
├── 保存 Result / Artifact / Checkpoint
├── Embedding / MySQL import
└── Index publish

Algorithm
└── parse(Compact, VideoChunk) -> VideoChunkParseResult
```

### 预计文件调整

```text
M  engine/parse/video_parse.py
   从整集 ParsePipeline.run 中提取单 Chunk 调用；
   保留 Context、事件 ownership、去重和 fallback 等算法逻辑。

M  engine/parse/runtime.py
   收缩为模型装配和单 Chunk parse_chunk(...)；
   不再让生产 Server 通过它管理整集窗口、工作目录和已完成 Chunk。

M  server/service/video_processing.py
   接管整集循环、恢复、Checkpoint、ASR 顺序、Artifact、Embedding、DB 和 Index；
   每次算法返回后先 Fence，再产生持久化副作用。

M  tests/engine/test_parse_pipeline.py
   覆盖单 Chunk Context、ownership、去重与 fallback。

M  tests/engine/test_runtime.py
   覆盖模型装配、单 Chunk 调用、候选 ASR 和 fallback。

M  tests/engine/test_parse_usage.py
   保证单 Chunk 调用仍正确记录模型用量上下文。

M  tests/server/test_video_processing_service.py
   接管整集顺序、恢复、Checkpoint、ASR pipeline 和迟到 Result 丢弃测试。

M  engine/README.md
   明确生产 Algorithm 契约和 Server/Engine 职责边界。
```

### 兼容处理

- `engine/parse/runtime.py` 的整集 `run(...)` 可以暂时作为 CLI compatibility wrapper；
- `engine/parse/checkpoints.py` 可以继续服务 CLI/local experiment，但不再是 Server 的任务事实来源；
- `engine/parse/cli.py` 暂不删除；
- 本阶段不新增重复的 Algorithm DTO、RPC Client 或独立 Algorithm Process。

### 本阶段文件增删

```text
A：无
D：无
```

### 验收条件

- `VideoProcessingService` 的主流程能直接看出“取下一 Chunk → 调 Algorithm → Fence → 保存”；
- Algorithm 接口中不存在 `job_id`、Lease、Repository、Checkpoint 或 UploadSession；
- Server 能从现有 DB Checkpoint 恢复，不依赖 Engine 自己猜测完成进度；
- Algorithm 调用期间 Lease 失效时，返回结果不会落盘、入库或发布索引；
- CLI 现有完整视频解析能力不回退；
- Engine 与 Server 相关测试全部通过。

---

## 第三阶段：实现 CANCELLED 软删除与 Retention Purger

### 目标

运行中的任务也允许删除。删除接口只做持久化逻辑取消，原始数据和处理产物保留到 `purge_after`，随后由 Server 内 RetentionPurger 定时物理清理。

第一版只引入一个持久化终态 `CANCELLED`，不增加 `CANCELLING`。HTTP 请求期间的短暂过程由前端本地 `deleting` 状态表达，数据库事务提交后直接进入 `CANCELLED`。

### 状态与字段

```text
UploadSession
├── status = CANCELLED
├── cancelled_at
└── purge_after

ParseJob
└── status = CANCELLED
```

保留期必须配置化，不在业务代码中硬编码：

```text
ANIVAG_CANCELLED_RETENTION_DAYS
ANIVAG_RETENTION_PURGE_INTERVAL_SECONDS
ANIVAG_RETENTION_PURGE_BATCH_SIZE
```

若没有另行产品决定，首版默认可采用保留 7 天、每小时扫描、每批 100 条。

### Cancel 事务

```text
DELETE UploadSession
→ 锁定 UploadSession 和关联 ParseJob
→ UploadSession.status = CANCELLED
→ 写 cancelled_at / purge_after
→ 所有关联 Job.status = CANCELLED
→ lease_version + 1
→ 清空 lease_owner / lease_expires_at / heartbeat_at
→ 清 completion token 和 active slot
→ 提交事务
→ 正常查询立即不可见
```

重复删除必须幂等，不能重复延长 `purge_after`。

在途模型调用不保证能在网络层瞬间中止，但算法返回后必须经过既有 Fencing。因为 Job 已不再是 RUNNING 且 lease version 已改变，迟到 Result 必须被丢弃，不能写 Checkpoint、导入事实或发布索引。

取消接口不能被 Qdrant 或文件清理阻塞。取消后立即通过 MySQL 查询过滤和 IndexSync 约束撤销检索可见性；派生索引可以异步撤销，原视频、Artifact、Checkpoint 和业务事实保留到物理清理阶段。

### Retention Purge

```text
Server startup
→ RetentionPurger 立即扫描一次
→ Event.wait(interval)
→ 批量领取 purge_after <= now 的 CANCELLED Session
→ 幂等清理外部资源
→ 最后删除数据库聚合
```

固定清理顺序：

```text
Qdrant point
→ Job / Video Artifact
→ Usage / Checkpoint / Index Intent / VideoChunk
→ Video / ParseJob
→ 最后删除 UploadSession
```

中间任一步失败，都保留 `CANCELLED + purge_after` Tombstone，下一轮继续幂等重试。

### 预计文件调整

```text
M  server/model/upload.py
   增加 CANCELLED、cancelled_at、purge_after 和到期扫描索引；
   删除 DELETION_PENDING marker 生命周期方案。

M  server/model/job.py
   增加 ParseJobStatus.CANCELLED 并更新数据库 CHECK。

M  server/model/dto/uploadDTO.py
M  server/model/vo/uploadVO.py
   在内部状态和 HTTP contract 中增加 cancelledAt / purgeAfter。

M  server/database.py
   校验既有数据库已经完成新列、索引和 CHECK 约束迁移。

M  server/config/upload.py
M  server/config/settings.py
M  .env.example
   增加保留期、扫描周期和 batch 配置。

M  server/repository/upload.py
   begin/finish_delete 改为 cancel_session、list_due_purges、
   prepare_purge、finish_purge。

M  server/repository/index_sync.py
   CANCELLED Upload 不允许重新物化或 UPSERT 索引。

M  server/repository/library.py
M  server/repository/video.py
   Library、计数、检索 hydrate 全部排除 CANCELLED Upload。

M  server/service/upload.py
   delete_session 只做逻辑取消；
   增加 purge_due_cancelled，复用现有幂等清理能力。

M  server/service/job.py
   默认列表和计数排除 CANCELLED；显式 get 仍能读取终态。

A  server/service/retention.py
   RetentionPurger 只负责定时和调用 UploadService；
   不直接包含 SQL、Qdrant 和文件清理业务。

M  server/bootstrap.py
M  server/api/app.py
   由 ApplicationServices 管理 RetentionPurger 的 start/close。

M  server/api/uploads.py
   delete 返回 CANCELLED UploadSession 和保留时间。

M  server/api/jobs.py
   CANCELLED 是 SSE 终态，canCancel 不再硬编码 false。

A  docs/migrations/20260916_cancelled_upload_retention.sql
   受控 ALTER：增加字段和索引，重建 Upload/Job status CHECK。

A  tests/server/test_retention.py
   覆盖启动扫描、周期执行、可中断关闭和单轮异常恢复。

M  tests/server/test_upload_service.py
M  tests/server/test_mysql_integration.py
M  tests/server/test_worker_fencing.py
M  tests/server/test_api.py
M  tests/server/test_job_repository.py
M  tests/server/test_database.py
M  tests/server/test_config.py
M  tests/server/test_bootstrap.py
M  tests/server/test_index_sync_repository.py
M  tests/server/test_index_sync_service.py
M  tests/server/test_library_views.py
M  tests/server/test_video_retrieval_service.py
   覆盖软删除、幂等、运行中取消、迟到结果、隐藏、到期清理和失败重试。
```

### 本阶段删除内容

不删除整个文件，只删除或替换旧符号与旧语义：

```text
UPLOAD_DELETION_PENDING_ERROR_CODE
UploadRepository.list_pending_deletions()
UploadRepository.begin_delete_session()
UploadRepository.finish_delete_session()
UploadService.reconcile_deletions()
bootstrap 中启动时单次 reconcile_deletions() 调用
```

### 验收条件

- QUEUED、RETRY_WAIT、RUNNING、FAILED、SUCCEEDED 关联任务都可以请求删除；
- DELETE 成功后立即返回 `CANCELLED`，并从正常 Library、Tasks、Search 中消失；
- RUNNING Job 取消事务提交后，Heartbeat、Checkpoint、Import、Mark Success 全部失效；
- 在途模型调用返回后 Result 被丢弃；
- 重复 DELETE 不产生新状态、不延长 `purge_after`；
- 保留期前不删除 canonical data；
- 到期后按固定顺序物理清理；
- Qdrant 或文件清理失败后下轮能够重试；
- Server 重启后仍能继续处理到期 Tombstone。

---

## 第四阶段：统一 Web 删除入口并同步文档

### 目标

所有用户入口使用同一套删除语义：进行中的上传、解析和已完成任务都可以请求软删除；提交期间显示“正在终止并删除”，成功后从正常 UI 消失。

### 预计前端调整

```text
M  web/src/api/contracts.ts
   UploadStatus / ParseJobStatus 增加 CANCELLED；
   UploadSessionView 增加 cancelledAt / purgeAfter。

M  web/src/features/tasks/TasksPage.tsx
   删除 RUNNING 的前置 return 和 disabled；使用 canCancel；
   确认文案改为软删除与延迟物理清理语义。

M  web/src/features/tasks/TasksPage.test.tsx
   覆盖 RUNNING 删除、重复点击、成功移除和失败恢复。

M  web/src/features/tasks/ParseUsageDetails.tsx
M  web/src/features/tasks/ParseUsageDetails.test.tsx
   将 CANCELLED 视为终态，停止持续轮询模型用量。

M  web/src/features/uploads/UploadProvider.tsx
   processing 状态允许 cancel；删除时关闭 EventSource；
   CANCELLED 作为 SSE 终态，防止迟到快照把任务写回 UI。

M  web/src/features/uploads/UploadProvider.test.tsx
   覆盖删除成功、失败恢复、SSE 关闭和迟到事件。

M  web/src/features/uploads/UploadDialog.tsx
   processing 状态继续展示删除入口。

M  web/src/features/uploads/upload-types.ts
M  web/src/features/uploads/upload-types.test.ts
   统一 deleting/cancelled 文案和进度口径。

M  web/src/features/library/LibraryPage.tsx
   不再主动屏蔽 processing 删除；CANCELLED Upload 不显示。

M  web/src/app/App.test.tsx
   覆盖上传弹窗中的运行中删除入口。
```

继续复用现有 `POST /api/uploads/delete` 和 `UploadSessionView`，因此不为了改名而修改 `web/src/api/client.ts`，也不新增 `/cancel` 路由。

### 预计文档调整

```text
M  docs/design/refactor_1.md
   回填四个阶段的实际结果、偏差和最终边界。

M  README.md
M  server/README.md
   同步单 Server Process、MQ package、软删除和 Retention。

M  engine/README.md
   明确 parse(Compact, VideoChunk) -> Result。

M  web/README.md
   记录运行中终止与软删除交互。

M  docs/api-ui-views.md
M  docs/design/frontend-api.md
   删除接口不再对 RUNNING 返回 409；补充 CANCELLED、SSE 和保留期。

M  docs/parse-model-usage.md
   “独立 Worker”改为“Server 内 MQ Consumer”；
   取消前已经发生的模型调用费用仍如实保留。

M  docs/design/series-background-api.md
   部署含义上的 Worker 改称 Parse Consumer / VideoProcessingService。
```

### 验收条件

- TasksPage、UploadDialog、LibraryPage 的删除能力一致；
- RUNNING 任务删除按钮可用；
- 请求中只发送一次 DELETE，按钮有明确等待状态；
- 成功后任务从缓存和 UI 中移除，旧 SSE 不会将其恢复；
- 失败后保留任务并显示可重试错误；
- CANCELLED 被所有轮询和 SSE 逻辑视为终态；
- 前端单测、构建和后端全量测试通过；
- README、API 文档和实际行为一致。

---

## 最终新增与删除文件汇总

### 新增

```text
A  server/config/mq.py
A  server/config/index_sync.py
A  server/mq/contracts.py
A  server/mq/mysql_queue.py
A  server/mq/consumer.py
A  server/service/retention.py
A  tests/server/test_mq_consumer.py
A  tests/server/test_mysql_queue.py
A  tests/server/test_retention.py
A  docs/migrations/20260916_cancelled_upload_retention.sql
A  docs/design/refactor_1.md（Git 视角；本地文件已存在）
```

### 删除

```text
D  server/mq/worker.py
   等价迁移到 server/mq/consumer.py。

D  tests/server/test_worker_runtime.py
   等价迁移到 tests/server/test_mq_consumer.py。
```

明确不删除：

```text
server/mq/
server/mq/__init__.py
server/mq/__main__.py
engine/parse/runtime.py
engine/parse/checkpoints.py
engine/parse/cli.py
```

真正开始接入 RocketMQ 时，再新增：

```text
server/mq/rocketmq_queue.py
server/mq/outbox_relay.py
```

同时采用 Transactional Outbox：上传完成事务同时写 ParseJob 和 Outbox，Relay 再发布只包含最小 `job_id` 的消息。RocketMQ 只替换任务发现和投递方式，MySQL 仍然保存 Job 状态、Checkpoint 与 Fencing 事实。

## 阶段执行规则

每个阶段都遵循同一套交付门槛：

1. 开始前确认上一阶段工作区干净、测试通过；
2. 只修改当前阶段列出的职责，不顺手推进下一阶段；
3. 先跑直接相关测试，再跑全量后端或前端测试；
4. 检查 `git diff --check`、新增文件和删除文件是否符合计划；
5. 单独 commit 并 push，建立可回滚点；
6. 更新本计划中该阶段的实际结果与偏差，再进入下一阶段。

建议提交顺序：

```text
1. refactor(mq): run parse job consumer inside server process
2. refactor(parse): make video parsing a chunk-scoped algorithm
3. feat(upload): soft-cancel tasks and purge expired tombstones
4. feat(web): allow cancelling active parse tasks
```
