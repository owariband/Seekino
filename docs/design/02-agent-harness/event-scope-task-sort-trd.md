# 1. 问题与现状

需求：Event 检索任务元数据的 JSON 投影参与 MySQL filesort，导致完整课程范围不可检索。

分支：`fix/event-scope-task-sort`；首个提交 `fad02e7b`（TRD），实现提交 `2bebd9af`；PR：[#159](https://github.com/owariband/Seekino/pull/159)。依赖 [检索内存修复 PR #154](https://github.com/owariband/Seekino/pull/154)，本次只修复其生产验收中暴露的任务列表查询。

2026-11-14，检索内存热修复上线后，真实游客 Chat 成功完成；进一步对八部课程的完整 Event 查询范围做只读验证时，`list_video_tasks` 报 MySQL 1038：`Out of sort memory`。查询同时选择冻结的 `parse_config` 与 `detail._event_index_target_v1`，再按视频、版本、任务 ID 排序。当前环境下携带 JSON 的排序失败；不能以单次 Chat 成功代替完整范围检索可用。

预期：完整范围仍能识别最新任务及发布版本；无需提高 MySQL 全局排序内存，不加载 Chunk 正文或索引向量缓存。边界限于 Server Repository，不改变 Chat、授权、模型、任务完成及索引合同。

# 2. 影响因素、假设与评价指标

假设：排序阶段携带 JSON 投影触发 MySQL 排序缓冲限制；先排序轻量任务 ID，再无排序读取这些任务的元数据，可以移除该放大因素。通过同一生产范围的修改前后只读探针验证；失败则继续定位，不调整全局数据库内存。

| 指标 | 基线 | 目标 | 方法 |
| --- | --- | --- | --- |
| 八部课程 Event 范围解析 | MySQL 1038 | 成功取得 880 个当前 Chunk 身份 | 同数据只读调用 `resolve_retrieval_scope` |
| 最新任务顺序与删除过滤 | 既有规则 | 保持视频升序、版本及 ID 降序，排除删除任务 | Repository 回归测试 |
| 热数据加载 | PR #154 约束 | 范围阶段不读 Chunk 正文/detail；5 个候选不读向量缓存 | SQL 捕获与 ORM load 监听 |
| 延迟、内存增量 | 待测，原查询失败 | 有界且无 GiB 级对象加载 | 记录探针耗时与 RSS；不等同完整 Chat |

# 3. 改造方案与建模

在同一 Session 中先查询和排序任务 ID；随后按这些 ID 投影所需元数据，不再给含 JSON 的查询添加 `ORDER BY`；按第一步 ID 顺序构建原 DTO。额外增加一次数据库往返，换取排序不携带 JSON。空输入或无任务及时返回，维持既有删除和版本语义。

不增加配置、依赖、Schema、全局缓存或索引重建。部署沿用当前生产镜像，仅更新该 Repository 文件；保留上一热修复镜像回滚。

# 4. 对照实验与结果分析

回归：公开课程就绪、检索服务与 Event 检索三个模块 `61 passed, 1 skipped`。新增用例覆盖任务顺序、最新失败任务保留、删除过滤、空范围及 JSON 查询不携带排序。

候选代码在生产容器的独立只读进程验证，不修改服务进程：同一八部课程取得 880 个身份，范围阶段 5 次查询、0 个 Chunk 正文 ORM 实体；一次暖缓存测量 37.73 ms、RSS 增加 0.50 MiB。随后回源 5 个候选，完整 detail 加载 0，累计 RSS 增加 4.89 MiB。真实 ES 词法 Event 召回返回 8 个 Event、5 个 Chunk、覆盖 8 个视频，`partial=false`，862.42 ms、进程 RSS 增加 28.50 MiB。同一探针早前冷启动为 320.83 ms、RSS 增加 0.38 MiB，冷热缓存差异不作为优化收益。该探针不包含模型生成或 embedding，不作为端到端 Chat 内存指标。

# 5. 试点验证与方案取舍

在用户已授权的生产 OOM 热修复范围内，仅重建 API。最终镜像 `seekino-api:hotfix-event-memory-2bebd9af` 已于 2026-11-14 00:50:20（UTC+8）部署，健康检查通过；相对第一个热修复镜像仅替换 `repository/video_parse_retrieval.py`，旧镜像保留回滚。

最终运行代码的只读复测取得 880 个身份，范围阶段 37.22 ms / RSS +0.50 MiB / 正文 ORM 0；5 个候选回源累计 RSS +4.95 MiB / 完整 detail 0；真实 ES 词法 Event 召回 8 Event / 5 Chunk / 8 视频，612.03 ms / RSS +28.60 MiB，`partial=false`。同一个 API Python 进程连续两次游客 Chat 都 HTTP 200 / COMPLETED，请求及后续 15 秒窗口最大 RSS 增量分别 86.57 / 40.36 MiB，第二次结束后 15 秒 RSS 419.92 MiB，swap 与 OOM 增量均为 0。保留旧镜像回滚，多用户长时稳定性仍待观察。

# 6. 结果验收与闭环

实现、61 项回归、最终 API 镜像部署、完整范围与真实 ES 召回、连续两次游客 Chat 均已完成。采用两阶段任务元数据查询，完整八课程范围的 1038 错误在同数据复测中消失，未提高数据库排序缓冲，排序与删除语义保持。多用户并发、长时间内存曲线及旧镜像与新镜像的整轮 Chat 对照尚未验证。部署记录与证据入口见[报告](../../report/server/service/video_event_retrieval/cloud-memory-hotfix-20261114.md)，当前状态见[工作台](../../dev/workbench.html) `RAG-QUERY-MEM-01`。
