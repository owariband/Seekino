# 项目问题清单

更新日期：2026-09-15。依据当前源码和本轮审查，区分确定缺陷、功能缺失和待联调项。本文件记录问题及拟修复方案，不表示方案已经实现，也不扩展当前开发范围。

## frontend

### F01：缺少新建聊天和历史对话切换

- 类型：功能缺失。
- 现状：SearchPage 调用 `useAgentConversation()`，自动创建或恢复浏览器保存的当前 Workspace 对话。后端支持多条 Conversation，客户端也有列表方法，但页面没有新建聊天、历史列表和切换入口。
- 影响：用户只能使用当前对话，不能通过 UI 管理多条聊天。
- 修复方向：消费现有 conversations/create、list、get；由页面传入选中的 conversationId，复用 Hook 已有的切换、订阅清理与旧回调隔离。
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

## 联调与验收

这些项目属于待验证或部署准备，不等于对应主体代码未实现。

| 项目 | 当前证据／缺口 | 待完成工作 |
| --- | --- | --- |
| 真实整链联调 | 当前检查的 MySQL、Qdrant、API、Web 服务端口未监听，连接配置未补齐 | 在隔离环境完成配置／Schema，使用小视频验收上传→解析→两路向量→索引→聊天→定位播放 |
| MySQL 集成测试 | 最近后端全量 446 passed，2 项因未配置 ANIVAG_TEST_DATABASE_URL 跳过 | 用隔离测试库执行已有集成项，验证事务、锁与迁移 |
| 浏览器端到端 | 前端最近全量 53 passed；Playwright 有配置但没有用例，本轮截图未成功 | 补真实浏览器验收，不将 mock / DOM 通过描述成整链通过 |
| Parse 消耗 | 采集、价格快照、Decimal 人民币计算及 UI 已实现；相关最新前端测试 18 passed、typecheck 通过 | 新版 Worker 执行后核对真实 Provider usage、MySQL 记录、费用与页面；缺细分保持未知 |
| 已有数据库升级 | init-db 有旧 Schema guard，不自动 ALTER 历史表 | 按已记录的受控迁移处理旧结构；本轮未执行真实 DDL |

## 当前能力边界与未纳入范围

- 媒体：当前用可确认兼容的原视频跳时播放；带原视频 atMs 的关键帧、独立短 Clip 和播放转码生产链未实现。Evidence 对未知关键帧明确返回 null / [] 与限制说明。
- Agent：模型异常或三轮审核未通过会明确失败，没有自动基础召回降级；执行恢复和跨实例 delta 回放未实现。B01 拟修复方案仅做过期失败收尾，不能描述成从上次 Turn 续跑。
- 取消／手动重跑／服务端 settings 写入：此前明确未纳入开发。上传“移除”只停止本地传输，不代表取消服务端任务。
- 历史 Parse 消耗：过去没有保存 usage 的已完成任务无法补算。新版 Worker 处理既有排队任务也能记录；旧任务重试只记录新调用，旧执行缺失时整体仍不完整；复用缓存不补造历史费用。
