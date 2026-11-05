# 1. 问题与现状

**大纲 Chunk 引用 TRD。状态：初稿（2026-11-05）。** 分支 `fix/outline-chunk-citation`，PR 待创建。上游：[video-outline-trd.md](video-outline-trd.md)（PR #41，已合并）。2026-11-05 发布验收中发现。

## 背景与触发场景

2026-11-05 在演示 API 19641（`dev` `978be570`，v11 课程库）跑 LOOP-08 门槛。LP02（跨课最短观看路线）中，Supervisor 读完两集大纲后用 `read_retrieval_evidence` 读取大纲 Chunk 的源证据，再用这些 Chunk 调 `submit_video_item`，全部得到 `UNKNOWN_ITEM`；随后它在回答里对用户写出“大纲里的 Chunk 不能直接作为片段提交，我改用事件检索定位可提交的区间”，再追加多次 `search_video_events`。

## 实际与期望行为

期望：Agent 已读取源证据的片段可以直接作为来源提交；工具内部限制不出现在回答里。实际：#41 规定大纲 Chunk “可读不可提交”，读取源证据后仍不可提交；Agent 撞到限制并向用户解释。

## 现状证据

- 记录：`outputs/verification/course-loop-dev-20261104/gates/loop08-1.json`，LP02：26 次工具调用（3 次 `get_video_outline`、9 次 `read_retrieval_evidence`、10 次 `submit_video_item`、4 次 `search_video_events`），96.7 秒；回答含内部术语“Chunk”。
- `submit_video_item` 只接受检索命中（`agent/chat/supervisor.py` 中 `retrieval_hit(retrieval_calls, video_chunk_id)`）；`read_retrieval_evidence` 对大纲 Chunk 走 `outline_evidence` 回退，不登记为命中（`agent/chat/video_search/tools/retrieval.py`）。
- 同轮 Q01、Q03 各有 1 次 `UNKNOWN_ITEM`（实时证据或跨轮 Chunk），回答未受影响。

## 负面影响

内部术语进入回答（C25 门槛的失败条件）；多余的检索与提交使 LP02 接近 100 秒。

## 本次边界

解决：已通过 `read_retrieval_evidence` 读取源证据的大纲 Chunk 可以提交；Prompt 禁止向用户解释工具失败与内部限制。不解决：实时证据提交（C22 残余）、大纲批量超限（`OUTLINE_TOO_LARGE`）。必须保持：未读取源证据的大纲 Chunk 仍不可提交（大纲摘要不等于证据）；提交时间范围仍须落在源证据内。

# 2. 影响因素、假设与评价指标

| 因素 | 假设 | 验证 |
| --- | --- | --- |
| 已读源证据不可提交 | H1：读取后登记为可引用即可消除 LP02 的 `UNKNOWN_ITEM` | 单元测试 + LP02 复测 |
| 工具失败被写进回答 | H2：Prompt 明确“工具失败与内部限制不写入回答”后不再出现 | LP02 复测检查内部术语 |

| 指标 | 基线 | 目标 / 护栏 |
| --- | --- | --- |
| LP02 回答内部术语数 | 1（“Chunk”） | 0 |
| LP02 `UNKNOWN_ITEM` 次数 | ≥ 5 | 0 |
| 未读源证据的大纲 Chunk 提交（护栏） | 拒绝 | 仍拒绝 |
| 提交时间范围校验（护栏） | 按源证据校验 | 不变 |

# 3. 改造方案与建模

`read_retrieval_evidence` 成功读取大纲 Chunk 的源证据后，把该证据登记为本轮可引用条目（与检索命中同一登记表，或单独的已读证据表供 `submit_video_item` 查询）；`submit_video_item` 先查检索命中，再查已读证据，时间范围校验使用同一份源证据。Prompt 改为：大纲 Chunk 读取源证据后可提交；工具报错、可用性限制和内部字段不写入回答。

# 4. 对照实验与结果分析

计划：单元测试覆盖读后可提交、未读拒绝、时间范围校验；19641 上复测 LP02 与 SE02，记录工具调用、耗时与回答内部术语。结果：待测。

# 5. 试点验证与方案取舍

以演示环境复测代替独立试点。

# 6. 结果验收与闭环

待实施。
