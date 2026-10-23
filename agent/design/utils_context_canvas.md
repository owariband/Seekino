# ContextCanvas：Parse 与 Chat 共用的上下文工作区

> 当前状态：Chat 检索已移除 Canvas，直接返回证据并保留来源与播放范围校验。本文的 Chat Canvas 段落是历史设计记录；Parse V2 和通用 Canvas 仍保留。

2026-10-22–23 的工具逻辑修订、备选方案、实验指标和成本估算见 [开发日志中的 Canvas 记录](../../docs/dev/devlog.md)。

更新时间：2026-10-10。状态：通用 Tool 抽取、ContextCanvas 基座、Parse/Chat 迁移与 Gateway 接入已实现，最终联合离线验证通过。真实模型、外部数据库及服务联调未执行。阶段证据见第 11、12、13 节。

## 1. 目标与已确认决定

将解析与 Chat 中“保存证据、维护草稿、通过工具局部修改、压缩后恢复上下文”的共同机制封装为 `ContextCanvas`。模型负责理解证据和提出操作，程序持有完整状态，并据此生成最终结果。

- 统一基座命名为 `ContextCanvas`，提供实际可复用的实现。
- 配套两个 Canvas 工具：`get_context_canvas` 与 `write_context_canvas`。
- `ParseContextCanvas` 和 `VideoSearchContextCanvas` 扩展各自的业务操作与校验。
- Parse 与 Chat 都必须实际迁移到同一基座、Tool 包装和读取机制；Chat 迁移属于交付与验收范围。
- 延续现有事件 JSON、数据库列、Chat 输出与播放范围契约。
- 复用现有 Gateway 工具循环、上下文压缩、调用控制和 usage 统计。

## 2. 现状与问题依据

重构前，解析侧使用 `persons/timeline` 字段级 proposal。[apply_chunk_update](../../engine/parsev2/validation.py) 会将显式提供的 `timeline` 整数组替换。模型只返回需要修正的事件时，未返回的事件就会丢失。已保存的低推理联调证据中，理解阶段生成 36 条事件，Refine 返回两条修正事件，最终只保存了两条。本轮模型入口改为局部 Canvas 操作；兼容的整字段类型只承载程序组装、经版本检查的提交结果。

重构前的 [Prefix 工具](../video_parse_v2/prefix/tools.py) 已经使用 Tool，但仍接收完整时间线并返回 proposal。因此，防止丢失需要改变更新契约：明确操作目标、保留完整草稿、由程序应用局部修改。

Chat 原有的 AgentWorkspace 已迁移为 [VideoSearchContextCanvas](../chat/video_search/video_search_canvas.py)，在模型消息之外保存检索证据、回答草稿、核准快照和渲染结果。Query、Evaluator、Chat 共用 Canvas，各自维护模型上下文和工具权限。[checkpoint](../chat/video_search/context.py) 提供导航信息，目录与读取工具恢复准确内容。共享基座以这些现有实现为范本。

## 3. 基类、接口与 BaseModel 的职责

`BaseModel` 本身就是基类。这里需要区分“通过基类共享实现”和“采用 Pydantic 数据模型基类”这两个用途。

| 形式 | 在本设计中的职责 |
|---|---|
| `ContextCanvas` 具体基类 | 提供状态存储、条目引用、有序目录、副本隔离、快照与修改记录等共同实现 |
| 普通类或 `dataclass` | 承载运行期状态、锁及执行内协作 |
| Pydantic `BaseModel` | 定义并校验工具参数、事件、草稿、操作与完成回执，提供序列化能力 |
| 小 `Protocol` | 共用调用方确实需要替换不同实现时，约束其实际消费的读取或操作能力 |

接口本身不提供实现；两个业务对象仅实现相同接口，仍不足以达成复用目标。共享机制在 `ContextCanvas` 和通用 Tool 组件中实现，业务 Canvas 通过继承及内部组件组合扩展。运行期锁、连接和模型执行流程不作为模型可写数据暴露。

## 4. 结构与职责归属

```text
ContextCanvas
├─ 条目存储、引用管理、有序目录与读取副本
├─ get() / atomic_batch() 的共同处理机制
├─ snapshot() / checkpoint()
└─ 本地 revision、修改记录与批次结果
    ├─ ParseContextCanvas
    │  └─ 事件操作、解析证据、时间与成员范围校验
    └─ VideoSearchContextCanvas
       └─ 检索证据、草稿审核、核准快照与回答渲染

get_context_canvas / write_context_canvas
└─ 共用 AgentTool 包装 → 现有 Gateway 工具循环
```

共享工具与后续 Canvas 基座归属中性的 `agent/utils`。[AgentTool](../utils/tool.py)、[ToolResult](../utils/tool_result.py) 和 [分页与分段读取](../utils/tool_output.py) 提供实际共用的实现，Chat 原有导入路径保留兼容。

Chat 的 conversation/message 日志字段、`draft_item_count`、进度事件以及业务审核规则保留在 Chat，或通过明确的元数据与回调注入。共享基座不依赖具体 Chat、Parse、SQL 或供应商类型。

本批抽取中，Chat 的 AgentTool 继承通用实现，仅扩展草稿计数和未知工作区条目反馈；会话日志适配继续留在 Chat。通用工具错误、结果类型与读取函数从 Chat 原路径直接重新导出同一对象。现有 `agent/common/chunk_understanding.py` 属于视频理解业务复用，本批保持其流程及调用方不变。

Canvas 保存执行中的准确状态，生命周期由宿主管理。它不代替数据库；snapshot/checkpoint 在本设计中也不自动承诺进程退出后的持久恢复。

## 5. 两个 Canvas 工具

### get_context_canvas

优先按完整条目读取。只传 `collection` 获取目录；传 `reference` 或 `references` 获取正文，`section` 默认 `content`，需要目录说明时显式指定 `overview`。

- 目录用于导航，详情才是准确内容。
- 一次先尝试返回全部请求项；实际超过 `maximum_result_bytes` 才按请求顺序返回能容纳的连续完整 `items` 前缀和 `remaining_references`，模型用剩余引用自行缩小批次。首项过大时返回空 `items`，模型可单独读取该项或显式选择其他引用。
- 目录也先尝试全量；仅超出字节预算时返回 `next_cursor`，续读参数名为 `catalog_cursor`。过期目录标记或无效引用返回可纠正的反馈。
- 单个 section 本身超大时，使用较小的命名 section，或用 `reference` 加 `char_start/char_end` 字符范围读取。批量不接受 item 索引范围；未返回的正文不计为已读。
- 旧宿主构造参数 `maximum_read_chars/maximum_catalog_items` 保留兼容，不再限制 Canvas 读取；去掉独立的 8,000 token 门槛，统一使用工具配置的字节预算。
- 模型只能读取宿主绑定范围内的资源和当前角色允许的视图。

### write_context_canvas

接收一批明确、经过类型校验的操作。操作类型与有效目标由业务 Canvas 及当前角色决定，例如添加事件、修正事件、维护回答草稿或视频块。

- 每个操作明确目标和变更语义，省略条目或字段表示保留。
- Parse 用 `remove_event(event_ref)` 显式删除事件；可删除初始或本轮新增事件，原始 evidence/background 仍只读。删除通过同一原子批次与修正一起验证，最终时间线可明确为空。
- 数组字段区分添加、移除和设值，避免把整体替换的歧义转移到事件内部。
- 每批操作先验证，再整体应用到当前草稿；失败时不留下部分修改。
- 返回修改结果、受影响引用和新的本地 revision，复用现有 ToolResult 与错误反馈通道。
- 复用已有 tool-call 重放保护，并结合业务键或操作记录保证已执行操作的重放幂等。
- 工具修改 Canvas 草稿，不在每次调用中直接写 SQL。

工具名字和共同机制统一，操作参数保持业务类型与明确语义。宿主签发目标引用、绑定角色能力与范围；模型不能通过参数扩大任务、租约或数据库写入目标。

## 6. Parse 的业务扩展

`ParseContextCanvas` 保存三类信息：

1. 只读证据：ASR、OCR、帧与视频引用、背景上下文及其来源。
2. 可修改草稿：完整事件时间线、人物信息、当前条目引用。
3. 宿主状态：成员快照版本、范围、租约及修改记录；模型不能修改这些控制信息。

| 阶段 | Canvas 操作 |
|---|---|
| 初次理解 | 根据视频与 ASR/OCR，从空草稿批量添加事件 |
| Refine | 结合相邻片段补齐上下文、修正已有事件，有证据时补充遗漏事件 |
| Prefix | 维护背景摘要，通过同一机制修正绑定片段的事件 |
| 最终保存 | 宿主从完整草稿生成结果，校验后一次事务提交，并沿用索引更新流程 |

Refine 默认提供补充与修正能力，不注册删除或清空事件的操作。修改两条事件时，其余事件由程序保留；不使用事件数量硬阈值代替正确的更新契约。

ASR 继续输出秒，由程序转换一次为绝对源视频毫秒。进入 Canvas 的时间统一为绝对毫秒；背景人物信息用于理解，沿用现有对白归属规则。原始 ASR/OCR 证据由宿主保留并接入读取路径，不能用已生成时间线的投影冒充原始证据。重构前 Refine 未独立载入这些原始证据，本轮在 Chunk detail 保留并供 Refine/Prefix 读取；旧数据缺少证据时明确标记不可用。

模型完成时只提供小回执或完成说明。最终 `VideoParseResult` 来自 Canvas，模型的终态文本不能重新覆盖工具维护的时间线。

## 7. 事件定位与并行提交

当前公开事件结构没有独立事件 ID。起步采用宿主签发的执行内引用，内部绑定成员、原始内容版本、原始位置及完整事件指纹。引用在草稿内保持稳定，不能把可修改的时间或未检查版本的数组下标当作身份。

每个 Chunk 或 Refine 窗口拥有自己的 Canvas 快照，模型请求继续并行。工具操作期间不持有数据库锁。提交进入现有事务后检查租约、读取快照的版本与指纹，再应用修改并校验完整结果。

重构前 Interval 提交允许将字段应用到最新锁定行，不能防止旧快照覆盖新结果；本轮补齐所有输入成员的版本及完整内容指纹检查。冲突时刷新并复核相关精修，保留已完成的 ASR 与理解结果。重复事件或已改变的目标不得通过猜测重新定位。

Canvas 本地 revision 表示执行内草稿变化；数据库 content version 表示持久结果版本。两者分别使用。后续若需要跨版本的稳定事件身份，再在现有内部 JSON 中设计侧表，不增加公开事件字段或 SQL 列。

## 8. Chat 必须实际迁移

`VideoSearchContextCanvas` 承接现有 AgentWorkspace 的状态。Chat 的迁移不是预留接口，而是本次体系落地的完成条件。

| 当前能力 | 迁移目标 |
|---|---|
| 检索结果与准确证据 | 登记到 Canvas，保留范围与来源 |
| 草稿目录、证据目录、详情读取 | 收敛到 `get_context_canvas` |
| 草稿添加、修改、删除 | 使用 `write_context_canvas` 的草稿操作 |
| Evaluator 核验通过 | 宿主形成核准草稿快照 |
| 最终视频块添加、修改、删除 | 使用 `write_context_canvas` 的渲染操作 |
| Markdown 流式输出 | 沿用现有文本回调，写入同一 Canvas 的渲染状态 |
| 压缩后的内容恢复 | checkpoint 导航，再通过 `get` 读取 |

Query、Evaluator、最终 Chat 继续拥有各自的模型上下文和权限：Query 维护草稿，Evaluator 读取并核验，最终 Chat 使用核准快照组装回答并维护渲染块。核准证据和范围对最终 Chat 保持只读。

统一 get 工具后继续记录目录遍历与逐条草稿精读审计。沿用[当前审核流程](../chat/video_search/runtime.py)：只有 Evaluator 返回 complete、读取审计完整且播放范围程序校验通过，宿主才形成核准快照。

外部检索仍由检索能力执行，结果进入 Canvas；Canvas 的两个工具处理工作区内状态。检索算法与排序策略继续由现有检索层承担。保持现有回答结构、视频播放范围、流式响应和 usage 归属契约。

## 9. 复用 Gateway 与上下文恢复

准确内容始终保存在 Canvas，checkpoint 只提供引用、计数、范围和必要的任务锚点。模型通过目录与读取工具恢复完整证据；消息被压缩不会删除 Canvas 内容，也不代表已执行的操作需要再次执行。

使用现有 `context_checkpoint` 回调与 Gateway 的 ContextWindowController、工具循环、取消检查、deadline 和实际调用预算。继续沿用现有 usage observer，不增加一套统计账本。

Parse 接入工具循环时，需要保持已确认的输出预算、reasoning 和备用模型行为，并处理多模态输入及媒体预算。本轮通过 Responses/Chat Completions 桥接复用原有循环，沿用各供应商预算、reasoning、备用模型与 usage 控制。工具循环允许完成必要的后续轮次，批量操作减少逐事件往返；实际 Token 与耗时收益以联调证据判断。

## 10. 实施顺序与验收

建议先抽取现有通用 Tool 与读取机制，建立 ContextCanvas 的最小共同实现；接入 Parse 的 Refine/Prefix，修复事件局部更新与提交冲突；再统一初次理解，并完成 Chat 的实际迁移。两边都接入后才进行整体验收。

| 验收项 | 通过条件 |
|---|---|
| 实际复用 | Parse 与 Chat 使用同一 ContextCanvas 基座、Tool 包装和目录读取实现 |
| 事件完整性 | 重放既有 36 条事件案例，只修正两条时其余事件保留，最终文本不能覆盖 Canvas |
| 写入原子性与重放 | 无效批次无部分修改，同一已执行操作重放不重复追加 |
| 时间与定位 | 时间单位保持绝对毫秒；无效引用、过期版本和歧义目标被明确处理 |
| 并发精修 | 旧快照不能覆盖其他窗口的新结果；冲突不触发已完成 ASR/理解重跑 |
| Chat 全链路 | 检索证据可通过统一 get 精读，草稿和渲染可通过统一 write 维护 |
| 核准快照与输出 | 保留完整目录与逐条读取审计、播放范围程序校验；最终 Chat 使用核准证据与范围，保持现有响应和流式行为 |
| 上下文压缩 | 压缩后证据、草稿和事件保留，可用 get 恢复准确内容 |
| 范围与隔离 | 对话、解析任务、并行窗口及角色权限隔离，无越界读写 |
| 配置与统计 | 输出预算、reasoning、fallback、取消控制和真实 usage 统计正确传递 |

本文件维护已讨论的设计与实施验收目标。记录设计不代表源码已经迁移或联调已经通过。

## 11. 当前实施证据（2026-10-10）

本批按确认范围先解除通用工具与 Chat 业务的混用，完成后再续接 ContextCanvas 基座开发。

以下为通用工具抽取批次完成时的状态；续接阶段进展见第 12 节。

| 内容 | 已设计 | 已实现 | 离线验证 | 真实联调 |
|---|---|---|---|---|
| 通用 Tool、工具结果、工具错误、分页与分段读取抽取到 `agent/utils` | 是 | 是 | 通过 | 未执行 |
| Chat 构造入口、草稿反馈、日志及旧导入路径兼容 | 是 | 是 | 通过 | 未执行 |
| ContextCanvas 基座与两个 Canvas 工具 | 是 | 否 | 未执行 | 未执行 |
| Parse 局部事件操作、原始证据与提交版本冲突处理 | 是 | 否 | 未执行 | 未执行 |
| Chat 接入 Canvas 的草稿、核准快照、读取审计与渲染 | 是 | 否 | 未执行 | 未执行 |

验证基于当前工作区，保留其他任务已有改动：

- 修改前及首次源码抽取后，原有 7 个回归文件均为 **299 passed, 1 skipped**。
- [工具测试](../../tests/agent/chat/test_tools.py) 新增 3 项：禁止导入 Chat 的独立进程直接使用通用工具；同一类型的旧入口兼容及草稿反馈、日志适配；通用 UTF-8 分段反馈、分页与旧游标拒绝。
- 最终运行 `tests/agent/chat/{test_tools,test_runtime,test_supervisor,test_realtime_understanding}.py` 与 `tests/gateway/test_tool_images.py`：**215 passed**。
- 最终运行 `tests/server/{test_agent_chat_service,test_sse_events}.py`：**87 passed, 1 skipped**；跳过项需要未配置的 `ANIVAG_TEST_DATABASE_URL`。合计 **302 passed, 1 skipped**，覆盖图片 artifact、审核与播放范围、流式输出、取消、范围隔离及现有错误映射。
- 最终完整回归曾因另一项并行迁移暂时缺失 `server.constants.workspace` 在收集阶段中断；该迁移随后更新为 `server.constants.tenant`，以上最终 Server 回归在其更新后通过。本批未修改该迁移文件。
- 本批没有服务启动或重启、现存数据库操作、付费模型联调、commit 或 push。局部测试通过不代表 ContextCanvas 整体已完成。

## 12. ContextCanvas 续接实施与最终离线验收（2026-10-10）

本轮基于已抽取的 `agent/utils` 开发共同基座、Parse/Chat 业务接入与 Responses 工具通道。公开事件 JSON、SQL 列、Chat 输出和播放范围继续沿用当前工作区契约。

| 内容 | 已设计 | 已实现 | 离线验证 | 真实联调 |
|---|---|---|---|---|
| ContextCanvas 与共用 get/write 工具 | 是 | 是 | 通过 | 未执行 |
| Parse 理解、Refine/Prefix、原始证据及提交版本检查 | 是 | 是 | 通过 | 未执行 |
| Chat 草稿、精读审核、核准快照及回答渲染 | 是 | 是 | 通过 | 未执行 |
| Responses 视频/帧、已有 openai_vision 及控制/usage 接线 | 是 | 是 | 通过 | 未执行 |

共同机制验证：`.venv/bin/pytest tests/agent/utils/test_context_canvas.py -q` → **10 passed**。覆盖副本隔离、失败批次无部分状态或重放记录、重放幂等、角色与任务隔离、并发版本冲突、提交前输出预算、原子证据冻结、只读保护、宿主绑定工具权限/次数与分段精读恢复。

准确数据由 Canvas 持有；checkpoint 仅含范围、revision、集合计数与受控引用。模型写入在独立草稿上整体执行，完成业务校验、只读检查和返回预算检查后才发布。证据登记可由宿主原子追加并冻结，模型批次不能修改只读集合。

兼容修复收尾前，扩展运行 `tests/agent/{utils,chat}`、`tests/gateway`、`tests/engine/parsev2` 与 `tests/server/{test_agent_chat_service,test_sse_events}.py`：**866 passed, 2 skipped, 1 deselected**。外部数据库/Elasticsearch 地址从测试环境移除，付费 `live_agent` 测试排除；HTTP 模拟服务在获准后于沙箱外运行。该中间结果只验证当时的 Chat 与 Gateway 回归，不代替当时尚在收尾的 Parse/Repository 联合验收或真实模型联调。

最终在所有接入和兼容修复完成后联合运行：**1125 passed, 35 skipped, 1 deselected**。测试范围包括上述目录，以及 `tests/agent/video_parse_v2`、Server 的 `test_video_parse_repository`、`test_video_parse_worker`、`test_video_parse_queue`、`test_video_parse_media`、`test_worker_fencing`、`test_video_parse_model_calls`、`test_parse_usage`、`test_agent_system_integration`、`test_mysql_integration`、`test_elasticsearch_integration`。外部 DB/ES 地址仍明确移除，付费测试仍排除。跳过项需要未配置的外部数据库、Elasticsearch 或 Langfuse 环境；没有将跳过记录标为通过。相关文件 `git diff --check` 通过。

具体实现和证据：

- [共同基座](../utils/context_canvas.py) 与 [共用工具](../utils/context_canvas_tools.py) 承担实际存储、副本隔离、批次原子发布、操作指纹重放、只读检查、角色绑定及有界读取。JSON 字段顺序变化不影响同一批次的重放身份。
- [Parse Canvas](../video_parse_v2/timelineEvent_canvas.py) 用宿主签发的事件引用执行局部操作。`patch_event` 保留省略字段；数组明确 add/remove/set 并校验元素类型；`set_event` 只允许明确设值一个完整事件以修正分类，保持原引用和来源身份。没有删除或清空事件操作。模型只返回完成回执，完整时间线由程序组装。
- [36 条原始 fixture](../../tests/agent/video_parse_v2/fixtures/context_canvas_refine_36.json) 来自既有低推理案例。[Canvas 回归](../../tests/agent/video_parse_v2/test_context_canvas.py) 与 [事务保存回归](../../tests/server/test_video_parse_repository.py) 均验证只修正两条后仍保存全部 36 条，其余 34 条完整 JSON 不变。
- [提交端](../../server/repository/video_parse.py) 在修改任何成员前检查所有输入成员版本与完整内容指纹，再校验全部候选并事务保存。回归覆盖无直接修改成员的并发变化、版本未增加但内容变化、第二候选失败的整批回滚。快照冲突与租约丢失、取消分别处理，只刷新相关精修。
- 历史跨窗口事实按宿主持久 scope 校验并保留；新事件和原事件时间修改仍受当前固定写入窗口约束。scope 空值、错误类型和越任务拓扑先明确拒绝。原始正规化 ASR/OCR 保存在 Chunk detail，Refine/Prefix 独立读取，不从 timeline 伪造证据；原 ASR 秒到绝对毫秒转换路径保持一次转换。
- [Chat Canvas](../chat/video_search/video_search_canvas.py) 保存检索证据、草稿、冻结的核准快照和渲染。实际 Query 注册 retrieve/get/write，Evaluator 注册 retrieve/get，最终 Chat 注册 get/write。[宿主审核](../chat/video_search/runtime.py) 在同锁内要求 complete、当前版本完整目录与逐项逐节精读、程序播放范围校验后才核准。
- [真实 Gateway 离线流式回归](../../tests/agent/chat/test_runtime.py) 使用实际 SDK 与 HTTP 传输桩，验证同轮 Markdown→视频→Markdown，无冲突重试。仅 Chat 视频批次可与连续宿主 Markdown 追加合并；任何实际视频、草稿或证据变更继续拒绝旧版本。全局 revision 与旧游标失效仍有效，两个版本整数仅作为控制信息，不建立渲染镜像。
- [Gateway 桥接](../../gateway/agent_model/multimodal.py) 复用原工具循环、上下文压缩与调用控制。原生视频、按序帧、Responses 与已有 Vision Chat Completions 的双向 fallback、供应商自身输出预算/reasoning、取消关闭传输及供应商返回 usage 的统计接线均经离线验证。无工具生成与 ASR 配置保持原行为。
- [参数省略回归](../../tests/gateway/test_multimodal_agent.py) 通过真实响应解析→工具参数校验→业务 Canvas 的路径验证局部更新。Gateway 序列化使用 `exclude_unset=True`，不会把未传字段补成显式 null。压缩回归清理旧工具正文后，准确状态仍可通过同一 get 恢复。
- 已删除 `agent/video_parse_v2/host.py` 的大型 Protocol 和空实现，原数据类型迁入既有 `state.py`，阶段直接调用注入的实际运行对象。未增加 SQL 列、新数据库表或第二套工具循环/usage 账本。

输出窗口限制：保留已配置的输出预算。当前保守上下文策略按 128k 窗口预留实际输出；131072 等输出预算无法在该窗口内预留时使用 `policy=None`，保留 400k 字符硬界及字符限额触发的压缩，不宣称提供 token 自动压缩策略，也不虚构更大的模型容量。初始视频 token 成本使用媒体投影估计，后续由供应商返回的 input usage 校准。真实容量、复杂操作 schema 的供应商兼容性及 Token/耗时收益仍需获准后真实联调。

本轮实际修改范围为 **38 个生产路径、19 个测试/fixture 路径、7 个文档路径，共 64 个路径（9 新增、54 修改、1 删除）**。其中 5 个新增生产模块合计 **1304 行**；修改路径中的其他任务原有内容被保留，不用工作区累计 Git diff 冒充本轮独立增删行数。原 checkout 中 `tests/gateway/test_models.py` 只执行回归，没有产生本轮修改。没有操作现存数据库、启动或重启项目服务、运行付费模型联调、commit 或 push。


<details>
<summary>本轮实际修改树</summary>

```text
生产代码:
[M] agent/chat/video_search/chat.py
[M] agent/chat/video_search/context.py
[M] agent/chat/video_search/evaluator.py
[M] agent/chat/video_search/runtime.py
[M] agent/chat/video_search/tools/__init__.py
[A] agent/chat/video_search/tools/canvas.py
[M] agent/chat/video_search/tools/registry.py
[M] agent/chat/video_search/tools/retrieval.py
[M] agent/chat/video_search/tools/shared.py
[M] agent/chat/video_search/workspace.py
[M] agent/common/chunk_understanding.py
[A] agent/utils/context_canvas_tools.py
[A] agent/utils/context_canvas.py
[A] agent/video_parse_v2/context_canvas.py
[D] agent/video_parse_v2/host.py
[M] agent/video_parse_v2/prefix/agent.py
[M] agent/video_parse_v2/prefix/context.py
[M] agent/video_parse_v2/prefix/tools.py
[M] agent/video_parse_v2/prepare.py
[M] agent/video_parse_v2/prompts.py
[M] agent/video_parse_v2/refine/agent.py
[M] agent/video_parse_v2/refine/input.py
[M] agent/video_parse_v2/runtime.py
[M] agent/video_parse_v2/scheduler.py
[M] agent/video_parse_v2/state.py
[M] agent/video_parse_v2/understanding/state.py
[M] agent/video_parse_v2/understanding/workflow.py
[M] engine/parsev2/validation.py
[M] gateway/agent_model/context.py
[A] gateway/agent_model/multimodal.py
[M] gateway/agent_model/tool_loop.py
[M] gateway/ark/multimodal.py
[M] gateway/ark/sse.py
[M] gateway/openai/multimodal.py
[M] gateway/openai/vision.py
[M] server/model/dto/jobDTO.py
[M] server/repository/video_parse.py
[M] server/service/video_parse.py

测试与 fixture:
[M] tests/agent/chat/test_runtime.py
[M] tests/agent/chat/test_tools.py
[A] tests/agent/utils/test_context_canvas.py
[A] tests/agent/video_parse_v2/fixtures/context_canvas_refine_36.json
[A] tests/agent/video_parse_v2/test_context_canvas.py
[M] tests/agent/video_parse_v2/test_framework.py
[M] tests/agent/video_parse_v2/test_runtime.py
[M] tests/engine/parsev2/test_validation.py
[M] tests/gateway/test_context_window.py
[A] tests/gateway/test_multimodal_agent.py
[M] tests/gateway/test_openai_media.py
[M] tests/gateway/test_sse_proxy.py
[M] tests/server/test_agent_chat_service.py
[M] tests/server/test_agent_system_integration.py
[M] tests/server/test_elasticsearch_integration.py
[M] tests/server/test_mysql_integration.py
[M] tests/server/test_video_parse_queue.py
[M] tests/server/test_video_parse_repository.py
[M] tests/server/test_video_parse_worker.py

文档:
[M] agent/chat/video_search/skills/chat/SKILL.md
[M] agent/chat/video_search/skills/evaluator/SKILL.md
[M] agent/chat/video_search/skills/query/SKILL.md
[M] agent/chat/video_search/skills/retrieval_transcript/SKILL.md
[M] agent/chat/video_search/skills/retrieval/SKILL.md
[M] agent/design/utils_context_canvas.md
[M] agent/video_parse_v2/README.md
```

</details>


## 13. 业务 Canvas 命名收尾（2026-10-10）

按本次明确指令，将 Parse 业务模块重命名为 `timelineEvent_canvas.py`（保留指定的大小写），Chat 业务模块重命名为 `video_search_canvas.py`。类名继续使用 `ParseContextCanvas` 与 `VideoSearchContextCanvas`，两者直接继承同一个 `agent/utils/context_canvas.py` 中的 `ContextCanvas`。Parse 文件内容与本批修改前完全相同，仅移动模块并更新引用。

Chat 生产路径移除 `AgentWorkspace` 别名，Query、Evaluator、最终 Chat、工具装配及执行内状态统一使用 `VideoSearchContextCanvas` 和 `canvas`；checkpoint 函数改为 `build_canvas_checkpoint`，其已有 JSON schema、字段和读取机制保持原样。租户范围参数及历史文档中的旧类名继续按原含义保留。第 12 节修改树记录此前实施时的路径，本节记录命名收尾后的实际路径。

| 内容 | 已设计 | 已实现 | 离线验证 | 真实联调 |
|---|---|---|---|---|
| 业务 Canvas 文件名、Chat 显式类型与调用路径收口 | 是 | 是 | 通过 | 未执行 |

验证证据：

- 重命名前保存本批文件快照；按授权的模块、类名、变量、参数、内部状态键及说明文字映射比较 AST，Chat 生产 13 个模块及 7 个测试文件保持其余结构等价。Parse Canvas 文件字节内容完全相同。
- 导入与构造检查验证两个业务类的直接基类均为同一个 `ContextCanvas`，Chat 新 checkpoint 能读取同一 Canvas ID；旧业务模块路径已不存在，生产与测试无旧别名或旧模块导入。
- 沿用第 12 节最终联合测试范围再次运行，排除付费 `live_agent` 项，并明确移除外部数据库/Elasticsearch 地址：**1125 passed, 35 skipped, 1 deselected**（32.39 秒）。覆盖 36 条事件案例、批次原子性、重放和版本冲突、Chat 草稿与核准审计、播放范围、流式输出、压缩恢复、角色范围以及 Gateway 预算/reasoning/fallback/取消/usage 接线。跳过项仍需要外部数据库、Elasticsearch 或 Langfuse 环境。
- 本批命名调整的局部差异空白检查及设计文档的有效文件链接检查通过。

本批共 **31 个文件：21 个生产文件、7 个测试文件、3 个文档文件；2 个重命名、29 个修改**。没有新增业务逻辑或配置、变更公开 JSON/SQL 契约、操作现存数据库、启动或重启项目服务、运行付费模型、commit 或 push。

<details>
<summary>本批实际修改树</summary>

```text
AniVAG/
├── agent/
│   ├── chat/
│   │   └── video_search/
│   │       ├── [M] chat.py  ← 显式消费 Canvas，沿用流式 Markdown 与视频块处理
│   │       ├── [M] context.py  ← checkpoint 改名 build_canvas_checkpoint，沿用序列化契约
│   │       ├── [M] evaluator.py  ← 显式消费 Canvas，沿用完整目录及逐条精读审核
│   │       ├── [M] query.py  ← 显式消费 Canvas 与 checkpoint，统一提示词名称
│   │       ├── [M] runtime.py  ← 创建并贯穿同一 canvas，统一内部状态键和调用参数
│   │       ├── [M] state.py  ← 内部 RunState 键改为 canvas，沿用运行期类型约定
│   │       ├── tools/
│   │       │   ├── [M] canvas.py  ← 共用工具工厂显式传入业务 Canvas
│   │       │   ├── [M] draft.py  ← 既有草稿工具统一 Canvas 参数，行为保留
│   │       │   ├── [M] registry.py  ← 各角色工具装配统一 canvas 参数
│   │       │   ├── [M] retrieval.py  ← 检索证据登记和读取统一 canvas 参数
│   │       │   ├── [M] shared.py  ← 草稿卡片读取显式使用业务 Canvas
│   │       │   └── [M] video_item.py  ← 既有视频块工具统一 Canvas 参数，行为保留
│   │       └── [R] video_search_canvas.py  ← 原 workspace.py；移除 AgentWorkspace 别名，保留业务实现
│   ├── design/
│   │   └── [M] utils_context_canvas.md  ← 更新有效链接并登记本批命名与验证证据
│   └── video_parse_v2/
│       ├── [M] README.md  ← 更新目录和业务 Canvas 链接
│       ├── prefix/
│       │   ├── [M] agent.py  ← 同步 Parse 业务 Canvas 模块导入
│       │   └── [M] tools.py  ← 同步 Parse 业务 Canvas 模块导入
│       ├── [M] prompts.py  ← 同步 Parse 业务 Canvas 模块导入
│       ├── refine/
│       │   └── [M] agent.py  ← 同步 Parse 业务 Canvas 模块导入
│       ├── [R] timelineEvent_canvas.py  ← 原 context_canvas.py；按用户指定大小写改名，文件内容不变
│       └── understanding/
│           ├── [M] state.py  ← 同步 Parse 业务 Canvas 模块导入
│           └── [M] workflow.py  ← 同步 Parse 业务 Canvas 模块导入
├── docs/
│   └── dev/
│       └── [M] problems.md  ← 仅修复两处现有业务模块链接，保留历史问题记录
├── server/
│   └── service/
│       └── [M] video_parse.py  ← 同步 Parse 业务 Canvas 模块导入
└── tests/
    ├── agent/
    │   ├── chat/
    │   │   ├── [M] test_runtime.py  ← 同步业务 Canvas 模块、类名或参数引用
    │   │   └── [M] test_tools.py  ← 同步业务 Canvas 模块、类名或参数引用
    │   └── video_parse_v2/
    │       ├── [M] test_context_canvas.py  ← 同步业务 Canvas 模块、类名或参数引用
    │       └── [M] test_framework.py  ← 同步业务 Canvas 模块、类名或参数引用
    ├── gateway/
    │   └── [M] test_multimodal_agent.py  ← 同步业务 Canvas 模块、类名或参数引用
    └── server/
        ├── [M] test_video_parse_repository.py  ← 同步业务 Canvas 模块、类名或参数引用
        └── [M] test_video_parse_worker.py  ← 同步业务 Canvas 模块、类名或参数引用
```

</details>


## 14. 独立提交内容核验（2026-10-10）

按用户本次 commit/push 指令，基于 `2548ee1` 的完整 HEAD 导出准备独立提交内容，逐文件核对前两阶段快照与命名快照，排除工作区已有的租户、登录、Realtime 和配置默认值变更。当前提交基线使用 `workspace_id/workspaceId`；仅在提交候选中沿用该公开契约，本地租户迁移版本仍完整保留。设计文档原被忽略，本次明确纳入提交；未修改全局忽略规则。

必要依赖随共同机制一起提交：共用 Chunk 理解组件、工具图片 artifact 的最小类型与 loop 接线、图片/视频预算投影、Ark 工具路径所需 reasoning 构造参数，以及已确认的仅对白人物登记约束。未纳入其它 Provider capability/factory/config 迁移、普通生成/stream reasoning 扩展或 ASR 设置变更。预算复用回归使用明确冻结的测试参数，不依赖本地尚未提交的配置默认值。

独立目录只包含 HEAD 与上述提交内容，不加载原工作区源码。同第 12 节相关目录及 Server 测试范围，排除付费 `live_agent` 项、明确移除外部数据库/Elasticsearch 地址：**1031 passed, 35 skipped, 1 deselected**（27.78 秒）。原工作区联合验证的 1125 项与本次独立提交的 1031 项分别记录；差额来自未纳入提交的其他任务及其测试，不能相互替代。HTTP 传输模拟测试因沙箱限制本地端口绑定，获准后在沙箱外完成离线验证；没有运行真实外部服务或模型。

| 内容 | 已设计 | 已实现 | 离线验证 | 真实联调 |
|---|---|---|---|---|
| HEAD 加本任务及必要依赖的独立提交内容 | 是 | 是 | 通过 | 未执行 |

本次提交包含前述三个批次及必要依赖，共 83 个文件路径；这与第 12、13 节单批次统计的口径不同。当前工作区其他任务的文件内容保持原样。
