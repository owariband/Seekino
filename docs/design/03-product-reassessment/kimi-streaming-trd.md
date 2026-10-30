# 1. 问题与现状

需求：正式 Kimi 视频解析接入 Chat Completions 流式响应。日期：2026-10-30。阶段：设计初稿，尚未实施。

- 分支：`fix/kimi-streaming`；目标分支：`dev`；PR：创建后回填。
- 当前问题状态由 [problems.md](../../dev/problems.md) 维护；实验与决策写入 [devlog.md](../../dev/devlog.md)。

正式 Kimi `profile: kimi` 经 `GatewayClient._build_generative` 进入 `OpenAIVisionModel`。直接返回解析 JSON 的调用和携带视觉工具的调用分别在 `gateway/openai/vision.py`、`gateway/agent_model/multimodal.py` 固定发送 `stream=False`。用户期待沿用历史成功实验的流式方式，但正式链路尚未接入。

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

顺序：先提交本 TRD 初稿，再实现最小共用协议收集与工厂接入，先以真实 SDK + 模拟 HTTP SSE 验证，再进行有界模型验证。不会以文档中的试点计划扩大真实素材调用授权。

# 4. 对照实验与结果分析

计划对照同一 HTTP fixture 在修改前后的行为：完整 JSON 分片、末尾空 choices usage、工具参数与 reasoning 分片、多工具索引、EOF、HTTP/读取错误、length/content_filter、取消及超时。通过 Gateway 工厂构造正式 Kimi 模型验证实际路由；运行现有 OpenAI media 与相关 Gateway 测试覆盖非 Kimi 行为。

| 指标 | 基线实测 | 方案实测 | 判断 |
| --- | --- | --- | --- |
| 正式请求 `stream` | `False`（源码） | 待测 | 待实施 |
| 模拟完整与异常流合同 | 尚无 Kimi SSE 覆盖 | 待测 | 待实施 |
| 真实 0–63 秒 Chunk 完成 | 3 次非流式均 504，参数不同，不能合并为成功率估计 | 待授权实测 | 未验证 |

执行后记录版本、命令、测试数量与证据。若只有模拟验证通过，则仅宣称接入和协议合同已验证，不宣称正式素材 504 已解决。

# 5. 试点验证与方案取舍

试点限定单个 Chunk，不自动启动整集、不自动重试；先保持模型、FPS、Schema、reasoning 配置一致，以终止事件、Schema 通过、耗时和 usage 作为观测项。执行者为当前项目维护者/Agent，具体真实素材调用按已有或新增明确授权执行。

若仍在首个事件之前 504，保存证据并进一步区分上游排队/视觉处理和输出传输，不归因于网络不稳定。若流正常结束但 Schema 失败，按模型输出问题处理。回退仅撤销 Gateway 流式选择，不改变持久任务或已提交数据。

# 6. 结果验收与闭环

初稿状态：实现、自动化测试、真实素材效果与部署均未完成。下一步是协议收集与正式工厂入口实施。交付时分别记录接入、测试、真实模型和部署结论；真实 Chunk 尚未验证前，原问题保留 TODO，不标记解决。
