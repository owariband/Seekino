# 1. 问题与现状

需求：正式 Kimi 视频解析接入 Chat Completions 流式响应。日期：2026-10-30。阶段：代码接入与合成图验证完成；长 Chunk 与服务部署待验收。

- 分支：`fix/kimi-streaming`；目标分支：`dev`；[PR #20](https://github.com/owariband/Seekino/pull/20)。首提交 `218d96a1` 仅包含 TRD 初稿。
- 当前问题状态由 [problems.md](../../dev/problems.md) 维护；实验与决策写入 [devlog.md](../../dev/devlog.md)。

修复前，正式 Kimi `profile: kimi` 经 `GatewayClient._build_generative` 进入 `OpenAIVisionModel`。直接返回解析 JSON 的调用和携带视觉工具的调用分别在 `gateway/openai/vision.py`、`gateway/agent_model/multimodal.py` 固定发送 `stream=False`。用户期待沿用历史成功实验的流式方式，但正式链路没有接入。

2026-10-30，MyGO EP04 首个 Chunk（视觉输入 0–63 秒）的非流式请求均返回 HTTP 504：3 fps / 189 帧耗时 301.732 秒，1 fps / 63 帧耗时 419.475 秒，0.5 fps / 32 帧耗时 337.148 秒；后两次各一次、最多 600 秒、SDK 无自动重试。未返回 usage，不能从帧数推算实测 context 占用率，也不能以单次结果估计总体成功率。原始证据保留在本地 `outputs/verification/kimi-fps-ab-20261030-2115/`。

历史 `evals/parse_protocol_benchmark.py` 使用 `k3`、Responses API 和 `stream=True`，`outputs/verification/parse-protocol-20261029/kimi-1-json/summary.json` 有成功记录；正式请求使用 `k3.1-pro` 和 Chat Completions。历史记录足以要求优先核对流式差异，不能证明本次请求改成流式后必然成功。

本次修复正式 Kimi generative 适配器及共用视觉工具轮次的流式收集。保持 FPS、图片、Prompt、Schema、模型、reasoning 参数、重试策略与业务提交条件不变，不处理独立实验脚本中的 gate 问题。不得将不完整 JSON 或未完成的工具调用交给业务层。

# 2. 影响因素、假设与评价指标

假设：长时间等待完整响应可能触发上游非流式网关限制；SSE 提前传输增量可能改善等待过程并提供诊断证据。流式不减少输入 token，也不能消除首个事件之前的排队或处理超时。验证时保持其他请求参数一致。

| 指标 | 定义与基线 | 验收目标 | 方法与证据 |
| --- | --- | --- | --- |
| 正式链路请求模式 | Kimi 直接输出和视觉工具均为 `stream=False` | 两条实际调用链均发送 `stream=True`，收集末尾 usage | 工厂入口 + HTTP MockTransport 集成测试 |
| 完整性 | 现有完整 JSON / 工具结果校验 | 收齐终止状态后才校验、返回或执行工具；EOF、断流、截断不得成功 | SSE 正常与异常复现用例 |
| 用量与推理连续性 | 非流式读取最终 usage / reasoning_content | 保留尾部 usage、请求标识及跨工具轮次 reasoning_content | 多片段、多轮测试 |
| 控制与隔离 | SDK `max_retries=0`、既有取消/超时 | 取消关闭连接，流式过程检查控制和时间预算；其他 profile 行为不变 | 取消、超时、网络错误和既有回归测试 |
| 真实素材结果 | 三种 FPS 非流式各一次 504 | 同一 Chunk 流式取得完整且通过 Schema 的结果；仍需单独实测 | 当前真实素材两次调用授权已用完，后续按明确授权有界验证 |

# 3. 改造方案与建模

1. 在 Gateway 的 Kimi profile 装配处启用流式，避免依赖 provider 显示名或业务层开关。其他 OpenAI-compatible 适配器保留既有默认。
2. 在 OpenAI Chat 协议层集中收集 SSE，供直接视觉 JSON 和视觉工具轮次复用。复用 SDK 的分片类型/协议能力，完整拼接 content、reasoning_content、tool_calls 与 usage。
3. 请求 `stream_options.include_usage`，记录增量事件及完整响应。完整响应继续走原有 finish reason、Schema 和工具合同校验；不执行增量工具调用。
4. 将断流/读取失败映射为既有模型失败原因，保持取消优先、关闭流及客户端、不增加自动重试；流式读取期间执行控制与时间预算检查。
5. 无数据迁移、无解析 Schema 变化。工具轮次沿用既有 Canvas 与 Server 的事务/提交边界。部署前需将本分支集成至实际服务所用代码。

已按此顺序实施。协议收集复用 OpenAI SDK `ChatCompletionStreamState`，额外检查单个 completion 的终止 choice，保存末尾 usage，并逐分片检查控制和总预算；无事件的阻塞读取沿用 HTTP 超时。自动化测试发现旧工具合同只为 DeepSeek 透传 `reasoning_content`，本次在流式 Chat 轮次补回完整 reasoning，以供 Kimi 下一轮请求使用。未改动通用 Responses 工具循环或其他 provider 的默认协议。

# 4. 对照实验与结果分析

通过正式 Gateway 工厂 + HTTP MockTransport 覆盖完整 JSON 分片、末尾空 choices usage、工具参数与 reasoning 分片、多工具索引、EOF、读取错误、length/content_filter、取消、宿主控制及超时。三种入口（直接 JSON、legacy、Deep Agents）均实际发送流式请求；既有 OpenAI media 与工厂测试覆盖非 Kimi 行为。

| 指标 | 基线实测 | 方案实测 | 判断 |
| --- | --- | --- | --- |
| 正式请求 `stream` | `False`（源码） | 三入口均为 `True`，并请求末尾 usage | 工厂测试通过 |
| 模拟完整与异常流合同 | 尚无 Kimi SSE 覆盖 | 新增测试通过；相关七文件 281 passed / 1 failed | 失败为既有 Ark → DeepSeek fallback context 测试，基线同样失败 |
| 真实合成图接口兼容 | 此流式版本未测 | 1 次通过；首片 5.520 秒、完成 7.431 秒、110 片；364 tokens | 仅合成图兼容验证 |
| 真实 0–63 秒 Chunk 完成 | 3 次非流式均 504，参数不同，不能合并为成功率估计 | 待授权实测 | 未验证 |

完整命令、环境、既有失败复现与原始合成图结果见 [实施记录](../../report/gateway/kimi-streaming-20261030.md)。在未修改 `dev` / `df83c3f9` 单独复跑失败用例，同样 `StopIteration`；未将其混入本次修复。单张合成图不能代替正式素材验收，不宣称原视频 504 已解决。

# 5. 试点验证与方案取舍

试点限定单个 Chunk，不自动启动整集、不自动重试；先保持模型、FPS、Schema、reasoning 配置一致，以终止事件、Schema 通过、耗时和 usage 作为观测项。执行者为当前项目维护者/Agent，具体真实素材调用按已有或新增明确授权执行。

若仍在首个事件之前 504，保存证据并进一步区分上游排队/视觉处理和输出传输，不归因于网络不稳定。若流正常结束但 Schema 失败，按模型输出问题处理。回退仅撤销 Gateway 流式选择，不改变持久任务或已提交数据。

# 6. 结果验收与闭环

代码已实现，新增协议与正式工厂测试通过，真实 Kimi 合成图流式成功。当前工作分支完成接入，尚未集成/重启正式服务，未追加真实视频调用。原问题 B23 保持 TODO；下一步是在明确授权的单个长 Chunk 上保持其他参数不变复测，再核对实际服务加载。由当前项目维护者/Agent 执行；既有两次真实素材授权已耗尽，不能将本设计中的试点计划当作追加授权。
