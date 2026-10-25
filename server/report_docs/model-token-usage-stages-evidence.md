# 本地 Token 统计：阶段证据与 check-out

更新日期：2026-10-09。原源码改造基准：`main@c4b4cbc`，含用户已有未提交修改；本次隔离联调源码见文末提交核对。
阶段结果：**U01–U06 本地 Token 统计已完成；本轮双 chunk 统计、系统通路及整 chunk 播放通过，逐事件时间精度未通过，无正在执行的统计阶段**。
本轮用户明确授权按 handoff 的 U02–U05 修改树连续实施代码、配置、测试和文档，沿用已确认八列 Schema。
U01 设计与修改树已被本轮执行指令确认；不再重复询问 Schema 或源码开工权限。
U02–U05 源码及离线直接验证已完成；用户追加“你全都去改就行了”后，本 Agent 已修复此前四项既有失败。
随后用户明确“直接开发吧”，本批补齐 U04 HTTP 跨层验收；统计测试 **41 passed**。
2026-10-08 十二文件 Server/Gateway 回归 **429 passed / 28 skipped，零失败、零筛除**；Web 沿用此前 42 passed 与 typecheck 证据。
2026-10-09 用户明确“然后可以进行联调测试吧”，授权本次隔离环境建表、服务运行与模型联调；此前两类测试适配 TODO 已关闭。
U02 八列 MySQL 历史验收为 **139 passed / 2 skipped**；endpoint 修复九生产 / 六测试的 **686 passed** 与三 callback **10,403 Token** 保留为先前局部证据。
本轮以 c8c5d2c + 开始时全部 dirty 冻结 **538 tracked** 文件，包含 common；运行中追加五生产/五测试的十个必要补丁，同期 ROOT V1/Qdrant 清理全部保留且不纳入真实服务。
默认90132ms双chunk解析于05:57:33UTC在补修前靠原重试/Seed完成；保留同一Parse数据，在修复schema适配/V2媒体/完整意图后冷重启验证Chat，不声称17条Parse均发生在最终十补丁版。
最终SQL/API/UI为 **99 calls / known 1,204,853 / missing 2**，公开input/output/total均null，11模型/4run；七failed的已知175,035与两个schema400未知全部保留。独立`audit-multichunk-final.json` passed/fullFlowPassed=true，仅指统计/系统通路、owner/预算/两receipt/ES、最新Chat双段命中/播放和重放。
**时间覆盖只证明结构、全局毫秒范围与整chunk播放边界；第二chunk当前version2的Interval updates在60004–60030ms（26ms）承载多句对白，逐事件精度未通过。** Chat/DTO保留原值，旧内容覆写/ASR原证未持久化，无法追认混入阶段；本轮不凭启发式乘1000或改历史SQL。默认V2未登记keyframes，图片覆盖unobserved。
本轮生产+106/-10、测试+264/-11；初始冻版23文件894passed/1skip，唯一MySQL timing另库1passed0skip，补丁相关与ROOT离线验证分开记载。前轮532+4的49/357825、endpoint686/三callback10403和更早失败均为历史，保留独立账本与资格；无新增commit/push，八列Schema不变。

## 有效入口与职责

- [阶段图](model-token-usage-stages.svg)：每个节点包含功能、改动、验收、Agent 指示和责任方。
- [可编辑场景](model-token-usage-stages.excalidraw)：与 SVG 来自同一场景。
- [设计](../../docs/design/model-token-usage.md)：唯一 schema、写入和聚合合同；图中的 `Ref` 均指向该文件的显式锚点。
- [开发 handoff](model-usage-handoff-20261008.md)：新 session 的阅读顺序、修改树、工作区基线与执行入口。

本 Agent 本轮负责 handoff 中 Server 持久化/采集/API 和 Web 统计替换，以及阶段证据维护。
授权来源是用户本轮“按 handoff 的 U02–U05 修改树连续执行代码、配置、测试和文档修改”。
以下首次文档交付描述为历史证据，不作为当前实现或操作授权：

上一轮 Agent 负责补充 Server 设计、阶段图、证据索引及下一批核对。
职责与写入授权来自用户“补充文档，然后 schema model 我认可了，check-out plan-map”。
该历史轮次实际写入仅上述 TRD、本索引、SVG、可编辑场景四个新增文件。
该历史轮次没有生产代码、配置、测试改动；没有建表、迁移、启动模型、重启服务、commit 或 push。

后续方向为 Gateway 已有用量 → Server 业务归属 → 现有 MySQL → overview → 当前模型消耗页。
两个业务值为 `video_parse`、`chat_retrieval`；后者覆盖助手直接回答、Web 和视频检索。
独立检索接口不因此新增统计类型或随机 run；Chat 内部 embedding/rerank 继承助手消息归属。
无 schema 关键未决项。`metadata.src` 无明确实际来源时为 null，无需虚构来源或新增模型配置。
用户最新命名决定：表为 `model_usage`，新 ORM 为 `ModelUsageRecord`，仓储为 `server/repository/model_usage.py`；`usage` JSON 字段、八列和 Gateway `ModelCallUsage` 合同保持。

## 阶段与状态索引

| 阶段 | 设计引用 | 状态及完成证据 |
|---|---|---|
| U01 设计与 check-out | [职责](../../docs/design/model-token-usage.md#scope)、[schema](../../docs/design/model-token-usage.md#schema) | 设计与树已确认，本轮明确授权 U02–U05 实施。 |
| U02 通用调用持久化 | [schema](../../docs/design/model-token-usage.md#schema)、[SQL 查询](../../docs/design/model-token-usage.md#query) | 六文件已实施；此前 SQLite/DDL 通过；2026-10-09 MySQL 8.4.11 独立测试库 139 passed / 2 skipped，真实存储验收通过。 |
| U03 V2 与 Chat 采集 | [采集](../../docs/design/model-token-usage.md#recording) | 本轮99个唯一调用owner/JSON/预算通过：Parse17，chat_retrieval82（77生成+5检索embedding）；七failed已知用量与两unknown保留。429/409/49为各自历史。 |
| U04 SQL 统计 API | [查询](../../docs/design/model-token-usage.md#query)、[公开合同](../../docs/design/model-token-usage.md#view) | SQL 聚合与 local/taskType/Token 合同已实施；补齐 HTTP 错误和部分 SDK 用量跨层验收后 41 passed；现有 GET 和五参数保留。 |
| U05 统计页面 | [页面](../../docs/design/model-token-usage.md#view) | 六文件与此前typecheck/42直接测试完成；本轮真实99/known1204853/missing2，input/output/total为null，两视图复用、series筛选和刷新通过，pageErrors/httpErrors空。 |
| U06 真实全流程验收 | [验收](../../docs/design/model-token-usage.md#acceptance) | ✓ 538冻结文件+十补丁范围的统计/系统通路及整chunk播放通过；保留补修前Parse→修复后Media/Chat，99/known1204853/missing2。逐事件时间精度未通过；ROOT并行清理未纳入。 |

依赖：U01 → U02；U02 后 U03 与 U04 可并行；U04 → U05；U03 + U05 → U06。
U03/U04 共用 `service/parse_usage.py` 和统计测试，分别按 capture/append 与 overview/aggregation 交接。
U02/U04 共用 `parseUsageDTO.py`，先原始 State/batch，再统计输出；不同时覆写共享区域。
本轮实际执行指令已覆盖 U02–U05 源码与离线测试；阶段图和 schema 认可本身不执行真实 DDL。

## 开工前源码证据（历史）

| 核查对象 | 源码事实与对计划的影响 |
|---|---|
| [gateway/usage.py](../../gateway/usage.py) | 已有独立 UUID call_id、TokenUsage、SDKModelUsage、不可变 context 与 observer fanout；无需再修改模型 adapter。SDK 费用字段不进入本地 Token JSON。 |
| [bootstrap.py](../bootstrap.py) | 当前仍构造 Langfuse writer/reader 并装配 ParseUsageService；SQL 切换尚未发生。 |
| [model/parse_usage.py](../model/parse_usage.py) | 旧模型调用表绑定 Job/解析执行字段及费用，不能直接作为新 Chat 通用表；旧模型保留。 |
| [database.py](../database.py) | 已导入 parse_usage 模型并 create_all；新模型沿用既有登记，只补 UTC 列和已存在表的检查，不把新表误塞进旧 worker 迁移前置表集合。 |
| [agent_chat.py](../service/agent_chat.py) | 真正的模型执行在 executor worker 内；Chat scope 必须在该线程绑定助手消息身份。 |
| [video_parse.py](../service/video_parse.py) | 已有 capture_video 与 asyncio.to_thread；新服务可沿用此调用，预算/租约控制保持。 |
| [video_parse_model_calls.py](../service/video_parse_model_calls.py) | 仅可选 usage_observer 非空时装订阅；生产唯一构造未传该参数。因此当前 manager 不覆盖外层统计，必要树不含该文件。 |
| [video_retrieval.py](../service/video_retrieval.py) | 已有 embedding/rerank 局部 model_call_context；可继承 Chat 根 observer，不需新 retrieval run 或新的统计依赖。停止 LF 注入后，可选 tracing scope 自然为空。 |
| [api/parse_usage.py](../api/parse_usage.py) | 已有 GET overview 和窗口/系列/run 参数；保留入口，改本地投影。 |
| [UsagePage.tsx](../../web/src/features/usage/UsagePage.tsx) | 已有一次 overview 请求、两视图、刷新及系列筛选；当前仍写 Langfuse 与 USD，后续仅对齐本地 Token 合同。 |
| [ParseUsageDetails.tsx](../../web/src/features/tasks/ParseUsageDetails.tsx) | 生产未挂载，但参与编译并消费公开费用字段；只做合同对齐，不新增任务页入口或预建功能。 |
| [test_mysql_integration.py](../../tests/server/test_mysql_integration.py) | 有 ANIVAG_TEST_DATABASE_URL / DatabaseEnvironment.TEST 和自身用例清理范本；新仓储测试沿用模式，无需建立新共享测试框架。 |

`/search` 页面实际请求 Agent 消息接口；独立 `/api/search` 存在，但当前无生产页面调用。
这两项事实不授予独立搜索统计产品范围，也不授权删除已有检索 API。

此前 Gateway Langfuse 阶段记录是历史接入证据；用户已取消该平台方向。
本地图复用其通用 Gateway 用量合同，不沿用其真实 Langfuse 项目待办。
不改旧 Gateway 地图或独立 Parse V2 主线的当前位置。

## 原 U02 proposal（历史，现已按六文件实施）

本批职责为通用表及 repository，不包含运行装配、API 切流和页面。
具体目标：调用可被追加、重复写入不增加统计、归属冲突被拒绝、SQL 先完整筛选再限量。

```text
AniVAG/
├── server/
│   ├── [M] database.py                         ← 登记新表 UTC 时间列与结构检查
│   ├── model/
│   │   ├── [M] parse_usage.py                  ← 新增八列 ORM，保留旧模型
│   │   └── dto/
│   │       └── [M] parseUsageDTO.py            ← 通用不可变 State 与有界 batch
│   └── repository/
│       └── [A] model_usage.py                 ← 短事务追加、归属幂等、SQL 过滤及 limit+1
└── tests/
    └── server/
        ├── [M] test_database.py               ← 八列、索引、UTC、JSON 与约束
        └── [A] test_model_usage_repository.py  ← 真实仓储与隔离/重放/过滤/截断验收
```

预计 6 个文件：4 个生产文件、2 个测试文件；4M、2A，无删除或重命名。
基于已读同层模型/仓储范本，粗估生产新增与删除合计 220–360 行、测试 180–300 行；不是已产生的 diff。
数据库 Schema 新增一张表，旧表和历史数据保留；本阶段不改变 HTTP 或前端行为。
该 proposal 的源码范围现已获本轮执行指令并实施；真实测试库建表仍另核实环境操作授权。

验收计划：

- SQLite 实存储、MySQL dialect DDL：快速核对模型、约束、UTC、JSON 与幂等，单独记录结果。
- 真实测试 MySQL：保存 unknown JSON null、UTC 毫秒、重复/并发 call_id；不同 owner 的同 ID 拒绝。
- 真实 SQL 先筛选/再 limit：先插入排序靠前且超过上限的其他 workspace/series/run，再插入目标；目标必须返回。匹配目标超过上限时才 truncated。
- 系列数组精确成员匹配，同毫秒按 call_id 稳定排序；不能用 JSON 文本 LIKE 或 Python 后筛选。

U02 真实 MySQL 未通过时，保留其实现/离线子项结果，不能宣称存储阶段全部验收完成。

## 原后续范围索引（已获本轮 U02–U05 授权）

下表保留原阶段树；源码已按 U02–U05 树执行，2026-10-09 的 U06 隔离运行事实见文末。

| 阶段 | 剩余必要文件与共享范围 |
|---|---|
| U03 | `server/bootstrap.py`、`service/parse_usage.py`、`service/agent_chat.py`；`config.yaml`；`tests/server/{test_bootstrap,test_parse_usage,test_agent_chat_service,test_video_parse_worker,test_video_retrieval_service}.py`；`tests/gateway/{test_config,test_model_config_v2}.py`，共 11M。 |
| U04 | `server/service/parse_usage.py`、`model/dto/parseUsageDTO.py`、`model/vo/parseUsageVO.py`、`api/parse_usage.py`、`tests/server/test_parse_usage.py`，共 5M，其中 3 个文件与前阶段共用。只改读取/聚合/API 区域。 |
| U04 使用说明 | `README.md`、`server/README.md`、`.env.example`、`docs/parse-model-usage.md`、`docs/api-ui-views.md`、`docs/design/frontend-api.md`，共 6M，仅调整用量来源和配置/合同的相关段落。保留可选 LF 空密钥字段。 |
| U05 | `web/src/api/{contracts.ts,client.test.ts}`、`features/usage/{UsagePage.tsx,UsagePage.test.tsx}`、`features/tasks/{ParseUsageDetails.tsx,ParseUsageDetails.test.tsx}`，共 6M。 |
| U06 | 使用既有运行与验证入口，对照真实模型/SQL/API/UI，结果写回本索引；环境与运行操作先按阶段核对。缺口归其所属模块，不能越界修补。 |

原 U02–U05 树共 31 个唯一文件，29M、2A：12 个生产文件、12 个测试文件、2 个配置文件、5 个说明文档。
用户本次追加授权修复旧 manager 测试，累计实施范围为 **32 个唯一文件，30M2A**；跨阶段共享文件不能重复累加。
原 U02–U05 修改树未包含 `video_parse.py`、`video_retrieval.py`、预算 manager 源码或 Web 金额 helper；后续 endpoint 修复已明确授权前两者的编码身份接线，预算 manager 和金额 helper 保留。

## 真实环境验收条件与范围

| 依赖及责任方 | 交付条件 | 阻塞范围 |
|---|---|---|
| 测试 MySQL 与操作授权；本次联调 Agent | 用户已有授权；独立namespace `anivag_usage_multichunk_20261009_052927_test`，八列/SQL/API/真实页面精确核对99/known1204853/missing2 | 统计与系统通路范围通过；原库/历史库不混用，生产数据不迁移 |
| 模型及现有 V2 运行依赖；本次联调 Agent | 原默认模型/理解/fps=1；90132ms两chunk解析、双向量/ES2、最终Chat两段命中播放通过，补修前Parse保留 | 538+十补丁的通路验收通过；逐事件精度未通过，图片未观察；并行V1/Qdrant清理仅ROOT离线 |
| 实时识别 sub-agent；独立 OKR 负责人 | 见现有 docs/dev/OKR.md；未来实现后沿用 Chat 业务归属 | 不阻塞本批，不预建新的 enum 或模块 |

## 首轮文档验证（历史）

主图由临时工具中的 `@excalidraw/excalidraw@0.18.1` 官方 [exportToSvg](https://docs.excalidraw.com/docs/@excalidraw/excalidraw/api/utils/export) 导出。
工具依赖仅在 `/private/tmp/anivag-token-usage-map-20261008`，没有改业务依赖；导出命令为该目录内 `node render.mjs`。
结果只证明设计交付，不证明上述验收计划通过。

| 本轮检查 | 实际结果与限制 |
|---|---|
| 场景与节点 | JSON 合法；86 个原生元素，六阶段各有四项及有效 Ref；当前标记仅 U01。 |
| 图源与 SVG | 官方 exportToSvg 成功；SVG XML 合法，viewBox 为 2984 × 1716.5，32 个 path、110 个 text、0 个 image；不是整图位图。 |
| 中文与布局 | 浏览器渲染后检查适屏及完整预览；没有文本超出卡片，最小正文/引用间距 54.5 场景单位；没有裁切或连线穿过正文。 |
| 文档与引用 | 七个显式设计锚点、图中 Ref、Markdown 本地目标及围栏通过；无尾随空白，git diff --check 通过。 |
| 独立接手 | U02/U04/U05 的职责、授权、树与阶段依赖可接手；已补明确 main_loop、截断及 unknown subset 的完整性传播，无新增表列或 API 字段。 |
| 工作区保护 | 529 个 tracked 文件与本轮基线内容一致；本轮仅新增四产物。原有十个 modified 文件保持原状。 |

`server/design/model-token-usage.md` 受现有 `.gitignore` 的 `**/design/` 规则忽略；文件实际存在，没有 force add 或修改忽略规则。
普通 git status 可见三个新增 report_docs 文件；不能据此漏算被忽略的设计文档。
没有运行应用测试、模型或数据库；U02/U03/U04/U05/U06 的验收均为后续计划。

## 表名与开发交接更新（历史）

依据用户要求统一 table name 并交给新 Agent session 开发，本次同步表名、DDL 约束/索引名与新 ORM 名，并新增开发 handoff。
本次实际只改设计、本索引及新增 handoff，共三个文档文件；业务代码、配置和数据库未改。
阶段图没有旧表名或旧 ORM 名，且 Ref 仍指向同一设计锚点；图源与 SVG 保留，不制造无意义的重导 diff。
原导出工具位于临时目录，当前已不可用；上面的导出记录是已完成的历史交付证据，不表示工具仍可运行。
新 session 的第一批仍为 U02 六文件；整条路线仍为 U02–U05 实现及 U06 真实验收。
交接期间复核 529 个 tracked 文件：525 个与本次开始基线一致，另四个 embedding/index-coordinator 源码及测试由工作区并发改动，全部保留，见 handoff 工作区快照。
表名/ORM/DDL 命名、31 文件树的 29M2A 与路径、文档引用和围栏检查通过；独立接手审阅通过，git diff --check 通过。

## 本轮 U02 阶段级 check-out 与实施证据

职责为八列通用持久化、不可变 State/batch、归属幂等和有界 SQL；按设计 #schema / #query，保留旧表。
实际六文件与 handoff U02 树一致；沿用 ParseUsageRepository 短事务、AgentRepository JSON 筛选、UTC_TIMESTAMP。
校验和事务归 repository，owner/投影归 service，公开输出归 API；无 Schema 未决项。

命令：`.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py -q`。
结果：**111 passed，24 skipped**。真实 TEST MySQL 未配置；SQLite 参数下的并发 MySQL 专项也跳过。
离线证据包括八列/两索引/CHECK/utf8mb4_bin MySQL dialect 编译、SQLite UTC 毫秒及 JSON null 往返、
同 owner 重放不覆盖、三种 scope 冲突、固定 JSON 形状及不可变快照、其他 IntegrityError 不吞，
真实 20,001 条排序靠前的非匹配系列数据先 SQL 过滤、同毫秒 call_id 排序及额外一行不计小计。
新表不存在时允许 init-db 登记；已有新表结构/类型不兼容时要求受控升级，不误归旧 worker 必备表。

收尾补充：JSON 查询显式区分 string / array，错误形状不能冒充系列成员；已有 MySQL 表/字符串列的非二进制 collation 拒绝静默复用。
最终同一 U02 命令结果 **114 passed，27 skipped**，新增验证均在离线 SQLite / dialect / reflection 替身上完成。
27 个跳过含未配置 TEST MySQL 的参数实例及 SQLite 下两个 MySQL 并发专项；不代表真实 MySQL 已通过。

环境核查仅看字段存在性：进程环境与 `.env.local` 均无 ANIVAG_TEST_DATABASE_URL；未输出密钥或连接串。
本轮没有连接或变更真实 MySQL，没有服务运行、模型调用、commit 或 push。

## 首次 U03 阶段核对与实施证据（历史，四项失败现已补修）

实际 11M 与 handoff U03 树一致。bootstrap 构造共享 ModelUsageRepository/ParseUsageService，
停止 Langfuse writer/reader 装配，tracing 保持 None；V1 processor 与 processor factory 不注入新统计。
V2 复用 capture_video 与现有 model_call_context；Chat 在 executor 实际执行线程 capture_chat。
稳定 callable combine=True 去重，owner ContextVar 随 asyncio.to_thread 继承；冲突身份拒绝，白名单投影不含正文或 SDK 费用。

命令为 handoff U03/U04 八文件 pytest 集合，暂排除四个已定位的既有失败：
`-k 'not uploaded_v2_pipeline_recovers and not manager_uses_gateway_call_id and not transient_concurrency'`。
结果：**199 passed，1 skipped，4 deselected**。跳过项为既有可选 TEST MySQL。
采集用例确认 Chat 成功/失败均保留 Token、在实际 executor 落库且重放不重复执行，
检索继承助手消息 owner/系列/视频/parse-task 数组，独立检索和 V1 不生成新统计身份。
真实 Server V2 worker/agent/repository/queue/manager 的离线组件流程生成 4 条调用、31 Token；
SQL call_id 与预算 used_call_ids 完全一致，任务/子任务完成、lease 清空、active reservations 最终为空。
模型和 ES/媒体边界使用替身；该证据不能替代 U06 真实模型/索引验收。

四个原有失败在 `/private/tmp/anivag-model-usage-baseline-20261008` 开工前快照中复现：
worker uploaded_v2_pipeline_recovers 的 full-window/short-window 缺 services.agent.message_options；
manager_uses_gateway_call_id 和 transient_concurrency 的 embedding fixture 缺 provider 实际 response.model。
完整用例为：

```text
tests/server/test_video_parse_worker.py::test_uploaded_v2_pipeline_recovers_indexing_searches_receipt_and_reconciles_delete[full-window]
tests/server/test_video_parse_worker.py::test_uploaded_v2_pipeline_recovers_indexing_searches_receipt_and_reconciles_delete[short-window]
tests/server/test_video_parse_model_calls.py::test_manager_uses_gateway_call_id_for_reserve_usage_and_release
tests/server/test_video_parse_model_calls.py::test_transient_concurrency_contention_waits_before_request
```

基线复现命令仅选择这四项，结果 **4 failed / 11 deselected**；没有改 manager 测试或相邻生产逻辑。
当时按原 handoff 将两类问题列 TODO；用户随后明确要求本 Agent 全部修复，已完成，结果见下文最新回归。

最终追加真实 SQLite 文件与双线程并发 Chat scope 验证：共享服务中 workspace / assistant message 不串归属；每线程短事务落一条。
该轮保留 manager 源码与旧测试；本次追加授权仅补齐旧测试响应替身，预算、heartbeat、取消与 release 断言保持。

## 本轮 U04 阶段核对与实施证据

职责为 service 读取/聚合、DTO/VO/API 投影与六处使用说明，实际 11M 与 handoff 一致，三个文件与前阶段共用。
owner/append 区域延续 U03；校验 SQL 输入由 repository/service 承担，API 只做公开投影。
原 GET `/api/usage/overview` 及 workspaceId / fromStartTime / toStartTime / seriesId / runId 五参数保持。
响应 source=local，status=available/unavailable；run.taskType 两值，删除费用/货币/LF 专用公开状态。
没有新增 taskType 请求参数、路由、Gateway adapter 或价格模块；无 Schema 未决项。

首次实施命令：`.venv/bin/pytest tests/server/test_parse_usage.py -q --tb=short`，当时 **33 passed**；本批 HTTP 验收补充后的 41 passed 见文末。
包含采集与聚合共享测试，不能和 U03 数量相加。
真实 SQLite repository → TestClient API 验证工作区隔离、五参数和 Token-only 响应；不是 mock API 数据源。
普通/SDK 明细替代父总数、distinct call_id、缓存/模态/推理子集、未知 subset、主用量未知、main_loop、失败调用、
截断后各层完整合计 null 与已读小计、空窗口零条已记录调用、SQL 失败不可用均有直接断言。
窗口默认 30 天/下一分钟截止与最多 31 天沿用；README / server README / env / 三份 API/用量说明已同步本地来源。

## 本轮 U05 阶段核对与实施证据

职责为既有 Web 合同、模型消耗页及直接回归，实际六文件 6M 与 handoff 一致。
沿用 client.ts、两视图、系列筛选、刷新/轮询和样式；未挂载 ParseUsageDetails 仅为合同/编译对齐。
未增加任务页入口、CSS、金额 helper、额外请求或产品功能。
页面展示已记录调用、已知 Token 小计、Chat / V2 归属；未知为 —，空窗口与读失败区分，截断清楚提示读取范围。
可空缓存/推理未知不会清零，也不会让已确认的核心总量变成缺失；模型与 run 的不完整总数旁保留已知小计。

`web/` 下 `npm run typecheck`：**通过**。
`npm test -- src/api/client.test.ts src/features/usage/UsagePage.test.tsx src/features/tasks/ParseUsageDetails.test.tsx`：**3 个文件、42 passed**。
补充场景发现共享 formatTokens(null) 的“未记录”含义不适合本页未知细项，采用两个组件内本地显示函数，未改通用 helper。
回归覆盖运输五参数/响应、视图/系列/刷新、空/失败/未知/小计/截断；没有真实产品服务或 U06 浏览器运行。

## 首次实现后的回归、范围和交付核对（历史）

最终命令（项目根目录）：

```sh
.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py tests/server/test_parse_usage.py tests/server/test_bootstrap.py tests/server/test_agent_chat_service.py tests/server/test_video_parse_worker.py tests/server/test_video_retrieval_service.py tests/server/test_video_parse_model_calls.py tests/gateway/test_config.py tests/gateway/test_model_config_v2.py -q --tb=line --show-capture=no -k 'not uploaded_v2_pipeline_recovers and not manager_uses_gateway_call_id and not transient_concurrency'
```

结果：**334 passed，28 skipped，4 deselected，1 warning**（既有 Starlette/AnyIO DeprecationWarning）。
28 skipped 为新仓储 27 项与既有可选 MySQL 项；MySQL 未配置，无真实连接。
在补充最终三个离线用例前，同一十文件集合未筛除已知失败时为 **331 passed / 28 skipped / 4 failed**；
四个失败完整名称与基线复现已在 U03 登记，不能把最终 deselected 表述为全套回归通过。

源码实施树为 31 个唯一文件（29M2A），另外维护五个已有产物：本索引、设计、handoff、原生场景和 SVG。
范围按开工前 `/private/tmp/anivag-model-usage-baseline-20261008` 内容核对，不能将用户原有 diff 算成本轮成果。
31 文件相对开工前快照的实际 diff 为 **1426 行新增 / 720 行删除**，不含五份阶段产物或用户已有变更。
529 tracked 文件加五份既有产物共 534 份基线：500 份内容不变，34 份按授权范围更新，另新增仓储和测试两文件。
树外十二个原有 modified 文件逐字节不变；retrieval 测试只插入新回归，frontend-api 仅替换统计段落，原有相邻修改均保留。
hypnos-code 三轮核对覆盖职责/最小变更、同层结构/输入校验、调用逻辑/直接证据；旧模型/仓储/价格历史与可选 tracing 模块保留。
真实建表、MySQL 用例清理、服务启动/重启、真实模型及 U06 未执行；没有 commit/push。
地图当前位置仅 U02，避免用后续源码进度越过尚缺的真实存储验收；U03–U05 离线结果与 U06 未执行分项标注。

官方导出工具在 `/private/tmp/anivag-model-usage-map-20261008`，使用 `@excalidraw/excalidraw@0.18.1` 的 exportToSvg；临时依赖未进入业务 package。
可编辑场景与 SVG 同步：**86 原生元素、32 path、110 text、0 image，viewBox 2976 × 1708.5**。
当前标记仅 U02，层级/顺序有效；六卡片标题、状态、四项与 Ref 均在卡片内，Agent 正文与 Ref 最小间隔 53.5 场景单位。
浏览器预览确认中文、连线与当前徽标可见，无文字裁切；不是整图位图，也未手改 SVG。
八份 Markdown 的本地目标/设计锚点、围栏与尾随空白通过；git diff --check 通过。

## U03 回归补修与收口（历史）

授权来源：用户“**不需要外部TODO,你全都去改就行了**”，接管前述两类四项既有失败，覆盖原 handoff 排除的旧 manager 测试。
本次源码改动为两个测试文件；生产逻辑、Schema 与公开合同沿用现有实现。无关键未决项，不重复请求源码授权。

```text
AniVAG/
├── tests/
│   └── server/
│       ├── [M] test_video_parse_worker.py      ← 补 agent options、当前 recall/索引版本与回执断言
│       └── [M] test_video_parse_model_calls.py ← 补 provider 实际 model 响应字段
└── server/
    ├── design/
    │   └── [M] model-token-usage.md           ← 更新 U03 状态与追加授权树
    └── report_docs/
        ├── [M] model-token-usage-stages-evidence.md ← 关闭两类 TODO，登记完整回归
        ├── [M] model-usage-handoff-20261008.md     ← 更新当前状态、树与验证入口
        ├── [M] model-token-usage-stages.excalidraw ← 同步 U03 节点与最新结果
        └── [M] model-token-usage-stages.svg        ← 同场景官方导出
```

最小修复说明：

- worker 的 agent 替身提供 message_options().top_k，继续验证实际搜索入口及系列未就绪的 409。
- 同一 worker 流程的 ES 替身复用 ElasticsearchVideoChunkStore.recall，按当前 index_generation 筛选，保留 workspace / series / task 精确范围。
- 当前 ChunkIndexReceipt 和公开 parse_provenance 已收敛为任务、内容版本与索引版本；旧断言读取 source/projection/contract 三个已移除字段，改为当前三个字段的完整等值对账。
- manager 假响应补 model=request["model"]，原 call_id / reserve / release / admission / concurrency / heartbeat / cancellation 断言保持。

完整回归命令：

```sh
.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py tests/server/test_parse_usage.py tests/server/test_bootstrap.py tests/server/test_agent_chat_service.py tests/server/test_video_parse_worker.py tests/server/test_video_retrieval_service.py tests/server/test_video_parse_model_calls.py tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/server/test_api.py tests/gateway/test_video_embedding.py -q --tb=short --show-capture=no
```

实际结果：**421 passed，28 skipped，1 warning；零 failed、零 deselected**。
包含原四项失败、原十文件统计集合及搜索 API / Gateway embedding 邻近回归；没有使用 -k 排除。
两种解析窗口均完整通过上传 → 解析/索引失败 → 恢复 → receipt/检索/API 对账 → reparse → 删除失败隔离 → 清理恢复。
28 skipped 仍是未配置真实 TEST MySQL 的既有跳过，warning 为既有 Starlette/AnyIO 弃用提示。
Web 未受本次测试修复影响，沿用上轮 typecheck / 42 passed；本次未重跑前端测试。
两类测试适配 TODO 已关闭；真实 MySQL / 服务 / 模型 / U06 保留原环境验收状态，当前标记仍仅 U02。

本次两测试文件相对 `/private/tmp/anivag-u03-fixtures-baseline-20261008` 的实际变更为 **12 行新增 / 5 行删除**，另外维护上述五份阶段产物。
累计实现树为 32 个唯一文件（30M2A），相对最初源码快照 **1438 行新增 / 725 行删除**；不混入用户原有 diff。
再次核对 534 份最初基线：499 份内容不变、35 份在已授权范围内更新，另新增仓储及测试两文件。

## 本次 U04 HTTP 验收补充与 U05 合同复核

授权来源：用户认可集中 callback 设计后明确“**直接开发吧**”；沿用 U02–U05 已授权文件树与八列 Schema。
本批只在现有 `tests/server/test_parse_usage.py` 追加三个测试函数、八个用例，**74 行新增、0 行删除**；原文件内容完整保留。
另外维护设计、本索引、handoff、原生场景与官方导出 SVG 五份已有产物；没有新增生产封装或分散 SQL 调用。
独立合同审查发现三处说明不精确，按原 U04 文档范围修正：`docs/api-ui-views.md` 和 `docs/design/frontend-api.md` 的全局时间约定补齐 `model_usage` UTC `TIMESTAMP(3)` 例外，HTTP 时间仍为 Unix 毫秒；`docs/parse-model-usage.md` 明确匹配记录超过 20,000 条才截断。
本批实际树为一个测试文件、三份使用说明和五份阶段产物；共享 frontend-api 仅额外修改这一条时间约定，其他既有改动完整保留。

```text
AniVAG/
├── tests/
│   └── server/
│       └── [M] test_parse_usage.py              ← 八个 HTTP 跨层验收
├── docs/
│   ├── [M] api-ui-views.md                     ← 数据库时间约定补 model_usage 例外
│   ├── [M] parse-model-usage.md                 ← 精确说明截断边界
│   └── design/
│       └── [M] frontend-api.md                 ← 仅补统计表 UTC 时间例外
└── server/
    ├── design/
    │   └── [M] model-token-usage.md             ← U04 验收状态
    └── report_docs/
        ├── [M] model-token-usage-stages-evidence.md ← 本批证据与范围
        ├── [M] model-usage-handoff-20261008.md     ← 最新接手状态
        ├── [M] model-token-usage-stages.excalidraw ← 最新 U04 / 联合回归
        └── [M] model-token-usage-stages.svg        ← 同场景官方导出
```

必要验收：

- 真实 SQLite repository → service → HTTP → VO：普通调用 120 Token 与 SDK 的已知 12 Token / 未知明细联合返回 132 的已知小计，完整总量为 null；SDK 父用量不重复计算。
- 同一 `runId=task` 的解析与 Chat 在 HTTP 返回中按 `taskType` 保持两条 run；模型分组与 run 小计均做精确匹配。
- 查询异常经过真实 HTTP 路由返回 200 / unavailable，调用数与核心总量为 null，分组为空；响应不暴露异常载荷，不伪造零成功。
- 空、倒序、超过 31 天窗口及含边缘空白的 runId 返回 400 / INVALID_REQUEST；时间类型错误与空 seriesId 返回 422 / VALIDATION_ERROR；所有错误请求均在 SQL 查询前拒绝。

直接命令：

```sh
.venv/bin/pytest tests/server/test_parse_usage.py -q --tb=short --show-capture=no
```

结果：**41 passed，1 warning**。包含原 33 个用例与新增八个 HTTP 验收，没有排除原测试。

联合命令：

```sh
.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py tests/server/test_parse_usage.py tests/server/test_bootstrap.py tests/server/test_agent_chat_service.py tests/server/test_video_parse_worker.py tests/server/test_video_retrieval_service.py tests/server/test_video_parse_model_calls.py tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/server/test_api.py tests/gateway/test_video_embedding.py -q --tb=short --show-capture=no
```

结果：**429 passed，28 skipped，1 warning；零 failed、零 deselected**。跳过项仍未提供真实 TEST MySQL 验收，warning 为既有 Starlette/AnyIO 弃用提示。
独立只读审阅确认新增测试保护跨层合同、精确小计和查询前校验，没有放宽原断言。
U05 源码复核确认消费 source/status/taskType、null 与已知小计、截断/空窗口/读失败及原有两视图/筛选/刷新一致；本批未改 Web、未重跑前端，沿用此前 typecheck / 42 passed。
集中采集继续由 Gateway 自动通知、ParseUsageService 统一校验投影、repository 统一事务负责；业务层只在 V2 模型阶段和 Chat 实际 executor 入口绑定归属。
真实 MySQL、产品服务、真实模型与 U06 本批未执行，保留各自验收状态。
交付核对通过：八份 Markdown 引用/锚点、围栏和空白，git diff --check、原生场景与 SVG 同步；图保留 86 元素、32 path、110 text、0 image，预览未见裁切。
累计实现仍为 32 个唯一文件（30M2A），相对最初快照 1513 行新增 / 726 行删除；不计五份阶段产物和用户原有改动。树外十二个原 modified 文件逐字节一致，共享检索测试的原内容及 frontend-api 的相邻改动保留。
用户原有树外十二个 modified 文件逐字节不变，两份共享文件中的原有修改保留；没有 commit/push。
官方导出及图形核验通过：仍为 86 原生元素、32 path、110 text、0 image；当前徽标仅 U02，U03 显示四项失败已修复。
八份 Markdown 本地引用/设计锚点、围栏、尾随空白及 git diff --check 通过；本次无新增生产逻辑、Schema 或公开合同变更。

## 前一轮 2026-10-09 e296d9b 联调（旧版本历史，不能代替当前修复版验收）

本节的 531 文件、临时 profile、27 calls / 115,158 Token 与 EOF/alias 失败均来自旧 `e296d9b`；后续毫秒重构与修复未纳入该次运行。当前状态以文首及本轮修复版证据为准，以下失败不作为当前未修复缺陷。

授权来源：用户明确“**然后可以进行联调测试吧**”，本次联调范围包括隔离测试数据库建表与用例清理、现有服务运行及真实模型测试。
本次只维护设计、本索引、handoff、原生场景和官方 SVG 五份已有产物；源码、Schema、公开合同与用户原有工作区改动保留。
运行采用已推送提交 `e296d9b8ab1a54122826c1c7fca4c093ecb8fbcc` 的隔离源码；531 个 tracked 文件逐字节匹配该提交。
根工作区 HEAD 已由并行任务更新为 `224cbbc` 且保留检索等未提交改动；这些后续源码不在本次运行和验收范围。
本次 pinned `e296d9b` 的十二文件 Server/Gateway 离线基线为 **409 passed / 28 skipped**，Web 为 **42 passed** 且 typecheck 通过。
此前 **429 passed / 28 skipped** 是 2026-10-08 当时工作区的十二文件回归，保留在历史阶段记录；它不代表本次隔离提交的离线结果。

环境与 U02 结果：

- MySQL **8.4.11**，连接时区 `+00:00`；数据库、Elasticsearch index、Qdrant collection 均为专用 `anivag_usage_u06_20261009_test`，不使用生产数据。
- 真实 `model_usage` 为已确认八列、全非空、UTC `TIMESTAMP(3)` 与 JSON；PK、两索引、CHECK、InnoDB / utf8mb4_bin 与设计一致。
- 在实际 TEST MySQL 配置下执行 `.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py -q`，结果 **139 passed / 2 skipped**；两项跳过仅为 SQLite 参数的 MySQL 并发专项，MySQL 参数并发用例实际执行。
- 真实 MySQL 通过 UTC 毫秒 / JSON null 往返、同 owner 幂等不覆盖、并发同 call_id 与归属冲突、精确 workspace/run/系列过滤先于 limit、同毫秒稳定排序、20,001 条截断边界。
- U02 真实存储验收已通过，当前标记由 U02 移到 **U06 运行中**；历史 SQLite / DDL 证据仍保留，不混同本次真实结果。

首条真实 V2 尝试：

- 输入为隔离工作区上传的 12 秒短片；真实任务 `4ddff2be-98e2-4583-b04f-802697e9254c`，视频 `acea16b8-d086-5560-b435-c18d64e217c2`。
- 任务终态为 `PARSING_FAILED`，失败发生在模型调用前的 FFmpeg 媒体准备。原始 chunk 错误包含 `Non full-range YUV is non-standard` 和 `ff_frame_thread_encoder_init failed`。
- 此失败尝试检查时，SQL 中 manager 的 `used_call_ids=[]`、`active={}`，`model_usage` 记录数 **0**，ES 命中 **0**；没有可对账模型调用，不能作为 V2 解析/index 或 Token 全链成功证据。
- 原始首条任务在临时联调目录 `/private/tmp/anivag-usage-u06-20261009/{first-attempt-task.json,first-attempt-upload.json}`；环境与后续 SQL/API/页面核对当时保存在该目录；该历史目录现已因 daemon recovery 消失，必要结果保留于本索引。只沉淀必要标识、状态和计数，不将凭据或模型正文写入阶段文档。

后续运行与部分对账：

| 尝试 / 真实 run | 结果与 Token 证据 | 验收范围 |
|---|---|---|
| 默认短片 `4ddff2be-98e2-4583-b04f-802697e9254c` | PARSING_FAILED；模型前失败，0 调用 / 0 ES | 保留默认采样 EOF 边界失败 |
| 降低采样后 `4905d808-c851-4f27-86b8-a7578700b131` | INDEXING_FAILED；5 调用、已知 56,856 Token，1 条超时缺用量，完整总量 null，0 ES | 验证重试、未知传播和失败业务仍记录用量；不算 index 成功 |
| direct Chat `df9f29f2-8a0b-4c0f-a695-86304d0600e2` | 成功；真实 call `0788e1f2-f2dd-470c-8b79-25aa6d550063`，DeepSeek Flash 742 input + 6 output = 748 | 未执行检索工具；operation 标签不能证明 Web 或视频检索实际触发 |
| bounded 视觉 `87580385-6130-4e9f-964d-d33678fdbbbc` | PARSING_FAILED；视觉响应 invalid JSON（char 263）；ASR 3,556 + 已失败视觉调用已确认 6,760 = 10,316 | call `4a5e0d67-412a-4bc8-8387-7a3a20bdfe1b` 为 failed 仍保留真实 usage；response_id `resp_0217914786044933862fb1b5b304033b62047eb9aa7ba762876da` |
| bounded ASR-only `f1c988dd-23ae-44b5-87fc-4616ba6f712e` | INDEXING_FAILED；ASR 3,410 + refine 3,853 = 7,263，0 ES | 原 embedding endpoint alias 未通过 Gateway visual sampling contract；未修改生产代码 |
| bounded canonical embedding `030b779b-fb30-4e05-903c-60116cb633e5` | COMPLETE；ASR 3,784 + refine 4,025 + text embedding 37 + video embedding 4,838 = 12,684，ES 1 条 | 有效 test profile 的 V2 / 预算 / receipt / SQL/API/UI 对账通过 |
| bounded 检索 Chat `912fd888-45aa-4485-9e8d-130a1f71a1d1` | 成功；19 次真实模型调用共 84,895 Token，为 17 次 agent_chat 与 2 次检索 text_embedding；两轮 retrievalTrace 均 1 hit | 继承同一助手消息 owner；同一请求重放新增调用为 0，成功配置的真实检索分支已触发 |

默认媒体失败已独立定位到采样边界：原 MP4 实长略超过 12 秒，数据库窗口向上取整为 13 秒；默认 fps=1 / max_frames=60 取 13 帧，末次时间 12.5 秒越过真实 EOF。
原生产 `prepare_chunk` 的完整流程独立复现为默认参数失败；仅任务级 fps=0.2 / max_frames=3 时，采样时间 `[2.1666666667, 6.5, 10.8333333333]` 全部通过。
没有修改媒体生产源码或项目默认配置，不能以此测试 profile 绕过结果宣称默认短片边界已修复。

根工作区 `usage-u06-20261009` 的独立 SQL/API 部分核对结果在 `verification-partial.json`：**6 calls / knownTokens=57,604 / missingUsageCount=1 / totalTokens=null**，`sqlApiExact=true`，系列/错误作用域/半开窗口筛选均通过，`fullFlowPassed=false`。
该 snapshot 包含失败 V2 的 56,856 已知小计与 direct Chat 的 748；视觉 Pro 的完成重试 23,880、refine 26,310、两次 ASR 3,410 / 3,256，以及一次 tokens=null 的视觉超时均逐 call 保留。
真实页面复核 `ui-verification.json` 为 passed：6 调用、4 模型分组、2 run，已知小计 **57,604**，完整 input/output/total 显示未知；系列筛选为 5 调用，两视图复用和刷新通过、无 pageErrors。
这份页面证据只证明已记录数据的呈现，与失败 V2 的 index 全链成功分别登记。

bounded 测试服务仅在临时运行配置中统一 index/query embedding 为 `doubao-embedding-vision-251215`；成功任务使用合法 ASR-only profile（understanding_enabled=false、fps=0.2、max_frames=3），源码仍为隔离 `e296d9b`。
成功任务的 text embedding call 为 `ed309fb8-41cd-4ba9-bc92-f43facba183c`，video embedding call 为 `5132588b-c8d4-40f0-a5a8-4d8010676f56`；原始任务、配置、SQL 和 ES 记录在临时目录的 `bounded/` 子目录。
另外观察到 `/api/series/get` 对 V2 `PARSING_FAILED` 返回 400：bootstrap 层的既有 VideoSummary schema 枚举未映射此状态，未以统计任务修改生产接口。

成功配置统计全链的最终证据：

- 隔离 workspace 为 `usage-u06-bounded-20261009`；`bounded/verification.json` 为 **passed / mode=full / runtimeSqlApiPassed=true**，覆盖实际 V2、预算、ES receipt、Chat 重放与 SQL/API。
- SQL/API 为 **27 calls / input=104,044 / output=11,114 / total=known=115,158 / missingUsageCount=0**；九个模型分组、四个 run（两条历史失败解析、成功解析与检索 Chat）一致。缓存/推理子集仍按未知返回 null，不影响可确认核心总量。
- 成功解析的四个 `used_call_ids` 与 SQL 精确一致，`active={}`；ES 一条记录关联同一解析 task，contentVersion=2 / indexGeneration=1。
- 检索 Chat 两次 retrievalTrace 均命中真实 `videoChunkId=7b5c0613-23b6-5a97-808f-9cc039b44bc7`；19 条真实调用为 17 次 agent_chat 与 2 次检索 text_embedding，同一 Chat 请求重放 `chatReplayAddedCalls=0`。
- SQL/API 精确算术、系列筛选、错误 workspace/run/系列作用域与半开时间窗口均通过；本次没有以调用标签代替真实工具轨迹。
- `bounded/ui-verification.json` 为 **passed**：真实页面 27 调用、9 模型分组、4 run、总量 **115,158**，系列筛选 27 调用，两视图复用和刷新通过、无 pageErrors。
- 独立只读审阅确认 27 个 call_id 唯一，解析及 Chat owner 全正确；metadata 固定 11 键与 context 白名单不含正文。parent / chunk / interval 均 COMPLETE，预算四 call 与 SQL 一致且 active 为空，receipt / ES / Chat 原始 parseProvenance 均对应同一 task、contentVersion=2 / indexGeneration=1。
- 本次实际 rerank 数为 **0**，全部 `sdk_model_usage=[]`；不宣称这些分支有真实模型覆盖，保留已有离线验证的独立范围。

结论按范围分别登记：**有效 test profile 的统计全链通过；默认配置未全流程通过**。U06 仍保留唯一当前标记，不标整体完成；默认短片 EOF、视觉 JSON 以及 embedding alias 的原失败均保留，未借本次联调修改媒体或产品源码。

收尾与保留：本次自建 API（80139 / 99714）已 graceful shutdown / exit 0，Web（80080 / 95272）已 SIGINT / exit 130；测试数据库、ES/Qdrant、输入与临时证据保留。
既有 9621 `/healthz` 仍为 status=ok / parseConsumer=running，既有 5173 进程 23656 仍在；没有修改原运行环境。
本次五产物的新联调证据未 commit/push；用户此前的推送仅对应上述 `e296d9b` 源码，保留所有并行与未提交工作区改动。

## 上一轮 2026-10-09 毫秒修复版默认配置联调（历史：当时索引未闭环）

本节保留 endpoint 身份修复前的毫秒默认流程记录；当时的模型身份阻断已经后续源码修复，不能当作当前未修缺陷。该历史临时目录因 daemon recovery 已消失，以下文件路径为原证据位置，必要计数和结果仍保留于本文。
用户当时指出源码已完成毫秒重构与修复；隔离旧 `e296d9b` 不能代表当时版本。因此沿用有效真实联调授权，以当前 HEAD 与全部现有 dirty 冻结源码重新验证，不替换模型、不降低默认采样参数。
本轮只维护设计、证据索引、handoff、原生场景与官方 SVG 五份已有产物；本轮开始前的五文件字节保存在 `/private/tmp/anivag-usage-u06-ms-20261009/baseline/`，其他 root 工作区文件哈希另行核对。

本轮来源、环境与已确认修复：

- 源码为 `224cbbcdaf9e8edbe77c855ced1577bbf863e65d` **加现有全部 dirty**；冻结目录 `/private/tmp/anivag-usage-u06-ms-20261009/source`，`source-manifest.json` 含 532 tracked 文件与逐文件哈希，捕获后与 root 当前文件逐字节一致。
- MySQL 8.4.11 / UTC `+00:00`，数据库、ES index、Qdrant collection 均为新的 `anivag_usage_u06_ms_20261009_test`；`video.duration_seconds` 注释为 `media_time_milliseconds`，model_usage 的八列 / TIMESTAMP(3) / JSON / 索引 / CHECK 保持。
- 同一输入 SHA256 为 `ec56f8cbd4c3b3b3b34212d7ee5ab4db31b0d423253d7d093aa1a83bcbf57792`；真实时长为 **12012 ms**，默认 frameFps=1 / maxFrames=60 / maxHeight=720，实际 13 个非空帧，atMs 从 462 至 **11550 < 12012**，全部在真实 EOF 前。
- `offline-media-verification.json` 为 passed，准备媒体时长与输入均为 12012 ms，lease workspace 已清理；这份当前代码证据确认旧版短片 EOF 边界已经修复，不能沿用旧失败判定当前版本。
- 独立现有 `test_video_parse_media.py`、`test_video_embedding.py`、`test_video_parse_index_coordinator.py` 为 **53 passed / 0 failed / 0 skipped / 0 deselected**；`offline-tests-verification.json` 确认验证后 532 tracked 文件哈希无漂移。

当时真实默认流程：

- workspace 为 `usage-u06-ms-20261009`；真实 task `57085694-6167-4ca4-a5ef-e561144eebe2`，video `f47abdad-68fa-5c5f-9c15-6a2f50110e13`，chunk `69c4fcec-32a0-56ba-9956-c661dcb75715`。
- 原默认 fps=1 / max_frames=60、understanding=true、原 ASR / Pro / embedding ep endpoint 均未作临时模型替换；`sql-media-time.json` 回读 `video.duration_seconds`、chunk `[0,12012)` 与 analysis window `[0,12012)` 全部为毫秒。
- chunk 已 **COMPLETE**；三次 ASR 用量为 3,468 / 3,582 / 3,474，三次 visual 为 16,150 failed / 21,701 failed / 29,515 completed。首 visual 失败已确认 timeline 排序问题；第二次具体错误未采样保存，不推断为同因。
- 首 refinement 为 **failed / length**，provider 已确认 input=4,511、output=32,768、reasoning=32,768、total=37,279，partial output 长度 0；reasoning 是 output 子集，不再次加入 total。该失败调用真实用量仍写入 SQL，之后默认重试 call `28b2368f` 完成为 **25,141 Token**；length 不是本轮最终阻断。
- 七条 / 115,169 Token 是当时中间快照。后续 refinement 重试 25,141 与两次 text embedding 各 1,032 Token，使最终 **10 SQL rows / total=known=142,374 / missingUsageCount=0**，input=34,010、output=108,364；四个模型分组、一个解析 run，无虚构零值。

终态与实际阻断：

- parent / interval 为 **INDEXING_FAILED**，chunk COMPLETE；ES 命中 **0**。真实默认媒体、ASR、visual 和 refinement 已完成，索引发布未成功，本轮未发送检索 Chat。
- 阻断发生于 `engine/rag/encoding.py:421` 的 strict response model 与 encoder.model 校验。请求及合同使用原 `ep-20260911212434-j4xcb`，两次 text embedding 返回实际 `doubao-embedding-vision-250615`，模型身份字符串不一致。
- 当时旧 fps guard 已移除，不是该轮错误；该轮 dimensions 尚未校验、video embedding 未触发，因此不宣称当时实际视频 fps 合同已通过。旧版本 EOF 与 fps guard 失败已被毫秒修复和媒体证据替代；之后真实 video / dimensions 验收见后续 endpoint 修复及完整默认流程记录。

本轮终态局部对账：

- 独立 SQL/API partial 核对通过：**10 calls / total=known=142,374 / input=34,010 / output=108,364 / missing 0**，四个模型分组、一个 run；精确算术、系列筛选、错误作用域与半开窗口均通过。
- terminal budget 的 `used_call_ids` 与 SQL 十个 call_id 完全一致，`active={}`，lease 已清理；已失败 visual/refinement 与已完成 text_embedding 的真实已确认用量均保留，不以随后索引校验失败抹去消耗。
- 实际统计页面为 **10 calls / 142,374 Token**，系列筛选十条、两视图复用与刷新通过、pageErrors 为空。当时另一路由 `/api/series/get` 仍返回 400；本验收只证明统计页 Token 合同与操作，不宣称全应用页面和所有 HTTP 路径均无错。

该轮 U06 保持未完成。上一轮 e296d9b / 临时 profile 的成功与 EOF/alias 失败已列为旧版本历史；该轮实际阻断为索引 response model 身份校验，默认索引发布与检索 Chat 未闭环。后续 endpoint 修复已替代这一阻断结论，见后续修复与完整默认流程证据。

独立终态审阅与证据入口：

- 十个 call_id 唯一且解析 owner 正确，状态为 **7 completed / 3 failed**；三条 failed 合计 **75,130 Token** 均保留，完整总量不因业务终态失败被置零。
- 冻结源码 **532/532** 文件哈希无差异；`profile-media-verification.json` 为默认配置与毫秒拓扑的纯文件核对 passed，**full=false**，不代替索引/检索全流程。
- 本轮历史临时目录 `/private/tmp/anivag-usage-u06-ms-20261009/` 当时保留 `source-manifest.json`、`offline-tests-verification.json`、`offline-media-verification.json`、`profile-media-verification.json`、`sql-media-time.json`、`lifecycle.json` 及终态 SQL/API/UI 原始审计；`lifecycle.json` 当时汇总最终阻断、预算释放和服务收尾。上述目录现已消失；本索引保留必要结果和原路径，不混算不同版本。

本轮服务收尾：临时启动入口 8871 / 8939 已收到 SIGINT；实际 API 18329 正常退出 0、Web 20703 正常退出 130，进程检查确认消失。原 9621 `/healthz` 为 ok / parseConsumer=running，原 5173 进程 23656 保留。
该轮收尾时测试数据库、ES/Qdrant、输入与临时证据保留；临时证据目录现已消失。该轮只有只读诊断、原默认真实联调与五份已有产物维护，没有写入源码、重构 Schema 或新增 commit/push。

## 前一轮 2026-10-09 endpoint 身份修复与真实 embedding 验收（局部历史）

用户明确授权 endpoint type enum 修复；源码基于 `224cbbcdaf9e8edbe77c855ced1577bbf863e65d` 和保留的原有 dirty，新增本轮九生产 / 六测试改动。改动前字节、工作区状态及 tracked 哈希在 `/private/tmp/anivag-endpoint-fix-20261009/{baseline,root-before.json,status-before.txt}`。
本轮文档职责仅维护既有设计、索引、handoff、可编辑场景和官方 SVG 五产物；不改 Schema、Token SQL 合同或业务源码，不追加模型调用。

实现与兼容行为：

- [gateway/model_sources.py](../../gateway/model_sources.py) 新增 `ModelReferenceType.MODEL/ENDPOINT` 和 frozen `ModelReference.resolve`，集中解析 `request_model` 路由与实际编码 `model`；业务层不复制字符串判断。
- [agent input](../../agent/video_parse_v2/input.py) 与 [Server parse profile](../config/parse_profile.py) 新增可选 `encoder_model`。新默认 endpoint 从根 config.yaml 已配置的 Gateway 实际模型绑定冻结 canonical 身份；upload/reparse 持久化该字段，重试沿用快照。
- [bootstrap](../bootstrap.py)、[upload](../service/upload.py)、[V2 worker](../service/video_parse.py) 与[检索装配](../service/video_retrieval.py) 统一用 canonical 身份构造 index/query/sparse 合同与 encoder ID，SDK 请求仍发送原 endpoint，provider 返回模型名及 usage 不改写。
- 旧快照缺少 `encoder_model` 时，只能采用 Server 显式配置的实际模型绑定；显式身份优先。直接 model 保持自身身份，endpoint 不能作为 canonical。source、dimensions、实际响应模型、encoder ID、input hash、缓存与查询严格校验保留，错误实际模型仍拒绝；不回填历史 receipt。
- [重解析仓储](../repository/upload.py) 先锁最近任务，再继承其完整配置并通过 upload 的既有解析规则为新任务冻结身份；未提交新配置的 reparse 不回退到初始 upload session 快照。终态新任务和 pending takeover 均保持最近任务的配置。
- 仅用于 fingerprint 的 requested 配置 JSON 在新字段 `encoder_model=None` 时省略该字段，保持旧显式配置上传的幂等指纹；其他 None 保持原样，显式编码身份继续参与指纹，effective V2 快照仍冻结实际模型。此兼容规则不改变 Token JSON 的 null 合同。
- index scope 比较消费 `embedding.encoder_model or embedding.model`，让不同 endpoint 但相同 canonical、直接模型与已冻结的同模型 endpoint 可共享索引范围；同 endpoint 的不同实际版本不能混选，未固化身份的旧 endpoint 不能与已冻结 canonical 混选。`Scope.embedding_model` 继续表达请求路由，不改写其语义。
- 追加前七生产 / 五测试的十四文件回归为 **614 passed / 14.86 s**，仅作追加前范围记录；最终修复含九生产 / 六测试，共十五个源码与测试文件：生产 **+123/-47**、测试 **+590/-2**。`change-verification.json` 确认 532 tracked 原始基线中只改这十五文件，`outsideChanged=[]`，原 dirty 保留。
- 最终相关十五测试文件 **686 passed / 0 failed / 0 skipped，26.70 s**，见 `final-regression.json` 和 `.log`；仅有现存 Starlette/AnyIO 弃用 warning。六个修改测试为 Gateway model_config、Server bootstrap、upload_service、video_parse_worker、video_retrieval_service 与 video_parse_repository；不把验证文件数十五误作修改测试数。
- 直接回归覆盖三种 reparse 继承/冻结场景和两种旧 Server/V2 配置指纹重放，以及四种索引 scope canonical 比较场景；原完整配置、旧显式配置幂等与错误实际模型拒绝行为均保持。此前 409/429/53 为不同版本或范围的历史证据，不混入 686。

最终回归实际命令（项目根目录；结果见上述 JSON / log）：

```sh
.venv/bin/pytest \
  tests/gateway/test_model_config_v2.py tests/gateway/test_video_embedding.py \
  tests/server/test_bootstrap.py tests/server/test_video_parse_worker.py \
  tests/server/test_upload_service.py tests/server/test_video_retrieval_service.py \
  tests/server/test_video_parse_index_coordinator.py tests/server/test_config.py \
  tests/agent/video_parse_v2/test_input.py tests/agent/video_parse_v2/test_framework.py \
  tests/server/test_video_parse_repository.py tests/engine/test_embedding_reuse.py \
  tests/engine/test_retrieval_pipeline.py tests/engine/test_retrieval_dependencies.py \
  tests/server/test_api.py -q --tb=short --show-capture=no
```

真实 I/O 证据为 `/private/tmp/anivag-endpoint-fix-20261009/live-embedding-verification.json`，UTC 2026-10-09 02:17:56 至 02:17:58：

| 真实调用 | 请求路由 / 编码身份 | 验收结果 | callback Token |
|---|---|---|---:|
| text embedding | 原 ep / `doubao-embedding-vision-250615` | 实际模型 250615、1024 维，Engine 接收通过 | 21 |
| video embedding | 原 ep / 同一 250615 | 实际 3000 ms、fps=1、1024 维，Engine 接收通过 | 10,363 |
| query embedding | 原 ep / 同一 250615 | 实际模型 250615、1024 维，query/index encoder ID 一致 | 19 |

三次 call_id 唯一、全部 completed；usage 回调 JSON 共 **10,403 Token**，均为 input，output=0。这里没有 SQL 写入，不能称三条 model_usage 新记录，也不与上一轮十条 142,374 或旧 profile 27 条 115,158 相加。
该轮结论是“**endpoint 身份修复和真实 text/video/query embedding 验收通过；当时完整默认流程尚未复验**”。本修复轮没有 SQL/ES mutation、Chat 或生成模型调用，`fullFlowPassed=false`；没有重跑默认 Parse V2 → ES 发布 → 检索 Chat，不提前标 U06 整体完成。
前一轮 `57085694-6167-4ca4-a5ef-e561144eebe2` 的 INDEXING_FAILED 为修复前历史，其模型身份阻断已经本轮源码与真实 I/O 证据替代；旧 EOF/fps guard 也不列当前问题。旧 `/private/tmp/anivag-usage-u06-20261009/` 和 `...-ms-20261009/` 均因 daemon recovery 不再存在，本文保留原路径和必要结果，不承诺旧文件当前仍可读。

收口范围：上述三次真实 I/O 证明 endpoint 路由与 canonical 编码接收；随后补齐的 repository scope、reparse 继承与指纹兼容由最终回归验证，没有追加 RPC 或把局部 I/O 当作 SQL/ES/Chat 验收。该轮 U06 保持未完成，后续完整默认验收见下节。文档/地图导出仅使用临时官方工具，不改变业务依赖，不新增 commit/push。

## 前轮 2026-10-09 532文件 + 四补丁单chunk U06 验收（历史）

**历史结论：该轮532冻结文件与四个记录补丁范围内，默认 Parse V2 → ES → 实际检索 Chat → SQL/API/UI 全链通过。** 独立实时审计 `status=passed`、`fullFlowPassed=true`、`failedChecks=[]`；U01–U06 完成，移除专用当前标记。结论不覆盖联调期间另行推进的 engine/common 迁移真实运行。

### 授权、版本与隔离

- 操作授权沿用用户“然后可以进行联调测试吧”及本轮修复后要求整体流程；任务内状态兼容与媒体单位最小修复已由 root 核对并执行。
- 初始版本为 `224cbbcdaf9e8edbe77c855ced1577bbf863e65d` 加开始时全部 dirty，`source-manifest.json` 冻结 **532 tracked** 文件；之后仅叠加 `source-patches.json` 的四文件。独立审计对补丁后冻结源核对零差异，配置 SHA-256 为 `fc41b30c74b22dd21bb37b01e12d7faa5c23062419c4ab1e034985dc0e9225c4`。
- 默认 `execution_mode=v2`，沿用配置的 pipelineVersion 名称 `m2-seconds`；名称不作为执行版本或媒体单位的判断依据。理解启用、原 ASR/视觉/embedding endpoint、视频 fps=1 与实际默认参数未降低或替换，`profileOverrides=false`。
- 专用 MySQL 数据库、ES index、Qdrant collection namespace 为 `anivag_usage_u06_fixed_20261009_024542_test`；workspace 为 `usage-u06-fixed-20261009-024542`。独立 API 9622 / Web 5174；原运行环境与生产数据未被改写。
- 本轮恢复既有 MySQL/ES/Qdrant 本机依赖与临时官方图导出工具，没有新增业务依赖；运行与测试产物均在 `/private/tmp/anivag-usage-u06-fixed-20261009/`。
- 联调期间并行 engine/common 迁移的 **45 tracked 变化** 与新增 common 目录全部保留，`parallelChangesIncludedInFrozenTestSource=false`；另一工作随后将迁移提交为当前 HEAD `c8c5d2cd9d28485dea0e5990de8813ef39a71a6f`。root 未执行该 commit/push，本轮四补丁仍 dirty；不得将冻结版结果写成最新迁移版已真实全链通过。

### 本轮最小源码与五产物范围

```text
AniVAG/
├── agent/
│   └── chat/
│       └── video_search/
│           └── skills/
│               └── [M] __init__.py          ← 既有三种 prompt 共享媒体毫秒合同
├── server/
│   ├── model/
│   │   └── vo/
│   │       └── [M] libraryVO.py             ← 既有 V1/V2 parse_status enum 联合
│   ├── design/
│   │   └── [M] model-token-usage.md
│   └── report_docs/
│       ├── [M] model-token-usage-stages-evidence.md
│       ├── [M] model-usage-handoff-20261008.md
│       ├── [M] model-token-usage-stages.excalidraw
│       └── [M] model-token-usage-stages.svg
└── tests/
    ├── server/
    │   └── [M] test_library_views.py        ← V1/V2 原状态回归
    └── agent/
        └── chat/
            └── [M] test_tools.py            ← prompt 媒体单位合同断言
```

只改两生产 / 两直接测试及既有五产物；四源码/测试文件合计 +53/-5（生产 +14/-5、测试 +39/-0），八列 Schema、usage/metadata、工具 DTO、实际时间数值与模型配置保持。状态修复解决实际 `/api/series/get` 的 V2 COMPLETE 校验 400，不转换原状态；媒体修复仅给 Query/Evaluator/Chat 既有 prompt 前置共享合同，没有给用户请求追加时间提示或扩大工具接口。

### 真实解析与两次 Chat

- 输入短片 SHA-256 `95ad4ab44f99547647c2789ad35073b34fe1720b8e036ed07dbfe407584ba610`；实际视频时长 **12095 ms**。默认任务 `07a4e4b1-5402-495d-a000-4ad0f6742aef` 首次 COMPLETE / lastError=null，parent/chunk/interval、发布回执和 ES 收口。
- 解析五调用均 completed：ASR **7,452**，视觉 **25,427**，refinement **21,936**，text embedding **1,062**，video embedding **10,366**，合计 **66,243 Token**。实际 ASR为 Seed Lite，视觉/refine为 Seed Pro；embedding SDK仍请求原 `ep-20260911212434-j4xcb`，实际 `doubao-embedding-vision-250615`、1024 维，semantic/vision vectors 均核对通过。
- ChunkParseContent `de27b168-9512-42fd-9259-3973a96c0e6e` 的 `detail._index_embeddings_v2.receipt` JSON 与唯一 ES 文档、Chunk `77940efa-7dce-5e41-aad6-b96f80f310b8` 对应；回执由 parse_task_id / video_chunk_id / content_version=2 / index_generation=1 标识，没有独立 receipt UUID。两次 Chat 的 SQL parseProvenance 均对应当前任务及同一回执。
- 首 Chat `796226d1-ad02-4ef2-8249-358286079bea` COMPLETED，三条真实 retrievalTrace 均 resultCount=1；**23 calls / 145,279 Token**。它把 12095 ms 自然语言误标成“秒”，该语义失败及真实用量保留。临时 driver 曾硬编码 pipelineVersion=v2；改为与实际 task.pipelineVersion 对照后仅 GET 原消息恢复判断，不产生新调用。
- 状态与媒体 prompt 补丁经临时 API 冷重启后，在新 conversation 用原默认请求独立执行 Chat `626e188f-3a12-4442-95f3-6350ea073a62`，无用户显式单位提示；COMPLETED，真实 retrievalTrace resultCount=1，命中同一当前 Chunk，**21 calls / 146,303 Token**。
- `media-time-fixed/media-time-verification.json` 实际回答 **0–12.095 秒（0–12095 毫秒）**，结构化视频范围仍 **0–12095 ms**；固定 key POST 重放的消息、答案、49 个 call_id 和用量均不变，不新增模型调用。

### SQL、API、页面与预算对账

| 实际 run | calls | input | output | total / known | missing |
|---|---:|---:|---:|---:|---:|
| 默认解析任务 | 5 | 31,837 | 34,406 | 66,243 | 0 |
| 首 Chat（错误单位历史） | 23 | 138,023 | 7,256 | 145,279 | 0 |
| 修复后默认 Chat | 21 | 138,700 | 7,603 | 146,303 | 0 |
| 全 workspace | **49** | **308,560** | **49,265** | **357,825** | **0** |

- 独立 `audit-final.json` 检查八列、49 个唯一 UUID call_id、49 completed、UTC 时刻、主 Token 包含关系、owner 与固定 JSON/白名单，全部通过；两 Chat 的 task_type 均为 chat_retrieval，40 生成调用 + 4 检索 text_embedding 都归各自实际助手 ID，五条解析调用归真实任务。
- SQL/API local overview 精确一致，9 模型分组、3 run；实际 workspace/run/series 精确筛选、错误作用域、系列前缀与半开时间窗口均通过。首轮用量未删除，也未把此前独立三 callback 的 10,403 Token 混入本轮账本。
- 解析 budget used_call_ids 等于 SQL 的五个 call_id，active 为空，终态预留释放；receipt/ES 的 semantic/vision vectors 与 Chat provenance 完全对应。
- `ui-verification.json` 为 passed：真实 5174 HTTP 页面显示 **49 / 357,825 / 308,560 / 49,265**；两视图复用同一 overview，实际 series 筛选为49条，手动刷新发新 GET 且 totals/models/runs保持；pageErrors=[]、httpErrors=[]。保存模型、任务、筛选与最终页面截图，没有 mock、拦截替换或额外网页 Chat 调用。
- 本轮没有实际 rerank 或 SDK 明细，未触发同名 run 跨类型/无 owner 分支；这些边界沿用现有离线直接覆盖，不把 operation 名称或未执行场景写成真实通过。更早未知/失败 Token 实例继续保留为独立历史证据。

### 离线及真实补验的范围

- V1/V2 状态兼容相关组 **134 passed / 1 skipped，13.54 s**；唯一跳过为未配置真实 MySQL 的消息 timing 用例。随后在独立 `anivag_usage_u06_fixed_20261009_timing_test` 库补跑同一用例，**1 passed / 0 skipped**（pytest 2.56 s、helper 总耗时 5.426 s），测试后消息/conversation/model_usage均0，主联调 namespace 未触碰；不是将原134组重计为同一次135。
- Agent 媒体合同相关回归 **152 passed / 1.28 s**，均为直接离线验证。
- 最新并行工作区七文件离线兼容 **293 passed / 27 skipped / 1 warning，11.46 s**；sourceChangedDuringTests=[]。关键五个 repository/service逐字节相同，迁移相关 AST/共享符号核对通过，测试补足导入兼容；真实全链仍仅证明上述冻结源码与四补丁。
- 前轮 endpoint **686 passed / 0 failed / 0 skipped，26.70 s** 与三 callback **10,403 Token** 保持历史局部资格；与本轮真实SQL全量49/357,825分开。没有新增 commit/push，原 dirty 和并行改动全部保留。

### 最终验收与生命周期

`acceptance-final.json` 为 **fullFlowPassed=true**，十一项检查均 true，SQL/API/UI 全量与上述49/357,825一致；补丁范围与冻结源零差异，最新并行迁移未混入。首次临时汇总脚本误读 `ui.final.overview.totals` 层级导致 KeyError，随后仅改汇总脚本字段路径并重跑通过，不改业务数据、聚合验证或源码。

- 临时 API **PID89122 / 9622** 于 **2026-10-09 03:54:07.722825 UTC** 以 SIGTERM 正常关闭，Application shutdown complete；原始证据 `api-stop-89122.json`。
- 临时 Web **PID43330 / 5174** 于 **2026-10-09 04:01:39.937 UTC** 以 SIGINT 正常关闭，exitCode=0，ps已无该PID；原始证据 `web-process.json`，其唯一自建会话也退出0。
- `cleanup.json` 捕获 **2026-10-09 04:02:11.259068 UTC**：9622/5174均不监听；恢复的基础 MySQL31407 / Qdrant31491 / ES31499 保持运行，3306/6333/9200均监听，testDataRetained=true。测试库/索引、输入、源码快照和审计证据保留，不关闭恢复的基础依赖。
- 本联调没有执行commit/push，本轮四补丁与五产物仍为本地修改；当前HEAD c8c5d2c来自独立并行迁移，原dirty与其余并行改动保留。

本轮地图以临时 `@excalidraw/excalidraw@0.18.1` 官方 exportToSvg 导出，同源86原生元素、73文本全部与SVG一致；SVG32path/112text/0image，viewBox2976×1708.5。六节点四项、责任与Ref完整，当前徽标已移除、U06显示验收通过；字段最小间距24、Agent/Ref最小26。实际渲染已检查中文、连线和全图，无裁切/重叠；65本地引用、围栏与空白核验通过，结果见本轮 `docs-verification.json`。

该历史轮原始证据入口：`/private/tmp/anivag-usage-u06-fixed-20261009/` 的 `environment.json`、`source-manifest.json`、`source-manifest-patched.json`、`source-patches.json`、`parse-result.json`、`library-api-verification.json`、`chat-result.json`、`media-time-fixed/chat-result.json`、`media-time-fixed/media-time-verification.json`、`audit-final.json`、`ui-verification.json`、`timing-mysql-tests.json`、`latest-workspace-compatibility.json`、`dependencies.json`、`acceptance-final.json`、`cleanup.json`、`api-stop-89122.json`、`web-process.json` 与 `baseline/`。

## 本轮 2026-10-09 538冻结文件双chunk U06 验收（统计与通路通过）

**结论：本地Token统计、保留默认Parse数据→修复后Media/Chat→SQL/API/UI系统通路，以及两个完整chunk播放通过；逐事件时间精度未通过。** `audit-multichunk-final.json` 为 passed / `fullFlowPassed=true` / failedChecks=[]，其资格限定统计和通路，不认证逐句定位质量。第二chunk当前version2的Interval模型updates在 **60004–60030ms的26ms** 内承载多句对白，合法整数/bounds不足以证明时间语义正确。Chat/DTO保存原值，旧version1已覆写且ASR原始证据未持久化，无法追认具体混入阶段；本轮不改历史SQL、不猜测乘1000、不新增prompt或模型重跑。后续应强化understanding/refine的毫秒与全局offset约束，并以真实reParse验收逐事件对齐。默认V2未登记keyframes，图片覆盖为 **unobserved**（registeredImages=0），没有宣称图片验收通过。

### 授权、冻结版本与准确执行时序

- 用户既有“然后可以进行联调测试吧”与修复后要求整体/多chunk流程的授权，覆盖独立服务、测试namespace、真实默认模型调用及实际缺口最小修复。文档Agent只维护既有五产物与真实页面证据，没有发Chat或模型、改源码、写DB/ES或新增业务依赖。
- 初始HEAD **c8c5d2cd9d28485dea0e5990de8813ef39a71a6f + 开始时全部dirty**，`source-manifest.json`冻结538tracked文件，已包含common迁移与前轮补丁；`source-patches.json`仅十路径，补丁后538哈希零差异，配置SHA256 `fc41b30c74b22dd21bb37b01e12d7faa5c23062419c4ab1e034985dc0e9225c4` 不变。
- 同期ROOT的V1/Qdrant清理未装入冻结服务，`parallelWorkspaceChangesIncluded=false`，完整ROOT未冒充真实范围。bootstrap与其测试因并行清理全文件字节不同，本轮必要接线补丁等价且各自哈希记录；不能宣称ROOT与SOURCE所有文件一致。
- MySQL/ES/Qdrant使用专用 `anivag_usage_multichunk_20261009_052927_test`，workspace `usage-multichunk-20261009-052927`，API9622/Web5174；复用既有基础依赖，原运行环境/生产数据不改写。
- 默认execution_mode=v2、配置名称m2-seconds；理解启用、原ASR/视觉/embedding endpoint、fps=1及60000ms chunk设置未替换或下调，`profileOverrides=false`。canonical实际250615/1024维与SDK route仍区分，从根config.yaml配置的Gateway实际模型绑定冻结。
- **Parse先于本轮修复完成**：05:30:30开始，05:57:33UTC parent COMPLETE，约27分钟，靠既有重试/Seed恢复；17调用含schema400未知与其他失败。没有在十补丁定版后重新Parse。
- schema/media修复后的第一次同namespace冷重启为06:03:16.866270UTC；首Chat只回答opening18秒。Supervisor修复后的第二次冷重启为06:32:56.028867UTC，保留全部Parse/SQL/ES数据；新Chat评估length失败后，用同原请求/默认配置再次新conversation成功。第二/三Chat之间无新增生产补丁或配置修改。
- DeepSeek schema修复以真实Provider Responses **VideoParseResult text-only probe1call/2740Token**与unit35验证，不能把该probe当完整视觉Parse重跑；它无owner、不写主SQL/预算，独立诊断不混99主账。

### 本轮十源码/测试与五产物修改树

```text
AniVAG/
├── agent/
│   └── chat/
│       └── [M] supervisor.py                ← 原完整user_message保留；toolquery独立传递
├── gateway/
│   └── openai/
│       └── [M] multimodal.py                ← DeepSeek Responses tagged union转允许anyOf
├── server/
│   ├── [M] bootstrap.py                    ← 既有V2 repository注入媒体服务
│   ├── repository/
│   │   └── [M] video_parse.py              ← 读取当前内容版本与V2媒体证据
│   ├── service/
│   │   └── [M] media.py                    ← 按既有V2当前快照返回媒体
│   ├── design/
│   │   └── [M] model-token-usage.md
│   └── report_docs/
│       ├── [M] model-token-usage-stages-evidence.md
│       ├── [M] model-usage-handoff-20261008.md
│       ├── [M] model-token-usage-stages.excalidraw
│       └── [M] model-token-usage-stages.svg
└── tests/
    ├── agent/
    │   └── chat/
    │       └── [M] test_supervisor.py       ← delegated query不覆盖完整请求
    ├── gateway/
    │   └── [M] test_models.py               ← provider允许schema与业务literal/属性保持
    └── server/
        ├── [M] test_bootstrap.py            ← V2媒体接线
        ├── [M] test_media_views.py          ← 当前V2文本/事件媒体输出
        └── [M] test_video_parse_repository.py ← 当前内容/范围与V1兼容
```

自己的baseline计算：**五生产+106/-10、五测试+264/-11，总十文件+370/-21**；整体gitdiff夹杂并行清理，不用作本轮计数。维护既有五产物，不新增第六份文档。保留八列Schema、usage/metadata、正式DTO、实际模型/采样配置、V1兼容；Supervisor不新增RunInput字段或改缓存/鉴权。DeepSeek适配仅provider分支降级discriminated oneOf/discriminator为其允许的anyOf，业务字段/literal保持。媒体修复复用V2 repository，不造新表。

### 真实双chunk解析与三轮Chat

- 输入 `/private/tmp/anivag-usage-multichunk-20261009/u06-current-90s.mp4`，SHA256 `ddbecf396cb32193e99c5168428adf97b6cdd6627103d534a69dc0da36f2e4b5`，实际 **90132ms（90.132秒）**；task `559ce12a-ea72-4d6d-867e-e7261e21be60`、parent/两child/Interval全部COMPLETE。seq0 retry1、seq1 retry2，保留原默认重试失败记录。
- 两chunk为 `6df3b05e-4fbc-53ed-b180-c60658a27d13` 的0–60000ms与 `4f9b3ace-864f-5eee-9f11-4e10706b05d0` 的60000–90132ms；当前contentVersion分别1/2，indexGeneration均1。两回执为当前内容行内JSON，无独立receiptUUID，ES两文档/双向量/Interval indexed_versions/Chat provenance全部对应。
- seq0/seq1当前timeline分别38/14事件，全局整数毫秒、scope[0,1]、scopeRange[0,90132]与第二段60000offset/bounds通过；**逐事件语义精度失败另列于首结论**，不能从38/14或bounds推断全部内容准确。
- 第一轮Chat `4eaf1bd4-dd34-4eb8-8c82-9c9568b6fd73` 业务COMPLETED、trace可检索双chunk，最终只有opening0–18000ms一个render；strict原key重放的allCurrentChunksInChatHits失败，其他10checks通过。累计52calls/known704336/missing2仅部分结果，先前permissive driver passed不等于严格双段通过。
- 第二轮 `981d32fb-ce03-447a-b111-d67bf09c31e9` 业务COMPLETED，但完整内容评估length失败，driverpassed=false且无最终双段hit/render；累计71calls/known906357/missing2保留。备用Ark failed0tokens来自真实provider_usage，错误细节未留日志，**不推断同为length**。
- 最后用同原请求/默认配置新conversation `5c03543b-a171-4d32-8d03-96a80c851c9f`，assistant `a9f70ef3-5151-43f8-8cd9-c04de4e6f0bb`，于06:45:58.576282UTC成功：COMPLETED/passed=true，两个当前chunk最终hit与两个videoRender **[0,60000]/[60000,90132]ms**。回答90.132秒/60秒分段正确，证明整段范围/换算，不认证每句语义时间。
- `intent-retry/chat-replay-verification.json` **12checks通过**：原POST固定key保持消息ID、答案/renderBlocks、99callids与SQLpayload、API用量完全不变；两chunk最终hit/render、实际证据GET200、全局文本事件边界与播放HTTP206（bytes0–255/9999051,video/mp4）通过。`intent-retry/semantic-answer-review.json`对已存解析数据的双段人物/画面/动作需求覆盖passed，eventPrecisionCertified=false；完整内容覆盖不是原音轨人工真值认证。`timeline-precision-proof.json`保存当前version2、validated refine update和DTO的60004–60030ms一致证据，起源层未能追认。registeredImageCoverage=unobserved，未自动创建keyframes。

### 主账、未知用量、真实API与页面

| 实际run | calls | 已知Token小计 | missing | 资格 |
|---|---:|---:|---:|---|
| 默认Parse（补修前完成） | 17 | 402,676 | 2 | total/input/output=null；两schema400保持未知 |
| 首Chat（opening18秒部分回答） | 35 | 301,660 | 0 | 严格双段覆盖失败，留账 |
| intent-fixed Chat（length评估失败） | 19 | 202,021 | 0 | 无最终双段结果，留账 |
| 原请求默认fresh retry Chat | 28 | 298,496 | 0 | 双chunk命中/播放与原key重放通过 |
| 全workspace | **99** | **1,204,853** | **2** | **公开input/output/total均null** |

- 八列、99唯一UUID、UTC、固定usage/metadata白名单与owner全部通过；92completed/7failed，失败的已知 **175,035Token** 保留。原始已知input1023503/output181350仅诊断可加小计，**不是API完整input/output总数**。未知两个调用不写0；零用量失败若真实provider返回0则保持0。
- Parse17归真实父task；三Chat共82条task_type=chat_retrieval，77生成+5检索embedding归各自实际assistant。agent_chat/stage/category不是新增task_type；metadata无正文/连接串/凭据。没有真实rerank或SDK明细/无owner主账覆盖。
- 主budget used_call_ids精确等于17条SQL，active为空、lease与终态预留释放；两receipt/ES/dualvectors及当前版本一致。schema独立probe2740与前轮49/357825、endpoint callback10403不混主账。
- SQL/API为local、99/known1204853/missing2、11模型/4run，12项真实筛选（workspace/run/series精确、错误scope/前缀/半开时间窗）通过；缺失时input/output/total均null，SDK/截断等未真实触发分支沿用离线测试。
- `ui-verification.json`通过9项实际HTTP检查：99次/已知Token小计1,204,853，输入输出“—”、missing2提示；模型及run每行值对应实际overview，两视图不重复请求，series筛选99条，刷新新GET后totals/models/runs稳定。pageErrors/httpErrors均空；保存`ui-models.png`、`ui-runs.png`、`ui-series.png`、`ui-final.png`。无mock/interception、未触发网页Chat额外模型。

### 离线、真实诊断与未扩大范围

- 初始538冻版23文件 **894passed/1skipped/1warning，27.32s**，sourceChangedDuringTests=[]。唯一skip为未配置真实MySQL的timing用例，随后独立 `anivag_usage_multichunk_20261009_timing_test` **1passed/0skip，helper5.485s**；消息/conversation/model_usage均0，主namespace不变。不把二者算作一次895回归。
- schema适配 **35passed/0fail/0skip，2.72s**；真实DeepSeek Responses VideoParseResult **text-only** probe1call **2684input+56output=2740Token**，无owner/SQL/预算，只证明此provider schema接受与业务校验，不证明完整Parse重跑。
- 冻结媒体四文件 **158passed/0fail/0skip，helper13.832s**；Supervisor三文件 ROOT **154passed/1.49s**、冻结SOURCE **152passed/1.26s**，两边已有基线不同，分开报告；各自修复前直接用例失败证明回归必要。
- 同期最新ROOT六文件 **195passed/0fail/0skip/1warning，19.96s** 仅离线兼容；ROOT并行V1/Qdrant清理未全真实验收。`independent-code-review.json`对本轮五生产/五测试无程序阻断，538+十补丁SHA与默认配置再次一致；全流审计仅此范围以及上述先Parse后补修的实际时序。
- 前轮532+4单chunk **49/357825/missing0**、其293/27离线common证据、endpoint **686passed/三callback10403** 与更旧未知/失败结果保留各自历史资格，不删除用量或改历史事实。

### 五产物与运行收尾

本轮维护仅既有三Markdown、同源Excalidraw与官方SVG；设计文件仍被`**/design/`忽略，四report_docs文件仍为既有untracked，不能从gitnumstat遗漏它们。官方exportToSvg、原生文本/Ref/边界/空白、真实渲染及本轮baseline增删计数见`docs-verification.json`。没有新增commit/push，原dirty及并行改动保留。

- 最终`acceptance-final.json`为 **passed_for_usage_system_flow_with_event_precision_limitation**，七项检查全部true，fullFlowPassed=true限定本节统计/系统通路，**eventPrecisionCertified=false**、cleanupCompleted=true。首次临时汇总脚本误读ui.final.totals导致只读KeyError，改为真实ui.final.overview.totals后通过；未改业务数据、模型、聚合或源码。
- 唯一临时API **PID7827/9622** 于 **2026-10-09 07:00:09.322656UTC** 以SIGTERM关闭，日志Application shutdown complete；统一会话exit143是SIGTERM终止，不是应用异常，原证据`api-stop-7827.json`。
- 唯一临时Web **PID59930/5174** 在核实命令为本轮run-web.mjs后，由收尾Agent于 **07:07:53.231286UTC** 发送SIGINT，**07:07:53.234UTC exit0**；PID消失、5174关闭，`web-stop.json`/`web-process.json`记录，没有停止其他进程。
- `cleanup.json` **2026-10-09 07:08:24.967914UTC** 确认9622/5174关闭、3306/6333/9200继续监听，reusedDependenciesLeftRunning=true、testDataRetained=true；没有删测试库/索引/媒体/快照/99条统计证据，没有新增commit/push。

本轮官方 `@excalidraw/excalidraw@0.18.1 exportToSvg` 已完成；86稳定元素、73原生文本均与SVG同步，32path/112text/0image，viewBox2976×1708.5。六节点四项/责任/Ref锚点完整，统计阶段无当前标记，U06专用徽标为“✓统计通过”并明确逐事件精度未通过；字段最小间距24、Agent/Ref最小26。66本地Markdown引用、围栏/空白、native/SVG边界及gitdiffcheck通过；已实际查看全图与真实UI最终截图，中文/连线完整，无裁切重叠，SHA与本轮五文件baseline增删记录在`docs-verification.json`。文档维护前后ROOT538tracked哈希无额外变化，未写业务源码。

当前原始证据入口：`/private/tmp/anivag-usage-multichunk-20261009/` 的 `environment.json`、`source-manifest.json`、`source-manifest-patched.json`、`source-patches.json`、`parse-result.json`、`chat-result.json`、`chat-replay-verification.json`、`intent-fixed/chat-result.json`、`intent-retry/chat-result.json`、`intent-retry/chat-replay-verification.json`、`intent-retry/semantic-answer-review.json`、`timeline-precision-proof.json`、`audit-multichunk-final.json`、`ui-verification.json`、`offline-tests.json`、`timing-mysql-tests.json`、`own-schema-tests.json`、`deepseek-schema-probe.json`、`media-tests.json`、`own-supervisor-tests.json`、`root-focused-tests.json`、`independent-code-review.json`、`independent-final-review.json`、`acceptance-final.json`、`cleanup.json`、`api-stop-7827.json`、`web-stop.json`、`web-process.json` 与 `baseline/`。

## 2026-10-09 ASR 秒制模型输出与程序毫秒转换修复

本轮目标来自用户明确“输出秒”，承接既有开发/真实联调授权。开工核对只负责ASR输出边界与下游单位说明；按本轮baseline的git numstat，实际生产两文件+35/-3，未超过小改动门槛。ASR接收私有strict、有限、非负秒数响应，允许整数及小数；公开TranscriptCue/TranscriptAnalysis不变，复用normalize_transcription一次完成乘1000、round、排序和片段末尾裁剪，Server仍加一次analysis-window偏移。OpenAI Audio入口、V1缓存及clamp helper未改，candidate_transcript operation与usage observer沿用。毫秒只统一存储单位，不提高模型时间精度。

```text
AniVAG/
├── engine/
│   └── common/
│       └── [M] transcription.py            ← 私有秒响应、ASR prompt及一次归一化
├── agent/
│   └── video_parse_v2/
│       └── [M] prompts.py                  ← 理解/精修保持已归一的全局毫秒
├── server/
│   └── report_docs/
│       ├── [M] model-usage-handoff-20261008.md
│       └── [M] model-token-usage-stages-evidence.md
└── tests/
    ├── engine/
    │   ├── common/
    │   │   └── [M] test_transcription.py    ← 秒制响应与转换边界
    │   └── parsev1/
    │       ├── [M] test_runtime.py          ← 实际ASR provider fake合同
    │       ├── [M] test_parse_usage.py      ← operation归属与新响应类型
    │       └── [M] test_video_chunk_parse.py ← raw秒/domain毫秒精确断言
    ├── agent/
    │   └── video_parse_v2/
    │       └── [M] test_framework.py        ← 首/后chunk交接与精修保留时间
    └── server/
        └── [M] test_video_parse_worker.py   ← 真实builder转换一次/偏移一次
```

共十个既有文件：两生产、六测试、两文档。按git numstat测试+172/-51；V1三测试是在共用ASR响应合同改变后追加的必要fake适配，不扩大生产范围。逐文件开工baseline、538 tracked SHA和增删留于当前证据目录；所有本轮八个tracked修改都在目标内，未碰其余既有dirty。两文档原为已有untracked产物，这里M表示相对本轮baseline的修改，并非新建文件。model_usage八列、usage/metadata JSON、正式API/SQL/domain单位、配置/模型/fps均不变；未迁移、改历史数据、启动/重启服务或commit/push。

### 必要离线验证与独立审阅

- `pytest -q -rs --tb=short tests/engine/common/test_transcription.py tests/server/test_video_parse_worker.py`：修复前定向 **10 failed/13 passed/20 deselected**，修复后完整 **43 passed/0 failed/0 skipped**。覆盖小数1.2346→1235ms、整数秒、空/零宽/越界、非法及非有限数；实际Ark/OpenAI/none builder覆盖 **1.5秒→1500ms→加12000ms得到13500ms**，结束4秒按3000ms窗口裁剪到全局15000ms。
- `.venv/bin/pytest -q tests/agent/video_parse_v2/test_framework.py --tb=short`：修复前定向 **4 failed/4 passed/7 deselected**，修复后完整 **15 passed/0 failed/0 skipped**。理解/refine/ASR-only的首/后chunk保留全局 **61000–63000ms**，输入快照和提交原值不变；prompt明确legacy字段单位与禁止重复换算/offset。
- `.venv/bin/pytest -q tests/engine/parsev1/test_runtime.py tests/engine/parsev1/test_parse_usage.py tests/engine/parsev1/test_video_chunk_parse.py --tb=short`：旧provider fake实际 **3 failed/30 passed**，必要适配后完整 **33 passed/0 failed/0 skipped**。raw响应 **0–2秒** 与原domain/缓存 **0–2000ms** 分开；精确相等断言补足原先仅看文本的盲点。
- 三组六文件不重叠，合计 **91 passed/0 failed/0 skipped**。43组harness显式清除测试database环境变量并使用测试fixtures；15/33组为fake runtime，不访问业务SQL。三组均无真实模型请求，记录每次命令、日志、source SHA与稳定性。`final-code-review.json`独立只读审阅passed、无阻断，公开类型/音频入口/normalizer/clamp的AST及其他原dirty保留，未重复计数历史894组。

### 同原片第二段真实ASR局部诊断

在当前ROOT调用原冻结ASR绑定 `ep-20260911212241-gg8bm`、fps1、timeout600，实际provider为 `doubao-seed-2-0-lite-260428`。输入与前轮同原片SHA `ddbecf396cb32193e99c5168428adf97b6cdd6627103d534a69dc0da36f2e4b5`，仅提取原seq1 analysis-window **60000–90132ms**，实际clip30.131995秒，音轨保留。沙箱首次域名被阻止、没有形成provider结果文件；按工具要求获得网络执行权限后完成一次真实请求，未改配置或业务环境。

- 模型返回14句片段内秒数；首句 **4.5–5.1秒→4500–5100ms**，下一句 **5.6–9.1秒→5600–9100ms**。诊断加60000偏移后分别 **64500–65100ms、65600–69100ms**，整体投影 **64500–90132ms**，不再将多句对白压成26ms。全局投影是诊断算术；实际Server偏移已由worker离线测试覆盖，此probe没有经过Server完整Parse提交。
- 第14句原末尾30.3秒超出30.132秒窗口 **168ms**，既有normalizer裁为30132ms。`real-asr-probe.json`原总状态 **failed_checks** 与 `rawSecondsWithinClip=false`完整保留，未改写、放宽阈值或重试掩盖。另行`real-asr-probe-review.json`区分：七项调用/operation/精确转换/裁剪后范围/多句跨度/source稳定检查通过，**conversionContractPassed=true**；原raw边界观察failed、精度未认证。秒制估计和程序裁剪符合本次合同，不声称模型输出处处精准。
- 唯一调用ID **46539b01-af91-42ce-b271-e04f290740c0**，request `20261009154046ELCn54sANvpXzXTW52H7`，completed/candidate_transcript，07:40:46.912–07:41:16.821UTC；用量 **13027 input +1575 output=14602 Token**，独立诊断无host owner/SQL/预算，旧99条账本未变。实际转写请求一次，无理解/精修/embedding/Chat/UI或服务启动。

该局部修复轮证据限定“模型输出秒、程序一次转换为毫秒与一次偏移合同”；当时 **fullParseRerun=false、eventPrecisionCertified=false、historicalTimelineUpdated=false**。旧version2的60004–60030ms没有猜乘1000回填，不能将该ASR14句结果追认为旧ASR原证或已经修好旧Interval。后续完整新Parse已在下一节单独重验；本局部轮未创建外部TODO或转交未授权环境操作。

当前证据目录 `/private/tmp/anivag-asr-seconds-20261009/`：`baseline.json`/`baseline/`、`status-before.txt`、`asr-tests-before.json`/`.log`、`asr-tests-after.json`/`.log`、`prompts-tests-before.json`/`.log`、`prompts-tests-after.json`/`.log`、`v1-tests-before.json`/`.log`、`v1-tests-after.json`/`.log`、`asr-own-test-delta.json`、`asr-independent-review.json`、`final-code-review.json`、`real-asr-probe.json`、`real-asr-probe-review.json`及`second-window.mp4`。旧双chunk主账、审计与阶段地图保持其统计/整chunk通路已通过、逐事件精度未认证的资格。

## 2026-10-09 秒制 ASR 修复后，同原片默认完整 ParseV2 重跑

**本次新Parse系统通路及秒级时间一致性独立验收通过，毫秒音轨真值精度未认证。** 用户明确“现在调整后重跑看一看”，开工按既有check-out/hypnos-code/项目门禁及plan-map核对；已有真实隔离联调授权涵盖新测试库、索引、临时API及真实模型调用。U01–U06统计完成状态不回退。本轮只执行与留证，不改生产代码、配置、测试或八列model_usage/usage/metadata合同。

实际服务为 `c8c5d2cd9d28485dea0e5990de8813ef39a71a6f + dirty` 的 **539个实际源码文件**：535个存在的tracked文件及4个已有untracked Python文件，另保留3个tracked删除路径。新ASR秒响应/归一化和understanding/refine提示词、当时V1/Qdrant清理已纳入。`source-manifest.json`与冻结目录逐字节一致；`environment.json`确认 `previousDefaultConfigEqual=true`、`profileOverrides=false`，原模型、fps1、600秒请求超时、三次默认尝试及已有DeepSeek fallback不变。运行期间ROOT继续发生并行修改，08:58核对26路径，另留`root-during-run-changes.json`；它们全部保留，未加载到冻结服务，不以本次通过追认后续ROOT全量验收。调整的两个生产文件仍匹配本次快照。

本轮预计与实际修改树一致，只有两个既有文档，M表示相对开工baseline更新；所有运行/观测/审计助手仅在`/private/tmp`：

```text
AniVAG/
└── server/report_docs/
    ├── [M] model-usage-handoff-20261008.md       ← 新重跑入口与历史范围
    └── [M] model-token-usage-stages-evidence.md  ← 各阶段原值及独立验收
```

### 真实运行与各阶段核对

- namespace `anivag_asr_reparse_20261009_081508_test`，workspace `asr-reparse-20261009-081508`，API9622；复用3306/9200/6333基础服务，本次不依赖或创建Qdrant集合。输入仍是前轮`u06-current-90s.mp4`，SHA `ddbecf396cb32193e99c5168428adf97b6cdd6627103d534a69dc0da36f2e4b5`、duration90132ms，原默认切成0–60000/60000–90132ms。
- parent `d3046375-9d70-4a8d-972d-98fd6429f519`，video `ebb4776a-95f3-5680-96e2-9c5efe369de0`；上传08:16:08UTC后由实际API启动，观察到08:53:01.315492UTC COMPLETE，约36分53秒。两个child和Interval `37082311-1582-4e09-94c5-833a3f4ef8ff`均COMPLETE，父任务attemptCount1、seq0 retry_count2、seq1/Interval retry_count0；观察等待界限未被当作业务失败，未重启任务或临时改变策略。
- 临时七处pass-through观测点各调用原逻辑一次并返回原值，完整保存raw Ark JSON、actual ASR全局返回、理解输入/成功返回、Chunk提交、精修前成员版本/内容、proposal、validated updates及提交。38条观测，无观测错误；未另装usage observer、生成额外call或修改预算。私有局部毫秒值是actual global减offset与raw算术推导，未冒称存在独立local-return hook；DeepSeek成功返回在Runtime层观测，无该provider raw JSON hook，范围明确保留。`observer-independent-review.json`独立核对冻结/默认配置/封装与已观察转换通过。
- ASR四次实际返回（seq1一次、seq0三次）均与独立`round(seconds*1000)`、片段裁剪及一次offset计算完全对应。seq1实际13句，首句4.4–5.1秒→global64400–65100ms，末句29.6–30.3秒→89600–90132ms；raw超出片段168ms仍记录为`rawBoundaryWithinClip=false`，程序裁剪精确，不写成模型raw全在范围内。
- seq1于08:26:09UTC成功理解/提交28事件，其中13DIALOGUE。seq0最后一次ASR为15句，由同lease3的DeepSeek成功理解/提交15事件、14DIALOGUE，于08:46:58UTC完成；失败理解raw不当成成功提交。seq0三次主模型失败分别是：首raw schema引用未登记`Kaname Raana (hand)`（call84a7dc7a.../60138Token）、第二次`SSEProxyError/length`（f1e81ad4.../60250）、第三次`ContentFilterError/content_filter`（7e9a4f73.../47132）；第三次后既有DeepSeek fallback completed39978Token。第二次输出32768，其中reasoning29016，长度失败没有完整raw JSON，不补造结果；首schema失败raw及独立重校验单独留存。
- Interval实际08:47:00UTC开始、08:52:44.147849UTC提交。精修仅更新seq1 persons/timeline，内容版本1→2；原28个事件的所有起止值逐项保留，seq0 version1保持。独立比较从pre-refine输入到proposal/validated updates再到SQL/DTO的实际值，确认无重复乘除1000、加offset或归零；更新前内容已在观测中保存，未因v2覆盖而丢失原证。

第二段的真实时间链路示例（秒数为ASR估计，SQL/API数值为毫秒）：

| 片段内ASR秒 | 加60000ms偏移后的毫秒 | 理解、精修、SQL及DTO |
|---|---|---|
| 4.4–5.1 | 64400–65100 | 起止值一致 |
| 6.0–9.1 | 66000–69100 | 起止值一致 |
| 9.7–11.7 | 69700–71700 | 起止值一致 |
| 29.6–30.3 | 89600–90132 | 按片段末尾裁剪后起止值一致 |

### 独立最终验收、账本与生命周期

`audit-parse-timing-final.json`实际`--full`只读SQL/GET/ES审计exit0：**status=passed、parseSystemFlowPassed=true、parseTimingEstimatePassed=true、fullParsePassed=true、failedChecks=[]**。SQL/API为 **14calls / 443098Token / missing0**，input295070/output148028，11completed/3failed；失败167520全部留账。四ASR94686、五understanding253369（含默认fallback）、一次refine32510、四embedding62533，合计一致。九个真实usage筛选场景含精确workspace/run/series、错误scope、series前缀、半开时间窗口均通过；owner与白名单、八列Schema、used14 IDs精确匹配SQL14、active为空，parent/prefix/两child/Interval lease均释放。

两当前content均有receipt和ES文档，contentVersion1/2与generation1一致，两个text和两个video向量各1024个有限值，actual mapping为float的dense_vector，逐维float32 bits相同。首exact-double审计曾有唯一失败：seq0视频向量第12维SQL `0.00011730194091796876`、ES `0.00011730194091796875`，差1 double ULP；1023维double相同、1024维float32 bits均相同。该首报与现场快照保留为`audit-parse-timing-exact-double.json`/`audit-live-state-exact-double.json`，`vector-precision-review.json`保存实际mapping及逐维核对；仅修正临时审计的存储精度合同，无容差放宽、源码/SQL/ES修改。

`media-verification.json`两chunk各七项真实GET/播放检查通过：DTO身份、0–60000/60000–90132ms范围、duration、字幕/事件全局整数毫秒、ready及Range HTTP206。`audit-parse-timing-final.json`的秒级估计检查使用原文匹配、2秒端点容许量及20倍异常压缩诊断，完整Chunk与实际精修覆盖是必要门槛；seq0最后ASR15句匹配14句，短句`はい。`0–600ms未独立匹配而不认证；seq1 13/13匹配，SQL/DTO全部13句从64400到90132ms，整体跨度25732ms，独立报告的24700ms仅为其中10条substantive匹配的跨度，无旧26ms压缩。**millisecondAudioPrecisionCertified=false**；动作语义/未匹配短句和逐句音轨精确真值未因上述通过而认证，模型秒级估计与程序毫秒存储分开。

旧99条账本在运行前后独立SELECT读取，`original-ledger-before.json`/`original-ledger-after.json`均passed、payload SHA `abab0528c8c610001ce3b1cbdf6421e0186a2c0426b54626c626f42ea4a425a9`相同；旧60004–60030ms未回填。本次443098不混旧1204853小计或前轮ASR-only14602，本次无新Chat/UI验收、无keyframes注册（图片覆盖unobserved）、无额外诊断模型call。

API **PID69657/9622** 于 **08:58:21.174946UTC** 验明本轮进程后SIGTERM退出；`cleanup.json`08:58:21.694234UTC确认9622关闭、3306/9200/6333仍监听、testDataRetained=true、reusedDependenciesLeftRunning=true。未删除测试库/索引/媒体/证据，未新增commit/push。文档本轮baseline、引用/围栏/空白、增删及独立交接审阅分别留在`baseline/`、`docs-verification.json`与`independent-docs-review.json`；并行ROOT变化另记，不以冻结服务通过宣称后续ROOT已验收。

本轮原始入口 `/private/tmp/anivag-asr-reparse-20261009/`：`preflight.json`、`environment.json`、`source-manifest.json`/`source/`、`observe-stages.py`/`stage-observations.jsonl`、`observer-independent-review.json`、`parse-identities.json`/`parse-history.json`/`parse-result.json`、`state-latest.json`、`understanding-schema-diagnosis.json`、`audit-live-state.json`、`audit-parse-timing-final.json`、`audit-parse-timing-exact-double.json`、`vector-precision-review.json`、`media-seq0.json`/`media-seq1.json`/`media-verification.json`、`original-ledger-before.json`/`original-ledger-after.json`、`root-during-run-changes.json`、`cleanup.json`、`docs-verification.json`、`independent-docs-review.json`。
