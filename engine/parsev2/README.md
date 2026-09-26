# Video Parse V2 Engine

状态：职责边界已确认；Canonical Evidence、Patch Reducer、Retrieval Projection 与发布 Revision 仍待评审。

本目录是 Video Parse V2 的领域算法边界。完整事件驱动拓扑、并发调度和状态不变量见
[`docs/design/parse-v2-harness.md`](../../docs/design/parse-v2-harness.md)；LangGraph Harness
位于 [`agent/video_parse_v2/`](../../agent/video_parse_v2/README.md)。

Engine 不应该做成一个“大而全的 Engine 类”，也不应该把所有能力都暴露成 Agent Tool。
它应该是一个可独立测试的领域算法库，分成 `parsev2` 和 `retrieval` 两个边界。

其中：

- `parsev2` 负责把模型观察变成可信、版本化的 Canonical Evidence；
- `retrieval` 负责把 Canonical Evidence 变成可检索表示，并完成召回、融合、Rerank；
- Agent Harness 只负责编排什么时候调用这些能力；
- LLM 只提交 Proposal，不能直接修改 Canonical State。

## 1. 推荐的 Engine 结构

不建议一开始创建几十个 `manager/service/factory/port`。按照目前已确定的语义，先保持下面这个规模：

```text
engine/
├── README.md
├── parsev1/                         # 已冻结的 V1
│
├── parsev2/
│   ├── __init__.py                  # 只导出稳定的公共 API
│   ├── schemas.py                   # ParsePlan、Evidence、Canonical Snapshot
│   ├── planning.py                  # D/W/K、完整 chunk、interval 规划
│   ├── intervals.py                 # interval membership、ready mask、scope 集合并
│   ├── patches.py                   # Agent Proposal、白名单校验、确定性归并
│   └── projection.py                # Canonical Evidence → RetrievalAtom
│
└── retrieval/
    ├── __init__.py
    ├── contracts.py                 # SearchRequest/SearchResult/Hit/Trace
    ├── video_retrieval.py           # 保留现有实现，逐步迁移而非先做空 facade
    ├── representations.py           # Atom → lexical/dense/vision 索引输入
    ├── lexical.py                   # normalize、Exact、BM25、字符 n-gram
    ├── ranking.py                   # Family 合并、分数规范化、RRF
    └── rerank.py                    # 候选特征、Rerank、no-result 决策
```

这个目录不是按 LangGraph 节点切，而是按稳定的领域概念切。

暂时不要创建：

```text
engine/parsev2/base_manager.py
engine/parsev2/local_service.py
engine/parsev2/global_factory.py
engine/common/utils.py
engine/interfaces/retrieve_client.py
```

原因是 Base/Local/Global 仍然是工作名，而 `manager/service/factory` 也没有形成稳定抽象。
正式名称确定前，这些工作名不能演变为稳定 API。

## 2. Engine 最核心的四层数据

Engine 内部真正要建立的是这四层，而不是一个巨大的万能 Schema。

### 2.1 Raw Evidence：不可篡改的原始证据

```text
RawAsrCue
RawOcrObservation
RawVisualObservation
ModelProvenance
MediaReference
```

保留：

- 原始文本；
- 原始时间；
- 模型和版本；
- 证据帧；
- 置信度；
- 缺失/降级标记。

后续 Agent Patch 不允许修改这一层。

### 2.2 Agent Proposal：不可信的模型提案

例如：

```text
BaseObservationProposal
IntervalCorrectionProposal
GlobalCorrectionProposal
SpeakerCandidateProposal
```

它只允许描述：

- 建议修订哪个语义；
- 建议将某个说话人识别成谁；
- 引用了哪些 Evidence；
- confidence 和 reason；
- 允许修改的白名单字段。

它不能携带：

- `chunk_id` 的任意覆盖；
- revision；
- interval 成员；
- scope；
- embedding；
- 数据库 ID；
- Qdrant point。

Agent 只提交结构化 Patch，不持有 Canonical State 或通用数据库写权限。

### 2.3 Canonical Evidence：Engine 验证后的可信视图

```text
CanonicalVideoSnapshot
├── chunks
├── utterances
├── events
├── visual_observations / shots
├── speaker_assertions
├── scopes
├── applied_interval_results
└── content_revision
```

Engine 负责：

- 时间和归属校验；
- Evidence 引用校验；
- Patch 白名单；
- 重叠区间结果归并；
- speaker 冲突处理；
- scope 有序集合并；
- 生成当前可读取视图；
- 判断哪些检索内容已经过期。

### 2.4 Retrieval Projection：解析与检索之间的交接物

这是整个架构里最关键的接口。

```text
RetrievalAtom
├── atom_id
├── atom_type                 # utterance / event / chunk_visual / shot
├── video_chunk_id
├── start_seconds
├── end_seconds
├── lexical_text
├── semantic_text
├── media_reference
├── speaker_candidates
├── source_evidence_ids
└── content_revision
```

Parse V2 不应该直接输出“一个大段 compact + 一个向量”。

它应该输出一组有来源、能独立重建的 `RetrievalAtom`。然后 `engine/retrieval` 再决定：

- 哪些 Atom 进入 Exact Phrase；
- 哪些进入 BM25/n-gram；
- 哪些生成 Text Embedding；
- 哪些生成 Vision Embedding；
- 命中 Atom 后怎样展开到视频播放范围。

这样 Parse Graph 和检索算法才能真正解耦。Projection 描述需要索引的内容，不保存
Provider、数据库连接、Qdrant point 或最终 embedding 向量。

## 3. Harness 怎么使用 Engine

这里要区分“Agent Harness 使用 Engine”和“LLM 使用 Tool”。

> 绝大部分 Engine 能力只由 Harness 代码调用，不应该暴露给 LLM 当 Tool。

例如 interval readiness、scope 合并、Patch 校验，都应该是普通 Python 调用：

```text
LangGraph Node
    ↓
调用 Gateway，让模型生成 Agent Proposal
    ↓
调用 Engine 校验并应用 Proposal
    ↓
Engine 返回可信的 PatchDecision / CanonicalDelta
    ↓
Harness 更新 State、发布 Event
    ↓
Server 持久化
```

可以用这个公式概括：

```text
Host Authority Envelope
+ Agent Proposal
→ Engine Decision
```

其中：

- Host Authority Envelope：程序指定本次允许操作的 job、interval、chunk、revision；
- Agent Proposal：模型提出的语义修订；
- Engine Decision：接受、拒绝、Canonical Delta、受影响的 Retrieval Atom。

示意代码会是这种形态：

```python
proposal = model.run_structured(
    prompt,
    result_type=IntervalCorrectionProposal,
    tools=[],
    ...
)

decision = apply_interval_patch(
    target=interval_target,       # Harness 固定，模型不能修改
    snapshot=current_snapshot,
    proposal=proposal,
)

if not decision.accepted:
    # Harness 决定进入失败、重试或降级路径
    ...

# Harness 把 Engine 已验证的结果写回 Graph State
return {
    "canonical_delta": decision.canonical_delta,
    "changed_atom_ids": decision.changed_atom_ids,
}
```

关键原则是：

> Prompt 可以要求模型遵守规则，但 Engine 必须再次用代码强制验证；不能把安全边界寄托在 Prompt 上。

## 4. Engine 给 Parse Harness 的最小公共接口

不建议先建一个万能 `ParseV2Engine` 类。第一版只需要少量稳定函数：

```text
build_parse_plan(video, config)
    → ParsePlan

build_interval_watches(plan)
    → list[IntervalWatchSpec]

mark_chunk_base_ready(watch, chunk_index)
    → ReadinessDelta

validate_base_proposal(target, raw_evidence, proposal)
    → BasePatchDecision

apply_interval_patch(snapshot, target, proposal)
    → PatchDecision

materialize_canonical_view(snapshot)
    → CanonicalVideoSnapshot

build_retrieval_projection(snapshot)
    → RetrievalProjection
```

这些名称表达目标语义，不代表待评审的工作名已经成为稳定 API。真正导出前必须完成 Schema
与归并规则评审。

Engine 返回的是领域计算结果，不返回：

- LangGraph `Command`；
- 下一个节点名称；
- 数据库操作；
- Qdrant 操作；
- Event Queue 消息；
- Provider SDK 响应。

例如 `mark_chunk_base_ready()` 可以返回：

```text
ReadinessDelta
├── ready_mask
├── became_ready
└── affected_interval_key
```

然后由 Agent Harness 决定是否产生 `IntervalReady` Event。这样 `engine` 不依赖 LangGraph，
但 Harness 也不需要自己重新实现 ready-mask 算法。

## 5. Chat Agent 怎么使用 Retrieval Engine

Chat Agent 的交互方式与 Parse Harness 不完全相同。

Parse Harness 可以直接调用 Engine 的纯函数；Chat Agent 则应该通过一个注入的搜索入口：

```text
Query Agent
   ↓ 调用 retrieve Tool
Agent Tool
   ↓ 调用注入的 search()
Server VideoRetrievalService
   ├── 固定 workspace / series / budget
   ├── 调 Gateway 生成 Query Embedding
   ├── 调 MySQL / Qdrant 获取各通道候选
   ├── 调 Engine 做 Lexical Family / Fusion / Rerank
   └── hydrate Canonical Evidence
         ↓
      SearchResult
         ↓
Agent Workspace 保存完整 Evidence
         ↓
LLM 只看到有限的结果卡片和 Evidence Handle
```

现有 `agent/chat/video_search/AgentRuntime` 已经采用这个方式：构造时注入 `retrieve`
callable。这个模式应该保留。

未来只是把现在不断增长的参数：

```python
retrieve(
    query,
    top_k,
    transcript_weight,
    text_weight,
    vision_weight,
)
```

收敛为结构化命令：

```text
SearchCommand
├── authority
│   ├── workspace_id          # Host 固定
│   ├── series_ids            # Host 固定
│   └── maximum_top_k         # Host 固定
└── plan
    ├── intent                # best_match / enumerate / count
    ├── lexical_query
    ├── semantic_query
    ├── visual_query
    └── channel_preferences   # Agent 可建议，Engine 做限制和归一
```

Agent 可以表达“我要找原台词”“我要枚举全部 occurrence”，但不能控制：

- 搜索其他 workspace；
- 任意扩大 Series 范围；
- 指定 Qdrant collection；
- 指定内部 revision；
- 修改 RRF 公式；
- 绕过 no-result 阈值；
- 直接读向量。

## 6. 现在真正需要决定的东西

目录本身不是最大的难点。真正阻塞 Engine 实现的是以下四组决策。

### P0：Canonical Evidence Schema

必须先决定：

- `Utterance / Event / Shot / VisualObservation` 的最小字段；
- Raw ASR 与 Canonical ASR 如何分层；
- speaker 是唯一值还是 candidates/assertions；
- Evidence ID 怎样稳定；
- 时间精度使用整数秒还是毫秒；
- 一个 occurrence 跨 cue 时如何表示；
- compact 是正式事实还是派生展示文本。

建议：

- Raw Evidence 不可变；
- Agent 只产生带 Evidence 引用的 Proposal；
- speaker 保存 assertion/candidates，不强迫唯一答案；
- compact 从 Canonical Evidence 重新生成，不接受区间 Agent 任意覆盖；
- 稳定 ID 与 revision 由宿主程序管理。

### P0：重叠 Patch 的归并规则

当前设计已经明确禁止 last-write-wins，但还没有定义 reducer。

推荐第一版：

- 每个 Interval Result 保存为不可变产物；
- 不允许 Interval 直接覆盖 Chunk；
- 用确定性 Reducer 物化 Chunk 当前视图；
- semantic facts 按 Evidence 身份去重合并；
- speaker 保存多条 assertion，再根据置信度和证据规则生成 canonical candidate；
- 冲突无法解决时保留 ambiguous/unknown，不再调用模型重算。

这样无论 `012` 和 `123` 谁先完成，最终视图一致。

### P0：什么时候允许发布检索版本

第一版建议：

```text
所有包含 chunk(i) 的阶段二区间
已经 DONE 或 FAILED_FINAL
        ↓
chunk(i) 进入 LocalSettled
        ↓
物化 Canonical Chunk View
        ↓
生成一次 Retrieval Projection
        ↓
生成 embedding 并发布 revision
```

也就是第一版先不要做“每完成一个重叠区间就重新 embedding”。

原因是那会立刻引入：

- 多次向量费用；
- 旧 Atom 失效；
- Index revision 切换；
- 查询时混合版本；
- 重复检索结果；
- 大量一致性测试。

阶段三不阻塞第一版召回。Global 成功后再产生新的 Canonical Revision，并触发独立的重新投影和索引切换。

### P0：Retrieval Atom 和命中展开方式

第一版建议只做三类核心 Atom：

| Atom | 主要检索方式 | 用途 |
| --- | --- | --- |
| `utterance` | Exact + BM25 + 字符 n-gram，可选 Dense | 原台词、短词、人物说过什么 |
| `event` | Semantic Dense | 剧情、动作、关系、情绪 |
| `chunk_visual` | Vision Dense | 画面、物体、动作、构图 |

等 Shot Detection 真正稳定后，再加入 `shot` Atom。

`compact` 暂时作为阅读上下文或 event/chunk 的派生文本，不作为一张独立的 RRF 票。
除非离线评测证明它确实带来增益。

命中应该先落到 occurrence/atom：

```text
utterance hit
  → 精确时间范围
  → 所属 canonical chunk
  → 展开前后 Evidence Window
  → 最后再聚合成播放片段
```

不能一开始就把一切压回 60 秒 Chunk，否则 Utterance Schema 的意义又丢了。

## 7. 哪些决策可以晚一点

这些不应该阻塞 Engine 核心合同：

- 三个阶段的最终宣传名称；
- `base_batch_capacity` 的最终默认值；
- `release_step` 是 1 还是 2；
- Provider 和模型选型；
- Qdrant 的物理 collection 名称；
- 阶段三的完整优化策略。

但以下不能边写边猜：

- Canonical Evidence Schema；
- Patch reducer；
- LocalSettled/发布条件；
- Retrieval Atom；
- content revision 与 index revision；
- SearchRequest/SearchResult 合同。

## 8. 推荐实施顺序

```text
1. 冻结 Canonical Evidence / Agent Proposal / RetrievalAtom 三套 Schema
2. 实现 ParsePlan、interval、ready mask、scope 的纯函数和不变量测试
3. 实现 Patch 白名单与确定性 reducer
4. 用 Fake Proposal 跑通最小 LangGraph，不接真实模型
5. 实现 Retrieval Projection
6. 先做 Utterance Exact/BM25 + Event Dense + Chunk Vision
7. 建立离线评测后再做 RRF 调权、Rerank 和 no-result
8. 最后接 Server 的 revision/index 切换和真实 Gateway 模型
```

最重要的一句话是：

> Agent Harness 不需要“获得 Engine 的所有能力”；Harness 只需要获得少数强类型的 Proposal Schema 和纯函数入口。LLM 提建议，Harness 控制权限，Engine 判定是否合法，Server 才负责持久化。

## 9. 硬性职责边界

本目录允许拥有：

- Parse V2 领域 Schema 与不变量；
- Parse Plan、完整 chunk 和 interval 算法；
- ready mask、scope、Patch 白名单和确定性 reducer；
- Canonical Evidence 物化规则；
- Retrieval Projection policy；
- 可脱离 LangGraph、Gateway、Server 独立运行的测试。

本目录禁止拥有：

- LangGraph Graph、Node、State、Event、Command 或路由；
- Prompt 拼装、Tool 注册、模型上下文和对话历史；
- Provider SDK、API key、模型 fallback 实现；
- Job、Lease/Fencing、Checkpoint、Artifact、durable outbox；
- MySQL Repository、Qdrant Client、事务和索引切换；
- HTTP/API DTO、租户鉴权或前端交互；
- 对 `engine/parsev1` 的导入、调用或兼容转发。

职责判断标准：

- 在定义“数据是什么、是否合法、如何确定性计算”时，属于 Engine；
- 在决定“下一步运行哪个 Node、什么时候调用模型”时，属于 Agent Harness；
- 在处理“如何可靠保存、恢复、重试、切换版本”时，属于 Server；
- 在处理“怎样调用某个模型供应商”时，属于 Gateway。
