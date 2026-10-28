# Refactor 3：Elasticsearch 统一检索与 Qdrant 替换计划

更新时间：2026-09-28。

> **Event 索引修订（2026-10-17，待实现）**：[Event 细粒度检索 TRD](event-retrieval-trd.md) 增加 MySQL Event 派生实体与独立 ES Event 集合；Chunk 的完整有序 Timeline 仍为权威事实，现有 Chunk 三路索引保留。任务与恢复以 [Parse Agent TRD](video-parse-harness-trd.md) 当前基线为准，Interval 只 Refine，同一 ChunkTask 的 INDEX 阶段承担 Chunk/Event 同步。下文旧 Interval 索引及 Qdrant 运行状态说明保留原时点，不作为当前实现依据。

状态：当前先学习 RAGFlow，评审 ES 架构与 Index 设计。已有基础代码及验证记录见第 13 节；其中独立构建/发布方案已被下述决定替代，不能继续当作生产接线目标。当前文档同步不代表代码改接、真实 ES/MySQL 验证或数据切换已完成。

> **2026-09-28 用户确认：撤销“重解析或重建时旧版必须持续可检索”的要求。**
> 以当前选定的 Video 解析任务状态控制检索资格：非 `COMPLETE` 时，即使 ES 仍有旧值也不得返回该任务的数据。
> 各通道与 MySQL 事实回查仍须使用同一任务和相符的内容版本。任务状态机以两份 TRD 为准；
> 不为持续服务旧版新增独立 build / 发布指针。下文实施批次需经设计评审后按实际授权推进。

本文可交给新的 Agent 会话独立阅读。目标是让 Elasticsearch 承接词项、Chunk 文本语义向量和 Chunk 视频向量检索，并完成索引同步、评测、部署与调用方的接入。

**后端选择已确定：MySQL + Elasticsearch。Qdrant 退出目标架构。**

本文最初只记录计划。第 6 节是整理时的 V1 预计范围，不代表全部文件已修改；实际实施范围和验证结果以第 13 节为准，当前运行系统仍使用 Qdrant。

## 1. 阅读顺序与执行边界

### 1.1 先读哪些资料

| 资料 | 用途 |
| --- | --- |
| [refactor_2.md](refactor_2.md) | V2 任务、成果与 Worker 接入计划；确认它在执行时已经完成到哪一步 |
| [Engine TRD](video-parse-engine-trd.md) | 第一章是正式解析合同；2.3 是三路检索设计，2.3.11 是 ES 接入与查询就绪边界 |
| [Harness TRD](video-parse-harness-trd.md) | Chunk/Interval 任务与成果合同、解析及索引状态机；独立索引发布表草案已撤销 |
| [Harness README](../../../agent/video_parse_v2/README.md) | 当前 ParseAgent 入口、注入边界与实现状态 |
| [Engine Schema](../../../engine/parsev2/schemas.py) | 对照目标合同与运行代码差距；不能用旧人物 ID 实现覆盖已确认的人名合同 |
| [refactor_1.md](refactor_1.md) | Server 内部 Consumer 与纯 Engine 分工的历史依据，不照搬其中旧路径 |
| [seekino-change-gate](../../../.agents/skills/seekino-change-gate/SKILL.md) | 根据实际授权和改动范围安排执行批次 |
| [hypnos-code](/Users/bytedance/.codex/skills/hypnos-code/SKILL.md) | 保持既有分层、最小改动和可验证性 |

本机项目根目录：/Users/bytedance/workspace/other-project/Seekino。

本文的相对文件路径均相对此仓库；Markdown 链接相对本文。迁移到其他机器后替换根目录，不把本机路径写进产品代码。

用户最新明确决定优先于历史说明。当前源码是实现现状的证据，TRD 中的目标设计不等于已经上线。

本文和相关 design 文档被 .gitignore 的 `**/design/` 忽略。后续会话须直接读取，不能以 git status 未显示为由认为文件不存在。

### 1.2 与 refactor_2 的衔接

refactor_2 负责 V2 上传、任务、Chunk/Interval 成果和 Worker 接入，明确将 V2 embedding/index 排除在其实现范围之外。

本文承接检索与索引工作，不能假设 refactor_2 已经完成，也不能为了 ES 接入重做它的任务模型。

本文的 60 文件清单根据当前 V1 运行链路盘点：

- 若执行时仍是 V1，可先用现有持久化事实验证 ES 适配器、查询与同步合同。
- 若 refactor_2 已完成，应直接从新的任务/成果 Repository 取数，修订对应文件落点。
- 不应先完整重做即将淘汰的 V1 读取链，再为 V2 重写一遍。
- ES 的字段、查询与测试环境可先推进；V2 正式成果读取和任务状态接入依赖上游持久化合同。
- 保持 refactor_2 已确认的语义：主动重解析新建任务，失败重试沿用原任务；固定 VideoChunk 拓扑复用。

按 2026-09-28 TRD，解析就绪与索引就绪是不同阶段；各 Chunk 可独立进入索引阶段，
Video 主任务在必需解析及索引全部就绪后汇总为 `COMPLETE`。非 `COMPLETE` 的选定任务不开放检索。

### 1.3 授权和范围

用户已确定后端选择。本文最初的编写会话未执行源码改造；2026-09-27 开始的实施批次见第 13 节。

后续会话以用户当次执行指令和已有授权为准；已经授权的批次无需反复询问。涉及范围新增时，先核实授权与适用规则，再给出具体可审阅的变更。

本文没有授权清空已有数据卷、删除业务索引、执行生产迁移、重启运行服务、commit 或 push。执行这些操作前按已有会话授权和对应环境规则处理。

## 2. 已确认的目标合同

### 2.1 数据与职责分工

| 层 / 组件 | 职责 |
| --- | --- |
| MySQL | 正式解析成果、内容版本及任务中的解析/索引状态；提供索引重建来源 |
| Elasticsearch | 保存可重建的词项内容、文本向量、视频向量及过滤/定位字段，执行三路检索 |
| Engine | 定义领域 Schema、检索投影和查询策略；生成编码输入，做 Chunk 归并、融合及证据校验 |
| Parse Harness | 消费 Engine 提供的 Schema，编排模型理解、纠偏和增强，不接触 ES 客户端 |
| Gateway | 执行已有的编码和模型调用，承接用量、超时、取消与成本统计 |
| Server Service / Worker | 读取可信范围、组织模型与索引调用、持久化任务状态、重试、对账及查询就绪检查 |
| Server 存储适配器 | 将 Engine 的要求落实为 mapping、analyzer、全文/KNN 查询及 ES 网络读写 |

Engine 保持可脱离 ES、MySQL、Gateway 和 LangGraph 运行。SDK、认证、连接、物理索引名和网络响应解析放在 Server。

现有分层能承载这次替换。第一版沿用具体模块，不预建通用 SearchBackend 工厂、多数据库路由或庞大的表达式框架。

### 2.2 默认三路检索

~~~text
正式解析结果 + 已启用的检索增强
    ↓ Engine 派生表示
词项字段             Chunk 语义编码输入       Chunk 实际视频
    ↓                      ↓ Gateway               ↓ Gateway
ES 全文/短语检索      ES semantic_vector      ES vision_vector
    └──────────────────────┼───────────────────────┘
                           ↓
                   Engine 按 Chunk 归并
                           ↓
                     现有加权 RRF
                           ↓
               按固定版本回查正式事实与证据
~~~

- 文本语义向量默认按 Chunk 构建，通常每 Chunk 一个。
- 视频向量默认按固定 Chunk 的实际视频构建，使用相容的跨模态 Query 编码器。
- 两类向量分字段保存，各自遵守编码空间、维度和相似度合同。
- questions 第一版参与词项检索；独立 question_vector 默认关闭。
- 同通道多个表示先按 Chunk 归并，再进入 RRF，避免增强条目多就获得更多票。
- 原话匹配保留原始字面与正式对白证据，普通 BM25 命中不自动证明说过该台词。
- 第一版继续在 Engine 融合，不以 ES 内置 RRF 功能或特定许可证为前提。
- RAPTOR、GraphRAG、逐事件向量、新的复杂 Reranker 仍是后续工作。

当前编码器是否允许共用一个 Query 向量，要由编码合同决定。现有服务要求两路维度相同并共用 Query 编码，这是现状，不能只去掉维度校验就宣称支持任意异构编码器。

### 2.3 Source 与证据约束

正式 Parse V2 结果以 Engine TRD 的 persons + timeline 为基础。

人物直接使用人名：speaker、participants、visible_persons 等目标字段不使用 person_id。当前 schemas.py 中旧 ID 字段的同步属于已存在的 V2 合同工作，接入时确认其完成状态。

Chunk 检索视图由正式结果、固定拓扑和可选增强装配：

- 视频没有必须让模型生成的通用“正文 text”；用于编码的文本由 Engine 从事实派生。
- 对白、画面文字、事件描述、人名、摘要、关键词、问题分别保留消费语义。
- context_before / context_after 是相邻 Chunk 的背景，优先用于消歧、展开和重排。
- 邻块内容出现在背景里，不会因此成为当前 Chunk 发生过的台词或动作证据。
- timeline_index 由投影侧派生，用于定位 Timeline 条目；必须绑定来源解析任务、Chunk 和内容版本，不能单凭下标回查已经变化的内容。
- timeline_index 不进入分词文本或 Embedding，也不代表默认采用事件级向量。
- 对白/OCR 语言描述对应文本的语言；增强 language 描述生成表示的语言，不能全局套成“音轨语言”。
- 当前上游未产出的增强保持缺失；数据库迁移不能凭空补造问题、字幕或人物事实。

固定 Chunk 拓扑、按 Task/Lease 生成临时媒体、Gateway 统一成本与取消管理沿用各自设计。ES 替换不承担重设计这些机制的工作。

## 3. 当前实现与源码入口

源码基线 HEAD：a60268d4b48f0a18687aadb7ee5289bbc1906d01，工作区另有未提交修改。

行号可能随其他会话变化，优先按文件和符号定位。

| 入口 | 当前行为 / 本次需要核对的点 |
| --- | --- |
| [server/storage/qdrant.py](../../../server/storage/qdrant.py)，QdrantVideoChunkStore | 两类 named vector、范围过滤、upsert/delete/inspect/search；正式接入的主要替换点 |
| 同文件 VideoChunkIndexPoint | 当前只携带归属、source_revision、fallback 和两类向量；缺少完整词项输入 |
| [server/service/parsev1/index_sync.py](../../../server/service/parsev1/index_sync.py)，IndexSyncService | 同步、指数退避、reconcile、rebuild；_apply_item 构造向量记录 |
| [server/repository/index_sync.py](../../../server/repository/index_sync.py)，_compute_index_source_revision | 当前摘要覆盖身份、fallback、操作和向量；新增词项内容及投影合同后必须调整 |
| [server/model/index_sync.py](../../../server/model/index_sync.py)，IndexSyncIntentRecord | 当前每 Chunk 一条同步意图，generation 防止过期回调覆盖；不是整版发布模型 |
| [server/model/dto/indexSyncDTO.py](../../../server/model/dto/indexSyncDTO.py)，IndexSyncItem | 索引同步的数据交接入口 |
| [server/service/video_retrieval.py](../../../server/service/video_retrieval.py)，VideoRetrievalService.search | MySQL 限定可信范围，ASR 内存匹配，Query 编码，Qdrant 双路查询，RRF 与 hydrate |
| [engine/retrieval/video_retrieval.py](../../../engine/retrieval/video_retrieval.py) | 文本投影、rank_transcript_chunks、fuse_video_chunk_rankings 等纯算法 |
| [engine/retrieval/contracts.py](../../../engine/retrieval/contracts.py) | 候选排名、MatchedEvidence、SelectionReason 和正式检索结果 |
| [server/repository/video.py](../../../server/repository/video.py) | 当前成果导入、搜索范围、ASR 读取、正式证据回查及身份/版本信息 |
| [server/service/parsev1/parse_persistence.py](../../../server/service/parsev1/parse_persistence.py)，persist | 当前事实持久化入口，先提交 MySQL，再由执行服务同步索引 |
| [server/service/parsev1/video_processing.py](../../../server/service/parsev1/video_processing.py)，_sync_index | 当前按 Chunk 同步索引后推进 checkpoint；不等于 V2 整版发布 |
| [server/service/qdrant_mutation_lock.py](../../../server/service/qdrant_mutation_lock.py) | MySQL 命名锁保护同环境集合的并发写入/删除 |
| [server/service/upload.py](../../../server/service/upload.py)，delete_session | 索引删除、Artifacts 清理和业务删除状态协作 |
| [server/repository/upload.py](../../../server/repository/upload.py)，reparse | 旧流程重启原 Job；refactor_2 已要求改为新建解析任务 |
| [server/bootstrap.py](../../../server/bootstrap.py)、[server/config/settings.py](../../../server/config/settings.py)、[server/cli.py](../../../server/cli.py) | 客户端生命周期、环境变量及运维命令 |
| [scripts/local_services.py](../../../scripts/local_services.py) | 目前会探测并懒启动原生 Qdrant，也负责健康检查和退出 |
| [evals/recall/qdrant_index.py](../../../evals/recall/qdrant_index.py) | 独立实验索引，有自己的 dataset_id；不是生产 Store 的调用方 |
| [evals/recall/recall_eval.py](../../../evals/recall/recall_eval.py) | 当前报告主要是文本/视频向量及融合指标，Qdrant 与 local 两种后端 |
| [agent/chat/video_search/state.py](../../../agent/chat/video_search/state.py)，RetrievalChannelWeights | 当前 transcript 权重明确描述 ASR 原话；新词项语义需要更新 |
| [web/src/features/search/explain.ts](../../../web/src/features/search/explain.ts) | transcriptRank 被直接解释成“ASR 原话”，不能拿来装一般全文命中 |

### 3.1 为什么不只是替换 SDK

1. ES 需要实际词项内容，当前同步 DTO 与版本摘要只覆盖向量记录所需字段。
2. 检索通道、证据说明和 UI 目前将词项路限定为 ASR，需要与广义全文语义对齐。
3. ES 索引可查询就绪需要明确验证，不能机械沿用旧 upsert 成功就标记完成的处理。
4. 生产和评测有两套独立 Qdrant 接入，替换范围必须覆盖两者。
5. 多份测试使用 QdrantClient(":memory:")；分析器、短语与向量查询需要真实 ES 验证。
6. 现有按 Chunk 更新的事实表与索引记录不提供 V2 的完整新旧版本共存能力。

### 3.2 RAGFlow 的可学习依据

本机 RAGFlow 位于 ../ragflow，优先直接读源码，无须重新做一轮网页调查。

| 文件 | 学习点 |
| --- | --- |
| [rag/nlp/query.py](../../../../ragflow/rag/nlp/query.py) | FulltextQueryer 的分字段查询与权重 |
| [rag/utils/es_conn.py](../../../../ragflow/rag/utils/es_conn.py) | MatchTextExpr / MatchDenseExpr 同后端处理；insert 的 refresh 与逐项错误检查 |
| [conf/mapping.json](../../../../ragflow/conf/mapping.json) | 全文字段与 dense_vector 映射 |
| [common/settings.py](../../../../ragflow/common/settings.py) | 存储选择和检索器装配边界 |

RAGFlow 的默认 ES 路径同时保存全文与向量。它的目录结构、所有词权重或融合公式不自动成为 Seekino 合同，也不能据此声称 MySQL 与 ES 有跨库事务。

## 4. 索引与查询的具体改造要求

### 4.1 索引输入

默认把同一 Chunk 的词项内容与两类向量放在同一 ES 文档中。发生分段表示时，记录必须继续关联同一 Chunk、解析任务和来源内容版本。

2026-10-17 补充：上述“一 Chunk 一文档”限定 Chunk 索引，不限制新增独立 Event 搜索池。
Event 使用原始 Timeline 位置、正式内容版本及事件时间定位来源，首版类型过滤与词法匹配不强制新增向量。
字段、过期集合清理及回填统一见 [Event TRD](event-retrieval-trd.md)，不在本节复制 mapping 合同。

| 内容 | 处理要求 |
| --- | --- |
| 对白 | 保留原文、语言、角色、直接人名与时间；用于全文/短语候选和正式证据回查 |
| 画面文字 | 独立于对白的字段与来源，避免混淆字幕、台词和其他屏幕文字 |
| 事件描述 | 用于全文及 Chunk 语义输入，保留事件时间和来源定位 |
| 人名 | 使用名称；全文召回与精确过滤按查询合同处理 |
| summary / keywords / questions | 分字段保存并使用对应权重；只消费上游实际提供的增强 |
| semantic_vector / vision_vector | 独立配置、独立 KNN 查询；验证模型、维度、相似度及输入合同 |
| 范围与定位 | 携带可信业务范围、Chunk、解析任务、内容版本、时间、语言及必要证据位置 |

ES mapping、语言分析器和输入规范必须显式版本化。中文、日文及跨语言对白需要实际样例验证。

若条件要求“同一事件中的人物与台词”，不能把多事件对象扁平化后错误地跨条目匹配。采用保留关联的索引结构或回查校验，具体实现按最小可行合同确定。

### 4.2 同步、版本摘要和恢复

优先沿用已有“正式事实与同步意图同事务提交，外部索引随后写入”的方式。

Event 补充：全部关联 Refine 提交后，由既有 Chunk INDEX 从当前正文派生并幂等保存 Event，再同步 Chunk/Event ES。
完成判定涵盖预期 Event 集合、来源版本、租约和查询可见性，含零事件及旧 Chunk ES 已就绪的复用分支。
沿用当前 Index 恢复点，不新建 Event 调度队列；重试与回填不重跑成功的 Refine，不提高事实内容版本。

索引源摘要至少覆盖实际消费的词项内容、向量/编码身份、业务归属、投影版本和检索合同。新增字段必须有消费方，不为未知未来字段预建冗余状态。

重点行为：

- 只修改台词、关键词或问题，也能使相关索引进入待同步状态。
- 一个 ES 文档的必需字段按同一来源快照写入，避免词项来自新版而向量来自旧版。
- 批量写入逐项检查，失败项保留可恢复状态；HTTP 成功不能替代逐项成功。
- 写入超时可能存在未知成功状态，重试依赖确定性身份和版本约束。
- 查询就绪采用与选定 ES 版本相容的 refresh/查询验证策略，不在每条热路径上无条件强制刷新。
- 对账覆盖缺失、版本过期、删除及必需字段不完整；不能只比较记录总数。
- 旧 Worker 的成功回调不能把新的同步意图标记成已完成。
- 保持租约/fencing、generation 和已有写锁的作用，不因更换 SDK 删除并发保护。
- 切换期间，旧 Qdrant Writer 与新 ES Writer 不能共用无法区分目标的意图并互相标记成功。

### 4.3 查询与证据

Server 从 MySQL 读取可信范围、当前选定任务的状态及内容版本，只让 `COMPLETE` 任务进入各路检索。
ES 中存在旧文档或向量不能绕过状态条件。各路候选与事实回查沿用同一任务及相符的内容版本；
回查时状态或版本已变化，不能返回不再符合条件的候选或拼接新旧事实。对话模型不能扩大授权范围。

三路独立取候选，按 Chunk 归并后执行现有加权 RRF。词项未命中不应自动阻止语义/视频候选。

广义词项合同建议使用清晰的 lexical 语义；最终字段名在接口批次确定。不能直接把已有 transcriptRank 的含义改成“任何全文命中”而继续展示 ASR 标签。

兼容范围包括：

- Chat 工具输入、实际调用参数和 trace；
- 持久化的历史 Agent 结果；
- API DTO/VO 与前端 TypeScript 合同；
- 旧评测报告和已有缓存。

保留旧结果读取时说明旧字段的真实含义，不伪造新的命中来源。

原话、人物、时序等硬要求由正式证据验证。生成问题只提供召回线索，不能当作实际对白；检索分数不能覆盖明确条件。

### 4.4 评测与成本

保留本地精确向量检索作为对照。ES approximate/exact 模式按实际实现命名，不能把 Qdrant 的 exact 参数直接原样透传。

既有评测主要覆盖文本与视频向量。基础三路验收还需记录词项路及融合后的指标、实际命中来源和时间定位。

迁移优先从 MySQL 的正式向量记录及有来源元数据的 Artifact 重建 ES。输入、模型、维度、采样及归一化合同相容时复用向量。

不要仅因为更换存储重新解析视频或重新调用 Embedding 模型。无法证明相容的向量应明确报告缺口，不静默重新消耗模型费用。

## 5. 分批实施与完成条件

### 批次 0：复核执行基线和待定合同

- 读取 refactor_2 的实际完成状态、两份 TRD、当前源码和工作区差异。
- 确认正式成果的读取入口、任务与内容版本身份、`COMPLETE` 状态条件及 Engine Schema。
- 固定 ES 服务端/客户端版本、部署方式、分析器、mapping 和 Query 编码合同。
- 确定 lexical 合同及历史结果兼容策略。
- 从第 6 节清单缩小本批修改树；若已迁入 V2，替换失效的 V1 落点。
- 在实现前明确第 8 节中会阻挡本批的事项，其余允许后续决定。

完成条件：字段来源、消费方、身份、兼容及实际修改文件可以逐项说明。

### 批次 1：ES 适配器和基础连接

- 实现明确的字段 mapping、两类向量查询及全文/短语候选查询。
- 实现索引存在性/合同校验、写入、删除、inspect 和响应转换。
- 装配客户端生命周期，提供受控测试配置。
- 验证身份一致、范围过滤、维度不匹配、错误分层及真实 ES 查询。

完成条件：独立测试索引中可保存并查询三路表示，错误不会被吞成成功。

### 批次 2：事实投影与可靠同步

- 从当前正式成果 Repository 获取字段；按执行时的 V1/V2 合同选择唯一输入路径。
- 扩展索引同步 DTO、版本摘要、待执行判断与重建过程。
- 处理逐项失败、未知成功、重试、过期回调、对账和删除交错。
- 补齐查询可见性判断，避免提前推进 INDEXED / SYNCED。

完成条件：事实或词项变化能触发同步；ES 失败不破坏 MySQL 正式结果，恢复后能自动收敛。

### 批次 3：三路检索、Chat 与展示

- 三路查询通过同一 ES 适配器执行，保留可信范围与版本约束。
- Engine 归并和融合；原话/人物/时间条件回查正式事实。
- 更新 Chat 权重、卡片、trace、API 合同及前端说明。
- 验证历史结果可读，且非对白命中不会显示为 ASR 原话。

完成条件：三路能够分别产生有效候选，融合和证据归因都正确。

### 批次 4：评测、CLI、部署与依赖收敛

- 替换评测侧的独立 Qdrant 接入，保留数据集隔离和本地向量基线。
- 更新同步/对账/重建/搜索 CLI、环境变量及运行文档。
- 调整 local_services 和 Compose 的启动、健康检查及停止行为。
- 清除运行代码、评测和测试对 qdrant-client 的依赖。
- 历史文档与迁移脚本中的 Qdrant 记录按其历史语义保留，不用全仓字符串替换改写历史。

完成条件：仅配置 MySQL 与 ES 即可完成已接入的运行、索引和评测流程。

### 批次 5：V2 任务状态、索引就绪与检索资格

2026-09-28 用户明确撤销旧版持续服务要求。沿用两份 TRD 的任务状态机，
**当前选定任务非 `COMPLETE` 时不可检索；ES 残留旧值不能成为放行依据。**

与 refactor_2 的任务/成果模型衔接：

1. 从正式成果读取选定解析任务、Chunk、内容版本及编码合同；这些身份同时进入索引与事实回查。
2. 重解析或重建按任务状态流转；仅重建索引复用成果及相容向量，不重跑已成功的解析。
3. 每个 Chunk 的必需表示逐项写入成功并确认可查询后，才可将该 Chunk 标记为 `COMPLETE`；
   Video 主任务汇总必需 Chunk / Interval，选定 Video 任务完成后才开放检索。
4. 索引失败保留 MySQL 成果及子任务恢复点；ES 部分成功、旧值或旧 Worker 回调都不能将新内容误标完成。
5. 各路查询和事实回查使用同一任务及相符的内容版本；在途内容/状态变化不能产生新旧混合证据。
6. 删除、重试和对账按可信任务/视频范围处理；旧索引残留不能绕过 MySQL 状态与版本检查。

解析次数、内容版本和同步 generation 仍各有职责，不能互相替代。
Refine / Prefix 改写检索内容时增加内容版本，并让受影响 Chunk 回到 `INDEXING`，主任务重新汇总。
不维护独立 build ID 或发布指针；已有三表及配套代码按后续评审结果改接任务流程，
不因本次文档决定直接执行删表、迁移或数据清理。

完成条件：非 `COMPLETE` 任务在旧索引仍存在时也不可检索；全部必需表示就绪后恢复检索，
最终证据与命中的任务、内容版本相符，失败与中断可从已有成果恢复。

**批次 1～4 完成不代表 V2 任务状态与检索资格已接通；独立整版发布不再是本批目标。**

## 6. 预计修改树：ES 替换与基础三路

本清单为当前源码下的预计范围，共 60 个文件，不包括本文，也不包括批次 5 尚待确定的额外落点。

M：修改；R：重命名并调整实现，箭头后为原路径；A：新增。

列入清单不表示必须机械修改所有文件。实现前按最新源码收缩范围；新增范围应有具体消费方和验收依据。

~~~text
Seekino/
├── agent/
│   └── chat/
│       └── video_search/
│           ├── tools/
│           │   ├── [M] retrieval.py
│           │   │   影响：传递词项通道参数并记录实际生效权重。
│           │   └── [M] shared.py
│           │       影响：检索卡片正确表达词项排名与实际命中来源。
│           └── [M] state.py
│               影响：更新词项通道合同与说明，处理持久化历史权重的读取。
├── deploy/
│   ├── elasticsearch/
│   │   └── [R] compose.yaml ← deploy/qdrant/compose.yaml
│   │       影响：独立检索服务部署改为 ES，原数据保留策略另行执行。
│   └── [M] README.md
│       影响：说明 ES 部署、资源、数据目录及切换步骤。
├── docs/
│   ├── [M] api-ui-views.md
│   │   影响：更新真实检索联调及来源展示要求。
│   ├── [M] parse-config.md
│   │   影响：更新编码合同与索引配置的对应关系。
│   └── [M] parse-model-usage.md
│       影响：明确索引重建、向量复用与模型费用统计的关系。
├── engine/
│   ├── parsev2/
│   │   ├── design/
│   │   │   └── [M] trd.md
│   │   │       影响：记录实际索引合同、接入与验收边界。
│   │   └── [M] README.md
│   │       影响：按实际进度同步 ES 接入状态。
│   ├── retrieval/
│   │   ├── [M] contracts.py
│   │   │   影响：扩展词项排名与命中来源合同，明确旧结果的兼容读取。
│   │   └── [M] video_retrieval.py
│   │       影响：补充分字段词项投影及融合接入，复用 Chunk 归并、RRF 和原话验证。
│   └── [M] README.md
│       影响：更新检索边界及离线评测用法。
├── evals/
│   └── recall/
│       ├── [M] cli.py
│       │   影响：调整评测后端选择、ES 参数及校验。
│       ├── [R] elasticsearch_index.py ← evals/recall/qdrant_index.py
│       │   影响：替换评测后端，保留数据集隔离与确定性记录身份。
│       └── [M] recall_eval.py
│           影响：接入 ES 排名、词项评测与报告字段，处理旧报告兼容。
├── scripts/
│   └── [M] local_services.py
│       影响：调整服务启动、复用、健康检查、停止和配置指纹。
├── server/
│   ├── api/
│   │   └── [M] errors.py
│   │       影响：接入调整后的索引异常，保留既有 HTTP 错误分层。
│   ├── config/
│   │   ├── [M] retrieval.py
│   │   │   影响：补齐词项路的默认预算与权重，保持参数校验一致。
│   │   └── [M] settings.py
│   │       影响：加载 ES 连接、认证和索引配置，校验相容的编码配置。
│   ├── model/
│   │   └── dto/
│   │       └── [M] indexSyncDTO.py
│   │           影响：索引同步输入携带实际词项内容及必需的来源信息。
│   ├── repository/
│   │   └── [M] index_sync.py
│   │       影响：读取词项来源，将内容及投影合同纳入版本摘要和同步判断。
│   ├── service/
│   │   ├── [R] index_mutation_lock.py ← server/service/qdrant_mutation_lock.py
│   │   │   影响：沿用索引写入并发保护，明确目标索引及切换期间的锁作用范围。
│   │   ├── [M] index_sync.py
│   │   │   影响：调整索引输入、逐项失败处理、重试、对账及查询就绪判据。
│   │   ├── [M] upload.py
│   │   │   影响：视频删除时清理 ES 记录，保留失败恢复和并发保护。
│   │   └── [M] video_retrieval.py
│   │       影响：从 ES 获取三路候选，执行可信范围检查、融合与正式证据回查。
│   ├── storage/
│   │   └── [R] elasticsearch.py ← server/storage/qdrant.py
│   │       影响：实现 ES mapping、分析器、全文与两类向量查询、写入、删除和结果校验。
│   ├── [M] bootstrap.py
│   │   影响：创建、注入和关闭 ES 客户端，装配相关服务。
│   ├── [M] cli.py
│   │   影响：同步、对账、重建和搜索命令改为连接 ES。
│   └── [M] README.md
│       影响：更新同步、对账、重建和搜索命令。
├── tests/
│   ├── agent/
│   │   └── chat/
│   │       ├── [M] test_runtime.py
│   │       │   影响：验证 Runtime 检索调用及历史数据兼容。
│   │       └── [M] test_tools.py
│   │           影响：验证词项通道的工具输入、输出和权重。
│   ├── engine/
│   │   └── [M] test_video_retrieval.py
│   │       影响：验证词项投影、Chunk 归并、融合及原话约束。
│   ├── evals/
│   │   ├── [R] test_elasticsearch_index.py ← tests/evals/test_qdrant_index.py
│   │   │   影响：验证评测索引和数据集隔离。
│   │   ├── [M] test_recall_eval_cli.py
│   │   │   影响：验证新的命令参数及无效组合。
│   │   └── [M] test_recall_eval.py
│   │       影响：验证 ES 评测后端、各路指标和报告。
│   ├── server/
│   │   ├── [M] test_agent_chat_service.py
│   │   │   影响：同步检索调用合同，保护 Chat 接线。
│   │   ├── [M] test_agent_system_integration.py
│   │   │   影响：将 Agent 检索整链接到受控 ES 测试环境。
│   │   ├── [M] test_api.py
│   │   │   影响：同步测试配置并保护检索响应与范围约束。
│   │   ├── [M] test_bootstrap.py
│   │   │   影响：验证 ES 客户端装配、关闭及退出顺序。
│   │   ├── [M] test_config.py
│   │   │   影响：验证新环境变量、编码约束及配置错误。
│   │   ├── [A] test_elasticsearch_integration.py
│   │   │   影响：用真实 ES 验证分析器、短语、向量、过滤和查询可见性。
│   │   ├── [R] test_elasticsearch_storage.py ← tests/server/test_qdrant_storage.py
│   │   │   影响：验证适配器读写合同、输入检查和错误处理。
│   │   ├── [M] test_index_sync_repository.py
│   │   │   影响：验证词项内容及合同版本变化会触发同步。
│   │   ├── [M] test_index_sync_service.py
│   │   │   影响：覆盖部分失败、重试、对账、过期写入与删除交错。
│   │   ├── [M] test_local_services.py
│   │   │   影响：验证 ES 启动、复用和健康检查。
│   │   ├── [M] test_mysql_integration.py
│   │   │   影响：验证调整后的跨进程索引写锁及 MySQL 状态更新。
│   │   ├── [M] test_upload_service.py
│   │   │   影响：验证 ES 清理、失败恢复与并发保护。
│   │   └── [M] test_video_retrieval_service.py
│   │       影响：验证三路召回、可信范围、身份校验和证据回查。
│   └── [A] conftest.py
│       影响：新增受控的 ES 测试连接与索引隔离；不连接业务实例。
├── web/
│   └── src/
│       ├── api/
│       │   └── [M] contracts.ts
│       │       影响：接收调整后的候选排名、来源和兼容字段。
│       └── features/
│           └── search/
│               ├── [M] EvidencePlayer.test.tsx
│               │   影响：同步候选数据，保护证据播放展示。
│               ├── [M] explain.test.ts
│               │   影响：防止普通全文或生成问题命中被标为 ASR 原话。
│               ├── [M] explain.ts
│               │   影响：区分词项命中与原话证据，避免错误的 ASR 原话标签。
│               └── [M] SearchPage.test.tsx
│                   影响：同步搜索结果合同与展示断言。
├── [M] .env.example
│   影响：提供 ES 连接、认证、索引及检索参数示例。
├── [M] compose.yaml
│   影响：本地检索服务改为 ES，定义明确的持久化与就绪配置。
├── [M] pyproject.toml
│   影响：替换数据库客户端依赖；必要时登记 ES 集成测试标记。
├── [M] README.md
│   影响：更新启动要求、配置和主链路说明。
└── [M] uv.lock
    影响：锁定相容的 ES 客户端及其依赖。
~~~

## 7. 改动量与兼容性估算

| 类别 | 预计文件数 |
| --- | ---: |
| 生产与工具代码 | 23 |
| 测试（含前端测试） | 23 |
| 依赖、配置与部署 | 5 |
| 文档 | 9 |
| 合计 | 60 |

按操作计算：修改 52 个、重命名并改造 6 个、新增 2 个。重命名文件包含实质实现变更。

| 类别 | 预计新增 | 预计删除 |
| --- | ---: | ---: |
| 生产与工具代码 | 1,000～1,800 行 | 800～1,500 行 |
| 测试 | 800～1,500 行 | 600～1,300 行 |

这是量级估算。移动文件不按整份文件重复计算，锁文件和文档另计。主要逻辑集中在存储适配器、投影、同步和检索服务。

兼容性影响：

- 配置：Qdrant 环境变量及 CLI 参数需要迁移，ES 版本与认证方式需要固定。
- API/Chat：词项通道、候选排名、卡片和历史结果需要明确兼容处理。
- 前端：更新命中来源及合同；基础替换不要求重做搜索页面或播放器。
- MySQL：沿用 V2 任务与成果合同；旧发布三表代码的改接及数据库实际迁移需另行核对，不因采用 ES 新增发布表。
- 持久化数据：需要构建 ES 索引；旧数据保留、转换和回收须有具体执行步骤。
- 模型费用：可复用的既有向量优先迁移，重建索引不等于重新推理。
- 部署：代码完成后仍需独立安排服务切换与验收，不能把单元测试通过当作部署完成。

## 8. 执行时仍需确定的事项

这些是实现选择，不能当作用户已经决定的细节；ES 选型及非 `COMPLETE` 任务不可检索已确认，旧版持续服务要求已撤销。

| 事项 | 推荐处理 | 必须确定的阶段 |
| --- | --- | --- |
| ES 与客户端版本 | 选择并固定相容版本；查询当时的官方能力、限制和许可，默认复用 Engine RRF | 批次 0～1 |
| 部署与认证 | 优先明确本地 Compose 与已有实例的连接方式；决定 start.sh 是否仍支持原生 ES 懒启动，不悄悄改变运行前提 | 批次 0～1、4 |
| 多语言分析器 | 用中文/日文、人名、数字、短语及翻译样例验证；索引和查询共用相容配置 | 批次 1 |
| 词项命中粒度 | 明确 Chunk 文档中的事件关联与证据位置；需要同事件条件时禁止跨事件误配 | 批次 1～3 |
| 旧合同读取 | 确定 transcript 与 lexical 的新字段、兼容读取范围及历史持久化载体 | 批次 0、3 |
| 实际 Source 输入 | 先核对 refactor_2 和 Engine Schema；已有词项先接入，增强由实际产出驱动 | 批次 0、2 |
| 来源身份与状态检查 | 解析任务、Chunk、内容版本、稳定 ES 文档 ID 及过期回调约束；各通道共用 COMPLETE 门槛 | 批次 5 |
| 旧数据转换 | 核对实际库结构与可复用向量；无可靠来源的 V1→V2 字段不能自动伪造 | 迁移执行前 |
| 向量与索引参数 | 依据已有编码合同固定；不要仅凭维度相等判断向量空间相容 | 批次 1～2 |
| 残留索引清理 | 按任务/视频范围清理过期记录，不让残留记录绕过状态与版本检查 | 批次 5 |

这些缺口只阻挡依赖它们的批次。可以继续推进其他已经明确的适配器、测试和只读调查工作。

## 9. 验证与验收

### 9.1 必须覆盖的行为

| 场景 | 预期结果 |
| --- | --- |
| 只修改台词/问题等词项内容 | 版本摘要变化，触发索引更新 |
| 原话只出现在生成问题里 | 可作为问法线索，不能被返回为实际对白证据 |
| 人物和台词分属不同事件 | 不被误认为同一人物在同一事件说了该台词 |
| 中文/日文短语、人名、数字 | 全文与原话合同符合样例预期，来源和时间可回查 |
| 相同模型/输入的索引迁移 | 复用已有向量，不重复调用模型 |
| ES 部分写入失败或超时 | 状态保留并可重试，已成功项不造成重复实体 |
| 写入成功但尚不可查询 | 不提前标记 COMPLETE 或查询就绪 |
| 旧任务回调晚到 | 不能覆盖新意图、任务状态或新内容 |
| 并发同步与删除 | 不复活已删除记录，不删除其他视频/环境的数据 |
| 三路任一查询 | 始终使用同一可信范围和选定版本 |
| 不同增强数量的同一 Chunk | 同一路只贡献一次归并后的排名 |
| ES 候选与 MySQL 事实不匹配 | 明确报告一致性问题，不静默拼接不同版本证据 |
| 重解析/重建中或失败，ES 仍留旧值 | 选定任务非 COMPLETE 时不返回其数据；恢复并完成后再开放检索 |
| 查询期间状态或内容版本变化 | 不返回已失去检索资格的候选，不拼接不同版本的事实 |
| 历史 Chat 结果与评测报告 | 按已确定的兼容规则读取，来源解释不被篡改 |
| Qdrant 不可用或未安装客户端 | 已完成的 ES 运行与评测路径正常工作 |

### 9.2 测试方式

- Engine 纯函数测试：投影、来源定位、归并、RRF、原话与时间约束。
- 服务层测试：受控替身验证失败分支、重试、依赖装配与删除流程。
- 真实 ES 集成测试：mapping/analyzer、全文/短语、两路 KNN、过滤、逐项错误与可见性。
- MySQL 集成测试：沿用独立测试库，验证事务、写锁、过期回调及任务状态/内容版本竞争。
- 前端测试：重点检查词项来源解释、旧结果读取和证据播放范围。
- 小规模离线评测：分别记录词项、语义、视频及融合指标，比较召回、时间定位、延迟和成本。

ES 集成测试通过独立的 `SEEKINO_TEST_ELASTICSEARCH_URL`（可选 `SEEKINO_TEST_ELASTICSEARCH_API_KEY`）及唯一测试索引前缀隔离；未配置时跳过，不连接业务实例。

MySQL 测试沿用 SEEKINO_TEST_DATABASE_URL。测试只能清理自己创建的隔离数据。未配置环境时报告 skipped/未验证，不能自动使用开发或生产业务库。

优先运行受改动直接影响的测试，随后按具体风险扩大；不为文档改动启动数据库或调用模型。

### 9.3 实施后的核对命令示例

以下保留最初计划的核对入口。具体已执行的测试及仍不存在的目标文件，以第 13 节为准。

~~~bash
git status --short
git diff --check
rg -n -i 'qdrant|qdrant_client' server engine agent evals tests scripts web/src pyproject.toml uv.lock
.venv/bin/pytest tests/engine/test_video_retrieval.py tests/server/test_index_sync_repository.py tests/server/test_index_sync_service.py tests/server/test_video_retrieval_service.py
.venv/bin/pytest tests/server/test_elasticsearch_storage.py tests/server/test_elasticsearch_integration.py
.venv/bin/pytest tests/evals/test_elasticsearch_index.py tests/evals/test_recall_eval.py tests/evals/test_recall_eval_cli.py
~~~

运行命令前确认 ES 客户端依赖和测试环境已按授权准备。前端测试使用 web/package.json 中届时实际存在的命令。

Qdrant 字符串核对结果按用途判断：运行依赖、历史报告兼容、历史文档不是同一种情况。不要为了搜索零结果改写已执行的历史迁移脚本。

## 10. 数据迁移、上线与回收

代码接入、ES 索引重建、业务切换和旧数据清理是不同动作，应分别记录执行结果。

建议顺序：

1. 核对 MySQL 实际结构、已完成解析和向量元数据；历史迁移脚本存在不代表数据库已执行。
2. 准备目标 ES 实例和明确版本的索引合同，确认容量、认证与就绪检查。
3. 从正式事实和相容的已有向量构建目标索引，记录缺失和不可转换内容。
4. 通过固定样例核对三路候选、可信范围、证据时间、失败恢复和版本一致性。
5. 在明确的运行切换窗口内切换客户端/Worker，防止旧写者继续标记新目标的同步成功。
6. 按批次 5 验证选定 V2 任务的必需表示及 COMPLETE 条件；完成后才开放其检索。
7. 观察查询与对账结果，再按已确定的保留规则和清理授权处理残留索引及旧依赖数据。

不要仅比较总条数就宣布迁移完成；需要核对身份、来源版本、必需表示、维度和实际查询。

代码中移除 Qdrant 接入不等于可以删除现有 Qdrant 数据卷。数据清理和回滚能力依据实际切换方案与用户授权处理。

## 11. 当前工作区与本次交付记录

编写本文前，工作区已有以下未提交内容：

~~~text
 M agent/video_parse_v2/README.md
 M agent/video_parse_v2/__init__.py
 M engine/parsev2/README.md
?? agent/video_parse_v2/agent.py
?? agent/video_parse_v2/chunk_interval_listener.py
?? agent/video_parse_v2/chunk_scheduler.py
?? agent/video_parse_v2/chunk_understanding/
?? agent/video_parse_v2/errors.py
?? agent/video_parse_v2/input.py
?? agent/video_parse_v2/precompact/
?? agent/video_parse_v2/refine/
?? agent/video_parse_v2/state.py
?? agent/video_parse_v2/video_parse_prepare.py
?? tests/agent/video_parse_v2/
~~~

这是 2026-09-27 的快照，执行时必须重新检查，不能覆盖用户或其他会话的成果。文档中引用的 HEAD 也不包括这些修改。

目标文件在本次写入前已经存在，内容为空。本次仅填写 docs/design/refactor_3_es_qdrant.md，没有修改其他源码、测试、配置或 Git 忽略规则。

未运行业务测试，未启动 ES、安装依赖、调用模型、迁移数据库、重启服务、commit 或 push。

由于本文被 Git 忽略，交接时需显式携带此文件；本文不会自动随普通 Git 提交进入新工作区。

## 12. 可复制给下一 Agent 的开场指令

> 请先直接读取 docs/design/refactor_3_es_qdrant.md，并按第 1 节阅读 refactor_2、两份 TRD 和当前源码。本文及其他 design 文件被 Git 忽略，文件存在性应直接检查。
>
> 后端选择已确定为 MySQL + Elasticsearch，Qdrant 退出目标架构。ES 承接词项、Chunk 文本语义向量和 Chunk 视频向量；Engine 保持纯算法与合同，Server 负责 ES 适配、同步及任务就绪检查，Harness 继续消费 Engine Schema。
>
> 先核对 refactor_2 的实际完成状态和当前脏工作区，再根据本次执行授权推进对应批次。60 文件清单基于整理时的 V1 实现，如果 V2 已接入，应使用新的正式成果读取路径并调整本批修改树，避免重复建设旧链路。
>
> 保留已确认约束：人物直接使用人名；questions 先走词项；默认向量粒度为 Chunk；生成问题不能冒充原话；相邻上下文按 Chunk 关联；已有相容向量优先复用；模型调用沿用 Gateway 的用量、超时与取消管理。
>
> 特别检查三处：词项内容是否参与索引源版本、transcript/ASR 合同是否被错误复用于一般全文、ES 写入成功是否被误当作查询就绪。生产和评测的 Qdrant 接入、内存模式测试、CLI 与本地部署都要覆盖。
>
> 2026-09-28 已撤销“旧版继续服务，新版完整后切换”的要求。当前选定任务非 COMPLETE 时，即使 ES 仍有旧值也不可检索；各通道与 MySQL 事实须匹配同一任务及内容版本。沿两份 TRD 的任务状态机落实批次 5，不新增独立发布指针，不混用 generation、内容版本和解析次数。
>
> 遵循 seekino-change-gate 和 hypnos-code，尊重已有授权，不重复询问已经决定的后端与需求。先完成可独立验收的批次，保留其他会话修改；需要扩大范围时给出具体原因、修改树和影响。按第 9 节验证，明确区分代码完成、真实环境验证、数据切换与未完成事项。

## 13. 2026-09-27 实施状态与交接

> 历史实现记录：以下保留当时已有代码和验证结果。涉及独立 build、published map、冻结快照和整版发布的实现属于已撤销方案，仍待评审改接；不能因代码已存在而继续按旧方案接入生产。当前目标以开头决定及批次 5 为准。

本节记录当前工作区中的实际改动，不把第 5～6 节的目标清单当作已实现能力。开始实施时工作区另有大量 refactor_2 V2 未提交改动，已保留；它们仍处于分批实施中。生产启动和检索链路继续使用 V1/Qdrant，不能因为本节列有 ES 文件就认定系统已切换。

| 可独立验收的部分 | 当前代码状态 | 验证边界 |
| --- | --- | --- |
| Engine V2 人名与 Chunk 索引投影 | `engine/parsev2/schemas.py` 使用人名；新增 `engine/retrieval/index_projection.py`，从正式 Timeline 和已有增强生成带时间、来源、语言与角色的词项字段，以及 Chunk 语义输入；questions 只进入词项。`compute_index_projection_revision` 覆盖完整词项、语义文本和投影合同版本 | 纯函数测试通过。生产 `server/repository/index_sync.py::_compute_index_source_revision` 仍是 V1 摘要，**尚未包含该投影**，不能宣称生产词项变更会触发同步 |
| ES 存储适配器 | 新增 `server/storage/elasticsearch.py`：8.19 mapping、嵌套词项、两路向量、确定性 build/Chunk 文档身份、范围及版本过滤、逐项 bulk 错误、实时 inspect 与独立 search 可见性核对 | 受控替身测试通过；真实 ES 集成测试存在但本机没有独立测试 URL，尚未验证真实 analyzer、KNN、refresh 和逐项失败 |
| 词项融合与结果兼容合同 | Engine RRF 可接受独立 lexical 排名/权重，拒绝与 ASR 通道同时计票；`VideoChunkCandidate` / 前端合同新增可选 lexical 排名和分数；`SelectionReason.lexicalMatches` 保留 Timeline/增强来源，生成问题明确只作召回线索，不进入事实证据；旧 transcript 仍为 ASR | Engine、Server VO 透传、前端说明单测和类型检查通过；生产服务尚未调用词项通道，不能当作三路上线 |
| Chat 历史兼容 | 历史 trace 继续按 transcript=ASR 读取，新 trace 合同可用 lexical/semantic/visual；卡片能承载 lexical 排名；旧结果与新合同经 API/前端投影测试 | live Tool 仍调用 `search(transcript_weight=...)`，没有偷换成 ES 词项；新 live 参数、trace 写入与检索服务需同批切换 |
| 离线评测 ES 后端 | 旧 V1 双向量评测的 Qdrant 适配改为独立 ES 评测索引，保留 local 精确基线、旧报告读取和数据集隔离 | 离线评测单测通过；真实 ES 评测测试未运行；该入口尚不读取 V2 正式词项，不代表生产三路评测完成 |
| 客户端依赖 | `pyproject.toml` 与 `uv.lock` 增加 Elasticsearch Python 8.19 客户端 | 依赖安装和锁文件成功；Qdrant 仍是生产依赖，尚不可移除 |
| 可选运行入口 | `SEEKINO_ELASTICSEARCH_*` 配置、Bootstrap 客户端生命周期、显式 `init-es-index` CLI 与 Compose 的 ES profile 已加入；显式配置 ES 时本地启动脚本还会校验集群和索引映射 | 未改旧 Qdrant 默认服务；本机无 Docker，未启动 Compose；真实 ES 配置和服务启动未验证 |
| MySQL 构建/发布基础 | `server/model/index_publication.py`、`server/repository/index_publication.py` 与独立迁移：冻结每 Chunk 事实 JSON、时间、词项投影、两路向量、来源摘要和编码合同；desired/published 指针、租约/代际栅栏、整版 READY 条件发布、固定 build 回读、授权范围检查及删除 tombstone/全构建枚举 | 定向仓储/DDL 测试通过；正式 V2 成果组装 Writer、ES 真正查询就绪证明、删除/对账 Worker、生产查询、实际迁移及真实 MySQL 测试未接；这不是 V2 整版发布完成 |
| 向量复用判定 | `server/service/embedding_reuse.py` 只在可信来源、输入摘要和完整编码合同相容时返回已有向量；导入路径的视频摘要或缺元数据返回具名缺口 | 纯函数测试通过；普通 V1 向量元数据缺 provider/revision/fps 等，不能默认自动复用；尚未接正式成果 Writer，Worker 只消费已冻结向量，也未调用模型 |
| ES 构建 Worker | `server/service/es_index_build.py` 从冻结快照重建 ES 文档，租约/代际下批量写入，逐项搜索确认可见后置 READY；整版再次验证后按 MySQL fence 发布；超时部分成功、过期租约、删除交错和 READY 漂移可重试或阻断发布 | Worker/仓储定向 44 passed、3 skipped；真实 ES/MySQL 测试环境未配置；尚未有 V2 正式成果 Writer、生产调度及 tombstone 周期清理。发布候选前 N 长期失败可能使后续构建饥饿，接调度前需处理 |
| 固定版本 ES 查询核心 | `server/service/es_published_search.py` 按授权 Series 一次固定已发布 build map，三路搜索复用该 map；启用向量前检查编码合同，仅词项查询不调用 Gateway；按 `(video, build, Chunk)` 批量回查冻结事实并比较版本与摘要 | 仓储/服务定向 46 passed、1 skipped；返回不透明 `fact_snapshot` 和词项来源，尚未装配成正式 V2 Evidence，也未替换生产 API/Chat 的 V1 检索 |

早期基础批及相关旧链路的 Python 回归合跑为 **583 passed、4 skipped**；随后 Worker、固定版本查询、词项说明和本地部署入口又各有定向测试通过，最终合并回归数量需在本批收尾重新统计。`git diff --check`、`uv lock --check` 与前端 TypeScript typecheck 均通过。未配置 `SEEKINO_TEST_ELASTICSEARCH_URL`、`SEEKINO_ES_PUBLICATION_TEST_DATABASE_URL`，本机无 Docker；未运行真实 ES/MySQL 验证、业务数据迁移、服务重启、commit 或 push。Compose 只经静态解析与 profile 断言，没有实际启动。

### 13.1 旧发布方案的实现与缺口（历史记录，非当前接线指令）

以下保留旧方案当时的分析，包括其中的接线建议；旧版持续服务及独立发布的建议已撤销。
当前应按 COMPLETE 任务状态控制检索，后续源码修改另行评审；本节不表示已执行代码清理。

refactor_2 当前仅有 V2 任务 ORM、迁移草案和上传侧任务创建等批次 0～2 的成果；生产 Bootstrap 仍启动 V1 Parse Runtime。`chunk_parse_content` 在同一任务下是一行可变成果，其 `version` 是并发写入版本。Refine/Prefix 可继续更新该行，所以 `video_parse_task.id` 或解析 `version` 不能单独固定一次索引构建的证据。

新增的 `es_index_build`、`es_index_build_chunk`、`es_video_publication` 模型与仓库提供 ES 构建专用的不可变快照和 MySQL 指针基础。创建构建时核对当前 V2 Task、Chunk、content 版本及摘要，并将调用方提供的正式事实 JSON、Engine 投影、两路向量复制到新 build；`source_revision` 覆盖词项投影版本、向量值、编码合同、物理索引与业务身份。`publish` 只在全部 Chunk 标记 query READY 且 desired/fence 仍匹配时切换 published；新构建失败时旧 published 与快照继续保留，旧构建晚完成不能回滚。仓库能按固定 build 回读旧快照，并在 tombstone 后枚举所有构建供 ES 清理。

这还不是可运行的跨库整版业务发布：正式 V2 内容 JSON 的组装/读取 Writer 尚未落地，仓库把其事实快照当调用方提供的 JSON，并只能核对当前 content hash/version；其内容语义仍须由正式 V2 合同保证。新增 Worker 真正调用 ES `verify_ready` 后提供 `IndexSearchProof`，仓库只核证明中的目标 index、文档 ID 和来源版本，不自行联网证明可查询。迁移文件未执行；MySQL 真实事务竞争未验证。删除入口尚未在同事务调用 tombstone，ES 删除及重试/对账、旧版本保留与回收也未接线。

固定版本 ES 查询核心已可从 published map 做三路召回和冻结快照回查，但尚未替换生产 API/Chat；本地部署仅在显式配置 ES 时校验它，默认仍启动 Qdrant。随后还需将正式成果 Writer、生产同步/对账/重建、业务 Evidence、Chat、CLI 与部署切到新合同。当前 V1 `generation` 仍只负责同步回调防陈旧；解析次数、内容并发版本、同步 generation 和索引发布版本保持四种不同身份。已有向量只有在编码器、输入、维度、采样等合同均相容时才能复用；不能因为 ES 替换而重新付费调用模型。

现有检索还有独立的可用性门槛：`VideoRetrievalService` 通过 V1 `SeriesRecord.status == READY`、当前 Video 状态和旧 intent 判断 ready，Library 的 `readyVideoCount` 与前端 SearchPage 可选 Series 也沿用这一门槛。新解析期间这些值可能转为 INDEXING；即使旧构建仍在 ES，新查询也会被拒绝。生产切换必须把可检索性投影改为“有已发布可查询构建”，并在查询开始时固定 build map，用同一 map 做三路 ES 检索与不可变事实回查。

Chat 还有两处同类门槛：`AgentChatService._resolve_scope` 也拒绝非 READY Series；历史引用 `AgentRepository.load_reference_evidence(video_chunk_id)` 会回查当前 V1 Chunk。新版必须让一次 Agent Kick 的多次 Tool 检索固定同一发布快照，历史引用优先按原消息保存的构建身份回查；无法确定历史 build 的旧消息只能消费已存证据并严验授权与时间范围，不能拿最新事实替换旧引用。

一次查询的发布范围还必须携带 retrieval/编码合同。当前 Gateway Query 编码只产生一套模型/维度的向量，ES 两路向量查询则按 encoder ID 过滤。多视频已发布构建若编码合同不一致，必须事前明确拒绝或分组编码/检索，不能因过滤变成静默空命中；仅启用词项通道时不调用 Gateway。正常检索仍复用 Gateway 的费用、超时及取消管理。

V1 `video_chunk_embedding` 是按 Chunk 可覆盖的当前行，metadata 常见 `model/modality/dimensions/input_sha256`，缺 provider、编码器修订、视频 fps/MIME 等完整合同。原生解析的视频摘要是 canonical MP4 字节 SHA，import-parse 视频摘要却是路径、大小、时间等组合；不能把两者当同一种输入证明。向量迁移应先逐条判断相容性并报告无法证明的缺口，不静默调用模型。V2 `parse_config` 目前也没有 embedding 参数；新的构建配置快照须承载其编码合同。

**状态界限：**上述旧方案基础代码部分完成；真实环境验证未完成；数据切换未执行；V2 整版发布未实现且不再是当前目标。按 COMPLETE 任务状态的 ES 接线仍需实施和验证。Qdrant 仍服务现有生产路径，不能删除其运行依赖或数据。
