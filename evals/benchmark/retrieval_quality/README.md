# Retrieval Quality · 检索质量

从原 `evals/mygo` 拆出的 **13 个 Event／Chunk 检索配置、23 条历史观测、5 条已知参考事实**。它回答固定 query、scope、参数时是否召回已知来源及其排名，不测完整 Chat 的理解、组织或最终交付。

- [cases.json](cases.json)：保留原 case ID、query、数据 scope、权重、top_k、筛选条件与评分规则。
- [baseline.jsonl](baseline.jsonl)：保留全部 23 条原始精简观测，含失败、候选、排名、耗时、用量、原评审和来源 SHA。
- [共享运行器](../run.py)：复用现有生产检索工厂，不启动解析 Worker 或回填索引。

## 使用方法

在仓库根目录执行：

```bash
# 离线：不需要服务、数据库或模型。
.venv/bin/python -m evals.benchmark.run --suite retrieval_quality
.venv/bin/python -m evals.benchmark.run --suite retrieval_quality --replay evals/benchmark/retrieval_quality/baseline.jsonl

# 显式真实检索：连接已有 SQL/ES，调用配置中的 embedding/rerank。
uv run --no-sync --env-file .env.local python -m evals.benchmark.run --suite retrieval_quality --live retrieval \
  --only retrieval-ep04-event-quote retrieval-ep04-chunk-quote \
  --data-version '填写实际素材和索引版本' --repeat 3
```

`--data-version` 是操作者声明；数据 scope、媒体、索引 readiness／contract 仍须实际核对。需要生成式重排时同样遵守 Kimi 测试约定，runner 不替操作者改模型绑定。新结果写入 `outputs/verification/retrieval_quality-*.jsonl`；可用相同 `--suite` 与 `--replay` 回放。

## 评分与历史边界

旧基线 **19/23 次执行完成、16/23 次通过已知来源检查**。四次 Event 索引未就绪与三次原中文问题的 Chunk 漏召回保留；索引未就绪不能改记为空结果或算法漏检。

所有时间为整数毫秒。来源匹配检查 scope、集数、时间覆盖；Event 还核对原句与说话人，使用稳定文本／时间锚点，不以重新解析可能变化的 ID 判错。Chunk 覆盖父块不证明精确句定位。Event `best_rank` 为完整融合排名，`top_k` 限制父 Chunk 数，二者不可混淆。参考只是已知正例，不是全剧金标，不能计算全库 Recall@K。

2026-10-18 正式 13 集 scope 的 312 Chunk 并没有 Event receipt；独立 EP04 的 430 Event 不能代替正式 scope 就绪。在已覆盖的 EP04，日文原句 Event／Chunk 均首位命中，三次中位耗时 0.699／0.596 秒；这不证明端到端 Chat 更快。

固定中文查询、相同 scope、top_k=10、权重 0.65/0.35/0 的历史对照：

| 输入 | 参考目标 | Event 融合／父块排名 | Chunk 父块排名 |
| --- | --- | --- | --- |
| 不相干的人别插话 | 09:08 立希“局外人闭嘴” | 8 / 6 | 10 |
| 这又不关你的事，你少掺和 | 09:08 参考场景 | 12 / 8 | 前十无该父块 |
| 同上 | 07:02.5、07:04.5 拒绝来往的近义候选 | 5、8 / 4 | 4 |
| 不懂音乐就别来指点 | 20:03.2 爱音谈和弦 | 2 / 2 | 5 |

premise 的旧目标误用了 09:08 立希，现有 fixture 已据原文改为爱音的和弦台词；原旧排名继续以 `legacy_target_*` 保留。不能把旧目标排名用于当前机械评分，也不能把模糊问题的参考候选当成唯一正确答案。

完整 Chat 的同题回答、候选曝光及交付问题见 [query_capabilities 历史结论](../query_capabilities/README.md#历史结论event)。更早原始文件已退役的范围见 [归档边界](../query_capabilities/README.md#归档与清理边界)；本次只拆分已有记录，没有重新执行检索。
