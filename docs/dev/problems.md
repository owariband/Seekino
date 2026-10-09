# 项目问题清单

更新日期：2026-09-23。依据当前源码、运行日志和验证结果，区分已修复缺陷、功能缺失和待联调项。本文件同时记录问题根因、已实施方案与待处理方案，实际状态以每项标注为准，不扩展当前开发范围。

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

优先级：高。类型：解析性能、单位经济性与失败止损问题。状态：已由本地开发库和运行日志实测确认，待优化。

#### 实测数据与统计口径

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
