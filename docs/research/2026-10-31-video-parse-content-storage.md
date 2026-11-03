# Video 原片信息与解析成果的存储边界调研

日期：2026-10-31。范围：比较在 `video` 增加完整 ASR JSON 与新增 `video_parse_content`；核对 MySQL 官方机制、Google 官方设计建议和 Seekino 现有证据。关联 [本地 ASR 主设计](../design/03-product-reassessment/local-whisperx-asr-trd.md)、[Draft PR #18](https://github.com/owariband/Seekino/pull/18)，分支 `fix/video-asr-recovery`。

本文是研究依据，不是新的 TRD、实施报告或问题状态入口。源码核对基于 `67223c16` 工作树；只读源码与历史记录，未查询运行数据库、运行模型、修改业务代码、执行迁移或做性能实验。

## 判断与适用边界

建议把论证重心放在**原片实体与一次解析成果的基数、生命周期、读写方式不同**。新增成果表能够明确表达这些边界；“Video 列数多，所以 MySQL 一定慢”不是成立的理由。

`video` 与解析任务已经是 1:N；拟议成果与解析任务是 1:1。这首先是按业务实体和版本职责建模，同时隔离大正文，不只是把同一条记录机械地垂直拆成两个 1:1 表。它不等同于分库分片，也不能直接借用分布式数据库的吞吐结论。

两种候选都能实现合法持久提交和恢复：直接放 `video.asr_result` 时仍需携带、核对对应任务及模型/输入身份；独立成果表使这份身份和归属更自然，但建表本身不提供恢复正确性。

## Seekino 源码与历史证据

### 现有实体和访问路径

`Video` 定义了 17 个业务字段，并继承 4 个公共时间字段。下表按职责归类，不把列数作为性能证据。[Video 模型](../../server/model/video.py)、[BaseModel](../../server/model/base.py)

| 职责 | 字段 | 说明 |
| --- | --- | --- |
| 标识、归属与顺序 | `id`、`series_id`、`episode_order`、`upload_sequence` | `episode_order` 也可能由 Summary 建议调整，不是严格不可变的原片属性 |
| 原文件及媒体事实 | `filename`、`source_uri`、`hash`、`size_bytes`、`duration_ms` | 与输入资产本身关联 |
| 播放/推理媒体引用 | `proxy_uri` | 派生媒体引用，不是 ASR 解析正文 |
| 上传状态投影 | `upload_status`、`upload_progress` | 面向上传流程 |
| 当前展示信息 | `display_name`、`description`、`cover_uri` | Summary 可以发布到这些字段，普通列表消费当前采用值 |
| 解析状态投影 | `parse_status`、`parse_progress` | 面向当前解析进展展示 |
| 公共时间 | `create_time`、`create_date`、`update_time`、`update_date` | 继承的审计/维护字段 |

`VideoParseTask` 对 `(video_id, version)` 有唯一约束，同一原片可以拥有多个解析版本；`ChunkParseContent` 则按 `chunk_task_id` 唯一关联一次 Chunk 任务。这为按解析任务保存整片成果提供了现成建模基础。[任务模型](../../server/model/task.py)、[Chunk 成果模型](../../server/model/parse_content.py)

源码中 `LibraryRepository._video_states()` 使用 `select(Video, …)`，`video_parse_task.get_video()` 也使用 `select(Video)`。**源码推断：**若直接把 ASR 作为普通映射列加入 `Video`，同时保持这些实体查询不变，大字段会被纳入这些查询；需要显式列投影或延迟加载避免。尚未针对候选 DDL 执行 SQL 或测量传输、解码、内存成本。[Library 查询](../../server/repository/library.py)、[解析读取 Video](../../server/repository/video_parse_task.py)

Summary 当前已持久化：其提交结果和帧回执保存在任务 `detail` 中，同时发布当前标题、简介和封面地址到 `video`。因此，不能把 `video.display_name/description/cover_uri` 都视为应该删除的错误字段；版本化生成成果与当前展示投影有不同用途。[Summary 完成提交](../../server/service/video_parse_summarize.py)

### 本项目已有的大 JSON 教训

**B14 历史现场：**2026-10-18 的 Event 索引补建窗口约 45 分钟，记录 206,137 个 `UPDATE_ROWS` 事件、44,105,531,159 bytes 全部 binlog 事件。仅 `chunk_parse_content` 的 UPDATE 事件字节量就约 33.831 GB，`video_parse_task` 约 10.092 GB。历史记录把原因定位到逐事件/逐帧反复重写累计 JSON、无变化更新时间和当时 `ROW + FULL` 行镜像；UPDATE 事件数不等于 SQL 条数、事务数或更新行数。这是历史负载证据，不是本轮 ASR 存储测量。[B14 问题记录](../dev/problems.md#b14event-索引补建反复重写大-json45-分钟产生-2061-万个-update-事件与-44106-gb-binlog)

**B13 历史现场：**任务列表为了存在性、版本和少量标记，读取了完整 Chunk 成果实体；当次 23 个任务、501 份 Chunk 成果的正文与 detail 合计约 20.2 MiB/次数据库读取。这不是浏览器响应大小，也不能把当时页面全部延迟归因于这部分。历史 EXPLAIN 未显示关联索引缺失是主因。[B13 问题记录](../dev/problems.md#b13解析启动负载尖峰codex-重复加载-mcp-与任务列表过量读取)

这两个反例都包含已经独立的 `chunk_parse_content`：**有成果表能够表达职责分离，但不会自动防止整包重写、无变化写入和过量读取。** 不能仅凭该表已存在，就声称新表已经证明性能或健壮性。

## 一手资料核对

### MySQL 官方确实讨论过按需拆开大正文

MySQL 8.4 手册建议：如果某些查询不需要 BLOB 列，可以考虑把该列存入单独表，在需要时 join，以降低这些查询的内存需求。措辞是根据查询需要考虑拆分，不是所有大列都必须拆表。[MySQL：Optimizing for BLOB Types](https://dev.mysql.com/doc/refman/8.4/en/optimize-blob.html)

**对本项目的推断：**视频列表/上传逻辑与整片 ASR 恢复对正文的需要不同，这一原则与候选设计有关。手册针对 BLOB；ASR 拟使用 JSON，不能把它当成 JSON 列性能已验收的证明。

### Google 的原则同样依据访问模式，且同时说明合并的收益

Google 官方 Spanner 白皮书建议经常一起读写的数据尽量靠近；相关但不经常一起访问的数据可以分开，尤其是较大的低频数据。它明确提示，跨分布式分片的事务和读取也有额外成本。[Google：Optimizing Schema Design for Spanner，Tradeoffs of locality](https://docs.cloud.google.com/spanner/docs/whitepapers/optimizing-schema-design#tradeoffs_of_locality)

**可借鉴范围：**根据访问模式、大小和更新方式选择边界，而不是“拆得越细越好”。Spanner 的 interleaving、分片和两阶段提交成本不直接适用于 Seekino 的本地 MySQL；本记录不据此建议分布式化或承诺 MySQL 提速。

### InnoDB 已有行外存储，不能假定大字段全部塞在主记录页里

`ROW_FORMAT=DYNAMIC` 可将长 `VARCHAR/VARBINARY/BLOB/TEXT` 值存到行外，聚簇索引记录只保留 20 字节指针；是否行外存放取决于行总大小和页大小。因而不能简单推导“加了 2 MiB 大字段，每次只读标题也必然从磁盘读取完整 2 MiB”。[MySQL：DYNAMIC Row Format](https://dev.mysql.com/doc/refman/8.4/en/innodb-row-format.html#innodb-row-format-dynamic)

**对本项目的限制：**仍需检查实际 row format、SQL 投影、ORM 映射和运行负载；本轮没有查询实际数据库。物理行外存储也不等于应用已经避免 SELECT 和反序列化大字段。

### JSON 有专门存储和更新规则，不应与文本文件字节量混同

MySQL 原生 JSON 使用内部格式，存储空间大致与 `LONGBLOB/LONGTEXT` 相当，可用 `JSON_STORAGE_SIZE()` 测量，并受 `max_allowed_packet` 限制。部分原地更新要求使用 `JSON_SET/JSON_REPLACE/JSON_REMOVE` 等且满足其他条件；直接赋整个 JSON 不属于这种部分更新。[MySQL：The JSON Data Type](https://dev.mysql.com/doc/refman/8.4/en/json.html)

**对本项目的推断：**ASR 完成后少次写入与把 ASR 放进高频整体改写对象是不同方案。拆表后仍应保持阶段提交、无变化不写和避免无关更新重写正文；只改表名不会消除 B14 的机制。文件的紧凑 UTF-8 大小也不能当成真实数据库页、binlog 或应用内存大小。

### 加列不一定重建大表，拆表也不自动消除锁冲突

MySQL 8.4 的 `ADD COLUMN` 可使用 `INSTANT`，这是默认算法，但存在 row format、组合操作、行大小等限制。不能把“每次加列都会重写整表、长时间停机”作为本项目选择独立成果表的既定事实。[MySQL：Online DDL Operations](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html#online-ddl-column-operations)

InnoDB 记录锁锁定索引记录，不是只锁被修改的某一个业务列。[MySQL：InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html#innodb-record-level-locks)

**对本项目的推断：**把不同更新对象拆开可能改变竞争范围，但提交成果仍可能需要锁任务、核对 Video、发布展示字段；外键、锁顺序、索引与事务长度都影响结果。必须验证实际 SQL 和并发，不能声称分表后两层互不干涉。

## 如何补充两种候选的比较

| 比较点 | 在 Video 增加 ASR JSON | 新增按解析任务关联的 video_parse_content |
| --- | --- | --- |
| 初始改动量 | 较小：列、读写和恢复逻辑；若避免现有实体查询携带正文，需要处理投影 | 较大：新模型/表、Repository、关联读写和恢复逻辑 |
| 版本与身份 | 一行 Video 对应多个解析版本；一个“当前结果”列需要明确任务身份、覆盖与保留规则；把版本映射塞进 JSON 也是额外逻辑 | 成果直接归属于一次任务，沿用现有 Chunk 的任务/成果模型 |
| 后续解析扩展 | 解析字段或嵌套合同演进继续触及源视频实体；不代表每次都必须修改上传算法 | 解析结果 Schema 可主要在成果模型、保存/读取和消费者范围内演进 |
| 普通读取负担 | 可以靠窄查询/延迟加载控制，不能先断言必然变慢 | 不 join/不加载成果时，普通 Video 实体查询自然不携带 ASR；仍须防止上层无条件 join |
| 工程边界 | 需要人为区分原片事实、展示值、计算结果及各自写入者 | 存储边界更明确；删除、权限、发布、任务租约等仍有必要协作 |
| 新增代价 | 当前结果覆盖、历史版本归属和实体读路径审查 | 迁移/兼容、额外关联读取、跨对象事务、部分完成状态及删除保留策略 |

这里的“逻辑干净”可以具体检查：新增一个解析输出，是否需要改 Video 模型、上传写入、普通列表 DTO；是否存在只修改模型结果却必须理解上传字段的代码路径。不能用主观评分代替这些变更范围。

“冗余”也应具体化：异质字段同表首先是职责/生命周期混合，不自动构成重复数据；版本化 Summary 与当前展示值的有意投影需要同步合同；再保存完整 Chunk 拼接 Schema 才会引入派生副本、来源版本和失效问题。不能把这几种代价合并成一个“宽表问题”。

## 选择前后的验证边界

1. 核心功能：ASR 成功提交后，下游失败、暂停继续、重启恢复均新增转写 0 次；错误输入/模型/Schema 不误复用，旧租约迟到不提交。
2. 读路径：视频列表、上传进度、普通任务轮询不 SELECT/解码完整 ASR；记录查询投影、返回字节量及恢复时的加载次数。单独表仍须通过此项。
3. 写路径：无成果变化时不重写正文；不把心跳/模型调用占位放进大正文对象；记录同任务完整解析和恢复的正文更新次数、binlog 增量与事务耗时。
4. 建模与兼容：同一 Video 的不同解析版本不会互相覆盖；失败任务保留已完成成果；Summary 的版本结果与当前展示值对应；历史 Chunk/Summary 仍可读。
5. 性能和代价：用代表性长课产物测量两种候选的正常列表读取和恢复读取；显式控制 SQL 投影、数据集、缓存、数据库配置。没有本地对照前，仅可判断结构和机制，不宣称吞吐或锁等待改善。

本轮结论支持把 `video_parse_content` 作为有依据的候选：它主要解决成果归属、版本和访问边界，收益需要靠实际提交/读取策略兑现。MySQL 和 Google 资料提供了相关设计原则，B13/B14 提供本项目的真实失败机制；都不支持“字段多就必须拆”“拆表后自动稳健”或“改解析再也不会影响 Video”的绝对说法。
