# Parse V2 Schema-driven Architecture Draft

状态：Draft。Schema-driven Harness 是当前架构基线；具体解析 Schema、Projection 和目录迁移尚未实现。

原始讨论图 `parse_big_graph.png` 以及本文后半部分的事件驱动方案，是此前的算法探索素材，
不再作为当前实现约束。本文开头的“当前基线”优先级最高。

## 1. 当前结论

Video Parse V2 的核心不是让 Agent Harness 固化理解某一套 `utterance / event / speaker`
字段，也不是先建立 Proposal、Patch、Canonical Decision 等中间体系，而是：

> Engine 维护面向模型的解析结果 Model／Struct，并把它导出为 JSON Schema；通用 Agent
> Harness 只负责把当前 Schema、任务说明、视频证据和 Tool 提供给模型，再返回符合该
> Schema 的结构化结果。Engine 随后把完整解析结果拆成当前 embedding 与存储策略需要的
> 各类内部结构。

一次逻辑 Parse Run 只解析一个 Video。一个 Run 内可以有多次模型调用、工具调用、重试和
checkpoint，但这些机制不能让 Harness 依赖具体解析字段。跨 Video 排队、租户隔离、资源
配额、Lease/Fencing 和持久化恢复仍由 Server 负责。

当前最重要的设计对象是 **Parse Strategy**：

~~~text
Parse Strategy
├── Result Model / JSON Schema       # 要模型产出什么
├── Instructions                     # 字段语义与解析目标
├── Evidence / Tool 配置             # 模型可以观察和调用什么
├── Schema identity                  # name / version / hash
└── Projection                       # 完整结果怎样拆成检索和存储输入
~~~

如果把 Schema 广义理解为这份任务合同，可以说“策略的本质是 Schema”；如果只指裸 JSON
字段结构，则还必须同时包含 instructions、可用证据／工具和 Projection。

## 2. 主流程

~~~text
Engine / Parse Strategy
  产生 Result Model、JSON Schema、instructions 和 tools
            ↓
通用 Agent Harness
  将动态 Schema 与视频证据提供给模型
  不读取、不分支判断任何领域字段
            ↓
Structured Parse Result
  一份符合本次 Schema 的完整结构化结果
            ↓
Engine Projection
  Parse Result → Exact / Text / Vision / Storage Inputs
            ↓
Server Application Service
  调 Gateway 执行 embedding，调 Repository / Storage 持久化并发布
~~~

这里没有第二个负责审判模型语义的 Engine 模型，也不要求先把模型输出转换成
Proposal／Patch，再由 Engine 决定事实真伪。Engine 对结果执行的检查只是所属 Model 的
反序列化和结构约束；“台词是否修正正确、speaker 到底是谁”仍然来自本次 Agent 所使用的
多模态模型与证据。

## 3. Engine 拥有的两类数据结构

### 3.1 面向 Agent 输出的 Parse Result Model

Engine 以 Pydantic Model 或等价强类型结构作为单一来源，并在运行时导出 JSON Schema：

~~~text
VideoParseResult Model
        ↓ model_json_schema()
JSON Schema
        ↓ 注入 Harness / structured output tool
模型生成结构化结果
        ↓ model_validate(...)
VideoParseResult Instance
~~~

字段增加、删除、重命名或改变嵌套关系时，Harness 代码不应修改。Harness 只认识“本次输出
Schema”，不认识其中的 `utterance`、`speaker`、`event` 或任何未来字段。

现有 `gateway/agent_model/contracts.py` 的泛型 `run_structured(result_type=...)` 已经具备
这条路径的基础：Provider adapter 可以从注入的 Pydantic 类型生成 JSON Schema，并把模型
结果反序列化为该类型。`gateway/schema_normalization.py` 已负责部分 Provider Schema 方言
适配，不需要再建立一套领域相关的 Agent 输出协议。

### 3.2 面向 embedding 与存储的 Projection Struct

Agent 不接触以下内部结构。Engine 根据当前 RAG 策略把完整解析结果拆成例如：

~~~text
ExactIndexInput[]
TextEmbeddingInput[]
VisionEmbeddingInput[]
ParseStorageRecord[]
~~~

这些结构可以采用不同粒度、组合不同字段，但都来自同一份 Parse Result，并保留回指原始
视频时间和解析结果位置所需的信息。

Engine／Parse Strategy 决定“索引什么、如何拆分和组织输入”；真实 embedding Provider 调用
由 Gateway 执行，数据库／Qdrant／Artifact 写入与发布由 Server 执行。它们可以位于同一个
功能目录和同一个进程中，但 Schema／Projection 代码不直接持有数据库连接或 Provider SDK。

## 4. Harness 的零耦合边界

目标是 **字段级和领域级零耦合**，不是不存在任何运行协议。Harness 的最小稳定协议只有：

~~~text
RunSpec
├── instructions
├── output_schema / result_type
├── schema_name / schema_version / schema_hash
├── evidence / tools
├── model settings
├── budget
└── timeout

RunResult
├── schema identity
├── structured value
├── usage / trace
├── status
└── generic error
~~~

必须满足：

- Harness 不 import 具体 Video Parse Result Model；由装配层动态注入；
- Harness 不按领域字段写条件分支；
- Schema 字段变化不要求修改 Harness；
- Tool 若使用结构化参数，其输入 Schema 同样在运行时生成；
- Provider Schema 限制在 Gateway 适配，不泄漏为视频领域规则；
- 只有执行协议改变，例如从一次性结果改为增量文档编辑，才可能调整 Harness 通用能力。

第一版优先使用一次性 structured output。只有真实测试证明大型结果无法稳定一次生成时，
再考虑通用的 `set(path, value)`、`append(path, item)`、`read(path)`、`finish()` 文档工具；
不要在没有证据前预建领域 Patch、Patch Reducer 或多套 Proposal 类型。

## 5. Schema 与运行版本

每次 Parse Run 开始时必须固定：

~~~text
schema_name
schema_version
schema_hash
strategy_version
~~~

checkpoint 必须保存或稳定引用同一份 Schema。恢复中的 Run 不能因为服务代码升级而静默切换
到新 Schema；新版本只作用于新的 Parse Run 或显式 Reparse。

解析结果进入检索侧后，还应区分：

~~~text
Parse Result Schema Version
Projection Version
Encoder / Embedding Model Version
~~~

这样仅更换 Projection 或 Encoder 时，可以从已有 Parse Result 重建检索输入，而不必重新让
Agent 理解整部视频。

同一个已发布检索版本中的 Exact、Text 和 Vision 输入必须来自同一份 Parse Result 与同一
Projection Version，不能重新出现 V1 中不同召回通道消费不同语义版本的问题。新 Projection
或索引发布失败时，上一可用版本继续服务，不能形成“内容已切换、向量仍是旧版本”的状态。

## 6. 代码收敛方向

当前仓库只有一个根 `pyproject.toml`，Agent、Engine、Gateway 已由 Server 在同一进程内装配，
不存在三个独立发布包。Video Parse V2 也尚未落地生产代码，因此当前推荐按功能域收拢：

~~~text
server/
└── video_parse/
    ├── schemas.py       # Result Model 与 JSON Schema
    ├── strategy.py      # instructions、tools、版本与策略选择
    ├── harness.py       # 对领域字段无感的 Agent 执行壳
    ├── projection.py    # Parse Result → embedding / storage inputs
    └── service.py       # 调用 Harness、Gateway、Repository 与发布流程
~~~

第一版不需要为了形式继续拆成 `engine/`、`agent/`、manager、factory 或空 interface。只有文件
实际增长，或 Harness 出现第二个真实领域调用方时，再把通用运行能力提升到例如
`server/agent_runtime/`。

这个方向不等于现在机械搬迁整个顶层 `agent/` 和 `engine/`：现有 `agent/chat/`、
`engine/parsev1/` 与 `engine/retrieval/` 已有生产调用和大量 import，是否迁移应作为独立结构
调整评审。本文件当前路径也暂时保持不变。

## 7. 当前 P0

按依赖顺序，当前需要先确定：

1. 最小可用的 Video Parse Result Model，以及字段语义和 JSON Schema 表达；
2. 通用 `RunSpec → RunResult` 与 structured output／Tool 合同；
3. Parse Result 到 Exact、Text、Vision 和持久化输入的 Projection；
4. Schema、Projection、Encoder 与 checkpoint 的版本关系；
5. 用真实视频验证一次性 structured output 的大小、稳定性和 Provider 限制。

在这些问题建立之前，不把下列内容视为已确认架构：

- Proposal／Patch／Canonical Decision 链路；
- Engine 使用第二个模型做语义裁决；
- Harness 理解固定的 utterance／event／speaker 字段；
- LangGraph 是 Parse V2 不可替换的硬架构；
- 固定三阶段 Graph 是所有 Schema 策略的必经形态；
- 重叠 Patch Reducer 是 Schema 建立前的 P0；
- Agent 直接生成 embedding、索引版本或数据库定位条件。

---

## 历史附录：此前的事件驱动算法探索

以下内容保留此前关于 chunk、重叠 interval、readiness、scope、三阶段和 Patch 的推演，供后续
设计具体 Strategy 时参考。它们不再是当前规范；其中出现的“已确认”“必须”“当前基线”等
措辞只描述当时的讨论状态，除非被重新提升到上面的当前基线，否则不能直接进入实现。

## 3. 当时的算法结论（历史探索）

### 3.1 三个处理层次

| 工作称呼 | 已确认职责 | 当前未确定 |
| --- | --- | --- |
| 阶段一（暂称 Base） | 单 chunk 的 ASR、OCR、视觉输入整理、多模态理解和初步 speaker 归类 | 最终 Graph、State、Ready Event 名称 |
| 阶段二（暂称 Local） | 对完整 chunk 组成的 stride=1 重叠区间进行一次局部语义与 speaker 纠偏，并更新成员 chunk 的 scope | 区间结果的最终 Schema、冲突归并和阶段完成命名 |
| 阶段三（暂称 Global） | 使用独立的 all_pre 类型标识进行前缀级全局校正 | 标识名称、推进条件、产物结构和召回切换规则 |

阶段名称未定不影响以下算法事实：

1. 阶段一按 chunk 独立运行。
2. 阶段二按完整 chunk 区间运行，区间重叠是算法意图。
3. 阶段二区间互不等待，也不因 scope 扩大而重算。
4. 阶段三使用独立标识，不把 Global 信息塞进阶段二的 scope。

### 3.2 总体拓扑

以下节点名和事件名均为工作名：

~~~text
START
  ↓
split_video
  ↓
生成有序完整 chunk 列表
  ↓
根据 Parse Plan 计算阶段二区间成员数 K
  ↓
注册所有 stride=1 区间 watch
  ↓
启动阶段一渐进式调度与事件 Reactor
  │
  ├─ BaseDispatchPermit(chunk)
  │      ↓
  │   单 chunk 基础解析 Graph
  │      ↓
  │   ChunkBaseReady(chunk)
  │      ├─→ 更新阶段一批次完成度与下一批释放
  │      └─→ 更新所有包含该 chunk 的区间 ready mask
  │
  ├─ 某区间所有成员均 Base Ready
  │      ↓
  │   区间首次进入 ready queue
  │      ↓
  │   阶段二区间纠偏 Graph
  │      ↓
  │   原子集合并成员 chunk 的 scope
  │
  ├─ 满足 Retrieval 发布条件
  │      ↓
  │   embedding / publish
  │      ↓
  │   主解析流程结束
  │
  └─ 后台阶段三
         ↓
      独立维护 all_pre 类型全局标识
         ↓
      全局校正与必要的后续版本更新
~~~

阶段一批次调度、阶段二区间 readiness 和阶段三全局校正是不同的状态投影，不能互相冒充完成条件。

## 4. Parse Plan 与完整 chunk 区间

### 4.1 配置快照

每个 Parse Job 必须绑定不可变的 Parse Plan revision，至少固化：

~~~text
configured_chunk_duration
configured_interval_duration
interval_chunk_count K
algorithm_revision
experiment_assignment
~~~

chunk 长度需要支持约 15 秒至 60 秒的配置，用于 A/B 实验和用户高级设置。默认配置为：

~~~text
configured_chunk_duration = 60s
configured_interval_duration = 180s
K = 3
~~~

### 4.2 时间配置只用于推导完整 chunk 数量

设：

~~~text
D = configured_chunk_duration
W = configured_interval_duration
~~~

则规划阶段计算：

~~~text
K = ceil(W / D)
~~~

示例：

| chunk 配置时长 | 区间目标时长 | K |
| ---: | ---: | ---: |
| 60s | 180s | 3 |
| 40s | 180s | 5 |
| 30s | 180s | 6 |
| 15s | 180s | 12 |

一旦 K 写入 Parse Plan，运行时 readiness 只按完整 chunk 数量判断。

明确禁止：

- 按时间戳截取半个 chunk；
- 用连续时间区间与 chunk 做 overlap 后把部分 chunk 算作成员；
- 为了刚好等于 W 而拆分某个已生成 chunk；
- 在同一个 Parse Job 运行期间动态改变 K；
- 把配置变化原地应用到已经注册的 interval watch。

时间配置决定 K，K 决定离散区间成员。阶段二永远消费完整 chunk。

### 4.3 stride=1 的全部重叠区间

设：

~~~text
N = chunk 总数
K = interval_chunk_count
stride = 1
~~~

当 N 大于或等于 K 时，对每一个合法起点 s 创建：

~~~text
W(s) = [s, s + 1, ..., s + K - 1]

0 <= s <= N - K
~~~

默认 K=3：

~~~text
W0 = [0,1,2]
W1 = [1,2,3]
W2 = [2,3,4]
W3 = [3,4,5]
...
~~~

这些区间不是互斥 batch：

~~~text
012 可以一起更新
123 也可以一起更新
012 和 123 可以同时运行
~~~

不存在 target chunk、left/mid/right 角色，也不存在“只写 mid”的语义。区间内所有完整 chunk 都属于本次阶段二处理单元。

当 N 小于 K 时的降级行为尚未确定，见“待评审项”。

## 5. 阶段一：单 chunk 基础解析

### 5.1 单 chunk Graph

每个 chunk 的基础解析至少包括：

~~~text
               ┌─→ ASR ─┐
chunk(i) ──────┤        ├─→ 输入整理 → 多模态理解 → 基础结果
               └─→ OCR ─┘
~~~

当前已确认：

- ASR 与 OCR 可以并行；
- 两条证据与视觉输入在后续节点汇合；
- 可按配置抽帧，未要求抽帧时使用当前视频切片作为视觉输入；
- 多模态理解负责基础语义和初步 speaker 归类；
- 阶段一不执行最终 embedding；
- 阶段一成功后发布该 chunk 的基础就绪事件。

caption 这一历史工作名可能同时指视觉输入或模型输入包，正式 State Schema 必须改用准确类型名。

### 5.2 渐进式 Base 调度

阶段一调度至少需要以下配置，名称仍是工作名：

~~~text
base_batch_capacity
batch_tail_ratio
base_max_inflight
release_step
release_interval
~~~

当前默认语义：

~~~text
base_batch_capacity = 10
batch_tail_ratio = 0.1
~~~

对当前门控批次 b：

~~~text
batch_size(b)
    = 当前批次实际 chunk 数量

base_ready_count(b)
    = 已首次达到阶段一 Ready 的不同 chunk 数量

base_remaining(b)
    = batch_size(b) - base_ready_count(b)

tail_threshold(b)
    = max(1, ceil(batch_size(b) × batch_tail_ratio))
~~~

当存在下一批且满足：

~~~text
base_remaining(b) <= tail_threshold(b)
~~~

下一批可以幂等地进入 OPEN。

对于包含 10 个 chunk 的批次：

~~~text
第 9 个不同 chunk Base Ready
  → base_remaining = 1
  → 下一批 OPEN
~~~

这里的 remaining 只统计尚未达到阶段一 Ready 的 chunk，不统计阶段二区间、scope、embedding 或阶段三状态。

### 5.3 OPEN 不等于瞬时启动整批

下一批进入 OPEN 后，必须渐进发放执行许可：

- 每轮最多发放 release_step 个许可；
- 两轮之间至少间隔 release_interval；
- 全局 Base 运行数量不得超过 base_max_inflight；
- release 对 chunk 必须幂等；
- 定时器重入或恢复不能重复提交同一个 chunk；
- 上一批尾部任务与下一批已释放任务可以重叠运行。

初始实现建议 release_step 为 1，但最终默认值仍需评审。

BaseBatchCompleted 可以作为观测事件保留，但不能作为下一批开启门槛。

下一批调度明确不等待：

- 当前批全部结束；
- 任意阶段二区间完成；
- scope 更新；
- embedding；
- 阶段三校正。

### 5.4 同一个 Base Ready 事件必须同时推进两个投影

阶段一完成事件至少有两个逻辑消费者：

1. Base Scheduler：更新批次 Base 完成度，并可能打开下一批；
2. Interval Readiness Reactor：更新包含该 chunk 的所有区间 watch。

它们必须都能观察到同一个逻辑事件。不能把事件放进一个由两者竞争消费的普通队列。

允许的实现形态包括：

- 一个按 Parse Job 串行的 reducer，在一次一致性边界中更新两个投影；
- durable event log 加两个独立 subscription；
- transactional outbox 加两个独立幂等消费者。

## 6. 阶段二：重叠区间就绪 Reactor

### 6.1 Interval Watch

每个区间维护：

~~~text
IntervalWatch
    interval_key
    start_index
    member_count
    required_mask
    ready_mask
    status
~~~

状态工作名：

~~~text
WAITING
QUEUED
RUNNING
DONE
FAILED_RETRYABLE
FAILED_FINAL
~~~

区间身份至少绑定：

~~~text
parse_job_id
parse_plan_revision
interval_start_index
interval_chunk_count
algorithm_revision
~~~

同一个 interval_key 逻辑上只能成功执行一次。基础设施重试必须复用同一个逻辑身份。

### 6.2 ready mask

长度为 K 的区间：

~~~text
required_mask = (1 << K) - 1
~~~

成员 chunk i Base Ready 后，设置它在对应区间中的 bit：

~~~text
member_offset = i - interval.start_index
ready_mask = ready_mask OR (1 << member_offset)
~~~

重复事件只会重复设置同一个 bit，不会重复计数。

区间就绪条件：

~~~text
ready_mask == required_mask
~~~

满足后通过原子状态转换：

~~~text
WAITING → QUEUED
~~~

只允许一个事件处理者成功把该区间放入 ready queue。

### 6.3 一个 chunk 只影响有限数量的区间

对 chunk i，包含它的合法窗口起点为：

~~~text
start_min = max(0, i - K + 1)
start_max = min(i, N - K)
~~~

只需检查：

~~~text
start_min ... start_max
~~~

单个 Base Ready 事件最多影响 K 个区间：

~~~text
事件处理复杂度：O(K)
区间状态总量：O(N)
~~~

chunk index 连续、窗口成员可直接由公式得到，应用层不需要红黑树。即使一次 Parse Job 有几万个 chunk，也不改变该判断。

### 6.4 epoll-style callback 的职责

callback / event handler 只负责：

1. 幂等记录 chunk Base Ready；
2. 找到包含该 chunk 的有限数量区间；
3. 设置对应 ready bit；
4. 判断区间是否凑齐；
5. 对首次就绪区间执行 WAITING → QUEUED；
6. 写入 ready queue 或 durable outbox；
7. 立即返回。

callback 禁止：

- 直接调用模型；
- 等待阶段二区间 Graph 完成；
- 执行 embedding；
- 执行阶段三校正；
- 在事件事务中做长耗时计算；
- 扫描全部区间。

ready queue 保存的是候选工作；worker 领取后仍需检查区间状态和成员基础结果是否有效。

### 6.5 重叠区间的独立性

阶段二区间必须满足：

- 012 和 123 可以任意顺序完成；
- 012 和 123 可以并行运行；
- 123 不等待 012；
- 012 不等待 123；
- 一个区间完成不会要求其他区间重算；
- scope 扩大不会重新激活已经 DONE 的区间；
- 每个区间只依赖自身成员是否全部 Base Ready；
- 区间之间的输出是相互补充的局部判断。

## 7. scope

### 7.1 正式定义

每个 chunk 维护一个主要供 Agent 读取的字段：

~~~text
scope: list[int]
~~~

scope 必须满足：

- 只包含 chunk index；
- 有序；
- 去重；
- 单调增加；
- 必须显式包含 chunk 自身；
- 不叫 coverage；
- 不保存 Local relation ID；
- 不保存阶段三 Global 标识。

初始化：

~~~text
scope(i) = [i]
~~~

这里不能用 [0,2] 表示“0 到 2”。scope 是显式列表：

~~~text
错误：scope = [0,2]
正确：scope = [0,1,2]
~~~

### 7.2 区间完成后的 scope 更新

阶段二区间 W 成功完成后，对每个成员 i：

~~~text
scope(i) := ordered_unique(scope(i) ∪ W)
~~~

只把“该 chunk 已有 scope”和“当前完整区间成员”做集合并。

示例：

~~~text
初始：
scope(0) = [0]
scope(1) = [1]
scope(2) = [2]
scope(3) = [3]

012 完成：
scope(0) = [0,1,2]
scope(1) = [0,1,2]
scope(2) = [0,1,2]

123 完成：
scope(1) = [0,1,2,3]
scope(2) = [0,1,2,3]
scope(3) = [1,2,3]
~~~

因此 chunk 1 通过 012 和 123 两个相互补充的区间，把参考 scope 从 [0,1,2] 扩展为 [0,1,2,3]。

### 7.3 并发语义

scope 更新必须是原子、有序集合并，不能使用覆盖式 last-write-wins。

集合并必须满足：

~~~text
幂等：S ∪ W ∪ W = S ∪ W
交换：(S ∪ W0) ∪ W1 = (S ∪ W1) ∪ W0
结合：(S ∪ W0) ∪ W1 = S ∪ (W0 ∪ W1)
~~~

因此多个重叠区间同时完成时，共同成员的 scope 不会因为提交顺序丢失元素。

物理实现可以选择 bitset、规范化 membership、数据库原子集合并或 CAS + retry；Agent 看到的逻辑值始终是有序、去重的 chunk index 列表。

### 7.4 scope 不参与区间 readiness

scope 是阶段二区间已经为某个 chunk 建立过哪些局部参考范围的元数据，主要供 Agent 读取。

scope 更新不会：

- 让已经完成的区间重算；
- 阻塞尚未完成但已凑齐的区间；
- 成为下一批 Base 调度条件；
- 承载阶段三 all_pre 标识。

## 8. 阶段二区间结果与重叠 Patch

012 和 123 都可能对 chunk 1、2 提出语义或 speaker 调整。当前已经确定：

- 两个区间可以并行；
- 两个区间逻辑上都只执行一次；
- 两个结果相互补充；
- scope 使用集合并；
- 不允许未定义的 last-write-wins；
- 不因冲突触发另一轮模型重算。

以下仍待评审：

- speaker / semantic 结果是保存为区间产物，还是物化回 chunk；
- 同一 chunk 收到多个区间判断时如何确定最终视图；
- 是否需要确定性 reducer；
- 哪些字段允许集合、投票、置信度选择或拒绝；
- 阶段二 Patch 的最终白名单；
- 并发提交的事务边界。

在归并规则确定前，实现不得假设“最后完成的区间覆盖之前区间”。

## 9. Embedding、召回与主流程结束

阶段一不直接生成最终召回向量。阶段二之后需要生成可召回版本，但一个 chunk 可能属于多个重叠区间，因此以下问题尚未确定：

- 等所有包含该 chunk 的区间都 DONE 后再 embedding；
- 允许阶段二结果分批进入召回并更新 revision；
- embedding 基于物化 chunk 视图，还是同时索引区间级结果；
- 阶段三修改语义后是否重新 embedding 并切换索引版本。

在该规则确定前，不能把“任意一个区间完成”直接等同于 chunk 已经达到阶段二 Ready，也不能固化主图结束条件。

已经确定的原则：

- embedding 必须绑定明确的内容 revision；
- 内容与向量版本不能静默不一致；
- 新索引版本失败时旧版本继续服务；
- 阶段三不能直接写 embedding 向量。

## 10. 阶段三：独立的全局前缀标识

阶段三维护独立于 scope 的 all_pre 类型标识。工作示例：

~~~text
global_prefix_marker = all_pre_23
~~~

具体名称和结构尚未确定。

必须保持：

~~~text
阶段二：
scope = [0,1,2,3]

阶段三：
global_prefix_marker = all_pre_23
~~~

禁止混成：

~~~text
scope = [0,1,2,3,"all_pre_23"]
~~~

阶段三与阶段二是相互补充的上下文来源：

- Global 标识不写入 scope；
- Global 不修改阶段二区间的 DONE 状态；
- Global 不触发阶段二区间重算；
- 阶段二区间不等待 Global；
- Local 与 Global 使用不同幂等身份和状态命名空间；
- Global 可以读取已经可用的阶段二结果，但其 readiness 和推进规则单独定义。

阶段三的准确触发条件、prefix 推进单位、可并发程度、输出 Schema 和召回切换规则仍待评审。

## 11. 工作事件模型

以下名称都不是最终命名：

| 工作事件 | 已确认语义 |
| --- | --- |
| BaseDispatchPermit | 允许指定 chunk 启动阶段一基础解析 |
| ChunkBaseReady | 指定 chunk 的阶段一基础结果可用 |
| BaseBatchTailReached | 当前门控批次进入尾部阈值，可打开下一批 |
| BaseBatchCompleted | 当前批全部阶段一完成，仅用于观测和最终统计 |
| IntervalReady | 区间成员全部 Base Ready，且区间尚未入队或执行 |
| IntervalCompleted | 区间已经产生唯一成功结果，可更新成员 scope |
| ChunkScopeExtended | 可选观测事件，不能反向触发阶段二区间 |
| GlobalPrefixEligible | 工作名，Global 前缀满足执行条件 |
| GlobalPrefixCompleted | 工作名，Global 前缀结果可用 |

同一个 ChunkBaseReady 必须同时推进 Base Scheduler 和 Interval Reactor。

## 12. epoll 思想在 Parse V2 中的边界

本文使用“epoll-style”描述以下机制：

~~~text
预先注册 interest
  → chunk 状态变化产生轻量 callback
  → callback 只更新 readiness 并把候选项入队
  → worker 消费 ready queue 并执行重任务
~~~

映射关系：

| epoll 概念 | Parse V2 |
| --- | --- |
| interest item | IntervalWatch |
| fd readiness | chunk Base Ready |
| waitqueue callback | Base Ready event handler |
| ready list | Interval ready queue |
| ONESHOT | 同一个 interval_key 只成功执行一次 |
| 返回前复查 | worker 执行前复查 watch 和成员结果 |

不照搬：

- Linux 的红黑树：区间可由连续 index 和公式定位；
- Linux 的内存 ready list：跨进程恢复需要 durable queue/outbox；
- fd 的 ET/LT 细节：业务事件还要满足幂等、重试和持久化要求。

Parse V2 的重点是“interest 与 ready 分离、callback 轻量、重任务延后”，不是复制 Linux 内核数据结构。

## 13. 当时设想的 Patch 白名单（历史探索）

以下是当前设计边界，最终字段名需结合真实 Schema 评审。

### 13.1 阶段二允许提出的修改

- 当前区间成员的局部语义修订；
- 当前区间成员的 speaker / 角色归类修订；
- 支撑修订的 evidence、reason 和 confidence；
- 对区间成员执行定义明确的 scope 集合并。

### 13.2 阶段三允许提出的修改

- 程序指定目标内的 compact 内容；
- 程序指定目标内的 canonical ASR 修订；
- 程序指定目标内的 speaker / 角色归类修订；
- 程序指定目标内的 canonical 视频语义修订；
- 支撑修订的 evidence、reason 和 confidence。

### 13.3 Agent 禁止修改

- video_id、chunk_id、sequence 等身份字段；
- chunk 开始时间、结束时间和原始媒体引用；
- Parse Plan、K 或区间成员；
- 原始 ASR、OCR 和模型证据；
- LangGraph 状态、revision 或幂等身份；
- embedding 向量、索引版本或数据库定位条件；
- scope 之外的 Global 标识，或 Global 标识之外的 scope；
- 白名单外任意字段。

### 13.4 提交原则

- Agent 只返回结构化 Patch，不持有通用数据库写权限；
- 宿主程序固定允许写入的 job、区间和 chunk 集合；
- 身份、revision、字段白名单和类型全部校验后才提交；
- 不合法 Patch 不得破坏上一阶段可用结果；
- Global 变化若影响召回语义，由程序触发重新 embedding 和索引切换；
- 重叠 Patch 的最终归并规则未确定前，不允许使用 last-write-wins。

## 14. 降级原则

| 失败位置 | 当前降级原则 | 禁止行为 |
| --- | --- | --- |
| ASR 或 OCR 单分支失败 | 是否允许剩余证据继续，待评审 | 静默伪造缺失结果 |
| 阶段一多模态理解失败 | 明确失败或进入有标识的基础降级结果 | 产出无来源的正常结果 |
| 阶段二区间失败 | 保留阶段一结果；其他区间继续；同一逻辑 run 可重试 | 伪装 DONE、更新 scope 或阻塞所有区间 |
| 阶段二 Patch 非法 | 拒绝该区间 Patch，保留已有结果 | 部分写入 |
| 阶段三失败或低置信 | 保留阶段二结果和已有召回版本 | 覆盖后无法恢复 |
| 重新 embedding / 索引失败 | 旧版本继续服务并记录未切换 | 内容已切换但向量仍是旧语义 |

共同原则：

- 后一阶段失败不能破坏最近一个已验证版本；
- 事件可能重复、乱序，所有状态更新必须幂等；
- 只有成功完成的阶段二区间才能更新 scope；
- 单个区间失败不能阻塞其他独立区间；
- 降级结果必须显式可观测，不能伪装成正常成功。

## 15. 状态不变量

### 15.1 Parse Plan

1. 每个 Parse Job 绑定唯一、不可变的 Parse Plan revision。
2. D、W 和 K 在运行前确定。
3. 阶段二区间只由完整 chunk 构成。
4. 阶段二区间使用 stride=1。
5. 区间成员由连续 index 和固定 count 定义，不由运行时 timestamp overlap 定义。

### 15.2 阶段一

1. 同一 Base revision 的 Ready 事件可以重复投递，但只能首次计入完成度。
2. 下一批开启只依赖阶段一批次剩余量。
3. 下一批开启后渐进释放，不能瞬时启动整批。
4. 阶段二、scope、embedding 和阶段三不能阻塞 Base Scheduler。
5. 全局 Base 并发不得超过配置上限。

### 15.3 阶段二

1. 每个合法起点对应一个固定完整区间。
2. 区间只有在所有成员 Base Ready 后才能入队。
3. 同一个 interval_key 逻辑上只成功执行一次。
4. 重叠区间可以并行。
5. 区间之间不等待。
6. 区间完成不触发其他区间重算。
7. scope 扩展不触发区间重算。
8. 基础设施重试复用相同逻辑身份。
9. 重叠结果不得使用未定义的 last-write-wins。

### 15.4 scope

1. scope(i) 初始为 [i]。
2. scope(i) 永远显式包含 i。
3. scope 只包含 chunk index。
4. scope 有序、去重、单调增加。
5. 区间 W 完成后，只对 i 属于 W 的成员执行 scope(i) 与 W 的集合并。
6. 并发更新必须保证集合并不丢失。
7. scope 不保存 Local relation ID。
8. scope 不保存 Global marker。
9. scope 主要作为 Agent 的局部参考范围。

### 15.5 阶段三

1. Global 使用独立标识和独立状态。
2. Global 标识不得混入 scope。
3. Global 不能改变阶段二区间的幂等状态。
4. 阶段二区间不等待 Global。
5. Global 的 readiness 和推进规则必须单独定义。

## 16. 待评审项

### 16.1 三个阶段的正式名称

- 三个 Graph / Subgraph 名称；
- Scheduler、Reactor、Watch 和 Agent 名称；
- Ready / Completed 事件名称；
- State 和持久化对象名称。

正式命名前，工作名不能演变为稳定 API。

### 16.2 短视频边界

当 N 小于 K 时，需要选择：

- 不执行阶段二；
- 把全部 N 个 chunk 作为降级区间；
- 由最小区间配置决定。

在评审确认前不能隐式采用任何一种。

### 16.3 重叠区间结果归并

- speaker / semantic Patch 的数据结构；
- 多个区间判断的最终读取方式；
- 是否物化 chunk 最终视图；
- 冲突处理和置信度策略；
- 并发事务边界。

scope 的集合并已经确定，不代表其他字段也能使用相同规则。

### 16.4 阶段二 Ready 与 embedding

- 一个 chunk 何时视为完成阶段二；
- 是否等待所有包含它的区间；
- 是否允许部分召回；
- 哪些 Evidence 应投影为 cue／event／chunk 等检索 atom；
- 每类 atom 如何构造 embedding 输入并关联 canonical chunk；
- 命中后展开怎样的 evidence window、如何聚合回 chunk 和精确播放范围；
- embedding／projection 使用哪个 revision；
- 后续区间结果使哪些 atom 失效、重建或重新发布。

### 16.5 阶段三

- all_pre 标识的正式名称与结构；
- prefix 推进单位和触发事件；
- 阶段三是否按 chunk、区间或其他单位运行；
- 是否允许有限并发；
- 阶段三结果与 retrieval revision 的关系；
- 是否需要重新 embedding。

### 16.6 调度参数

- base_batch_capacity；
- batch_tail_ratio 的离散取整；
- base_max_inflight；
- release_step；
- release_interval；
- 阶段二区间最大并发。

### 16.7 失败与恢复

- ASR/OCR 单分支降级；
- 阶段二区间最终失败后，对关联 chunk 的阶段完成判断；
- durable event/outbox 的存储实现；
- Job 恢复时如何重建 ready queue。

## 17. 当时的设计基线（历史探索）

截至旧方案停止作为正式基线时，当时的开发计划输入如下。当前仓库只建立了
`agent/video_parse_v2/` 与 `engine/parsev2/` 的隔离包边界，以下 LangGraph、State、
Event 和算法尚未实现，不能把目录存在视为 V2 已可运行：

~~~text
独立的 LangGraph Parse V2
+ 可配置 15s～60s 的完整 chunk 切分
+ 时间配置在规划期离散为完整 chunk 数量 K
+ 阶段一单 chunk 基础并行解析
+ 当前批剩余至尾部阈值后打开下一批
+ 下一批渐进释放，而不是瞬时全开
+ Chunk Base Ready 同时推进 Scheduler 与 Interval Reactor
+ stride=1 的全部重叠完整 chunk 区间
+ 区间凑齐即独立执行一次，不等待、不重算
+ scope 是显式、有序、去重且包含自身的 chunk index 列表
+ 重叠区间通过 scope 集合并相互补充
+ Global 使用独立 all_pre 类型标识，不混入 scope
+ 程序固定写入作用域，Agent 只能提交白名单 Patch
~~~

这条“待定项不能反向改变区间与 scope 语义”的限制只属于旧方案，现已作废。三阶段、区间、
scope、Patch 和对应 Embedding 时机若被重新采用，必须作为某个 Parse Strategy 重新评审，
不能约束本文开头的通用 Schema-driven Harness。
