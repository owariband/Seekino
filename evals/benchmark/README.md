# Benchmark 索引

按**要衡量的能力**选择评测集。模型、harness、是否 multi-agent 属于每次运行的条件，不作为评测集名称。未来生成式模型测试使用 **Kimi K3.1 Pro（`k3.1-pro`）**；旧 GPT 结果只保留为历史观测，不能作为新运行的默认配置。规则见 [模型配置](../../docs/model-config.md)。

| 评测集 | 衡量什么 | 当前规模 | 输入与结果 |
| --- | --- | --- | --- |
| [query_capabilities · 查询能力覆盖](query_capabilities/README.md) | 人物关系、推荐、全剧回顾、动作、短台词、播放上下文等多类型任务的完成能力；原 multi-agent 那组 | 13 个 Chat 用例；13 条旧基线 + 13 条旧 Kimi + 39 条三框架对照 | [cases](query_capabilities/cases.json) · [三框架 Kimi](query_capabilities/harnesses-kimi-20261026.jsonl) · [旧基线](query_capabilities/baseline.jsonl) |
| [chat_behavior · 对话理解与应变](chat_behavior/README.md) | 接住反馈、切换意图、理解指代与否定、按顺序完成多个请求、诚实表达证据边界 | 10 个对话场景；100 条历史观测 + 60 条三框架对照 | [cases](chat_behavior/cases.json) · [三框架 Kimi](chat_behavior/baselines/harnesses-kimi-20261026.jsonl) · [历史结果](chat_behavior/baselines/) |
| [retrieval_quality · 检索质量](retrieval_quality/README.md) | 给定 scope/query/参数时，Event 与 Chunk 是否召回已知证据及其排名 | 13 个检索配置；23 条历史观测 + 13 条当前检查 | [cases](retrieval_quality/cases.json) · [baseline](retrieval_quality/baseline.jsonl) · [当前实测](reports/chat-harness-kimi-20261026.md#独立组件结果) |
| [intent_routing · 意图路由与改写](intent_routing/README.md) | 前置 respond/retrieve/mixed 分类与检索 query 改写，不包含完整 Chat 交付 | 13 个路由场景 + 1 个 API 协议 smoke；本轮 Kimi 重跑 | [cases](intent_routing/cases.json) · [脚本及历史记录](intent_routing/README.md) · [当前实测](reports/chat-harness-kimi-20261026.md#独立组件结果) |

想调整“真是高高在上呢 哪里出现过”之后又说“你说话怎么这么无趣”的那组，直接编辑 **[chat_behavior/cases.json](chat_behavior/cases.json)**；逐例验收要求在同目录 README。`query_capabilities` 也有一句相近的出处查询，但不含这一组反馈与意图切换场景。

最新实测见 **[Kimi：Deep Agents / Pi / Codex 对照报告](reports/chat-harness-kimi-20261026.md)**：99 条主计划记录，另有 13 条直接检索与 14 条路由／协议组件检查。分别保留执行状态、内容质量、交付、耗时和用量；失败、环境中断、单独补跑及评审修订均可追溯。

## 运行入口

从仓库根目录执行，下面全部离线，不启动服务或调用模型：

```bash
.venv/bin/python -m evals.benchmark.run --suite query_capabilities
.venv/bin/python -m evals.benchmark.run --suite query_capabilities --replay evals/benchmark/query_capabilities/baseline.jsonl
.venv/bin/python -m evals.benchmark.run --suite query_capabilities --replay evals/benchmark/query_capabilities/kimi-20261025.jsonl
.venv/bin/python -m evals.benchmark.run --suite query_capabilities --replay evals/benchmark/query_capabilities/harnesses-kimi-20261026.jsonl
.venv/bin/python -m evals.benchmark.run --suite retrieval_quality --replay evals/benchmark/retrieval_quality/baseline.jsonl
.venv/bin/python -m evals.benchmark.intent_routing.openrouter_jev --dry-run
.venv/bin/pytest -q tests/evals/test_benchmark.py tests/evals/test_harness_comparison.py
```

[run.py](run.py) 从原 MyGO runner 移入，支持**查询能力覆盖、检索质量**，要求显式传 `--suite`，避免默认选错集合。真实运行方式见各目录 README；模型由目标服务／项目配置决定，runner 不会自动切换成 Kimi，运行前必须核对实际绑定及 fallback。

三框架对照入口为 [compare.py](compare.py)，绑定适配在 [harnesses.py](harnesses.py)。默认只输出离线计划；真实运行需要显式 `--live`、Kimi 配置、已有 SQL/ES/素材、改写服务及隔离恢复的 Pi 依赖，准备命令见报告。它将完整 `recent_messages` 写入每例新会话，经实际 ASGI HTTP 入口执行；T01→T02 保留同会话追问。不能用无历史单句或路由 dry-run 代替这套 Chat 验收。生产模型配置和全局 Codex 配置不由对照入口修改。

## 结果与维护约定

- 用例按能力归类；同一句话出现在不同层级时，分别评估。不同 suite 的分母、完成率、质量分数不混算。
- 输入、参考事实、评分要求和实际观测分开。参考答案／`expected` 不发给模型；历史完整输入保留，改题不能反过来重标旧结果。
- 运行要注明 suite、用例版本或 SHA、模型／harness／prompt／预算、数据与索引状态、重复次数及计时口径；失败与未知用量保留。`COMPLETED`、来源检查通过、语义任务通过分别报告。
- `cases.json` 是维护入口，`baseline.jsonl`／`baselines/*.jsonl` 是历史证据；新的运行产物仍写入 `outputs/verification/`。只有审核过的精简基线才提升到这里，不把日志、源码快照和签名播放 URL 搬进 Git。
- 没有全剧穷尽金标，不报告全库召回率；10 个对话场景也不能代表通用“聪明程度”。未来增加长上下文、主动澄清等场景时，先补输入与验收标准，再生成结果。

## 迁移与相邻评测

旧 `evals/mygo` 的 26 个用例、36 条观测按 `kind` 无损拆成 `query_capabilities` 与 `retrieval_quality`；原 runner 移到本目录。旧 `evals/query_router` 移到 `intent_routing`，模块命令随之更新。旧路径不再作为活动入口；本地历史脚本里的旧 import 只说明当时运行环境，不作为当前命令。

`chat_behavior` 取自 2026-10-24 两轮端到端实验，保留 40 / 60 条观测及各自原题、评审、协议、来源 SHA。2026-10-25 的 Kimi 13 题归入 `query_capabilities`，不能代表 `chat_behavior` 已测过。

其他专门评测仍有明确用途，暂不设 `etc` 杂项桶：[延迟诊断](../latency/)、[召回 smoke](../recall/)、[声纹算法](../speaker/)、[Parse harness](../parse_harness.py)。它们尚未统一为本目录的固定回归集；三轮 Chat harness/checkpoint 冒烟也不能与 10 个对话场景混合计分。
