# Seekino Devlog

> 2026-10-24 旧实验退役说明：下文保留当时的观察；声纹、检测、图片出处实验的原始材料已删除，链接转到[结论摘要](design-experiment-cleanup.md#第四轮旧实验整体退役构图研究仅留本地归档)。构图／shot-tags 数据仅保存在被 Git 忽略的本地归档中，详见[恢复说明](design-experiment-cleanup.md#本地归档与恢复)。

## 维护原则

技术修改的 devlog 统一维护在 `docs/dev/devlog.md`，按日期和主题追加，不再按模块另建 devlog。任何技术层面的修改，都以实际业务功能的端到端闭环结果为评价依据，同时记录引入或减少的架构复杂度、维护成本、认知成本，以及带来的业务收益或对原有问题的改善。

每条记录固定为以下四个部分，选择理由、成本、收益、实验依据和未验证项均归入其中，不另扩展同级栏目：

1. **问题与现状**：现状是什么设计，导致遇到了什么问题，对产品有什么影响，并记录修改前的基线。
2. **方案候选**：列出实际考虑过的方案、适用边界和预期收益与代价；说明最终选择及其原因，以及未选其他方案的原因。
3. **方案验证**：记录业务 case、实验方法、控制变量、验收标准和证据；明确已验证与未验证的范围，后续待验证内容也归入本节。
4. **指标对比**：根据当前修改的业务功能灵活选择和调整指标，以能反映实际业务效果为准。对比修改前后业务闭环的总体相关指标，按场景记录耗时、token、费用、完成率、正确性、覆盖度，以及架构复杂度、维护成本和认知成本的变化；同时说明原问题改善、实际业务收益、新增问题和负面影响。不能量化的成本作具体定性说明，区分实测、预估与未验证，不能把局部技术指标改善直接当作整体收益。

**历史归档口径（2026-10-23）：**以下按业务问题合并问题清单、TRD与实验报告，同一实验的多份记录不重复算成果。状态以注明的日期、源码和环境为边界；原材料没有后续证据的事项保留待核验，不等于本轮重新确认当前仍存在。测试通过、实模运行完成、质量达标与正式部署分别记录。原设计、问题清单和实验产物保留作为来源，本轮仅整理此文件，没有重新运行模型、服务、测试或迁移。部分设计文件和本地产物尚未纳入版本控制，失效临时路径不声称可直接复跑；关键数字与失败边界保留在正文。

**覆盖边界：**原 `problems.md` 的F01–F06、B01–B14、C01–C19共39项均已映射；旧 `problem_backup.md` 的38项重叠编号不重复建条，另保留两组未编号验收/能力边界。VP2-001–007七项另保留。实验按代表报告、关键结论和原始summary核对，不是对所有逐帧、逐chunk或原始trace文件重新审计；纯研究/未训练、空TRD、未完成和失败实验均明确标记。

<details>
<summary>记录索引：按资料日期与业务主题定位</summary>

| 记录 | 原问题编号 |
|---|---|
| [2026-10-30：连续半集对照未完成与十分钟业务核查](#devlog-half-episode-ab-20261030) | B07、质量与成本 |
| [2026-10-29—30：整片 ASR 内存复用与 Chunk 区间读取](#devlog-full-video-asr-20261029) | B20、视频级转写与时长 |
| [2026-10-30：Kimi Code API 10 路并发实测](#devlog-kimi-concurrency-20261030) | B21、供应商并发与业务执行槽位 |
| [2026-10-30：Understanding 直接 JSON 与 30k 正式接入](#devlog-parse-direct-json-20261030) | B07、实现闭环 |
| [2026-10-29：Karpathy OCR 配置纠正与解析恢复](#devlog-karpathy-parse-recovery-20261029) | B07、B20、真实任务恢复 |
| [2026-10-29：普通展示模式开放解析有效总时长](#devlog-release-parse-duration-20261029) | F10、解析时长展示 |
| [2026-10-29：本地向量直连 llama.cpp 与独立运行入口](#devlog-llama-cpp-embedding-20261029) | B19、本地 embedding 与运行依赖 |
| [2026-10-29：系列封面采用 Summary 候选与上次观看画面](#devlog-series-artwork-20261029) | F09、观看封面规则 |
| [2026-10-29：WhisperX 正式可选接入与字幕字词时间](#devlog-whisperx-20261029) | B20、本地 ASR 与字幕证据 |
| [2026-10-29：本机部署与 MCP 分组提交](#devlog-deploy-mcp-publish-20261029) | B15、B17、B18；提交范围与验证 |
| [2026-10-29：本地 Qwen 文本向量、视觉开关与课程片段小实验](#devlog-local-text-embedding-20261029) | B19、课程检索与本地模型 |
| [2026-10-29：观看聊天面板收紧与设置折叠](#devlog-watch-chat-compact-20261029) | F07、观看聊天可用空间 |
| [2026-10-29：Summary 正式标题优先与内容标题回退](#devlog-summary-title-20261029) | F08、解析元信息与资料库展示 |
| [2026-10-29：身份缺证归因修正与多片段质量验证计划](#devlog-parse-quality-attribution-20261029) | B07、B12、证据与模型评测 |
| [2026-10-29：采用精简 JSON，结束 XML 选型与单块 30 秒追求](#devlog-parse-protocol-decision-20261029) | B07、协议决定与质量优先 |
| [2026-10-29：取消人物名单、增加 SHOT 的精简 JSON/XML 对照](#devlog-parse-compact-shot-20261029) | B07、协议归因与内容质量 |
| [2026-10-29：本地 Whisper、无独立 OCR 的 JSON/XML 单块对照](#devlog-parse-protocol-20261029) | B07、输出协议与内容质量 |
| [2026-10-29：全原生 localhost 环境与 Kimi 业务验收](#devlog-native-local-20261029) | B18、本机部署与真实运行 |
| [2026-10-29：面试前保护旧闭环并撤销裁剪](#devlog-interview-preserve-20261029) | B16、演示与其他工作依赖 |
| [2026-10-29：持久媒体移入 assets/library 与旧数据恢复核验](#devlog-persistent-media-20261029) | B17、持久媒体与换机恢复 |
| [2026-10-29：按角色宣传片目标退役 NPC 实验与 ACP Client](#devlog-pv-scope-prune-20261029) | B16、制作目标与代码范围 |
| [2026-10-29：localhost MCP 与外部 Agent 检索接入](#devlog-local-mcp-20261028) | B15、MCP 工具与素材取用 |
| [2026-10-28：解析性能 TRD 与事件包装 XML 复测口径](#devlog-parse-performance-20261028) | B07、Understanding 职责与输出协议 |
| [2026-10-26：Kimi 同模型 Chat 三框架对照](#devlog-chat-harness-kimi-20261026) | C19、Pi / Deep Agents / Codex、查询覆盖与交付合同 |
| [2026-10-25：按能力整合 benchmark 并纠正选集口径](#devlog-benchmark-layout-20261025) | 查询覆盖、Chat behavior、历史证据归属 |
| [2026-10-25：同一 chunk 的 ReAct、Deep Agents 与 Codex 解析实测](#devlog-parse-harness-ab-20261025) | Parse harness 简化、compact 预算、EP04 画外音身份 |
| [2026-10-25：Deep Agents + Kimi K3.1 Pro 查询能力覆盖回归](#devlog-chat-kimi-mygo-20261025) | 旧 multi-agent 多任务用例；非 Chat behavior 验收 |
| [2026-10-24：Cloud Chat 去 Pi Host 与 Deep Agents 持久化 Checkpoint](#devlog-cloud-chat-checkpoint-20261024) | C20、harness 职责重复、上下文恢复与部署复杂度 |
| [2026-10-24：Chat 前置 low / none 实测与 cloud 选型](#devlog-query-hook-chat-model-ab-20261024) | C19、前置模型替代、体验与建设成本 |
| [2026-10-24：Jev 旧链路与主 Chat 直接处理的真实端到端对照](#devlog-jev-chat-ab-20261024) | C19、意图路由、部署复杂度 |
| [2026-10-23：Canvas 完整条目读取、可修复反馈与端到端评估](#devlog-canvas-20261023) | B06、C07、C18 |
| [2026-10-22：画外音提示修正与 EP04 两个已知身份目标](#devlog-history-01) | B12 |
| [2026-10-22：Whisper 替代 ASR 与整集先转写](#devlog-history-02) | 独立实验/设计 |
| [2026-10-22：Event V3 迁移、EP13 索引与演示闭环边界](#devlog-history-03) | 独立实验/设计 |
| [2026-10-22：3fps 采帧、旧上限兼容与 main 运行来源](#devlog-history-04) | 独立实验/设计 |
| [2026-10-21：连续 turn 提示、混合 consumer 与纯源码复跑](#devlog-history-05) | 独立实验/设计 |
| [2026-10-21：短音频裁剪、VAD 与“同一人连续说话”证据](#devlog-history-06) | 独立实验/设计 |
| [2026-10-21：独立整集摘要 Agent、证据读取与标题定位](#devlog-history-07) | 独立实验/设计 |
| [2026-10-21：对白检索中权重、ANN 与文档粒度的不同失败](#devlog-history-08) | C03、C14 |
| [2026-10-21：声纹预填时长门槛、gap 与最终身份保留](#devlog-history-09) | 独立实验/设计 |
| [2026-10-21：OCR 注入的字幕收益与时间定位误差](#devlog-history-10) | 独立实验/设计 |
| [2026-10-21：Event 多向量索引完成度与正文翻译覆盖](#devlog-history-11) | 独立实验/设计 |
| [2026-10-20—10-21：Chat 意图路由、改写实验与前置 hook 接入](#devlog-history-12) | C19 |
| [2026-10-19：声纹决策门禁、纠错反馈和供应商失败](#devlog-history-13) | 独立实验/设计 |
| [2026-10-19：Shot tags 通过一致性门槛，但人工相关性评估仍未完成](#devlog-history-14) | 独立实验/设计 |
| [2026-10-19：B0 声纹基线、聚类和参考标签污染](#devlog-history-15) | 独立实验/设计 |
| [2026-10-18：解析负载下的 MCP 启动放大与任务列表大字段读取](#devlog-history-16) | B13 |
| [2026-10-18：更换 speaker encoder 能改善个别样例，但不能解决参考库身份问题](#devlog-history-17) | 独立实验/设计 |
| [2026-10-18：整集聚类能整理声音候选，不能直接把稳定簇当角色](#devlog-history-18) | 独立实验/设计 |
| [2026-10-18：Shot gallery 建立了可复现的画面候选单位，尚未证明检索质量](#devlog-history-19) | C16 |
| [2026-10-18：Query-only 收敛与真实回答质量边界](#devlog-history-20) | 独立实验/设计 |
| [2026-10-18：Event-first 检索与 HTTP 部署验证](#devlog-history-21) | 独立实验/设计 |
| [2026-10-18：Event 细粒度检索改善证据定位，但 readiness、候选曝光和模型回合仍影响全程](#devlog-history-22) | 独立实验/设计 |
| [2026-10-18—2026-10-19：Event 补建的大 JSON 写入放大与 binlog 留存](#devlog-history-23) | B14 |
| [2026-10-18—10-19：Chat 重复审核、慢响应与 Supervisor 遗留项](#devlog-history-24) | C17 |
| [2026-10-17：构图表征要分别验证类别、几何和内容，不宜混成一个相似分](#devlog-history-25) | 独立实验/设计 |
| [2026-10-17：声纹接入整集 Refine 后，参考误标和快照刷新比编码耗时更关键](#devlog-history-26) | 独立实验/设计 |
| [2026-10-17：CAM++ 的可用性取决于参考音频质量和参考库构成](#devlog-history-27) | 独立实验/设计 |
| [2026-10-16：截图找出处的瓶颈包括长链路与错误停止，而不只是检索排序](#devlog-history-28) | C15 |
| [2026-10-16：匿名说话人、补帧与 Prefix 能否修正 EP04 身份](#devlog-history-29) | 独立实验/设计 |
| [2026-10-16：GroundingDINO 可辅助主体布局，但不是构图与线段识别器](#devlog-history-30) | 独立实验/设计 |
| [2026-10-16：Chat 终止错误及时上抛与跨进程 PROCESSING 遗留](#devlog-history-31) | B01、C13 |
| [2026-10-15：9 条真实 Chat 暴露了证据覆盖和上下文传递问题](#devlog-history-32) | 独立实验/设计 |
| [2026-10-15—10-16：图片查询附件承载与真实视觉检索缺口](#devlog-history-33) | 独立实验/设计 |
| [2026-10-14：并发上传完成的父记录锁升级死锁](#devlog-history-34) | B09 |
| [2026-10-14—2026-10-16：Supervisor 最终提交、视频证据展示与公开表达](#devlog-history-35) | C05、C12、F06 |
| [2026-10-14—2026-10-16：Parse 超时、停服和租约恢复不能替代实际执行退出](#devlog-history-36) | B10、B11 |
| [2026-10-13：多模态检索应围绕用户任务和证据组织，而不是先增加模型](#devlog-history-37) | 独立实验/设计 |
| [2026-10-12：索引发布幂等与浮点精度诊断分开处理](#devlog-history-38) | B08 |
| [2026-10-11：嵌套图 Checkpointer、运行配置与实时视频数据失败的分层归因](#devlog-history-39) | C08、C09、C10、C11 |
| [2026-10-10：BaseModel 字段与时间合同迁移](#devlog-history-40) | 独立实验/设计 |
| [2026-10-10—10-21：游客、个人资源、共享与 Local/Cloud 边界](#devlog-history-41) | 独立实验/设计 |
| [2026-10-10—10-21：本地 Codex/ACP 与云端 Pi Host 的职责拆分](#devlog-history-42) | 独立实验/设计 |
| [2026-10-09：视觉推理强度与 Refine 全量事件丢失](#devlog-history-43) | 独立实验/设计 |
| [2026-10-09：ASR 秒制输出与全局毫秒边界修复](#devlog-history-44) | 独立实验/设计 |
| [2026-10-07—10-09：模型用量账本、失败计费与可观测性](#devlog-history-45) | 独立实验/设计 |
| [2026-10-02—10-06：VP2 并发、恢复与真实存储验收缺口](#devlog-history-46) | VP2-001–007 |
| [2026-09-23—2026-10-17：整集解析的性能、重复执行与 Refine 冲突](#devlog-history-47) | B07 |
| [2026-09-21：历史消息与检索 topK 的业务合同澄清](#devlog-history-48) | C04、C06 |
| [2026-09-21：Chat 未授权引用与泛化失败需要可追踪、可修正的反馈](#devlog-history-49) | C01、C02 |
| [2026-09-20—2026-10-16：历史联调验收与能力边界不能被“已解决”清单吞掉](#devlog-history-50) | 独立实验/设计 |
| [2026-09-17—09-19：Chat 上下文重复注入与有界读取的历史修复](#devlog-history-51) | 独立实验/设计 |
| [2026-09-16—10-17：解析职责、Parse V2 存储与 ES 迁移的历史收敛](#devlog-history-52) | B03、B05 |
| [2026-09-15：本地视觉模型微调与蒸馏候选（未训练）](#devlog-history-53) | 独立实验/设计 |
| [2026-09-13：视频理解模型、输入方式与结构成功后的语义缺口](#devlog-history-54) | 独立实验/设计 |
| [2026-09-13：EP07 双通道检索是否真的比纯文本更容易召回](#devlog-history-55) | 独立实验/设计 |
| [日期未记录；2026-10-23归档：低频累计耗时替代阶段明细高频写表](#devlog-history-56) | B04 |
| [日期未记录；2026-10-23归档：会话管理、解析重试和前端能力文案](#devlog-history-57) | F01–F05 |
| [日期未记录；2026-10-23归档：产品范围、综合评估与外部反馈的 Owner 复盘](#devlog-history-58) | 独立实验/设计 |
| [日期未记录；2026-10-23归档：上传事务成功后的附属清理错误](#devlog-history-59) | B02、B09 |
| [日期未明确（2026-10-23归档）：当前播放窗口的按需理解与复用](#devlog-history-60) | 独立实验/设计 |

</details>

<a id="devlog-full-video-asr-20261029"></a>

## 2026-10-29—30：整片 ASR 内存复用与 Chunk 区间读取

### 1. 问题与现状

正式 ASR 按 Chunk 调用；用户希望先转写全片，原 ASR 节点仅取对应区间，避免把这项改造扩大成 TimelineEvent 重构。跨界语音可能受固定切分影响，但准确率改善尚未实测。prod/release 时长展示已由前置提交 `c84a2169` 开放。

### 2. 方案候选

采用用户提出的进程内视频级转写缓存，按任务和协调器租约隔离；新增 WhisperX 默认配置选择 video scope，历史任务仍为 chunk。缓存准备成功后发布媒体，Chunk 深复制所有相交 cues，保留原句及绝对毫秒。没有新增持久 checkpoint 或事件身份；重启需要重新转写。将前移的 ASR 纳入已有租约计时和执行区间并集，避免展示漏计。

### 3. 方案验证

离线替身模型验证并发两 Chunk 仅转写一次、相交边界、字词时间、深复制、恢复重建、失败与取消资源清理、旧配置和总时长。SQLite 验证视频计时拒绝失效租约，任务投影包括 ASR，缺失样本保持不完整。最终定向回归 **813 passed**（全片 ASR、配置、Worker、持久化、任务投影、全部 Parse V2 Agent 与观察合同）；保留既有 Starlette 弃用警告。独立工作区前端 production build（含 TypeScript）与 diff 检查通过。真实 WhisperX 全片、质量与峰值内存仍未测；未修改现有业务任务或部署运行服务。

### 4. 指标对比

| 指标 | 原行为 | 本次离线证据或限制 |
|---|---|---|
| 同轮两 Chunk 的 ASR 调用 | 每块调用 | 共享一次视频转写；恢复重新调用一次 |
| 跨界 58–62 秒 cue | 受 Chunk 音轨切分 | 两 Chunk 均收到完整原句与同一绝对时间 |
| 总有效时长 | 只计子任务执行 | 纳入视频 ASR，保留区间并集、缺失和失败口径 |
| 识别质量、资源与速度 | 待对照 | 未测；首个 Chunk 增加等待整片转写的时间 |

新增一个配置字段、区间选择函数和视频准备缓存，复用原 Gateway、任务和提交边界。状态入口为 [B20](problems.md#b20本地-whisperx-正式接入与字幕时间证据)。

2026-10-30 接续核验与集成（同一需求）：

- **问题与现状：** 本机日志在 00:29 停机前仍显示 Canvas 工具循环及 8,192 输出预算；一个 Chunk 在 666,900 ms 后因模型读取超时失败。11:27–11:30 的只读进程/status 核对显示 API/Consumer、Web、MySQL、ES 均未就绪，不能把已提交修复称为运行稳定。PR #9 又落后于 dev 的 `13114f83`/`80bb293d`，产生 devlog 合并冲突。
- **方案候选：** 将最新 dev 合入原功能分支，保留直接 JSON、节点内重试与最终失败停止机制，解决文档冲突时保留两侧记录。沿用内存 ASR 方案；使用 Video=1 控制整片准备、Parse=10 释放理解并发。没有修改本机仍为 Parse=2 的配置或启动真实任务。
- **方案验证：** 扩展既有测试为 2/10 个 Chunk，进入真实 Understanding 子图后用 Barrier 保证同时在途，对其中一个注入暂态超时；最终每个 Chunk 仅提交一次，ASR 每轮仍调用一次，协调器恢复再调用一次。组合回归 **1,014 passed、1 个既有 Starlette 弃用警告**；命令为 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -p no:cacheprovider tests/server/test_full_video_asr.py tests/server/test_config.py tests/server/test_video_parse_worker.py tests/server/test_video_parse_repository.py tests/server/test_job_repository.py tests/server/test_video_parse_queue.py tests/server/test_video_parse_media.py tests/server/test_parse_output_budget.py tests/agent/video_parse_v2 tests/engine/parsev2/test_observations.py --tb=short -q`，使用已有 Python 3.12 venv，39.65 秒完成。原始临时日志 `/private/tmp/seekino-full-video-asr-dev-integration-tests.log`，测试与行为断言保留在仓库；与此前 813 项重叠，不累加。
- **指标对比：** 离线并发覆盖从 2 个扩大为 10 个 Chunk，新增理解失败恢复而 ASR 不增加调用的证据；实模速度、资源和稳定性仍未测。本机 WhisperX 超时仍为 1,200 秒，是否覆盖整节课程必须单独实测；旧任务普通 retry 不更新 scope/输出预算。下一步是新配置的隔离试点及端到端验收，不能直接把短请求 10 路成功当成长课程通过。

2026-10-30 落地补强与真实 ASR 冒烟（同一需求）：

- **问题与现状：** SQLite 复现 3 项失败：已有拓扑的 PARSING 任务重建 ASR 时，Chunk 仍可领取并提前计时；ASR Provider 异常没有增加 Prepare 失败计数；重复准备租约过期也未耗尽该预算。另外，新建观看问答的按需任务没有整片协调器，不能直接继承 video scope。用户随后将本 chat 的职责收回到 PR #9；此前仅修改本机 Video/Parse 为 3/3 并启动 MySQL、ES、embedding，未由本 chat 启动 API/Consumer 或创建新的课程解析版本。
- **方案候选：** 复用现有任务 detail 保存 `asr_prepared_lease_version`，在转写缓存和媒体都就绪后写入，Chunk 领取校验当前租约；新租约使旧回执失效。准备错误和租约过期复用有限 Prepare 失败预算。按需观看新任务保持 chunk scope。无需新表、持久转写格式或 TimelineEvent 迁移。
- **方案验证：** 修复后 Queue/全片 ASR 141 passed；扩大组合回归为 **1,017 passed、1 个既有 Starlette 弃用警告**，观看兼容单独 **26 passed**。组合命令沿上面的 1,014 项入口，新增 3 个恢复用例；观看命令为 `python -m pytest tests/server/test_realtime_understanding.py -q`。实模使用当前 large-v3 CPU/int8、batch 1、6 CPU threads，从原始 Micrograd 视频取 `[0,100000)` ms，调用实际 `VideoParseRuntimeService._prepare_video_asr`、Gateway、音轨提取与 WhisperX，再并发调用 10 次 `transcribe_chunk`；租约/计时持久层及媒体读取准入由探针替代，不修改业务数据库、不调用 Kimi。一次 ASR 成功，12 条 cues、334 个有效词时间，10 个窗口均非空，6 条 cue 跨窗口保留。证据：[结果](../../outputs/verification/full-video-asr-live-20261030/result.json)、[探针](../../outputs/verification/full-video-asr-live-20261030/probe.py)、[模型日志](../../outputs/verification/full-video-asr-live-20261030/probe.log)、[组合回归](../../outputs/verification/full-video-asr-live-20261030/regression.log)。
- **指标对比：** 恢复期领取/无界失败的 3 个复现断言由失败转通过；真实 ASR hook 和 10 窗口取证从未测变为一个成功样本。ASR 阶段 **331.318 秒**，探针进程峰值 RSS **2,565,750,784 bytes**（macOS `ru_maxrss`）；当时另一条开发 API/Consumer 已启动，因此存在共享机器负载，数值不代表独占资源性能、整片峰值或全课程 ETA。旧路径未做同条件 A/B，不报告加速或准确率提升；本次可支持提交评审，真实整节课程及 Kimi/Refine/Index 端到端仍待验收。

2026-10-30 PR 关联迁移：用户要求分支只表达需求类型，不含 Agent 身份。将同一实现提交 `b2fb02c4` 迁至 `feat/full-video-asr`，创建 [PR #12](https://github.com/owariband/Seekino/pull/12) 替代 [PR #9](https://github.com/owariband/Seekino/pull/9)。迁移时核对两个远端 PR 的 head SHA 一致；后续只更新 TRD、问题清单和本条关联记录，业务代码及既有验证结果不变。Agent 身份写在新 PR 描述中，不重新执行模型或解析任务。

<a id="devlog-kimi-concurrency-20261030"></a>

## 2026-10-30：Kimi Code API 10 路并发实测

### 问题与现状

用户希望确认当前 Kimi Code API 是否支持一次 10 路并发。官方介绍有最多 30 路的说明，但账号共享额度、滚动频控与实际请求负载可能影响结果；配置中的 `codex_local.max_concurrency=10` 又容易与 Kimi 混淆。关联 [B21](problems.md#b21kimi-code-api-10-路并发能力未验证)，本次目标是验证账号的 API 并发接受能力，不直接提高解析槽位。

### 方案候选

采用同一已配置 Key/`k3.1-pro`、Gateway Kimi profile 参数，发送不含业务数据的短 Python 输出题；做 3 次串行对照和两轮 10 路并发，关闭 SDK 重试及 fallback，用请求标识/整数答案校验响应完整性。HTTP hook 记录在途数与发出跨度，避免只创建多个协程却没有真正同时请求。选择有界短请求控制额度和现场干扰，不以此取代多模态长请求测试。

### 方案验证

2026-10-30 10:47（Asia/Shanghai）实测全部 23 请求成功；两轮并发分别 10/10，零 429、零网络错误、零重试。峰值 HTTP 在途均为 10，发出跨度为 5.957 ms 与 7.549 ms，响应模型均为 `k3.1-pro`。结果 token 合计 6,484，未查询套餐余额，不折算金额。没有修改服务配置、运行队列或业务库，不启动视频解析。API Key 未写入产物。

证据和复跑入口见 [报告](../report/gateway/kimi-concurrency-20261030.md)、[逐请求记录](../../outputs/verification/kimi-concurrency-20261030/results.json)、[在途事件](../../outputs/verification/kimi-concurrency-20261030/events.json)。此次为原始 API 测试，未验证完整 Seekino Agent 工具循环，也未审计其他同时使用该账号的工具负载。

### 指标对比

串行 3 请求整组 14.306 s，单请求中位 4.665 s；并发第一轮 10 请求整组 6.582 s、中位 5.126 s，第二轮整组 7.646 s、中位 4.717 s。题数不同且连接有预热影响，不据整组耗时比宣称加速倍数；两轮样本不报告尾延迟或长期稳定率。结论是短请求 10 路可用，长上下文、视频帧、持续吞吐及整季完成时长仍未验证；保留现有生产槽位，避免将供应商可接受的请求并发误当整个解析系统的最优设置。

<a id="devlog-release-parse-duration-20261029"></a>

## 2026-10-29：普通展示模式开放解析有效总时长

### 1. 问题与现状

服务端已有实际执行区间的有效总时长，任务页却仅在 debug 模式展示，prod/release 用户无法查看。该数值排除排队和停机，不等于创建任务至完成的墙钟时间。

### 2. 方案候选

直接复用已有时长字段和格式化逻辑，解除展示的 `isDebug` 条件；无需新增计时 API 或修改数据。内部执行计数及模型用量明细仍只供 debug 使用。

### 3. 方案验证

更新既有 release 行为测试：后端有效时长 125000 ms 显示为 02:05，同时继续隐藏内部解析计数。任务页 20 项测试、TypeScript 检查与 production build 通过。未部署生产服务，未做生产浏览器验收。

### 4. 指标对比

release 从不显示解析时长变为显示后端已有的有效总时长；未开始／未记录和已记录零值沿用原规则。未新增请求、依赖或数据迁移，不声称改善解析速度。问题状态：[F10](problems.md#f10普通展示模式隐藏解析有效总时长)。


<a id="devlog-module-publish-20261029"></a>

## 2026-10-29：稳定模块分组提交与并发修复隔离

### 问题与现状

hypnos 要求忽略根目录 `seekino/` 的参考分析与会话交接副本，并将其余已稳定改动按模块 push 到普通 dev。工作区同时存在正在进行的解析失败修复，不能将变化中的现场文件整包视为已经验收。

### 方案候选

从 `45e3262c` 建立临时源码快照，按忽略规则、Summary 标题与系列封面、观看页、上下文错误反馈、本地 embedding、WhisperX 基础接入、解析协议实验、公共文档拆分提交。共享文件按功能切分。`/seekino/` 与已有 `/assets/` 保留本地且忽略；带本机绝对路径的 `config/local-dev-config.yaml` 不纳入提交。并发新增的 OCR、provider token 校准和媒体等待恢复修复保留在工作区，不纳入本次候选。

### 方案验证

针对准备提交的源码快照运行 34 个后端测试文件：**1496 passed**，39.29 秒，1 条既有 Starlette/AnyIO 弃用警告。覆盖 Summary、资料库、上下文、embedding、ASR、解析任务与索引、启动装配和实验协议；使用测试隔离环境，不代表真实模型效果。前端资料库/观看页相关的 5 个测试文件 **90 passed**；`npm --prefix web run build`（含 TypeScript 检查）通过。`uv lock --check --offline` 通过，204 个锁定包。本轮未运行真实模型、重启正式服务、迁移数据库或重建业务索引。

本次验证快照保存在本机临时目录 `seekino-module-publish-v16btr2l`，不作为跨设备持久证据；对应测试入口及实现随模块提交。历史报告中的 `outputs/`、`assets/` 原始证据仍按原记录保存在本机或迁移材料中，不随源码发布，不能把新克隆仓库视为包含全部实验输入。

### 指标对比

从混合工作区改为八个可追踪模块提交；119 个文件纳入快照（共享文件可跨提交递进），参考视频、权重、数据库文件及本机配置不纳入。此次结果是提交范围与相关回归的确认；WhisperX 全系列解析、整库检索质量和并发修复的真实任务恢复仍按各项问题继续验收。

<a id="devlog-deploy-mcp-publish-20261029"></a>

## 2026-10-29：本机部署与 MCP 分组提交

### 问题与现状

hypnos 要求仅提交、推送已完成的本机部署与持久化目录、MCP 接入，并忽略新素材。多个会话共享 dev 工作区，当前还混有本地向量、WhisperX、Summary 标题和 UI 改动。B18 的历史真实短片验收使用当时完整工作区，不能当作本次分离提交版本的新实机验证。

### 方案候选

从 `424a2fb5` 提取两组必要修改，在独立临时副本验证；共享文件仅选取相关内容。保留原生启动器、媒体根默认值、FFmpeg 兼容和 SSE 停服修复，以及 localhost MCP 三工具、证据核验和 API 装配。`assets/` 未跟踪素材由 Git ignore 排除，已跟踪发布资产保留，媒体和数据库仍须独立备份；本地个人模型配置不入库。

### 方案验证

隔离副本运行部署、配置、生命周期、抽帧、媒体、持久层、MCP、bootstrap 和 HTTP API 的 11 个测试文件：898 项通过，2 项失败。失败项属于误选的本地文本向量新增测试，其对应功能不在提交范围；已将该测试文件完全恢复为基准版本并排除其改动，原有 5 个同类用例单独复验通过。未删改共享工作区中的这两个新增测试。`uv lock --check --offline` 通过，保持原锁定依赖版本，仅将既有 MCP SDK 显式列为直接依赖。原始日志在本机 `/private/tmp/seekino-publish-20261029/tests.log`，临时证据不随 Git 分发。

### 指标对比

本次限定为两组可独立追溯的提交；排除本地向量、WhisperX、Summary 标题及 UI 的其他改动。未重启服务、迁移真实数据或调用模型，没有新增实机性能与质量结论。B15/B17 的剩余业务验收继续按原问题记录推进。

<a id="devlog-whisperx-20261029"></a>

## 2026-10-29：WhisperX 正式可选接入与字幕字词时间

### 问题与现状

用户希望以可选本地 WhisperX 解决 ASR API 成本及字幕对齐。核对 dev：本机实验直连 whisper.cpp；正式 Gateway 只有 Ark/openai_audio。开发配置文件名 `local-kimi.yaml` 未反映多供应商、多角色范围。

### 方案候选

增加正式 `whisperx` 来源，在既有工厂内调用 WhisperX 3.8.6 的转写和 align，依赖/权重由用户选装。保留云 ASR。开发配置更名 `local-dev-config.yaml`。Gateway 复用 source/model runtime，Engine 转毫秒，Server 加原视频偏移并沿用 evidence JSON。无分数词移除上游插值时间；字幕排版与开关不由本次接入承担。

### 方案验证

首次 53 项定向测试中 7 项失败：测试环境未安装可选 NumPy，及测试修改冻结绑定。修正测试替身以匹配无 ML extra 环境，并使用不可变绑定副本；没有将真实模型失败改写为成功。随后 Gateway 配置/工厂/转写、Engine 和 Server 正式 ASR 入口共 189 passed。补查固定 upstream 实现，增加 tokenizer 及具体 Punkt 语言资源预检，防止缺文件时触发上游下载；新增 YAML 选择、Canvas 字词证据保留及回归后最终 **256 passed**。

扩大到 Server 配置、完整 Parse Worker 和 observations 回归：首次 512 passed / 6 failed，六项均在既有声纹匹配模块导入 NumPy 时失败。用 `uv run --no-sync --with numpy==2.2.6` 仅在临时环境补包，复验 **518 passed**。项目 `.venv` 未改变，复查仍未安装 NumPy。这两组测试有 4 项重叠；均无真实模型请求，数据库测试用隔离 SQLite，不等于 MySQL 运行验收。完整复验入口：

```bash
uv run --no-sync python -m pytest tests/gateway/test_whisperx.py tests/gateway/test_transcription.py tests/gateway/test_config.py tests/gateway/test_model_factory.py tests/engine/common/test_transcription.py tests/server/test_video_parse_worker.py::test_transcription_uses_frozen_source_prepared_media_and_absolute_milliseconds tests/agent/video_parse_v2/test_context_canvas.py -q
uv run --no-sync --with numpy==2.2.6 python -m pytest tests/server/test_config.py tests/server/test_video_parse_worker.py tests/engine/parsev2/test_observations.py -m "not live_agent" -q
```

依赖锁解析及 `uv lock --check --offline` 通过，新增 68 个可选依赖，既有包版本不变。默认环境未安装 WhisperX/NumPy；本轮没有下载权重、运行真实推理、重启服务或更换当前活动 ASR。原 `.env.local` 配置路径已更新，两个配置文件均可加载，旧文件不存在。相关 diff 空白检查和主 TRD 本地链接检查通过。

2026-10-29 后续实机部署（同日，覆盖上述安装前状态）：使用 `uv sync --frozen --inexact --extra whisperx --extra speaker` 安装项目环境；large-v3 与三种语言对齐权重逐文件校验。英语/日语原片、中文合成语音经正式 Gateway 成功，分别得到 42/9/38 个有效字词时间。首次因默认 Torch 缓存权限失败，改为项目内 `TORCH_HOME` 后通过；中文合成首次返回空 WAV，重新通过 macOS 语音服务生成后验证，未绕过空音频检查。补跑 WhisperX、llama.cpp embedding、transcription 和 text-only indexing 测试 **75 passed**。

「课程闭环进行」完成后独立复核 embedding 文档/查询的 1024 维单位向量，切换 `local-dev-config.yaml` 并重启开发服务。Qwen3 使用新 ES 索引和 generation 2，旧索引保留。8 集完整 Karpathy 原片经 Upload API 提交并形成 MySQL 任务，首集消费者已领取并调用真实 WhisperX；Kimi 视觉调用出现远端断开与 HTTP 504，进入自动重试，未将提交成功记作解析完成。模型固定版本、耗时、证据及恢复入口见[部署报告](../report/gateway/local/whisperx-deployment-20261029.md)。

### 指标对比

正式 ASR 来源新增本地 WhisperX；新合同保留 language/字词时间和对齐分数，旧响应形状回归通过。共享 runtime 测试中两次 Chunk 调用只有一次 ASR 加载、一次对应语言 aligner 加载；超时结果不会提交。API 成本减少属于本地方案的机制预期；三语单次调用耗时已记录，但整体吞吐、峰值内存、系统性识别质量和人工边界误差待测。中文样本的识别错误保留在证据中。依赖与各语言权重是新增维护成本。完整字幕功能未完成，不将字词证据接通写成播放器已支持字幕。主设计：[本地 WhisperX TRD](../design/03-product-reassessment/local-whisperx-asr-trd.md)，状态：[B20](problems.md#b20本地-whisperx-正式接入与字幕时间证据)。

<a id="devlog-llama-cpp-embedding-20261029"></a>

## 2026-10-29：本地向量直连 llama.cpp 与独立运行入口

### 问题与现状

用户希望直接使用 llama.cpp，缩短从本地 embedding 到课程解析验证的准备时间。
上一轮已经跑通 Ollama 文本向量，但没有原生 llama.cpp embedding 适配器。过程中用户
进一步收窄分工：本项只负责 local embedding 和 Ollama → llama.cpp，Whisper/音轨
对齐由另一项工作完成，不做全量解析。相关状态为 [B19](problems.md#b19课程本地文本向量与可关闭的视觉索引)。

### 方案候选

采用已有原生 llama-server 和 Qwen Q8_0 GGUF，不安装新的推理框架。Gateway 增加
`llama_cpp_embedding` 来源、能力声明与工厂接线；通过 `/v1/embeddings` 请求浮点向量，
校验响应模型、单条 index、维数、有限非零值并归一化。查询单独添加 instruction，
协议/指令使用独立 encoder revision。调用沿用 Gateway 用量、超时与取消边界。
保留 Ollama 适配器供已有配置使用，直连运行不依赖该服务。

新增 `scripts/run_local_embedding.py`，只启动准备好的原生程序与 GGUF，固定 last-token
pooling、单槽位、4096 token 上下文与 loopback 18843。它不下载模型、不启动 ASR，
也不是跨平台安装器；本次仅 Mac ARM64 实测。前台运行期间模型保持加载，未实现
Ollama 的自动空闲卸载，用户需显式停止，这是减少模型管理层后的维护取舍。

### 方案验证

原生程序来自此前校验过的 Ollama 0.40.13 发布包，提取其中独立 ARM64 llama-server；
`otool -L` 核对该架构只链接系统库。程序放在 `outputs/tools/llama.cpp/llama-server`，
相关许可证一同保留。GGUF 使用独立路径
`outputs/local-models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf`，与原文件
建立硬链接以免重复磁盘数据，运行不读取 Ollama manifest；SHA-256 已完整复核。
[安装来源与哈希](../../outputs/verification/llama-course-loop-20261029/standalone-install.json)。

独立进程启动后，通过真实 Gateway 进行文档、中文查询各一次编码，均为有限、
L2=1 的 1024 维向量；同时检查 Ollama 11434 端口未监听。6002 token 超长请求返回
400 `exceed_context_size_error`，没有静默截断。
[最终探测](../../outputs/verification/llama-course-loop-20261029/standalone-gateway-probe.json)、
[超长输入](../../outputs/verification/llama-course-loop-20261029/overlong-input.json)。

早期真实接线暴露 `scripts/local_services.start_elasticsearch` 默认创建多模态 mapping，
随后 CLI 按 text 合同检查失败。已修复启动器从同一模型配置读取 input_mode；失败时
创建的空验收索引保留，后续使用新的独立索引，没有删除或重建旧业务库。

在用户收窄范围前，已通过正式 HTTP 创建 Series 并提交唯一 60 秒 Micrograd 摘录，
原片范围 10:00–11:00；独立 MySQL 数据库 `seekino_llama_course_20261029_test` 与
`seekino_llama_course_*_20261029_test` ES 索引。任务
`b5b1f1c5-6d7b-48bd-b52e-d5cc26b2cf7b` 完成：1 Chunk、1 Refine、1 已索引 Chunk，
记录执行时长 109.346 秒，其中索引阶段 2.682 秒。该过程使用已有 Whisper.cpp 接口和
豆包画面理解，不能当作 WhisperX/强制对齐接入或课程内容质量通过。
[任务状态](../../outputs/verification/llama-course-loop-20261029/tasks-after-scope-change.json)。
用户明确分工后没有新增解析/检索/问答任务，已停止本次临时 API/Whisper，见
[范围收敛记录](../../outputs/verification/llama-course-loop-20261029/scope-stop.json)。

验证：新 llama.cpp 适配测试 9 passed；原本地向量/text-only 测试 41 passed；
本地启动器 83 passed（1 条既有 Starlette 警告）。测试覆盖调用合同、错误拒绝、
编码指令、工厂、关闭行为，以及多模态/text 两种启动映射。不宣称全仓回归通过。
未修改 Whisper 源码、默认业务模型配置或旧向量库；没有全量解析、重启恢复、
浏览器回看与追问验收。之后与 Whisper 工作合流时，应使用相同编码合同的新索引。

### 指标对比

| 项目 | 原 Ollama 试点 | 本次 llama.cpp 直连 |
| --- | --- | --- |
| 运行依赖 | Ollama daemon + runner | 独立 llama-server；11434 未监听仍可编码 |
| 运行文件 | 约 580.53 MiB 通用解压包，不含权重 | ARM64 可执行文件 19,658,832 bytes，另含许可证；非全平台完整包体对照 |
| 权重 | Q8_0，639,150,592 bytes | 同一 GGUF，未再下载 |
| 输出合同 | 1024 维、L2、查询 instruction | 相同维数/规则，独立协议 revision；未证明可直接混用旧向量 |
| 启动映射 | 本地脚本遗漏 text 合同 | 按配置选择，83 项启动回归通过 |
| 本机文档/查询编码 | 见上一轮测量，非同条件 A/B | 单次 314.54 / 20.80 ms，不作性能胜负或 SLA |

本轮交付是 embedding 接入与运行依赖的实际切换；整体课程闭环仍等待后续合流验证。

<a id="devlog-local-text-embedding-20261029"></a>

## 2026-10-29：本地 Qwen 文本向量、视觉开关与课程片段小实验

### 问题与现状

用户选择 Qwen3-Embedding-0.6B 本地实验，要求视觉向量可关闭，课程继续保留 OCR/VLM
理解。修改前 Gateway 的索引编码路径依赖 Ark，多模态合同强制要求 Chunk 视频和
Event 图片向量；供应商请求还存在 404。因此仅替换模型名不能让纯文本模型完成索引。
本次对应 [B19](problems.md#b19课程本地文本向量与可关闭的视觉索引) 与
[主设计](../design/03-product-reassessment/local-text-embedding-trd.md)。

### 方案候选

沿用远程多模态编码可以保留视觉召回，但不能解决用户当前的配置依赖。本次按用户选择
新增 Ollama 原生文本适配器，仍保留原多模态路径；未安装或比较 BGE-M3、Qwen3-VL。
`input_mode: text` 关闭索引专用采帧、图片/视频向量及视觉召回，ASR/OCR/VLM 配置不变。
模式、模型与查询指令冻结进任务，新的向量空间使用独立物理索引。

查询使用配置的 `Instruct: ...\nQuery: ...`，文档直接编码；响应检查模型身份、维数、
有限非零值并 L2 归一化，禁止静默截断。旧配置默认 multimodal，旧索引元数据保持兼容。
索引初始化从配置推导模式；不支持的 sparse 向量配置在启动/上传持久化前拒绝，BM25
仍可用。上传页面的 text 模式显示视觉关闭、隐藏无效 FPS；没有新增任意单任务模式切换。

新增维护成本是本地推理服务、模型权重与启动管理；并未降低 ASR/VLM 的推理成本。
模型 tag 本身可被重新下载覆盖，本轮保存实际 manifest digest；后续替换权重须重新
核对模型身份并重建向量，不能以同 tag、同维度保证空间一致。

### 方案验证

环境为 Apple M5、16 GiB、Elasticsearch 8.19.22。安装官方 Ollama 0.40.13 到
`outputs/tools/ollama/`，压缩包 SHA-256 为
`bae29252232b9e1358cf5801a7d65a07442af18fb5467cb68c7fc3711f59c511`。
模型为 `qwen3-embedding:0.6b / Q8_0`，权重 639,150,592 字节，manifest digest 为
`ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`。
模型放在 `outputs/local-models/ollama/`，服务监听 `127.0.0.1:11434`，使用 Metal。
服务当前独立运行，尚未加入项目统一启动脚本；退出后可用本次
[启动脚本](../../outputs/verification/local-text-embedding-20261029/run-ollama.sh)恢复。

取 4 段真实 Karpathy 原片各 60 秒：Micrograd 10:00–11:00、MLP 20:00–21:00、
Backpropagation 20:00–21:00、Tokenizer 02:00–03:00。本地 Whisper large-v3-turbo Q5
英文转写分别用时 4.383 / 3.397 / 3.859 / 4.032 秒；原始转写未经纠错。人工将文本
投影为 OTHER Event，8 条中文问题在读过转写后编写、检索前固定。

通过 [实验入口](../../evals/local_text_embedding.py) 调真实 Gateway 与 Engine：4 Chunk
和 4 Event 使用本地 Qwen 编码，写入独占的随机 `seekino_local_text_test_*` 索引；
检查发布、readiness、读回，执行词法/文本融合和 Engine 同版本来源核验。每条查询
只编码一次，供两类索引共享。共 16 次文本编码，0 次视觉编码、0 次生成模型调用，
读回额外编码 0 次；本次索引保留供查看。没有远程模型 API 调用费用。

复跑命令（会新建另一个隔离测试索引，前提是本地 Ollama 与 ES 已运行）：

```bash
.venv/bin/python -m evals.local_text_embedding \
  --config outputs/verification/local-text-embedding-20261029/config.yaml \
  --dataset outputs/verification/local-text-embedding-20261029/dataset.json \
  --output outputs/verification/local-text-embedding-20261029/trial-repeat.json \
  --es-url http://127.0.0.1:9200 --keep-indexes
```

原始证据：[trial.json](../../outputs/verification/local-text-embedding-20261029/trial.json)、
[dataset.json](../../outputs/verification/local-text-embedding-20261029/dataset.json)、
[模型探测](../../outputs/verification/local-text-embedding-20261029/model-probe.json)、
[指标汇总](../../outputs/verification/local-text-embedding-20261029/metrics-summary.json)。
资料在本地 outputs 下，换机复跑需携带原视频、模型与实验材料。

验证分层：Engine 相关定向回归 395 passed；Gateway 本地/Ark/factory 定向回归 118 passed；
root 的 Gateway、text-only Server、实验入口和真实隔离 ES 组合 50 passed；最终 CLI、
配置、启动与上传的 sparse 修复回归 631 passed。这些集合有重叠，不相加为独立总数。
`npm --prefix web run typecheck` 通过。Gateway 配置测试另有 3 个既有失败，agent 以
HEAD 版本配置代码复核仍失败，属于旧 fixture/默认配置假设不符，未在本次修复。
没有运行全仓回归。恢复验证来自 Coordinator 替身测试；真实 ES 只验证发布和读回。

未验证：完整 VLM 解析、正式 MySQL 任务终态/进程重启恢复、HTTP 回看追问、整库检索
质量，以及多任务并发内存。来源核验针对本次输入事实与原视频路径/毫秒范围，不能
等同生产 MySQL 回源。本轮未切换 `config/local-dev-config.yaml`（原 `local-kimi.yaml`，2026-10-29 更名） 或主服务，未迁移旧向量库，
未批量解析全部课程。B19 因正式任务验证缺口保留 TODO。

### 指标对比

| 指标 | 修改前 | 本次实测 |
| --- | --- | --- |
| 无视觉编码完成索引 | 不支持 | 4 Chunk + 4 Event 发布/读回通过 |
| 中文检索英文课程文本 | 无同数据基线 | 两类索引均 8/8 首位命中预期片段 |
| 视觉编码次数 | 多模态合同必需 | 0 |
| 本地模型 GPU 驻留 | 未部署 | Ollama 报告约 1.155 GiB，非系统总内存 |
| 首次文档编码 | 未测 | 2.131 秒，包含首次加载影响 |
| 课程实验查询编码，8 次 | 未测 | 中位 20.94 ms，范围 19.40–31.44 ms |
| 编码 + 两类 ES 检索 + 来源核验，8 次 | 未测 | 中位 59.73 ms，范围 47.69–111.57 ms |

这是一组 4 候选、读过转写后编写问题的探索性实验；8/8 不能作为整套课程召回准确率。
耗时不含 HTTP、MySQL 或生成回答，也不能作为产品响应 SLA。已改善的是本地文本编码
与无视觉索引可运行、错误配置提前暴露，正式课程闭环和质量收益仍需后续实跑。

<a id="devlog-series-artwork-20261029"></a>

## 2026-10-29：系列封面采用 Summary 候选与上次观看画面

### 问题与现状

用户觉得系列封面不合适，希望未观看时随机取各集 Summary 的推荐封面，观看后显示上次进度。原 Library 只选有关键帧的第一集；首页卡片有封面就不读取观看历史，大图还会用任意时间的预览帧盖掉静态封面。

### 方案候选

复用现有 `cover_uri` 与身份隔离的浏览器观看记录。服务端用 Series/Video ID 的稳定散列在 Summary 候选中抽选；比每次请求随机更稳定，且无需持久化随机状态。候选集改变时允许重新选取。前端只在有有效历史或缺少静态封面时加载原视频预览，保持毫秒和播放恢复合同。未引入跨设备观看记录或额外模型调用。

### 方案验证

后端 Library/Media/Cloud 权限回归 **74 passed**；前端 WatchHome、SeriesPage、PlaybackFrame、playbackProgress **47 passed**；typecheck/build 通过。新增测试首轮因 fixture 使用同一 hash 触发视频唯一约束、参数化数组被展开为函数参数而失败，分别修复测试数据和参数形状。多候选优先级、排序稳定、候选删除回退、过期观看记录、毫秒定位均有覆盖。

使用当前真实开发 API：原 `127.0.0.1` 页面保留用户进度，卡片帧实际 `currentTime=8.544`、可见且 readyState 4；用独立 localhost 站点存储验证未观看状态，Summary 图片成功加载，卡片没有 video 元素。随后关闭临时页。截图与 DOM 验收保存在 `outputs/verification/series-artwork-20261029/`，无需清空或伪造用户历史。当前真实库只有一个 Summary 封面，多集候选效果仅有隔离测试。服务经 `start.sh --branch dev start` 加载新源码，MySQL/ES 复用，无数据迁移。

### 指标对比

| 行为 | 原先 | 当前 |
|---|---|---|
| 未看系列默认封面 | 首个有关键帧的集 | 稳定随机的 Summary 封面；缺失保留回退 |
| 已看系列卡片 | 固定图或固定预览位置 | 上次观看位置的原片帧，本机为 8.544 秒 |
| Summary 无关键帧 | 不入系列候选 | 仍可作为系列封面 |
| 静态封面加载 | 大图会再取视频帧覆盖 | 未看时直接显示静态图；卡片图片懒加载 |

收益是让现有 Summary 成果真正用于系列展示，并能从封面辨认观看位置；未做审美盲评或大规模加载性能测量。Karpathy 当前仍无 Summary 成果，回退图不能称为 Agent 已选图。状态见 [F09](problems.md#f09系列封面未使用-summary-候选及上次观看画面)。

<a id="devlog-summary-title-20261029"></a>

## 2026-10-29：Summary 正式标题优先与内容标题回退

### 问题与现状

关联 [F08](problems.md#f08summary-标题未落到资料库展示且无正式标题时缺少回退)。用户询问 Summary 是否已接入解析及为何没有替换标题。源码与本机 MySQL 确认：末次 Refine 已同事务排队，独立 summary 消费槽实际运行，12 秒 Kimi 验收结果为 COMPLETE，简介及封面已发布；但官方标题限定导致本片 `title=null`，资料库又仅显示文件名，两个因素共同造成体验缺口。

### 方案候选

按用户确认的规则优先提取正式标题，没有可靠正式标题时生成简短事实标题；新增 `title_kind` 并复用现有证据校验、结果 JSON 和发布事务。正式标题仍需标题卡文字，生成标题可引用确实存在的场景/对白或已检查帧，长度上限 80 字符；不放宽集数、预告排除或租约/版本校验。前端资料库优先显示 `displayName`，文件名保留 tooltip 和回退。收益是普通素材也有可读标题；代价是生成措辞的语义忠实性需要模型判断和样例复核，引用存在不等于自动证明标题完全正确。

### 方案验证

Summary Agent / Service / Persistence 75 passed，前端 App/format 65 passed，typecheck 与 production build 通过。保留 [前状态](../../outputs/verification/summary-title-20261029/before.json)，在同一事务内只重置并领取验收视频当前 summary，复用既有 12 秒解析，调用 Kimi `k3.1-pro` 完成 5 次 summarize 请求；没有新增 ASR、Understand、Refine、Index 或整集调用。验收脚本首次因 DTO 不含展示字段在写入/模型调用前退出，改用 Video ORM 后执行成功，原运行证据未覆盖。

真实结果为「台阶夜谈《春日影》」，类型 generated；引用为粉发少女称赞《春日影》的原内容，台阶及夜晚由已检查原帧与同一 Chunk 支持。Assistant 核对原帧、引用、SQL 与页面，未用独立模型评分。浏览器确认资料库及观看页标题一致。最后在无活跃解析/总结/聊天时仅重启 API，加载新规则，MySQL/ES/Web PID 保持，健康与代理资料库查询通过。证据：[后状态及用量](../../outputs/verification/summary-title-20261029/after.json)、[资料库](../../outputs/verification/summary-title-20261029/library-title.jpg)、[观看页](../../outputs/verification/summary-title-20261029/watch-title.jpg)、[API 加载](../../outputs/verification/summary-title-20261029/api-reload.json)。

### 指标对比

| 指标 | 修改前 | 本次结果 |
| --- | --- | --- |
| 此片 Summary 标题 / Video display_name | null / null | 台阶夜谈《春日影》 / 同值，9 字符 |
| 资料库行标题 | native-acceptance-ep04-12s.mp4 | 台阶夜谈《春日影》 |
| Summary 实际集数 | null | null，组织集数仍为 1 |
| Parse 版本与状态 | 4 / COMPLETE | 4 / COMPLETE，Chunk 版本及来源指纹不变 |
| 本次 summary 单次耗时 | 未以同口径记录 | 59.591 秒，5 次模型请求成功，21,574 tokens |

只验证一段无标题卡短片的回退和展示；正式标题识别受离线回归保护，整季真实质量、标题卡发现率及延迟分布未验证。未来自动 summary 使用新规则，历史 COMPLETE 不隐式补跑；此项不恢复旧 MyGO 数据，也未启动 13 集重解析。

<a id="devlog-watch-chat-compact-20261029"></a>

## 2026-10-29：观看聊天面板收紧与设置折叠

### 问题与现状

关联 [F07](problems.md#f07观看聊天面板的固定栏和输入工具栏挤占正文空间)。用户提供的窄窗口截图中，顶部“边看边聊”、页签和暂停开关占据三行固定空间，底部工具栏继承移动端 44 px 按钮，聊天正文被挤压。用户要求去掉冗余标题、移动设置并压低输入框下部高度。

### 方案候选

选择合并顶部工具栏，把暂停开关收进原生 `details` 观看设置；补充点击外部、Esc 和关闭面板时的收起行为，复用现有偏好存储。输入区仅覆盖观看页的 `.embeddedPage` 样式，附件/发送按钮改为 30 px，工具行取消额外上边距，系列名保持单行并按可用宽度截断。保留输入区域与系列选择入口，避免删减实际功能；不新增设置页或组件依赖。

### 方案验证

`npm --prefix web test -- src/features/player/WatchPage.test.tsx` 为 24 passed；原暂停偏好用例增加设置默认折叠、Esc 焦点恢复、点击外部收起等断言。`npm --prefix web run typecheck`、`npm --prefix web run build` 通过。浏览器连接现有真实验收视频与已保存对话，在 491×815 窄窗口和 1280×900 桌面视口验证布局，检查设置开关、Esc、外部点击、聊天/选集切换、面板关闭重开；检查完已恢复默认视口，暂停偏好恢复为开启。没有发送新问题或调用模型。

证据：[浏览器量测](../../outputs/verification/watch-chat-compact-20261029/ui-report.json)、[窄窗口](../../outputs/verification/watch-chat-compact-20261029/compact-chat-narrow.jpg)、[桌面侧栏](../../outputs/verification/watch-chat-compact-20261029/compact-chat-desktop.jpg)、[设置弹层](../../outputs/verification/watch-chat-compact-20261029/settings.jpg)。未覆盖实体触屏设备或所有浏览器引擎。

### 指标对比

固定顶部由三行收敛为一行，改后实测 41 px；底部工具栏改后实测 30 px，空输入框实测 88 px，两种视口均无页面水平溢出。改前移动端按钮为 CSS 定义的 44 px，未重放旧实现做同视口像素量测，因此不报告整页净节省像素。正文可用空间增加，代价是暂停偏好需要多点击一次设置；交互状态仍由现有 React 偏好与原生 disclosure 管理，没有新增全局状态或依赖。此项不改变数据恢复、解析或问答业务质量结论。

<a id="devlog-native-local-20261029"></a>

## 2026-10-29：全原生 localhost 环境与 Kimi 业务验收

### 问题与现状

关联 [B18](problems.md#b18全原生-localhost-环境与真实业务验收) 与 [B17](problems.md#b17持久媒体目录与旧资料库恢复)。用户最终要求 MySQL、ES、API 和 Web 都在本机运行，避免 Docker，方便测试实验。既有 Python 3.12.16、uv、Node 24.22.1、npm 和 FFmpeg 9.0.2 可用，但没有原生 MySQL / ES；启动器不负责启动 ES，MySQL 默认数据仍在易被当作输出的目录。原片归档已完成，旧业务成果尚未找到，不能把新库初始化称为旧库恢复。

### 方案候选

按用户选择使用官方 macOS arm64 归档，安装程序放在可重新下载的 `outputs/tools`，数据库持久文件移出默认日志位置，采用 `data/mysql`、`data/elasticsearch`；媒体沿用 `assets/library`。本机 `.env.local` 选择二进制路径，保留凭据。既有 `start.sh --branch dev` 统一启动、检查、停止原生服务，不增加 Docker、独立队列或本地模型权重。ES heap 配置为 512 MiB、MySQL buffer pool 为 128 MiB，均仅绑定 loopback；这是配置值，不是整套服务的实测总内存或耗电值。

用户明确允许 EP04 原片 8–20 秒的 12 秒验收片向外部模型发送必要媒体/派生信息及检索问答，并进一步要求尽量使用 Kimi。本机有效配置单独保存在 `config/local-dev-config.yaml`（原 `local-kimi.yaml`，2026-10-29 更名），通过 `SEEKINO_MODEL_CONFIG` 选中，七个生成式角色使用 `kimi_coding/k3.1-pro`，ASR 与 embedding 使用 Ark 专用模型，向量保持 1024 维；不修改仓库面向其他环境的默认模型组合。验收只使用这一短片，未重解析 12 集归档素材。

### 方案验证

- 官方归档校验：MySQL 8.4.13，165,362,783 bytes，官方 MD5 匹配；ES 8.19.22，517,276,189 bytes，官方 SHA-512 匹配。下载中断后续传完成，二进制实际运行版本与选择一致。来源与校验值保存在 [setup-report.json](../../outputs/verification/native-local-20261029/setup-report.json)。
- 原生启动器加入 ES 启动、MySQL basedir 自动识别、checkpoint / Event 初始化、exec 后 PID 归属及退出竞态保护；专项最终 82 passed。旧 MySQL 非空目录仍要求明确选择，不会悄悄切换成新空库。现有 FFmpeg 9 不再接受 `-filter_script:v`，改为探测 `-/filter:v` 能力并保留旧版 fallback；抽帧与解析媒体 51 passed，含本机真实 FFmpeg。前端 `npm --prefix web run build` 通过。
- 四项服务启动成功，MySQL 28 张表及 22 条 checkpoint migration 可读；两个 ES 主分片正常，单节点 yellow 由副本未分配造成。API / Parse Consumer 健康，前端代理与直接 API 资料库响应一致。空库阶段完整 `stop → start` 通过且未重复初始化 MySQL；浏览器资料库和播放器正常渲染，Docker socket 不存在。记录见 [runtime-report.json](../../outputs/verification/native-local-20261029/runtime-report.json)。
- 短片上传与播放：上传、分片、complete 均成功，全文 HTTP SHA-256 与源片 `88a4220cf3a206ae94727c2a1b535b65d424f0f92618ebe07baaaa9c3b48484a` 一致；206 Range 字节与 `Content-Range` 正确，ffprobe 识别 H.264 / AAC、640×360、12.012 秒。浏览器实际播放从 0 推进到 12012 ms。证据见 [http-report.json](../../outputs/verification/native-local-20261029/http-report.json) 与 [播放器截图](../../outputs/verification/native-local-20261029/browser-player.jpg)。
- 首次 Kimi 配置继承了视觉 `high/medium`，在本机通用视觉适配器构造阶段失败；改为 `none` 后七角色构造检查通过。该路径实际省略 reasoning 参数，不表示关闭供应商 thinking。当前实现的 `retry` 保持冻结配置，必须用显式 `parseConfig` 的 `reparse` 才能采用修正配置；本轮据源码修正了原模型文档的错误说明。失败版本 1 保留，同一视频的版本 2 已完成 ASR / Kimi OCR，但理解出现反复工具调用并超时。
- Context 诊断：临时开启本地原始工具 trace，证实模型误填 Schema 字段后，压缩摘要丢失了“Extra inputs are not permitted”的纠错反馈；37 张图片固定估算约 75,776 tokens，也高于原 60,000 压缩目标。通用压缩改为近期反馈优先、保留有界错误/版本回执；真实记录离线重放从丢失错误变为保留错误，压缩后估算仅增加 4 tokens。直接 24 passed；扩大 Gateway 回归 243 passed、1 个既有 fixture 耗尽失败，已用 HEAD 原始 context.py 独立复现，不能写成全套通过。见 [真实记录重放](../../outputs/verification/native-local-20261029/context-feedback-trace-replay.json)。本机 Kimi 本地预算设为 262,144 tokens、压缩目标 150,000；未验证长视频或账号 1M 上下文权限。
- 观看页 SSE 暴露 Uvicorn 默认无期限 HTTP drain 会阻塞 lifespan 退出。`server.main` 增加 5 秒 HTTP 收尾期限，之后仍按原顺序等待业务清理，不跳过租约或强杀；原生启动、入口、生命周期相关测试合计 95 passed（含前述 82，不能相加）。
- 版本 3 的 Kimi 理解与 Refine 成功；索引调用旧 `doubao-embedding-vision-250615` 返回当前账号/接入点不可用的 404。改用 `251215` 的合成文本真实预检返回 1024 维，耗时 0.871 秒。新模型采用独立 Chunk/Event 物理索引，冻结的旧任务不改，显式新配置创建版本 4。旧模型并非全网停服：官方只标 EOM、保留存量服务，见 [官方公告](https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=zh)。
- 总结路径漏传工具 harness，导致同时约束必选工具与最终 JSON，真实六次响应均未调用工具。总结宿主改用已有 DeepAgents 完成工具合同，仍要求 `submit_video_summary` 成功；对应 Agent/Service/Persistence/Gateway 回归 166 passed。见 [诊断记录](../../outputs/verification/native-local-20261029/summary-harness-diagnosis.json)。版本 4 已加载此修复，真实总结成功，104 字描述及封面完成发布并通过 SQL/HTTP/文件哈希核对。

- 版本 4 最终为 `COMPLETE`，API `processingDurationMs=316737`（约 5 分 17 秒，非旧 SQL 兼容字段）；发布 1 个 Chunk、6 个 Event。查询“粉色长发女生说春日影是首好歌”返回这段视频和 [0,12012) ms，证据与原片开头字幕一致。Kimi 观看问答调用 `understand_current_video` 后回答中日原句，SSE `answer.completed`，42.8 秒，两条消息及最新 checkpoint messages blob 54,180 bytes 持久保存。见 [检索报告](../../outputs/verification/native-local-20261029/http-report.json)、[问答报告](../../outputs/verification/native-local-20261029/chat-report.json)。Parse/Refine 验收配置为 `maxModelCalls=12`、`maxAttempts=1`；Summary 与 Chat 沿用各自预算，不表示整个业务只有 12 次模型调用。
- 完整带数据重启：保持真实 SSE 连接，四项受管服务 10.418 秒正常停止，再用相同持久目录启动。只读事务核对最新任务、Chunk/Event 精确版本、媒体哈希、摘要和封面、消息正文、checkpoint 引用均通过。核验脚本早期因 SQL `usage` 保留字未引用而报错，修正查询后通过，未修改业务数据来迁就验收。见 [重启前](../../outputs/verification/native-local-20261029/persistence-before-restart.json)、[重启后](../../outputs/verification/native-local-20261029/persistence-report.json) 与 [SSE 停服记录](../../outputs/verification/native-local-20261029/sse-shutdown-report.json)。
- 浏览器实查修复：媒体 `readyState=4` 却进度条 disabled；测试复现 metadata 提前到达及同媒体时长刷新导致就绪状态被清空两条路径，未声称已唯一确定现场事件顺序。播放器改用 layout 初始化并读取已有 metadata，同一媒体时长刷新保留状态；48 项播放器/观看页相关测试、51 项播放器/身份隔离测试分别通过（重叠不相加），typecheck / production build 通过。浏览器重启后实测 Home、End、后退 10 秒，停在 2.012 秒，原问答可恢复；[前后证据](../../outputs/verification/native-local-20261029/player-readiness-diagnosis.json)、[最终截图](../../outputs/verification/native-local-20261029/browser-acceptance-final.jpg)。
- 用量边界：已记录全部失败/成功解析版本，而非只呈现最后成功一次。`model_usage` 中 107 条解析记录有 104 条返回 tokens，已报告总量 1,254,023（其中已报告 cached input 1,001,145 是 input 子集），另 3 条未知；本次 Chat 另有 3 条、13,338 tokens。`parse_model_call` 为空，数据库没有本次费用估算或账单金额；不把未知费用写成 0，两张表及 SDK 聚合不重复相加。额外合成文本预检单列，不计入视频任务。见 [用量历史](../../outputs/verification/native-local-20261029/usage-history-report.json)。临时详细 trace 配置和防休眠进程已关闭，既有诊断证据保留。

### 指标对比

本机从无 MySQL / ES、无法启动正式链路，变为四项原生服务可启动、HTTP 数据边界可访问、真实短片可上传与播放，持久数据库和媒体目录有明确保留规则。新增维护成本是两套本机数据库二进制及少量启动器生命周期逻辑；省去本机容器虚拟机。没有测量长期耗电差异，不以 heap / buffer 配置代替进程总占用。生成式配置已切 Kimi，真实短片验收中暴露的配置、Context 反馈、总结工具合同、长连接退出与播放器就绪问题已修复并完成本机闭环。最后成功解析约 5 分 17 秒、观看问答约 42.8 秒，只能作为这一样本的实测，不能视作长视频性能或全面质量达标。旧 MyGO 解析成果恢复仍是独立缺口。

<a id="devlog-persistent-media-20261029"></a>

## 2026-10-29：持久媒体移入 assets/library 与旧数据恢复核验

### 问题与现状

关联 [B17](problems.md#b17持久媒体目录与旧资料库恢复)。用户重新下载 MyGO，希望复用旧 MySQL 成果，并要求重要原片不要继续以 `outputs/server/artifacts` 为默认目录。本机旧媒体根实查无文件，原片此前已移到 Git 忽略的 `assets/example_video_mygo`；数据库保存的 `artifact://...` 不会自动扫描样例目录。最初 MySQL 3306 拒绝连接，Docker 未运行，不能以素材已到位代替旧库恢复。

同日全原生环境验收后，用户发现首页只有新验收视频，质疑已迁来的数据库为何没有恢复 MyGO。追加调查只读核对运行中 API 的连接目标、本实例全部数据库、已知 SQL 和迁移归档，区分“旧库已经存在但未连接”与“迁移材料未包含旧业务数据”；没有据此创建替代系列、导入 SQL 或重解析旧 MyGO。

### 方案候选

直接复制可保持路径，但会增加约 5.06 GB 的独立副本；指向根外样例目录的软链接不满足存储路径边界。选择现有 `SEEKINO_ARTIFACT_ROOT` 配置能力，将宿主机默认和本机有效值改成 `assets/library`，保留环境与 URI 合同。原片归档到 `sources/mygo`，通过同盘硬链接保留旧评测路径，待取得旧库记录后再按实际 URI 建立正式关联；不虚构系列、视频 ID 或解析成果。Docker 既有持久卷合同没有改动。

用户在恢复准备过程中明确不希望本机运行 Docker，已执行停止；云服务器部署方式仍在讨论。随后按用户授权将私钥移到本机 `~/.ssh/seekino.pem`（0600），保留既有配置并添加 `seekino-server` SSH 别名。主机可 ping 通，但首次直接连接 22 端口及独立 TCP 复核超时，尚未进入认证。用户补充已有 docrag 的 Clash 专用代理方案后，SSH 接入本机 SOCKS，并仅为新服务器 IP 的 TCP 22 增加同一日本节点优先规则。持久订阅规则和运行配置均备份、校验并热加载，12 个既有代理组选择保持不变；随后以 `ubuntu` 成功登录，第二次合成数据传输也成功，规则计数记录两次命中。先前超时不能据此归因为远端安全组。

远端只读检查：4 vCPU、约 7.25 GiB 总内存／6.67 GiB 可用、约 107.6 GiB 磁盘剩余和 1.94 GiB swap；常见项目路径未发现 Seekino，3306/9200 未监听。未安装或启动远端数据库、上传素材、部署应用。可选分工是本机 API/前端/原片、云端 MySQL/ES，避免先上传全部素材，但数据库跨代理访问的实际性能未验证。

### 方案验证

- `test_config.py`、`test_local_services.py`、`test_artifact_storage.py`：422 passed、1 个 Starlette 弃用 warning，17.63 秒。验证默认值、显式覆盖与既有存储边界，不代表真实资料库可用。
- 12 个完整 MP4 共 5,055,403,557 bytes，移动前后 SHA-256 和大小一致，`ffprobe` 可识别 H.264/AAC；新归档与样例路径 inode 相同。10 集哈希命中历史验收记录，EP01、EP07 历史哈希未确认，第 11 集 `.part` 保留在 Downloads。实际 `.env.local` 解析为 `assets/library`，12 个持久原片路径均未被 Git ignore。记录见 [原片清单](../../assets/library/recovery/mygo-20261029.json)。
- 限定检查换机报告、personal 迁移清单和 `seekino-archive-20261028`，未发现旧 MyGO 业务备份。另在 `~/Documents/ChatGPT/casual/work/seekino-rename-20261028/database-before.sql` 找到 57,017 bytes 的 MySQL 8.4.13 dump：28 个表结构，仅 `checkpoint_migrations` 含 INSERT，无视频或解析业务行。未导入该空业务备份，不从检查范围推断所有数据已丢失。
- 追加实机证据：运行中 API PID 85892 的进程环境与 `.env.local` 均连接 `127.0.0.1:3306/seekino_development`，输出未包含凭据。只读 `@@datadir` 确认为仓库 `data/mysql/`，版本 8.4.13；`SHOW DATABASES` 只有四个系统库和一个 `seekino_development`。该库 28 张表中，`series=1`、`video=1`、`video_parse_task=4`、`chunk_parse_content=2`、`interval_parse_content=2`。这是当时单条验收视频的快照，不是旧 MyGO 13 集已经恢复后被首页隐藏，也未发现同实例内另一个旧业务库可直接切换。
- 核对原始库名与导出来源：`/Users/hypnos/Documents/ChatGPT/casual/work/seekino-rename-20261028/baseline-before-rename/deploy/README.md:137` 记载旧主业务库为 `anivag_local_20261010_development`；`/Users/hypnos/Documents/ChatGPT/casual/work/seekino-rename-20261028/database-before.sql:3` 的头部却是 `anivag_development`，SHA-256 为 `b8f68819f1906fd3b33e30fb8df17193efcd92fd5fe1045b05972db643e2a56f`，唯一 INSERT 位于 `checkpoint_migrations`。该 dump 同目录的 `model-and-namespace-notes.txt:65–67` 也记载切换前业务表为空、索引 0 文档，因此使用新空开发库。现有文档中的 `seekino_local_20261010_development` 是命名空间修改后的文字，不能据此推定真实旧库已经搬迁或改名。
- 核对迁移范围：`/Users/hypnos/workspace/restore-report.json:354` 明确写明目录迁移未恢复数据库等运行依赖；仓内[换机材料范围](../migration/20261023/README.md#仍需私下备份的内容)和[迁移清单](../migration/20261023/manifest.json)也将数据库备份列为私有材料。使用 `rg` 有界检查项目、`seekino-rename-20261028` 和恢复报告指向的 `seekino-archive-20261028`，并只读检查 rename 目录的 `source-before.tar.gz`、`personal-before.tar.gz` 以及 archive 目录的 `source-snapshots.tar.gz` 成员；其中只找到源码迁移 SQL，没有旧业务 dump 或 MySQL 数据文件。归档内 `Seekino/.env.local` 本来就指向空业务库 `anivag_development`；本机旧路径 `outputs/local-services/mysql` 不存在。没有启动 Docker、停服、导入 SQL 或写入业务数据；未检查到的旧设备、其他备份和停止的实例存储仍不能排除。
- 本轮没有读到旧业务库，未关联正式 video URI、恢复派生媒体或验证播放/检索；没有重解析、ES 重建、供应商请求或付费模型调用。源码目录修改与旧资料库恢复分别报告。
- SSH 合成上传：16 MiB 数据传给远端 `wc -c` 计数后丢弃，没有远端文件；包括建连在内共 16.942 秒，有效吞吐约 7.92 Mbps。按该单次样本线性估算 5.055 GB 约 85 分钟，仅供选择部署位置参考，不是视频实际上传或持续带宽测量。

### 指标对比

宿主机三处默认入口及本机有效配置由 `outputs/server/artifacts` 对齐到 `assets/library`，持久媒体的目录语义更明确；历史 URI 和存储算法不变。12 个原片从仅存在于忽略的样例路径，增加可见的持久归档路径，新增视频内容占用为 0 bytes（硬链接的目录元数据另计）。硬链接共享修改，不是独立备份。旧视频播放、历史解析成果与搜索可用性尚无恢复结果，不能宣称业务闭环完成；后续取决于旧业务数据来源和选定部署位置。

追加核对将当前缺口定位为：正在使用的实例及已检查迁移材料均未包含真正旧主业务库，而已找到的 dump 来自另一空业务库。B17 保持 TODO，下一步仍是取得 `anivag_local_20261010_development` 的业务备份或完整旧实例存储，再核对原片引用和索引；现有证据不支持“旧 MyGO 已全部永久丢失”的结论。

同日用户随后确认旧电脑与整机备份均不可用，并要求核对项目 `docs/migrations`。已逐项检查 22 个 SQL，INSERT 目标只有 `_bm_*` 临时结构校验表、默认本地 user/tenant、checkpoint 版本表，没有旧 MyGO 业务行；这些文件可升级已有库，不能恢复丢失内容。同时检查 personal 本地全部 Git 历史的备份候选、AniVAG `untracked.tar`、学习报告源码 tar/zip，以及 Documents/Downloads/Desktop/workspace 的数据库和归档候选；未发现可恢复的旧业务库或完整解析导出。本机 Time Machine 未列出本地快照或目的地。Docker 磁盘仍存在，但本轮没有启动 Docker 或读取其文件系统内容，不将其计作已排除；10 月 28 日初始化记录与现有 SQL 均指向当时新建的空库。审计证据见 [database-audit-20261029.json](../../assets/library/recovery/database-audit-20261029.json)。未导入 SQL、修改业务库或调用模型；12 集完整原片约 284.10 分钟仍可用于后续重建，第 11 集仍为 `.part`。重建会产生新的解析结果，不能称为原库恢复，超出已授权 12 秒实模验收的整集调用尚未执行。

<a id="devlog-interview-preserve-20261029"></a>

## 2026-10-29：面试前保护旧闭环并撤销裁剪

### 问题与现状

用户明确次日（2026-10-30）面试需要旧的解析、导出、检索、问答与陪看闭环，旧能力还服务于其他工作；新 SVG／Three.js 创作闭环短期无法作为已完成能力展示。先前仅从新作品的生产调用判断裁剪，遗漏了面试、研究与旧能力依赖这一使用边界。关联 [B16](problems.md#b16面试前保护旧闭环并撤销-npc-实验与-acp-client-裁剪)。

### 方案候选

停止继续裁剪，恢复本聊天此前删除的 NPC 实验、ACP Client、专用测试及依赖；不回滚整个工作区。逐项确认 9 个删除文件在裁剪前没有用户修改后，从 Git 原内容恢复；四份只有本轮退役改动的模块／历史入口恢复原内容。pyproject、uv.lock 与设计总索引仅逆转本聊天的删除片段，保留此前 MCP 新增及其他工作。

面试前保留原功能和运行合同，不实施 Chat、Cloud、Web、供应商适配或数据库的退役与迁移。新方向以待验证探索表达；项目介绍围绕业务问题、方案取舍、验证结果与未解决边界组织。

### 方案验证

- 9 个文件已恢复，相关源码／测试与 Git 原内容一致；ACP 依赖与锁记录恢复后，pyproject 与 uv.lock 相对 HEAD 仅剩此前已有的 MCP 直接依赖新增。
- `UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv lock --check --offline`：136 packages，一致性检查通过。
- `UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv run --no-sync python -m pytest tests/gateway/test_bridge.py tests/training/test_character_npc_prepare_sft.py tests/gateway/test_model_factory.py tests/gateway/test_agent_factory.py tests/server/test_bootstrap.py tests/server/test_mcp.py tests/server/test_mcp_transport.py tests/server/test_mcp_retrieval.py -m 'not live_agent' -q`：364 passed，47.98 秒，1 条已有 Starlette/AnyIO deprecation warning。包含恢复的 ACP/NPC 专用测试及模型工厂、启动装配、MCP 定向回归；不是当前真实视频演示验收。
- 只读探测默认 localhost 5173、9621、3306、9200，本次检查时均未监听；不能据此排除其他端口或远端部署。仓库保留素材库、检索和陪看截图及历史 benchmark，但它们不能替代明天运行环境的预演。本轮未启动／重启服务、访问真实模型或迁移数据。

### 指标对比

撤销上一轮 792 行源码、858 行专用测试与 4 行训练依赖的删除，锁定包数从 135 恢复至 136。该恢复保护既有工作范围，不宣称性能、准确率或作品质量提升。当前没有本聊天引入的功能裁剪；后续优先完成可复核的旧闭环演示准备与面试表述。

<a id="devlog-pv-scope-prune-20261029"></a>

## 2026-10-29：按角色宣传片目标退役 NPC 实验与 ACP Client

本条是当时的删除经过；随后用户明确面试和其他依赖，本次删除已全部撤销，当前状态见 [恢复记录](#devlog-interview-preserve-20261029)。

### 问题与现状

关联 [B16](problems.md#b16面试前保护旧闭环并撤销-npc-实验与-acp-client-裁剪)。用户明确当前闭环：assets 艺术参考与 MyGO 视频支撑剧本、分镜和特效理解，由 Agent 融合风格，制作约一分钟、围绕千早爱音与“爱与勇气”主题的 SVG／Three.js 宣传片。完整原始目标与研究边界见 [既有研究笔记](../research/2026-10-29-reference-driven-svg-threejs.md)。用户要求裁掉不服务于该闭环的复杂度。

依赖核查确认 `training/character_NPC/` 仅由专用测试引用；`gateway/bridge/` 是未接线的 ACP Client，仅由自身及专用测试使用，未进入 Server bootstrap。它与已实现的 Server MCP 是相反方向的接入，删除 ACP 不删除外部 Agent 素材入口。

### 方案候选

先删除上述两组独立源码、专用测试、训练 requirements 和 `agent-client-protocol` 依赖，并更新模块入口和历史源码链接。历史设计与研究保留，原有 MCP 未提交修改保留。没有引入替代抽象、功能开关或新部署形态。

依赖审查同时发现内部 Chat/陪看可以退役，但需一起拆服务装配、OpenAPI 定制、会话存储和旧短片素材脚本；Cloud 身份需保留本地 User/Tenant 初始化，整套 Web 删除还会失去已有上传与任务管理入口。这些仍是待用户决定的产品兼容性边界，本条没有实施。正式 Parse 仍使用 Deep Agents 多模态工具执行，不能因 Chat 可删而一并删除共享 harness。原片、时序、角色证据与纠错职责继续服务于本次制作目标。

### 方案验证

- 剩余生产代码、测试、evals、脚本和技能 Python 中未发现 `gateway.bridge`、`acp`、`training.character_NPC` 的导入。
- 直接 `uv lock --offline` 因临时 cache 缺失已有 Claude SDK 的解析元数据而失败；按现有锁文件仅移除 ACP 包及根依赖引用后，`UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv lock --check --offline` 通过，135 packages，其余版本保持。
- `UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv run --no-sync python -m pytest tests/gateway/test_model_factory.py tests/gateway/test_agent_factory.py tests/server/test_bootstrap.py tests/server/test_mcp.py tests/server/test_mcp_transport.py tests/server/test_mcp_retrieval.py -m 'not live_agent' -q`：319 passed，33.54 秒；1 条已有 Starlette/AnyIO deprecation warning。覆盖保留的模型工厂、服务装配与 MCP 合同，使用隔离替身/SQLite，不证明真实模型效果。
- 未启动正式服务、修改数据库或索引、删除媒体、调用真实模型、制作作品、commit 或 push。真实素材到一分钟作品的收益未验证。

### 指标对比

两组原有 9 文件共 1,654 物理行被删除：源码 792、专用测试 858、训练 requirements 4。直接依赖减少 1 项，锁定包从 136 减至 135。删除的测试仅服务于已退役模块，保留路径的 319 项定向回归通过。源码和测试行数分别报告，不把删除测试算成产品实现减少。

这次减少的是两条无生产消费者的维护分支；未测得解析时间、检索精度或最终成片质量变化。内部 Chat/Cloud/Web 的大范围裁剪尚未实施，不将这次小范围退役称为整个项目已收敛。

<a id="devlog-local-mcp-20261028"></a>

## 2026-10-29：localhost MCP 与外部 Agent 检索接入

### 问题与现状

关联 [B15](problems.md#b15localhost-mcp-与外部-agent-素材取用闭环)。10 月 28 日提出、跨午夜实施。用户优先要求在 10 月 30 日早上前通过 Codex 演示 Seekino 的素材取用，首版明确 localhost。已有 Event 检索只注入内部 Agent，缺少对外工具发现、稳定证据读取与媒体交付。Cloud 的通用执行和 HTML 作品后置；用户确认演示先恢复旧机 MyGO，再提供新系列。

本机 `.env.local` 的非秘密配置指向 development/local、API 127.0.0.1:9621、`outputs/server/artifacts`。当前可见监听中没有 API/MySQL/ES，该媒体根为空；未启动正式服务或执行数据迁移。不能将此处的隔离 fixture 当作 MyGO 召回结果。

### 方案候选

选择同一 API 进程挂载 `/mcp`，复用已安装并锁定的 `mcp==2.2.0` SDK。工具合同来自 Python 类型、Field 参数说明和 docstring，SDK 发布 `tools/list` 并分派 `tools/call`。`McpRetrievalService` 只组织本地范围、有界输出和版本化回读；Event 排序和 SQL 核验仍由原服务执行。没有引入内部 Chat 循环或新执行器。

协议限定 local 和真实 loopback，校验 Host/Origin、拒绝代理转发；Cloud 不挂载。`evidence_ref` 是带来源版本的定位引用，不是授权凭证；回读重新 hydrate，时间以 ms 输出。原片 URL 复用现有媒体接口，缺失媒体明确警告。方案及阶段边界见 [主 TRD](../design/03-product-reassessment/mcp-access-trd.md)。

### 方案验证

- `UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv lock --check --offline`：136 packages，锁文件一致。MCP 已是传递依赖，本轮只显式声明直接依赖，未升级现有包。
- `uv run --no-sync python -m pytest tests/server/test_mcp.py tests/server/test_mcp_transport.py tests/server/test_mcp_retrieval.py -q`（使用上述临时 uv cache）：53 passed。包含真实 SQLite/Library/ArtifactStore/MediaService 的来源回读与版本 1→2 后拒绝旧引用；ES 和 codec probe 使用 stub。
- `uv run --no-sync python -m pytest tests/server/test_bootstrap.py tests/server/test_api.py tests/server/test_video_event_retrieval.py tests/server/test_media_views.py tests/server/test_search_lexical_contract.py -m 'not live_agent' -q`：260 passed。两组共同出现已有 Starlette/AnyIO deprecation warning。
- 临时 loopback Uvicorn 端点使用本次真实传输代码、明确标记 `TEST_FIXTURE_ONLY` 的目录服务；MCP 2.2 SDK 完成初始化、发现三个工具并调用 `list_series`。结果见 [SDK 网络证据](../../outputs/verification/mcp-local-20261028/sdk-smoke.json)。测试脚本初次沿用旧 SDK 三流返回而失败，按本机 2.2 的两个流签名修正后成功；生产代码不受该脚本错误影响。
- 使用本机 `codex-cli 0.165.0-alpha.1.2`、临时 MCP 配置、ephemeral 会话仅允许 `list_series`；模型采样连续 `request timed out`，110 秒后结束，服务端业务调用数为 0。保留 [摘要](../../outputs/verification/mcp-local-20261028/summary.json) 与 [Codex 事件](../../outputs/verification/mcp-local-20261028/codex.jsonl)。不能称为模型自主调用成功；全局 Codex 配置未修改。
- 随后使用同一实际 CLI 的 app-server 协议，创建临时会话，按名称禁用既有 MCP，仅启用本次测试端点。`mcpServerStatus/list` 返回 connected 及三个工具的名称、说明和参数 Schema；`mcpServer/tool/call` 直接调用 `list_series`，宿主记录恰好一次，返回唯一 fixture 名称。全程 0 个模型 turn。证据：[真实 Codex 工具清单](../../outputs/verification/mcp-local-20261028/codex-appserver-tools.json)、[调用结果](../../outputs/verification/mcp-local-20261028/codex-appserver-smoke.json)。它验证实际客户端发现/执行，不能替代前述失败的模型自主调用或真实素材验收。

未验证真实 MySQL/ES/模型检索、MyGO 媒体下载和制作作品；用户准备恢复数据后再执行。临时测试进程退出后已关闭端点。未重启正式服务、重建索引、迁移、commit 或 push。

### 指标对比

| 维度 | 修改前 | 本次结果与限制 |
|---|---|---|
| 对外工具 | 无产品 MCP | 三个工具的发现、调用与参数说明可用 |
| 证据回读 | 内部 Agent 状态组织 | 独立引用、SQL 重验、按需分页；不依赖原 Agent run 内存 |
| 素材获取 | HTTP 媒体能力已有 | 工具提供绝对本机媒体 URL；真实素材仍待恢复 |
| 自动化验证 | 无 MCP 覆盖 | 53 个定向测试、260 个既有相关回归通过 |
| 实际 Codex 客户端 | 未测 | app-server 发现三个工具、一次直接调用成功；输入为明确 fixture |
| 模型自主调用 | 未测 | Codex 请求超时，未完成，保留失败记录 |
| 数据与运维成本 | 已有 API/DB/ES | 无迁移或新常驻服务；新增 SDK 入口和薄业务投影 |
| 业务交付 | 外部 Agent 无正式入口 | 实现可接入，真实素材到作品闭环尚未完成 |

<a id="devlog-parse-quality-attribution-20261029"></a>
## 2026-10-29：身份缺证归因修正与多片段质量验证计划

### 问题与现状

用户指出：47 秒爽世画外音在 K3 和豆包都未正确归属；模型只收到帧和无身份转写，不能据此证明豆包 non-thinking 相对低质。原文错误及业务风险成立，但身份、合并和剧情立场可能是同一缺证错误链；重复同一片段不构成多样本泛化证据。

### 方案候选

保持精简 JSON 及既有并行取舍，新增 [主 TRD §4.7](../design/03-product-reassessment/video-parse-performance-trd.md#47-多片段质量验证计划分开模型能力身份补证与调度)，分别检验共同输入下的视觉/内容质量、声音身份补证、同豆包 thinking 消融、同依赖和上下文下的串并行。Refine 是处理阶段，声音/可信参考是证据；匿名音色簇不能直接给角色姓名，不预设音色补丁必然解决。用户进一步指出真正的串行基线会传递前文，可能无需独立跨块 Refine；因此架构对照修正为带历史的轻量串行 workflow 与现有并行及实际 Refine，不强制两者相同历史和后处理。

### 方案验证

本次只复核已有输入条件与结论、由独立 Agent 审查实验设计并修改文档，没有新增模型调用、金标、样本运行或生产修改。建议先选 6 个新的连续 2 分钟片段，按原 60 秒 Chunk 得到 12 块；同输入两模型一次筛查建议 24 次。具体路径/窗口、独立人工金标和预算待冻结；EP04 前 60 秒保留为回归，不计新验收样本。更长单次输入另作变量，不能暗中降帧或换上下文。

### 指标对比

| 维度 | 已有证据 | 本次修正与后续口径 |
| --- | --- | --- |
| 47 秒说话人 | 两模型均错，原输入无声音身份 | 错误保留；从供应商/thinking 独有劣势证据中拆出，按证据充分/不足分层 |
| 独立样本 | 一个 60 秒片段多次重复 | 计划扩展多作品、多场景连续序列，尚未执行；不把重复次数当样本多样性 |
| 模型与 thinking | K3 low 对比豆包 disabled | 前者是实际配置比较；thinking 因果需同一豆包版本单独消融 |
| 身份与架构 | 单块独立请求不代表带历史串行；尚无完整策略对照 | 分开评估补证；架构比较允许串行前向传递上下文且不强制 Refine，对并行计入实际 Refine/重算成本，核对最终质量与整体墙钟 |

收紧结论增加标注与独立对照工作量，避免把输入缺口误判成模型或架构因果。更多样本允许支持、否定或限定原判断，不以证明豆包较差或并行必胜作为目标。原 XML 不采用的决定保持，详细失败记录保留在[质量复核报告](../report/agent/video_parse_v2/doubao-nonthinking-quality-review-20261029.md)。

<a id="devlog-parse-protocol-decision-20261029"></a>
## 2026-10-29：采用精简 JSON，结束 XML 选型与单块 30 秒追求

### 问题与现状

关联 B07。两轮各 8 次协议实验已结束：字段精简降低正文负担，但 XML 相对同字段 JSON 没有足够替换收益；K3 前置等待和共同内容错误仍存在。用户据此作出最终决定，见 [主 TRD §1.4](../design/03-product-reassessment/video-parse-performance-trd.md#14-最终取舍精简-schema继续-json质量优先)，原始结果见 [第二轮报告](../report/agent/video_parse_v2/parse-compact-shot-20261029.md)。

### 方案候选

选择精简 Schema、继续 JSON，保留 SHOT 方向，不采用 XML。用户明确解析质量优先，没有必要盲目追求单 Chunk 30 秒；接受更高人力成本，认为既有可靠并行解析策略已足够承担当前效率需求。停止本轮协议扩跑，之前提出的视觉输入预算实验不再自动继续；未来仅针对具体整集瓶颈另行判断。

### 方案验证

本次是基于既有证据的工程决策和文档收敛，没有新增模型调用、并行实验或生产修改。两轮耗时、错误、样本量与原门槛保持原记录，不把停止方案写成质量达标。精简合同只在实验实现，生产 Schema/SHOT、宿主直接输出、旧数据与索引兼容仍待接入；由解析模块维护者推进时核验质量、整集表现及恢复。

### 指标对比

| 维度 | 此前探索口径 | 最终取舍及影响 |
| --- | --- | --- |
| 协议 | JSON/XML 对照，继续寻找提速收益 | 保留精简 JSON，不新增 XML 生产解析和维护成本；实验代码与证据留存 |
| 单 Chunk 耗时 | 历史约 30～40 秒目标 | 不再把 30 秒作为优化/验收要求；耗时保留诊断，优先内容正确性和时间定位 |
| 整体效率 | 拟继续协议和输入预算实验 | 沿用既有可靠并行策略，以整集到索引可见、完成率、恢复与成本评价；本次没有新加速比 |
| 人力与维护成本 | 实验、协议接入及调优投入 | 接受用户指出的更高人力成本，停止收益不明确的 XML 扩张；未量化工时或金额 |
| 完成范围 | 协议仍是候选，拟继续实验 | 本轮协议选型 DONE；生产接入及 B07 其他未完成事项保持 TODO |

<a id="devlog-parse-compact-shot-20261029"></a>
## 2026-10-29：取消人物名单、增加 SHOT 的精简 JSON/XML 对照

### 问题与现状

关联 B07。首轮 XML 对完整 JSON 不能隔离字段压缩与格式影响，且名单/身份、跨人合并及分类均有问题。用户希望取消 participants、visible_persons，增加镜头专用 enum，再测整体耗时。沿用 [主 TRD](../design/03-product-reassessment/video-parse-performance-trd.md) 六阶段，不新建竞争设计；[第二轮报告](../report/agent/video_parse_v2/parse-compact-shot-20261029.md) 保存八次结果与质量边界。

### 方案候选

两组均取消逐事件名单与根级 persons，增加 SHOT，保留 speaker、动作主体、双语对白和毫秒时间。新增实验 CompactTimeline，共享字段、默认值与校验；JSON 也采用精简表达，不再比较完整 Schema 与缩略 XML。生产枚举尚无 SHOT，故不伪装成 SCENE 或填空名单接入 Canvas。保留旧合同实验供历史复现，生产配置和数据不改。

### 方案验证

同 EP04 前 60 秒、同 180 张 3 FPS 720p 帧、同本地 Whisper 观察与背景、OCR 关闭，逐文件及帧 hash 相同。K3 low、豆包 2.1 Pro disabled 各 JSON→XML→XML→JSON，8 次串行独立请求，零工具，无重试/修复/fallback。调用前冻结新版检查表，将已知 47 秒爽世画外音纳入但不发送答案；已删除名单不计为质量失败。

58 项定向测试通过，223/223 证据一致性核验通过；8/8 completed，JSON/XML 标准语法全合法，合同通过率 3/4 与 4/4。豆包 JSON-1 的三条 SHOT 多了合同外 description，原样保留失败。非盲内容复核显示：八份都有镜头描述，125 条原始对白均带姓名，但 47 秒画外音仍全部错认；最快的豆包 JSON 还跨人合并。SUNWAY、ESP 各出现于 3/8 份，独立画面文字类型仍缺。无精确声学边界、完整金标或独立盲审，未验证生产、检索、恢复或整集。

### 指标对比

| 指标，每组 n=2 | 精简 JSON | 精简 XML | 结论与代价 |
| --- | ---: | ---: | --- |
| K3 全部尝试耗时中位数 | 118.52 s | 144.57 s | XML +21.97%，两次 XML 201.36/87.78 s，reasoning 波动显著 |
| 豆包同口径 | 44.41 s | 52.50 s | XML +18.21%；JSON 包含一次失败，不能当合格成功延迟 |
| K3 正文时段中位数 | 15.28 s | 17.92 s | XML 可见 token 少 16.15% 仍未更快 |
| 豆包正文时段中位数 | 26.97 s | 27.07 s | 两者相近；XML 输出 token 反多 3.84% |
| 关键目标正确数 | 0/4 | 0/4 | speaker 非空未解决真实身份；删字段不是此次错误的评分原因 |

相比首轮，精简 JSON 正文 token 约减少 50%（K3）/57%（豆包），正文时段也缩短；K3 前置等待却上升。共同 Prompt/SHOT/字段及缓存均有变化，不将全部收益归给删除两字段。8 次累计尝试 720.00 s、1853036 total tokens，含失败，无账单金额；ASR 沿用首轮一次 5.33 s 测量，未另行执行。

新增实验模型、解码器及 34 项测试有维护成本，但没有增加生产包装层。决定保留精简字段/SHOT 及精简 JSON 候选，不为性能切换 XML；下一步先完善画外音、跨人分段、类型与文字覆盖，再冻结视觉输入预算实验。B07 不关闭，不继续无上限重复同一协议。

<a id="devlog-parse-protocol-20261029"></a>

## 2026-10-29：本地 Whisper、无独立 OCR 的 JSON/XML 单块对照

### 问题与现状

关联 B07。EP04 已恢复且 SHA-256 与旧记录相同，但事件包装 XML 尚无实模证据，生产仍依赖 Canvas tools。用户要求测试新的 XML 是否降低单 Chunk Understanding 总耗时，并将观察条件改为 localhost Whisper、关闭独立 OCR。完整设计见 [主 TRD](../design/03-product-reassessment/video-parse-performance-trd.md)，方法及全部原始值见 [实验报告](../report/agent/video_parse_v2/parse-protocol-20261029.md)。

### 方案候选

固定前 60 秒的 180 张 720p 图片、本地转写及人物背景，对照零工具完整领域 JSON 与事件包装 XML。两组均普通文本、本地校验，绕开生产强制 JSON Schema 通道；不修改生产模型配置或 Canvas 流程。同字段精简 JSON 留作下一轮归因；本轮不能把字段压缩、可省略属性和语法差异合成单一因果结论。

本机编译 whisper.cpp、使用官方 large-v3-turbo Q5 权重并启动 localhost 服务，未改项目依赖或全局 ASR binding。独立 OCR 关闭仅作为本轮条件；PaddleOCR/DeepSeek-OCR-2 未调用。新增维护面为一个实验 runner，原文/源码快照保留在忽略目录；生产接线没有新增维护面。

### 方案验证

K3 `k3/low`、豆包 `doubao-seed-2-1-pro-260628/disabled` 各按 JSON→XML→XML→JSON 串行调用，共 8 次；每次 600 s 上限、30000 output tokens、无 SDK 重试/模型修复/fallback。原文、SSE、usage、输入/代码 hash、终态及错误见 [证据目录](../../outputs/verification/parse-protocol-20261029/)。全部 completed，JSON/XML 语法均 4/4 合法，协议/领域校验各 3/4 通过；豆包 XML 失败因同一 event 有两个主类型，豆包 JSON 失败因 speaker 不在 participants。未人工修复后提交。

事前冻结局部视觉检查，随后由 Codex 非盲审；没有完整音视频金标。K3 XML 两次分别有 13/30、11/28 个空 participants；豆包 XML 首次 10 条对白全缺 speaker、15 个 event 全缺 participants，并合并跨人台词。画面文字与事件分类也不稳定。评审中补查项目已有的约 47 秒爽世画外音真值，八份原文均未正确归属，不能把 JSON 也不合格作为 XML 可上线的依据。

24 项 decoder/Canvas/Refine 内存测试通过，冻结证据一致性核对 44/44；这些不代替语义验收。未经过 Seekino 生产 API、持久化、Refine/Prefix、检索或整集验证。决定保留 XML 候选，先修合同与共同质量问题，再做同字段 JSON 和输入预算实验；暂不替换生产协议。

### 指标对比

| 指标，每模型每协议 n=2 | JSON | XML | 判断 |
| --- | ---: | ---: | --- |
| K3 全部尝试总耗时中位数 | 103.22 s | 100.17 s | −2.96%，未达预设 10% 探索门槛 |
| K3 正文时段中位数 | 33.48 s | 24.29 s | −27.46%，但首正文更晚；reasoning 中位数 29.5→1121.5 |
| 豆包全部尝试总耗时中位数 | 98.73 s | 64.68 s | −34.49%，各含一次失败且 XML 内容退化，不能计为合格提速 |
| 豆包正文时段中位数 | 53.76 s | 26.95 s | −49.87%，含遗漏身份和合并台词的影响 |
| 协议/领域通过 | 3/4 | 3/4 | completed、格式合法不等于结果可用 |

本地 ASR 观察准备约 5.33 s，其中抽音频 0.33 s，18 条转写；服务已加载，下载/编译/冷启动不计，Q5 精度未对照。八次 Understanding 总用量 1862756 tokens（含失败，reasoning 不重复相加）；未取得实际费用。K3 首次 JSON 无缓存、其余几乎全命中；豆包仅首份 XML 大量命中，其他均报告 0。缓存和服务时延波动尚不能隔离，不报告 p95/p99 或稳定降幅。正文变短已有证据，同质量整体加速和正式产品收益尚未成立。

<a id="devlog-parse-performance-20261028"></a>

## 2026-10-28：解析性能 TRD 与事件包装 XML 复测口径

### 问题与现状

关联 B07。旧实验已显示输出耗时可能下降，但还存在人物、事件分类和时间质量缺口；生产仍走 Canvas tools，新 Mac 的原视频与旧原始证据缺失。用户要求依照 [common TRD principle](../design/common/trd-principle.md) 统一记录问题、因素、方案和实验过程，再提供视频开展复测。

### 方案候选

建立 [解析性能主 TRD](../design/03-product-reassessment/video-parse-performance-trd.md)，保留旧 XML v2 原稿并关联新事件包装候选。先比较零工具完整 JSON 与事件包装 XML；同字段精简 JSON 留作归因消融。模型负责理解和输出，宿主负责解码、校验及原子写 Canvas；不删除其他消费者的 Canvas 工具或改变领域/存储合同。区分媒体重复解码、冷启动、thinking、输出、ASR/OCR、缓存与失败重算，避免将假设当根因。

### 方案验证

前序实现的 [decoder 测试](../../tests/evals/test_parse_output_protocol.py) 24 项通过，覆盖排版、非法/截断输出、领域边界及 Canvas/Refine 内存往返。本轮仅维护文档与入口，不复跑业务测试或调用真实模型。计划恢复视频后冻结输入，K3 low 与 Doubao 2.1 Pro disabled 各做 JSON/XML 两次交叉顺序对照，共 8 次；精确模型 ID、权限和出站参数仍需预检。质量标注、真实调用、完整接线和整集结果均未取得。

### 指标对比

| 维度 | 本轮前 | 本轮后 | 证据与限制 |
| --- | --- | --- | --- |
| 设计与验收口径 | 旧协议稿、交接和本轮讨论分散 | 一份主 TRD 串起六阶段，关联 B07 和历史来源 | 维护成本增加一份主文档；历史稿只保留来源，避免双份现行合同 |
| 新协议实际耗时与质量 | 无模型基线 | 仍待测 | 无原视频；不能把文档或离线测试当性能收益 |
| Canvas 兼容 | 已有 24 项离线测试 | 兼容设计和未覆盖范围写入 TRD | 未验证持久化、Prefix、索引与失败恢复 |

<a id="devlog-chat-harness-kimi-20261026"></a>

## 2026-10-26：Kimi 同模型 Chat 三框架对照

### 1. 问题与现状

去 Pi Host 的生产简化已有工程证据，但此前 Deep Agents + Kimi 只跑了旧 multi-agent 的查询覆盖题，不能回答 C19“高高在上→说话无趣”的对话能力，也不能隔离历史 GPT 与当前 Kimi 的模型差异。此次使用整理后的两个 Chat 集，另检查公共检索和路由组件；报告入口为 [Kimi 三框架对照](../../evals/benchmark/reports/chat-harness-kimi-20261026.md)。

基线固定为当前生产 Chat Prompt、工具、Query 和数据；仅替换 Supervisor harness。正式 MyGO EP02 最新索引失败，其余 12 集及独立 EP04 可用。本次不把整套旧 multi-agent 架构、不同日期模型成绩或自动来源检查当作框架效果对照。

### 2. 方案候选

历史成绩对比成本低，但模型、Prompt、预算与索引状态不同；只测路由或裸模型又不能覆盖完整历史和产品交付。因此选择同为 Kimi `k3.1-pro` 的 Deep Agents、隔离恢复的退役 Pi、Codex 自定义 provider 对照，通过真实 ASGI HTTP 入口及 SQL/ES 执行业务。用户要求的两类能力分别统计：10 个行为场景每套两遍，共 60 条；13 个查询任务每套一次，共 39 条。

Supervisor、Query 等可配置业务生成角色及后置评分均用 Kimi，无 GPT 业务调用或生成 fallback；公共 Jev/Qwen 前置链路和 Doubao embedding 保留。Deep Agents/Pi 走 Chat Completions，Codex 走 Responses；三套原生指令、压缩和输出控制不能完全等同，结论限定为框架及适配器的产品组合效果。Pi 仅在实验目录恢复，补 Kimi 不支持 developer role 的兼容配置，没有重新引入生产 Host。

新增维护面是 `evals/benchmark/compare.py`、`harnesses.py` 两个评测入口、三项离线保护测试与精简结果；不新增生产运行层、自动线上评分器或 sandbox 接入。既有去 Pi 变更所减少的 Host、配置与代码属于前一记录的收益，不能再次记成本轮删减。

### 3. 方案验证

冻结 326 个相关 Python 文件、配置与用例 SHA，最终版本／包／配置核对不变。完整历史写入独立会话，T01→T02 继承各自上一轮回答并跨 worker；固定顶层串行、轮换 arm 顺序，Supervisor 总预算 480 秒，外部 watchdog 570 秒。真实输入核对中，同组 Prompt 和工具一致，只有 T02 自然继承不同上轮历史。没有把参考答案送给被测模型。

原始查询批次受系统时间间隔及 DNS/连接中断影响，14 条原件完整保留，整组排除并重跑 39 条，未择优覆盖失败。行为 60 条保留，其中时钟异常影响的整组三方样本从延迟配对子集排除；正式超时仍计失败。恢复批次无同类时钟异常。Codex T01 另有一次供应商过载，T02 依赖阻塞；主实验后单独补跑成功，61.5／121.1 秒，确认该次跨 worker 历史承接可工作，但没有覆盖主成绩，仍缺 T02 冻结标准的午餐回述。

Kimi 在计时结束后打乱样本、隐藏 arm 标签评分，执行代理逐条核对证据并针对人物冲突补看四张已有检查帧。原裁判分、初审与修订分别保存，不声称独立人工金标。连续动作／短促音型仍缺连续看片和回听；T02 标准比字面追问更强、旧时间锚点漂移等题目问题单列，不边测边改题。

`.venv/bin/pytest -q tests/evals/test_benchmark.py tests/evals/test_harness_comparison.py`：**22 passed**；新入口编译通过，默认只输出 99 条离线计划且不调用 API。最终 SQL 核对 98 条实际主请求为 92 COMPLETED／6 FAILED；另外 1 条 BLOCKED 未发送。自有额外 19 条预检／中断／补跑消息也均为终态，无 PROCESSING 或正文持久化不一致。数据快照保持 14 视频、44 任务及原状态摘要。

未验证的架构维度包括长上下文压缩保真、并发容量／RSS、常驻 Host 复用、崩溃／跨节点恢复、sandbox 生命周期与权限隔离。这些仍是 Deep Agents 选型应衡量的成本和风险，不能由短场景通过率代替。

### 4. 指标对比

执行完成：Deep Agents **32/33**、Pi **31/33**、Codex **29/33**；分母含 Codex 1 条前置依赖阻塞。完成不代表内容达标，质量与逐例证据另见报告及 [60 条行为基线](../../evals/benchmark/chat_behavior/baselines/harnesses-kimi-20261026.jsonl)、[39 条查询基线](../../evals/benchmark/query_capabilities/harnesses-kimi-20261026.jsonl)。Pi 的 31 条完成回答均存在增量断段，实际 HTTP 与 SQL 一致；内容诊断拼接与实际交付分开评分，不能将适配合同故障泛化成 Pi 智力差。

任务与证据通过数：行为 Deep Agents **16/20**、Pi **14/20**、Codex **14/20**；查询分别 **5/13、4/13、5/13**。原 C19 反馈 Deep Agents／Codex 各 2/2 通过，Pi 两次仍复述旧剧情；不将此等同幽默感偏好实验。考虑交付后的综合通过数，行为为 **16/20、0/20、14/20**，查询为 **4/13、0/13、5/13**；Pi 的零分源于适配断段，Deep Agents 查询另有正文缺段。继续以 Deep Agents 为默认是合理工程选择，但本次不支持其全面优于 Codex；错误前提题中 Deep Agents 误认说话人，Pi/Codex 正确，明确保留回归。

99 条主记录均完成评分与复核；2 条评分输出上限失败保留原件后仅补评，没有重跑 Chat。4 条修改维度分、1 条只纠正理由，其余保留 Kimi 原判断。评分实际上下文受绑定限制为 128k，离线核对 101 个主／诊断评分包，在初评及补评预算下原始载荷均未被截断。实际限制、原分与修订记录随基线保存。

三套共同未完成 T05 五片段回顾，实际都收到 Jev“不新增检索”的冲突说明；独立路由 13/13 标签正确并未覆盖这个输入。直接检索 13/13 执行、9/13 通过旧机械来源检查，另有 1/1 结构协议 smoke；它们不合入 Chat 分母或冒充语义准确率。其他具体缺口包括同 chunk 重复提交覆盖卡片、正文越过暂停点、静帧推断说话人和过度确定的人物动机。

干净成功配对的行为平均耗时为 Deep Agents **92.9s**、Pi **74.6s**、Codex **94.9s**（每套 17 条）；查询为 **115.4s／124.6s／152.0s**（每套 8 条）。失败与时钟异常未混进这个条件化平均值，但都留在完成率及原记录中。无缓存隔离、无 P95／显著性推断；已知 token 是缺失调用存在时的观测下界，不据此换算金额或宣布成本胜者。

本轮 canonical 变更 **10 文件：6 新增、4 修改；评测源码 2、测试 1、文档 5、数据 2，生产代码 0**，详细修改树在报告。收益是首次获得当前同模型、完整历史、实际交付的三框架证据；新增成本为离线适配、档案及后置评分调用。本次启动的 Qwen 进程已停止，原有 SQL/ES/API 不动；保留测试会话、原生 checkpoint 和检查帧供追溯。无迁移、重解析、commit 或 push，无关工作区变更保留。

<a id="devlog-benchmark-layout-20261025"></a>

## 2026-10-25：按能力整合 benchmark 并纠正选集口径

### 1. 问题与现状

原 `evals/mygo` 同时放着 13 个旧 multi-agent Chat 任务和 13 个直接检索配置，名称不能表达所测能力。“高高在上／说话无趣”对应的 10 个端到端场景只保存在两份被忽略的本地实验目录；早期路由组件又单独放在 `evals/query_router`。这导致此前选择错误评测集，拿查询覆盖结果回答 Chat 应变能力问题。

### 2. 方案候选

选择按能力划分 [evals/benchmark](../../evals/benchmark/README.md)：`query_capabilities` 查询能力覆盖、`chat_behavior` 对话理解与应变、`retrieval_quality` 检索质量、`intent_routing` 意图路由与改写。模型和 harness 留在运行条件中。没有按 multi-agent／Deep Agents 分目录，也没有新增 `etc` 桶或通用评测框架；延迟、声纹、Parse 等现有专门诊断入口列为相邻评测。

原 MyGO runner 迁到共享 `run.py`，要求显式 `--suite`，校验 fixture 与 live 模式一致。两个旧集合的输入／历史观测按 kind 拆分，保留原 ID 与评分逻辑；新增 Chat behavior 精简基线，携带原始 fixture、协议、评审、来源 SHA 和失败。其当前 cases 使用第二轮明确 MyGO 五人首次登台的冻结题目，第一轮的含糊题仍按当时输入与 `unassessed` 保留。新模型测试继续约定 Kimi `k3.1-pro`。

本轮实际修改树（`R` 为移位并调整引用，`A` 为新增维护入口或从本地产物提升的精简记录，`M` 只修改本次相关内容）：

```text
Seekino/
├── evals/
│   └── benchmark/
│       ├── [A] README.md                         ← 分类、运行与维护入口
│       ├── [R] run.py                            ← 原 mygo/run.py；必须显式选择 suite
│       ├── query_capabilities/
│       │   ├── [R] README.md                     ← 原 MyGO 说明；明确查询覆盖口径
│       │   ├── [R] cases.json                    ← 原混合集的 13 个 Chat 输入
│       │   ├── [R] baseline.jsonl                ← 对应 13 条旧观测
│       │   └── [A] kimi-20261025.jsonl            ← 13 条 Kimi 观测、失败与复核
│       ├── chat_behavior/
│       │   ├── [A] README.md                     ← 10 个场景逐例验收与运行边界
│       │   ├── [A] cases.json                    ← 最新正式冻结输入与完整历史
│       │   └── baselines/
│       │       ├── [A] jev-vs-direct-20261024.jsonl ← 40 条历史观测、协议与旧版输入
│       │       └── [A] hook-models-20261024.jsonl   ← 60 条历史观测、协议与新版输入
│       ├── retrieval_quality/
│       │   ├── [A] README.md                     ← 独立检索使用与评分边界
│       │   ├── [A] cases.json                    ← 拆出 13 个直接检索配置
│       │   └── [A] baseline.jsonl                ← 拆出 23 条历史检索观测
│       └── intent_routing/                       ← 原 query_router
│           ├── [R] README.md                     ← 标明组件层级与历史 Codex 边界
│           ├── [R] cases.json                    ← 原 14 个输入保持不变
│           ├── [R] openrouter_jev.py             ← 更新模块命令
│           ├── [R] chat_rewriter.py              ← 更新 import
│           ├── [R] local_rewriter.py             ← 原样移位
│           ├── [R] template_rewriter.py          ← 更新 import
│           └── [R] rewrite_prompt.txt            ← 原样移位
├── tests/
│   └── evals/
│       └── [R] test_benchmark.py                 ← 原测试迁移；保护 suite 与历史版本边界
├── docs/
│   ├── [M] README.md                             ← 统一 benchmark 索引
│   ├── design/
│   │   └── [M] visual-retrieval-trd.md            ← 更新历史证据链接
│   └── dev/
│       ├── [M] devlog.md                         ← 本条与前次误选范围纠正
│       ├── [M] problems.md                       ← 区分 Chat／检索证据链接
│       └── [M] design-experiment-cleanup.md       ← 指向新入口，保留旧清理统计
├── deploy/
│   └── [M] README.md                             ← 更新路由评测路径
├── [M] README.md                                 ← 新索引
└── [M] README.en.md                              ← 新索引
```

### 3. 方案验证

`.venv/bin/pytest -q tests/evals/test_benchmark.py`：**19 passed**，包含既有来源／多轮／时间范围／输出保护测试，新增显式选集及历史题目版本边界。离线 replay 得到旧 Chat **13/13 完成、12/13 来源通过**，Kimi **11/13、9/13**，直接检索 **19/23、16/23**，均没有模型／API 调用。Jev dry-run 读取 14 个输入，移动后的改写模块通过离线入口导入检查。

逐条迁移审计确认原 26 个输入不变、原 36 条基线行无损拆分；两轮 Chat 的 100 条输入版本、最终回答、实际卡片、耗时和源文件 SHA 对齐；Kimi 的 13 条观测除明确不搬运的庞大 HTTP 原始证据外保持一致。原本地实验目录未删除。更新的 benchmark 本地文件链接均可解析；全仓 `git diff --check` 仍发现此前已有的 `docs/dev/reflection.md` EOF 空行与 `docs/research/2026-10-24-shot.md` 尾空格，本轮未修改这两处，当前任务涉及文件的检查通过。

本轮没有复跑模型，也没有新的语义质量结论。`chat_behavior` 已有可维护输入、协议与历史结果，但旧端到端 runner 依赖历史源码／Codex 会话和临时脚本，尚未迁成当前 Deep Agents + Kimi 可直接复跑的入口；该后续工作不能用无历史单句请求或路由 dry-run 冒充完成。

### 4. 指标对比

按本轮开始前快照区分已有 dirty 工作区：本轮最终涉及 **29 个文件位置：12 个移位、9 个新增、8 个既有文档修改**；其中评测 Python 入口 5 个、测试 1 个、文档 13 个、数据／prompt 10 个，生产代码 0。对照开工快照逐文件执行 `git diff --no-index --numstat`，合计 **+1210 / -467 行**（含数据拆分／归档）；评测 Python 与测试合计 +86 / -21，文档 +280 / -57，数据与 prompt +844 / -389。新文件尚未 staged，原无关 dirty 变更不计入这些数字。拆分前 26 个混合用例／36 条基线，拆分后仍为 13+13／13+23；另提升 10 个对话场景／100 条历史观测及 13 条 Kimi 观测。新增数据是已有实验的精简归档，不是新样本或新实测成绩。

业务收益是用户能直接找到所需对话题、比较时不会默认选错分母；新增维护成本为四份能力说明与独立结果档案。复用现有 runner，没有增加服务依赖、自动 LLM judge、数据 Schema、API、模型配置或前端改动。原 Python CLI/import 路径随迁移更新；历史本地脚本保留当时路径，不承诺原封不动在现代码上复跑。未重启、迁移、commit 或 push，原有无关工作区变更保留。

<a id="devlog-chat-kimi-mygo-20261025"></a>

## 2026-10-25：Deep Agents + Kimi K3.1 Pro 的查询能力覆盖回归

记录范围：后续生成式模型评测改用 Kimi K3.1 Pro。本轮实际选用了原 `evals/mygo` 的 13 个 Chat 用例，各一次、串行，现归入 [query_capabilities](../../evals/benchmark/query_capabilities/README.md)；13 个直接检索配置未重跑。**选集与用户原意不符：这组来自旧 multi-agent 多类型任务，用户要的是“高高在上／说话无趣”对应的 [chat_behavior 10 题](../../evals/benchmark/chat_behavior/README.md)。本轮不能作为后者的验收。** 已完成实际所跑用例的执行或失败记录、返回证据复核和资源清理。**11/13 执行完成、9/13 通过自动结构／已知来源检查；历史为 13/13、12/13。自动通过不是语义准确率。**

### 1. 问题与现状

前一条去 Pi Host 的决策解决职责重复与部署复杂度，尚不能说明历史 Chat 业务质量提高。本轮检查相识追问、推荐、全剧回顾、连续动作、短促台词、播放上下文及记忆偏差台词定位。历史九题使用 `gpt-6-astra`，四条 Event Chat 使用 `gpt-5.6-sol`；历史记录只读，没有重新调用 GPT。

当前正式 MyGO scope 的 EP02 最新任务 `509d458d-5c53-4de8-b4f4-412826402c71` 已是 `INDEXING_FAILED`，生产检索按最新任务规则实际覆盖 **12/13 集**；独立 EP04 scope 可检索。视频文件都可读取。这个前提影响全剧覆盖与 T07 的 EP02 正例，不能算成模型漏召回；EP04 17:11.2–17:11.6 的「は？」在本轮 T04 返回证据中仍然存在，T07 漏掉它具有当前数据依据。

### 2. 方案候选

采用当前生产工具、服务和 Deep Agents 运行路径，通过 FastAPI TestClient 发真实 ASGI HTTP 建会话／发送消息，复用 canonical benchmark 的输入和评分函数；答案、参考事实不送给模型。T01→T02 共用新会话，其他题独立建会话。生成式绑定（Supervisor、Query、视觉、摘要、重排）均指向 `k3.1-pro`，去掉生成式 fallback；既有 Jev 分类、Qwen 改写和 Doubao embedding 保持原配置。业务服务默认配置未切换。

历史比较用于判断现状回归。模型、Prompt、解析内容、部分数据就绪状态和旧运行并发条件不同，本轮没有恢复旧 Pi 做同模型 A/B，也没有用历史 GPT 再跑一轮。结论不能单独归因于 Deep Agents 或 Kimi，亦不代表 Kimi 调优后的上限。

### 3. 方案验证

- Kimi Coding `/models` 与一次红色图片探针确认 `k3.1-pro` 可用；探针报告 150 token，单独保存，不混入正式用量。实际生成调用没有 GPT；证据字段中的历史 GPT 解析标签是旧素材来源。
- 固定 `reasoning_effort=low`、输出上限 8192；Supervisor 上下文 64000、压缩阈值 48000、保留 24000；`limits_enabled=false`，Query 阶段 480 秒、执行及 Supervisor 总预算 1200 秒。中途没有调参、改 Prompt 或生产逻辑。319 个生产 Python 文件的哈希、模型配置与 fixture 在三批之间一致，最终源码未变化；ES 索引 UUID／文档数按索引名归一后前后一致。
- 初批 T01、T02 完成后，原先复用的 MySQL／ES 被外部信号关闭，T03 中断、后续未真正执行。保留两题结果和中断用量，再由测试持有依赖生命周期重跑尚未完成部分。该中断不计为 Kimi 质量失败，也没有隐去其成本。
- T05 在 1200.1 秒以 HTTP 502、`AGENT_MODEL_ERROR`／根因 `ReadTimeout` 结束。T06 检索工具已返回，线程随后阻塞于 SSL socket read，超过总预算仍未返回；约 1323.8 秒时停止自己的测试进程，作为失败保留，后续七题用相同配置继续。T05、T06 未重试。T06 的时间来自外部时间戳观察，不是终态 HTTP 的 `perf_counter` 样本；不得作为精确成功延迟。
- 11 条完成消息的返回正文与持久化正文一致。因环境中断留下的 T03 和外部停止的 T06 均通过既有取消接口关闭为 FAILED；T05 原本已是 FAILED。本轮拥有的 MySQL、ES、Qwen 进程均已停止，没有迁移、重解析、commit 或 push。
- `.venv/bin/pytest -q tests/evals/test_mygo_benchmark.py`：14 passed；合并 JSONL 通过 canonical 离线 replay，得到相同的 13／11／9 计数。14 份素材的媒体 Range 请求均返回 206；这仅证明文件可读取，没有完整浏览器播放、连续动作或逐句音频核验。
- 复核方式为本次 Assistant 对最终回答、真实卡片区间及返回来源文字逐例检查，没有另开模型 judge，也不是独立人工盲审。精简观测、失败与评审现收录于 [query_capabilities/Kimi](../../evals/benchmark/query_capabilities/kimi-20261025.jsonl)。原始运行保存在 [本地实验目录](../../outputs/verification/mygo-kimi-20261025/)，包括 [合并观测](../../outputs/verification/mygo-kimi-20261025/observations.jsonl)、[汇总](../../outputs/verification/mygo-kimi-20261025/summary.json)、[证据复核](../../outputs/verification/mygo-kimi-20261025/reviews.json) 和 [最终审计](../../outputs/verification/mygo-kimi-20261025/final-audit.json)；outputs 属于本地忽略产物。

### 4. 指标对比

耗时单位为秒。历史九题使用客户端耗时，四条 Event Chat 使用服务端耗时；当前使用 ASGI HTTP 客户端计时，T06 单独注明外部停止。各题仅一次，不报告 P95、全库召回率或整体回答准确率。

| 用例 | 历史耗时 | 本轮耗时 | 自动检查 | 返回证据复核 |
|---|---:|---:|---|---|
| chat-T01 | 215.6 | 55.2 | 通过 | 两段相识片段；事实与人物心理解释仍混杂。 |
| chat-T02 | 215.1 | 149.7 | 来源未通过 | 漏课堂自我介绍、午餐回述；初遇另有 0.4 秒严格边界差。 |
| chat-T03 | 188.7 | 1180.4 | 通过 | 三张演出卡，但正文把实际 EP12 的第三段写成 EP13。 |
| chat-T04 | 204.4 | 87.7 | 通过 | 三段笑点有来源；正文编号 1、3、2 与卡片顺序不一致。 |
| chat-T05 | 277.4 | 1200.1 | 执行失败 | 1200 秒后 HTTP 502／ReadTimeout，没有完成五段回顾。 |
| chat-T06 | 108.6 | 1323.8（外部中止） | 执行失败 | 超过执行预算仍阻塞于 SSL 读取，外部停止；没有最终动作片段。 |
| chat-T07 | 154.0 | 519.6 | 来源未通过 | 漏可检索的 EP04 正例；EP02 不可检索；声称 11 处却交付 9 卡。 |
| chat-T08 | 91.2 | 378.6 | 通过 | 前序关系检索更具体，但正文引用暂停点之后的对话。 |
| chat-T09 | 249.2 | 236.2 | 通过 | 关系链和时间约束基本成立；末卡截在所引台词开始处。 |
| chat-event-original | 103.4 | 111.5 | 通过 | EP13、祥子、原句与前后语境匹配；译本推测未独立核验。 |
| chat-event-jp | 226.1 | 38.3 | 通过 | EP04、立希、日文原句与实际播放区间匹配。 |
| chat-event-drift | 156.1 | 92.1 | 通过 | 找到参考近义场景并给出替代；对“与此无关”的译义有过度归因。 |
| chat-event-premise | 154.1 | 204.7 | 通过 | 定位到参考台词，但把参考说话人爱音说成爽世。 |

四条 Event Chat 的平均记录耗时为 159.9→111.6 秒，但 premise 的说话人错误、drift 的译义限制仍然存在，不能据此称质量整体提高。九题当前记录耗时中位数为 378.6 秒，历史为 204.4 秒；包含失败及外部中止的统计只反映本次任务记录，不是服务延迟分位数。T03 单题 112 次 Kimi 请求、约 274.5 万已报告累计 token；T05 64 次、约 150.0 万，仍未完成。

正式 13 题记录 **330 次 Kimi 请求，7,806,132 已报告累计 token**（输入 7,645,459、输出 160,673；输入中缓存 5,213,053）。其中 217 次属于 `structured_query`；4 次失败请求缺供应商用量，另有 T06 未终止请求无终态用量，不能当作零。外部环境中断的旧 T03 尝试另耗 8 次 Kimi 请求、181,811 token；不混入正式 13 题，也不删除。未核定实际账单价格，未换算金额。各批共有 562 条已结束调用记录，call_id 全部唯一，GPT 调用数为 0。

需要补充的业务验收与取舍：

1. **收敛与失败成本。**复杂题多次耗尽 Query 阶段；关闭调用次数限制后，时间预算无法防止大量低收益回合。`agent/chat/supervisor.py` 只合并成功 Query 的证据，失败报告不携带已找到的候选，会让主 Agent 重新检索。应评估停止条件、超时的已验证候选交接，以及能够真正终止阻塞 I/O 的执行边界。两次上下文压缩后仍超过应用估算预算的告警一并保留，不能仅凭告警断定根因。
2. **实际交付一致性。**T03 把 EP12 写为 EP13；T07 声称 11 处却只有 9 卡。`submit_video_item` 按 `video_chunk_id` 覆盖修改，同一 Chunk 中的多个时间段无法分别保留；本轮 EP04 12:09／12:33／12:44 正好暴露这个合同限制。正文数量、集数、区间应和最终卡片核对，单纯检查“有卡”不足。
3. **播放时间约束。**T08 卡片没有越界，正文却引用 15:03.9、15:05.7、15:55.2 的未来对白来解释 15:00 场景；已从最新 V2 证据核实。时间过滤需要约束当前理解输入和正文推理，而不只约束最终卡片。T09 明确不提前断定后续谈话，表现较好，但末卡结束在所引句开始处。
4. **身份与不确定性。**premise 的参考说话人是爱音，当前源转写 target speaker 为空，回答仍确定地写成爽世。应保留身份未知，或增加有根据的核验；自动来源覆盖通过不代表人物身份正确。EP02 索引就绪后仍需补全受数据前提影响的回归。

本轮新增的是可复核的失败样例和用量证据，生产架构复杂度没有变化：当轮生产代码改动 0，仅补充两份文档及本地测试产物。去 Pi 的职责简化收益仍由前条实现记录支持；在所选**查询能力覆盖**用例中，Deep Agents + Kimi 存在收敛／超时、交付与时间／身份约束问题。用户指定的 **Chat behavior 10 题仍未在当前体系上复跑**，不能由本轮推断其通过或失败；后续生成式对照继续使用 Kimi。

<a id="devlog-cloud-chat-checkpoint-20261024"></a>

## 2026-10-24：Cloud Chat 去 Pi Host 与 Deep Agents 持久化 Checkpoint

记录范围：Cloud Chat Supervisor 的架构决策、实现与验收。本地 Codex／Claude、Parse 和 Sandbox 不在本条修改范围内。已完成代码实现、离线回归、开发环境真实 Chat 对照和 MySQL 重启恢复；生产迁移与部署验收未执行。真实对照同时暴露耗时与输出时间一致性风险，不写成无差异通过。

### 1. 问题与现状

云端引入 pi 时有明确约束：不能采用一个 Conversation 一个 Codex 进程，不准备在云端维护 Codex 账号；目标单机 4 CPU／8 GB，希望一个 Host 复用多 Conversation；当时 Deep Agents 使用 `InMemorySaver` 且忽略 `session_context`，原生上下文、compact 和跨进程恢复尚未形成可靠合同。引入 pi 因而避免了当时立即自研 SessionManager，并提供了单 Node Host 多会话、tool 历史和 compaction。历史验证仅覆盖 fake API、协议与隔离，H03 容量、RSS、p95、真实模型等待与费用均未验收，见[历史职责拆分](#devlog-history-42)。

后续排查还修正了早期失败归因：任务 `4065a67a-358b-4640-910b-46a04601ed5b` 的 chunk 0 重试耗尽，现存 trace 显示模型输出“自然语言总结＋JSON”，最终不符合 `CanvasCompletion` Schema。这是输出合同与恢复策略问题，不能用来证明 Deep Agents 工具循环存在同类故障。

pi 接入后仍保留 SQL 展示消息和业务终态，同时增加 `runtimeSessions` 的 sessionFile／leaf／digest、pi JSONL 原生上下文、Python↔Node IPC 和确认／回滚。Host 在 JSONL 不可用时仅按 SQL user／assistant 重建，tool call／result 无法原样恢复。由此形成“外部 SessionManager 已存在，但业务仍需维护映射和有损降级”的协调成本。

现在 Deep Agents＋LangGraph 持久化 checkpointer 已提供另一套实现：graph state 按 `thread_id` 保存，原生 messages、tool 结果、compact 后状态及 StateBackend 的 `files` 均可进入 checkpoint。当前单实例不需要独立 Node 故障域或多 API worker 共享 Host，因此 pi 的模型／工具循环、原生上下文、compact 和恢复职责出现重复。这里的“冗余”是当前约束下的职责重复，不是历史上错误引入，也不是已证明 Deep Agents 性能更好。

修改前静态基线：pi 路径包含 1,592 行生产代码和 1,162 行测试代码（不含 lockfile 和设计文档），`PiHostConfig` 有 15 个专属字段；Chat 激活后每个 API worker 额外运行一个 Node Host。净维护成本不能仅按这些删除量计算，因为新接入、测试和迁移仍有成本。

### 2. 方案候选

| 方案 | 收益与适用边界 | 代价与本次选择 |
|---|---|---|
| 保留 pi Host | 保留成熟 SessionManager、独立故障域；适合未来多 worker 或独立生命周期成为硬需求 | 保留 Node、IPC、session 映射和 JSONL 运维；当前不选 |
| Deep Agents＋现成持久化 saver | 复用现有 loop、compact 和 checkpoint；去掉额外 harness 进程，可复用现有 MySQL | **本次选择**；仍需接入、BYOK 边界核对、兼容测试、迁移和运维 |
| Deep Agents＋自研 ContextStore | 完全掌握 SQL 投影和 compact 合同 | 现成 saver 不满足时才采用，不预先增加一套存储引擎 |
| Chat 直接使用模型 API | 少一层 SDK | 不适合开放式多轮工具 Chat；Parse 的直接 API 决策不混入本条 |

最终选择 Deep Agents＋`PyMySQLSaver==3.0.0`。`PyMySQLSaver` 的依赖声明接受当前 `langgraph-checkpoint==4.2.0`，源码包含 `task_path` 和 pending-write 恢复；是否存在 DeltaChannel 行为差异必须以实际测试判断，不能把“可安装”记成“已兼容”。

实现采用“本轮内存执行，SQL 消息确认后才以 `Overwrite` 提交完整 checkpoint”的方式。内存 checkpoint 只是本轮临时状态，MySQL 只保存已确认状态；失败、取消和关闭不得删除已确认历史，只有显式删除 Conversation 才删除对应 checkpoint。没有 checkpoint 的旧 Conversation 仍按现有 SQL 展示历史冷启动，历史 pi JSONL 保留但不承诺旧 tool 上下文无损迁移。`runtimeSessions` 继续服务本地 Codex，不顺手清理。

### 3. 方案验证

已完成以下实现与离线验证：

1. 接入 `PyMySQLSaver`、Server 生命周期及现有 SQLAlchemy 连接池；`init-db` 才执行 `setup()`，API 启动不自动迁移。增加[checkpoint Schema](../migrations/20261024_deepagents_checkpointer.sql)，已在开发库初始化，未在生产数据库执行。
2. 每轮使用独立内存 checkpoint；确认后以 `Overwrite` 写入完整持久状态。专项覆盖确认提交、失败与取消、确认写失败、关闭、显式删除、tool 历史和 compact/offload。checkpoint 与连接专项为 **34 passed／2 skipped**。
3. Chat、Gateway、Server 和 cloud 相关套件为 **782 passed／4 skipped／10 failed**。这 10 个配置失败在 `79776d0` 原始版本上逐项复现，不属于本次回归。
4. 全量非 live 测试为 **4,905 passed／107 skipped／1 deselected／12 failed**。除上述 10 项外，另外 `test_imports_do_not_load_v1_in_a_fresh_process[common]` 与 `test_fallback_media_is_checked_against_the_active_context_budget` 也在原始版本复现；因此 12 项均为既有失败，本次没有新增失败。

随后完成开发环境真实验收：

5. 开发库执行 `init-db` 后，真实 MySQL 的 6 个 checkpoint 集成用例全部通过，覆盖 tool 历史、DeltaChannel、compact 摘要事件和 offload 文件恢复。
6. Codex Supervisor 连续三轮均完成；Deep Agents＋DeepSeek Supervisor 连续三轮均完成，并在第二轮后重启 API。数据库回放恢复 **30 条原生消息，其中 13 条 ToolMessage**，最后一条为重启后的第三轮回答，证明恢复不是只依赖 SQL 展示文本。视频资源按 Range 返回 **206 Partial Content／video/mp4**。
7. 对照使用同一 MyGO series、同一组三轮问题和新 Conversation。Codex 为 `codex_local/gpt-5.6-sol`，Deep Agents 为 `deepseek_api/deepseek-flash`，检索子任务继续使用 Codex。因此这是产品版本对照，不是同模型纯 harness A/B；原始结果与质量发现见[comparison.json](../../outputs/verification/chat-harness-ab-20261025/comparison.json)。

真实对照发现：Deep Agents 第二轮更快，但第一、第三轮明显更慢；第三轮文案为 18:00–18:18，视频块结束时间却为 18:38.1，存在 20.1 秒结束时间不一致。不能把“正常完成和重启恢复”写成“性能或质量不下降”。

### 4. 指标对比

#### 真实 Chat 对照

| 轮次 | Codex 用时 | Deep Agents 用时 | 倍数（Deep Agents／Codex） | 业务观察 |
|---|---:|---:|---:|---|
| 第 1 轮 | 32.914 s | 166.444 s | 5.06× | 均完成；Deep Agents 更冗长，视频范围为 50–60 s，Codex 为 54.6–56.0 s |
| 第 2 轮 | 16.054 s | 3.564 s | 0.22× | 均正确引用上一轮 speaker、集数和时间，证明多轮上下文有效 |
| 第 3 轮 | 18.234 s | 75.975 s | 4.17× | Deep Agents 重启后完成连续检索，但出现文案与视频块结束时间不一致 |
| 合计 | 67.202 s | 245.983 s | 3.66× | 均完成 3/3 轮；不能据此推断普遍时延分布 |

#### 架构、恢复与成本

| 指标 | 修改前 | 修改后当前结果 | 证据状态 |
|---|---:|---:|---|
| 额外 Node Host／API worker | 1 | 代码路径已为 0 | 源码与开发 API 确认；生产未重启 |
| Python↔Node IPC 与 pi-session 映射 | 存在 | 已删除 | 源码确认 |
| pi_host 专属配置字段 | 15 | 0 | 源码确认；旧配置明确拒绝 |
| 跨轮上下文主要存储 | SQL＋pi JSONL | MySQL checkpoint | 开发库已验证；生产未迁移 |
| pi 生产／测试源码 | 1,592／1,162 行 | 共删除 2,754 行 | 静态删除量，不等于净收益 |
| package／lock 配置 | 1,548 行 | 同步删除 | 主要为 lockfile，不计作认知收益 |
| 新增 MySQL 接入／迁移／集成测试 | 无 | 57／55／145 行 | 新增运维与验证成本 |
| 本轮相关文件 | 多一层 pi 架构 | 42 个文件（21 M／16 D／5 A） | 不含并行文档迁移的内容归属 |
| 离线回归 | 历史 pi Python 520 passed、Node 24 passed | 相关 782 passed；全量 4,905 passed | 既有 12 失败原始版本均复现 |
| 真实 MySQL 集成 | 未接入 | 6 passed | tool、DeltaChannel、compact、offload 均通过 |
| 重启后原生上下文 | SQL 降级路径丢失 | 30 条消息／13 条 ToolMessage 可回放 | 开发环境实测 |
| BYOK | pi 有 profile 通道；Server 未找到实际 `model_profile=` 注入 | 未宣称新增 BYOK；保持当前授权模型边界 | 用户级 BYOK 仍未形成端到端验收 |
| 完成率、正确性、覆盖度 | 无同条件基线 | 两组均完成 3/3 轮；Deep Agents 第三轮存在时间不一致 | 小样本，不能声称质量不下降 |
| token、费用、RSS 与存储增长 | 无有效对照 | 未测 | 新增 DB 写入和 retention 成本仍待量化 |

本轮确定的技术结果是：pi 的重复进程与映射被删除，Deep Agents 能正常完成真实多轮 Chat，MySQL 能跨进程恢复原生工具上下文。当前不能给出性能或质量不下降的结论；Deep Agents 的耗时和第三轮时间一致性需要后续处理，生产迁移仍需单独验收。

### 5. Kimi K3.1 Pro 接入复验（2026-10-25）

本次只增加 Chat 的 Kimi Coding Plan 模型适配，不改变 Parse、根目录 `config.yaml`、数据库 Schema 或生产部署。最小直连 `k3.1-pro` 成功，返回 `ok`，烟测为 91 prompt tokens＋46 completion tokens＝137 total tokens；该数值不包含后续 Chat 消耗。现有 `standard` 请求原样探针被 HTTP 400 拒绝，服务端明确返回 `invalid temperature: only 1 is allowed for this model`，因此新增独立 `kimi` profile：使用 `temperature=1`、透传 `reasoning_effort`，保持 `max_completion_tokens`，不影响 DeepSeek 的 `temperature=0` 与 thinking 合同。

Agent 工具循环、结构化检索和配置解析的 6 个定向合同测试全部通过；相关 Gateway 套件为 **356 passed／1 failed**。剩余失败是未修改的根配置选择 `codex_local`，而既有默认配置断言期待 `deepseek_api`，不属于 Kimi 请求适配失败。

真实验收使用同一 MyGO series、同一组三轮问题，Chat `agent` 与 `supervisor` 均配置 `kimi_coding/k3.1-pro`、`reasoning_effort=low`，Conversation 为 `932a3aec-c9cf-4f32-a371-6b75fc84ccab`。真实流量覆盖 Supervisor 工具循环；结构化检索路径由 mock-transport 合同测试覆盖，不把本次结果写成所有子代理路径均已 live 验收。

| 轮次 | Kimi 用时 | 结果 |
|---|---:|---|
| 第 1 轮 | 53.044 s | 正确定位第 13 集约 00:54.6–00:56，返回 50–60 s 视频块 |
| 第 2 轮 | 25.730 s | 正确沿用上一轮台词、speaker、集数和时间 |
| 第 3 轮 | 29.358 s | API 重启后完成；文案与视频块均为 21:01.7–21:10.7 |
| 合计 | 108.132 s | 3/3 轮 COMPLETED；小样本不能外推 p95 或普遍性能 |

第二轮后仅重启开发 API（PID 29003→29742），MySQL／Elasticsearch 与数据保持运行。重启后的图状态回放得到 **24 条原生消息、9 条 ToolMessage**，同时包含第一轮时间锚点和第三轮回答；视频 Range 请求返回 **206／video/mp4**、1,024 bytes。第三轮首次 `read_retrieval_evidence` 出现一次 `UNKNOWN_ITEM`，随后通过新检索恢复；三轮 query-hook router 均完成，但未启动的本地 rewriter 均以 `ConnectError` fallback，以上问题未在本次修改。

原始结果见[kimi-results.json](../../outputs/verification/chat-harness-ab-20261025/kimi-results.json)。本次没有测量生产 compact 频率、长任务 Plan 额度消耗、RSS、存储增长或同模型质量对照，不能据此声称质量不下降或性能普遍优于 Codex／DeepSeek。测试启动的本地 API、Web、MySQL、Elasticsearch 已全部停止；未执行生产迁移、生产重启、commit 或 push。

<a id="devlog-canvas-20261023"></a>

## 2026-10-23：Canvas 完整条目读取、可修复反馈与端到端评估

记录范围：2026-10-22 至 2026-10-23 的工具修订、已有实验及本次成本核算。本文整理既有结果，没有为写文档再发起模型实验，也没有改变生产配置或计费规则。后续实验按日期追加，保留当时的模型、数据、源码和价格口径。

相关设计：[ContextCanvas 设计与历史实施记录](../design/02-agent-harness/context-canvas-design.md)、[工具设计研究](../../docs/research/2026-10-22-canvas-agent-tool-design.md)。历史文档中的 Chat Canvas 和 greedy 打包状态不代表当前实现。

### 1. 问题与现状

以下“原方案”指本次修正前的设计。产品目标是完成视频事件的解析、修订和总结，以及向用户返回准确的台词与可播放片段。

#### 状态和写入

Canvas 已在模型消息之外持有完整草稿、引用和只读源证据。模型通过局部操作修改草稿，宿主校验后提交；未提到的事件保留，最终模型文本不能重新覆盖草稿。revision、原子批次、重放幂等、角色/成员范围、源身份和只读限制本来就存在。

此前 Parse 没有显式删除事件的操作，并要求所有初始事件 reference 保持存在。数组字段中的 remove 只能删除数组元素，无法删除一个 event；这让模型无法明确撤销错误或重复事件。

#### 读取和反馈

| 原行为 | 具体问题 |
|---|---|
| 指定 reference 默认读取 `overview`，基类正文 section 叫 `value` | 已知对象也可能多读一次目录，或猜正文名称 |
| 单项与批量复用 `start/end` | 单项表示字符、批量表示项下标，单位歧义 |
| 目录继续读取用泛称 `cursor` | 与正文字符范围、批量条目续读混淆 |
| 字符数、项数、8000 token、结果字节多层限制 | 预算彼此覆盖，模型反复试探可读范围 |
| 批量结果包含完整项和未完整返回项的简略信息，按 `complete_end` 续读 | 已读边界不直观，恢复时重复元数据较多 |
| 参数错误只保留前四个字段位置 | 根级校验可能只返回 `arguments`，具体缺项原因被丢弃 |

这个阶段的问题是工具合同不够清楚和重复限制，不是“Canvas 没有保存完整内容”。

**对产品的影响：**读取单位歧义和重叠限制增加了模型继续读取、修复调用的负担；缺少事件级删除使错误或重复事件难以在原编辑流程内撤销。Chat 侧过于简略的参数反馈导致纠错和调查往返，表现为等待更久、更多失败调用；已有实验仍出现遗漏和可见正文错误，因此“请求完成”也不足以代表答案质量达标。

同一排查批次还发现两个独立问题：检索 SQL 读取并排序不需要的大 JSON，触发 MySQL 1038 并拖慢检索；Supervisor 在关闭额度限制后仍向子 Query 传递默认 24 次额度，使调查被提前截断。这些后端问题与 Canvas 读取协议分属不同环节，分别验证和归因。

当前 Chat 使用检索及证据分页工具，尚未接入 Canvas 读取协议；本次 Chat A/B 验证的是共享错误反馈，不能以 Chat 的结果替代 Canvas 全链路业务验证。

**原问题 B06/C07/C18 的历史证据：**

原编号 **B06、C07、C18**。B06 是早期共用结构化 Canvas 设计，C07 是需盘点的参数修正反馈，C18 是真实调用中 overview 默认、单/批 start/end 双语义及引用恢复造成误用。批读当时已经存在，不能写成新功能。下述历史数据不能替代那一轮的实际交付与测试。

来源：[B06/C07/C18](problems.md)、[通用 Canvas 工具](../../agent/utils/context_canvas_tools.py)、[Canvas 设计](../design/02-agent-harness/context-canvas-design.md)。

### 2. 方案候选

| 方案 | 状态与逻辑 | 收益假设 | 代价与适用边界 |
|---|---|---|---|
| A. 旧多重限制 + 位置续读 | 历史对照，不作为当前默认 | 单次上下文较小 | 多次读取与歧义；保留作回归基线 |
| B. 完整 item + 连续前缀 + 明确剩余引用 | **当前实现**；超大项走 section/字符恢复 | 减少工具往返，完整项边界明确，保序 | 单次结果更大；仍依赖 Agent 读取剩余项 |
| C. greedy 完整项填空 | 已试验，最终未采用 | 最大化每次字节容量利用率 | 隐式跳序；时序任务收益不足以抵消该复杂性 |
| D. Chat 采用同类完整证据 item 协议 | **可选，未实现/未测**；保留检索和来源校验，调整证据供给而非重新照搬旧 ChatCanvas 审核架构 | 少读目录、少复制 cursor、按任务保持必要证据完整 | 需要同时验证范围过滤、证据曝光、答案覆盖和累计输入 |
| E. 将完整任务状态直接注入 prompt | **可选，未实现/未测**；不再通过工具逐项获取 | 极小且固定任务可能减少读取轮次 | 大状态及多轮历史重复注入；不能把全视频/全库无差别注入 |

当前采用 B，并保留具体错误反馈。D 应作为独立实验，不能把 B 的离线指标直接套给 Chat。是否选择 E 取决于任务状态规模和重复读取成本，而不是“少调用一次工具”本身。

**选择原因：**B 将批量恢复收敛为完整项、连续前缀和剩余引用，减少歧义，同时保留时序；C 的少量调用收益不足以抵消隐式跳序。具体错误反馈直接补充纠错所需信息。SQL 窄投影和额度 flag 贯穿分别处理已定位故障；D/E 涉及不同的证据供给方式，仍作为待验证候选。

#### 读取：先尝试完整返回，真正超预算再恢复

1. 只传 `collection` 获取目录；已知对象通过 `reference/references` 直接读取，默认 `section=content`。`overview` 作为显式导航用途保留。
2. 批量只接收引用，不再接收 item 下标范围。先尝试返回全部请求项；实际超过 `maximum_result_bytes` 时，返回按请求顺序能容纳的**连续完整前缀**。
3. `items` 只放完整返回项，未返回的引用进入 `remaining_references`；Agent 用这些引用自行继续批量召回。
4. 目录只有真正超出字节预算才分页，续读参数明确为 `catalog_cursor`。
5. 单项本身过大时，使用更小的命名 section，或 `reference + char_start/char_end`。批量首项装不下会返回空 items 和剩余引用，不静默丢项。
6. Canvas 仅读取路径的字符/项数构造参数保留兼容，不再执行其独立限制；移除独立 8000 token cap，统一检查真实序列化结果字节。**这不等于删除模型上下文容量或业务权限边界。**

实现：[读取工具](../../agent/utils/context_canvas_tools.py)、[批量与范围返回](../../agent/utils/tool_output.py)、[Canvas 基类](../../agent/utils/context_canvas.py)。

中间曾采用 greedy 填空：当前项装不下就继续塞后面的小项。实测它只为 Summary 少省了一次调用，却改变跨调用的时间顺序。因此最终采用连续前缀；Agent 若需要其他项，应明确请求那些引用，而不是由打包器隐式跳序。

字符恢复提示也修正为可执行的精确边界：按建议结束位置读取，再以实际返回的结束位置继续，避免“允许更小 end，却要求直接跳到较大边界”的漏读风险。

#### 写入：明确删除，省略仍表示保留

新增 `remove_event(event_ref)`，允许删除角色可修改的初始事件或本轮新增事件；与 add/patch/set 共用原子批次。删除后不再要求该 reference 存活，但存活事件继续校验 source/time/member 等约束。

- 不修改原始 evidence/background；不新增额外人工确认、软删除或强制理由。
- 不提及事件仍然表示保留；只有显式 remove 才删除。
- 同批删除后再修改该对象会失败并整体回滚；相同 batch 重放沿用既有幂等回执。
- 模型终态文本仍不代替 Canvas 的最终草稿。

实现：[Parse 操作与校验](../../agent/video_parse_v2/timelineEvent_canvas.py)。

#### 反馈：让模型能修复具体调用

`INVALID_ARGUMENTS` 从只列字段路径，改为列出字段路径、错误类型及具体原因。根级 validator 的“缺少 query/top_k/channel_weights”等原因现在可见；没有把非法参数静默改成默认值。Parse/Summary 的读取指引同步调整到新的参数与恢复方式。[公共反馈](../../agent/utils/tool.py)

单条反馈可能更长。判断它是否值得，要看是否减少完整任务的错误往返和重复调查，而非只比较这条消息的长度。

**原问题 B06/C07/C18 的历史证据：**

历史候选是统一 Chat/Parse 的结构化上下文和受限编辑、明确 reference(s)/目录/正文、单项字符范围和批量完整项语义、提供可执行错误恢复提示。区分可修正参数、数据完整性和依赖失败，不能把所有异常吞成 INVALID_ARGUMENTS。保留只读原始证据、revision、批次原子性和 Summary 阅读审计；这些边界是事实完整性责任，而非任意“防御代码”。

共享契约降低多套工具的认知和维护成本；更完整返回会扩大单次上下文，分页/恢复则增加调用轮次。必须比较整体 token、工具错误、总时延与语义保留，而非仅减少参数。

### 3. 方案验证

#### 验证范围与归因

| 改动层 | 解决的问题 | 已有证据 | 不能据此声称 |
|---|---|---|---|
| Canvas 读写协议 | 读法歧义、多重截断、缺少明确删除 | 真实快照的确定性读取对照、合同测试 | Parse/Refine/Summary 全链路模型更快或更准确 |
| 公共 Tool 错误反馈 | 模型只知道参数错，不知道怎么修 | 4 轮真实 Chat，仅切换旧/新 validation feedback | 整个历史旧 Chat 与最新 Chat 的全量版本比较 |
| 检索 SQL 窄投影 | 大 JSON 排序报错、无用读取/解码/复制 | 真实 MySQL/ES 检索与回归 | 被省掉的约 360 MiB 原本进入了模型 context |
| Query 额度配置贯穿 | 关闭限制仍错误套用默认 24 次额度 | 先红后绿回归、最终服务烟测 | 此修复贡献了前面的 A/B 提速比例 |

**当前 Chat 未注册 Canvas 读取工具。**它仍使用 `retrieve`、`search_video_events`、`read_retrieval_evidence`。Canvas 的离线收益与 Chat 的实测收益必须分开记账；共享的错误反馈才是本次 Chat A/B 的实验变量。[Chat 工具注册](../../agent/chat/video_search/tools/registry.py)

#### Canvas 完整读取：case 和方法

- 输入：EP04 的 24 个 `understanding-first-visible` 快照，392 个 Event；这是首次持久化 Understanding 产物，不是实际 post-prefill Refine 输入。
- Event：8 个不重叠的三 Chunk 窗口，各 32、46、39、58、63、71、52、31 项。
- Summary：同样的 24 个 Chunk，background 为空。
- 两版均传 32000 bytes / 12000 chars / 100 items 的构造参数；**旧版执行多重 cap，新版仅执行字节预算，有效限制不同。**
- 调用者已知 `section=content`，没有给旧版人为增加 overview 调用；每窗先读一次 catalog，旧版按 `complete_end` 恢复，新版按 `remaining_references` 恢复。
- 比较每项恢复内容与 canonical section 字符串，检查引用重复、未读项及跨调用顺序。
- **没有模型调用。**tokens 用 o200k 统计工具返回文本，不包括请求/schema/历史/模型输出，不是完整任务账单。

#### 完整 Chat：case、控制变量与限制

| Case | 自然请求 | 顺序 |
|---|---|---|
| Quote | 真是高高在上呢 哪里出现过？ | 旧→新 |
| EP13 | 请找《BanG Dream! It's MyGO!!!!!》第13集祥子的对白，给出原话和能播放的片段。 | 新→旧 |

Supervisor / Query 均为 `codex_local / gpt-5.6-luna`，相同 MyGO series、请求 topK=3、空历史、新 conversation 和幂等键。Agent 可自行改变检索 top_k/权重和调查路径。每个问题每版一次，共 4 轮，串行运行；没有人为注入错误参数。

独立进程通过正式 HTTP ASGI 路由调用真实模型/MySQL/ES。计时从发送消息请求到最终答案返回，包括消息持久化和调度，不包括浏览器渲染及真实网络传输。两臂都已应用 SQL 窄投影；唯一替换的是 `_validation_feedback`，不代表整个旧 Chat 版本。四轮均发生了被测参数错误，实验变量实际进入了执行路径。

两臂都仍包含后来修复的子 Query 24 次额度漏洞；每轮 rewriter 均连接失败回退并缺少该次 usage。模型路径和缓存会波动；不能给 p95、统计显著性或普遍提速承诺。

#### 邻接修复与部署验证

SQL 使用真实 MySQL/ES 检索核对结果数量，并运行相关回归；额度修复验证关闭限制时两个各 25 次检索均可完成，开启限制时仍共享预算。最后通过重启后的 main API 发送真实 HTTP 请求确认部署可运行。通过数量、耗时和输出差异见“指标对比”；这些验证发生在此前修复批次，本次文档整理没有重跑模型、测试或重启服务。

#### 后续待验证的业务 case

下一轮目标是判断“少等待的价值，是否值得新增模型费且保持答案质量”，而非继续追求工具次数最低。以下均是计划，不是已完成结果。

| Case | 实验变量/任务 | 主要质量验收 | 性能与成本观测 |
|---|---|---|---|
| P1：局部修正和删除 | 固定事件草稿，只修目标两项/删除指定重复项 | 未涉及事件不变，无误删，批次失败不留部分状态 | 模型/工具轮次、恢复次数、总 U/C/W/O、完整时长 |
| P2：整窗 Refine / Summary | 同一 3 fps 输入、同一快照，旧读取 vs 当前完整项前缀 | 人工确认必须保留事实、顺序、遗漏和错误归属 | 总 token 与最大单次输入、首段/最终时长、按真实价格算费 |
| P3：超大单项和跨批恢复 | 实际 voiceprint/长 section，单项超过字节预算 | 完整恢复、无跳段，未读不误计为读完 | 额外召回与失败次数、context 峰值 |
| C1：具体台词定位 | 保留旧/新错误反馈对照 | 集数、speaker、原话、时间及可见正文无矛盾 | 完整时长、失败/委派、费用 |
| C2：整集人物对白 | Chat 当前 excerpt/cursor vs 候选完整证据 item；先固定 episode/speaker 范围 | 人工清单上的覆盖与引用准确性；明确示例还是全集 | 检索/续读/重复读取、子 Query 输入、完整费用 |
| C3：查无结果或部分索引 | 保持相同缺口，观察是否不必要扩搜 | 不编造、不把部分覆盖说成不存在、不误称完整 | 无效重试、结束时长、无收益上下文占比 |

固定模型、reasoning、schema、预算、数据/index 版本、历史和输入范围；每 case 多次交错运行，保留冷/热缓存区别和失败样本，留出未参与调参的问题。只用 4 轮不能给 p95 或稳定收益区间。

至少区分以下状态：**系统召回了什么、实际返回给模型什么、模型又读取了什么、最终答案用了什么**。评测产物保存逐次必要工具返回、引用/页范围和逐请求 usage，避免只凭最终 full evidence 推断模型见过全部内容；常规运行日志继续控制敏感内容暴露。

费用指标采用实际 provider/model/tier 的 `U/C/W/O`，并记录每次请求是否跨长上下文档位；SDK 整段 token 累计不能代替每次请求信息。质量相当后，再比较单次完成成本、等待时间和成功率；更贵更快但漏答不算无条件收益。

#### 原始材料与复现条件

| 材料 | 路径 / 用途 |
|---|---|
| Canvas 原比较脚本 | [compare_reads.py](../../outputs/verification/canvas-chat-review-20261023/read-comparison/compare_reads.py) |
| 前缀候选脚本 | [compare_prefix.py](../../outputs/verification/canvas-chat-review-20261023/read-comparison/compare_prefix.py) |
| Canvas 最终生产结果 | [final-results.json](../../outputs/verification/canvas-chat-review-20261023/read-comparison/final-results.json) |
| Chat A/B 脚本及版本哈希 | [chat_ab.py](../../outputs/verification/chat-latency-20261023/chat_ab.py)、[runtime.json](../../outputs/verification/chat-latency-20261023/runtime.json) |
| Chat 请求结果与用量 | [chat-summary.json](../../outputs/verification/chat-latency-20261023/chat-summary.json)、[usage-summary.json](../../outputs/verification/chat-latency-20261023/usage-summary.json) |
| SQL 修复与测试 | [sql-report.md](../../outputs/verification/chat-latency-20261023/sql-report.md) |
| 额度修复与先红后绿 | [limits-fix-report.md](../../outputs/verification/chat-latency-20261023/limits-fix-report.md) |
| 最终真实 HTTP 烟测 | [live-summary.json](../../outputs/verification/chat-latency-20261023/live-summary.json) |

Canvas 原脚本硬编码的 `/tmp` before 目录已不存在，不能直接声称原命令现在即可复跑。已按 `results.json` 中 SHA-256 核对，[保存的旧源码](../../outputs/local-services/api-source-322db8e/agent/utils) 四个相关文件完全一致；复跑前须先恢复对应隔离 baseline 路径或在隔离脚本中改为该副本，并核对输入 hash。本文没有修脚本或重跑模型。`outputs/verification` 属本地验证产物，关键数字在本 devlog 中保留，避免只依赖临时文件。

当前读取实现 SHA-256 与最终结果记录一致：

```text
context_canvas_tools.py  e5975d005442829c5d1bd67d5c05c7faaa23786c20b5cfad4a6b7aa6ad4deb23
tool_output.py           35cab63cbdfe331a077c096d5c8063b9da9f65a7ea615b071b4b02fdb3fce736
```

**原问题 B06/C07/C18 的历史证据：**

C18 真实 B 样本未用批读，四次猜 8000 字范围面对实际 359/1663/1866/1724 字产生四个错误，Supervisor 再有两次范围错误；C 样本把 batch overview 的 end=20 用于一项目录，Chat 又读不存在的 catalog section。错误工具只花毫秒，不能用它们单独解释 154–156 秒。原历史记录尚无修复后实验；后续交付须引用 Canvas 专项原始验证，不沿用“待实现”旧状态。

### 4. 指标对比

#### 当前综合判断

当前方案减少了 Canvas 完整读取的工具往返和返回冗余。真实 Chat 对照中，两类问题都更快，工具失败也更少；简单台词定位同时减少 tokens，广泛对白汇总的 tokens 则增加，答案覆盖没有证明更好。保留当前读取协议、具体错误反馈及已确认的后端修复；不能将它们合并宣传成“整个 Chat 的时间、成本和质量稳定同步改善”。

#### Canvas：最终生产实现与旧版

| 指标 | Event 旧版 | Event 最终版 | Summary 旧版 | Summary 最终版 |
|---|---:|---:|---:|---:|
| 完整读取项数 | 392 | 392 | 24 | 24 |
| 总调用（含目录） | 31 | 23 | 13 | 8 |
| 详情调用 | 23 | 15 | 12 | 7 |
| 总返回 bytes | 453,539 | 418,617 | 190,883 | 178,042 |
| 工具返回 tokens | 162,774 | 152,017 | 51,808 | 47,664 |
| 单次最大 bytes | 23,023 | 31,820 | 19,740 | 29,684 |
| 单次最大 tokens | 7,959 | 11,457 | 5,492 | 8,132 |
| 顺序变化窗口 | 0/8 | 0/8 | 0/1 | 0/1 |
| 最终未读项 | 0 | 0 | 0 | 0 |

Event 总调用减少 **25.8%**，返回 tokens 减少 **6.6%**；Summary 分别减少 **38.5% / 8.0%**。合计调用 **44→31（−29.5%）**，返回 tokens **214,582→199,681（−6.9%）**。canonical 正文逐字一致，Event/Summary 正文总量仍为 251,750 / 149,279 bytes；减少的是读取调度和返回冗余。

单次最大 tokens 上升，说明“总返回更少”和“单次上下文更大”可以同时发生。未测试真实模型的完成质量、推理累计 tokens、端到端时间和账单；不能将模型调用为零写成“任务耗时为零”。这批真实 item 都能整项装入，未覆盖超大 voiceprint 单项的真实模型恢复。

#### Canvas：greedy 与连续前缀的取舍

| 指标 | Event greedy → prefix | Summary greedy → prefix |
|---|---:|---:|
| 总调用 | 23→23 | 7→8 |
| 返回 tokens | 151,951→152,017（+66） | 47,317→47,664（+347） |
| 返回 bytes | 418,494→418,617（+123） | 177,235→178,042（+807） |
| 顺序变化窗口 | 2/8→0/8 | 1/1→0/1 |

为保序，Summary 比 greedy 多一次调用和约 0.73% 返回 tokens。这是小额机械代价；尚未用 LLM 证明保序带来多少质量收益。

证据：[旧版/greedy 原始数据](../../outputs/verification/canvas-chat-review-20261023/read-comparison/results.json)、[prefix 对照](../../outputs/verification/canvas-chat-review-20261023/read-comparison/prefix-comparison.md)、[最终生产实现数据](../../outputs/verification/canvas-chat-review-20261023/read-comparison/final-results.json)。README 的“新版”是历史 greedy，不应再作为最终版指标。

#### 完整 Chat：时间、调用和已记录 tokens

| 指标 | Quote 旧→新 | EP13 旧→新 |
|---|---:|---:|
| 完整请求时长 | 63.888→31.857 s（−50.1%） | 201.487→177.025 s（−12.1%） |
| 工具调用 | 10→6 | 42→36 |
| 工具失败 | 2→1 | 14→7 |
| INVALID_ARGUMENTS | 2→1 | 3→2 |
| Codex SDK run | 2→1 | 2→2 |
| 全 provider 已记录 tokens | 357,206→194,421（−45.6%） | 1,646,827→1,938,583（+17.7%） |
| 工具返回 UTF-8 bytes | 59,451→27,508 | 282,154→309,723 |
| 最终播放卡 | 1→1 | 4→5 |

两组相加：时长 **265.375→208.882 s（−21.3%）**，已记录 tokens **2,004,033→2,133,004（+6.4%）**，调用 **52→42**，失败 **16→8**。两个异质问题的合计不能代替产品流量加权平均。

Quote 旧版连续两次没修正参数，随后委派子 Query；新版一次纠错后直接完成。EP13 新版子 Query input 增加 311,967，Supervisor input 减少 17,157；输入增长集中在子 Agent。工具调用更少，但返回字节增加 9.8%。现有记录没有每次采样 prompt/压缩前后量，无法精确归因于历史重读、内容选择或内部轮次；也不能把 SDK run 当成底层推理调用数。

#### 完整 Chat：最终输出质量

**Quote：**旧/新集数、人物、原文、译文及目标 54.6–56.0 秒与已解析证据一致，两版都提交 50–60 秒完整问答。新版却把开场第一分钟写成“第13集后段”，属于最终可见正文中的事实瑕疵。核心任务完成，不能判质量全面提升。[新版正文](../../outputs/verification/chat-latency-20261023/quote-current-response.json)

**EP13：**旧版正文 16 条、新版 8 条发言均与已存 transcript 和祥子 speaker 对应，并完整落在各自卡片范围内；最终没有第 3 集污染。旧版 4 卡共 109.9 秒，新版 5 卡共 26.9 秒。新版有 6 个 hits，是因为第一卡 59.3–65.3 秒跨两个 Chunk，**不是 6 张卡**。

两版共同列出 3 条，旧版独有 13 条，新版独有 5 条。新版补到开场等旧版未列场景，旧版连续对话更充分。新版系统响应附带的已召回证据含 27 条祥子发言，正文只选了 8 条；“当前索引中确认有8句”的表述容易造成完整性过强的印象。

**召回不等于模型已读。**Chat 检索卡当前约 1920 字符，详情约 2000 字符/页；完整结果先保存在宿主，再生成有界卡片。现有日志没有逐次 tool 全文，无法证明 27 条都进入了模型 context，不能确定遗漏发生在分页供给、续读选择还是最终提炼。[Chat 返回构造](../../agent/chat/video_search/tools/retrieval.py)

这里只核对回答与解析证据一致性，没有重听整集或人工全集标注；不计算全集 recall，不把 READY 或播放卡数量当作答案正确率。[旧 EP13](../../outputs/verification/chat-latency-20261023/ep13-old-response.json)、[新 EP13](../../outputs/verification/chat-latency-20261023/ep13-current-response.json)

#### token 费用估算与敏感性

##### 费率、统计和账单边界

2026-10-23 已检索并打开 [GPT-5.6 Luna 官方模型页](https://developers.openai.com/api/docs/models/gpt-5.6-luna)：公开文本价每百万 token，普通输入 **$0.20**、缓存读取 **$0.02**、输出 **$1.20**。以下用该型号标准短上下文价格做 **API 等价成本估算**，不是项目实际 Codex 账户账单。

设 `I` 为累计输入、`C` 为缓存读取、`O` 为输出，`B=I−C` 为未计为缓存读取的输入。输出已经包含 reasoning，不再额外收费一次。

| Codex token 分项 | Quote 旧→新 | EP13 旧→新 | 两组合计旧→新 |
|---|---:|---:|---:|
| B（含无法识别的 cache writes） | 66,886→32,649 | 88,884→115,022 | 155,770→147,671 |
| C | 286,592→159,744 | 1,543,040→1,811,712 | 1,829,632→1,971,456 |
| O | 2,990→1,344 | 13,940→10,821 | 16,930→12,165 |

input 包含 cached input，不能重复相加。仅对 Codex 分项计价；router、embedding 及失败 rewriter 不混用同一费率。本项目 [正式估价实现](../../server/service/model_pricing.py) 对 provider=codex 返回未知，本文没有修改它。

##### 基础情景：暂按 B 全部使用普通输入价

```text
cost_USD = (0.20 × B + 0.02 × C + 1.20 × O) / 1,000,000
delta = cost_new − cost_old
```

| Case | 旧估算 USD | 新估算 USD | 差额 USD | 差额比例 | 预算人民币差额* |
|---|---:|---:|---:|---:|---:|
| Quote | 0.02269704 | 0.01133748 | −0.01135956 | −50.0% | −¥0.0795 |
| EP13 | 0.06536560 | 0.07222384 | +0.00685824 | +10.5% | +¥0.0480 |
| 两组合计 | 0.08806264 | 0.08356132 | −0.00450132 | −5.1% | −¥0.0315 |

\* 人民币仅用预算假设 **1 USD = 7 CNY** 换算，不是实时汇率、结算汇率或含税报价。

对用户关心的 EP13“多消耗”案例：已记录总 tokens 增加约 29.2 万，并不应统一乘普通输入价。Codex 分项增减为：

```text
新增 B：   26,138 × $0.20 / 1M = +$0.00522760
新增 C：  268,672 × $0.02 / 1M = +$0.00537344
减少 O：   3,119 × $1.20 / 1M = −$0.00374280
净变化：                           +$0.00685824 / 次
```

按该样本和基础情景，**每次额外约 4.8 分人民币，少等待约 24.46 秒**；1000 次同类请求净增约 **$6.86 / ¥48.01**。这是线性预算情景，不是规模化实测，也没有证明输出质量等价。Quote 则同时节省时间和估算费用。

##### 缓存写入和长上下文敏感性

[官方缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching) 将普通输入、缓存读取、缓存写入分开计价；默认 30 分钟写入为普通输入价的 1.25 倍，12 小时写入为 2.5 倍，写入不是再叠加一次普通输入全价。令 `W30/W12` 为 B 内对应写入量，标准短上下文下：

```text
cost_USD = base_cost + (0.05 × W30 + 0.30 × W12) / 1,000,000
0 ≤ W30 + W12 ≤ B
```

现有 7 个 Codex SDK run 的 `cache_write_input_tokens` 均记录为 0，但 SDK schema 对缺省字段也填 0；没有保存字段显式存在性或原始通知，无法区分上游明确 0 与 SDK 默认 0，也没有 30m/12h 拆分。**基础情景不能被当作已证明没有缓存写入。**

| 短上下文、30m 写入假设 | Quote 差额 USD | EP13 差额 USD | 两组合计差额 USD |
|---|---:|---:|---:|
| 两臂 W=0 | −0.01135956 | +0.00685824 | −0.00450132 |
| 两臂 W=B | −0.01307141 | +0.00816514 | −0.00490627 |
| 两臂各自 W 可在 [0,B] 独立变化 | −0.01470386 至 −0.00972711 | +0.00241404 至 +0.01260934 | −0.01228982 至 +0.00288223 |

因此，在“标准短上下文、只可能有 30m 写入”的限定条件下，EP13 增量约 **1.7–8.8 分人民币/次**；两种相同写入假设约 **4.8–5.7 分/次**。这不是包含所有未知因素的账单上下界，合计实际费用也不能保证下降。

官方模型页还说明单次 prompt 超过 272K 时有输入/输出倍率。**累计 193 万输入不等于某次 prompt 超过阈值。**现有记录缺逐次请求长度，不能确定该倍率是否适用；服务档位、12h 写入、渠道/账户计费、其他 provider 费用均未纳入以上区间。

只考虑无写入溢价、固定费率时，EP13 差额的一般形式是：

```text
Δcost = (26138 × p_input + 268672 × p_cached − 3119 × p_output) / 1M
```

后续换模型或费率时重算，不把本次 Luna 的“几分钱”推广到更贵的模型。

#### 邻接修复与最终部署烟测

SQL 排序原本持续报 MySQL 1038。修掉 task 大 JSON 投影后，进一步去掉 scope 不使用的 Chunk 正文/detail，真实检索结果数量保持一致：

| 路径 | 纯词法：task 投影后→最终 scope 投影 | 混合检索：同口径 |
|---|---:|---:|
| 独立 scope | 5.5673→0.0132 s | 4.6269→0.0137 s |
| 完整 Chunk search | 5.2229→0.3895 s | 5.9078→1.1785 s |
| 完整 Event search | 9.7103→4.9252 s | 9.9369→5.3928 s |

这是后端读取/解码/复制收益，search 已包含 scope，不相加。MySQL sort buffer 保持 256 KiB。Event coverage 仍做全发布内容校验，单次约 5 秒，未在此轮扩大修改。

A/B 后补齐 Supervisor 的工具额度 flag：`limits_enabled=false` 时不再向子 Query 透传默认 24。关闭限制时两个各 25 次检索全部完成；开启限制仍正确共享预算。SQL 相关回归 **21 passed**；Supervisor/Query 回归 **214 passed**。

最终 API 从 main 当前工作区重启后，经实际 `127.0.0.1:9621` 请求，Quote **22.641 秒**完成，返回一张第 13 集播放卡。仍发生一次参数校验错误但成功纠正；片段为 54.6–56.0 秒，只有目标一句，比 A/B 的 50–60 秒上下文更短。它验证最终部署可运行，不能把 31.857→22.641 的差异算作另一项修复的确定收益。

#### 架构复杂度、维护成本和认知成本

| 维度 | 修改前 | 修改后 | 收益与保留的代价 |
|---|---|---|---|
| 架构复杂度 | Canvas 读取叠加字符、项数、token 和字节限制，批量还需区分完整与部分项 | 统一按实际字节预算返回完整前缀，以剩余引用恢复；沿用已有 Canvas 和原子写入机制 | 减少限制组合和部分项状态；单项超大时仍需 section/字符恢复，没有消除所有分支 |
| 维护成本 | 同名 start/end 承担不同单位，反馈无法充分解释字段错误 | 参数语义明确，反馈保留具体原因；新增 remove_event 行为及删除、冲突、重放合同 | 定位错误和理解参数更直接；工具 schema、guide、测试需同步维护，旧构造参数仍承担兼容成本 |
| 认知成本 | Agent 需猜正文名称、范围单位及续读位置 | 默认 content，明确 catalog_cursor、字符范围和 remaining_references | 常见读取路径更直接；Agent 仍需判断剩余引用及超大项恢复，不能把单次返回当作全部读取完成 |
| 业务闭环与成本取舍 | 工具往返较多，Chat 参数纠错效率低，后端存在排序和额度故障 | 离线读取往返减少；两类 Chat 样本耗时下降，但 EP13 token/估算费用上升，答案覆盖未证明改善 | 支持保留当前修正并继续验证，尚不足以认定时间、费用、质量全面正收益 |

上述复杂度、维护和认知成本属于基于实际实现的定性评估，未测维护工时、人类理解时长或模型学习成本；不能将其记为量化收益。

**原问题 B06/C07/C18 的历史证据：**

已观察到明确的参数误用和额外回合；B06/C07 原材料未给完整错误清单总数、修复前后 token/费用或全链节省值。当前合同与对照数据见本条前文的专项对照；保留“较少错误不必然更少 token”的评估约束。

<a id="devlog-history-01"></a>

## 2026-10-22：画外音提示修正与 EP04 两个已知身份目标

### 1. 问题与现状

原问题编号 **B12**；[问题清单](problems.md)排序日期停在10/19，但正文已更新至10/22。

v24 中画面不见爽世、可见人物的嘴部状态等视觉线索，被用于推翻正确声音证据。对白人物可能位于画外，错误会直接污染人物检索。修正该判断时还需防止重复对白、null speaker 和解释性描述变成新的质量损失。

### 2. 方案候选

选择最小 Refine 提示：画面缺席或嘴部不可见本身不能构成身份反证，允许画外音；并未改成永远相信声纹 top1，也没有新增全局记忆或身份只读体系。这降低了单条规则的认知歧义，额外架构改动有限，但仍依赖模型遵循且需回归其他身份场景。

B12仍未全面闭环：B0编码前单人准入、Refine归因只读/受限合并和全局memory仍是候选，不能锁住初始错误姓名来伪造正确率。严格大于500ms（整数501ms起）的预填规则的独立开发副本仅离线通过，未部署或完成新全集验证；详见时长/gap实验。

### 3. 方案验证

v25 完整重跑与 v24 比较，两个目标均最终正确；实际声纹预填输入没有完整捕获，证明链来自 B0 结果、规则、后续决策与最终事件，不能声称逐字留证了该阶段输入。ASR、Understanding 和参考池重新生成，单次 2/2 不能确定纯提示因果或全片准确率。证据：[v25 报告](../../outputs/verification/ep04-offscreen-rerun-20261022/report.md)。

B12原材料还记录声学工具接入1319 passed；这是代码回归，完整音频工具数值复现、12范围/231窗及失败重跑分别在对应条目，不重复计算为另一套身份评测。参考设计：[声纹TRD](../design/02-agent-harness/voiceprint-trd.md)。

### 4. 指标对比

| 指标 | v24 | v25 |
| --- | ---: | ---: |
| 完成耗时 | 38 分 13 秒 | 37 分 11 秒 |
| 已知身份目标 | 1/2 | 2/2 |
| 最终对白 / null | 359 / 58（16.16%） | 368 / 82（22.28%） |

v25 的 247 条 ACCEPT 中，243 条明确保留 top1，2 条为 null，另 2 条受重复事件影响而不计为明确保留。出现 12 组精确重复对白，每组至少一条 null；Understanding 的 356 条原输出没有这些精确重复，Refine 窗口 18 已提交重复，但责任属于 Agent 生成还是 Canvas 应用仍未被区分。目标身份改善同时伴随覆盖和去重问题，不能宣布整体质量提升。复杂度主要增加在提示例外与回归样本维护，未测维护工时。

<a id="devlog-history-02"></a>

## 2026-10-22：Whisper 替代 ASR 与整集先转写

### 1. 问题与现状

云端 ASR 延迟、费用及匿名 speaker 质量促使尝试 Whisper。替换后转写可以更快，但 Whisper 输出不提供原匿名 speaker，可能把身份工作推给后续模型；整集转写还面临上下文重复、幻觉及句子跨 chunk 分发问题。

### 2. 方案候选

已比较分 chunk Whisper 替代、整集 Turbo/large-v3、关闭历史上下文 max_context=0、指定语言与 beam 等组合。整集先转写减少重复调用，但需要宿主把句子分配回窗口，处理边界和重复；large 模型提高本地计算成本。改变语言和 beam 的组合不能单独归因到某个参数。

### 3. 方案验证

分 chunk 对照有完整流水线数据；整集先转写只做到 ASR→Understanding，没有 Refine、index 或数据库发布。整集实验存在源码漂移，报告提供受控模块指纹和帧检查，不能称整个仓库完全冻结。没有人工 WER/CER 集，少量字幕检查不足以建立通用转写或身份准确率。

证据：[分 chunk 对照](../../outputs/verification/ep04-whisper-20261022/comparison-summary.json)、[整集参数与 Understanding 对照](../../outputs/verification/ep04-whisper-full-first-20261022/comparison.json)、[整集验证边界](../../outputs/verification/ep04-whisper-full-first-20261022/report-validation.json)。

### 4. 指标对比

| 指标 | v25 云端 ASR 基线 | 分 chunk Whisper |
| --- | ---: | ---: |
| 24 个 ASR 请求耗时总和 | 867.089 秒 | 47.485 秒 |
| 单请求中位数 | 36.284 秒 | 1.595 秒 |
| 完整流水线 wall time | 2,231.272 秒 | 2,216.057 秒 |
| ASR cues / null speaker | 365 / 29 | 373 / 373 |
| 最终对白 / null speaker | 368 / 82（22.28%） | 312 / 227（72.76%） |

请求耗时总和不是并行墙钟：ASR 明显更快，完整流水线耗时几乎没有下降，最终有名人物覆盖显著减少。重复对白组 12→0 不能单独证明质量更好。整集 Turbo 默认 33.161 秒但只有 117 cues，包含重复错句；关闭历史后 35.765 秒、404 cues；large-v3 关闭历史 81.586 秒、333 cues，默认却用 167.826 秒输出 690 cues，其中 664 条为同一句重复。整集 Turbo 的 Understanding wall time 954.978 秒含 388.275 秒冷启动准备；large-v3 对应模型阶段 541.580 秒、313 条对白中 192 条 null。额外本地模型、上下文参数与跨窗口分发扩大维护范围，净费用和最终业务质量均未验证。

<a id="devlog-history-03"></a>

## 2026-10-22：Event V3 迁移、EP13 索引与演示闭环边界

### 1. 问题与现状

演示需要数据库、运行代码、索引合同和可选剧集状态一致；部分索引就绪时，同时选择未就绪实验剧集会让整个 Chat 返回 SERIES_NOT_READY。用户目标包含真实检索与回答，不能只凭健康检查或 Event 数量宣称完成。

### 2. 方案候选

在主 main 来源服务上做 metadata 迁移和 EP13 重建，先直接验证 lexical/hybrid 工具，再只选择 READY 的主 mygo 发真实 Chat。其余集数在后台排队。这减少演示路径的不确定性，但迁移、索引、series readiness 与聊天状态都需分别留证，维护面比单个工具 smoke 更大。

### 3. 方案验证

14:44:44 截止前，数据库与主服务恢复、EP13 24/24 sources 和直接检索验证已完成；14:48:59 快照进一步确认 EP12 完成，其余部分仍在处理。主 mygo 在 Chat 发起时为 READY 3/13，实验 EP04 为 INDEXING，二者一起选择得到 409；改为只选主 mygo 后真实 Chat 开始执行。完整回答反复检索，最后主动取消，终态 FAILED/AGENT_CANCELLED，不能宣称 Chat 端到端通过。

证据：[演示报告与最终快照](../../outputs/verification/event-v3-demo-20261022/report.md)、[EP13 索引数量](../../outputs/verification/event-v3-demo-20261022/ep13-es-count.json)、[真实 Chat 工具统计](../../outputs/verification/event-v3-demo-20261022/chat-smoke-summary.json)。

### 4. 指标对比

- EP13：24/24 sources、544 Events；当时工具覆盖的 EP01/03/13 共 72 sources。14:48:59 EP01/03/12/13 COMPLETE，EP11 17/24、EP10 14/24、EP09 1/24；EP04–08 和实验任务排队，EP02 INDEXING_FAILED。另提交的 10 个任务/240 chunks 不等于全部完成。
- lexical smoke：retrieve 1.970 秒、3 个 EP13 hits；search_video_events 3.091 秒、3 个 EP13 hits；整轮 9.035 秒，无模型调用。
- hybrid smoke：retrieve 2.700 秒、3 个 EP03 hits；search_video_events 3.262 秒、3 个 EP13 hits；整轮 10.537 秒、2 次 query 模型调用。不能说两工具都命中 EP13。
- 真实 Chat：search_video_events 10 次成功；retrieve 2 成功/1 参数失败；当时的 Canvas 7 成功/9 参数或引用错误；submit_video_item 1 次成功。工具可用与部分卡片提交不等于完成回答，演示资源释放时主动取消。
- 主服务记录 main source_root 与源码指纹，证明当次来源；本次未找到独立浅层 main/worktree 懒人启动完整报告，启动策略实现由相应 problem/设计记录补充。迁移与分阶段验收增加运维步骤，但避免把局部 READY、命中数量和最终回答质量混为同一指标；没有总体费用或自然问答成功率数据。

<a id="devlog-history-04"></a>

## 2026-10-22：3fps 采帧、旧上限兼容与 main 运行来源

### 1. 问题与现状

视觉解析希望把默认采帧率提升到 3fps；旧图片数上限和固定每图 2,048 token 的预检可能使配置显示 3fps，实际仍少采图或提前拒绝。用户体验风险是字幕、动作和人物细节遗漏，而不是配置字段本身错误。

### 2. 方案候选

实际张数按 ceil(duration×fps)，解除旧 cap 对新视觉解析的执行影响，保留旧字段/API 兼容与文本预算；旧任务输入保持冻结，索引重建不等同重新视觉解析。更多图片增加供应商输入、费用和延迟风险，需要真实业务评测，本轮先验证从采帧到请求的数量与顺序。

### 3. 方案验证

本地真实 FFmpeg 加假 Codex session 验证图片打包；没有发出 198 图的真实供应商请求，也没有全片质量实验。MySQL/ES 使用原有数据恢复，./start.sh 启动 main 来源 API/Web，health 与 consumer 检查通过；没有数据库迁移、重新索引或提交新解析任务。该运行来源是当次部署证据，不是“恒定 main”全部实现与生命周期测试的独立报告。

证据：[3fps 实现与运行验证](../../outputs/verification/parse-3fps-20261022/report.md)。

### 4. 指标对比

60 秒 180 帧、66 秒 198 帧，间隔为 333/334ms；真实 3 秒 FFmpeg 产出 9 帧，即使旧 cap=3 也不截断。假 session 收到有序且字节一致的 180/198 图。后端 130+163=293 测试、前端 25 测试与 typecheck 通过。运行时上传配置 fps=3、60 秒窗口、3 秒 overlap，ASR/embedding 仍为 1fps。减少旧 cap 与新 fps 双重控制的认知冲突，但保留兼容字段需要维护“不再执行”的边界；真实模型容量、时延、费用与内容改善均未验证，不将张数增加直接写作质量提升。

<a id="devlog-history-05"></a>

## 2026-10-21：连续 turn 提示、混合 consumer 与纯源码复跑

### 1. 问题与现状

Understanding 可能把多个 ASR cue 合成连续对白，却没有检查合并后的完整音频；Refine 仍可能依据可见人物推翻正确预填。连续多次重跑还暴露旧 consumer 未退出、新旧代码同时消费任务的问题，导致“完成了新版本任务”不等于“全程运行新代码”。

### 2. 方案候选

候选是在提示中要求连续 turn 合并前检查完整音频，并在集成运行时记录源码、输入与 consumer 来源。v23 之后采用纯已知来源 v24 复跑来排除混源；这增加了快照、源文件指纹和输入留证成本，但直接降低实验结论的歧义。要求身份只读或正式单人 admission 当时仍是候选，未在这些提示改动中实施。

### 3. 方案验证

v21 最终因 ASR DNS/timeout 失败，不能评价提示效果。v22 全流程完成；v23 虽 COMPLETE，但旧 PID 在 HTTP 端口释放后仍消费任务，只有 11/24 个新源码 Understanding 输入被留存，整轮不得作为纯新提示对照。v24 留存 24 个实际输入，并核对 345 个源文件指纹无变化；仍重新生成 ASR/参考池，并非冻结输入的一因素实验。

证据：[v21 变更及失败](../../outputs/verification/ep04-continuous-turn-rerun-20261021/change-report.md)、[v22](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v22/report.md)、[v23 混源](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/report.md)、[v24](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/report.md)。

### 4. 指标对比

| 指标 | v22 | v24 |
| --- | ---: | ---: |
| 完成耗时 | 40 分 44 秒 | 38 分 13 秒 |
| 已知身份目标 | 1/2 | 1/2 |
| 最终对白 / null | 355 / 60（16.9%） | 359 / 58（16.16%） |
| ACCEPT | 237 | 249 |
| 保留 top1 | 235 | 246 |

v21 有 69 次 DNS 与 1 次 timeout，只有 1/24 Understanding 完成。v22 的 31 次 Refine attempts 包括 22 次提交与 9 次版本冲突；不能全算模型失败。v24 的爽世已正确 ACCEPT 和预填，却被 Refine 以画面理由改成爱音，且 Understanding 没有普遍完成整段合并检查。提示约束没有解决两个关键业务边界。v23 的混源发现证明运行来源审计必要，但本次没有独立量化其日常维护成本。

<a id="devlog-history-06"></a>

## 2026-10-21：短音频裁剪、VAD 与“同一人连续说话”证据

### 1. 问题与现状

连续对白合并或身份回填要求先判断音频是否含多个人。音频向量变化可能来自静音、音乐、短窗口或声音变化，不能直接当换人；没有触发也不能当作同一个人的认证。误用会把多人的对白合成一个身份。

### 2. 方案候选

候选包括尾部裁剪、固定 CAM++ 窗口的声纹变化诊断、VAD 掩码，以及独立匿名 diarization 的研究方案。裁剪和 VAD 可复用现有音频与向量流程；新增 diarization 会引入另一模型及时间轴对齐，报告当时只是研究建议，没有项目 DER 或生产收益。工具输出保持 advisory，比输出无依据的“单人认证”更符合证据能力。

### 3. 方案验证

裁剪实验只改变采样片段；VAD 在 12 段既有 MP4、231 个固定 CAM++ 窗口上比较。后续从真实 MP4 经过 prepare_chunk 再调用工具，离线 ASR 为 stub，无模型、数据库或 worker。两个人工修正标签改变了判断：#11 实际全为爱音，是误报；#12 为立希→爱音，是真换人。

证据：[裁剪实验](../../outputs/verification/speaker-tail-crop-20261019/report.md)、[diarization 研究](../../outputs/verification/speaker-diarization-research-20261019/report.md)、[VAD 掩码](../../outputs/verification/ep04-speaker-change-20261020/vad-mask-report.md)、[真实音频工具路径](../../outputs/verification/ep04-understanding-tools-20261021/report.md)。

### 4. 指标对比

- 裁剪 12 组、60 个变体，57 个可编码、3 个短于 400ms；最后成功批次耗时 5.337 秒，不含此前失败。已知两个目标原片段的 top1 本来就正确；裁剪未形成稳定改善，同人对照的向量也会移动。
- 75% VAD 阈值消除了 #11 误报，但混合片段仅 3/7 触发，存在漏报代价，不能写成只去掉静音影响。
- 工具路径的 231 个向量与冻结输入最大差为 0；12 段总耗时 13.828 秒，混合片段 6/7 触发、对照 1/5 触发。场景重复且部分对照无逐段真值，不能推广为独立准确率。
- 这一轮验证了算法接线与数值复现；没有验证自然 Agent 如何使用 advisory，也没有测最终身份正确率。额外阈值和证据解释会增加维护、认知成本，减少误用仍依赖工具语义和业务评测。

<a id="devlog-history-07"></a>

## 2026-10-21：独立整集摘要 Agent、证据读取与标题定位

### 1. 问题与现状

**TRD中的宿主实现与离线交付：**

来源：[video-summarize-trd.md](../design/02-agent-harness/video-summarize-trd.md)，无明确文档日期；迁移文件名 `20261021_video_metadata.sql` 不作为实施日期。目标在全部 Refine 提交后生成官方标题、事实简介、真实集数及封面；源码和离线测试已交付，TRD 明确未迁移、未重启、未发真实摘要、未跑真实 MySQL 并发，归档不把这些状态改成已部署。

Summarize 独立于 Chat，与 Index 并行、不等待 Prefix，也不阻塞父 Parse COMPLETE。历史视频不会自动全量补跑。

**独立真实模型实验：**

摘要 Agent 必须读取全部相关 chunk 后提交；固定工具次数上限可能在尚有证据未读时终止。即使正文与引用通过，标题采帧错过片头仍会漏掉集数和标题，影响用户识别摘要所属集。

### 2. 方案候选

**TRD中的宿主实现与离线交付：**

复用现有 task detail、Repository、lease 和独立 summary 槽，不增加任务表；按完整 Chunk 内容/版本构造只读 Canvas，提交前核对完整阅读。原片取帧由宿主绑定身份，封面只接受实际返回过的帧回执；标题/集数需要标题卡或正式 ON_SCREEN_TEXT/直接画面证据，不能从文件名、排序或预告推断，缺证据保留原值。

新增 Video 三个可空元信息字段；租约、source fingerprint、冻结配置、READ COMMITTED、锁顺序和 savepoint 保护并行改号与发布。集数冲突保留原顺序，保存其他有效元信息及原因。复用持久化降低表与框架数量，但 summary 状态、版本栅栏、Upload/Prefix/Library 锁和取消收尾明显增加跨模块维护成本。默认并发 1、失败重试 2、总超时 600 秒；不加模型轮次/工具/看帧次数帽，保留大小、时间与来源校验。

**独立真实模型实验：**

先在提交失败中给出缺失 reference 提示，再取消工具/模型/帧数计数上限，保留 deadline、取消和读取证明，并增加通用片头标题定位指导。后一方案允许完成真实工作，但也可能重复采帧和增加 token；标题规则不硬编码 EP04 名称，降低特例维护。

### 3. 方案验证

**TRD中的宿主实现与离线交付：**

专项 50 passed，覆盖真实 Agent→LangChain tools→Host→SQL 组装，Gateway 为替身。扩大回归 1312 passed/1 skipped/3 failed；三项是并行 OCR 默认配置变化影响，独立临时进程还原 OCR 默认后通过，但原常规回归的 3 failed 没有被抹去。Web 36 passed、typecheck 通过，Upload 105 passed。未验证真实模型标题/集数/简介准确性，SQLite 不证明 MySQL 锁行为。

**独立真实模型实验：**

同一 EP04 v20、24 chunks、153,569 字符输入，使用真实模型；未经过数据库发布与 worker 生命周期。第二次成功时没有触发新增缺失提示，不能把加速归因于该提示。第三次是在提示和计数限制共同变化后运行，并非单因素实验。

证据：[首两轮](../../outputs/verification/video-summarize-ep04-20261021/report.md)、[标题与限制后续](../../outputs/verification/video-summarize-ep04-20261021/revision-title-guidance/report.md)。

两种证据范围不同：TRD的“未发真实摘要”仅描述其交付记录，下述独立模型实验已执行；它没有证明SQL发布、后台Worker或真实MySQL并发闭环，不能互相代替。

### 4. 指标对比

**TRD中的宿主实现与离线交付：**

实际触及 52 文件（34 生产/16 测试/2 文档迁移），14 新增/38 修改；8 新增生产文件 1052 行。既有路径 diff +916/-100 含并行工作，不等于本功能净增量。没有摘要时延、token/费用、字段准确率、自动改号冲突率或用户收益数据；离线合同通过不能宣称真实质量和维护净收益为正。

**独立真实模型实验：**

| 指标 | 第一次 | 第二次 | 标题/限制调整后 |
| --- | ---: | ---: | ---: |
| 耗时 | 168.220 秒 | 116.178 秒 | 134.587 秒 |
| 工具数 | 37 | 29 | 41 |
| 结果 | 超过 36 次上限，2 chunk 未读 | 24 chunk 已读、正文可提交，标题/集数 null | 标题“一生だよ!?”、集数 4 正确 |
| inspect 调用 / 图片 | — | 5 / 11 | 11 / 35 |

首两次总 token 3,236,815→1,860,890，包含缓存，不能按全部付费输入计算费用。第二次 6 个引用的覆盖字节验证正确；第三次从 176,875ms 图像取得标题，有 2 次可恢复错误和重复采帧。用户可见元数据得到修复，同时调用/图片比第二次增加；一集成功不足以证明通用质量。减少计数规则降低一类维护成本，但代价转为 deadline 内资源消耗和证据读取证明维护，净费用未测。

<a id="devlog-history-08"></a>

## 2026-10-21：对白检索中权重、ANN 与文档粒度的不同失败

### 1. 问题与现状

同一“真是高高在上呢”目标可能已经被某个通道找到，却因融合截断没进入最终工具结果；也可能精确相似度排名靠前而 ANN 漏召回；还可能原日语表达与查询根本不匹配。这三类问题分别影响曝光、召回和查询表达，不能统一归为“embedding 坏了”。

**原问题 C03/C14 的假设与复测背景：**

原编号 **C03、C14**。C03 的早期多轮问题把“初次见面”定位到后来组乐队阶段，另一次无依据限定前三集。C14 对“真是高高在上呢”的两次问法表现不稳：错误人物/场景假设持续影响搜索；正确 Chunk 未进入工具返回候选。Supervisor 最后承认无法确认，说话人错误未成为最终事实；根因不能全归为 Evaluator 删掉正确答案。

来源：[C03/C14](problems.md)、[RAG 组合报告](../../outputs/verification/mygo-quote-rag-20261017-003118/REPORT.md)、[Embedding 诊断](../../outputs/verification/mygo-quote-rag-20261017-003118/embedding-diagnosis/REPORT.md)。

### 2. 方案候选

先调融合权重，再分别检查词法匹配、ANN 与精确 cosine、chunk/短对白粒度、双语与中文文本表示。扩大通道深度会增加候选处理；拆细 Event 或独立 text variant 增加索引与去重维护；只保留中文可能损失日语查询能力。因此没有把单个中文查询的向量分数提升当成删除日语或重建全库的依据。

**原问题 C03/C14 的假设与复测背景：**

Query 将用户给出的条件与可撤回搜索假设分开；无直接证据时解除人物/场景约束补查。Evaluator 审核是否反复依赖同一前提，Supervisor 保持最终事实责任。检索层同时评估通道深度、RRF 权重、最终 topK 和台词/事件粒度，而非只改提示或硬加词项门槛。复用现有角色、Skill 与 Trace，不增加假设表或 Agent 层；更细索引会增加编码、存储、版本关联和查询融合维护成本。

### 3. 方案验证

10/17 的 89 次真实工具查询没有 Agent 模型调用；只围绕已知正例做诊断。10/21 固定 336 sources、4,978 dialogue events，分别运行通道与精确排名检查；文档消融只改少数样本，未全库重索引。中间召回、宿主内候选、最终工具曝光必须分开。

证据：[权重与工具结果](../../outputs/verification/mygo-quote-rag-20261017-003118/REPORT.md)、[向量一致性与粒度](../../outputs/verification/mygo-quote-rag-20261017-003118/embedding-diagnosis/REPORT.md)、[ANN/top20](../../outputs/verification/gaogao-channel-top20-20261021/REPORT.md)、[日语词法](../../outputs/verification/gaogao-japanese-lexical-20261021/REPORT.md)、[语义消融](../../outputs/verification/gaogao-semantic-ablation-20261021/REPORT.md)、[纯对白](../../outputs/verification/gaogao-plain-dialogue-20261021/REPORT.md)、[中文消融](../../outputs/verification/gaogao-plain-dialogue-20261021/chinese-only/REPORT.md)。

**原问题 C03/C14 的假设与复测背景：**

正式 retrieve→Service 的 71 组主矩阵、18 组权重/topK 对照均无执行错误，使用真实 SQL/ES/Embedding，未重跑角色模型。历史失败首 query 在10/17当时的Chunk索引复测中，目标词法第1、语义第17、融合第13，超出旧top8；这是该日复测，不冒充历史失败请求的原始通道留档，也不与10/21 Event/ANN排名混用。强词法目标在 `.1/.9` RRF 下仍可能被挤出 top10。此前一次成功的日语查询还依赖相邻台词公共字串，不证明稳定语义理解。

目标 ES 语义文本确有中文译文与原句，但将 21 Event、1775 字的一分钟内容编码为单个 1024 维向量。8 次真实 Embedding 隔离对照确认向量可重现；目标在全 312 向量精确排序也低，不能全部归因 ANN。背景确实含关系/行为信息，是否触发最初错误联想仍无法从 Trace 区分。

### 4. 指标对比

- “真是高高在上呢 说话 居高临下”在某组权重下 lex rank 1、semantic rank 17、融合 rank 13，最终 K=8 无目标；通道命中不等于 Agent 看到。
- 旧 chunk 的目标约 1,775 字，query 对短句 cosine 0.811762、对 chunk 0.357940；全量精确排名 123。重新编码与缓存/ES 向量差为 0，支持粒度问题而非写入损坏。
- 新 Event 集“高高在上”目标精确排名 10，ANN top20 仍漏；词法查询“高高/上から目線/偉そう/見下す”目标本身不匹配，正确日语正例才 rank 1。
- 固定短/完整两种中文 query，description+标签双语分数 0.440068/0.559714；纯双语 0.530703/0.750310；纯中文 0.633940/0.807492。分数是少量文档局部相似度，没有全库召回率或自然 Chat 成功率。
- 诊断分清了需改表示、ANN 参数还是融合截断，减少盲目调权重的认知成本；正式方案的索引体积、时延、费用与多语言质量代价尚未量化。

**原问题 C03/C14 的假设与复测背景：**

近义短句单独编码余弦 0.811762；同 query 对目标 110 字单事件为 0.443881、对整分钟为 0.357940。全库精确排序下“高高在上”目标第 208、“真是高高在上呢”第 123、中文原句第 89；单目标证据支持粒度不匹配，不是通用准确率。提高词法权重能救回某些样本，但全局最优权重、困难负例误召回、细粒度方案净成本收益未知。

<a id="devlog-history-09"></a>

## 2026-10-21：声纹预填时长门槛、gap 与最终身份保留

### 1. 问题与现状

短对白即使声纹 top1 正确，也可能被时长或 gap 门槛拦在 ACCEPT 外；反之预填正确也可能被 Refine 改错。产品目标是正确人物及有效对白覆盖，不能仅优化 ACCEPT 数。

### 2. 方案候选

先比较 1.5 秒→1 秒的预填门槛，再把 gap 改成描述性证据；10/22 的后续窄改将可预填时长改为严格大于 500ms（整数条件 501ms）。400ms 编码下限和 1,000ms 标签可疑诊断门槛具有不同职责，没有一起取消。减少硬门槛可增加覆盖，但更依赖 Refine 判断和参考池质量；保持参数职责清楚可减少错误调参。

### 3. 方案验证

v19 与 v18 同时存在模型切换，因此不是纯策略对照；冻结 v19 输入用于验证 1 秒门槛的确定性影响，随后 v20 实跑重新生成了 ASR、参考池和 PCM，不能把全部差异归因于阈值。gap 改动通过冻结回放验证旧显式参数结果不变；501ms 后续仅回放已有 v25 样本，没有重跑模型或 Refine。

证据：[v19](../../outputs/verification/ep04-full-rerun-20261021/report.md)、[v20](../../outputs/verification/ep04-prefill-1s-rerun-20261021/report.md)、[gap 调整](../../outputs/verification/ep04-gap-advisory-20261021/change-report.md)、[501ms 策略](../../outputs/verification/ep04-duration-policy-20261022/report.md)、[501ms 回放](../../outputs/verification/ep04-duration-policy-20261022/replay-summary.md)。

### 4. 指标对比

| 指标 | v19 | v20 |
| --- | ---: | ---: |
| 已知身份目标 | 1/2 | 1/2 |
| 最终对白数 | 338 | 378 |
| null speaker | 52（15.4%） | 91（24.1%） |
| 任务状态 | COMPLETE | COMPLETE（约 50 分 24 秒） |

冻结 v19 的 ACCEPT 从 118 增至 155；v20 实际 127 条 ACCEPT 都保留 top1，其中 33 条小于 1.5 秒，但没有对应全量身份真值。爽世目标仍未修正；增加 ACCEPT 没有自动带来本例业务收益。v20 外层隔离检查退出码为 1，因无关视频被外部修改，报告未把该修改归因于本次任务。gap 改动 894 个测试通过、旧参数回放 0 差异；501ms 改动 311 个测试通过，900ms 立希样本在初始 Canvas 从 NO_VOICE/null→ACCEPT/立希，后续最终输出未验证。参数减少了部分硬分类，但阈值治理与阶段归因成本仍在，未量化。

B12另记取消gap门槛的冻结输入回放：ACCEPT/实际预填127→248，新增121/121；未调用生成式模型，不能等同最终人物正确。此结果与v19门槛118→155属于不同对照，不合并为一条累计增益。

<a id="devlog-history-10"></a>

## 2026-10-21：OCR 注入的字幕收益与时间定位误差

### 1. 问题与现状

只依赖视觉理解会遗漏画面中的中文字幕。OCR 能提供文字证据，但模型如果把文字与错误帧时间关联，会让字幕文本正确、引用时间错误；OCR 观察区间也不是对白真实时长。

### 2. 方案候选

将旧时间数组提示改为每张图内联时间戳，并把 OCR 输出提供给 Understanding。保留独立 OCR 可增加字幕证据，但新增一条模型链路和证据合并职责。让模型返回 image ID、由宿主绑定时间属于后续候选，本报告没有实施，不能作为既成收益。

### 3. 方案验证

smoke 在三个 60 秒 chunk 使用相同 96 张 JPEG（每 chunk 32 张、最长边 720），不创建数据库任务。目标字幕成功提取，但 seq13 两行被分配到下一张空白帧。另一个全流程批次只确认 EP01 COMPLETE；EP02 INDEXING_FAILED，其余未提交。

证据：[OCR smoke](../../outputs/verification/mygo-luna6-ocr-20261021/smoke/summary.md)、[批次状态](../../outputs/verification/mygo-luna6-ocr-20261021/summary.md)。

### 4. 指标对比

改写后的三个 chunk 分别输出 29/35/39 行 OCR，其中中文 13/17/15 行；耗时 17.370/14.413/19.456 秒。seq13 Understanding 从首轮 55.435 秒、15 条对白/9 条中文，到次轮 71.446 秒、19 条对白/15 条中文；目标双语内容入结果，但单次并非严格质量或性能保证。EP01 的 24/24 OCR 输出非空，共 803 行、354 行中文，最终 395 events、345 dialogues、78 null speaker。OCR 的 1ms 观察区间不能当发言时长；时间错位约一采样间隔的风险仍在。字幕覆盖局部改善伴随额外调用与时序校验成本，整库准确率、净 token 和费用未验证。

<a id="devlog-history-11"></a>

## 2026-10-21：Event 多向量索引完成度与正文翻译覆盖

### 1. 问题与现状

Event 检索从大 chunk 改为细粒度事件，需要同时检查向量写入与解析正文。每个 Event 的 description、不同语言 text 分别编码，可以避免多个语义共用一个向量，但索引完整不代表原始字幕已被识别或翻译完整。

### 2. 方案候选

本轮结合 Luna6 解析、B0 预填与 Event 多向量写入；description 和各 text variant 分别生成 1,024 维向量，通过输入 hash 复用缓存。代价是每个事件有更多向量与版本匹配状态；description 如果混入声纹分数或判断理由，仍会污染语义，不能靠存储多向量自行修复。

### 3. 方案验证

报告快照仅 EP01、EP02 COMPLETE，EP03 PARSING，其余尚未提交；不是 13 集全量验收。ES 与期望 Event、向量缓存输入核验通过，同时抽查可见中文字幕缺失。模型、预填和多向量同时改变，不能把正文变化归因于单项索引设计。

证据：[索引汇总](../../outputs/verification/mygo-luna6-multivector-20261021/summary.md)、[质量问题](../../outputs/verification/mygo-luna6-multivector-20261021/quality-findings.md)。

### 4. 指标对比

EP01/EP02 分别 425/372 events，共 797/797 期望事件匹配，缺失为 0。EP01 旧版 566 events、393 dialogues、261 条中文（66.41%）；新版 425 events、368 dialogues、83 条中文（22.55%），3 个可见中文字幕样本没有最终译文，尚不能区分采帧遗漏与模型遗漏。B0 准备 36.81 秒，368 行中 358 可编码、105 ACCEPT、104 保留 top1，仍非身份准确率。多向量解决写入与表示能力，未解决翻译覆盖；其索引体积、查询费用和人工维护成本没有在本轮量化。

<a id="devlog-history-12"></a>

## 2026-10-20—10-21：Chat 意图路由、改写实验与前置 hook 接入

### 1. 问题与现状

同一上下文里“找台词”和“评价助手回答”容易被误判，需要三分类 respond／retrieve／mixed；分类器与 query rewrite 又是不同任务。Jev 只输出类别与分数，不能替代改写器。10/20 的早期离线路由/改写候选实验独立运行，当时未接入 Chat Supervisor，也没有执行 state 内的业务工具。[完整实验与脚本入口](../../evals/benchmark/intent_routing/README.md)

**同主题的接入验证：**

把路由判断、自然回复和检索改写都交给同一个小模型，容易遗漏人物、截短台词或错误决定是否检索。前置 hook 也会增加串行延迟；只有在省下后续无效检索或改善结果时才可能有业务收益。

**原问题 C19 的真实触发与历史限制：**

原编号 **C19**，只存在于当前 problems，不在 backup。用户在台词检索后说“那又咋了，你说话怎么这么无趣啊”，被继续解释成剧情检索。最新记录是 10 月 21 日 `query_hook` 已接入；10 月 20 日的 `respond/retrieve/clarify + current_task` 是旧提案，不能和现接口并列称为已实现。

来源：[问题原文 C19](problems.md)。历史实验与输入留档继续引用原条，不重新发模型请求。

### 2. 方案候选

先测 OpenRouter Jev，再比较本地 Qwen3-4B 两版 prompt、Qwen3-VL-8B、Qwen3-8B Q4／BF16 和少量 gpt-5.6-sol。最后按职责收敛：Jev 输出 action，代码选择固定模板；respond 仅跳过重写模型，重写模型只填 retrieval slot，mixed 的回应任务和顺序保留给 Supervisor。这样减少“改写用户到底要做什么”的自由度，但仍需检验台词完整保留。

成本包括额外分类网络请求、GGUF 权重（4B Q4 约 2.497 GB，最终纯文本 8B Q4 约 5.028 GB；8B 转换曾产生 16.39 GB BF16 中间文件，实验后已清理）、llama.cpp server／量化／上下文配置、schema constrained decoding、跨回合上下文和语义评审。格式合法率不能替代语义正确率；同一 13 条反复调试也不是 held-out 泛化结果。

**同主题的接入验证：**

比较 Qwen3-4B、VL-8B、text-8B 的改写，再拆成 Jev 路由：respond 跳过改写，retrieve/mixed 才调用 text-8B。原始用户文本仍保留，避免改写截短成为唯一信息源。代价是增加本地模型服务、路由模板、超时与取消语义；不能只比较不同职责下的通过率就宣布模型改善。

**原问题 C19 的真实触发与历史限制：**

现方案在 Supervisor 前先做 `respond/retrieve/mixed` 路由，retrieve/mixed 再调用 Qwen3-8B 生成 retrieval_query，respond 不走重写；固定模板追加信息并保留原用户文本。历史显式补充“我不需要检索视频”只作为人工对照，不应成为永久禁检规则；第一人称、口语或反馈语气也不能机械决定路由。

当前服务只给最近 6 条消息、最多 2000 字符，按消息而不是 6 轮；尾部截断与 harness compaction 是不同机制。纯离线例子“这不是动画中的角色台词。”被截成“动画中的角色台词。”说明可能改变否定含义，实际误路由率未知。额外串行 router/rewriter 带来延迟、调用和上下文投影维护成本，长对话治理未一并完成。

### 3. 方案验证

Jev 固定 13 条中文样本参考标签不发给模型；本地改写复用它的历史 decision，未重复云分类。Qwen 本地使用非思考、temperature=0、JSON schema 约束。模板方案对 4 条 respond 跳过，只测其余 9 条；新评分只检查目标、原话和指代，与此前两字段任务不是同一准确率口径。“真是高高在上呢”仍有截断，不能报告无损成功。Codex 对照只完成 2 条，另有 usage 持久化故障，不能外推全部 13 条。

**同主题的接入验证：**

早期 13 例为已使用的合成测试，不是 holdout；Sol 因 429 仅有 2 个可用样本，无法完整比较。职责拆分复用已保存的 Jev 动作，新增本地调用 35 次、没有新增云端调用。接入测试另真实调用 Jev 4 次、rewriter 2 次、Supervisor 1 次；唯一完整 live Chat 为 respond 路径，不能证明检索路径整体效果。附件、播放上下文和过长文本跳过 hook；同步 HTTP 无法即时取消，只丢弃晚到结果，报告未部署 OS 自启动。

证据：[模型对照](../../outputs/verification/rewriter-comparison-20261020.json)、[text-8B 与职责拆分](../../outputs/verification/rewriter-text8b-report-20261020.json)、[hook 集成](../../outputs/verification/query-hook-integration-report.json)。

**原问题 C19 的真实触发与历史限制：**

最初问题样本 33.729 秒、2 次搜索、1 次 Canvas。控制相同前一轮的 fork 中，原话 147.943 秒、6 次搜索、8 次 Canvas、1 次 delegate、1 次 submit；人工补充无需检索后 10.629 秒、0 工具，但答案仍误解为剧情回应。更明确 task 的两次追加为 3.282/3.393 秒，均 429，不能作为质量成功。用户后续“效果还挺好”没有可匹配的 run ID 和统计，不能替代路由准确率评测。

### 4. 指标对比

| 条件 | 实测语义／结构 | 延迟中位 |
|---|---|---:|
| Jev 路由 | 13/13，respond 4、retrieve 5、mixed 4 | 529.79 ms（512.39–1709.06） |
| Qwen3-4B 第一版 | 字段通过 10/13 | 993.46 ms |
| Qwen3-4B 第二版 | 字段 13/13；语义 6 pass／5 partial／2 wrong | 876.79 ms |
| Qwen3-VL-8B Q4 | 10 pass／3 partial／0 wrong | 1.936 秒 |
| Qwen3-8B Q4 | 5 pass／7 partial／1 wrong | 1.866 秒 |
| Qwen3-8B BF16 | 6 pass／7 partial／0 wrong | 3.676 秒 |
| gpt-5.6-sol | 只完成 2 条且通过；全套未完成 | 两条 9.234／6.031 秒 |
| 固定模板＋纯文本 Qwen3-8B 填 slot | 9/9 schema；8 pass／1 partial／0 wrong；4 respond 跳过 | 1.151 秒（0.924–1.426），共 164 输出 tokens |

Jev 完整 14 次（含 noul）报告 $0.000382410，连先前 smoke 共 15 次 $0.000398538。noul=0.07 只是冒烟结果，不是实际工具授权策略。历史 Jev＋slot 中位相加约 1.734 秒、respond 约 0.535 秒，属于跨运行估算，**不是已接入 Chat 的端到端时延**。本地模型金额未配置，不能记免费。`evals/speaker` 的参考一致性／算法比较、`evals/latency/chat_latency.py` 则是可复用 harness，只有脚本不能算新增实测；Chat latency harness 会发真实模型请求和创建 conversation，本次归档没有执行。

**同主题的接入验证：**

- 集成 9 文件 +889/-1 行；真实 respond Chat COMPLETED，Jev 684 token、Supervisor 29,960 token、rewriter 0。没有旧/新自然检索完整对照，不能量化净节省。
- 147、219+1 skip、最终 88 等测试批次存在覆盖重叠，不累加；更广检查 165 pass、9 个已知基线失败。职责拆分降低单模型任务歧义，同时增加服务与模板维护，部署稳定性、GPU 成本和检索业务收益未验证。

**原问题 C19 的真实触发与历史限制：**

现有证据说明显式意图能改变工具轨迹，也说明“少调用工具”不能单独代表理解正确。自动 hook 的受控前后准确率、混合意图保留率、额外 token/延迟、缓存收益及长历史截断风险的发生率均未记录；不能把人工补充样本的约 137 秒差额归因于已实现的自动 router。

上面的原问题“自动hook受控前后结果未记录”，指完整自然Chat的因果对照；13条离线样本和一条respond接入已测，不能与之混淆。两份实验资料中的13例与9例是同批数据，此处不重复计为新的实验。

<a id="devlog-history-13"></a>

## 2026-10-19：声纹决策门禁、纠错反馈和供应商失败

### 1. 问题与现状

Refine 需要为指定对白完成声纹审查，但旧流程可能在缺少必要决策时结束，或者在模型结束后才发现不满足条件。反复启动外层模型会放大耗时；同时供应商余额、容量和限流失败会使算法根本没有被执行，不能混作身份方法失败。

### 2. 方案候选

先尝试强制审查 gate、严格 LOO 与 lead 阈值调整，再加入同一次 Agent 会话内的 stop_feedback，让模型针对缺失或不一致决策继续修正。候选收益是复用当前 Canvas 和上下文；代价是会话继续运行、跨 provider 的反馈接入以及 deadline/预算的协调。保留终止和 deadline，不能把反馈改成无限重试。

### 3. 方案验证

两窗口 replay 曾完成 9/9 必需决策、0 audit violations，但已知身份仍只有 1/2；另一 tail replay 两窗口均 invalid_model_response，不能把中间草稿算成答案。v13 的 24 个 ASR 请求因 AccountOverdue 失败，v14 为模型容量不足；v15 只完成 3 个 Refine 窗口即因决策不一致而失败；v16 初次运行 23/24 Understanding，Refine 未执行，不能用于证明调低阈值有效。后续诊断只能增加今后的详细错误留证，不能从缺失的历史工具参数中补造归因。

反馈接入的代码报告新增测试通过但当时没有真实模型效果；之后 v17 才实际运行，最终仍 PARSING_FAILED。证据：[成功 gate replay](../../outputs/verification/speaker-voice-replay-20261019/replay-report.md)、[失败 tail replay](../../outputs/verification/speaker-label-tail-replay-20261019/replay-report.md)、[v13](../../outputs/verification/speaker-voice-decisions-20261019-v13/run-failure-report.md)、[v14](../../outputs/verification/speaker-voice-loo-luna-20261019-v14/terminal-voice-report.md)、[v15](../../outputs/verification/speaker-voice-ranking-retry-20261019-v15/ranking-voice-report.md)、[诊断留证](../../outputs/verification/speaker-voice-review-diagnostics-20261019/report.md)、[v16](../../outputs/verification/speaker-voice-lead005-20261019-v16/README.md)、[反馈实现记录](../../outputs/verification/speaker-voice-review-feedback-20261019/report.md)、[v17 终态](../../outputs/verification/speaker-voice-feedback-20261019-v17/README.md)。

### 4. 指标对比

- 成功 replay 两窗口耗时 73.188 / 120.634 秒，0 次数据库提交；失败 replay 共 4 attempts、约 357.207 秒，没有可接受终稿。
- 反馈代码验证为新增 36 个测试通过；全套 1,202 通过、9 失败、2 跳过，基线同样 9 失败，不能写“全绿”。
- v17：Refine 16/22 完成、3 失败、3 等待；288 次 gate 检查中 198 拒绝、90 接受，38 次完成反馈涉及 8 个窗口，其中 6 个最终提交。仍有 429 和 max model calls 失败，目标短句混段问题未解决。
- 门禁正确执行只证明流程约束，不能证明声音身份正确。反馈减少另起会话的需求属于架构作用，真实节省量未做同条件 A/B；Claude 与持久会话后端的反馈方式不同，维护复杂度有所增加且未量化。v18 只有保存的 ASR 对照被本次检查，未据此推断整轮成功。

<a id="devlog-history-14"></a>

## 2026-10-19：Shot tags 通过一致性门槛，但人工相关性评估仍未完成

### 1. 问题与现状

需要知道 camera、composition、subjects、frame、content 五组结构化标签是否稳定，以及能否帮助“类似构图”检索。方案明确把 PCP 与 GroundingDINO 独立保留，content 只用于排除，不把内容重合偷偷当构图加分。[实验 brief](../design/02-agent-harness/shot-tagging-experiment-brief.md)、[字段 schema](../design/02-agent-harness/shot-composition-schema.md)、[Shot-tags 归档摘要](design-experiment-cleanup.md#shot-tags-标注与排序)

### 2. 方案候选

比较 gpt-5.6-sol 两次独立标注与 doubao-seed-2-0-lite-260428；选定可复用 primary_a cache。选择依据是结构合法率与 shot size／vertical angle 的一致性门槛，而不是尚未测出的检索准确率。后续排序 M0 image-only、M1 加同源时间排除、M2 加 tags、M3 加 PCP、M4 加 boxes、M5 tags＋PCP＋boxes，RRF k=60。

成本包括冻结五组 schema／prompt、strict validation 与明确 correction retry、图片 hash 去重、三路人工对照页面、模型与价格快照、融合通道及盲评池。M5 的复用排重仍用 image 信息，不能简单叫“完全无图像”。

### 3. 方案验证

200 unique shot/hash pilot 覆盖 104 个 episode×duration×motion strata，保留 4 个指定 anchors；“motion”来自像素变化，不独立证明运镜。主模型 A/B 首次 200/200 合法，cheap 首次 198/200、修正后 200/200。pilot gate 通过后开全量，后被用户明确停止，只交付完成子集，明显偏前几集。HTML 有图、框和三个模型字段，浏览器因本地文件 policy 未完成目视验收，不能省略此限制。[Shot-tags 归档摘要](design-experiment-cleanup.md#shot-tags-标注与排序)

25 queries×6 methods 共 150 排序；735 query-shot pairs collapse 为 718 blind candidates（每 query 19–37），但用户相关性判断未完成、score.py 未执行。某 anchor 从 M0 rank 22 到 M4 rank 2 是排序变化，不是人工已确认收益。

### 4. 指标对比

| 字段 | 主模型自一致率／κ | 主模型 vs cheap κ |
|---|---|---:|
| Shot size | 81.5%／0.784 | 0.497 |
| Vertical angle | 95.5%／0.888 | 0.673 |
| Primary composition | 81.0%／0.770 | 0.394 |
| Main-subject cell | 88.5%／0.747 | 0.573 |
| Main-subject scale | 80.5%／0.682 | 0.522 |
| Person-count bucket | 93.0%／0.898 | 0.891 |

| 成本／覆盖 | 实测 |
|---|---|
| primary A／B pilot wall | 457.008 秒／6 workers；654.544 秒／4 workers，不是等并发速度 AB |
| 全量原目标／完成交付 | 8,147 帧；交付 4,015 帧、2,889 shots、3,964 unique hashes |
| 全量已完成有效调用／加 pilot cache 后 hashes | 3,765／3,965；5 行因代表帧未完排除，交付子集为 3,964 hashes |
| 未完成请求目标 | 4,132 帧、3,009 shots；不能推断全集质量 |
| Offline features | 8,147 帧／8,092 hashes，4,845.34 秒；PCP 中位 5.68 ms、DINO 544.63 ms |
| 全量最后 wall 快照 | 4,963.906 秒（不是精确停止时刻） |
| pilot＋smoke＋full 已完成 usage | input 61,491,905；output 1,577,749；total 63,069,654 tokens；10 个中断请求 usage 不明 |
| 可计价 cheap 小计 | CNY 0.606627；Codex 无配置单价，总金额 unavailable |

743 shots 出现标签差异（25.72%），另 2,140 shots 少于两帧、37 个变化检查不完整；不能将未检查视为稳定。预测 8,147 帧约 5.17 小时／6 workers 或理想 2.89 小时／10 workers只是 pilot 外推，不是实跑完成耗时。nDCG、P@5、pooled recall 全部尚未验证；实验没有修改生产 DB、索引或代码。

<a id="devlog-history-15"></a>

## 2026-10-19：B0 声纹基线、聚类和参考标签污染

### 1. 问题与现状

以已有解析人物名构建声纹参考池时，参考标签本身可能错误；用这些标签评价算法会得到“同源标签一致率”，不能等同真实说话人准确率。若直接把高分当真实身份，错误标签会传播到后续对白。

### 2. 方案候选

候选包括 B0 最近参考、聚类后的 C1/C2、以及 doubtful/tail/严格 leave-one-out 参考诊断。聚类可能压制少样本人物；严格排除同 cue、重叠音频及同 PCM 可减少自匹配污染。tail/LOO 只标记可疑参考，不自动改名；这增加了参考池来源和排除规则的维护成本，但避免把相似分数包装成概率。

### 3. 方案验证

10/18 使用冻结的 192 维 CAM++ 向量比较；10/19 v12 才进入真实解析。随后 doubtful、tail、LOO 都在同一批既有向量上回放，不是新人工标注集。阈值为实验设定，不能声称经过校准。证据：[算法对照](../../outputs/verification/speaker-algo-20261018/report.md)、[B0 集成实测](../../outputs/verification/speaker-b0-integration-20261019/actual-test-summary.md)、[doubtful](../../outputs/verification/speaker-label-doubtful-20261019/report.md)、[tail](../../outputs/verification/speaker-label-tail-20261019/report.md)、[严格 LOO](../../outputs/verification/speaker-label-loo-20261019/report.md)。

### 4. 指标对比

- 冻结集：339 行对白、335 行可编码；B0 标签一致率 80.87%，C1 约 77.85–78.52%，C2 约 76.85–77.18%。B0 候选 auto-accept 102/339，尚不能称身份准确率 80.87%。
- v12：工程校验 36/36、任务 COMPLETE，但两个已知身份目标仅 1/2 正确；317 行、315 向量，标签一致 208/277（75.09%），ACCEPT 子集 89/97（91.75%），仍是代理指标。
- doubtful 两遍标记 13→11；tail 为 19→19；严格 LOO 为 17→17。它们衡量参考筛选行为，不代表改正了相应数量的身份。
- 维护上增加了参考池排除、版本和诊断字段；人工标签成本、最终误认率与业务费用没有测量。不能凭局部标签一致率决定上线自动改名。

<a id="devlog-history-16"></a>

## 2026-10-18：解析负载下的 MCP 启动放大与任务列表大字段读取

### 1. 问题与现状

原编号 **B13**。列表曾耗时 9.1–13.97 秒、Series 详情 24.315 秒，但随后列表降到 377.5/474.4ms，health 16ms、单详情 60.2ms；不是所有请求永久堵塞。两个已确认放大点是每次 Codex 会话启动继承全局 MCP，以及只为统计任务进度却加载完整成果 JSON。MCP 实例开关已实现并做真实无推理验证；任务列表轻量投影和真实服务负载收益在此历史记录仍待处理。

来源：[B13](problems.md)、[Codex client](../../gateway/codex/client.py)、[任务统计](../../server/repository/job.py)、[TasksPage](../../web/src/features/tasks/TasksPage.tsx)。

### 2. 方案候选

实例 `mcp=False` 读取有效服务器集合并逐项禁用；True 保留配置。V2 解析和内层角色关闭，Codex Supervisor 开启，角色策略放装配层而非 Gateway。任务列表先改为存在性、版本和必要 JSON 叶子的窄投影，再评估索引或统计冗余；普通覆盖索引不会自动解决大 JSON 解码。共享 app-server、前端请求截止时间是独立候选，尚未一起实施。

实例参数增加 start/resume/rollback 一致性责任，但无需共享进程的新隔离框架。共享进程还需证明工具回调、取消、历史和用量不会串线。列表轻投影维护少量字段契约，代价小于新增统计表，但须保留版本、耗时完整性及旧成果兼容。

### 3. 方案验证

bundled SDK 0.156.1 的独立无推理 thread 核对有效 MCP 全部 disabled、工具数 0、无对应进程，宿主动态工具可用；受控配置覆盖 start/resume/rollback 和原本 disabled 的服务器，0 推理 turn。回归 536 passed。现有 API 未重启、未重跑解析；一次 open 约 1.32 秒不能算前后性能对照。只读 EXPLAIN 的 Chunk 成果关联为 eq_ref，没有缺关联索引的证据。

### 4. 指标对比

当次列表覆盖 23 任务、501 成果，content 4697554 bytes、detail 16484904 bytes，约 20.2 MiB/次 DB 读取，未含父任务及 Interval；不是浏览器响应大小。长度聚合 SQL 约 51.8ms，传输/JSON/ORM 未隔离计时，不能归因全部 13 秒。101 进程样本有 10 Codex + 10 组 MCP，RSS 合计约 11.4 GiB 含共享页；高 load、压缩内存和后续 CPU 空闲不能直接证明 OOM。修复后任务列表和整体资源变化未知。

<a id="devlog-history-17"></a>

## 2026-10-18：更换 speaker encoder 能改善个别样例，但不能解决参考库身份问题

### 1. 问题与现状

CAM++ 在困难爽世样例上有错排，需要区分 encoder 能力、参考质量、adapter 与聚合方式。比较使用完全相同 PCM，固定 47 条音频（v11 参考 31＋平衡参考 15＋target）。真正有外部确认的目标句只有两条，不是大规模准确率测试。[声纹模型实验退役摘要](design-experiment-cleanup.md#声纹模型与适配器)

### 2. 方案候选

候选为 CAM++、ERes2NetV2、ReDimNet-B6 Vox／mixed、WavLM，以及 WavLM 官方旧环境复测。保留 pair mean 与 centroid 为不同指标；不因某个目标用 centroid 更好就事后切换胜负口径。192／256 维区别也不是精度因果证据。

成本包括模型权重和来源核验、多环境 adapter、旧依赖 pin、固定音频 hash 及聚合协议。WavLM 缓存约 2.570 GB，需同时处理 SV 权重与基础配置；官方旧环境使用 Python 3.9、Torch 1.9、NumPy 1.20 和固定 s3prl commit，并在本机经 Rosetta x86 运行。[声纹模型实验退役摘要](design-experiment-cleanup.md#声纹模型与适配器)

### 3. 方案验证

CPU 4 threads、FP32、batch 1 比较新编码器；CAM++ 使用既有缓存，没有加入同轮速度排名。WavLM 与固定官方实现重复同 47 PCM，验证 710 项相关权重严格匹配（分类投影例外）；微小数值差没有改变目标排名，因而 adapter 数值误差不能解释困难例失败。[声纹模型实验退役摘要](design-experiment-cleanup.md#声纹模型与适配器)

### 4. 指标对比

| 模型 | 困难爽世 v11，pair-mean 排名 | CPU 单次中位 | 47 条累计 |
|---|---:|---:|---:|
| CAM++ | 4 | 本轮未重测 | 本轮未重测 |
| ERes2NetV2 | 4 | 34.032 ms | 1.970 秒 |
| ReDimNet-B6 Vox | 1（margin 0.026857） | 144.948 ms | 7.830 秒 |
| ReDimNet-B6 mixed | 1（margin 0.007730） | 138.164 ms | 7.528 秒 |
| WavLM | 5 | 124.285 ms | 7.240 秒 |

平衡参考条件下各模型均把爽世排第一，爱音目标也均第一，说明参考条件影响显著。WavLM 官方复测 vector 最大差 3.304304e-6、最小 cosine 0.99999999988，585 pair 最大差 4.592118e-6；困难目标仍第 5。WavLM load 4.583 秒、warmup 2.007 秒、RSS 2.843 GB。实验代码 6 个脚本共 1,091 行，87 个文件；没有生产模型切换，也没有全体角色准确率或 DER 结论。

<a id="devlog-history-18"></a>

## 2026-10-18：整集聚类能整理声音候选，不能直接把稳定簇当角色

### 1. 问题与现状

固定参考容易误标和采样集中，候选方向是先按声学信号聚类，再由 Agent 根据画面和上下文归名。研究中的 AHC／spectral 候选只是调研，实际验证采用 WavLM＋普通欧氏 KMeans。[声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)、[聚类与选源退役摘要](design-experiment-cleanup.md#聚类与选源)

### 2. 方案候选

比较不同 K 的稳定性／silhouette，随后固定 K=8 做探索参考替换与 Agent 辅助归名。聚类标签可用于挑候选，不应作为角色真值；未知角色与待复核状态需要保留。生产向量维度为 192，实验 WavLM 256 维只在本地 schema 适配，未发布到生产。

具体成本为音频窗口 eligibility、短段／歌曲／overlap 排除规则、cluster 到 source hash 的映射、代表样本选择、可见人物与真实发声的区分、参考完整性发布门槛。只有稳定度没有身份 ground truth，会使错误簇更“稳定”。

### 3. 方案验证

24 chunks 的 378 ASR cues 切为 436 个不重叠且不超过 3 秒窗口，加 3 holdout，共 439 embeddings；226 fit，181 个短于 1.5 秒窗口不参与 fit。归一化输入，但非 spherical KMeans、无 PCA／whitening。K=2..12、5 组 seeds、每组 10 starts，按 median silhouette 选 K；最优 K=2 把两个已知角色都放同一簇。

K=8 参考替换只换 4 组并保留 6 人，随后 18 个代表候选交给 Voiceprint Agent；75 个 uncertain 没有送入。该 Agent 实际看每候选 3 帧，不能直接听音频。最终 15 refs／5 人未达严格 6/11 角色覆盖，ready=false、未发布。[聚类与选源退役摘要](design-experiment-cleanup.md#聚类与选源)

### 4. 指标对比

| 项目 | 实测与含义 |
|---|---|
| 最佳 K=2 | silhouette 0.237962，最小 seed ARI 0.981924；稳定但未分开已知角色 |
| 439 次 WavLM | 累计 52.290 秒，单次中位 120.316 ms；load 4.793 秒、warmup 0.804 秒、RSS 2.844 GB |
| clustering | 6.394 秒 |
| K=8 参考替换困难爽世 | pair mean 排名 5→2；centroid 4→1；不同统计不可混用 |
| 最终 Agent | 149.47 秒，input 1,315,582／cached 1,174,272／output 4,795 tokens；15 refs、5 人，未 ready |
| superseded 1／2 | 78.34／54.66 秒；前者把弱标签重复成员当独立支持，后者缺角色且未登记 |

最终 Agent-only 爽世 mean 第一，补回缺失竞争角色后变第二；不能通过删掉竞争者制造提升。没有完整 diarization ground truth／DER，没有上线收益或可用总金额。

<a id="devlog-history-19"></a>

## 2026-10-18：Shot gallery 建立了可复现的画面候选单位，尚未证明检索质量

### 1. 问题与现状

原问题编号 **C16**。早期问题记录的17张选图、13张共同人物框池，无法区分候选缺失与排序失败；它所说的“共同图库待建”是当时状态。下述10/18 gallery已补齐数据组织，人工相关性验收仍未完成。

Event 不一定对应单一镜头，做构图检索与相似画面排重需要 shot、keyframe 和复用组。10/18 建立独立离线 gallery，不改变 Event index 的权威内容。[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)

### 2. 方案候选

可以继续以 Event 为候选，也可以保留 Event 的语义、用 shot／keyframe 承担画面单位，再通过 overlap 映射。选用后者进行后续 shot-tags 实验；这只是实验数据组织，不是新增生产索引收益已验证。

维护成本包括镜头切分规则、代表帧与额外帧、原图 hash、OP/ED 和低信息标志、同镜头／跨集复用关系及 source revision。不能把一个复用组当一个身份，也不能用同镜头多帧冒充多个独立正确候选。

原问题要求在同一帧库比较image embedding、PCP与人物框，不用Event最多3帧的聚合向量冒充逐帧特征库。Event回填7103条的验证归入Event条目，避免重复算成图库排序收益。来源：[C16](problems.md)。

### 3. 方案验证

13 集生成 gallery，所有 7,103 Events 都有 overlap shots；对 unique JPEG 做 1024 维 doubao embedding，并记录复用组。后续 shot-tags 用该 v1 作为冻结来源，因为当时没有可验证 v1.1。没有在本轮重新切镜、抽帧或计算向量。

### 4. 指标对比

| 产物 | 数量 |
|---|---:|
| Shots | 5,898 |
| Keyframes | 12,447 |
| Unique JPEG hashes／已完成 embedding | 12,389 |
| 复用组 | 5,585 |
| 多 shot 复用组／跨集组 | 169／58 |
| 最大复用组 | 12 shots |
| 已映射 Events | 7,103／7,103 |

这些是数据完整性和规模指标，不是 retrieval precision／recall；全量耗时、货币成本和质量基准尚未给出可比实测。

<a id="devlog-history-20"></a>

## 2026-10-18：Query-only 收敛与真实回答质量边界

### 1. 问题与现状

原 Query→Evaluator→ChatResult 链路包含多层协调与判断，维护和执行路径较长。本轮希望以 Supervisor 直接使用原子工具、必要时调用 Query LangGraph 替代；真正验收是用户拿到正确对白、人物和时间范围，而不是删除了多少模块。

### 2. 方案候选

选择收敛 Query/Evaluator/ChatResult 层，把检索工作交给直接工具或 Query 子 Agent。减少中间对象和协议有利于维护，但 Supervisor 需要承担更清晰的任务分派与引用责任；并行子 Query 可以重叠执行，不能将子调用时间简单相加视为总耗时。工程收敛不自动消除翻译错误。

### 3. 方案验证

报告包含自然问题、组合问题与多次修订；计时从 runtime kickoff 开始，排除 HTTP 排队和 SQL，所以不能与 HTTP 完整响应直接比较。没有同输入旧架构控制组，也未在该报告中重启 API。局部测试与真实调用通过情况分别记录，不以测试数替代回答质量。

证据：[验证总表](../../outputs/verification/chat-query-only-20261018/verification-summary.json)、[组合任务复核](../../outputs/verification/chat-query-only-20261018/combined/review-summary.json)、[最后一轮复核](../../outputs/verification/chat-query-only-20261018/r4/review-summary.json)。

### 4. 指标对比

生产变更 24 文件、+702/-2,255 行；这是结构减少量，不是产品性能指标。一次 C 问题 44.87 秒，台词、人物和区间通过；B 第二轮 101.68 秒因语义翻译失败，最后 B 100.40 秒主要结论正确但替代译文仍错。组合任务 127.36 秒，两个 Query 有约 46.59 秒重叠，引用片段有依据；整体 quality_acceptance 为 false。Chat 测试 351 通过；server 2,352 通过、107 跳过、11 排除，并复现了 11 项基线失败，不应描述成所有测试全绿。层数和维护面缩小，但没有测量净时延改善、认知成本或泛化正确率。

<a id="devlog-history-21"></a>

## 2026-10-18：Event-first 检索与 HTTP 部署验证

### 1. 问题与现状

精确对白问题如果先找 chunk，再额外读取长证据，可能增加工具轮数与参数错误。目标是把可定位的 Event 优先交给 Agent，同时确认运行时答案、SSE 和数据库持久化一致，而不是只验证检索函数。

### 2. 方案候选

在 prompt 中优先使用 Event 检索，只有需要上下文时再读完整 transcript；Supervisor 可直接完成，不强制经过 Query。代价是必须正确解释事件与完整语境的关系，避免只看短句就误判人物。部署后用真实 HTTP 请求验证；该历史版本还注册了 Canvas，不能据此描述当前 Chat 的工具清单。

### 3. 方案验证

同一原始问题、13 集/312 chunks/7,103 events 范围，各有一轮前后运行；没有强制工具、没有 holdout，属于实例对照。目标两边均 top1，改善主要来自路径减少。重启后另发真实 HTTP/SSE 请求，核对 content/renderBlocks 持久化。

证据：[Chunk-first](../../outputs/verification/chat-total-series-quote-20261018/review-summary.json)、[Event-first](../../outputs/verification/chat-event-first-prompt-20261018/review-summary.json)、[HTTP 重启后验证](../../outputs/verification/chat-http-after-restart-20261018/summary.json)。

### 4. 指标对比

| 指标 | Chunk-first | Event-first |
| --- | ---: | ---: |
| runtime 耗时 | 48.451 秒 | 29.340 秒 |
| 工具调用 / 错误 | 7 / 1 | 3 / 0 |
| Event 检索本身 | 5.356 秒 | 5.618 秒 |

Event-first 读取完整 902 字 transcript，给出覆盖 54.6–56 秒目标的 46.4–60 秒卡片；没有证明底层检索更快。重启后的 HTTP 总耗时 28.695 秒，首 SSE 0.062 秒、首进度文本 6.847 秒、首实质文本 22.990 秒、卡片 24.669 秒，3 工具、0 错误。content/renderBlocks 一致；六项融合分数有末位浮点差，不能声称整个对象逐字节相同。少调用显示该案例调度收益，但没有稳定时延、费用或多问题质量数据；prompt 规则较轻，维护成本主要是事件证据契约与回归案例。

<a id="devlog-history-22"></a>

## 2026-10-18：Event 细粒度检索改善证据定位，但 readiness、候选曝光和模型回合仍影响全程

### 1. 问题与现状

原正式 13 集 scope 有 312 chunks，却没有 Event published receipts；另一个 EP04 实验任务的 430 Events 不能代替该 scope 的索引。自然台词 query 先报 Event not ready，再回退 chunk；因此先判断实际数据范围和 readiness，不能把尚无索引当成检索模型失败。[原 scope 实验](../../evals/benchmark/query_capabilities/README.md#历史结论event)

### 2. 方案候选

保留 chunk 基线、增加 Event 召回与父 chunk 播放、对已有 canonical 内容执行 INDEX-only 回填；文字精确句、翻译扩展、自然中文与图片 query 分开比较。Event 是从 canonical SQL 派生的索引，不另设 Event SQL 权威表。图片直接同源和构图描述搜索也是不同任务。

维护成本包括 receipt／content version 一致性、canonical 内容不变的证明、逐事件／逐帧 embedding 与 ffmpeg、coverage 查询、Event 候选到 chunk 的映射。更细粒度不等于全程更快。

### 3. 方案验证

独立 EP04 v11 有 24 chunks／430 Events，日语「部外者は黙ってて。」Event 首位为立希 548–549 秒、播放 540–553 秒。两条自然中文 Chat 均无需 chunk fallback；其中目标 Event 初次 rank 22 不在首屏，改 query 后 rank 11 才曝光。完整召回集合不等于模型实际已读集合。[EP04 日语](../../evals/benchmark/query_capabilities/README.md#历史结论event)、[EP04 中文](../../evals/benchmark/query_capabilities/README.md#历史结论event)

随后正式 13 集回填 312/312 chunks、7,103/7,103 Events；验证 content id／version／SHA 未变，16:17 完成、16:19 核验，INDEX retry errors=0。图像 query 的原图同源首位 EP01 9:02.3–9:04.3、cos 0.9294221；只写构图则可能命中 EP13 类似大／小主体，宽泛描述还可命中 ED。文字候选池 20 与图片 40 不同，top_k 又是父 chunk 数，不能直接做公平模态优劣比较。[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)

### 4. 指标对比

| 条件 | Event／Chunk 表现 | 时间边界 |
|---|---|---|
| 原 scope 自然台词 Chat | Event not ready，扩展 query 的 chunk 目标 rank 2 | 全程 103.399 秒，首字 12.068 秒，工具合计约 1.112 秒 |
| EP04 精确日语直接检索 | 两者均 top1 | 3 次中位 Event 0.699／Chunk 0.596 秒 |
| EP04 精确句 Chat | 正确事件与播放范围 | 226.111 秒，53 个工具调用、7 次看帧、2 轮评审；无 chunk-only Chat AB |
| 中文两条 Chat | 37／40 Events 召回，首屏只有 15／14 | 156.105／154.078 秒，首字 7.375／8.079 秒 |
| 中文直接查询 A／B／C | Event 目标或邻近 rank 8、5/8、2；Chunk rank 10、4、5 | Event 1.408／0.643／0.585 秒；Chunk 0.588／0.537／0.552 秒 |
| 正式回填后 vision-only | 19／8 Events，各 5 父 chunks，partial=false | 5.75／5.30 秒 |

回填共 16,535 次帧采样（允许重复）；某 5 分钟窗口 image 1,749、text 773 次调用，无失败，中位 320／125 ms。未给全量货币成本。Event 证明了更精确的事件定位和可用索引，不足以证明整轮 Chat 更快或全集 recall 更高。

<a id="devlog-history-23"></a>

## 2026-10-18—2026-10-19：Event 补建的大 JSON 写入放大与 binlog 留存

### 1. 问题与现状

原编号 **B14**。MyGO 13 集 Event 补建在约 45 分钟内产生 44.106 GB binlog，核心负担是不断重写增长中的 JSON，并非普通文本日志或同一 Chunk 无限重试。历史状态为缓存批量保存、无变化聚合、扫描游标修复已完成源码核对，七天 binlog 留存已启用；修复后真实工作负载降幅与运行服务加载仍未在本条复验。

来源：[问题原文 B14](problems.md)、[索引协调器](../../server/service/video_parse_index_coordinator.py)、[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)。原条所记 `server/repository/video_parse.py` 已迁移，历史路径保留用于追版本，不伪装成当前可打开文件。

### 2. 方案候选

选择减少实际 UPDATE：pending 结果满 8 个 Event 或距上次 30 秒，在 Event 边界保存；正常结束 flush，异常尽力 flush；没有变化时不保存聚合结果。仅把同样多的 UPDATE 包在一个事务里不会消除 ROW/FULL 行镜像。七天留存减少历史占盘，不降低每次补建的写入量。

方案复用既有成果和提交路径，减少重复写大字段；代价是进程硬退出可能丢掉尚未 flush 的派生缓存，需要重建，并增加 pending/flush/扫描进度的维护约束。模型槽位 reserve/release 与大父任务 detail、embedding 混入 JSON 的后续职责仍需按实际写入量评估，不能仅按更新次数决定优化。

### 3. 方案验证

只读扫描 48 个 binlog 文件，按二进制事件时间筛选，错误与截断均为 0。补建窗口 15:31:46–16:17:04 有 312 次 INDEX 开始/完成、重复同一 `(task, execution)` 开始为 0。确认原 13 集、312 Chunk、7103 Event 与 16535 帧采样记录；帧采样记录不等于独立图片。源码确认修复路径，不冒充再次执行修复后补建。

### 4. 指标对比

| 修复前精确窗口指标 | 数值 | 口径 |
| --- | ---: | --- |
| API 文本日志 | 30324 行 / 4763577 bytes | 与二进制日志分开 |
| embedding HTTP 200 日志 | 23334 条 | 不是 SQL 次数 |
| binlog 全部事件 | 1144814 / 44105531159 bytes | 44.106 GB |
| UPDATE_ROWS 事件 | 206137 | 不等于 SQL 条数、事务数或受影响行数 |
| chunk_parse_content UPDATE | 31060 / 33.831 GB | 重写大成果 detail |
| video_parse_task UPDATE | 60420 / 10.092 GB | 重写父任务 detail |

两张大字段表占全部 binlog 字节约 99.59%；同期最终成果字段 content 约 5.231 MB、detail 约 318.805 MB，是字段长度聚合而非物理表空间。修复后的写入次数、字节量、重建耗时和故障恢复代价未知，不能给出节省百分比。

<a id="devlog-history-24"></a>

## 2026-10-18—10-19：Chat 重复审核、慢响应与 Supervisor 遗留项

### 1. 问题与现状

[Chat性能计划](perf_plan.md)的台词样本耗时161秒，工具执行约2秒，主要时间消耗在约25–30次模型往返、重复读证据、修改播放范围及内外两次答案生成。QUEUED等标签包含模型工作，不等于数据库排队时间。架构简化后仍有并发、预算、取证和交付边界，不能仅凭少一层Agent宣称稳定快速。

**原问题 C17 的阶段追踪：**

原编号 **C17**。两个中文台词样本用时约 154–156 秒；Query→Evaluator→内层 Chat→Supervisor 对同一证据重复读取、审核、润色，检索后端本身耗时很短。此条是当时拓扑和测量记录，后续 10 月 23 日 Chat A/B 属另一轮实验；不能把当前 Canvas 改动或 SQL 后端收益补写成当时已验证。

来源：[C17](problems.md) 及原条链接的真实 Chat Trace。

### 2. 方案候选

先跳过Supervisor下的内层Chat生成，范围/措辞建议由最终Supervisor处理；再评估单检索循环与并行Query workers。确定性校验替代可机械判断的重复审核，证据不足才继续调查。减少重复推理的同时，Supervisor需承担合成、工具预算和跨worker证据一致性，维护职责并未消失。

**原问题 C17 的阶段追踪：**

候选是减少内层润色与角色交接、局部修订无需重复检索，并保持 C12 的 Supervisor 最终事实和选段责任；先补时间分段观测，避免为计量新增高频写表。代价是减少独立审核可能影响证据质量，职责合并也需要明确谁补查、谁批准与谁最终提交，不能仅优化调用数。

### 3. 方案验证

历史中文台词单次147→100秒，RESPONDING49→19秒；当时tests/agent/chat437 passed。10/19并行架构记录1115 passed，但未做真实模型端到端延迟验收；既有local_services8失败不属于该次修改。Query-only/Event-first、路由hook和10/23反馈实验见各自条目，模型、数据和变量不同，不串成一条累计提速曲线。

**原问题 C17 的阶段追踪：**

历史两轮并行/先后实测不是随机、重复、同输入的严格 A/B，也不是 P95。B 约 42 次工具、33 次 Canvas；C 约 39 次工具、31 次 Canvas。检索仅 0.731/2.258 秒，模型会话、编排和工具往返是主要剩余时间，但不能把所有剩余时间叫纯推理。C 的修订轮增加约 51.227 秒，现证据不能认定这部分全可删除。

### 4. 指标对比

| 原评审项 | 归档状态与产品影响 |
|---|---|
| 1 并发上限只约束单批 | 多个query_video可能叠加并发；原材料未记录闭环修复 |
| 2 worker耗尽共享tool预算导致交付失败 | 当时limits关闭时不触发；开启时需保留交付能力，未记录验收 |
| 3 model预算每批重复分配 | 当时休眠问题；无请求级累计预算修复证据 |
| 4 limits关闭仍向worker传24次上限 | 已在10/23 Canvas/Chat条目的额度修复中验证，不再仅按10/19记为未解决 |
| 5 未给最终合成预留时间 | stage timeout部分缓解；完成证据的保留与总deadline仍需单独验收 |
| 6 未分类的provider失败取消整批 | 可恢复性和成功worker结果保留待证据 |
| 7 grounding仅靠prompt | 已存在DraftReadAudit不等于正式交付已消费；缺强制已读取重叠证据闭环 |
| 8 一个EVIDENCE_CONFLICT丢整批 | 重建期间的局部失败隔离未记录结果 |
| 9 tool_lock串行化网络直搜 | 并行直搜时延收益未测 |
| 10 抽帧cap按session倍增 | 当时limits关闭；共享限制范围待验收 |
| 11 max_loop_turns/UI仍暴露但无效 | 配置认知与实际行为不一致，原材料没有修后验收 |

除第4项有后续明确证据，其余是源评审遗留状态，本轮未逐条重新审查当前代码，不据此断言仍全部存在。历史单次提速不能代替质量、成功率、p95与成本的流量分布；这些指标仍需同case重复对照。

**原问题 C17 的阶段追踪：**

| 样本 | Query | Evaluator | 内层 Chat | Supervisor | 口径 |
| --- | --- | --- | --- | --- | --- |
| B | 约 56 秒 | 37.5 秒 | 31.1 秒 | 22.3 秒 | 分阶段观察 |
| C | 29.4 + 32.5 秒 | 24.4 + 18.7 秒 | 24.5 秒 | 14.7 秒 | 含修订轮 |

完整 Series 背景约 1.3 万字符，单次 prompt 约 2.27–2.82 万 token；整 run 累计 usage 不能当成单 prompt 大小。具体压缩/角色精简的前后质量、token、费用和时延收益在本条未知，须用后续同口径实验补充。

<a id="devlog-history-25"></a>

## 2026-10-17：构图表征要分别验证类别、几何和内容，不宜混成一个相似分

### 1. 问题与现状

“类似构图”可能指类别相同，也可能指人物位置、画面线条或视角相似。PCP 分类、SAMP/MUSIQ 质量分、CLIP/DINO embedding 与检测框编码各回答不同问题。[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)

### 2. 方案候选

比较原图模型特征、DINO 对框／线条栅格图的特征、PCP 与全物体／人物框融合，以及真实 Chat 按构图描述搜索。融合的 PCP 0.8、box 0.2 是试验默认值，未优化或校准；没有足够数据选定通用赢家。

新增维护包括多模型权重和预处理、特征是否缺失的语义、类别映射、坐标／线段来源、融合权重与评测口径。框的轴对齐会丢倾斜信息；质量分不可直接冒充构图类别概率。[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)

### 3. 方案验证

固定 17 张图、10 组探索性三元组。栅格实验用同一批原图，分别编码原图、全部框、线条、框＋线条；共同有效 query 为 16 条、共同有效三元组 9 条。PCP＋人物框只在 13/17 图存在特征，粉色倾斜图漏人不能当作无人物。Chat 另在真实视频 corpus 找构图：它的候选库不同，不能与 17 图离线分数直接对比。[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)

### 4. 指标对比

| 首轮表征 | 三元组胜出 | 单次中位／设备 | 权重大小 |
|---|---:|---|---:|
| PCP fc7 | 8/10 | 4.17 ms／CPU | 217.1 MiB |
| CLIP 原图 | 7/10 | 6.67 ms／MPS | 337.6 MiB |
| SAMP | 5/10 | 12.81 ms／CPU | 174.9 MiB |
| PCP 分类概率 | 4/10 | 同 PCP | 同上 |
| DINO 原图 | 4/10 | 6.97 ms／MPS | 84.2 MiB |
| MUSIQ | 4/10 | 38.04 ms／MPS | 103.6 MiB |

后端／预处理不同，耗时不能当等条件速度排名。栅格原图／框／线／框＋线的同类别 top3 共现为 12/48、6/48、14/48、14/48；共同 9 个三元组胜出 4、3、7、5。36 种小扰动的高 cosine（部分 0.954–0.985）不等于排序稳定。融合人物框 CPU 13 次中位 21.31 ms，未得到整体准确率。

真实 Chat 耗时 358.389 秒，7 次 retrieve、22 次看帧工具，75 帧／60 unique，返回 2 个 EP07 卡；两卡属于同一机位，不能当两种独立构图命中。栅格实验本身留下 121 个文件、约 6.9 MiB、106 张 PNG 和 315 行脚本，也体现评估与复核成本。没有生产上线收益或完整货币金额。

<a id="devlog-history-26"></a>

## 2026-10-17：声纹接入整集 Refine 后，参考误标和快照刷新比编码耗时更关键

### 1. 问题与现状

10/17–10/18 的 EP04 多轮实验将声纹准备、Refine 和 Index 串起来，暴露出重启、参考库变更引发的 generation refresh、混合 ASR 时间段和静态可见人物误归名。下面记录历史 v4–v11，不把当时方案当成现行生产结论；较新的 offscreen／短句状态见 [voiceprint TRD](../design/02-agent-harness/voiceprint-trd.md)，后续验证由对应日期另记。

### 2. 方案候选

实验依次尝试多参考、取消 prefix gating、合并事件、可信静态声纹＋null speaker、视觉重叠、3/4 理解完成时采样。每次运行存在 prompt、准备时机或参考策略变化，不是只改变一个变量的严格 AB。不能因最终 speaker 改对就忽略其他台词误标，也不能因减少刷新就认定身份更准。

维护成本包括 generation 与参考 identity 的绑定、过期结果拒绝、模型调用 trace、准备阶段对 Refine 的依赖、错误参考向后传播及 mixed-speaker 音频拆分。工具几十毫秒不代表 Agent 准备阶段也短。

### 3. 方案验证

v4 服务重启后失败，understand 24/24、refine 8/22、index 1/24，目标仍 null；288 次 CAM++ 中 1 次失败，冻结的 30 秒预算故障被 Agent 重试遮蔽。v5 完成并将目标从燈纠正为爽世且进入索引；v7 同配置重试仍有大量 refresh。v8 更快却把目标错成爱音，输入 ASR 35.6–48.6 秒是混合语音，而且没有成功的目标比较，不能归为 CAM++ 识别错误。[VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)、[v7 时延探索总结](design-experiment-cleanup.md#v6-网络失败与-v7-性能结论)、[VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)

后续一致性审计发现参考误标：v9 目标错归燈，对本组 mean 0.378263、爽世 0.7157；v10 K5 实际爱音却登记祥子，且 K4/K5 cosine 0.651728，说明简单相似阈值不能保证身份。v11 18/24 chunk 完成后触发，6 人 31 条参考仍有两条已知误标，爽世 6 条中 4 条来自同 chunk，未实现充分覆盖。[VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)

### 4. 指标对比

| 历史条件 | 整集时长 | Refine generations／refresh | Refine CAM++ | 质量边界 |
|---|---:|---:|---|---|
| v5 多参考，无 prefix | 38:43.518 | 54／32 | 950 次、66.113 秒 | 指定目标改对，非全集标注 |
| v7 event merge 重试 | 43:22.011 | 54／32 | 889 次、59.192 秒 | wall 增约 12%，至少 9 次旧参考快照拒绝 |
| v8 trusted static＋null | 30:08.913 | 37／15 | 270 次、14.894 秒 | 更快但指定目标错误 |

v8 声纹 Agent 阶段 6:29.539，准备 encoder 25 次仅 3.914 秒。v11 31 refs 的 465 个 pair 中，30 条可做同组比较，25 条本组最高、5 条其他组更高；这是一致性，不能称 83.3% 身份准确率。known error 移除后仍有错排，误标不是唯一原因。未统一价格／usage 全量口径，不能报告总货币收益。

<a id="devlog-history-27"></a>

## 2026-10-17：CAM++ 的可用性取决于参考音频质量和参考库构成

### 1. 问题与现状

需要用声音证据纠正动漫台词 speaker，先验证本地 CAM++ 是否能区分角色。固定片段的闭集识别很好，并不说明混合说话人、短音频、情绪变化和参考误标都已解决。[声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)

### 2. 方案候选

比较每人单参考、双参考、leave-one-out、多参考等权平均、归一化 centroid、截断时长加权。单条情绪案例可用于诊断，不足以选择全局阈值或认定情绪是因果。维持同等参考预算与排除目标泄漏比单纯增加参考数量更重要。[声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)

架构与维护成本是音频裁剪／解码、参考来源和 PCM hash、已知角色标签、参考重叠与时长控制、聚合分数的解释。cosine 的均值与 centroid 数值不可互换，margin 大也不能证明音频没有混合说话人。

### 3. 方案验证

首轮 4 人各 6 条，共 24 references、92.3 秒；原标签来自 timeline，不是独立专家重标。单参考、双参考、LOO 都是 closed-set 且无拒识机制。随后固定爽世 47.7–48.7 秒目标诊断，再用 3 人各 5 条、不同 chunks 的平衡参考测试。13.5 秒混合音频对爱音 cosine 0.9117、爽世 0.5761，明确展示“最像谁”不能代替 diarization。

### 4. 指标对比

| 项目 | 结果 |
|---|---|
| 单／双参考、LOO | 20/20、16/16、24/24；仅当前 closed-set 样本 |
| 同人／跨人 pair cosine 中位 | 0.6912／0.3925；pair AUC 0.9726，pairs 非独立 |
| 1.7 秒 B12 目标 | 爽世 0.7849，领先 0.2485 |
| CPU encoder／ffmpeg 中位 | 27.06／62.23 ms；首次 import 37.30 秒，重测 2.29 秒；RSS 521.62 MiB |
| 平衡参考固定爽世目标：单参考 margin | 0.021380 |
| 等权 pair mean／centroid／时长加权 margin | 0.152796／0.194285／0.181083 |

单目标对参考选取非常敏感；情绪实验近距离同一句参考还有词句相似混杂。尚无开放集身份准确率、整集 DER 或生产收益，货币费用未测。

<a id="devlog-history-28"></a>

## 2026-10-16：截图找出处的瓶颈包括长链路与错误停止，而不只是检索排序

### 1. 问题与现状

10/16–10/17 的截图实验中，实际 Chat 路径是图片交给 VLM 描述，再用文本 query 检索语义／视觉通道和看候选帧，并不是直接把上传图的 embedding 查向量库。完整画面、咖啡店裁剪和茶杯小裁剪难度不同。[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)

**原问题 C15 的停止方案与验证状态：**

原编号 **C15**。282×214 茶杯局部图样本运行 1200.199 秒后超时，空 Draft 反复被退回，用户只得到开场白。10/18 曾用 deterministic Loop Control 处理并通过单元测试，采用10/18该Loop Control后的茶杯真实样本未重跑；10/19 的 Query-only 收敛又移除了 Evaluator、inner Chat、Result 和 Loop Control，旧设置仅兼容 YAML。因此 10/17 的 stop 提案和 10/18 的两轮控制都不能当作当前停止保证；当前架构的茶杯回归仍无验证证据。

来源：[C15](problems.md)、[perf_plan.md §3.5](perf_plan.md)、[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)。

### 2. 方案候选

候选为扩大任务超时、减少重复检索并明确视觉核验后才停止、直接图像 embedding 找同源。提高 timeout 能让部分请求完成，但没有解决错误候选的最终确认。直接 embedding 的小池实验只验证局部 crop-to-full 关系，未完成全剧同源召回评估。

成本包括持久媒体引用与 hash、候选到原视频帧的可追溯映射、重复检索与多 Agent 回合、无匹配时的停止依据。扩大超时会延长失败等待，不能记为质量提升。

**原问题 C15 的停止方案与验证状态：**

旧方案让 Evaluator 用 complete/continue/stop 表示证据充分、具体下一步、无有效方向。10/18 的历史方案在 single-pass Result 下最多 `result_max_loops=2` 个外层循环，只在 Result 自检或 Evaluator 指出具体缺口、且本轮有进展时重新规划，否则正常交付“尚未核实”。不新增数据库或前端失败状态。

这把退出责任从“草稿空就继续”变成可观察的缺口和进展，降低反复编排成本；代价是提前停止可能漏掉后续可找到的证据，需用困难成功样本同时验证。局部图文字描述的区分度不足与缺逐帧/区域视觉索引是能力边界，不能靠多轮或单枚举解决；缓存命中、候选暂未缩小也不能机械判为无进展。

### 3. 方案验证

6 图分两组：600 秒预算下 chat-06、07 超时；后来 1200 秒预算下 chat-08、09 各返回 3 卡，6 张候选经过独立帧检查。咖啡店裁剪 630×412，最终靠较宽的场景描述找到 EP01 约 17:33。282×214 茶杯裁剪首次 1200 秒超时；第二次更换模型及其他运行条件后虽完成，却选错 EP02 11:38.5，独立源帧不匹配。不能把不同条件两次运行当模型优劣 AB。[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)

直接 embedding 另做 8 次 doubao-embedding-vision-250615 调用和 1 次追加脸部裁剪查询：杯子 crop 对 full 图 top1/7，cos 0.364079，领先第二名 0.032004；反向 full→crop 排末。脸部 crop 对 full 图 cos 0.705202、top1/7，但 margin 仅 0.029257。没有充分 hard negative，部分 crop 精确同帧关系未证实。[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)

**原问题 C15 的停止方案与验证状态：**

失败轮 48 次成功 retrieve、6 轮 Query、5 次 Evaluator，交付候选 423 次、去重 81 Chunk；81 次取帧检查 24 Chunk、256 帧次、131 个不同 SHA，43 次缓存命中。`FRAME_INSPECTION_LIMIT` 与 `VIDEO_CONTEXT_BUDGET_EXCEEDED` 均为 0，没有这些硬限制触发的证据。底层检索原词未留档，不能从 Supervisor 描述复原。10/18 Loop Control 只有单元测试，且10/19已随架构收敛移除；没有当前实现下茶杯定位或正常停止的实跑质量证明。

### 4. 指标对比

| 条件 | 时长 | 结果／限制 |
|---|---:|---|
| 6 图前两组，600 秒预算 | 约 600 秒／组 | 超时，无最终卡；子检索成功不代表任务完成 |
| chat-08／09，1200 秒预算 | 706.510／789.081 秒 | 各 3 卡；非固定资源基准 |
| 咖啡店裁剪 | 236.994 秒 | 1 个正确来源；6 次 query |
| 茶杯 run1 | 1200.199 秒 | 超时；423 次候选交付、81 chunks、256 帧／131 hashes |
| 茶杯 run2 | 1107.097 秒 | 1 张错误卡；实际看帧仅 2 个 chunks |
| 杯子直接 embedding 8 次 | 合计 4.990 秒，均值 609.992 ms | 只测 7 候选小池；input 9,662 tokens（image 9,558、text 104） |

茶杯 run1 的 retrieve 合计 25.402 秒、看帧 21.939 秒、read canvas 12.301 秒，不能把 20 分钟主要耗时归给向量检索。货币总成本未齐，直接 embedding 也尚未接入该 Chat 实验。

**原问题 C15 的停止方案与验证状态：**

旧失败样本墙钟1200.199秒；同条另有旧架构run2的1107.097秒错误结果。采用10/18 Loop Control后的复测，以及10/19 Query-only移除它后的当前架构回归，均没有茶杯样本的墙钟、定位成功率、诚实无结果率或费用证据。历史外层上限 2 不再是当前实现保证；即使在当时，也不能直接换算成固定响应时间或证明单轮内部工具循环已收敛。茶杯失败、咖啡店扩展场景后成功与六图样本应共同作为回归集。

<a id="devlog-history-29"></a>

## 2026-10-16：匿名说话人、补帧与 Prefix 能否修正 EP04 身份

### 1. 问题与现状

EP04 47–49 秒“祥ちゃんも悪くない”的人工确认说话人是长崎爽世；原解析错认成高松灯。上游 ASR 曾把 35.5–48.7 秒合为同一 speaker，目标短句没有独立切开，后续模型容易把连续音频与画面中的人物混为一人。错误会污染人物对白检索与引用。

### 2. 方案候选

已尝试匿名 speaker 标注、Refine 读取补充帧、以及通过 Prefix 向后续 Prefix 传递 compact。匿名 ID 只在 chunk 内有效；补帧只能补画面证据；Prefix compact 是滚动上下文，不等于重新切分音轨。增加阶段和工具扩大调度、版本冲突及证据来源的维护负担，也要求调用方分清画面推断与声音身份。

### 3. 方案验证

EP01 匿名标注审计覆盖 24 个 chunk；EP04 v1 完成全链路，但目标由错名变成 null，补帧后的 Chat“应该是爽世”属于叙事推断，不能当音频身份已修复。v2 补帧运行在 API 重启后发生 lease 过期，最终 PARSING_FAILED。v3 Prefix 的报告仍为进行中；后续 effect summary 只证明已观察到的 9 次 commit，不能补写成全片完成。

证据：[匿名 ASR 汇总](../../outputs/verification/asr-speaker-ep01-20261016-142829/summary.json)、[v1 身份复核](../../outputs/verification/mygo-ep04-speaker-review-20261016/report.md)、[v2 补帧失败](../../outputs/verification/mygo-ep04-refine-frames-20261016/report.md)、[Prefix 报告](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/report.md)、[Prefix 已观察影响](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/prefix-effect-summary.json)。

### 4. 指标对比

- 匿名 ASR：24/24 chunk，171/171 cues 有 speaker 标签，17 个 chunk 多 speaker、3 个为空；没有人工身份真值，标签覆盖不是身份准确率。生产视频请求总计 639,034 token，音频对照只有 2 次请求、3,716 token，覆盖不同，不能直接比较节省比例。
- EP04 v1：24/24 chunk、22/22 Refine、24/24 index 完成，有效运行约 14 分 41 秒；目标身份仍未正确落地。一个慢窗口因版本冲突执行 5 次 Astra，provider retry_count 为 0。
- EP04 v2：运行约 11 分 05 秒，Understanding 13/24、Refine 8/22、index 4/24；8 个窗口已有 141 次成功帧读取，目标仍错认。10 个在途调用随停机取消，不应计成供应商失败。
- Prefix 已观察 9 次 commit，其中 3 次发生内容变化；早期窗口曾把 9 个有名 speaker 变成 null，正确性未验证。多阶段增加了执行与追踪成本，尚无证据证明本轮身份问题得到解决。

<a id="devlog-history-30"></a>

## 2026-10-16：GroundingDINO 可辅助主体布局，但不是构图与线段识别器

### 1. 问题与现状

需要在动漫画面中获得人物和物体位置，辅助构图检索。GroundingDINO Tiny 在本机可运行，但目标框、构图类别、主要线段、机位和人物身份是不同输出。[GroundingDINO 退役摘要](design-experiment-cleanup.md#groundingdino-检测)

### 2. 方案候选

比较 CPU/MPS、源图分辨率、人物／建筑／布局提示词，以及助手目视几何描述与检测框。模型框可作为布局辅助；“上下分区”的 split_y 是后处理推断，不能归为模型直接理解。

具体成本为约 658 MiB 权重、Torch/MPS 环境、预处理与坐标还原、prompt／阈值／NMS 版本维护；一套框不能替代身份或构图标注。源图变小也不保证推理变便宜。

### 3. 方案验证

6 张 EP01 1920×1080 图，6×3 次计时和 2 次 warmup。另用 360/480/720/1080 图比较，发现默认 processor 都变成 1333×750 tensor；室内图比较 3 组提示词；水族馆单图先冻结助手的 6 条线和 2 人框，再比较检测器。助手标注不是专家 ground truth，也没有标准 AP。[GroundingDINO 退役摘要](design-experiment-cleanup.md#groundingdino-检测)

### 4. 指标对比

| 项目 | 实测 |
|---|---|
| 6 图人物目视检查 | 19 个可见人物中检出 18 个；非标准 recall |
| MPS／CPU pipeline 中位 | 784.81／1999.66 ms |
| MPS／CPU forward 中位 | 736.84／1973.84 ms |
| MPS／CPU RSS | 0.63／2.86 GiB；MPS driver 3.25 GiB 不能直接与 RSS 相加 |
| 360／480／720／1080 源图 pipeline | 721.18／683.88／698.69／694.58 ms；18 个 baseline 人物均匹配 |
| 室内 baseline／建筑／布局 region prompt | 9／17／0 框；553.39／735.15／709.03 ms |
| 水族馆人物／结构 prompt | 7／11 框，737.9／778.1 ms；两个人框 IoU 约 0.76–0.97 |

低分辨率多出的框包含重复与真实新增，不能把框数提高当准确率提升。当前证据只支持辅助几何候选，未验证生产构图检索收益。

<a id="devlog-history-31"></a>

## 2026-10-16：Chat 终止错误及时上抛与跨进程 PROCESSING 遗留

### 1. 问题与现状

原编号 **B01、C13**。PROCESSING 已持久化而执行仅在内存，进程退出或终态写库失败后可能永久阻塞同一对话；409 单执行门禁有必要，缺的是遗留状态收尾。10 月 16 日已有手动停止、进程内超时和错误失败路径；跨实例持久截止时间及独立过期扫描未完成。C13 的 Codex fatal 通知传播已在 `dd622aa` 提交、重启并真实复测，计时口径问题仍未解决。

来源：[B01/C13](problems.md)、[Codex client](../../gateway/codex/client.py)、[Chat Service](../../server/service/agent_chat.py)、[消息 Repository](../../server/repository/agent.py)、[真实计时](../../outputs/verification/mygo-ep04-speaker-review-20261016/comparison-summary.json)。

### 2. 方案候选

已实现订阅结构化 ErrorNotification、匹配 thread/turn，fatal 或认证错误及时抛出，保留可重试语义，关闭无限连接重试；沿既有 FAILED 状态提交后发布。失败也写不进库则发 interrupted 促使读快照，不假装持久终态。未实施候选是不可变持久 deadline、过期失败收尾及拒绝迟到提交，不是 checkpoint 自动续跑。

复用已有状态降低接口迁移成本，持久扫描仍需处理锁顺序、幂等重放与终态竞争。阶段累计时间和墙钟并存会增加用户认知负担，应明确标注，不能用一个短数字替代等待体验。

### 3. 方案验证

旧约 10 分钟样本在初始连接已有 CONNECT 403，Query/Evaluator 和检索尚未开始，不能归因召回过多或额度。真实 SDK 通知替身下 fatal 无 completed 也在不足 3 秒保存 FAILED；早到/异 thread/旧 turn/可重试 503 有回归。历史提交树后端 1808 passed/56 skipped、前端 398 passed，typecheck/build 通过；随后连通探针及两次真实 Astra Chat 成功，不等于定位了旧代理拒绝策略。

### 4. 指标对比

第一真实 EP04 样本阶段累计 `131795ms`、请求起点至持久化更新时间 `197289ms`，差额未逐段归因；第二问 `157659/157662ms` 接近。前者不能报告用户只等两分钟，也不能把差额全部叫推理。跨进程遗留消息自动恢复率及 deadline/reaper 方案效果未知。

<a id="devlog-history-32"></a>

## 2026-10-15：9 条真实 Chat 暴露了证据覆盖和上下文传递问题

### 1. 问题与现状

13 集 ready 的 MyGO scope 上，9 次请求、8 个 conversation 均技术完成，但返回 COMPLETED 不等于语义任务完成。任务覆盖人物关系、精彩／搞笑片段、全剧归纳、动作、短台词和当前播放位置。T01 存在无充分证据的心理解释及午餐卡遗漏，T02 追问才补；T07 短台词检索漏掉此前 T04 已出现的 EP04 17:11.2–17:11.6 立希「は？」。[总报告](../../evals/benchmark/query_capabilities/README.md#历史结论chat)、[历史回答与基线](../../evals/benchmark/query_capabilities/baseline.jsonl)

### 2. 方案候选

保持当前多 Agent 检索链作为基线；可选改进是显式传递播放锚点与 conversation context、区分全量覆盖与局部 TopK、让证据与最终引用逐项对应。T08 的嵌套 query 缺当前播放位置，T09 在文字中明确场景后得到更具体的检索链；这不是隔离其他变量的 AB，不能量化某一个改动的收益。

维护成本集中在跨 Agent context 合同、候选／已读证据／最终视频卡的区分、评审轮次和覆盖证明。仅把最终状态或卡数当质量指标会遮蔽漏项。

### 3. 方案验证

使用 gpt-6-astra，两个 worker 的任务并发，峰值模型并行 4；因此不是隔离资源的性能基准。原实验逐条保存回复、tool trace、usage 和源数据索引；2026-10-24 已将问题、历史回答、用量及关键诊断收敛为 benchmark，旧 deep trace 已退役。本次归档没有重跑或全量重解析。[运行汇总](../../evals/benchmark/query_capabilities/baseline.jsonl)、[诊断摘要](../../evals/benchmark/query_capabilities/baseline.jsonl)、[归档范围](../../evals/benchmark/query_capabilities/README.md#归档与清理边界)

### 4. 指标对比

| Case／任务 | retrieve 次数 | 候选数 | 最终卡数 | 全程秒 |
|---|---:|---:|---:|---:|
| T01 关系 | 1 | 8 | 2 | 215.6 |
| T02 关系追问 | 1 | 5 | 3 | 215.1 |
| T03 精彩片段 | 1 | 8 | 3 | 188.7 |
| T04 搞笑片段 | 1 | 8 | 3 | 204.4 |
| T05 全剧问题 | 3 | 17 | 5 | 277.4 |
| T06 动作 | 1 | 8 | 1 | 108.6 |
| T07 短台词 | 3 | 18 | 1 | 154.0 |
| T08 播放锚点 | 1 | 1 | 1 | 91.2 |
| T09 显式场景 | 5 | 28 | 4 | 249.2 |

中位 204.4 秒，范围 91.2–277.4 秒；汇总 56 次模型调用，input 10,708,975、output 50,805、total 10,759,780 tokens。价格／cache 计价资料不完整，金额为 unavailable。partial=true 指向注册关键帧位置不全，不能解释成 13 集没有解析。没有全量人工 ground truth，不能报告全集 recall。

<a id="devlog-history-33"></a>

## 2026-10-15—10-16：图片查询附件承载与真实视觉检索缺口

### 1. 问题与现状

用户希望上传表情包找出处，原Chat只有文字和历史视频引用；附件存储、消息恢复、模型看图与视觉索引查询是不同环节。后端能上传图片不等于Agent已读取或能找到正确片段。

### 2. 方案候选

文字输入继续多通道检索；媒体输入保留真实图像编码的独立路径，而非先强制转文字。首轮只做静态图的会话受管上传、权限、持久化和恢复，GIF/MP4与视觉召回分批。复用现有图片配置与ArtifactStore减少重复机制，但增加附件授权、删除竞争和历史恢复合同。

### 3. 方案验证

[视觉检索TRD](../design/02-agent-harness/visual-retrieval-trd.md)记录10/16后端交付；[Event早期方案](../design/02-agent-harness/event-retrieval-trd.md)已被Sub-Chunk合同替代，输入模态与检索粒度分别评估。8个后端文件511 passed / 1 skipped，隔离MySQL附件迁移/发布锁2 passed；开发库31会话、72消息旧字段逐项不变，72条旧消息附件为NULL。

### 4. 指标对比

附件承载从缺失到有后端合同与迁移证据；该批前端/算法未接入，返回ATTACHMENT_INPUT_UNAVAILABLE而不假装已看图。真实图片找出处的Hit@K、答案正确率、端到端时间和模型费用均未测。后续图搜实验属于算法候选，不自动关闭Chat上传→召回→播放的完整产品验收。

<a id="devlog-history-34"></a>

## 2026-10-14：并发上传完成的父记录锁升级死锁

### 1. 问题与现状

原编号 **B09**。13 文件批量上传中最初 3 个并发完成，两个请求在提交前的 CREATING_JOB 遇 MySQL 1213；媒体已校验，但数据库事务未完成。根因是外键先取得父 Series 的 S 锁，随后同时升级 X 锁。最小修复及真实隔离 MySQL 回归已完成；当时运行 API 未重启，未重新上传原 13 视频。

来源：[B09](problems.md)、[Upload Repository](../../server/repository/upload.py)、[Upload 回归](../../tests/server/test_upload_service.py)。

### 2. 方案候选

保留 UploadSession→Series 顺序，在插入 Video 前先取得 Series X 锁，使同剧上传完成的 DB 事务串行；分片传输、合并与媒体探测仍在事务外。只改 12 行新增/5 行删除，不新增事务重试或修改前端并发。候选通用重试与全链锁顺序重整未一并实施。

降低父锁升级风险，代价是同 Series 完成事务需排队；已有 create_session 的另一路取锁顺序仍可能独立竞争，不能宣称消除所有死锁。扩大为全局队列会增加维护成本，当前证据只支持局部锁时序调整。

### 3. 方案验证

87 项 Upload 回归通过。新增 3 独立连接并发完成的 V1/V2 用例，隔离加载旧实现为 2 failed，均复现 1213；修复后连同删除、序号和租约等共 6 项真实 MySQL 回归通过。使用临时独立库并清理，未触碰业务库或调用模型。

### 4. 指标对比

旧实现 2/2 竞争用例失败，修复后对应路径通过；真实上传的死锁率、并发事务等待时间、吞吐和用户成功率前后值未知。回归有效性证明针对的锁升级缺陷，不等于全系统并发风险清零。

<a id="devlog-history-35"></a>

## 2026-10-14—2026-10-16：Supervisor 最终提交、视频证据展示与公开表达

### 1. 问题与现状

原编号 **C12、F06、C05**。C12 过去由程序强制沿用内层草稿，Supervisor 无法真正修改 claim/范围；现已补显式最终提交，离线链路验证完成，真实选段语义待验收。F06 已移除把 BM25 姓名命中当 claim 证据的公开展示，并用最终范围原视频预览；C05 的“按获批草稿/视频块”等内部过程语言仍是待定位的历史问题，源阶段未确定，不能假装全部由 UI 修好。

来源：[C12/F06/C05](problems.md)、[Supervisor](../../agent/chat/supervisor.py)、[输出组装](../../agent/chat/video_search/output.py)、[SearchPage](../../web/src/features/search/SearchPage.tsx)。原条的 `agent/chat/video_search/video_search_canvas.py` 是已迁移的历史路径。

### 2. 方案候选

内层答案作为草稿，Supervisor 读冻结的审核证据后决定正文、claim 和播放范围；合法范围改写不受旧草稿原文/边界限制，继续校验来源、租户、版本、canonical 时间。相邻两 Chunk 可支持有完整身份和连续性证据的范围。最终 SQL、REST、SSE 和下轮引用保持同版本；纯文字答复可不提交视频。

展示清空公开 matchedEvidence/lexicalMatches，但内部诊断保留。缩略图可为空而不把 READY 改为 partial，毫秒用于播放、mm:ss 用于展示。显式最终提交增加一层提交与冻结契约，收益是责任明确；不能把媒体预览缺口、选段质量和内部话术混作同一修复，C05 应先追实际输入输出。

### 3. 方案验证

离线 Runtime/Canvas/Service 验证真实组装链，模型为替身；未跑真实模型、未重启或迁移。覆盖合法扩缩范围、撤回全部视频、纯文字、缺证据、跨租户/越界拒绝以及取消时不发布草稿。历史整分钟 04:00–05:00 合法范围并不单独证明选段错误。F06 没有真实浏览器截图/播放位置验收记录，C05 没有归因或修复结果。

### 4. 指标对比

结构合同与展示变化有实现证据；真实选段精度、卡片理解成本、媒体首帧时间、C05 内部话术发生率和用户体验对照均未量化。源码边界通过不能等同剧情语义正确。

<a id="devlog-history-36"></a>

## 2026-10-14—2026-10-16：Parse 超时、停服和租约恢复不能替代实际执行退出

### 1. 问题与现状

原编号 **B10、B11**。计划停服会消耗业务失败预算，父任务阶段收尾还可能被全局视频槽占用；阶段 deadline 后，等待取消不代表底层线程/SDK/远端请求已停止，租约清空后重试可能与旧调用重叠。B10/B11 在原文仍待完整修复，不能因手动恢复部分任务或晚到结果被拒绝而称生命周期已闭环。

来源：[B10/B11](problems.md)、[ExecutionCalls](../../server/utils/execution.py)、[Consumer](../../server/mq/video_parse_consumer.py)、[Queue](../../server/mq/video_parse_queue.py)、[实时解析候选设计](../design/02-agent-harness/realtime-parse-trd.md)。

### 2. 方案候选

统一正常停服、阶段 deadline、lease expiry/reaper、已领取未调用以及 pending execution 的恢复语义；明确并发名额限制可领取任务还是仍在实际运行的调用。评估可取消客户端、隔离进程与供应商任务 ID 的查询/复用，继续保留 lease fence 和版本提交校验。仅延迟 `retry_chunk()` 不够，reaper 仍可能重派；改 Redis 也不会自动停止旧线程/远端计算。

隔离进程与持久执行回执会增加资源归属、退出和恢复维护成本，但只靠数据库 RUNNING 计数会掩盖重叠。原则是先修现有生命周期，再决定是否增加队列基础设施；本地进程退出也不能保证远端不计费。

### 3. 方案验证

离线真实 realtime/Consumer 路径以可控不响应取消的线程复现：50ms deadline 后已调用 retry，Provider 仍运行，ExecutionCalls.pending=true；207 项现有回归通过不能否定这个反例。10 月 16 日实际停机后 7 Chunk 以 worker lease expired 失败，max_retry_count=0 导致预算耗尽，13 Chunk/8 Refine 已提交成果保留。旧租约未重领与 7 条 pending 撤销记录吻合，但日志无 execution ID，不能逐条证明线程结束时刻。

10 月 14 日曾用既有入口恢复部分任务调度，后续供应商欠费失败是另一事实，不能拿来验证停服修复。专用 ASR 调研已查官方接口/历史价格，未接入或对 MYGO 做质量对照；该候选可并入 ASR 主题。

### 4. 指标对比

现有证据确认重叠可能性与 7 子任务恢复失败，真实远端重叠时长、额外费用、修复后恢复率未知。B10 的 13 集用量审计为 ASR 377 请求：280 有完整成功 usage、94 error、3 无完整终态；已记录成功估算约 ¥6.48，Embedding 已记录约 ¥31.96，均非账号账单。13 集 5.12923 音频小时的一次专用转写预算按 10/14 资料约 ¥1.48/¥4.06/¥4.10；质量、重试和输入不同，不能直接相减为节省。30 次重复成功 ASR 估算约 ¥0.70；替换 ASR 不会减少索引 Embedding 费用。

<a id="devlog-history-37"></a>

## 2026-10-13：多模态检索应围绕用户任务和证据组织，而不是先增加模型

### 1. 问题与现状

10/13–10/18 的设计讨论逐步区分“同源”“同一实体”“特征相似”“事件解释”“声音事件”。同样输入一张图片，找原片与找类似构图的成功标准不同；“首次”“全部”“后来”又涉及范围和故事／播放顺序。TopK 的局部合理候选不能自动支持整集、全剧或因果解释。[检索 TRD](../design/02-agent-harness/multimodel-retrieve-trd.md)、[多模态研究（10/17 初稿、10/18 更新）](../design/02-agent-harness/多模态理解解析研究.md)

### 2. 方案候选

保留平面 TopK 基线；候选包括分层摘要／章节 map-reduce、关系或时间图、单 Agent 自审与独立 Evaluator、密集／场景采样加按需回看。构图以类别相似为一般目标，但 query 明确的“人物在左”“俯视小人物”仍是约束。VBench、RAPTOR、GraphRAG、PICD、CADB 等仅用于问题拆解或方案参考，不是本项目已经跑出的指标；PICD 24 类也没有被当作现成可接入分类器。

新增方案的具体成本包括摘要与原始证据的版本同步、时间关系的标注与冲突处理、额外索引及模型依赖、Evaluator 的模型轮次、采样覆盖的可审计性。引入图数据库或更多专用模型尚无本地收益证据。

### 3. 方案验证

文档提出四类受控比较：相同预算的自审／独立评审／按需调用；含闪回与无答案的时间问题；平面 TopK／摘要／全章节覆盖；32 帧基线／密集采样／按需补看。评估应分别定位输入是否看见、解析是否表达、检索是否返回、答案是否正确引用。相关构图、声纹和 Event 实验分别记在下文，不能把研究引用计作产品实测。

### 4. 指标对比

文档整理 35 类目标 query 和多个可观察特征，属于需求覆盖数量。上述架构候选没有共同测试集、同预算端到端 AB、全集 recall 或成本收益实测，结论为**未验证**。保留当前基线和明确证据单位，是后续实验能比较的前提。

<a id="devlog-history-38"></a>

## 2026-10-12：索引发布幂等与浮点精度诊断分开处理

### 1. 问题与现状

原编号 **B08**。索引已有相同版本时仍可能重复 embedding；同时调查过 SQL JSON、向量持久化和 ES 的浮点差异。这是重复成本与数值合同两条问题。版本身份/查询可见性幂等策略已实施，embedding 字段拆分仍待处理；用户当时选择不推进精度修复，不能把早先保留的 StringEncodedJSON 尝试当作本轮已部署方案。

来源：[B08](problems.md)、[索引协调器](../../server/service/video_parse_index_coordinator.py)、[ES 存储](../../server/storage/elasticsearch.py)。

### 2. 方案候选

采用 tenant/series/video/task/chunk、content version 和 index generation 的完整身份：同版本且发布查询可见则复用，缺失/更低版本发布，更高版本拒绝覆盖。将正文与向量/重建状态分离是候选，避免读写大 JSON；浮点改存储需独立证明同调用原始向量→SQL→ES 的完整链。

幂等增加版本和可见性判定，但复用既有身份防止重复编码；拆字段需迁移并维护两处生命周期，不能仅凭一处小数显示变化引入。数值兼容、模型差异和序列化差异须分层诊断。

### 3. 方案验证

原记录确认一个 binary64 ULP 的序列化差异，而非证明模型/ES 内容损坏；未保留大范围三方同调用原始向量完整链，较大差异来源仍未知。版本策略验证为存储/Coordinator 99 passed、Worker 索引 7 passed、真实随机隔离 ES 索引 8 passed，无模型调用；该轮未恢复真实任务。10 月 12 日 EP03 后续正式发布/可见性完成与 B07 对应，不能将整集提速全部归给幂等。

### 4. 指标对比

同版本重复调用的程序路径已避免；实际节省多少 embedding 请求、费用与墙钟未单独测量。五组 1024 维向量有限扰动中 cosine 最大变化约 1.11e-16，不保证近乎同分排序均不变；ULP 不是召回质量指标，改存储前后召回率无完整对照。跨 Refine/Index 共用 retry_count、历史 bulk item 日志不足与同版本缓存覆盖候选路径仍是原条独立未闭环项。

<a id="devlog-history-39"></a>

## 2026-10-11：嵌套图 Checkpointer、运行配置与实时视频数据失败的分层归因

### 1. 问题与现状

原编号 **C08、C09、C10、C11**。C08 是业务图继承 Supervisor 的 InMemorySaver 引发序列化失败，已修；C11 是旧 API 进程未加载新配置，重载后复测通过。C09 的真实 SDK 暴露 default general-purpose task 与假模型预期不同，尚未定位；C10 的实时理解 partial 来自三个相邻失败 Chunk，主 Parse 已失败，根因未逐个追完。四者不可统一写作“Chat 工具坏了”。

来源：[C08–C11](problems.md)、[Chat Runtime](../../agent/chat/video_search/runtime.py)、[实时 Parse TRD](../design/02-agent-harness/realtime-parse-trd.md)。

### 2. 方案候选

业务子图显式 `compile(checkpointer=False)`，保留 Supervisor 自己的会话恢复；部署时核对进程启动时间、源码/配置生效，再评估模型支持。C09 应先核对真实 SDK/profile 装配而非重写所有 SubAgent；C10 要追任务/Chunk 数据失败，不以 C08 的图修复替代。

边界显式化降低隐式继承认知成本；同时保留 checkpoint、provider capability、工具配置和数据状态四条诊断线，增加一点记录成本，避免跨层大改。

### 3. 方案验证

C08 原工具约 7ms 内 TypeError、整轮 996ms；修复 219 + 37 项离线回归通过，API 加载后真实当前视频 Chat 完成但有 C10 的 partial。C11 的 PID 启动早于代码/配置，旧 vision=openai 请求约 12.6ms 即 400；重载后真实 API Chat 通过，浏览器/播放定位未验。C09 只观察到工具暴露，未实际调用该 default task；C10 涉及 0–60/60–120/120–180 秒三个失败 Chunk，不是假定底层请求必然同因。

### 4. 指标对比

图序列化和旧配置的失败路径有前后行为证据；无稳态时延/成本量化。C09 的实际泄露工具调用数、C10 的成功修复比例及浏览器端体验仍未知，不能据一次 Chat 完成把四个编号同时关闭。

<a id="devlog-history-40"></a>

## 2026-10-10：BaseModel 字段与时间合同迁移

### 1. 问题与现状

系统审计时间、媒体时间及PK/FK命名分散，使迁移和跨层交接容易混淆；旧表与新模型不一致会导致启动校验或数据读取失败。目标是统一合同并保留业务值，而非重写历史媒体时间。

### 2. 方案候选

复用BaseModel及明确字段映射，用显式迁移和拒绝异常预检保证一致；不把媒体毫秒自动换算，也不静默接纳半迁移结构。统一命名减少理解成本，但一次性Schema改动与历史兼容增加迁移维护成本。

### 3. 方案验证

[字段命名合同与迁移验证](../design/02-agent-harness/base-model-field-names.md)在隔离MySQL 8.4.11验证空库、带数据迁移、全行保留、重复执行拒绝和异常预检。该批未执行业务数据库迁移，不把后续业务库状态反写为当时完成。

### 4. 指标对比

旧24表/323列/29行迁到24表/390列；315个业务字段及PK/FK/JSON/媒体数值保留，58组审计映射正确。真实MySQL 25 passed，异常预检18/18；Server全量1433 passed / 80 skipped / 12 failed，12项与旧Agent工具替身基线一致。该结果支持隔离迁移正确性，不代表全回归零失败；业务时延、费用及维护工时未测。

<a id="devlog-history-41"></a>

## 2026-10-10—10-21：游客、个人资源、共享与 Local/Cloud 边界

### 1. 问题与现状

本地owner与云游客权限不同；用户需要游客浏览公开内容、邮箱登录后管理个人资料，并自主公开Series。早期平台租户发布方式不能直接支持跨用户只读共享；地址可达、登录身份和数据来源又容易混为一谈。

### 2. 方案候选

首版选择User/Tenant、Redis邮箱OTP与服务端权限，不增加Workspace实体、独立设备会话或团队成员表；Series Sharing以private/public和来源Tenant授权实现共享，不设“所有人默认同租户”。local与cloud用独立进程、数据库、索引、媒体和凭证边界，自动同步和团队成员留后续。

### 3. 方案验证

来源：[身份TRD](../design/02-agent-harness/user-tenant-trd.md)、[身份交接](../report/server/user-tenant-handoff-20261010.md)、[Series共享TRD](../design/02-agent-harness/series-sharing-trd.md)、[Local/Cloud设计](../design/02-agent-harness/local-cloud-trd.md)。身份阶段分隔离MySQL/Redis、模拟浏览器与真实HTTPS/OTP流程验证；本机共享迁移不等于手机cloud数据库已迁移。

**材料冲突：**Local/Cloud 10/21仍写“普通用户public/private尚未实现”，Sharing明确记录10/19 phase1本机后前端及开发库迁移完成、独立phone/cloud库未迁移。分别保留代码/本机与cloud环境状态；不能据旧状态认定功能全无，也不能认定所有部署均已支持。

### 4. 指标对比

首版身份记录Python2617 passed / 83 skipped、Web229项及2个模拟浏览器用例；隔离SQL22→24表。后续HTTPS/验证码注册、再次登录、权限及退出记录31项通过，手机真实视频业务、正式SMTP、完整个人上传解析检索闭环不能由此推定完成。Sharing文档确认phase1本机交付，phase2团队仍为提案；未记录共享带来的访问转化、等待时长或活跃使用收益。

减少实体和共享规则有利于理解；新增认证、撤回、多入口授权、跨实例配置与缓存隔离带来持续维护成本。文档状态冲突本身也是认知成本，归档明确环境但本轮不修改权限或迁移。

<a id="devlog-history-42"></a>

## 2026-10-10—10-21：本地 Codex/ACP 与云端 Pi Host 的职责拆分

### 1. 问题与现状

本地coding runtime的一会话一进程模式不能直接当作受限云服务器多用户方案；内置Supervisor、外部ACP与云端Pi的规划和实施进度混写，容易误判已具备会话恢复、BYOK或容量保障。

### 2. 方案候选

10/10历史方案分别考虑内置Deep Agents与原生Codex/Claude ACP接入；10/21产品决定以连接用户自带Codex/Claude为本地目标，旧“local默认Deep Agents”不再是当前产品默认。云端采用一个Node Pi Host承载独立会话/凭证，Python保留权限和业务提交；各路径复用已有工具和SQL消息合同。避免另造通用harness，代价是SDK/进程生命周期、JSONL协议、确认/丢弃与持久恢复的维护。

### 3. 方案验证

[本地Bridge TRD](../design/02-agent-harness/codex-bridge-trd.md)、[旧阶段表](../report/gateway/bridge/agent-bridge-stages.md)、[Pi Host TRD](../design/02-agent-harness/pi-host-trd.md)对读。旧P01“未开始”是原规划，10/21本地文档已核对接线文件存在；存在不等于完整Bridge或真实迁移验收。Pi通过真实Python→Node→本机fake API验证，不调用外部模型。

### 4. 指标对比

Pi相关Python520 passed / 1 skipped / 1 deselected，Node24 passed，构建/类型检查通过；H01协议与隔离通过，H02接线完成但用户级模型配置管理未完成，H03目标4CPU/8GB容量验收未做。共享SDK代码和资源限制是设计/实现收益，RSS、并发容量、p95、真实模型等待与费用均无实测对照。旧本地阶段表和新Pi状态分开，不宣称全部接入完成。

<a id="devlog-history-43"></a>

## 2026-10-09：视觉推理强度与 Refine 全量事件丢失

### 1. 问题与现状

90.132 秒视频的 none/low reasoning 对照原本要判断解析质量和成本。两个配置都能通过工程校验，但 low 的 Refine 把局部更新当成完整 timeline 替换，导致已经识别的内容大量消失。产品影响是视频可以显示解析成功，实际对白与事件覆盖却不完整。

### 2. 方案候选

候选是在同一冻结源码、同一输入和相同 65,536 输出预算下切换 none→low，并检查阶段间事件保留。提高推理强度增加模型时间与输出量；采用显式局部更新、保留未提及事件是针对丢失问题的后续方向，不能用更强模型替代写入语义正确性。本轮对照本身未证明该写入问题已经修好。

### 3. 方案验证

冻结的 542 个源文件仅 reasoning 配置不同。两臂各 10 次调用、1 次失败，均出现内容过滤与 fallback；只有一组运行，不能把全部耗时差都归因于 reasoning。SQL duration 为 0，以下采用 driver wall time；阶段有并行，不能相加作为总耗时。证据：[none/low 对照](../../outputs/verification/vision-controls-low-20261009/none-low-comparison.md)。

### 4. 指标对比

| 指标 | none | low |
| --- | ---: | ---: |
| driver 耗时 | 100.285 秒 | 661.334 秒 |
| 总 token | 193,985 | 232,369 |
| 输出 token | 6,843 | 45,774 |
| Understanding→Refine events | 29→29 | 36→2 |
| Understanding→Refine dialogues | 28→28 | 21→2 |
| 工程校验 | 31/31 | 34/34 |

low 丢失 34/36 events、19/21 dialogues，语义验收为 FAILED。该例没有显示推理加深的业务正收益；暴露了“工程通过≠内容完整”的验证成本。新增阶段差分审计便于定位，但其维护成本与人工复核时间没有量化。

<a id="devlog-history-44"></a>

## 2026-10-09：ASR 秒制输出与全局毫秒边界修复

### 1. 问题与现状

一次90.132秒双Chunk验收虽完成统计和整块播放，第二段多句对白却被挤进60004–60030ms的26ms；这会让逐句播放和事件定位失真。毫秒存储并不意味着模型具备毫秒识别精度。

### 2. 方案候选

在模型输出边界接收片段内秒数，由既有转换器乘1000、round和裁剪，宿主只加一次全局offset；理解/精修明确消费全局毫秒。选择边界归一而非猜测并修改历史数据，保留既有公开/持久化合同；不把整条链路重复乘除1000。

### 3. 方案验证

先做ASR局部诊断和91项直接回归，再以同一原片及默认模型/fps/重试/fallback完整重跑，核对SQL、媒体DTO、receipt与ES版本。来源：[ASR修复和完整重跑](../report/server/model-usage-handoff-20261008.md)。旧26ms失败记录保留，新运行不追认旧数据正确。

### 4. 指标对比

第二段13句从原错误26ms集中范围恢复到64400–90132ms、跨度25,732ms；13/13相对ASR匹配，第一段15句匹配14句，短句“はい。”未独立认证。parent、两child和Interval COMPLETE，2receipt/2ES版本一致，实际Range播放206。末句估计超出片段168ms，裁剪证据保留；毫秒级音轨真值与逐句声学准确性仍未认证。

增加一个明确响应转换边界，减少上下游单位猜测；仍需维护秒/毫秒及offset合同。没有同口径端到端耗时或费用对照，不能将恢复可用性写成速度收益。

<a id="devlog-history-45"></a>

## 2026-10-07—10-09：模型用量账本、失败计费与可观测性

### 1. 问题与现状

各模型路径用量粒度不同、失败与缺失 usage 容易被漏记，产品无法可信展示解析/Chat 成本或解释预算。追踪平台成功接收请求也不能代替本地账本一致性和完整业务闭环。

### 2. 方案候选

Gateway 统一调用记录与上下文，Server 按稳定 call_id 入账和业务归属；Langfuse 作为外部追踪而非账本真相。关闭机制比较直接依赖 SDK flush 与隔离 OTLP 出口，后者用于控制自身生命周期。未知保持未知，失败调用也保留，避免用零伪装缺失或重复估算费用。

### 3. 方案验证

[Server 用量 TRD](../design/02-agent-harness/model-token-usage.md)、[阶段证据](../report/server/model-token-usage-stages-evidence.md)、[最新交接](../report/server/model-usage-handoff-20261008.md)、[Gateway TRD](../design/02-agent-harness/gateway-model-observability-trd.md)和[追踪验证](../report/gateway/model-observability-stages-evidence.md)分别记录账本与追踪。读取的是历史验收；部分 `/private/tmp` 原始证据已失效，不能直接承诺原命令可复跑。

### 4. 指标对比

| 验证层 | 已记录结果 | 边界 |
|---|---|---|
| Gateway 离线组合 | 378 passed / 1 skipped；其中公共记录41、Adapter153、追踪61存在交集 | 真实 Langfuse 项目未提供，不能称云端可见性通过 |
| 当时 Server 追踪接线 | 隔离118 passed / 1 skipped / 2 failed；普通收集4 errors | Engine 导出及既有身份合同问题未消除，非普通全链通过 |
| 双 Chunk、Parse + 三轮 Chat | 99 calls；已知小计1,204,853 tokens、missing2；92 completed / 7 failed | 缺失时公开总量为未知，失败已知175,035 tokens保留；不是零成本失败 |
| 随后同默认配置完整 Parse | 14 calls / 443,098 tokens / missing0；11 completed / 3 failed；失败167,520 tokens入账 | budget used14与SQL14一致；该次不含新Chat/UI验收 |

收益是可追溯与避免漏账，不是模型费用下降；增加统一记录、异步归属、导出与关闭生命周期的维护成本。真实 provider、SDK run 与逐 inference 粒度不同，不能合并成一个调用数；后续 Canvas/Chat 费用估算沿用此边界。

<a id="devlog-history-46"></a>

## 2026-10-02—10-06：VP2 并发、恢复与真实存储验收缺口

### 1. 问题与现状

[潜在问题记录](../design/02-agent-harness/potential_problems.md)的 VP2-001～007 混有已复现缺陷、已修复项和测试环境限制。产品影响涉及 retry 死锁、合法短视频恢复被拒、成果与索引身份不一致、索引启动误拒；仅观察到的生命周期风险不能升级成生产事故。

### 2. 方案候选

对已确认问题做原层最小修复：统一锁序并允许在途任务自然收尾；短窗校验复用 Engine `plan_intervals()`；ES mapping 仅补齐等价默认 analyzer 语义。对 Prefix marker、SQLite 并发和跨 loop 观察先补序列/环境验证，不因假设风险新增另一套状态、通知或清理机制。

### 3. 方案验证

MySQL 使用真实行锁和独立连接控制交错；ES 使用隔离真实节点；其余组合以 SQLite 和替身验证持久不变量。原编号与结果全部保留如下，真实数据库、代码测试与产品质量的证明范围分别记录。

### 4. 指标对比

| 原编号 | 问题与验证结果 | 状态/剩余边界 |
|---|---|---|
| VP2-001 | retry/提交原 17 passed / 2 deadlock failed；修后 8 个真实竞争用例三轮均通过、完整 MySQL 25 passed、相关同版集 149 passed | 覆盖场景未再死锁；不承诺全系统无死锁 |
| VP2-002 | 上传→失败恢复→正式 Search→删除恢复，本地完整/短窗组合通过 | 该记录的真实全链与质量门槛未通过 |
| VP2-003 | committed→失效→新链越过旧位置；旧写操作仍拒绝，Repository 16 passed | 本地未发现旧 Worker 写入权；真实 MySQL 仍待验 |
| VP2-004 | SQLite 双 Worker 可丢 ready_mask 更新 | SQLite 无相同行锁语义，不作为 MySQL 缺陷结论；不改生产事务迁就替身 |
| VP2-005 | 20 s 视频合法 `[0,2)` 窗 retry 原 409；复用规划后完整/短窗组合 2 passed | 最小修复有本地闭环证据 |
| VP2-006 | 同一 Runtime close/start 后 Event 绑定旧 loop | 本地已观察；正式支持合同与生产影响未验证 |
| VP2-007 | ES 省略默认 search_analyzer 导致初始化误拒；直接修前 3 failed、修后定向 262 passed，真实 ES 6 passed | 仅等价默认语义兼容，不放松错误 mapping；非全链验收 |

复用既有规划和比较逻辑减少规则复制；锁序、租约及真实竞争夹具仍有维护成本。没有用户等待时长、故障率或维护工时的前后统计，不能量化总体收益。

<a id="devlog-history-47"></a>

## 2026-09-23—2026-10-17：整集解析的性能、重复执行与 Refine 冲突

### 1. 问题与现状

原编号 **B07**。从 ParseV1 长时和重复 ASR/embedding，到 V2 Canvas 读取、索引幂等和并发 Refine 冲突，属于多个阶段的问题，不能压成一项“模型慢”。10 月 12 日 EP03 已完整完成；10 月 17 日三方事件合并已有离线证明，仍无使用新合并逻辑的全集前后对照。更早详细账本和每轮输入保留原文，不重新运行。

来源：[B07](problems.md)、[V2 Agent](../../agent/video_parse_v2/)。历史 `engine/parsev1/pipeline.py` 已迁移。

### 2. 方案候选

历史候选包括 ASR durable checkpoint、媒体复用、有界并行、减少 fallback/重试浪费、有界正文批读、索引版本幂等。10 月 17 日按 Base/Current/Proposed 对事件三方合并，保留独立修改，仅同一事件或来源冲突时重做，避免任意修改都导致完整窗口生成作废。

并行缩短墙钟但增加冲突和归因成本；checkpoint 增加恢复契约，须避免回到高频时长写表。降低 reasoning 并不自动保证质量：模型把整份时间线当局部 patch 替换时，即便工程检查通过也会丢内容。整体 token/cost、语义保留与 wall time 必须一起验收。

### 3. 方案验证

9 月 23 日审计 7 jobs/17 attempts/675 calls，5 成功样本合计视频 118.508 分钟、Worker 已知时长至少 430.542 分钟。10 月 9 日同一 90.132 秒素材的 none/low 两轮分别约 100.285/661.334 秒，Refine 3.073→454.095 秒；low 最终只剩 2/36 事件，34/34 工程检查通过也不能证明语义正确。两轮 ASR 文本不同，不是完全同 prompt 对照。

10 月 12 日 EP03 attempt=1 COMPLETE：理解 24/24、Refine 22/22、索引 22/22，68/68 单元；全链执行账本 complete=true。10 月 17 日 EP04 全集墙钟 38分43.518秒，54 次生成对应 22 窗口、32 次冲突刷新；三方合并收益只有离线候选验证，不能当作已节省这 32 次真实调用。

### 4. 指标对比

| 记录 | 指标 | 可得结论 |
| --- | --- | --- |
| V1 五个成功样本 | Worker/素材时长至少 3.633 倍；575 calls、17376984 tokens | 混合重试和不完整账本，不是纯单轮模型倍率 |
| V1 三个完整计价样本 | 每集约 ¥14.76–¥15.47，均值约 ¥15.21 | 历史记录估算，其他调用缺价不能补零 |
| V2 EP03 10/12 | 墙钟 28分16.029秒；全链有效并集 28分13.979秒 | 两种口径接近但不同 |
| EP03 仅 Under+Refine | 有效并集 26分59.496秒 | “26 min”不包含 Index 尾段 1分14.483秒 |
| EP03 模型用量 | 169 completed、0 failed；其中 Refine invocation 42 | invocation 包含原生 Agent 整次执行，不等于底层采样次数 |

EP03 各阶段并行，阶段并集和 worker 累计时间不能直接相加；历史“90→110→37→26 分钟”混合不同集数、错误与配置，不能形成严格加速比。EP04 冲突生成占 Refine 累计时间 64.4%，不是可直接兑现的总墙钟降幅。

<a id="devlog-history-48"></a>

## 2026-09-21：历史消息与检索 topK 的业务合同澄清

### 1. 问题与现状

原编号 **C04、C06**。C04 核实失败 assistant 的 lastError 不进入下一轮模型历史，仅 COMPLETED assistant 被消费；对应已完成 USER 仍会留在历史，本轮可恢复工具反馈则属于本轮上下文。C06 已取消把每次检索 topK 错当全局草稿上限的约束，按稳定 Chunk ID 累积去重。

来源：[C04/C06](problems.md)。这两项分别是事实澄清和合同修复，不因同列“已解决”就统一当作性能优化。

### 2. 方案候选

历史按消息状态组装，错误详情留在展示/诊断而非伪造成功 assistant。topK 继续表示本次检索候选数量，显式请求可覆盖默认值，多次检索可形成更大证据集；用 already_present/updated 回执解释累积结果。新增全局数量上限需单独业务理由，不能复用相同字段隐式实现。

复用状态和身份合同，不需新表；多次搜索积累更多内容会增加上下文预算压力，需要由读取/编排治理，不能靠改变 topK 语义偷换成限流。

### 3. 方案验证

C04 是源码链核实，无新性能实验。C06 后端/前端相关测试通过，原条未记具体数量；服务重启与真实端到端待验。FAILED 不进入历史不代表 USER 消失，也不表示所有工具错误从模型上下文清除。

### 4. 指标对比

行为合同已有依据；历史污染率、候选去重收益、允许累积后的 token 变化和用户命中率未量化。不能把澄清一项误解报告为节省上下文。

<a id="devlog-history-49"></a>

## 2026-09-21：Chat 未授权引用与泛化失败需要可追踪、可修正的反馈

### 1. 问题与现状

原编号 **C01、C02**。C01 将未知/未检索到的 video_chunk_id 当模型调用异常，使整轮 FAILED；引用来源校验必要，但模型可修的参数问题缺少明确反馈。C02 保存了一组先失败后成功的真实体验，日志有递归深度、模型和 cursor 等不同错误，缺失逐次 message ID 对齐，不能全归给 C01。

来源：[C01/C02](problems.md)、[Chat 工具](../../agent/chat/common/tool.py)、[Chat Service](../../server/service/agent_chat.py)。

### 2. 方案候选

对未知引用、可修正范围/参数给出工具名、字段和合法恢复动作，继续拒绝伪造来源；真实依赖/不可恢复错误仍走失败路径。补安全结构化 Trace，ID 可定位、自由文本仅保留必要摘要/hash。历史 `update_draft_tem` 拼写已在 `b41b1f5` 修正，不能把其他未验证工具拼写一起宣告解决。

统一反馈降低重试猜测，但错误分类不能掩盖 DB/网络故障；日志增加少量维护责任，换来避免把多种失败混成单因。

### 3. 方案验证

C02 记录等待 8/29/29 秒失败，随后 77 秒成功，跟问 8 秒失败后 31 秒成功；原材料没有完整请求对应关系或受控复现。C01 的 typo 修复有提交依据，但其他反馈修复、真实全链恢复率当时未记录。

### 4. 指标对比

只能保留逐次体验数字，不能计算同条件成功率或修复收益。参数错误自动恢复次数、避免重启整轮的 token/延迟/费用差额未知，应由后续正式 A/B 补充。

<a id="devlog-history-50"></a>

## 2026-09-20—2026-10-16：历史联调验收与能力边界不能被“已解决”清单吞掉

### 1. 问题与现状

来源是 [problem_backup.md](problem_backup.md) 的未编号“联调与验收”“当前能力边界与未纳入范围”，当前 problems 没有这两个独立章节。这里保留早期验收缺口与随后已更新边界，避免去重时丢失。历史待验不代表 10 月 23 日仍全部未验，后续具体结果由对应条目覆盖。

### 2. 方案候选

使用小视频验证上传→解析→两路索引→Chat→定位播放；独立 MySQL 测试库验证锁与迁移；真实浏览器验证展示/播放；历史 Schema 副本演练 timing 迁移。生存检查、替身/DOM 测试和源码审查各自负责不同证据，不能相互替代。保留未知 usage，缓存复用不补造历史费用。

验收矩阵增加少量文档与样本维护成本，但能避免同一“通过”混用部署、运行、质量、计费四种意义。无需另建平台才能先记录这些边界。

### 3. 方案验证

9 月 20 日本地 MySQL/Qdrant/API/Web 可用、health ok/Consumer running，未有新视频整链；后端 841 passed/8 skipped、前端 53 passed，Playwright 仅配置无用例，截图未成功，旧库升级未演练。10 月 16 日手动停止、fatal 上抛、最终提交与预览已有 B01/C13/C12/F06 后续记录，不能继续笼统称“均未实现”。

### 4. 指标对比

此处测试数仅属于当时基线，不与之后更大套件相加。历史 Parse 费用与时长引用 B07；没有保存的历史用量无法补算。执行恢复与跨实例 delta 回放、独立短 Clip/转码等早期未纳入事项如需判断当前状态，须查后续专门证据，本轮没有重新验收。

<a id="devlog-history-51"></a>

## 2026-09-17—09-19：Chat 上下文重复注入与有界读取的历史修复

### 1. 问题与现状

“下雨天 MyGO 里面都发生了什么？”已召回候选却在 Evaluator 阶段失败：10 hits 的完整结果约 260,149 字符，被 retrieval_calls/result/selected_output 重复注入后 prompt 约 780,531 字符；12,586 字符系列背景也按 hit 重复。产品表现为检索成功但回答未完成，重启不能消除输入重复。

### 2. 方案候选

比较重复完整 DTO、确定性角色投影与额外 LLM 压缩，先选宿主持有 canonical state、按角色投影并去重；后续两层工具与 replacement history 处理累计增长。选择减少事实改写和额外模型调用，同时保留可恢复证据。该阶段字符/token 多层预算是历史设计，Canvas 读取端后来删除重复限制的决策以本 devlog 10/23 条目为准。

### 3. 方案验证

[Harness 调教日志](../design/02-agent-harness/agent-harness调教log.md)和[上下文设计修复](../design/02-agent-harness/agent_harness_context修复.md)记录 overflow 根因、实际状态替换、六轮工具批次压力测试、usage 校准和硬边界恢复。09/19 全量为 831 passed / 8 skipped / 9 failed，失败属于当时 Parse timing/SSE fixture；真实 1/6/13/26 集质量/容量实验仍待完成。

### 4. 指标对比

去掉同一请求三份完整证据和逐 hit 背景重复是确定性结构改善；旧 780,531 字符与配置默认 200,000 字符上限不是一组修后实测 prompt 对比。后续 48K 软阈值、55,808 输入硬边界和 24K 目标是当时配置，不是实测消耗。没有本批完整 Chat 时间/费用/正确率 A/B，不能补造节省比例。

复杂度由重复 DTO 转为投影、外部状态、恢复读取与窗口控制；单次输入可控，但多次读取和多套预算又增加工具/Agent 认知负担。这一代代价是后来 Canvas 工具优化的问题背景，而非永久正确的限制清单。

<a id="devlog-history-52"></a>

## 2026-09-16—10-17：解析职责、Parse V2 存储与 ES 迁移的历史收敛

### 1. 问题与现状

早期 MQ 混入持久化和算法编排，V1 上传/任务/成果与 V2 Harness、索引状态之间存在重复或断开的合同，容易造成任务成功但检索不可用。旧设计又包含独立索引构建/发布指针、Interval 承担索引等已被替代方案，不能继续作为当前接线指令。

**原问题 B03/B05 的输入与职责边界：**

原编号 **B03、B05**。B03 的 ASR 点状 Cue 被强制当正区间拒绝，已在 `dabcb8d` 修复；B05 的 Lite/Pro 档位与 fallback 隐式决定拓扑，已按算法 Pipeline 与 Server 生产持久化重新划分，但原始/校正转写类型和独立 ASR durable checkpoint 仍未迁移。

来源：[B03/B05](problems.md)。原条记录的 `engine/parsev1/schemas.py`、`engine/parsev1/pipeline.py`、`server/service/parsev1/video_processing.py` 均是已迁移的历史路径，按原提交追溯，不当作现行目录。

### 2. 方案候选

保留既有上传、DB Queue 和 Worker 通道，由 Server 负责事务与恢复、Engine 负责算法、Agent 负责执行编排；V2 引入独立解析轮次与成果身份。ES 方案早期考虑重建期间继续提供旧版本，后改为当前任务 COMPLETE 才开放；10/14 任务合同进一步选择现有 ChunkTask 用 phase 区分 Parse/Index、Interval 只 Refine，10/17 Event 合同复用既有 INDEX 派生 ES，不新增 Event/Frame SQL 表或队列。

选择复用既有状态与领取通道，减少重复调度和多套真相；代价是任务、正文版本、租约、索引 receipt 的组合约束仍需维护，不能以“少一张表”代表全部复杂度下降。

**原问题 B03/B05 的输入与职责边界：**

允许原始 TranscriptCue start=end，保留非负整数、非空文本、未知字段拒绝；正式 TimelineEvent/canonical 区间继续 end>start。检索 Evidence 仅在投影时把点 Cue 表示为 `[t,t+1ms)`，不改原始事实；损坏反向 Cue 跳过并记录 warning。

Engine 显式拥有 transcribe→understand→embed→persist 算法顺序；Server 管理 lease、repository、artifact 和生产提交端口。model/tier 是配置与 provenance，不再决定拓扑。划分减少理解算法时跨层阅读，但 DTO 映射和恢复边界仍有维护成本；Pipeline 内存状态不能当跨 Attempt checkpoint。

### 3. 方案验证

[职责重构](../design/02-agent-harness/refactor_1.md)、[V2 存储批次](../design/02-agent-harness/refactor_2.md)、[ES 历史提案](../design/02-agent-harness/refactor_3_es_qdrant.md)、[V2 阶段证据](../report/agent/video_parse_v2/video-parse-v2-migration-stages-evidence.md)、[解析任务 TRD](../design/02-agent-harness/video-parse-harness-trd.md)、[Engine TRD](../design/02-agent-harness/video-parse-engine-trd.md)、[Sub-Chunk 最终合同](../design/02-agent-harness/sub-chunk-trd.md)按日期对读。验证分代码/替身、隔离真实存储、真实模型产品闭环；不同日期和重叠测试集不累加。

阶段表的 08A 未签收是当时状态；后续实测已在本 devlog 的 ASR、Event V3 和解析实验中单列，不能反向改写旧阶段结论，也不能凭较新代码存在宣布所有迁移门槛关闭。

**原问题 B03/B05 的输入与职责边界：**

B03 全量 844 passed/8 skipped，跳过为未配置隔离 MySQL 库；保留反向正式事件拒绝。B05 原材料描述实现边界，没有新的整体速度/准确率实验。剩余窗口 Understanding 失败后仍重新 ASR，因为只恢复连续 PARSED 前缀，不能声称所有阶段断点续跑已具备。

### 4. 指标对比

| 历史检查点 | 已记录结果 | 不能推出的结论 |
|---|---|---|
| 09/16–17 职责收拢 | 回归由该阶段的 590 passed / 8 skipped 到下一阶段 606 passed / 8 skipped | 用例数量变化不等于性能或业务收益 |
| 09/27 V2 批次 0–2 | Server 769 passed / 3 skipped；独立 MySQL 9 passed；Harness 28 passed | 不是 V2 全链路完成 |
| 10/02 R6 | 1690 passed / 29 skipped；阶段集 207 passed / 25 skipped | 当时真实环境和产品质量仍待验收 |
| 10/17 Event 首版 | 1477 passed / 9 skipped；后续 202 passed 与纵向 9 passed 存在交集 | 没有运行索引回填或真实模型效果证明 |
| 10/21 Event 多向量合同 | 514 passed；真实隔离 ES 2 passed | 不是 MyGO 全库重建完成，后续演示另记 |

产品收益是职责与检索准入可追溯、复用恢复通道；历史迁移增加的 Schema/状态兼容和多份旧 TRD 解读成本仍存在。本轮只归档，没有迁移数据库或修改这些实现。

**原问题 B03/B05 的输入与职责边界：**

点 Cue 不再导致同类 schema 失败；原始值不变是可验证合同。此类失败的历史数量、重试费用节省、分层后的代码量/认知成本数值均未记录；ASR checkpoint 的收益仅是待测候选。

<a id="devlog-history-53"></a>

## 2026-09-15：本地视觉模型微调与蒸馏候选（未训练）

### 1. 问题与现状

本地 Qwen 的跨帧重复、字幕缺漏和无依据身份推断影响 Timeline 与检索。调教文档提出训练方案，但没有实际训练日志、产物、显存或吞吐实测；不能将教师 API 可用、框架支持训练写成模型已改善。

### 2. 方案候选

先优化输入组织和证据约束；训练侧先比较 LoRA/量化 LoRA 的监督微调，积累可靠偏好与奖励后再考虑 DPO/GRPO。优先匿名视觉事实、字幕与引用，人物真名和最终 Timeline 作为后续任务。选择理由是先验证可测的错误来源；训练无法补回未输入的帧与音频。

### 3. 方案验证

计划先用 20–50 条核验样本验证编码、训练、保存与重载，再用未参与训练的片段做质量/成本比较，最后回归 Timeline 和检索。以上都是计划；没有实施或效果通过记录。来源：[Qwen 调教方案](../design/01-mvp/Qwen调教.md)。

### 4. 指标对比

已知基线来自上一条推理实验；训练后字幕覆盖、事实准确、重复、无依据新增、时间引用、端到端召回、训练/推理费用均未测。新增训练数据治理、adapter/基础模型版本、部署兼容和独立评测成本；收益未验证，暂不登记净收益。

<a id="devlog-history-54"></a>

## 2026-09-13：视频理解模型、输入方式与结构成功后的语义缺口

### 1. 问题与现状

原链路以整段 MP4 调用 Doubao，MyGO 第七集第 11 个 Chunk 曾反复触发审核；改用本地视觉模型后，又出现字幕遗漏、重复事实、人物错配和时间顺序错误。用户需要的是可检索、可播放的事实证据，JSON 成功或事件数增加不足以证明产品可用。本条保留当时模型选择，不代表 2026-10-23 的默认配置。

### 2. 方案候选

实际尝试过原生视频、二分视频、有序抽帧、Qwen 单请求与短分组、匿名视觉证据加文本整理、MiniCPM、llama.cpp 直接视频。最终历史路线将明确审核失败后的取证与整理交给既有 fallback；普通成功仍保留主模型结果。拆分只作为诊断，不能把不相容的半段结果冒充正式 Chunk。增加 fallback 可提高可完成性，也增加模型绑定、证据合并、去重和失败归因的维护成本。

### 3. 方案验证

同一失败 Chunk 做输入/模型实验，再从第七集 10/24 checkpoint 续跑，检查时间归属与逐 checkpoint 结构；之后人工审计全片语义。实验的 wall time 与分组耗时之和不同，不混算速度排名。来源：[模型调教日志](../design/01-mvp/模型调教log.md)。该文引用的旧 `packages/engine` 产物路径仅为历史记录，本文不声称仍能直接复跑。

### 4. 指标对比

| 历史方案/结果 | 实测 | 业务影响与限制 |
|---|---|---|
| Doubao 15 帧 v1 → v2 | 188.929 s、13 个非法区间 → 172.710 s、15 observed / 14 owned | Schema 恢复，但召回和时间精度未达门槛 |
| Qwen 33 图 → 15×3 分组 | 332.354 s、34 SCENE / 0 DIALOGUE → 219.354 s wall、37 events / 14 DIALOGUE | 恢复部分对白，身份、说话人和时序仍错 |
| Qwen 中性证据 / MiniCPM | 44 observations、16 条重复 / 48 observations、39 条重复 surplus | 数量增长不能当作有效覆盖增长 |
| 主动抽帧 / 直接视频 | 191.376 / 425.177 s；均 2/3 组 Schema 成功 | 直接视频未解决尾组失败或重复 |
| 正式整集续跑 | 24/24 Chunk、468 owned events、0 个 fallback Chunk | 主模型本轮自行成功，不能据此证明 fallback 效果 |
| 结构与语义 | 时间并集覆盖 98.49%，但 speaker 错配、RELATION_CHANGE=0、预告标题误归本集 | 结构闭环通过，语义质量未全面通过 |

架构上保留模型选择与 provenance，降低静默替换的认知成本；代价是多条输入/降级路径的验证负担。没有可归因的全任务成本对照，也没有本批 fallback 召回质量数据。后续 EP07 召回对照归入检索实验记录。

<a id="devlog-history-55"></a>

## 2026-09-13：EP07 双通道检索是否真的比纯文本更容易召回

### 1. 问题与现状

视频检索同时有文本和视觉向量，但需要先验证融合是否提高用户找台词、动作、人物和跨边界事件的命中率。此次目标单位是 24 个 canonical 60 秒 VideoChunk，不能把解析窗口 overlap 当成另一个命中片段。12 条 smoke query 的标签来自原视频 contact sheet 目视检查，series bible 只用于角色别名归一；不是从解析描述反向生成答案。[样本与首次实测](../../evals/recall/ep07.recall-smoke.md)

### 2. 方案候选

比较纯文本、纯视觉与等权 RRF（各通道 top 20，RRF k=60）。文本仅取 self_compact 和归属本 chunk 的 events，不读邻接 context。融合是否值得保留需要逐任务看结果，不能预设多通道一定更好。

成本主要是独立原视频标注、边界归属维护、两路 embedding 与候选融合。报告没有货币费用或完整运行耗时，不能据此计算单位命中成本。没有 fallback chunk，fallback 召回仍未验证。

### 3. 方案验证

实际使用配置的 Ark 多模态 embedding、1024 维、1 FPS 跑完整 24 chunk。拥抱跨 353–361 秒，标 seq 5/6；长独白跨 seq 14/15；春日影质问在 1260 秒之前结束，只标 seq 20。这些边界检查防止 overlap 造成虚高。没有负例／干扰项全集标注，12 条探索样本不构成全集 recall 或统计代表性测试。

### 4. 指标对比

| 通道 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR |
|---|---:|---:|---:|---:|---:|
| 文本 | 58.3% | 91.7% | 91.7% | 100% | 0.747 |
| 视觉 | 25.0% | 58.3% | 58.3% | 100% | 0.441 |
| 等权 RRF | 41.7% | 75.0% | 91.7% | 100% | 0.623 |

此样本中等权融合低于文本的首位命中和 MRR；它支持继续分析 query 类型与权重，不支持宣称融合已有整体收益。

<a id="devlog-history-56"></a>

## 日期未记录；2026-10-23归档：低频累计耗时替代阶段明细高频写表

### 1. 问题与现状

原编号 **B04**。解析阶段 timing 明细增加持久化写入与恢复复杂度，已在 `c1ab11a` 删除两张明细表及阶段写入，转用既有 job 累计 durationms 和 complete 标记。原文未明确该问题日期，迁移文件中的 20260919 不用来补造实施日。

来源：[B04](problems.md)、[Job Repository](../../server/repository/job.py)、原条所列受控 timing 迁移脚本。

### 2. 方案候选

checkpoint 事务按本 Attempt 绝对累计值单调更新，ack/retry 结算；过期失联 lease 标为时长不完整，不补造丢失时段。迁移先停 Worker、拒绝 RUNNING，再归并历史结果并清理旧表。复用 job 字段减少表/事务维护，代价是舍弃持久的细阶段粒度，后续细诊断依赖低频 Trace/日志。

### 3. 方案验证

841 passed/8 skipped；8 项 MySQL 集成未配置。health 与 Schema 检查不等于历史库迁移演练或新视频整链；backup 的验收清单仍把历史 timing 升级演练列为缺口。

### 4. 指标对比

已移除 2 表和阶段写路径；实际少写多少事务、平均延迟/空间节省未测。duration 是 Worker 有效工作累计，不是素材时长或用户墙钟；失联历史保留 incomplete，不能补算成精确总耗时。

<a id="devlog-history-57"></a>

## 日期未记录；2026-10-23归档：会话管理、解析重试和前端能力文案

### 1. 问题与现状

原编号 **F01、F02、F03、F04、F05**。F01 会话列表/创建/切换/确认删除与最近选择恢复已实现；F03 区分失败重试和全量重解析已实现。F02 前端允许 PARTIAL 而后端只允许 READY、F04 刷新后无法继续分片上传、F05 任务页轮询却写 SSE，原材料仍列 TODO。

来源：[F01–F05](problems.md)、[TasksPage](../../web/src/features/tasks/TasksPage.tsx)、[UploadProvider](../../web/src/features/uploads/UploadProvider.tsx)、[Upload Service](../../server/service/upload.py)。

### 2. 方案候选

会话复用既有订阅与晚到事件隔离，PROCESSING 删除返回 409，终态物理删除按现有 MySQL cascade。失败重试使用 uploadSessionId+expectedJobVersion，保留 checkpoint/attempt/usage；全量重解析清空 Checkpoint 从片头运行，保留 attempt/usage 历史，旧成功快照在新解析导入前仍可检索。

F02 最小方案对齐前端 READY，正式支持 PARTIAL 需独立定义范围；F04 利用后端 missing segments/pending uploads，重新选择并校验文件身份后绑定会话，文件名/大小不足以认证；F05 只改轮询文案，不为一句说明增加工作区 SSE。文件恢复需持久身份与 UI 状态，存在额外兼容成本；后两者应与已有后端能力分开验收。

“旧成功快照在新解析导入前可检索”属于该问题记录的历史合同；后续V2选定任务非COMPLETE不开放的决定见解析/ES迁移条目，不能把两代行为同时当当前规则。

### 3. 方案验证

原文给出 F01/F03 已实现行为，但未记录独立真实端到端验证数字。F02 后端会在模型前 409 SERIES_NOT_READY；F04 File 与队列仅在内存、后端能力存在不代表前端续传完成；F05 TasksPage 每 5 秒 jobs/list 轮询是源码事实。

### 4. 指标对比

完成状态逐项保留，不能将五项合为“前端已完成”。切换隔离、误导点击、续传成功率、上传节省流量和文案理解成本的实测前后值均未知。

<a id="devlog-history-58"></a>

## 日期未记录；2026-10-23归档：产品范围、综合评估与外部反馈的 Owner 复盘

### 1. 问题与现状

来源：[reflection_1.md](reflection_1.md)。复盘指出产品原目标是本地素材解析并由 Codex 辅助剪辑，过程中范围扩大、上线时间缺少约束、外部反馈和文档责任不足。这里归档为治理问题，不把个人自责当技术结论。相关 [角色 NPC TRD](../design/02-agent-harness/character-npc-trd.md) 是 0 字节空文件，只能确认缺少需求/方案/验收记录，不能推断项目已训练、成功或失败。

### 2. 方案候选

明确 MVP、交付时间和职责，按主产品目标判断新增功能；通过外部使用者交流收集 good/bad cases，形成可维护的 golden set。改工具和 context 时同时评估 token、错误频率、工具总耗时、最终质量/成本/复杂度，避免只报告一个指标改善。角色 NPC/SFT 在原复盘中被视为与主流程关联弱的探索，不作为已经验证的产品增长方案。

候选能减少功能分叉和重复评估的认知成本，但维护看板、样本与反馈也占用单人 PM/开发/测试/运维/运营时间；原材料没有选定具体流程或证明新治理已执行。

### 3. 方案验证

原文为定性复盘，没有实验、正式用户研究、上线复盘指标或治理执行记录。相关 [2026-10-20 评测研究](../research/2026-10-20-video-evaluation-system.md) 提出独立 GT、阶段净收益及覆盖/正确性/成本联合评估；它明确没有实现评测平台或运行新质量实验，属于候选方案。归档本身只建立问题—候选—验证—指标的可追踪结构，不证明 MVP、golden set 或外部反馈闭环已经完成。空 anon TRD 不填造候选与结果。

### 4. 指标对比

原计划日期、实际延期天数、外部用户数、反馈采纳率、每次迭代人时和收益均未记录。后续应从真实迭代开始记录，不能倒填历史值；定性目标是减少无关功能与无人维护的文档，暂无量化成效。

<a id="devlog-history-59"></a>

## 日期未记录；2026-10-23归档：上传事务成功后的附属清理错误

### 1. 问题与现状

原编号 **B02**。Video、ParseJob 和 COMPLETED 已提交后，删除临时目录的 PermissionError/OSError 穿透 API，用户首次收到失败但任务实际成功。原文状态为已复现、待修；归档未重新检查现代码是否后来变化。

来源：[B02](problems.md)、[Upload Service](../../server/service/upload.py)、[Artifact Store](../../server/storage/artifact.py)。与 B09 同属上传成功率，但事务前死锁与事务后清理是不同失败边界。

### 2. 方案候选

只在提交后的 staging 清理处捕获 OSError，安全记录 session/job/error_type 并返回真实成功；不调用 release、不重合并、不重发任务。提交前校验、发布、探测错误仍失败。遗留目录可用独立幂等清理命令处理，不能全局吞存储错误。

最小方案减少业务误报，代价是需追踪残留磁盘文件；通用清理队列尚非必要前置，不能为附属删除重做上传流程。

### 3. 方案验证

离线故障注入首次 complete 返回 403 PERMISSION_DENIED，DB 已 COMPLETED 且有 jobId，再次 complete 返回 200。修复实施和首次返回 200 的回归结果未记录；原文验收条件仍是待办。

### 4. 指标对比

确认一例“接口失败、业务成功”的不一致；发生率、遗留空间、修复后失败率和清理恢复耗时均未知。

<a id="devlog-history-60"></a>

## 日期未明确（2026-10-23归档）：当前播放窗口的按需理解与复用

### 1. 问题与现状

未完成整片解析时，用户仍可能询问当前播放内容；普通系列检索不能把未READY视频直接当作完整已索引资料。播放位置字段名与实际毫秒单位不一致，也容易让取证窗口偏移。

### 2. 方案候选

选择固定当前播放窗口的四节点Graph，两路基础证据并行后汇合，复用已有持久结果和派生缓存，再接回正式解析；不默认重跑全片。租约、共享工作取消隔离和缓存失效可减少重复处理，但增加按需/后台两条入口的生命周期理解成本。

### 3. 方案验证

[实时Parse TRD](../design/02-agent-harness/realtime-parse-trd.md)记录Supervisor的understand_current_video接线，冻结毫秒位置，校验可访问范围；Graph/数据库宿主51 passed。综合1027项首跑1023 passed / 1 skipped / 3 failed，修复既有Bootstrap替身后受影响组37 passed，原集合最终1026项通过/1项跳过，重叠不重复计数。

### 4. 指标对比

已验证窗口、状态隔离、租约恢复、取消和缓存合同；未做真实视频/模型端到端延迟测量、真实MySQL并发锁压测。缓存命中可省去对应工作属于路径事实，首轮等待时间、费用和回答质量改善未量化；不能将“实时”解释为已承诺固定秒数响应。

<a id="devlog-jev-chat-ab-20261024"></a>

## 2026-10-24：Jev 旧链路与主 Chat 直接处理的真实端到端对照

### 1. 问题与现状

用户要求用旧链路与主 Chat 直接处理的**完整耗时、token、任务完成度**判断是否值得保留 Jev，而不是凭架构复杂程度或单个模型的分类命中率作决定。原问题 C19：上一轮已经完成台词检索，用户说“那又咋了，你说话怎么这么无趣啊”，系统仍把对助手的反馈当作待查台词。旧实现是 **Jev 三路判断（respond/retrieve/mixed）→ 按需调用本地 Qwen3-8B 抽取检索槽位 → 原始消息附加应用说明 → Chat 及其工具**。原始用户消息没有被整个替换。

此前的 [意图路由记录](#devlog-history-12) 主要验证了局部分类、槽位及少量在线行为，没有完整证明该前置链路的净收益。同日先前的 14 条固定空工具回放和 6 条真实 runtime 调用，对比的是**无 hook 的旧 prompt 与无 hook 的新 prompt**，不能作为“有 Jev 的旧链路”基线。新 prompt 在空工具回放中工具选择 14/14、严格行为 13/14，也不能推出真实 HTTP 历史会话中的 C19 已解决。本次另建基线，前述样本不计入以下 40 次。

本轮结果不支持直接下结论“Jev 没必要”：新链路在 C19 两次均误检索；旧链路两次均正确保持对话路径。但旧链路也有回答越过证据、时间码错误和 8B 抽错目标的问题。两种方案都没有达到完整业务验收，不能把某一方描述为已经稳定解决问题。

### 2. 方案候选

| 方案 | 预期收益与代价 | 本轮状态 |
|---|---|---|
| 保留 Jev + 8B + 旧 Chat prompt | 独立路由可以约束检索惯性；增加外部 key、网络依赖、本地模型及三处行为规则 | 真实基线，20 次 |
| 新 Chat prompt 直接理解原始消息，关闭整个 hook | 少一项外部服务配置、一个常驻本地模型；意图判断与回答统一，也可能被长检索说明和历史牵引 | 候选方案，20 次 |
| 仅去掉 8B 改写，保留 Jev 路由 | 有机会降低本地部署负担，同时保留 C19 的路由约束 | **未运行消融**；不能拿上述两组差异推断此方案的效果 |

本次只比较前两条链路；没有微调小模型、添加新模型、边跑边修改 prompt，也没有改变默认 hook 配置。上一轮准备的 v4 prompt 修改仍是工作区候选；本轮没有新增业务代码改动。**v4 prompt + 启用 hook 的组合不属于本次已验证的任一实验臂**，不把当前工作区草稿等同于旧基线或已验收部署。当前决定是暂不据此删除 Jev；若继续简化，先分离路由与 8B 改写的作用，再验证独立收益。

### 3. 方案验证

实验产物位于 [jev-chat-e2e-ab-20261024](../../outputs/verification/jev-chat-e2e-ab-20261024/)；主入口为 [run_arm.py](../../outputs/verification/jev-chat-e2e-ab-20261024/run_arm.py)、冻结的 [protocol.json](../../outputs/verification/jev-chat-e2e-ab-20261024/protocol.json)、[cases.json](../../outputs/verification/jev-chat-e2e-ab-20261024/cases.json) 与 [schedule.json](../../outputs/verification/jev-chat-e2e-ab-20261024/schedule.json)。`outputs` 是本地实验产物，关键数字在本文保留，不假定随 Git 分发。

控制条件与测量范围：

- 两个独立 OS 进程，每组 10 个场景 × 2 次重复，共 40 次。按配对顺序交替先跑旧/新，第二轮反转场景顺序；同时只发起一个顶层请求，保留超时后的迟到终态与 usage。每次新建独立 SQL/native 会话，种入相同历史；没有共享上一条测试生成的回答。
- 两组 Supervisor 均为 `gpt-5.6-luna / medium`、Codex harness；Query 为同一 `gpt-5.6-luna / high`，同一 embedding、工具 schema 与 runtime 设置。总超时均为 240 秒，`limits_enabled=false`。服务端 cache 未清空，fresh native session 不等于冷 cache；两次重复不提供 p95 或统计显著性结论。
- 旧组冻结 Supervisor 源码 SHA-256 为 `62e5b794eb8d93b2911dbf30f8e5cf5f26ed6946d515b57f7aa062c46a067730`，新组为 `a7f0a1c5ad14ee9835855b855fcf61c92d936cdb7826a6693dd43ac4578fe949`。旧组真实调用 Jev `typesafe/jev-1.14`（响应版本 `jev-1.14-20261014`）与本地 Qwen3-8B-Q4_K_M；新组 `_query_hook=None`，将原始消息原样交给 Chat。完整源码与配置元数据分别保存在 `old/`、`new/`。
- 使用原有 MyGO 13 集数据、MySQL、Elasticsearch 与真实工具。索引 `seekino_video_chunks_local_20261010_development`，前后 UUID 与文档数 **71,736** 一致。检索结果存在 `partial=true` 和第 2 集覆盖不足警告；能确认已命中的正例，不能证明全剧中不存在某句台词。
- 通过实际 `/api/agent/conversations/create`、`/api/agent/messages/send` 的 ASGI HTTP 路径执行，记录发送到最终持久化/HTTP 错误的耗时；不包括新建会话、准备 fixture、真实客户端网络传输和浏览器渲染。首个文本可能只是过程说明，不等于首次给出正确答案。
- 20 对 Supervisor 入口的历史、动态上下文、工具 schema 与原始请求一致；新组没有隐式改写原始消息。进一步读取对应 native rollout，**40/40 的历史消息均按原文、原顺序出现在当前 prompt 前**。旧组 `r1-holdout_mixed`、新组 `r2-retrieve_correct_negation` 有历史注入确认超时警告，但原生日志确认消息实际存在；不能将警告直接解释为历史丢失。证据：[input-audit.json](../../outputs/verification/jev-chat-e2e-ab-20261024/input-audit.json)、[native-history-audit.json](../../outputs/verification/jev-chat-e2e-ab-20261024/native-history-audit.json)。
- 保存每次 HTTP 响应、阶段耗时、Jev 返回标签、8B 槽位、实际传入 Chat 的消息、工具参数、返回给模型的证据正文、可见回答与 provider/SDK usage。仅工具调用成功不足以判定任务完成；逐条对照当前请求、执行顺序、证据、提交片段与最终文本，审读记录见 [reviews.json](../../outputs/verification/jev-chat-e2e-ab-20261024/reviews.json)。这是 Agent 审读，未做独立人工盲评或全剧逐帧标注。

场景覆盖 8 条经典问题及 2 条新的表述：C19 原始历史、真实台词出处、只解释已有证据、先回应吐槽再找首次登台、停止检索并压缩、纠正“不解释≠不检索”、按历史解析“第一件/第二件”、明确搜索类似吐槽的角色台词，以及两条新的反馈/混合表达。正例包括第 13 集「いいご身分ですわね。」54.6–56 秒与第 7 集首次 live 的对白/舞台片段。

质量分级：`pass` 为当前请求完整完成且最终说法与证据一致；`partial` 为方向正确但只有近似片段、复述错对象、时间展示错误或结论越过证据；`fail` 为意图/目标错误、漏掉任务、无依据宣称精确命中或超时；`unassessed` 不算成功。近似字幕翻译与原句可核对时可以通过，语义不同的“无聊的女生”不能算“你说话怎么这么无趣”的精确出处。

评测自身有一处问题：`holdout_mixed` 的历史明确提到祥子/睦，当前“她们第一次登台”却在预期里被写死为 MyGO 五人，存在指代歧义。发现后保存 [fixture-caveat.json](../../outputs/verification/jev-chat-e2e-ab-20261024/fixture-caveat.json)，没有改题重跑；两组该场景的两次运行全部保留耗时与 token，业务质量标为 `unassessed`，所以每组只有 **18 次可判任务质量**。执行“先压缩再找”的顺序仍可独立判断。

预检曾发现实验记录器保留已解压响应的 `Content-Encoding`，造成重复解压，并发现 HTTP 外层 usage observer 未自动进入业务执行线程。立即停止预检，修复记录器、单独校准 Jev 和 8B 后才开始上述正式 40 次；预检保存在 `harness-preflight-*`，不作为旧链路质量失败或正式费用统计。正式运行过程中未改记录器和两组 prompt。

闭环核对：SQL 最终 **38 COMPLETED、2 FAILED、0 PROCESSING**，38 个成功响应与持久化正文一致；两个失败均为 `AGENT_EXECUTION_TIMEOUT`。成功响应引用的 6 个不同视频 URL 均经真实内容接口返回 **HTTP 206、1024 bytes、video/mp4**，验证了资源可读与 Range 路径；未执行浏览器全片播放或完整解码验收。见 [persistence-audit.json](../../outputs/verification/jev-chat-e2e-ab-20261024/persistence-audit.json)、[playback-probe.json](../../outputs/verification/jev-chat-e2e-ab-20261024/playback-probe.json)。本轮临时启动的 MySQL、ES、8B 服务已仅按记录的自有 PID 停止，数据、权重及带 `[Jev A/B]` 标题的测试会话保留供追溯；未启动解析任务或重建索引。

未验证：长上下文容量、用户图片附件的意图理解、其他 Chat 模型/供应商、真实并发负载、独立 Jev/8B 消融、广泛用户问题分布与最终部署效果。本轮只支持上述冻结条件下的链路比较。

### 4. 指标对比

主统计包含所有正式运行和失败，不删除慢样本；**共 20 次/组**。来源：[summary.json](../../outputs/verification/jev-chat-e2e-ab-20261024/summary.json)。

| 指标 | 旧链路：Jev + 8B + 旧 Chat | 新链路：v4 Chat 全处理 |
|---|---:|---:|
| 20 次累计耗时 | 843.65 秒 | 1,172.38 秒 |
| 平均每次耗时 | 42.18 秒 | 58.62 秒（+39.0%） |
| 耗时中位数 | 33.54 秒 | 32.29 秒 |
| 首个可见文本：平均 / 中位数 | 7.23 / 7.07 秒 | 8.05 / 5.01 秒 |
| HTTP 与最终持久化完成 | 20/20（100%） | 18/20（90%） |
| 严格业务通过（可判的 18 次） | 10/18（55.6%） | 12/18（66.7%） |
| 部分完成 / 失败 | 8 / 0 | 2 / 4 |
| 目标歧义，未判业务质量 | 2/20 | 2/20 |
| 意图与先后顺序检查 | 19/20 | 18/20 |
| 全链路已观测 token | **7,233,511，记录完整** | **至少 4,351,225，仍不完整** |

新组的 4 次失败是 C19 两次误检索和第二轮两次超时。旧组的 8 次部分完成包括：C19 一次回应错对象、已有文字解释两次过度推断、台词出处一次时间码错误、雨中等人两次近似候选、明确吐槽台词检索两次近似候选。新组通过数较高与失败数较多同时成立，不能仅选一个数字宣称全胜。意图/顺序是局部行为指标：超时前选择正确路径也可以通过此项，但不会因此算业务成功。

每个场景的两次完整耗时如下；顺序为第 1 / 第 2 次，质量字母 `P=通过、△=部分、F=失败、?=未判`：

| 场景 | 旧：秒；质量 | 新：秒；质量 |
|---|---|---|
| C19 原始历史后的吐槽 | 7.56 / 5.77；△ / P | 36.85 / 45.21；F / F |
| “高高在上”台词出处 | 25.62 / 38.62；P / △ | 20.68 / 17.00；P / P |
| 只解释已有证据 | 5.29 / 4.89；△ / △ | 4.72 / 5.33；P / P |
| 先回应反馈再找首次登台 | 44.60 / 35.25；P / P | 53.26 / 23.49；P / P |
| 停止检索、压成一句话 | 6.46 / 6.91；P / P | 4.17 / 4.50；P / P |
| “不解释”并非“不检索” | 31.83 / 46.16；P / P | 28.89 / 116.03；P / P |
| 第二件反馈先答，第一件雨中等人继续找 | 170.57 / 161.00；△ / △ | 36.75 / 240.15；△ / F |
| 明确要求寻找“说话无趣”的角色台词 | 70.20 / 70.08；△ / △ | 69.95 / 240.17；△ / F |
| 新表述：出处已知，只批评表达 | 7.76 / 7.40；P / P | 4.77 / 5.70；P / P |
| 新表述：压缩出处，再找“她们”首次登台 | 36.71 / 60.94；? / ? | 35.70 / 179.04；? / ? |

关键失败证据及阶段作用：

- **C19 是当前替代方案的明确回归。**旧组两次均 `respond`、0 次检索，分别 23,333 / 23,448 token；新组两次均去搜索并返回第 8 集无关近似台词，分别 **199,219 / 289,953 token**。新组还将用户反馈解释成“记混了两处台词”。历史已确认实际注入，因此不能用“没拿到历史”解释这个结果。旧组第一次虽无误检索，却把反馈写成给用户使用的回话示例，仍只计部分完成。
- **8B 改写存在独立缺陷。**12 次调用中，6 次保留目标、2 次把原台词缩为“高高在上 台词”、4 次抽错当前检索任务。两次“第二件先答、第一件继续找”均抽成“为什么总把我的吐槽当台词”；两次压缩旧出处后找新场景均抽回已知旧台词。Chat 有时仍依靠原始消息搜索正确目标，不能把全部额外耗时因果归给错误槽位。详细见 [hook-review.json](../../outputs/verification/jev-chat-e2e-ab-20261024/hook-review.json)。
- **路由正确不保证最后正确。**Jev 的冻结三分类标签为 20/20，但旧组仍只有 10/18 严格通过；例如第二轮混合改写任务先检索、最后才压缩，没有遵守顺序。已有文字解释也出现“不能判断不喜欢”之后又断言“并非讨厌对方”的矛盾。
- **新组两次超时的原因不能合并成意图失败。**明确台词搜索一直扩搜并调用 Query，240 秒仍无最终答案；雨中等人一例在约 39 秒已提交室内等待候选，之后未完成最终答复，240 秒返回 502。未独立排除 native SDK/远端推理停顿，不能把全部超时归因于删掉 Jev。它们仍是真实端到端失败，应保留在主要统计。

token 与费用必须分口径。Gateway 记录旧组 input **7,196,699**、output **36,812**、cached input **6,364,976**；新组可见 input **3,596,824**、output **20,008**、cached input **2,897,664**，但新组 2 个 Chat、1 个 Query 超时调用没有返回完整 usage。新组 Gateway 的 **3,616,832** 因而只是部分记录，不能与旧组直接比较得出“省了一半”。进一步从对应原生会话日志恢复两个失败 Chat 的最后计数 **559,620 + 174,773**，与 Gateway 已记录调用不重叠，得到 **4,351,225 的已观测下界**；失败 Query 的 ephemeral 会话未保存可恢复日志，超时尾部也可能尚未回报。38 个成功 Chat 的 native 总计数与 Gateway 全部一致，恢复依据见 [usage-recovery.json](../../outputs/verification/jev-chat-e2e-ab-20261024/usage-recovery.json)。

| 阶段 | 旧链路 token | 新链路已观测 token |
|---|---:|---:|
| Jev 路由 | 16,228（20 次） | 0 次 |
| 8B 槽位 | 4,140（12 次） | 0 次 |
| 主 Chat | 3,804,152 | 至少 3,436,059（含两个失败会话的部分恢复） |
| Query 子调用 | 3,408,183（2 次） | 至少 913,597（1 次有 usage、1 次超时未知） |
| 文本 embedding | 808 | 1,569 |

为给出不把未知计零的可比数字，另列**两边用量都完整的 18 对样本**，同时排除这两个新组超时及它们对应的旧组运行：旧 **4,596,795** → 新 **3,616,307 token，下降 21.3%**；耗时合计 **612.56 → 692.06 秒，上升 13.0%**。其中具备 cache 明细的 Chat/Query 未缓存输入 **570,767 → 697,591，反而增加 22.2%**；输出 **27,512 → 20,008**。不能把总 token 下降比例当成账单下降比例。该子集只是补充，不能替代包含失败的主统计。再排除两组各一次历史确认警告对应的配对样本后，16 对耗时 **529.69 → 540.33 秒**，token **4,139,198 → 3,324,985**；同样不是随机独立的新实验。仅排除指代歧义场景、仍保留失败时，每组 18 次的平均耗时为 **41.44 → 53.20 秒**，新链路并未因此变成整体更快。

Jev 20 次响应中直接返回的 `usage.cost` 合计 **$0.000649656**，仅为本次 Jev 路由费用，不含任何 Chat/Query/embedding/本地计算。Jev 路由累计 **17.59 秒**，8B 改写累计 **10.56 秒**；前置阶段平均每个用户请求约 **1.41 秒**（8B 只在 12/20 次触发）。两阶段合计 **20,368 token，约占旧链路总量 0.28%**，本次主要模型消耗来自后续 Chat/Query 的检索与核验循环。Codex 的 usage 是 `sdk_turn_run / codex_thread_total` 聚合，不足以还原每个供应商请求的上下文计价档位；cached/write/reasoning 属于明细子集，不重复相加，未返回的 cache 信息不当零处理。未取得完整供应商账单，不填写整体美元节省额；本地 8B 算力也不当作免费。

部署复杂度方面，新方案确实省去一项 Jev API key/网络依赖及一个本地 llama-server 服务，减少路由说明、槽位 prompt、Chat prompt 的多处协调。现有 8B 权重实测 **5,027,783,584 bytes（约 5.03 GB）**；一次 RSS 观察约 **5.85 GiB**，不代表完整 Metal/GPU 占用，也不是隔离压测得到的新旧净内存差。实际用户配置步骤、首次安装时间和维护人时未测。**可确认部署依赖减少，尚不能确认总费用更低或核心任务不退化。**当前证据支持把“Jev 路由是否有价值”和“8B 改写是否值得保留”拆开判断，未支持直接把整个前置链路删除后作为已验收版本发布。


<a id="devlog-query-hook-chat-model-ab-20261024"></a>

## 2026-10-24：复用 Chat 模型做意图判断与 query 改写，三组实测与 cloud 选型

### 1. 问题与现状

上一轮证明了“直接去掉前置节点、仅让 Chat 处理”仍会在 C19 把用户对助手的吐槽当作台词检索，但这并不证明 **Jev 这个供应商不可替代**。本次按用户要求，在 `dev` 分支实现一个职责受限的前置调用：复用 Chat 的模型与认证，一次只输出 `action` 和 `retrieval_query`，分别测试 `low`、`none` 推理强度，和 Jev + 本地 8B 对照。

本次的旧路径是 **Jev → 按需 Qwen3-8B 槽位 → 当前 v4 Chat**，不是上一轮的旧 Chat prompt。三组均使用相同 v4 Chat prompt，Jev 基线重新实测；不能把本次 15/20 与上一轮 10/18 直接相减当成修改收益。上一轮有歧义的“她们首次登台”混合题，在本次冻结前明确改为“MyGO!!!!! 五人第一次登台”；这也不代表修好了旧 8B 的指代能力。

结果分两层：三组前置标签都为 **20/20**，low 和 none 都能承担受限的前置职责；整条链路严格业务通过为 **15/20、13/20、11/20**。low 的本轮全链路平均耗时与总 token 较低，但质量没有超过基线；none 有两次后续检索超时。因此，当前证据支持提供“复用 Chat 的前置节点”来减少用户配置，**不支持宣称已等效替换整个 Jev 链路、整体更优，或已经验证长上下文能力**。

### 2. 方案候选

| 方案 | 本次决策与代价 |
|---|---|
| Jev + 本地 Qwen3-8B + v4 Chat | 用户确认 cloud 继续选用。前置更快，本轮严格业务通过数更高；独立 Jev key 与 8B 服务由部署团队维护，仍有抽错检索目标的问题。 |
| 同款 Chat 模型 / low 一次输出两字段 | 新增为可选绑定，复用已有 source/认证，不需要 Jev key 或 8B 服务。前置本身更慢、token 更多；本轮整体质量未达到不退化。 |
| 同款 Chat 模型 / none 一次输出两字段 | 同一实现，仅改推理强度。确实没有 reasoning token，但前置节省很小，整链路有两次超时；本轮没有充分依据优先采用它。 |
| Chat 直接处理、没有前置节点 | 只有上一轮的实测证据，本次不重跑、不并入三组统计。 |

**Cloud 选择（用户于 2026-10-24 确认）：继续使用 Jev + 按需 8B query 改写，不切换到 low / none 前置。** Cloud 产品的 Jev 认证和 8B 推理由服务端统一承担，普通终端用户无需分别提供这个 key 或运行 8B；表中的“本地 8B”指后端连接的模型服务，不要求运行在终端用户设备上。选择理由是优先保留本轮更快的首次文本响应、纯回应速度和更高的严格业务通过数，接受团队承担额外服务的部署维护。low 保留为减少自托管配置负担的可选路径，none 暂不优先；这不是“Jev 不可替代”或“cloud 效果已经验收”的结论。

只读核对 `config.yaml`、当前本地 `outputs/local-services/main-model-config.yaml`、现有 cloud 联调环境 `outputs/local-services/cloud-auth/phone.env` 指向的 `model-config.yaml`：三者均为 `query_hook.enabled=true`，且没有 `models.query_hook` 替代绑定，实际配置选择已是 Jev + 8B。此次补记不需要切换配置，也没有重启或发布服务。云环境的并发、硬件成本和真实客户端等待未重新实测，不能把下面的本地对照数字当作生产 cloud SLA。

实际最小改动为 3 个生产文件 **+83/-12**、1 个测试文件 **+92/-4**，本条记录追加到统一 devlog。`QueryHookDecision` 复用现有槽位约束：三种 action、最多 160 字符的非空候选或 null、禁止多余字段，respond 必须为 null。新分支只有一次结构化生成，无工具、无多轮规划；结果仍通过原固定模板附加在原始用户消息后。原文、已有历史、工具请求的职责不变，未把整条用户消息替换成改写文本。

总 hook deadline 保持 8 秒，继承原来最近 6 条/2,000 字符历史及当前 query 的有界输入策略。超时、供应商错误或无效 schema 回到原始消息；不会偷偷调用 Jev 或 8B。取消、父任务超时和用量准入拒绝继续上抛，不能当普通可降级错误吞掉。模型由现有 GatewayClient 创建和关闭，没有增加独立模型服务或新配置 Schema。

可选 low 路径的启用方式是在既有模型配置的 `models` 下增加 `query_hook` 绑定，复用 Chat 已配置的 source；下面是实验配置示意，**不是本次选定的 cloud 配置**，其他既有绑定保留：

```yaml
query_hook:
  enabled: true
  total_timeout_seconds: 8
models:
  query_hook:
    source: codex_local
    model: gpt-5.6-luna
    reasoning_effort: low
    supports_vision_input: false
    request_timeout_seconds: 8
    max_output_tokens: 512
    fallback: null
```

本次 none 仅将 `reasoning_effort` 改为 `none`。完整实验配置见产物中的 `low-model-config.yaml`、`none-model-config.yaml`；`max_output_tokens=512` 是配置值，不把它当作已验证的 native SDK 强制输出上限。正式前置调用实际最大输出为 low 84、none 72 token。未提供 `models.query_hook` 时继续旧 Jev/8B 路径；`query_hook.enabled=false` 时仍完全关闭 hook。**默认 `.env.local` 与主模型配置未切换**，没有 commit、push、迁移或重启用户正在运行的应用。

减少的是新部署的额外认证与本地模型依赖：可以少配一项 Jev key、少运行一个 llama-server、少下载约 5.03 GB 的现有 8B 权重。5.03 GB 是已有权重文件的实测大小，不是本轮隔离测得的净内存节省。仓库仍保留旧适配器以兼容，且新增一个可选分支和 prompt，不能声称维护成本已全部消除；用户配置耗时、安装成功率和维护人时未量化。

### 3. 方案验证

可复查产物：[实验目录](../../outputs/verification/query-hook-chat-model-ab-20261024/)、[冻结协议](../../outputs/verification/query-hook-chat-model-ab-20261024/protocol.json)、[worker.py](../../outputs/verification/query-hook-chat-model-ab-20261024/worker.py)、[cases.json](../../outputs/verification/query-hook-chat-model-ab-20261024/cases.json)、[schedule.json](../../outputs/verification/query-hook-chat-model-ab-20261024/schedule.json)。`outputs` 为本地实验产物，不假定随 Git 分发，关键结果保留在本记录中。

- 三个独立 OS 进程，每组 10 题 × 2 次，共 **60 次真实模型/工具调用链**。每题轮换组顺序，第二轮反转题序；同一时刻只发一个顶层请求，避免相互争抢测试资源。每次新建 SQL/native 会话，写入相同历史，不把上次生成的答案传给下一题。
- 三组主 Chat 均为 `gpt-5.6-luna / medium`，Query 为同模型 `/ high`，新前置为同模型 `/ low` 或 `/ none`，均走当前 Codex harness；Jev 为 `typesafe/jev-1.14`，改写为本地 Qwen3-8B-Q4_K_M。同一工具、embedding、MyGO 数据、240 秒完整任务 deadline、`limits_enabled=false`。前置预检使用过 20 秒预算，已排除，正式三组均为 8 秒预算。正式运行没有改 prompt、case、生产代码或 runner。
- 通过真实 `/api/agent/conversations/create` 和 `/api/agent/messages/send` 的 ASGI HTTP 路径测量，从 send 到最终响应/持久化或 HTTP 错误；不计建会话、写 fixture、真实客户端网络和浏览器渲染。FTT 使用记录器捕获的主模型首次 `handle_text` 回调作为代理指标，可能是“我去查一下”等过程说明，**不是浏览器实际首 token 或首个正确结果时间**。SDK/服务端缓存未清空，新会话不等于冷缓存；不是并发负载测试。
- [输入审计](../../outputs/verification/query-hook-chat-model-ab-20261024/input-audit.json) 验证 **20/20 三元组**的主 Chat prompt、历史正文、工具 schema、原始请求一致；low/none 前置 prompt 与 schema 相同，SDK 实际请求的 effort 分别为 low/none，主 Chat 仍为 medium。**60/60 native 会话都按原文、顺序收到历史**，无历史注入确认警告。生产文件、case、runner SHA-256 与冻结记录一致。
- 沿用 MyGO 13 集资料，返回证据仍有 `partial=true`、第 2 集覆盖不足等限制。Chunk 索引 `seekino_video_chunks_local_20261010_development` 的实验前后 UUID 和 **71,736** 文档数一致。Event 索引 `seekino_video_events_development_v3` 使用固定配置；本次只额外取得运行中与结束时快照，两者 UUID/文档数 **59,730** 一致，**没有把该快照冒充实验前快照**。未启动解析或重建索引。
- 逐条审读 HTTP 文本、过程顺序、工具参数/证据、提交片段和返回卡片描述，见 [reviews.json](../../outputs/verification/query-hook-chat-model-ab-20261024/reviews.json)。`pass` 为完整处理当前任务且说法与证据一致，`partial` 为近似片段、回应错位、时间/片段描述错误或越过证据，`fail` 为错目标、漏任务或超时。没有把标签正确、工具返回成功当作业务通过。对话措辞存在审读判断，例如 low 第二次“并非明确排斥”按未明确表达排斥判通过；none 第二次 C19 转为品评用户原句判部分。未做独立人工盲评，不能把这些分数包装成客观大样本正确率。
- 正例锚点包括第 13 集 54.6–56 秒「いいご身分ですわね。」和第 7 集 607.7–610.8 秒首次 live 对白。首次登台请求接受经证据确认的同场演出片段，不要求必须截到这句对白，例如 Jev 第二轮提交 840–900 秒的演奏片段可通过；若卡片明确宣称包含这句对白而所选范围没有，则计部分。雨中等人与“说话无趣”两类仅得到近似候选，未确认全剧不存在精确目标，不以索引缺失证明不存在。

闭环检查：[持久化审计](../../outputs/verification/query-hook-chat-model-ab-20261024/persistence-audit.json) 为 **58 COMPLETED、2 FAILED、0 PROCESSING**，58 个成功响应正文均与 SQL 一致，失败均为 `AGENT_EXECUTION_TIMEOUT`；两次失败均保留 240 秒完整耗时和迟到用量记录。[播放资源探测](../../outputs/verification/query-hook-chat-model-ab-20261024/playback-probe.json) 的 5 个不同视频 URL 均返回 HTTP 206、1,024 bytes、`video/mp4`；这是内容/Range 路径验证，不是浏览器完整播放或逐帧人工验真。本次启动的 MySQL、ES、8B 服务已按自有 PID 停止，见 [cleanup.json](../../outputs/verification/query-hook-chat-model-ab-20261024/cleanup.json)；保留数据、权重和 `[Hook model A/B]` 测试会话供追溯。

相关回归共 **218 项最终通过**：初次执行 `pytest tests/agent/chat/test_query_hook.py tests/gateway/test_query_hook.py tests/server/test_bootstrap.py -q` 为 217 passed / 1 failed；失败是新增测试把共有免责声明“候选检索用语”误当成有槽位，修正为检查实际槽位标签后，受影响 hook 文件 **32 passed**，此前未受影响 Gateway/Bootstrap **186 passed**，重叠不重复计数。覆盖一次附加、原文保留、禁止调用旧模型、schema、用量上下文、迟到结果、取消/超时/准入拒绝及 Supervisor 接线；不是全仓测试。`git diff --check` 和冻结源码检查通过。

未验证：超出当前有界历史的长上下文、用户图片附件、其他模型/供应商、真实并发、全量视频覆盖、完整账单、长期效果和用户安装体验。没有凭这 60 次决定训练小模型或改变检索/Query 的实现；发现的扩搜不收敛、时间码/卡片描述和证据边界问题保留在本轮记录中，未扩大代码修改范围。

### 4. 指标对比

主统计包含全部 20 次/组以及失败；来源：[summary.json](../../outputs/verification/query-hook-chat-model-ab-20261024/summary.json)、[component-summary.json](../../outputs/verification/query-hook-chat-model-ab-20261024/component-summary.json)。此次多维表为同一批 60 次实验的整理和派生计算，没有新跑模型，也没有把同一批样本重复计数。FTT 为服务端首次文本回调，TT 为请求到成功或失败终态的耗时；错误地提前交付也可能 TT 较短，不能把它直接解释为更高效率。

| 指标 | Jev + 8B | Chat 前置 low | Chat 前置 none |
|---|---:|---:|---:|
| 累计 TT | 601.72 秒 | 546.49 秒 | 906.05 秒 |
| 平均每次 TT | 30.09 秒 | 27.32 秒（-9.2%） | 45.30 秒（+50.6%） |
| TT 中位数 | 24.68 秒 | 25.32 秒 | 31.79 秒 |
| 最慢一次 TT | 130.33 秒 | 125.50 秒 | 240.67 秒 |
| TT 超过 60 秒 | 2/20 | 2/20 | 3/20 |
| 超时未完成 | 0/20 | 0/20 | 2/20 |
| FTT 代理指标：平均 / 中位数 | 6.13 / 6.14 秒 | 7.78 / 7.70 秒 | 7.89 / 7.58 秒 |
| 浏览器实际首 token / 首个正确结果时间 | 未测 | 未测 | 未测 |
| HTTP + 持久化完成 | 20/20 | 20/20 | 18/20 |
| 严格业务通过 | 15/20（75%） | 13/20（65%） | 11/20（55%） |
| 部分 / 失败 | 5 / 0 | 6 / 1 | 7 / 2 |
| 意图与任务顺序检查 | 20/20 | 20/20 | 20/20 |
| 前置分类正确且说明实际附加 | 20/20 | 20/20 | 20/20 |
| 全链路已观测 token | 3,412,745，完整 | 2,842,916，完整 | 至少 2,742,713，仍不完整 |

“意图与顺序”通过表示走对路径、按要求先回应再查找；后续超时或答案错误仍是业务失败。low 的全链路总 token 比基线少 **16.7%**，但这不是质量不退化或账单下降的证明。两次重复不足以提供 p95、统计显著性或稳定胜率。

任务质量的三类互斥且合计为 20 次，例如 low 是 **13 次通过 + 6 次部分 + 1 次失败**。部分完成同时包含“诚实说明只能找到近似候选”和“时间码等局部错误”，衡量的是当前请求的整体达成，不是前置模型单独的准确率；HTTP 完成也不等于业务正确。low 的服务端首次文本平均晚 **1.66 秒**，8 次纯回应平均 TT 增加约 **2.22 秒**；其整体 TT 均值较低但中位数略高，不能解读为大多数用户请求都更快。

资源投入与 Token cost 派生指标：

| 成本维度 | Jev + 8B | low | none |
|---|---:|---:|---:|
| 全链路平均每次请求 token | 约 17.06 万 | 约 14.21 万 | 至少约 13.71 万 |
| 平均每次前置 token | 约 1,016 | 约 10,800 | 约 10,783 |
| Codex 阶段已知未缓存输入 | 584,573 | 599,829 | 至少 611,007，仍不完整 |
| 每个严格通过结果分摊的 token | 约 22.75 万 | 约 21.87 万 | 至少约 24.93 万 |
| 完整美元账单 | 未取得 | 未取得 | 未取得 |

“每个严格通过结果分摊的 token” = 全组已观测 token ÷ 严格通过数量，包含部分完成和失败的资源投入，**不是成功请求本身的平均用量，也不是重试直到成功的成本预测**。按此口径 low 仅少约 **3.9%**，不再是全组总量的 16.7%；同样不等于美元成本下降。Codex 阶段只包括主 Chat、Query 和新前置：none 的 611,007 = Gateway 已知 565,654 + 两个失败 Chat 原生日志恢复的未缓存输入 45,353，仍缺失败 Query 和可能未回报的尾部。cached / reasoning 是明细子集，不重复加总；模型 token 和本地算力不能当作同价单位或免费资源。

下表为每个场景两次实际耗时与质量，`P=通过、△=部分、F=失败`：

| 场景 | Jev：秒；质量 | low：秒；质量 | none：秒；质量 |
|---|---|---|---|
| C19 原始历史后的吐槽 | 6.54 / 5.88；P / P | 7.32 / 7.00；P / P | 7.64 / 7.44；P / △ |
| “高高在上”台词出处 | 27.53 / 21.64；P / P | 25.92 / 24.04；△ / P | 31.55 / 24.12；△ / P |
| 只解释已有证据 | 4.62 / 5.39；P / △ | 8.15 / 7.45；P / P | 8.20 / 7.19；△ / △ |
| 先回应吐槽再找首次登台 | 33.74 / 31.30；P / P | 30.66 / 31.64；P / △ | 38.41 / 35.00；P / P |
| 停止检索、压成一句话 | 5.03 / 4.84；P / P | 7.80 / 7.20；P / P | 6.80 / 7.16；P / P |
| “不解释”并非“不检索” | 44.03 / 60.98；P / P | 30.68 / 31.40；P / P | 37.64 / 32.41；P / P |
| 第二件反馈先答，第一件雨中等人继续找 | 21.82 / 36.00；△ / △ | 36.49 / 36.13；△ / △ | 35.59 / 35.66；△ / △ |
| 明确搜索“说话无趣”的角色台词 | 59.98 / 130.33；△ / △ | 125.50 / 60.73；△ / △ | 63.62 / 240.15；△ / F |
| 新表述：出处已知，只批评表达 | 5.20 / 4.75；P / P | 7.64 / 7.50；P / P | 7.15 / 7.63；P / P |
| 新表述：压缩旧出处，再找 MyGO 五人首次登台 | 50.51 / 41.59；P / P | 24.72 / 28.54；P / F | 240.67 / 32.03；F / P |

主要差异与失败证据：

- **C19：三组两次均没有误检索。**Jev、low 的两次业务回应均通过；none 第二次先认领反馈，随后又复述并品评用户原句，严格审读计部分。这个结果说明主模型同款前置可以保住 C19 的路由约束，不支持“必须是 Jev”。
- **槽位独立计分，见 [hook-review.json](../../outputs/verification/query-hook-chat-model-ab-20261024/hook-review.json)。**每组有 12 次需要检索：Jev+8B 保留目标 8 次、原句压缩丢词 2 次、抽错目标 2 次；low 保留目标 9 次、保守返回 null 3 次；none 保留目标 12 次。null 是允许的“不提供候选”，不能计为完成了具体改写。保留“她们”等未明确指代也不等于完全消解；none 有一次加入不要检索旧台词的排除说明，并非最简检索词。旧 8B 两次都把“雨中等人”抽成反馈问题，Chat 依靠原文纠回方向，不能把下游表现直接等同于槽位优劣。
- **low 的错目标发生在下游。**第二次明确 MyGO 五人首次登台题，前置目标正确，Chat 却把第 3 集开头演奏场景断言为首次登台，未用“首次”的证据核对，计失败。另一次首次 live 选到第 7 集正确场次，但卡片将不在所选 548.4–599.9 秒内的“第一次开 Live”对白写入说明，计部分；低推理前置并没有解决这些证据对齐问题。
- **none 的两个超时都不是动作分类失败。**一次是明确 MyGO 首次登台，一次是“说话无趣”的台词搜索。前置分别在 3.15 / 3.09 秒完成，随后 Chat/Query 持续检索到约 240 秒仍未交付。none 第一轮首次登台与 low 第二轮同题的主 Chat 提示、工具、历史正文和候选 query 完全相同，却分别超时和返回错片段，说明不能仅凭归组把失败因果归为 none。其余长尾同样存在：Jev 台词题第二次 130.33 秒，low 第一次 125.50 秒。
- **前置正确不保证文字正确。**原台词查询中 low、none 各一次把 54.6 秒写成约 54 分钟；已有文字题 Jev 一次、none 两次排除了证据未能排除的情绪/动机。雨中等人与明确无趣台词均未得到可靠精确目标；近似结果保留为部分完成。

前置阶段的实测成本：

| 指标 | Jev + 8B | low | none |
|---|---:|---:|---:|
| 前置模型调用数 | 20 Jev + 12 次 8B | 20 | 20 |
| 平均前置耗时 | 1.34 秒 | 3.51 秒 | 3.34 秒 |
| 前置中位数 / 最大耗时 | 1.20 / 3.03 秒 | 3.38 / 4.49 秒 | 3.13 / 4.36 秒 |
| 累计前置 token | 20,324 | 215,991 | 215,651 |
| 正式新前置 schema 有效、未 fallback | 不适用 | 20/20 | 20/20 |
| 8 次纯回应的全链路平均耗时 | 5.28 秒 | 7.51 秒 | 7.40 秒 |
| 8 次纯回应的全链路 token | 188,119 | 267,848 | 267,744 |

Jev 路由累计 18.70 秒，本地 8B 累计 8.09 秒；low、none 的前置累计 70.27 / 66.81 秒。**降低 effort 并没有消掉新增调用的固定开销。**low 和 none 的前置输入总量都为 **215,040 token**，输出 951 / 611，其中 reasoning 为 382 / 0，属于输出明细不能重复加总；none 只少 340 个总 token（0.16%），平均只快约 0.17 秒。约 10.75k 的每次输入包含当前 Codex harness 的上下文开销，不能当作这份短决策 prompt 本身需要这么多 token，也不能外推到其他裸 API 调用方式。

全链路阶段用量：

| 阶段 | Jev + 8B | low | none 已观测 |
|---|---:|---:|---:|
| Jev | 16,236 | 0 次 | 0 次 |
| 本地 8B | 4,088 | 0 次 | 0 次 |
| 新前置 decision | 0 次 | 215,991 | 215,651 |
| 主 Chat | 2,751,687 | 2,179,676 | 至少 2,525,623 |
| Query | 639,941（1 次） | 446,546（1 次） | 2 次超时，用量未知 |
| 文本 embedding | 793 | 703 | 1,439 |

none 的 Gateway 直接记录 **2,165,120 token**，另有 2 个 Chat、2 个 Query 未返回 usage。根据对应持久 native 日志，仅对缺失 Chat 恢复最后计数 **273,332 + 304,261 = 577,593**，无重复加总，得 **2,742,713 的已观测下界**。两个失败 Query 为 ephemeral，会话没有可恢复日志，取消尾部也可能尚未计入。58 个成功 Chat 的 native input/output/total 与 Gateway 均一致。证据：[usage-recovery.json](../../outputs/verification/query-hook-chat-model-ab-20261024/usage-recovery.json)。未知 Query 绝不按零费用/零 token 成功计算。

另列**三组用量都完整的 18 组三元样本**作为描述性补充：Jev **420.88 秒 / 2,248,859 token**，low **461.04 秒 / 2,409,169 token**，none **425.24 秒 / 2,142,427 token**。此时 low 相对基线耗时 +9.5%、token +7.1%，与全量平均的方向不同，显示少数长尾对结果影响很大。该子集同时排除 none 两个超时和它们对应的其他组样本，**不能替代保留失败的主统计**，更不能拿它证明 none 更可靠。

费用口径：Jev 响应直接返回的 20 次 `usage.cost` 合计 **$0.000649992**，不含 Chat/Query/embedding 或本地计算。Jev 组全链路 input/output 为 **3,390,988 / 21,757**，low 为 **2,824,660 / 18,256**，已知 cached input 分别 **2,789,045 / 2,224,128**。只比较具有完整 cache 明细的 Codex 阶段（Chat、Query、新前置），未缓存输入反而从 **584,573 增至 599,829（+2.6%）**。SDK 聚合用量不是逐请求计价账单，不同模型 token 也不能按同一价格相加；未给出整体美元节省额，不把本地算力当免费。

团队建设与部署维护视角，以下为架构事实和定性判断；安装耗时、维护工时、支持工单和云实例费用均未实测：

| 建设与维护维度 | Jev + 8B | low | none |
|---|---|---|---|
| 相对已有 Chat 的额外认证 | Jev key，由服务端管理 | 复用 Chat 认证 | 同 low |
| 额外推理服务 | 需要维护 8B 服务 | 无需该服务 | 同 low |
| 额外模型权重 | 实验配置约 5.03 GB | 无需这份 8B 权重 | 同 low |
| 自托管用户首次配置 | Jev 认证、8B 下载/启动/地址 | 增加前置模型绑定 | 同 low |
| Cloud 普通终端用户 | 无需单独配 Jev key 或运行 8B | 无需单独配置前置模型 | 同 low |
| 生效路径的前置调优 | 路由规则 + 8B 槽位提取 | 一个节点输出两个字段 | 同 low |
| 额外排障事项 | Jev 请求、8B 进程、端口、权重与资源 | 前置绑定、schema、已有 Chat 调用 | 同 low |
| 现有 Chat 服务的额外调用 | 前置由 Jev/8B 承担 | 每个适用请求增加一次同源调用 | 同 low |
| 当前仓库的兼容维护 | 旧路径继续保留 | 新增可选分支，没有退役旧代码 | 与 low 共用实现 |

部署依赖减少不等于当前代码维护立即减少：本次生产改动 **+83/-12，净增 71 行**，目前仍需维护旧/新两种路径。未来若退役旧适配器可能减少维护对象，但未量化人时。low 和 none 的部署要求相同，关闭 thinking 没有额外的部署简化收益；现有 Chat 服务的并发/额度承载也没有在本轮压测。

| 选择目标 | 当前决定或证据 |
|---|---|
| Cloud 默认路径 | **按用户选择保留 Jev + 8B**；额外依赖集中由团队承担，优先本轮首次文本速度、纯回应速度与严格业务通过数 |
| 减少自托管配置负担 | low 保留为可试用选项；接受额外前置等待，业务质量不退化尚未证明 |
| 关闭推理的 none 路径 | 保留实验结论，不作为 cloud 默认；相比 low 的前置收益很小，本轮有两次下游超时 |
| 最低实际账单 | 暂不能判断；总 token、缓存、不同模型计价和失败用量不能合并成未经验证的美元节省额 |

Cloud 保留 Jev 的决定不表示它已全面解决任务：8B 抽错目标、取证不收敛、时间/片段描述和证据边界问题仍存在，未在本次文档补记中修改。后续若调整默认，仍应围绕这些具体失败扩大独立样本，再评估质量与成本；不能仅以“前置 20/20”或一次总 token 较少作为发布验收。

<a id="devlog-parse-harness-ab-20261025"></a>

## 2026-10-25：同一 chunk 的 ReAct、Deep Agents 与 Codex 解析实测

### 问题与现状

目标是判断 Parse 是否能去掉 Deep Agents 建图层、保留普通 ReAct 和简单 compact，从而减少维护对象。用户观察到移除 harness 后解析变差，本轮构造可复跑实验并实际调用模型，没有修改生产解析路径。

当前理解阶段向 Gateway 传入 `tool_harness="deepagents"`，但 native Codex 分支直接进入 Codex adapter；Deep Agents 与 Codex 是两条替代路径，不能把它们理解为前后叠加的两层。Parse 的 Deep Agents profile 已关闭默认摘要 middleware、通用 subagent 和文件工具；实际模型可见工具只有 Canvas 读、写、完成。源码依据：[model_factory.py](../../gateway/model_factory.py)、[deepagents_multimodal.py](../../gateway/agent_model/deepagents_multimodal.py)。

质量目标沿用 MyGO EP04 的已知问题：约 47–49 秒「祥ちゃんも悪くない／也不是小祥的错」，用户已经确认说话人是**长崎爽世**。不能用“有合法 JSON”“对白数量多”或“所有 speaker 都非空”代替这个身份目标，历史依据见[原记录](#devlog-history-29)及 [problems.md](problems.md)。

### 方案候选

| 路径 | 本轮实现 | 保留的职责与边界 |
|---|---|---|
| LangChain ReAct + Kimi | 将 `create_deep_agent` 替换为 `langchain.agents.create_agent`，去掉 StateBackend 和 FilesystemMiddleware | 保留现有 provider adapter、超时/取消、错误恢复、完成校验和 ContextWindowController；不是新写一套 compact，也尚未删除 Python 的 Deep Agents import |
| 当前 Parse Deep Agents + Kimi | 使用现有 Parse profile 和 graph | 作为同模型、同业务合同的基线；不启用生产本来就未使用的额外能力 |
| Codex + Kimi | 临时 custom provider，通过原生 Responses 调用 `k3.1-pro` | 使用当前 Codex 工具和完成合同；仅进程级配置，不修改用户全局 Codex 设置 |
| Codex + 原有 GPT 配置 | 原有 `models.vision = codex_local/gpt-6-luna/high` | 按用户要求，在 Codex + Kimi 完成合同失败后保留实际可运行的产品基线；不同模型/effort/协议，不能据此证明纯 harness 因果 |

Kimi 使用 `k3.1-pro / low`，API 两组均为 `temperature=1`、每请求 `max_tokens=8192`、600 秒总调用期限、无 fallback。当前 vision factory 拒绝非 none 的 reasoning_effort，因此实验在相同 transport 包装中显式注入 `low`，没有改生产 factory。Kimi 官方文档列出视觉输入、最长 1M 上下文和 Codex Responses 接入：[模型能力](https://www.kimi.com/code/docs/kimi-code/models.html)、[Codex 接入](https://www.kimi.com/code/docs/third-party-tools/codex)。这说明可以尝试该组合，不等于本项目完成合同已经兼容。

本轮结论支持继续收敛 Parse 建图层，但未授权或实施生产替换。Deep Agents 的部署工时、内存、包体节省没有实测；Cloud Chat 仍使用它，不能把 Parse 替换直接记作全仓依赖退役。

### 方案验证

冻结 EP04 chunk 0 的 `[0, 60000)` ms，使用同一份 180 张图片（3 fps、最高 720p）、19 条 ASR、31 条 OCR 和背景。模型收到图片与文本证据，没有收到原始音频。复用当前 Canvas、业务 prompt、读写工具、Pydantic Schema 和时间范围校验；每次新建内存 Canvas/native session，不重新跑 ASR/OCR，不经过 Refine、Prefix、索引或数据库提交。

ReAct、Deep Agents 和 GPT Codex 各跑两次，串行执行；API 两组第二轮反转顺序。另有 Codex + Kimi 默认合同 1 次、移除提前 JSON Schema 的诊断 2 次，以及原 128k 预算的人工中止诊断 1 次。预检、离线 fake transport 和中止诊断不混入完成率主表。版本为 LangChain 1.4.4、Deep Agents 0.7.23、LangGraph 1.2.11、openai-codex 0.156.1。

九次正式/原生诊断的 prompt、业务工具定义、图片哈希、证据、runner 和源码快照一致。进一步在真实 180 图输入上截取 API 两组首轮请求，除 timeout 外，完整请求字典完全相同，包括图片、系统/用户提示、工具 Schema、模型和采样参数，SHA-256 均为 `4fc7f9f6eb769c2e709371a7d63d047ee26d56df15e0f5ea031fd9b266eac672`。证据：[offline-wire-equality.json](../../outputs/verification/parse-harness-kimi-20261025/offline-wire-equality.json)。后续工具历史仍可能分化，不能将两次样本的耗时差直接归因于框架。

**评分分开看：**

- 在正式输出前冻结 15 条 OCR 字幕锚点、6 条可见人物辅助 speaker 锚点、4 项画面事实。文字按规范化相似度及时间重叠匹配，不使用模型裁判。15/15 表示覆盖已有证据，既不是独立人工转写准确率，也不保证时间和身份正确。
- 47 秒目标另用既有用户确认真值评分；该历史记录在首批输出后找回，单独保存，始终没有注入模型输入。
- 逐条补看事件描述、人物表、画面文字和关键帧；这是 Agent 的补充审读，不是盲评人工金标。尤其同时检查 `speaker` 与 description，防止把字段留空误算成“没有身份错误”。

**旧预算诊断：**原配置为 context 128000、auto compact 90000、target 60000。首轮 provider 实际输入为 **225965 token**；本地图片保守估算使整体约为 **384769 token**，而扣除输出和安全余量后的最大输入仅 **117760**。固定图片前缀不会被 compact 移走，`ContextWindowController.prepare()` 在 hard limit 下反复清除最新工具回执；投影又取本地估算与 provider 校准值的较大者，无法靠较低实测值校正估算。ReAct 实际跑到 **154.6 秒、12 次请求启动、12 次 compact、35 次读取/35 条回执释放、0 条事件写入** 后由操作者停止，已观测至少 **2,493,432 token**，最后一次 usage 缺失。它不是自然超时，也不算质量完成样本。证据：[trace.jsonl](../../outputs/verification/parse-harness-kimi-20261025/react-r1/trace.jsonl)、[operator-stop.json](../../outputs/verification/parse-harness-kimi-20261025/react-r1/operator-stop.json)、[context.py](../../gateway/agent_model/context.py)。同一控制器也被 Deep Agents 使用，但本轮没有实跑旧预算的 Deep Agents，不能替它虚构失败结果。

主对照将 Kimi 的实验窗口设为 **1048576**，auto/target 为 **786432/524288**；API 两组此后均完成且 **0 次 compact**。这证明当前输入与旧预算冲突，以及该 compact 行为会破坏循环进展；不证明长期 compact 质量已通过。GPT 保留原 binding，context/输出预算字段不代表原生 Codex 内部与 API 使用同一套限制；SDK 当前也没有传入与 API `max_tokens=8192` 等价的硬输出上限。

**Codex + Kimi 的合同诊断：**保留当前提前 output Schema 时，两次 native turn 都直接返回 `{"complete":true}`，没有业务工具成功，最终 `MissingRequiredFunctionCallError`。只去掉提前 Schema 后，两次均能读写 Canvas，留下覆盖完整的 draft，但最终给出自然语言而非有效 JSON，仍以 `MissingStructuredQueryDraftError` 失败。一个独立小工具 nonce 预检也呈现“无 Schema 时实际调用、带 Schema 时跳过”的区别。不能说 Codex 完全不能连接 Kimi；当前组合的问题集中在工具调用与结构化结束合同。draft 有内容不能算完整成功，也没有手工补 receipt 把失败改成成功。证据：[codex-kimi-tool-capability.json](../../outputs/verification/parse-harness-kimi-20261025/codex-kimi-tool-capability.json)。

复跑入口是 [evals/parse_harness.py](../../evals/parse_harness.py)，例如从仓库根目录执行：

```bash
uv run --no-sync --env-file .env.local python -m evals.parse_harness \
  --arm react \
  --inputs outputs/verification/ep04-chunk0-direct-schema-20261025 \
  --config outputs/verification/parse-harness-kimi-20261025/kimi-config.yaml \
  --context-window 1048576 \
  --out outputs/verification/parse-harness-kimi-20261025/replication-react
```

替换 `--arm deepagents` 可复跑同模型基线；`--arm codex --context-window 1048576` 为 Kimi 默认合同，额外 `--native-no-schema` 为诊断。原 GPT 基线使用 `--arm codex --codex-gpt-baseline` 并省略 `--context-window`。每次必须选新的 `--out`；真实调用会使用已有认证及额度。离线重算：`.venv/bin/python outputs/verification/parse-harness-kimi-20261025/analyze.py`。输入和原始 trace 在 Git 忽略的本地产物目录，换机器须携带这些材料，不能只克隆仓库就声称可复跑同一输入。

现有回归命令 `.venv/bin/python -m pytest -q -p no:cacheprovider tests/gateway/test_deepagents_multimodal.py tests/gateway/test_context_window.py` 为 **46 passed**。将 factory 换成实验 ReAct 后，复用其中的 Parse harness 用例为 **28 passed**，另有 2 个 pytest 插件重复导入警告；这是同一批用例的替换执行，不是额外 28 个独立用例。runner 编译通过。所有真实运行后源码快照核对无漂移。

本次实际纳入版本管理的变更范围只有新增实验 runner 与追加本 devlog；没有生产代码、API、数据库、默认模型配置或前端行为变更，未重启服务、迁移、commit 或 push。源码快照、输入配置、评分器、参考锚点、每次 result/draft/trace/assessment、矩阵及补充审读均保存在 [本地产物目录](../../outputs/verification/parse-harness-kimi-20261025/)。

后续待验证事项：共享预算应识别“固定媒体本身已经超限”，不再靠丢弃新回执维持无进展循环；ReAct 的简化应保留当前错误恢复和完成校验；Codex + Kimi 需要独立修复工具与最终 receipt 合同；身份问题需要在更多已确认台词上验证证据/推断策略。以上均未在本轮修改。若要证明 Codex harness 本身是否带来收益，仍须同一 GPT 模型直连 API 与 Codex 对照，当前用户授权的跨模型基线不能回答这个纯因果问题。

### 指标对比

主表的“完成”只表示有效完成 receipt、Canvas Schema 和时间范围通过；不表示语义全部正确。每格两次数值按 r1/r2 排列：

| 指标 | ReAct + Kimi low | Deep Agents + Kimi low | Codex + 原有 GPT high |
|---|---:|---:|---:|
| 结构完成 | 2/2 | 2/2 | 2/2 |
| 实际耗时，秒 | 201.99 / 234.87 | 180.59 / 144.49 | 138.08 / 137.11 |
| 平均耗时，秒 | 218.43 | 162.54 | 137.59 |
| 字幕锚点覆盖 | 15/15、15/15 | 15/15、15/15 | 15/15、15/15 |
| 6 条辅助锚点的 speaker | 均 6 条命名一致 | 均 6 条命名一致 | 均 6 条留空；非 6 条错名 |
| 47 秒用户确认目标 | 两次均错为千早爱音 | 两次均错为千早爱音 | 两次 speaker 留空；r2 描述仍错归粉发女生 |
| 业务工具调用 | 9 / 8 | 9 / 9 | 10 / 14 |
| 可恢复工具错误 | 1 / 1 | 0 / 0 | 0 / 0 |
| API 模型请求轮数 | 6 / 6 | 4 / 5 | 原生内部轮数，不以 outer turn 冒充同口径 |
| host compact 次数 | 0 / 0 | 0 / 0 | 原生管理，不按 API 控制器计数 |
| 总 token | 1,447,244 / 1,450,959 | 954,007 / 1,195,587 | 2,045,646 / 1,830,853 |
| 平均总 token | 1,449,101.5 | 1,074,797 | 1,938,249.5 |

完整结构评分见 [comparison.json](../../outputs/verification/parse-harness-kimi-20261025/comparison.json)，汇总见 [aggregate.json](../../outputs/verification/parse-harness-kimi-20261025/aggregate.json)。Token 为各次输入/输出用量的累计，cached 和 reasoning 属于子集，不重复相加。缓存未清除，未取得实际账单；GPT 与 Kimi 也不是同价单位。GPT 两次在首次工具前均出现 4 次 WebSocket 重连通知，运行自行恢复，上表耗时包含重连。两次重复不能用于 p95、显著性或稳定速度排名。

原生 Kimi 的失败必须保留在结果里：

| Codex + Kimi 诊断 | 结构完成 | 耗时，秒 | 业务工具 | draft 字幕覆盖 | 47 秒 speaker | 总 token |
|---|---:|---:|---:|---:|---|---:|
| 当前提前 Schema | 0/1 | 30.71 | 0 | 0/15 | 无事件 | 455,441 |
| 去提前 Schema，r1 | 0/1 | 239.26 | 8 | 15/15 | 错为高松灯 | 1,216,162 |
| 去提前 Schema，r2 | 0/1 | 156.67 | 9 | 15/15 | 错为高松灯 | 1,202,264 |

具体质量差异见 [visual-review.json](../../outputs/verification/parse-harness-kimi-20261025/visual-review.json)：

- ReAct 两次均因引用不在 persons 中的丰川祥子导致整批写入被拒，随后按反馈修正并完成。Deep Agents 两次没有这个错误。它增加了本轮 ReAct 的调用成本，但首轮请求完全一致、样本仅两次，不能证明框架本身更容易生成此类错误。
- Kimi API 四次均保留台阶夜景、爱音/灯并坐、灯低头/抱膝、爽世到场等画面事实，仍然全部错认 47 秒画外音。Codex + Kimi 的 draft 也错认该句，增加 native harness 并没有纠正它。
- GPT 两次全部对白 speaker 留空（19/19 与 11/11），人物表也均未识别长崎爽世，影响按人物检索。第二次 description 又把 47 秒及后续「誰も悪くない」归给粉发女生，说明只检查空 speaker 会漏掉正文中的错误身份推断。
- 50.166 秒购物袋实际画面为 **SUNWAY SUNWAY**，已存 OCR 为 **SUNDAY SUNDAY**。Deep Agents r1 修正了它，r2 又照抄错误 OCR；ReAct r1 和 GPT r1 同样照抄，其余部分输出未提袋上文字。这是可见质量差异，不是稳定的框架胜负。
- ReAct r2 将后续爽世台词的开始时间对齐到 52833 ms 字幕锚点；多次其他输出沿用 48400 ms ASR 起点，相差约 4.43 秒。这里只比较证据时间一致性，未重新听音建立独立发声起止真值。分组输出较少的对白事件仍覆盖全部锚点，不能把事件数直接当召回率。

对当前问题的判断是：**本轮未观察到 Codex 是该 chunk 解析质量的必要条件；也没有证据说明 ReAct 因缺少 Deep Agents 就丢失了字幕覆盖。**当前最明确的循环故障是共享 compact 与固定媒体预算不匹配；改用合理实验预算后，ReAct 和 Deep Agents 都能完成，但同样没有解决关键身份错误。Deep Agents 在这两次样本中耗时和 token 更少，GPT 更快但身份输出不完整且仍有正文误归属。生产简化是否值得上线，仍需修复预算行为、保持业务合同，并在多 chunk/长上下文及已知身份错误集上验收；本轮没有测量维护人时、部署内存或长期 compact 质量，不能把这项局部对照写成全面质量不退化。

<a id="devlog-karpathy-parse-recovery-20261029"></a>
## 2026-10-29：Karpathy OCR 配置纠正与解析恢复

### 问题与现状

用户发现正式任务连续失败。提交时本机配置仍有 `models.ocr`，8 集快照因此均为 `ocr_enabled=true`，没有落实用户关闭独立 OCR 的要求。前两集的模型调用统计有 9 次 OCR 失败（含截断/网络错误）、6 次成功，Understanding 有 119 次成功返回但 0 个已提交 Chunk。另有图片 token 估算不能向下校准和重启后无生产者的媒体等待问题；这些属于正式链路故障，不能归因于 WhisperX 未部署或 embedding 未就绪。

### 方案候选

先暂停失败运行。撤回临时 OCR 分批处理改动，移除本机独立 OCR 默认绑定；通过正式服务为全部 8 集创建新解析版本，而普通 retry 仍保持旧快照。扩展 reparse 支持已排队或已停止且租约完全回收的任务，保留历史并撤销旧非终态版本的调度资格/容量。Gateway 按 provider 实报校准上下文，媒体等待增加父任务存活检查。保持全部配置帧、ASR 模型及时间合同，不采用降低画面输入的办法掩盖问题。

### 方案验证

659 项定向测试通过：上下文估算/工具反馈、DeepAgents、Parse worker/媒体、UploadService 与持久 Queue。真实运行先拒绝了仍持有子任务租约的第二集；等待实际到期并通过 Queue 回收后，服务完成全部 8 集 v2 创建，没有越过活动租约。23:44 服务加载修复后恢复，8 个新快照的 OCR 均为 false。原始记录见 `outputs/verification/parse-failures-20261029/`；实时提交和模型操作统计记录在 `live-progress.json`，完整整集/索引效果仍待确认。

### 指标对比

| 指标 | 修复前 | 本次验证 | 边界 |
| --- | --- | --- | --- |
| 8 集独立 OCR 配置 | 8/8 开启，与要求不符 | 8/8 关闭，模型绑定为空 | 旧版本仍保留原快照，退出调度 |
| 新证据压缩 | 固定图片估算约 422K，即使实报约 248K 仍释放工具结果 | 对应回归用例保留新证据 | 真实长上下文完成率仍需观察 |
| 无生产者的媒体等待 | 子任务继续续租等待 | 生产者停止/更换租约用例立即退出 | 保留已就绪媒体复用 |
| 相关测试 | 已复现上述缺陷 | 659 passed | 不代表模型质量达标 |
| 内容提交与索引 | 前两集 0 个 Chunk 提交 | 验证进行中 | 不承诺 880 个 Chunk 已完成 |

<a id="devlog-parse-direct-json-20261030"></a>
## 2026-10-30：Understanding 直接 JSON 与 30k 正式接入

### 问题与现状

此前零工具和 30,000 只在 `evals/parse_protocol_benchmark.py` 生效，正式 Understanding 仍承担 Canvas 读写/核验/完成，默认及实际本机 YAML 仍为 8,192。协议决定未推进到宿主实现，形成实验与正式路径断层。

### 方案候选

按用户点名两项先落地：Understanding 直接生成 `VideoParseResult` JSON，宿主一次校验并原子写 Canvas；Refine 保留工具。复用 Gateway 结构化通道，不新增协议开关。默认和本机 `vision` / `vision_fallback` 改为 30,000，其他角色预算不改。字段精简/SHOT 需独立兼容迁移，本次保留原合同。

### 方案验证

`tests/agent/video_parse_v2`、`tests/server/test_video_parse_worker.py`、`tests/server/test_parse_output_budget.py`、`tests/gateway/test_config.py` 合计 **380 passed**；日志 `outputs/verification/parse-direct-json-20261030/tests.log`。覆盖无工具输入、人物/事件完整往返、词对齐原证据、非法时间整批拒绝、清理/重试、Refine 工具及隔离入库/检索组件链路。预算/配置三文件另有 409 passed；扩展 Gateway 测试有一项已有上下文分支回归失败，未修或宣称全量通过，详见主 TRD §4.9。未重启、未迁移旧任务、未实模验收或测时。

### 指标对比

| 项目 | 修改前 | 修改后 |
| --- | --- | --- |
| Understanding 工具职责 | 读/写/回读/完成 | 0 工具；正常成功路径一次 JSON 生成，宿主校验与导入 |
| 视觉主模型/fallback 输出上限 | 8,192 | 30,000；旧任务冻结值不变 |
| 维护成本 | 模型维护 Canvas revision/batch/事件操作 | 宿主一次批次导入，Refine 继续复用原工具 |
| 时长、token、业务质量 | 历史协议实验 | 新正式链路未实测，不宣称提速百分比；输出上限用于避免截断 |

源码在 dev 工作区；服务启动时加载配置，普通重试不会改旧快照。新默认需在服务加载修改后用于新解析版本。

<a id="devlog-node-retry-20261030"></a>
## 2026-10-30：Understanding 节点自行重试，最终异常停止视频任务

### 问题与现状

hypnos 要求失败节点自行重试，避免 Kimi 理解失败后 Consumer 重排整个 Chunk、连带再次运行已成功的本地 ASR。原链路的观察结果在单次子图内，整块重试无法复用未提交的 ASR。

### 方案候选

采用 Understanding 子图内 ASR、visual、OCR、understanding 分节点有限重试。暂态超时/传输异常使用任务冻结配置的重试次数与退避；总截止时间与持久模型调用预算不重置。最终异常上抛给 Consumer 后，只记录失败并通知，不再重排 Parse Chunk。同视频停止新领取，拒绝兄弟任务续租及迟到的 Chunk/Interval 提交；首个结构化错误保留，前端沿用任务 API/SSE 展示失败阶段和原因。提交不自动重放；进程崩溃恢复及 Index/Refine 策略独立保留。

### 方案验证

Framework、Queue、Worker、模型调用预算、API、SSE、Bootstrap 定向回归 580 passed（59.75 秒）。故障注入验证理解成功恢复与重试耗尽时，ASR/视觉准备均只执行一次；耗尽不提交结果，Consumer 不重排 Parse Chunk，Queue 拒绝兄弟任务续租与新领取，任务 API 返回受控错误。补充 Interval 提交保护后，Worker/Queue 回归 299 passed（12.08 秒）。随后增加并行协调器不能覆盖首个节点错误及迟到 Chunk 提交的用例，最终结果以本条补充记录为准。

未重启服务、未调用真实模型、未迁移或清理真实任务。原生 ASR 已进入的阻塞调用仍需在返回控制点后取消，不能将此次测试写成内存峰值实测改善。手动暂停单视频/全量及任务清理未实施；状态统一见 B22。

### 指标对比

| 指标 | 原路径 | 当前离线证据 |
| --- | --- | --- |
| Understanding 超时重试范围 | Consumer 重新调度整个 Chunk | 节点内重试；成功观察复用 |
| 理解失败后的 ASR 调用 | 整块再次执行可能重复转写 | 恢复/耗尽用例均为 1 次 |
| 最终失败处理 | 排队直至任务重试耗尽 | 记录首个失败、停止派发、通知前端 |
| 长视频真实完成率/内存峰值 | 仍有未完成任务 | 本轮未实测，不宣称改善数值 |

最终补充验证：并行协调器错误不覆盖首个 `PARSE_NODE_FAILED`，失败后迟到 Chunk 提交被拒绝；最终 Worker/Queue 回归 **299 passed（14.09 秒）**，`git diff --check` 通过。上述测试集合有重叠，不相加为独立用例总数。

## 2026-10-30：任务页默认显示当前解析版本

### 问题与现状

用户将 Karpathy 历史失败卡误认为当前任务失败。前端展示所有版本，后端只允许最新版本重试，界面没有解释这个差异。

### 方案候选

复用全分页列表已有 videoId/version 映射，加当前／历史／全部版本筛选与替代说明；统计随版本范围改变，状态筛选在版本判定后执行。保留历史，不扩展上传聚合删除语义。

### 方案验证

任务页 21 项回归与生产构建（含 TypeScript）通过；初次构建发现新增测试使用不支持的 exact 参数，移除后重跑通过。Playwright 默认浏览器未安装，使用现有 Chrome 连接独立 5174 预览和真实 API：当前 10、历史 19、全部 29 条，历史操作按钮 0；390px 无横向溢出。截图位于 /tmp/task-history-{current,history,mobile}.png。没有修改真实业务数据或重启解析 Consumer，5173 页面尚未部署。

### 指标对比

| 指标 | 原行为 | 实测新行为 |
| --- | --- | --- |
| 默认展示 | 29 条混合版本 | 10 条当前版本 |
| 历史入口 | 混在当前列表 | 19 条可单独筛选，显示替代说明 |
| 历史重试／删除 | 无按钮但缺解释 | 仍无按钮，解释为何只供追溯 |

范围为本次只读列表样本与组件回归，不能证明解析成功率改善。部署待合并，状态见 problems。

## 2026-10-30：直接 JSON 模型连接失败诊断与恢复

### 问题与现状

Karpathy v3 的 4 次成功 Kimi 请求耗时 183–296 秒；EP1 504 请求实际 312 秒，不能把含 ASR 的 969 秒当成推理时长。EP4/5 与 EP7/8 各有 4 个请求在同一时刻连接失败。现有日志遗漏 cause，无法确定断连责任端；SDK 错误未命中节点重试。

### 方案候选

采用 Gateway 通用错误分类、现有有限节点重试及脱敏日志。直接 JSON 接上已有 opt-in 诊断，记录每次调用的异常链；保留既有并发、租约与 deadline。仅增加提示不足以恢复暂态错误，无限重试会掩盖持续故障，均不采用。

### 方案验证

使用真实 OpenAI SDK 和 MockTransport 产生连接、读取超时、429/500/502/503/504 与 400/401，检查原因与重试资格；工作流验证最多 3 次且 ASR 不重复执行；取消优先，公开错误不包含原始 provider 内容。测试结果见 [TRD](../design/03-product-reassessment/model-network-failure-trd.md)。本机证据 `outputs/verification/karpathy-start-20261030/kimi-call-timings.json`，按模型请求起止统计。EP1 正式 retry 继续原 v3，整集完成仍待验证。

### 指标对比

| 指标 | 基线 | 本次改造 |
|---|---|---|
| 连接/超时/504 公开原因 | 执行异常 | 分别显示连接异常、模型请求超时、服务网关超时 |
| 节点暂态重试 | SDK 错误漏判 | 最多 2 次重试，共 3 次尝试，沿用 deadline |
| 原始原因追溯 | 直接 JSON 缺 trace、终止缺 cause | opt-in 逐请求 trace + 脱敏终止异常链 |
| 正式完整视频成功 | 0 | 待验证，不能用离线测试代替 |


## 2026-10-30：解析任务暂停、继续与准备阶段显示（B22）

### 问题与现状
用户只要 Building GPT，Micrograd 仍占视频容量并运行 3 个 Chunk；Building GPT 实际已取得容量，13:26 起执行整集 ASR，页面仅显示“准备中”。

### 方案候选
采用独立持久 paused 标记，保留 accepted 与业务 phase，暂停撤销父/子/Prefix/Summary 租约、释放容量，继续沿用任务和已提交内容。未采用删除媒体或伪造失败。兼容旧 Consumer 使用远期调度时间；新调度入口显式检查 paused。原生 WhisperX 不可抢占，迟到结果以租约隔离。

### 方案验证
队列/总结/实时解析相关 180 项测试通过；补充门禁和 API 状态投影后暂停专项 5 项通过；任务页 21 项通过，包含暂停→继续请求及 leaseVersion 变化。开发库在 13:43 执行暂停 Micrograd，日志出现取消/lease lost 且 recorded=false，旧执行没有覆盖暂停事实。Building GPT 原 lease=1、PID 40888 持续运行。13:56 本机 5173 浏览器已验证进行中=1、整集转写与暂停入口、暂停筛选；正式控制 API 幂等暂停成功、旧 lease 返回 409。暂停专项最终 6 项通过，包含旧实时 Consumer 不能绕过暂停；合并历史筛选后任务页 22 项通过。整集完成仍未验证。

### 指标对比
Micrograd 活动 Chunk 租约 3→0、容量 true→false，未删除视频、内容或创建新解析版本。新增控制事务和两个公开控制布尔能力字段；无数据库 schema 迁移。整片 ASR 为内存缓存，重启会丢失运行，因此不能用部署重启冒充无损切换；执行速度与整集质量收益未测。
<a id="devlog-full-video-asr-rollout-20261030"></a>
## 2026-10-30：GPT 整集启用视频级 ASR 与 10 路 Understanding

### 问题与现状

关联 B20。16:00 演示目标最终确定为完整《Let's build GPT》，本地第 7 集、时长 6,979.873 秒、117 个 Chunk。此前 v4 仍使用逐块 WhisperX，Parse 并发 6；与本机其他任务共享串行 ASR runtime，理解槽位包含 ASR 排队。用户明确要求将整片 ASR 改造上线，避免继续只验证分支。

### 方案候选

继续逐块解析可以保留已有进度，但不能满足本次明确要求的 ASR 前置。选用已实现的整片 ASR 方案，合入 dev 网络修复后部署 PR #12；通过正式 reparse 创建 v5，冻结 `scope=video`、Parse 并发 10、0.5 FPS，并将 ASR 超时从 1,200 秒调为 14,400 秒。超时是保护上限，不是 ETA。保持 WhisperX large-v3、CPU/int8、batch 1 和对齐合同；Refine 并发 10、Video 准入 3 保持现有值。整片 ASR 完成后才能派发 Chunk；缓存仍不支持跨重启恢复。

### 方案验证

同步 dev 网络修复后的定向回归为 **367 passed，18.13 秒**，覆盖整片 ASR、Queue/Worker、OpenAI 网络错误及 Understanding 框架，与此前测试集合重叠。PR #12 于 13:22 合入 dev `029faa34a332d4c1fcc4550985bf6ba261cc92d9`；演示用本机 development API PID 40888 于 13:24 启动并通过健康检查。没有云端或独立 production 环境部署，也没有数据库迁移。

GPT v5 任务 `650f7c9c-1e69-43b9-ac63-12bd355ede1e` 于 13:26:08 通过 `/api/uploads/reparse` 创建，13:26:09 正式日志记录视频级 `phase=asr`。音轨头核验为 16 kHz 单声道、6,979.873 秒、223,356,200 字节。进入 ASR 时任务为 PREPARING，Chunk 尚未派发，符合前置门禁。旧 v4 已停止；已提交的 8 个 Chunk、2 个 Refine 与 1 个索引结果留在历史版本，不冒充新版本成果。确认旧 API 已退出后，使用既有 `release_execution` 释放其剩余 3 个子任务租约，再提交新版本。

原片 HTTP Range 返回 206；浏览器实际播放从 00:00 推进至 00:10，显示整集 116:19。检索、引用跳转和追问尚待新版本完整索引后验收。配置快照、部署元数据、回归日志、音轨核验与阶段观察保存在本机 `outputs/verification/gpt-full-demo-20261030/`；主要文件为 `deployment.json`、`video-asr-submitted-task.json`、`full-asr-audio.json`、`deploy-regression.log` 与 `video-asr-progress.jsonl`。

### 指标对比

已核实的变化为逐块 ASR → 视频级前置、Parse 槽位 6 → 10、ASR 超时 1,200 → 14,400 秒。尚未取得整集 ASR 完成耗时、真实 10 路 Kimi 同时执行、全流程耗时和课程回答质量证据，不能声称 16:00 前一定完成或准确率提高。没有增加数据库表或新服务；代价是首个理解 Chunk 需等待整片转写，进程重启仍会重做 ASR，Refine 可能使 Kimi 总在途请求数超过 10。


<a id="devlog-whisperx-progress-20261030"></a>
## 2026-10-30：WhisperX 阶段、进度与本轮无重启观测

### 问题与现状

关联 B20。GPT 整片 ASR 从 13:26:09 开始，13:32:30 记录英语语言识别，随后原生采样确认 CTranslate2 Whisper 解码活跃；现有日志无法区分等待共享 runtime、模型加载、转写与对齐，更无法回答当前百分比。13:34 任务仍为 PREPARING，用户要求预估耗时并补日志。

### 方案候选

接入已安装 WhisperX 3.8.6 的 `transcribe(progress_callback=...)` 和 `align(progress_callback=...)`，在 Gateway 记录通用调用进度，宿主只透传关联 context。采用 INFO `local_asr_progress`，覆盖等待、读音频、模型加载、转写（包含 VAD）、对齐模型加载、对齐和结果归一。首次上游回调、每增加约 5 个百分点及达到 100% 时记录进度；独立日志线程每 30 秒记录阶段存活，退出时停止并回收。

保留音频长度、设备／精度／batch／线程、调用 ID、总／阶段耗时、阶段百分比和距上次推进时间。百分比基于上游片段，不换算成视频分钟或端到端完成率；无回调时保留 null，不伪造 VAD 百分比或精确 ETA。转写 100% 仍需对齐；异常只记类型，不写转写正文与原始异常文本。观测回调不改变结果、超时、取消和原生资源释放语义。

### 方案验证

WhisperX、转写工厂、用量及整片 ASR 定向回归 **85 passed，4.90 秒**。覆盖模型／aligner 复用、字词合同、阶段切换、调用关联、进度节流、对齐失败不报告成功、共享 runtime 等待超时，以及无上游回调时仍有存活日志且调用退出后线程结束。这些是替身模型验证，不冒充新的真实 ASR 速度或识别质量实验。

本轮正在运行的调用早于代码修改，不能安全补入进度回调。未为日志重启 PID 40888；启动只读 `trace-live-asr.py`，每 30 秒记录正式任务状态、ASR 日志阶段／累计耗时、API 负载、内存及 Chunk 进度。13:46 仍为原租约 1、ASR running、约 20 分钟、无终止错误，百分比未知。原始证据在本机 `outputs/verification/gpt-full-demo-20261030/` 的 `asr-live-trace.jsonl`、`asr-live-trace-latest.json`、`progress-logging-tests.log`、`asr-historical-timings.json`、`asr-estimate.json`。Adapter 新日志需进程加载新代码后才能用于新调用。

### 指标对比

此前同机两课程共享串行 runtime 的连续 14 个完成间隔中位数 53.007 秒、范围 46.835–127.589 秒，对应约 60 秒音频块。避免把包含锁等待的单次调用总时长直接当推理时间；按中位数线性外推完整 6,979.873 秒约 103 分钟，暂以 90–120 分钟作为安排时间的粗估。小样本、整片 VAD／对齐、后续资源竞争均可能改变结果，不是测得的整集 ETA，16:00 完整闭环仍有时间风险。

增加的是每个在途 ASR 调用一个低频日志线程和有限回调，不增加模型实例、外部服务、数据库表或业务状态。当前任务获得连续状态记录，未来调用可以精确区分阶段及上游进度；本轮已执行部分仍无逐段百分比，不能追溯恢复不存在的日志。


<a id="devlog-half-episode-ab-20261030"></a>
## 2026-10-30：连续半集对照未完成与十分钟业务核查

**问题与现状。** 用户要比较当前体系完整解析连续12分钟，与 Doubao non-thinking 带前文串行解析同一段的业务质量及效率。执行超过一小时仍未交付完整对照，用户质疑 token 浪费；准备隔离环境和运行恢复占用过多时间，不能作为完成目标的替代。

**方案候选。** 独立 PR #19 沿用当前 V2 Understanding/Refine/持久化/索引，另一臂采用精简 JSON/SHOT、30000 输出、真实前序输出、无 Refine；共享 ASR 与核心3fps720p帧，A另保留两侧3秒视觉上下文。现方案比较整体策略，不归因 thinking 单一因素；保留当前生产实现。

**方案验证。** A 首次 HTTP 504，原任务一次恢复仍无正文后取消；B 前十分钟完整原文309条、7块严格合同通过，第11块read90s超时，第12块未请求。先冻结60项源片需求，再核查已有输出对应50项：36核心事实保留、6局部可用、6错误、2源证据不足。跨块错人、跨人合并和时间错位影响精确素材；也有对白、纸杯、电车与追逐情节保留的正例。37项相关离线测试通过，不能替代质量验收。已停止远程请求和实验独占服务，证据/数据保留。

**指标对比。** A无内容，不给虚构比较；B完整请求92.02–143.89秒、中位109.52秒，尝试墙钟1226.20秒；已报告2,517,767 input/19,434 output tokens，B一次超时与A两次尝试usage缺失。未复现30秒，也未完成配对；已有证据支持精确直出不达标、有粗检索价值，不支持笼统“几乎不可用”。B07继续TODO，本轮不追加调用。见 [TRD](../design/03-product-reassessment/parse-half-episode-ab-trd.md)、[完整报告](../report/agent/video_parse_v2/parse-half-episode-ab-20261030.md)、[PR #19](https://github.com/owariband/Seekino/pull/19)。
