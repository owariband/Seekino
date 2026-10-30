# seekino Engine

Engine 是 seekino 的领域算法边界。视频解析算法位于 `parsev2/`，Agent 运行程序位于
`agent/video_parse_v2/`，检索算法位于 `rag/`。版本无关的媒体与模型能力从 `common/` 导入。
Engine 不拥有生产任务、租约、Artifact 或数据库；这些生命周期由 Server 负责，外部模型请求由 Gateway 实现。

## 媒体时间单位

视频时长、Chunk／事件边界、ASR 和采样帧位置统一使用整数毫秒，保留已有
`*_seconds`／`*Seconds` 字段名。例如 `8.008` 秒视频记录为 `8008`。配置中的
Chunk／Interval／重叠／分组时长仍为秒，FPS 仍为帧／秒；超时与租约单位不变。
FFprobe、ASR、FFmpeg 和播放器在接入边界完成秒与毫秒转换；页面展示小数秒。

本地 WhisperX 的字词对齐由 `common/transcription.py` 统一规范化为可选
`transcript[].words`：`text/start_ms/end_ms/score`，未知时间为一对 `null`。
这些时间相对当前音频窗口；Server 加偏移后作为原视频绝对毫秒保存至 ASR evidence。
句段旧字段名和非 WhisperX 响应保持兼容；字幕排版不在此时间归一层实现。
历史整数秒数据须由 Server 迁移并标识单位，不能直接按新语义读取。

## 代码边界

- `common/`：共享媒体探测、抽帧、ASR、背景文本、模型构造和用量记录。
- `parsev2/`：V2 解析领域 Schema、时间校验和规划；不拥有 Server 租约或数据库事务。
- `rag/`：索引表示、输入处理、召回、融合、证据核验和精筛；消费 V2 正式结果。

解析 Agent 位于 `agent/video_parse_v2/`，持久化及调度由 Server 负责。

RAG 能力按处理职责分开：

```text
rag/
├── contracts.py              # Engine／Agent／Server 共享的检索领域合同
├── encoding.py               # 编码合同、响应校验与 Query 缓存工具
├── query_pipeline.py         # 查询上下文校验与输入处理编排
├── indexing/
│   ├── projection.py         # 正式 Timeline → 来源词项与文字语义输入
│   └── pipeline.py           # Chunk Source → Index Plan → Document／Receipt
├── nlp/
│   ├── query.py              # 文字查询 → 编码信号及结果校验
│   └── transcript.py         # 通用 ASR cue 短语匹配及真实时间范围
├── vision/
│   ├── inputs.py             # 视频片段／有序帧输入、采样与实际 manifest
│   └── query.py              # 图片／GIF 查询 → 相容编码请求
└── retrieval/
    ├── contracts.py          # 查询、可信任务范围、候选与证据数据结构
    ├── pipeline.py           # 编码信号 → 召回请求 → Chunk 候选归并
    ├── fusion.py             # 共享排名与 RRF
    ├── evidence.py           # 同版正式事实、相邻来源、人物／对白／时序核验
    └── rerank.py             # 评分请求、整批评分校验、排序与最终 top_k
```

索引入口是 `plan_chunk_index`／`finalize_chunk_index`；查询先由
`plan_retrieval_query` 准备输入信号，再由 `build_recall_requests`、
`fuse_retrieval_candidates` 和 `verify_retrieval_evidence` 处理召回与真实来源。
调用方提供可信 COMPLETE 任务及正式事实，执行媒体读取、模型调用和 ES 查询；
Engine 只产出中立计划、校验返回值并执行策略。文字输入可以产生兼容的文字或视觉
Dense 信号，输入模态不与召回通道一一绑定。

课程纯文本索引可显式令 Chunk 的 `vision_encoder` / `visual_input_contract`、Event 的
`vision_encoder` / `capture` 成对为 None；此时不产生视觉编码计划，文档没有视觉向量，
Event capture frames 为空，并拒绝视觉查询通道。多模态合同仍要求完整视觉表示；缺失
向量不能用零向量或文本向量代替。模式属于冻结索引合同，变更需新物理索引，详见
[本地文本向量设计](../docs/design/03-product-reassessment/local-text-embedding-trd.md)。

`nlp/query.py` 处理文字查询，`vision/query.py` 处理图片／GIF 查询。图片和 GIF
保留媒体输入；GIF 保留完整帧序及相对时间。实际编码协议和产品接入仍需外部验收。
当前视觉命中可返回 Chunk 证据；图片／GIF 的可靠同源定位和帧级定位仍未完成。

### 可选精筛调用合同

精筛使用同一个 `candidate_budget`。Engine 先核验整个候选池，再提交合格候选，最后
按评分排序并取 `top_k`。候选因明确条件不符而被拒绝后，评分不能使其重新入选。

- `verify_candidate_evidence()` 返回整个合格池，不截断；旧
  `verify_retrieval_evidence()` 继续核验后取 `top_k`，保持 P4 调用行为。
- `plan_candidate_rerank(query_plan, candidates, facts, scorer=..., contexts=...,
  visual_inputs=...)` 完成核验并准备评分请求。`scorer=None` 关闭精筛；没有合格候选时
  也不请求评分。正式事实的任务、版本或范围不一致时仍报一致性错误。
- 宿主执行 `plan.request`，提供模型标识与 `(video_chunk_id, score)` 序列。
  `accept_rerank_result(plan.request, model=..., scores=...)` 严格校验返回值；每个合格
  候选必须有且只有一个有限数值，模型也须相符。评分越大越靠前，不设分数阈值。
- `finalize_candidate_rerank(plan, result)` 消费评分，再取最终 `top_k`；同分保持 RRF
  顺序。请求关闭时传 `None`，直接使用 RRF。模型调用失败、超时，或严格接受步骤
  抛出 `ValueError` 时，宿主传 `None`；也可传原始 `RerankResult` 让此处校验。
  缺失或非法评分会整批弃用，记录 warning 并回到 RRF。禁止只采用部分评分。

`RerankScorerContract` 声明模型、协议依据、查询模态及候选输入类型。声明不能证明实际
模型支持该组合，Gateway 必须另行验证。文字候选输入只来自正式 Timeline，不包含
生成的 questions、keywords 或 summary。视觉候选输入保留完整视频片段或有序帧
manifest：视频范围须等于 Chunk；帧使用原视频绝对时间，须位于 Chunk 范围内，且
结构化采样须符合自身时间计划。Engine 不读取媒体字节，也不从评分生成新事件事实。
媒体与当前正式任务的关联、模型执行、超时、取消和成本统计由宿主负责。

### 版本规则与 P5／P6／P7 交接

索引文档、receipt、召回和正式事实共同沿用 `parse_task_id`、Chunk ID、
`content_version` 与 `index_generation`。正式内容或 enrichments 改变时，宿主提高
`content_version`；同一 generation 的投影和编码配置保持固定，规则或模型改变时由
宿主提高 `index_generation` 并重建。Engine 不生成这些版本号。

Chunk embedding 跟随外层版本，不维护独立的输入摘要／模型摘要缓存身份。
编码结果绑定当前 Plan 的请求对象，防止不同执行的结果混用；这不产生新的持久化 ID。
Query 编码的相容性与可选缓存、实际媒体字节／帧摘要仍各自保留。Plan 内的投影摘要
只检查规划后内容是否被改动，不作为 ES 文档的版本对齐字段。

Engine 的中立调用合同与本轮外围改接状态如下；部署仍需单独签收：

| 阶段 | Engine 交付 | 外部剩余事项 |
| --- | --- | --- |
| P5 | 可选精筛计划、完整评分校验、排序、旁路及失败回退；Server 已接文字评分，Gateway 已执行真实结构化评分 | 图片／GIF 评分或帧级增强须另选并证明实际协议 |
| P6 | Engine 合同样例；Server／ES／Gateway 改接；独立 ES、MySQL、真实模型与小样本端到端验证 | 当前业务配置相容性、真实标注集与生产质量／延迟门槛尚未签收 |
| P7 | 当前字段、版本规则和调用合同说明 | 合同签收后由发布负责人重建／迁移、配置切换、数据对账与恢复验证 |

Server 已改为按任务／Chunk／`content_version`／`index_generation` 恢复编码结果，
不恢复已删除的摘要复用接口。ES 消费 `RecallRequest` 执行 BM25、文字向量、视觉向量、
原生 Sparse 四路召回；查询固定可信 COMPLETE 任务，并回读整个候选池的同版正式
内容，再校验证据、评分，最后取 `top_k`。向量命中只证明 Chunk 范围，不能选择其中
第一条视觉事件当作命中证据。文字评分失败或整批评分无效时 warning 并回到 RRF；
宿主预算、租约或取消控制仍向上交付。

`candidate_budget` 默认按本次启用通道数 × `channel_top_k` 计算，并至少覆盖请求
`top_k`；显式配置预算时也至少覆盖请求 `top_k`。`sparse_weight` 默认 0，
`rerank_enabled` 默认 false。
HTTP 搜索与聊天共用当前 `agent_runtime` 的 topK 配置：下限为 1，上限为
`maximum_top_k`；HTTP 未传 topK 时使用 `default_top_k`。
开启 Sparse 时，当前任务每个 Chunk 必须已有合法 Sparse 结果；空权重有效，缺失
结果会在模型调用前拒绝。启用评分使用已有 DeepSeek Agent 模型，不自动换模型。
普通 Server 文字搜索不会自动把自然语言转换为严格事件／时间条件；Engine 已有的
条件接口仍由调用方显式提供。HTTP 图片／GIF 输入不属于本批新增接口。

2026-10-08 小规模验收结果（观察值，不设生产门槛）：

| 验证 | 结果与实际边界 |
| --- | --- |
| 18 个直接相关测试文件 | 729 passed；1 个既有 Starlette 弃用 warning |
| 独立 ES 8.19.22 | 7 passed：四路召回、原生 Sparse 点积、任务／generation 隔离、外部版本、部分写入／删除恢复；这些测试的事实库和编码边界为 SQLite／确定性样本 |
| 独立 MySQL 8.4.11 | V2 并发版本分配、Prefix 原子失效、重试／Worker 租约拦截与提交竞态子集 10 passed |
| 真实 Gateway 协议 | 文字／PNG／视频与 Sparse 实际调用成功；251215 返回 1024 维向量，视频原生请求 FPS=1；DeepSeek 返回两候选完整评分，usage 记录实际响应模型 |
| MySQL → 编码 → ES → 检索 → 评分 | 20 秒可生成的红／蓝视频，正式内容为手工指定的两条事件；文字查询 2/2、准备好的图片查询 1/1 选中预期 Chunk，图片证据仍为 Chunk 粒度 |
| 同一真实链路的恢复 | 部分 ES 写入并丢失确认后重试，6 次编码调用前后不变；重试约 1054 ms，正式任务经宿主汇总为 COMPLETE |
| 小样本查询耗时 | 两次文字查询约 3519／1203 ms（含评分）；准备好的图片经 Engine 接口约 187 ms，未经过新增 HTTP 图片入口 |

成功端到端样本共 13 次模型调用、3752 tokens。按实测文字／图片／缓存／输出用量和
官方公开单价估算约 ¥0.0060–0.0073（DeepSeek 闲时／高峰区间），不是账单金额。
单价来源：[方舟模型价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh)、
[DeepSeek 模型价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
原始记录与可执行样本脚本分别位于
`/private/tmp/seekino-p6-e2e-stk66klm/result.json`、
`/private/tmp/seekino-p6-e2e-stk66klm/acceptance.py`；脚本要求独立测试 MySQL／ES 与
本地配置的模型密钥，合成视频由 ffmpeg 生成，正式事件不声称来自真实解析模型。

当前业务选择值为 endpoint 别名，实际响应为 `doubao-embedding-vision-250615`；
严格模型名称校验不能把二者直接当作相同。250615 也没有已证明的原生指定 FPS
协议，因此当前采样合同会明确拒绝它。验收单独使用 `251215`，未改业务模型配置。
GIF 有序帧／相对时间输入与帧级同源确认尚无本批已证明的外部编码协议。

已有物理索引须按当前 `seekino-video-chunk-es-5` mapping 新建／重建；旧 mapping 会被
明确拒绝，不会自动迁移。配置或规则改变由发布方提高现有 generation，分别核对
写成功、查询就绪和业务可搜索。本批只使用独立临时实例与测试索引，未切换数据或
发布；历史计划中的旧字段和旧路径说明不能覆盖这里的当前合同。

通用 ASR cue 短语匹配位于 `rag/nlp/transcript.py`。排名与 RRF 由
`rag/retrieval/fusion.py` 提供。存储读写和正式事实回查由 Server 实现，模型执行由 Gateway 实现。
旧 V1 解析、CLI、召回评测执行器及 Qwen3-VL 的 V1 数据准备路径已删除；历史评测问题文件保留。
