# Seekino 技术方案续篇（工作补充）

> 本文件续接原技术方案正文，自第 14 章开始记录已确认的工程结构决策与后续实施状态。它是开发过程中的工作补充，不替代原 `.mdv` 文档。

## 14. 工程目录边界决策

### 14.1 根目录就是工程与应用边界

Seekino 是一个完整应用工程，不是准备发布到 PyPI、供其他项目按 SDK 方式安装的 Python 公共包。因此，仓库根目录直接承担工程边界，不再增加 `packages/`、`src/`、`seekino/` 三层包装：

```text
Seekino/
├── server/
├── engine/
├── gateway/
├── tests/
├── evals/
├── pyproject.toml
└── uv.lock
```

根 `pyproject.toml` 统一声明运行与开发依赖，并设置 `tool.uv.package = false`。它的作用是管理整个应用的 Python 环境和工具配置，而不是构建、安装或向外发布一个名为 `seekino` 的 Python 包。

`server/`、`engine/`、`gateway/` 是应用内部的模块命名空间：

- `server` 表达工程服务与持久化边界；
- `engine` 表达视频解析、检索等算法能力；
- `gateway` 表达模型供应商及其他外部能力的适配边界。

这些命名空间不等于三个独立发行包。其 `__init__.py` 只用于稳定模块识别，不通过 `__all__` 或聚合导入制造 barrel export；调用方应从真实定义模块显式导入。这样可以让依赖关系在代码中可见，也避免一个入口无意间把大量内部实现暴露出去。

### 14.2 领域切片术语

Seekino 的正式视频时间片统一称为 `VideoChunk`，主键为 `videoChunkId`。解析结果、用于
召回的 `VideoChunkRetrievalDocument`、Text/Vision Embedding、候选与 Evidence 都必须
归属于同一个 VideoChunk，不能再为解析或检索建立另一套并行切片身份。

`Segment` 只允许出现在断点续传上传语境：它表示
`UploadSession` 下按字节范围传输、校验和重试的上传分片，不表达视频时间轴语义，也不
参与解析或检索。上传模型统一为 `UploadSession/Segment`。

历史实验产物中的 `segment-NNNN` 文件名可以作为兼容读取格式保留，但不能据此恢复
旧领域语义；新产物写为 `chunk-NNNN`。

### 14.3 不使用 `packages/` 的依据

`packages/` 适合一个仓库同时维护多个可独立构建、独立发布、独立版本化的包。Seekino 当前没有这些需求：Server、Engine 与 Gateway 要共同组成同一个产品，使用同一套依赖锁、测试入口与部署上下文。继续保留 `packages/*/pyproject.toml` 会人为产生三套包边界和多个入口，增加依赖漂移与运行方式歧义。

本项目参考 DoVideoAI、VideoRAG、LightRAG 时，只吸收以下工程思想：

- 将 API/任务编排、算法流水线、外部模型适配、存储实现分离；
- 将离线索引构建和在线检索视为可独立演进的流程；
- 通过清楚的存储适配边界隔离数据库或向量库实现；
- 让可执行入口、配置、测试和部署方式对工程使用者可发现。

不照搬它们面向公共 SDK、可安装库、示例集合或多包仓库的目录外形。目录设计的依据是 Seekino 自身的所有权、运行进程、依赖方向和部署边界，而不是与参考项目保持文件名一致。

## 15. 当前实现与完整蓝图

### 15.1 当前阶段实际文件（非完整工程）

下列清单表达的是结构迁移及 M1 Upload-to-Search 后端落地后的实际代码范围，不表示完整工程未来只会有这些文件。Web 与部署目录仍遵循“出现真实实现时再建立”。

```text
Seekino/
├── server/
│   ├── api/                  # app、依赖装配与独立进程入口
│   ├── worker/               # Durable Worker、处理编排与进程入口
│   ├── config.py
│   ├── identity.py
│   ├── database.py / schema.py / cli.py
│   ├── model/                # video、upload、job、index_sync
│   ├── repository/           # library、upload、job、video、index_sync
│   ├── service/              # 上传、媒体、任务、检索与同步用例
│   └── storage/              # artifact、qdrant
├── engine/
│   ├── __init__.py
│   ├── parse/
│   │   ├── cli.py / runtime.py / checkpoints.py
│   │   ├── schemas.py / analysis_windows.py
│   │   └── ...
│   └── retrieval/
│       ├── __init__.py
│       └── video_retrieval.py
├── gateway/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── schemas.py
│   └── ...
├── tests/
│   ├── server/               # API、Upload、Job、Worker、Storage 与检索测试
│   ├── engine/
│   ├── evals/
│   └── gateway/
├── evals/
│   └── recall/
│       ├── __init__.py
│       ├── cli.py
│       ├── qdrant_index.py
│       ├── recall_eval.py
│       └── ...
├── outputs/
│   ├── engine/
│   └── gateway/
├── pyproject.toml
└── uv.lock
```

本节中的省略号代表当前已存在但无需在架构决策中逐一展开的实现文件；它不是待创建的空目录或空壳清单。实验性的 Qdrant backend、召回 runner 与 recall CLI 已从 Engine 移入 `evals/recall`；`engine/retrieval` 只保留可被产品编排复用的纯检索算法。正式持久化 Qdrant 与 Artifact Store adapter 仅位于 `server/storage/`。

### 15.2 完整目标蓝图

以下是产品成熟后的职责地图。它描述最终应具备的工程面，而不是要求当前阶段一次性生成所有文件。

```text
Seekino/
├── server/
│   ├── api/                 # HTTP 应用、依赖装配、路由和协议转换
│   │   ├── app.py
│   │   ├── dependency.py
│   │   └── routes/
│   ├── service/             # 用例编排、事务边界之外的流程协调
│   ├── repository/          # MySQL 事实数据与同步意图的读写语义
│   ├── model/               # ORM 持久化模型
│   ├── storage/             # Qdrant 与媒体/二进制 Artifact Store 适配器
│   ├── worker/              # 后台任务消费、重试与周期性 reconcile
│   ├── schema.py            # Server 输入、输出及跨层数据契约
│   ├── database.py          # 数据库引擎、会话与建表装配
│   └── cli.py               # 工程运维与阶段性命令入口
├── engine/
│   ├── parse/               # VideoChunk 理解、上下文编排和结构化结果
│   └── retrieval/           # 向量构造、候选召回、融合与排序算法
├── gateway/                 # 模型厂商、推理服务和外部 API 适配
├── web/                     # 用户界面及其前端构建配置
├── tests/
│   ├── server/
│   ├── engine/
│   ├── gateway/
│   └── integration/
├── evals/                   # 算法评测集、评测脚本与基线结果
├── deploy/                  # 镜像、进程、环境和发布配置
├── engines/                 # 需要纳入工程管理的本地模型/运行时资源配置
├── docs/                    # 产品、架构、接口和运维文档
├── assets/                  # 非代码静态资产与受控样例
├── outputs/                 # 本地生成结果；不作为源码提交
├── pyproject.toml
├── uv.lock
├── .env.example
└── README.md
```

未来目录遵循“有真实职责和首个实现时才创建”的原则。`server/api/` 与
`server/worker/` 已有可运行实现；尚未进入当前里程碑的其他目录不提前建立空壳。

### 15.3 层次职责与依赖方向

主要调用方向如下：

```text
web
  │
  ▼
server.api ───────┐
                  ▼
server.worker ──> server.service
                    ├──> server.repository ──> server.model / MySQL
                    ├──> server.storage ──────> Qdrant / 媒体 Artifact Store
                    └──> engine ──────────────> gateway ──> 外部模型服务
```

- `server.api` 只处理传输协议、鉴权、参数校验和响应映射，不直接拼接 SQL 或操作 Qdrant。
- `server.service` 负责应用用例和跨资源编排，是 API、CLI、Worker 可复用的业务入口。
- `server.repository` 负责 MySQL 的事实模型、事务和查询语义，不承载模型推理算法。
- `server.storage` 当前封装 Qdrant 和环境隔离的本地 Artifact Store；后者管理原视频、Proxy、VideoChunk、AnalysisWindow、关键帧、Clip 等媒体或二进制产物。Artifact Store 不默认承载 Embedding；Embedding 的 durable source 是独立存储决策。
- `engine` 负责可评测的算法过程，接收明确输入并返回明确结果，不拥有数据库会话、持久化状态或任务重试。
- `gateway` 隔离外部服务 SDK、鉴权、请求与响应差异，不反向依赖 Server 的业务实现。

当前 Engine 可以显式依赖 Gateway 提供的模型适配；Server 可以组合 Engine、Repository 与 Storage。禁止 `engine -> server`、`gateway -> server` 以及层间循环依赖。

### 15.4 Redis 限流与过载保护（完整目标）

Redis 只在多用户、多实例阶段承担短生命周期的分布式限流状态，不是 M1 的前置依赖，也不成为上传、任务或索引的事实源。已受理的 `UploadSession`、后台 Job、Checkpoint、Lease 与用量审计仍持久化在 MySQL；MySQL DB Queue 是任务真源，RocketMQ 只负责通知和分发。Redis 状态丢失不能导致已受理任务消失，也不能绕过数据库中的幂等约束和积压上限。

完整目标采用分层准入，而不是用一个令牌桶解决所有过载问题：

1. **上传速率**：创建新的 `UploadSession` 前，按用户或 Workspace 获取上传令牌；每个新视频消耗一个令牌，桶容量与补充速率由部署配置决定。同一幂等键或 `uploadId` 的断点续传、缺片补传和请求重试返回原会话，不重复扣减；不能对每个上传 Segment 单独计数。系列批量上传按视频数原子申请额度，或进入受控批次，不能通过一次批量请求绕过限制。
2. **上传与任务硬上限**：令牌桶只限制进入速度，不能保证队列有界。Server 还必须校验用户或 Workspace 的活动上传会话数、存储额度以及 `QUEUED + RUNNING` Job 数；Worker 以用户级和全局并发上限消费。这样既允许在额度内连续提交多集视频，也不会让一次大批量请求无限堆积 RocketMQ、DB Queue、磁盘或模型任务。
3. **模型调用限流**：用户查询或解析提交可以设置用户级请求桶；每一次真正的 VLM、ASR、Embedding 或 LLM 外部调用，则必须在 Gateway 边界获取 `provider + model` 维度的全局令牌，并可叠加用户或 Workspace 额度。Planner、Executor、Critic、补查以及失败重试均按实际调用计数；切换到降级模型后使用对应 Provider 的独立额度，不能只在最外层 Job 提交时扣一次令牌。
4. **不同资源分别治理**：请求令牌桶控制单位时间调用速率；在途模型请求数由有界 Worker、Semaphore 或线程池控制；模型 TPM、日/月费用和存储配额由独立计量与持久化账本控制，不能把这些概念合并成一个“Token 限流”。

Redis 限流操作必须原子执行。额度不足时，新建上传会话或交互式模型请求返回 `429` 与 `Retry-After`；后台 Job 获取不到模型额度时进入 `RETRY_WAIT`，不记为业务失败。多用户部署中 Redis 不可用时，新建上传会话和新的付费模型调用默认拒绝准入；已经写入 MySQL 的 Job 保留原状态，待限流服务恢复后由 DB Queue 继续调度。Redis 不用于保存 Upload Segment 完成事实，也不替代 MySQL Lease、唯一键、幂等键或 Qdrant 稳定 point ID。

## 16. Server 持久化与 Stage 2 Index Sync

### 16.1 持久化归属

当前阶段使用的 MySQL、Qdrant 与 Artifact Store 都是工程运行状态的一部分，其生命周期、恢复、环境隔离和一致性策略由 Server 负责。Embedding 的 durable source 是 MySQL `VideoChunkEmbedding`，Qdrant 是可重建派生索引；Artifact Store 承载媒体和其他二进制产物。是否将向量本体迁出 MySQL 必须另行设计，不能与 Artifact Store 建设自动绑定。Engine 只产生 VideoChunk 解析结果、文本向量、视觉向量或检索评分，不直接决定这些结果如何持久化。

本次术语重构已经修改 ORM 与目标物理表/列名，但没有自动改写现有开发 MySQL。
`create_schema()` 会在发现旧 `video_segment*` 表或旧关联列时于建表前明确拒绝，避免
`create_all()` 留下新旧 Schema 混合状态。现有 24 行开发数据需要另行执行受控的表、列、
外键和约束重命名并核对行数；完成前，真实 MySQL 的新命名 Schema 仍属于待验收项。

职责进一步拆分为：

- `server/model`：定义视频事实、Upload、ParseJob、Checkpoint 和 `IndexSyncIntent` ORM；
- `server/repository`：在 MySQL 事务内写事实数据、任务租约和同步意图，并提供条件回写语义；
- `server/storage`：实现 Qdrant point 与媒体/二进制 Artifact Store adapter；
- `server/service`：把 Repository、Storage、Engine 与 Gateway 编排成上传、解析、同步和检索用例；
- `server/worker`：从 MySQL DB Queue Claim，使用 Lease/Heartbeat/Retry/Reaper 并复用上述 Service。

### 16.2 事实数据与同步意图同事务

Video、VideoChunk、解析信息与 Embedding 写入 MySQL 时，必须在同一个数据库事务中写入对应的持久化 `IndexSyncIntent`。事务提交后再由 Index Sync Service 调用 Qdrant：

```text
解析/导入结果
    │
    ▼
MySQL 单事务
  ├── Video / VideoChunk / VideoChunkInfo / VideoChunkEmbedding 等事实
  └── IndexSyncIntent（期望状态）
    │ commit
    ▼
Index Sync Service ──> Qdrant
```

因此 Qdrant 暂时不可用不会回滚或丢失已提交的 MySQL 事实；失败只更新意图的重试状态。意图至少应表达目标操作（`UPSERT` 或 `DELETE`）、`videoChunkId`、`sourceRevision`、状态、尝试次数、下次可用时间、最近错误和同步时间。

当文本向量与视觉向量均满足索引要求时，期望操作为 `UPSERT`；当 VideoChunk 尚不完整，或新事实要求清除旧索引时，期望操作为 `DELETE`。同一 VideoChunk 的新 revision 覆盖旧期望，Worker 对旧 revision 的迟到确认不能把新意图错误标记为成功。

`generation` 是并发写回的代次令牌，初始值为 1。以下三类变化都会递增代次：事实变化产生新的 desired state、reconcile/rebuild 将当前状态重新排队，以及检测到旧任务可能已经产生外部副作用后对当前意图进行失效重排。Worker 领取任务时携带 `videoChunkId + sourceRevision + operation + generation` 快照；成功、失败等所有写回回执都必须比较代次及目标内容，只有与当前记录完全匹配才能更新状态。迟到回执不能覆盖新意图；若迟到任务可能已经写过 Qdrant，则再次递增当前代次并重排，以新的 desired state 最终收敛。

### 16.3 Qdrant 数据契约

Stage 2 的正式 Qdrant adapter 放在 `server/storage`，并遵循以下约束：

- point ID 直接使用正式、稳定的 `videoChunkId` UUID，不再依据评测集序号或临时数据生成身份；
- 使用 `text` 与 `vision` 两个 named vector；启动装配时必须显式提供二者维度，由 adapter 先创建或校验完整 collection schema，不能根据收到的第一个 point 猜测维度；
- payload 只保存过滤与一致性校验所需字段：`seriesId`、`videoId`、`videoChunkId`、`sourceRevision`、`isFallback`；
- 剧情文本、时间信息、来源地址等业务事实仍从 MySQL 读取，不复制到 Qdrant 形成第二份事实源；
- `sourceRevision` 是目标 point 内容的确定性摘要，用于识别已同步、缺失或过期状态。

### 16.4 幂等、重试、校准和重建

- **幂等同步**：相同 `videoChunkId + sourceRevision` 可重复执行；Qdrant 使用稳定 point ID 覆盖写，不增加重复点。
- **失败重试**：网络或 Qdrant 故障只记录 `RETRY_WAIT`、错误信息、尝试次数和退避时间，后续 CLI 或 Worker 可继续消费。
- **最小 reconcile**：读取 MySQL 当前意图并核对 Qdrant 的 point 与 `sourceRevision`。缺点、旧 revision 或应删除但仍存在的 point 被重新置为待处理，再走同一同步路径。
- **全量 rebuild**：当前实现从 MySQL 事实表与 `VideoChunkEmbedding` 中保存的文本、视觉向量重新物化意图并生成 point，Qdrant 本身不是恢复来源。未来若要外置 Embedding，必须单独选择 durable store、定义引用和恢复契约；Artifact Store 的媒体职责不预设这个结论。
- **READY 条件**：只有 Series 的全部目标 Video 都解析成功且各自产生 VideoChunk，并且全部 VideoChunk 都具备所需向量、对应最新 `UPSERT` 意图均同步成功，Series 才能进入 `READY`。存在未解析 Video、缺失、待同步或失败状态时保持 `INDEXING`。

M1 只实现支撑上述一致性的最小闭环，不引入 `IndexVersion`、Alias 切换、蓝绿 collection 或复杂分布式任务框架。这些能力在规模、在线迁移或零停机重建成为真实需求后再设计。

M1 同样不支持物理删除 VideoChunk、Video 或 Series。物理级联删除会让依附于 VideoChunk 的同步意图先消失，无法再可靠驱动 Qdrant 删除，因此不能把 cascade intent 当成索引清理方案。后续开放删除能力时，必须引入能独立存活到外部删除确认完成的 tombstone/outbox 记录；待其同步成功后，才清理事实数据与墓碑。

### 16.5 Stage 2 验收标准

Stage 2 至少满足：

1. 对相同意图重复同步，不增加 Qdrant point 数量；
2. Qdrant 不可用时，MySQL 事实事务仍能成功提交，失败意图可查且可重试；
3. Qdrant 恢复后再次同步，意图最终收敛为成功；
4. 人工删除一个 point 后，reconcile 能发现并恢复它；
5. Qdrant 清空后，当前可从 MySQL 的 `VideoChunkEmbedding` 重建，而不是依赖 Qdrant 中残留的数据；
6. Series 只在全部最新 VideoChunk 索引一致时进入 `READY`。

## 17. 可执行入口策略

所有 Python 命令都从仓库根目录运行，统一使用根环境和根 lockfile。当前已实现的入口与未来计划入口必须分开表述。

当前阶段入口：

```bash
uv run python -m server.cli --help
uv run python -m engine.parse.cli --help
uv run python -m evals.recall.cli --help
uv run python -m gateway.cli --help
```

Stage 2 的索引同步与 reconcile 命令归入 `server.cli`，不再为 Storage 或 Engine 创建独立发行入口。

当前 API 与后台 Worker 入口为：

```bash
uv run python -m server.api
uv run python -m server.worker
```

根目录暂不增加一个仅负责转发的 `main.py`。API 与 Worker 是不同进程；在没有单一产品启动语义前，虚构统一入口只会隐藏实际运行边界。

## 18. 实施记录

| 项目 | 状态 | 记录 |
| --- | --- | --- |
| 根工程环境 | 已完成 | 使用根 `pyproject.toml` 与 `uv.lock`，`tool.uv.package = false`。 |
| 移除多包结构 | 已完成 | 代码迁至根级 `server/`、`engine/`、`gateway/`，不再采用 `packages/`、`src/`、`seekino/` 包装。 |
| 模块导入边界 | 已完成 | 根级内部模块命名空间；`__init__.py` 不承担 barrel export。 |
| 测试与评测归位 | 已完成 | 测试集中在根 `tests/`，召回评测产物归入 `evals/`。 |
| MDV 设计基线 | 已完成 | Reference `v_0b6632b1a51f0e7b7269a8eabb328367` 与 Document `v_97d26af35995f1ac340fba8a7973e774` 已提交且 aligned；Document 精确绑定该 Reference Version。 |
| Stage 2 数据模型与事务意图 | 已完成（SQLite 验收） | 已实现持久化 `IndexSyncIntent`、generation 并发防护及与事实写入同事务的更新逻辑。 |
| Stage 2 Qdrant Storage | 已完成（内存 Qdrant 验收） | 正式 adapter 位于 `server/storage`，已覆盖稳定 ID、显式 named-vector schema、精简 payload 和环境隔离校验。 |
| Stage 2 Sync/Retry/Reconcile | 已完成（SQLite + 内存 Qdrant 验收） | 已实现幂等同步、故障留痕与退避、reconcile、rebuild，以及迟到外部副作用后的重新收敛。 |
| 自动化测试 | 已完成 | 全仓回归持续通过；精确数量与最近命令见 `transfer.md`。 |
| 真实基础设施验收 | 部分完成 | 已在隔离 MySQL 8.4 验证新 Schema、分片完成事务、并发 Claim 与 lease fencing；真实新视频的模型调用纵向联调仍待执行。 |
| Server API / Worker | M1 已完成 | 已实现 Series/Upload/Job/Search/Range/SSE API，以及 MySQL DB Queue Worker、chunk checkpoint 和精准 Index Sync。 |
| Web / Deploy | 未开始 | 属于后续产品化阶段；当前不创建空目录。 |

本表记录实现事实，不用“规划完成”替代代码与测试完成。隔离 MySQL 8.4 已完成 Schema、事务回滚、分片 Complete、同 Series 并发 Claim 与 lease fencing 验收；仍需用一条真实新上传视频完成模型解析到正式检索的纵向联调，并补独立 Qdrant 故障恢复验收。
