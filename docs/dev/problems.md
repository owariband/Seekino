# 项目问题清单

更新日期：2026-10-14。依据源码、运行日志和验证结果，区分已修复缺陷、功能缺失和待联调项。本文件同时记录问题根因、已实施方案与待处理方案，实际状态以每项标注为准，不扩展当前开发范围。此前补充 B07 的 Codex 原生 Refine 超时诊断及 B08 的数值编码改动；随后记录 B08 的索引版本策略、Embedding 独立存储要求与精度／差异来源的证据边界。2026-10-12 在 B07 记录 MYGO EP03 完整完成的耗时收敛里程碑，以及索引进度权重降至 5%。2026-10-14 新增 B09，记录同剧集并发完成上传时的 MySQL 死锁、锁定对象、最小修复和真实 MySQL 回归；运行服务尚未重启加载修复。新增 B10，记录解析停服交接、重试预算和恢复调度缺口及当时任务快照，并补充 DB Queue／Redis 取舍、本地 RagFlow 两条执行路径和分批修复验收。其余条目保留各自日期下的历史状态，未作为当前工作区的全量复核。

## frontend

### F01：新建聊天、历史对话切换与删除

- 类型：已修复。
- 当前行为：SearchPage 显示 Workspace 内的 Conversation 列表，支持新建、切换和二次确认删除；刷新后恢复最后选中的 Conversation。切换时复用原 Hook 的订阅清理与迟到事件隔离，不另建 SSE 管理器。
- 删除语义：终态 Conversation 在事务中物理删除，Message 和 Timing 由 MySQL 外键级联清理。存在 `PROCESSING` 回答时返回 `409 MESSAGE_IN_PROGRESS`，不与运行中的 checkpoint / complete / fail 回写竞态。
- 依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx)、[useAgentConversation.ts](../../web/src/features/search/useAgentConversation.ts)、[client.ts](../../web/src/api/client.ts)。

### F02：前端允许 PARTIAL，后端只接受 READY

- 类型：确定的前后端契约缺陷。
- 现状：SearchPage 的 `isSearchable()` 同时允许 READY / PARTIAL；Agent 的范围解析与基础检索都严格要求 READY。
- 复现：显式选择 PARTIAL，或 Workspace 只有 PARTIAL 时空选发送，均返回 `409 SERIES_NOT_READY`，没有执行模型或检索。
- 影响：页面显示可检索、允许发送，实际请求一定失败。
- 修复方向：前端按当前后端契约只允许 READY；已选动画失效后仍能取消选择，禁止发送且不能静默扩大为全部动画。正式开放 PARTIAL 是另一项业务功能，需要同步设计可检索 Video 范围。
- 依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx)、[agent_chat.py](../../server/service/agent_chat.py)、[video.py](../../server/repository/video.py)。

### F03：解析失败后的“重试”没有重跑解析

- 类型：已修复。
- 当前行为：上传传输失败仍由上传面板恢复；解析主 Job 最终 FAILED 时，任务卡调用 `uploads/retry-parse`，以 `uploadSessionId + expectedJobVersion` 将原 Job 重新排队。接口保留阶段、进度、Checkpoint、attempt 与用量历史，并以版本条件拦截并发或响应丢失后的重复投递。
- 补充行为：任务页另提供“整集重新解析”。`uploads/reparse` 只接受已完成上传的 SUCCEEDED / FAILED 主任务，清空 Checkpoint 后从视频开头运行；attempt 与用量历史保留，旧成功快照在新解析导入前仍可检索。
- 依据：[TasksPage.tsx](../../web/src/features/tasks/TasksPage.tsx)、[uploads.py](../../server/api/uploads.py)、[upload.py](../../server/repository/upload.py)。

### F04：刷新后缺少恢复未完成上传的完整交互

- 类型：功能缺失。
- 现状：上传队列与源 File 对象只在页面内存中。后端保存缺段信息，资料库可以列出 pendingUploads，但页面没有重新选取源文件、绑定已有 uploadSessionId 并恢复上传的入口。
- 影响：当前页面中的暂停／继续可用，页面刷新后的恢复体验尚未交付。
- 修复方向：从服务端读取未完成上传，重新选择并验证源文件身份后绑定已有 Session；按 missingSegmentNumbers 续传，不能只比较文件名和大小就认定内容一致。
- 依据：[UploadProvider.tsx](../../web/src/features/uploads/UploadProvider.tsx)、[LibraryPage.tsx](../../web/src/features/library/LibraryPage.tsx)。

### F05：任务页将轮询描述成 SSE

- 类型：确定的展示文案缺陷。
- 现状：TasksPage 的任务列表使用 jobs/list 每 5 秒轮询，工具栏却写着“SSE 实时更新”。上传面板订阅指定 Job SSE 是另一条已实现路径。
- 修复方向：文案准确说明定期刷新。不要为匹配文案增加当前未实现的 Workspace 级 SSE。
- 依据：[TasksPage.tsx](../../web/src/features/tasks/TasksPage.tsx)。

## backend

### B01：Agent 遗留 PROCESSING 永久阻塞对话

优先级：高。类型：执行生命周期和故障收尾缺失。状态：待修复。

#### 问题是什么

`AgentRepository.start_message()` 先在数据库中保存 USER 和 PROCESSING ASSISTANT，然后 Service 将执行放入 ThreadPoolExecutor。消息状态持久化了，执行任务和控制对象仍只在内存中。

```text
数据库提交 PROCESSING
    → 进程异常退出，内存任务消失
    → 数据库仍是 PROCESSING
    → 下次发送发现该对话已有处理中消息，返回 409
```

还有一个同类触发条件：任务已经结束，但 COMPLETED 和 FAILED 都写库失败；finally 仍移除内存任务，数据库恢复后没有机制补做失败收尾。

这里的 409 门禁本身有必要，它阻止同一 Conversation 同时执行两个 Kick。问题是没有为失去执行的消息提供退出 PROCESSING 的路径。REST / SSE 快照只能读取遗留状态，不能恢复任务或解除门禁。

#### 推荐的最小修复

当前先保证遗留消息能够失败结束、用户能够继续发送；不自动重放同一消息的模型请求。

1. **保存一个固定截止时间。** ASSISTANT 创建时持久化内部字段 `processingDeadlineAt`，表示本次请求必须结束的绝对 UTC 时间。预算从受理开始，覆盖线程池排队和执行，不能每个 Turn 或每次 Heartbeat 重新延长。
2. **补过期收尾。** 后台定期扫描过期的 ASSISTANT PROCESSING；发送请求前也先处理当前 Conversation 的过期消息，且必须发生在幂等消息查找之前，否则旧 Key 仍会提前返回 409。状态变为 FAILED，清空 phase、递增公开 version、写入明确的超时错误，保留已经保存的正文和证据。
3. **在事务中确认过期。** 只更新仍为 PROCESSING 且截止时间已到的消息。已经完成／失败的消息不能再修改；未到截止时间的有效执行仍保留 409 门禁。多个 API 实例同时扫描只应产生一次终态变化。
4. **将写入保护与截止时间对齐。** 当前 checkpoint / complete 已有行锁与 PROCESSING 检查，fail 也只改变 PROCESSING，不需要凭空重做一套状态体系。checkpoint / complete 还需要检查有效截止时间，避免截止已过、扫描尚未执行时迟到结果仍提交 COMPLETED。普通异常 fail 仍可终结 PROCESSING；过期收尾另要求截止时间已到，不能给普通 fail 错加“尚未超时”的限制。
5. **同步停止迟到输出。** Service 将剩余预算传给 Harness，排队结束后不能让 kick 重新获得完整 180 秒。progress / evidence / delta 发布前检查同一截止时间；本进程收尾时触发 cancellation。确认 FAILED 已提交后才发失败事件，状态无法确认时沿用 interrupted 和快照恢复。

这项修复保持同一 messageId 只执行一次，也不将终态改回 PROCESSING，因此不需要为它引入 executionVersion、自动接管或 LangGraph 执行恢复。原 Idempotency-Key 仍返回原失败消息；用户的新请求使用新 Key，在同一 Conversation 中继续。

#### 实现时必须注意

- 不能在服务启动时把所有 PROCESSING 一律改为 FAILED；其他 API 实例可能仍在正常执行。判定依据是持久化截止时间，不能只看当前进程的 `_executions`。
- 截止时间使用一致 UTC 时间源，多实例部署优先用数据库时间判定；获得行锁后重新读取当前时间，不能用等待锁之前的 now 判断截止。`monotonic()` 用于进程内剩余预算，不能持久化后跨进程比较。
- 当前 complete / fail 先锁 Message，再锁 Conversation。发送前的过期收尾应使用独立短事务并遵循相同锁顺序，提交后再进入 start_message；不能在持有 Conversation 锁时反向锁 Message。
- 仅有 SQL 保护不足以停止 SSE：当前 delta 约每秒才持久化一次，必须同时约束每次输出。写入被拒绝时读取已有终态，不再尝试保存 chunks 覆盖它；前端现有终态拒绝迟到增量的逻辑可复用。
- 历史 PROCESSING 没有截止时间，需要受控升级：停止并确认旧 API 执行进程退出后，将遗留处理中消息失败收尾，保留内容，再启用新版字段。不能在旧实例仍工作时盲目回填并判死。
- 取消为协作停止，阻塞的 Provider 请求仍依靠有限网络 timeout；失败收尾解除对话阻塞，不代表能够立刻硬杀线程或停止所有底层计算。

#### 修改归属

| 位置 | 拟修改内容 |
| --- | --- |
| server/model/agent.py | 内部截止时间字段和迁移要求 |
| server/repository/agent.py | 过期终结方法，沿用终态写保护并补截止判定 |
| server/service/agent_chat.py | 发送前收尾、定期扫描、取消与输出停止、已有终态处理 |
| agent/chat/video_search/runtime.py、agent/chat/common/state.py | 接收剩余预算，保持 Harness 独立于数据库／HTTP |
| server/api/app.py、bootstrap.py、config/agent.py | 扫描任务的启动／关闭与配置装配 |

#### 验收条件

- 保存 PROCESSING 后模拟进程退出，截止后另一实例将其置为 FAILED，同一对话可以发送新消息。
- 未过期任务不被另一个实例或定期扫描误杀；同一 Conversation 仍只能有一个有效 Kick。
- 完成与过期扫描并发时只产生一个终态；旧执行不能覆盖失败状态或继续发布正文。
- 排队耗尽预算的任务不再开始新的模型调用；旧幂等 Key 不会偷偷重跑模型。
- 终态写库暂时失败后，数据库恢复时扫描能够完成失败收尾。

依据：[agent_chat.py](../../server/service/agent_chat.py) 的 start_message / _execute / close；[agent.py](../../server/repository/agent.py) 的 start_message / checkpoint_assistant_message / complete_assistant_message / fail_assistant_message；[runtime.py](../../agent/chat/video_search/runtime.py)、[state.py](../../agent/chat/video_search/state.py)。

### B02：上传提交成功后，临时目录清理失败导致接口误报失败

优先级：中。类型：事务完成后的错误边界缺陷。状态：已复现，待修复。

#### 问题是什么

`UploadService.complete()` 完成校验、合并、发布原视频和探测，然后 `complete_upload()` 提交 Video、ParseJob 与 COMPLETED 上传状态。提交以后，Service 直接调用 `delete_upload_staging()`；该方法执行 `shutil.rmtree()`。

```text
Video + ParseJob + COMPLETED 已提交
    → 删除临时上传目录出现 PermissionError / OSError
    → 异常传到 API，返回错误
    → 用户看到失败，实际上传与任务已经成功
```

本轮离线故障注入结果：首次 complete 返回 `403 PERMISSION_DENIED`；查询数据库时上传已 COMPLETED 且 jobId 存在；再次 complete 返回 200。数据没有丢失，但首次结果误导用户，临时目录也遗留。

根因不是业务事务提交失败，而是附属清理操作的失败改变了已经成立的业务成功结果。

#### 推荐的最小修复

只在提交完成后的清理边界捕获 `OSError`（包含 PermissionError / FileNotFoundError），记录安全的 warning，随后返回原 `completed`。校验、合并、发布、探测或提交失败仍沿用现有失败路径，不扩大捕获范围。

```python
try:
    self._artifact_store.delete_upload_staging(session_id)
except OSError as exc:
    _logger.warning(
        "Could not remove completed upload staging",
        extra={
            "upload_session_id": session_id,
            "job_id": completed.job_id,
            "error_type": type(exc).__name__,
        },
    )
return completed
```

业务状态和错误字段保持已提交的成功结果，不调用 release_completion，不重新合并、不重新发布、不创建第二个 Job。遗留目录可后续单独补幂等清理重试或清理命令；不能为重试清理而重新触发解析。

修改归属：`server/service/upload.py` 的提交后清理位置。`LocalArtifactStore` 仍负责实际目录删除，不全局吞掉所有存储错误。

#### 验收条件

- 清理抛出 PermissionError / OSError 时，首次 complete 仍返回 200 和真实 Video / Job ID。
- 上传保持 COMPLETED；同请求重复 complete 仍复用相同 Video / Job。
- warning 能定位 uploadSessionId 和 jobId；原视频与 Job 工作目录不被清理。
- 清理成功路径不变；提交前的校验／合并／发布错误仍失败，不能因本修复被吞掉。

依据：[upload.py](../../server/service/upload.py) 的 complete；[artifact.py](../../server/storage/artifact.py) 的 delete_upload_staging；[errors.py](../../server/api/errors.py) 的 PermissionError 映射。

### B03：Lite ASR 点时间戳被候选转写 Schema 误判

优先级：高。类型：输入契约过度校验。状态：已修复（`dabcb8d`）。

#### 问题与根因

上传完成后的解析阶段频繁出现结构化结果解析错误。实际失败点不在上传链路，也不是 Lite Provider 本身不可用，而是 `TranscriptCue` 曾强制要求 `end_ms > start_ms`。Lite 返回 `start_ms == end_ms` 的点状 Cue 时，结果会在候选转写反序列化阶段被 Pydantic 拒绝，后续解析尚未开始就进入失败／重试，因此日志中表现为同类解析错误连续出现。

这里混淆了两种不同契约：原始 ASR Cue 只是候选文本及粗粒度定位，不应被要求伪造一个正长度区间；`TimelineEvent`、AnalysisWindow 和 canonical VideoChunk 才需要严格的正区间不变量。Cue 字段中的 `_ms` 是视频内相对时间的内部表示，不是 Chat Submit 的 Unix 毫秒时间戳，也与 ParseJob 的处理耗时无关。问题不在于 Lite 是否能输出“精确到 1 ms 的持续时间”，而在于系统不应在 Lite 输出入口强制这种语义。

#### 已实施方案

- 删除 `TranscriptCue` 的跨字段严格区间校验，不再在 Lite 候选转写入口判断起止关系，允许 `start_ms == end_ms` 的点状 Cue。
- 保留必要的结构校验：起止值仍须为非负整数、文本非空、未知字段仍被拒绝。没有把原始模型输出当作任意 JSON 直接传入后续链路。
- 保留 `TimelineEvent` 等正式时间区间的 `end_ms > start_ms` 校验；本次没有放宽 canonical 视频区间、事件区间或数据库区间约束。
- 原始 ASR 与数据库事实保持原值；只有检索层在构造要求正区间的公开 Evidence 时做软投影：点状 Cue 临时表示为 `[t, t+1ms)`，反向或损坏 Cue 跳过并记录 warning。该投影不回写数据库，也不会令 ParseJob 失败或重试。
- 增加点状 Cue 回归测试，明确 `TranscriptCue(start_ms=3000, end_ms=3000, ...)` 可被接收，同时原有反向 TimelineEvent 拒绝测试继续保留。

#### 验收结果

- Lite 点状 Cue 不再触发候选转写 Schema 错误和由此产生的重复解析失败。
- 没有为了通过 ASR 输入校验而改写原始 Cue，也没有把 Chat Submit 的绝对毫秒时间戳规则错误套到 Lite ASR；`[t, t+1ms)` 仅是检索 Evidence 的非持久化兼容表示。
- 当前全量测试验证为 `844 passed, 8 skipped`；跳过项均为未配置隔离测试库的 MySQL 集成测试。

依据：[schemas.py](../../engine/parsev1/schemas.py) 的 `TranscriptCue` / `TimelineEvent`；[video.py](../../server/repository/video.py) 的检索边界软投影；[test_schemas.py](../../tests/engine/parsev1/test_schemas.py) 与 [test_video_retrieval_service.py](../../tests/server/test_video_retrieval_service.py) 的回归测试。

### B04：ParseJob 逐 Attempt／逐 Stage 计时造成高频写库

优先级：高。类型：不必要的持久化复杂度与并发开销。状态：已修复（`c1ab11a`）。

#### 问题与根因

上一版计时设计新建了 `parse_job_attempt_timing` 和 `video_chunk_stage_timing`，并在 SPLIT、ASR、PARSE、TEXT_EMBEDDING、VISION_EMBEDDING、INDEX 等阶段分别开始和结束计时。每个 VideoChunk 的每个阶段都会产生额外事务，并行 ASR 还会争用同一 Attempt 计时记录；但当前产品实际只消费 ParseJob 的累计 Worker 处理耗时和该数值是否完整。

这套明细计时与 Lite Cue 的时间单位没有关系。Lite 的相对媒体定位不应为了 ParseJob 性能统计而满足毫秒级正区间；ParseJob 的 `processing_duration_ms` 则是 Worker 使用单调时钟测得的执行耗时，两者必须保持解耦。

#### 已实施方案

- **没有新建计时表。** 在现有 `series_parse_job` 上增加 `processing_duration_ms` 和 `processing_timing_complete` 两个字段。
- Consumer 使用单调时钟测量一次 Attempt 的执行耗时。每个 VideoChunk 到达 PARSED checkpoint 时，复用现有 checkpoint 事务，以“此前 Attempt 累计值 + 当前 Attempt 已运行时间”为绝对目标单调更新 `processing_duration_ms`；`acknowledge` / `retry` 再以同一口径结算该 Attempt 的最终值，避免重复累计。
- Lease 过期时无法可靠知道完整耗时，因此不猜测数值，只将 `processing_timing_complete` 永久置为 `false`；后续重试成功也不会把历史缺口误恢复为完整。
- 删除逐 Attempt／逐 Stage 的 Model、DTO、Repository、Service 和 `VideoProcessingService` 阶段计时包装，消除每个 Chunk、每个 Stage 的额外写库及锁竞争。
- API 继续返回任务级累计处理耗时；运行中的任务保持 `processing_timing_complete=false`，其当前下界按 PARSED chunk 推进，前端按 RUNNING 状态显示“进行中”。任务结束后若因 Lease 过期或历史记录缺失仍不完整，则显示“至少 ...（不完整）”，不把已知下界伪装成精确总耗时。
- 这次 chunk 粒度刷新没有新增表、字段或独立事务，只复用现有 ParseJob 字段和 checkpoint 保存事务。

#### 受控升级

[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 是一次性迁移，执行前必须停止 Parse Worker；脚本会在仍有 `RUNNING` Job 时主动中止。

迁移直接给 `series_parse_job` 增加两个字段。若旧明细表已经部署，则仅在 Attempt 记录数量、序号 `1..N` 和完整性全部一致时汇总旧耗时；否则保守标记为不完整。若旧明细表从未部署，已经运行过的历史 Job 同样标记为不完整，不会误报成“0 ms 且完整”。完成折叠后删除两张旧明细表。

#### 取舍与验收结果

- 保留任务级累计处理耗时，并按 PARSED chunk 更新；仍放弃逐 Attempt、逐 Chunk、逐 Stage 的持久化诊断明细，这是有意的性能／可维护性取舍。
- 成功、失败重试、最终 Lease 过期以及“过期后再次执行仍保持 incomplete”均有回归覆盖。
- 全量测试结果为 `841 passed, 8 skipped`；本地 MySQL、Qdrant、API、Web 均已启动，`GET /healthz` 返回 `status=ok`，Parse Consumer 为 `running`。

依据：[job.py](../../server/model/job.py) 的 Job 字段；[consumer.py](../../server/mq/consumer.py) 的单调计时；[mysql_queue.py](../../server/mq/mysql_queue.py) 的原子累计、重试与 Lease 过期处理；[job.py](../../server/service/job.py) 的公开完整性语义；[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 的受控迁移。

### B05：Parse Agent 边界被模型档位和 fallback 隐式耦合

优先级：高。类型：解析编排与恢复边界不清。状态：已按算法 Pipeline 与生产持久化重新划分，数据契约迁移待后续处理。

#### 问题与根因

旧编排先把 Lite／Pro 这样的 provider tier 当成流程角色，ASR、视频理解和内容审核 fallback 的职责彼此渗透；随后又把完整阶段拓扑放进 `VideoProcessingService`，导致 Server 同时拥有 Job 生命周期和算法流程，Engine 退化成一组无法从目录看出顺序的 Agent。前者无法判断一次视频会调用哪些能力，后者则让算法演进被 Lease、Repository 和 Artifact 细节包围。

#### 已实施方案

- Engine 以显式 `ParsePipeline` 拥有 `transcribe → understand → embed → persist` 顺序。ASR 阶段跨 VideoChunk 有界并发并按序交付；Understanding 阶段按序演进 Context；Embedding 阶段构造检索文档并生成 Text/Vision 向量。
- `persist` 是算法 Pipeline 的提交端口，不是 Engine 内的数据库实现。Server 实现 `ParsePersistence`，在当前 Lease 下完成 Parse Artifact、Checkpoint 和 MySQL 事务；Pipeline 返回后的 Qdrant 派生索引同步仍由 Server Job Adapter 负责。
- Server 的 `VideoProcessingService` 只承担 Job Adapter：加载任务与已提交前缀、校验原视频、管理 Lease/Fencing、提供生产 `ParsePersistence`，并把最终业务 ID 返回给 Consumer。它不再定义 ASR、理解和 Embedding 的算法拓扑。
- Engine 契约只使用算法 DTO、路径和 `sequence_no`，不引用 `ParseJobLease`、Server Checkpoint DTO、Repository、Artifact URI 或 Index Service；Server 将 Pipeline 输出映射到稳定业务 ID 和持久化模型。
- provider、model 与 tier 只保留为配置和 provenance，不决定拓扑。内容审核 fallback 只属于 understand 阶段，复用 transcribe 阶段交付的同一份 Transcript；显式空 Transcript 不会触发补跑。
- Pipeline State 只是当次算法执行内存。异常仍由 ParseJobConsumer 和 MySQL Queue 发起下一 Attempt；MySQL Checkpoint、已提交 Artifact 与 Lease/Fencing 是跨 Attempt 的唯一恢复真相。

#### 尚未迁移的契约与已知恢复缺口

- 本轮**没有**把 ASR raw DTO 迁移为 `start_time` / `end_time` 字符串，也没有新增“原始 ASR”与“经理解校正后的 canonical transcript”两套独立类型和存储。当前 `TranscriptAnalysis` / `TranscriptCue` 仍沿用 `start_ms` / `end_ms` 整数契约；这项数据模型重构需单独设计和迁移。
- 生产 ASR 阶段当前没有独立 durable checkpoint，结果只保存在本次 Pipeline State。Understanding 失败后，下一 Attempt 会恢复已经持久化的连续 PARSED 前缀，但会为剩余窗口重新执行 ASR；即使上一次已经在内存中完成这些 Transcript，也无法跨 Attempt 复用。

依据：[pipeline.py](../../engine/parsev1/pipeline.py) 的算法阶段顺序；[transcribe.py](../../engine/parsev1/transcribe.py)、[understand.py](../../engine/parsev1/understand.py)、[embed.py](../../engine/parsev1/embed.py) 和 [persist.py](../../engine/parsev1/persist.py) 的阶段契约；[video_processing.py](../../server/service/parsev1/video_processing.py)、[consumer.py](../../server/mq/consumer.py) 与 [job.py](../../server/repository/job.py) 的 Lease、Checkpoint 和恢复边界。

### B07：视频解析成本高昂且耗时超过片长数倍

优先级：高。类型：解析性能、单位经济性与失败止损问题。状态：2026-10-12 已取得真实整集完成的耗时收敛里程碑；历史问题、已实施修复与仍待优化的边界按下列日期分别记录。

#### 2026-10-12：整集解析耗时收敛里程碑，MYGO EP03 完整完成

状态：有界正文批读与索引版本幂等策略已实施，真实整集的理解、Refine、索引发布均完成，当前长耗时主路径已显著改善。此处的“解决”对应本次任务正常完成及下列已实施行为，不表示所有并发重算、短错误或语义质量问题都已消失。

##### 最终运行证据与时间口径

- Parse Task：`20b05d4c-becf-4fce-b744-0d3ac8f2f8e1`，MYGO EP03；父任务 `attempt_count=1`，终态 `COMPLETE`。理解 **24/24**、Refine **22/22**、索引 **22/22**，完成单元 **68/68**。索引完成意味着当前成员版本已通过发布／查询可见性校验并持久提交，不只是 SDK 返回 completed。
- 此前卡在索引的 EP01（`d470e300-9813-4c19-a62e-f52503e2e1ae`）与 EP02（`5584368a-3d77-4fad-b11b-3b8c3f9e249b`），当日 14:49 的已采集快照也确认均为 `COMPLETE`、索引 22/22。它们保留历史失败执行，时长完整性仍为 false，不能把旧累计值当作本次单轮完整基准。
- 开始：`2026-10-12 14:45:27.792 +0800`；完成：`15:13:43.821 +0800`。墙钟 **28 分 16.029 秒**；全链有效执行区间并集 **28 分 13.979 秒**（`1693979ms`），账本 `complete=true`、所有执行已闭合，页面按整秒显示 **28 分 13 秒**。
- 仅 Understanding 与 Refine 的执行区间并集为 **26 分 59.496 秒**。用户提到的“26 min”与这一口径对应；它不等于含 Index 的全链耗时。Index 在解析结束后增加的尾段为 **1 分 14.483 秒**。
- 本轮冻结模型为 `gpt-6-astra`、`reasoning_effort=low`，原生 invocation deadline 仍为 600 秒；视觉输入为 `ordered_frames`，最多 32 帧、max_height=720。没有通过扩大 timeout、自动重试或 fallback 预算来解释这次收敛。
- 用户回顾的整集体验为约 **90 分钟 → 异常样本约 110 分钟 → 优化后仍有错误的约 37 分钟 → 本轮解析主体约 26 分钟**。这些包含不同集数、执行轮次、错误重试及记录完整性；不能当成同片、同配置的严格 A/B 加速比。本轮完整有效时长以 **28 分 13.979 秒**为准，也不要与下面 2026-10-09 的“90 秒视频耗时 37 分钟”样本混用。

| 本轮阶段 | 执行记录 | 阶段时间区间并集 | worker 累计工作时间 |
| --- | --- | ---: | ---: |
| 完整 chunk（含 ASR、理解及宿主执行） | 24，全部 completed | 16 分 27.492 秒 | 52 分 04.593 秒 |
| Refine lease | 22，全部 completed | 19 分 22.296 秒 | 51 分 01.657 秒 |
| Index（含 embedding、媒体准备与 ES 发布） | 24：22 completed、2 failed 后恢复 | 3 分 08.736 秒 | 8 分 14.165 秒 |

阶段之间并行交错，**三个阶段并集不能直接相加**；worker 累计量也不是整集墙钟时间。

本轮 ModelUsage 共 **169 条，全部 completed、0 failed**：

| 调用 | 次数 | 单次平均 | 单次最长 |
| --- | ---: | ---: | ---: |
| ASR | 24 | 27.763 秒 | 43.439 秒 |
| Codex Understanding invocation | 24 | 96.058 秒 | 117.180 秒 |
| Codex Refine invocation | 42 | 72.849 秒 | 104.083 秒 |
| 文本 embedding | 37 | 0.206 秒 | 0.534 秒 |
| 视频 embedding | 42 | 7.400 秒 | 9.587 秒 |

Codex invocation 包含整个 native agent 执行，不等于一次底层采样。Refine lease 平均为 139.166 秒、最长 370.275 秒，包含窗口内多次 invocation；不能与上表单 invocation 的平均值混用。Embedding 本身为亚秒／秒级，Index 总时长还包含非模型工作，不能把索引拖尾都归因于 embedding 模型慢。

##### 已实施的解决方案

1. **Canvas 支持有界批量读取正文。** `get_context_canvas` 接受 `references=[...]` 与明确 section，按零基 `start/end` 项范围读取（end 不包含），批读不使用 cursor；已知正文 section 可直接读取，省去每个事件先 overview 再 content 的双重调用。整个 JSON 回执限制为 **8,000 o200k_base tokens**，预算不足时裁剪后续项并返回明确 warning／truncated／complete_end，要求缩小读取范围；简略项不能当作已完整核验。写入继续复用既有原子批次，保留 revision、issued references 和提交快照校验。减少逐项调用也减少同步 PostToolUse 的触发次数；没有关闭或修复托管 hook 内部实现。
2. **索引以身份、版本、完整性和可见性决定完成。** 对当前正确版本的 ES 文档直接验证、复用，跳过重复媒体构建与 embedding；缺失或较旧版本才发布，较新版本拒绝旧目标覆盖。修正了“同版本缓存数值差异 → 重写相同 external version → 失败重放”的决策路径，同时保留租约、SQL 当前版本和 ES 外部版本防护。精度保存与差异来源研究独立保留，不将新 JSON 编码、重新生成向量或推进 generation 作为所有任务恢复的前提。
3. **总有效时长覆盖完整业务链。** 使用同一 execution_timing 账本记录 Understanding、Refine、Index 的实际执行区间，取并集；失败执行计入，排队、停机和等待重试排除。Index 全量完成或部分发布释放租约后，已观察到的计时样本仍可安全闭合；未观察到的历史执行不补造，记录不全时返回 `complete=false`。ModelUsage 保留单次模型调用起止，独立 EMBD 时长栏仍未在任务页展示。
4. **索引阶段进度权重改为 5%。** 理解与 Refine 在其余 95% 内按完成单元分摊；当前固定拓扑的公式为 `95 × (completedChunks + completedRefines) / 46 + 5 × indexedIntervals / 22`。理解与 Refine 全提交为 95%，索引 1/22 为约 95.2%，全部提交为 100%。原始完成单元计数仍为 68/68，百分比不再等于完成单元数除以 68。5% 是固定展示权重，不是 ETA 或精确的模型耗时比例。

##### 尚未解决的边界

- **Refine 并发快照冲突仍存在。** 本轮记录 **20 次冲突，涉及 10 个窗口**；42 次 Refine invocation 对应 22 个完成窗口与 20 次冲突重算。当前领取逻辑尚未排除共享成员的有效 Refine lease，不能记录为已实现互斥调度或已消除重复核验。此次没有模型超时，不代表未来不会再次超过 600 秒。
- **本轮并非零错误。** 两次 Index 执行分别在 96ms、47ms 后以 `VideoParseIndexCoordinationError` 失败，各恢复一次后完成；它们不是模型 timeout。最终 last_error 已清空，日志未保留具体错误消息，不能补写更细原因。
- 同版本缓存并发、重复 embedding 与批读实际调用次数的进一步优化仍需各自证据；托管 hook 内部等待原因、旧 Pro 空输出问题未被本次成功解决。完整完成与可检索状态也不替代人物、对白、动作等语义质量的独立验收。

本轮权重调整的离线验证：后端 Job Repository **32 passed**，TasksPage **18 passed**，覆盖 95% 解析／5% 索引、持久成果计数、未知拓扑及完成后 100%。没有新增真实模型调用、任务重试、服务重启、数据库迁移、commit 或 push；上述 EP03 运行证据来自本轮只读核验的既有任务。

依据：该 Parse Task 的最终 API 快照、ModelUsage／execution_timing 一致快照，以及 `outputs/local-services/api.log` 中 `14:45:27–15:13:43 +0800` 的记录；[Canvas 批读工具](../../agent/utils/context_canvas_tools.py)、[Refine 读取指引](../../agent/video_parse_v2/prompts.py)、[索引版本策略](../../server/service/video_parse_index_coordinator.py)、[ES 就绪验证](../../server/storage/elasticsearch.py)、[执行计时](../../server/service/video_parse.py)、[计时持久化与快照提交](../../server/repository/video_parse.py)、[进度权重](../../server/model/job_progress.py)、[任务汇总](../../server/repository/job.py)。下方历史条目及 B08 中“尚未恢复”的描述保留其采集时点，真实恢复与新集完成的最新结果以上述日期条目为准。

#### 2026-10-11：Refine 单项读取反复等待原生 PostToolUse，耗尽整次执行预算

优先级：高。状态：已定位一个超时样本的主要等待环节，待修复；托管 hook 内部为何耗时数秒仍未确认。以下为当日已采集的运行证据，不表示当前任务实时状态，也不替代后面的历史模型与 Pipeline 结论。

##### 现象与实际执行路径

- Parse Task：`d470e300-9813-4c19-a62e-f52503e2e1ae`；Video：`e709a2d8-aabb-509e-b323-e7f9aa7018b2`。本轮实际使用 `codex_local / gpt-6-astra`、`reasoning_effort=low`；Refine 跟随 Understanding 模型绑定。
- Gateway 对 native adapter 直接执行 `CodexAgentModel.generate()`，走 Codex 原生 app-server agent；Server 传入 `tool_harness="deepagents"` 不代表本轮经过 DeepAgents 图或其 hook。
- 当前 `request_timeout_seconds=600` 作为整个 native invocation 的 deadline，覆盖会话启动、模型生成、工具调用、工具后的 hook 等待和结果返回。到期由 AniVAG 终止 session，并报 `Codex Agent execution deadline expired`；不是一次供应商 HTTP 请求必然等待了十分钟。
- 已观测到的主要模式是逐项读取之间的同步等待：AniVAG 已将成功工具回执交给 Codex，调用工具的 Agent 脚本仍要等 PostToolUse 完成，才能拿到结果并继续下一次读取。

```text
Agent 脚本请求 get_context_canvas(item 1)
    → AniVAG 执行读取，将成功结果交给 Codex
    → Codex await PostToolUse hook
    → hook 完成后，结果才交回调用脚本
    → 脚本请求 item 2，重复上述过程
    → 整次 invocation 达到 600 秒 deadline，尚未形成持久提交
```

##### 完整超时样本与耗时口径

样本为 Refine `[19,22)`、`lease_version=2`，invocation `37723952-ebd2-4143-af64-db313743b4f7`。时间为 `2026-10-11 +0800`；原始采集于 `16:48:33` 核实覆盖整次 invocation，native 日志后续滚动不能视作仍可完整重放。

| 指标 | 已采集结果 |
| --- | --- |
| invocation 起止与耗时 | `16:10:06.460–16:20:06.436`，`599.976 s`，最终超时 |
| 成功事件读取 | 127 次：64 次 overview、63 次 content；涉及 64 个唯一事件，最后一个只读到 overview |
| 相同路径重复读取 | 0；该样本不是反复读取同一对象的模式 |
| 已闭合 hook | 126 段，时间并集 `481.954 s`，占整次 invocation 的约 `80.3%` |
| event 回执中段 | 首末成功回执跨度 `483.459 s`；其中闭合 hook 并集 `481.954 s`，其余合计 `1.505 s`，尚未细分读取、传输与推进 |
| 写入与提交冲突 | 采集范围内未观察到写入或 snapshot-conflict；该次超时发生在持久提交前 |

`481.954 s` 是 hook/started → hook/completed 生命周期区间的并集，不是 Canvas 执行时间、handler CPU 时间或已证实的网络等待。另有末尾未闭合 hook，未计入该值；整次执行其余约 118 秒尚未完整分解，不能直接标为模型推理时间。读取属于业务核验，不能把整次 600 秒都命名为“无效时长”。

##### 源码与本机配置确认的机制

1. **工具回执与脚本取得结果之间存在同步 hook。** 已安装 SDK 与 bundled CLI 为 `0.156.1`；对应官方 `rust-v0.156.1` 源码中，registry 先 await 业务工具，再 await PostToolUse，最后才返回结果。code-mode 内部工具调用也经过这条路径。PostToolUse 可以追加反馈或阻止原始结果继续到达脚本，因此同步方式会等待其决定；不是工具一返回就立即恢复脚本。
2. **这类动态工具默认串行，范围是同一个执行 runtime。** DynamicToolHandler 没有覆盖 `supports_parallel_tool_calls`，默认值为 false；同一个 code-mode turn worker 的 nested calls 共享 ToolCallRuntime，其写锁覆盖工具 dispatch 和后面的 PostToolUse 等待。不是按工具名称分别建锁，也不是四个独立 Refine process 共用全局锁。仅把单项调用改为 `Promise.all` 不会减少 hook 次数，在该版本的同一 worker 中仍受串行门禁约束。
3. **当前存在匹配全部工具的同步托管命令。** 本机 `/etc/codex/requirements.toml` 启用 hooks，PostToolUse 的 matcher 为 `.*`，命令为 CorpLink Resources 下的 `corplink-bridge`，timeout 为 5 秒，未设置 async，按同步方式执行。Codex 的命令 runner 启动外部进程、传入事件 JSON，并等待输出与进程结束；5 秒是命令执行超时配置，不是固定 sleep，也不是整个 Refine 的预算。采集中的通用 hook 事件缺少逐条 handler 关联，不能声称 126 段已逐一证明均属于这个命令。
4. **读取契约放大调用次数，写入已有批次能力。** `get_context_canvas` 已支持目录 list/分页，但单次正文读取只接受一个 reference；工具说明引导先 overview，再读取具体 section。样本中 64 个事件由此产生 127 次成功读取。`write_context_canvas` 的 timeline 写入已经支持 `operations[]`，不能记录为读写都只能逐项，也不需要据此重写整个 Canvas。

##### 尚未确认的原因与独立问题

- CorpLink 安装资源只有编译后的 `corplink-bridge`，没有可读 handler 实现。当前只能把等待定位到 Codex 的同步 hook 生命周期；其内部是否等待 IPC、其他系统服务、网络、队列或进程启动／退出，均未证实，不能凭名称断言是在做内容审核或模型调用。
- 没有证据证明该样本是“大量结果已经计算完，再堵在一个全局返回队列”。观测到的是前一次读取完成后等待 hook，再继续下一项；源码另确认同一 runtime 中非并行动态工具的执行门禁。
- 其他窗口已独立证实存在共享成员被相邻 Refine 修改、提交版本／fingerprint 冲突，然后在同一 lease 内重新核验的重复工作。该机制继续成立，但不能解释本样本在提交前耗尽预算；同样不能把这里的 `80.3%` 外推到全部 Refine 超时或整集墙钟时间。
- invocation 超时后临时 Canvas 未持久提交，既有队列重领时重新创建 Canvas 和原生 thread，无法直接续接此前读取进度。提交冲突后重新 invocation 也会重新获得单次 deadline，因此一个 Refine lease 可以超过十分钟。SDK completed 不等于成果已持久提交。

##### 待处理方向与验收

- 优先在现有读取契约上支持多个 references 或按 member 的有界正文批量读取；已知 section 时可直接读取正文，保留 issued references、revision、输出大小预算与分页，减少实际工具调用和 hook 触发次数。不能用无界全量输出代替分页，也不能把 `Promise.all` 当作同等修复。
- 补齐 task／interval／lease／invocation／tool call 与 hook handler 的关联、起止、回执、错误及超时状态、Canvas revision 和最后有效动作；区分业务读取、hook 生命周期与 handler 内部阶段，不记录模型私有推理、credentials 或媒体 base64。托管 handler 内部延迟需要其处理方的执行证据。
- 调度层另行评估领取 Refine 时避开正在执行的共享成员窗口，保留不重叠并行和提交快照校验；不能删除版本校验掩盖冲突，也不强制无需修改的 Refine 制造更新。
- 先以 fake provider + 真实 Canvas／queue／repository 离线验证批读结果和边界、调用次数下降、无修改正常完成、已提交成果不重复执行，以及不重叠窗口并行。批读后的真实耗时与质量尚未验证，不承诺仅修这一处就能整集完成；总 deadline、自动重试及 fallback 调整需独立评估。

依据：[诊断 HTML 与样本明细](../../outputs/reports/refine-timeout-20261011.html)、本机 `outputs/local-services/api.log`、原采集的 `/Users/bytedance/.codex/logs_2.sqlite` 中 `codex_app_server::outgoing_message` 记录，以及 ModelUsage／execution_timing 快照；[Canvas 读取工具](../../agent/utils/context_canvas_tools.py)、[timeline 批次写入](../../agent/video_parse_v2/timelineEvent_canvas.py)、[Codex adapter](../../gateway/codex/agent.py)、[Refine agent](../../agent/video_parse_v2/refine/agent.py)、[领取逻辑](../../server/mq/video_parse_queue.py)、[提交校验](../../server/repository/video_parse.py)。原生机制以对应 release 的官方源码为依据：[registry 的 tool → PostToolUse → 返回](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/registry.rs#L624)、[dynamic handler](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/handlers/dynamic.rs#L82)、[默认并行设置](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/tools/src/tool_executor.rs#L112)、[执行锁范围](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/parallel.rs#L181)、[code-mode 共享 runtime](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/code_mode/delegate.rs#L432)、[命令 hook runner](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/hooks/src/engine/command_runner.rs#L193)与 [OpenAI 官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)。

#### 2026-10-09：ParseV2 的 90 秒视频耗时 37 分钟

状态：功能及相对 ASR 的秒级时间链路已通过；人物名单校验已按用户明确规则缩小至 `DIALOGUE`。理解/精修输出预算与思考控制已完成源码、兼容性、离线和同片真实通路验证，约100.3秒完成；ASR 保持原设置。视觉事件输出变少，完整语义质量未验收。失败恢复、总时限和退避配置仍待处理。以下37分钟耗时与失败记录对应修复前冻结源码。

本次使用同一 90132ms 原片、原默认配置，在隔离 namespace `anivag_asr_reparse_20261009_081508_test` 重跑 task `d3046375-9d70-4a8d-972d-98fd6429f519`。服务运行开工时 `c8c5d2c + dirty` 的 539 文件冻结源码，模型、fps、重试和 fallback 没有临时调整；后续并行工作区变化未加载到该服务，以下根因严格对应本次运行。

**并行已经生效，但最慢 Chunk 的连续失败重试决定了总耗时。** 两段几乎同时开始，第二段约 9 分 57 秒完成，第一段因三次主模型失败及恢复花了 30 分 46 秒。唯一 Interval 需要两段均成功，随后才执行跨段精修；增加并发槽位不能缩短同一 Chunk 内依次发生的失败尝试。

| 关键路径 | 耗时 | 实际结果或依赖 |
| --- | ---: | --- |
| 第一段首次 Pro 理解 | 574.969 秒，约 9 分 35 秒 | Provider 返回 JSON，程序人物引用校验失败 |
| 第一段第二次 Pro 理解 | 653.048 秒，约 10 分 53 秒 | Provider 返回 `length` 不完整终态 |
| 第一段第三次 Pro 理解 | 397.302 秒，约 6 分 37 秒 | Provider 返回 `content_filter` 不完整终态 |
| 同轮 DeepSeek fallback | 7.276 秒 | 成功提交第一段 |
| 第一段 ASR、退避及准备 | 约 213.454 秒 | ASR 执行三次，另有两次退避 |
| 跨段精修模型 | 343.738 秒，约 5 分 44 秒 | 等待两段成功后执行并提交 |
| 其余领取、阶段交接、发布及轮询 | 约 23.211 秒 | 两段向量和 ES 发布完成 |
| **总墙钟时间** | **2212.998 秒，36 分 53 秒** | **08:16:08.318–08:53:01.315 UTC，最终 COMPLETE** |

第二段的理解与第一段首轮重叠，不能把全部模型请求耗时相加作为墙钟时间。完整关系是 `max(两段各自耗时) + 跨段精修 + 索引发布`，再计入准备与交接。第一段三次 Pro 失败合计约 27 分 5 秒；当时校验规则下均没有被接收的完整业务结果。

本轮 SQL/API 为 **14 次调用、443098 Token、missing0**，三次失败保留 **167520 Token，占本轮约 37.8%**；其中 failure output 为 85314、reasoning 为 75101。Token 按供应商实际用量保存，业务校验或供应商终态失败不会撤销已消耗的 Token。本节不估算金额，不混入前轮 99 条账本或下述 ParseV1 费用快照。

#### 三次长调用为何最后失败

1. **首轮是完整 JSON 的业务引用失败。** `persons` 已登记 `Kaname Raana`，但 `timeline[17]`、`[22]`、`[29]` 的 `visible_persons` 分别写成 `Kaname Raana (hand)`、`Kaname Raana (hand/guitar)`、`Kaname Raana (guitar)`。模型把身体/道具附注放进了人物引用字段，顶层 validator 在第一处未知引用立即抛错；供应商的 JSON Schema 结构约束没有保证跨字段引用一致。只在内存副本修复第17条再校验，仍暴露第22条，所以不能描述为“只有一个错误”。本次没有更改 raw 或历史结果，未证明其余内容都高质量。
2. **第二轮是供应商输出预算耗尽。** 实际 output32768，其中 reasoning29016、其余3752；供应商返回 `length`，没有可供业务接收的完整成功 JSON。Responses 的输出上限同时计入回答和思维链，思考占掉约88.5%的预算；不是程序拿到完整合法结果后随意拒绝。依据：[官方 Responses 参数说明](https://docs.volcengine.com/docs/ark/create-model-responses-api?lang=zh)。
3. **第三轮是供应商内容过滤终态。** 实际 output19829，其中 reasoning19581；Provider 返回 `content_filter`，程序按失败处理并调用已有 DeepSeek fallback。没有完整 raw JSON 用来判定具体触发内容，不能把它追认为本地人物校验失败或猜测过滤原因。

三次都在请求结束后才能确认最终结果。本次可定位到远程请求耗时和大量 reasoning Token，但没有供应商内部指标来分摊排队、推理与生成时间，不能把全部耗时都声称为本地校验或网络等待。

#### 本次确认的执行策略缺口

- **该冻结轮尚未接入 Ark 思考控制。** 五次 Pro 请求（理解与精修）output140100，其中 reasoning122954，约87.8%。当时 Ark 多模态适配器不接收或发送 `thinking` / `reasoning`，主路也未显式设置结果输出上限。下节源码已接入控制；实际关闭使用 `thinking.type=disabled`，不能仅在配置中填写字段就声称已控制推理预算，具体质量影响须同片验证。[官方说明](https://docs.volcengine.com/docs/ark/deep-thinking?lang=zh)
- **理解失败重跑整个 Chunk。** ASR 结果仅在单次 graph 内存中，重新领取失败 Chunk 后重跑 ASR、媒体准备与理解。本次两次额外 ASR 合计74.30秒，没有阶段成果复用。
- **fallback 只接住 `ContentFilterError`。** 人物引用校验失败和 `length` 不进入备用路径，而是先重试整个 Chunk；第三次 `content_filter` 才在同一次尝试中切换到 DeepSeek。程序缺少按结果错误类型选择定向修复、阶段重试或 fallback 的恢复策略。
- **600秒不是请求总墙钟上限。** 该值传给 SDK 网络 timeout，SSE 循环没有独立总 deadline；本次653.048秒请求没有在600秒结束，最终仍为 `length`。后续实现总时限时须沿用既有 cancellation/client close，并等待线程收尾，不能只取消 asyncio 等待而让底层请求继续消费 Token。
- **任务退避配置没有用于 Chunk consumer。** 快照为初始1秒、最大30秒；实际 consumer 使用 MQ 初始30秒、最大900秒，因此两次退避为30/60秒，加调度准备后间隙35.69/66.34秒。Prefix 的任务级退避与 Chunk consumer 的队列级退避不能混称为同一有效策略。

#### 人物引用校验修复与离线验证（2026-10-09）

用户明确：只有 `event_type=DIALOGUE` 时，人物引用需要匹配当次 `persons`；其余六种事件不受人物名单限制。仅看到吉他或手部不能据此判断人物归属。实现已在所有事件共用的时间排序检查后，仅对 `DialogueEvent` 执行名单校验；非对白的 `participants` / `visible_persons` 原文保留，不删括号、不补人物、不推断物体归属。对白的 `speaker=null`、非空 speaker 属于 participants、人物名单引用，以及所有事件的结构、时间和顺序检查继续生效，静态 JSON Schema 未变。

回归验证：新增六种非对白事件 × 两个人物引用字段的12项原文保留/JSON往返用例，另有1项混合事件排序用例；13项在旧实现下均失败。修改后 `.venv/bin/pytest -q tests/engine/parsev2 tests/agent/video_parse_v2/test_framework.py` 为82项通过，`.venv/bin/pytest -q tests/engine/test_index_projection.py` 为17项通过，合计99项通过；独立只读审阅通过。

使用首轮调用 `84a7dc7a-7031-48f0-b241-d1966e833328` 的未修改 raw JSON 离线重放：38条事件通过完整 `VideoParseResult` 校验及 `[0,60000)` 时间范围检查；第17、22、29条均为 `ACTION`，原附注及全部正文/时间完整保留。原始证据文件内容不变，没有新模型调用或数据库写入。验证记录为 `/private/tmp/anivag-dialogue-persons-20261009/raw-replay-verification.json`；本项结果说明旧名单阻断已解除，不代表重新完成真实全流程或确认模型描述中的人物归属。

#### 理解/精修默认预算与思考控制修复（2026-10-09）

用户明确批准仅执行理解和精修接线，ASR 保持原设置。`models.vision` 新任务默认为 `max_output_tokens=65536`、`reasoning_effort=none`，经 Gateway 配置、Server profile 和固定 V2 任务快照进入实际 Ark 请求；未单独配置的 refine 继续复用 understanding binding。Vision 配置校验允许更大的显式预算，默认值与最大可配置值分开。没有调整 ASR、Chat、备用模型、并发、重试或数据库 Schema。

Ark 结构化与流式路径均处理控制值：`none` 显式发送 `thinking={"type":"disabled"}`；`None` 完全省略控制参数，保留旧任务原行为；其它支持的值发送 `reasoning.effort`。旧固定任务不会回读新默认值。新增 Server Vision 可选字段未设置时，仅省略其 snake/camel 序列化键，保护旧显式上传请求的幂等指纹，其它 nullable 字段保持；V2 任务的原有 reasoning 字段仍正常持久化。OpenAI Vision 对不支持的非 `none` 值在 YAML 边界拒绝，备用模型保留独立配置。

离线验证覆盖配置传播、旧快照恢复、实际 SDK 请求参数、refine 继承与上传幂等兼容：`.venv/bin/pytest -q tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/gateway/test_models.py tests/gateway/test_usage.py tests/server/test_config.py tests/server/test_video_parse_worker.py tests/server/test_upload_service.py tests/engine/parsev1/test_model_sources.py tests/engine/parsev2 tests/agent/video_parse_v2/test_framework.py --tb=short`，**573 passed**；仅有既有 Starlette/AnyIO 弃用警告。修复后的独立只读源码复核通过。

第一次实测 namespace `anivag_parse_budget_20261009_121056_test`、task `75501ba3-6d8d-410c-a653-bc2dc134c4b6` 在 ASR 供应商连接阶段即失败；恢复时数据库只保留4条 failed candidate_transcript，tokens 均缺失，父任务仍为 PARSING。进程及临时目录已消失，不能把该轮作为性能或端到端通过证据，也不能将缺失用量当零。已恢复原 MySQL/ES 数据目录；按原第6集680秒起点与相同 ffmpeg 参数重建素材，大小9999051、SHA256 `ddbecf396cb32193e99c5168428adf97b6cdd6627103d534a69dc0da36f2e4b5`、精确时长90.131708秒与原记录完全一致，取整为90132ms。恢复后的新一轮证据保存到持久目录 `outputs/verification/vision-controls-20261009-resumed/`。

恢复后 task `f8e59ff7-1c05-4ec4-b788-c742353aa6eb`、namespace `anivag_parse_budget_resume_20261009_132118_test` 使用 `29b8051 + dirty` 的542文件冻结源码、原 Series 元数据和字节相同的素材。新旧 profile 与固定任务快照只差 Vision 的两个控制值；没有请求级配置覆盖。父任务、两 Chunk 和唯一 Interval 全部 COMPLETE，Chunk/Interval retry均0。提交到轮询观测终态为 **100.285秒**（13:22:03–13:23:44 UTC），其中 coordinator 观测时长95.470秒；轮询时间与处理时间分别保留，数据库 `processing_duration_ms=0` 不用作有效耗时。

| 同片观测 | 修复前冻结轮 | 本轮 |
| --- | ---: | ---: |
| 总墙钟时间（含终态轮询） | 2212.998秒 | 100.285秒 |
| 模型调用 | 14 | 10 |
| Total Token | 443098 | 193985 |
| 失败调用 / Token | 3 / 167520 | 1 / 27885 |
| 主路理解/精修 reasoning Token | 122954（5次） | 0（3次均明确报告0） |

本轮墙钟减少约95.5%、Token减少约56.2%，是一次实测对照，不保证未来请求时长，也不是只切换一个参数的因果实验：源码还包含已完成的人物校验及宿主改动，模型输出和精修工作量不同。两段仍并行：ASR远程请求分别37.468/18.540秒；第一段 Ark理解17.965秒后返回 `content_filter`，同次尝试的既有 DeepSeek fallback 成功，理解合计25.261秒；第二段 Ark理解32.736秒成功。精修远程请求3.048秒，索引发布16.392秒。本轮没有 `length`、人物校验失败或整个Chunk重跑。

实际 SDK 白名单证明三次 Ark理解/精修均发送65536和 `thinking.type=disabled`，ASR两次仍省略这两项控制。10条SQL用量均来自供应商，input187142、output6843、total193985，API逐项一致、missing0；内容过滤失败的27885 Token同样保留。ASR reasoning分别418/311，四次embedding未报告reasoning，因此全局reasoning聚合仍为null，不将未知当0。10个used call ID与账本集合相同，active调用与全部执行租约均0；model_usage八列保持原设计。两内容version1/generation1、Interval input/indexed_versions、ES两文档及四个1024维有限float32向量全部对应。两媒体DTO的0–60000/60000–90132ms范围、字幕/事件边界和播放READY通过；真实Range请求HTTP206，bytes0–1023/9999051。

ASR原始17/11条秒制估计全部精确等于独立计算的 `round(seconds*1000)`、片段裁剪及一次offset后的实际返回；第二段首条4.5–5.1秒正确存为64500–65100ms。最终28句对白均与ASR原文标准化对应，27句起止一致，1句开始提前400ms、结束一致；第二段11句横跨25632ms，未重现26ms压缩。这里只验证程序换算及最终结果与观察到的ASR一致，不认证音轨毫秒真值。

**本轮不作高质量解析验收。** 精修raw为 `{}`、output仅2 Token，意味着没有内容修改提案，不能与旧343.738秒的精修工作量等同。旧SQL当前内容为第一段14对白+1场景、第二段13对白+14动作+1场景；本轮为第一段17对白、第二段11对白+1动作。第二段非对白事件由15条变为1条，视觉信息输出明显更少；事件数量本身不能判定真值或归因于单独关闭reasoning，但必须作为质量对照保留。字幕、统计、存储、检索发布与播放通路通过，不等于人物/动作/场景描述全部准确完整。

前端兼容已按追加批准的三文件修改树补齐：Vision最大输出输入框与JS校验均为262144，接受新65536默认值；ASR及备用模型仍限制32768。新增7项预算边界回归，`npm test -- src/features/uploads/upload-types.test.ts src/features/uploads/UploadProvider.test.tsx`，23项通过；`npm run typecheck`通过。前端不新增reasoning编辑控件，取得的有效配置通过原有对象复制和请求路径保留该附加字段。

独立终态复核31/31项通过，结论仅为参数、统计、秒级时间、存储索引及播放通路通过，保留上述质量资格。已对本轮隔离API PID14346核实身份后发SIGTERM，9622关闭；恢复的3306/9200依赖与新旧测试数据保留。未执行commit、push或业务库迁移；新增测试库按现有Schema初始化，不改变model_usage八列设计。

收尾时当前ROOT出现并行ORM类及仓储引用改名，相关源码和共享测试中的改动完整保留。本轮配置及四个模型控制生产文件与实测冻结版字节相同；最新工作区在这些并行改动后再次运行相同离线命令，573项通过（11.78秒）。真实模型、SQL/API和播放验收仍以13:21冻结的542文件为证，不把后来的整个ROOT版本追认为同一次实测。

本轮本地证据：[配置核对](../../outputs/verification/vision-controls-20261009-resumed/default-verification.json)、[实际请求与最终账本审计](../../outputs/verification/vision-controls-20261009-resumed/audit-result.json)、[ASR时间审计](../../outputs/verification/vision-controls-20261009-resumed/asr-timestamp-audit.json)、[同片耗时及质量资格](../../outputs/verification/vision-controls-20261009-resumed/performance-comparison.json)、[媒体验证](../../outputs/verification/vision-controls-20261009-resumed/media-verification.json)。源代码、输入素材与原始payload保存在同一持久输出目录；旧临时目录证据已消失，本轮旧耗时沿用已记录的历史文档，旧SQL快照只读恢复，未补造历史raw或更改旧账本。

#### 同片轻推理对照与精修覆盖回归（2026-10-09）

用户要求理解和精修改用轻推理对照。新轮只复制上一轮 `none` 的542文件冻结源码，将测试副本 `models.vision.reasoning_effort` 改为 `low`；其余541文件字节相同。视频仍为同一SHA256、9999051字节、90132ms，Series prompt元数据、65536输出预算、ASR、备用模型、并发和重试配置均保持；普通上传无请求级覆盖，ROOT默认仍为 `none`。两轮实际ASR分句与文字存在差异，因此只证明配置单项变化，不能宣称逐字prompt相同的严格A/B或仅由reasoning造成全部差异。

本轮 namespace `anivag_parse_budget_resume_20261009_135022_test`，task `5940fbfb-439f-4a66-b066-68d8bc97c91d`；13:56:47–14:07:48 UTC提交至终态轮询 **661.334秒**。之前的沙箱本地连接拒绝发生在提交前，没有创建第二个任务或消耗模型Token，不计入解析墙钟。父任务、两Chunk和唯一Interval均COMPLETE，Chunk/Interval retry均0；这只表示执行通路结束，**本轮语义质量验收失败**。

| 同片对照 | 关闭推理 none | 轻推理 low |
| --- | ---: | ---: |
| 总墙钟时间（含终态轮询） | 100.285秒 | 661.334秒 |
| 模型调用 | 10 | 10 |
| 总Token | 193985 | 232369 |
| 主路理解/精修reasoning Token | 0 | 37332 |
| 失败调用 / Token | 1 / 27885 | 1 / 35474 |
| 精修生成阶段耗时 | 3.073秒 | 454.095秒 |
| 理解初稿事件总数 | 29 | 36 |
| 最终保存事件总数 | 29 | **2** |

low耗时约为none的6.59倍，Token增加约19.8%。两段理解仍并行：第一段主路129.374秒后内容过滤，既有DeepSeek备用模型接续，理解合计136.672秒；第二段主路161.829秒成功，理解合计161.855秒。精修远程454.069秒、完整生成阶段454.095秒，索引17.710秒；没有length或整个Chunk重跑。三次真实Ark请求均发送 `reasoning.effort=low`、65536，供应商reasoning分别6731、6638、23963；最后一项来自精修，占精修24253输出Token的大部分。ASR设置不变，reasoning仍真实报告477/311，embedding未知字段继续为null；reasoning属于output的组成部分，不重复累加到total。

**精修将完整时间线覆盖成局部修改，必须先修复该契约问题。** low初稿第一段17事件（10对白、7场景），第二段19事件（11对白、4动作、2情绪、1场景、1画面文字）。精修只返回两段各一条修改参与者的对白，未携带其余事件。现有提示要求仅返回变化的成员/字段，但未明确强调一旦提供timeline必须给完整替换数组；Engine与Repository按整字段替换执行，结构、人物和时间校验均放行。两段最终version2各1条对白，36条事件降为2条，净减少34条事件、19条对白；SQL、API与ES都保存了这个缩减结果。精修有更新、版本一致或任务COMPLETE不能作为内容可用的依据。none轮精修为空，保留29条，未触发这个覆盖路径；问题属于已有精修协议/校验缺口，不能归因于低推理参数本身，也与秒转毫秒无关。

离线使用未修改的冻结Engine重放本轮raw精修提案，现有校验接受两份单条timeline，`apply_chunk_update`得到1+1条，精确等于最终SQL；没有新增模型调用或数据库写入。应先明确并落实精修timeline的全量替换或事件级修改语义，防止局部纠正丢弃未修改事件；不以人物登记表或简单数量阈值代替更新契约。

六张代表帧10/35/55/65/75/85秒已独立核对：low初稿增加部分场景和可见人物覆盖，但35秒画面没有其声称可见的白发少女，且「やらないの？」比本轮ASR提前4.2秒。精修没有证明修正该问题，该事件随其余内容被替换掉。none的对白description也含视觉观察，不能把独立ACTION/SCENE数量直接等同视觉理解准确率；音箱开/关等动作单凭代表帧不能裁决。最终ASR秒到毫秒程序转换仍正确，精修丢失的对白属于内容覆盖失败，不能靠换算通过代替验收。

用量与工程证据复核34/34通过：10个唯一call_id与预算used集合一致、missing0，input186595/output45774/total232369与API逐项相等，失败用量保留；全部执行租约释放，两文档最终version2及索引回执对应，四个1024维有限float32向量一致，Range播放206。顶层验收明确为 `failed_semantic_quality`，不将这些工程检查宣传为解析质量通过。仅启动本轮隔离API和初始化现有Schema的独立测试资源；API PID27755已核对身份后停止，MySQL/ES与新旧测试数据保留。ROOT生产源码/默认配置、八列model_usage和并行工作区改动保持；本轮只追加本问题文档与忽略目录内测试证据，未commit或push。

本轮证据：[准备单变量核对](../../outputs/verification/vision-controls-low-20261009/independent-preparation-review.json)、[统计与终态审计](../../outputs/verification/vision-controls-low-20261009/final-reviewed.json)、[独立内容对照](../../outputs/verification/vision-controls-low-20261009/none-low-independent-quality-comparison.json)、[精修覆盖离线重放](../../outputs/verification/vision-controls-low-20261009/refine-replacement-replay.json)、[ASR时间换算](../../outputs/verification/vision-controls-low-20261009/asr-timestamp-audit.json)。原始素材、manifest、SDK请求、raw响应、版本与索引快照保存在同一持久目录，none轮证据未改。

#### 处理方向与验收条件

- 理解/精修的思考策略与输出预算接线及同片真实请求核对已通过，关闭思考与65,536预算已生效；视觉事件减少与精修空提案必须纳入质量对照，尚未完成高质量语义验收。
- 人物名单约束仅适用于 `DIALOGUE`，源码修复及离线验证已完成；后续真实联调核对非对白观察可直接保留，对白归属不确定时允许 `speaker=null`，所有事件仍须满足结构、时间范围和任务/lease 边界。观察到吉他、手部或附注不触发人物名单阻断，也不自动清洗名字或推断人物归属。
- 按 schema 引用失败、输出预算耗尽、内容过滤、网络故障分别确定阶段恢复和 fallback；复用有效 ASR 成果所需的身份、配置、provenance 与租约边界必须保持，不能为了省 Token 把不完整/过滤结果当成成功。
- 使总请求耗时和 Chunk 退避遵循真正有效的配置；以能持续输出超过总预算的流式故障验证取消、用量保留、线程收尾及不再启动后续调用，不新增逐阶段高频计时表。
- 用同一90132ms原片报告墙钟、调用次数、reasoning/总Token、失败用量及并发重叠；定向修复应保留已完成工作，重试不再无区别重复 ASR。字幕/动作时间、人物引用、内容覆盖和播放范围需要共同验收，结构通过不能代替原片语义质量评测。

用户提出“约15分钟完成高质量解析”的预期。**在修复前冻结轮去掉失败重试、沿用该轮首次理解和精修耗时，曾粗估约16分钟**（理解约10分钟、精修约6分钟、准备和索引），该历史估算不是本轮100.3秒的实测结果。最新默认配置未保证固定时限，也没有由完成时长证明高质量；视觉信息变少和空精修已留证。既有验收只确认新Parse通路与相对ASR的秒级时间一致性，未认证毫秒音轨真值或全部人物/动作/对白语义；ASR输出秒、程序保存毫秒的修复继续有效。

依据：[模型适配器](../../gateway/ark/multimodal.py)、[SSE 终态处理](../../gateway/ark/sse.py)、[引用校验](../../engine/parsev2/schemas.py)、[Chunk Workflow](../../agent/video_parse_v2/understanding/workflow.py)、[宿主与 fallback](../../server/service/video_parse.py)、[Consumer 退避](../../server/mq/video_parse_consumer.py)、[最新运行证据索引](../../server/report_docs/model-token-usage-stages-evidence.md)。原始证据 `/private/tmp/anivag-asr-reparse-20261009/` 的 `source/`、`environment.json`、`stage-observations.jsonl`、`understanding-schema-diagnosis.json`、`state-latest.json`、`audit-parse-timing-final.json` 与 `acceptance-final.json`；装配/持久层有后续ROOT变化，根因以该冻结源码及实际调用记录为准。

#### 2026-09-23：ParseV1 历史实测数据与统计口径

以下实测、结构性原因、处理方向和验收条件保留该日期的 ParseV1 语境，不把历史“Understanding 按 Context 串行”或价格统计套到上面的 ParseV2 运行。

以下数据取自 2026-09-23 的本地开发库 `series_parse_job`、`parse_usage_attempt`、`parse_model_call`，共 7 个 Job、17 个 Attempt 和 675 次已记录模型调用；EP07 仍为 QUEUED 且没有消耗，未列入下表。表中“Worker 耗时”是 Consumer 使用单调时钟累计的活跃处理时间，不是视频时长；“已知预估费用”按每次调用保存的本地人民币价格快照计算。它只是当前配置单价估算中已经能够计价的部分，不等同于 Provider 最终账单，也不包含赠金、资源包或折扣；未计价调用和不完整 Attempt 会让配置单价估算缺少一部分，不能据此反推实际账单下界。

| 样本 | 结果 | 源片长 | Worker 耗时 | 相对片长 | Attempt | 已记录调用 | Total tokens | 已知预估费用 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EP01／`4770ed42` | 成功 | 23.84 min | 81.54 min | 3.42× | 6 | 117 | 3,585,909 | ¥15.40 |
| EP02／`d57d8145` | 成功 | 23.67 min | 74.52 min | 3.15× | 1 | 96 | 3,051,231 | ¥14.76 |
| EP03／`e8ab3ef3` | 成功 | 23.67 min | 77.74 min | 3.28× | 2 | 119 | 3,465,478 | ¥15.47 |
| EP04／`0a45ce3d` | 成功 | 23.67 min | 至少 99.55 min | 至少 4.21× | 3 | 139 | 4,219,958 | 已知 ¥14.39；48 次调用未计价 |
| EP05／`2d24ee44` | 成功 | 23.67 min | 97.20 min | 4.11× | 1 | 104 | 3,054,408 | 已知 ¥12.89；48 次调用未计价 |
| EP06／`757b6064` | 失败，进度 17% | 23.67 min | 至少 29.48 min | 至少 1.25× | 4 | 100 | 2,921,862 | 已记录 ¥4.62，账本覆盖不完整 |

- 4 个计时完整的成功样本均约 23.7 分钟，但活跃处理耗时为 74.52–97.20 分钟，即片长的 3.15–4.11 倍。EP04 的计时不完整，但已累计至少 99.55 分钟和 4.21 倍片长。
- 5 个成功样本合计 118.508 分钟源视频，已知 Worker 耗时至少 430.542 分钟，聚合后至少为实时播放时长的 3.633 倍；已记录 575 次模型调用和 17,376,984 tokens。
- EP01–EP03 的调用与价格覆盖完整，单集预估费用为 ¥14.76–¥15.47，均值 ¥15.21，约为每源视频分钟 ¥0.64。5 个成功样本的已计价调用合计 ¥72.91，但 EP04、EP05 各有 48 次 Embedding 调用未计价，因此不能把 ¥72.91 当作完整总费用。
- EP06 最终只到 17% 且没有产出可用结果，却已记录 100 次调用、约 292 万 tokens 和 ¥4.62 已知费用。其已记录调用数已超过一次无重试成功的 EP02，说明页面进度不能作为资源消耗的近似值。
- 合并成功样本与 EP06 后，当前已执行 Job 共记录 675 次调用、20,298,846 tokens 和 ¥77.5359478 已计价调用预估费用；完整的配置单价估算还缺少 96 次 Embedding 调用，且该小计仍不是实际账单。
- 2026-09-23 的本地 Artifact 快照还显示：`outputs/server/artifacts/development/jobs` 中 6 个已运行 Job 的工作目录约 4.5 GiB；`outputs/server/artifacts/development/media` 中排除原视频后的派生 Chunk、Analysis Window 和关键帧约 4.4 GiB。发布实现使用文件复制，工作目录与稳定媒体目录中的派生文件是两份独立副本。这属于本机转码、I/O 和磁盘放大，不能与上表的模型费用混算。

EP01 的各 Attempt 跨越了隔夜和人工恢复间隔，所以 `completed_at - started_at` 约 23.8 小时，不能描述成连续计算耗时。排除该样本后，另外 4 个成功 Job 的端到端墙钟时间为 74.5–107.6 分钟，仍明显超过源片长。

#### 已确认的结构性原因

1. **单集天然产生大量远程调用。** 默认按 60 秒切片并在相邻切片两侧各保留 3 秒重叠。约 23.7 分钟视频形成 24 个 Chunk；没有 fallback 和重试时，每个 Chunk 仍至少执行 1 次 Lite 候选转写、1 次 Pro 视频理解、1 次文本向量和 1 次视频向量，即理论最低 96 次模型调用。
2. **主要阶段串行。** ASR 只有 4 路有界并发，Pipeline 等全部待处理 ASR 完成后才进入 Understanding；Understanding 为演进 Context 按 Chunk 串行。文本向量全部完成后才串行执行视频向量。同一服务实例只装配一个 Parse Consumer，Job 之间也逐个处理。按 EP02 的 Job 日志和调用顺序近似拆分，ASR 约 29.9 分钟、主视觉理解约 39.3 分钟、Embedding 约 2.9 分钟、本地处理与阶段交接约 2.5 分钟；这只是日志反推值，当前没有持久化的逐阶段正式指标。
3. **Pro 视频理解是当前首要金额来源。** 在无重试且全部调用可计价的 EP02 中，24 次 `video_timeline` 占 45.30% tokens，却贡献 ¥12.44，即该集预估费用的 84.27%；24 次视频向量调用贡献 11.32%，Lite 候选转写贡献 4.22%，文本向量贡献 0.19%。该比例只代表此实测样本，但足以定位本轮优化的第一对象。
4. **媒体准备和上传重复。** 每个 Chunk 依次生成 Analysis Window、Canonical Chunk 和关键帧；ASR、Pro 理解和视频向量还会分别读取并内联编码视频。一次 24-Chunk Attempt 至少涉及 48 次片段转码和 24 次独立关键帧提取，当前没有按源内容与解析配置复用整套准备结果。
5. **重试会重做已经成功但未持久化的工作。** ASR 没有独立 durable checkpoint。Understanding 在中途失败后，下一 Attempt 只恢复已提交的连续 PARSED 前缀，其余已经在内存中完成的 ASR 会重新调用；媒体准备也会写入新的 `lease-*` 目录并重新生成全部窗口。实测候选转写调用从干净样本的 24 次放大到 EP01 的 41 次、EP04 的 63 次和失败 EP06 的 91 次。
6. **fallback 继续放大长尾。** 一个典型 66 秒窗口触发内容审核 fallback 后，会在已发生的主 Pro 请求之外，再增加 3 次 `neutral_visual_evidence` 和 1 次 `fallback_video_timeline`；本地日志已出现这一 1+4 调用路径。主请求单次超时上限为 600 秒，fallback 单次为 1200 秒，当前没有整个 Job 的总耗时、总调用或总费用预算。

#### 处理方向

1. 先补低频结构化指标或 Metrics，分别统计 media prepare、ASR、understand、text embed、video embed 和 index 的耗时、调用数与费用；不要恢复 B04 已删除的逐 Chunk／逐 Stage 高频写库表。
2. 优先完成重试止损：为 ASR 增加可恢复的 Chunk 级持久化结果，以内容哈希、解析配置和模型 provenance 作为复用条件；恢复时复用已发布媒体，避免重新转码、上传和计费。为 Job 设置明确的总耗时、总调用和总费用预算。
3. 以同一批视频做质量对照，评估减少 Pro 重复输入和调用数量的方案，包括降低视觉采样、合并相邻窗口、先筛选再调用 Pro；任何降本方案都必须同时检查时间线和 Evidence 质量，不能只看费用。
4. 在不破坏 Context 顺序的前提下，将独立的 Embedding 和 fallback 分组改为有界并发或流水处理；Job 级并发需要同时验证 Provider 限流、内存占用和 Qdrant 写入边界。
5. 对最终成功或失败的 Job 做引用审计后，清理无 Checkpoint 引用的旧 Lease 和重复派生媒体；价格快照缺失时必须明确显示“未计价／下界”，不能显示成完整总费用。

#### 验收条件

- 用固定视频集同时报告源片长、端到端墙钟时间、活跃 Worker 时间、Attempt、模型调用、tokens、已知费用、未计价调用和用量覆盖状态，并形成可重复的优化前基线。
- 普通成功、Understanding 中途失败重试和 Worker 中断恢复都不再重复调用已经持久化的 ASR，也不重新生成内容与配置一致的媒体文件。
- 优化后的同批样本在 Worker 时间、墙钟时间和每源视频分钟费用上均有可重复的下降，同时 Timeline、Transcript 和检索 Evidence 的质量回归通过。
- 所有调用都有价格快照，或在 API／UI 中明确标记未知费用；失败 Job 达到预算后可解释地终止，不再让重试无限线性放大资源消耗。

依据：[pipeline.py](../../engine/parsev1/pipeline.py)、[transcribe.py](../../engine/parsev1/transcribe.py)、[understand.py](../../engine/parsev1/understand.py) 与 [embed.py](../../engine/parsev1/embed.py) 的阶段顺序和并发边界；[video_chunk_parse.py](../../engine/parsev1/video_chunk_parse.py) 的主理解与 fallback；[analysis_windows.py](../../engine/parsev1/analysis_windows.py)、[video_processing.py](../../server/service/parsev1/video_processing.py) 与 [artifact.py](../../server/storage/artifact.py) 的媒体准备、Lease 目录和复制发布；[consumer.py](../../server/mq/consumer.py) 的 Job 消费；[parse_usage.py](../../server/service/parse_usage.py) 与 [parse-model-usage.md](../parse-model-usage.md) 的价格快照和费用口径；[config.yaml](../../config.yaml) 的当前模型与 fallback 配置；本地开发库和 `outputs/server/logs/development/api.log` 的 2026-09-20 至 2026-09-23 运行记录。

### B08：索引版本策略、Embedding 存储边界与 JSON 数值精度

优先级：高。类型：索引更新策略、向量持久化职责与精度研究。状态：2026-10-12 已实现按身份／版本确认索引完成；独立 embedding 字段尚未实施。用户根据精度影响研究选择本次不推进精度修复，仅调整索引策略；此前工作区的数值编码改动保留原状，不计入本次实现或部署结论。

#### 2026-10-12 确认的三项问题

1. **索引按身份与版本决定更新。** 可信身份为 tenant／series／video／parse task／chunk，版本为 content_version 与 index_generation。相同版本、文档完整且 query-visible 时直接确认完成，跳过重复构建和 embedding；版本较低或文档缺失才发布，较高版本阻止旧任务覆盖。同版本尚未可见时确认可见性，不以向量数值差异触发重写。原 `is_ready`、`verify_ready` 与异常读回的完整 source 相等判断违背此策略，数值尾差只是暴露问题的一个触发条件；维度及数值合法性校验和数据审计与更新决策分开。SQL receipt 的身份／版本仍需正确。
2. **独立 `embedding JSON` 字段承接缓存。** 将 `detail._index_embeddings_v2` 的完整 envelope（content_version、receipt、representations）移至 `ChunkParseContent.embedding`；detail 保留 evidence、scope、compact。字段拆分解决职责和读写粒度，不能宣称仅新增原生 JSON 列就避免浮点往返误差。具体数值编码、历史数据兼容及迁移执行尚待落实，本次记录不授权迁移或重启。
3. **研究精度机制、影响及差异来源。** 已确认本机 MySQL JSON 转换对部分数值产生一个 binary64 ULP 的往返差异；需要区分十进制解析、二进制存储及输出格式化，量化 cosine／排序的影响，并核对 ES 原始 `_source` 与内部向量表示。既有对比是 SQL 缓存读回值与 ES `_source`，没有原始模型响应、缓存及 ES 三端的同一次调用关联，不能表述为“模型内存原值被 ES 改坏”或“模型不稳定”。数百维差异的来源保持未定位，源码允许并发覆盖只是候选路径。

当前判断修正：优先解决版本幂等语义。身份／版本／完整性／可见性正确的 ES 文档可直接认可，纠正旧缓存数值或推进 generation 不是所有任务恢复的前置条件。精度保存和来源追踪分别作为持久化与诊断问题研究。

本次实现：ES 前置 readiness、写入后的验证和异常实时读回均以身份／版本及必需字段完整性判定，取消正文与向量逐值相等。Coordinator 在构建媒体／embedding 前确认已有版本；同版本可检索时跳过重建，必要时轻量补 receipt，并在完成前复核；同版本未可见时按现有规则重试，更高版本拒绝旧目标覆盖。外部版本防护和租约／SQL 当前内容版本校验保留，未迁移数据库、恢复真实任务或重启服务。

本次验收：存储／Coordinator 单元回归 99 项通过；Worker 索引相关回归 7 项通过；真实 ES 8 项集成回归通过（含同版本尾差、迟到版本、部分发布恢复），测试仅使用随机隔离测试索引并清理，没有外部模型调用。真实 MYGO 任务尚未用新代码重跑，不能以测试通过宣称这两集已经恢复。

#### 本轮研究结论与边界

- **解析机制已用同版本源码实验隔离。** MySQL 8.4.11 官方 bundled RapidJSON 默认解析对故障小数产生一个 binary64 ULP 的差异，full-precision flag 对这些输入恢复参考值；binary JSON 仍以八字节 double 保存。输入解析即可解释尾差，不需要假设向量降成 float32 或读出时固定截断。当前 MySQL 离线，实验针对官方解析器，不冒充本轮服务器逐函数调试。
- **本例数值影响很小，严格比较影响很大。** 已复现坐标的绝对差约 `1.36e-20`，相对差约 `1.30e-16`，转换到 float32 后位相同。五组现有 1024 维向量的有限扰动实验中，cosine 最大变化约 `1.11e-16`；没有据此担保全部检索或所有近乎同分排序不变。尾差会改变逐值相等／哈希结果，不能成为同版本发布的更新条件。
- **当前 ES 原始 source 保存路径已只读核对。** ES 为 8.19.22，目标索引 `index.mode=standard`、`index.mapping.source.mode=STORED`，default／final pipeline 均为 `_none`，mapping 无 source 过滤，两路向量为 1024 维 hnsw。原始 `_source` 与内部 float32 向量索引是独立路径，不能拿内部表示推断原始 source 被改。当前配置快照不补齐历史调用来源，也不证明所有历史时刻配置相同。
- **归因边界保持不变。** 当前已比较的是 SQL 缓存与 ES source；没有证明同一次模型响应的内存值被 ES 改成另一组值。客户端 dumps／loads 对四个故障数值保留原位模式；数百维大差仍需追踪原始响应、实际发布 payload 与读回结果的调用身份。

完整机制、官方源码链接、独立解析复现和数值影响见 [MySQL JSON 向量精度研究（2026-10-12）](../research/2026-10-12-mysql-json-vector-precision.md)。该研究轮次仅记录与研究；随后版本策略已按用户指示实施，字段迁移及真实任务恢复仍未执行。

#### 已采集现象与失败机制

- 排查中的 MYGO EP01 Parse Task 为 `d470e300-9813-4c19-a62e-f52503e2e1ae`。理解 24/24、Refine 22/22 已完成，17:31:06 的索引日志记录 `ElasticsearchBulkWriteError`，17:31:23 主任务进入 INDEXING_FAILED。当时快照为 24 个 Chunk COMPLETE、22 个 Interval 中 4 COMPLETE／17 INDEXING／1 INDEXING_FAILED，ES 有 20/24 个文档。这些是已采集快照，不表示当前实时状态。
- 对已存在 ES 文档的完整 source 比对，身份、内容版本、文本及其他字段一致；部分向量坐标有 binary64 末位差。例如缓存为 `-0.00010395050048828124`，ES 为 `-0.00010395050048828125`，绝对差 `1.3552527156068805e-20`，恰好 1 ULP。本机 MySQL 8.4.11 的只读 JSON 转换已复现数值变化，Python JSON 往返则保持原值。另有 4 个文档的向量数百维不同、最大差异约 `0.0068`；来源尚未关联到原始模型响应，不能据此归因于模型或并发。
- 发布代码直接使用构建出的内存 artifact，后续重放优先读取 SQL 缓存。MySQL JSON 数值往返的已复现尾差可以解释部分缓存／ES 差异，但这条代码路径不证明历史 ES `_source` 改变了模型原始响应。此前把缓存对比结果表述为模型内存与 ES 的差异过于确定，应以上述三端关联证据边界为准。

```text
ES 已有 ID／版本 V、向量 A
    → 同版本缓存恢复出 A′，哪怕只差 1 ULP
    → is_ready 比较完整 source，返回 false
    → 以 external 版本 V 重写已有文档
    → 同版本写入冲突；inspect_exact 仍判定 false
    → 原 bulk 异常重新抛出，缓存重放再次走同一路径
```

已用真实 coordinator／storage 逻辑与内存客户端构造复现：首轮发布成功，仅修改同 receipt／版本缓存中的一个坐标为相邻 binary64 值，随后连续三轮均为 `is_ready=false → bulk item 409 → inspect_exact=false → 原异常抛出`，没有重新编码。**构造客户端的 409 验证了确定性机制，不是历史 bulk item 错误的原始证据。** 原日志未保留 item status／error，历史 409 仍属根据代码与快照作出的推断。

#### 已实施的精度修复与验收范围

- 通用 JSON 编解码放在 [server/utils/json.py](../../server/utils/json.py)，[StringEncodedJSON](../../server/model/types.py) 只负责 SQLAlchemy 绑定与结果转换。物理列仍为 JSON；写入时将目标数值 payload 编码为 JSON 字符串，读取时由 Python 解码，避免数据库再次解析其中的浮点数字。业务内存、DTO、Engine 及 ES／Qdrant 请求继续使用数字数组／权重映射。
- Parse V2 只编码 `ChunkParseContent.detail._index_embeddings_v2.representations`，覆盖 dense text／video 向量和 sparse weights；外层 content_version、receipt、index_generation 及其他 detail 字段保持原生结构。编码前复制数据，不修改仍要发布到 ES 的内存结果。
- Legacy `VideoChunkEmbedding.text_vector`／`vision_vector` 同样使用字符串编码。ORM 与列查询读取时自动恢复数值，IndexSync 的发布快照和 source_revision 计算不需要业务层适配；旧数字数组／对象仍可读取，重新写入后使用新格式。
- 本批 **310 项离线测试通过**，覆盖字符串物理存储、故障坐标 `float.hex()` 精确保留、V2 dense／sparse 恢复、旧格式兼容，以及 legacy 重复导入的 source_revision／generation 稳定。尚未进行实时 MySQL／ES 联调，没有重启服务、修改历史缓存或恢复该任务。已发生精度变化的旧数字不能通过再次字符串化还原为原始值。

#### 独立待处理项与验收条件

| 问题 | 已确认边界与影响 | 待处理与验收 |
| --- | --- | --- |
| 同版本缓存并发覆盖候选路径 | 相邻 Interval 可为共享 Chunk 编码；`save_index_embeddings()` 对相同 receipt／版本的 representations 使用 `update`，后保存结果可覆盖前结果，各自仍使用本地 artifact 发布。源码存在这条路径，但尚未证明它造成了历史数百维差异，也未证明模型同输入重复请求会产生不同结果。 | 关联 call_id／输入指纹／结果标识及缓存、发布记录，先确认真实来源；同版本发布按版本幂等策略处理，缓存生命周期问题独立核对。 |
| Refine／Index 共用 retry_count | Interval 的 Refine 失败计数在成功转入 INDEXING 后保留。采集中的 Interval 16 已有两次 Refine 超时，随后第一次 Index 失败即达到 `3 > max_retry_count=2`，进入 INDEXING_FAILED。 | 明确重试额度是否按阶段分配，验证前阶段失败后仍具有约定的 Index 重试机会；不通过删除异常或放松 source 校验掩盖失败。 |
| 原始 bulk item 错误日志缺失 | `ElasticsearchBulkWriteError` 保存 failures／status_codes，但消费者主要记录异常类型与摘要，无法从当次日志确认 item 是版本冲突还是其他 ES 错误。 | 记录 task／interval／lease、失败文档 ID、item status 与 ES error type／reason；故障注入能还原逐项原因，不输出向量正文或凭据。 |
| 既存缓存与任务索引恢复 | 新编码不能恢复已经发生舍入的原始值；INDEXING_FAILED 状态和缺失文档不会因部署新代码自动恢复。旧逻辑下 `request_index_rebuild()` 仅重置状态和 retry_count，保留缓存，不能改变错误的 source 比较策略。 | 先实施按身份／版本／完整性／可见性判断的策略，再确认全部目标可检索、Interval 与 Task 正常完成；已有相同正确版本无需因缓存尾差强制重算或推进 generation。精度或来源修复只针对独立证实的数据问题，不作为全部任务恢复的前置条件。 |

依据：[缓存保存与索引 receipt](../../server/repository/video_parse.py)、[索引协调与缓存恢复](../../server/service/video_parse_index_coordinator.py)、[ES external version 与完整 source 校验](../../server/storage/elasticsearch.py)、[Interval 重试计数](../../server/mq/video_parse_queue.py)、[消费者错误日志](../../server/mq/video_parse_consumer.py)、[模型 JSON 类型回归](../../tests/server/test_model_json.py)、[V2 Repository 回归](../../tests/server/test_video_parse_repository.py)、[Legacy IndexSync 回归](../../tests/server/test_index_sync_repository.py)，以及 `outputs/local-services/api.log` 中上述时间段记录和当日只读 MySQL／ES 比对结果。

### B09：同剧集并发完成上传时，父记录 S → X 锁升级导致死锁

优先级：高。类型：上传完成事务的并发锁冲突。状态：最小代码修复与真实 MySQL 回归已通过，运行服务尚未重启加载修复。记录日期：2026-10-14；以下故障时间均为 `+0800`。

#### 现象与影响

用户一次批量上传 13 个视频，首批 3 个文件并发上传收尾时出现错误。`2026-10-13 23:34:49`，其中两个请求在 `CREATING_JOB` 阶段报 MySQL `1213 Deadlock found when trying to get lock; try restarting transaction`，`POST /api/uploads/complete` 返回 HTTP 500。分片传输、合并、校验与媒体探测已经通过，失败发生在 Video／待执行解析任务记录的数据库提交阶段，尚未提交成功，也尚未执行这些文件的解析；与 B02 的提交后清理失败是独立问题。

| 上传会话 | 失败请求 ID | 后续恢复 |
| --- | --- | --- |
| `8c8d1954-5789-4e7a-9ca1-2471b107eb53` | `7535ac6a-b44e-4fac-846e-9d76fff363f4` | `23:36:05` 原会话重试完成 |
| `88e54a7d-7752-44f3-9dbe-34f241abe67b` | `e9ab99c2-7799-4f7e-97c2-df30eb454b54` | `23:35:15` 原会话重试完成 |

统计窗口为 `2026-10-13 23:34:43–23:36:06`，只统计 `outputs/server/logs/development/api.log*`，不与重复写入的 `outputs/local-services/api.log` 累加：

- 分片 PUT 共 293 次，全部 HTTP 200。
- 完成请求含重试共 8 次，其中 2 次 HTTP 500，请求失败率 25%。
- 首批同时完成的 3 个文件中，2 个首次失败，比例为 66.7%；不能将该比例外推为所有文件的最终失败率。
- 已进入完成阶段的 6 个文件在重试后均完成；窗口内另外两个新建会话尚未进入完成阶段，不计入该结果。
- 排查时保留日志中共 7 条 `upload_completion_failed`：6 条死锁（9 月 18 日、9 月 22 日、10 月 13 日各两条），另 1 条为视频身份冲突。历史错误按各自原因处理。

#### 两种锁分别锁什么

S（Shared，共享）和 X（Exclusive，排他）描述的是锁的**访问模式**，不是不同的数据对象，也不单独决定锁的粒度。本次发生冲突的两种锁都落在同一个数据库对象上：

```text
数据库：anivag_local_20261010_development
表／索引：series / PRIMARY
记录：id = 836fa9f0-2bf4-50fb-9c64-75526fa1dea9（mygo）
粒度：这一条主键索引记录，对应这一行 Series
```

| 模式 | 本次加锁来源 | 锁住的对象及目的 |
| --- | --- | --- |
| S 共享记录锁 | `INSERT video` 的 `video.series_id → series.id` 外键检查，由 InnoDB 自动取得 | 父表的上述 Series 记录；保证检查过的父引用在事务期间受到保护，阻止其他事务对该记录取得 X 锁 |
| X 排他记录锁 | `refresh_series_statuses()` 中 `SELECT series ... WHERE id = ... FOR UPDATE` | 同一条 Series 记录；为重新计算并更新剧集状态取得排他访问 |

虽然一个操作检查 `series.id`，另一个操作要更新 `series.status`，锁的单位仍是同一条记录，不是分别锁两个字段。每个文件还会锁各自的 UploadSession、插入各自的 Video，但本次等待环出现在它们共同引用的父记录 Series 上。死锁报告明确为 `locks rec but not gap`，因此这里是记录锁，不能解释成文件锁、整表锁、整页锁或范围／间隙锁。

对**不同事务在同一条记录上**申请的记录锁，兼容关系为：

| 已持有的锁／另一事务申请的锁 | S | X |
| --- | --- | --- |
| S | 兼容，可同时持有 | 不兼容，需等待 |
| X | 不兼容，需等待 | 不兼容，需等待 |

事务自己的 S 锁不阻止自己申请 X；阻止升级的是其他事务仍持有的 S 锁。本次外键检查得到的 S 锁不会在 INSERT 语句结束后立即释放，而是持有到事务提交／回滚。上述兼容表针对记录锁请求；当前 `REPEATABLE-READ` 下普通 SELECT 可以通过 MVCC 读取快照，不能把 X 锁理解为阻止所有普通查询。

#### 死锁如何形成及直接证据

前端 `MAX_PARALLEL_FILES = 3`。文件大小接近时，多个完成请求会在接近的时间进入 `UploadRepository.complete_upload()`。故障版本的事务先锁自己的 UploadSession，再插入并 flush Video，最后才通过 `refresh_series_statuses()` 对 Series 申请 X 锁。

```text
事务 A：插入 Video A → 持有 Series R 的 S 锁
事务 B：插入 Video B → 持有 Series R 的 S 锁（S 与 S 兼容）
事务 A：申请 Series R 的 X 锁 → 等 B 释放 S；A 自己的 S 仍保留
事务 B：申请 Series R 的 X 锁 → 等 A 释放 S；B 自己的 S 仍保留
结果：A 等 B，B 等 A；InnoDB 检测到死锁并回滚一个事务
```

2026-10-13 排查时只读执行 `SHOW ENGINE INNODB STATUS`，最新死锁时间与应用日志同为 `23:34:49`。实际版本为 MySQL `8.4.11`、隔离级别为 `REPEATABLE-READ`。报告中的事务 `2306436` 与 `2306437` 均持有上述 `series / PRIMARY` 记录的 S 锁，并等待同一记录的 X 锁，随后记录 `WE ROLL BACK TRANSACTION (2)`。摘要如下，原报告当时未另存文件：

```text
TRANSACTION 2306436: HOLDS lock mode S locks rec but not gap
                    WAITING lock_mode X locks rec but not gap waiting
TRANSACTION 2306437: HOLDS lock mode S locks rec but not gap
                    WAITING lock_mode X locks rec but not gap waiting
SQL（两者相同）: SELECT ... FROM series WHERE series.id = '836fa9f0-2bf4-50fb-9c64-75526fa1dea9' FOR UPDATE
```

等待环由数据库报告直接证实；S 锁的来源由事务代码中的 Video 插入、外键定义和 InnoDB 外键加锁规则共同解释。最新报告只保留最后一次死锁，两个失败请求则分别由应用日志确认，不把一份报告解释成完整记录了三个事务的全部冲突。

当前后端没有对 1213 做事务级自动重试；异常沿 `UploadService.complete()` 失败路径返回，释放完成认领并将会话恢复为 UPLOADING，保留已确认分片。前端完成接口异常直接标记整个上传任务失败，因此本地传输成功也会显示“上传失败”；原会话重试可复用已有分片。

#### 已实施的最小修复（2026-10-14）

- 仅调整 `server/repository/upload.py` 的 `complete_upload()`：锁住 UploadSession 并复核 `COMPLETING`／completion token 后，立即通过 `SELECT Series ... FOR UPDATE` 取得父记录 X 锁，再查询冲突、插入 Video 和任务，最后提交。原 Series 存在性检查前移并复用结果，生产代码新增 12 行、删除 5 行。
- 保留既有 `UploadSession → Series` 锁顺序，与删除收尾一致；未改成全路径 `Series → UploadSession`，也未调整删除和旧版索引回写的锁顺序。各并发完成请求在插入 Video 之前排队，避免先同时取得父行 S 再升级 X。
- 取得 Series X 后，后续 INSERT 的外键检查不会把它降为 S；排他锁持续到同一事务提交／回滚。串行范围仅为同剧集上传完成的数据库事务，分片传输、合并与媒体探测仍在锁外执行。
- 上传／删除竞争仍由原有加锁与状态机制保护：`begin_delete_session()` 锁住 UploadSession 后拒绝 `COMPLETING`；允许删除时将其置为 `EXPIRED`、清除 token 并保存删除标记；完成事务在锁内复核 status/token，拒绝失效请求。此机制不是检查一个 `Series.status = DELETED` 字段。
- 不新增事务重试，不改变 API、Schema、配置或前端并发数。运行中的 API 未重启，本条验证不等于已对原 13 个真实视频重新上传验收。

#### 验证结果

- `.venv/bin/python -m pytest -q tests/server/test_upload_service.py`：**87 passed**，包含完成幂等、删除拒绝 COMPLETING、分片与删除竞争及清理恢复等既有行为。
- 新增 `test_mysql_completes_distinct_uploads_in_one_series_concurrently[v1/v2]`：同一 Series 的 3 个不同文件通过 3 个独立数据库连接并发完成；SQLAlchemy 事件在 INSERT Video 后最多等待 1 秒以放大旧版竞争窗口，修复后串行路径无需等待其他 INSERT 到齐。验证三个上传均完成、视频和任务唯一，重复 complete 复用原 ID。
- 在新建的临时独立 MySQL 测试库中，将 HEAD 的旧 `complete_upload()` 方法仅加载到测试子进程执行上述新增用例：**2 failed**，V1／V2 均复现原 `SELECT Series ... FOR UPDATE` 的 **1213 死锁**。此为预期失败的回归有效性验证，未回写或回滚工作区源码。
- 随后运行修复版新增两项，以及 `test_mysql_delete_fences_in_flight_upload_and_preserves_order`、`test_mysql_delete_completed_upload_removes_video_job_and_usage`、`test_mysql_enforces_episode_order_under_concurrent_requests`、`test_mysql_chunked_upload_claim_and_lease_fencing`：**6 passed**。测试结束后仅删除本次新建的临时库，没有修改现有业务库；未运行解析 Worker 或模型调用。
- `git diff --check` 通过。未执行服务重启、业务库迁移、commit 或 push。

#### 观察到但本次未改

- `create_session()` 的幂等重放已避免同时持有 Series 再等待原 Upload；真正新建路径仍存在 `Series → occupying Upload` 顺序，与完成路径的反向取锁是既有独立风险，需要单独核对冲突序号及扫描范围。上述回归不代表覆盖了所有新建与完成交错，也不宣称消除所有数据库死锁。
- 上传收尾是否需要每次重算整部剧状态，以及 1213 的有限事务重试，留作独立评估；本次保持业务状态规则，专门修复已证实的父记录 S → X 升级冲突。

依据：[上传事务](../../server/repository/upload.py) 的 `complete_upload()`、[剧集状态刷新](../../server/repository/index_sync.py) 的 `refresh_series_statuses()`、[Video 外键](../../server/model/video.py)、[上传失败收尾](../../server/service/upload.py)、[前端上传调度](../../web/src/features/uploads/UploadProvider.tsx)；本机应用轮转日志中的上述 request_id／upload_session_id 与当时只读获取的 InnoDB 死锁报告。锁语义参考 [MySQL 8.4 InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)、[各 SQL 语句的加锁规则](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)。

### B10：正常停服没有解析任务交接，恢复消耗重试且整集收尾受调度阻塞

优先级：高。类型：V2 解析生命周期与恢复调度缺陷。状态：诊断已确认；10:20 已通过现有接口恢复第 7、8 集的调度资格，解析生命周期／调度代码尚未修复，本轮没有重启 API。最初记录时间：2026-10-14 02:28–02:31（`+0800`）；10:13–10:22 的新现场见下方补充。这是解析阶段问题，与 B09 上传完成事务的死锁分开处理。

#### 02:28 历史任务快照与当时的重启判断

本机 MySQL 的 13 个未删除、非测试 VideoParseTask：

| 视频 | 数据库状态 | 本次核验的执行情况 |
|---|---|---|
| 第 1、2、3、4、6 集 | COMPLETE | 已完成，不因重启重做 |
| 第 5 集 | PARSING | 唯一活跃整集租约，owner=`local-consumer-10201`，attempt=1/max=3；02:28 快照有 10 个 COMPLETE、4 个 UNDERSTANDING、10 个 QUEUED Chunk，4 个在途 Chunk 的 retry 均为 0/max=2 |
| 第 7、8 集 | PARSING | 主任务无 owner，last_error=`worker lease expired`，attempt=1/max=3；已完成 Chunk 分别 8/24、19/24，等待整集调度恢复后继续派发 |
| 第 9 集 | INDEXING | 主任务无 owner，last_error=`worker lease expired`，attempt=1/max=3；24/24 Chunk 和 22/22 Interval 已 COMPLETE，整集成功状态尚未收尾 |
| 第 10–13 集 | PENDING | 尚未开始，attempt=0/max=3 |

按该快照，重启一次不会仅因上述活跃任务的重试预算直接耗尽而失败，但不能描述为无损、立即续跑：第 5 集尚未提交的在途 Chunk 会重做，并消耗重试；已提交结果保留。快照会随运行变化，执行重启前必须重新读取在途状态，不能把这组次数当作永久保证。

02:31 的待领取主任务顺序为第 8 → 9 → 7 → 11 → 10 → 12 → 13 集；第 5 集仍占据唯一主调度名额。如果此时再中断第 5 集，回收会把它的 next_execute_time 推到当时的“现在 + 30 秒”，可能排在这些已有任务之后。日志显示上一进程在 01:15:22 开始 shutdown，新进程 10201 在 01:43:13 启动；这些是既有记录，本轮没有操作重启。

#### 已确认的原因

1. **API 与解析 Consumer 在同一进程，重启 API 同时中断解析。** Consumer 虽在独立线程运行，生命周期仍由 API lifespan 管理。
2. **正常停止直接取消在途任务，没有归还 Video／Chunk／Interval 租约。** `run_forever()` 的 finally 调用 cancel + gather，三个执行入口对 CancelledError 仅重新抛出。新进程依靠 reap_expired 处理旧租约；当前默认租约 300 秒、失败退避 30 秒，还要等待调度名额。Prefix 的正常取消已有主动归还逻辑，是例外。
3. **计划停服被当作失联，消耗失败预算。** Video 重新领取增加 attempt；过期时 attempt 已达 max 则停止自动恢复。Chunk／Refine／Index 过期直接增加 retry，超过冻结配置中的 max_retry_count 则失败。当前业务任务上限分别是 3 次领取、2 次重试；反复中断可以耗尽预算，已有成果并不会因此被删除。
4. **持久化边界是完成一个 Chunk，不是完成一次 ASR／模型调用。** ASR／OCR／Canvas 在理解期间只存内存；整段理解结束才提交。取消清理本租约临时媒体，所以未提交 Chunk 的媒体准备和 ASR／理解需要重做。已提交 Chunk、Refine 结果、索引回执会保留，不能说重启必然整片从头解析。
5. **整集恢复及成功收尾共用唯一主调度名额。** 一个 Video handler 一直持有名额直到整集完成。领取按 next_execute_time、create_date、id 排序，不按恢复优先。子任务可以跨视频继续领取，但未派发或需要重新派发的 Chunk 依赖各自父调度；Interval 索引提交只更新子任务，不归并父成功状态，过期回收也不补成功归并。这解释了第 9 集子任务全完成却仍显示 INDEXING：重新领取父任务后可直接收尾，无需重跑已经完成的内容。

#### 证据边界与后续验收要求

- **系统记录确认了停服前的两次自动睡眠。** 只读 `pmset -g log`：00:16:34 进入 Idle Sleep，00:21:02 唤醒（268 秒）；01:02:59 进入 Idle Sleep，01:14:30 唤醒（691 秒）。第 8 集 00:21:43、第 9 集 01:14:30 的主任务租约丢失均在相应唤醒后，且早于 01:15:22 停服。应用同时存在约 267／688 秒的墙钟与 monotonic 执行耗时差，与系统暂停吻合。睡眠期间无法续心跳，而数据库租约按 UTC 墙钟到期；这些已有失联不能归因于后来的服务重启。第一段睡眠虽小于 300 秒，叠加入睡前已消耗的心跳周期仍可能越过租约期限；未拿到入睡前逐次心跳时间，不声称精确还原了该段到期瞬间。
- 已有单元测试覆盖过期接管、旧 owner 隔离、部分成果复用等；没有找到“真实在途任务 → SIGTERM → 新进程恢复 → 不扣计划停服重试”的完整验收。本轮只读测试源码，没有运行真实模型或中断业务任务。
- 修复首先应让计划停服具备明确交接：停止领取、在受控期限内完成或取消在途工作、以当前租约版本归还可续跑任务，并与真实执行失败的重试计数分开。还需要让已完成子任务的整集成功收尾不被长时间运行的另一个 Video 阻塞。ASR 等细粒度成果复用是独立的重做成本优化，不能替代停服交接。

#### 10:13–10:22 现场：服务存活，但恢复等待没有明确状态和手动入口

本轮按用户要求执行 `./start.sh status` 和 `./start.sh start`。MySQL、API／Parse Consumer、Web、Elasticsearch 均在运行；启动流程复用原进程，API PID 仍为 10201，未中断第 13 集的在途执行。`/healthz` 返回 HTTP 200，`parseConsumer.status=running`、`failure=null`；直连 API 与 Web 代理的任务列表一致。B09 的上传事务修复仍只是磁盘代码，本轮没有通过重启加载。

| 视频 | 10:13 现场 | 本轮处理与 10:21 验证 |
|---|---|---|
| 第 1–6、9 集 | COMPLETE，共 7 集 | 已完成成果保留 |
| 第 7 集 | PARSING_FAILED，attempt=3/max=3，主租约已清空，last_error=`worker lease expired`；8/24 Chunk、2/22 Interval COMPLETE | 10:20 调用现有 `retry-parse` 返回 200，原 task ID／version=1 保持，max_attempts 补到 4；上述成果数量不变，等待整集恢复调度 |
| 第 8 集 | PARSING_FAILED，同样因主任务租约失效耗尽 3 次领取；19/24 Chunk、9/22 Interval COMPLETE | 同样返回 200，原任务不变、max_attempts=4；已完成成果数量不变，10:21 已有两个 Chunk 进入 UNDERSTANDING |
| 第 10–12 集 | PARSING，但主任务 owner／expiry 均为空，attempt=2/max=3，last_error 同为租约过期；已完成 Chunk 分别为 4／0／10 | 已到可领取时间，仍等待整集协调器；已有可执行子任务不一定完全停止，例如第 12 集有一个 Interval 新完成。不能把“主任务无 owner”等同于“所有子任务无人执行” |
| 第 13 集 | PARSING，有效 owner=`local-consumer-10201`；24/24 Chunk COMPLETE，正在精修 | 10:21 精修结果达到 20/22，另外两个 Interval 为 REFINING；主租约持续续期。Consumer 没有整体停摆 |

10:24 最后复核：四项服务状态仍正常，第 13 集已从 PARSING 推进到 INDEXING；第 7、8、10–12 集仍无主任务 owner。以上恢复等待尚未全部解除。

10:21 主任务的到期候选顺序为第 11 → 10 → 12 → 7 → 8 集。`VideoParseConsumer.run_once()` 仅在 `_video_tasks` 为空时领取下一个整集任务；第 13 集仍持有唯一协调器名额，因此其他集即使 `next_execute_time` 已到，也不会立即领取主租约。已派发的部分子任务仍能跨视频执行；需要重新派发的 Chunk 则依赖父协调器。这是代码中的调度策略限制，不是发现了新的 MySQL 死锁，也不能从“服务健康”推导出“每个任务都在推进”。

**今晨租约失效另有新的睡眠证据。** `pmset -g log` 显示 02:49:14 进入 Clamshell Sleep（合盖），之后反复 Maintenance Sleep／DarkWake，直到 09:41:21／09:41:27 才记录完整 Wake；本轮不能称为 Idle Sleep。第 8 集第三次主任务失败在 08:49:38，恰逢 08:41:02 起睡眠 516 秒后的 DarkWake；第 7 集第三次失败在 08:54:53，前面先睡 252 秒、短暂唤醒后又睡 38 秒，最后于 08:54:52 DarkWake。第 11、10、12 集的主任务失败分别在 09:07:00、09:31:19、09:39:55，也紧邻短暂唤醒。结合 DB 的 `worker lease expired`，这些记录强烈支持反复睡眠导致续租中断；尚未逐条取得最后一次成功 heartbeat，不声称每个失败都只有这一个原因。应用日志里的阶段标签 `prepare` 覆盖整集协调器，不表示故障发生在上传或尚未解析。

上述主任务失败日志均为 `retry_recorded=false`：当前 owner／租约已不符合续排写入条件，不能把日志中计算出的 retry_at 当成成功重新排队。09:41 完整唤醒后另外出现 Ark API 连接失败／DNS `gaierror`，且 `retry_recorded=true`；这是模型调用的网络故障，须与睡眠时的租约失效分开记录。本轮没有修改系统睡眠设置，也没有通过反复重启来尝试恢复。

**手动入口缺口已同时由页面、接口和源码确认：**

- 第 7、8 集在本轮恢复前，任务页确实有“重试解析”按钮，列表 `canRetry=true`。`POST /api/uploads/retry-parse` 以 `uploadSessionId + tenantId + expectedJobVersion` 恢复最新失败任务；它保留已完成成果及执行历史，为父任务补足至少一次领取额度。它不建立新解析版本，也不等于抢占当前整集协调器。
- 第 10–12 集仍是 PARSING，列表同时返回 `canRetry=false`、`canReparse=false`。前端据此隐藏操作，后端也只允许失败终态走 retry；直接调用接口不能绕过。当前没有为“已接受、非终态、主租约已释放、等待恢复”提供专门的手动继续入口。
- 同一条记录却仍返回“视频解析失败，请重试”的公共错误，任务卡显示“正在解析画面与对白”。第 7、8 集续跑成功后也暂时保留旧 `last_error`，直到父任务再次领取时才清空。当前页面混淆了正在执行、恢复排队、最终失败，无法帮助用户判断是否需要操作。
- 上传弹窗的“重试”调用上传恢复／`completeUpload`，不是解析 `retry-parse`；已经上传成功的解析卡住问题不能靠重复完成上传来修复。

**待实施的恢复入口必须与调度一起设计。** 对最新任务校验版本、删除状态、失败意图及父／子／Prefix 租约；正在执行时返回明确原因，已经排队时返回幂等的排队结果，确需恢复时保留完成产物并唤醒有界恢复调度。不能仅以 `lease_owner=NULL` 就清空子任务或强制重做，也不能只是新增一个把状态改成 PARSING 的按钮——这两集调用现有 retry 后仍需等待父协调器，已证明“重新排队”与“马上续跑”不是一回事。页面需要直接展示恢复等待原因、下一次调度时间和可执行操作；验收须覆盖“另一个长任务仍运行时，中断任务也能在有界时间内恢复”，并保持模型调用并发限制。

本轮另修复了一个启动脚本的小问题：Web 页面品牌已是 Seekino，旧探针硬编码 HTML 必须包含 `anivag`，错误拒绝复用正常的 5173 端口。`scripts/local_services.py` 改为检查 Vite 客户端与 `/src/main.tsx` 入口，随后仍验证 Web 的资料库 API 代理；修改后 `./start.sh start` 成功。此探针错误与解析调度缺陷分别处理。现有启动脚本测试为 24 passed／8 failed；8 个失败都来自 `_model_config()` 测试替身缺少当前配置函数使用的 `normalized_models`。在测试进程内恢复旧探针后，仍得到同样的 8 个失败，未修改这些无关测试或配置代码。

补充依据：[手动续跑接口](../../server/api/uploads.py)、[续跑状态校验和成果保留](../../server/repository/upload.py)、[操作能力投影](../../server/api/jobs.py)、[任务页按钮](../../web/src/features/tasks/TasksPage.tsx)、[上传恢复入口](../../web/src/features/uploads/UploadProvider.tsx)、[启动探针](../../scripts/local_services.py)；本机 10:13、10:21 MySQL 快照、10:20 两次续跑 HTTP 200 与 10:22 健康／任务列表检查。这里记录的是恢复资格与已观察到的子任务推进，不代表六个未完成视频已经解析完成。

#### 10:43–10:47 新现场：已恢复调度，但模型账号欠费导致失败

本次再次检查时，问题已变化，不能继续把所有失败解释为租约或排队：

- 10:43 第 13 集已 COMPLETE，第 12 集已经领取第三次主租约；第 10、11 集陆续出现失败 Chunk。它们父任务的 `VideoParseTask is draining after a terminal failure` 只是发现子任务失败后停止派发的结果。
- 读取失败 Chunk 的原始 `last_error`，第 10、11、12 集均为 HTTP 403、`AccountOverdueError`，供应商明确返回 `account has an overdue balance`。10:47 再检查，第 7 集四个失败 Chunk 也都是相同欠费错误；这些 Chunk 的 retry_count 均为 3。
- 10:47 第 8、13 集已 COMPLETE；第 7、10、11、12 集均为 PARSING_FAILED，任务列表 `canRetry=true`。失败任务目前已有安全的手动重试入口。本轮没有继续对欠费任务重试、切换模型或重启服务。
- 第 8 集在 10:43 已有 24/24 Chunk 和 22/22 Interval COMPLETE，但父任务仍为 PARSING；10:47 才通过第四次领取完成父状态收尾。说明自动成功归并缺口仍在，只是本次最终轮到了它。

**即时恢复顺序：** 如果沿用现有模型，先恢复实际使用的 Ark 账号服务，再通过任务页“重试解析”或原 `retry-parse` 恢复第 7、10、11、12 集。保留已完成成果和原任务版本，不需要重新上传或整集重新解析。服务重启不能修复账号欠费。若要切换模型，需要先确认可用来源及对应阶段；第 10 集冻结配置中 ASR 和 Embedding 来源均为 `ark_default`，理解来源为 `codex_local`，仅更换理解模型不会替换 Ark ASR／Embedding。

当前 `VideoParseConsumer._run_chunk()` 对所有 Exception 走同一 `retry_chunk()`，队列每次增加 retry_count，所以欠费错误也会耗尽自动重试额度。当前生成模型 fallback 只接收 `ContentFilterError`，transcription／embedding 则没有对应 fallback，不能据“配置了备用模型”断言欠费可自动绕过。后续需要给欠费／认证等人工干预错误明确分类和可操作提示，避免按临时网络错误反复重试；本轮尚未修改该机制。

**具体失败模型已由请求 Trace 确认。** 第 10、11、12 集失败调用的 operation 均为 `candidate_transcript`（ASR 转写），provider=`ark`、source=`ark_default`、request.model=`ep-20260911212241-gg8bm`，响应均为 403／`AccountOverdueError`。同一端点在当日 10:37:56 的成功响应明确返回 model=`doubao-seed-2-0-lite-260428`，因此可定位为 ASR 使用的豆包 Seed 2.0 Lite 所属 Ark 账号返回欠费；这些调用不是本地 Codex 理解调用，也不是 Embedding。连接来源配置使用 `ARK_API_KEY`。失败证据：[EP10 请求／错误](../../outputs/model-traces/b2fac332-22cd-4769-9563-9cad42f02534/events.jsonl)、[EP11 请求／错误](../../outputs/model-traces/b5d9df7e-bbb5-44ab-a586-6242de1e564c/events.jsonl)、[EP12 请求／错误](../../outputs/model-traces/4bc3bc1c-1db3-4533-9ecc-b374f1d1800a/events.jsonl)；端点模型名依据：[紧邻的成功请求／响应](../../outputs/model-traces/7de40feb-965d-4554-951e-c9f59f51d290/events.jsonl)。上述判断使用实际请求与响应，没有拿当前 YAML 的新模型名替代历史调用证据。

**页面为何只报“视频解析失败，请重试”：错误上报链存在两层信息丢失。** Chunk 的 last_error 保存了欠费原文，但 `_aggregate_video_task()` 只归并状态／failure_status，没有将失败子任务的根因传到父任务；父协调器随后因 failure drain 退出，`retry_video()` 将父 last_error 写成 `VideoParseTask is draining after a terminal failure`。`project_video_parse_task()` 又完全不使用错误内容，只要 last_error 存在就生成固定 `PROCESSING_FAILED` 文案。`TasksPage` 原样显示这段后端 message，前端没有进一步吞掉欠费信息。

这处展示缺陷需要沿后端错误链修复：保存和保留可识别的供应商错误码、实际失败阶段与调用模型，避免后续收尾异常覆盖首要根因；API 将已知欠费错误映射为明确的公开文案，未知异常才使用通用提示，保留内部原文不直接透出的边界。现存失败任务也应能从失败子任务补出根因，不能要求用户再次触发欠费。现有 `Failure.code/message` 和前端展示已足够承载准确提示，不必为此新增按钮。验收须覆盖“ASR 欠费 → 子任务失败 → 父任务收尾 → 页面仍显示账号欠费”，同时保留敏感原文不泄露的测试；当前投影测试 `test_video_parse_task_projection_hides_internal_failure_detail` 将所有错误固定泛化，需要同步调整预期。此修复尚未实施。

最小独立代码修复可先补无主父任务的成功归并：扩展现有 Queue 周期收尾，在新的事务中锁定符合条件的父任务，复用既有归并规则；全部子任务完成时直接完成父任务，不再为了收尾领取一次主租约。保持失败优先、在途子任务保护和旧租约隔离；候选筛选及 limit 不能使完成任务长期排在不会变化的等待任务之后。这一补丁与账号恢复分别处理，多父协调器、停服交接和页面等待展示随后按独立范围实施。

依据：[Consumer 异常处理](../../server/mq/video_parse_consumer.py)、[重试计数与周期归并](../../server/mq/video_parse_queue.py)、[Fallback 触发条件](../../gateway/model_factory.py)、[模型阶段约束](../../gateway/config.py)；10:43–10:47 本机数据库的失败 Chunk、冻结配置及任务列表只读快照。供应商账号实际余额未直接查询，欠费判断依据是其明确返回的错误码与错误消息。

#### DB Queue 与 Redis 的判断（2026-10-14 补充调研）

**当前建议：先修复任务生命周期与恢复语义，保留 MySQL 队列；将 API 与解析 Worker 拆成独立进程。Redis 可作为后续消息分发优化，但不是本次脆弱性的必要修复条件。** 本段为待实施方案，尚未调整解析架构或迁移 Redis。

需要区分三件事：队列负责“哪项工作该交给哪个 Worker”；任务状态机负责“中断后从哪里继续、什么时候算失败”；进程部署决定“重启 API 会不会同时停止 Worker”。换 Redis 主要改变第一件事，后两件事仍要由应用实现。

| 当前问题 | 所属机制 | 只换 Redis 能否解决 |
|---|---|---|
| 重启 API 同时取消解析 | API 与 Worker 共进程 | 不能；独立 Worker 使用 MySQL 队列同样能隔离 API 重启 |
| 正常停服不归还租约，等待过期 | Worker 停止与交接流程 | 不能；Redis pending 消息也需要明确的恢复／接管逻辑 |
| 计划停服、睡眠消耗失败预算 | 领取次数／失败次数混用 | 不能；Redis 投递次数也不应直接当业务失败次数 |
| ASR 成功后中断仍重做 ASR | 片段内中间成果未持久化 | 不能；队列不保存内存中的解析成果 |
| 第 9 集全部子任务完成，父仍 INDEXING | 成功归并依赖唯一主调度名额 | 不能；要调整归并触发和恢复调度 |
| 空闲轮询、任务量增大后的 DB 调度负担 | 消息发现与分发 | Redis Streams 的阻塞读取和消费组可能有帮助；本轮没有吞吐／锁等待基准证明这里是瓶颈 |
| 电脑自动睡眠，Worker 无法续心跳 | 执行宿主暂停 | 不能；同机 Redis 不能让睡眠中的 Worker 工作，异机接管仍需要可访问的媒体和正确的旧执行隔离 |

MySQL 本身并不排斥队列表：官方明确将 `SKIP LOCKED` 用于多会话访问队列表时避开已锁记录。AniVAG 的领取逻辑已经使用它；当前还拥有“创建业务记录和待执行任务可在同一数据库事务提交”的便利。需要修复的是我们在其上实现的停止、重试和调度规则，不能由这次故障推导出 MySQL 队列必然不可靠。[MySQL 锁定读取](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)、[当前领取代码](../../server/mq/video_parse_queue.py)。B09 的 Video 外键／Series 事务锁顺序也是独立问题：换成 Redis 分发任务不会消除这些业务 SQL。

#### 本地 RagFlow 实际怎么做

核对对象是同级 `../ragflow` 的本地源码，HEAD=`313ca90f6abd7682fe8523e16fd67b3653a3fa84`，核对时该工作区干净；不是对所有 RagFlow 发布版本或线上配置的概括。它的 Python 和 Go ingestion 路径必须分开看。

**Python 路径使用 Redis 协议 Streams，当前 Compose 对应服务是 Valkey。** `queue_product()` 用 XADD 入队，`queue_consumer()` 用 XREADGROUP 消费，`RedisMsg.ack()` 用 XACK 确认。启动时先读取同名 consumer 的未 ACK 消息，再取新消息；业务 Task、进度和 retry_count 仍保存在关系数据库，解析成果也由业务存储承接。API 与 task executor 有独立启动进程，这种部署隔离值得借鉴。[Redis 封装](../../../ragflow/rag/utils/redis_conn.py)、[任务接收与执行](../../../ragflow/rag/svr/task_executor.py)、[TaskService](../../../ragflow/api/db/services/task_service.py)、[启动脚本](../../../ragflow/docker/entrypoint.sh)。

但该 Python 路径并不自动提供我们缺少的全部能力：

- 未 ACK 回放仍会经过 `TaskService.get_task()` 并增加数据库 retry_count；前三次领取可执行，第四次领取读到 `retry_count >= 3` 时标记失败并放弃。`TE_RUN_MODE=0` 的重构执行器也共用这层领取和 ACK 逻辑。
- 当前可达消费路径没有找到 XCLAIM／XAUTOCLAIM 的跨 consumer pending 接管；`get_pending_msg()`／`requeue_msg()` 有定义但未找到调用。失联 Worker 清理删除注册与心跳，不等于已将其消息转给其他 consumer。不能把“同名 Worker 重启可回放”说成“任意 Worker 都能自动接管”。
- 普通执行异常会记录失败后走 ACK；协程 CancelledError 则跳过尾部 ACK，留下 pending。有限等待在途完成只在启用 `MAX_TASKS_PER_WORKER > 0` 的退出流程中执行，默认值是 0，不能概括为默认优雅停服都会等待任务完成。

**本地 Go ingestion 的现行队列是 NATS JetStream，已经不是 Redis 队列。** `InitMessageQueue()` 只装配 NATS；KVrocks 中虽然保留 Redis Streams helper，但没有发现 Go 业务调用。消息主要携带 task_id/task_type，业务状态仍查数据库。值得参考的是它明确区分“停服中断”和“用户取消”：停服保留非终态，交给 broker 再投递；终态通常 ACK，未终态 NACK；Stop 先停取新任务，再取消执行并有期限地等待清理，超时留下未确认任务。长任务仍需心跳续期。[MQ 装配](../../../ragflow/internal/engine/global.go)、[Go 任务执行与停止](../../../ragflow/internal/ingestion/service/ingestion_service.go)、[消息合同](../../../ragflow/internal/common/task.go)。

这仍不等于模型调用中间位置可继续：Go pipeline 明确从图入口重放，由组件缓存决定复用哪些已有成果；另有已写 DB、尚未确认入队任务的启动补发。**可借鉴的是停服语义、消息确认与业务终态的关系、结果复用和补发机制，而不是仅复制一个 Redis 依赖。** [Pipeline 重放边界](../../../ragflow/internal/ingestion/pipeline/pipeline.go)、[持久任务补发](../../../ragflow/internal/service/ingestion_task_service.go)。

#### Redis 能额外提供什么，以及增加什么成本

Redis Streams 消费组提供消息分摊、待确认记录和阻塞等待；XPENDING 可查看已投递但尚未确认的消息，XCLAIM／XAUTOCLAIM 可用于转移符合条件的未确认消息。它可以减少用业务 DB 高频轮询发现任务的需要，并为更多独立 Worker 提供统一消息分发能力。多 Worker 本身不是 Redis 独有能力，当前 DB 领取也有并发隔离；是否值得迁移应看领取延迟、调度 SQL 成本和实际扩展需求。[XREADGROUP](https://redis.io/docs/latest/commands/xreadgroup/)、[XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)。

如果后续采用 Redis，应选择具备确认／恢复语义的 Streams 消费组或经过验证的任务框架；普通 Pub/Sub 断连时会丢消息，不适合作为持久任务队列的唯一来源。[Redis Pub/Sub 投递语义](https://redis.io/docs/latest/develop/pubsub/)。还必须设计以下边界：

1. **数据库与消息发布的一致性。** “任务已在 MySQL 提交，进程在 XADD 前退出”会留下没有消息的任务。可采用与业务任务同事务保存的待投递记录，再由发布者重试发送；或以 DB 待执行状态为依据做可重放的投递和补偿扫描。不能只写 `commit(); xadd()` 就宣布可靠。
2. **允许消息重复，成果提交必须幂等。** “成果已提交，XACK 前退出”会重复投递。新 Worker 应先读任务版本和完成状态，已完成则确认跳过；未完成才领取执行。Redis 的消息 ownership 不会替旧进程撤销已经发出的模型请求，仍需保留任务版本／租约校验和索引版本保护，拒绝旧执行迟到提交。这是跨 DB、模型和索引写入边界的设计要求。
3. **Redis 自身的持久化和运行维护。** 需要配置并验证重启恢复、持久化、容量及清理策略。RDB 是定时快照，AOF 的刷盘策略也有不同的丢失窗口；不能把内存队列当成天然比现有 DB 更耐故障。任务可由 DB 补投时，可降低消息层丢失对业务的影响。[Redis 持久化](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。

AniVAG 虽已有 `redis>=5,<7` 客户端依赖，目前 Redis 接入属于 cloud auth 的 OTP／限流／查询并发控制；local 模式不创建该客户端。它不是已经部署好的解析队列，迁移仍需独立的队列配置与生命周期管理，不能把认证 Redis 的职责直接混进解析队列。[当前 Redis 接入](../../server/caller/redis.py)、[认证配置](../../server/config/auth.py)。

#### 建议分批解决与验收

以下是待实施的职责划分，不代表本轮已经改代码或授权迁移。先完成不依赖新中间件的修复，每批单独验证；不把 Redis 迁移作为这些修复的前置条件。

| 批次 | 具体目标与归属 | 必须通过的验收 |
|---|---|---|
| 1：停服交接与次数语义 | Consumer 停止领取并冻结新派发，继续维持在途租约心跳；在有限期限内让在途步骤提交或取消，确认本地执行及其受管子进程已退出；之后按 owner + lease_version 条件归还未完成任务。Queue 区分计划停服、失联恢复与真实执行错误，正常停服不扣业务失败预算 | 有在途 Chunk／Refine／Index 时正常停启，保留已提交成果，不因计划停服耗尽预算；归还后立即具备领取条件；旧 owner 的迟到提交被拒绝 |
| 2：成功收尾与恢复调度 | Repository／Consumer 让子任务完成后的成功归并不再依赖唯一整集名额；加入有界的恢复调度，兼顾已有任务与新任务，保持当前并发预算 | 一个视频长时间运行时，另一个子任务全完成的视频能及时变为 COMPLETE；中断的第 7／8 集能继续派发；新任务也不会永久饿死 |
| 3：独立解析 Worker | 复用现有业务装配和 DB Queue，提供独立 Worker 进程入口，API 不再默认拥有该 Worker 的生命周期；启动脚本分别管理 PID／退出与健康状态 | 只重启 API 时 Worker PID、在途执行和失败预算不变；单独重启 Worker 时满足第 1 批；同机源码变更仍应遵守任务配置／代码版本边界 |
| 4：片段内成果复用 | 在语音识别等昂贵步骤成功后保存完整结果，按原视频身份、片段范围、步骤配置／模型及任务版本校验后复用；沿用现有持久存储，不依赖临时目录 | ASR 已成功、理解未完成时中断，恢复跳过 ASR，仅重做未完成理解；输入／配置变化不误用旧结果；模型用量记录不因复用重复记账 |

第 1 批有两个不能漏掉的细节：一是 `claim_video()` 每次领取都会增加 attempt_count，**只增加 release、却保留原来的领取与 max_attempts 判定，仍会因正常停启耗尽预算**；必须一起明确执行历史与失败余额的口径。正常交接不回退已发生的模型用量／调用预算，每次重新领取仍递增 lease_version。二是不能在旧执行还可能写入时先归还租约；本地执行无法在期限内停止时，不主动清空租约，应停止续租并退出或终止旧进程，由租约过期接管兜底。已发出的远端请求可能仍在执行，接管后仍须以租约版本和索引版本拒绝迟到写入，不能降低版本校验来强行续跑。失联恢复也应有独立的次数／速率限制和告警，避免把确定性崩溃变成无限重放。

独立 Worker 目前没有正式常驻入口：`tests/manual/server_mq_once.py` 只是单次消费的测试入口，不应直接当生产 Worker 启动。未来需要修改进程装配与启动脚本，这一批应单独评估；同机独立进程也不能消除整台电脑睡眠。[现有装配](../../server/main.py)、[本地服务脚本](../../scripts/local_services.py)、[单次测试入口](../../tests/manual/server_mq_once.py)。

共同验收还应包括：强制退出或暂停超过租约后可接管；旧进程恢复不能覆盖新结果；在“结果提交成功但确认／响应丢失”的窗口重复执行不会重复发布；日志区分 shutdown／lease_expired／model_error，并能看到剩余重试、恢复等待和重复调用成本。停机测试应先在隔离数据库与可控模型替身中执行，不拿当前 13 个真实视频做破坏性演练。只有在上述行为正确后，仍测得队列 SQL 开销或领取延迟显著、或者需要更复杂的分布式消息分发，才单独评估 Redis Streams／其他 broker。

依据：[API 生命周期](../../server/main.py)、[Consumer 停止／领取／心跳](../../server/mq/video_parse_consumer.py)、[租约回收与重试预算](../../server/mq/video_parse_queue.py)、[持久结果提交与整集归并](../../server/repository/video_parse.py)、[整集调度](../../agent/video_parse_v2/runtime.py)、[Chunk 理解提交边界](../../agent/video_parse_v2/understanding/workflow.py)；本机 MySQL 的上述只读快照，以及 `outputs/server/logs/development/api.log` 的既有记录。

#### 2026-10-14 专用ASR替代调研

状态：已核对官方价格与接口文档，尚未接入或对 MYGO 音频做质量实测。范围仅为日语对白转写及时间戳，不替换画面理解／Refine。以下国内方案价格为人民币常规按量价格，不含免费额度、资源包折扣或额外存储／流量。

**现有 Lite 用量审计，不能与充值金额直接画等号。** 按当前 13 个 VideoParseTask ID 关联本地 `outputs/model-traces/*/events.jsonl`，找到 377 次 `candidate_transcript` 请求：280 次 provider completed 且有完整 usage，94 次 error（73 次欠费 403、15 次连接错误、4 次 ReadTimeout、2 次 ArkTimeout），3 次没有完整终态／usage。以下只计算 280 次成功返回的用量，未获得用量的请求不计入估算，但不据此承诺供应商没有计费；也未查询供应商余额、整账号账单或其他项目消费。

| 已返回的 ASR 用量 | Token 数量 | 按普通在线、输入 ≤32K 的官方刊例价估算 |
|---|---:|---:|
| 非音频输入（视频／文字） | 6,876,544 | 0.6 元／百万，约 4.13 元 |
| 音频输入 | 103,642 | 9 元／百万，约 0.93 元 |
| 输出（包含推理 Token） | 395,787 | 3.6 元／百万，约 1.42 元 |
| 合计 | 输入总计 6,980,186；输出 395,787 | **约 6.48 元，属有记录调用的估算，不是供应商账单** |

280 次成功调用均在 32K 输入档内、cached_tokens=0。输出中 reasoning_tokens=218,031，约占输出 55.1%，已经包含在输出总数内，不能重复收费估算；请求中未传 thinking／reasoning 关闭参数，实际响应确有推理用量。同一批任务中存在 30 次重复成功 ASR 调用，主要集中于第 1、2 集，不能把所有重复请求或所有 403 都算成付费重做。2026-10-13 23:00 后的 179 次成功调用按同一口径约 4.13 元；2026-10-14 当天的 155 次约 3.59 元。[普通在线计价及分模态费率](https://docs.volcengine.com/docs/ark/model-pricing?lang=en&redirect=1)

全部 377 次 ASR 请求都使用 `input_video`、fps=1；当前 `VideoASRAgent` 将视频窗口送给通用多模态模型，即便业务只要求转写。ASR 成果仅在整个 Chunk Understanding 完成时一起提交，中途失败会重新转写。这分别带来视频输入开销和重复调用开销；更换专用音频 ASR 与增加 ASR checkpoint 是两项不同的降本措施。[视频转写输入](../../engine/common/transcription.py)、[片段内成果提交](../../agent/video_parse_v2/understanding/workflow.py)

**同批次 Embedding 的已记录费用更高。** MySQL `model_usage` 中，关联上述 13 个任务的成功 Embedding 均为 `ark / doubao-embedding-vision-250615`，按不同 call ID 记录；成功视频 Embedding 为 449 次，图像输入 17,377,605 Token、文本输入 245,204 Token；成功纯文本 Embedding 为 400 次、文本输入 722,738 Token。按官方图像 1.8 元／百万、文本 0.7 元／百万估算，合计 **31.9572484 元，约 31.96 元**，其中图像输入约 31.28 元。另有 1 次失败视频 Embedding 缺少 usage，未计入估算。2026-10-13 23:00 后的 Embedding 按同一口径约 22.62 元。还存在较早的 Pro 视觉理解调用，本段并非整个账户的账单核对；目前只能判断这批已记录用量中 Embedding 比 ASR 更贵，不能据此解释全部充值去向。换 ASR 不会减少检索索引的 Embedding 费用。[方舟向量模型官方价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=en&redirect=1)

DB 同时确认这 13 集总时长为 18,465,233 ms，即 **5.12923 音频小时**；当前 312 个 Chunk 的 window 长度总和等于原片段长度总和，没有实际重叠，不应把默认 overlap 配置当作本批费用成因。上述 30 次额外成功 ASR 均为同一 Chunk、相同媒体 SHA／接入点在不同 lease_version 下重新调用，估算约 0.70 元，确有浪费但不是全部消费的大头。按完整音频只转写一遍、无重试／额外精修计算，下面三个国内方案整批约为 Paraformer **1.48 元**、Qwen3 **4.06 元**、豆包录音 2.0 **4.10 元**；这是全量一次转写的对照预算，不能与当前尚未全部完成、且含重试的 Lite 估算直接相减作为实际节省。

| 候选 | 常规单价与服务地域 | 日语与时间戳 | 非实时文件输入限制／调用方式 |
|---|---|---|---|
| 阿里百炼 `paraformer-v2` | 华北 2（北京）：0.00008 元／秒，折合 **0.288 元／音频小时** | 支持日语；句级和字／词级毫秒时间戳 | ≤12 小时、≤2 GB；公网可访问音频 URL，HTTP 异步提交／查询 |
| 阿里百炼 `qwen3-asr-flash-filetrans` | 华北 2（北京）：0.00022 元／秒，折合 **0.792 元／音频小时** | 支持日语；默认句级，`enable_words=true` 开启字级，日语在字级支持名单内 | ≤12 小时、≤2 GB；DashScope 异步文件转写，不是同名实时或 Chat 接口 |
| 火山豆包录音文件识别模型 2.0，`volc.seedasr.auc` | 火山引擎国内语音服务：**0.8 元／音频小时** | 指定 `audio.language=ja-JP`；`show_utterances=true` 返回句／词级毫秒时间戳 | <5 小时、<512 MB；音频 URL，`/api/v3/auc/bigmodel/submit` 提交后查询 |

表格依据：百炼 [Paraformer 模型信息](https://help.aliyun.com/zh/model-studio/paraformer-v2)、[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[语言及音频规格](https://help.aliyun.com/zh/model-studio/asr-model)、[非实时语音识别／时间戳](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)；火山 [计费说明](https://docs.volcengine.com/docs/DoubaoVoice/Billinginstructions-21?lang=zh)、[录音文件标准版 API](https://docs.volcengine.com/docs/DoubaoVoice/LargemodelrecordingfilerecognitionstandardversionAPI?lang=zh)、[产品规格](https://docs.volcengine.com/docs/DoubaoVoice/ProductOverview-6?lang=zh)。核对日期：2026-10-14。

价格和版本须分开看：

- 百炼 `paraformer-8k-v2` 面向中文电话音频，不能替代本项目日语转写；`paraformer-v2` 才是此处候选。百炼官方将 Paraformer 定位为较早一代模型，因此低价只构成测试理由，不能直接证明动漫对白效果足够。[模型及语言](https://help.aliyun.com/zh/model-studio/asr-model)
- `qwen3-asr-flash` 的 OpenAI 兼容 Chat 接口不返回时间戳；本项目应比较 `filetrans`。更新的 `qwen-audio-3.1-asr-flash-filetrans` 已按输入／输出 Token 计费，不能套用上述每秒价格。[时间戳接口](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)、[价格](https://help.aliyun.com/zh/model-studio/model-pricing)
- 火山旧大模型标准版 2.3 元／小时、极速版 4.5 元／小时列在计费页的历史模型价格中；不要把它们当成 2.0 标准版价格。`volc.bigasr.auc_turbo` 是另一资源：同步返回，≤2 小时／100 MB，支持 `ja-JP`；不能仅因“极速”就套用 0.8 元单价。[计费说明](https://docs.volcengine.com/docs/DoubaoVoice/Billinginstructions-21?lang=zh)、[极速版 API](https://docs.volcengine.com/docs/DoubaoVoice/recording-file-recognition-lite-http?lang=zh)
- 免费额度单列：北京 `paraformer-v2` 当前为每月发放 10 小时、当月有效；`qwen3-asr-flash-filetrans` 为 10 小时、按开通／发布等规则起算 90 天有效。它们不代表常规调用免费；火山资源包也是预付费而非免费额度。[百炼额度及规则](https://help.aliyun.com/zh/model-studio/model-pricing)

接入边界与建议：

1. 先用相同的短样本比较 Paraformer、Qwen3 和豆包 2.0，覆盖日语人名、重叠对白、背景音乐、长静音及歌曲；检查漏字／幻觉、时间戳偏移和真实耗时。先验证便宜的 Paraformer 是否够用，再决定是否选择约 0.8 元／小时的两种方案；尚无本项目实测质量排名。
2. 这些专用 ASR 使用各自的语音 HTTP／SDK 协议和结构化转写结果。需要适配鉴权、提交／查询、错误状态及 Cue 时间戳转换，不能只把现有 Chat 模型 ID 改成 ASR 名称；火山语音服务的开通和密钥也应独立核对。
3. 异步供应商任务 ID 和已成功的转写结果应持久保存；重启后先查询／复用原任务，避免重新提交同一音频。结果仍按视频身份、音频范围和模型配置校验后交给理解阶段，和本项片段内成果复用一起设计。
4. 本轮不更换模型、不重跑视频、不调用付费 ASR；实际节省比例应以当前任务的 ASR 用量审计和音频时长为基准，不能把理解／Refine 的成本算进 ASR 对照。

另一个接入成本较低的云候选是 Groq `whisper-large-v3-turbo`：官方价 **0.04 美元／音频小时**，支持多语种、`verbose_json` 句段时间戳及 OpenAI Audio Transcriptions 协议；`whisper-large-v3` 为 0.111 美元／小时。可用账号和网络需实际核对，未将美元按假设汇率换算成人民币。[Groq 官方模型与价格／协议](https://console.groq.com/docs/speech-to-text)。本地 Whisper 可避免按调用支付 API 费用，但需要本地计算资源及 adapter／兼容服务，当前项目没有直接的本地 Whisper 执行入口。[Whisper 官方实现](https://github.com/openai/whisper)

**切换边界：** AniVAG 已有 `openai_audio` 适配器，可发送准备好的 16kHz 单声道 WAV 并读取 `verbose_json.segments`；兼容服务可复用这条通路。只有 `json.text` 的响应会退化为整个 Chunk 一个时间范围，不能当作等价的句段字幕。国内上述异步接口仍需适配。已有失败任务的 `retry_parse()` 明确保留 ASR 冻结配置，只刷新理解／Refine，因此修改 YAML 后按原按钮重试不会切换这批任务的 ASR；需要单独定义保留完成 Chunk、只对未完成部分更新 ASR 的恢复策略，并保留配置与成果来源记录。[现有音频适配器](../../gateway/transcription.py)、[时间戳规范化](../../engine/common/transcription.py)、[重试冻结配置](../../server/service/upload.py)

## Chat 联调问题（2026-09-21）

### C01：草稿引用未召回的 Evidence 导致回答失败

- 状态：故障原因已定位，校验和失败路径仍待改进。此前 Chat 日志中的 `Draft Item must reference retrieved Evidence` 来自 `AgentWorkspace.upsert_draft_item()`：Query Agent 提交的 `video_chunk_id` 不在本轮已召回的 Evidence 集合中。这里的 Evidence ID 是视频证据片段的 `video_chunk_id`，不是日志或请求的追踪 ID。
- 影响：该错误抛出 `AgentModelInvocationError`，本次 Assistant 消息被标记为 FAILED，页面显示“这次对话未能完成，请稍后重试。”；不是服务进程 panic。合法性校验必须保留，但目前未知 ID 没有像非法时间范围那样返回可供 Agent 修正的 Tool 反馈。
- 已做：`update_draft_tem` 已统一更名为 `update_draft_item`（代码、Query Skill 与测试，提交 `b41b1f5`）。工具调用现在记录 `Query`／`Evaluator`／`Chat` 角色、对话与消息 ID、工具名、调用 ID、参数摘要、结果摘要或错误、耗时；自由文本用长度与哈希代替原文。业务日志持续写入 `outputs/server/logs/development/api.log`，并按大小轮转。
- 待办：明确未知 Evidence ID 的可恢复 Tool 错误语义，给 Agent 返回可操作的纠正信息；验证失败后能继续检索或改用已召回 ID，同时不能放行未检索证据。`read_trieve_item`、`get_draft_catelog` 也存在拼写异常，目前仍是实际 Tool 名，尚未更名，改名时须同步 Tool、Skill 和回归测试。

依据：[video_search_canvas.py](../../agent/chat/video_search/video_search_canvas.py) 的 `upsert_draft_item`；[tools/](../../agent/chat/video_search/tools/) 的 Tool 定义与 [tool.py](../../agent/chat/common/tool.py) 的调用追踪；[agent_chat.py](../../server/service/agent_chat.py) 的失败收尾；[query/SKILL.md](../../agent/chat/video_search/skills/query/SKILL.md) 的工具清单。

### C02：多次“回答未完成”仍需逐条归因

- 状态：页面现象由用户提供的记录确认，具体失败并非同一种。原问题“爱音和灯是怎么认识的？谁是咕咕嘎嘎？”连续出现约 8 秒、29 秒、29 秒的失败，后来一次约 1 分 17 秒完成；追问“为什么没解释清楚？”也先经历约 8 秒失败，随后约 31 秒完成。仅靠页面通用文案和耗时，不能把这些失败全部归因于 C01。
- 同期日志线索：同一会话在 10:41 检索阶段约 8 秒、10:45 与 10:52 审核阶段各约 29 秒失败，错误类型均为 `GraphRecursionError`；16:22 另有检索阶段约 8 秒的 `AGENT_MODEL_ERROR`。17:09 还出现 `read_trieve_item` 游标不匹配，最终以 `PROCESSING_FAILED` 结束。页面摘录没有 `message_id`，这些日志尚未与摘录中的每次失败逐一关联。工具名拼写异常本身也不能据此判定为游标错误的根因。
- 待办：用 `conversation_id`／`message_id` 对齐页面失败与 `agent_execution_failed`、`agent_tool_failed`；分别排查模型错误、游标误用和 Evidence ID 校验。对游标错误检查 Agent 是否把某个证据的 `next_cursor` 用于另一证据，及 Tool 返回的纠错信息是否足够；复测失败后的继续发送。

依据：`outputs/server/logs/development/api.log` 中 2026-09-21 10:41、10:45、10:52、16:22 和 17:09 的失败记录；[tool.py](../../agent/chat/common/tool.py) 的 Tool 追踪；[agent_chat.py](../../server/service/agent_chat.py) 的公开错误映射。

### C03：“怎么认识的”被回答成后续组队过程

- 状态：回答质量缺陷，尚未完成根因定位。首个成功回答把第 1 集约 11:00–21:00 的追人、摔倒、创可贴、卡拉 OK 邀请当作“相识”的完整起点。用户继续追问后，系统才检索到第 1 集约 04:00 的校外偶遇、04:24 后的教室邻座再碰面、约 05:02 的自我介绍。按后续提供的片段，11:27 起是认识后的组队接近，首答遗漏了更早的接触。
- 同一问题还包含“谁是咕咕嘎嘎？”。首答只称在“第 1–3 集范围”未找到，并推测可能是别的译名或粉丝梗；用户选择的是 MyGO，不能凭这次未命中断言全系列没有，也需要核查为何回答自行收窄到第 1–3 集。该称呼所指目前仍未核实。
- 待办：复盘首轮 Query 检索词、候选排序、精读与 Evaluator 放行记录，确认 04:00 片段是未召回、被忽略还是在审核时被遗漏。对“初次／最早／怎么认识”类问题检查时间线完整性，对包含多个子问题的提问逐项核验；未找到时准确说明实际检索范围和限制。回归用此对话验证首答能区分“首次碰面、正式自我介绍、后来组队”。

依据：用户提供的本次对话页面记录；[query/SKILL.md](../../agent/chat/video_search/skills/query/SKILL.md) 与 [evaluator/SKILL.md](../../agent/chat/video_search/skills/evaluator/SKILL.md) 的检索及审核流程。04:00 等情节来自后续答案所展示的视频片段，仍需在回归时独立核验。

### C04：失败信息与下一轮 Context 的边界

- 状态：当前行为已确认。页面对 FAILED Assistant 展示 `lastError.message`，下一轮历史只读取 `COMPLETED` 消息，构造 Agent 输入时也不带 `lastError`；因此页面异常文案不会直接注入下一轮 Context。
- 注意：失败请求对应的 USER 消息仍为 `COMPLETED`，下一轮可能再次看到原问题。可恢复的 Tool 错误若作为结果返回，则会进入**当轮**模型上下文；这与页面上的失败提示不是同一条路径。

依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx) 的失败展示；[agent.py](../../server/repository/agent.py) 的 `list_recent_completed_messages`；[agent_chat.py](../../server/service/agent_chat.py) 的历史构造。

### C05：用户可见回答混入 Agent 流程描述

- 状态：页面摘录中的成功回答末尾出现“已按获批草稿整理完成”“三个视频块分别对应”等内部整理说明，首答还说“正好用满本次上限”。这些句子没有回答用户的剧情问题，也把草稿审批和视频块上限暴露到正文中；当前仅凭页面文本不能断定是哪个 Agent 阶段生成的。
- 待办：按消息 ID 对照 Chat Agent 输出、服务端正文组装和前端渲染，确认该内容的来源；让最终回答只保留用户需要的结论、依据和必要的不确定性说明，并用这段对话做回归。

依据：用户提供的本次对话页面记录；[chat/SKILL.md](../../agent/chat/video_search/skills/chat/SKILL.md) 的最终回答流程；[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx) 的消息展示。

### C06：Query Tool 将 topK 误用为整轮草稿数量上限

- 状态：已修复，待服务重启后整链复测。`topK` 只控制单次 `retrieve` 的候选数；Query 草稿不再受其限制，合法性、Tool 输出大小、精读范围和模型 Context 的保护仍保留。
- 已做：移除 Query Prompt、Runtime、输出绑定和草稿模型中的整轮条目上限；多次检索可累计选择超过单次 `topK` 的不同片段。
- 已做：草稿按稳定 `video_chunk_id` 去重；原样重复返回 `already_present`，内容变化返回 `updated`。`retrieve`、`update_draft_item`、`remove_draft_item` 向 Query Agent 返回当前 `draft_item_count`。
- 已做：Chat 只保存和发送用户显式覆盖的选项，默认值由服务端当前配置决定；页面将 `topK` 明确标为单次检索候选数。相关后端和前端回归测试已通过。

依据：[tools/](../../agent/chat/video_search/tools/) 的 `retrieve`／`update_draft_item`；[video_search_canvas.py](../../agent/chat/video_search/video_search_canvas.py) 的片段与草稿去重；[runtime.py](../../agent/chat/video_search/runtime.py) 和 [output.py](../../agent/chat/video_search/output.py) 的草稿绑定；[query.py](../../agent/chat/video_search/query.py) 的提示词。

## 联调与验收

这些项目属于待验证或部署准备，不等于对应主体代码未实现。

| 项目 | 当前证据／缺口 | 待完成工作 |
| --- | --- | --- |
| 真实整链联调 | 2026-09-20 本地 MySQL、Qdrant、API、Web 已启动，`/healthz` 为 ok 且 Parse Consumer running；尚未用新视频跑完整业务链 | 使用小视频验收上传→解析→两路向量→索引→聊天→定位播放，不能用存活检查替代整链验收 |
| MySQL 集成测试 | 最近全量 `841 passed, 8 skipped`；8 项均因未配置 `ANIVAG_TEST_DATABASE_URL` 跳过，本地开发库启动时 Schema 校验通过 | 使用独立测试库执行集成项，验证事务、锁和历史 Schema 迁移；禁止直接拿开发库跑破坏性测试 |
| 浏览器端到端 | 前端最近全量 53 passed；Playwright 有配置但没有用例，本轮截图未成功 | 补真实浏览器验收，不将 mock / DOM 通过描述成整链通过 |
| Parse 消耗 | 已完成本地开发库实测审计；约 23.7 分钟的成功样本需 74.52–97.20 分钟完整 Worker 时间，EP04 已知下界 99.55 分钟；完整计价样本约 ¥14.76–¥15.47／集，详见 B07 | 按 B07 建立分阶段低频指标，优先做重试止损、媒体复用和 Pro 调用降本；缺失价格继续明确显示未知或下界 |
| 已有数据库升级 | init-db 有旧 Schema guard，不自动 ALTER 历史表；本地当前 Schema 已通过启动校验，但未演练带旧 timing 明细表的历史库升级 | 停止 Worker 后执行受控迁移，并在历史数据副本上核对汇总结果、Incomplete 标记与旧表清理 |

## 当前能力边界与未纳入范围

- 媒体：当前用可确认兼容的原视频跳时播放；带原视频 atMs 的关键帧、独立短 Clip 和播放转码生产链未实现。Evidence 对未知关键帧明确返回 null / [] 与限制说明。
- Agent：模型异常或三轮审核未通过会明确失败，没有自动基础召回降级；执行恢复和跨实例 delta 回放未实现。B01 拟修复方案仅做过期失败收尾，不能描述成从上次 Turn 续跑。
- 取消／手动重跑／服务端 settings 写入：此前明确未纳入开发。上传“移除”只停止本地传输，不代表取消服务端任务。
- 历史 Parse 消耗：过去没有保存 usage 的已完成任务无法补算。新版 Worker 处理既有排队任务也能记录；旧任务重试只记录新调用，旧执行缺失时整体仍不完整；复用缓存不补造历史费用。

## TODO（待修复解决）

### C07：收集并治理 Tool／Context 链路的其他异常终止

- 状态：待盘点。当前已有统一 Tool 结果封装和部分可纠正错误反馈，但仍存在 Tool 次数、输出大小、草稿／证据校验、Context 预算、模型调用与阶段超时等可能结束整轮的路径。不能将所有中断一概视为应移除的限制；证据一致性和真实服务故障需要保留明确的失败语义。
- [ ] 逐项列出 Query、Evaluator、Chat 中从 Tool 调用或 Context 处理走到整轮 FAILED 的路径；对齐实际日志的 `message_id`、Tool 调用 ID、触发条件、错误类型和页面表现，标明哪些是 Agent 可修正输入、哪些是数据一致性保护或真实依赖故障。
- [ ] 对可修正的 Tool 参数、未知条目、读取范围、Catalog 游标和输出过大错误，统一返回有错误码、纠正提示及必要有效范围的受限结果，使 Agent 能在当轮缩小请求或重试；对必须终止的错误保留清晰的阶段、原因和日志，不用通用 Tool 错误掩盖根因。
- [ ] 按盘点结果逐项修复并补回归：合法的多次检索及重复片段可继续完成；超范围读取能缩小后重试；无效证据不能进入最终答案；Context 溢出、模型失败和超时各有可定位的终止原因。

依据：[tool.py](../../agent/chat/common/tool.py) 的 Tool 包装和 [shared.py](../../agent/chat/video_search/tools/shared.py) 的调用预算；[tool_output.py](../../agent/chat/common/tool_output.py) 的输出限制；[runtime.py](../../agent/chat/video_search/runtime.py) 与 [context.py](../../agent/chat/video_search/context.py) 的执行及 Context 预算；[agent_chat.py](../../server/service/agent_chat.py) 的失败收尾。

### B06：缺少可复用的结构化 Context Canvas 与受限 Patch 框架

优先级：中。类型：Agent 结构化状态编排重复建设。状态：待修复，当前暂缓实施。

#### 问题与背景

Chat Agent 已经维护一份结构化 Workspace／Canvas，并通过 Tool 逐步修改草稿、证据和时间范围。Parse 若要让 VideoUnderstandingAgent 校正 ASR 结果，也会出现同一种需求：ASR 先把原始证据写入每个 Chunk 的 Context，Understanding 只能修改被授权的特定 bullet，完整 Chunk 校验通过后再生成 Artifact、Checkpoint 和最终业务事实。

这不是某个 Prompt 的局部问题，而是一类重复出现的 Agent 基础设施：结构化上下文、字段级写权限、Tool Patch、校验、版本、差异记录、临时草稿与持久化快照。目前 Chat 与 Parse 各自手写类似机制，会重复实现状态模型、Tool 参数、路径白名单、错误处理和恢复语义；继续复制会让两套行为逐渐分叉。

当前 Parse Pipeline 已满足“生产 ASR 不单独写业务库，Understanding 完成后才通过 Server `ParsePersistence` 提交 Chunk Parse Artifact／Checkpoint，全部 Embedding 完成后再统一导入业务事实”。缺口不在落库时机，而在 Context 和 Agent 输出契约：Understanding 可以重新生成 Timeline Event 时间，却不能受控地修改或产出 canonical Transcript；原始 ASR 与理解后的事件时间仍可能并存且不一致。

#### 当前取舍

- 本轮只纠正 Parse Pipeline 的代码所有权，不为这一点同时重构 raw／canonical Transcript 数据契约。后者会波及 Prompt、Tool、Checkpoint 兼容、持久化、召回和历史数据语义，仍需单独设计。
- 当前 Chat Query／Evaluator 可以在召回后根据 Evidence 选择更准确的最终播放区间，能够覆盖主要用户路径；它只负责本次答案的范围选择，不会修复已经持久化的 ASR 或 Event 数据。
- 保留现有生产执行方式：ASR 跨 Chunk 有界并发，全部 ASR 完成后 Understanding 按 Context 顺序串行；不在本 TODO 中顺带改造成 Chunk 级流水线。
- 原始 ASR 证据不应被 Agent 原地覆盖。未来应将 raw evidence 与 Agent 生成的 canonical state 分开，最终持久化哪一层必须由显式契约决定。

#### 目标框架

- 定义通用 Canvas Document：由稳定 `block_id`、明确 block 类型、revision 和 metadata 组成；业务字段仍由各领域自己的 Schema 定义，框架不理解 Anime、Transcript 或 Chat 等领域含义。
- Tool 只能执行白名单操作，例如读取 block、替换指定 bullet、追加受控条目或提交草稿；每个 Agent 显式声明可修改的 block／字段，禁止任意对象写入和任意 JSON Path Patch。
- Raw Evidence block 只读；Canonical／Draft block 可按 Policy 修改。一次 Tool Patch 必须先在副本上完成结构校验和领域校验，再原子提交；失败不能留下部分修改。
- 每次修改记录 revision、调用方、目标 block 和结构化 diff，支持审计、乐观并发检查、重放测试以及 Artifact／Checkpoint 恢复。
- 区分运行期 Draft 与 Durable Snapshot。Agent 只操作当前 Draft；业务流程在明确提交点保存完整、已验证的 Snapshot，而不是让 Tool 直接写数据库。
- Chat 与 Parse 只实现薄的领域适配器，共用 Canvas、Patch、Policy、Validation 和 Revision 核心；不能把某一侧的 Workspace 类型直接泄漏到另一侧。

#### TODO

- [ ] 盘点 Chat Workspace／Tool 的现有更新动作，以及 Parse `Pipeline State`、`ContextSnapshot`、Chunk Artifact 的状态修改点，整理真正共有的最小操作集。
- [ ] 定义最小 Canvas／Block／Patch／Policy／Revision 协议，先写状态转换和非法 Patch 的纯单元测试，不先接模型。
- [ ] 明确 Parse 的 block：不可变 `raw_asr`、可修改 `canonical_transcript`、`timeline_events`、`context_before` 与 `context_after`；决定 raw ASR 是否只保留在 Artifact 中。
- [ ] 明确 ASR raw 时间字段契约。若采用 `start_time`／`end_time`，不得继续把它们伪装成已经规范化的毫秒；无效或未知时间不能导致整条可用文本被丢弃。
- [ ] 为 VideoUnderstandingAgent 提供受限 Tool，只允许修改 canonical block 的指定 bullet；完成后生成不可变 Parsed Chunk，再沿现有 PARSED Checkpoint 路径提交。
- [ ] 将 Transcript 字面召回的时间真相切换到 canonical Transcript，或明确改用已校正的 DIALOGUE Event；不能继续静默混用 Lite ASR 时间与 Understanding Event 时间。
- [ ] 选择一个边界清楚的 Chat 更新流程作为第二个接入方，验证共享的是框架而不是复制另一套业务模型。
- [ ] 补齐 Tool 越权、Schema 失败无部分写入、revision 冲突、Chunk 重试恢复、旧 Artifact／历史行兼容及 raw evidence 不可变回归测试。

#### 验收条件

- Agent 只能修改 Policy 明确授权的 bullet；越权字段、未知 block 和过期 revision 均被稳定拒绝。
- 同一次 Tool 调用要么提交完整合法的新 revision，要么不改变 Context，不存在半更新状态。
- Raw Evidence 始终可追溯且不可被覆盖；Canonical 数据的来源、版本和最终持久化时机明确。
- Chat 与 Parse 至少各有一个真实流程使用同一套 Canvas/Patch 核心，且没有复制两份路径校验、revision 或 diff 实现。
- Parse 失败重试能够从最后一个已提交的完整 Chunk Snapshot 恢复，不依赖进程内共享可变对象。

## Chat 联调问题（2026-10-11）

以下为本次新增记录；前面的条目保留各自日期下的历史状态，本轮未做全量复核。时间均为 `+0800`。

### C08：业务图继承 Supervisor checkpoint，导致视频工具执行前失败

优先级：高。状态：最小代码修复与离线回归通过，API 已加载修复，真实当前视频 Chat API 调用链已完成；片段证据仍不完整，见 C10。

#### 现象与根因

- 原故障时间：`2026-10-11 00:31:22`；Conversation 为 `e9e55375-55f4-48b8-8dd6-2566e2472ce3`，Assistant Message 为 `a7ff9c39-f93e-483f-8465-16a3d57842e6`。`understand_current_video` 执行 `7 ms` 后发生 TypeError，整轮 `996 ms`，最终包装为 `AgentModelInvocationError / AGENT_MODEL_ERROR`。API 与 Web 健康检查成功无法证明这条视频调用链成功。
- 当前 Supervisor 调用 `create_deep_agent()`，由 Deep Agents SDK 自动生成模型／工具循环的 graph；`supervisor.py` 没有另建 Supervisor `StateGraph`。两个专业业务 runtime 各自保留固定业务图，并通过普通工具调用；没有向 `create_deep_agent()` 注册业务 `subagents=`。
- 外层 Supervisor 使用 `InMemorySaver`。内部业务图原来使用默认 `builder.compile()`，会沿工具调用的运行上下文继承外层 checkpointer。单独运行这些业务图时没有父 saver，因此原有普通 runtime 测试没有暴露此问题。
- 实时理解把 `RealtimeRequest` 放入 graph 状态，其中的 `check_control` 是关联本次取消／超时控制的函数，不能被当前 msgpack 序列化器保存。视频检索状态中的 `VideoSearchContextCanvas` 同样无法直接序列化。`InMemorySaver` 仍需序列化状态，并非直接保留任意 Python 对象引用。
- 使用真实 Supervisor、真实 Deep Agents 与真实业务 runtime，只替换外部模型、host 和检索，分别复现 `TypeError: Type is not msgpack serializable: RealtimeRequest` 与 `TypeError: Type is not msgpack serializable: VideoSearchContextCanvas`。实时理解复现时 `prepare_context_calls=0`、`model_calls=1`，故障发生在业务上下文准备之前。

checkpoint 本身可用于业务图，但状态必须可序列化；需要恢复执行时，还要明确运行时依赖如何重建。当前两个 runtime 按单次工具任务设计，未实现从 LangGraph checkpoint 恢复这些临时对象的能力。关闭 checkpoint 保存不影响执行中的状态传递、Canvas 处理、取消／超时检查，也不关闭底层 Chunk 结果的业务持久化。未来如需中断续跑，应单独设计可保存状态与恢复语义，而非直接继承外层 saver。

#### 当前安装版本的配套错误

核对安装版本：`deepagents 0.7.23`、`langgraph 1.2.11`、`langgraph-checkpoint 4.2.0`、`langchain 1.4.4`。

仅将两个业务图改为 `compile(checkpointer=False)` 仍不足：外层 `durability="sync"` 会独立传播到没有 saver 的业务图。当前 LangGraph 仍等待未创建的 `_put_checkpoint_fut`，实时理解和视频检索分别复现 `AsyncPregelLoop`／`SyncPregelLoop` 缺少该属性的 AttributeError。这是当前安装版本在无 checkpointer 嵌套调用中的同步等待边界问题。

#### 已实施的最小修复

| 文件 | 调整 | 作用 |
| --- | --- | --- |
| [实时理解 runtime](../../agent/chat/realtime_understanding/runtime.py) | `builder.compile(checkpointer=False)` | 本次请求与回调不进入 Supervisor checkpoint |
| [视频检索 runtime](../../agent/chat/video_search/runtime.py) | `builder.compile(checkpointer=False)` | 本次 Canvas 与控制对象不进入 Supervisor checkpoint |
| [Deep Agents 适配器](../../gateway/agent_model/deepagents.py) | 外层 `graph.invoke(..., durability="async")` | 保留外层 saver，避免向无 saver 业务图传播同步 checkpoint 等待 |
| [Supervisor 测试](../../tests/agent/chat/test_supervisor.py) | 新增两个参数化测试，共 4 个用例 | 验证真实嵌套调用、会话确认与错误清理 |

保留视频检索的 Query → 检索 → Evaluator → Chat，以及实时理解的准备上下文 → 并行片段证据／画面观察 → 汇总。没有新增转发 LLM、迁移到 `CompiledSubAgent`、使用 LangGraph 私有配置键或升级依赖；DTO、VO、API、数据库 Schema、前端字段及 `gateway/bridge` 均未修改。

#### 离线回归

新增用例修复前得到 `2 failed`，分别复现两种 msgpack 错误；仅关闭两个业务图 checkpoint 后，再次得到 `2 failed`，分别复现上述同步等待错误；完整修复后相关回归合计 **256 passed**，均在 `-W error` 下执行：

```bash
.venv/bin/python -W error -m pytest -q \
  tests/agent/chat/test_realtime_understanding.py \
  tests/agent/chat/test_runtime.py \
  tests/agent/chat/test_tools.py \
  tests/gateway/test_deepagents.py \
  tests/gateway/test_agent_factory.py
# 219 passed

.venv/bin/python -W error -m pytest \
  tests/agent/chat/test_supervisor.py -q --tb=short
# 37 passed
```

新增 4 个用例使用真实 `SupervisorRuntime → DeepAgentsSupervisorModel → 业务 runtime`，只 fake 外部模型、检索和 host。覆盖实时 host 的 prepare／chunk／visual／compose、视频检索批准答案及证据、外层 checkpoint 存在、`confirm_turn` 保存最终答案、第二轮保留第一轮确认后的回答、pending 清空，以及取消／host 原始错误传播与失败状态清理。连续两轮的会话保留结论来自离线回归，本次真实模型只验证一轮。

#### 服务加载与真实模型验收

- 获得单独授权后，仅重启 API，PID 从 `42228` 变为 `80107`；`13:33:24` 完成启动，parse consumer 为 `running`，Web PID `41425` 保持运行。
- 获得指定视频数据与模型目的地授权后，使用真实 HTTP Chat API 创建回归会话，询问 mygo 视频 `90.123 s` 附近正在发生什么。冻结播放字段 `positionSeconds=90123` 沿用既有合同，其实际单位为毫秒。
- 回归 Conversation：`59bff08f-76a2-4244-b448-aef6a42ae91c`；Assistant Message：`5485fc08-1dfe-4085-857b-d33dfe1bce4c`；Tenant：`local-default`。
- `13:39:02` 工具启动，`13:39:21` 工具成功返回，`duration_ms=19467`；`13:39:35` Supervisor 与整轮执行完成，`duration_ms=40733`，回答为 `451` 字符。消息 API 确认为 `COMPLETED`、`lastError=null`，本轮未见 msgpack、`_put_checkpoint_fut` 或轮次确认错误／警告。
- 模型用量记录确认火山方舟 `doubao-seed-2-1-pro-260628` 的 `RealtimeVisualObservation` 和 `RealtimeContextSummary` 各调用一次；外层使用当前 DeepSeek／Ark 配置。上述实际业务调用与终态共同作为验收证据，不以 HTTP 200 代替链路成功。
- 工具结果为 `partial=true`，含义见 C10；不能据此宣称完整片段解析已恢复。当前实时理解结果不单独写入 `searchResult`，故本轮 `searchResult=null`、`evidence_hits=0` 不等于工具失败。
- 浏览器环境没有可用实例，本次未完成浏览器操作或 SSE 传输联调；真实视频检索完整链路也未在本轮验证。视频检索的嵌套调用、批准答案和证据由上述离线回归保护。

依据：[deepagents.py](../../gateway/agent_model/deepagents.py)、[supervisor.py](../../agent/chat/supervisor.py)、[RealtimeRequest](../../agent/chat/realtime_understanding/state.py)、[test_supervisor.py](../../tests/agent/chat/test_supervisor.py)；本机 `outputs/local-services/api.log` 与 `outputs/server/logs/development/api.log` 及轮转文件中对应 Conversation／Message 的记录；`GET /api/agent/messages/get` 与 `GET /api/usage/overview?tenantId=local-default&runId=5485fc08-1dfe-4085-857b-d33dfe1bce4c` 的只读核验。

### C09：真实 provider 的工具列表仍暴露默认 general-purpose task

优先级：待确认。状态：已观察到，独立问题，本次未修复；不阻断 C08 的这次真实 Chat 执行。

- 当前代码为 `anivag_supervisor` 注册禁用 general-purpose SubAgent 的 harness profile，wrapper 的 `model_name` 使用 `anivag_supervisor:<configured-model>`；真实适配器追踪仍报告实际 provider，例如 DeepSeek／Ark。profile 匹配路径是否符合预期仍待独立复核，不能直接把猜测写成根因。
- 本次真实回归的模型 operation／工具列表包含 `read_file,task,search_web,search_video_graph,understand_current_video`，而已有 fake-model 测试预期不包含 `task`。这里的 `task` 是 SDK 默认 general-purpose 工具，不是已注册的视频业务 SubAgent；工具列表包含它不代表本轮实际调用了它。
- 待办归属：`gateway/agent_model` 的 SDK harness 配置适配。先以真实 provider 适配器复核 profile 选择与最终绑定工具，再确定最小修复；保留当前两个业务工具和专业工作流，不借此迁移整个 SubAgent 架构。
- 验收：真实 DeepSeek／Ark 适配器与 fake-model 都不再暴露被明确禁用的默认 `task`，普通视频工具调用、会话 checkpoint 和错误传播仍通过。

依据：[deepagents.py](../../gateway/agent_model/deepagents.py) 的 harness profile 注册与模型包装、[test_deepagents.py](../../tests/gateway/test_deepagents.py) 的工具列表断言，以及 C08 真实 Message 的模型用量记录。

### C10：实时理解已返回，但相邻 Chunk 证据仍缺失

状态：已观察到，片段解析根因未独立定位，本次未修复。

- 既有 mygo Video 为 `e709a2d8-aabb-509e-b323-e7f9aa7018b2`，全量 Parse Task 为 `d470e300-9813-4c19-a62e-f52503e2e1ae`；联调前任务 API 已返回 `PARSING_FAILED`。
- C08 的实时工具成功返回 `partial=true`。回答明确说明 `0–60 s`、`60–120 s`、`120–180 s` 三个相邻片段解析失败，缺少完整前因与后续证据；本轮仍取得暂停画面观察并完成上下文汇总与最终回答。
- 本轮没有独立逐 Chunk 查询或复现其解析错误，不能仅凭整轮 `COMPLETED` 认定三个片段成功，也不能把既有 Parse 失败归为 C08 已修复的 checkpoint 问题。
- 待办归属：基础视频解析 runtime／Server 解析任务恢复链路。按上述 Parse Task 与目标 Chunk 核对现有失败记录及模型／媒体阶段，分别定位原因；后续验收应确认缺失片段恢复后的证据覆盖与来源。未调用全量解析重试／重置接口，未扩大本次代码修复范围。

依据：联调前 `GET /api/jobs/video-parse/list` 的任务状态、C08 的工具日志及真实回答；[realtime_understanding.py](../../server/service/realtime_understanding.py) 的既有 Chunk 读取与部分证据返回语义。既有全量解析成本／恢复问题仍按 B07 的历史记录分别处理，不套用其旧根因作为本轮结论。

### C11：API 仍加载旧 Gateway 校验器，读取新视觉配置时拒绝 Chat 请求

优先级：高。状态：重新加载 API 后已消除该配置错误，真实单轮 Chat API 复测完成；截图的具体播放位置与浏览器操作未验证。

#### 现象与根因

- 用户截图显示 `GatewayConfig: vision does not support adapter 'openai'`，请求编号为 `7509f2c9-cb20-45a6-b289-6ebc22f8215a`。对应日志为 `2026-10-11 14:13:38`，`POST /api/agent/messages/send` 返回 HTTP 400，耗时 `12.6 ms`，尚未进入 Supervisor 或视频工具。这与 C08 的工具状态序列化错误不同，不能把 C08 的成功验收概括成所有 Chat 请求均已恢复。
- API PID `80107` 于 `13:33:24` 完成启动；磁盘 `gateway/multimodal.py` 修改时间为 `13:56:01`，`gateway/config.py` 为 `13:57:26`，`config.yaml` 为 `14:05:27`。当前配置的 `models.vision` 已从 Ark 改为 `deepseek_api / deepseek-flash`，但运行中的 API 仍加载修改前的 Python 校验器。
- 旧校验规则只允许 vision 使用 `ArkModelSourceConfig` 或 `OpenAIVisionSourceConfig`，因此拒绝 DeepSeek 对应的 `OpenAIModelSourceConfig(adapter="openai")`，精确产生截图报错。当前工作区代码已经根据适配器能力校验，并支持 OpenAI／DeepSeek 的有序图片输入；使用当前源码执行 `load_config()` 和 `read_global_agent_settings()` 均通过。
- Chat 的 `AgentChatService._current_config()` 每次发送前调用配置 loader，沿 `read_global_agent_settings() → _read_document() → GatewayConfig.model_validate()` 校验整份 YAML。因此进程内旧代码可以读到磁盘新配置，使全局 vision 配置变化阻断 Chat；`/api/agent/messages/options` 和 `/api/agent/global-settings` 也会受影响。重启前只读请求全局设置接口再次复现同样的 HTTP 400。

#### 本次处理与服务加载

- 沿用已授权的 API 重启范围，只重新加载 AniVAG API；没有追加生产代码修改，也没有把视觉配置改回 Ark。C08 的两个 `compile(checkpointer=False)` 和外层 `durability="async"` 保留。
- 旧 API 收到 SIGTERM 后停止监听，但仍等待一个现有 HTTP 长连接关闭；SIGINT 后也未退出。核对 PID 与原启动签名后，仅对旧 API `80107` 使用 SIGKILL 完成退出，再启动 API；Web、MySQL 和其他进程未停止。
- 新 API PID 为 `11135`，`14:20:00` 完成启动；parse consumer 为 `running`，Web PID 仍为 `41425`。重启后 `/api/agent/global-settings` 与 `/api/agent/messages/options` 均返回 HTTP 200，全局配置 revision 与当前磁盘一致。接口恢复仅证明配置阻断解除，完整业务证据见下述复测。

#### 回归与实际验收范围

- 当前配置与适配器离线回归：`.venv/bin/python -W error -m pytest -q tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/gateway/test_multimodal_agent.py tests/gateway/test_deepagents_multimodal.py`，**145 passed**。
- 扩展回归：`.venv/bin/python -W error -m pytest -q tests/server/test_bootstrap.py tests/server/test_config.py tests/agent/chat/test_supervisor.py --tb=short`，**303 passed / 2 failed**。两项失败均发生在导入 Starlette TestClient 时，原因是已安装 AnyIO 的 `anyio.abc.BlockingPortal` 弃用警告被 `-W error` 提升为异常；它们属于扩展 Server 测试的依赖兼容问题，本次未升级依赖或修改测试来隐藏警告。
- 上述两项使用 `.venv/bin/python -m pytest -q tests/server/test_bootstrap.py -k test_app_lifespan_closes_only_services_it_owns --tb=short` 单独复跑，**2 passed / 1 warning**。不能据此声称扩展回归在 `-W error` 下全部通过。
- 真实模型复测沿用此前已授权的同一 mygo 视频与 `90.123 s` 位置。Conversation：`d717e944-8c34-4d8e-af5b-9ef7cb9f1325`；Assistant Message：`18eb1c89-b56e-4750-b0db-cf7f01ca8408`；Tenant：`local-default`。
- `14:20:45` 启动 `understand_current_video`，`14:21:10` 成功返回，`duration_ms=24559`；`14:21:24` 整轮完成，`duration_ms=45035`，回答 `593` 字符。消息只读 API 再次确认 `COMPLETED`、`lastError=null`。本轮未出现截图的 GatewayConfig 错误或 C08 的 checkpoint 错误。
- 用量记录确认 Ark `doubao-seed-2-1-pro-260628` 的 `RealtimeVisualObservation` 与 `RealtimeContextSummary` 各调用一次，外层使用 DeepSeek／Ark 配置内备用模型。既有 mygo 的实时 host 从 Parse Task 读取冻结的 `ParseConfig`，所以全局 vision 改为 DeepSeek 不代表已有任务的实时观察模型自动切换；本次没有验证新建 Parse Task 的 DeepSeek 视觉模型真实调用。
- 实时结果仍为 `partial=true`，不表示 C10 的基础片段失败已修复；模型工具列表仍含默认 `task`，C09 继续独立待处理。浏览器自动化没有可用实例，未验证浏览器／SSE，也没有对截图的 `10:27.52` 位置发起新的真实模型测试。

依据：[GatewayConfig](../../gateway/config.py)、[适配器能力](../../gateway/multimodal.py)、[全局设置读取](../../server/config/global_agent_settings.py)、[Chat 配置读取](../../server/service/agent_chat.py)、[实时 host](../../server/service/realtime_understanding.py)；本机 `outputs/local-services/api.log` 与 `outputs/server/logs/development/api.log` 中上述请求编号、Conversation／Message 与进程启动记录；消息与模型用量只读 API。

## Chat 联调问题（2026-10-14）

### C12：Supervisor 没有最终证据审核与时间范围决定权，内层答案被程序强制原样输出

优先级：高。类型：Agent 职责、证据供给与最终输出契约缺口。状态：已实施 Supervisor 最终提交修复，离线回归通过；真实模型选段质量待样本验收。

#### 现象与证据边界

- 用户预期 Supervisor 对最终回答、选择理由及每个视频片段的 `start/end` 负责；即使 Query 选段过宽或过窄，Supervisor 也应依据文字 Timeline、对白修正后再交付前端。
- 2026-10-13 的截图请求为 Conversation `d8226119-4624-45a8-b440-39c0de491f25`、Assistant Message `2d3b482e-b33d-42a5-a6f2-6262e71c945e`。首段显示 `04:00–05:00`，与该 Chunk 的 canonical 范围 `240000–300000 ms` 相同。不能仅凭完整 Chunk 范围断言它必然错误；需要核对支撑用户问题及最终主张的事件边界。
- 该请求日志中 Query 成功写入两项草稿，Evaluator 返回 `complete`，审核意见称“所有播放范围均位于 canonical 证据边界内”。工具日志没有记录 `write_context_canvas.operations` 内的实际时间值，不能据此复原模型每次提交、修改范围的完整过程。
- 前一轮只读复核时，本机 API 9621 和 MySQL 3306 均未监听，因此未重新取得这条消息的数据库原始 `renderBlocks`。不将两个 canonical Chunk 范围冒充两项已重新核实的最终播放范围。
- 此历史请求的 Query/Evaluator/Chat 经 DeepSeek 402 后实际使用 Ark 豆包 Lite；外层 Supervisor 使用 Astra。当前角色绑定已另行调整，该历史样本不能当作“内层 Astra 已完成本次审核”的证据，也不能仅以模型名称解释缺陷。

#### 修复前已确认的代码原因

1. **Supervisor 只拿到摘要。** `search_video_graph` 的模型可见返回仅包含结果数、复用标记、最多 1000 字的 `answer_preview` 和提示语，没有逐项 canonical 范围、Query 选段、完整 Timeline、对白或 Evaluator advice。宿主内存中有完整结果，不等于模型已收到或能按需读取它。
2. **Prompt 禁止最终修订。** Supervisor 当前被要求不得重写、否定内部 Query/Evaluator/Chat 的结论，并原文呈现内部批准答案。
3. **程序绕过 Supervisor 的视频回答。** 有 `video_result` 时，Supervisor Runtime 直接使用 `video_result.agent_reply.message`，只在特定分支追加实时解释或网页补充；Service 对不以前述答案开头的改写抛出 `Supervisor changed the approved video answer`。
4. **最终范围固定为 Query 的获批范围。** Query 新草稿必须填写 start/end；缺失会失败，没有自动补成整 Chunk 的逻辑。Evaluator 放行后冻结草稿，Chat 工具不能修改时间，`build_chat_output()` 将 Query 范围直接写入最终 `renderBlocks`。
5. **最终事件也来自内层答案。** Service 已将 `answer.evidence/block/delta` 缓存在 `pending_answer_events`，SQL 提交后发送；若只让 Supervisor 修改返回值而仍发送这些旧事件，SSE 会与数据库最终结果不一致。

前端已按 `renderBlocks.startSeconds/endSeconds` 显示时间、定位和控制播放；`hit.evidence.start/end` 则保留完整 canonical 证据范围。两者语义不同。历史命名的 `*_seconds` / `*Seconds` 媒体时间字段实际单位均为整数毫秒。

通用 Retrieval Skill 已要求完整覆盖支持结论的事件、动作或对白，并去掉明显无关的前后内容。因此不能把问题简单归结为“Prompt 从未要求精确选段”，也不能仅加强 Query 提示而保持 Supervisor 的上述限制。

#### 修复实现：数据供给、Tool、Prompt

| 层 | 已实施调整 | 边界 |
| --- | --- | --- |
| 数据供给 | 内层完成后向 Supervisor 交接本次真实召回的证据快照、原草稿与范围、Evaluator advice、内部 Chat 草稿及检索覆盖信息。工具首屏只给有界目录和读取入口；Timeline、对白、背景按需分页／分段读取。 | 复用已有 Canvas 和读取机制；不把完整证据塞入单次工具返回，不把内部交接对象塞进 HTTP VO 或数据库公开结果；同一 Chunk 的来源与版本保持一致。 |
| Tool | Supervisor 使用 `get_context_canvas` 按需读取文字证据，通过 `submit_video_item(video_chunk_id, start_seconds, end_seconds, claim)` 提交最终视频项；重复 ID 原位更新，`remove_video_item` 撤回。模型最后的非空正文与提交项统一组装为既有 `AgentFinalOutput/renderBlocks`。 | 源证据和内部审核快照只读；提交项按请求隔离。程序校验租户／Series／已提供证据 ID、整数毫秒和 canonical 范围，不限制为 Query 原先选定的范围。不新增 `replace_final_answer` 或前端协议。 |
| Prompt | 明确 Query/Evaluator/内部 Chat 提供证据与经过审核的草稿；Supervisor 对最终事实表述、片段选择和时间范围负责。要求按用户主张核对文字 Timeline、对白，既避免截断必要内容，也去掉明显无关内容。 | 移除 Supervisor 不得修订和必须原样输出的指令；不要求看完所有 section，不以整 Chunk 范围为程序拒绝条件，不增加自动循环审核层。 |
| 输出接入 | 解除保存层的原文前缀限制；从 Supervisor 最终结果统一生成保存的正文、选择理由、`renderBlocks`、SSE 和下一轮历史。 | 保留 SQL 提交后确认 harness 的顺序、10 分钟总预算和手动停止；不能静默退回内层旧答案并声称 Supervisor 已完成最终审核。 |

Supervisor 可以把 Query 过宽的范围收窄，也可以在已提供的 canonical 证据范围内修复过窄的范围；不能把允许的修改错误限制成 `Query.start <= final.start < final.end <= Query.end`。跨出已提供证据覆盖或引入未知 ID 仍应反馈错误，不能臆造支持。

最终 Markdown、选择理由和片段范围应由同一个最终草稿产出；不能同时保存 Supervisor 新正文与内部 Chat 旧视频块。无命中、全部候选不支持问题、网页与实时理解混合回答也要明确处理，不强制保留不支持答案的视频块。

日志已补齐 Canvas 读取的 collection/reference/section 和有界批量 references；Supervisor 的 `submit_video_item` 使用顶层 ID/start/end，现有工具日志可记录每次最终范围提交及成功／失败回执。Evaluator 结论继续沿用原有日志。工具调用回执不证明模型已理解证据，不记录隐藏推理；历史 Query 批量写入未记录的 operations 时间不能事后恢复。

#### 2026-10-14 实施记录

- 旧 `submit_video_item` 原属 Chat（`862c269`，2026-09-17）；`d71358b` 在 2026-09-20 改为 Workspace 的 update/remove，`2d5ce92` 在 2026-10-10 又合并到 Canvas 写操作。Supervisor 首次加入于 `5a30db0`（2026-09-22），当时没有最终视频提交工具。本次按用户指令给 Supervisor 补上显式提交能力，内层 Chat 保留独立调用能力。
- Runtime 成功后通过本次调用的 callback 交出 Canvas 与最后审核轮次，冻结 `review/latest`。覆盖信息合并本次所有 Query/Evaluator 检索，避免漏掉 Evaluator 补查带来的 partial、warnings 和已搜索视频范围。读工具沿用既有分页／字符范围预算；Supervisor 候选目录和详细证据使用同一固定快照。
- 内层 Chat 正文／视频块作为审阅草稿。服务层忽略这些草稿内容事件，从 Supervisor 最终结果统一生成正文、claim、播放范围和 SSE；只在 SQL 提交及现有 harness 确认后发布。没有最终视频提交时允许明确的纯文字答复，不自动补回内层视频。
- 保留既有租户／Series 隔离、canonical 边界、10 分钟总预算和停止能力；允许合法改写、范围扩大／缩小、撤回全部视频。最终模型正文为空时失败，不能拿程序追加的网页来源列表冒充完整回答。
- 离线验证覆盖真实 Runtime／Canvas／服务组装链，但使用本地模型替身；未调用真实 Astra 或其他模型，未重启服务、迁移数据库或改写历史消息。验证数量见本次交付记录；真实剧情与选段语义仍需真实样本验收。

#### 本次范围与相邻问题

- 本项先使用已有文字 Timeline；自选帧数的看图 Tool 暂不增加，抽帧、Redis 图片缓存和 Parse V2 关键帧持久化不作为本项修复前提。
- BM25 命中姓名对应的公开 `matchedEvidence/lexicalMatches` 已在前序修改中清空，前端列表已移除；内部检索证据仍保留。该展示调整不等于删掉 Timeline，也不会自动改善 Agent 选段或补齐缩略图。
- 空缩略图与缺关键帧提示属于独立媒体预览缺口；本项保持既有前端时间范围合同，不借此重写媒体服务或全站预览。
- 本轮未重跑真实模型、未重启服务、未迁移数据库，未改写历史消息。

#### 验收条件

- 构造 Query 整 Chunk 草稿，Supervisor 读取已有文字 Timeline 后提交更精确范围；验证其文本、claim、起止时间真正成为最终结果。
- 构造 Query 过窄草稿，Supervisor 在同一 canonical 范围内补回必要上下文；验证程序不会按旧 Query 边界拒绝。
- 目录分页及长 Timeline 分段可读，证据缺失和覆盖限制可见；未提供的 ID、跨租户／Series、越界、非整数或空范围被拒绝，合法改写不再被原文前缀限制拒绝。
- 最终 SQL、REST、SSE evidence/block/completed、harness 确认内容和下一轮引用保持同一版本；失败或取消时不发布内层草稿作为最终答案。
- 无命中、删除全部视频项、纯聊天、纯网页、实时理解以及混合回答保持正常；检索重复调用复用当前请求，不重跑模型来代替最终审核。
- 保留既有超时、停止、原始证据不可改写及幂等语义。离线测试保护数据合同与传递；真实样本的选段语义质量另行验收，不能以 fake-model 测试证明真实模型一定选段正确。

依据：[Supervisor](../../agent/chat/supervisor.py) 的 `search_video_graph`、Prompt 与最终内容选择；[Canvas 草稿与冻结](../../agent/chat/video_search/video_search_canvas.py)；[范围组装](../../agent/chat/video_search/output.py)；[通用检索要求](../../agent/chat/video_search/skills/retrieval/SKILL.md)；[Service 保存与 SSE](../../server/service/agent_chat.py)；[工具日志摘要](../../agent/chat/common/tool.py)；[SearchPage](../../web/src/features/search/SearchPage.tsx)；本机 `outputs/server/logs/development/api.log` 中该 Message 的 `agent_tool_started/finished`、`agent_turn_evaluated` 和完成记录。
