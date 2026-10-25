# AniVAG model_usage 统计替换开发 handoff

初始日期：2026-10-08；更新日期：2026-10-09。项目根目录：`/Users/bytedance/workspace/other-project/AniVAG`。
原始源码改造基准：`main@c4b4cbc`，含当时已有未提交修改；前轮双chunk基准为 c8c5d2c + 开始时全部dirty的538tracked冻版（包含common）与十个必要补丁，实际Parse先于补修完成，随后验证Media/Chat。同期V1/Qdrant清理未纳入该真实服务；接手时重新核对，不reset或覆盖。

## 当前续接入口：秒制 ASR 修复后完整 ParseV2 重跑通过

2026-10-09 按用户“现在调整后重跑看一看”指令，用同一 **90132ms** 原片与原默认配置完成新 ParseV2：task `d3046375-9d70-4a8d-972d-98fd6429f519` 的 parent、两 child 与 Interval 均 **COMPLETE**，观察终态时间 **08:53:01.315492UTC**。本次服务加载开工时 `c8c5d2c + dirty` 的 **539 个实际源码文件**，含4个已有untracked Python文件、保留3个tracked删除路径；调整后的ASR/understanding/refine协议及当时V1/Qdrant清理已纳入。`previousDefaultConfigEqual=true`、`profileOverrides=false`，模型/fps/超时/重试/fallback均用原默认值，没有重设计八列Schema。

真实ASR返回片段内秒数，程序乘1000/round/裁剪后只加一次全局offset。第二段13句首条 **4.4–5.1秒→64400–65100ms**，后续例如 **66000–69100ms、69700–71700ms**；理解、精修、最终SQL和媒体DTO对应。精修只更新seq1人物与timeline，28个事件的起止时间逐项保留；全部13句对白从64400到90132ms，横跨 **25732ms**（独立报告中10条substantive匹配的跨度为24700ms），本次没有再挤在旧26ms内。末句raw估计30.3秒超出30.132秒片段168ms，实际被既有转换器裁至90132ms，此估计越界仍留证。

独立 `audit-parse-timing-final.json` 为 **passed / fullParsePassed=true**：两receipt/两ES文档与当前contentVersion1/2、generation1一致，text/video共四向量均为1024个有限值；两媒体GET保留SQL毫秒值，实际播放Range均HTTP206。SQL/API为 **14calls / 443098Token / missing0**（input295070/output148028），11completed/3failed；失败 **167520Token** 全部留账，budget used14精确等于SQL14、active为空、各lease已释放。第一段三次主模型失败依次为未知人物引用（60138）、`length`（60250）、`content_filter`（47132）；最后一次由既有DeepSeek fallback成功完成（39978）。这次通过依赖默认重试/fallback，不意味着模型首次调用稳定。

验收范围为完整Parse系统通路及相对ASR的秒级时间一致性：第一段最后一次ASR15句匹配14句，短句`はい。`（0–600ms）未独立匹配，明确不认证；第二段13/13匹配。**毫秒音轨真值精度仍未认证**，毫秒是统一存储单位。本次未重新验证Chat/UI，也未改写旧60004–60030ms或前轮99条账本；后者读前/读后payload SHA相同。向量首报的exact-double比较有1 ULP表示差异，实际mapping的float存储逐维bits一致后审计通过；首份失败报告与精度核实证据保留，未修改业务数据来通过审计。

真实操作沿用已有隔离联调授权，namespace `anivag_asr_reparse_20261009_081508_test`、workspace `asr-reparse-20261009-081508`，未创建Qdrant集合。API69657/9622于 **08:58:21UTC** SIGTERM正常关闭，3306/9200/6333继续运行，测试库/索引/媒体/证据保留。本轮仅回写本handoff和证据索引，无生产源码改动、commit或push。运行期间ROOT出现后续并行修改（08:58核对为26路径），全部保留且未加载到这次冻结服务；调整的ASR及prompts仍与运行快照一致，不能把此验收扩称后续ROOT的全量验收。证据目录 `/private/tmp/anivag-asr-reparse-20261009/`，详细阶段原值、失败/账本、审计与生命周期见[证据索引最新节](model-token-usage-stages-evidence.md)。

## 前轮 ASR 输出边界修复与局部诊断（完整重跑前历史）

2026-10-09 按用户“输出秒”要求，已修复当前 ROOT 的 ASR 输出边界。默认 VideoASRAgent 使用私有模型响应类型接收片段内秒数，允许整数秒和小数秒；复用既有 normalize_transcription，将秒数乘1000并round、裁剪到片段范围，返回原有整数毫秒 TranscriptAnalysis。Server仍只加一次 analysis-window 起点偏移。公开 start_seconds/end_seconds 旧字段名沿用，但domain、SQL及API的单位仍是毫秒；毫秒存储不代表模型具备毫秒识别精度。OpenAI Audio 入口原本已有秒→毫秒转换，本轮未改。

understanding/refine prompt已明确输入是归一后的原视频全局毫秒，禁止重复乘除1000、加offset或归零；人物/引用精修应保留原时间范围，除非证据支持时间修正。按本轮baseline的git numstat，生产仅两个文件+35/-3，六个直接测试+172/-51；既有本handoff与证据索引补记，原dirty完整保留，八列model_usage Schema、usage/metadata、模型配置及fps不变。

离线六文件分三组完整 **43+15+33=91 passed，0 failed，0 skipped**；此前ASR/交接/V1合同分别实际出现10/4/3个定向失败，修复后通过。独立代码审阅passed，无阻断。真实ASR诊断使用前轮原片的第二段 **60000–90132ms** 与原冻结ASR绑定/fps1，得到14句秒制对白；首句 **4.5–5.1秒→4500–5100ms**，诊断全局投影 **64500–65100ms**。最后一条模型估计到30.3秒，超过30.132秒片段168ms，被既有转换器裁至30132ms；原始probe的raw边界检查failed仍保留，七项转换/裁剪/调用/范围检查通过，不改写为全项通过。该独立一次candidate_transcript调用 **14,602 Token** 不写SQL，不混前轮99条账本。

该局部修复轮当时没有重新执行完整Parse/understanding/refine、没有改旧60004–60030ms时间或认证逐句对齐，也未启动服务、迁移、commit/push；后续完整新Parse已在上节单独验收，不能用本节ASR局部结果替代它。该轮代码/测试基线、真实raw秒/转换后毫秒、失败边界和独立审阅在 `/private/tmp/anivag-asr-seconds-20261009/`，详见[证据索引](model-token-usage-stages-evidence.md)。保留下述历史统计结论和原始证据。

## 前轮双chunk统计验收入口

**U01–U06本地Token统计已完成；双chunk统计/系统通路与整chunk播放验收通过，逐事件时间精度未通过。** 原默认90132ms媒体按60000ms拆两chunk，Parse于05:57:33UTC在补修前靠既有重试/Seed完成；同一Parse数据保留，schema/V2媒体/原意图修复后冷重启验证Chat，不声称最终十补丁版重新跑了17Parse调用。

初始c8c5d2cd9d28485dea0e5990de8813ef39a71a6f + 全部dirty的538文件已包含common；随后仅十文件（五生产+106/-10、五测试+264/-11）及既有五产物维护，原配置/模型/理解/fps=1不替换或下调，profileOverrides=false。并行ROOT V1/Qdrant清理全部保留、仅另做离线195passed，不混真实范围；本轮未commit/push、八列Schema不变。

task559ce12a-ea72-4d6d-867e-e7261e21be60的parent/两child/Interval均COMPLETE，ES2/receipt2与当前contentVersion1/2、generation1一致。首Chat4eaf1bd4...只opening18秒（35calls/301660）；intent-fixed981d32fb...评估length失败（19/202021）；原请求/defaultfresh retry a9f70ef3-5151-43f8-8cd9-c04de4e6f0bb成功（28/298496），两当前chunk命中/播放0–60000及60000–90132ms。先前部分/失败均留账，最终原POSTkey重放12checks通过、99callids/SQLpayload/答案/用量不变。

全workspace **99calls/已知小计1,204,853Token/missing2**，Parse17/known402676，两个DeepSeek schema400 usage未知；公开input/output/total均null，原始knowninput1023503/output181350仅诊断小计。92completed/7failed、失败已知175035保留。独立audit-multichunk-final passed/fullFlowPassed=true，owner/白名单/预算、双receipt/ES/provenance、12实际API筛选和真实UI11模型/4run均通过；两视图复用、series筛选/刷新及缺失2/小计/输入输出“—”对应，页面无错误。

**时间质量边界必须随结论保留：**验证了结构/全局毫秒范围/整chunk播放；seq1当前version2的Interval模型updates在60004–60030ms的26ms内含多句对白，逐事件时间精度未通过。Chat/DTO原值、旧内容覆写与ASR原证未持久化，不能追认混入步骤或猜乘1000修改历史值；下一步强化understanding/refine的毫秒/全局offset约束后真实reParse验证。本轮不扩大prompt修复或模型重跑。默认V2未登记keyframes，图片覆盖unobserved。

初始冻版23测试文件894passed/1skip（27.32s），唯一MySQL timing另库1passed0skip；schema35、冻结媒体158、Supervisor ROOT154/冻结152分别记录。真实Provider Responses VideoParseResult text-only probe1call/2740Token为独立无owner/noSQL诊断，不混99或17Parse。当前原始目录/private/tmp/anivag-usage-multichunk-20261009/中的independent-code-review.json无程序阻断、acceptance-final.json限定统计系统通路通过；intent-retry/semantic-answer-review.json的完整内容覆盖仅指已存解析数据，eventPrecisionCertified=false，timeline-precision-proof.json保存26ms精度缺口。完整时序/修改树/验证与生命周期见[证据索引最新节](model-token-usage-stages-evidence.md)。五产物仍是既有ignored/untracked文件，接手保留全部工作区改动。

最终acceptance-final.json七项统计/通路检查true、eventPrecisionCertified=false、cleanupCompleted=true。API7827于07:00:09.322656UTC SIGTERM正常关闭；Web59930于07:07:53.234UTC SIGINT/exit0关闭。cleanup.json在07:08:24.967914UTC确认9622/5174关闭、3306/6333/9200继续监听，testDataRetained=true；api-stop-7827.json/web-stop.json保留精确进程证据，未删测试数据或新增commit/push。

## 前轮532文件 + 四补丁单chunk入口（历史）

用户已实际发送文末启动指令；本次已按 U02–U05 修改树连续完成代码、配置、测试与使用说明。
八列 Schema 未重设计；原 31 文件树已实施，用户随后授权补修旧 manager 测试，原 U02–U05 累计 32 文件（30M2A）；原有十四个 modified 文件的改动保留。
阶段图显示 **U01–U06 已完成；U06 本轮冻结源码范围的默认真实全链验收通过**，无正在执行阶段。八列 usage Schema 保持，历史与当前范围分开。
U03 实际集中 callback/owner采集、U04 local SQL overview 与 U05 Token 页面均已实现；此前 typecheck/42页面测试、41统计测试、429/409历史回归继续保留在[证据索引](model-token-usage-stages-evidence.md)。
本轮运行是 `224cbbcdaf9e8edbe77c855ced1577bbf863e65d` + 开始时全部 dirty 的 **532 tracked 冻结文件**，后续仅叠加四文件补丁：`server/model/vo/libraryVO.py`、`tests/server/test_library_views.py`、`agent/chat/video_search/skills/__init__.py`、`tests/agent/chat/test_tools.py`；合计+53/-5（生产+14/-5、测试+39/-0）。本轮维护既有五产物，不改Schema/工具DTO/媒体数值，未执行commit/push。
原实际默认模型、理解与 fps=1 均保持，`profileOverrides=false`；pipelineVersion 名称 `m2-seconds` 沿用，execution_mode 为 `v2`，媒体数值使用毫秒。
默认解析任务 `07a4e4b1-5402-495d-a000-4ad0f6742aef` 首次 COMPLETE，视频12095ms、ASR/visual/refinement与text/videoembedding完成，receipt1/ES1；解析五调用 **66,243 Token**。
首检索 Chat `796226d1-ad02-4ef2-8249-358286079bea` 实际命中当前Chunk但错误标注自然语言时间单位，**23调用/145,279 Token**保留。
媒体合同最小修复后，默认请求的新独立Chat `626e188f-3a12-4442-95f3-6350ea073a62` 实际命中同Chunk，**21调用/146,303 Token**，在无用户单位提示下回答 **0–12.095秒（0–12095毫秒）**；结构化范围仍0–12095ms。固定key POST重放不新增调用。
全workspace独立SQL/API与真实页面均为 **49calls/357,825 Token/missing0，input308,560/output49,265，9模型分组/3run**；owner/JSON、预算usedIDs精确/active为空、receipt/ES/provenance一致，页面两视图复用、series筛选和刷新通过，pageErrors/httpErrors均为空。`audit-final.json` 为 passed / `fullFlowPassed=true`。
最终`acceptance-final.json`十一项均true；临时API89122于03:54:07UTC以SIGTERM关闭，Web43330于04:01:39.937UTC以SIGINT/exit0关闭。04:02:11.259068UTC的`cleanup.json`确认9622/5174关闭、基础3306/6333/9200仍运行，测试数据与证据保留。
运行namespace为 `anivag_usage_u06_fixed_20261009_024542_test`，workspace为 `usage-u06-fixed-20261009-024542`，隔离API9622/Web5174；操作沿用既有真实联调授权。证据目录 `/private/tmp/anivag-usage-u06-fixed-20261009/` 保存环境、补丁/哈希、解析/两Chat、独立审计、真实UI与本轮前五文件baseline，详见索引末节。
本轮状态兼容相关组134passed/1skip（13.54s），唯一MySQL timing在另一个专用库补验1passed/0skip（pytest2.56s、helper5.426s），主联调数据保持；Agent媒体prompt合同152passed（1.28s）。
11:42起并行engine/common迁移45tracked变化与common新增全部保留，随后由另一工作提交为当前HEAD `c8c5d2cd9d28485dea0e5990de8813ef39a71a6f`；本轮四补丁仍dirty，root未执行该commit/push。迁移未装入本轮冻结真实服务，最新工作区七文件离线兼容293passed/27skip（11.46s）和AST/字节检查通过，不能写作该迁移已真实全链验收。

### 前轮 endpoint 修复的局部历史

endpoint身份修复已完成九生产/六测试，相关十五测试文件 **686passed/0failed/0skip，26.70s**；Gateway `ModelReferenceType.MODEL/ENDPOINT` 与 frozen `ModelReference.resolve` 区分SDK route与canonical编码身份。
可选`encoder_model`的新默认身份从根 **config.yaml已配置的Gateway实际模型绑定**冻结，upload/reparse保留到快照；旧字段缺失只采用Server已配置绑定。index/query/sparse使用canonical，SDK仍请求原ep，actualmodel/usage不改写，错误响应仍strict拒绝。
reparse(None)锁住最近task继承完整配置后冻结新task；terminal/pending takeover不回到初次session。仅fingerprint的requested配置JSON省略`encoder_model=None`，其他null/显式值与effective快照保持；scope比较canonical身份保留边界。
当时原ep的text、3000ms/fps1video、query三调用实际250615/1024维通过，callback JSON共 **10,403 Token**；该局部轮没有SQL/ES/Chat，`fullFlowPassed=false`，不混入该历史轮49calls账本。
前轮原始文件仍在 `/private/tmp/anivag-endpoint-fix-20261009/`；更旧两轮U06临时目录已因daemon recovery消失，文档保留原路径和必要结果，不宣称旧文件仍可读。

## 前一轮 224cbbc + dirty 毫秒默认联调（身份修复前历史）

本节保留当时 532 tracked 冻结源码验收，原证据路径 `/private/tmp/anivag-usage-u06-ms-20261009/` 已消失。
独立三文件 53 passed；同一短片为 12012 ms，`video.duration_seconds` 注释为 `media_time_milliseconds`。默认 fps=1 / max_frames=60 得到 13 个非空帧，末帧 11550 ms 在 EOF 前；chunk 和 refinement 最终 COMPLETE。
默认任务 `57085694-6167-4ca4-a5ef-e561144eebe2` 当时 parent/interval 为 INDEXING_FAILED、ES=0，strict response model 校验将原 ep 路由误作编码身份，与实际 250615 不同；该阻断已经后续修复，不代表当前状态。
当时十条调用 / 142,374 Token / missing 0 与 SQL/API/统计页和预算完全一致；七 completed / 三 failed、失败用量 75,130 保留，active/lease 清理，532/532 哈希一致。统计页两视图/筛选/刷新通过，另 `/api/series/get` 的 400 不等于全应用无错。
该轮没有触发视频 embedding 或检索 Chat，不能宣称当时全链通过。临时 API/Web 已退出，原 9621/5173 在当时检查中保留；当时只诊断和联调，未写源码或新增 commit/push。

## 前一轮 e296d9b 联调（旧版本历史）

本节保留前一轮实际范围及限制；毫秒重构与修复未纳入该次运行，不代替当前冻结工作区的默认配置重验。
2026-10-09 用户明确“然后可以进行联调测试吧”，授权隔离建表、服务运行和真实模型测试；U02 在 MySQL 8.4.11 专用测试库完成 **139 passed / 2 skipped**，两项跳过仅为 SQLite 参数的 MySQL 并发专项。
本次隔离运行的 531 个 tracked 源码文件逐字节匹配已推送 `e296d9b8ab1a54122826c1c7fca4c093ecb8fbcc`；根 HEAD 的后续 `224cbbc` 与检索等 dirty 不在本次运行范围。数据库、ES index、Qdrant collection 均使用专用 `anivag_usage_u06_20261009_test`。
本次 pinned 提交的十二文件离线基线为 **409 passed / 28 skipped**，Web **42 passed** 与 typecheck 通过；与上述历史工作区 429 passed 分别登记。
首条 12 秒 V2 任务 `4ddff2be-98e2-4583-b04f-802697e9254c` 在模型调用前媒体准备失败；SQL 用量与 ES 命中均为零，此尝试保留为实际失败证据。
真实 direct Chat `df9f29f2-8a0b-4c0f-a695-86304d0600e2` 已成功，DeepSeek Flash 742 + 6 = 748 Token；没有实际检索工具调用，operation 标签不证明分支触发。
后续失败 V2 与 Chat 的独立 SQL/API/UI 部分对账为 6 calls / known 57,604 / missing 1 / total null；页面两视图、系列筛选与刷新通过，`fullFlowPassed=false`。
首条媒体问题定位为默认短片 EOF 采样边界；任务级 fps=0.2 / max_frames=3 可运行，未修改项目默认配置。视觉 invalid JSON 与旧 embedding alias 的 INDEXING_FAILED 尝试及其真实已知 Token 均保留。
专用测试服务只调整合法 test profile 和统一 index/query embedding `doubao-embedding-vision-251215`；真实 V2 `030b779b-fb30-4e05-903c-60116cb633e5` 已 COMPLETE / ES 1 条，四调用已知 12,684 Token，预算 call_id 与 SQL 一致且预留已释放。
成功配置的检索 Chat `912fd888-45aa-4485-9e8d-130a1f71a1d1` 为 19 次真实调用、84,895 Token；两次检索均命中实际 Chunk，重复请求不新增调用。
独立审阅确认 Chat 实际为 17 次 agent_chat + 2 次检索 text_embedding，27 个 call_id 唯一、owner/白名单/预算/receipt/ES/parseProvenance 一致；没有真实 rerank 或 SDK 明细覆盖。
该 workspace 的独立最终 SQL/API 与真实页面核对通过：27 calls / 115,158 Token / missing 0，9 模型分组、4 run；系列/错误作用域/半开窗口、两视图与刷新均通过。
范围结论为“**有效 test profile 的统计全链通过；默认配置未全流程通过**”。该旧轮 U06 当时未整体完成；默认 EOF 与 embedding alias 失败保留为历史证据，不能用临时 profile 替代默认验收，不重新实施 U02–U05 或改 Schema。
本次临时 API/Web 在当时检查中已正常停止，测试库/索引/输入/证据当时保留；既有 9621 健康、5173 仍在。临时证据目录现因 daemon recovery 消失，结果和原路径留在索引。本次五份产物未 commit/push，不覆盖并行源码与原有改动。

下文的原始工作区快照及开工说明保留为接手依据；当前状态以本节、设计和证据索引为准。

## 先读这些依据

1. 项目 `.agents/skills/anivag-change-gate/SKILL.md` 与可用的 `hypnos-code`、`check-out`、`plan-map` 技能。沿用既有职责和实际启动指令的授权，不反复索取已授予的权限。
2. [Server 设计](../../docs/design/model-token-usage.md)：schema、采集、SQL/聚合和 API 的有效合同。该目录被 `**/design/` 忽略，必须直接读取，不因普通 rg/git status 看不到而判缺失。
3. [阶段图](model-token-usage-stages.svg)、[可编辑场景](model-token-usage-stages.excalidraw)和[证据索引](model-token-usage-stages-evidence.md)。U01–U06统计完成，图无正在执行的统计阶段；最新539文件冻版Parse与秒级时间一致性通过，毫秒音轨真值精度未认证。旧538+十补丁的26ms时间缺口、旧Chat/UI验收及532+四补丁结果分别作为历史保留；后续并行ROOT修改不混入最新运行范围。
4. 对照目标阶段读真实调用链和测试范本，然后按阶段 check-out。已有文档不能代替源码核对。

本 handoff 是执行导航，详细合同以 Server 设计为准；不生成第二套竞争规范。
旧 `gateway/report_docs/model-observability-stages*` 是 Langfuse 历史接入记录，不是本轮执行路线。
独立 Parse V2 主线的地图不因本任务自动推进或授权修改。

## 用户的最终决定

- 先不用 Langfuse，复用现有 MySQL 实现最简 Token 统计，替换现有统计后端与页面的数据来源。
- **表名固定 `model_usage`，新 ORM `ModelUsageRecord`，仓储 `server/repository/model_usage.py`。** ORM 仍加到既定 `server/model/parse_usage.py`，不擅自新增模型模块或扩大注册树。
- `usage` 与 `metadata` 是两个独立 JSON 字段，八列结构已认可。Gateway 的 `ModelCallUsage` 保持，不全仓重命名。
- 本批只统计 Token；无价格、费用换算、RMB/USD 合同或完整 trace 建设。
- 不部署 Langfuse，不安装 Docker 或新增存储服务；停止 Langfuse 装配/消费，保留现有模块、可选参数和依赖。
- 两个业务值固定为 `video_parse` 与 `chat_retrieval`。后者包括助手 direct、Web、视频检索，并非每条调用必然发生 retrieval。
- 不建立独立检索统计产品/enum/run；不预建未来实时识别 sub-agent。

完整 schema 见[设计 §2](../../docs/design/model-token-usage.md#schema)，八列为：

```text
call_id       VARCHAR(36)   PK
workspace_id  VARCHAR(64)
task_type     VARCHAR(16)   CHECK video_parse / chat_retrieval
run_id        VARCHAR(256)
started_at    TIMESTAMP(3)  UTC
usage         JSON
metadata      JSON
created_at    TIMESTAMP(3)  UTC
```

全部非空；无 updated_at、无多态外键。两索引与 utf8mb4_bin 沿用设计，约束/索引名前缀使用 model_usage。
`video_parse.run_id` 为真实 `video_parse_task_id`；`chat_retrieval.run_id` 为真实 `assistant_message_id`。
`usage={tokens: TokenUsage|null, sdk_model_usage:[{model_name,tokens}]}`，不持久化 SDK cost_usd。
metadata 按设计白名单显式投影；model_name 取实际 Gateway model，src 无明确实际来源时为 null。

## 开工前已经做了什么，尚缺什么（历史）

已经交付设计、原生 Excalidraw 阶段图、证据索引和本 handoff；表名已统一。
**没有实现 model_usage ORM/repository，没有建表，没有将采集/API/UI 切换到 SQL。**
当前 `server/bootstrap.py` 仍装配 Langfuse writer/reader；usage service 和页面仍走 Langfuse/费用合同。
Gateway 已有 call_id、TokenUsage、SDK 模型明细、context 和 observer fanout；本批不需再改模型 adapter。
V2 已是默认流程，保留显式 V1 opt-in 和旧统计表/历史；新表不回填旧账本，也不把 V1 job_id 冒充 V2 task ID。
上一轮的文档/图验证已通过；当时没有本地统计实现测试或真实 MySQL/模型验收结果，不能借历史 passed 数量标完成。

## 接手职责与授权

新 session 承担已规划的 Server 持久化/采集/API 与 Web 统计替换；下方树为原 31 文件加本次用户授权的 manager 测试补修，共 32 文件。
原交接轮次仅统一命名并生成交接；本次用户已明确要求连续实施 U02–U05，具体结果见文首与证据索引。
**新 session 以用户实际发送的启动指令确定执行授权**：若明确要求按本 handoff 开发 U02–U05，阶段核对后连续完成已授权范围，无需每阶段重复确认。
该执行指令也提供进入 U02 的依据；先核对已交付的 U01 和最新源码，再登记当前阶段，不因旧“计划待审阅”文字重新询问已确认的 schema。
仅发送“读取/检查 handoff”则保持只读；handoff 文件本身不产生环境操作、建表、重启、commit 或 push 权限。
任务内单测可以执行；真实测试库建表/数据清理、开发库初始化、服务/真实模型运行分别沿用已有有效授权，缺失时只询问具体操作。
环境限制不能阻断独立的源码工作或离线验证，也不能把未通过项写成已完成。

## 开发顺序与 checkpoint

| 阶段 | 先完成的能力 | 验收及交接 |
|---|---|---|
| U02 持久化 | 新 ORM、不可变 State/batch、短事务 repository、UTC/结构登记 | 八列/幂等冲突/精确 SQL/limit+1；SQLite 与真实 TEST MySQL 分别记录。第一批为 6 文件、4M2A。 |
| U03 运行采集 | 装配 SQL 服务，V2/Chat 的稳定 observer；默认 tracing=false | call_id 一条；Chat worker 内绑定；子检索继承；失败保用量/业务；V2 预算与租约保持。 |
| U04 SQL overview | 保留 GET 路由和参数，换 SQL 聚合，local/taskType/Token-only | 核对 SDK/缓存/未知/截断/空窗口/读取失败，同步公开合同与六处说明。 |
| U05 页面 | 对齐 Web 合同和当前模型消耗页，移除费用/LF 提示 | 两视图、系列筛选、刷新、未知/小计/截断与失败；typecheck 与直接页面回归。 |
| U06 真实联调 | 实际 V2 解析/index 与至少一次 Chat | 对真实触发的模型 usage、SQL 行、API、页面逐层对账，写环境/输入/run/call_id/限制。 |

依赖 U01 → U02；U02 后 U03/U04 可并行；U04 → U05；U03+U05 → U06。
U03/U04 共用 service 与测试，分别负责 capture/append 和 overview/aggregation；并行 Agent 不同时覆写共享区域。
每批记录实现、直接验证、真实环境验收各自状态；按有效证据维护主图/源图/索引，不提前移动当前位置。
推进地图须按 plan-map 用官方 Excalidraw 能力导出，不能单独手改 SVG 使其与源图分叉。
本次已在 `/private/tmp/anivag-model-usage-map-20261008` 恢复官方 exportToSvg 导出工具，并同步原生场景及 SVG；业务依赖未改。

## U02–U05 预计修改树

这是已核对并执行的范围，含用户本次追加授权。叶子标明阶段，共 32 个唯一文件（30M、2A）。

```text
AniVAG/
├── [M] .env.example                          ← U04 本地统计说明；保留可选 LF 空字段
├── [M] config.yaml                           ← U03 默认 tracing.enabled=false
├── [M] README.md                             ← U04 更新用量来源与配置说明
├── server/
│   ├── [M] README.md                         ← U04 更新统计接线与使用说明
│   ├── [M] bootstrap.py                      ← U03 SQL 装配，停用 LF，V1 不注入新统计
│   ├── [M] database.py                       ← U02 新表 UTC/结构检查
│   ├── model/
│   │   ├── [M] parse_usage.py                ← U02 新增 ModelUsageRecord，保留旧模型
│   │   ├── dto/
│   │   │   └── [M] parseUsageDTO.py          ← U02 原始 State/batch；U04 Token 统计合同
│   │   └── vo/
│   │       └── [M] parseUsageVO.py           ← U04 local、taskType、Token-only
│   ├── repository/
│   │   └── [A] model_usage.py                ← U02 追加、归属幂等、SQL 有界查询
│   ├── service/
│   │   ├── [M] parse_usage.py                ← U03 capture/append；U04 SQL 读取聚合
│   │   └── [M] agent_chat.py                 ← U03 在执行线程绑定 Chat owner
│   └── api/
│       └── [M] parse_usage.py                ← U04 保留请求签名、调整投影
├── tests/
│   ├── gateway/
│   │   ├── [M] test_config.py                ← U03 默认 trace 关闭
│   │   └── [M] test_model_config_v2.py        ← U03 配置兼容断言
│   └── server/
│       ├── [M] test_database.py              ← U02 DDL/UTC/约束
│       ├── [A] test_model_usage_repository.py ← U02 实际仓储/隔离/过滤/截断
│       ├── [M] test_bootstrap.py             ← U03 SQL 装配/关闭/V1 边界
│       ├── [M] test_parse_usage.py           ← U03 采集；U04 聚合/API
│       ├── [M] test_agent_chat_service.py    ← U03 Chat 身份与线程内捕获
│       ├── [M] test_video_parse_worker.py    ← U03 SQL 与真实 manager/control 共存
│       ├── [M] test_video_parse_model_calls.py ← U03 本次追加授权；对齐 provider model 替身
│       └── [M] test_video_retrieval_service.py ← U03 继承父 owner，不造 retrieval run
├── web/
│   └── src/
│       ├── api/
│       │   ├── [M] contracts.ts              ← U05 对齐公开 Token 合同
│       │   └── [M] client.test.ts            ← U05 响应 fixture 与运输回归
│       └── features/
│           ├── usage/
│           │   ├── [M] UsagePage.tsx         ← U05 本地 Token/两视图/状态
│           │   └── [M] UsagePage.test.tsx    ← U05 页面直接回归
│           └── tasks/
│               ├── [M] ParseUsageDetails.tsx      ← U05 未挂载组件仅合同对齐
│               └── [M] ParseUsageDetails.test.tsx ← U05 对应回归
└── docs/
    ├── [M] parse-model-usage.md              ← U04 来源/Token/限制
    ├── [M] api-ui-views.md                   ← U04 overview 文档
    └── design/
        └── [M] frontend-api.md               ← U04 只改用量相关合同段落
```

32 文件分类：12 生产、13 测试、2 配置、5 使用说明。阶段计数不可累加：DTO、service、统计测试跨阶段共用。
阶段证据/地图维护另沿用现有产物，不把文档维护计为新统计功能。

## 易误改处与必须守住的行为

- U02 新表仍用已有 parse_usage 模型导入注册；不要把缺新表误判为旧 worker 必备表缺失。复用 UTC_TIMESTAMP，不继承 BIGINT/updated_at 的 TimestampedRecord。
- 幂等只允许同 call_id 的 workspace/task_type/run 全部一致；并发 PK 冲突回滚后再读校验。不要吞掉其他 IntegrityError，不覆盖已记录 JSON。
- 先 SQL workspace/time/run/精确 series scalar-or-array 筛选，再 started_at/call_id 排序和 20k+1；不做 Python 后筛选，不对 JSON 文本 LIKE。
- SQL subscriber 复用稳定 callable，combine=True；显式投影 JSON，不对含 MappingProxyType 的整个事件 asdict。每调用结束后短事务；写失败保业务结果，统计只声明已记录范围。
- Chat 在 executor 实际执行线程绑定 scope，不能只在 submit 所在线程绑定。asyncio.to_thread 复制 ContextVar；Codex reader context 已有实现，本批不重改 adapter。
- `video_parse.py` 已调用 capture_video；`video_retrieval.py` 已有 model_call_context。bootstrap 停止 LF 注入后，这两处可复用，不清理 dormant tracing 参数或新增 usage 参数。
- V2 manager 生产构造未传可选 usage_observer，当前不会覆盖外 observer。不改 manager 的 combine 分支；真实 manager 与 SQL 采集共同验证，保预算/并发/heartbeat/取消/lease/release。
- 旧 repository/table/价格历史保留，新统计不混入、不回填。独立检索无受支持 owner 时不生成统计身份。
- SDK 明细替代父总数；调用数按不同 call_id。cached/cache-write/modality 属 input 子集，reasoning 属 output 子集，不能重复加 total。
- unknown subset 只使对应 subset 合计 null；核心 unknown、main_loop 与 truncated 的完整合计/已知小计传播严格按设计 §4.1，不用费用字段代替覆盖状态。
- API 保持 GET /api/usage/overview 及原窗口/系列/run 参数，不新加 taskType 请求筛选。响应 source=local、status=available/unavailable，run.taskType 两值。
- ParseUsageDetails 当前未生产挂载，仅因编译/合同变化调整，不新增 TasksPage 功能或额外迁移 props/status。client.ts、CSS、金额 helper 无必要改动。

## 开工前工作区快照与环境

本次交接开始时已有十个 modified 文件，均不是本次统计代码成果：

```text
docs/design/frontend-api.md
engine/README.md
server/api/search.py
server/config/retrieval.py
server/config/settings.py
server/model/dto/searchDTO.py
server/service/video_retrieval.py
tests/server/test_api.py
tests/server/test_config.py
tests/server/test_video_retrieval_service.py
```

handoff 生成期间，又观察到四个既有 tracked 文件发生并发修改，本 Agent 未写这些文件，同样保留：

```text
gateway/ark/embedding.py
server/service/video_parse_index_coordinator.py
tests/gateway/test_video_embedding.py
tests/server/test_video_parse_index_coordinator.py
```

交付前共十四个 modified 文件；接手时必须重新读 git status，不把此快照当作实时状态。
其中 frontend-api 文档和 retrieval 测试会与本任务共享，必须保留原 diff，仅改自身用量合同/回归部分；retrieval 生产源码不纳入必要修改树。
新增 report_docs 文档尚未提交，TRD 被忽略但已存在；不 force add、不清理工作区或借本任务自动提交。
原交接没有验证服务健康、测试库连接、模型配置或运行依赖；本次仅补字段存在性核查与离线验证，不能宣称真实在线流程已跑通。
如需读 `.env.local` 只查所需字段存在性，禁止输出密钥、完整连接串或自由文本载荷。
测试使用 ANIVAG_TEST_DATABASE_URL 与 DatabaseEnvironment.TEST，独立 *_test 库；缺配置按真实限制记录，不拿 development/production 数据冒充测试。
旧 Langfuse 地址/密钥缺失不阻塞本方案，禁止继续其部署/下载待办。

## 验证入口（本轮统计与通路通过，逐事件精度未通过；并行清理另做离线）

以下保留完整命令入口，实际结果见证据索引。四项基线失败已按用户追加授权修复，当前回归无需 deselect。
MySQL 用例在独立测试库及环境操作授权齐备后执行；未齐备时记录 skipped/待验收。

```sh
# 项目根目录：U02
.venv/bin/pytest tests/server/test_database.py tests/server/test_model_usage_repository.py

# 项目根目录：U03 / U04
.venv/bin/pytest tests/server/test_parse_usage.py tests/server/test_bootstrap.py tests/server/test_agent_chat_service.py tests/server/test_video_parse_worker.py tests/server/test_video_retrieval_service.py tests/server/test_video_parse_model_calls.py tests/gateway/test_config.py tests/gateway/test_model_config_v2.py

# web/ 目录：U05
npm run typecheck
npm test -- src/api/client.test.ts src/features/usage/UsagePage.test.tsx src/features/tasks/ParseUsageDetails.test.tsx

# 项目根目录：每批范围与空白核对
git diff --check
git status --short --untracked-files=all
```

原“不要修改旧 manager 测试”的限制已被用户本次“全都去改”覆盖；仅补其 provider 响应替身，预算控制断言保持。
worker 替身同步当前 agent options / recall / 回执合同，完整流程通过；两类原 TODO 均已关闭。
U06 按实际有效启动配置运行，至少真实 V2 和一条 Chat，对实际触发的调用对账；不要求一次 Chat 同时走 direct/Web/retrieval。

## 本次已使用的启动指令

用户已实际发送以下指令，构成本次 U02–U05 源码与离线验证的执行授权；不扩展为真实环境操作授权：

```text
请读取 /Users/bytedance/workspace/other-project/AniVAG/server/report_docs/model-usage-handoff-20261008.md，续接 AniVAG 的本地 Token 统计 refactor。
表名已确认 model_usage，usage/metadata JSON 与八列 schema 按设计；使用 hypnos-code、check-out 和现有 plan-map。
先完成 U02 阶段级 check-out，然后按 handoff 的 U02–U05 修改树连续执行已列代码、配置、测试和说明文档，不要再重新设计 schema。
每阶段核对并回写证据，保留已有工作区改动。真实 MySQL 建表、服务运行和 U06 联调先核实具体操作授权；环境待办不阻断已授权的源码与离线验证。
```
