# Gateway 统一模型记录 TRD

状态：代码与 Gateway 离线验收已交付，真实项目验收待提供环境。
版本：G01-D5。
日期：2026-10-07。
当前阶段：G05B。
当前 Langfuse 授权：用户于 2026-10-07 指示“直接做完整的”，覆盖已展示的 Gateway 十八文件、Server 十文件和设计同步。
本批执行代码、依赖解析和离线验收；真实后端仍需专用项目。
本批不调用付费模型，不执行部署、服务启停、存储操作或提交文件。
V1 退役完成后的 commit/push 授权仍属于独立退役任务。

本文采用中文短句、固定术语和单句单要求。
本文以约 80% ASD-STE100 风格为写作目标。
本文不声明 ASD-STE100 合规。

<a id="scope"></a>
## 1. 目标与职责

GW-S01：Gateway 应统一可观测模型调用的记录边界。
GW-S02：Gateway 应传递逐次调用上下文。
GW-S03：Gateway 应向追踪后端发送调用记录。
GW-S04：Gateway 应保持各 Adapter 的输入、输出和错误语义。
GW-S05：Gateway 应保留宿主提供的调用控制接口。
GW-S06：本次观测重构不得改变现有预算控制规则。

Server 拥有业务身份、持久化账本、CNY 价格快照和 admission 规则。
Agent 拥有 Prompt 和执行编排。
Engine 拥有领域 Schema、索引和检索策略。
基础设施责任方拥有追踪后端的部署和维护。

本次不开发新的用量页面。
本次不默认删除历史用量接口。
本次不迁移或清理历史账本。
本次不改变模型选择、重试次数或 fallback 策略。

现有层级以 [Gateway README](../../gateway/README.md) 为准。
现有记录入口位于 [usage.py](../../gateway/usage.py)。
G01 的文档生成本身不代表设计通过；本次用户明确实施指令提供范围签收依据。

<a id="call-record"></a>
## 2. 调用记录

| 术语 | 含义 |
| --- | --- |
| 调用记录 | 一个可观测调用边界产生的事实记录 |
| Provider 请求 | Gateway 可直接观测的单次 Provider 请求 |
| SDK turn/run | 原生 SDK 暴露的外层执行或聚合结果 |
| usage | Provider 或 SDK 实际返回的计数 |
| unknown | 当前缺少可使用的观测值 |
| observer | 接收已结束调用记录的订阅者 |
| 追踪 | 用 trace/span 关联调用和执行上下文 |
| 账本 | Server 持久化的业务用量记录 |

现有 `ModelCallTracker` 已提供逐次 `call_id`。
现有记录包含 Provider、模型、操作、请求身份、状态和 usage。
现有 `TokenUsage` 校验总量和子集关系。
这些事实尚不能证明所有模型路径均已覆盖。
本版记录已补结束时间、观测粒度、usage 来源和中立调用上下文。
下列字段和行为构成当前合同；运行证据见索引。

GW-R01：应复用现有逐次记录组件。
GW-R02：不得为统一记录引入大模型父类。
GW-R03：不得在共享模型实例上保存 `last_usage`。
GW-R04：每个可观测调用应有独立 `call_id`。
GW-R05：调用记录应标明实际观测粒度。
GW-R06：观测粒度应区分 `provider_request` 与 `sdk_turn_run`。
GW-R07：应只增加当前调用方需要的能力字段。
GW-R08：上层 span 数不得作为内部 inference 次数。

GW-R09：Adapter 应在可观测请求结束时发送一次终态记录。
GW-R10：重复结束操作不得发送第二次终态记录。
GW-R11：重试产生的新 Provider 请求应使用新 `call_id`。
GW-R12：fallback 产生的新 Provider 请求应使用新 `call_id`。
GW-R13：记录应保留本次请求实际使用的 Provider 和模型。
GW-R14：本地校验拒绝不得记录 Provider usage。
GW-R15：直接 Provider 请求在 admission 拒绝时不得生成请求记录。

GW-R16：应在业务 JSON 校验前捕获已返回的 usage。
GW-R17：业务输出无效时应保留已返回的 usage。
GW-R18：`failed` 状态不得清空已捕获的 usage。
GW-R19：unknown 不得转换为零。
GW-R20：不得用 tokenizer 或文本长度填补 unknown。
GW-R21：缺失的缓存、推理或模态计数应保持 unknown。
GW-R22：无效计数应保持 unknown。
GW-R23：只在返回值明确可用时记录零计数。
GW-R24：记录应提供真实观测边界的开始和结束时间。
GW-R25：原生 SDK 的外层耗时不得标为内部请求耗时。

取消、流中断和缺少终态事件可能产生 unknown usage。
已返回 usage 与业务成功是两个独立事实。
SDK 未暴露的内部 round 无法精确观测。
调用状态描述本次可观测调用边界的完成或失败。
上层业务校验发生在该边界外时，不得据调用状态推断业务成功。
G03 应把 usage 捕获放在业务校验前。
G03 应在各 Adapter 的验收记录中说明状态边界。

<a id="context"></a>
## 3. 上下文与分发

现有 observer 使用单个 `ContextVar`。
内层 `capture_model_usage` 会替换外层 observer。
本版保留默认替换行为，并用显式 fanout 或 combine=True 组合账本与追踪。

GW-C01：逐次调用上下文应与模型实例分离。
GW-C02：Server 应在执行 scope 提供业务身份。
GW-C03：Gateway 应把业务身份视为不透明关联数据。
GW-C04：Gateway 不得据此判断业务归属或任务状态。
GW-C05：记录开始时应固定本次调用所需的上下文。
GW-C06：并发执行不得修改另一个调用的上下文。
GW-C07：scope 退出应恢复进入前的上下文。

GW-C08：账本 observer 与追踪 observer 应显式 fanout。
GW-C09：一个 observer 失败不得阻止另一个 observer。
GW-C10：observer 失败不得触发模型重试。
GW-C11：observer 失败不得改变模型返回值。
GW-C12：同一执行 scope 的注册责任方应显式组合必需的 observer。
GW-C13：`capture_model_usage` 应保留内层替换和退出恢复的兼容语义。

当前合同：fanout_model_usage 或显式 combine=True；既有 capture 默认不自动叠加。
组合分发应逐支隔离普通异常。
账本分支仍只接收一次终态记录。
追踪分支可以接收开始钩子和对应的终态记录。
嵌套 capture 不自动继承外层分支。
宿主改接属于 [S1 交接](#server-handoff)。

Server 使用 `asyncio.to_thread` 执行同步操作，本版按实际模型阶段绑定上下文。
该路径会复制当前 context。
Chat 当前使用普通 `ThreadPoolExecutor.submit`。
本版 Chat 已在 worker 入口绑定执行上下文。
普通 executor、线程和进程不能据此视为自动传播。
GW-C14：新增线程路径应显式验证上下文传播。
GW-C15：进程边界应由宿主传递序列化的关联数据。

业务上下文示例包括任务身份、执行身份和 trace 关联身份。
这些字段的来源和有效性由 Server 确定。

<a id="adapters"></a>
## 4. Adapter 覆盖

以下内容只来自源码核对。

| 路径 | 当前记录边界 | 当前限制 |
| --- | --- | --- |
| Ark 多模态、Agent、Embedding、SSE | 多数直接请求已有 tracker | 仍需逐入口和异常路径核验 |
| OpenAI 多模态、Vision、Audio、Agent | 多数直接请求已有 tracker | structured Query 走另一条实现 |
| Ollama、llama.cpp | 直接调用已有 tracker | 缺少 usage 时仍为 unknown |
| LangChain structured Query | payload 本地构造成功后逐请求进入 tracker | 主请求、repair、fallback 独立记录；原始 usage 在结果校验前捕获 |
| Codex | 一个外层 invocation 记录最终已返回 turn 的聚合 usage | invocation 可含多个 turn；业务失败保留已返回 total；内部 inference 不逐请求记录 |
| Claude Agent SDK | feedback round/CLI 结果聚合，明确 sdk_turn_run | ResultMessage usage 不能还原每次内部请求 |

直接请求参考 [Ark 多模态](../../gateway/ark/multimodal.py)、[OpenAI Vision](../../gateway/openai/vision.py) 和 [SSE](../../gateway/ark/sse.py)。
structured Query 参考 [structured.py](../../gateway/openai/structured.py)。
原生路径参考 [Codex Adapter](../../gateway/codex/agent.py)、[Codex client](../../gateway/codex/client.py) 和 [Claude Adapter](../../gateway/claude_code/agent.py)。

Codex 当前在 `executor.submit` 后进入 tracker。
Claude 当前在 `client.query` 后进入 tracker。
因此不能承诺所有路径均在调用前完成 admission。
因此不能承诺取消一定停止 Provider 计费。
这两项是静态时序限制。

Codex structured 路径在工作线程中先取得 turn，再校验业务 JSON。
本版用每次 invocation 的局部已返回 turn 在 finally 捕获 usage。
业务 JSON 无效或回调失败时，已返回的 usage 保留。
该修复按 [G03B](#g03b) 直接回归验证。
Codex 当前读取 `ThreadTokenUsage.total`。
该值不得按可见 turn 数重复累加。

Claude 当前先捕获 ResultMessage usage，再检查 SDK result error。
Claude structured 的最终业务校验发生在 tracker 外。
业务校验失败可以同时存在 usage 和 `completed` 调用状态。
这不证明业务执行成功。

GW-A01：G03 应核对所有公开模型入口的可观测粒度。
GW-A02：直接 Provider 请求应使用统一记录组件。
GW-A03：structured Query 应在实际模型调用边界接入记录。
GW-A04：Query 修复、重试和 fallback 应分别记录可见请求。
GW-A05：SSE helper 与外层 Adapter 不得重复记录同一次请求。
GW-A06：原生 SDK 应按实际可见的 turn/run 记录。
GW-A07：原生 SDK 应保留聚合 usage 的来源。
GW-A08：隐藏 round 应明确标为不可逐请求观测。

TODO：Gateway Adapter 负责人应核对外层启动与 tracker 的时序。
TODO：Server 负责人应核对该时序与 admission 规则的关系。
时序修复属于独立执行控制任务，需单独核对范围。
SDK 未暴露的内部请求和取消计费边界由 SDK 与宿主责任方评审。

<a id="tracing"></a>
## 5. 追踪方案

已确认：追踪平台采用 Langfuse。
决定来源：用户于 2026-10-07 确认“那就用langfuse吧”。
当前接入：私有 OpenTelemetry TracerProvider 手工创建 span，并发送到 Langfuse 官方 OTLP HTTP 入口。
当前出口：有界内存队列和本地 IPC；网络导出由独立进程执行。
Langfuse Python SDK 仅用于 G04A 隔离核验，不加入生产依赖。
平台选择已关闭。
用户随后授权按已展示范围完整实施。
真实项目和部署方式仍未交付。

| 方案 | 官方能力与限制 | 本次判断 |
| --- | --- | --- |
| Langfuse | Python SDK 基于 OTel；核心仓库为 MIT，EE 有独立条款 | 用户已选定的追踪平台 |
| OpenTelemetry | 提供 span、context 和 exporter 接口 | 用作接口基础，不单独提供模型 trace UI |
| LangSmith | 提供追踪与评估；自托管为 Enterprise 附加项 | 可选商业后端，暂不推荐引入 |
| Phoenix | 支持 OTel/OpenInference；主仓库采用 ELv2 | 可选后端，需确认许可限制 |

依据：[Langfuse SDK](https://langfuse.com/docs/observability/sdk/overview)、[Langfuse 许可](https://github.com/langfuse/langfuse/blob/main/LICENSE)、[OTel Python](https://opentelemetry.io/docs/languages/python/instrumentation/)。
对照：[LangSmith 自托管](https://docs.langchain.com/langsmith/self-hosted)、[Phoenix 追踪](https://arize.com/docs/phoenix/tracing/concepts-tracing/how-does-tracing-work)、[Phoenix 许可](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)。

Langfuse OSS 的核心功能不收软件许可费。
自托管的服务器、存储、备份和维护费用由部署方承担。
Langfuse Cloud 提供免费套餐和付费套餐。
部分 Enterprise 功能需要商业许可。
费用依据：[官方价格](https://langfuse.com/pricing)、[自托管许可](https://langfuse.com/self-hosting/license-key)。
费用资料核对日期：2026-10-07。
选择 Langfuse 不构成付费订阅或部署授权。

GW-T01：桥接应消费统一调用记录。
GW-T02：一个调用只应创建一个模型 observation。
GW-T03：同一路径不得同时自动 patch 和手工创建模型记录。
GW-T04：配置应允许关闭追踪导出。
GW-T05：关闭导出不得关闭 Server 账本 observer。
GW-T06：exporter 故障不得改变模型结果。
GW-T07：缓冲、导出和关闭等待的上限应由阻塞实验验证。
GW-T08：短生命周期入口应使用已验证的有界关闭机制。
GW-T09：关闭失败应留下可诊断状态。
GW-T10：默认不得导出原始正文、视频、密钥或连接凭据。
GW-T11：载荷导出策略应在启用前明确确认。
GW-T12：实际消费的追踪依赖应在实施前锁定。

### 5.1 依赖基线与候选组合

项目使用 Python 3.12。
当前直接依赖包括 `openai-codex==0.156.1` 和 `claude-agent-sdk==0.2.158`。
实施前锁文件包含 `opentelemetry-api==1.44.0`。
该 API 原由 Claude SDK 的 MCP 依赖传递引入。
当前锁文件还包含 LangChain 的 `langsmith==0.12.4` 传递依赖。
传递依赖存在不证明 Gateway 已接入任一追踪后端。

2026-10-07 核验的候选为 Langfuse Python SDK `4.17.0` 与 OTel `1.45.0`。
Langfuse `4.17.0` 要求 OTel API、SDK 和 OTLP HTTP exporter 至少为 `1.45.0`。
采用该候选时，现有 OTel API `1.44.0` 必须升级。
该 SDK 组合已在隔离环境解析并安装。
SDK 默认关闭路径没有满足总截止要求，正式项目只声明实际消费的 OTel 组合。
固定版本依据：[Langfuse v4.17.0 依赖声明](https://github.com/langfuse/langfuse-python/blob/v4.17.0/pyproject.toml)。

### 5.2 observation 生命周期与字段

桥接应在调用开始钩子中执行 OTel `start_span`。
直接请求的开始钩子应位于 admission 通过后、SDK 请求前。
原生 SDK 的开始钩子应使用其真实可见边界。
桥接不得把原生路径当前的 tracker 时序写成请求前拦截。
OTel `start_span` 接受实际 `start_time`，单位为纳秒。
桥接不得等到终态回调才创建 observation，再伪造其开始时间。

开始钩子应返回固定本次 span 句柄的终态闭包。
终态分发应更新该句柄的状态、模型和 usage。
终态分发应调用 `span.end(end_time=...)` 一次。
OTel 结束时间应从记录的实际结束时间转换为纳秒。
桥接不得在终态分发中创建第二个模型 observation。
开始、更新或结束失败不得改变模型结果。
生命周期依据：[OTel Python](https://opentelemetry.io/docs/languages/python/instrumentation/)、[Langfuse OTLP 与属性映射](https://langfuse.com/integrations/native/opentelemetry)。

标准 usage 字段应来自已归一化的实际计数。
unknown 字段应省略。
标准计费桶应互斥。
桥接不得把 input 总量和其缓存、音频或图像子集同时写成相加的计费桶。
桥接不得把 output 总量和其 reasoning 子集同时写成相加的计费桶。
未形成互斥拆分的子集计数应保存在 metadata。
metadata 应说明这些计数属于 input 或 output 的子集。
本轮不引入推测的价格或用量估算。
字段依据：[Langfuse 用量与费用](https://langfuse.com/docs/observability/features/token-and-cost-tracking)。

### 5.3 关闭机制前置门禁

Langfuse `4.17.0` 的 flush/shutdown 未提供进程退出的 hard bound。
OTel `1.45.0` 的相关 `force_flush` 路径未使用其 timeout 参数。
Langfuse 资源管理器还存在队列等待、线程等待和 atexit 路径。
设置 timeout 或在外层等待超时，不足以证明进程能按时退出。
当前不得宣称 GW-T07 或 GW-T08 已由 SDK 满足。

[G04A](#g04a) 应先验证候选组合与阻塞关闭场景。
G04A 未通过时，不得启动 [G04B](#g04b) 的生产桥接和生命周期实现。
阻塞实验应覆盖 exporter 阻塞、flush、shutdown 和 atexit。
实验应区分调用返回上限与进程退出上限。
测试父进程强制终止子进程只用于实验清理。
该强制终止不能代替产品关闭机制的验收。

若候选 SDK 不能满足上限，责任方应报告实际限制。
本次在同一 tracing.py 候选文件内采用直接 OTLP 与隔离网络进程。
私有 provider 不注册退出钩子，SimpleSpanProcessor 仅做有界本地入队，不在模型线程执行 HTTP。
关闭按配置截止分配收尾预算，先停本地写入，再等待出口排空；超时尝试停止并回收网络进程。
OS 调度可能使墙钟略超配置；资源仍未退出时须记录错误与退出状态。
配置截止不声明所有运行环境下的绝对进程退出保证。
产品强制停止属于本次选定机制，必须记录 forced 和未确认送达，不能声明可靠送达。
这与实验父进程为了清理 SDK 卡死而强杀的证据不同。
若备选扩大职责或文件范围，应重新展示精确修改树并取得开工授权。
关闭依据：[Langfuse resource_manager.py](https://github.com/langfuse/langfuse-python/blob/v4.17.0/langfuse/_client/resource_manager.py)、[OTel v1.45.0 批处理实现](https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.0/opentelemetry-sdk/src/opentelemetry/sdk/_shared_internal/__init__.py)。

Langfuse 自托管需要 Web、Worker、PostgreSQL、ClickHouse、Redis/Valkey 和对象存储。
部署成本与依赖依据 [官方架构](https://langfuse.com/self-hosting)。
Cloud 与 self-host 的选择只阻塞真实后端验收。
该选择不阻塞职责内的离线替身验收。
正式依赖和出口实现按 G04B 锁定与验收，不加入未消费的 Langfuse SDK。

<a id="server-handoff"></a>
## 6. Server 交接

Server 继续拥有 V2 业务身份和执行身份。
Server 继续拥有 lease、admission、预算和取消规则。
Server 继续持久化调用和尝试记录。
Server 账本可以因记录故障处于 partial 状态。
追踪导出不能替代账本持久化。

GW-H01：交接应提供逐次 `call_id` 和已返回 usage。
GW-H02：Server 应继续按 `call_id` 幂等保存记录。
GW-H03：Server 应继续保存调用时的 CNY 价格快照。
GW-H04：Server 应保留 unknown、partial 和缺少价格的区别。
GW-H05：业务校验失败应保留该请求的有效 usage 和费用依据。
GW-H06：记录失败应保留已有的模型结果保护行为。

Langfuse 标准 `cost_details` 使用 USD。
CNY 金额不得写入该标准字段。
币种依据 [Langfuse 成本说明](https://langfuse.com/docs/observability/features/token-and-cost-tracking)。
需要关联 CNY 时，应使用明确币种的 metadata 或账本引用。
后端推断价格不得覆盖 Server 的 CNY 快照。

现有 Server 依据：[用量服务](../../server/service/parse_usage.py)、[用量仓储](../../server/repository/parse_usage.py)、[调用控制](../../server/service/video_parse_model_calls.py)。
现有 Parse wrapper 位于 [旧 usage 模块](../../engine/parsev1/usage.py)。
TODO：Server 应接收中立调用上下文，迁出 Parse wrapper 的业务 scope。
这项共享依赖迁出沿独立 V1 退役修改树核对，不包含在下列 S1 十文件接线候选中。
历史 usage API 的处置沿用独立 V1 退役任务的范围核对。
V2 用量明细查询和产品总览仍是既有缺口。
这两项不构成本 TRD 的新增界面目标。
TODO：Server 应单独完成 V2 账本与 Gateway 事件的直接联验。

S1：本轮已按用户完整实施指令分配 Server Agent，完成下列十文件宿主接线。
Gateway 和 Server 的层级职责保持独立。
普通 Server 测试收集被既有 Engine 缺失导出阻断。
隔离回归的结果与剩余合同失败见证据索引，不能据此登记整链通过。

| Server 实施文件 | 已实施接线 | 影响与直接验证 |
| --- | --- | --- |
| `server/bootstrap.py` | 初始化并注入进程内桥接；由 ApplicationServices 关闭；向 `_LazyQueryEmbedder` 提供 scope | 活动模型调用退出后关闭追踪；HTTP 检索 Embedding 不漏记录；对应 `tests/server/test_bootstrap.py` |
| `server/service/parse_usage.py` | 在账本 observer 的拥有位置显式组合追踪分支 | 保留 recording_complete、partial 和 CNY 快照；对应 `tests/server/test_parse_usage.py` |
| `server/service/video_parse.py` | 在 `_model_stage` 绑定执行身份和追踪 scope | `asyncio.to_thread` 继承本次上下文；不改 admission、lease 或 heartbeat；对应 `tests/server/test_video_parse_worker.py` |
| `server/service/agent_chat.py` | 在 `_execute` 的 worker 入口绑定 conversation/message/workspace 身份 | 不依赖普通 executor 自动传播；对应 `tests/server/test_agent_chat_service.py` |
| `server/service/video_retrieval.py` | 在检索 Service 拥有 workspace、series 和 task 身份的位置建立业务 scope | Query Embedding 关联实际检索身份；lexical-only 不产生模型调用记录；仅 lazy embedder 接线不足以提供这些身份；对应 `tests/server/test_video_retrieval_service.py` |

API lifespan 和手动诊断 `tests/manual/server_mq_once.py` 已调用 `ApplicationServices.close`。
基础关闭接线不要求先改这两个入口。
`gateway/cli.py` 的 Embedding 冒烟已迁为 `tests/manual/gateway_embedding.py`。
手动测试脚本和保留的 Server 管理 CLI 不属于服务端核心接入的前置条件。
本版不为这些工具新增追踪宿主或专用 CLI 测试。
工具调用覆盖应按独立验收目标核对。
S1 接线代码已实施，完整验收仍需宿主身份、账本、追踪和关闭顺序的直接联验。
S1 剩余条件不阻塞 Gateway 离线验收。
S1 验收未通过时，Gateway 模块通过不得写成整链通过。

<a id="acceptance"></a>
## 7. 验收

离线替身验收应使用模拟 Adapter、内存 observer 和模拟 exporter。

| 用例 | 必须观察的结果 |
| --- | --- |
| 直接请求成功 | 一次请求产生一条终态记录 |
| retry | 每个可见重试请求保留独立身份 |
| fallback | 切换后的可见请求保留独立身份和实际模型 |
| 直接请求的本地校验或 admission 拒绝 | 无 Provider usage 记录 |
| 业务输出无效 | 已返回 usage 保留；按实际调用边界解释状态，不推断上层业务成功 |
| cancel、流关闭、终态缺失 | 不完整 usage 保持 unknown |
| 并发与嵌套 scope | 业务身份隔离，必需 observer 不丢失 |
| fanout 单支故障 | 其他分支和模型结果保持正常 |
| 禁用 exporter | 无后端导出，账本 observer 仍收到记录 |
| exporter 阻塞或关闭失败 | G04A 证明关闭调用和进程退出的上限；未证明时禁止 G04B 开工 |
| 原生 SDK 聚合 | 明确粒度，未知内部 round 不被虚构 |

真实后端验收应使用专用追踪项目。
真实后端验收应核对层级、字段、终态、去重和有界关闭。
真实后端验收可使用模拟模型事件。
后端位置、授权、凭据和保留策略未确认时不得宣称通过。

付费语义验收应单独获得模型调用授权。
付费语义验收应先确认输入、模型和金额预算。
调用次数上限不等于金额硬上限。
付费语义验收应验证 Provider 实际返回值的映射。

Server 真实账本联验属于外部 TODO。
历史 Parse 测试通过数量不能替代本次 Gateway 验收。
本轮执行离线替身、原始 OTLP 与本地 HTTP 出口验证；真实后端验收未执行。

<a id="plan"></a>
## 8. 阶段计划

保留 G01 至 G05 的父阶段身份。
G03 拆为 G03A 和 G03B。
G04 拆为 G04A 和 G04B。
G05 拆为 G05A 和 G05B。
原 G02 → G04 的整体依赖在本版分解。
G04A 的独立兼容与关闭原型不依赖 G02 的最终记录字段。

开发依赖：G01 → G02 与 G04A 并行。
开发依赖：G02 → G03A 与 G03B 并行。
开发依赖：G02 与 G04A 均通过后 → G04B。
验收依赖：G03A、G03B 与 G04B 均通过后 → G05A → G05B。
G05B 的真实项目验收另需 S2。
Server 整链通过另需 S1。

G01 已交付设计草案和地图。
Langfuse 平台选择已确认。
G01 已由本次明确实施指令签收。
G02、G03A、G03B、G04A、G04B 已实施并直接验证。
G05A 的离线组合证据见索引。
当前 G05B 等待专用项目，Server 普通回归仍受外部 Engine 缺口限制。

| 父阶段 | 子阶段与交付目标 | 当前状态 |
| --- | --- | --- |
| G01 | 确定职责、合同、追踪平台和验收边界 | 已签收；用户授权完整实施 |
| G02 | 统一调用记录、上下文和 observer 分发 | 已实施；公共合同直接回归通过 |
| G03 | G03A structured Query；G03B 原生 SDK 聚合与 usage 保留 | 已实施；两支 Adapter 离线回归通过 |
| G04 | G04A SDK 兼容与关闭机制验证；G04B 桥接、配置与生命周期 | 已验证并实施；采用隔离 OTLP 出口 |
| G05 | G05A Gateway 离线验收；G05B 真实追踪项目与交接 | 离线证据已交付；真实项目与 Server 普通联验未通过 |

以下文件为本次已获授权的实施范围。
实现、原型、离线测试与依赖解析沿用户完整实施指令执行。
Server 十文件接线也已纳入本轮并交由 Server Agent 实施。
Server 普通联验仍受 Engine 缺口限制；真实后端和部署条件按 S2 交付。

<a id="g01"></a>
### G01：设计与范围确认 · 已签收

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 让用户审阅统一记录、Langfuse 接入、阶段依赖与外部交接；形成可接手的设计基线 |
| 改动 | `gateway/design/trd.md`、三张 `gateway/report_docs/model-observability-stages*.svg`、对应三个 `.excalidraw` 和 `model-observability-stages-evidence.md`；同步职责、已实施范围和证据；本节点不替代各实现节点 |
| 验收 | 用户按本表和地图节点审阅职责、调用粒度、关闭门禁和验收边界；静态检查证明引用、场景与图文一致；文档生成不等于设计签收 |
| Agent | Gateway 文档负责人；来源为既有设计授权及用户本次完整实施指令；按 [scope](#scope)、[decisions](#decisions) 和本节维护八个成果；据各节点证据更新阶段；环境与付费调用仍分别核对 |

<a id="g02"></a>
### G02：公共记录与上下文 · 已实施／直接回归通过

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 向 Adapter、账本与追踪提供同一逐次记录合同和开始/终态钩子 |
| 改动 | `gateway/usage.py` 与 `tests/gateway/test_usage.py`；补实际结束时间、粒度、usage 来源和不透明上下文；保留旧 capture 替换语义；新增显式 fanout；不改 Server SQL 或预算规则 |
| 验收 | `.venv/bin/pytest tests/gateway/test_usage.py -q`；比较成功、admission 拒绝、业务输出无效、显式组合 scope 的嵌套/并发和单支失败；必需订阅保留，旧 capture 不自动合并外层分支；一调用一身份、一终态，unknown 不变零；普通 executor 传播另按 S1 验证 |
| Agent | Gateway 公共记录负责人；来源为统一记录目标，生产树已由完整实施指令授权；按 [call-record](#call-record)、[context](#context) 实施；固定本次上下文；不引入模型父类或实例 `last_usage`；更新本阶段证据 |

<a id="g03a"></a>
### G03A：structured Query 请求覆盖 · 已实施／直接回归通过

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 让 LangChain structured Query 的实际模型请求进入公共记录，覆盖修复与 fallback |
| 改动 | `gateway/openai/structured.py` 与 `tests/gateway/test_agent_model.py`；在实际 `_generate` 请求边界接入；在结果转换与业务校验前捕获 usage；每个可见新请求使用新 call_id；不改重试次数或 fallback 策略 |
| 验收 | `.venv/bin/pytest tests/gateway/test_agent_model.py -q`；假 client 展示主请求、修复请求、跨 Provider fallback、incomplete 和无效 JSON；记录身份与模型正确，失败仍保留已返回 usage；不用 Agent 外层 span 代替逐请求记录 |
| Agent | Gateway OpenAI Adapter 负责人；来源为 G03 覆盖目标，生产树已由完整实施指令授权；前置 G02；按 [adapters](#adapters) 和 [call-record](#call-record) 实施；优先复用请求转换边界；不同时启用 LangChain 自动模型记录 |

<a id="g03b"></a>
### G03B：原生 SDK 粒度与 usage 保留 · 已实施／直接回归通过

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 让 Codex invocation 与 Claude feedback round 明确记录聚合来源；保留业务校验前已返回的 usage；让 SDK reader thread 的宿主工具调用使用本次上下文 |
| 改动 | `gateway/codex/agent.py`、`gateway/codex/client.py`、`gateway/claude_code/agent.py`、`tests/gateway/test_codex_model.py`、`tests/gateway/test_codex_client.py` 和 `tests/gateway/test_claude_code_model.py`，共六个实施文件；收口 Codex JSON 校验前的 usage 捕获；在 client 的工具回调接线中固定宿主上下文快照；标明 sdk_turn_run；保留控制策略；不虚构内部 inference |
| 验收 | `.venv/bin/pytest tests/gateway/test_codex_model.py tests/gateway/test_codex_client.py tests/gateway/test_claude_code_model.py -q`；假 session/client 展示多 turn、feedback、无效业务结果、取消和缺失终态；并发会话的 reader thread 工具调用保留各自身份；普通 executor 的 context 复制不算 reader thread 传播证据；聚合不重复相加；有效 usage 不因业务失败丢失；当前启动时序限制明确保留 |
| Agent | Gateway 原生 Adapter 负责人；来源为 G03 覆盖目标，生产树已由完整实施指令授权；前置 G02；按 [adapters](#adapters)、[context](#context)、[call-record](#call-record) 实施；在 executor 与 client 的宿主工具回调边界恢复本次快照；不改变工具预算、取消或 admission 策略；若需改变启动/admission 顺序，应另报执行控制树 |

<a id="g04a"></a>
### G04A：SDK 兼容与关闭机制验证 · 已验证／采用隔离 OTLP

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 在生产桥接前证明候选 SDK 可解析、可使用，并验证关闭调用和进程退出的上限 |
| 改动 | 专用临时目录 `/private/tmp/anivag-langfuse-lifecycle-probe/` 内已创建 pyproject.toml、uv.lock、sdk_probe.py、transport_probe.py、report.py、report.json；固定 SDK/OTel/httpx 候选并记录关闭结果；正式依赖仅在 G04B 写回；临时脚本不是产品文件 |
| 验收 | 已核 `langfuse==4.17.0` 与 OTel `1.45.0`；10ms SDK flush 请求实际约405ms；隔离出口正常/积压自然排空、堵塞时在截止内由产品机制强制关闭且资源退出；报告区分明确丢弃和未确认送达；实验清理不能算 SDK 总截止通过；正式机制还需 G04B 直接测试 |
| Agent | Gateway 追踪负责人；来源为本次完整实施授权；前置 G01，不依赖 G02 最终字段；按 [tracing §5.1–5.3](#tracing) 核验；选择同一 tracing.py 内的私有 OTel 与隔离 OTLP 出口；扩大生产文件或公共行为时重新核对范围 |

<a id="g04b"></a>
### G04B：Langfuse 桥接、配置与生命周期 · 已实施／直接回归通过

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 把统一调用生命周期导入 Langfuse，并为进程宿主提供配置、scope 与已验证关闭入口 |
| 改动 | 已新增 `gateway/tracing.py`、`tests/gateway/test_tracing.py`；修改 `gateway/config.py`、`gateway/README.md`、`tests/gateway/test_config.py`、`pyproject.toml` 与 `uv.lock`；桥接初始化、start_span、终态属性 update/end、字段映射、禁用分支和生命周期入口集中在 tracing.py；README 说明配置与宿主职责；旧 YAML 默认关闭；手动冒烟与管理 CLI 不计入本阶段核心树；本地 HTTP/生命周期测试复用 G05B 验证文件 |
| 验收 | `.venv/bin/pytest tests/gateway/test_config.py tests/gateway/test_tracing.py tests/gateway/test_langfuse_integration.py -q`；内存 exporter、原始 OTLP 和本地 HTTP 接收器展示一次创建和结束、unknown、标准 usage 与 metadata 子集、禁用与故障；生命周期实现复核 G04A 的阻塞与进程退出结果；Server 初始化和关闭顺序按 S1 独立联验 |
| Agent | Gateway 追踪负责人；来源为 Langfuse 已确认选择，生产树已由完整实施指令授权；前置 G02 与 G04A 均通过；按 [tracing](#tracing)、[server-handoff](#server-handoff) 实施；只用手工桥接；不得复制 CNY 价格计算或自动写宿主业务身份；S1/S2 留在外部交接 |

<a id="g05a"></a>
### G05A：Gateway 离线验收 · 已执行／证据见索引

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 让用户据可复现的离线证据判定 Gateway 公共记录、Adapter 覆盖和追踪桥接是否模块通过 |
| 改动 | 汇总前阶段直接回归；核验 `tests/gateway/test_usage.py`、`test_agent_model.py`、`test_codex_model.py`、`test_codex_client.py`、`test_claude_code_model.py`、`test_openai_media.py`、`test_video_embedding.py`、`test_models.py`、`test_sse_proxy.py` 及 G04B 的两个直接测试；同步既有 TRD、地图、场景和证据索引；不因失败自动扩大生产树 |
| 验收 | 逐项覆盖 [acceptance](#acceptance) 的离线表；记录每条实际命令和结果；对未覆盖的公开入口列出具体缺口；直接请求与 sdk_turn_run 均正确；SDK 阻塞关闭实验仍有效；仅静态检查或测试总数不能代替覆盖证据 |
| Agent | Gateway 验收负责人；来源为 G05 模块验收目标；前置 G03A、G03B 与 G04B 均通过；按 [acceptance](#acceptance)、[evidence](#evidence) 复核；只运行已授权替身测试；真实模型、Server 账本和后端联验分别登记 |

<a id="g05b"></a>
### G05B：真实追踪项目与交接 · 当前／真实项目待提供

| 节点项 | 执行依据 |
| --- | --- |
| 功能 | 在专用 Langfuse 项目确认导出结果，并交付 Gateway 模块结论与 S1/S2 剩余条件 |
| 改动 | 已新增 `tests/gateway/test_langfuse_integration.py`；包含本地原始 OTLP/HTTP/生命周期回归和默认跳过的真实项目入口；维护 `gateway/README.md` 的后端验收与宿主交接说明；同步既有 TRD、地图、场景和证据索引；Server 十文件接线已实施；部署和测试项目由 S2 提供；不修改 Engine 策略 |
| 验收 | 前置 G05A 与已授权 S2 环境；用模拟模型事件导出并核对层级、call_id、粒度、usage、终态和去重；记录实际关闭与退出结果；不以传输成功代替字段验收；Server 联验未交付时，仅登记 Gateway 与追踪项目通过 |
| Agent | Gateway 验收负责人承担自身导出验证与交接；Server/部署负责人承担 S1/S2；来源为 G05 交接目标；按 [acceptance](#acceptance)、[server-handoff](#server-handoff) 执行；Cloud/自托管与保留策略在真实接入前收敛；付费模型调用另获预算授权；外部 TODO 未完成不得宣称整链通过 |

S2：部署负责人尚未指定。
S2 应提供后端位置、专用项目、凭据、保留策略和环境操作授权。
Cloud 或自托管未选择时，G05B 的真实项目验收未具备条件。
该未决项不阻塞已有授权内的 G01 文档维护。
该未决项也不自动禁止职责内已获授权的离线原型或实现。

地图：[主图](../../gateway/report_docs/model-observability-stages.svg)、[源场景](../../gateway/report_docs/model-observability-stages.excalidraw)。
分图：[G01–G03](../../gateway/report_docs/model-observability-stages-01-03.svg)、[源场景](../../gateway/report_docs/model-observability-stages-01-03.excalidraw)。
分图：[G04–G05](../../gateway/report_docs/model-observability-stages-04-05.svg)、[源场景](../../gateway/report_docs/model-observability-stages-04-05.excalidraw)。

<a id="decisions"></a>
## 9. 决定、待决项与实施核验

D01 已确认：追踪平台采用 Langfuse。
用户已对已展示的合同与核心修改范围发出完整实施指令。
D02：真实后端验收前应选择 Cloud 或 self-host。
D03：真实后端验收前应确认数据保留和载荷导出策略。
G02 应保留现有 observer 入口的替换语义，并验证显式 fanout。
G04A 应验证候选 SDK/OTel 组合和关闭机制。
G04B 应只使用已通过 G04A 的组合和机制。
这些项属于责任内实施核验，默认不要求用户选择内部命名或方法。
若发现接口不兼容或显著成本变化，应在 check-out 中报告。
Server 用量页面和历史 API 沿用独立退役任务的范围核对。
本次代码实施授权不自动授权部署、订阅或付费模型调用。

<a id="evidence"></a>
## 10. 证据与维护

证据索引：[model-observability-stages-evidence.md](../../gateway/report_docs/model-observability-stages-evidence.md)。
现有直接回归依据：[Gateway usage tests](../../tests/gateway/test_usage.py)、[Server usage tests](../../tests/server/test_parse_usage.py)。
本轮已运行 Gateway 离线测试与 Server 隔离回归，具体命令、数量和限制见证据索引。

维护阶段地图时应同步主图、分图、源场景和证据索引。
证据应区分源码核对、离线替身、真实后端和付费语义。
证据应记录实际命令、范围、结果和限制。
阶段标记应依据阶段完成条件更新。
