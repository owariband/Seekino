# Parse V2 事件驱动解析设计

> **后续基线（2026-10-17）**：任务模型、Index 归属与恢复以 [Parse Agent TRD 的 2026-10-14 基线](../../agent/video_parse_v2/design/trd.md) 为准，取代本文历史 Interval INDEX 描述；Interval 仅 Refine，ChunkTask 合并 Parse/Index。新增 Event 派生持久化及同步见 [Event TRD](event-retrieval-trd.md)：保留完整 Chunk Timeline，在全部覆盖 Refine 提交后由既有 Chunk INDEX 建立，待实现。本文保留算法与历史记录，不重复维护新 Event 合同。

状态：实现合同已收敛（2026-09-28）；历史工作名仅用于解释演进
原始讨论图是本地设计素材 `parse_big_graph.png`；本文文字与状态不变量才是可 Review 的规范来源。

## 1. 文档目的

本文固化 Parse V2 当前已经确认的算法拓扑、并发调度、区间就绪机制、状态不变量、Patch 边界和降级原则，使讨论结论成为可检索、可 Review 的正式设计。

本文中的正式算法边界是 **VideoParseV2**：一次 Graph Run 只解析一个 Video。跨 Video
排队、租户隔离、资源配额和版本选择不进入该 Graph，由 Server 在运行边界之外负责。
下文为行文简洁仍简称 Parse V2。

本文刻意区分：

- **已确认约束**：后续实现不能偏离；
- **当前算法结论**：算法语义已经收束，实现状态按本文第 17 节及 handoff 验证；
- **工作名**：为了描述流程临时使用，不代表最终 Graph、Node、Event、State 或数据库命名；
- **后续范围**：未纳入本批的能力不能在实现时自行扩展。

三个处理层次的正式名称目前尚未确定。本文统一使用“阶段一 / 阶段二 / 阶段三”描述职责；括号中的 Base、Local、Global 仅是工作名。

## 2. 硬约束

### 2.1 Parse V1 与 Parse V2 必须物理隔离

- 当前解析实现整体归入 `engine/parsev1/`，作为可独立调用、可独立测试的 V1 实现。
- V2 的 LangGraph program、State、Event、Reactor 与子图编排归入
  `agent/video_parse_v2/`。
- V2 的领域算法、窗口／关系策略、Patch 规则以及 retrieval projection policy 归入
  `engine/parsev2/`，不得依赖 LangGraph 或 Server。
- `agent/video_parse_v2/` 与 `engine/parsev2/` 都不导入、调用或拼接
  `engine/parsev1/` 的 Pipeline、Node、State、Agent、Prompt 或阶段实现。
- `engine/parsev1/` 同样不依赖任何 V2 包。
- 一次解析任务只能选择 V1 或 V2，不能在一次运行中混用两个版本的阶段。
- 版本选择、Job、Lease、重传、恢复和 durable event/outbox 属于 `server/`；版本选择器
  只负责选择入口，不参与解析业务。
- 通用且与解析算法无关的基础设施是否可以复用，需要在开发计划中逐项列明；业务图、状态和算法实现不能借此混用。

### 2.2 Parse V2 必须使用 LangGraph

- Parse V2 的主流程、条件路由、动态任务、子图执行、事件归约和结束条件由 LangGraph 表达。
- 不能只在最外层包一个 LangGraph 节点，内部继续调用 V1 Pipeline。
- 单 chunk 基础解析、阶段二区间纠偏和阶段三全局校正都必须是 Parse V2 自己的 Graph 或 Subgraph。
- 事件传输、持久化队列和定时唤醒可以作为运行时基础设施，但业务调度决策必须属于 Parse V2。

### 2.3 四层职责边界

| 目录 | Video Parse V2 职责 |
| --- | --- |
| `agent/video_parse_v2/` | LangGraph program、State、Event、Reactor、调度与子图组合 |
| `engine/parsev2/` | 与运行框架无关的 Parse 算法、关系合并、Patch 校验、Evidence／Retrieval Projection 策略 |
| `gateway/agent_model/` | Provider 无关的模型执行协议、上下文治理、Tool Loop 与 Provider fallback；不理解 Video Parse 状态 |
| `server/` | Job、Lease/Fencing、重传、恢复、持久化、版本选择、API 与外部事件交付 |

Graph 使用 Engine 策略并通过 Gateway 调用模型；Server 托管 Graph 的可靠执行，但不能把
节点路由、区间 readiness 或 Patch 决策重新实现一遍。

## 3. 当前算法结论

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

当 `0 < N < K` 时只建立唯一缩短区间 `[0,N)`，照常执行 Refine 和索引；
它不是失败降级，也不能因为缺少完整窗口而跳过阶段二。

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

区间产物独立持久化；模型输出经过 Engine 白名单和成员／时间范围校验后，实际修改字段
物化回 Chunk 当前正文。不同区间修改同一字段时后提交值生效，未修改字段保留数据库当前值；
`scope` 始终从数据库当前值做有序集合并。正文修改、scope 合并、区间产物和 Interval
进入 `INDEXING` 在一个事务中完成，不因内容版本变化重新调用模型。

## 9. Embedding、召回与主流程结束

阶段一不直接生成最终召回向量。一个 Chunk 只有在包含它的全部 Interval 都已提交 Refine
后才放行；判断的是 `INDEXING / INDEXING_FAILED / COMPLETE`，不等待相关 Interval 索引完成。
索引始终读取物化后的当前 Chunk 正文和内容版本，经 Engine Projection 生成词项与两类向量输入。

- embedding 与 ES 文档都绑定内容版本和固定编码合同；
- 相容的已有向量可以复用，其余表示重新编码；
- ES 写入使用稳定文档 ID、逐项结果检查和外部版本保护；
- 对外检索继续只允许选定状态为 `COMPLETE` 的 VideoParseTask。重解析期间即使 ES 尚有旧记录，
  也不能从非 `COMPLETE` 任务返回旧值；
- Prefix 修改检索正文时由程序递增版本并恢复相关 Interval `INDEXING`，Prefix 自身不写向量。

## 10. 阶段三：独立的全局前缀标识

阶段三由 `PrefixContextAgent` 维护独立于 scope 的持久 compact 和升序游标。概念示例：

~~~text
global_prefix_marker = all_pre_23
~~~

物理状态保存在 VideoParseTask 的 Worker 私有 detail 命名空间，包含独立 lease、retry、
下一 Chunk 游标、Video compact 和 Series compact，不进入可持久化 LangGraph RunState。

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

Prefix 只处理最左侧满足条件的 Chunk：固定前序集和本集前缀已就绪，且目标 Chunk 的全部已创建
Interval 已提交 Refine。它失败时停留在原游标独立重试，不阻塞主解析完成；正文改变时按第 9 节重建索引。

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

## 13. Patch 白名单

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
- 重叠 Patch 只对本次实际修改字段采用提交顺序语义；scope 使用数据库当前值的集合并。

## 14. 降级原则

| 失败位置 | 当前降级原则 | 禁止行为 |
| --- | --- | --- |
| ASR、OCR 或模型请求失败 | 当前执行任务失败并按固定配置退避重试 | 静默伪造缺失结果或换用重启后的默认模型 |
| 阶段一多模态理解失败 | 明确失败或进入有标识的基础降级结果 | 产出无来源的正常结果 |
| 阶段二区间失败 | 保留阶段一结果；其他区间继续；同一逻辑 run 可重试 | 伪装 DONE、更新 scope 或阻塞所有区间 |
| 阶段二 Patch 非法 | 拒绝该区间 Patch，保留已有结果 | 部分写入 |
| 阶段三失败或低置信 | 保留已提交主解析结果，后台游标独立重试 | 覆盖后无法恢复 |
| 重新 embedding / 索引失败 | 记录 `INDEXING_FAILED`，保留 MySQL 正式成果并从索引阶段恢复 | 把非 `COMPLETE` 任务的旧 ES 值继续对外返回 |

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

## 16. 2026-09-28 已确认的实现合同

本节替代本文件早期版本中关于短视频、冲突重算、Embedding Ready 和 Global
推进仍待评审的表述。字段、状态机和事务细节以最新 Agent / Engine TRD 为准。

### 16.1 窗口与短视频

- `K = ceil(interval_duration / chunk_duration)`，且 `1 <= K <= 63`。
- `N >= K` 时只建立起点 `0..N-K` 的完整 stride=1 滑窗，不额外建立尾窗。
- `0 < N < K` 时建立唯一缩短窗口 `[0,N)`；它与完整窗口一样执行 Refine 和索引。
- Interval 使用持久化的 `start_sequence_no + chunk_count` 和成员任务 ID；所有成员判断均采用半开区间。

### 16.2 Refine 写回与 scope

- 每个 Interval 在自身成员基础成果全部提交后独立执行，不等待相邻窗口。
- 模型只提出白名单字段更新；宿主计算实际变化并只写实际修改字段，同字段后提交值生效。
- 提交时从数据库读取每个成员的当前 `detail.scope`，与本窗口成员做有序、去重的集合并；
  不使用旧快照覆盖，也不因并发冲突重新调用模型。
- 只有检索正文实际变化才递增内容版本；仅 scope 或 compact 元数据变化不重建索引。

### 16.3 逐 Chunk 索引放行

- Interval Refine 与内容更新、scope 并集和自身 `INDEXING` 在同一 MySQL 事务提交。
- 对某个 Chunk，只有包含它的全部 Interval 都已提交 Refine，才允许读取当前正文和版本执行 Projection、
  embedding／复用和 ES 写入。`INDEXING`、`INDEXING_FAILED`、`COMPLETE` 都表示 Refine 已提交。
- 不等待相关 Interval 全部 `COMPLETE`；否则索引与 Interval 完成条件会互相等待。
- Interval 可先处理已放行成员。其余成员等待通知或数据库扫描时保持 `INDEXING`，不持租约、
  不记失败、不增加 retry。
- ES 使用稳定文档 ID 和外部版本保护；逐项确认 bulk 结果和查询可见性。索引重试只读当前成果，
  不重跑 Understanding 或 Refine。

### 16.4 调度、恢复与 Prefix

- `batch_max_size` 同时限制所有开放批次的活跃 Chunk；下一批只依据当前批基础 `COMPLETE`
  达到 `ceil(batch_size * 0.9)` 开放，不等待 Refine 或索引。
- DB 状态、租约版本和内容版本是恢复事实源；通知只缩短等待，周期扫描补偿丢通知。
- Prefix 使用 VideoTask `detail` 中独立的持久游标和租约命名空间，按固定前序推进，不参与主解析完成屏障。
- Prefix 修改检索正文时递增版本，并仅把包含目标且已经提交 Refine 的 Interval 恢复为 `INDEXING`；
  未提交 Refine 的 Interval 不得越级。

## 17. 当前实现基线

实现按以下稳定边界组织：Agent 持有 LangGraph 编排，Engine 提供规划、Schema、校验和 Projection，
Server Repository 提供原子 claim／lease／成果事务，Server Worker 装配媒体、模型与 ES 网络边界。
V1 与 V2 通过显式执行模式互斥；V2 的模型身份和请求参数随任务保存，凭据只保留在运行环境。

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
+ Prefix 使用独立持久游标和 compact，不混入 scope
+ 程序固定写入作用域，Agent 只能提交白名单 Patch
~~~
