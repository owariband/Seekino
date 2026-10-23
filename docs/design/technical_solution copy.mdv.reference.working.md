# 零、M1开发目标

关键边界：单用户可靠的数据传递、视频解析
支持功能：
1. 视频按照series进行分组，即系列
2. 支持限定series进行retrieval（可以拼多个series）
3. parse视频的时候，底层复用videoRAG，but我们要增加一个额外功能：
    允许有一个Watcher监管者Agent审核当前Agent Parse是否带了extra信息
    这个extra信息包含：
    人物信息介绍补充（让识别Agent对齐人物，可能是Watcher自己亲自下场改文字schema，例如识别Agent说棕发少女正在给蓝发少女下跪，Watcher自己根据立绘信息判定为是soyo在给saki下跪，但是关于音色其实也应该识别的，但是我不确定现在的agent是否能判别音色）
    因此让Parse Agent生成的内容保证符合extra 信息的补充
    做到背景知识相符合
    （事件发生的地方的识别可能还有点困难）
    并且再来个Searcher Agent联网搜索补充相应画面的一些热梗八卦


4. retrival后返回相应的提示词（agent问答助手式补充返回的有哪些，定位在第几集多少分～多少分） + 对应的压缩版视频（截回的视频尽可能短）
5. 一次性上传多集，允许后台持续解析，并展示upload 和 parse的进度
    （需要queue，但是现在容量没有大到需要用mq，可以暂时不用）
    每个video的status和进度都需要同步展示给user（状态定义）
6. 后台被异常关闭中断了传输，支持打开后恢复上传，并且过去的上传解析状态能够得以保留恢复展示

   
做到最小型MVP：
  虽然第一阶段不做多用户，但仍然要有一个最小产品壳：

  Web 前端
     ↓
  AnimeVAG API
     ↓
  本地 Job Worker
     ↓
  VideoRAG Adapter
     ↓
  本地视频 / 索引 / 元数据


# 一、示例流程

1. user创建一个series，计划在里面里面存放相应的所有视频，例如：mygo series，并补充了extra info
2. user对mygo series upload 磁盘本地的13个视频
3. 上传列表里，展示各个series（可展开）
4. 展开mygo series后可以看到13个视频上传情况，一个list有筛选选项
5. 可以按状态筛选（待解析，待上传，解析完成，上传完成，解析中，上传中，，）
6. 对于上传并解析完毕的剧集
7. 我们来到chat，选中相应series scope，和agent对话问“惊世一跪是什么？”
8. agent解释惊世一跪是什么，并返回相应的视频压缩短切片以及具体的时间定位


# 二、Agent Parse Workflow

三大Agent：
1. Parse Agent（主要对VideoChunk做text schema解析的agent，伴有背景信息辅助，每次解析前都要检查信息识别信息后再进行parse，并且每次解析能拿到之前剧集剧情的compact）
2. Watcher Agent（检查Parse是否有遗漏信息，是否质量过关——过关则解析通过，不过关重新让ParseAgent解析该片段内容；负责compact已经解析过的内容辅助Parse Agent生成更好更准确的内容质量）
3. Searcher Agent（八卦Agent，主要负责联网搜索相关片段有什么网络热梗内容，能拿到过往的所有compact剧集信息）

> compact剧集信息：按照剧集发展存的，每一个VideoChunk解析完后都要往后增加相应信息，每一个VideoChunk拿到的compact内容是不一样且确定持久化的
> 或者说其实就是之前所有解析结果的浓缩版本
> 本集之外的浓缩，本集的可以全保留——防止context撑爆的现象

## 2.1 M1实际落地
（早期需要缩减，而且可能是冗余设计）

  目前的想法是：

  VideoChunk 1 解析
  → 生成 compact
  → VideoChunk 2 携带 compact 解析
  → 更新 compact
  → VideoChunk 3 携带新 compact 解析

  这会形成严格的串行依赖：

  S1 → S2 → S3 → S4 → ... → Sn

  如果“本集信息可以全保留”，上下文还会不断增长。若不设置严格上限，总 Token 成本很容易接近二次增长。

  你提出的“精确性和效率是否只能二选一”，答案是：不需要二选一，可以使用并行 Map + 有序 Reduce + 定向修复。

  推荐工作流：

  阶段 A：并行基础解析
      ├─ ASR
      ├─ 抽帧
      ├─ 初步 Caption
      └─ 视频特征
               ↓
  阶段 B：并行 VideoChunkParser
      每个 VideoChunk 获得：
      Character Bible
      + 相邻片段窗口
      + 本集基本元信息
               ↓
  阶段 C：按时间顺序 Event Stitcher
      合并人物、行为、因果和事件边界
               ↓
  阶段 D：QualityGate
      找出身份冲突、低置信度和事件断裂片段
               ↓
  阶段 E：只重新解析异常片段
               ↓
  阶段 F：构建并发布索引

  这意味着绝大多数昂贵的视觉模型调用可以并行，只有较便宜的文本归并阶段按时间有序执行。

  Compact 也不应该保存“前面全部原文”，而应保存成有界状态：

  EpisodeStateSnapshot
  ├─ 当前出场人物
  ├─ 最近发生的关键事件
  ├─ 人物关系状态
  ├─ 未闭合事件
  └─ 最近 K 个事件引用

  例如限制在 1,000～2,000 Token，而不是随 VideoChunk 数量无限增长。

  跨集上下文则分成：

  SeriesBible
      稳定背景：人物、别名、外观、基础关系

  PreviousEpisodeSummary
      上一集或相关剧集的结构化摘要

  LocalNeighborContext
      当前 VideoChunk 前后各 1～2 个片段

  不需要每个片段都拿到前面十二集的完整 compact。




# 三、Agent Chat-Retrieve Workflow

1. Chat Agent负责调用RAG工具的聊天者
2. Evaluator Agent负责测评ChatAgent调用的准确性和提示词的query正确性，具有当前series的所有剧集大概信息，可以辅助ChatAgent正确地使用query RAG


# 四、总编排


# 五、问题
1. 出于效率按理来说应该对全部的videoChunk做并行化parse
    但是出于语义精准性我们又希望能够获取前置知识让parse更加精确
    如何平衡这两个点？

2. 如果全并行化处理，则compact注定不好维护甚至没有什么用
    除非单开一个阶段和Agent跑compact，但是那意味着额外又做了一轮有序性parse，是否效率过于低了？

3. 或者说，时间和精确性没办法共存，为了精确性只能牺牲时间，这是一种权衡？


# 六、VideoChunk 返回与字段储存设计

一些很显然的页面显示或者作为文件必带的字段就懒得加上了，AI来补

## 6.1 retrieve 联合召回的信息综合

VideoChunk:
string videoChunkId
string videoId
Time start_time,end_time
VideoChunkInfo videoChunkInfo

VideoChunkParseResult:
string videoChunkId
string all_time_event
string self_compact
string context_compact

VideoChunkInfo:
// Server 对 VideoChunkParseResult 的持久化事实
string videoChunkId
string all_time_event
string self_compact
string context_compact

VideoChunkRetrievalDocument:
// 与 VideoChunk 一对一，只是检索文本投影，不产生独立切块身份
string videoChunkId
string retrievalText

VideoChunkEmbedding:
string videoChunkId
// 对当前 VideoChunk 的结构化事件、selfCompact 和对白生成
vector textVector
// 直接对当前原始 VideoChunk 生成
vector visionVector
// entities_vdb + chunk_entity_relation_graph 属于 VideoRAG 上游能力，M1 暂不引入

Video:
string videoId
int orderId（用户上传文件的顺序决定了解析的语义顺序！）
string seriesId
VideoChunk[] chunks
VideoStatus status（例如，上传中，已上传，解析中，已解析，解析完毕的时候才是可用状态）
other extra metadata for show
## 6.2 视频上传

Series：
string seriesId
optional ImportantExtraData: need to provide 主要人物背景、人设图等关键信息
other extra metadata for show,eg: series_name,description,
（一个可自定义无限拓展的json schema字段，不固定）

//我们没有必要保留上传记录，有必要吗？感觉没有必要专门保留Batch
VideoUploadTask:
string seriesId
Video[] upload_video_list

//为了支持展示上传的总进度，需要引入更多的model
UploadSession:
//当前上传会话的id
string uploadSessionId
videoId
preferredSegmentSizeBytes
totalSizeBytes
totalSegments
status
diff kinds type of time




//为了实现中断恢复会话上传，需要设计上传分片
Segment
string uploadSessionId
int segmentNumber
int actualSegmentSizeBytes
//用于校验传递完整性
string checksum
Enum status




# 七、VideoRAG的改造
根据调查VideoRAG用的还是旧版本的没有模型支持视频识别的case，它可以做到对画面的图像语义识别（如人物动作）
同时DoVideoAI（github可搜到）也只是对视频竟然逐个抽帧的OCR + ASR识别，聚合文字做RAG



我们所做的就是把VideoRAG给现代化


我们的链路：

1. prepare_inference_proxy(originVideo)
  保留原视频，生成推理压缩版
       ↓
2. split_video(inferenceProxy)
  生成 1 分钟规范 VideoChunk
  同时为模型构造带前后余量的 analysisWindow
       ↓
3. video_to_text(analysisWindow)
  原始短视频 + SeriesBible + contextBeforeCompact
  → 现代视频模型
  → 结构化人物、动作、对白、情绪和相对时间
       ↓
4. merge_segment_information()
  校验 Schema
  相对时间 → 本集绝对时间
  事件去重与人物 ID 对齐
  生成 all_time_event
  生成 self_compact / context_compact
  保存 VideoChunkInfo
       ↓
4A. build_video_chunk_retrieval_document()
  all_time_event + self_compact + transcript
  → Text Embedding
  → textVector
       ↓
4B. video_segment_feature_vdb.upsert()
  规范 VideoChunk 视频
  → ImageBind
  → visionVector
       ↓
5. upsert_indexes_and_checkpoint()
  textVector 与 visionVector 均使用 videoChunkId
  保存索引、Checkpoint 和解析进度



# 八、系统架构

1. web front
2. Server——负责视频管理传递数据持久化等
3. RAG算法层

持久化层选用：MySQL+ Qdrant


## 8.1 目前已确定的架构component
- “MySQL 与 Qdrant 一致性”是一项系统保证，不直接作为组件名。
- 实现这项保证的组件，可以叫做 Index Lifecycle Service（索引生命周期组件）。
- DB Queue 整套能力则可以叫做 Durable Job Runtime（可靠任务运行时）。

它们的关系应该是：

Durable Job Runtime
├─ JobStore / MySQL DB Queue
├─ Dispatcher
├─ Lease / Heartbeat / Reaper
├─ Retry / Idempotency
└─ Worker Executor
          │ 调度
          ▼
Index Lifecycle Service
├─ IndexSyncJob / Transactional Outbox
├─ IndexSyncWorker
├─ IndexPublisher
├─ IndexReconciler
└─ IndexCleanupWorker
          │
          ├─ MySQL：权威事实源
          └─ Qdrant：派生检索索引

在系统架构图中，不需要把 Outbox、Reconciler、Publisher 全画成顶层方框；顶层画：

Job Runtime
Index Lifecycle Service
MySQL
Qdrant

在详细组件图或建库时序图中，再展开内部模块。这比直接画一个“数据一致性组件”准确，因为一致性是它承担的职责，而 Index Lifecycle Service 才是真正的软件边界。

## 8.2 待确定的component
（以下架构名字并非真实采纳的名称， ##8.3里面透露的图片结论才是最后真正的架构图）
1. Media Asset & Ingestion Service（之前已讨论）
   - Series / Video 资产管理
   - 分片上传、合并校验与断点续传
   - Artifact 生命周期、播放与 Clip

2. Video Understanding Pipeline（之前已讨论）
   - 视频切分与现代视频模型理解
   - VideoChunk 事件归并和上下文状态
   - Text / Vision Embedding 生成

3. Retrieval & Evidence Service（先简单用一个react agent实现）
   - Query Rewrite 与 Series Scope
   - Active IndexVersion 查询
   - Text / Vision 联合召回与重排
   - VideoChunkEvidence Hydrate、时间证据和播放跳转
    & Conversation Service
    ├─ 会话创建、重命名、列表与找回
    ├─ 用户消息和助手消息持久化
    ├─ 当前 Series Scope
    ├─ 最近 K 轮上下文
    ├─ ConversationSummary 有界摘要
    ├─ 追问补全与独立 Query Rewrite
    ├─ 调用 Scene Retrieval Service
    ├─ 基于证据生成回答
    ├─ 保存 VideoChunkEvidence / Citation
    └─ SSE 流式返回与断线恢复


所以最准确的结论是：

> 如果只看 Server，缺的是“视频资产上传”和“检索证据返回”；算法解析属于 Engine。
> 如果看整个系统，待细化的核心 Component 一共有三个：上传、理解、检索。

## 8.3 串起所有component的系统架构图（前端架构不考虑）

- 可交互系统架构图 (docs/design/diagram/anivag-system-components.html)
- 架构图原始规格 (docs/design/diagram/anivag-system-components.architecture.json)



# 九、MySQL和Qdrant的数据一致性链保证
真正可靠的组合
> MySQL 事务 + 持久化 IndexSyncJob/Outbox + Qdrant 幂等 Upsert + 版本化索引发布 + 定时对账修复。

Qdrant 的基本模型：

Collection
└─ Point
    ├─ pointId
    ├─ Named Vectors
    │  ├─ text
    │  └─ vision
    └─ Payload
      ├─ workspaceId
      ├─ seriesId
      ├─ videoId
      ├─ indexVersion
      └─ sourceRevision


## 9.1 Qdrant Collection
创建一个 VideoChunk Collection 即可
每个 Point 对应一个 VideoChunk，并通过 named vectors 保存 text、vision 两种向量
Payload 只包含定位 `videoChunkId` 所需的必要 metadata

> IMPORTANT！:跨语言能力！
> ASR是日文的前提下，我们的query先默认是中文，对ASR的召回能力会很差
> 所以ASR获取的内容需要交给agent归一化中文或者英文，统一化存储
> 原版台词也需要持久化存储备份
>
## 9.2 为了数据一致性要做的努力

1. payload filter——只允许recall当前uesr workspaceId（还没出多user拓展呢，，预案）、seriesId 的 VideoChunk
2. upsert Qdrant VideoChunk Collection的时候，对于Point要有唯一标识UUID，防止随机ID造成一个片段可能多次创建到处都是它生的孩子的结果
3. wait=true + WAL：保证一次 Qdrant 写入自身的可靠性
4. Collection Alias：保证整套向量索引升级时的无缝切换
5. MySQL也要存vector（虽然mysql用不了召回），永远是权威数据事实源，并且MySQL操作成功后才能去幂等更新Qdrant
6. 


# 成本调研

有，而且你先要把三个很容易混淆的概念拆开：

| 类型               | 输入 → 输出                  | 主要用途              |
| ---------------- | ------------------------ | ----------------- |
| 文本 Embedding     | 文本 → 向量                  | 按语义检索字幕、摘要、剧情描述   |
| 视觉/多模态 Embedding | 图片/视频 → 向量               | 文搜图、文搜视频、画面相似度检索  |
| 视觉语言模型 VLM       | 图片/视频 + Prompt → 文本/JSON | 识别人、动作、场景，生成结构化描述 |

## 视觉向量和文本向量有什么区别？

本质上输出都是一串浮点数，例如 1024 维：

$$
x=[0.12,-0.37,\ldots]
$$

区别主要在编码对象：

* 文本向量从 token、词义和上下文中提取语义。
* 图像向量从图像 patch 中提取人物、物体、构图、动作等特征。
* 视频向量还需要对多个帧进行抽样，并建模时间变化。

最重要的是：**普通文本模型的向量和普通视觉模型的向量不能直接比较**。必须使用经过图文对齐训练的多模态 Embedding，例如 CLIP、Qwen3-VL-Embedding，才能做到：

$$
\text{cosine}(\text{“一个人跪下”},\text{视频片段向量})
$$

Qwen3-VL-Embedding 就把文本、图片、视频映射进统一向量空间，明确支持 text-video matching 和视频检索。([help.aliyun.com][1])

但视觉向量是一种高度压缩的检索表示，它通常不会告诉你：

* 谁为什么跪下；
* 这一跪在剧情里意味着什么；
* 前后人物关系是什么；
* “惊世一跪”这个梗具体指什么。

这些依然需要 VLM 生成描述，或者结合 ASR、字幕和全局剧情信息。

## 视觉向量便宜吗？

**API 单价很便宜，但视频因为帧多，最终成本取决于抽帧策略。**

例如现在阿里云的 `qwen3-vl-embedding`：

* 图片/视频：1.8 元/百万输入 Token；
* 文本：0.7 元/百万输入 Token；
* 支持 256～2560 维向量；
* 单个视频文件限制 50MB。([help.aliyun.com][1])

豆包公开的 `Doubao-embedding-vision` 也是：

* 文本约 0.7 元/百万 Token；
* 图片约 1.8 元/百万 Token。([volcengine.com][2])

粗略理解：假设一个关键帧折合 1000 个视觉 Token：

* 单帧约 `0.0018 元`；
* 1000 个关键帧约 `1.8 元`。

所以做 AnimeVideoRAG Demo，Embedding 成本基本不用担心。真正可能更贵的是：

1. 对大量帧调用 VLM 生成详细描述；
2. 长视频高频抽帧；
3. 输出大量剧情分析文本；
4. 视频存储、转码与带宽。

此外可以本地部署 CLIP、SigLIP、Qwen3-VL-Embedding-2B；没有 API 费用，但会转变成 GPU 时间和工程成本。对你现在的 Demo，我更建议先调 API。

## 目前哪些厂商支持视频理解？

以 2026-09-10 的公开接口为准：

### 豆包：原生支持

火山方舟已经提供视频理解，能够直接分析视频中的物体、动作逻辑等；其长视频理解服务基于豆包模型做多模态解析。([volcengine.com][3])

适合直接做：

* 视频摘要；
* 分段描述；
* 人物行为识别；
* 按时间轴输出结构化事件。

注意别把豆包的“视频生成模型 Seedance”和“视频理解模型”混为一谈，它们是两件事。

### 通义千问：目前非常适合你的项目

Qwen 系列直接支持视频文件输入，不只是传图片：

* 新型号最长支持 1～2 小时视频；
* 支持 MP4、AVI、MKV、MOV 等格式；
* 可以控制抽帧和图像像素；
* 同时有 `Qwen3-VL-Embedding` 和 `Qwen3-VL-Reranker`。

阿里云文档明确说明，视频会先被抽帧，然后所有帧转换为视觉 Token。([help.aliyun.com][4])

它现在几乎能够提供你需要的一整套：

```text
Qwen-VL：视频 → 结构化描述
Qwen3-VL-Embedding：文本/视频 → 统一向量
Qwen3-VL-Reranker：Query 与候选片段精排
```

### DeepSeek：能看图片，但目前没有原生视频接口

当前 `deepseek-flash` 已经支持视觉输入，而且价格很便宜；但官方接口只列出了 JPEG、PNG、GIF、WebP，**没有直接上传 MP4 的原生视频理解能力**。([DeepSeek API Docs][5])

你可以自己：

```text
视频 → 抽帧 → 多张图片 → DeepSeek Flash
音轨 → ASR
两部分结果再合并
```

它单次最多支持 600 张图，单张图最多折算为 1024 Token，因此可以分析短片段，但：

* 不会自动处理音轨；
* 时间顺序和连续动作理解需要你自己组织；
* 不如原生视频模型省工程量。

所以 DeepSeek 可以做你的廉价“帧描述器”，但不是现阶段最适合作为主视频解析器的模型。

### 其他选择

* Gemini：原生支持视频理解，Gemini 2.5 及以后单次最多输入 10 个视频，长视频能力成熟。([ai.google.dev][6])
* 智谱 GLM：目前也有支持图片、视频、文本输入的 GLM-5V/多模态模型。([平台介绍][7])

## 对 AnimeVideoRAG 的直接建议

你第一版其实**不需要把视觉向量当主角**。最稳的检索结构是：

```text
视频
├── ASR 字幕：负责台词、专有名词、原句定位
├── VLM 描述：负责人物、动作、场景、画面事件
└── 多模态向量：补充纯视觉召回
```

每个 VideoChunk 保存：

```text
start/end timestamp
ASR text
OCR text
visual description
characters
actions/events
story context
text embedding
visual/video embedding
```

查询“哪里出现了真是高高在上啊”时：

* ASR/BM25 精确召回原台词；
* 文本向量处理语义近似表达。

查询“惊世一跪是指什么”时：

* 文本向量召回带有“跪下、请求、转折”的结构化描述；
* 视频向量补召回真正出现跪姿的画面；
* VLM/LLM 根据前后片段和剧情脉络解释含义。

因此，**VLM 负责把视频变成可以理解的知识，Embedding 只负责快速找到它**。对你的第一版，我会优先选 Qwen-VL + ASR + 文本混合检索，视觉 Embedding 放在第二阶段加入。

[1]: https://help.aliyun.com/zh/model-studio/qwen3-vl-embedding?utm_source=chatgpt.com "qwen3-vl-embedding 模型信息-大模型服务平台百炼(Model Studio)-阿里云帮助中心"
[2]: https://www.volcengine.com/product/doubao?utm_source=chatgpt.com "https://www.volcengine.com/product/doubao"
[3]: https://www.volcengine.com/docs/82379/1895586?utm_source=chatgpt.com "视频理解--火山方舟"
[4]: https://help.aliyun.com/zh/model-studio/vision?utm_source=chatgpt.com "如何使用Qwen3.6模型实现视觉理解-大模型服务平台百炼(Model Studio)-阿里云帮助中心"
[5]: https://api-docs.deepseek.com/guides/vision?utm_source=chatgpt.com "Vision"
[6]: https://ai.google.dev/gemini-api/docs/video-understanding?utm_source=chatgpt.com "Video understanding  |  Gemini API  |  Google AI for Developers"
[7]: https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5v-turbo?utm_source=chatgpt.com "GLM-5V-Turbo - 智谱AI开放文档"

