# Event 查询有界内容读取：实现与生成数据验证

日期：2026-11-13（Asia/Shanghai）。分支 `perf/event-query-bounded-content`，[PR #154](https://github.com/owariband/Seekino/pull/154)，目标 `dev`。设计见 [TRD](../../../../design/03-product-reassessment/event-query-bounded-content-trd.md)，当前状态见[工作台](../../../../dev/workbench.html) `RAG-QUERY-MEM-01`。

本报告只依据当前分支的公开源码和仓库内生成的隔离测试数据，不包含真实素材、运行环境或性能采样；实现与测试由主 Agent 负责，测试结论不代替整轮回归、部署或线上效果验收。

## 问题与改动

修改前，`VideoEventRetrievalService.search` 通过 `resolve_retrieval_scope` 调用 `validate_event_coverage`，读取选中范围全部 Chunk 的正文和 `ChunkParseContent.detail`，逐块重建 Event 投影核对发布凭据，之后才执行 ES 有界召回。`top_k` 只限制返回片段数，不限制这段前置读取；`detail` 中的 `_index_embeddings_v2.representations` 还会被一起实例化和深拷贝。

本次改动：

- `server/repository/video_parse_retrieval.py`：任务投影返回冻结 Event target，Chunk 投影返回 `video_chunk_id` 与 `content_version`；候选查询以 `defer(ChunkParseContent.detail, raiseload=True)` 延迟完整 detail，并以 SQL JSON 路径投影 `detail->compact` 与 `detail->evidence->camera_hints`。
- `server/service/video_parse_read.py`：`resolve_retrieval_scope` 只使用最新 COMPLETE 任务、匹配的冻结 target、INDEX 完成且正文版本一致的 Chunk 元数据；移除搜索入口对 `validate_event_coverage` 的调用。
- 候选回源、相邻上下文和单条证据读取均不加载完整 `detail`；候选与邻居只把上述两个小字段映射进 DTO，供 `_video_parse_evidence` 生成摘要与镜头提示，`content_data(include_detail=False)` 不再映射索引缓存，也不读取 `_index_embeddings_v2`。`load_indexed_chunk_content` 的单条证据身份查询仍不投影 detail，实际证据回源走候选查询并保留小字段。
- `published_reference_sources` 需要读取编辑参考回执，仍显式 `include_detail=True`，不在“检索回源不读完整 detail”的范围内。
- INDEX 发布事务继续调用 `_require_event_publication`；显式审计仍使用 `validate_event_coverage`，并继续逐块读取正文/完整 detail。没有改模型、排序、候选预算、API 响应或数据库 Schema。

## 生成数据与断言

测试 fixture 为 8 个生成视频，每个视频 2 个 Chunk。E01–E06 的 2 个 Chunk 都有匹配的 Event 发布，共 12 个来源；E07 只有 Chunk 发布；E08 处于 INDEXING。`top_k` / `candidate_budget` 分别取 1 和 5。

| 观测 | top_k/candidate_budget=1 | top_k/candidate_budget=5 |
| --- | ---: | ---: |
| ES 调用前来源元数据 | 12 | 12 |
| ES 调用前正文实体 | 0 | 0 |
| ES 调用前加载的完整 `ChunkParseContent.detail`（含索引缓存） | 0 | 0 |
| ES 调用后正文 SQL | 1 | 1 |
| ES 调用后加载/返回的正文实体 | 1 | 5 |

SQL 观测在 ES 调用前检查正文列未进入 SELECT，ORM `load` 事件检查被加载的 `ChunkParseContent` 实例均将完整 `detail` 标记为 unloaded。ES 调用后只出现一次批量正文查询，返回的 Chunk ID 不重复；实现上该查询只投影 `compact` 与 `evidence.camera_hints` 两个 JSON 路径，不选择完整 detail 列，也不读 `_index_embeddings_v2`。`load_indexed_chunk_content` 的身份查询不投影 detail。

行为断言：

- 发布回执缺少事件集合、回执哈希被伪造或索引缓存被移除时，已完成发布仍可检索，覆盖视频集合为 E01–E06。
- 正文版本不一致、Chunk 未完成或冻结 target 不符时，该视频被排除，结果返回 `partial` 并保留其余视频。
- 既有公开课程授权、片段子范围和逐视频代次用例保留；`published_reference_sources` 仍需读取编辑参考回执，不使用本次省略完整 detail 的路径。

## 定向回归

主 Agent 提供的命令与结果：

```bash
python -m pytest tests/server/test_public_course_readiness.py tests/server/test_video_parse_repository.py \
  -k 'bounded_candidate or completed_publication or incomplete_current or scope_reads_only' -q
```

结果：**9 passed, 140 deselected**，早于小字段保留修正。该命令覆盖有界正文、完成发布、未完成/当前版本排除和范围投影用例。

## 相关模块回归

小字段保留修正后的最终命令：

```bash
python -m pytest \
  tests/server/test_public_course_readiness.py tests/server/test_video_parse_repository.py \
  tests/server/test_video_event_retrieval.py tests/server/test_video_event_index_coordinator.py \
  tests/server/test_video_parse_index_coordinator.py tests/server/test_video_retrieval_service.py \
  tests/server/test_editing_mcp_flow.py tests/server/test_mcp_edit_references.py -q
```

结果：**248 passed、4 skipped**。已验证搜索、直接 hydration 和 `get_chunk_evidence` 保留原有摘要与运镜提示，同时 ORM 完整 detail 未加载。跳过的用例需要显式启用外部集成或浏览器环境。仅有一条第三方 Starlette/AnyIO 弃用警告。

## 验证边界

已验证：生成数据下的 ES 前 0 正文/0 完整 detail、12 条元数据来源；ES 后 1 次批量正文查询及 1/5 个正文实体；缓存缺失/伪造与版本/完成状态/目标排除行为。

未验证：PR 合并、部署后真实检索链路、RSS、延迟、token、真实素材召回质量。范围元数据仍为 O(scoped Chunk count)，本次没有把所有 SQL 都收敛到 top_k。没有实测性能或业务收益数字，结论只支持结构性读取收敛。

未运行全量后端回归或真实外部服务验收。
