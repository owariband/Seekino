# Chat 模型窗口与同步压缩：本地对照

日期：2026-11-13（UTC+8）。分支 `perf/chat-model-context-compact`，基于本地 `dev` / `80cd796b`；[PR #144](https://github.com/owariband/Seekino/pull/144)，[主 TRD](../../../../design/03-product-reassessment/chat-model-context-compaction-trd.md)，工作台 `CHAT-COMPACT-01`。

本轮真实模型对照已完成：原策略在三个模型边界分别阻塞 22.302／29.739／23.372 秒生成摘要；动态策略均不摘要。三个边界合计从 84.481 秒降到 14.940 秒，减少 82.32%。结论只覆盖 compact 加一次正常续推，尚不证明整轮 Chat 同比提速或最终交付质量不退化。

## 问题与已取得的证据

既有 Cloud T01–T03 整轮耗时为 75.659 / 47.880 / 88.877 秒，摘要分别占 45.191 / 30.194 / 51.932 秒。本地当前未启动 MySQL、ES 和 Chat 服务，默认配置为 Kimi，因此不直接使用默认配置或启动生产 Consumer 做对照。

本轮重新查询同一 DeepSeek 官方连接，`deepseek-flash` 返回 `context_window=1048576`、`max_output_tokens=393216`。新 Gateway 发现函数也实际读取到 1M，而非仅在测试中伪造元数据。本轮仍将生成输出限制为 8192，不把供应商最大输出能力当作请求预算。

已获准导出三条请求关联的历史和请求前后 checkpoint；只读导出，不修改云端数据。原始聊天、工具内容、图片、checkpoint 和后续真实请求／响应仅保存在私有临时目录，不进入 Git。原始保存值包含 DeltaChannel 更新，使用相同 SDK 的 `get_state()` 重建后再序列化；不是把底层 delta 列表误当成完整消息。

## 实验范围与可比条件

入口为 [chat_compaction_replay.py](../../../../../evals/latency/chat_compaction_replay.py)。从原始历史应用之前的摘要事件，定位本轮第一处会触发旧阈值的模型调用边界；冻结该边界已有的真实工具结果，使用当前 `dev` Supervisor 代码构造 prompt 和 tool schema。原始完整 system prompt 未记录，重建时只传入冻结的系列名称、播放位置与聊天历史，不声称与 Cloud 原请求字节完全一致。

真实执行范围为生产 DeepAgents graph、Filesystem／Summarization middleware、Gateway 和一次正常模型续推。正常响应返回后主动停止，不执行其新工具；因此测量“compact 加一次模型续推”，不测整轮 Chat、SQL／ES、媒体读取、HTTP／SSE／DOM 或最终视频卡交付。Cloud 整轮时间不能直接作为这一步的基线。

每组固定同一来源、`deepseek-flash`、`high`、8192 输出、4096 安全余量、24000 尾部消息保留预算和原始输入。A 使用原 `dev` 的 64000／48000 策略；B 使用新装配逻辑，要求实际解析到 1048576／891289，否则实验直接失败，不把回退声明冒充实时能力。

| 输入 | 进入摘要前完整输入估算（token） | 离线 A 摘要调用数 | 离线 B 摘要调用数 | 压缩前输入一致性 |
| --- | ---: | ---: | ---: | --- |
| T01：桥上场景定位 | 49395 | 1 | 0 | 相同 |
| T02：核对拉手 | 48267 | 1 | 0 | 相同 |
| T03：17:36 抽帧核验 | 50152 | 1 | 0 | 相同 |

上表只证明实际 SDK 的触发路径。模型响应在离线检查中被本地替身替代，不能据此报告时延收益。进入摘要前的哈希在摘要中间件的实际入口采集，按 Gateway 可见的 OpenAI 消息（含 reasoning）与工具定义计算，排除 LangGraph 随机分配的内部消息 ID。两组哈希逐一核对；工具与固定输入均未被简单文本长度差替代。

## 真实模型结果

共执行 9 次真实生成：A 每例一次摘要加一次正常续推，共 6 次；B 每例只需一次正常续推，共 3 次。原始 trace 核实所有请求均为 `deepseek-flash`、`reasoning_effort=high`、`thinking.type=enabled`、`max_tokens=8192`，9 次均完成，未重试。两组压缩前请求哈希及 fixture 哈希逐一相同；A 的真实 factory 源码来自未修改的 dev，B 来自实验 worktree，哈希保存在 [metrics.json](metrics.json)。

| 输入 | A：compact＋续推 | A：摘要 | A：正常续推 | B：compact＋续推 | B：摘要 | B：正常续推 | 时间减少 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| T01 | 25.283 s | 22.302 s | 2.965 s | 2.752 s | 0 | 2.737 s | 89.12% |
| T02 | 33.496 s | 29.739 s | 3.737 s | 6.891 s | 0 | 6.881 s | 79.43% |
| T03 | 25.702 s | 23.372 s | 2.314 s | 5.297 s | 0 | 5.288 s | 79.39% |
| 三例合计 | 84.481 s | 75.414 s | 9.016 s | 14.940 s | 0 | 14.906 s | 82.32% |

摘要在 A 中占所测边界的 88.21%／88.79%／90.93%。usage 时间戳核对为摘要结束后才开始正常请求，确认为串行等待。B 的正常续推合计反而多用 5.890 秒；本轮的净改善主要来自省去 75.414 秒摘要，没有通过降低思考档位或令正常模型静默失败换速度。B 首次模型构建含能力查询共 0.334 秒，A 为 0.0007 秒；该一次性成本不计入表中的模型边界，当前 factory 后续复用缓存。

| 输入 | 未压缩完整输入估算 | A 压缩后完整输入估算 | 估算净回收 | B 未压缩 provider input | A 压缩后正常 provider input | 同输入下 provider 净差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| T01 | 49395 | 39067 | 10328 | 43909 | 33752 | 10157 |
| T02 | 48267 | 31749 | 16518 | 36654 | 21624 | 15030 |
| T03 | 50152 | 29205 | 20947 | 33910 | 20819 | 13091 |

单位均为 token。实际完整输入的净回收不是 `48k - 24k`，摘要、固定 system/tools 和近期消息共同占用压缩后输入。这里的 provider 净差用 B 的同一未压缩上下文对照 A 的压缩后正常请求；不使用仅包含历史前缀的摘要请求来代替完整压缩前输入。估算与 provider 口径不同，媒体尤其可能存在差距。

| 三例合计的 provider 用量 | A：包含摘要和续推 | B：只有续推 |
| --- | ---: | ---: |
| input token | 117635 | 114473 |
| output token | 15865 | 1437 |
| 其中 reasoning token | 9626 | 863 |
| total token | 133500 | 115910 |
| cached input token | 48128 | 21504 |
| 未缓存 input token | 69507 | 92969 |

reasoning 是 output 的子集。B 正常请求保留的输入更多，但省掉摘要后总 input 略降、output 显著减少；未缓存 input 却增加 23462 token，不能从 total token 的减少直接推导费用同比下降。本轮未读取账单、未按实时单价换算金额。

所有正常续推都返回了工具调用，6 次调用参数通过实际 tool JSON Schema 校验，脚本没有执行这些新工具。T02 两组都选择提交片段，但结束点分别为 1040 s 与 1045 s；未重验原片范围，不能宣称完整质量相同。T03 另用项目 `cursor_position()` 离线核对：A 产生的 cursor 无法解析，会被拒绝；B 的 cursor 可解析、资源匹配，并与冻结工具返回一致。这是单个边界观察，不作为质量评分或泛化结论。

三个场景各只运行一对，顺序为全部 A 后全部 B；缓存命中及模型下一步决策不同。不能据此报告 p95/p99、重复方差或把所有毫秒变化归因到同一因素。这些输入都约 50k，实验支持消除提前摘要，并不证明 85% 比其他足够高的安全比例更优。

## 实现与验证

模型绑定新增可选 `auto_compact_ratio`，默认 `None` 保留旧策略。本地配置仅为 `models.supervisor` 设置 `0.85`；不修改其他角色或生产配置。新策略复用模型发现的鉴权、出站、分页、大小与错误控制，缓存选中模型的容量；查询失败时明确记录并回退到绑定声明。主备能力取较小值，触发受本轮输出和安全预算约束。普通 Query/Parse、原生 Agent、个人 BYOK 路径不随本轮改变。

主 Agent 验证的命令与结果：

```text
PYTHONPATH=<实验 worktree> <项目 Python 3.12> -m pytest -q -p no:cacheprovider \
  tests/gateway/test_model_discovery.py tests/gateway/test_agent_factory.py \
  tests/gateway/test_context_window.py tests/gateway/test_deepagents.py \
  tests/gateway/test_deepagents_multimodal.py
# 190 passed

PYTHONPATH=<实验 worktree> <项目 Python 3.12> -m pytest -q -p no:cacheprovider \
  tests/gateway/test_model_config_v2.py -k '<排除下述三项既有失败>'
# 130 passed, 3 deselected
```

三项失败为 `test_default_model_migration_preserves_asr_embedding_agent_and_parse_strategy`、`test_independent_supervisor_keeps_agent_models_and_environment_overrides_separate`、`test_codex_supervisor_without_independent_binding_requires_the_agent_source_to_match`。主 Agent 用 `git archive dev` 的干净快照独立重跑，仍为相同 3 项失败；不修改这些无关断言来制造全绿。新增 ratio 边界、能力缺失、模型切换／缓存、较小 fallback、输出预留及 SDK 接线均包含在相关验证中。

离线完整 SDK 回放的三对输入哈希一致，A 每个边界都先摘要，B 每个边界均直接续推。脚本编译与 `git diff --check` 通过。

## 复跑与未覆盖范围

先准备经授权的私有 fixture。`--prepare` 需要私有历史／checkpoint 导出包，裸 clone 不包含这些材料；不能把无输入时的脚本存在视为可复跑同一请求。

```bash
# baseline 源码指向未修改的 dev；runner 本身来自实验分支。
PYTHONPATH="$baseline_checkout" "$project_python" "$experiment_checkout/evals/latency/chat_compaction_replay.py" \
  --fixtures "$private_fixture" --arm baseline --out "$private_baseline_output"

PYTHONPATH="$experiment_checkout" "$project_python" "$experiment_checkout/evals/latency/chat_compaction_replay.py" \
  --fixtures "$private_fixture" --arm dynamic --expected-context-size 1048576 --out "$private_dynamic_output"
```

本轮不证明接近 891289 token 时的摘要质量、满窗成本、长对话稳定性或完整业务质量不退化；也不包括 50% 后台 compact、工具并发优化或 Cloud 部署。本地同输入的真实模型边界已确认改善，支持在本次明确范围内保留动态策略；完整 Chat／视频卡验收与生产试点仍需另行补证。
