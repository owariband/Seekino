# PR #18 范围收口与现有任务交接（2026-11-03）

本次按明确的职责决定收口：#18 交付已实现并验证的音频成果持久恢复、Mac 转写／说话人分组、Parse Agent 工具与 Prompt 修复、直接 JSON Refine、错误反馈及相关请求控制。传统 Server / MySQL 查询与 Consumer 故障交给 [Draft PR #30](https://github.com/owariband/Seekino/pull/30)，不继续纳入 #18 实现。

## 决定与验收边界

- ASR 完成后暂停继续、下游失败、进程重启新增转写为 0，已有[真实接口与进程证据](video-asr-api-recovery-20261031.md)。该主体交付可以收口。
- 完整课程质量、整课耗时及检索问答仍未验收通过，转后续专项，不因 #18 收口改写为成功。课程 QA 已有 [PR #24](https://github.com/owariband/Seekino/pull/24)；独立 Apple 对齐已有搁置的 [PR #21](https://github.com/owariband/Seekino/pull/21)，本轮不恢复它或重复开项。
- Mac 当前正式链路是 GPU 转写与 pyannote 分组；独立 VAD 只有实验，独立声学强制对齐未接回该路线。此前“Mac 版 WhisperX”的概括不能代表完整能力已具备。
- 12:26 的范围交接没有执行合并、修改实现、服务重启或数据库迁移；随后收到合并授权，整合与验证见下方续记。旧验收计划与失败记录按日期保留，现行职责以本记录为准。

## 12:26 CST 持久成果核对

任务 `93d60de9-b0ab-4aa6-aeea-342935e237b2`，视频 `cd7c4da8-051c-5c6a-b95f-9fdaffefbeee`，parse version 11。原文及当前版本的摘要保存在[交接 manifest](evidence/pr18-handoff-20261103/continuation-manifest.json)。这是只读核对清单，不是数据库和媒体备份。

| 项目 | 当前事实 |
| --- | --- |
| Understanding 正文 | 117 份，保存内容版本与正文摘要 |
| Refine | 106/115 完成，保存 106 份成果摘要 |
| Index | 96/117 完成，ES 对应 96 Chunk、1,528 Event |
| ASR / diarization | 各一份已提交成果，规范摘要与持久回执完全一致 |
| 总任务 | PARSING，未出现完成时间 |
| Consumer | failed / OperationalError，当前不会自行继续 |

合并 Git 代码不执行业务 SQL，不删除 MySQL / ES / ArtifactStore，不重置任务进度。新分支恢复同一任务时必须沿用以下数据与冻结配置；不能通过重新上传、reparse 新版本或新建空库代替恢复。尚未提交的在途请求可能重做，已完成音频无需重算。

## 运行与存储交接

- MySQL：127.0.0.1:13361，`seekino_video_asr_recovery_20261031_test`。
- Elasticsearch：127.0.0.1:19231；`seekino_recovery_chunks_20261031_test`、`seekino_recovery_events_20261031_test`。
- ArtifactStore：`/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/library/test`，不在将要删除的 worktree 临时目录中。
- 当前 API：19621，控制记录 PID 5212，工作目录 `/Users/hypnos/.codex/worktrees/full-video-asr/Seekino`，Python `-m server.api`。后续操作前再次核对 PID 与控制者，不仅凭此历史号码操作。
- 控制与证据：主工作区 `outputs/verification/lets-build-gpt-deepseek-flash-v11-20261103/` 的 run.json、api.process.json、control.py、日志、用量与新交接 manifest。
- 不删除当前 worktree，直到服务与控制路径已经明确交接。切换代码不等于切换数据库；默认主开发配置不能替代上述隔离配置。

## 后端故障移交

任务详情查询首个 1038 为 12:01:48，原全行排序查询 5/5 失败、轻量字段查询 5/5 成功，sort buffer 为 256 KiB。另一条 prefix 查询 3/3 成功，Consumer 的具体停止异常尚未取得堆栈，不能仅凭 OperationalError 判为同源。PR #30 首个提交只含 TRD，先定位实际失败入口，再进行查询修复与同任务恢复。

12:26 时 dev 为 737f6d7b；当时无工作区变更的合并预演发现 docs/dev/devlog.md、docs/dev/problems.md、docs/report/README.md、gateway/streaming/chat.py 四处冲突。它们属于 #18 的 Git 收尾，并不把后端故障或性能优化重新纳入 #18。


## 11-03 合并授权后的整合与交接

已将 dev `737f6d7b` 整合到 #18。三处文档冲突保留双方记录；SSE 接收器同时保留 dev 的正文增量／计量及 #18 的首事件期限、取消唤醒和异常分类。两个后端定向集合共 **800 passed / 1 skipped**；唯一跳过项为缺少 `SEEKINO_TEST_DATABASE_URL` 的可选 MySQL Chat 计时检查。前端 typecheck、API / Chat 交付 **64 项通过**。原始结果及边界见[合并验证](evidence/pr18-handoff-20261103/merge-validation.json)。实际 GitHub 合并状态以 [PR #18](https://github.com/owariband/Seekino/pull/18) 为准。

本次整合不重启 API、不迁移数据库，也没有调用真实模型。当前进程仍加载启动时的代码；文件已经整合不代表进程已加载新实现。原任务和 Consumer 停止状态交给 #30 接手。

### 分支与数据库的关系

数据库并不属于 Git 分支。新 Agent 从合并 #18 后的最新 dev 建立或更新分支，并且保持 ORM / Schema 及数据合同兼容，就可以显式连接当前这套存储；单纯“从 dev 分支创建”不会复制或自动选择数据库。#30 当前设计分支基于合并前的 dev，实施前须同步已含 #18 的最新 dev。

恢复目标仍是 v11 `93d60de9-b0ab-4aa6-aeea-342935e237b2`。用现有成果继续，不通过重新上传、创建新解析版本或重建库替代恢复。模型冻结配置与向量编码／索引身份必须保持兼容。

| 配置入口 | 当前值 / 要求 |
| --- | --- |
| `SEEKINO_ENVIRONMENT` | `test` |
| `SEEKINO_DATABASE_URL` | 现有控制脚本中的 127.0.0.1:13361 / `seekino_video_asr_recovery_20261031_test`，不要使用默认主开发库 |
| `SEEKINO_ELASTICSEARCH_URL` | `http://127.0.0.1:19231` |
| `SEEKINO_ELASTICSEARCH_INDEX` | `seekino_recovery_chunks_20261031_test` |
| `SEEKINO_ELASTICSEARCH_EVENT_INDEX` | `seekino_recovery_events_20261031_test` |
| `SEEKINO_ARTIFACT_ROOT` | `/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/library`；应用会追加 `test`，不要再把配置设成 `library/test` |
| `SEEKINO_MODEL_CONFIG` | 当前 v11 控制目录中的 `model-config.yaml`；任务执行仍以原冻结配置为准 |
| `SEEKINO_MODEL_PRICING_CONFIG` | 同目录 `model-pricing.yaml` |
| `PYTHONPATH` / API `cwd` | 改为接手 Agent 自己的工作区；现有 `control.py` 仍引用 #18 旧工作区，不要直接照跑旧入口 |

同库读取可共享；本次开发交接只保留一个负责推进该任务的 API / Consumer。接手前先核对控制记录、真实 PID、端口与 Consumer 状态，再停旧进程、由新工作区恢复；不能因为 Consumer 已 failed 就忽略仍存活的旧 API。保留原数据库、ES、模型服务与媒体。普通自动化测试继续使用独立临时库，不能让初始化／清理 fixture 连接这套课程成果库。
