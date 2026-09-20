# 项目问题清单

更新日期：2026-09-20。依据当前源码、运行日志和验证结果，区分已修复缺陷、功能缺失和待联调项。本文件同时记录问题根因、已实施方案与待处理方案，实际状态以每项标注为准，不扩展当前开发范围。

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
| server/agent/runtime.py、state.py | 接收剩余预算，保持 Harness 独立于数据库／HTTP |
| server/api/app.py、bootstrap.py、config/agent.py | 扫描任务的启动／关闭与配置装配 |

#### 验收条件

- 保存 PROCESSING 后模拟进程退出，截止后另一实例将其置为 FAILED，同一对话可以发送新消息。
- 未过期任务不被另一个实例或定期扫描误杀；同一 Conversation 仍只能有一个有效 Kick。
- 完成与过期扫描并发时只产生一个终态；旧执行不能覆盖失败状态或继续发布正文。
- 排队耗尽预算的任务不再开始新的模型调用；旧幂等 Key 不会偷偷重跑模型。
- 终态写库暂时失败后，数据库恢复时扫描能够完成失败收尾。

依据：[agent_chat.py](../../server/service/agent_chat.py) 的 start_message / _execute / close；[agent.py](../../server/repository/agent.py) 的 start_message / checkpoint_assistant_message / complete_assistant_message / fail_assistant_message；[runtime.py](../../server/agent/runtime.py)、[state.py](../../server/agent/state.py)。

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
- 增加点状 Cue 回归测试，明确 `TranscriptCue(start_ms=3000, end_ms=3000, ...)` 可被接收，同时原有反向 TimelineEvent 拒绝测试继续保留。

#### 验收结果

- Lite 点状 Cue 不再触发候选转写 Schema 错误和由此产生的重复解析失败。
- 没有为了通过校验而把点时间强行改写成 `[t, t+1ms)`，也没有把 Chat Submit 的绝对毫秒时间戳规则错误套到 Lite ASR。
- 全量测试与 B04 一并验证为 `841 passed, 8 skipped`；跳过项均为未配置隔离测试库的 MySQL 集成测试。

依据：[schemas.py](../../engine/parse/schemas.py) 的 `TranscriptCue` / `TimelineEvent`；[test_schemas.py](../../tests/engine/test_schemas.py) 的点状 Cue 回归测试。

### B04：ParseJob 逐 Attempt／逐 Stage 计时造成高频写库

优先级：高。类型：不必要的持久化复杂度与并发开销。状态：已修复（`c1ab11a`）。

#### 问题与根因

上一版计时设计新建了 `parse_job_attempt_timing` 和 `video_chunk_stage_timing`，并在 SPLIT、ASR、PARSE、TEXT_EMBEDDING、VISION_EMBEDDING、INDEX 等阶段分别开始和结束计时。每个 VideoChunk 的每个阶段都会产生额外事务，并行 ASR 还会争用同一 Attempt 计时记录；但当前产品实际只消费 ParseJob 的累计 Worker 处理耗时和该数值是否完整。

这套明细计时与 Lite Cue 的时间单位没有关系。Lite 的相对媒体定位不应为了 ParseJob 性能统计而满足毫秒级正区间；ParseJob 的 `processing_duration_ms` 则是 Worker 使用单调时钟测得的执行耗时，两者必须保持解耦。

#### 已实施方案

- **没有新建计时表。** 在现有 `series_parse_job` 上增加 `processing_duration_ms` 和 `processing_timing_complete` 两个字段。
- Consumer 使用单调时钟测量一次 Attempt 的执行耗时；`acknowledge` 在 current-lease 条件下原子累加成功 Attempt，`retry` 锁定 Job 后累加已结束的失败 Attempt。
- Lease 过期时无法可靠知道完整耗时，因此不猜测数值，只将 `processing_timing_complete` 永久置为 `false`；后续重试成功也不会把历史缺口误恢复为完整。
- 删除逐 Attempt／逐 Stage 的 Model、DTO、Repository、Service 和 `VideoProcessingService` 阶段计时包装，消除每个 Chunk、每个 Stage 的额外写库及锁竞争。
- API 继续返回累计处理耗时；任务未终态、Lease 过期或历史记录缺失时明确返回不完整语义，不把已知下界伪装成精确总耗时。

#### 受控升级

[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 是一次性迁移，执行前必须停止 Parse Worker；脚本会在仍有 `RUNNING` Job 时主动中止。

迁移直接给 `series_parse_job` 增加两个字段。若旧明细表已经部署，则仅在 Attempt 记录数量、序号 `1..N` 和完整性全部一致时汇总旧耗时；否则保守标记为不完整。若旧明细表从未部署，已经运行过的历史 Job 同样标记为不完整，不会误报成“0 ms 且完整”。完成折叠后删除两张旧明细表。

#### 取舍与验收结果

- 保留任务级累计处理耗时，放弃逐 Attempt、逐 Chunk、逐 Stage 的持久化诊断明细；这是有意的性能／可维护性取舍。
- 成功、失败重试、最终 Lease 过期以及“过期后再次执行仍保持 incomplete”均有回归覆盖。
- 全量测试结果为 `841 passed, 8 skipped`；本地 MySQL、Qdrant、API、Web 均已启动，`GET /healthz` 返回 `status=ok`，Parse Consumer 为 `running`。

依据：[job.py](../../server/model/job.py) 的 Job 字段；[consumer.py](../../server/mq/consumer.py) 的单调计时；[mysql_queue.py](../../server/mq/mysql_queue.py) 的原子累计、重试与 Lease 过期处理；[job.py](../../server/service/job.py) 的公开完整性语义；[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 的受控迁移。

## 联调与验收

这些项目属于待验证或部署准备，不等于对应主体代码未实现。

| 项目 | 当前证据／缺口 | 待完成工作 |
| --- | --- | --- |
| 真实整链联调 | 2026-09-20 本地 MySQL、Qdrant、API、Web 已启动，`/healthz` 为 ok 且 Parse Consumer running；尚未用新视频跑完整业务链 | 使用小视频验收上传→解析→两路向量→索引→聊天→定位播放，不能用存活检查替代整链验收 |
| MySQL 集成测试 | 最近全量 `841 passed, 8 skipped`；8 项均因未配置 `ANIVAG_TEST_DATABASE_URL` 跳过，本地开发库启动时 Schema 校验通过 | 使用独立测试库执行集成项，验证事务、锁和历史 Schema 迁移；禁止直接拿开发库跑破坏性测试 |
| 浏览器端到端 | 前端最近全量 53 passed；Playwright 有配置但没有用例，本轮截图未成功 | 补真实浏览器验收，不将 mock / DOM 通过描述成整链通过 |
| Parse 消耗 | 采集、价格快照、Decimal 人民币计算及 UI 已实现；相关最新前端测试 18 passed、typecheck 通过 | 新版 Worker 执行后核对真实 Provider usage、MySQL 记录、费用与页面；缺细分保持未知 |
| 已有数据库升级 | init-db 有旧 Schema guard，不自动 ALTER 历史表；本地当前 Schema 已通过启动校验，但未演练带旧 timing 明细表的历史库升级 | 停止 Worker 后执行受控迁移，并在历史数据副本上核对汇总结果、Incomplete 标记与旧表清理 |

## 当前能力边界与未纳入范围

- 媒体：当前用可确认兼容的原视频跳时播放；带原视频 atMs 的关键帧、独立短 Clip 和播放转码生产链未实现。Evidence 对未知关键帧明确返回 null / [] 与限制说明。
- Agent：模型异常或三轮审核未通过会明确失败，没有自动基础召回降级；执行恢复和跨实例 delta 回放未实现。B01 拟修复方案仅做过期失败收尾，不能描述成从上次 Turn 续跑。
- 取消／手动重跑／服务端 settings 写入：此前明确未纳入开发。上传“移除”只停止本地传输，不代表取消服务端任务。
- 历史 Parse 消耗：过去没有保存 usage 的已完成任务无法补算。新版 Worker 处理既有排队任务也能记录；旧任务重试只记录新调用，旧执行缺失时整体仍不完整；复用缓存不补造历史费用。
