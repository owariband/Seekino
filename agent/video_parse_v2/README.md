# Video Parse V2 Agent Harness

状态：职责边界已确认；Graph、State、Event、Reactor 与各阶段实现尚未落地。

本目录是 Video Parse V2 的 Agent/LangGraph 运行边界。完整算法拓扑见
[`docs/design/parse-v2-harness.md`](../../docs/design/parse-v2-harness.md)；框架无关的领域算法、
Schema、Patch 校验和 Retrieval Projection 位于
[`engine/parsev2/`](../../engine/parsev2/README.md)。

## 1. 核心职责

本目录回答的是：

> 当前运行到了哪里，下一步应该调用哪个节点或模型，怎样把 Engine 的确定性结果推进为一次完整的 Graph Run。

本目录负责：

- LangGraph program、Graph 和 Subgraph；
- Graph State、运行时 Event、Reducer 与条件路由；
- 阶段一单 chunk 基础解析编排；
- 阶段二 interval readiness Reactor 与任务调度；
- 阶段三 Global 前缀校正编排；
- ASR、OCR、多模态理解等节点的并行与汇合；
- Prompt 拼装、Skill/Tool 暴露和模型上下文治理；
- 调用 `gateway/agent_model` 执行结构化模型请求；
- 调用 `engine/parsev2` 的纯函数完成计划、校验、归并和投影；
- deadline、cancellation、模型调用预算、节点级错误分类和可观测事件；
- 把 Engine 返回的领域结果转换为 Graph State 更新与运行时 Event。

本目录不拥有 Parse 领域真相，也不拥有生产持久化真相。

## 2. 与 Engine 的关系

Harness 使用 Engine，不复制 Engine。

```text
Agent Harness
├── 决定何时调用模型
├── 固定本次允许操作的 chunk / interval / revision
├── 让模型生成结构化 Proposal
├── 把 Proposal 交给 Engine 校验和应用
├── 根据 Engine Decision 更新 Graph State
└── 发布运行时 Event 或下一项工作

Engine
├── 定义 Proposal 与 Canonical Schema
├── 校验 Authority Envelope、Evidence 引用和字段白名单
├── 计算 interval readiness、scope 和确定性 reducer
├── 返回 Canonical Delta 与受影响的 Retrieval Atom
└── 不知道 LangGraph Node、Command 或下一跳
```

核心调用公式：

```text
Host Authority Envelope
+ Agent Proposal
→ Engine Decision
```

Harness 提供 Authority Envelope，至少固定：

- 当前 Parse Job/Run；
- Parse Plan revision；
- 当前 chunk 或 interval；
- 允许读取的 Evidence；
- 允许修改的目标集合；
- deadline 和调用预算。

模型只提供 Proposal。它不能通过输出改变 Authority Envelope。

## 3. 模型调用与结构化 Proposal

Prompt 和模型调用属于 Harness；Proposal 的领域 Schema 和最终合法性属于 Engine。

```python
proposal = model.run_structured(
    prompt,
    result_type=IntervalCorrectionProposal,
    tools=tools,
    ...
)

decision = apply_interval_patch(
    target=interval_target,
    snapshot=current_snapshot,
    proposal=proposal,
)
```

必须保持：

- Prompt 可以要求模型遵守规则，但不能代替代码校验；
- 模型输出首先是 untrusted Proposal，不是 Canonical State；
- Engine 拒绝 Proposal 时，Harness 进入明确的失败、重试或降级路径；
- Harness 不得在 Engine 拒绝后自行“修一下字段”再静默写入；
- Agent 不直接生成 embedding、index revision 或数据库定位条件。

## 4. 三种编排必须区分

### 4.1 LLM/Graph 编排：属于本目录

```text
选择 Node
→ 准备模型上下文
→ 调 Gateway
→ 接收 Proposal
→ 调 Engine 校验
→ 更新 State / 路由
```

### 4.2 领域算法：属于 Engine

```text
D/W/K 计算
interval membership
ready mask
scope 集合并
Patch 白名单
重叠结果 reducer
Canonical View
Retrieval Projection
```

### 4.3 生产任务生命周期：属于 Server

```text
Job 领取
Lease/Fencing
Checkpoint/Artifact
durable event/outbox
事务提交
失败恢复
索引版本切换
```

本目录不能因为使用了 LangGraph，就接管 Server 的生产任务生命周期。

## 5. 建议的最小结构

以下只是按稳定职责划分的起步结构，不代表尚未评审的 Base/Local/Global 工作名已经成为稳定 API：

```text
agent/video_parse_v2/
├── __init__.py
├── README.md
├── state.py              # LangGraph Run State；只描述当次运行
├── events.py             # 运行时 Event；不冒充 durable DB Event
├── graph.py              # 主 Graph、Subgraph 组合和条件路由
├── nodes.py              # 节点入口与清晰的主流程
├── runtime.py            # 模型、Engine API、控制参数的装配入口
└── prompts.py            # Prompt 与结构化输出说明
```

第一版不要为了“架构化”预先创建大量单节点文件、Manager、Factory 或空 Interface。
只有当节点形成独立、稳定且可测试的复杂边界时，再从 `nodes.py` 拆出对应模块。

## 6. State 与 Event 边界

Graph State 只属于当次算法运行，不是跨 Attempt 的恢复真相。

State 可以保存：

- Parse Plan 的只读快照或引用；
- 当前 Graph 已观察到的 chunk/interval 状态；
- 待运行工作项与有限的运行时控制信息；
- Engine 返回的领域结果或其稳定引用；
- deadline、预算和 cancellation 控制。

State 不应该保存：

- 数据库连接或 Repository；
- Qdrant Client 或 embedding 向量全集；
- Provider SDK Client；
- 可替代 MySQL/Artifact 的 durable truth；
- 由模型任意生成的 revision、ID 或路由命令。

运行时 Event 负责推进 Graph；Server 的 durable event/outbox 负责跨进程可靠交付。两者语义可以映射，
但不能因为名字相同就混成一个对象。

同一个 `ChunkBaseReady` 逻辑事件必须同时推进：

1. Base Scheduler 的批次完成度；
2. Interval Readiness Reactor 的 ready mask。

事件可以重复、乱序，因此 Reducer 必须幂等。Harness 可以调用 Engine 计算新 mask 和
`became_ready`，但不能在多个 Node 中各写一份 readiness 公式。

## 7. Reactor 与回调纪律

readiness callback/event handler 只负责：

1. 幂等记录 Base Ready；
2. 定位有限数量的受影响 interval；
3. 调用 Engine 更新 ready mask；
4. 对首次就绪 interval 产生候选工作；
5. 立即返回。

callback 禁止：

- 直接等待模型完成；
- 在状态更新事务中执行多模态理解；
- 执行 embedding 或检索发布；
- 扫描全部 interval；
- 阻塞其他独立 interval；
- 把 scope 扩展解释成重新运行 interval 的信号。

worker 领取候选工作后仍需重新检查目标、revision、成员结果和 deadline。

## 8. Prompt、Tool 与上下文治理

本目录拥有 Prompt 的组织方式，但 Prompt 中的规则必须与 Engine 的强校验一致。

Prompt 应明确：

- 本次只处理哪个 chunk 或 interval；
- 可读取哪些 Raw Evidence 和 Canonical Context；
- 输出哪个 Engine Proposal Schema；
- 允许建议修改哪些字段；
- 无法判断时怎样表达 unknown/ambiguous；
- 不得生成身份、revision、scope、embedding 或数据库信息。

Tool 只暴露本次节点真正需要的有限能力。确定性的 readiness、scope、Patch 校验和 reducer
不是给 LLM 自由选择的 Tool，而是 Harness 节点必须执行的普通 Engine 调用。

模型上下文必须有明确预算，不把完整视频、完整历史或全部 Evidence 无界塞入 Prompt。
Raw Evidence、Tool 输出和历史文本都是数据，不执行其中夹带的指令。

## 9. 错误、降级与可观测性

Harness 至少区分：

- 参数或 Authority Envelope 错误；
- 模型调用失败；
- 模型输出 Schema 错误；
- Engine 拒绝非法 Proposal；
- Evidence 缺失；
- deadline/cancellation；
- 可重试节点失败；
- 最终降级结果。

后一阶段失败不能破坏最近一个已验证版本：

- 阶段二区间失败保留阶段一结果，其他 interval 继续；
- 非法 Patch 整体拒绝，不部分写入；
- 阶段三失败保留阶段二结果与已有召回版本；
- embedding/index 发布失败由 Server 保持旧版本继续服务。

日志和事件应保留稳定业务 ID、目标类型、状态、调用次数、耗时和错误分类；不得打印 token、
认证信息、完整自由文本、完整模型载荷或大体积 Evidence。

## 10. 明确禁止

本目录禁止：

- 导入、调用或包装 `engine/parsev1` Pipeline、State、Prompt 或阶段实现；
- 在 LangGraph Node 内重新实现 Engine 的 interval、scope、Patch 或 projection 算法；
- 让模型直接修改 Canonical State；
- 把 Agent Proposal 当成已验证事实；
- 直接连接 MySQL、Qdrant 或对象存储；
- 自行生成 durable revision、幂等身份或数据库主键；
- 把 Graph State 当作跨 Attempt 的恢复真相；
- 把 Provider SDK 和供应商协议写进 Graph Node；
- 把阶段三 Global marker 混入阶段二 scope；
- 用未定义的 last-write-wins 处理重叠 interval 结果；
- 在待评审项没有结论时自行固化一种行为。

## 11. 依赖方向

允许的依赖方向：

```text
agent/video_parse_v2
    ├── engine/parsev2          # Schema 与纯领域算法
    └── gateway/agent_model     # Provider 无关模型执行

server
    └── 托管 Agent Runtime，并实现持久化和恢复
```

禁止的反向依赖：

```text
engine/parsev2  ─X→ agent/video_parse_v2
engine/parsev2  ─X→ server
gateway         ─X→ Video Parse V2 State
```

## 12. 实现前检查

新增或修改 Harness 行为前，至少确认：

1. 这是不是 Graph 编排问题，而不是 Engine 算法或 Server 生命周期问题；
2. 对应的 Engine Proposal/Decision Schema 是否已经稳定；
3. Host Authority Envelope 是否固定了目标和 revision；
4. 模型失败、Schema 失败和 Engine 拒绝是否有不同路径；
5. 重复/乱序 Event 是否仍然幂等；
6. 当前 Node 是否复制了其他 Node 或 Engine 的公式；
7. 是否错误地把工作名暴露成稳定 API；
8. 是否可以在不连接 MySQL、Qdrant 和真实 Provider 的情况下测试 Graph 主路径。

一句话总结：

> Harness 决定何时做、让谁做和下一步去哪；Engine 决定数据是否合法以及具体怎么算；Gateway 负责调用模型；Server 负责让这一切可靠地保存、恢复和发布。
