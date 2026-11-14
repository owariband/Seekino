# CONFLICT 预填与原身份评分回放

2026-11-14；分支 `fix/voiceprint-conflict-prefill`，依赖 [PR #165](https://github.com/owariband/Seekino/pull/165)。设计见[TRD](../../../../design/03-product-reassessment/voiceprint-conflict-prefill-trd.md)，当前状态见[工作台 B12](../../../../dev/workbench.html)。

旧 Canvas 只预填 ACCEPT，CONFLICT 保留 Understanding 名字；B0 又只导出前三，因此原身份落在前三之外时没有对照分数。修复后两类分档均可先填 top1，原身份有真实评分则一并保留；Refine 仍有审核、更正和置 null 权限。

直接覆盖 Canvas／Refine、B0 算法和 Server 原子发布的隔离测试 **379 passed，5.30s**。修改前选定的 12 项回归中 3 项失败，分别证实 CONFLICT 草稿、完整 Refine 提交和原身份候选丢失。执行命令：

```bash
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest \
  tests/agent/video_parse_v2 \
  tests/engine/parsev2/test_voiceprint_matching.py \
  tests/server/test_video_parse_voiceprint.py -q --tb=short
```

冻结输入取自本机整集 v2 `7504e1bb-2958-4b1d-9d77-407b829e93c9`，包含 314 行、313 个声纹向量和 24 份首次 Understanding。保持模型、encoder、参数和参考池不变，重算所有可编码目标，逐条断言前三及 gap 与原 artifact 相同；随后使用真实 Canvas／Prompt 构造器重建覆盖目标的原始 Refine 窗口。原始 artifact SHA-256 与调用关联见 [replay.json](replay.json)。完整资料留在本机 `outputs/verification/ep04-prefill-context-20261114/private/`；回放脚本为其中 `replay-conflict-prefill.py`。

| 检查项 | 修改前 | 修改后 |
| --- | --- | --- |
| 整集前三及 gap | 原评分 | 313/313 完全一致 |
| 原身份不在前三而有评分 | 未提供 | 63 行补回 |
| 294500–296500ms 草稿 | 若葉睦 | 千早愛音 |
| 对照评分 | 千早愛音 0.757265；睦缺失 | 千早愛音 0.757265；若葉睦 0.512934 |
| gap／分档 | 0.053973／CONFLICT | 不变；conflict=true 保留 |
| 原始参考池／引用 ID 进入模型 | #165 已移除 | 仍未进入 |
| 新 Agent 最终身份 | 未运行 | 未运行 |

DeepSeek Flash 通过 `delegate` 的 OpenCode 后端仅读最小评分摘要，确认草稿置换和 NOT_RUN 边界；主 Agent 复核了原始向量重算断言、实际输入关联和源文件不变。摘要中的第四项是为比较附上的原身份，不代表其在全量人物中排名第四。

这次证明输入路径已经按 top1 预填并提供原身份对照，不能据此声称新整集归因全部正确、Agent 会保留爱音、token／费用降低或解析耗时改善。没有新模型调用、数据库写入、服务重启或 CPU/prod 访问，旧 v2 结果保留。Understanding 跨 cue 重组与附带摘要失败沿用独立问题记录。
