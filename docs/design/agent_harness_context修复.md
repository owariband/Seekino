#user

有没有研究context隔离的?我觉得本质上是context隔离
我们某一个特定的adk将其对标为coding agent,让她能用一系列tool,并且能自主进行compact
对于每一个召回的片段只给出一定的meta info,先召回description和相应id,提供额外too来拿到这个的全量信息
工具执行超出一定的字数范围后立马做拦截
然后提供一个草稿纸context,就是那个query需要写的draft,原本是放在query的context里面,我们独立出来,放在graph里面的另外一个空间,提供一个tool可以查看draft里面的备选item,同样也是要提供两层:description和具体全量内容的查看层
然后evalutora没有draft的修改权,只能通过get tool查看draft里面的候选item以及其evidence(query给的)
然后根据user input决定是否放行

我考虑这样是不是就能防止context爆炸了?

#chat

有，而且你描述的方案几乎正中最近这批研究的核心。更准确的名字不是单纯的 `context isolation`，而是：

> **Role-scoped、addressable、budget-enforced context runtime**
> 角色隔离 + 可寻址外部状态 + 强制预算的上下文运行时。

你的直觉是对的：**Prompt 不应该是 Agent 的数据库，只应该是当前调用的 working set。Graph/DB 才是 canonical state，Tool 是分页换入机制。**

## 最接近你设计的研究

### 1. VISTA：和你的方案几乎一一对应

[VISTA: LLM Agents Are Latent Context Managers](https://arxiv.org/html/2606.30005) 是目前最接近的，但截至当前仍是 arXiv 预印本，不是已确认顶会论文。

它做了这些事：

- 每段上下文变成 typed、addressable block；
- 每个 block 有稳定 ID、类型、token 数、最近访问时间、是否归档；
- Agent能看到一个 context dashboard；
- 大块内容可以 archive 到外部存储；
- Prompt 中只留下 handle 和摘要；
- Agent可按 handle 重新读取完整 payload 或指定范围；
- 每次模型调用之前都有硬 preflight，保证输入不超过预算；
- 如果上下文已经超限，就暂时禁用普通任务工具，只允许 Agent 做 archive/delete/recovery；
- 新 Tool Result 太大时，不允许它先进入 messages 再处理，而是直接外置，只返回一个可恢复的 placeholder。

VISTA甚至明确写了：

```text
模型操作的不是 raw transcript，
而是 visible blocks + archived handles + dashboard
```

这就是你的：

```text
description + ID
       ↓
按 ID 查看详细内容
       ↓
详细内容仍可再次归档
```

它还给出了一个非常重要的结论：**只有可恢复性还不够，Agent 还得知道每块内容有多大、多久没用、预算还剩多少。**否则 Agent 不知道应该 compact 哪一块。

### 2. ACM：Agent 自主决定什么时候 compact

[ACM: Agentic Context Management](https://arxiv.org/html/2607.23809) 同样是近期预印本。

它把 context management 直接做成 Agent 可调用的工具：

- Agent自行决定何时 compact；
- 被移出工作上下文的内容进入外部 memory；
- 原始内容保留，因此是 lossless；
- 以后可以按需 query memory；
- 训练模型学习什么时候使用 context editing tools。

这对应你说的“把 ADK 对标 Coding Agent，让她能自主 compact”。

不过这里一定要区分：

- Agent自主 compact：是一种提高效率的策略；
- Harness硬预算检查：才是防止 overflow 的保证。

不能把“模型应该记得 compact”当作系统安全边界。

### 3. CAT：固定区、长期区和近期工作区隔离

[Context as a Tool](https://aclanthology.org/2026.findings-acl.1032/) 是 ACL 2026 Findings，不算 CCF A 主会，但结构与你的想法接近。

它把上下文划成：

```text
Fixed Segment
  - System Prompt
  - User Intent

Long-Term Memory
  - 历史阶段的结构化摘要

Working Memory
  - 最近 k 轮的完整 ReAct 轨迹
```

然后 context management 本身也是一个 Tool。模型可以在里程碑、策略切换、重复失败等时机主动调用。

这证明“context 分区”并不是仅仅把 prompt 切成几个标题，而是不同区域必须具有不同生命周期：

- User Intent 不允许被 compact；
- Long-term memory 可以更新；
- Recent working memory 保留高保真；
- 更早的原始内容在外部仍然可回读。

### 4. COMPASS：角色之间的 context 隔离

[COMPASS，ACL 2026 主会](https://aclanthology.org/2026.acl-long.152/) 是顶会里最接近“不同 Agent 不应该共享同一坨 Context”的工作。

它的 Main Agent、Meta-Thinker、Context Manager 各有独立上下文：

- Main Agent 看当前执行需要的上下文；
- Meta-Thinker 看足够判断继续/换路/停止的信息；
- Context Manager 读取 notes、当前轨迹、战略信号，生成下一阶段视图。

它不是让所有 Agent共享整个 history，然后靠 prompt 说“请只关注属于你的部分”。

### 5. DACS：显式提出 Context Isolation

[DACS](https://arxiv.org/html/2604.07911) 是一篇预印本，而且是独立研究者工作，不应当按顶会成果看待；但概念很直接。

它给每个 Agent 一个独立的 full context，Orchestrator 平时只看每个 Agent 不超过 200 token 的 registry summary。只有某个 Agent 请求 steering 时，才进入：

```text
Focus(agent_i)
= agent_i 的完整上下文
+ 其他 Agent 的轻量 registry
```

论文把核心 invariant 写成：

```text
每次调用前，Context Builder 必须保证 |C| ≤ T
```

你给 Query、Evaluator 做角色隔离，本质上就是这个方向。

## 你的方案能不能防止 Context 爆炸？

**能，但必须补齐几个硬约束。**

如果只是：

```text
数据放 Graph
→ Agent用 Tool 读取
→ 偶尔 compact
```

还不能保证。因为 Graph 里的内容仍可能被一次性序列化进 prompt，`get_full` 也可能一次返回 200K token，历史 ToolMessage 也可能在下一轮全部重新出现。

真正的保证来自下面四层隔离。

### 1. Storage Isolation：完整数据永远不直接属于 messages

```text
EvidenceStore
  └── 完整 transcript/events/caption

DraftStore
  └── 完整 DraftItem 和版本

Agent Graph State
  └── 运行状态、引用、审核结果

LLM Messages
  └── 只包含本轮投影出来的 bounded view
```

关键是：

> Graph State 和 Model Context 必须是两个不同概念。

LangGraph state 里有完整 Evidence 没关系，但构造 Query/Evaluator messages 时只能走显式 allowlist projection。不能把整个 state dump 给模型。

### 2. View Isolation：每个角色只能看到职责需要的数据

我建议明确成下面这样：

| 区域 | Query Agent | Evaluator | Chat |
|---|---:|---:|---:|
| 冻结后的 User Intent | 读 | 读 | 读 |
| Series/Episode metadata | 有界读 | 不需要或轻量读 | 不需要 |
| Candidate Cards | 读 | 只读 selected cards | 不读 |
| 完整 Evidence | 按 ID 有界读 | 只读被引用 Evidence | 默认不读 |
| Draft Items | 读写 | 只读 | 只读 approved items |
| Evaluation Review | 读 | 只能追加 Review | 读 approved verdict |
| Query 原始工具轨迹 | 自己当前工作集 | 不读 | 不读 |
| Evaluator 内部轨迹 | 不读 | 自己当前工作集 | 不读 |

这才是真正的 context isolation，而不是不同 Agent 使用不同 system prompt，但仍然接收同一份大 DTO。

### 3. Authority Isolation：读写权限由服务端执行

你说“Evaluator 没有 Draft 修改权”非常正确，但不能只写在 prompt 中。

工具层应该物理区分：

```text
Query capabilities:
- search_evidence
- read_evidence
- create_draft_item
- update_draft_item
- list_draft_items
- compact_workspace

Evaluator capabilities:
- list_draft_items
- read_draft_item
- read_evidence
- append_review
- request_more_evidence

Chat capabilities:
- list_approved_items
- read_approved_item
- render_answer
```

Evaluator 可以写新的 `Review`，但不能改原 `DraftItem`：

```json
{
  "review_id": "review-17",
  "draft_item_id": "item-4",
  "draft_version": 3,
  "decision": "NEED_MORE_EVIDENCE",
  "reason": "缺少第 6 集以后人物态度变化的证据",
  "requested_scope": {
    "episodes": [6, 7, 8],
    "evidence_type": ["dialogue", "visual"]
  }
}
```

然后由 Query 根据 Review 生成 DraftItem v4。

这比让 Evaluator直接修改草稿好，因为：

- Query和Evaluator职责不混；
- 审核结果可追踪；
- 不会出现 Evaluator暗中改结论后又自己放行；
- 可以比较每个版本用了哪些 Evidence。

### 4. Budget Isolation：每一次模型调用独立硬封顶

这是能否真正“不爆”的决定因素。

对于每一个 Model Call，必须满足：

\[
T_{\text{fixed}}
+T_{\text{user}}
+T_{\text{tools}}
+T_{\text{role-view}}
+T_{\text{new-observation}}
+T_{\text{output-reserve}}
+T_{\text{safety}}
\leq T_{\text{window}}
\]

不要只限制 Evidence；System Prompt、Tool Schema、历史消息和预留输出都要算进去。

而且需要分别配置：

```text
Query Agent Budget
Evaluator Budget
Chat Budget
Tool Result Budget
Evidence Expansion Budget
Draft View Budget
Output Reserve
```

## 你的 `get_full` 必须改成“有界读取”

这是当前设计最大的剩余漏洞。

如果工具是：

```text
get_evidence_full(evidence_id)
```

那么只要某个 Evidence 有 150K tokens，一次调用仍然会爆。

应该是：

```text
read_evidence(
    evidence_id,
    fields=["transcript", "events"],
    time_range={"start": 120, "end": 180},
    max_tokens=4000,
    cursor=null
)
```

返回：

```json
{
  "evidence_id": "chunk-123",
  "content": "...",
  "partial": true,
  "returned_tokens": 3912,
  "omitted_fields": ["baselineVisualCaption"],
  "next_cursor": "cursor-456"
}
```

也就是说：

> “全量内容可访问”不等于“允许一次性全量进入 prompt”。

正确语义应该是“完整内容无损保存、可分页读取”。

Draft也一样：

```text
list_draft_items()
→ 只返回 ID、一句话 claim、状态、引用数、估算 token 数

read_draft_item(item_id, max_tokens, cursor)
→ 返回某一项的详细内容

read_draft_evidence(item_id, evidence_ids, max_tokens)
→ 只展开这一项引用的少量证据
```

## Tool Result 拦截必须发生在进入 ToolMessage 之前

这一点很容易实现错。

错误流程：

```text
Tool返回 300K 字符
→ LangChain创建 ToolMessage
→ 加入 messages
→ 然后发现太大
```

此时下一次模型调用已经可能把它序列化进去。

正确流程：

```text
Tool执行
→ 原始结果进入 External Payload Store
→ 计算 token / 字段体积
→ Context Admission Controller
    ├── 能装入：返回 bounded result
    └── 不能装入：返回 handle + description + paging metadata
→ 最后才创建 ToolMessage
```

例如 ToolMessage永远只返回：

```json
{
  "result_id": "tool-result-789",
  "description": "命中第 3、5、7 集共 42 个片段",
  "item_count": 42,
  "estimated_full_tokens": 126430,
  "visible_items": [...最多若干 CandidateCard...],
  "partial": true,
  "next_cursor": "..."
}
```

字符阈值可以保留作快速 L0 防护，但硬判断必须尽量使用对应模型 tokenizer。中文、日文、JSON、ID 和 Tool Schema 的字符/token比例差异很大。

## Draft 不应该是自由文本“思维草稿”

我建议把草稿定义为结构化、可版本化的 Candidate Claim Store，而不是保存一大段 Query Agent 的自由推理过程。

```json
{
  "id": "draft-item-4",
  "version": 3,
  "claim": "人物 A 在第 5 集后开始怀疑人物 B",
  "status": "PROPOSED",
  "evidence_refs": [
    {
      "evidence_id": "chunk-91",
      "episode": 5,
      "time_range": [612, 648],
      "supports": "A 明确质疑 B 的说法"
    }
  ],
  "counter_evidence_refs": [],
  "open_questions": [
    "第 6–8 集是否出现关系修复"
  ],
  "created_by": "query",
  "estimated_detail_tokens": 1800
}
```

原因是：

- 自由草稿会再次无界增长；
- 推理过程和事实结论混在一起，Evaluator很难核验；
- 结构化 item 天然支持分页、单项审核和并行检索；
- 不必保存模型原始 chain-of-thought，只保存可审计的 claim、证据、反证和未决问题。

## 推荐的最终拓扑

```text
                         ┌─────────────────────┐
                         │ Immutable TaskSpec  │
                         │ 用户目标/范围/约束  │
                         └──────────┬──────────┘
                                    │
                   ┌────────────────▼────────────────┐
                   │ Context Runtime / Graph State   │
                   │                                 │
                   │ EvidenceStore   DraftStore      │
                   │ ReviewStore     WorkspaceStore  │
                   │ PayloadArchive  BudgetLedger    │
                   └───────┬───────────┬─────────────┘
                           │           │
              bounded view│           │bounded read-only view
                           │           │
                 ┌─────────▼───┐   ┌───▼───────────┐
                 │ Query Agent │   │ Evaluator     │
                 │             │   │               │
                 │ 搜索/展开    │   │ 只审 selected │
                 │ 写 Draft    │   │ Draft+Evidence│
                 │ 自主 compact │   │ 写 Review     │
                 └──────┬──────┘   └──────┬────────┘
                        │                 │
                        └────修订循环─────┘
                                          │ PASS
                                          ▼
                                  ┌──────────────┐
                                  │ Chat Agent   │
                                  │ Approved     │
                                  │ claims only  │
                                  └──────────────┘
```

这里每个 Agent 都不能直接访问完整 Graph State，只能通过专属 Context Projector 和 Tool capabilities。

## 必须建立的系统不变量

只要下面这些在代码层成立，就能把“不会 Provider overflow”从愿望变成可验证性质：

1. 每次 Provider 调用前，都有：

   ```text
   assembled_input_tokens + output_reserve <= model_limit - safety_margin
   ```

2. 任何 Tool Result 在成为 ToolMessage 之前就完成 budget admission。

3. 没有任何返回无上限字符串的 Tool；所有 read/list/search 都有：

   ```text
   max_tokens + limit + cursor
   ```

4. 完整 Evidence、完整 Draft、完整历史永远只存在于外部 Store，不直接进入 messages。

5. 每个对象都有稳定的：

   ```text
   id + version + token estimate + provenance + partial flag
   ```

6. Query、Evaluator、Chat 使用独立 allowlist projection，禁止整个 Graph State 自动序列化。

7. compact/archive 必须可恢复；summary只是导航信息，不是唯一事实副本。

8. 当上下文达到软阈值，例如 70%–80%，Agent可主动 compact；达到硬阈值时，Harness强制进入 context-maintenance mode。

9. Chat 最终阶段也有独立预算。否则你只是把 overflow 从 Evaluator 推迟到最终答案生成阶段。

10. 除 per-call context budget 外，还要有整个任务的：

    ```text
    max_tool_calls
    max_expanded_evidence
    max_total_input_tokens
    max_iterations
    ```

    上下文隔离能保证单次不爆，但不能防止 Agent不断分页读取导致总费用无限增长。

## 最终判断

你的设计方向是正确的，而且比“给 RAG 加一个摘要器”更接近真正的 Agent Harness：

- Evidence ID 类似文件描述符；
- Description 类似目录项/inode metadata；
- `read_evidence` 类似 page-in；
- compact/archive 类似内存换出；
- Prompt 是工作集，不是持久存储；
- Graph State 是外部地址空间；
- Evaluator 是只读进程；
- Context Assembler 类似 MMU + admission controller。

但要把结论从“通常不会爆”提升到“结构上不可能爆”，还需要三个决定性修改：

1. **把字符拦截升级为每次调用的 token-aware hard preflight；**
2. **把 `get_full` 改成无损但始终有界、分页的读取；**
3. **把角色权限和 Context Projection 写死在 Harness，而不是依赖 Prompt 自觉。**

完成这三点后，13 集、26 集甚至素材库继续增长，增长的只会是外部 EvidenceStore，不会再让单次模型上下文跟着无界增长。

---

#user

1. tool截断输出是tool粒度的,所有tool都该实现的规范,和agent无关,这一点请你去../codex好好学一下
2. 同样我们要做的是提供各种不同粒度的tool,并把tool的使用和组织弄成skill提供给三个agent,而不是你所说的那样
3. 我再说一遍,关于这个long horizon的context控制,你直接去../codex好好学一下

#chat

你说得对。看完 `../codex` 的实际实现后，我前面的回答需要做架构级纠正，不只是换个说法：**我把 Tool 自己必须履行的输出契约，错误地提升成了 Agent/中央 middleware 的主职责；又把 Skill 应该承担的 Tool 使用编排，混进了 Context Runtime。**

Codex 真正的分层是：

```text
Tool    = 自己保证本次返回有界、可继续读取
Skill   = 教 Agent 如何选择、组合、逐层展开 Tool
Agent   = 在自己的能力表内使用 Tool，不负责修补 Tool 的无界输出
Harness = 管整个活跃历史、token 水位、换窗与 compact
Store   = 保存完整、可寻址、可恢复的 canonical data
```

## 1. Tool 截断确实是 Tool 粒度的规范

Codex 在 [`ToolCall::response_byte_budget`](../../../codex/codex-rs/tools/src/tool_call.rs) 中把 Host 给出的输出预算与 Tool 自己的最大响应大小取较小值，但**如何在预算内返回有意义的内容，由每个 Tool 自己实现**。

几个直接证据：

- `skills.list` 自己限制每页最多 20 个 Skill，实际序列化响应，放不下就减少条目，并返回 `next_cursor`；见 [`list.rs`](../../../codex/codex-rs/ext/skills/src/tools/list.rs)。
- `skills.read` 不是把整个资源丢给 Agent 再让 middleware 截断，而是用二分法找到能装进响应预算的页尾，并返回 `next_cursor`；见 [`page_response`](../../../codex/codex-rs/ext/skills/src/tools/read.rs)。
- `exec_command` / `write_stdin` 自己有 `max_output_tokens`、`original_token_count`、`chunk_id` 和 `session_id`，模型看到的是 Tool 生成的有界输出；见 [`shell_spec.rs`](../../../codex/codex-rs/core/src/tools/handlers/shell_spec.rs) 和 [`ExecCommandToolOutput`](../../../codex/codex-rs/core/src/tools/context.rs)。
- Codex 在历史写入边界还会统一截断 `FunctionCallOutput`；见 [`history.rs`](../../../codex/codex-rs/core/src/context_manager/history.rs)。但这是防止某个 Tool 或外部 MCP 违反契约时的**最后兜底**，不是主要的检索/分页机制。

所以正确设计不是一个中央 `Context Admission Controller` 替所有 Tool 猜怎样截断，而是：

```text
每个 Tool 都声明并执行自己的 Output Contract
    ├── 服务端硬上限
    ├── 序列化后的大小校验
    ├── 适合该数据类型的裁剪/分页语义
    ├── next_cursor / chunk_id / session_id
    └── original_count / has_more / warnings

Harness 只再做一次不可绕过的 generic hard cap
```

可以共享 `bounded_response`、cursor、序列化计量等基础库，但不能把语义责任重新推回 Agent 或一个通用 middleware。

## 2. AniVAG 应该提供多粒度 Tool，而不是一个 `get_full`

“完整内容可访问”应该像 Codex 的 `skills.read`：**完整资源可以通过稳定 cursor 全部遍历，但任何一次调用都不返回无界内容。**

Evidence 至少拆成三层：

| 粒度 | Tool | 单次返回 |
|---|---|---|
| L0 发现 | `search_evidence_cards` | `evidence_id`、一句 description、episode、时间、命中通道、估算大小；分页 |
| L1 核验 | `get_evidence_detail` | 一个或少量 ID 的结构化摘要、可用字段、证据范围；硬上限 |
| L2 原文 | `read_evidence_field` | 指定 ID、字段、时间范围或 cursor 的一页 transcript/events/visual；返回 `next_cursor` |

必要时再提供 `search_within_evidence(evidence_id, query)`，避免 Agent 为找一句话而顺序读取整个长片段。

Draft 同样分层：

| 粒度 | Tool | 单次返回 |
|---|---|---|
| L0 发现 | `list_draft_items` | item ID、claim description、状态、版本、证据数 |
| L1 核验 | `get_draft_item` | 单个 item 的 claim、范围、evidence refs、review 状态 |
| L2 证据 | `read_draft_item_evidence` | 只展开该 item 引用的某条证据，并分页 |
| 写入 | `upsert_draft_item` / `remove_draft_item` / `submit_draft` | 只返回版本、变更摘要和稳定 ID |

这些 Tool 的输出上限与调用它的是 Query、Evaluator 还是 Chat 无关。同一个只读 Tool 被多个 Agent 复用时，应该保持完全相同的有界返回契约。

角色差异只体现在 Harness 给它注册哪些 Tool：

| Agent | Tool 能力 |
|---|---|
| Query | 搜索/读取 Evidence；list/get/upsert/remove/submit Draft；可请求 checkpoint/compact |
| Evaluator | list/get Draft；按引用读取 Evidence；写 Review；**没有任何 Draft mutation Tool** |
| Chat | list/get approved items；读取获批范围；`submit_video_item`；不做自由检索、不改 Draft |

这也修正了我前面“给每个 Agent 做特殊 Context Projector”的重心偏差：需要角色隔离，但最清楚的实现边界是**每个 Agent 的 Tool capability surface**；Tool 的分页和截断仍然是 Tool 自身规范。

## 3. Tool 的组织和使用策略应该放进 Skill

Codex 对 Skill 也不是把所有正文永久塞进 Prompt：

- 初始只暴露 Skill 的名称、description 和 locator；Skill metadata 有独立预算，默认约占 context window 的 2%，并有 10K-token 配置上限；见 [`render.rs`](../../../codex/codex-rs/ext/skills/src/render.rs)。
- 选中某个 Skill 后才读取它的 `SKILL.md`；主 Skill prompt 仍有 8KB 硬上限；见 [`extension.rs`](../../../codex/codex-rs/ext/skills/src/extension.rs)。
- 大量 Tool schema 也可以 deferred，先只给 namespace/description，再用 `tool_search` 找到并在下一次调用中暴露具体 Tool；见 [`ToolExposure`](../../../codex/codex-rs/tools/src/tool_executor.rs) 和 [`tool_search_spec.rs`](../../../codex/codex-rs/core/src/tools/handlers/tool_search_spec.rs)。

AniVAG 应该按三个 Agent 提供 Role Skill，而不是继续把 Tool 说明堆进各自 system prompt：

```text
shared/evidence-navigation
  search cards → 比较候选 → 展开少量 detail
  → 只读必要字段 → 保留 evidence_id/version/cursor

query/draft-authoring
  将证据写成 DraftItem → 检查引用 → 提交版本
  → 收到 Review 后只修改受影响 item

evaluator/draft-review
  先看 Draft cards → 展开待审 item
  → 只读取该 item 引用的 Evidence → 写 Review
  → 不尝试修改 Draft

chat/approved-answer-rendering
  只遍历 approved items → 按需展开
  → 组织 Markdown 与 submit_video_item
```

Skill 负责“先用哪个 Tool、什么时候升级粒度、什么时候停止读取”；Tool schema 和服务端注册表负责真实能力与权限。**Skill 不是权限边界。**

## 4. Codex 的 long-horizon 控制不只是 compact

Codex 仓库级不变量直接写着：Context 增量构建；任何注入项都必须有 hard cap；单项不得超过 10K tokens；见 [`AGENTS.md`](../../../codex/AGENTS.md)。其运行时还有四个连续层次：

1. **单项有界**：Tool Result、Skill、环境片段、Tool namespace 都分别有硬上限。
2. **渐进披露**：Skill、Tool、资源正文都先发现 metadata，再按需展开。
3. **状态增量**：`WorldStateSection` 保存 typed snapshot，正常轮次只渲染 diff，而不是每轮重发全量状态；见 [`world_state/mod.rs`](../../../codex/codex-rs/core/src/context/world_state/mod.rs) 和 [`record_context_updates_and_set_reference_context_item`](../../../codex/codex-rs/core/src/session/mod.rs)。
4. **历史换窗**：监控真实 active tokens，到阈值时 pre-turn 或 mid-turn compact；compact 会建立新的 replacement history/window，并重新注入当前 canonical world state，而不是把外部完整状态重新塞回 Prompt；见 [`context_window.rs`](../../../codex/codex-rs/core/src/session/context_window.rs)、[`turn.rs`](../../../codex/codex-rs/core/src/session/turn.rs) 和 [`replace_compacted_history`](../../../codex/codex-rs/core/src/session/mod.rs)。

因此 compact 是**累计历史增长后的换窗机制**，不是单次大 Tool Result 的修复器。顺序必须是：

```text
Tool 单次有界
→ Agent 按 Skill 渐进读取
→ 活跃状态只注入增量
→ 历史累计到阈值后由 Harness compact / new window
```

Agent 可以通过 Tool 主动请求 checkpoint/new context window，但真正的硬阈值、是否允许继续采样、replacement history 如何构造，必须由 Harness 掌握。不能把“不爆 Context”的正确性依赖于模型记得主动 compact。

## 5. AniVAG 当前代码与目标的具体差距

目前 [`search_video_chunk_evidence`](../../server/agent/tools.py) 仍然把 `project_search_result` 生成的整包 Evidence 返回，再调用 `context_budget.consume(...)`。这只是“超限后报错”，不是“该 Tool 始终产出有界结果”。

当前 [`ModelContextBudget`](../../server/agent/state.py) 也是累计字符后抛异常；Gateway 的 agent loop 则会把 Tool 输出原样 append 到 messages。也就是说，一旦 Tool 返回大对象，Harness 没有把它自动变成可继续读取的 page。

另外：

- Query 会重放 `previous_turns` 和上一轮 `available_evidence`；见 [`query.py`](../../server/agent/query.py)。
- Evaluator 会一次性接收全部 previous turns、draft 和 available evidence，而且没有只读 Tool；见 [`evaluator.py`](../../server/agent/evaluator.py)。
- Chat 又把 approved selected evidence 投影进 Prompt，目前只有 `submit_video_item`；见 [`chat.py`](../../server/agent/chat.py)。
- 目前只有 Query 直接拼接一份 Retrieval Skill；见 [`skills/__init__.py`](../../server/agent/skills/__init__.py)。这还不是给三个 Agent 配置 Skill catalog 和各自 Tool workflow。

所以 AniVAG 的正确改造顺序应该是：

1. 先定义所有 Tool 都必须通过的 `BoundedToolOutput` 测试规范：服务端 cap、序列化后大小、cursor 稳定性、单条过大行为、空预算行为。
2. 把现有检索 Tool 拆成 Evidence cards/detail/field page，完整 Evidence 留在 Store。
3. 把 Draft 从 Query prompt 中移到 DraftStore，提供 list/get/read/mutation Tool；Evaluator 只注册只读与 Review Tool。
4. 给 Query、Evaluator、Chat 分别挂载对应 Skill，让 Skill 规定 Tool 的升级粒度和停止条件。
5. 最后给 Agent Harness 加真实 token ledger、world-state diff、自动换窗和可请求 compact；generic ToolMessage truncation 只作为最后兜底。

这样到了 13 集、26 集，增长的是 Store 中的可搜索对象数和任务总读取成本，不是某一次模型调用的 Context 大小。即使用户要求跨 13 集汇总，Agent 也应分批检索、把中间结果写进 DraftStore、换窗后继续，而不是把 13 集召回结果同时留在 messages 里。

---

#user

是的,我们对标的就是这种,设置两层tool就够了所以,一个负责召回catalog,另一个负责精读
然后目前query agent需要相应的retrieve tool(原本就有的),还有read_trieve_item tool来负责全量获取某个item
然后使用update_draft_tem和remove来进行调整(update可以直接修改,也可以直接insert,注意通过chunkId进行去重,顺带疑问,chunkId是所有chunk都唯一的uuid,还是只是针对一个video切出来的顺序ID?)
最终不需要submit_draft,只需要get_draft_catelog 和read_draft_item 两种粒度的工具即可

evaluator拥有同样的retrieve tool(原本就有的),还有read_trieve_item tool来负责全量获取某个
  item ,get_draft_catelog 和read_draft_item , 以及传递advice到query(旧链路逻辑)
以及控制gateway(我忘了怎么控制的了,也是用tool是吗?)

然后chat是remove_video_item和update_video_item,以及 get_draft_catelog 和read_draft_item 两种

应该这么设计

#chat

## 2026-09-19 生效设计：严格两层 Tool，名称按用户原文固化

本节是当前权威设计，覆盖本文前面出现过的三层 Evidence/Draft Tool、`submit_draft`、`submit_video_item`、专用 `get_evidence_detail` 和其他自行扩展的 Tool 提案。Tool 名称按用户原文保留，包括 `read_trieve_item`、`update_draft_tem` 和 `get_draft_catelog` 的拼写；未经用户确认不得重命名。

### 两层读取模型

```text
Catalog Tool
  → 只返回有界 description + 稳定 handle/chunkId

Read Tool
  → 按 handle/chunkId 精读一个 item
  → 小对象可一次返回完整内容
  → 大对象在同一个 Read Tool 内分页并返回 next_cursor
```

分页属于 Read Tool 的输出协议，不构成第三层 Tool。

### Tool 精确清单

| Agent | Tool | 权限与语义 |
|---|---|---|
| Query Agent | `retrieve` | 复用原有检索能力，但只返回 Retrieval Catalog，不返回完整 Evidence |
| Query Agent | `read_trieve_item` | 按 `chunkId` 精读某一个 Retrieval Item；单次输出有界，必要时通过 cursor 续读 |
| Query Agent | `update_draft_tem` | Draft upsert；同一 `chunkId` 不存在则 insert，存在则直接 update |
| Query Agent | `remove_draft_item` | 按 `chunkId` 从 Draft 删除 item |
| Query Agent | `get_draft_catelog` | 返回有界 Draft Catalog，只含 description、`chunkId` 和必要状态元数据 |
| Query Agent | `read_draft_item` | 按 Draft Catalog handle/`chunkId` 精读一个 Draft Item；单次输出有界，必要时续读 |
| Evaluator | `retrieve` | 与 Query 使用同一个有界 Retrieval Catalog Tool；Tool 输出规范不因 Agent 改变 |
| Evaluator | `read_trieve_item` | 与 Query 使用同一个 Retrieval Item 精读 Tool |
| Evaluator | `get_draft_catelog` | 只读 Draft Catalog |
| Evaluator | `read_draft_item` | 只读单个 Draft Item |
| Chat Agent | `remove_video_item` | 从最终视频 item 工作区移除指定 `chunkId` |
| Chat Agent | `update_video_item` | 按 `chunkId` 新增或修改最终视频 item，并按 `chunkId` 去重 |
| Chat Agent | `get_draft_catelog` | 只读 Draft Catalog |
| Chat Agent | `read_draft_item` | 只读单个 Draft Item |

明确不存在：

- 不设置第三层 Tool；
- 不设置 `submit_draft`；
- 不保留 `submit_video_item`；
- 不新增 Evaluator 的 Draft mutation Tool；
- 不新增“控制 Gateway”的 Tool。

### `chunkId` 已确认语义

当前代码中的 `videoChunkId` 是 canonical UUID，也是 `video_chunk.video_chunk_id` 的全局主键，不是只在单个 Video 内有效的顺序号。它由下式确定性生成：

```text
videoChunkId = UUIDv5("anivag:video-segment", videoId, sequenceNo)
```

同一个 Video 内另有独立的 `sequenceNo`，数据库同时对 `(video_id, sequence_no)` 建唯一约束。因此 Draft 和最终 Video Item 都按 `videoChunkId` 去重是成立的；重复 upsert 不应生成第二个 item。

代码依据：`server/identity.py::derive_video_chunk_id` 与 `server/model/video.py::VideoChunkRecord`。

### Evaluator advice 与审批闸门

当前旧链路不使用 Tool 控制审批闸门或 Gateway：

```text
Evaluator
  → 返回结构化 EvalDecision(action, advice)
  → AgentRuntime 将 review 写入 LoopTurn
  → action=continue：LangGraph 路由回 Query
  → Query 从 previous_turns[].review.advice 读取修改建议
  → action=complete：LangGraph 路由到 Chat
```

若“gateway”指 Query/Evaluator 之间的放行闸门，它由 `EvalDecision.action + LangGraph conditional edge` 控制，不是 Tool。若指模型 Gateway，Evaluator 也不直接控制 Gateway；模型调用仍由 Runtime 注入的 model adapter 执行。该旧链路继续保留。

### Skill 组织

三个 Agent 的 Skill 只负责教模型如何使用各自已注册的上述 Tool，不新增 Tool：

```text
Query Skill
  retrieve → 从 Catalog 选少量 chunkId
  → read_trieve_item 精读
  → update_draft_tem / remove
  → get_draft_catelog 检查当前工作集

Evaluator Skill
  get_draft_catelog → read_draft_item
  → 必要时 retrieve / read_trieve_item 独立核验
  → 输出 EvalDecision(action, advice)

Chat Skill
  get_draft_catelog → read_draft_item
  → update_video_item / remove_video_item
  → 形成最终回答与视频 item 顺序
```

Tool 自己保证单次输出有界；Skill 决定何时从 Catalog 升级到精读；Harness 负责累计历史的 token 水位、compact 和换窗。

## 2026-09-19 实现状态：Agent 已接入两层 Tool Workspace

#user

ok,调整agent接入tool的体系

#user

skill禁止写相符合某个具体动画的特例

#chat

已完成 Agent 接入层调整：每次 Kick 创建独立 `AgentWorkspace`，完整 Retrieval Item、Draft Item、获批 Draft 快照和 Chat Render Draft 都保存在模型消息之外。Query、Evaluator、Chat 分别只注册已确认的 6 / 4 / 4 个 Tool；不存在 `submit_draft`、`submit_video_item` 或 Gateway Tool。Query 与 Evaluator 各自使用隔离的 structured Tool loop，Evaluator 仍以 `EvalDecision(action, advice)` 控制 LangGraph 的 `continue → Query` 与 `complete → Chat` 路由；Chat 使用获批 Draft 的只读 Tool，并通过 `update_video_item` / `remove_video_item` 维护最终视频块。

`retrieve` 与 `get_draft_catelog` 只返回有界 Catalog；`read_trieve_item` 与 `read_draft_item` 在同一个 Read Tool 内用 `next_cursor` 分页。所有 Tool 返回在序列化后受 `max_tool_result_bytes` 硬限制，Catalog 同时受 `max_catalog_items_per_page` 限制；分页仍属于两层模型中的传输协议，不构成第三层。三个 Agent 的累计模型消息还受 `max_prompt_chars` 约束，Chat 的自定义 function-calling loop 也会在每次 Provider 请求前重新计算完整 messages + Tool schema 大小。

Evaluator 的 `complete` 不是无条件可信：Server 记录其是否完整读完 Draft Catalog 和每一个 Draft Item；未完成只读核验时把 `complete` 改为 `continue`，不会进入 Chat。Draft 的 `update_draft_tem` 和最终视频的 `update_video_item` 都按全局 `videoChunkId` 原子 upsert 去重。

三个 `SKILL.md` 只描述作品无关的方法、Tool 协议与角色权限，明确禁止写入或依赖任何具体动画、人物、集数、剧情、台词或镜头特例。检索示例也只使用抽象占位描述。

当前代码配置新增：`ANIVAG_AGENT_MAX_TOOL_RESULT_BYTES`（默认 16384）和 `ANIVAG_AGENT_MAX_CATALOG_ITEMS_PER_PAGE`（默认 20）；两层 Tool 需要多次读取，因此 `ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN` 默认调整为 24。

---

#user

可以,哪些要放utils,common,constants也要分清楚,然后模仿codex来完成吧！

#chat

完成了。Long-horizon 第三阶段已经落地，Tool 清单和名称没有变化。

代码没有塞进泛化的 `utils.py` / `common.py`：

- `gateway/context_window.py` 独占通用 Harness 机制：窗口预算、Provider token ledger、换窗状态、replacement history、硬上限恢复。只属于该协议的 marker、估算系数和摘要上限作为模块私有常量保留在这里；`gateway/constants.py` 没有被污染。
- `gateway/agent_model.py` 是接入层：自定义 function-calling loop 和 LangChain/LangGraph loop 在每次模型调用前都经过同一个 Context Window Controller，并回填 Provider 的真实 `input_tokens`。
- `server/agent/context.py` 只负责 AniVAG 领域投影：从外部 canonical Workspace 生成有界 navigation checkpoint，不把完整 Evidence 或 claim 重新塞回 Prompt。
- `server/agent/workspace.py` 只负责每次 Kick 的 canonical 状态与线程安全快照。
- `server/agent/query.py`、`evaluator.py`、`chat.py` 只负责各角色的稳定任务锚点、可压缩初始快照和 checkpoint 回调。
- `server/config/agent.py` / `settings.py` 保存可配置预算；`server/bootstrap.py` 负责组装，未新增全局杂项常量。

运行语义按 Codex 的控制面实现：每次 Provider 请求前计算完整 messages + Tool schema；优先采用上一请求真实 input token 修正本地保守估算；达到软阈值后建立新的 window ID，以 replacement history 真正替换旧消息，并重新注入有界 canonical workspace checkpoint；最近一批尚未被模型消费的 Tool Result 保持原样。只有碰到硬上限且没有其他安全空间时，Harness 才把最大的 Tool Result 释放成可恢复 receipt，并明确禁止因为 receipt 被释放而重复 mutation；精确内容仍通过既有 Catalog/Read Tool 恢复。

三个 Agent 的初始输入也已拆层：System/Skill 和当前任务锚点固定；history、reference context 等初始快照在被模型消费一次后即可回收。因此旧对话不会因为被拼在首条 user message 中而永久占据每一个后续请求。

默认窗口为 64K tokens，预留 4096 output + 4096 safety；48K 自动 compact，目标 24K，硬输入边界为 55,808 tokens。另保留 200,000 字符的应用级兜底。若连固定指令、Tool schema 或单条当前用户输入本身都超过硬边界，系统会在调用 Provider 前明确失败；不会伪装成“已经成功 compact”。

这不是逐行复制 Codex：Codex 可使用 Responses API 的 remote opaque compaction item，AniVAG 当前仍是 Chat Completions/Ark/DeepSeek，所以采用可审计的本地 replacement checkpoint。相同的是 token 水位、自动换窗、历史替换、window identity、canonical state 重注入和硬边界恢复；AniVAG 的摘要载荷使用确定性的活动索引与 typed Workspace checkpoint，避免让摘要成为事实源。

验证结果：Context/Gateway/Agent/Tool/配置聚焦回归全部通过；全量回归为 831 passed、8 skipped、9 failed。9 个失败都来自工作树中另一组 Parse Job timing 改动：`ParseJobState` 已新增 `processing_duration_ms`、`processing_timing_complete`，但 `tests/server/test_sse_events.py` 的旧 fixture 未提供它们；与本次 Context Harness 调用链无关，我没有越权修改那组代码。

设计记忆已同步到 `docs/design/agent_harness_context修复.md`、`docs/design/agent-harness调教log.md` 和 `docs/design/technical_solution copy.mdv`。MDV 只保存了 Document 工作副本（generation 74），没有修改 Reference、没有创建 MDV Version，完整校验为 valid/complete。
