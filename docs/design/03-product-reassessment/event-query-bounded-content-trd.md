# 1. 问题与现状

需求：Event 查询在 ES 召回前全量加载 Chunk 内容，导致单用户搜索内存与素材库大小一起增长。

日期：2026-11-13（UTC+8）。工作分支：`perf/event-query-bounded-content`，基于 `dev`。PR：创建后回填，目标分支 `dev`。当前进度与验证门槛统一维护在[管理工作台](../../dev/workbench.html)。

当前 `VideoEventRetrievalService.search` 调用 `resolve_retrieval_scope`。后者通过 `validate_event_coverage` 加载所选任务的全部 Chunk 任务、正文和 detail，再逐块构造预期 Event 投影核对发布凭据；之后才执行 ES 有界召回与候选回源。`top_k` 不限制前置重数据读取。`ChunkParseContent.detail` 包含会被 ORM 二次解码的 `_index_embeddings_v2.representations`，索引缓存被一起实例化和深拷贝。

可复核的代码依据为 `server/service/video_parse_read.py` 的范围装配、`server/repository/video_parse_retrieval.py` 的全实体查询，以及 `server/model/types.py` 的嵌套 JSON 解码。本分支的公开验证只使用生成的隔离测试数据，不包含运行环境、真实素材、聊天记录或生产测量。

本次授权是直接消除 ES 查询前全范围重内容校验。前置只读取权限、当前任务与已完成索引的必要标识/状态/版本，保留 ES 来源元组配对过滤；正文由候选回源读取，不载入索引缓存。INDEX 发布时的内容、租约、版本与 Event 集合校验继续保留。不改模型、排序、候选预算、API 响应或数据库 Schema，不清理真实数据，不部署或重启服务。

# 2. 影响因素、假设与评价指标

决定性因素是热路径读取了与命中数无关的正文和向量缓存，而不是 float 的小数位数或 GC。INDEX 完成提交已验证发布内容，查询可依赖当前 COMPLETE/INDEX 状态、indexed_content_version 和冻结目标；实际候选仍按 MySQL 正文验证。缓存 JSON 被删除或损坏不应使已完成且来源一致的命中变成不可检索；当前任务、正文版本或索引目标变化仍须使来源失效。

| 指标/护栏 | 基线 | 验收目标 | 验证方法 |
| --- | --- | --- | --- |
| ES 查询前读取的 Chunk 正文 ORM 行数 | 与选中范围 Chunk 数相同 | 0 | ORM load/SQL 观测及失败哨兵 |
| 搜索和候选回源读取 ChunkParseContent.detail | 包含完整向量缓存 | 不读取该字段 | deferred/raiseload 和 ORM 加载观测 |
| 查询前重建 Event 发布投影 | 每个 Chunk 一次 | 0 | 对内容校验调用设置失败哨兵 |
| 候选正文读取 | 已有 candidate_budget 控制 | 保持有界，只读取候选及显式相邻上下文 | 固定 top_k/候选的集成测试 |
| 权限、当前任务、版本、代次、部分就绪 | 现有行为 | 保持元数据校验和召回后的真实内容校验 | Repository / Event / 公开课程回归 |
| 索引发布和恢复 | INDEX 提交验证凭据及内容 | 不放宽 | 既有 INDEX Coordinator 回归 |
| 新实现整轮 RSS/延迟 | 待测 | 明确减少无关重数据读取；不以单测冒充线上收益 | 生成数据的隔离观测；部署效果另行验证 |

前置轻量元数据允许随授权范围增长；本次不声称所有 SQL 只读取最终 top_k 个 Chunk，也不新增固定候选数作为隐藏默认值。

# 3. 改造方案与建模

1. Repository 的检索任务/Chunk 范围投影返回冻结 Event 目标、Chunk ID、当前内容版本等标量事实，避免读取全部正文和 detail。
2. Event 范围装配直接使用 COMPLETE 任务、匹配的冻结目标以及 INDEX 完成且正文版本一致的来源标识。移除搜索入口对 `validate_event_coverage` 的调用及逐 Chunk 的预期投影重建；显式审计/发布检查不受本次移除影响。
3. 保持现有 `(parse_task_id, video_chunk_id, version)` 来源过滤和有限候选召回。发布未就绪、重解析任务、来源越权、版本不一致继续排除，公开课程仍可按视频提供部分就绪结果。
4. 候选和证据读取显式排除 ChunkParseContent.detail；复用既有身份核验、Timeline 回源核验与结果组织，不引入新的检索服务或包装层。
5. INDEX 完成事务继续调用 `_require_event_publication`。不会把删除查询时的重复核验扩大成取消写入正确性检查。

# 4. 对照实验与结果分析

初稿计划：先以隔离 SQLite、Fake ES 和当前现成 fixture 重现查询前内容加载；增加观测，明确区分范围元数据读取与候选正文读取。修改后确认搜索不会触发重校验、不会加载 detail，命中内容和授权过滤仍正确。再覆盖公开课程、证据读取和 INDEX 发布回归。

新实现的 RSS、延迟与真实 Chat 质量尚未测量。保存隔离测试的 SQL/ORM 证据及未验证项，不宣称已部署提速。

# 5. 试点验证与方案取舍

本次不改变 ES 文档、配置或数据，先以集成验证作为试点：同一搜索在未命中 Chunk 带有大 detail、缓存被移除/损坏、某视频重解析、当前版本不一致、不同视频代次不同等场景下，验证候选正确性与部分可用性。INDEX 完成发布检查保持严格。

真实环境后续固定素材范围、问题、历史与配置，比较读取行数、API RSS、整轮与检索阶段时间，以及最终片段/时间范围正确性。暂不进行服务切换或真实模型调用，未获得真实效果前仅交付可审阅修复。

# 6. 结果验收与闭环

初稿：实现、回归、隔离观测、PR 及真实环境效果均待完成。验收逐项回到第 2 节，记录测试命令、数量、结果和证据入口；在同一 TRD 更新最终改动范围及未验证边界。

采用条件：前置正文/缓存读取和投影重建归零，候选读取有界，权限/版本/发布写入护栏通过。交付代码不等于线上部署或长期效果验收。
