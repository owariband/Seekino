# 2026-11-15 Chat 子 Agent 模型配置继承修复验证

日期：2026-11-15（本地 UTC+8）。分支 `fix/chat-model-config-inheritance`，目标 `dev`；实现提交 `30680fe4`；[dev PR #171](https://github.com/owariband/Seekino/pull/171) 已以 `dc9c86fa81952b3ad43d3041d28f151d6d807c82` 合入。历史回放证据来自已合入 dev 的 [PR #166](https://github.com/owariband/Seekino/pull/166)（合并提交 `ff9f8fe555c1bf0f678d5af0617e1076e4d4390f`）。本分支于 2026-11-14 开始，没有 main 发布，也没有部署；修复尚未在正式环境生效。

## 1. 结论与边界

本次改动只修复 Chat 子 Agent 的模型配置来源：Server 从当前 Supervisor 的选择出发，一次性装配父子两个角色的模型、来源、主备、思考档位、输出与超时预算、字符保护和上下文容量策略。Gateway 让 DeepAgents、structured 与 loop 三类 API builder 共用同一个动态容量解析入口，不再由 structured Query 单独维护一套静态窗口。

验证结果支持“实现完成、相关回归通过、当前 Cloud 配置的父子策略一致”，不支持“修复已上线”。完整 Server 在线装配、修复后的原始 Chat 回放、TT／FTT／token／准确率收益均未验证。显式的 200000 字符上限仍然生效，可能早于 891289 token 触发压缩，因此不能写成“Query 的 9 次压缩将变为 0”。

## 2. 原问题与实现

修改前，`server/bootstrap.py` 分别构造 Query 与 Supervisor。Query 固定使用全局 `agent` binding/factory，Supervisor 可以使用独立 `supervisor` binding 或会话选定的模型。即使模型名相同，Gateway 的 DeepAgents 构建会解析动态容量，structured Query 构建却没有调用同一解析，只合并旧静态窗口。Cloud 的实际策略因此分叉为 Supervisor 1048576／891289／24000，Query 64000／48000／24000，后者另受 200000 字符上限约束。T01 的两个 Query 子任务共记录 9 次本地上下文替换；压缩是否直接导致分页重读仍没有因果对照。

现在的装配关系是：

- 有独立 Supervisor 时，不再额外创建 Chat 无法消费的全局 agent factory；父子都使用当前 Supervisor binding 与 factory。
- 只有全局 agent 时，父子仍共用该 binding，并保留既有 fallback model 覆盖。
- 个人 ChatAPI 目标使用同一个 `PersonalModelTarget` 构建 structured 与 supervisor，复用目标模型、协议、视觉能力和客户端关闭路径。
- 请求超时、输出 token、字符上限和 `ContextWindowPolicy` 只从当前选择装配一次，父子使用相同值；显式输出、超时和字符保护继续取小值。
- `gateway/agent_model/factory.py` 的 `_controls` 统一调用动态容量解析。DeepAgents、structured、loop 和 native builder 都经过该入口；继续使用既有模型元数据缓存、主备容量取小和失败回退，没有新增容量、比例或策略类。
- Chat 不再读取独立的 Query 模型名或 `query_reasoning_effort` 覆盖；这些旧配置字段仍能解析，避免旧 YAML 或环境变量直接报错，但不会再把子 Agent 带向另一套模型。
- 未启用 `auto_compact_ratio`，或模型容量既不能发现也没有声明时，沿用原共享静态策略；发现失败但声明了容量时，仍按声明容量推导比例策略。native harness 自行管理上下文，不套这组 API 窗口。

Query 的 prompt、工具、结构化报告、循环、模型调用上限、共享工具预算和失败语义没有改；JEV、独立改写器、检索与证据授权也没有改。原生 Codex／Claude 上下文继续由原生 runtime 拥有。

## 3. 测试范围与命令

测试使用 mock provider，没有真实 Chat 模型生成调用。两个范围的文件不重叠，合计 `338` 项唯一通过；worker 的重复运行不计入该总数。

```bash
PYTHONPATH="$PWD" /Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest tests/gateway/test_agent_factory.py tests/gateway/test_model_factory.py tests/gateway/test_context_window.py -q --tb=short -m "not live_agent"

PYTHONPATH="$PWD" /Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest tests/server/test_bootstrap.py tests/server/test_personal_models.py tests/gateway/test_user_model_turns.py -q --tb=short -m "not live_agent"
```

| 范围 | 通过 | 失败 | 用时 | 备注 |
| --- | ---: | ---: | ---: | --- |
| Gateway factory／model／context | 202 | 0 | 2.73 s | 无警告 |
| Server 装配／个人协议 | 136 | 0 | 32.76 s | 1 条既有 Starlette `anyio.abc.BlockingPortal` 弃用警告 |
| 合计 | 338 唯一通过 | 0 | 两次运行 | 文件不重叠，worker 重复运行未计入 |

修改前的红证据由受控 worker 运行、主 Agent 复核最终断言：7 项 Gateway 新回归失败，2 项静态控制通过；Bootstrap 捕获到 `configured-query-model != configured-chat-model`。这说明测试确实能区分旧装配和新装配，而不是只验证“能构建”。

覆盖内容包括：

- structured 与 DeepAgents 在相同 binding 下得到相同动态容量；API builder 随所选模型切换容量，主备存在时取较小值，元数据失败时使用声明容量并只查询一次，未启用比例时保留旧共享策略。
- Bootstrap 覆盖独立 Supervisor、仅全局 agent、Codex native、Ark alternate YAML、fallback 与父子输出／超时／字符／容量一致性；native 路径保留自己的上下文所有权。
- 个人模型覆盖 `openai_chat`、`openai_responses`、`anthropic_messages`、`gemini` 四种协议的完整结构化工具循环，均为 mock；构造失败和正常结束都会关闭私有客户端。
- 旧 YAML 的 Chat override 在编辑前后仍可解析；相关配置测试通过，没有把兼容字段删除。

## 4. 当前 Cloud 配置预检

预检使用当前生产 YAML 和一次真实模型元数据 GET。PATCHED Gateway 在同一个容器内、单独的临时进程中导入，避免替换正在运行的 API 模块；没有重启服务、修改生产模块文件或替换在线 Server。它验证的是当前选择对应的 binding、factory 和 Gateway 容量解析，不是完整的在线 Server 装配。

| 项目 | 结果 |
| --- | --- |
| 模型／来源／思考档位／输出 | deepseek-flash／deepseek_api／high／8192 |
| Query policy | context 1048576、output reserve 8192、safety margin 4096、auto compact 891289、compact target 24000 |
| Supervisor policy | 同上：1048576／8192／4096／891289／24000 |
| 元数据请求／生成调用 | 1／0 |
| 模型配置／运行文件／patch SHA-256 | `547729ba…67d2`／`98c16c9e…7990`／`e9431b4d…4de4` |
| 服务状态 | 未重启，容器与启动状态未变，健康 |

模型配置、运行文件和 patch 的完整哈希、生产文件哈希及逐项边界见 [checks.json](checks.json)。父子策略一致说明动态解析已经进入当前选择的两个 API builder，且只触发了必要的元数据查询；`generation_calls=0` 说明没有借预检跑真实回答。

该结果仍有明确边界：

- 预检没有运行完整 live Server assembly，也没有验证 HTTP／SSE、视频卡、工具执行和前端交付。
- 200000 字符应用上限仍在。长上下文可能先触发字符保护，实际压缩点不一定等到 891289 token。
- native harness 的上下文仍由原生 runtime 管理，不能用这组 API 预检值代替。
- 没有修复后的原始 Chat 回放，因此不能报告 TT、FTT、token、准确率或“9 次压缩降为 0”的收益。

## 5. 正式发布候选与后续工作

- 正式发布候选为 [main PR #172](https://github.com/owariband/Seekino/pull/172)，只选取 3 个运行文件、3 个直接回归文件及 main 原有 Gateway README 的最小升级说明，未带入内部材料。同范围 338 项在 main 选取后通过（18.93 秒，1 条既有警告，不另计唯一测试），发布树 810 路径／10 资产及 GitHub `release-files` 通过，main 忽略规则未变；尚未合并或部署。
- 合入后应在真实 Server 装配中确认父子实际使用同一 binding／个人目标，并保留模型、策略和哈希证据，不只看单元测试。
- 需要沿用原 T01–T03 的输入、历史和权限边界做修复后回放，单独记录 TT、FTT、token、压缩次数、工具预算与回答质量；样本数、成本和失败项要一起保留。
- 需要观察 200000 字符上限与 891289 token 阈值同时存在时的实际触发顺序，以及主备切换、元数据失败、长上下文和并发下的表现。
- Query 的循环停止、分页进度恢复、部分失败报告、早回应 prompt、`UNKNOWN_ITEM`、取帧时间精度和共享预算仍由 `CHAT-QUERY-BUDGET-01` 等独立事项跟踪，本次没有修改。

相关入口：[配置继承 TRD](../../../../design/03-product-reassessment/chat-model-config-inheritance-trd.md)、[压缩 TRD 的本次证据](../../../../design/03-product-reassessment/chat-model-context-compaction-trd.md)、[devlog](../../../../dev/devlog.md#devlog-chat-model-config-inheritance-20261115)、[原始回放报告](../../../agent/chat/history-latency-replay-20261114/README.md)、[历史压缩实验](../chat-context-compaction-20261113/README.md)。
