# Gateway 模型记录与追踪：阶段证据索引

日期：2026-10-07。
工作区：`main@0950a68`，含既有 tracked、untracked 和 ignored 成果。
当前阶段：**G05B 真实追踪项目与交接**。
当前设计版本：G01-D5。G03、G04、G05 各拆为 A/B 子阶段，父身份保留。
阶段状态：Gateway 和 Server 接线代码已实施，Gateway 离线验收已执行。
真实 Langfuse 项目未提供；Server 普通联验受既有 Engine 缺口限制。
当前实施证据见 §9；§5 至 §8 保留此前各批的历史状态，不作为当前阶段结论。

文档维护授权来自用户指定 `gateway/design/trd.md` 并维护目标、方案和 plan-map 的指令。
平台决定来自用户于 2026-10-07 确认“那就用langfuse吧”。
当前实施授权来自用户对已展示完整修改树指示“直接做完整的”。
覆盖 Gateway 十八文件、Server 十文件和既有八个设计文件，共三十六文件。
本批已完成代码、依赖同步、离线验收及设计同步；真实项目验收保持待提供环境。
用户随后明确授权迁移两个手动测试 CLI；该独立重构已执行，结果见 §8。
入口迁移属于前一批；本批 Langfuse 实施依据后续明确执行授权。
V1 退役与其完成后的 commit/push 沿用独立任务授权。

## 1. 有效入口

- [TRD](../../docs/design/gateway-model-observability-trd.md)：目标、职责、调用合同、已确认平台和验收边界。
- [主图](model-observability-stages.svg) / [可编辑源场景](model-observability-stages.excalidraw)。
- [G01–G03 分图](model-observability-stages-01-03.svg) / [源场景](model-observability-stages-01-03.excalidraw)。
- [G04–G05 分图](model-observability-stages-04-05.svg) / [源场景](model-observability-stages-04-05.excalidraw)。

每个阶段节点包含功能、改动、用户验收和 Agent 指示。
本索引只提供定位和证据，不复制节点要求。

| 阶段 | 设计依据 | 当前证据与状态 |
| --- | --- | --- |
| G01 | [设计签收](../../docs/design/gateway-model-observability-trd.md#g01) | 用户已明确授权完整实施；边界为已展示 Gateway18 + Server10 + 设计8 |
| G02 | [公共记录](../../docs/design/gateway-model-observability-trd.md#g02) | 已实施；41 passed，包含旧 capture 兼容、fanout、上下文与生命周期 |
| G03A | [structured Query](../../docs/design/gateway-model-observability-trd.md#g03a) | 已实施；主请求、repair、fallback 和失败 usage 直接回归通过 |
| G03B | [原生 SDK](../../docs/design/gateway-model-observability-trd.md#g03b) | 已实施；聚合粒度、失败 usage、reader thread 上下文直接回归通过；G03A/B 合并 153 passed |
| G04A | [兼容与关闭机制](../../docs/design/gateway-model-observability-trd.md#g04a) | 临时 SDK 4.17 关闭候选未通过；私有 OTel 1.45 + 自有 OTLP 进程机制已验证 |
| G04B | [桥接、配置与生命周期](../../docs/design/gateway-model-observability-trd.md#g04b) | 已实施；61 passed、1 skipped，含真实 protobuf、本地 HTTP 和资源收尾 |
| G05A | [离线组合验收](../../docs/design/gateway-model-observability-trd.md#g05a) | 已执行；Gateway 全部离线测试 378 passed、1 skipped；真实 Provider 文件排除 |
| G05B | [真实项目与交接](../../docs/design/gateway-model-observability-trd.md#g05b) | 当前；真实项目测试入口已实现并默认跳过；S1 接线完成但普通联验受阻，S2 项目待提供 |

阶段对应：G03 = G03A + G03B；G04 = G04A + G04B；G05 = G05A + G05B。
细化依赖：G01 → G02 与 G04A 并行；G02 → G03A 与 G03B。
G02 与 G04A 均通过后 → G04B。
G03A、G03B 与 G04B 均通过后 → G05A → G05B。
G04A 的独立兼容/关闭原型不依赖 G02 最终字段。
G05B 真实后端另需 S2；Server 整链验收另需 S1。
阶段实施授权来自用户的完整执行指令。
阶段图不授予代码或环境操作权限。

## 2. 源码核对

以下是当前源码事实；测试证据及其限制见 §9。

| 范围 | 依据 | 可确认事实 |
| --- | --- | --- |
| Gateway 职责 | [README](../README.md) | Gateway 是模型调用边界；业务身份由宿主提供 |
| 公共记录 | [usage.py](../usage.py) | 逐次 tracker、独立 call_id、原始 usage 归一化和终态 observer 已存在 |
| observer | [usage tests](../../tests/gateway/test_usage.py) | 测试源码规定内层 observer 替换外层，退出后恢复；不能用嵌套 capture 代替多路分发 |
| 直接模型请求 | [Ark](../ark/multimodal.py)、[Vision](../openai/vision.py)、[Audio](../openai/audio.py)、[Embedding](../ark/embedding.py) | 多个请求边界已使用 tracker；不代表全部异常路径已验证 |
| structured Query | [structured.py](../openai/structured.py) | 在实际请求边界接 tracker；转换或业务校验前捕获原始 usage；repair/fallback 使用独立 call_id |
| Codex | [Adapter](../codex/agent.py)、[client](../codex/client.py) | 记录外层调用及聚合 usage；无逐内部 inference 的请求前通知 |
| Claude | [Adapter](../claude_code/agent.py) | 记录 feedback round/CLI 返回的聚合 usage；内部请求不可还原 |
| 原生调用时序 | 同上两个 Adapter | Codex 在 submit 后、Claude 在 query 后进入 tracker；观测合同不证明统一调用前拦截 |
| Codex usage 保留 | [Adapter](../codex/agent.py) | invocation 内保存最新累计 usage；业务 JSON 或回调失败仍捕获一次，避免累加重复累计值 |
| 原生工具线程 | [Codex client](../codex/client.py)、[client 测试](../../tests/gateway/test_codex_client.py) | executor 与 reader thread 均恢复 invocation 快照；并发 reader 工具回调各自复制 Context，直接回归已验证 |
| V2 上下文 | [video_parse.py](../../server/service/video_parse.py) | 当前执行 scope 使用 asyncio.to_thread；此线程路径继承当前 context |
| V2 账本 | [服务](../../server/service/parse_usage.py)、[仓储](../../server/repository/parse_usage.py) | 按 call_id 保存调用、执行尝试与价格快照；记录可能不完整 |
| 宿主控制 | [video_parse_model_calls.py](../../server/service/video_parse_model_calls.py) | 调用预算、并发 reservation、续租和取消属于 Server 控制 |
| V1 共享依赖 | [engine.parsev1.usage](../../engine/parsev1/usage.py) | V2 仍使用 Parse scope 和事件包装；不能因本地图直接删除 |

Gateway 已新增 [tracing.py](../tracing.py)，通过 Langfuse 官方 OTLP/HTTP 导出。
正式依赖为 OTel API、SDK 和 proto-common 1.45.0，已解析并同步开发环境。
Langfuse SDK 仅用于临时候选探针，未作为未使用的正式依赖安装。
追踪默认禁用，首次使用才初始化私有 provider 和出口资源。
依赖依据：[pyproject.toml](../../pyproject.toml)、[uv.lock](../../uv.lock)。
本轮运行的离线测试只证明相应合同，不证明真实后端可见性或账本整链。

## 3. 框架依据

资料核对日期：2026-10-07。

- [Langfuse SDK](https://langfuse.com/docs/observability/sdk/overview)：推荐用 SDK 的 OTel 基础接入；实现前核对兼容版本。
- [Langfuse LangChain 集成](https://langfuse.com/integrations/frameworks/langchain)：可接现有 LangChain 路径；不要求重写全部模型接口。
- [Langfuse 用量与费用](https://langfuse.com/docs/observability/features/token-and-cost-tracking)：标准成本字段采用 USD；CNY 账本采用自身明确币种的口径。
- [Langfuse 自托管](https://langfuse.com/self-hosting)：需要应用和多个存储组件；部署方式尚未选择。
- [Langfuse 价格](https://langfuse.com/pricing)：Cloud 有免费和付费套餐；价格按实际订阅时的官方规则核对。
- [Langfuse 自托管许可](https://langfuse.com/self-hosting/license-key)：OSS 核心功能无需软件许可费；部分 Enterprise 功能需要商业许可。
- [OpenTelemetry Trace SDK](https://opentelemetry.io/docs/specs/otel/trace/sdk/)：批量导出存在缓冲、丢弃和关闭边界；追踪不能证明账本完整。
- [LangSmith 自托管](https://docs.langchain.com/langsmith/self-hosted)：自托管属于 Enterprise 方案。
- [Phoenix 许可](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)：当前主仓使用 ELv2。

Langfuse 平台选择已由用户确认，随后已明确授权完整实施。
Cloud 或自托管、数据保留和载荷导出策略仍未确定。
业务桥接和实际消费的 OTel 依赖已实施；真实后端与部署未执行。
出口协议依据 [Langfuse 原生 OpenTelemetry 接入](https://langfuse.com/integrations/native/opentelemetry)。
HTTP 200 的部分拒绝依据 [OTLP partial success](https://opentelemetry.io/docs/specs/otlp/#partial-success-1)，已纳入 failed 计数。

## 4. 外部交接与独立任务

| 编号 | 责任方 | 交付条件 | 阻塞范围 |
| --- | --- | --- | --- |
| S1 | 本轮 Server Agent | 十文件接线已完成；隔离回归 118 passed、1 skipped、2 个既有合同失败；普通收集缺少 Engine 导出 | 普通 Server 联验与整链验收仍受阻；不阻塞 Gateway 离线结论 |
| S2 | 部署负责人，待指定 | 选定 Cloud 或自托管，提供专用测试项目、版本与操作授权 | 真实后端验收；不阻塞离线实现 |
| S3 | 既有 Parse/Server 退役负责人 | 按独立修改树迁出共享 usage 能力，保留有效账本 | V1 退役；不授权本轮删除或移除旧界面 |

Gateway Adapter 负责人核对自身外层启动与 tracker 的时序。
Server 负责人核对该时序与预算规则的关系。
必要的控制修复属于独立执行控制任务。
SDK 隐藏请求和取消计费限制继续明确记录。

Parse V2 地图不受本轮修改。
其当前阶段仍为 08A，尚未通过。
02、07B 和 08B 的既有验收限制不因本次文档交付消失。

## 5. 首次文档交付验证

本轮使用 `@excalidraw/excalidraw@0.18.1` 的官方 `exportToSvg`。
导出工具及其依赖位于临时目录；项目依赖文件保持原状。
场景采用原生文字、矩形和箭头。
SVG 使用矢量图元，不以整图位图代替地图。

交付检查记录在下表。该表只证明文档和地图交付。

| 检查 | 结果与限制 |
| --- | --- |
| TRD 引用与稳定锚点 | 通过；本地引用有效，10 个锚点唯一 |
| 场景 JSON、节点四项与阶段标记 | 通过；3 个场景合法，每个阶段四项齐全；当前标记仅属 G01 |
| SVG XML、矢量内容与实际渲染 | 通过；3 个 SVG 均为矢量，浏览器文本边界无越界；已检查中文、裁切和重叠 |
| 主图、分图、场景和索引一致性 | 通过；对应节点文本、状态、依赖和引用一致；独立接手审阅无新增实质问题 |
| 工作区保护与空白检查 | 检查完成；520 个基线路径中，519 个内容一致；另 1 个外部文件并发变化，保留原状；git diff --check 通过，本组 Markdown 无尾随空白 |

导出命令：`node render.mjs`，在临时导出目录运行。
导出使用 Playwright 浏览器 DOM 和系统字体。
浏览器渲染检查覆盖主图和两张分图。
独立审阅只证明文档可接手，不替代用户设计签收。

最终核对发现 `engine/rag/retrieval/evidence.py` 与本轮开始快照不同。
本轮工具和文档 Agent 未写该文件。
该变化按外部工作区成果保留，不计入本次八个交付文件。
首次静态核对时，520 个基线路径内容均一致。

无本轮产品测试结果。
无本轮真实数据库、真实追踪后端或付费模型证据。
历史 Parse 测试计数不复用为本次 Gateway 验收。

维护状态时，应同时更新 TRD、主图、分图、源场景和本索引。
G01 需用户完成设计评审后才能移走当前标记。
模块通过、Server 联验通过和整体验收通过应分别登记。

## 6. 平台选择更新

日期：2026-10-07。
TRD 版本：G01-D2。
用户决定：采用 Langfuse 追踪平台。
本批只同步 TRD、主图、两张分图、三个源场景和本索引。
本批生产代码改动为 0。
G01 保持当前且未通过。
G02 至 G05 均未执行。
部署方式、付费订阅和环境操作未由平台选择自动授权。
本批使用既有临时工具重新执行 `node render.mjs`。
三个 SVG 均由同版 Excalidraw 场景导出。

| 本批检查 | 结果 |
| --- | --- |
| 平台选择与阶段状态 | 主图、两张分图、源场景和索引一致；Langfuse 已确认，G01 未通过 |
| 节点四项与引用 | 通过；节点正文与导出输入一致，53 个本地引用有效，10 个 TRD 锚点唯一 |
| 场景与 SVG | 通过；3 个场景 JSON 合法，3 个 SVG 为真实矢量导出 |
| 浏览器渲染 | 通过；3 个 SVG 的文字边界无越界；已检查中文、裁切和重叠 |
| 工作区保护 | 通过；526 个基线路径中，仅本批 8 个设计文件变化，其余 518 个内容一致 |
| 空白检查 | git diff --check 通过；本组 Markdown 无尾随空白 |

实际修改树如下。每个 `[M]` 表示修改本批开始时已存在的文件。

```text
Seekino/
└── gateway/
    ├── design/
    │   └── [M] trd.md ← 确认 Langfuse 平台并记录费用边界
    │       影响：关闭平台选择；完整设计和部署方式仍待确认
    └── report_docs/
        ├── [M] model-observability-stages.svg ← 同步主图 G01、G04 和平台状态
        │   影响：当前位置保持 G01，阶段验收不变
        ├── [M] model-observability-stages.excalidraw ← 同步主图源场景
        │   影响：可编辑场景与 SVG 一致
        ├── [M] model-observability-stages-01-03.svg ← 同步 G01 平台决定
        │   影响：合同评审仍待签收
        ├── [M] model-observability-stages-01-03.excalidraw ← 同步分图源场景
        │   影响：可编辑场景与 SVG 一致
        ├── [M] model-observability-stages-04-05.svg ← 同步 G04 平台状态
        │   影响：SDK 和运行接入仍未实施
        ├── [M] model-observability-stages-04-05.excalidraw ← 同步分图源场景
        │   影响：可编辑场景与 SVG 一致
        └── [M] model-observability-stages-evidence.md ← 记录用户决定和本批验证
            影响：后续接手不再重新询问追踪平台
```

本批修改既有设计文件 8 个，新增、删除和重命名均为 0。
生产文件和测试文件改动均为 0。
TRD 仍被既有 design 忽略规则覆盖。
七个地图和索引文件仍为既有 untracked 成果。
因此 git diff --stat/--numstat 不包含本组改动；文件范围采用内容快照核对。
API、数据库 Schema、配置和前端行为均无本批变化。
SDK 安装、服务启停、数据库操作、付费调用和 commit/push 均未执行。

## 7. 具体开发阶段更新

日期：2026-10-07。
TRD 版本：G01-D3。
本批依据：用户要求 plan-map 给出具体开发阶段，并核对接入是否可以一次到位。
本批沿用本组设计文档维护授权。
本批不授予候选生产树、原型安装、环境操作或真实后端验收权限。

旧版 G01–G05 是父阶段骨架。
旧版未在节点内给出 structured、原生 SDK、关停机制和后端验收的独立落点。
本版保留父身份，并将 G03、G04、G05 各拆成 A/B 开发子阶段。
每个节点本体均写功能、具体候选文件、可观察验收和 Agent 职责依据。
当前标记仍为 G01，完整设计仍待签收。
所有实现、原型和运行验收均未执行。

| 父阶段 | 具体开发子阶段 | 阶段通过条件 |
| --- | --- | --- |
| G01 | G01 设计签收 | 用户完成完整设计评审；地图生成不等于通过 |
| G02 | G02 公共记录、上下文和生命周期分发 | 兼容、身份隔离和逐次记录合同均通过 |
| G03 | G03A structured；G03B 原生 SDK 与线程关联 | 两个子阶段均通过；既有直接路径由 G05A 核对覆盖 |
| G04 | G04A SDK/关停机制；G04B 桥接、配置与 CLI | 机制先通过；G04B 同时使用已通过的 G02 合同 |
| G05 | G05A 离线组合；G05B 真实项目与交接 | 分别登记 Gateway 模块、真实后端、Server 联验和整链结论 |

细化后的 G04A 只核 SDK 兼容和资源机制。
它可以在 G01 签收且范围获准后与 G02 并行。
G04B 同时依赖 G02 与 G04A。
G03A、G03B 与 G04B 均通过后，才进入 G05A。
G05B 的真实后端部分另需 S2。
S1 只限制 Server 联验与整链签收。
S3 保持独立 V1 退役任务。

SDK 候选为 Langfuse 4.17.0 与 OTel 1.45.0。
这是声明与源码核验后的候选，尚未经过项目 resolver 或运行验证。
现有 OTel API 1.44.0 不能作为该候选组合已兼容的证据。
G04A 预计使用专用临时隔离原型；原型文件尚未创建。
正式业务依赖只在 G04B 的获准树内写回项目。
候选依据：[固定版本依赖](https://github.com/langfuse/langfuse-python/blob/v4.17.0/pyproject.toml)。

G04A 必须区分关闭入口返回、后台资源退出和进程退出。
阻塞 exporter、连接失败、积压、重复关闭与 atexit 均属于计划验收场景。
Langfuse 和 OTel 内置的 timeout 参数不能直接证明总关闭时限。
监督器强杀实验子进程只算清理，不算机制通过。
若机制失败，G04B 不得开工；备选导出路径先核新的精确树。
源码依据：[Langfuse 关闭实现](https://github.com/langfuse/langfuse-python/blob/v4.17.0/langfuse/_client/resource_manager.py)、[OTel 1.45 批处理实现](https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.0/opentelemetry-sdk/src/opentelemetry/sdk/_shared_internal/__init__.py)。

本批实际修改范围为既有八个设计文件。
TRD 补接入合同和八个阶段表。
主图及两张分图改为具体子阶段，并同步可编辑场景。
本索引补父子对应、最新依赖、源码事实和验收限制。
生产文件、产品测试、业务依赖和环境改动均为 0。
因此目前不能承诺接入简单或一次到位。

本批地图由既有临时工具的官方 `exportToSvg` 重新导出。
命令：`node render.mjs`，工作目录为 `/private/tmp/seekino-observability-map-jexpxd_a/`。
静态核对：`python3 validate-development.py`，在同一临时目录执行。
临时工具与验证脚本不是产品依赖。

| 本批文档检查 | 结果与证明范围 |
| --- | --- |
| 场景与 SVG | 三个 JSON、三个 SVG XML 合法；使用真实矢量导出，无整图 image 元素 |
| 节点与依赖 | 主图八个阶段、分图各四个；每节点四项及 Ref 齐全；G01 当前标记与索引一致 |
| 图文一致 | SVG 文字逐行与源场景一致；主/分图节点与导出输入一致；TRD 与节点候选范围已对齐 |
| 引用与锚点 | 本组本地引用有效；原十个锚点保留，新增八个阶段锚点，共十八个且无重复 |
| 浏览器检查 | 三图文字边界无越界；已检查中文、卡片及依赖线；前半分图的跨节点箭头改为外侧绕行 |
| 工作区保护 | 对比写前内容快照；本批只改八个设计文件，其他核对路径内容一致；暂存区为空 |
| 空白检查 | `git diff --check` 通过；另核 ignored TRD 与 untracked 索引无尾随空白 |

本表只证明设计文件和地图交付。
本轮未运行 Gateway 产品测试、SDK 原型、真实后端或 Server 联验。
未安装业务 SDK、启动服务、迁移数据、调用模型或 commit/push。

## 8. 手动入口迁移与 Langfuse 核心修改树核对

日期：2026-10-07。
TRD 版本：G01-D4。
本批依据：用户要求“先 refactor 一下，一些核心逻辑不会用的 CLI 挪到 test file 里面”，随后汇报 Langfuse 的 refactor file tree check-out。
入口迁移沿用该明确执行授权；Langfuse 部分仍为 G01 设计维护与范围核对。
G01 当前标记保持原位；G02 至 G05 未执行。

### 8.1 已完成的入口迁移

| 实际改动 | 作用 |
| --- | --- |
| `gateway/cli.py` → [tests/manual/gateway_embedding.py](../../tests/manual/gateway_embedding.py) | Embedding 手动冒烟；改绝对导入及命令显示名，保留参数、输出和调用行为 |
| `server/mq/__main__.py` → [tests/manual/server_mq_once.py](../../tests/manual/server_mq_once.py) | 单任务诊断；改绝对导入、命令显示名和拒绝持续模式的提示，保留 run_once_sync 优先、兼容回退及 finally close |
| [根 README](../../README.md)、[Gateway README](../README.md)、[Server README](../../server/README.md) | 更新当前入口与使用示例，区分服务入口和手动测试 |
| [test_bootstrap.py](../../tests/server/test_bootstrap.py) | 既有三条 MQ 入口回归改为新模块导入和 argv |
| [frontend-api.md](../../docs/design/frontend-api.md) | 更新仍有效的 MQ 单任务诊断示例 |

实际入口重构为 2 个移动及 5 个文件的局部修改。
Queue、Consumer、Server 管理 CLI 和 ApplicationServices 生命周期实现未因这次迁移改变。
两个手动模块没有 test_ 前缀，不进入默认 pytest 用例收集。
从仓库根目录运行：

```bash
uv run --env-file .env.local python -m tests.manual.gateway_embedding --help
uv run --env-file .env.local python -m tests.manual.server_mq_once --once
```

以上 MQ 命令是迁移后的使用方式，当前工作区实际导入仍受下表的 Engine 缺口限制。

| 本批验证 | 结果与证明范围 |
| --- | --- |
| Gateway help、embed-query help、embed-video help | 三条命令通过；新模块可运行，参数帮助保持 |
| 迁移前后隔离对照 | 9 个用例通过：query defaults、video flags、环境配置、JSON 文件输出、run_once 回退、run_once_sync 优先、失败仍 close、拒绝持续模式、隔离宿主的 MQ help；无真实 Provider 调用或队列任务 |
| 既有 MQ 三条回归 | `.venv/bin/pytest tests/server/test_bootstrap.py -q -k 'mq_once or mq_entrypoint'` 在收集阶段失败，未执行用例；当前 `engine.rag` 缺少 Server 仍引用的 `reuse_embedding_artifact` |
| 迁移前基线复核 | 写前 `server/mq/__main__.py` 在当前工作区同样报上述 ImportError；没有通过修补 Engine 绕过外部问题 |

隔离对照脚本与写前内容位于 `/private/tmp/seekino-cli-refactor-20261007/`。
复核命令：`env PYTHONPATH=. .venv/bin/python /private/tmp/seekino-cli-refactor-20261007/verify_moved_entrypoints.py`。
隔离验证不等于当前真实 Server 导入或账本联验通过。

### 8.2 当前阶段职责与下一批

本 Agent 当前负责 Gateway 调用记录、Adapter 与 Langfuse 桥接的设计合同；Server 业务身份、账本和可靠执行按 S1 交接。
当前开发方向是先补公共记录和 observer 分发，再覆盖实际请求及 SDK 聚合，最后使用 G04A 已验证的机制实现桥接。
当前可执行授权已完成入口迁移及本组八个设计文件的同步；下一批 Langfuse 生产实现尚未由本次 check-out 授权。
离线设计无关键未决项。Langfuse 平台已选定；Cloud/自托管、专用项目及保留策略在 G05B 真实后端验收前收敛。

首批候选 G02 是 `gateway/usage.py` 与 `tests/gateway/test_usage.py`，共两个既有文件。
完成标准为 ended_at、粒度、上下文快照、开始/终态分发、显式 fanout 与旧 capture 替换/恢复兼容；unknown 保持未知。
G01 签收并进入执行后，G04A 可与 G02 并行；原型仅拟在 `/private/tmp/seekino-langfuse-lifecycle-probe/` 新增 pyproject.toml 和 probe.py，并生成 uv.lock/report.json，尚未创建或安装。
G04B 必须等待 G02 和 G04A 均通过；SDK 阻塞关闭和进程退出上限尚未证明，不能承诺一次到位。

### 8.3 Gateway 完整核心候选树

以下是未来 G02 至 G05B 的完整候选范围，不是本批已实施内容。
共 18 个文件：7 个生产文件、8 个测试、2 个依赖文件和 1 个 README；3 个新增、15 个修改。
按调用合同、三个 Adapter 接点与独立桥接估算，生产代码约增加 300–600 行、删除 40–100 行，测试约增加 350–700 行；依赖锁文件与文档不计入该粗估。
若 G04A 需要不同导出架构，应重新核对树和规模。

```text
Seekino/
├── [M] pyproject.toml ← G04B 声明已验证 SDK/OTel 组合
├── [M] uv.lock ← 锁定同一组合
├── gateway/
│   ├── [M] usage.py ← G02 公共记录、上下文与开始/终态分发
│   ├── [M] config.py ← G04B 默认禁用与兼容配置
│   ├── [A] tracing.py ← G04B 私有 provider、scope、字段映射与生命周期
│   ├── [M] README.md ← 配置、宿主交接和真实验收说明
│   ├── openai/
│   │   └── [M] structured.py ← G03A 主请求、repair、fallback 逐请求记录
│   ├── codex/
│   │   ├── [M] agent.py ← G03B 校验失败仍保留已返回聚合 usage
│   │   └── [M] client.py ← G03B reader thread 的宿主工具上下文
│   └── claude_code/
│       └── [M] agent.py ← G03B feedback round 聚合粒度与 unknown
└── tests/
    └── gateway/
        ├── [M] test_usage.py ← G02 兼容、并发、嵌套与 fanout 故障
        ├── [M] test_agent_model.py ← G03A repair/fallback 与失败 usage
        ├── [M] test_codex_model.py ← G03B 聚合和无效业务结果
        ├── [M] test_codex_client.py ← G03B reader thread 并发身份
        ├── [M] test_claude_code_model.py ← G03B feedback、缓存与终态
        ├── [M] test_config.py ← G04B 旧配置与默认禁用
        ├── [A] test_tracing.py ← G04B 假 SDK、映射、故障与关闭
        └── [A] test_langfuse_integration.py ← G05B 显式启用的专用项目合成事件验收
```

G05A 还复用既有 OpenAI/media、Embedding、models 和 SSE 直接测试；运行它们不自动把其源码加入修改树。
手动测试模块及 Server 管理 CLI 不作为核心接入前置条件；不新增 gateway CLI 或 test_cli.py。

### 8.4 Server 外部 TODO 候选树

S1 需 10 个既有文件：5 个生产接点及 5 个直接测试。
这是 Server 负责人的独立范围；不混入 Gateway 当前可写树。
它阻塞 Server 联验和整链验收，不阻塞 Gateway 离线模块开发。

```text
Seekino/
├── server/
│   ├── [M] bootstrap.py ← 初始化/注入桥接，活动调用退出后关闭
│   └── service/
│       ├── [M] parse_usage.py ← 在 observer 拥有位置组合账本与追踪
│       ├── [M] video_parse.py ← 模型 stage 的任务/尝试身份
│       ├── [M] agent_chat.py ← worker 入口的会话/消息/workspace scope
│       └── [M] video_retrieval.py ← 实际检索身份与 Query Embedding scope
└── tests/
    └── server/
        ├── [M] test_bootstrap.py ← 初始化与关闭顺序
        ├── [M] test_parse_usage.py ← 账本、partial、unknown 和 CNY
        ├── [M] test_video_parse_worker.py ← 异步/线程执行身份
        ├── [M] test_agent_chat_service.py ← worker 与宿主工具身份
        └── [M] test_video_retrieval_service.py ← 真实检索 scope 的 Embedding 关联
```

`server/api/app.py` lifespan 及新 MQ 手动入口已经调用 ApplicationServices.close，当前没有新增入口接线的必要。
Query Embedding 的业务身份应由 video_retrieval.py 提供，仅向 lazy embedder 注入桥接不能解决身份归属。
当前 Engine 导出缺口另阻塞 Server 直接回归；该外部并行改动原样保留。
S2 的测试项目与环境条件只阻塞 G05B；S3 的 V1 退役和 Parse V2 08A 位置继续独立维护。

### 8.5 G01-D4 地图同步

本组八个既有设计文件同步移除 G04B/G05A 的核心 CLI 要求，并将检索 Service 补入 S1。
地图使用既有临时工具的官方 exportToSvg 重新导出；阶段身份、依赖及 G01 当前位置保持。
本批执行 `node render.mjs` 和 `python3 validate-cli-refactor.py`，工作目录为既有临时导出目录。
三张图已逐张查看中文、卡片、依赖线和外部 TODO；文本没有裁切或越界。

| 本批同步核对 | 结果 |
| --- | --- |
| 场景与 SVG | 三个 JSON 与 XML 合法；官方矢量导出，无整图 image 元素；SVG 文字逐行与场景一致 |
| 节点与依赖 | 主图八节点、分图各四节点；四项完整；主图十条内部依赖，G01 当前标记不变；版本均为 G01-D4 |
| 本地引用 | 87 个引用有效；TRD 十八个唯一锚点；G04B/G05A 无核心 CLI 候选，S1 包含实际检索 Service |
| 浏览器渲染 | 主图与分图共 317 行 SVG 文字，边界检查无越界；逐张查看通过 |
| 工作区保护 | 526 个写前基线路径中，15 个本批原路径变化，其余 511 个内容一致；新增两个迁移目标，无范围外变化；既有 dirty/untracked/ignored 成果保留 |
| 空白与暂存 | `git diff --check` 通过；本组 Markdown 无尾随空白；暂存区为空 |

两份脚本共 159 行移动；与写前源码对照仅 8 行替换，未增加模型或 Consumer 实现。
本批实际改动为 2 个移动、13 个修改，共 15 个逻辑文件；没有新增业务依赖。
Gateway/Server 未来核心候选合计 28 个文件，不包含设计同步和 G04A 临时原型。
本批未实施 Langfuse、未安装业务 SDK、未启动服务、未执行真实模型/后端调用、未 commit/push。

## 9. G01-D5 完整实施与离线验收

日期：2026-10-07。
授权：用户在完整核心树展示后指示“直接做完整的”。
实际实施范围：Gateway 十八文件、Server 十文件、既有八个设计文件。
共三十六文件，三个新增、三十三个修改。
前一批 CLI 迁移已经包含在实施写前基线中，不重复计入本批。
当前阶段迁到 G05B；G02、G03A/B、G04A/B 和 G05A 已有直接执行证据。
真实后端未配置；S1 普通联验受独立 Engine 缺口限制；整体验收未通过。

### 9.1 实际行为与框架选择

- 公共调用记录补实际结束时间、粒度、usage 来源和宿主上下文快照；旧 capture 默认替换/恢复，显式 combine 才组合 observer。
- 开始钩子在 tracker 标记请求开始时创建 span，终态结束一次；账本仍接收一次终态。追踪单支异常保留模型结果及其他订阅。
- structured 在本地 payload 构造成功后追踪每次主请求、repair、fallback；业务校验失败保留已返回 usage。
- Codex 在 invocation 内保留最新累计 usage，失败时捕获一次；executor 和 SDK reader 恢复本次上下文。Claude 只补两行聚合粒度和来源声明。
- 正式桥接使用私有 OTel 1.45.0 和 Langfuse 官方 OTLP/HTTP；默认关闭，lazy 初始化，无全局 provider 或 atexit 注册。
- 网络在自有进程执行，模型终态只做有界本地入队；关闭按截止排空，必要时停止并回收出口。Unix socketpair/pass_fds 的正式实现面向 macOS/Linux。
- 每个 span 携带 session、environment、release 和允许的业务身份；不导出原始 prompt、响应正文、媒体或错误文本。
- input/output/total 使用标准 usage；缓存、推理等可能重叠的子集只写 metadata；unknown 不填零，CNY 只写明确币种的 metadata。
- Server 绑定实际任务、lease/attempt/stage、会话/消息与检索身份；账本 observer 和追踪显式组合。Consumer 与 Agent 退出后关闭模型和追踪资源。

官方出口协议依据：[Langfuse 原生 OpenTelemetry 接入](https://langfuse.com/integrations/native/opentelemetry)。
HTTP 200 中的部分拒绝不能算全部成功，协议依据：[OTLP partial success](https://opentelemetry.io/docs/specs/otlp/#partial-success-1)。
`exported` 仅为 HTTP 传输确认；`dropped` 为已知拒绝；`unconfirmed` 为未收到确认，可能已经被后端接收。
机制收尾通过不等于可靠投递、exactly-once 或真实项目可见性通过。

G04A 可复现资料：`/private/tmp/seekino-langfuse-lifecycle-probe/{pyproject.toml,uv.lock,sdk_probe.py,transport_probe.py,report.py,report.json}`。
命令：`uv run --project /private/tmp/seekino-langfuse-lifecycle-probe python /private/tmp/seekino-langfuse-lifecycle-probe/report.py`。
候选 SDK 4.17.0 + OTel 1.45.0 可解析，但请求 10ms 的 flush 实测约405ms，不能证明整体收尾截止。
隔离出口原型正常排空约30ms、积压约29ms、堵塞约361ms；最后一项由产品机制强制收尾，owned 线程和进程均退出。
正式项目只安装实际消费的 OTel API、SDK 和 proto-common；未加入未使用的 Langfuse SDK。

### 9.2 直接测试与限制

| 命令或入口 | 实际结果 | 证明范围与限制 |
| --- | --- | --- |
| `.venv/bin/pytest tests/gateway/test_usage.py -q` | 41 passed | 公共记录、默认替换、显式 fanout、故障隔离、嵌套/并发身份、开始与结束时序 |
| `.venv/bin/pytest tests/gateway/test_agent_model.py tests/gateway/test_codex_model.py tests/gateway/test_codex_client.py tests/gateway/test_claude_code_model.py -q` | 153 passed | 三类 Adapter 正常/失败/repair/fallback、累计 usage、并发 reader 工具身份；不证明内部 SDK inference 次数或请求前统一拦截 |
| `.venv/bin/pytest tests/gateway/test_config.py tests/gateway/test_tracing.py tests/gateway/test_langfuse_integration.py -q` | 61 passed、1 skipped，5.79s | 真正 protobuf 和本地 HTTP、父子/远端父、字段时钟、unknown/零、正文省略、阻塞/积压/并发关闭、初始化失败回收和 partial-success；跳过真实项目 |
| `.venv/bin/pytest tests/gateway -q --ignore=tests/gateway/test_agent_provider_live.py` | 378 passed、1 skipped，11.63s | 全部 Gateway 离线回归，包含本地 HTTP 验证文件；真实 Provider 文件排除，唯一 skip 是真实 Langfuse 项目 |
| `.venv/bin/pytest tests/gateway/test_langfuse_integration.py -q`，最终真实读回断言复核后 | 8 passed、1 skipped，5.53s | 本地 HTTP/OTLP 与生命周期回归仍通过；加强的真实后端断言未运行，继续等待项目 |
| 五个 Server 文件普通收集 | 28 collected、4 collection errors，未执行 | `engine.rag` 缺少 Server 仍引用的 `reuse_embedding_artifact`；没有通过修改 Engine 绕过 |
| 四个 Server 完整文件 + 第五文件五个 worker 用例，缺失 Engine 名称仅作隔离 | 118 passed、1 skipped、2 failed，9.97s | 使用正式 gateway.tracing；未调用缺失 Engine 实现；外部 MySQL 用例 skip。两个既有检索 identity 合同失败保留，不能登记普通 Server 联验通过 |
| consumer/agent 失败后资源清理的参数化 focused 回归 | 2 passed，4.08s | 最后追加 ES 关闭断言后验证完整关闭顺序；仍使用同样的缺失 Engine 隔离 |

以上计数有包含关系，不能相加。
真实读回入口最终复核补齐全部子节点 parent/call_id/状态/时钟、CNY metadata 和 unknown usageDetails 空值断言。
unknown 的 usage_source 按合同省略，不强填 synthetic_result。
该入口默认跳过，代码存在和本地回归均不证明真实后端验收。

Server 原始命令、选择范围和可复现 runner：
`/private/tmp/seekino-langfuse-implementation-20261007/{server-verification-record.md,verify_server.py}`。
复现命令：`.venv/bin/python /private/tmp/seekino-langfuse-implementation-20261007/verify_server.py`。
runner 检查实际 tracing 模块位置；缺失 Engine placeholder 一旦被实际调用就失败。
两项保留失败：

```text
tests/server/test_video_retrieval_service.py::test_v2_retrieval_fuses_es_channels_and_hydrates_exact_identity
tests/server/test_video_retrieval_service.py::test_v2_retrieval_rejects_cross_channel_identity_drift
```

当前 Engine 的 VideoChunkRetrievalIdentity 拒绝 source_revision、projection_revision、retrieval_contract_id、semantic_encoder_id、vision_encoder_id。
没有修改 Engine 源码、放宽合同或将失败标为通过。

### 9.3 实际修改树

```text
Seekino/
├── [M] pyproject.toml ← 声明实际消费的 OTel 1.45.0 依赖
├── [M] uv.lock ← 解析并锁定同一组合
├── gateway/
│   ├── [M] usage.py ← 上下文快照、实际结束、开始/终态与 fanout
│   ├── [M] config.py ← 默认禁用、环境变量与有界出口配置
│   ├── [A] tracing.py ← 私有 provider、字段映射、隔离 OTLP 与关闭报告
│   ├── [M] README.md ← 配置、关闭口径和后端验收入口
│   ├── openai/
│   │   └── [M] structured.py ← 主请求/repair/fallback 独立记录
│   ├── codex/
│   │   ├── [M] agent.py ← 失败保留最新累计 usage 及调用上下文
│   │   └── [M] client.py ← SDK reader 工具线程上下文
│   ├── claude_code/
│   │   └── [M] agent.py ← 两行聚合粒度及来源声明
│   ├── design/
│   │   └── [M] trd.md ← G01-D5 合同、实际机制与 G05B 状态
│   └── report_docs/
│       ├── [M] model-observability-stages.svg ← 主图状态和当前位置
│       ├── [M] model-observability-stages.excalidraw ← 主图源场景
│       ├── [M] model-observability-stages-01-03.svg ← 前半阶段实施证据
│       ├── [M] model-observability-stages-01-03.excalidraw ← 前半源场景
│       ├── [M] model-observability-stages-04-05.svg ← 后半机制、验收及当前位置
│       ├── [M] model-observability-stages-04-05.excalidraw ← 后半源场景
│       └── [M] model-observability-stages-evidence.md ← 当前执行证据与历史边界
├── server/
│   ├── [M] bootstrap.py ← lazy 初始化/注入及失败分支关闭
│   └── service/
│       ├── [M] parse_usage.py ← 显式组合账本与追踪
│       ├── [M] video_parse.py ← 实际模型 stage 身份和线程 scope
│       ├── [M] agent_chat.py ← 实际 worker 会话/消息/workspace 身份
│       └── [M] video_retrieval.py ← Query Embedding 身份及 lexical-only 边界
└── tests/
    ├── gateway/
    │   ├── [M] test_usage.py ← 兼容、并发、生命周期和故障隔离
    │   ├── [M] test_agent_model.py ← 请求级覆盖及失败 usage
    │   ├── [M] test_codex_model.py ← 累计值、业务校验及回调失败
    │   ├── [M] test_codex_client.py ← 并发 reader 工具上下文
    │   ├── [M] test_claude_code_model.py ← 聚合来源和未知终态
    │   ├── [M] test_config.py ← 默认关闭及配置兼容
    │   ├── [A] test_tracing.py ← 内存出口、字段时钟和资源故障
    │   └── [A] test_langfuse_integration.py ← 本地原始 HTTP/OTLP 与 opt-in 真实读回
    └── server/
        ├── [M] test_bootstrap.py ← 初始化与异常关闭顺序
        ├── [M] test_parse_usage.py ← 账本/CNY/partial/unknown 兼容
        ├── [M] test_video_parse_worker.py ← 实际 worker 与 asyncio.to_thread 身份
        ├── [M] test_agent_chat_service.py ← worker scope、宿主工具与幂等复用
        └── [M] test_video_retrieval_service.py ← Embedding 关联及 lexical-only 不发记录
```

既有 Server 管理 CLI 单独构建服务，目前不在上述核心十文件接线范围。
真实部署、数据库迁移、付费 Provider、页面与 API schema 均未作为本批执行项。
S3 和 Parse V2 的当前位置 08A 继续独立维护。

### 9.4 地图与工作区交付核验

使用既有临时工具的官方 `@excalidraw/excalidraw@0.18.1 exportToSvg` 导出。
命令：`node render.mjs`，工作目录 `/private/tmp/seekino-observability-map-jexpxd_a/`。
静态核对：`python3 validate-langfuse-implementation.py`，同一工作目录。
原始实施基线和授权集合：`/private/tmp/seekino-langfuse-implementation-20261007/baseline.json`。
三个 SVG 已逐张查看中文、卡片、当前徽标和依赖线。

| 最终核验 | 结果 |
| --- | --- |
| 导出与场景 | 三个合法 JSON、三个合法 SVG XML；官方矢量导出，无 image 元素；SVG 文字逐行与源场景一致 |
| 阶段与依赖 | 主图八节点、分图各四节点，每节点四项齐全；主图十条内部依赖；G05B 专用当前徽标在主图和后半分图一致 |
| 本地引用与锚点 | 87 个有效本地引用，18 个唯一 TRD 锚点；当前索引、TRD 与场景均为 G01-D5/G05B |
| 浏览器渲染 | 共330行 SVG 文字，文本边界均在 viewBox 内；三图逐张查看无裁切或内容重叠 |
| 写前基线保护 | 528 个原基线路径中，33 个授权原路径变化、495 个内容一致；另新增3个授权目标；范围外变化为0 |
| 空白与暂存 | git diff --check 通过；TRD/索引无尾随空白；暂存区为空 |

核验输出：`/private/tmp/seekino-observability-map-jexpxd_a/langfuse-implementation-validation.json`。
以上统计只属于本批，既有 dirty/untracked/ignored 成果原样保留。
逐文件对照本批写前副本的行数变化：12 个生产文件 +901/-81，13 个测试文件 +1269/-41。
另有2个依赖文件和9个文档/图文件；图源排版变化不计入生产代码规模。
该基线已包含前一批 CLI 迁移及其他任务改动，不能用当前 git HEAD 总 diff 代替本批统计。
授权路径中的 `git diff --stat` / `--numstat` 相对 HEAD 汇总为25个 tracked文件 +1347/-137。
该值包含同文件此前改动，且不含三个新增目标与八个 untracked/ignored设计文件；本批规模采用上述写前副本对照值。
原始 stat/numstat 与逐文件对照存于 `/private/tmp/seekino-langfuse-implementation-20261007/{tracked-head-stat.txt,tracked-head-numstat.txt,diff-summary.json}`。

### 9.5 下一步可执行范围

代码和离线范围已完成，不再列新的生产修改批次。
G05B 的现有入口是 `tests/gateway/test_langfuse_integration.py::test_real_langfuse_v2_observations_read_back`。
取得 S2 专用项目、base URL 和本机凭据后，显式设置 `SEEKINO_LANGFUSE_INTEGRATION=1` 才运行。
模拟调用无需真实模型；必须读回实际 observation，不能只看出口 HTTP ACK。
Server 普通回归需 Engine 负责人先恢复有效导出和检索 identity 合同，然后沿上述十文件回归范围联验。
如果真实项目验收暴露合同差异，应先定位归属，并在既有职责与授权内修正；不得因此自动扩展 Engine、部署或存储范围。
