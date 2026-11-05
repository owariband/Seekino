# 长任务 MySQL 排序故障与 Consumer 恢复验收（2026-11-03）

[PR #30](https://github.com/owariband/Seekino/pull/30) 接续 [PR #18](https://github.com/owariband/Seekino/pull/18)，主设计为[任务查询与恢复 TRD](../../design/03-product-reassessment/parse-task-query-recovery-trd.md)。分支 `fix/parse-task-query-recovery` 已整合含 #18 的 dev `99e27b04`；首个需求提交仍为纯 TRD，没有新开重复 PR。

## 根因与修复

原任务详情的 prefix 辅助查询、任务列表主查询都把完整 VideoParseTask 携入排序，长任务的模型调用和计时 JSON 使 MySQL 8.4.13 在 256 KiB sort buffer 下报 1038。列表改为在相同租户和筛选范围内分页选择 ID，再无排序加载该页正文并还原顺序；prefix 只投影身份与对应 JSON 子对象，共享原有严格租约判断。

Consumer 的旧进程没有记录外层异常堆栈。13:39 在同一任务受控恢复时再次失败，新日志定位到 `claim_chunk_index → _parent_candidates`，错误同为 1038；[完整异常链](evidence/parse-task-query-recovery-20261103/consumer-failure.log)保留具体 SQL。该 SQL 虽只 SELECT id，但 JSON 筛选仍使 filesort 携带大字段：同库带 ORDER BY 3/3 失败，无 ORDER BY 3/3 成功。独立 1 MiB JSON 用例又复现 `claim_video` 的同类错误。

两处候选扫描改为无 SQL 排序地读取轻量标量，在 Python 保留原排序及轮转。原扫描本就全量读回候选 ID；本次没有扩大候选集合、放宽容量/租约/事务条件，也没有加无限重试。Consumer 外层异常现在写出堆栈，健康接口继续仅返回类型。

## 验证结果与边界

所有自动化 MySQL fixture 使用独立 `seekino_pr30_sort_20261103_test`，不连接课程成果库。测试会话固定 `sort_buffer_size=262144`，未改全局参数。[验证清单](evidence/parse-task-query-recovery-20261103/validation.json)记录测试集合、基线失败和进程代码标识。

| 验证 | 结果 | 解释 |
| --- | --- | --- |
| 1 MiB JSON，同一 MySQL 回归用例运行旧 Repository | 1038，失败 | 新测试能够检出原问题 |
| 大 JSON / Queue / 租约与真实 MySQL 并发 | 195 passed | 覆盖候选顺序、轮转、容量、事务、权限、分页、最新版本、过期与 JSON 类型 |
| 扩展提交/上传/权限/Summary 集合 | 331 passed | 从首次扩展集合中扣除 API 文件后的独立用例数 |
| API 文件，修正已有测试替身后 | 119 passed | 三处 SimpleNamespace 补 max_query_chars；未改 Chat 产品逻辑 |
| 合计去重 | 645 passed | 分组运行，非全仓库测试；保留一条已有依赖弃用警告 |
| 原课程库 Repository 详情 / 列表 | 各 5/5 成功 | [只读结果](evidence/parse-task-query-recovery-20261103/repository-validation.json) |
| 运行中的正式 HTTP 详情 / 列表 | 各 5/5 HTTP 200 | [HTTP 样本](evidence/parse-task-query-recovery-20261103/http-validation.json)；同期解析与回归有负载，不能当成提速对照 |

首次扩展集合是 346 passed / 104 failed，104 项都在同一旧测试替身初始化失败；已在修复前 Repository 上复现。补齐三处 `max_query_chars` 后 API 119 项全部通过，原失败结果保留在验证清单。没有前端改动，也没有运行前端回归。

## 现有任务与进程交接

目标始终是 v11 `93d60de9-b0ab-4aa6-aeea-342935e237b2`。原 MySQL 13361、ES 19231、两个原索引、媒体和模型配置都沿用 [#18 交接](pr18-scope-handoff-20261103.md)，没有 reparse、重新上传、建新解析版本或迁移 Schema。

- 已核对并正常停止旧 API PID 5212。首个诊断进程 19161 复现 Consumer 失败后也已正常停止。
- 当前 API / Consumer PID **19479**，端口 **19621**，cwd `/Users/hypnos/.codex/worktrees/parse-task-query-recovery/Seekino`；Consumer ID `pr30-query-recovery`。PID 是报告时点事实，后续操作须重新核验。
- 控制目录：`/Users/hypnos/workspace/agent/Seekino/outputs/verification/pr30-query-recovery-20261103/`。`control.py` 继承旧存储和模型环境，但明确替换 PYTHONPATH/cwd；`api.process.json` 记录代码基线、server diff 摘要与日志起点。原 worktree 和资料保留。
- 恢复前 117 份 Chunk 正文和版本、106 份 Refine、音频及冻结配置与 #18 清单完全一致。[恢复前](evidence/parse-task-query-recovery-20261103/before.json)与[13:46:48 快照](evidence/parse-task-query-recovery-20261103/after.json)保存可复核摘要。

| 持久事实 | 接手前 | 13:46:48 CST |
| --- | --- | --- |
| Consumer | failed / OperationalError | running，健康 ok |
| 理解正文 | 117 | 117，全部 ID 保留 |
| Refine 完成 | 106/115 | 109/115，原 106 份摘要不变 |
| 索引完成 | 96/117 | 99/117 |
| 此任务 ES Event | 1,528 | 1,602 |
| ASR 服务累计调用 | 3 | 3，新增 0 |
| ASR / diarization 正文与配置摘要 | 交接值 | 均相同；日志分别命中持久缓存 |
| 总任务 | PARSING v11 | PARSING v11 |

新增 Refine 合法更新了 3 份 Chunk，版本均递增；所有旧 Chunk ID 保留，没有内容变化而版本未变的记录。任务仍在处理剩余节点，此表不是完整课程成功报告。恢复后用量记录只有 index / refine，未发生新的音频调用。

**13:50:14 CST 续记：** [追加快照](evidence/parse-task-query-recovery-20261103/progress-135014.json)显示 Refine **112/115**、索引 **103/117**、ES Event **1,685**；Consumer 继续 running，总任务仍 PARSING v11。音频/冻结配置与原 106 份 Refine 摘要保持，全部旧 Chunk ID 保留，发生内容变化的 Chunk 均有递增版本。

## Review 范围与剩余项

本次后端故障的复现、最小修复、真实读取和持久推进已形成证据。PR #30 留在 Draft 供 review，尚未合入 dev；运行改动只加载在上述隔离 API。

没有需要先决定才能继续此修复的产品取舍。可重点 review：列表分页前后权限是否相同、候选排序是否保持优先级与公平轮转、恢复前后成果/版本是否满足护栏。

B13 中 `_video_parse_tracking` 仍会读完整 Chunk 成果；本次避免排序失败，没有交付全列表的轻量统计或延迟目标。该优化留在原问题记录，若后续实施应独立定界。完整课程终态、内容保真、性能与问答仍由原专项验收；本报告不把后台继续运行等同于它们已达标。
