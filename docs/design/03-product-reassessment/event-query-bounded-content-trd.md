# 1. 问题与现状

需求：Event 查询在 ES 召回前全量加载 Chunk 内容，导致单用户搜索内存与素材库大小一起增长。

日期：2026-11-13（UTC+8）。工作分支：`perf/event-query-bounded-content`，基于 `dev`。PR：[#154](https://github.com/owariband/Seekino/pull/154)，目标分支 `dev`。实现与相关八模块隔离回归完成（248 passed、4 skipped）；合并、部署与真实环境效果待验；状态统一维护在[管理工作台](../../dev/workbench.html) `RAG-QUERY-MEM-01`，生成数据证据见[实施报告](../../report/server/service/video_event_retrieval/bounded-query-20261113.md)。

修改前，`VideoEventRetrievalService.search` 调用 `resolve_retrieval_scope`。后者通过 `validate_event_coverage` 加载所选任务的全部 Chunk 任务、正文和 detail，再逐块构造预期 Event 投影核对发布凭据；之后才执行 ES 有界召回与候选回源。`top_k` 不限制前置重数据读取。`ChunkParseContent.detail` 包含会被 ORM 二次解码的 `_index_embeddings_v2.representations`，索引缓存被一起实例化和深拷贝。检索范围越大，查询先创建的 ORM 实体、JSON 解码结果和临时对象越多，即使最终只命中少量片段。

可复核的代码依据为 `server/service/video_parse_read.py` 的范围装配、`server/repository/video_parse_retrieval.py` 的全实体查询，以及 `server/model/types.py` 的嵌套 JSON 解码。本分支的公开验证只使用生成的隔离测试数据，不包含运行环境、真实素材、聊天记录或生产测量。

本次授权是直接消除 ES 查询前全范围重内容校验。前置只读取权限、当前任务与已完成索引的必要标识/状态/版本，保留 ES 来源元组配对过滤；正文由候选回源读取，不载入索引缓存。INDEX 发布时的内容、租约、版本与 Event 集合校验继续保留。不改模型、排序、候选预算、API 响应或数据库 Schema，不清理真实数据，不部署或重启服务。

# 2. 影响因素、假设与评价指标

决定性因素是热路径读取了与命中数无关的正文和向量缓存，而不是 float 的小数位数或 GC。INDEX 完成提交已验证发布内容，查询可依赖当前 COMPLETE/INDEX 状态、indexed_content_version 和冻结目标；实际候选仍按 MySQL 正文验证。缓存 JSON 被删除或损坏不应使已完成且来源一致的命中变成不可检索；当前任务、正文版本或索引目标变化仍须使来源失效。

| 指标/护栏 | 修改前基线 | 修改后目标或本次证据 | 验证方法 |
| --- | --- | --- | --- |
| ES 查询前读取的 Chunk 正文 ORM 行数 | 与选中范围 Chunk 数相同 | 生成数据为 0；范围元数据为 12 条 | ORM load 事件与 SQL 列观测 |
| 搜索和候选回源读取 ChunkParseContent.detail | 包含完整向量缓存 | 检索路径的 ORM 完整 detail 不加载；候选与邻居仅投影 `detail->compact`、`detail->evidence.camera_hints`，不读 `_index_embeddings_v2`；`published_reference_sources` 因回执核验仍显式读取完整 detail | deferred/raiseload、SQL 投影列和 ORM 加载观测 |
| 查询前重建 Event 发布投影 | 每个 Chunk 一次 | 0；搜索入口不再调用 `validate_event_coverage` | SQL/ORM 观测与调用链检查 |
| 候选正文读取 | 已有 candidate_budget 控制 | ES 后 1 次批量正文查询；top_k/candidate_budget 1 和 5 分别加载并返回 1 / 5 个正文实体 | 固定 top_k/候选的集成测试 |
| 权限、当前任务、版本、代次、部分就绪 | 现有行为 | 保留元数据校验和召回后的真实内容校验；既有授权、子范围、逐视频代次用例保留 | Repository / Event / 公开课程回归 |
| 索引发布和恢复 | INDEX 提交验证凭据及内容 | 不放宽；写入事务仍调用 `_require_event_publication`，显式审计仍走 `validate_event_coverage` | 既有 INDEX Coordinator 回归 |
| 新实现整轮 RSS/延迟 | 待测 | 未测；不以单测冒充线上收益 | 生成数据的隔离观测；部署效果另行验证 |

范围元数据仍为 O(scoped Chunk count)，本次不声称所有 SQL 只读取最终 top_k 个 Chunk，也不新增固定候选数作为隐藏默认值。

# 3. 改造方案与建模

1. Repository 的检索任务/Chunk 范围投影返回冻结 Event 目标、Chunk ID、当前内容版本等标量事实，避免读取全部正文和 detail。
2. Event 范围装配直接使用最新 COMPLETE 任务、匹配的冻结目标以及 INDEX 完成且正文版本一致的来源标识。移除搜索入口对 `validate_event_coverage` 的调用及逐 Chunk 的预期投影重建；显式审计/发布检查不受本次移除影响。
3. 保持现有 `(parse_task_id, video_chunk_id, version)` 来源过滤和有限候选召回。发布未就绪、重解析任务、来源越权、版本不一致继续排除，公开课程仍可按视频提供部分就绪结果。
4. 候选和相邻上下文查询用 `defer(ChunkParseContent.detail, raiseload=True)` 延迟整个 detail 列，另以 SQL JSON 路径仅投影 `detail->compact` 与 `detail->evidence->camera_hints`，映射时只把这两个小值放进 DTO；`_index_embeddings_v2` 的缓存、receipt 和 representations 均不读取。单条证据的身份查询 `load_indexed_chunk_content` 仍完全不读 detail，实际证据回源走候选查询并保留上述小字段。`published_reference_sources` 需要核对编辑参考回执，仍显式传 `include_detail=True`；编辑参考的回执读取不在“检索回源不读完整 detail”的结论内。
5. INDEX 完成事务继续调用 `_require_event_publication`。不会把删除查询时的重复核验扩大成取消写入正确性检查；`validate_event_coverage` 仍保留给显式审计和需要逐块发布凭据的调用方，显式审计继续逐块读取正文/detail。

实现调整记录：

| 日期 | 原判断或方案 | 新证据 | 调整与影响 |
| --- | --- | --- | --- |
| 2026-11-13 | 初稿将查询范围简单描述为依赖“已完成 Event 发布” | 生成数据证明缓存缺失/伪造时仍按持久完成状态可检索，stale version、pending Chunk、wrong target 会排除对应视频 | 明确查询信任 INDEX 完成事实；逐块 receipt 重验留在写入与显式审计，不再由搜索入口承担 |
| 2026-11-13 | 初稿计划候选/证据读取不读 detail | `published_reference_sources` 必须读取回执才能核对编辑参考来源 | 检索回源与编辑参考回执分开表述，后者显式 `include_detail=True` |
| 2026-11-13 | 首轮实现把候选/证据读取描述为完全不读 detail | 证据 DTO 需要 `detail.compact` 与 `detail.evidence.camera_hints`，全部省略会使摘要和镜头提示缺失 | 完整 detail 继续延迟加载，以 SQL JSON 路径保留两个小字段；索引缓存仍不读 |

# 4. 对照实验与结果分析

本轮没有运行修改前旧实现做同口径 RSS/耗时对照；修改前行为由调用路径确认：搜索入口在 ES 前读取选中范围 Chunk 正文/detail 并逐块重建投影。以下结果来自隔离测试数据。

生成数据为 8 个视频 × 2 个 Chunk：E01–E06 各 2 个 Chunk 有匹配的 Event 发布，E07 为 Chunk-only 发布，E08 为 INDEXING。SQL/ORM 观测在 ES 调用前断言来源元数据为 12 条、正文实体为 0、完整 detail（含 `_index_embeddings_v2`）未加载；ES 调用后恰好 1 次批量正文查询。实现上该批查询不取完整 detail 或 `_index_embeddings_v2`，仅投影 `compact` 与 `evidence.camera_hints`；`load_indexed_chunk_content` 的身份查询仍不投影 detail。

| 观测 | top_k/candidate_budget=1 | top_k/candidate_budget=5 |
| --- | ---: | ---: |
| ES 前来源元数据 | 12 | 12 |
| ES 前正文 / 完整 detail | 0 / 0 | 0 / 0 |
| ES 后正文 SQL | 1 | 1 |
| 加载并返回的正文实体 | 1 | 5 |

行为回归同时覆盖：缓存缺失、回执被伪造或索引缓存被移除时，已完成发布仍可检索；正文版本不一致、Chunk 未完成或冻结目标不符时，该视频被排除并返回部分结果。既有公开课程授权、片段子范围和逐视频代次用例保留。

主 Agent 提供的修正前定向回归命令为 `python -m pytest tests/server/test_public_course_readiness.py tests/server/test_video_parse_repository.py -k 'bounded_candidate or completed_publication or incomplete_current or scope_reads_only' -q`，结果为 9 passed、140 deselected。该结果早于小字段保留修正。

# 5. 试点验证与方案取舍

本次不改变 ES 文档、配置或数据，先以生成数据的集成验证作为试点：同一搜索在未命中 Chunk 带有大 detail、缓存被移除/损坏、某视频重解析、当前版本不一致、不同视频代次不同等场景下，验证候选正确性与部分可用性。INDEX 完成发布检查保持严格。

真实环境后续固定素材范围、问题、历史与配置，比较读取行数、API RSS、整轮与检索阶段时间，以及最终片段/时间范围正确性。暂不进行服务切换或真实模型调用。

# 6. 结果验收与闭环

当前结论：实现与相关八模块隔离回归完成（248 passed、4 skipped），状态为 VERIFYING。已验证搜索入口在 ES 前只读取 12 条来源元数据，正文实体与完整 detail 均为 0；ES 后按候选预算批量读取 1 或 5 个正文实体；缓存缺失/伪造、版本不一致、未完成 Chunk 和目标不符的行为符合预期；INDEX 发布写入校验与显式审计保持原路径。小字段保留修正为完整 detail 延迟加载、仅 SQL 投影 `compact` / `evidence.camera_hints`，摘要和运镜提示在搜索及直接证据读取中的保留已验证。

待验事项：PR #154 合并、部署后的真实检索链路、RSS/延迟/token 采样，以及真实素材上的召回质量。范围元数据仍随授权范围 Chunk 数增长；本轮没有证明所有 SQL 都已按 top_k 收敛。原问题是否在真实环境改善，需以上述证据补齐后再决定验收。
