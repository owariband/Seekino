# GPT 整课解析 API 与 ASR 中断恢复验收（2026-10-31）

## 问题与范围

此前 60 秒和 116 分 20 秒实验只验证了 Gateway 到 whisper.cpp 的转写调用，不能证明 VideoParseRuntimeService、MySQL 成果提交、API Retry 或重启恢复。用户要求运行 PR #18 worktree 的完整 API／Consumer，经解析接口实际中断和恢复。

本次代码目录 `/Users/hypnos/.codex/worktrees/full-video-asr/Seekino`，分支 `fix/video-asr-recovery`，HEAD `67223c16` 加既有未提交实现及本轮修复。使用真实 MySQL 8.4.13、Elasticsearch 8.19.22、同一 Metal Turbo 服务和本地 Qwen embedding；没有替换 Server Repository、Consumer 或 ASR。新建隔离测试库与索引，不迁移或改写旧开发库、旧 GPT v5 任务。

## 入口与输入

- API：`http://127.0.0.1:19621`，启动命令为上述 worktree 中的 `python -m server.api`；`/healthz` 确认 Consumer running。
- MySQL：`127.0.0.1:13361/seekino_video_asr_recovery_20261031_test`；ES：19231，独立 `_test` 索引。
- 测试素材目录：主工作区 `outputs/verification/video-asr-api-recovery-20261031/library/test/`。数据、运行控制和日志均保留，未作为缓存删除。
- 原片：`07 - Building GPT.mp4`，336,630,635 字节；内嵌标题、作者和来源确认为 Andrej Karpathy 的公开课程 `https://www.youtube.com/watch?v=kCc8FmEb1nY`。业务探测时长 6,979,873 ms，117 Chunk；容器时长约 116 分 20 秒。
- 经 `POST /api/series/create`、`POST /api/uploads/create`、41 次分片 PUT、`POST /api/uploads/complete` 创建任务，未直接向业务表插入任务或 ASR。
- 任务：`613f40e2-7bcb-4e38-9ae4-9ad1021afb51`；视频：`cd7c4da8-051c-5c6a-b95f-9fdaffefbeee`；上传：`fbfa1c9e-bd90-4ac0-87fa-1eaa6bc75b0f`。
- 冻结 `models.asr.scope=video`、`whisper_cpp_local/large-v3-turbo-q5_0`。WhisperX 未启用；句段／token 时间不代表声学强制对齐。

为缩短故障验收等待，专用服务租约为 30 秒、心跳 5 秒；节点重试仍沿用冻结的 30／900 秒和最多 2 次重试。Parse／Refine 并发仍为原配置的 10，未增加本地 ASR 模型实例。

首次 ASR 由正式 runtime 自行提取原片音轨并转写，执行时间记录为 184,107 ms。结果由当前合法租约 1 提交到 `video_parse_content`，共 2,476 段；没有导入之前独立实验的 JSON。

## 持久恢复结果

所有恢复均沿用同一个 task、同一 parse version、同一冻结 ASR 身份。实际转写次数由 whisper-server `operator(): processing` 请求日志计数，并与 Server 的 cache-hit 日志、数据库成果和租约快照交叉核对。计数基线排除了此前两次独立实验。

| 实际操作 | 观察到的行为 | 本任务累计转写 |
|---|---|---|
| 首次完整 ASR | MySQL 提交 ASR 正文、摘要、提交租约；随后通过 API 暂停 | 1 |
| Pause → Resume API | 新租约 4 读取既有成果，cache hit 1 | 1 |
| SIGINT → 启动新 API 进程 | 新租约 5 读取成果，cache hit 2；同时暴露取消清理竞态 | 1 |
| SIGKILL → 启动新 API 进程 | 真实进程被强制终止，租约回收后新租约 6 cache hit 3 | 1 |
| Understanding 503 → 正式失败 → Retry API | 10 个 Understanding 实际收到受控错误，任务 PARSING_FAILED；Retry 后新租约 8 cache hit 4 | 1 |
| 修复准备预算后，再 Retry／SIGINT／重启 | 新租约 10、11 分别 cache hit 5、6；再次进入 PARSING | 1 |
| 最终代码在已有 6 个 Chunk 时再 SIGKILL／重启 | 新租约 14、15 分别 cache hit 7、8；再次暂停为租约 16，6 个 Chunk 的版本及正文摘要不变 | 1 |

持久成果 ID 始终为 `5f9af428-31e5-49dc-9508-b83afc06025f`；正文规范摘要始终为 `1910697d9ea39ce5c671a3be13327ebda4518dd310c61152f1f9877adcf597c4`。12 个阶段快照核对同一行、同一摘要，恢复新增转写均为 **0**。ASR 对应的 `committed_lease_version` 仍为 1，后续执行租约增长不改写已完成成果。

下游故障通过专用 loopback TLS 代理返回明确的 503，再放行到原配置的 `https://api.kimi.com/coding/v1`。代理不生成模型结果，不改变成功输出；请求正文和凭据不写入代理日志。客户端只在本测试进程信任生成的测试证书。503 是受控注入，不冒充供应商真实故障。切换真实 Kimi 曾被自动审批拦截；核实唯一输入的公开来源及既有任务授权后，重新审批通过。

## 真正进程验收发现并修复的问题

1. **取消时清理仍在执行的生成器。** SIGINT 发生在 `prepare_episode().__enter__` 后台线程未返回时，原先的 finally 立即执行 `__exit__`，产生 `ValueError: generator already executing`，将正常取消误计为准备失败。协调器现在持有既有 `ExecutionCalls`，媒体资源清理延迟到阻塞调用实际结束；不创建新的执行框架或修改 ASR。
2. **旧准备预算污染下一次恢复。** 成功的准备未清零 `prepare_failures`；已有 Chunk 拓扑的任务 Retry 又只清 child 状态，不清该预算。本次连续中断使旧计数保留为 2，下一次过期租约回收后达到 3，任务立即失败。现在准备成功清零连续失败计数，用户显式 Retry 始终重置准备预算；自动接管仍保留有限失败保护，不允许坏输入无限重试。

保留了修改前的真实日志、失败任务快照及定向失败测试：取消竞态 1 failed，预算边界 2 failed；修复后相关 runtime／成果／队列／上传回归 **442 passed**。修复已加载进验收 API，正常停服不再报告生成器错误；最终新租约 11 完成媒体准备后，`prepare_failures=0`，回执属于租约 11。

## 全课程闭环边界

恢复后真实 Kimi 返回了可提交结果；截至 16:06:17，经正式暂停 API 收尾时，**6／117 个 Chunk 已提交 Understanding 成果**，115 个 Interval 尚未完成，索引尚未完成。暂停后状态为 `PARSING`＋`isPaused=true`，执行租约 12，准备失败计数为 0。ASR 成果仍未改变，新增转写仍为 0；这证明恢复后能够继续真实业务提交，不只恢复进程或状态字符串。

同一轮真实 Kimi 响应记录中有 **8 次 504**，耗时 313.9–355.9 秒，与先前注入的 503 分开记录。成功响应既有 234.0–343.5 秒的理解请求，也有 17.4 秒的其他业务请求；不能把全部 HTTP 200 都计为已提交 Chunk。完整课程解析、最终索引和检索回看追问未完成，仍不能宣称 P0 闭环通过。

**10-31 复核纠正：** 前一版把 `execution.max_model_calls=100` 写成“117 个 Chunk 必然超过而停止”的判断不成立。当前基线源码已不按累计次数拒绝请求，但仍把该字段作为必填配置校验、放进预约 DTO 和向部分 Agent 路径传参；此前据此推断完整课程不能执行，是把残留配置当成有效防御。按用户要求，本轮删除这些视频级残留，不以调大数值替代删除；保留调用审计、当前租约校验、取消、并发、节点重试及执行期限。旧任务不重建、不改写冻结模型，不需要重跑 ASR。

最终代码在已有 6 个 Chunk 的条件下再次经 SIGKILL／新进程恢复，ASR 累计仍为 1 次、cache hit 达 8 次；随后立即通过 API 暂停，6 个 Chunk 的版本／正文 SHA 均不变，健康检查通过。此次真实准备中断按既有有限失败合同计 1 次准备失败，未耗尽预算；先前“成功准备清零”与“显式 Retry 清零”均有独立证据。

收尾复测脚本曾额外断言“所有模型都没有新增调用”，该断言失败：恢复时独立 Refine 调度正常针对已完成 Chunk 启动了两次请求，累计预留从 43 到 45；ASR 没有新增调用。该过宽断言及原脚本保留，核验按本需求的 ASR 指标与既有成果不变执行，不把 Refine 的重新派发写成重复转写或隐藏成全模型零调用。最终任务为持久暂停，`activeExecutions=0`。

## 10-31 Kimi stream 与 504 复核、视频级限制清理

完整 `model-trace/*/events.jsonl` 的 44 条 Kimi 请求均明确含 `stream: false`；与 `gateway/openai/vision.py` 的直接 JSON 请求一致。测试代理只转发请求；其 `httpx.Client.stream(...)` 是传输读取方式，没有把请求体改成 `stream: true`。截至完整 trace 复核共有 **9 次**真实上游 nginx 504，客户端记录耗时 **312.043–355.989 秒**；前文 8 次为较早代理快照。另有 22 条受控 503 异常，不能计为供应商故障。

9 次 504 请求各有 189–198 张图片。6 次成功 Understanding 响应的输入为 232,862–243,793 tokens，且仍有 5,475–8,427 reasoning tokens。已确认的是上游在本地 600 秒超时之前返回了 504，不是本次 ASR 错误或此前的 DNS 异常。非流式长响应、输入规模、服务排队或推理耗时对网关超时的贡献尚未分离；没有供应商内部日志，也未做 `stream=true` 对照，不能宣称改成 SSE 即可修复。原始请求／异常链和不含正文的统计保存在 `kimi-stream-audit.json`。

视频级限制的配置、环境变量读取、预约 DTO 上限及 Runtime 传参已删除；历史字段只在兼容读取和旧上传完整请求哈希校验中识别。回归覆盖新配置无该字段、旧快照忽略该字段、118 次调用与重入仍可登记、旧租约不能请求、节点重试不变。历史上传幂等重放保留完整哈希比较，其他参数改变仍拒绝。

验证记录：首次组合为 1,042 passed／1 failed／10 skipped，失败来自本轮迁移测试错误地要求规范化前后 execution 字典完全相等，已修正；上传／迁移复验 174 passed／10 skipped。最终组合为 **1,043 passed／2 failed／10 skipped**，两项 Worker 失败分别为调用审计集合不齐、删除后媒体租约文件仍存在，随后原代码单独复验 **2 passed**。修改前独立副本的同范围组合为 1,039 passed／10 skipped，两个用例单独重复 5 次也通过，故不能把本轮两项失败直接归因于既有问题，也不能宣称已排除并发稳定性风险；原日志保留待定位。本次没有通过放宽这两项断言让它们通过。

修改已加载到 19621 测试 API（进程 31912）；`GET /api/uploads/parse-config` 不再包含视频级上限。旧任务仍持久暂停、租约 16，原 ASR 行及 SHA 不变，这次加载新增转写为 **0**；没有恢复 Kimi 派发。未 commit／push，完整课程闭环、流式对照与上述偶发回归根因仍未完成。

## 可复核证据

运行资料保留于主工作区 `outputs/verification/video-asr-api-recovery-20261031/`，关键快照同步至本 worktree 同名 `outputs/verification/` 目录（Git 忽略）。

- `control.py`、`verify_api.py`、`retest_budget_fix.py`、`*.process.json`：实际 API 调用、已核对进程命令、SIGINT／SIGKILL 与重启。
- `run.json`、`upload-completed.json`、`task-created.json`：原片、接口返回和冻结任务配置。
- `committed-asr.json`、`recovery-matrix.json`、各阶段 JSON：持久正文与不变身份、摘要、转写计数。
- `api.log`、`whisper-server.log`、`kimi-requests.jsonl`：准备／恢复／调用记录。`fixed-shutdown-expiry-failure-detail.json` 保留中途失败，不把整轮都写成第一次通过。
- `cancel-before.xml`、`budget-before.xml`、`recovery-fixes.xml`、`tested-source-sha256.json`：修改前后测试结果与代码指纹。
- `kimi-stream-audit.json`、`call-limit-reload-verification.json`、`parse-defaults-without-video-call-limit.json`：真实 stream／异常统计和加载后配置、成果验证。
- `remove-video-call-limit*.xml`、`call-limit-worker-recheck.xml`、`call-limit-baseline-*.xml`：本轮通过、失败与修改前对照；不把单独复验通过写成组合运行全绿。

API 仍在 19621 提供查询，Consumer 健康，测试任务通过持久暂停停止新增派发；专用服务控制记录在主工作区证据目录的 `*.process.json`。没有 commit／push；旧开发库仍未迁移。此次隔离实机恢复结果不能代替开发库迁移、人工转写质量或声学强制对齐验收。
