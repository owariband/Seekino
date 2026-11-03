# 1. 问题与现状

主题：长解析任务的大 JSON 查询与 Consumer 故障恢复。日期：2026-11-03。分支：`fix/parse-task-query-recovery`。PR：[Draft #30](https://github.com/owariband/Seekino/pull/30)。关联问题：B13（任务列表过量读取）、B10（执行恢复）。

本需求独立于 [PR #18](https://github.com/owariband/Seekino/pull/18) 的 ASR 持久恢复、解析 Agent、工具与 Prompt 修复。11-03 经范围复核，传统 Server / MySQL 查询与 Consumer 故障由本 PR 接手；性能、内容保真与产品 QA 不扩大为本次后端修复。依赖 #18 的成果模型与恢复能力；先合入 #18，再整合本分支并实施验证，目标分支为 dev。

真实试点是《Let's build GPT》116 分 20 秒、117 Chunk，v11 task `93d60de9-b0ab-4aa6-aeea-342935e237b2`。11-03 12:01:48 CST 开始，任务详情接口持续返回 MySQL 1038 `Out of sort memory`。当前系列活动状态辅助查询读取全部版本的完整任务行，再按 video / version / id 排序；EXPLAIN 有 Using temporary / Using filesort。v11 detail 存储 261,894 字节，session/global sort_buffer_size 均 262,144 字节。JSON 中主要是模型调用记录及执行计时，完整 ASR 仍独立保存。

同库只读复现：原完整行查询 5/5 报 1038；同筛选和排序下，身份及 prefix 轻量投影 5/5 成功、各返回 11 行。不能把 detail 与 buffer 大小简单比较当作所有执行计划的精确阈值。

12:20 健康接口进一步确认 parseConsumer failed / OperationalError，任务停在 117 理解、106/115 Refine、96/117 Index。现有 Consumer 只存异常对象、健康接口仅给类型，未取得停止时的完整堆栈；不能直接认定它与任务 GET 的 1038 同源。另一条 series prefix 全行查询只读重放 3/3 成功，此反证保留，后续先定位 Consumer 的实际失败入口。

期望：长任务仍可查询并可靠继续，已提交成果不会因合并代码、进程恢复或修复而丢失。当前任务详情不可读、Consumer 已停止，继续等待不构成恢复。

# 2. 影响因素、假设与评价指标

已确认：只需轻量状态的查询携带大 JSON，执行记录随阶段、重试增长。待确认：Consumer 的具体 SQL / 异常原因；停止原因需完整异常链支持。只增大数据库全局内存预算可能暂时绕过，不能说明字段读取设计已经改正。

| 指标 | 基线 | 验收目标 | 测量 |
| --- | --- | --- | --- |
| 大 detail 任务查询 | 原查询 5/5 报 1038 | 相同数据和 256 KiB sort buffer 下 5/5 成功 | 隔离 MySQL，详情及列表入口 |
| 业务状态一致性 | 当前 GET 不可读 | 最新版本选择、租户范围、prefix 租约判断保持 | 有多个版本／视频及失效租约的回归 |
| Consumer 故障 | failed，异常类型已知但堆栈缺失 | 定位具体失败入口；恢复后健康、工作推进 | 完整异常链、健康与持久节点状态 |
| 已提交成果保留 | 117 理解、106 Refine、96 Index；音频摘要已冻结 | 同 task/version 恢复；新增 ASR 和 diarization 0 次 | 交接 manifest、模型调用及版本核对 |
| 原请求语义 | 当前授权范围与提交租约有效 | 不放宽租约、权限、版本及有限重试 | 相关合同回归 |

# 3. 改造方案与建模

优先修复真正执行选择和排序的 Repository 查询，选择需要的轻量字段，或先选身份再按主键读取正文。原有 _has_active_prefix_lease 的判断语义保持，避免把 JSON 类型、null 或最新版本规则改掉。是否需要调整索引由同数据执行计划决定，不预设新增表或全局扩容。

Consumer 先补足实际错误证据，再决定最小修复位置；不把所有数据库错误吞掉后无限重试，也不据异常类猜根因。若它是独立问题，明确子原因、验证与范围，不修改模型 Prompt 来绕过。

预计涉及 server/repository/job.py、与真实故障对应的 Server 查询或 Consumer 入口、相关隔离测试及文档。首个提交只含本 TRD，尚未实施。

恢复沿用同一数据库、ArtifactStore、ES 索引、模型配置及 v11 task。禁止用 reparse 新版本、重新上传、重建库或删除历史成果代替继续。正常尚未提交的请求可以重新执行；已经提交的内容按现有版本合同读取，后续合法 Refine 更新不视为数据丢失。

# 4. 对照实验与结果分析

先冻结 12:26 交接 manifest，包含 task/version、冻结配置摘要、ASR/diarization 摘要、117 份 Chunk 内容版本与摘要、106 份 Interval 内容摘要、96 Chunk / 1,528 Event 的 ES 计数。原始文件位于主工作区 outputs/verification/lets-build-gpt-deepseek-flash-v11-20261103/pr18-continuation-manifest-20261103.json。它是核对清单，不是数据库／媒体备份。

| 对照 | 当前实测 | 修复后 |
| --- | --- | --- |
| 完整任务行排序 vs 轻量字段排序 | 5/5 失败 vs 5/5 成功 | 正式 API 待测 |
| 另一条 prefix 系列任务查询 | 3/3 成功 | 不是已确认故障，勿顺手改写 |
| Consumer 异常原因 | 仅 OperationalError 类型 | 完整入口及 SQL 待定位 |
| 恢复后的任务继续 | 尚未执行 | 不重做音频并推进剩余节点，待测 |

验证使用独立 MySQL 重现 256 KiB 配置，覆盖不止一条记录及比当前更大的 detail。禁止连接主开发／生产数据替代隔离验证。

# 5. 试点验证与方案取舍

代码检查通过后，先核对 API 的 PID、worktree 和控制目录，再按已有授权恢复隔离服务。不得因本 PR 合入就自动切换到主开发库。保持 ASR、分组及 DeepSeek 的冻结身份，不重新选型或做全课 A/B。

试点以同一 v11 的读取恢复、Consumer 健康、已提交成果保留和剩余节点推进验收。记录真实起止和未知用量，不将模型内容质量或课程总时长目标算作后端修复收益。停止条件为成果身份不符、旧租约提交、错误任务／存储环境或新的未解释失败。

# 6. 结果验收与闭环

当前只有设计、只读复现及交接证据，未修改实现、重启服务、运行迁移或恢复任务。B13 / B10 的这项故障保持 TODO。合并 #18 只改变代码，不移动 MySQL、ES 或媒体；它也不会自动使 failed Consumer 恢复。

本 PR 完成时需给出：原查询故障消失、Consumer 实际失败原因和修复、当前任务持久成果保留、正式读取及继续推进的证据。项目的内容保真、独立声学对齐、解析性能与检索问答仍由各自主设计和专项 PR 验收。
