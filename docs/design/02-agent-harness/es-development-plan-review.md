# Engine-first 检索重构：阶段计划与当前实现评审

核对日期：2026-10-02。对象：当前 Seekino 工作区（含未提交、未跟踪代码），不是单独的 Git HEAD。本文是**阶段计划与现状报告**，不自动授权迁移、清理、服务重启或发布。本次只更新地图与核对源码；历史业务测试按原日期和范围保留，见 §6。

**主交付：[整体阶段图 SVG](diagram/es-development-plan-review.svg)**。逐阶段阅读：[P0–P3](diagram/es-development-plan-review-p0-p3.svg)、[P4–P7](diagram/es-development-plan-review-p4-p7.svg)。[可编辑 Excalidraw 源图](diagram/es-development-plan-review.excalidraw)。三张 SVG 均从同一源场景导出。

配套具体样本：[一个 Chunk 的索引与检索设计](es-chunk-index-review.md)、[样本图](diagram/es-chunk-index-review.excalidraw)。样本仍是设计提案，不是运行证据。

## 0. 本轮收回并固定的职责边界

本轮按最新工作区同步 plan-map 的实现与验证事实。P0–P7 编号和依赖关系继续沿用；唯一的 **▶ 当前阶段** 仍是 **P2**。P2 的中立索引基线与 P3 的薄适配代码已经落地，但尚未签收的策略和真实组件验收不能由代码存在或替身测试代替。

**本 Agent 是 Engine 检索主线的实现者**：可写范围以 `engine/retrieval`、必要的 Engine 公共合同及直接测试为中心，负责 Source 后索引处理、召回、融合、精筛合同与证据规则。职责依据为本会话的 Engine-first 决定、本文 §0，以及最新 [V2 handoff §0.1](../../video-parse-v2-handoff.md#01-当前职责方向下一步和未决项)。`engine/parsev2` 的正式 Schema、解析规划和领域校验由解析主线维护，本 Agent 消费其接口；Server 租约、状态机、事务、媒体及 Gateway/ES 执行属于外部依赖，不因读到调用链就扩大本 Agent 可写职责。

整个方向以一条硬边界为中心：

> **Engine 是 Source Schema、Source 后索引处理 pipeline、召回处理 pipeline 的完整能力提供层。Agent、Server、Gateway 和 Elasticsearch 只消费合同或实现端口。V2 Engine 核心不得依赖这些外层模块及其 SDK。**

这里的“完整能力”包括确定性的领域 Schema、索引表示、编码请求、指纹与复用判定、查询计划、候选归一/归并、融合、可选精筛流程和证据判据。数据库事务、媒体读取、模型调用、ES mapping/DSL/bulk、任务运行状态和网络 I/O 仍由外层适配器执行。

| 节点 | Engine-first 阶段身份 | 当前判断 |
| --- | --- | --- |
| P0 | 固定 Engine Core 与 ports/adapters 的依赖方向 | 核心边界已确认；细化后的验收规则供 review |
| P1 | Engine 向 Agent 暴露正式 Source Schema、规划和校验 | 核心实现及 Agent 消费已有，由解析主线维护；整阶段未签收 |
| **P2 ▶ 当前阶段** | Engine 完成 `Source → 中立 Index Plan` 的纯索引 pipeline | 索引基线及历史纯测已有；来源记录沿用现实现，结构化采样与可选 Sparse 合同待补，具体参数和评分未定稿 |
| P3 | Server/Gateway/ES adapters 忠实执行 Engine Index Plan | video clip 的薄适配及 fake/SQLite 证据已有；有序帧/Sparse 执行为外部 TODO，真实协议未验 |
| P4 | Engine 完成 `Query → 候选 → 融合 → 证据判定` 的召回 pipeline | V2 Search/Chat、ES 三路与同版回查已本地接线；完整 Engine Query Plan/归一/证据 pipeline 尚缺 |
| P5 | Engine 完成可选精筛 pipeline，Gateway 实现 scorer port | TRD 保留位置；专用合同和实现缺失 |
| P6 | 验收 Engine 独立能力、adapter conformance 和完整真实链路 | 10-01 历史 262 项 Engine 与 94 项 adapter 本地证据保留；10-02 本次未跑业务测试，真实整链未通过 |
| P7 | 受控重建、数据切换与整版发布 | 未执行、未证明 |

主要依据：[Engine TRD](video-parse-engine-trd.md)、[Harness TRD](video-parse-harness-trd.md)、[ES 主计划](refactor_3_es_qdrant.md)、[V2 handoff](../../video-parse-v2-handoff.md)。图内缩写：E＝Engine TRD；H＝Harness TRD；M＝主计划；R＝本文；S＝Chunk 样本；D＝V2 handoff。

Engine TRD 中的“Schema 还是旧 person_id”“ES 文件尚未创建”等历史状态句与当前源码不符；旧 60 文件清单基于 V1，也不再作为建设清单。本报告只复用仍有效的目标和合同，并以当前 V2 源码判断实现状态。

## 1. 主要职责、代码方向与当前位置

**Engine 的三条公共能力面与本 Agent 分工**：

1. `engine/parsev2` 向 Agent/Harness 暴露正式 Source Schema、JSON Schema、规划与校验；已有能力由解析主线维护，本 Agent 消费必要接口并反馈兼容问题。
2. `engine/retrieval` 接收同版正式 Source，确定性地产出存储中立的索引表示、编码请求、revision、复用决定和候选身份；这是本 Agent 的直接开发职责。
3. `engine/retrieval` 接收 Query、授权范围、各路原始命中和同版正式证据，确定性地产出召回计划、Chunk 候选、融合/精筛结果及证据判定；这是本 Agent 的直接开发职责。

**依赖方向**：

```text
Agent / Server / Gateway adapters  ─────→  Engine 公共合同
Engine Core  -X→  Agent / Server / Gateway / ES / MySQL / Provider SDK
```

Agent 组织模型任务并产出 Engine Schema；Gateway 执行 embedding/scorer 请求；Server 提供 MySQL 正式事实、媒体、任务状态、事务和传输；ES 执行物理索引与查询。外层运行这些流程，不因此取得投影、复用、融合或证据语义的所有权。

**当前事实**：P1 的 Schema、规划和校验主要在 Engine，Agent 也直接 import 这些入口。P2 的基线代码已落在 `engine/retrieval/index_pipeline.py` 与 `embedding_reuse.py`：逻辑索引计划/文档、video clip 与 ordered frames manifest、encoder identity、复用判定、receipt 和候选身份均由 Engine 提供；ordered frames 仍仅用不透明 `sampling_contract_id` 描述采样，结构化规则及其派生指纹尚未实现，Sparse 合同尚缺。P3 已有消费合同的 video clip coordinator/adapters 及历史替身验证，并显式拒绝有序帧编码。P4 的 V2 Search/Chat 已通过 ES 三路、Engine versioned RRF 与 MySQL 同版 hydrate 本地接线；完整 Engine Query Plan、原始命中归一和证据 pipeline 尚未形成，源码仍保留 query-policy TODO。

因此当前阶段仍是 **P2**。当前批次准确定位为 **P2 收尾并交接 P3**：在已有索引 pipeline 内补结构化采样、可选 Sparse 和相应校验/复用合同。P3 与 P4 可以基于稳定合同并行；P3 可查询样本限制 P4 的真实验收，不限制先做查询设计。已有替身验证或产品本地接线均不代替策略签收及真实协议验收。

本会话已澄清并沿用现有来源记录投影：`TimelineEvent → LexicalField[]` 是确定性提取与重组，ES `nested` 保留同一记录内字段关联。分词器、类别权重、评分聚合和完整人物/对白判据仍未因此定稿。P2 剩余边界为采样规则、预处理及 Sparse 编码合同；**均匀中点采样、Sparse 关闭时不预建仅为建议，尚未批准**。实际视觉/Sparse 模型协议与 adapter 执行交 P3；P4 的同源定位与扩展切片、P5 的模型/预算/阈值/失败策略、P6 的质量门槛继续在各阶段收敛。**Engine 是能力拥有者、外围只做适配**已经确认，不再列为待定项。

## 2. 三条公共 pipeline

### 2.1 Engine-first 主链

```text
Agent / Harness
  └─ 直接消费 Engine Source Schema、规划和校验
                         ↓ 正式 Source

Engine Index Processing Pipeline
  Source → ChunkSource → 类别词项 / 文字 Dense 输入
         → 视频片段或有序帧输入 manifest
         → revisions / encoder identity / reuse decision
         → 存储中立 IndexDocument + CandidateIdentity
                         ↓ ports

Gateway / Server / Elasticsearch adapters
  执行模型调用、媒体读取、MySQL 读取、ES mapping/bulk/query
                         ↓ 原始命中 + 同版正式事实

Engine Retrieval Processing Pipeline
  Query → 通道计划 → 原始命中归一 / 同路 Chunk 归并
        → 跨路 RRF → [可选] 精筛 → 硬条件与证据判定
        → 最终候选及可解释拒绝原因
```

Engine 可以定义纯数据合同、Protocol/port 和确定性算法。Engine 不读取文件、数据库或网络，不导入 Elasticsearch/SQLAlchemy/模型 SDK，也不依赖 Agent、Server 或 Gateway 的具体类型。物理 adapter 可以按运行环境翻译 Engine 计划，但不能改变字段语义、revision、候选身份、融合或证据判据。

这个分层参考本地 `../ragflow` 的“正式输出 → 中间 Chunk/IndexDoc → 存储后端”的持久化边界；RAGFlow 的目录结构、字段权重和具体公式不自动成为 Seekino 规范，也不会让 ES 取得业务事实所有权。

### 2.2 视频 Dense 的输入合同

```text
Chunk 的原视频时间范围
  ├─ 视频片段 ──────────────────────┐
  └─ 有序采样帧 + 绝对时间 ─────────┤
                                    ↓ 相容的视觉/视频编码 port
                            Chunk 级视觉检索表示
```

输入可为视频片段或有序帧，默认候选粒度仍为 Chunk。Engine 应保留采样、帧序、绝对时间、预处理和联合编码所需的 manifest；Gateway 只执行已选 encoder。当前解析 Understanding 支持抽帧，索引服务仍调用 `embed_video`，所以“解析抽帧已接线”不能写成“抽帧 Dense 已接线”。

不在 plan-map 中擅定平均池化、逐帧 ES 文档或具体模型。同源截图/GIF 定位还需要帧序和时间证据；视觉 Dense 高分不能单独推出帧级时间。

### 2.3 可选精筛仍属于 Engine 召回 pipeline

```text
有限候选 + 同版真实文本/片段/有序帧
  → Engine 生成 RerankPlan
  → Gateway scorer port 执行
  → Engine 消费评分、排序/筛选、处理失败
  → Engine 再做权限/版本/原话/人物/时序硬条件与证据判定
```

关闭时走确定性旁路；开启时只处理有预算的有限候选。Gateway 负责调用、用量、超时和取消，Server 提供同版事实与媒体，Engine 决定候选预算、评分如何消费、失败语义和硬条件。生成问题仍只是召回线索，不得成为对白事实证据。

## 3. P0–P7 阶段计划

状态含义必须分开记录：**设计通过、代码完成、接线完成、本地验证、真实环境验证、数据切换、V2 整版发布**不能相互替代。历史验证与本次地图检查见 §6；本地纯测试和替身测试不升级为真实组件验收。各节点中“本 Agent”均指本文 §0 的 Engine 检索主线分工。

### P0｜Engine Core 边界

- **功能**：固定 Engine 是 Source Schema、索引 pipeline 和召回 pipeline 的完整能力拥有者；外围只消费合同或实现 ports。
- **预计改动/影响范围**：Engine 公共 API、依赖方向、TRD/plan-map；本阶段不新增业务表，不改变运行数据。
- **用户验收**：确认依赖只由 Agent/Server/Gateway 指向 Engine；V2 Engine 可脱离数据库、ES 和模型运行；外层不定义投影、复用、融合或证据规则。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1，以及 E「零」、§1、§2.3；H「索引持久化边界」；M §2.1–2.3。本 Agent 维护检索公共合同与依赖边界；`engine/parsev2`、`engine/retrieval` 不得反向 import 外层或基础设施 SDK。V1 `engine/parsev1` 的 Gateway 耦合是历史债务，不冒充当前已清理，也不得扩散到 V2。
- **当前判定**：核心边界已确认；该结论不自动把 P1–P7 标成通过。
- **外部 TODO / 剩余边界**：解析主线和各 adapter 负责人遵守同一依赖方向；宿主装配与历史 V1 清理由所属模块负责。本 Agent 检查接口相容性，不接管外围实现或 V1 重构。

### P1｜Engine Source Schema 与领域 pipeline

- **功能**：Engine 向 Agent 暴露正式 `VideoParseResult`/Timeline Schema、JSON Schema、Chunk/Interval 规划、领域校验和 Refine 合并规则。
- **预计改动/影响范围**：正式 `engine/parsev2` Schema、规划/校验及 Agent 消费接口由解析主线维护。本 Agent 只消费已有正式 Schema、核对索引所需接口，不扩展解析规则；必要共享接口变更先按职责确认。
- **用户验收**：合法/非法样本可在 Engine 独立运行；序列化往返一致；Agent 的 prompt、state 和结果使用同一 Engine Schema；人名、对白、半开时间和成员边界正确。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1/§0.3，以及 E §1.1–1.8、H §0.3。本 Agent 以 [`schemas.py`](../../../engine/parsev2/schemas.py)、[`planning.py`](../../../engine/parsev2/planning.py)、[`validation.py`](../../../engine/parsev2/validation.py) 为正式输入依据，保持消费相容性；解析规则维护、模型编排与持久化分别交解析主线、Agent/Gateway 和 Server。
- **当前判定**：`schemas.py`、`planning.py`、`validation.py` 及 Agent 直接消费已有；整条真实解析质量和正式逐项签收仍未完成。
- **外部 TODO / 剩余边界**：解析主线提供同版正式成果和既有时间/成员规则，并验收实际解析质量；本 Agent 不重复建设 Source Schema，不用 ES 进度替代解析主线验收。

### P2｜Engine Index Processing Pipeline ▶ 当前阶段

- **功能**：Engine 接受正式 Source，完整输出 `ChunkSource`、类别词项、文字 Dense 输入、视频片段/有序帧 manifest、逻辑 `IndexDocument`、source/projection/encoder identity、复用决定和候选身份。
- **预计改动/影响范围**：本 Agent 以现有 `engine/retrieval/index_pipeline.py`、`embedding_reuse.py`、公共 contracts 及直接测试为修改中心，补结构化采样、实际 manifest 校验与可选 Sparse 请求/结果/身份，沿用既有投影。采样指纹由规范化规则派生是待实现计划；物理 ES mapping/DSL、媒体读取与模型调用交外部 adapter。
- **用户验收**：仅输入正式 Schema、Chunk 范围、索引配置及编码结果即可得到确定性 plan/document/receipt，无需启动 ES/MySQL/Gateway；验证帧序/绝对时间/内容身份与计划一致，编码变化和投影变化正确影响重写/复用，Sparse 开关及不符结果可解释。来源记录沿用现实现；采样建议、Sparse 编码边界、分词/权重/评分仍按具体行为签收。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1，以及 E §2.3.1/.2/.5/.10/.11/.12 与 S；实现基线见 [`index_pipeline.py`](../../../engine/retrieval/index_pipeline.py)、[`embedding_reuse.py`](../../../engine/retrieval/embedding_reuse.py)、[`index_projection.py`](../../../engine/retrieval/index_projection.py)。本 Agent 实现中立规则与直接测试；未签收策略不得由 adapter 或样本默认值代定，不新增独立业务表。
- **当前判定**：逻辑 Index Plan/Document、video clip 与 ordered frames manifest、encoder identity、复用/receipt 和 `VideoChunkRetrievalIdentity` 已实现并有历史纯测试。nested 与来源投影已澄清并沿用；结构化采样/派生指纹、可选 Sparse 尚未实现，P2 仍是唯一当前阶段且尚未通过。
- **外部 TODO / 剩余边界**：解析主线提供正式 Source 与已持久化配置；P3 所属模块提供按计划物化媒体及编码 port。均匀中点采样、Sparse 关闭时不预建仅为未批准建议；分词、类别权重、评分聚合及实际编码相容性不可由当前实现代签收。

P2 的完整可审阅链为：

```text
Timeline → ChunkSource → Engine IndexDocument / EmbeddingRequests
→ adapter mapping / ES JSON → Engine CandidateIdentity → 同版证据引用
```

`LexicalField` 是从 Timeline 或 Chunk 扩展成果提取的检索条目；`nested` 是 ES 保留条目内关联的物理映射，不是新领域事件。P2 沿用现有来源记录，不据此宣称分词、权重或评分策略已定稿。剩余收尾为结构化采样规则及其派生指纹、实际帧清单校验、可选 Sparse 编码合同与复用判据；均匀中点算法和关闭时不预建仍是建议。Sparse 的编码/写入执行属于 P3，Query 编码/召回/融合属于 P4，当前均未实现。Engine 能表达 manifest 不等于外围已能采样、物化或调用相容模型。

### P3｜Adapters：编码、ES 写入与恢复

- **功能**：Server/Gateway/ES adapters 忠实执行 Engine Index Plan：读取当前正式 Source/媒体、调用编码 port、执行 bulk、确认查询就绪、回写运行状态并按必要步骤恢复。
- **预计改动/影响范围**：Server、Gateway 和 ES adapter 的执行接入由所属模块／解析集成主线负责，具体新增 adapter 负责人待确认。本 Agent 只提供或补正 Engine 稳定 port/contract 及验收样本；MQ、租约、事务和任务状态留宿主可靠性。
- **用户验收**：adapter 输入输出逐字段符合 Engine 合同；向量输入与 manifest 一致；bulk 部分失败、refresh 前后、未知成功后的 exact readback、旧写/删除交错、同版投影重建、复用/失效在 fake 与隔离真实 ES/MySQL 中分别验证。旧文档升级必须提高 `index_generation` 或在空索引重建；同 generation/content 的 strict external version 会返回 409，不能当作成功覆盖。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1/§0.5，以及 E §2.3.10/.11、H §0.3 与索引放行规则 4–6；外部实现基线见 [`video_parse_index_coordinator.py`](../../../server/service/video_parse_index_coordinator.py) 与 [`elasticsearch.py`](../../../server/storage/elasticsearch.py)。本 Agent 交付中立计划和 conformance 预期，不接管 Server 租约/状态机；外围按合同物化媒体、调用 port、执行 bulk/readback/readiness、持久化 receipt 和推进状态。
- **当前判定**：thin coordinator/adapters 已在本地实现并经 fake/SQLite 覆盖 bulk、精确 readback、查询 readiness、receipt、重试与 generation；当前视觉 adapter 仅实现 video clip 并显式拒绝 ordered frames。真实 ES/MySQL/模型协议均未验证，写成功、查询就绪、业务搜索资格继续是三个条件。
- **外部 TODO / 剩余边界**：Server 媒体 adapter 按 Engine 采样计划产出有序帧及实际 manifest；Gateway adapter 执行获选多帧/Sparse encoder 并证明协议及文档/Query 相容；ES adapter 翻译获批字段、写入和验证查询就绪。责任归所属模块／解析集成主线，具体新增 adapter 负责人待确认；完成逐字段 conformance 和隔离真实验证后才能签收 P3。

P3 只限制 P4 的真实闭环验收，不限制 P4 先完成纯召回设计和实现。P3 不负责 API/Chat 检索，也不执行数据切换。

### P4｜Engine Retrieval Processing Pipeline

- **功能**：Engine 完整提供 Query 信号处理、通道选择/预算、存储中立查询请求、原始命中合同、身份校验、同路 Chunk 归并、跨路 RRF、证据需求和最终结果判定。
- **预计改动/影响范围**：以 `engine/retrieval` 与 Engine 测试为主；Server 只固定当前 COMPLETE 任务、翻译 ES DSL、执行查询和回查 MySQL；Gateway 只生成相容 Query 向量。
- **用户验收**：输入 Query、各路原始命中和正式证据，无需 ES/MySQL 即可复现相同候选、融合分、拒绝原因和最终证据；真实入口另证明 adapter 执行同一计划。错任务/版本、questions 冒充对白、speaker/时序错配均拒绝。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1/§0.3/§0.5，以及 E「零」、§2.3.3/.6/.12 与 M §4.3/§9.1。本 Agent 实现 `engine/retrieval` 内 Query Plan、命中归一、候选融合及证据规则；ES DSL、SQL 和媒体读取交外部。宿主的 [`query-policy TODO`](../../../server/service/video_retrieval.py#L375) 等待并消费 Engine 抽象，不在 Server 复制策略。
- **当前判定**：versioned RRF/部分 DTO 已有；V2 Search/Chat 已通过 ES 三路和 MySQL 同版 hydrate 本地接线，且不回退 Qdrant。完整 Engine Query Plan、raw-hit 归一及人物/原话/时序等证据 pipeline 尚缺；本地接线不等于真实入口验收或完整策略通过。
- **外部 TODO / 剩余边界**：Server 查询 adapter／解析集成主线消费 Engine Query Plan，固定 latest COMPLETE 任务与相符版本并提供同版证据；Gateway 提供相容 Query 编码，产品入口负责人证明执行同一计划。具体 adapter 负责人待确认；图片/GIF 同源定位、切片扩展和完整证据判定按 P4 review，真实验收依赖 P3 可查询样本。

默认基础组合仍是 BM25 + 文字综合语义 Dense + 视频/视觉 Dense。图片/GIF 按输入模态选择通道，不要求先转文字；Sparse 默认关闭但需保留可实现接口。截图/GIF 的同源定位与语义相似分别验收，证据不足时只返回支持的事件/Chunk 范围。

### P5｜Engine 可选精筛 pipeline

- **功能**：Engine 提供 Rerank Plan、scorer 输入/输出 port、候选预算、排序、旁路、失败策略和硬条件保护；Gateway 实现实际 encoder/LLM/多模态 scorer。
- **预计改动/影响范围**：Engine 为主，Gateway 只新增所选 scorer adapter，Server 只 materialize 同版文本/片段/有序帧。
- **用户验收**：用 fake scorer 独立验证关闭、正常、预算边界、超时、缺失/非法分数、并列分和硬条件；启用还需实际所选 scorer 的协议证据，质量收益由 P6 评测。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1/§0.5，以及 E §2.1.10、§2.3.3/.6/.8。本 Agent 实现 Engine scorer 合同、精筛计划/结果消费、旁路与硬条件保护；Gateway 实现获选模型 port，Server 提供同版素材。高分不能覆盖权限、版本、原话、人物或时序。
- **当前判定**：TRD 有架构位置，Engine 专用合同/pipeline 仍缺；模型、预算、阈值及失败策略待 review。
- **外部 TODO / 剩余边界**：Gateway scorer adapter 和 Server 同版素材物化由所属模块负责，新增 adapter 负责人待确认；启用须提供实际所选 scorer 协议证据，模型/预算/阈值/失败策略未决，收益由 P6 验收。

### P6｜Engine 独立能力、adapter conformance 与全链验收

- **功能**：分别验收 Engine 三条公共能力、外层 adapter 是否忠实执行，以及真实端到端效果/恢复。
- **预计改动/影响范围**：本 Agent 负责 Engine contract suite、检索模块依赖检查及中立 conformance 预期；Server/Gateway/ES 真实接入测试和 API/Web/CLI/eval/deploy 全入口由所属模块／解析集成主线验收。
- **用户验收**：① Engine 测试无需数据库、ES、媒体服务或模型；② 无 V2 Engine→外层反向依赖；③ adapter tests 证明外层未改变 Engine 语义；④ 隔离真实 ES/MySQL/模型完成效果、成本、延迟与故障验证；⑤ 目标运行、评测和部署退出 Qdrant。
- **Agent 指示与真实引用**：按本会话、本文 §0/§6、D §0.1/§0.6，以及 M §4.4、§5「批次4」、§9.1–9.2 与 E §2.3.8/.12。本 Agent 按变更运行 Engine 直接测试和依赖检查；外部验收分别报告 adapter 接线、真实协议、入口与质量，不用测试总数代签收。
- **当前判定**：10-01 历史 `tests/engine` 262 passed、Server 定向 94 passed、ES integration 3 skipped 与依赖扫描记录保留。10-02 本次只更新地图及核对源码，未跑业务测试；R6 其他会话已有本地产品回归记录，不能相加或冒充本批结果。真实 ES/MySQL/实际模型与质量仍未验收。
- **外部 TODO / 剩余边界**：所属模块／解析集成主线提供隔离真实 ES/MySQL、实际模型和完整产品场景证据；环境、质量阈值及验收负责人待确认。Engine 独立能力可先验，外围缺口如实限制整链验收，数据切换与发布留 P7。

### P7｜数据切换与整版发布

- **功能**：Engine 合同冻结、adapter 验收完成后，执行受控重建/迁移、配置切换、真实请求和恢复验证。
- **预计改动/影响范围**：Server、运维、配置和实际数据；Engine 只提供版本化合同标识，不承载迁移或发布逻辑。
- **用户验收**：目标配置、task/content/contract/revision 一致；旧文档以更高 `index_generation` 重建或写入已确认的空索引；真实请求使用新 pipeline；非 COMPLETE 任务不可检索；数据对账和恢复符合审阅方案。
- **Agent 指示与真实引用**：按本会话、本文 §0、D §0.1/§0.5，以及 M §10、H 状态机、E §2.3.11。本 Agent 只提供冻结的 Engine 合同、版本身份和相容性说明，不执行运维或数据切换；升级不得用相同 external version 覆盖旧文档，否则 strict external version 409。执行存量迁移、破坏性清理、服务重启、commit/push 前核对相应明确授权。
- **当前判定**：未执行、未通过；代码存在、容器启动或 alias 切换都不等于整版发布。
- **外部 TODO / 剩余边界**：Server／运维及发布负责人负责重建、迁移、配置切换、对账与恢复；具体负责人、窗口及目标环境待确认。须先完成合同签收与所需真实验收，再按独立操作授权执行。

## 4. 当前源码证据与放置差距

| 能力 | 当前证据 | 已有范围 | 仍需收回或补齐 |
| --- | --- | --- | --- |
| Engine Source Schema | [schemas.py](../../../engine/parsev2/schemas.py)、[planning.py](../../../engine/parsev2/planning.py)、[validation.py](../../../engine/parsev2/validation.py) | 正式 Timeline、人名、Chunk/Interval 规划和校验，由解析主线维护 | 本 Agent 消费必要接口；真实解析质量与正式签收交解析主线 |
| Agent 消费 Engine | [runtime.py](../../../agent/video_parse_v2/runtime.py)、[state.py](../../../agent/video_parse_v2/understanding/state.py)、[prompts.py](../../../agent/video_parse_v2/prompts.py) | Agent 直接 import Engine Schema/规划/校验 | 继续禁止平行领域 DTO |
| Engine Index Pipeline | [index_pipeline.py](../../../engine/retrieval/index_pipeline.py)、[embedding_reuse.py](../../../engine/retrieval/embedding_reuse.py)、[index_projection.py](../../../engine/retrieval/index_projection.py) | `ChunkIndexSource/Plan/Document/Receipt`、两种视觉 manifest、encoder/input identity、复用判定、候选身份及字段投影，已有历史纯测；来源记录沿用 | 本 Agent 补结构化采样/派生指纹、可选 Sparse 与校验/复用；参数/分词/权重/评分按具体行为签收 |
| Engine Retrieval Pipeline | [contracts.py](../../../engine/retrieval/contracts.py)、[video_retrieval.py](../../../engine/retrieval/video_retrieval.py) | 部分 DTO、lexical/text/vision 的 RRF 与版本化融合 | 本 Agent 补 Query Plan、通道请求、raw-hit 归一及完整证据/精筛 pipeline，外围提供真实查询和同版事实 |
| 物理索引 adapter | [video_parse_index_coordinator.py](../../../server/service/video_parse_index_coordinator.py)、[elasticsearch.py](../../../server/storage/elasticsearch.py)、[video_parse.py](../../../server/service/video_parse.py) | thin coordinator 读取正式成果/媒体并消费 Engine plan；两路编码、复用、bulk、exact readback、readiness、receipt 与状态推进已有 fake/SQLite 验证 | ordered frames adapter；真实 ES/MySQL/实际模型协议与故障恢复验收 |
| 当前产品检索接线 | [server video_retrieval.py](../../../server/service/video_retrieval.py)、[bootstrap.py](../../../server/bootstrap.py)、[D §0.3–0.6](../../video-parse-v2-handoff.md) | V2 mode 的 Search/Chat 已本地接 ES 三路、Engine versioned RRF 和 MySQL 同版 hydrate；V1 mode 保留旧服务 | 本 Agent 提供完整 Engine Query Plan/证据规则；外围 adapter 改接该合同并补真实产品验收 |

10-01 历史 V2 核心依赖扫描没有发现 `engine/parsev2` 或 `engine/retrieval` 导入 Agent、Server、Gateway、Elasticsearch 或 SQLAlchemy，这是正确基础。不能扩大为“整个 `engine/` 已解耦”：历史 `engine/parsev1` 仍直接依赖 Gateway，属于另行处理的遗留债务。本次未将历史扫描冒充重新运行。

原先位于 `server/service/video_parse_indexing.py` 与 `server/service/embedding_reuse.py` 的索引合同、encoder/input identity 和复用规则已迁入 Engine；旧模块已退出。`server/storage/elasticsearch.py` 现在接收 Engine `VideoChunkIndexDocument` 并负责物理 ID/external version、mapping/DSL、bulk、响应解析和查询可见性。Server 继续保留 Repository 读写、媒体物化、模型 port 调用、任务状态/事务、receipt 持久化与故障恢复；这些 I/O 职责不把纯索引语义带回 Server。

### 4.1 revision、外部版本与 receipt

`source_revision` 表示正式来源身份，`projection_revision` 表示投影合同与投影内容身份；encoder identity 决定向量相容性。它们与 `content_version`、`index_generation`、解析任务身份不是同一个概念。Engine 已定义这些逻辑语义及重写/复用决定，Server 将它们翻译成持久化字段。

当前 ES adapter 使用 `(index_generation << 31) | content_version` 生成外部版本，并以严格 `version_type: external` 写入；`index_generation` 由 `server/config/elasticsearch.py` 经 bootstrap 配置，不是不可变常量。升级已有文档时必须提高 `index_generation`，或确认目标是空索引后重建；若沿用相同 generation/content，严格 external version 会以 409 拒绝覆盖。更高 generation 压过旧 Worker 的所有 content version，相关拒绝与重用已有本地测试，真实 ES 仍待验证。

已确认的结构化 receipt 可以保存在现有任务 detail 中，记录 `task/chunk`、`content_version`、`source_revision`、`projection_revision`、`retrieval_contract_id`、两路 encoder ID 和 `index_generation`。ES 是文档存储，没有“新增表”的概念；receipt 也不是新业务表或发布指针。

### 4.2 写成功、查询就绪和业务可搜索

```text
bulk 每项成功
  → refresh 后 Engine 规定的完整文档可查询且身份匹配
  → Interval / Video 索引状态按成员汇总完成
  → 查询入口确认当前选定解析任务为 COMPLETE，并固定相符 content_version
  → 同版正式事实回查后才可返回
```

Chunk 基础解析状态可以先于索引完成；索引就绪只限制 Interval/Video 的 COMPLETE 汇总。选定 Video 任务非 COMPLETE 时，即使 ES 残留旧值也不得返回。不为旧版持续服务新增 build/publication 表或发布指针。

## 5. 层间合同表

| 层 | 必须提供 | 可以消费 | 不得拥有 |
| --- | --- | --- | --- |
| Engine | Source Schema/规划/校验；完整索引 pipeline；完整召回/RRF/精筛/证据 pipeline；存储/模型中立 ports | 纯数据、配置、adapter 返回值 | Agent/Server/Gateway 模块，ES/MySQL/Provider SDK，文件/网络 I/O |
| Agent/Harness | 消费 Engine Schema，组织模型工作并返回正式结果 | Engine 公共 Schema/校验，Gateway 模型调用 | 平行 Source Schema、索引投影或召回策略 |
| Gateway | 实现 embedding/scorer port，管理调用成本、超时和取消 | Engine 模型中立请求合同 | 字段、候选、融合、证据或失败业务语义 |
| Server | MySQL/媒体/状态/事务/ES I/O，adapter 翻译、可靠同步、入口装配 | Engine pipelines 与 Gateway adapter | 索引投影、复用判定、查询融合和证据判定 |
| Elasticsearch | 执行 adapter 翻译后的 mapping、文档持久化和检索 | Engine 逻辑规格的物理翻译 | 正式事实、COMPLETE 判定和业务检索策略 |

“运行时向 Engine 提供数据”不代表 Engine 可以 import 宿主模块。代码依赖始终由 Agent/Server/Gateway 指向 Engine。HNSW、倒排结构、refresh、bulk 和 ES DSL 是物理执行；字段语义、通道用途、候选身份和融合规则是 Engine 合同。

## 6. 验证证据与本轮检查

**2026-10-01 历史本地记录**按能力层保留。以下命令并未在本次 10-02 地图更新中重跑，不能合并为完整真实验收：

| 10-01 历史命令/检查 | 结果 | 能证明的范围与限制 |
| --- | --- | --- |
| `.venv/bin/pytest -q tests/engine` | **262 passed** | Engine 纯领域、Index Pipeline/复用、候选身份与现有 Retrieval 子集；无需 ES/MySQL/实际模型。测试全集也包含历史 parsev1，262 不等于 P2/P4/P5 全部签收 |
| Server 定向 9 文件：index coordinator、repository/worker、ES storage、media/model calls/queue、ES CLI、lexical contract | **94 passed** | fake ES/embedder 与 SQLite 下的 thin coordinator/adapters：bulk、readback、readiness、receipt、generation、重试和状态子范围 |
| `.venv/bin/pytest -q tests/server/test_elasticsearch_integration.py` | **3 skipped** | 未配置真实 Elasticsearch URL；因此不构成真实 ES 协议证据 |
| `compileall` | **通过** | 涉及 Python 模块可编译；不证明运行协议或业务语义 |
| V2 Engine 依赖扫描 | **通过** | 未发现 `engine/parsev2`、`engine/retrieval` 导入 agent/server/gateway/elasticsearch/sqlalchemy；不扩展为整个历史 `engine/` 已解耦 |
| `git diff --check` | **通过** | 当时差异无 whitespace error；不证明功能正确 |

10-01 Server 定向命令为：

```text
.venv/bin/pytest -q tests/server/test_video_parse_index_coordinator.py tests/server/test_video_parse_repository.py tests/server/test_video_parse_worker.py tests/server/test_elasticsearch_storage.py tests/server/test_video_parse_media.py tests/server/test_video_parse_model_calls.py tests/server/test_video_parse_queue.py tests/server/test_cli_elasticsearch.py tests/server/test_search_lexical_contract.py
```

10-01 历史本地自动化合计 **356 passed**，只代表 Engine 纯测试与 fake/SQLite adapter 子范围；真实 ES 为 3 skipped。该数字不能证明后续工作区或完整检索合同已验收。

**2026-10-02 R6 其他主线记录**见 [D §0.6](../../video-parse-v2-handoff.md#06-r5-历史证据与-r6-已运行验证)：Python 最终 `1646 passed, 17 skipped`、Web `201 passed`，并有 Search/Chat、CLI、eval 等本地接线证据。其定向分组存在交集，不能与 356 或彼此相加；这是引用其他会话的同版历史记录，不是本 Agent 本批重跑结果。D 仍明确未证明真实 ES/MySQL、付费模型、数据切换和发布，完整 Engine query-policy 也未提供。

**2026-10-02 本次地图更新**只检查当前源码、handoff/报告引用及阶段职责，并同步可编辑场景和官方导出 SVG。已检查 8 个阶段的四项内容及职责/外部 TODO；146 个原生元素 ID 全部保留，主图及两分图文字与导出场景一致，P2 当前标记在主图和 P0–P3 分图各出现一次。三张 SVG 经 `@excalidraw/excalidraw` 0.18.1 的官方 `exportToSvg` 导出，XML/viewBox 合法且无整图栅格附件；已实际渲染主图、两分图及 P2/P3/P4/P6 节点核对中文、裁切和布局。未修改业务代码、未运行业务测试；真实 ES/MySQL、实际所选模型协议、效果/成本/延迟、故障恢复、数据切换和 V2 整版发布继续未验收。

P6 还必须补三类独立证据：

1. Engine standalone contract suite：不用真实基础设施即可覆盖 Source、Index、Retrieval pipeline。
2. dependency/adapters conformance：无反向依赖，外层输入输出不改变 Engine 语义。
3. real components/end-to-end：隔离真实 ES/MySQL、实际所选模型协议、公开入口、效果/成本/延迟和故障恢复。

## 7. 目标完成判定

| 判定 | 当前是否达成 | 还差什么 |
| --- | --- | --- |
| 本轮 Engine-first plan-map | **本轮已更新并通过产物检查，供用户 review** | 已确认职责继续有效；采样/Sparse 建议仍待收敛 |
| Engine 独立提供 Source、Index、Retrieval 三条能力面 | **部分达成** | P1 基础由解析主线维护；本 Agent 补 P2 结构化采样/Sparse、P4 完整查询与 P5 精筛合同 |
| 外层只是 ports/adapters | **索引/查询链本地部分达成** | P3 video clip adapter 与 P4 产品接线已消费 Engine 现有合同；有序帧/Sparse、完整 Query Plan 的改接和真实 conformance 仍待外部执行 |
| ES 目标检索代码完成 | **否** | P2 收尾签收、P3 缺口与真实验收、P4 完整 Engine 策略、P5 确认范围、P6 全入口/质量 |
| 真实环境验证完成 | **否** | 隔离 ES/MySQL、实际模型协议与质量、并发/恢复验收 |
| 存量数据切换完成 | **否** | 方案 review、明确授权、执行与对账 |
| V2 整版发布完成 | **否** | 上游 V2、检索和产品全链通过后受控发布 |

## 8. 本轮修改与后续维护

本次同步 5 份设计产物：本文、可编辑 `.excalidraw`、主 SVG、P0–P3 SVG、P4–P7 SVG。保留原 P0–P7 结构和已完成成果，逐节点补本 Agent 的 Engine 检索职责与外部 TODO；当前批次定位为 P2 收尾并交接 P3，P2 保持唯一当前阶段。同步来源记录/nested 的已澄清含义及结构化采样/Sparse 的待开发边界，纠正 P4 V2 Search/Chat 本地接线状态，并将历史测试与本次检查分开记录。

三张 SVG 已通过官方 Excalidraw 从同一版可编辑源场景导出；两分图只重排对应阶段及引用，不在 SVG 中独立修改文字。**本次产物检查通过**：源图 JSON/节点字段、当前标记一致性、源码/文档文件引用、主图及分图文字一致性、纯矢量官方导出和实际渲染可读性。独立 Agent 仅凭阶段节点及其引用完成接手审查，未发现阻塞交付的问题。该结果只验收地图产物，不代表 P2 设计全部通过或 P3/P4 真实链路已通过。

这些 plan-map 产物记录当前工作区已有源码与测试，不将设计同步本身冒充业务实现。没有运行迁移、清理、重启、commit 或 push；工作区其他既有或并行修改原样保留。

后续源码或决定改变时，继续使用相同 P 编号更新状态与证据。任何纯索引/召回规则新增前先判断是否能在 Engine 脱离基础设施运行；能独立表达的规则一律先进入 Engine，再由 Server/Gateway/ES adapter 执行。源图变化后重新用官方 Excalidraw 导出主图和两张分图，不能单独手改 SVG。
