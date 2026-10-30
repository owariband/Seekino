# 项目问题清单

> 2026-10-24 旧实验退役说明：下文保留当时的观察；声纹、检测、图片出处实验的原始材料已删除，链接转到[结论摘要](design-experiment-cleanup.md#第四轮旧实验整体退役构图研究仅留本地归档)。构图／shot-tags 数据仅保存在被 Git 忽略的本地归档中，详见[恢复说明](design-experiment-cleanup.md#本地归档与恢复)。

整理日期：2026-10-20。本次仅整理文档结构与排序，不代表重新验收历史问题。

**维护规则（后续 Agent 补写或更新时必须遵守）**

1. **问题只分为“已解决”和“TODO（待修复解决）”两大块。** 原文明确主体问题已修复或疑问已澄清的条目归入“已解决”；尚未修复、仅部分完成，或同条目仍包含未解决主体问题的条目归入 TODO。代码修复、服务加载、真实模型／浏览器验收和性能收益必须分别说明，不能因归入“已解决”就宣称全部验收通过。
2. **两大块内部都按“排序日期”从新到旧排列，不再按 frontend、backend 或 Chat 分组。** 每条保留 `### 编号：标题`，紧接一行 `排序日期：YYYY-MM-DD。`；排序日期取该条最新的实质进展日期（诊断、实施、复核或验收），优先使用条目明确记录，缺失时可继承原所属章节的明确日期。不要把引用文件名、历史故障时间或本次排版日期当作更新日期。
3. **同日按问题编号字母顺序、再按数字升序排列；无法确定日期的条目标为“日期待确认”，放在所属块末尾。** 不得根据条目位置或整份文档的更新日期补造日期。后续补齐有依据的日期后，再移到正确位置。
4. **新增或更新后立即重新排序，不能一律追加到文末。** 补充进展时保留原日期与证据，更新该条排序日期并移动整个条目；状态改变时整体迁入对应大块。一个编号只保留一份正文，F（frontend）、B（backend）、C（Chat）编号保持不变，不因移动重新编号。
5. **不得丢失或以摘要替换已有内容。** 问题现象、根因、方案、已做与待办、日志／样本 ID、代码、表格、链接、验证结果和证据边界均须保留；相互矛盾的历史状态用日期和说明区分，以最新实质记录为准，不悄悄删除旧结论。新增有日期的进展放在条目现状附近，并保持其独立记录按日期倒序；已有根因→方案→验收等完整叙述不拆散。
6. **交付前核对完整性。** 确认问题编号无遗漏或重复、两块日期均为降序、无日期条目位于末尾，正文和引用未因重排丢失。整理前的完整旧文档（含历史更新、联调表和能力边界）已归档至 [problem_backup.md](problem_backup.md)，该备份已废弃、不再维护，后续只更新本文件。旧记录中的历史“待实施／未重启”不能直接当作当前部署状态。

## 已解决

以下按原文主体问题的修复或澄清记录归档；待部署、待真实样本复测及性能验收等限制仍以各条正文为准。

### B21：Kimi Code API 10 路并发能力未验证

排序日期：2026-10-30。

状态：已解决（当前账号短请求并发疑问已实测澄清）。使用开发配置同一 Kimi Code Key / `k3.1-pro`，先做 3 次串行对照，再连续两轮各 10 路并发；23/23 请求 HTTP 200、JSON 标识与答案正确，零 429、零自动重试。两轮并发整组耗时分别为 6.582 s、7.646 s，峰值客户端 HTTP 在途均为 10，合计 6,484 tokens。

本次只发送合成代码题，未调整服务槽位、重启或改写业务数据。该结果支持当前账号可用 10 路短请求，不证明服务端推理实例数量、长期频控余量或长视频并行质量/吞吐；实际解析优化仍由 B07 跟进。配置里的 `codex_local.max_concurrency=10` 与 Kimi 无关。

依据：[实测报告](../report/gateway/kimi-concurrency-20261030.md)、[devlog](devlog.md#devlog-kimi-concurrency-20261030)、[逐请求原始结果](../../outputs/verification/kimi-concurrency-20261030/results.json)。

### B16：面试前保护旧闭环并撤销 NPC 实验与 ACP Client 裁剪

排序日期：2026-10-29。

状态：已解决（本轮裁剪已撤销）。用户随后明确 2026-10-30 面试需用旧闭环展示，旧能力还承担其他工作依赖，不能仅按新创作目标判断删除。已恢复此前删除的 9 个文件及 `agent-client-protocol` 依赖，并撤销对应退役说明；MCP 和其他任务已有修改保留。恢复后的定向验证见 devlog。

面试前停止进一步裁剪和结构重构，保留解析、导出、检索、问答、陪看、Cloud、素材管理及其依赖。SVG／Three.js 角色宣传片保留为探索方向，不作为明天已完成的演示承诺。历史删除经过仍保存在 [原记录](devlog.md#devlog-pv-scope-prune-20261029)，恢复决定与当前边界见 [恢复记录](devlog.md#devlog-interview-preserve-20261029)。源码恢复不等于演示环境已启动或真实模型闭环已复验。

### B18：全原生 localhost 环境与真实业务验收

排序日期：2026-10-29。

类型：本机部署、运行验证。状态：**已解决：全原生 MySQL、ES、API、Web 已运行，授权的 12 秒短片完成上传、Kimi 解析/总结、索引、检索、观看问答及重启后持久化验收。** 用户最终选择本机开发，不运行 Docker，要求整套服务跑通，并明确允许这段 12 秒短片调用外部模型，随后要求尽量使用 Kimi。


2026-10-29 19:28（Asia/Shanghai）闭环验收：同一视频版本 4 为 `COMPLETE`，API 记录处理用时 316,737 ms；当前 MySQL / ES 精确对应 1 个 Chunk、6 个 Event。检索“粉色长发女生说春日影是首好歌”命中正确视频与 [0,12012) ms 范围，无 warning；Kimi 观看问答实际调用 `understand_current_video`，42.8 秒完成，回答原句与画面字幕一致，两条消息及 checkpoint 均落库。摘要和封面已发布并核对磁盘/SQL/HTTP 内容。保持真实 SSE 连接时四项原生服务 10.418 秒正常停止，随后重启，任务、索引、媒体、摘要、消息和 checkpoint 再次核对全部通过。

播放器最终实查发现已加载媒体仍禁用进度控件，修复 metadata 初始化与同媒体时长刷新后的状态同步；浏览器已验证进度条定位及后退 10 秒，暂停位置为 2.012 秒。相关播放器/观看页测试 48 passed，播放器/进度/身份隔离回归 51 passed（两组重叠），类型检查与最终 production build 通过。临时模型 trace 配置及防休眠进程已关闭，四项本机服务保留运行。七个生成式角色配置为 Kimi；ASR 使用 Ark `doubao-seed-2-0-lite-260428`，向量使用已实测的 `doubao-embedding-vision-251215` / 1024 维及独立新索引。

验收范围是这一短片与单次检索、单轮观看问答；没有据此宣称长视频质量或跨视频召回全部达标。旧 12 集原片归档不等于旧 MySQL 解析成果已恢复，B17 继续保持 TODO。证据：[重启后持久化报告](../../outputs/verification/native-local-20261029/persistence-report.json)、[真实 SSE 停服](../../outputs/verification/native-local-20261029/sse-shutdown-report.json)、[浏览器结果](../../outputs/verification/native-local-20261029/browser-acceptance-final.jpg)。

以下是闭环前的初验记录，保留配置错误与失败版本的追溯信息：

- 已实施：从官方安装包配置 MySQL 8.4.13 / Elasticsearch 8.19.22 Apple Silicon 原生运行时；本机 `.env.local` 指向安装路径，数据分别保存到 `data/mysql` / `data/elasticsearch`，媒体仍为 `assets/library`。数据库底层文件不提交 Git，但明确不是缓存。启动器增加 ES 原生启动与既有 checkpoint / Event index 初始化，保留已存在实例复用和旧 MySQL 目录保护。
- 兼容修复：本机 FFmpeg 9.0.2 已移除 `-filter_script:v`，抽帧改为探测文件参数能力并使用 `-/filter:v`；旧版本显式不支持时仍使用旧参数。原生启动器专项最终 82 passed，抽帧与解析媒体 51 passed，前端 production build 通过。
- 实机验证：四个服务均仅监听 `127.0.0.1`，API / Parse Consumer 健康，Web 代理查询与直接 API 一致；MySQL 28 张表及 22 条 checkpoint migration 可读，Chunk / Event 两个索引可用。ES 单节点 yellow 的未分配项均为副本，两个主分片正常。执行完整 `stop → start` 成功，MySQL 未重复初始化；浏览器正常显示新开发库空状态。Docker socket 不存在，云服务器未部署。
- 真实验收进展：EP04 8–20 秒、12 秒验收片已上传；HTTP 全文 SHA-256 一致、206 Range 精确字节和 H.264/AAC 元数据检查通过。自动审批最初要求确认媒体外传，用户随后明确授权并指定优先 Kimi；本机 `config/local-dev-config.yaml`（原 `local-kimi.yaml`，2026-10-29 更名） 将七个生成式角色设为 `kimi_coding/k3.1-pro`，转写与向量仍为 Ark。首版因继承视觉 `reasoning_effort=high/medium` 在调用前失败，修正为 `none` 并通过七角色无网络构造检查；显式带新配置重解析同一视频为版本 2，保留版本 1 的失败证据。Kimi OCR 实际请求已返回成功，理解仍在执行；检索与观看问答尚未验收完成。不能将环境健康写成视频全链路验收完成，也不等于 B17 的旧 MyGO 成果恢复。

依据：[原生启动说明](../../deploy/README.md#可选全原生本机开发)、[运行报告](../../outputs/verification/native-local-20261029/runtime-report.json)、[安装与检查记录](../../outputs/verification/native-local-20261029/setup-report.json)、[本次 devlog](devlog.md#devlog-native-local-20261029)。


### F10：普通展示模式隐藏解析有效总时长

排序日期：2026-10-29。状态：已解决（代码与定向测试；生产部署未验证）。

任务 API 已返回有效解析时长，但任务页以 `isDebug` 限制显示，prod/release 用户无法查看。现已解除该字段的 debug 限制，沿用服务端实际执行区间的时长口径和未开始／未记录空态，排队与停机不计入。内部 Chunk、Refine、索引计数和模型用量明细继续由 debug 控制。

任务页 20 项测试、TypeScript 检查通过。未重启或部署生产服务。依据：[任务页设计](../ui-design/tasks-simplification-approved.md)、[本次 devlog](devlog.md#devlog-release-parse-duration-20261029)。

### F07：观看聊天面板的固定栏和输入工具栏挤占正文空间

排序日期：2026-10-29。

状态：已解决（本机浏览器验证）。用户截图中“边看边聊”标题、聊天/选集页签和常驻“发送问题时暂停视频”开关占据三行，底部附件、系列选择和发送按钮所在行也过高。已删除冗余标题，将页签、观看设置和关闭按钮合并为一行；暂停开关移入按需展开的观看设置，保留原偏好持久化与发送暂停行为。嵌入式聊天输入区减少内边距，附件/发送按钮缩为 30 px，并限制长系列名溢出。

本机浏览器在 491×815 和 1280×900 视口实测：顶部工具栏 41 px、底部工具栏 30 px、空输入框 88 px，页面没有水平溢出；设置开关、Esc 收起与焦点恢复、点击外部收起、选集切换和面板开合通过。观看页 24 项测试、类型检查和 production build 通过。本轮只验证 UI，不新增模型调用，也未把 B17 的旧资料库恢复标为完成。

依据：[本次 devlog](devlog.md#devlog-watch-chat-compact-20261029)、[窄窗口截图](../../outputs/verification/watch-chat-compact-20261029/compact-chat-narrow.jpg)、[桌面截图](../../outputs/verification/watch-chat-compact-20261029/compact-chat-desktop.jpg)。

### F08：Summary 标题未落到资料库展示且无正式标题时缺少回退

排序日期：2026-10-29。

状态：已解决（本机短片真实验收）。Summary Agent 已在全部 Refine 提交后接入，先前 Kimi 验收已发布简介及封面；用户仍看到文件名，是因为原规则缺少标题卡就返回 null，而资料库没有优先消费 `displayName`。用户确认改为“优先正式标题，没有时按内容生成简短标题”。

已增加 `title_kind=official|generated`，正式标题保持直接证据要求，无可靠正式标题时生成有引用的中文短标题；生成标题硬上限 80 字符，集数仍不允许推断。类型及证据保存在现有 summary 结果 JSON，无需新迁移。资料库优先 `displayName`，原文件名保留 tooltip/回退。

复用 EP04 8–20 秒验收片的原 Chunk，只重跑 Kimi summary，59.591 秒完成，5 次模型请求均成功。标题由 null 变为「台阶夜谈《春日影》」，SQL、资料库和观看页一致；解析仍为版本 4 / COMPLETE，Chunk 版本和来源指纹未变，未推断实际集数。后端相关测试 75 passed，前端 App/format 65 passed，typecheck/build 通过。确认无活跃任务后仅重启 API，MySQL/ES/Web 保留原进程，健康与资料库访问通过。整季标题质量尚未验证，历史结果不会自动补跑，B17 旧业务数据恢复仍独立保留 TODO。

依据：[主 TRD](../design/02-agent-harness/video-summarize-trd.md)、[本次 devlog](devlog.md#devlog-summary-title-20261029)、[真实结果](../../outputs/verification/summary-title-20261029/after.json)、[资料库截图](../../outputs/verification/summary-title-20261029/library-title.jpg)。


### F09：系列封面未使用 Summary 候选及上次观看画面

排序日期：2026-10-29。状态：已解决（代码、隔离回归与本机单候选实查）。用户要求未观看系列随机使用各集 Summary 选出的封面，观看后改为上次观看位置的原片画面。

Library 原先只挑第一部有关键帧的视频；现在优先从已发布 Summary 封面中稳定随机选取，候选不变时刷新/集数排序不会更换。观看首页卡片、大图及系列页使用有效观看记录覆盖默认图；没有 Summary 时保留原片预览。沿用身份隔离的浏览器记录，预览不写进度，无跨设备同步或新增数据库结构。

后端 74 项、前端 47 项相关测试及 typecheck/build 通过。真实页面验证同一视频的无历史 Summary 静态图与已有历史 8.544 秒画面，未修改用户观看记录；服务已加载新规则。当前 Karpathy 尚无 Summary 封面，仍走回退；多集真实审美效果待产出后观察。设计：[Video Summarize TRD](../design/02-agent-harness/video-summarize-trd.md)；证据与取舍：[devlog](devlog.md#devlog-series-artwork-20261029)。

### B14：Event 索引补建反复重写大 JSON，45 分钟产生 20.61 万个 UPDATE 事件与 44.106 GB binlog

排序日期：2026-10-19。

日期：2026-10-18。优先级：高。类型：数据库写入放大、无变化更新与日志留存成本异常。状态：**Claude 已完成本次代码修复：Event 缓存批量保存、无变化聚合不更新时间，以及配套的任务扫描游标；本轮已核对源码，七天留存已生效。** 下文保留修复前现场和根因，并记录已实施范围、踩坑教训与后续优化；真实负载下降幅度及服务加载情况未在本轮复测。本项与 B08 的向量缓存存储边界、B13 的大 JSON 读取负担相关。

#### 触发工作与统计口径

- 当日通过现有 `request-es-index-rebuild --with-events` 为 MyGO 13 集补建 Event 索引，复用已有 Parse / Refine / Timeline。15:31 第一集试跑，随后提交其余 12 集，16:17 全部完成；共 **312 个 Chunk、7,103 个 Event、16,535 条帧采样记录**。帧记录包括不同 Event 间的重复采样，不等于独立图片数量。
- 精确观察窗口为 **2026-10-18 15:31:46–16:17:04（UTC+08:00，首尾秒包含，约 45 分钟）**。后文 GB / MB 均为十进制；磁盘占用、文本字节、SQL 字段长度和 binlog 事件大小分别标明，不混用。
- binlog 是二进制数据库变更记录，不是按换行划分的文本日志；`UPDATE_ROWS` 事件数也不直接等于 SQL 语句数、事务数或实际受影响行数。不能将 23,334 条 embedding 成功日志写成“23,334 行 binlog”。

| 应用日志指标 | 精确窗口内数量 | 含义 |
| --- | ---: | --- |
| `outputs/local-services/api.log` 物理日志行 | **30,324 行，约 3.03 万行** | 窗口内每行均有时间戳；文本本体 **4,763,577 bytes，约 4.764 MB** |
| `/embeddings/multimodal` HTTP 200 成功记录 | **23,334 行，约 2.33 万行** | embedding 成功请求日志，不是数据库更新次数 |
| INDEX `running` / `completed` | **312 / 312** | 312 个唯一 `(task, execution)`，13 个父任务各 24 个 Chunk |
| 重复开始的同一 `(task, execution)` | **0** | 没有证据表明该窗口存在同一 Chunk 无限重试；不同 Chunk 的历史 lease 代次不能混作重试次数 |

应用日志按行头时间做闭区间过滤，embedding 只计明确的 HTTP 200；INDEX 只计 `phase=index` 的对应 outcome。原文件该窗口为物理行 101170–131493，后续日志轮转时应按时间及事件重新定位。较宽的 15:30:00–16:17:59 窗口有 30,383 行，但 embedding 和 INDEX 数量相同；本项使用精确窗口。

#### 空间现场与 binlog 直接证据

- 用户发现项目文件夹显示约 **125.68 GB** 后开始排查。初次 `du` 核对 `outputs` 实际分配约 **97.08 GB**，其中 `outputs/local-services/mysql` **64.363 GB**、binlog **63.335 GB（约占 MySQL 目录 98.4%）**；全部 `.ibd` 文件仅约 **0.845 GB**。Finder 逻辑大小与磁盘块计量不同，不能将这些值直接混加。
- 初次按 binlog 文件结束时间／mtime 归档：**10 月 18 日约 48.463 GB**、10 月 17 日约 7.511 GB、10 月 16 日约 2.148 GB；其中 `.000094`–`.000134` 在 15:32–16:16 连续滚出约 **44 GB**。文件级日期统计不能代替逐事件时间统计，也不能将全天 48.463 GB 全部算入上述精确补建窗口。
- MySQL **8.4.11**，`log_bin=ON`、`binlog_format=ROW`、`binlog_row_image=FULL`，`max_binlog_size=1 GiB`（轮转阈值，不是单事务的硬性大小上限）。异常主要是大量数据库更新的历史镜像；上述 3.03 万行普通应用日志本体只有约 4.764 MB。

**全量复核：** 2026-10-18 22:18:33–22:18:48 只读扫描当日 mtime 的 **48 个** binlog（`.000089`–`.000136`），按每个事件头的时间戳筛选，取得以下精确计数：

| 时间范围（北京时间） | 全部 binlog 事件数 | 其中 UPDATE_ROWS 事件数 | 全部事件字节量 |
| --- | ---: | ---: | ---: |
| 10 月 18 日，截至扫描快照 | **1,733,251，约 173.33 万** | **321,869，约 32.19 万** | **47,785,845,629 bytes，47.786 GB** |
| 补建窗口 15:31:46–16:17:04 | **1,144,814，约 114.48 万** | **206,137，约 20.61 万** | **44,105,531,159 bytes，44.106 GB** |

补建窗口的表级 UPDATE 统计全部属于 `seekino_local_20261010_development`：

| 表 | UPDATE_ROWS 事件数 | UPDATE 事件字节量 |
| --- | ---: | ---: |
| `chunk_parse_content` | **31,060，约 3.11 万** | **33,831,258,083 bytes，33.831 GB** |
| `video_parse_task` | **60,420，约 6.04 万** | **10,091,842,747 bytes，10.092 GB** |
| `chunk_parse_task` | **114,481，约 11.45 万** | **59,671,862 bytes，0.060 GB** |
| 其他三表（message / conversation / video） | **176** | **9,562,059 bytes，0.010 GB** |
| 全部 UPDATE | **206,137** | **43,992,334,751 bytes，43.992 GB** |

仅前两张大字段表的 **91,480 个 UPDATE_ROWS 事件**就贡献 **43.923 GB，占该窗口全部 binlog 字节量约 99.59%**；`chunk_parse_task` 虽然更新次数更多，字节量却仅约 60 MB，说明不能只按次数定位负担。全天前两表分别为 33,057 / 69,955 个 UPDATE，共 103,012 个、47.464 GB，约占当天事件字节量 99.33%。

扫描以各文件开始时的 size 为上限，包含最新 `.000136` 的 1,160 bytes 快照；所有文件扫描前后大小一致，错误 0、截断 0、未发现压缩事务事件。只读取事件头、TABLE_MAP 与 table ID（约 94.01 MB），通过 seek 跳过行正文；**上述全量次数是二进制事件数，不是逐行解码所得的受影响行数**。48 个文件逻辑长度共 48.447849752 GB，其中 `.000089` 含前一日晚间事件，所以严格按当天事件时间得到 **47.785845629 GB**；与初次按文件 mtime／磁盘块得到的 **48.463 GB** 口径不同。事件时间戳为秒精度，窗口包含结束时刻的整秒。

另完整统计并抽样解码 `binlog.000094`、`.000110`、`.000134`（共约 3.223 GB），两张大字段表在该样本占约 99.1%；这三个文件用于比对 before / after，不把样本占比替代上述全量计数。直接解码证据如下：

- `chunk_parse_content` 在三个文件样本中平均 **1.103 MB / UPDATE**，最大 **2.743 MB**；三个最大样本均仅修改 `detail` 和两个更新时间字段，**解析正文 `content`、`version` 完全未变**。`detail` 前后各约 1.28–1.37 MB，一次更新产生约 **2.58–2.74 MB** binlog。
- `.000094` 的父任务样本仅从 `_worker_runtime_v2.model_calls.active` 删除一个 **194 bytes** 的调用占位对象，却产生 **143,797 bytes** binlog；`.000110` 同类操作产生 **160,255 bytes**。
- `.000134` 的父任务样本仅改变 `update_time/update_date`，时间相差 **176 ms**，约 **149.6 KB 的 detail 完全未变**，仍产生 **311,808 bytes** binlog。heartbeat / lease 字段没有变化，不能将该样本归因于普通租约续期。
- 当前主库 **541 条** `chunk_parse_content` 的 SQL 字段长度统计：正文合计 **5.231 MB**，`detail` 合计 **318.805 MB**，单条 `detail` 最大 **1.753 MB**。这是字段长度口径，不是 InnoDB 物理页占用；几百 MB 的最终缓存反复重写，可以累计生成几十 GB 的 binlog。

#### 修复前已确认的原因链

1. **Event / 帧级 checkpoint，实际按整个 Chunk 缓存写回。** `VideoParseIndexCoordinator._index_events()` 在文本 embedding 完成、每一帧 embedding 完成、最终 Event document 完成时分别调用 `_save_representations()`；最多三帧时，一个新 Event 最多保存 **5 次**，另有 Chunk / 发布回执更新。`save_index_embeddings()` 每次复制整份 `detail`，合并之前全部 Event 的 representations，再将整个 `detail` 赋回。处理第 20 个 Event 的一个小步骤，也会重写前 19 个 Event 的缓存；在 Event 大小近似时，该部分累计写量可随单 Chunk Event 数呈平方增长，而不是仅追加当前向量。
2. **大向量以 JSON 字符串留在频繁更新的字段中。** `detail._index_embeddings_v2.representations` 使用 `StringEncodedJSON` 保存完整浮点向量文本；最终 Event document 含文本／视觉向量、文本和帧时间／SHA-256。此路径没有把图片 base64 或原始视频写入 MySQL；Event 完成时也会以最终 document 替换临时逐帧向量。真正的放大是中间阶段反复重写全部已累计 Event，不能误记为“所有临时图片永久存库”。
3. **每次模型调用都更新父任务整包 detail。** `reserve_model_call()` 追加 `used_call_ids`、写入 active slot，`release_model_call()` 删除一个 active slot；两者均整包写回父任务 `detail`。因此一次 embedding 至少有获取／释放两次父任务更新，且调用 ID 持续累积使后续更新更贵；长调用的 renew 和执行计时也使用相同的大字段。
4. **无实际状态变化的聚合仍写更新时间。** Consumer 每次 `run_once()` 先 `reap_expired()`，后者对 PARSING / INDEXING 父任务执行 `_finalize_draining_videos() → _aggregate_video_task()`；聚合末尾无条件设置 `update_time`。成功派发后可立即进入下一轮，从而产生仅更新时间的重复 UPDATE。这与 311,808 bytes 的时间字段样本吻合；本轮未逐条归因所有无变化更新的调用栈。
5. **`ROW + FULL` 再记录完整旧行与新行。** 即使只改更新时间或删除一个小 slot，未变化的正文、配置和大 detail 也进入 before / after；而改变整个 detail 时，本就需要保存的大缓存再次形成双份镜像。原生启动使用 `--no-defaults` 且此前未指定保留期，沿用 **30 天**，将持续写入放大进一步累积为磁盘占用。

关键调用位置：[逐 Event／逐帧保存](../../server/service/video_parse_index_coordinator.py) 的 `_index_events()`；[整包缓存、调用占位和无条件时间更新](../../server/repository/video_parse.py) 的 `save_index_embeddings()`、`reserve_model_call()`、`release_model_call()`、`_aggregate_video_task()`；[队列聚合](../../server/mq/video_parse_queue.py) 的 `reap_expired()`／`_finalize_draining_videos()`；[Consumer 循环](../../server/mq/video_parse_consumer.py) 的 `run_once()`；[缓存字段](../../server/model/job.py) 的 `ChunkParseContent.detail` 与 [字符串编码](../../server/model/types.py)。MySQL before / after 行镜像语义见 [官方 `binlog_row_image` 说明](https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html#sysvar_binlog_row_image)。

#### 踩坑教训：batch 要减少重复写入，不能只减少 commit

2026-10-18 复盘感受：“这下懂为什么 batch 高性能了；单条操作、没有 batch 提交，一次爆出好几万条 log。”本次需要把这个直觉落实成两个不同的优化层次：

| 做法 | 能减少什么 | 对本次大 JSON 写入放大的作用 |
| --- | --- | --- |
| 将 100 次 UPDATE 放进一个事务，最后 commit 一次 | 摊薄事务提交、往返和持久化的固定开销；实际收益取决于配置及负载 | 仍执行 100 次行更新，不能期待 binlog 自动只保留最终状态；大 JSON 的前后镜像仍会反复记录 |
| 将同一对象的 100 个可合并变化先累积，再执行一次 UPDATE | 同时减少 UPDATE 次数、整包序列化、行更新和 binlog 内容量 | 直接针对本次问题；适用于允许合并的中间 checkpoint，不能跳过业务必须持久保存的独立状态 |

**这次的主要教训是“提交太碎，而且每次提交都重写越来越大的累计对象”。** 仅把逐帧 UPDATE 包在一个更大的事务里，仍会重复写大字段；应先合并内存中的变化，在合适的恢复边界持久化。可以类比为“每写一段就重新保存整本笔记，数据库再复印修改前后两本”；需要减少整本重写的次数，而不只是把多次复印放进同一个提交步骤。这里借用 batch 的直觉，不将 Kafka 批发送机制等同于数据库事务。

- **可靠性需要明确恢复粒度。** “每一步都立刻保存”看似能少重做一个向量，但必须计算整包缓存、事务与日志成本。批量越大，硬崩溃后可能重做的未持久化工作越多；应同时限定批次大小和等待时间，在正常结束时 flush、可恢复异常时尽力 flush，并保留租约和版本校验。不能宣称进程硬退出时内存 pending 也能被保存。
- **完成单次功能验收，还要验收累计成本。** 小样本上单次 UPDATE 很快，不代表几千个 Event 的累计写入合理；同一数据集应分别统计模型调用、checkpoint 保存、UPDATE 事件、提交次数、binlog 字节与恢复重做量。此前 3.03 万行应用日志只有 4.764 MB，数据库却有 20.61 万个 UPDATE_ROWS 事件并产生 44.106 GB binlog，最值得看的是后者的次数与每次写入大小。
- **无变化轮询不制造持久化变化。** 状态聚合只读到相同结果时，不应仅为了表示“检查过”而更新时间；批处理也不能替代这一条。日志轮转／七天保留属于留存管理，不解决每轮重复写入的放大。

#### 2026-10-18 Claude 已实施的代码修复

- **Event 缓存先合并，再批量保存。** `_index_events()` 用 `pending` 在内存累积文本／逐帧结果；完成 Event 后，在 pending 达到 **8 个 Event**或距上次 flush 达到 **30 秒**时写一次缓存，正常循环结束再 flush。时间阈值在 Event 完成边界检查，不是可打断任意模型调用的严格 30 秒定时器；完成的旧缓存仍可恢复复用。
- **异常退出尽力保存，保留原错误。** 普通异常路径尝试 flush 已获得的 pending，再重新抛出原异常；租约失效等情况仍可能拒绝保存。硬崩溃可能重做未持久化批次，这就是减少落库次数所选择的恢复粒度，不能承诺所有中间向量永不重做。
- **聚合结果不变就不 touch 更新时间。** `_aggregate_video_task()` 通过 `session.is_modified(task, include_collections=False)` 判断任务是否有实际变化，再决定是否更新 `update_time`，消除无变化检查触发的整行 binlog。
- **扫描方式配套调整。** `_finalize_draining_videos()` 改为按任务 ID 游标分页并回绕；因为无变化任务不再更新时间，继续按旧 `update_date` 排序会使后页任务长期得不到检查。此次修复保留扫描覆盖与收尾语义。
- **已见对应回归用例。** `test_event_cache_is_flushed_in_batches_not_per_encoding_step` 检查两个 Event 在不同批次阈值下合并保存、最终 document 和发布回执；队列用例覆盖无变化聚合与分页推进。本轮为文档维护，仅核对实现和测试源码，不补造 Claude 的测试通过数量，也未重启／重跑整批补建来测降幅。
- **代码完成与运行态分开记录。** 本次只读核对中，三个生产文件最后修改时间为 22:19:43–22:20:13；记录的 API 进程 PID 66388 启动于 21:51:51，日志最后 startup 为 21:51:57，之后未见重启，且启动入口未开启自动 reload。因此上述“已修复”指工作区代码已完成；当时 API 尚未加载这些修改，后续加载与真实 binlog 降幅需另行确认。

依据：[批量缓存实现](../../server/service/video_parse_index_coordinator.py)、[无变化聚合保护](../../server/repository/video_parse.py)、[扫描游标](../../server/mq/video_parse_queue.py)、[Event 缓存回归](../../tests/server/test_video_event_index_coordinator.py)、[队列回归](../../tests/server/test_video_parse_queue.py)。修复减少了保存频次及无效更新；`save_index_embeddings()` 单次仍合并整份缓存，父任务 `reserve_model_call()`／`release_model_call()` 仍整包更新 detail，不能将本次修复扩大描述为所有大 JSON 更新已消除。

#### 已实施的留存调整与边界

- 已按用户要求将当前实例 `binlog_expire_logs_seconds` 从 **2,592,000（30 天）改为 604,800（7 天）**，`binlog_expire_logs_auto_purge=ON`；同步 [原生启动参数](../../scripts/local_services.py) 和 [Compose 参数](../../compose.yaml)，两文件各新增一行。
- 通过 MySQL `FLUSH BINARY LOGS` 触发过期检查，自动清掉 **41 个**超过七天的文件（`.000025`–`.000065`），实际释放 **1.615647 GB**。执行后保留 **71 个 binlog，共 61.719171 GB**；最老 `.000066` 的结束时间为 10 月 11 日 22:39:44，当前新文件为 `.000136`。这些是执行后的时点值，运行中数量会变化。
- **2026-10-19 复核发现运行设置回退，并已恢复。** 09:48:56 启动的 mysqld（PID 17683）实际参数没有 `--binlog-expire-logs-seconds=604800`，运行值回到 **2,592,000（30 天）**；当前源码与 Compose 文件仍为七天。进程参数与 `outputs/local-services/mysql.pid.json` 记录一致，但现有证据无法确定哪个入口生成了缺参数的命令。10:09:30 再次设置 GLOBAL 为 **604,800**，并通过 `FLUSH BINARY LOGS` 自动清掉新过期的 `.000066`（逻辑大小 **871,109,950 bytes，约 0.871 GB**）；之后的文件快照为 **72 个、物理占用约 61.310 GB**，最老 `.000067`、活动文件 `.000138`。运行值与自动清理开关已复核为 **604800 / ON**。
- **配置文件正确，还要核对实际启动参数。** `SET GLOBAL` 只影响当前实例，重启后的值取决于启动时真正加载的配置（见 [MySQL 官方说明](https://dev.mysql.com/doc/refman/8.4/en/persisted-system-variables.html)）。当前原生脚本已经带七天参数；下次启动需核对真实 mysqld 参数和运行变量，不能仅凭仓库文件正确就宣布设置持续生效。本次未重启数据库。
- 此操作保留近七天日志及业务数据，没有直接 `rm` 数据库文件，没有重启服务。过期检查发生在启动／日志刷新或轮转时，不是精确到秒的定时删除。
- **七天留存只限制历史积累，不减少每轮索引的 binlog 生成量。** 今天产生的日志仍在七天内；不能将此次释放 1.62 GB 或 C16 功能验收成功写成应用性能问题已修复。

#### 后续优化与性能验收

1. Event checkpoint 合并已落代码，先按有限固定样本测实际降幅；若批量保存后仍有明显大字段负担，再评估更小的持久化粒度，继续保护昂贵 embedding 的恢复能力、输入／版本校验、lease fencing 和精确发布回执。
2. 无变化聚合不再更新时间及游标扫描已落代码；Consumer 每轮仍会调用 reap，是否进一步独立节流按实测决定。父任务调用占位和累计调用 ID 仍整包更新，持久化边界可另行优化，避免修改一个小对象就重写完整历史。
3. `binlog_row_image=MINIMAL` 可作为另一项评估，减少未变化列，但当前 detail 本身整体变化时仍会记录大字段；不能把数据库开关当成替代应用修复。初次诊断值为 FULL，本轮文档维护未重新核对运行态或更改此开关；另行实施前需核对恢复／复制需求和既有连接的会话设置。
4. 验收同时记录固定数量 Event 的 embedding 次数、checkpoint / UPDATE 事件数、binlog 字节增量和耗时；验证无变化轮询不再写库，有限批次修复前后可比较，并覆盖中断恢复不重复已缓存调用、旧 lease 不覆盖新结果、Event 发布集合正确。不能用文本日志行数替代写入量，也不为测量再次无条件补建全 13 集。

依据：[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)、[应用原始日志](../../outputs/local-services/api.log)、[本地 MySQL binlog 目录](../../outputs/local-services/mysql/)。本次文档维护记录历史故障、Claude 已实施的修复、七天留存和 batch 教训，不改写其他 Agent 的应用代码、数据库结构或服务状态；binlog 证据会按新留存策略过期，核心计数与解码结论保存在本项。

### C15：局部截图检索缺少收敛与主动停止判断，空草稿反复退回直到超时

排序日期：2026-10-18。

归类说明：以 2026-10-18 的 Loop Control 实现记录为最新进展；下方 2026-10-17 的 Evaluator `stop` 待实施方案保留为历史记录。当前仅有单元测试，茶杯样本尚未重跑。

> Status 2026-10-18 (English note): addressed by deterministic Loop Control instead of an Evaluator `stop` action (see [perf_plan.md §3.5](perf_plan.md)). Under the single-pass Result, a search runs at most `result_max_loops` (2) outer loops, re-plans only when the Result self-check or the Evaluator names a concrete gap and the loop made progress, and otherwise ends normally with a "not fully verified" answer instead of `LOOP_LIMIT_EXCEEDED` or a timeout. Unit-tested only; the teacup image case has not been re-run.

优先级：高。类型：Agent 检索策略与结束语义缺失。状态：2026-10-17 已复现，`stop` 方案已确认、待实施；本次仅维护问题与解决方案，未修改运行逻辑。

#### 问题与真实证据

- 用户只上传 282×214 的茶杯局部图，询问“这张图片出自哪一集？”，范围限定 MyGO。图片上传及模型输入正常，但最终在 **1200.199 秒**后收到 `answer.failed / AGENT_EXECUTION_TIMEOUT`，仅保留开场白，没有定位集数、时间或视频卡。会话为 `8016a767-9528-42e2-939f-28bb1ff75cfe`，完整输入、日志与统计见[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)。
- 实际执行 48 次成功 retrieve（47 次新查询、1 次翻页），交付候选 423 次、去重 81 个 Chunk；Query 取帧 81 次，检查 24 个不同 Chunk，交付 256 帧次、131 个不同图片 SHA，其中 43 次取帧命中缓存。经历 6 轮 Query、5 次 Evaluator 审核，每次均因 Draft 为空返回 `continue`；Evaluator 未独立取帧，最后在第六轮 Query 触发整轮截止时间。
- 本轮 `FRAME_INSPECTION_LIMIT` 与 `VIDEO_CONTEXT_BUDGET_EXCEEDED` 均为 0。大量搜索和取帧不代表完整穷举，也不能据此认定截图不在原片。失败产物未保存底层 retrieve 原词，只保留长度、hash、权重及候选 ID；不能用 Supervisor 描述冒充实际检索词。

#### 根因与能力边界

- **能描述图片，不代表描述具有定位区分度。** 白杯、黄色茶水、茶碟等是可识别但通用的线索；杯把形状、局部纹理和物件精确位置需要视觉匹配能力。当前 Chat 路线主要是看图生成文字查询，再检索 Chunk、取帧核验，没有直接用上传图片向量匹配逐关键帧或局部区域。线索弱时容易退化为反复试探，不能只责怪模型“不读图”。
- **审核目标缺少“当前无法解决”的正常结束决定。** `EvalDecision.action` 只有 `complete / continue`，Evaluator 提示要求先审用户意图、任一层不合格都 continue。空 Draft 因未提供集数而不断退回，即使 Query 诚实说明尚未找到，也没有明确的停搜标准。当前已有无获批项时只输出正文的能力，但缺少区分“仍值得继续”与“已无有效方向”的协议和指令。
- **有工具活动不等于有进展。** 同义改写、重复候选及反复读取已有图片可能没有增加证据；另一方面，缓存命中或暂未缩小候选范围也不能单独证明浪费。独立审核、补看未覆盖时段、解除错误假设或扩大到正确场景都可能有效。此前[图片检索退役摘要](design-experiment-cleanup.md#图片向量与出处定位)就是改用较宽的场景描述后找到，不能硬编码“每轮 scope 必须变小”。

#### 已确认方案：Evaluator 增加 `stop`

沿用既有 Query → Evaluator → Supervisor 职责，在 Evaluator 增加“继续检索必要性检查”，放在要求完成用户意图之前；不另建分类 Agent、不增加未经测量的轮次／取帧硬上限。

| action | 含义 |
| --- | --- |
| `complete` | 证据足够，进入回答整理；现有允许的诚实无结果答复仍保持兼容 |
| `continue` | 尚未完成，但存在具体、具有区分度的下一步检索或核验动作 |
| `stop` | 尚未完成，当前证据与工具下已没有值得继续的方向，正常结束本次检索 |

- **继续条件：** 检查是否新增匹配证据、缩小候选／时间范围、排除关键假设，或获得新的可验证线索。返回 `continue` 时，`advice` 必须说明下一步动作及依据；不以“草稿为空”“用户还没得到答案”单独要求继续。
- **停止条件：** 当前线索无法区分候选，已有尝试没有带来有效进展，下一步只能重复查找、重复查看或无依据地扩大搜索时返回 `stop`。可先根据图片的可用定位线索判断风险，再结合实际检索核验；不凭模型自报置信度、单一分数或固定次数直接判死。
- **原因复用 `advice`：** 说明尚未核实的目标、停止依据、当前能力／线索限制及建议补充的周边画面、相邻截图或短视频，不新增原因字段或错误码。`stop` 不表示“原片不存在”，也不表示异常、用户取消或超时。
- **Supervisor 正常交付：** 接收停搜决定后说明尚未定位及后续可补充的信息，不自动重发相同搜索。保持此前已确认／已提交项，不把本轮未核实候选作为已确认视频卡，不以泛化失败代替说明。

#### 最小实现边界与仍需接通的逻辑

- 主体是 Query／Evaluator／Supervisor 提示词和 `EvalDecision.action` 的调整，`LoopTurn.status` 同步接受 `stop`。继续复用 Canvas 历史、检索调用与取帧来源记录作为判断依据，不增加数据库状态或前端消息 Schema。
- **不能只加枚举而漏接执行分支。** 当前 Runtime 只有 `continue → turn`、`complete → chat`，冻结草稿、内部 Chat 入口及最终输出校验都以 `complete` 为前提。必须补齐 `stop` 的正常结果构造、路由及 Supervisor 交接，携带停止说明与检索记录；不能直接跳 END 导致缺少 `final_output`，也不能把 `stop` 当作批准未审核 Draft 的捷径。具体接线应复用既有结果／正文通道，不重构整个会话体系。
- Query 提示同时要求在无有效方向时结束当前阶段并说明限制，让 Evaluator 有机会判断。Evaluator 只在 Query 返回后执行，新增 `stop` 不能保证单次 Query 内部工具循环立即退出，更不能承诺固定响应时长。
- 保留既有取消、真实 Provider 失败及超时语义，不把它们伪装成业务停搜。普通对话的等待预算、搜索提前返回并为最终说明留时间，以及关键帧／局部视觉索引是相邻待设计项，本次不靠新增硬编码或延长 20 分钟来替代。

#### 验收条件

- 弱线索且无有效进展时可返回 `stop`，搜索正常返回 Supervisor 并形成非空的未解决说明，不再次进入相同 Query 循环，不产生 `answer.failed`；未经核实的 Draft 不被批准或展示为确认片段。
- 有具体新方向、独立核验理由或待补看的高价值区间时仍可 `continue`，不因候选暂未减少、需要扩大场景查询或命中缓存而机械停止；既有 `complete` 和无结果正常答复保持兼容。
- 多个并行／先后搜索中，一个任务 stop 不撤销其他已确认结果；Supervisor 能区分停搜、超时、取消和模型异常。
- 用已有茶杯失败、咖啡店成功及六图样本回归，记录实际 action、advice、工具轨迹和最终消息；不把假模型的路由通过当成真实图片定位质量验收，不承诺仅新增 enum 即解决全部长耗时问题。

依据：[Evaluator 指令](../../agent/chat/video_search/evaluator.py)、[Evaluator Skill](../../agent/chat/video_search/skills/evaluator/SKILL.md)、[决策与轮次类型](../../agent/chat/video_search/state.py)、[Runtime 路由](../../agent/chat/video_search/runtime.py)、[草稿冻结](../../agent/chat/video_search/video_search_canvas.py)、[内部 Chat](../../agent/chat/video_search/chat.py)、[最终输出校验](../../agent/chat/video_search/output.py)、[Supervisor 工具交接](../../agent/chat/supervisor.py)。实验目录沿用本地 ignore 规则，引用的是本机原始产物；方案尚未编码、部署或重新实测。

### F06：检索命中姓名、原始时间线与封面缺失混入最终展示

排序日期：2026-10-16。

- 2026-10-16 源码核对：前序 `matchedEvidence` 将 BM25 等检索来源命中转换为文本，其中大量是事件里的说话人／参与者姓名；前端曾把它们全部列成“命中的可核验事实”，没有与最终 claim 绑定。公开结果现在清空 `matchedEvidence/lexicalMatches`，页面卡片保留最终 claim；内部检索与诊断数据未删除。姓名命中可帮助定位，不等于支撑“如何相识”等具体主张。
- Parse V2 的理解帧原本属于临时媒体，未发布到 `keyframe_uris`，因此 `playbackStatus=READY` 可以与 `thumbnail=null / keyframes=[]` 同时成立。当前列表按 Supervisor 最终片段起点用原视频生成小预览；不依赖长期图片 URI，也不要求为旧视频重跑解析才能显示封面。
- 后端已移除“缺少带原视频时间位置的已登记关键帧”这一展示 warning；`partial` 根据检索覆盖标记和范围记录决定，不再由任意媒体 warning 推导。空图片登记不表示视频不可播或检索不完整。
- 普通页面时间统一显示 `mm:ss`，底层播放仍保留毫秒精度。PPE／debug 的原始资料只在打开视频详情后展示，列表不再铺开整段 Timeline；Markdown 正文的 debug 文本仍可能保留细节。
- Supervisor 的最终提交与相邻片段范围见 C12；当前工具已允许最多两个同视频、序号相邻且覆盖连续的已召回 Chunk 共同支持一个范围，保持来源 ID、版本和租户校验，不新增 HTTP VO。此处是当前源码现状，不补造所有历史消息的浏览器验收。

依据：[公开检索投影](../../server/api/search.py)、[卡片与预览](../../web/src/features/search/SearchPage.tsx)、[视频详情](../../web/src/features/search/EvidencePlayer.tsx)、[时间展示](../../web/src/shared/format.ts)、[连续范围校验](../../agent/chat/video_search/output.py)。

### B09：同剧集并发完成上传时，父记录 S → X 锁升级导致死锁

排序日期：2026-10-14。

优先级：高。类型：上传完成事务的并发锁冲突。状态：最小代码修复与真实 MySQL 回归已通过，运行服务尚未重启加载修复。记录日期：2026-10-14；以下故障时间均为 `+0800`。

#### 现象与影响

用户一次批量上传 13 个视频，首批 3 个文件并发上传收尾时出现错误。`2026-10-13 23:34:49`，其中两个请求在 `CREATING_JOB` 阶段报 MySQL `1213 Deadlock found when trying to get lock; try restarting transaction`，`POST /api/uploads/complete` 返回 HTTP 500。分片传输、合并、校验与媒体探测已经通过，失败发生在 Video／待执行解析任务记录的数据库提交阶段，尚未提交成功，也尚未执行这些文件的解析；与 B02 的提交后清理失败是独立问题。

| 上传会话 | 失败请求 ID | 后续恢复 |
| --- | --- | --- |
| `8c8d1954-5789-4e7a-9ca1-2471b107eb53` | `7535ac6a-b44e-4fac-846e-9d76fff363f4` | `23:36:05` 原会话重试完成 |
| `88e54a7d-7752-44f3-9dbe-34f241abe67b` | `e9ab99c2-7799-4f7e-97c2-df30eb454b54` | `23:35:15` 原会话重试完成 |

统计窗口为 `2026-10-13 23:34:43–23:36:06`，只统计 `outputs/server/logs/development/api.log*`，不与重复写入的 `outputs/local-services/api.log` 累加：

- 分片 PUT 共 293 次，全部 HTTP 200。
- 完成请求含重试共 8 次，其中 2 次 HTTP 500，请求失败率 25%。
- 首批同时完成的 3 个文件中，2 个首次失败，比例为 66.7%；不能将该比例外推为所有文件的最终失败率。
- 已进入完成阶段的 6 个文件在重试后均完成；窗口内另外两个新建会话尚未进入完成阶段，不计入该结果。
- 排查时保留日志中共 7 条 `upload_completion_failed`：6 条死锁（9 月 18 日、9 月 22 日、10 月 13 日各两条），另 1 条为视频身份冲突。历史错误按各自原因处理。

#### 两种锁分别锁什么

S（Shared，共享）和 X（Exclusive，排他）描述的是锁的**访问模式**，不是不同的数据对象，也不单独决定锁的粒度。本次发生冲突的两种锁都落在同一个数据库对象上：

```text
数据库：seekino_local_20261010_development
表／索引：series / PRIMARY
记录：id = 836fa9f0-2bf4-50fb-9c64-75526fa1dea9（mygo）
粒度：这一条主键索引记录，对应这一行 Series
```

| 模式 | 本次加锁来源 | 锁住的对象及目的 |
| --- | --- | --- |
| S 共享记录锁 | `INSERT video` 的 `video.series_id → series.id` 外键检查，由 InnoDB 自动取得 | 父表的上述 Series 记录；保证检查过的父引用在事务期间受到保护，阻止其他事务对该记录取得 X 锁 |
| X 排他记录锁 | `refresh_series_statuses()` 中 `SELECT series ... WHERE id = ... FOR UPDATE` | 同一条 Series 记录；为重新计算并更新剧集状态取得排他访问 |

虽然一个操作检查 `series.id`，另一个操作要更新 `series.status`，锁的单位仍是同一条记录，不是分别锁两个字段。每个文件还会锁各自的 UploadSession、插入各自的 Video，但本次等待环出现在它们共同引用的父记录 Series 上。死锁报告明确为 `locks rec but not gap`，因此这里是记录锁，不能解释成文件锁、整表锁、整页锁或范围／间隙锁。

对**不同事务在同一条记录上**申请的记录锁，兼容关系为：

| 已持有的锁／另一事务申请的锁 | S | X |
| --- | --- | --- |
| S | 兼容，可同时持有 | 不兼容，需等待 |
| X | 不兼容，需等待 | 不兼容，需等待 |

事务自己的 S 锁不阻止自己申请 X；阻止升级的是其他事务仍持有的 S 锁。本次外键检查得到的 S 锁不会在 INSERT 语句结束后立即释放，而是持有到事务提交／回滚。上述兼容表针对记录锁请求；当前 `REPEATABLE-READ` 下普通 SELECT 可以通过 MVCC 读取快照，不能把 X 锁理解为阻止所有普通查询。

#### 死锁如何形成及直接证据

前端 `MAX_PARALLEL_FILES = 3`。文件大小接近时，多个完成请求会在接近的时间进入 `UploadRepository.complete_upload()`。故障版本的事务先锁自己的 UploadSession，再插入并 flush Video，最后才通过 `refresh_series_statuses()` 对 Series 申请 X 锁。

```text
事务 A：插入 Video A → 持有 Series R 的 S 锁
事务 B：插入 Video B → 持有 Series R 的 S 锁（S 与 S 兼容）
事务 A：申请 Series R 的 X 锁 → 等 B 释放 S；A 自己的 S 仍保留
事务 B：申请 Series R 的 X 锁 → 等 A 释放 S；B 自己的 S 仍保留
结果：A 等 B，B 等 A；InnoDB 检测到死锁并回滚一个事务
```

2026-10-13 排查时只读执行 `SHOW ENGINE INNODB STATUS`，最新死锁时间与应用日志同为 `23:34:49`。实际版本为 MySQL `8.4.11`、隔离级别为 `REPEATABLE-READ`。报告中的事务 `2306436` 与 `2306437` 均持有上述 `series / PRIMARY` 记录的 S 锁，并等待同一记录的 X 锁，随后记录 `WE ROLL BACK TRANSACTION (2)`。摘要如下，原报告当时未另存文件：

```text
TRANSACTION 2306436: HOLDS lock mode S locks rec but not gap
                    WAITING lock_mode X locks rec but not gap waiting
TRANSACTION 2306437: HOLDS lock mode S locks rec but not gap
                    WAITING lock_mode X locks rec but not gap waiting
SQL（两者相同）: SELECT ... FROM series WHERE series.id = '836fa9f0-2bf4-50fb-9c64-75526fa1dea9' FOR UPDATE
```

等待环由数据库报告直接证实；S 锁的来源由事务代码中的 Video 插入、外键定义和 InnoDB 外键加锁规则共同解释。最新报告只保留最后一次死锁，两个失败请求则分别由应用日志确认，不把一份报告解释成完整记录了三个事务的全部冲突。

当前后端没有对 1213 做事务级自动重试；异常沿 `UploadService.complete()` 失败路径返回，释放完成认领并将会话恢复为 UPLOADING，保留已确认分片。前端完成接口异常直接标记整个上传任务失败，因此本地传输成功也会显示“上传失败”；原会话重试可复用已有分片。

#### 已实施的最小修复（2026-10-14）

- 仅调整 `server/repository/upload.py` 的 `complete_upload()`：锁住 UploadSession 并复核 `COMPLETING`／completion token 后，立即通过 `SELECT Series ... FOR UPDATE` 取得父记录 X 锁，再查询冲突、插入 Video 和任务，最后提交。原 Series 存在性检查前移并复用结果，生产代码新增 12 行、删除 5 行。
- 保留既有 `UploadSession → Series` 锁顺序，与删除收尾一致；未改成全路径 `Series → UploadSession`，也未调整删除和旧版索引回写的锁顺序。各并发完成请求在插入 Video 之前排队，避免先同时取得父行 S 再升级 X。
- 取得 Series X 后，后续 INSERT 的外键检查不会把它降为 S；排他锁持续到同一事务提交／回滚。串行范围仅为同剧集上传完成的数据库事务，分片传输、合并与媒体探测仍在锁外执行。
- 上传／删除竞争仍由原有加锁与状态机制保护：`begin_delete_session()` 锁住 UploadSession 后拒绝 `COMPLETING`；允许删除时将其置为 `EXPIRED`、清除 token 并保存删除标记；完成事务在锁内复核 status/token，拒绝失效请求。此机制不是检查一个 `Series.status = DELETED` 字段。
- 不新增事务重试，不改变 API、Schema、配置或前端并发数。运行中的 API 未重启，本条验证不等于已对原 13 个真实视频重新上传验收。

#### 验证结果

- `.venv/bin/python -m pytest -q tests/server/test_upload_service.py`：**87 passed**，包含完成幂等、删除拒绝 COMPLETING、分片与删除竞争及清理恢复等既有行为。
- 新增 `test_mysql_completes_distinct_uploads_in_one_series_concurrently[v1/v2]`：同一 Series 的 3 个不同文件通过 3 个独立数据库连接并发完成；SQLAlchemy 事件在 INSERT Video 后最多等待 1 秒以放大旧版竞争窗口，修复后串行路径无需等待其他 INSERT 到齐。验证三个上传均完成、视频和任务唯一，重复 complete 复用原 ID。
- 在新建的临时独立 MySQL 测试库中，将 HEAD 的旧 `complete_upload()` 方法仅加载到测试子进程执行上述新增用例：**2 failed**，V1／V2 均复现原 `SELECT Series ... FOR UPDATE` 的 **1213 死锁**。此为预期失败的回归有效性验证，未回写或回滚工作区源码。
- 随后运行修复版新增两项，以及 `test_mysql_delete_fences_in_flight_upload_and_preserves_order`、`test_mysql_delete_completed_upload_removes_video_job_and_usage`、`test_mysql_enforces_episode_order_under_concurrent_requests`、`test_mysql_chunked_upload_claim_and_lease_fencing`：**6 passed**。测试结束后仅删除本次新建的临时库，没有修改现有业务库；未运行解析 Worker 或模型调用。
- `git diff --check` 通过。未执行服务重启、业务库迁移、commit 或 push。

#### 观察到但本次未改

- `create_session()` 的幂等重放已避免同时持有 Series 再等待原 Upload；真正新建路径仍存在 `Series → occupying Upload` 顺序，与完成路径的反向取锁是既有独立风险，需要单独核对冲突序号及扫描范围。上述回归不代表覆盖了所有新建与完成交错，也不宣称消除所有数据库死锁。
- 上传收尾是否需要每次重算整部剧状态，以及 1213 的有限事务重试，留作独立评估；本次保持业务状态规则，专门修复已证实的父记录 S → X 升级冲突。

依据：[上传事务](../../server/repository/upload.py) 的 `complete_upload()`、[剧集状态刷新](../../server/repository/index_sync.py) 的 `refresh_series_statuses()`、[Video 外键](../../server/model/video.py)、[上传失败收尾](../../server/service/upload.py)、[前端上传调度](../../web/src/features/uploads/UploadProvider.tsx)；本机应用轮转日志中的上述 request_id／upload_session_id 与当时只读获取的 InnoDB 死锁报告。锁语义参考 [MySQL 8.4 InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)、[各 SQL 语句的加锁规则](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)。

### C12：Supervisor 没有最终证据审核与时间范围决定权，内层答案被程序强制原样输出

排序日期：2026-10-14。

优先级：高。类型：Agent 职责、证据供给与最终输出契约缺口。状态：已实施 Supervisor 最终提交修复，离线回归通过；真实模型选段质量待样本验收。

#### 现象与证据边界

- 用户预期 Supervisor 对最终回答、选择理由及每个视频片段的 `start/end` 负责；即使 Query 选段过宽或过窄，Supervisor 也应依据文字 Timeline、对白修正后再交付前端。
- 2026-10-13 的截图请求为 Conversation `d8226119-4624-45a8-b440-39c0de491f25`、Assistant Message `2d3b482e-b33d-42a5-a6f2-6262e71c945e`。首段显示 `04:00–05:00`，与该 Chunk 的 canonical 范围 `240000–300000 ms` 相同。不能仅凭完整 Chunk 范围断言它必然错误；需要核对支撑用户问题及最终主张的事件边界。
- 该请求日志中 Query 成功写入两项草稿，Evaluator 返回 `complete`，审核意见称“所有播放范围均位于 canonical 证据边界内”。工具日志没有记录 `write_context_canvas.operations` 内的实际时间值，不能据此复原模型每次提交、修改范围的完整过程。
- 前一轮只读复核时，本机 API 9621 和 MySQL 3306 均未监听，因此未重新取得这条消息的数据库原始 `renderBlocks`。不将两个 canonical Chunk 范围冒充两项已重新核实的最终播放范围。
- 此历史请求的 Query/Evaluator/Chat 经 DeepSeek 402 后实际使用 Ark 豆包 Lite；外层 Supervisor 使用 Astra。当前角色绑定已另行调整，该历史样本不能当作“内层 Astra 已完成本次审核”的证据，也不能仅以模型名称解释缺陷。

#### 修复前已确认的代码原因

1. **Supervisor 只拿到摘要。** `search_video_graph` 的模型可见返回仅包含结果数、复用标记、最多 1000 字的 `answer_preview` 和提示语，没有逐项 canonical 范围、Query 选段、完整 Timeline、对白或 Evaluator advice。宿主内存中有完整结果，不等于模型已收到或能按需读取它。
2. **Prompt 禁止最终修订。** Supervisor 当前被要求不得重写、否定内部 Query/Evaluator/Chat 的结论，并原文呈现内部批准答案。
3. **程序绕过 Supervisor 的视频回答。** 有 `video_result` 时，Supervisor Runtime 直接使用 `video_result.agent_reply.message`，只在特定分支追加实时解释或网页补充；Service 对不以前述答案开头的改写抛出 `Supervisor changed the approved video answer`。
4. **最终范围固定为 Query 的获批范围。** Query 新草稿必须填写 start/end；缺失会失败，没有自动补成整 Chunk 的逻辑。Evaluator 放行后冻结草稿，Chat 工具不能修改时间，`build_chat_output()` 将 Query 范围直接写入最终 `renderBlocks`。
5. **最终事件也来自内层答案。** Service 已将 `answer.evidence/block/delta` 缓存在 `pending_answer_events`，SQL 提交后发送；若只让 Supervisor 修改返回值而仍发送这些旧事件，SSE 会与数据库最终结果不一致。

前端已按 `renderBlocks.startSeconds/endSeconds` 显示时间、定位和控制播放；`hit.evidence.start/end` 则保留完整 canonical 证据范围。两者语义不同。历史命名的 `*_seconds` / `*Seconds` 媒体时间字段实际单位均为整数毫秒。

通用 Retrieval Skill 已要求完整覆盖支持结论的事件、动作或对白，并去掉明显无关的前后内容。因此不能把问题简单归结为“Prompt 从未要求精确选段”，也不能仅加强 Query 提示而保持 Supervisor 的上述限制。

#### 修复实现：数据供给、Tool、Prompt

| 层 | 已实施调整 | 边界 |
| --- | --- | --- |
| 数据供给 | 内层完成后向 Supervisor 交接本次真实召回的证据快照、原草稿与范围、Evaluator advice、内部 Chat 草稿及检索覆盖信息。工具首屏只给有界目录和读取入口；Timeline、对白、背景按需分页／分段读取。 | 复用已有 Canvas 和读取机制；不把完整证据塞入单次工具返回，不把内部交接对象塞进 HTTP VO 或数据库公开结果；同一 Chunk 的来源与版本保持一致。 |
| Tool | Supervisor 使用 `get_context_canvas` 按需读取文字证据，通过 `submit_video_item(video_chunk_id, start_seconds, end_seconds, claim)` 提交最终视频项；重复 ID 原位更新，`remove_video_item` 撤回。模型最后的非空正文与提交项统一组装为既有 `AgentFinalOutput/renderBlocks`。 | 源证据和内部审核快照只读；提交项按请求隔离。程序校验租户／Series／已提供证据 ID、整数毫秒和 canonical 范围，不限制为 Query 原先选定的范围。不新增 `replace_final_answer` 或前端协议。 |
| Prompt | 明确 Query/Evaluator/内部 Chat 提供证据与经过审核的草稿；Supervisor 对最终事实表述、片段选择和时间范围负责。要求按用户主张核对文字 Timeline、对白，既避免截断必要内容，也去掉明显无关内容。 | 移除 Supervisor 不得修订和必须原样输出的指令；不要求看完所有 section，不以整 Chunk 范围为程序拒绝条件，不增加自动循环审核层。 |
| 输出接入 | 解除保存层的原文前缀限制；从 Supervisor 最终结果统一生成保存的正文、选择理由、`renderBlocks`、SSE 和下一轮历史。 | 保留 SQL 提交后确认 harness 的顺序、10 分钟总预算和手动停止；不能静默退回内层旧答案并声称 Supervisor 已完成最终审核。 |

Supervisor 可以把 Query 过宽的范围收窄，也可以在已提供的 canonical 证据范围内修复过窄的范围；不能把允许的修改错误限制成 `Query.start <= final.start < final.end <= Query.end`。跨出已提供证据覆盖或引入未知 ID 仍应反馈错误，不能臆造支持。

最终 Markdown、选择理由和片段范围应由同一个最终草稿产出；不能同时保存 Supervisor 新正文与内部 Chat 旧视频块。无命中、全部候选不支持问题、网页与实时理解混合回答也要明确处理，不强制保留不支持答案的视频块。

日志已补齐 Canvas 读取的 collection/reference/section 和有界批量 references；Supervisor 的 `submit_video_item` 使用顶层 ID/start/end，现有工具日志可记录每次最终范围提交及成功／失败回执。Evaluator 结论继续沿用原有日志。工具调用回执不证明模型已理解证据，不记录隐藏推理；历史 Query 批量写入未记录的 operations 时间不能事后恢复。

#### 2026-10-14 实施记录

- 旧 `submit_video_item` 原属 Chat（`862c269`，2026-09-17）；`d71358b` 在 2026-09-20 改为 Workspace 的 update/remove，`2d5ce92` 在 2026-10-10 又合并到 Canvas 写操作。Supervisor 首次加入于 `5a30db0`（2026-09-22），当时没有最终视频提交工具。本次按用户指令给 Supervisor 补上显式提交能力，内层 Chat 保留独立调用能力。
- Runtime 成功后通过本次调用的 callback 交出 Canvas 与最后审核轮次，冻结 `review/latest`。覆盖信息合并本次所有 Query/Evaluator 检索，避免漏掉 Evaluator 补查带来的 partial、warnings 和已搜索视频范围。读工具沿用既有分页／字符范围预算；Supervisor 候选目录和详细证据使用同一固定快照。
- 内层 Chat 正文／视频块作为审阅草稿。服务层忽略这些草稿内容事件，从 Supervisor 最终结果统一生成正文、claim、播放范围和 SSE；只在 SQL 提交及现有 harness 确认后发布。没有最终视频提交时允许明确的纯文字答复，不自动补回内层视频。
- 保留既有租户／Series 隔离、canonical 边界、10 分钟总预算和停止能力；允许合法改写、范围扩大／缩小、撤回全部视频。最终模型正文为空时失败，不能拿程序追加的网页来源列表冒充完整回答。
- 离线验证覆盖真实 Runtime／Canvas／服务组装链，但使用本地模型替身；未调用真实 Astra 或其他模型，未重启服务、迁移数据库或改写历史消息。验证数量见本次交付记录；真实剧情与选段语义仍需真实样本验收。

#### 本次范围与相邻问题

- 本项先使用已有文字 Timeline；自选帧数的看图 Tool 暂不增加，抽帧、Redis 图片缓存和 Parse V2 关键帧持久化不作为本项修复前提。
- BM25 命中姓名对应的公开 `matchedEvidence/lexicalMatches` 已在前序修改中清空，前端列表已移除；内部检索证据仍保留。该展示调整不等于删掉 Timeline，也不会自动改善 Agent 选段或补齐缩略图。
- 空缩略图与缺关键帧提示属于独立媒体预览缺口；本项保持既有前端时间范围合同，不借此重写媒体服务或全站预览。
- 本轮未重跑真实模型、未重启服务、未迁移数据库，未改写历史消息。

#### 验收条件

- 构造 Query 整 Chunk 草稿，Supervisor 读取已有文字 Timeline 后提交更精确范围；验证其文本、claim、起止时间真正成为最终结果。
- 构造 Query 过窄草稿，Supervisor 在同一 canonical 范围内补回必要上下文；验证程序不会按旧 Query 边界拒绝。
- 目录分页及长 Timeline 分段可读，证据缺失和覆盖限制可见；未提供的 ID、跨租户／Series、越界、非整数或空范围被拒绝，合法改写不再被原文前缀限制拒绝。
- 最终 SQL、REST、SSE evidence/block/completed、harness 确认内容和下一轮引用保持同一版本；失败或取消时不发布内层草稿作为最终答案。
- 无命中、删除全部视频项、纯聊天、纯网页、实时理解以及混合回答保持正常；检索重复调用复用当前请求，不重跑模型来代替最终审核。
- 保留既有超时、停止、原始证据不可改写及幂等语义。离线测试保护数据合同与传递；真实样本的选段语义质量另行验收，不能以 fake-model 测试证明真实模型一定选段正确。

依据：[Supervisor](../../agent/chat/supervisor.py) 的 `search_video_graph`、Prompt 与最终内容选择；[Canvas 草稿与冻结](../../agent/chat/video_search/video_search_canvas.py)；[范围组装](../../agent/chat/video_search/output.py)；[通用检索要求](../../agent/chat/video_search/skills/retrieval/SKILL.md)；[Service 保存与 SSE](../../server/service/agent_chat.py)；[工具日志摘要](../../agent/chat/common/tool.py)；[SearchPage](../../web/src/features/search/SearchPage.tsx)；本机 `outputs/server/logs/development/api.log` 中该 Message 的 `agent_tool_started/finished`、`agent_turn_evaluated` 和完成记录。

### C08：业务图继承 Supervisor checkpoint，导致视频工具执行前失败

排序日期：2026-10-11。沿用原“Chat 联调问题（2026-10-11）”章节日期。

优先级：高。状态：最小代码修复与离线回归通过，API 已加载修复，真实当前视频 Chat API 调用链已完成；片段证据仍不完整，见 C10。

#### 现象与根因

- 原故障时间：`2026-10-11 00:31:22`；Conversation 为 `e9e55375-55f4-48b8-8dd6-2566e2472ce3`，Assistant Message 为 `a7ff9c39-f93e-483f-8465-16a3d57842e6`。`understand_current_video` 执行 `7 ms` 后发生 TypeError，整轮 `996 ms`，最终包装为 `AgentModelInvocationError / AGENT_MODEL_ERROR`。API 与 Web 健康检查成功无法证明这条视频调用链成功。
- 当前 Supervisor 调用 `create_deep_agent()`，由 Deep Agents SDK 自动生成模型／工具循环的 graph；`supervisor.py` 没有另建 Supervisor `StateGraph`。两个专业业务 runtime 各自保留固定业务图，并通过普通工具调用；没有向 `create_deep_agent()` 注册业务 `subagents=`。
- 外层 Supervisor 使用 `InMemorySaver`。内部业务图原来使用默认 `builder.compile()`，会沿工具调用的运行上下文继承外层 checkpointer。单独运行这些业务图时没有父 saver，因此原有普通 runtime 测试没有暴露此问题。
- 实时理解把 `RealtimeRequest` 放入 graph 状态，其中的 `check_control` 是关联本次取消／超时控制的函数，不能被当前 msgpack 序列化器保存。视频检索状态中的 `VideoSearchContextCanvas` 同样无法直接序列化。`InMemorySaver` 仍需序列化状态，并非直接保留任意 Python 对象引用。
- 使用真实 Supervisor、真实 Deep Agents 与真实业务 runtime，只替换外部模型、host 和检索，分别复现 `TypeError: Type is not msgpack serializable: RealtimeRequest` 与 `TypeError: Type is not msgpack serializable: VideoSearchContextCanvas`。实时理解复现时 `prepare_context_calls=0`、`model_calls=1`，故障发生在业务上下文准备之前。

checkpoint 本身可用于业务图，但状态必须可序列化；需要恢复执行时，还要明确运行时依赖如何重建。当前两个 runtime 按单次工具任务设计，未实现从 LangGraph checkpoint 恢复这些临时对象的能力。关闭 checkpoint 保存不影响执行中的状态传递、Canvas 处理、取消／超时检查，也不关闭底层 Chunk 结果的业务持久化。未来如需中断续跑，应单独设计可保存状态与恢复语义，而非直接继承外层 saver。

#### 当前安装版本的配套错误

核对安装版本：`deepagents 0.7.23`、`langgraph 1.2.11`、`langgraph-checkpoint 4.2.0`、`langchain 1.4.4`。

仅将两个业务图改为 `compile(checkpointer=False)` 仍不足：外层 `durability="sync"` 会独立传播到没有 saver 的业务图。当前 LangGraph 仍等待未创建的 `_put_checkpoint_fut`，实时理解和视频检索分别复现 `AsyncPregelLoop`／`SyncPregelLoop` 缺少该属性的 AttributeError。这是当前安装版本在无 checkpointer 嵌套调用中的同步等待边界问题。

#### 已实施的最小修复

| 文件 | 调整 | 作用 |
| --- | --- | --- |
| [实时理解 runtime](../../agent/chat/realtime_understanding/runtime.py) | `builder.compile(checkpointer=False)` | 本次请求与回调不进入 Supervisor checkpoint |
| [视频检索 runtime](../../agent/chat/video_search/runtime.py) | `builder.compile(checkpointer=False)` | 本次 Canvas 与控制对象不进入 Supervisor checkpoint |
| [Deep Agents 适配器](../../gateway/agent_model/deepagents.py) | 外层 `graph.invoke(..., durability="async")` | 保留外层 saver，避免向无 saver 业务图传播同步 checkpoint 等待 |
| [Supervisor 测试](../../tests/agent/chat/test_supervisor.py) | 新增两个参数化测试，共 4 个用例 | 验证真实嵌套调用、会话确认与错误清理 |

保留视频检索的 Query → 检索 → Evaluator → Chat，以及实时理解的准备上下文 → 并行片段证据／画面观察 → 汇总。没有新增转发 LLM、迁移到 `CompiledSubAgent`、使用 LangGraph 私有配置键或升级依赖；DTO、VO、API、数据库 Schema、前端字段及 `gateway/bridge` 均未修改。

#### 离线回归

新增用例修复前得到 `2 failed`，分别复现两种 msgpack 错误；仅关闭两个业务图 checkpoint 后，再次得到 `2 failed`，分别复现上述同步等待错误；完整修复后相关回归合计 **256 passed**，均在 `-W error` 下执行：

```bash
.venv/bin/python -W error -m pytest -q \
  tests/agent/chat/test_realtime_understanding.py \
  tests/agent/chat/test_runtime.py \
  tests/agent/chat/test_tools.py \
  tests/gateway/test_deepagents.py \
  tests/gateway/test_agent_factory.py
# 219 passed

.venv/bin/python -W error -m pytest \
  tests/agent/chat/test_supervisor.py -q --tb=short
# 37 passed
```

新增 4 个用例使用真实 `SupervisorRuntime → DeepAgentsSupervisorModel → 业务 runtime`，只 fake 外部模型、检索和 host。覆盖实时 host 的 prepare／chunk／visual／compose、视频检索批准答案及证据、外层 checkpoint 存在、`confirm_turn` 保存最终答案、第二轮保留第一轮确认后的回答、pending 清空，以及取消／host 原始错误传播与失败状态清理。连续两轮的会话保留结论来自离线回归，本次真实模型只验证一轮。

#### 服务加载与真实模型验收

- 获得单独授权后，仅重启 API，PID 从 `42228` 变为 `80107`；`13:33:24` 完成启动，parse consumer 为 `running`，Web PID `41425` 保持运行。
- 获得指定视频数据与模型目的地授权后，使用真实 HTTP Chat API 创建回归会话，询问 mygo 视频 `90.123 s` 附近正在发生什么。冻结播放字段 `positionSeconds=90123` 沿用既有合同，其实际单位为毫秒。
- 回归 Conversation：`59bff08f-76a2-4244-b448-aef6a42ae91c`；Assistant Message：`5485fc08-1dfe-4085-857b-d33dfe1bce4c`；Tenant：`local-default`。
- `13:39:02` 工具启动，`13:39:21` 工具成功返回，`duration_ms=19467`；`13:39:35` Supervisor 与整轮执行完成，`duration_ms=40733`，回答为 `451` 字符。消息 API 确认为 `COMPLETED`、`lastError=null`，本轮未见 msgpack、`_put_checkpoint_fut` 或轮次确认错误／警告。
- 模型用量记录确认火山方舟 `doubao-seed-2-1-pro-260628` 的 `RealtimeVisualObservation` 和 `RealtimeContextSummary` 各调用一次；外层使用当前 DeepSeek／Ark 配置。上述实际业务调用与终态共同作为验收证据，不以 HTTP 200 代替链路成功。
- 工具结果为 `partial=true`，含义见 C10；不能据此宣称完整片段解析已恢复。当前实时理解结果不单独写入 `searchResult`，故本轮 `searchResult=null`、`evidence_hits=0` 不等于工具失败。
- 浏览器环境没有可用实例，本次未完成浏览器操作或 SSE 传输联调；真实视频检索完整链路也未在本轮验证。视频检索的嵌套调用、批准答案和证据由上述离线回归保护。

依据：[deepagents.py](../../gateway/agent_model/deepagents.py)、[supervisor.py](../../agent/chat/supervisor.py)、[RealtimeRequest](../../agent/chat/realtime_understanding/state.py)、[test_supervisor.py](../../tests/agent/chat/test_supervisor.py)；本机 `outputs/local-services/api.log` 与 `outputs/server/logs/development/api.log` 及轮转文件中对应 Conversation／Message 的记录；`GET /api/agent/messages/get` 与 `GET /api/usage/overview?tenantId=local-default&runId=5485fc08-1dfe-4085-857b-d33dfe1bce4c` 的只读核验。

### C11：API 仍加载旧 Gateway 校验器，读取新视觉配置时拒绝 Chat 请求

排序日期：2026-10-11。沿用原“Chat 联调问题（2026-10-11）”章节日期。

优先级：高。状态：重新加载 API 后已消除该配置错误，真实单轮 Chat API 复测完成；截图的具体播放位置与浏览器操作未验证。

#### 现象与根因

- 用户截图显示 `GatewayConfig: vision does not support adapter 'openai'`，请求编号为 `7509f2c9-cb20-45a6-b289-6ebc22f8215a`。对应日志为 `2026-10-11 14:13:38`，`POST /api/agent/messages/send` 返回 HTTP 400，耗时 `12.6 ms`，尚未进入 Supervisor 或视频工具。这与 C08 的工具状态序列化错误不同，不能把 C08 的成功验收概括成所有 Chat 请求均已恢复。
- API PID `80107` 于 `13:33:24` 完成启动；磁盘 `gateway/multimodal.py` 修改时间为 `13:56:01`，`gateway/config.py` 为 `13:57:26`，`config.yaml` 为 `14:05:27`。当前配置的 `models.vision` 已从 Ark 改为 `deepseek_api / deepseek-flash`，但运行中的 API 仍加载修改前的 Python 校验器。
- 旧校验规则只允许 vision 使用 `ArkModelSourceConfig` 或 `OpenAIVisionSourceConfig`，因此拒绝 DeepSeek 对应的 `OpenAIModelSourceConfig(adapter="openai")`，精确产生截图报错。当前工作区代码已经根据适配器能力校验，并支持 OpenAI／DeepSeek 的有序图片输入；使用当前源码执行 `load_config()` 和 `read_global_agent_settings()` 均通过。
- Chat 的 `AgentChatService._current_config()` 每次发送前调用配置 loader，沿 `read_global_agent_settings() → _read_document() → GatewayConfig.model_validate()` 校验整份 YAML。因此进程内旧代码可以读到磁盘新配置，使全局 vision 配置变化阻断 Chat；`/api/agent/messages/options` 和 `/api/agent/global-settings` 也会受影响。重启前只读请求全局设置接口再次复现同样的 HTTP 400。

#### 本次处理与服务加载

- 沿用已授权的 API 重启范围，只重新加载 Seekino API；没有追加生产代码修改，也没有把视觉配置改回 Ark。C08 的两个 `compile(checkpointer=False)` 和外层 `durability="async"` 保留。
- 旧 API 收到 SIGTERM 后停止监听，但仍等待一个现有 HTTP 长连接关闭；SIGINT 后也未退出。核对 PID 与原启动签名后，仅对旧 API `80107` 使用 SIGKILL 完成退出，再启动 API；Web、MySQL 和其他进程未停止。
- 新 API PID 为 `11135`，`14:20:00` 完成启动；parse consumer 为 `running`，Web PID 仍为 `41425`。重启后 `/api/agent/global-settings` 与 `/api/agent/messages/options` 均返回 HTTP 200，全局配置 revision 与当前磁盘一致。接口恢复仅证明配置阻断解除，完整业务证据见下述复测。

#### 回归与实际验收范围

- 当前配置与适配器离线回归：`.venv/bin/python -W error -m pytest -q tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/gateway/test_multimodal_agent.py tests/gateway/test_deepagents_multimodal.py`，**145 passed**。
- 扩展回归：`.venv/bin/python -W error -m pytest -q tests/server/test_bootstrap.py tests/server/test_config.py tests/agent/chat/test_supervisor.py --tb=short`，**303 passed / 2 failed**。两项失败均发生在导入 Starlette TestClient 时，原因是已安装 AnyIO 的 `anyio.abc.BlockingPortal` 弃用警告被 `-W error` 提升为异常；它们属于扩展 Server 测试的依赖兼容问题，本次未升级依赖或修改测试来隐藏警告。
- 上述两项使用 `.venv/bin/python -m pytest -q tests/server/test_bootstrap.py -k test_app_lifespan_closes_only_services_it_owns --tb=short` 单独复跑，**2 passed / 1 warning**。不能据此声称扩展回归在 `-W error` 下全部通过。
- 真实模型复测沿用此前已授权的同一 mygo 视频与 `90.123 s` 位置。Conversation：`d717e944-8c34-4d8e-af5b-9ef7cb9f1325`；Assistant Message：`18eb1c89-b56e-4750-b0db-cf7f01ca8408`；Tenant：`local-default`。
- `14:20:45` 启动 `understand_current_video`，`14:21:10` 成功返回，`duration_ms=24559`；`14:21:24` 整轮完成，`duration_ms=45035`，回答 `593` 字符。消息只读 API 再次确认 `COMPLETED`、`lastError=null`。本轮未出现截图的 GatewayConfig 错误或 C08 的 checkpoint 错误。
- 用量记录确认 Ark `doubao-seed-2-1-pro-260628` 的 `RealtimeVisualObservation` 与 `RealtimeContextSummary` 各调用一次，外层使用 DeepSeek／Ark 配置内备用模型。既有 mygo 的实时 host 从 Parse Task 读取冻结的 `ParseConfig`，所以全局 vision 改为 DeepSeek 不代表已有任务的实时观察模型自动切换；本次没有验证新建 Parse Task 的 DeepSeek 视觉模型真实调用。
- 实时结果仍为 `partial=true`，不表示 C10 的基础片段失败已修复；模型工具列表仍含默认 `task`，C09 继续独立待处理。浏览器自动化没有可用实例，未验证浏览器／SSE，也没有对截图的 `10:27.52` 位置发起新的真实模型测试。

依据：[GatewayConfig](../../gateway/config.py)、[适配器能力](../../gateway/multimodal.py)、[全局设置读取](../../server/config/global_agent_settings.py)、[Chat 配置读取](../../server/service/agent_chat.py)、[实时 host](../../server/service/realtime_understanding.py)；本机 `outputs/local-services/api.log` 与 `outputs/server/logs/development/api.log` 中上述请求编号、Conversation／Message 与进程启动记录；消息与模型用量只读 API。

### C04：失败信息与下一轮 Context 的边界

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

归类说明：原文已澄清失败提示与下一轮 Context 的边界，无新增修复结论。

- 状态：当前行为已确认。页面对 FAILED Assistant 展示 `lastError.message`，下一轮历史只读取 `COMPLETED` 消息，构造 Agent 输入时也不带 `lastError`；因此页面异常文案不会直接注入下一轮 Context。
- 注意：失败请求对应的 USER 消息仍为 `COMPLETED`，下一轮可能再次看到原问题。可恢复的 Tool 错误若作为结果返回，则会进入**当轮**模型上下文；这与页面上的失败提示不是同一条路径。

依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx) 的失败展示；[agent.py](../../server/repository/agent.py) 的 `list_recent_completed_messages`；[agent_chat.py](../../server/service/agent_chat.py) 的历史构造。

### C06：Query Tool 将 topK 误用为整轮草稿数量上限

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

- 状态：已修复，待服务重启后整链复测。`topK` 只控制单次 `retrieve` 的候选数；Query 草稿不再受其限制，合法性、Tool 输出大小、精读范围和模型 Context 的保护仍保留。
- 已做：移除 Query Prompt、Runtime、输出绑定和草稿模型中的整轮条目上限；多次检索可累计选择超过单次 `topK` 的不同片段。
- 已做：草稿按稳定 `video_chunk_id` 去重；原样重复返回 `already_present`，内容变化返回 `updated`。`retrieve`、`update_draft_item`、`remove_draft_item` 向 Query Agent 返回当前 `draft_item_count`。
- 已做：Chat 只保存和发送用户显式覆盖的选项，默认值由服务端当前配置决定；页面将 `topK` 明确标为单次检索候选数。相关后端和前端回归测试已通过。

依据：[tools/](../../agent/chat/video_search/tools/) 的 `retrieve`／`update_draft_item`；[video_search_canvas.py](../../agent/chat/video_search/video_search_canvas.py) 的片段与草稿去重；[runtime.py](../../agent/chat/video_search/runtime.py) 和 [output.py](../../agent/chat/video_search/output.py) 的草稿绑定；[query.py](../../agent/chat/video_search/query.py) 的提示词。

### B03：Lite ASR 点时间戳被候选转写 Schema 误判

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

优先级：高。类型：输入契约过度校验。状态：已修复（`dabcb8d`）。

#### 问题与根因

上传完成后的解析阶段频繁出现结构化结果解析错误。实际失败点不在上传链路，也不是 Lite Provider 本身不可用，而是 `TranscriptCue` 曾强制要求 `end_ms > start_ms`。Lite 返回 `start_ms == end_ms` 的点状 Cue 时，结果会在候选转写反序列化阶段被 Pydantic 拒绝，后续解析尚未开始就进入失败／重试，因此日志中表现为同类解析错误连续出现。

这里混淆了两种不同契约：原始 ASR Cue 只是候选文本及粗粒度定位，不应被要求伪造一个正长度区间；`TimelineEvent`、AnalysisWindow 和 canonical VideoChunk 才需要严格的正区间不变量。Cue 字段中的 `_ms` 是视频内相对时间的内部表示，不是 Chat Submit 的 Unix 毫秒时间戳，也与 ParseJob 的处理耗时无关。问题不在于 Lite 是否能输出“精确到 1 ms 的持续时间”，而在于系统不应在 Lite 输出入口强制这种语义。

#### 已实施方案

- 删除 `TranscriptCue` 的跨字段严格区间校验，不再在 Lite 候选转写入口判断起止关系，允许 `start_ms == end_ms` 的点状 Cue。
- 保留必要的结构校验：起止值仍须为非负整数、文本非空、未知字段仍被拒绝。没有把原始模型输出当作任意 JSON 直接传入后续链路。
- 保留 `TimelineEvent` 等正式时间区间的 `end_ms > start_ms` 校验；本次没有放宽 canonical 视频区间、事件区间或数据库区间约束。
- 原始 ASR 与数据库事实保持原值；只有检索层在构造要求正区间的公开 Evidence 时做软投影：点状 Cue 临时表示为 `[t, t+1ms)`，反向或损坏 Cue 跳过并记录 warning。该投影不回写数据库，也不会令 ParseJob 失败或重试。
- 增加点状 Cue 回归测试，明确 `TranscriptCue(start_ms=3000, end_ms=3000, ...)` 可被接收，同时原有反向 TimelineEvent 拒绝测试继续保留。

#### 验收结果

- Lite 点状 Cue 不再触发候选转写 Schema 错误和由此产生的重复解析失败。
- 没有为了通过 ASR 输入校验而改写原始 Cue，也没有把 Chat Submit 的绝对毫秒时间戳规则错误套到 Lite ASR；`[t, t+1ms)` 仅是检索 Evidence 的非持久化兼容表示。
- 当前全量测试验证为 `844 passed, 8 skipped`；跳过项均为未配置隔离测试库的 MySQL 集成测试。

依据：[schemas.py](../../engine/parsev1/schemas.py) 的 `TranscriptCue` / `TimelineEvent`；[video.py](../../server/repository/video.py) 的检索边界软投影；[test_schemas.py](../../tests/engine/parsev1/test_schemas.py) 与 [test_video_retrieval_service.py](../../tests/server/test_video_retrieval_service.py) 的回归测试。

### B04：ParseJob 逐 Attempt／逐 Stage 计时造成高频写库

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

优先级：高。类型：不必要的持久化复杂度与并发开销。状态：已修复（`c1ab11a`）。

#### 问题与根因

上一版计时设计新建了 `parse_job_attempt_timing` 和 `video_chunk_stage_timing`，并在 SPLIT、ASR、PARSE、TEXT_EMBEDDING、VISION_EMBEDDING、INDEX 等阶段分别开始和结束计时。每个 VideoChunk 的每个阶段都会产生额外事务，并行 ASR 还会争用同一 Attempt 计时记录；但当前产品实际只消费 ParseJob 的累计 Worker 处理耗时和该数值是否完整。

这套明细计时与 Lite Cue 的时间单位没有关系。Lite 的相对媒体定位不应为了 ParseJob 性能统计而满足毫秒级正区间；ParseJob 的 `processing_duration_ms` 则是 Worker 使用单调时钟测得的执行耗时，两者必须保持解耦。

#### 已实施方案

- **没有新建计时表。** 在现有 `series_parse_job` 上增加 `processing_duration_ms` 和 `processing_timing_complete` 两个字段。
- Consumer 使用单调时钟测量一次 Attempt 的执行耗时。每个 VideoChunk 到达 PARSED checkpoint 时，复用现有 checkpoint 事务，以“此前 Attempt 累计值 + 当前 Attempt 已运行时间”为绝对目标单调更新 `processing_duration_ms`；`acknowledge` / `retry` 再以同一口径结算该 Attempt 的最终值，避免重复累计。
- Lease 过期时无法可靠知道完整耗时，因此不猜测数值，只将 `processing_timing_complete` 永久置为 `false`；后续重试成功也不会把历史缺口误恢复为完整。
- 删除逐 Attempt／逐 Stage 的 Model、DTO、Repository、Service 和 `VideoProcessingService` 阶段计时包装，消除每个 Chunk、每个 Stage 的额外写库及锁竞争。
- API 继续返回任务级累计处理耗时；运行中的任务保持 `processing_timing_complete=false`，其当前下界按 PARSED chunk 推进，前端按 RUNNING 状态显示“进行中”。任务结束后若因 Lease 过期或历史记录缺失仍不完整，则显示“至少 ...（不完整）”，不把已知下界伪装成精确总耗时。
- 这次 chunk 粒度刷新没有新增表、字段或独立事务，只复用现有 ParseJob 字段和 checkpoint 保存事务。

#### 受控升级

[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 是一次性迁移，执行前必须停止 Parse Worker；脚本会在仍有 `RUNNING` Job 时主动中止。

迁移直接给 `series_parse_job` 增加两个字段。若旧明细表已经部署，则仅在 Attempt 记录数量、序号 `1..N` 和完整性全部一致时汇总旧耗时；否则保守标记为不完整。若旧明细表从未部署，已经运行过的历史 Job 同样标记为不完整，不会误报成“0 ms 且完整”。完成折叠后删除两张旧明细表。

#### 取舍与验收结果

- 保留任务级累计处理耗时，并按 PARSED chunk 更新；仍放弃逐 Attempt、逐 Chunk、逐 Stage 的持久化诊断明细，这是有意的性能／可维护性取舍。
- 成功、失败重试、最终 Lease 过期以及“过期后再次执行仍保持 incomplete”均有回归覆盖。
- 全量测试结果为 `841 passed, 8 skipped`；本地 MySQL、Qdrant、API、Web 均已启动，`GET /healthz` 返回 `status=ok`，Parse Consumer 为 `running`。

依据：[job.py](../../server/model/job.py) 的 Job 字段；[consumer.py](../../server/mq/consumer.py) 的单调计时；[mysql_queue.py](../../server/mq/mysql_queue.py) 的原子累计、重试与 Lease 过期处理；[job.py](../../server/service/job.py) 的公开完整性语义；[20260919_parse_job_processing_timing.sql](../migrations/20260919_parse_job_processing_timing.sql) 的受控迁移。

### F01：新建聊天、历史对话切换与删除

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 类型：已修复。
- 当前行为：SearchPage 显示 Workspace 内的 Conversation 列表，支持新建、切换和二次确认删除；刷新后恢复最后选中的 Conversation。切换时复用原 Hook 的订阅清理与迟到事件隔离，不另建 SSE 管理器。
- 删除语义：终态 Conversation 在事务中物理删除，Message 和 Timing 由 MySQL 外键级联清理。存在 `PROCESSING` 回答时返回 `409 MESSAGE_IN_PROGRESS`，不与运行中的 checkpoint / complete / fail 回写竞态。
- 依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx)、[useAgentConversation.ts](../../web/src/features/search/useAgentConversation.ts)、[client.ts](../../web/src/api/client.ts)。

### F03：解析失败后的“重试”没有重跑解析

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 类型：已修复。
- 当前行为：上传传输失败仍由上传面板恢复；解析主 Job 最终 FAILED 时，任务卡调用 `uploads/retry-parse`，以 `uploadSessionId + expectedJobVersion` 将原 Job 重新排队。接口保留阶段、进度、Checkpoint、attempt 与用量历史，并以版本条件拦截并发或响应丢失后的重复投递。
- 补充行为：任务页另提供“整集重新解析”。`uploads/reparse` 只接受已完成上传的 SUCCEEDED / FAILED 主任务，清空 Checkpoint 后从视频开头运行；attempt 与用量历史保留，旧成功快照在新解析导入前仍可检索。
- 依据：[TasksPage.tsx](../../web/src/features/tasks/TasksPage.tsx)、[uploads.py](../../server/api/uploads.py)、[upload.py](../../server/repository/upload.py)。

## TODO（待修复解决）

### 模型暂态连接失败导致 Karpathy 整集停止（2026-10-30）

直接 JSON 链路漏识别 SDK 连接/超时/504，节点未重试且用户只看到执行异常。修复增加明确原因、有限重试、逐请求 trace 和脱敏终止日志；隔离验证见 [TRD](../design/03-product-reassessment/model-network-failure-trd.md)。EP1 已恢复原 v3，整集成功与稳定性尚待正式验收。历史同时断连的责任端无法由旧日志补推，模型单次输入和耗时仍需独立评测。


以下保留未修复、部分完成或同条目仍有主体问题未解决的记录，已实施的阶段性修复与历史证据一并保留。


### B22：Understanding 失败导致整块重跑与缺少任务停止控制

排序日期：2026-10-30。

10-30 续版关联：节点内重试不重复本轮 ASR，但 GPT 实测在最终网络失败后退出协调器，完整转写缓存仍被清理。跨协调器/跨重启复用由 [B20](#b20本地-whisperx-正式接入与字幕时间证据)的新跟进承接；暂停和网络分类继续保留原职责，不把新 PR 扩成全部任务控制重写。

2026-10-30 暂停控制进展：新增 [暂停/继续设计](../design/03-product-reassessment/parse-pause-resume-trd.md)、持久控制 API 与任务页入口，隔离测试通过。开发库 Micrograd 已暂停（容量已释放、活动 Chunk 租约归零），Building GPT 保持原进程进行整集 ASR。旧 Consumer 通过未来调度时间兼容暂停，正式新入口同时检查 paused。13:56 本机 5173 浏览器验收通过，进行中为 1、ASR 阶段与暂停/继续入口准确，幂等暂停成功、旧租约返回 409；未中断 GPT 的原进程。Understanding 失败的阶段复用仍属本条待办，不能由暂停能力推定解决。


状态：TODO（节点重试和最终失败停止已实现；真实运行验收、手动暂停与任务清理未完成）。hypnos 发现解析持续空转，要求 Understanding 内部 node 自行有限重试，Consumer 收到最终异常后停止该视频任务并返回前端错误。原实现 Consumer 重新排队整个 Chunk，导致成功但尚未提交的 ASR 可能重复执行。

2026-10-30：拆分 ASR、视觉准备、OCR、理解节点；暂态错误只重试失败节点，成功观察保存在本次子图状态内。最终失败持久化 `PARSE_NODE_FAILED`，停止新派发、拒绝兄弟任务续租和迟到提交，沿用任务通知与 API/SSE 展示阶段及原因。进程崩溃的租约回收仍独立存在；本地原生 ASR 已进入的阻塞调用不保证立即取消。未重启服务、未变更真实任务，未声称内存峰值已经实测改善。

仍待：真实失败到前端验收；指定视频暂停、一键全量暂停和保留素材的无用任务清理。设计见 [失败边界](../design/03-product-reassessment/video-parse-performance-trd.md#61-2026-10-30understanding-节点重试与最终失败边界)，验证见 [devlog](devlog.md#devlog-node-retry-20261030)。

### B15：localhost MCP 与外部 Agent 素材取用闭环

排序日期：2026-10-29。

优先级：高。状态：**MCP 实现、离线验证、真实 Codex 客户端发现与直接工具调用已通过；模型自主调用超时，真实素材到作品演示仍待恢复数据，保持 TODO。** 目标是在 10 月 30 日早上前让 Codex 实际发现 Seekino 工具、检索 Event、核对版本、取得媒体并完成外部作品。用户确认先恢复旧机 MyGO，后续另提供一系列视频。

现有 Event 检索和媒体服务已复用到本地 `/mcp`，暴露 `list_series`、`retrieve`、`read_evidence`。固定宿主 local tenant，回读重验任务/内容版本/事件身份，对外时间为整数毫秒；Cloud 不挂载，远端及转发连接拒绝。本轮不接通用 Sandbox、不改解析或索引算法。

验收须分别记录工具协议、实际 Codex 连接、真实数据检索与媒体消费。目前可见 API/数据库/ES 未监听，`.env.local` 指定的媒体根为空；隔离 fixture 不代替真实视频验收。实现与检查结果见 [devlog](devlog.md#devlog-local-mcp-20261028)，设计见 [MCP 接入 TRD](../design/03-product-reassessment/mcp-access-trd.md)。

### B17：持久媒体目录与旧资料库恢复

2026-10-29 补充：hypnos 要求将整个 `assets/` 加入 Git ignore，排除未跟踪素材；已跟踪发布资产保留。此前“未忽略”的检查仅描述当时状态，媒体仍须独立备份。

排序日期：2026-10-29。

类型：持久资产布局、换机恢复。状态：**媒体默认目录和本机配置已改为 `assets/library`，12 集原片已归档并校验；旧 MyGO 数据库成果与实际播放、检索尚未恢复，保持 TODO。** 用户要求重要原片离开易被视为临时输出的目录，并复用过去 MySQL 中的解析成果；随后明确不希望本机运行 Docker，最终选择全原生 localhost，环境配置与验收进展见 B18。

- 已完成：配置解析、启动器和 `.env.example` 的媒体默认根一致，本机 `.env.local` 同步；`assets/library` 不加入 ignore。旧媒体根实查没有文件，无文件需要从旧根迁走。12 个完整 MP4 共 5,055,403,557 bytes，保存于 `assets/library/sources/mygo/`，与旧样例目录为同盘硬链接；大小、SHA-256、inode 对应关系已核验，`ffprobe` 可识别音视频。第 11 集仍为下载中的 `.part`，未纳入恢复。
- 恢复依据：10 集哈希与历史验收记录一致，EP01、EP07 未找到历史哈希依据；尚未与真实旧库核对。改名前的部署记录指定旧 MyGO 主业务库为 `anivag_local_20261010_development`；找到的本机 `seekino-rename-20261028/database-before.sql` 实际导出自另一个库 `anivag_development`，为 57,017 bytes、28 个表结构，只有 `checkpoint_migrations` 含 INSERT，没有视频和解析成果，不能作为旧 MyGO 备份导入。
- 同日追加只读核对：运行中 API 的进程环境与 `.env.local` 均指向 `127.0.0.1:3306/seekino_development`；MySQL 8.4.13 实际数据目录为 `data/mysql/`。该实例除系统库外只有这一个业务库，`series=1`、`video=1`、`video_parse_task=4`、`chunk_parse_content=2`、`interval_parse_content=2`，对应本轮单条验收视频及其解析版本；没有另一个已加载的旧库可直接切换。这个快照不代表旧设备或其他数据库实例的状态。
- 同日恢复请求补查：用户明确旧电脑和整机备份均已不可用，并指认 `docs/migrations`。逐项检查该目录 22 个 SQL：INSERT 仅写入 `_bm_*` 临时结构校验表、默认本地 user/tenant 和 checkpoint 版本，不包含 MyGO 的系列、视频、解析成果或对话数据。个人仓库 Git 历史、AniVAG 未跟踪源码包及学习报告源码归档也未找到业务备份；本机 Time Machine 未列出快照或备份目的地。现有原片可作为重建输入，但不能据此恢复旧 ID、解析版本或消息历史。本轮未执行升级脚本、导入 SQL、启动 Docker 或触发整集重解析；[逐文件审计](../../assets/library/recovery/database-audit-20261029.json)保留文件哈希和检查范围。
- 迁移缺口：`/Users/hypnos/workspace/restore-report.json` 明确记录目录迁移未恢复数据库；10 月 28 日命名空间切换记录也说明当时检查的 `anivag_development` 业务表为空，所以启用了新空库。项目与已知迁移目录的有界搜索，以及三份源码／个人归档的成员核对，未发现旧 MyGO 业务 dump 或 MySQL 数据文件。仓内[换机材料范围](../migration/20261023/README.md#仍需私下备份的内容)明确将数据库备份列为另行私下保存的内容；完整核对路径见下方 devlog。未排除旧设备、其他备份或停止的数据库存储，不能断言旧数据永久丢失。
- 验证：配置、启动器与存储相关测试 422 passed；实际配置解析为 `assets/library`，12 个原片路径均未被 Git ignore。曾启动 Docker Desktop 进行恢复准备，用户提出不使用 Docker 后已执行停止；未导入 SQL、重建索引或触发模型调用。
- 云端准备：按用户授权配置本机 `seekino-server` SSH 别名与 0600 私钥，未把密钥写入仓库。直连超时后，按用户提供的 docrag 经验增加本机 SOCKS ProxyCommand 和目标 IP/22 端口的日本节点规则，实际登录及 16 MiB 合成传输通过。服务器 4 vCPU、约 8 GB 内存、约 108 GiB 磁盘剩余；常见项目路径未发现 Seekino，MySQL/ES 未监听。保持既有代理组选项，未实施云端部署或上传原片。
- 剩余验收：读取真实旧业务数据库或其备份；按旧 URI 与哈希关联原片，核对派生媒体，验证播放与 ES 检索。只有表结构或原片不能恢复旧解析成果，评测 fixture 不作为业务数据库替代品。本轮新开发库初始化不代表恢复了旧库。

依据：[媒体目录与迁移合同](../../deploy/README.md#persistent-media-root)、[原片清单](../../assets/library/recovery/mygo-20261029.json)、[本次 devlog](devlog.md#devlog-persistent-media-20261029)。

### B19：课程本地文本向量与可关闭的视觉索引

排序日期：2026-10-29。

状态：TODO（本地接入、视觉向量开关、llama.cpp 独立运行和正式 MySQL 单任务冒烟已完成；恢复与课程闭环验收待做）。用户选择本地 Qwen3-Embedding-0.6B、1024 维，明确要求视觉向量可关闭，保留 OCR/VLM 理解。修改前 Ark-only 编码和强制视觉索引阻碍此方案；19:08 的旧模型请求还出现供应商 404。

验收：本地模型真实编码；显式 text 模式冻结至任务；Chunk/Event 不调用视觉编码也能正确发布和恢复；查询仅使用匹配文本空间；默认多模态检查不退化；隔离课程样本保留来源、时间、召回与延迟证据。代码、单测、实模运行和完整课程效果分开报告。不得以同维度混用新旧向量。

2026-10-29 实证：4 个课程片段经本地 Whisper 转写后人工投影、实际 Qwen 编码与隔离 ES 发布；8 条中文查询均首位命中预期片段并核对文件/时间，0 次视觉编码。仅 4 个候选，问题在读过转写后编写，不能作为整库质量结论。真实 MySQL 完成/重启恢复、HTTP 回看与追问仍待验证。50 项集成测试（含真实隔离 ES）通过，CLI/sparse 追加修复的 631 项组合回归通过，计数有重叠。

设计：[本地文本向量 TRD](../design/03-product-reassessment/local-text-embedding-trd.md)；过程与指标：[devlog](devlog.md#devlog-local-text-embedding-20261029)。原始证据：`outputs/verification/local-text-embedding-20261029/`。本轮未切换主服务、未重建旧业务索引、未批量解析全课程。

后续更新（2026-10-29）：按用户选择完成 Ollama → llama.cpp 直连，独立原生程序与 GGUF
路径已通过无 Ollama 服务的真实编码。修复本地启动器遗漏 text 模式导致的索引合同冲突。
用户收窄范围前已提交的唯一 60 秒任务为 COMPLETE；用户随后明确本项只做 embedding，
Whisper/音轨对齐由另一项工作处理，因此不再执行课程、回看或追问验收。
临时验收 API 与 Whisper 已关闭，embedding 服务独立保留。见
[直连接入记录](devlog.md#devlog-llama-cpp-embedding-20261029)。

### B20：本地 WhisperX 正式接入与字幕时间证据

**10-30 新一轮跟进：** #12 整片前置复用与 #15 阶段日志均已合入 dev；需求仍为 TODO。新分支 `fix/video-asr-recovery` 围绕“长视频 ASR 代价高，且下游失败后成果不可复用”开启 Draft 跟进，当前提交背景续版、修复方案与实测证据。拟按 `video_parse_content` 独立保存完成产物，补租约安全发布/恢复、正式 Metal 转写与独立对齐，并通过完整课程验收。下一项是完成后重启/失败重试的复现与持久化修复；这些实现及运行切换尚未完成。沿用[原主设计](../design/03-product-reassessment/local-whisperx-asr-trd.md)，关联 B07、B22 和模型网络问题，不新建重复状态入口。

2026-10-29 整片增量：新建 WhisperX 任务支持默认视频级转写，原 Chunk ASR 节点改为读取相交 cues，保留绝对时间及只读证据；旧任务缺省仍逐块执行。内存缓存随协调器租约释放，重启会重新转写；未实现持久 ASR checkpoint。ASR 时间加入既有有效总时长。代码与离线验证已完成，真实整片识别质量、资源、吞吐和运行部署仍待验收；没有启动全量解析。[设计](../design/03-product-reassessment/local-whisperx-asr-trd.md#视频级转写与-chunk-读取)、[本次验证](devlog.md#devlog-full-video-asr-20261029)。

2026-10-30 增补：独立 [PR #12](https://github.com/owariband/Seekino/pull/12)（由 #9 迁移至 `feat/full-video-asr`，实现与验证保留）同步 dev 的直接 JSON 与节点重试修复，扩展离线验证为 10 个 Chunk 并发且一个理解节点暂态失败，仍仅进行一次整片 ASR。运行服务未部署；当前本机 1,200 秒 ASR 超时是否覆盖长课程待测，旧任务需新解析版本才能采用 video scope。B07/B22 的实现和隔离验证不能替代课程全链路稳定性验收。

同日落地补强：复现并修复恢复时 Chunk 提前领取、视频 ASR 失败/过期租约未进入有限准备预算的问题。当前租约的 ASR 与媒体准备完成后才写入派发回执；新租约不复用旧回执。观看问答新建的按需任务保持 chunk scope。测试与实机小样本边界继续记录于同一 devlog；此增量尚未合并或部署。

2026-10-30 13:26 上线进展：用户明确要求上线后，PR #12 合入 dev `029faa34`，本机演示 API 已重启加载。完整《Let's build GPT》新任务 v5 冻结 `models.asr.scope=video`、Parse 并发 10、ASR 超时 14,400 秒；正式日志已进入视频级 `asr` 阶段，输入为 6,979.873 秒完整音轨。原逐块版本成果保留；整集转写、10 路真实 Understanding、最终索引及课程检索追问尚未完成，不将本次部署记为整集验收通过。[部署证据与边界](devlog.md#devlog-full-video-asr-rollout-20261030)。

同日可观测性补充：WhisperX Adapter 新增关联调用／任务 context 的阶段、30 秒存活及转写／对齐进度日志；85 项定向回归通过。当前已启动的整片调用无法补入新的回调，使用每 30 秒读取任务及进程状态的独立日志保持本轮可追踪，不重启 API。百分比只代表对应阶段的上游片段比例，尚无回调时保留未知；本轮整集耗时仍待测。[验证与生效边界](devlog.md#devlog-whisperx-progress-20261030)。

2026-10-30 14:46 核验：GPT 整片 WhisperX 已于 14:42:41 完成，实际 76 分 31.557 秒，随后真实启动 10 个 Understanding 调用。用户质疑时长后实测本机 M5 Metal：同一 GPT 60 秒音频为 3.760／3.474 秒，300 秒音频为 12.992 秒；同 Turbo Q5 模型的 CPU 60 秒对照为 17.196 秒。四次归一化校验通过。GPU 使用独立 whisper.cpp，未切换正式 WhisperX、未做强制对齐或整集 GPU 验收；本机性能适配和完整闭环仍待解决。[实测报告](../report/gateway/local/whisper-metal-benchmark-20261030.md)。

同日 15:08 全量补验：116 分 20 秒 GPT 原音轨的 Metal Turbo Q5 单路转写实际 293.578 秒，含提取和启动约 5 分钟，2,476 cues 全量归一化通过，完整原始/规范化结果已落盘；尚无强制对齐和人工质量验收。正式任务此前因 Kimi 504 后重试 DNS 失败停止，保留 8/117 Chunk。协调器退出就清理整片内存转写，失败重试也会重做；视频级 ASR 持久化和正式 Metal 接线是待解决的恢复/性能阻塞，实验文件不冒充生产 checkpoint。详细证据续记同一报告。

排序日期：2026-10-29。状态：TODO（正式接入、三语实机部署与 Karpathy 任务提交已完成；完整系列效果及字幕消费待验收）。

用户需要可自行下载模型、直接运行项目代码的低 API 成本 ASR，并用强制对齐支持字幕时间。此前本机实验是 whisper.cpp，正式配置为 Ark，不能算 WhisperX 已接入。

本次加入 `adapter: whisperx`、可选依赖、正式 Gateway 工厂与共享模型生命周期；转写后执行 align，字词结果经 Engine 毫秒归一及 Server 原片偏移后随 ASR evidence 保留，未知字词时间为 null。配置改名 `config/local-dev-config.yaml` 并更新本机环境入口，保留原有云端来源作为可选项。未安装本地 ML extra 也可运行原链路。字幕分行、展示和导出仍未实现。

2026-10-29 后续部署：已安装可选运行时并校验 large-v3、英/中/日对齐权重；三语短样本经正式 Gateway 取得有效字词时间，中文为合成语音且存在“字幕→字母”识别错误。75 项相关回归通过。本机开发服务已切换 WhisperX 和本地 Qwen3 文本 embedding；使用新 ES 索引保留旧向量数据。8 集 Karpathy 原片（约 14.61 小时）通过正式 API 创建持久解析任务，首集已被消费者领取。启动观察中 Kimi 视觉请求出现远端断开和 HTTP 504，消费者正在重试；本地 ASR 验证通过不等于 VLM/索引闭环通过。验收边界：未完成全系列解析、人工对齐精度评测、恢复验证和播放器字幕。证据见[部署报告](../report/gateway/local/whisperx-deployment-20261029.md)。

设计与部署：[WhisperX TRD](../design/03-product-reassessment/local-whisperx-asr-trd.md)、[模型配置](../model-config.md#本地-whisperx-asr-与对齐)；过程与测试结果：[devlog](devlog.md#devlog-whisperx-20261029)。

### C20：Cloud Chat 的 pi 与 Deep Agents 上下文职责重复，需收敛持久化与恢复合同

排序日期：2026-10-24。

优先级：高。类型：Chat harness、上下文持久化与部署复杂度。状态：**代码实现、离线回归、开发环境真实 Chat 与 MySQL 恢复已验证：真实 MySQL 6 passed，两组均完成 3/3 轮，重启后恢复 30 条原生消息／13 条 ToolMessage。Deep Agents 总耗时约为 Codex 的 3.66 倍，且第三轮出现文案与视频块结束时间不一致；生产迁移、性能与质量回归评估、净维护成本仍未完成，保持 TODO。**

#### 背景与当前问题

云端引入 pi 时，需要避免一会话一进程、复用已有 SessionManager／compaction，并补齐当时 Deep Agents 仅使用 `InMemorySaver` 的恢复缺口。随着 Deep Agents＋LangGraph 持久化 checkpointer 被核实可覆盖模型／工具循环、原生消息、compact 和 thread 恢复，pi 的 Node Host、Python↔Node IPC、Conversation↔pi-session 映射及 JSONL 生命周期成为当前单实例部署中的重复层。

本次只收敛 Cloud Chat Supervisor，不处理本地 Codex／Claude、Parse 或 Sandbox。`agent_message` 继续承担前端展示与业务终态；Deep Agents checkpoint 承担原生 Agent 上下文。历史 pi JSONL 不删除，旧会话没有 checkpoint 时仍按现有 SQL 展示历史冷启动，不承诺旧 tool 上下文无损迁移。

#### 方案与验收条件

采用 Deep Agents＋`PyMySQLSaver`，每轮先在内存执行，SQL 消息确认后才提交完整 checkpoint；失败、取消和关闭不得删除已确认历史。cloud 改为要求 `supervisor_harness: deepagents`，删除 pi Host、Python client、配置和专属测试，保留退役设计作为历史依据。

验收必须分别记录：

1. tool call／result、DeltaChannel、compact 和 offload 文件在 store 重建后保持一致；`history_message_limit` 不影响 Agent 恢复。
2. 失败、取消和未确认 turn 不污染上一已确认 checkpoint；只有显式删除会话才删除其 checkpoint。
3. cloud 配置、共享 store 生命周期、初始化和既有 Chat 回归通过；真实 MySQL `setup()` 与恢复使用隔离数据库。
4. 真实业务 case 固定模型、数据、工具和预算，对比较完成率、证据正确性、覆盖度、完整时延、U/C/W/O、RSS 与存储增长。没有数据时明确写未测，不用删除代码量替代整体收益。

依据：[统一 devlog](devlog.md#devlog-cloud-chat-checkpoint-20261024)、[Deep Agents 适配器](../../gateway/agent_model/deepagents.py)、[历史 pi Host TRD](../design/02-agent-harness/pi-host-trd.md)。

### C19：Chat Agent 误把表达反馈当台词检索，Query Router 需明确本轮任务再追加提示

排序日期：2026-10-21。

优先级：高。类型：多轮对话意图识别、Query 改写与回答目标偏移。状态：**`query_hook` 已接入 Chat；当前优先测试短对话效果，长对话上下文截断与 harness 对接暂缓。** 下方 2026-10-20 的 schema 与“待接入”描述保留为历史方案，当前实现以本次更新为准；分类准确率、回答效果与长对话稳定性仍待验收。

#### 2026-10-21：沿用服务层近期消息，先验收短对话

- **当前链路。** Supervisor 进入 harness 前执行一次 `query_hook`：Jev 判定 `respond / retrieve / mixed`；`retrieve / mixed` 再由纯文本 Qwen3-8B 填写 `retrieval_query`，`respond` 跳过 8B；代码将固定模板与候选词追加到模型输入。业务消息仍保留用户原文。当前不采用下方历史方案中的 `clarify / current_task` 输出。
- **上下文现状。** Hook 使用服务层传入的已完成用户／助手消息，与 Supervisor harness 的内部上下文分开；当前配置最多取最近 **6 条消息（不是 6 轮）／合计 2000 字符**，边界消息按尾部字符截取。它没有消费 harness compact 的结果，也没有单独维护新的 context 表或摘要模型。
- **本次决定。** 暂时沿用这条服务层链路，不迁移 pi／Codex hook，不实现长对话压缩，不修改现有截断逻辑；先让用户测试短对话。此次只更新问题记录，未修改运行代码或重启服务。
- **截断影响。** 超过窗口后，Jev／8B 可能丢失指代对象、前文限定或用户刚纠正的意图。按字符截取还可能改变句意：此前离线边界复现中，`这不是动画中的角色台词。` 保留最后 9 字符变成 `动画中的角色台词。`；这是输入构造缺陷的复现，尚未据此测得真实 Jev 的误判率。短对话只要相关历史未超过上述限制，就不会触发这项历史裁剪；即便轮数少，单条长回答仍可能超限。

待办（本阶段暂缓）：

- [ ] 明确 Hook 长对话上下文的来源与预算：继续由服务层提供近期消息，或复用 harness 的有效上下文／摘要；届时再决定，不先增加第二套记忆存储。
- [ ] 处理字符截断破坏语义的问题，优先评估保留连续完整消息；补测窗口边界、否定句、指代与意图切换，不通过补造历史弥补缺失。
- [ ] 短对话效果稳定后，再比较长对话中裁剪前后的路由、填槽与最终回答；若接 harness，保证每个用户轮次只运行一次 Jev／8B，工具循环复用结果。

当前短对话验收：使用用户原句依次测试“台词出处 → 评价助手表达 → 再次要求检索”，另开短会话测试独立回应与检索混合请求；不要手工添加 Router 提示。分别检查 Jev 动作、8B 候选词是否保留原句和约束、最终回答是否回应本轮诉求、工具调用是否符合需要，以及 Hook／整轮耗时。服务健康检查通过只说明可访问，不等于上述效果已验收。

依据：[Hook 与历史截取](../../agent/chat/query_hook.py)、[当前分类规则与固定模板](../../agent/chat/query_hook_prompts.py)、[Supervisor 接入点](../../agent/chat/supervisor.py)、[默认配置](../../gateway/config.py)。

#### 2026-10-20：最新进展与证据边界

当时状态：**已定位“问起来很笨很蠢”的具体行为，确定 Query Router 三字段方案与 append 模板；用户最新测试反馈“效果还挺好的”。** 当前正向结论属于改写提示的体验反馈；自动 Router 接入、识别准确率和多轮稳定性尚未在本项验收，故保留在 TODO。本次只维护问题记录，不修改运行代码。

- 用户在明确两轮完整改写示例后反馈最新效果较好，认可“保留原文 + 追加本轮任务解释”的方向。下方保存采用的 schema、具体输入和此前失败过程，便于后续继续测试。
- 这次反馈没有附带可关联的新 Conversation／Message ID、完整模型回答或工具统计，不能补造最新成功样本的耗时、零工具次数或通过率，也不能写成“自动 Query Router 已上线”。
- 早期有原始日志的对照实验中，“更明确的意图提示”曾连续两次遇到 HTTP 429；那两次没有有效回答，属于服务失败。后续用户的正向反馈是新的证据，不把它倒填为早期实验成功。

#### 原始问题：第一轮找出处，第二轮已经在评价助手

真实对话的任务变化是：

1. 用户第一轮发送 `真是高高在上呢 哪里出现过`，要求定位台词出处；此时检索符合请求。助手找到第 13 集开头的相近表述，并解释集数、位置、人物和上下文。
2. 用户第二轮发送 `那又咋了，你说话怎么这么无趣啊`，在评价助手的表达方式。Agent 却继续把这句话当成待定位的对白，检索“那又如何”“无趣”等内容；下方独立复测中还交付了新的片段。
3. 用户需要进一步明确“不是，我是说你……我没叫你查片段了啊”，Agent 才回到对话反馈。用户感到“笨”，具体体现为不能识别本轮谈论对象、不能切换回答目标，以及为错误目标持续调用工具。

最初真实游客会话为 `7a0b468e-edf8-4caf-8acc-673a5d95f7fc`；其中第二轮 Assistant Message `ca5be22e-ccc9-4e66-9d90-00ee60fa1b1c` 耗时 **33.729 s**，调用 2 次搜索、1 次 Canvas 读取。之后用于独立对照的会话为 `485647e5-ebf9-47d0-8841-0ab635118646`，首轮 Assistant Message 为 `974e4827-ddde-4c98-8ff3-c1c904d069f8`。这些回答可以在技术状态上 `COMPLETED`，同时在语义上没有完成本轮任务；HTTP 成功或正常结束不足以判断体验正确。

**关键澄清：`我不需要检索视频` 是用户手工模拟 Router 产出的测试前缀，不是产品用户的真实意图或新增禁令。** 要自动识别的原始第二轮消息始终是 `那又咋了，你说话怎么这么无趣啊`。不能从测试文本反推“用户已经明确禁止检索”，也不能要求真实用户每次自行加此前缀。

#### 改写试验经过：只约束工具，仍然会回答错任务

有原始记录的对照从同一个已完成首轮的 native Codex 历史边界分别 fork，保留模型／工具历史；使用同一运行中的 Cloud API、模型、工具及预算，在独立测试游客会话发送不同的第二轮文本。原会话及首轮历史哈希未变。下表前四行来自该实验，第五行为后续用户反馈：

| 阶段／输入 | 观察结果 | 暴露的问题或结论 |
| --- | --- | --- |
| 原句：`那又咋了，你说话怎么这么无趣啊` | **147.943 s**；6 次搜索（含 Query 子任务）、8 次 Canvas 读取、1 次 Query 委派、1 次视频提交 | 把表达反馈当作新台词，调查多个场景，交付用户并未请求的视频 |
| 手工加前缀：`我不需要检索视频    那又咋了，你说话怎么这么无趣啊` | **10.629 s**；0 次工具调用；先说“行行行，不查了”，接着又解释第 13 集的剧情和出处 | 工具倾向改变，但本轮回答目标仍错；口语化或加表情不能代替意图识别 |
| 更明确提示，第 1 次：追加“这条消息是在评价助手上一条回复的表达方式……不要继续上一轮的剧情解释。本轮不检索视频。” | **3.282 s**，HTTP 429，无有效回答 | 无法评价提示效果，不计为语义成功或失败 |
| 同一更明确提示，等待后第 2 次尝试 | **3.393 s**，再次 HTTP 429，无有效回答 | 停止有限重试，保留实验未完成状态 |
| 后续采用下方三字段 schema 所表达的任务解释，并明确两轮完整 append 文本 | 用户最新反馈“效果还挺好的” | 改写方向获得体验层面的正向反馈；不能由此推导自动 Router 的识别准确率 |

原句对照 Assistant Message：`c789ce52-d281-4f28-b474-b69011f115a2`；无检索前缀对照 Assistant Message：`ff89bce4-4fa8-44fe-a66b-86cace298381`。原计划每组重复两次，实际只完成上述两个回答样本；耗时是顺序运行的描述性记录，缓存与模型服务负载没有受控，不能当成稳定性能收益。

用户此前单独测试同一前缀的 Assistant Message 为 `7e5d2643-6a8e-4162-8cdd-40900c32237a`，耗时 **10.281 s**、0 次工具调用，也继续讲剧情，并自行延伸出“以后你随口聊剧情，我也随口聊”的承诺。独立对照的无检索回答还把用户记忆中的近似说法作为直接引语，丢掉首轮已说明的字幕措辞差异。这些现象说明验收不能只检查“不调用工具”。

#### 问题理解与方案取舍

- **缺的是本轮任务解释。** 当前 [Supervisor](../../agent/chat/supervisor.py) 对台词、动作、情绪检索给出了详细策略；这可能强化检索倾向，但不是已经通过消融证明的唯一原因。已观察到的行为是：Agent 沿用上一轮找视频的目标，未正确处理当前消息对助手的反馈。
- **工具选择、当前回答目标、历史判断需要分开。** “这一次回应反馈，无需检索”不等于“上一次不该检索”。上一轮用户确实问了出处；Router 不能仅凭本轮处理动作让模型为上轮检索道歉、否定已核实内容或形成永久不检索的偏好。
- **避免以第一人称替用户补话。** 测试前缀只用于验证引导效果。正式方向是保留原始消息，将 Router 生成的解释单独标为应用侧、本轮有效的辅助说明；不把推断包装成“用户明确要求”。
- **先识别任务，再决定是否补证据，最后才生成候选检索词。** “她为什么这么说”可能可以利用现有证据回答；“别讲得像报告，再帮我找另一场演出”同时包含反馈与新检索目标；不能把所有反馈都机械路由成不检索。
- 保持固定 system prompt，按固定模板在本轮输入中 append 动态任务信息，符合此前尽量维持稳定前缀的设计目标；本项未测量 prompt cache 命中率。主 Agent 仍需结合原文核对 Router 判断；Router 不负责替代证据核验或修改资源权限。

#### 采用的 Query Router schema

输入包括当前原始消息、近期用户／助手对话，以及已有视频证据的简要信息（如有）。先使用三个输出字段，不另加庞大的意图分类、置信度数字或“上一轮是否正确”字段：

| 字段 | 取值与职责 |
| --- | --- |
| `action` | `respond`：现有对话足以回应；`retrieve`：需要补充视频证据；`clarify`：上下文无法消除、且会改变行动或目标的歧义 |
| `current_task` | 描述本轮要完成的任务，包含必要的指代解释；忠实保留混合请求，不新增事实结论、用户禁令、说话风格偏好或长期承诺 |
| `retrieval_query` | `string \| null`；候选检索用语保留用户台词原话，只补充明确指代，不猜测人物或剧情；`respond / clarify` 必须为 `null`，`retrieve` 没有可靠检索词时也允许为 `null` |

`retrieve` 表示需要补充证据，不强制重新搜索；可以先读已有候选的上下文。候选检索词不覆盖用户原句。Router 只输出这三个字段，由代码按固定模板生成 append；当前采用的是该协议和提示方案，本项没有完成自动生成／消费这份 schema 的生产接入。

第一轮期望的 Router 输出：

```json
{
  "action": "retrieve",
  "current_task": "在当前可检索的视频范围内定位“真是高高在上呢”的出处，核对上下文并回答集数和时间位置。",
  "retrieval_query": "真是高高在上呢"
}
```

第二轮期望的 Router 输出：

```json
{
  "action": "respond",
  "current_task": "回应用户对助手上一条回答表达方式的评价。",
  "retrieval_query": null
}
```

#### 两轮实际采用的改写文本示例

以下为提供给主 Agent 的完整文本；原始消息保留，任务说明由应用追加。第一轮：

```text
真是高高在上呢 哪里出现过

[本轮任务说明｜应用生成，仅适用于当前消息]
用户在询问“真是高高在上呢”这句台词在视频中的出处。

本轮任务：在当前可检索的视频范围内查找这句台词，
核对命中的上下文，回答它出现在哪一集、什么时间位置。

处理方式：需要检索视频。
候选检索用语：真是高高在上呢

用户提供的措辞是待核验的线索。若找到的是相近译法或表述，
应说明差异，不要把相近内容直接当作原句命中。
[/本轮任务说明]
```

第二轮：

```text
那又咋了,你说话怎么这么无趣啊

[本轮任务说明｜应用生成，仅适用于当前消息]
结合上一条助手回答，当前消息中的“你”指助手。
用户正在评价助手的表达方式，并未提出新的台词定位请求。

本轮任务：回应用户对回答方式的反馈。
这句话应作为对助手的反馈来理解，不作为待检索的视频台词。

处理方式：直接根据当前对话回应，无需调用视频检索工具。
回应重点是用户当前的反馈，不继续复述上一轮的出处和剧情解释。

本轮无需检索，不表示上一轮检索不当，也不意味着之前的事实结论错误。
不要据此推导用户以后都不需要检索，或擅自认定用户喜欢某种说话风格。
[/本轮任务说明]
```

第一轮交给搜索工具的候选 query 仍是 `真是高高在上呢`，不是整段任务说明；第二轮不生成检索 query。完整模型输入与搜索工具参数是两种不同的数据，不混为一个“改写 query”字段。

#### 后续实施与验收

- 分开验证两层：先手工给定正确 Router 输出，验证主 Agent 是否理解 append；再验证 Router 能否从未经人为补充的用户原文和历史中自动生成正确结果。手工提示有效不能证明自动识别已经有效。
- 固定回归至少覆盖两轮原始对话、已有证据追问、反馈与检索混合请求，以及需要澄清的指代；后续用户再次要求查片段时应正常检索，不能继承旧轮次的无需检索判断。
- 同时检查任务是否答对、工具是否必要、是否无依据否定历史答案／检索、是否擅自新增未来偏好，以及引用事实是否保持证据边界；不能只验收零工具调用或 `COMPLETED`。
- 自动接入时保留用户原文，内部另留本轮路由结果和版本，便于重放、重试及上下文恢复核对；当前没有新增数据库字段或 history 表。自动 Router 接入、真实调用延迟及多样本稳定性属于后续工作。

依据：[对照实验摘要](../../outputs/local-services/cloud-auth/query-rewrite-eval-20261020-171035/summary.md)、[完整输入／输出及样本 ID](../../outputs/local-services/cloud-auth/query-rewrite-eval-20261020-171035/report.json)、[运行日志摘录](../../outputs/local-services/cloud-auth/query-rewrite-eval-20261020-171035/runtime-log-excerpt.txt)，以及 2026-10-20 本次讨论中的用户测试反馈。`outputs/` 为本地实验产物，关键输入、结果、schema 和限制已保存在本条，避免后续仅依赖本地文件。

### B12：ASR 匿名说话人分组不准，Refine 看帧后仍未纠正目标归属

排序日期：2026-10-19。

优先级：高。类型：解析语义质量、音频分段、证据供给与可观测性。状态：**截至 2026-10-22 仍未全面闭环；v25 两句固定目标本轮均正确**。历史 v5 曾纠正爽世单例，不代表后续版本稳定识别正确；v11～v18 暴露参考错名、跨人合并及 Refine 决策三类问题。最新归因与未完成项见下，旧轮次记录保留。记录日期：2026-10-16；更新日期：2026-10-22。

#### 2026-10-22 后续：声纹预填最短时长调整为严格 >500ms（已实现，未部署）

- 用户针对#12开头900ms立希短句仍为null，要求把时长条件放宽至严格大于500ms。开发副本仅将B0 ACCEPT最短时长改为501ms，并让NO_VOICE最短时长引用同一常量；现有整数毫秒与“低于最小值拒绝”的合同不变，500ms仍不自动预填，501ms起可通过时长条件。
- 只改变候选分档／自动预填的时长条件；400ms编码最低时长、label_doubtful的1秒判断条件、独立参考数量、歌曲／重叠排除、gap描述性规则、CAM++及矩阵检测均不变。历史artifact继续使用其冻结参数，不重写v25结果。
- 修改既有从默认参数到真实Refine Canvas预填的参数化回归，覆盖499/500/501/900/1000/1200ms以及短句参考不足／同cue参考。相关三组测试共311 passed（9.73s）。代码只在独立开发副本修改，运行中的v25副本仍为1秒规则；本轮未重启、未重解析、未修改数据库，重复台词问题另列未修。
- 固定v25真实900ms立希句（top1椎名立希0.671305、32条独立参考）离线重放：原1000ms为NO_VOICE且Canvas姓名null；只换两个时长参数为501后，实际 `ParseContextCanvas.from_refine` 已预填椎名立希／voice_default。未重编码、未调用Refine，不能据此宣布新全集终态正确。
- 变更与离线核对见[时长策略记录](../../outputs/verification/ep04-duration-policy-20261022/report.md)。

#### 2026-10-22：单句画外发声提示与 v25 全集验收，两句固定目标均正确

- **本轮仅一处生产修改：** 用户提出全局memory可能有助于身份判断，但明确只修改一处prompt并重跑。本轮替换Refine一条声纹身份规则：考虑“未见其人先闻其声”与画外发声，结合精确有效音频区间的程序声音证据和可靠上下文；单帧张口、可见人物、被提到的名字或未出现在采样帧中，均不能单独证明该句身份。保留ACCEPT并非身份认证、相似度并非概率的说明；没有新增memory、权限限制、算法或阈值。
- **真实启动与终态：** 先用项目原生ES脚本恢复依赖，再由独立副本原样 `start.sh start` 启动MySQL／标准API／Web。EP04 v25 `dbf599bf-3d5c-4fce-abaa-3f2202f10684` 于北京时间10:28:54–11:06:05完成，37分11秒；Under 24/24、Refine 22/22、索引24/24。API9621 PID28802及依赖保持健康运行。相对v24的345运行Python文件只变prompts.py一处（+1/-1），既有framework 22 passed；ParseConfig和有效模型配置未变，Under／Refine仍gpt-6-luna/high。
- **两句已知真值本轮2/2：** 爽世目标47.1–48.3秒：首次Under高松灯→B0爽世0.750163／ACCEPT→持久化Refine窗口0选择voice_top1→最终长崎爽世。爱音目标293.7–296.1秒：Under爽世→B0爱音0.830967／ACCEPT→窗口2、3、4均voice_top1→最终千早爱音。爽世句理由明确“无对该句的具体身份反证”，最终描述也说明画面可见灯不足以归人；不过description仍混入程序判定过程。爽世ASR字形为「咲希ちゃんも、悪くない。」：严格alias待复核另列，本轮按同视频SHA／时间／语义对应用户已确认台词，未改原真值，也不认证整个cue单人纯净或边界准确。
- **既有工具链本轮正例：** 原ASR35.5–48.6秒为13.1秒长cue；宿主检测候选45–48秒，真实局部ASR回执查询35–50秒。Under保留35.5–46.5连续爱音一整条，单列47.1–48.3目标句与48.7–50回应；目标名字由后续B0／Refine纠正。这个上游结果不能归因于本次下游Refine提示，回执也不提供完整调用／写入顺序。
- **全片仍有质量问题：** 终态368条DIALOGUE，286非空、82 null（22.28%；v24为58/359）。首次Under356条无同成员／范围／文本重复，终态比首次Under多12条，形成12组各两条的重复，每组至少一条null；Refine窗口18持久化提交产物已有这些重复，暂不能判断Agent写入还是Canvas组装造成。#12首句1201.7–1202.6秒重复且均null，B0立希top1但900ms不足1秒ACCEPT，不能宣称本轮#12已正确形成完整立希→爱音。原始条目数及null比例受重复影响，不等于准确率。
- **归因稳定性仅作描述：** 247条B0 ACCEPT中243终态仍top1、2为null、2因同范围同文本重复而无法唯一关联（1193.0–1194.2、1211.8–1214.3秒各一具名、一null）。可唯一关联的245条没有改成其他具名人物。392条持久化必审收据对应142条B0行，其中137条多次写入、2条后窗标签变化（587.2–588.3灯→null；613.3–614.9null→灯）。这不是全片错误率，也不覆盖全部模型尝试。
- **证据与边界：** 24份首次Under均在任何Refine落库前保存；没有本轮实际post-prefill Canvas输入，不能把B0 ACCEPT加规则推导写成已观察到预填。345源码、3控制脚本与配置全程不变，其他18任务／13视频未变。两轮重建ASR／Under／参考池，1/2→2/2符合提示意图，但不是冻结输入单因素因果证明，也不能决定是否需要全局memory。未提交／推送／合并PR，未执行迁移，未触碰主MyGO系列；主工作区其他生产改动未纳入。
- 证据：[本轮报告](../../outputs/verification/ep04-offscreen-rerun-20261022/report.md)、[单句patch](../../outputs/verification/ep04-offscreen-rerun-20261022/prompt.patch)、[首次Understanding](../../outputs/verification/ep04-offscreen-rerun-20261022/understanding-audit.md)、[终态归因](../../outputs/verification/ep04-offscreen-rerun-20261022/attribution-audit.json)、[执行边界](../../outputs/verification/ep04-offscreen-rerun-20261022/execution-integrity.json)、[懒启动日志](../../outputs/verification/ep04-offscreen-rerun-20261022/lazy-start.log)。

#### 2026-10-21：v24 全集完成，同人合并前检查与最终归因仍未闭环

- **交付与终态：** 连续话轮提示已部署，标准API重跑EP04 v24 `40276e86-e076-4547-9b39-6cf71c5f2bc4`，Understanding 24/24、Refine 22/22、索引24/24 COMPLETE，38分13秒。API9621为PID32185，继续运行；未提交／推送／合并PR，未迁移。最终提示要求同人连续发言不按标点／语气碎切，合并前检查完整拟合并范围，候选需完整上下文复核；保留Canvas编辑权限，没有新增程序门禁。
- **来源已核验：** v23保留为新旧consumer混源诊断，不用于纯新版验收。v24启动前确认旧API、启动器及已记录子进程退出；24份实际Under输入全部含D23最终5段完整指令，输入／输出→提交→持久化链齐，19份租约样本仅32185。终态345运行源码／3冻结观察脚本及配置未变，其他18条任务、13个视频未变；phone API未动。这些来源检查不等于人物准确性验收。
- **同人合并仍未达标：** 35–49秒原ASR已是35.4–37.8、38.6–44.6、45.3–47.0、47.4–48.8四条，Under全部原范围保留并标爱音，没有显式检测／重ASR。前3条连续爱音未合并；爽世目标短cue由原ASR产生，不能算Under成功拆分。全片两处措辞合并（288.7–292.7、1143.3–1150.8）都没有完整合并范围检测；另有381.7–385.1仅扩时覆盖另一cue但未包含其全部措辞，单列。三者均不能认证检查通过，逐cue预扫不能替代合并后检查。
- **固定两句仍1/2：** 47.4–48.8秒爽世句：Under爱音→B0爽世0.736442／ACCEPT→真实输入已预填爽世→Refine窗口0以keep／visible_speaking改回爱音并成功提交。理由称48.1秒爱音张口与字幕相符，这是Agent声称的视觉证据，没有独立口型对齐验证。293.7–295.7秒爱音句：Under null→B0爱音0.795080／ACCEPT→实际预填与终态均爱音。前者ASR字形为「さきちゃんも、悪くない。」导致严格alias待复核，另已按同视频SHA、时间与语义人工对应；不能用字形漂移掩盖已知错误，也不认证整个cue绝对单人。两句最终description还混入候选判定过程。
- **工具正例与限制：** 373/373原ASR有效唯一区间有宿主回执，显式analyze 4次、transcribe 12次。#11同人语气变化候选经整段重ASR后仍保留一条爱音，没有机械按返回4行拆开；超出用户已确认范围的两侧端点未认证。#12终态立希→爱音→爱音，但原ASR已分3条、Under未显式复核，不能称Under主动发现换人，也未合并后2条爱音。
- **覆盖不等于正确：** 最终359条DIALOGUE，301非空、58 null；249 ACCEPT终态246仍top1、2 null、1改其他具名人物，最后1条正是爽世错误。31次Refine观测22次成功提交、9次并发冲突后重试；399条必审记录精确关联137条cue，其中134条多写、3条后窗变标签，共4次转换。这是必审子集；爽世仅1个窗口，预填被改错不计入后窗变化。没有全片人工真值，不能宣布整体错误率或稳定性改善。
- **改动与测试：** 本次新增仅prompt与对应framework断言（2文件+13/-3），另更新B12与TRD；此前EP04工具／预填整合单列。最终framework 22 passed；首次扩展1645 passed／9 failed，9项已在原样322db8e复现同样默认配置预期差异，未弱化断言。检测规则、CAM++、1s／400ms、无VAD及B0参数不变。B0编码前准入、Refine归因只读仍未实现；当前程序确实允许覆盖正确预填。
- 证据：[v24完整验收](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/report.md)、[工具审计](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/tool-workflow-audit.md)、[实际归因链](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/attribution-summary.md)、[重点范围清单](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/focus-range-audit.md)、[最终提示patch](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/continuous-turn-final.patch)、[执行边界](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v24/execution-integrity.json)。完整重跑会重新生成ASR、Under和参考池，即使模型配置相同也不是冻结输入单因素对照。

#### 2026-10-21：v23 确认新旧进程共同执行，不能用于新版提示的全片验收

- 针对v22已有局部两匿名组却保留长cue的真实反例，补充先建立话轮再命名、原已混cue同样复核、局部转写截断时扩大到完整上下文、图片不能否定画外发声；换人有依据但名字未知时拆开且保留null。未要求机械按ASR分组切碎，不增加Canvas硬门禁。
- 独立部署副本 `outputs/worktrees/ep04-continuous-turn-20261021-v23` 相对v22的345个运行源码文件仅 `agent/video_parse_v2/prompts.py` 不同；模型配置逐字节相同，检测／B0参数未动，既有framework回归22 passed。v22源码和证据保留不动。
- v22终态后启动API9621 PID7107，标准完整重解析创建v23 `d3d08d2a-025a-43e7-8891-82421af693eb`。随后确认旧70280收到SIGTERM后仍在等待连接退出，端口释放但consumer继续领取v23；24个Understanding中11个由新7107执行、13个由旧70280执行，整片B0与Refine也不能统一归因新版提示。此前以端口释放判定重启完成不充分，本轮不作为纯新版提示的全片验收。
- 已核对旧进程命令／启动时间后发送SIGINT，18:00确认70277／70280均退出，未向这两个进程发送SIGKILL。v23已按既有流程自然完成，保留全部证据；其#0／#4有完整新提示输入至提交链，可单独审计，但不能代替全片结果。后续v24沿用同一D23源码，不再修改提示／算法，并在新任务前确认旧进程实际退出。
- 执行来源证据：[混源审计](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/mixed-consumer-provenance-audit.md)、[旧进程退出确认](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/old-consumer-exit-confirmed.json)。
- 证据：[提示patch](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/prompt-conflict-followup.patch)、[审查](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/prompt-review.md)、[启动](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/api-startup.json)、[任务回执](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v23/submission.json)。完整重跑仍会重新生成ASR与参考池，不能仅因代码只改Prompt就宣称是固定输入的单因素试验。

#### 2026-10-21：EP04 v22 全流程完成，连续话轮提示仍未保证拆干净

- v21 因连接故障失败后，使用标准完整重解析创建 v22 `4f47e2eb-2e94-4216-886e-b91bf0fa99c9`，不复用失败轮只有 11 条的 B0。运行目录为 `outputs/verification/ep04-continuous-turn-rerun-20261021-v22`；本地 API 9621 PID `70280`，同一部署源码，模型配置与 v21 字节一致，未再次调算法或提示。
- **终态：** `COMPLETE`，Understanding 24/24、Refine 22/22、索引24/24，约40分44秒。367/367有效原ASR区间有宿主分析回执，显式检测28次（8次缓存）、局部ASR15次，观察器无错误。5处跨完整原cue合并均检查同范围，但1处仍有候选、4处缺逐接合处正常扫描证据，不能把调用成功计为单人通过。
- **目标仍未通过：** 35.4–48.7秒原混cue虽经45.7–47.9局部ASR给出两个匿名组，仍整段写爱音；局部查询还裁掉目标句尾。整段B0 top1爱音0.916416不能代表爽世短句。293.6–295.5秒目标从Understanding睦经B0爱音0.797181预填，最终姓名及描述均为爱音。按已确认两句人工匹配仍1/2正确；另有295.7–296.8「と。」条目是否重复待复核。
- **稳定性不等于准确率：** 最终355条DIALOGUE、60条null（16.90%，v20为91/378=24.07%）。237条ACCEPT终态235保留top1、2变null、0改其它具名人；持久化必审记录的130条多窗复审台词中6条后改名（5具名→null、1null→具名）。不能据此宣称全片错误率下降。31次Refine尝试22提交、9次版本冲突后刷新，未混算为成功决定。
- 终态345源码／3脚本SHA与启动相同，模型配置一致，预检中的其它18条任务和13个视频未变。旧v21失败证据保留。依据v22具体失败另备v23提示词：先轮次后命名、原混cue同样复核、局部ASR完整上下文、图片不能否定画外声音；只改一个生产文件，既有framework测试22 passed，后续部署复验状态见上节。
- 证据：[v22验收报告](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v22/report.md)、[工具审计](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v22/tool-workflow-audit.md)、[提示遵循审计](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v22/prompt-adherence-audit.md)、[执行范围](../../outputs/verification/ep04-continuous-turn-rerun-20261021-v22/execution-integrity.json)。

#### 2026-10-21：连续话轮提示已部署，v21 被 ASR 连接故障阻断

- **本轮新增提示：** 同一人物连续发言（含普通停顿）保留为一个 DIALOGUE，不按标点、语义小句或语气变化拆碎。跨 ASR cue 合并前调用 `analyze_speaker_consistency` 检查整个拟合并范围并读完整证据；出现 `block_change`／`low_support` 时用 `transcribe_segment` 和实际上下文复核，保留不受可疑范围影响的同人连续部分。候选范围、峰值及 `scan_count` 不是准确切点或要求拆分的条数。`completed`／无候选／少窗／支持不足均不认证单人；没有新增 Canvas 写入或完成硬门禁。
- **运行版本：** 部署副本 `outputs/worktrees/ep04-continuous-turn-20261021` 以本地已有部署 `322db8e` 为基础，移入先前已授权的工具、1 秒预填、取消 gap 门槛和 Refine 决策检查，保持当前 DTO／SQL 分层。主工作区其他未部署的 metadata／query hook 等改动没有纳入；未迁移数据库。当前模型配置仅投影排除该部署未使用的 `query_hook`，模型参数保持当前服务值，Understanding 为 `gpt-6-luna/high`。与 v20 相比不是仅 prompt 改变的严格对照。
- **验证与启动：** 扩展回归 **1645 passed、9 failed**；9 项均为默认模型配置预期差异，在原样 `322db8e` 冻结源码复现同一失败列表（98 passed、9 failed），未修改它们以掩盖失败。补齐既有 medium 配置的 Codex 校验后，另 **73 passed**。已确认无活动解析任务后，优雅重启本地 API 9621；新 PID `60613`，源码与配置 SHA 已冻结。另一套 phone API 9622 保留不动。
- **v21 终态：** 通过标准 `/api/uploads/reparse` 提交固定隔离 EP04，task `7c34f471-b7b0-47b7-9c76-31c80cece0dd` 最终 `PARSING_FAILED`：仅 1/24 Chunk 完成 Understanding，23 个 ASR 连接故障耗尽重试；主要错误为域名解析失败，另有请求超时。B0 仅有 11 条评分／样本，22 个 Refine 窗口均未完成，不能据此验收全片分段或人物效果。当前独立 DNS／TLS／无凭据 HTTPS 检查可达，不代表历史连接故障已确知根因或永久解决。将保留失败证据并以新版本完整重跑，避免复用该不完整 B0。
- **执行与观察：** 使用服务本身的 Consumer，没有另起 Worker／调度框架。观察记录区分真实模型输入、显式工具调用／缓存复用、Understanding 成功提交、B0 原始 artifact、Refine 生成结果与提交结果。唯一已完成 Chunk 的成功提交快照已写入；随后附加观察事件因重复 `observed_at_ms` 关键字出现 TypeError，该非致命观察错误未改变业务结果，需在下一轮观察脚本修正。未将流程或局部结果当成人物正确认证。
- 证据入口：[本轮最小提示词 patch](../../outputs/verification/ep04-continuous-turn-rerun-20261021/continuous-turn.patch)、[启动来源](../../outputs/verification/ep04-continuous-turn-rerun-20261021/api-startup.json)、[提交回执](../../outputs/verification/ep04-continuous-turn-rerun-20261021/submission.json)、[测试记录](../../outputs/verification/ep04-continuous-turn-rerun-20261021/tests-final.log)。未提交／推送／合并 PR。

#### 2026-10-21：gap 改为展示证据，固定 v20 输入验证两句均正确预填

- **按用户要求取消 gap 采纳门槛：** 新生成 B0 artifact 的 `accept_gap`／`conflict_gap` 均为 `null`。不再因 top1–top2 差值小、为零或单候选无 gap 阻止 ACCEPT；保留至少 1000ms、3 个独立 cue、非歌曲／重叠及有效候选条件。候选 cosine、gap、引用数继续展示，均不解释为校准置信度或概率；ACCEPT 只代表预填资格。未更改 CAM++、评分、两轮参考清理或 Understanding 无 VAD 矩阵规则。
- **Refine 同步：** 移除 0.05 的采纳／弃权提示与程序判断。必审事件有候选时，置 null 必须明确提交 contradiction 及具体理由；不能仅因 gap 小恢复旧名或弃权。`keep` 只用于具名的 Understanding 姓名；原名为空必须使用 `null`，避免绕过上述检查。仍保留具体证据支持的改名／弃权权限，尚未实现 Refine 归因只读。
- **固定输入实测：** 冻结 v20 task `e442375b-06c0-486b-9afc-0108a996b46a` 的 378 条评分、371 个样本、24 份首次 Understanding。调用当前真实 Runtime 分类与 Refine Canvas，旧参数重放 0 差异；仅把两项 gap 参数改为 null 后，ACCEPT／实际预填均 **127→248**，新增 **121/121** 条实际预填。47.5–48.8s 从爱音预填为爽世（cosine 0.773058，gap 0.056029）；293.3–295.8s 从睦预填为爱音（0.822999，gap 0.134204）。所有评分、引用、排除与源字段保持不变。
- **兼容与验收边界：** 历史 artifact 的数值参数仍按旧规则重放，原存储 band 不重写；评分方法仍为 v4，策略由完整冻结参数区分。gap 数值完整性检查保留。此次零模型调用、零数据库操作、无音频重编码、未重跑全集；初始两句正确预填不等于最终 Refine 两句已正确，也不认证新增 121 条人物名或单人纯度。改动留在隔离 worktree，未部署／重启 API、迁移、提交或推送。
- **回归与审查：** engine/parsev2、agent/video_parse_v2、server voiceprint／repository 四组最终 **894 passed、0 failed**，1 条第三方弃用警告；改后审查复核历史兼容、实际预填与弃权旁路。初次测试样例／旧提示断言失败及修正记录完整保留。
- 证据：[固定输入实测](../../outputs/verification/ep04-gap-advisory-20261021/replay.md)、[逐行结果与来源校验](../../outputs/verification/ep04-gap-advisory-20261021/replay.json)、[改动与验证记录](../../outputs/verification/ep04-gap-advisory-20261021/change-report.md)。以下 v19／v20 历史记录保留当时策略与终态。

#### 2026-10-21：预填最短时长改为 1 秒，v20 完成，两句仍为 1/2

- **本轮改动与验证：** `VOICEPRINT_B0_ACCEPT_DURATION_MS` 1500→1000；gap 0.15、独立 cue 数、NO_VOICE 1000ms、B0 编码窗 1500ms、CAM++、prompt 与 Refine 权限不变。三组回归 **640 passed**。371 个运行源码仅该常量一行变化；实际 B0 artifact 参数已确认 1000ms，模型配置与 v19 相同。代码留在隔离 worktree，未部署 API。
- **固定输入对照：** v19 的 338 条原评分／引用／Under 输入不变，真实 Canvas 预填的 ACCEPT 118→155（37 条新增）；旧 47.4–48.6s 句从爱音预填为爽世。新全集另行重新 ASR 与建池，不能混成同一输入的因果对照。
- **v20 实际终态：** task `e442375b-06c0-486b-9afc-0108a996b46a`，24/24 Chunk、22/22 Refine COMPLETE，50 分 24 秒。127 条 ACCEPT 均实际预填并最终保留 top1，其中 33 条短于旧 1.5 秒门槛；40 组真实 Refine 输入／输出齐全，22 窗口均唯一关联到正式提交。最终保留 top1 不认证人物真值，现有改名权限仍未取消。
- **两句仍 1/2：** 爽世句本轮为 47.5–48.8s，top1 爽世 0.773058、第二睦 0.717029、gap 0.056029，仍 CONFLICT；实际输入爱音，Refine 因画面冲突置 null。爱音句 293.3–295.8s，top1 爱音 0.822999／gap 0.134204，仍 CONFLICT；三个窗口均 voice_top1，最终爱音。此次没有出现“目标句已正确预填又被覆盖”；门槛已生效，但新评分的 gap 未满足 0.15。两轮目标 PCM、参考 PCM 都改变，不能把漂移单归因于参考池。已知两句均被排除出第二轮正式参考向量，不排除第一轮对标疑集合的间接影响。
- **时间线：** #11 仍一条爱音；#12 为立希→爱音→爱音，符合粗粒度反馈。最终 378 条 DIALOGUE、91 条 null（24.1%，v19 为 15.4%）；244 条决策／22 窗口与 Chunk 历史一致，83 条多次写入 cue 中 12 条变化、13 次转换（6 name→null、5 null→name、2 name→另一人）。未见整体覆盖改善；不同版本仅 7 条严格同区间同原文，不能宣称整体稳定率或错误率改善。378 个原 ASR 唯一区间全部扫描，14 候选，12 条成功局部 ASR 回执，均不等于声学正确率。
- **外围检查未通过：** 管线本身 COMPLETE，runner 因非目标记录变化退出 1。期间 video `e709a2d8-aabb-509e-b323-e7f9aa7018b2` 新增 task `577674ad-68bc-4ac7-89ed-8a53f0bc9521` v2 并进入 PARSING；其余原 13 个非目标任务所选字段未变。触发者未知，保留原失败记录，未改动／停止该任务；不认证全部隔离检查通过。本轮未重启 API、迁移或提交推送。
- 证据：[验收报告](../../outputs/verification/ep04-prefill-1s-rerun-20261021/report.md)、[时间线 HTML](../../outputs/verification/ep04-prefill-1s-rerun-20261021/report.html)、[固定输入对照](../../outputs/verification/ep04-prefill-1s-rerun-20261021/frozen-input-comparison.md)、[非目标变化核对](../../outputs/verification/ep04-prefill-1s-rerun-20261021/unrelated-check.md)。

#### 2026-10-21：EP04 v19 全集重跑完成，两句纠正 1/2，归因仍未闭环

- **终态与范围：** 按用户要求采用当前服务配置完成 v19 `04960abe-fa62-4806-919e-df3f28ac6040`：24/24 Chunk、22/22 Refine 均 COMPLETE，约 40 分 36 秒。5 个 Refine 窗口因 `max_model_calls` 各重试 1 次后成功，Chunk 无阶段重试。只对固定 EP04 视频执行，终态核对非目标视频任务未变化；未重启 API、迁移、合并 PR、commit/push。
- **两句真值：** v18 最终 0/2 正确，本轮 1/2。爽世句已独立为 47.4–48.6s，Under 错标爱音，B0 top1 爽世 0.773138／gap 0.188161，Refine 却以“48 秒画面显示爱音在说该字幕”的 `visible_speaking` 理由 `keep` 爱音，最终仍错。爱音句保持 293.2–295.6s，Under 错标睦，B0 top1 爱音 0.785129／gap 0.103957，三个覆盖窗口均采用 `voice_top1`，最终纠正为爱音。两条均 CONFLICT，不会走仅 ACCEPT 的自动预填；爽世是 Refine 保留错名，不能写成“覆盖了已预填正确姓名”。
- **对照与分段边界：** #11 最终保留 738.7–744.8s 一条爱音；#12 为 1202.0–1203.0s 立希、1203.2–1208.7s 爱音，符合人工反馈。但 #12 本轮原 ASR 已拆，四处重点均未局部重 ASR，不能把正确顺序归因于工具主动拆分，也不认证完整单人/重叠准确率。
- **全片与稳定性：** 最终 DIALOGUE 从 263 增至 338；未归属从 141/263（53.6%）降到 52/338（15.4%），只是标签覆盖。171 条决策收据覆盖 22/22 窗口、60 条 cue，与 Chunk 历史多重集一致；57 条多次写入 cue 中 9 条发生标签变化（6 null→具名、2 具名→另一人物、1 具名→null）。v18 只有 4/22 窗口 17 条收据，不能直接比较改名比例来宣布稳定率提升。无全片真值，不计算全片错误率。Under→Final 338 条区间/文字一对一，48 条姓名改变，不等于 48 条纠错。
- **实际工具使用：** 343 个有效原 ASR 唯一区间均自动分析，1107 个窗口引用，18 个候选均 low_support；9 条成功局部 ASR 证据。24 次 Under 均提供两个工具；显式分析工具调用次数缺原生执行日志，保持未知。继续无 VAD 的既有规则，CAM++ 完整 encoder revision、B0 方法/参数与 v18 一致。
- **配置与代码来源：** 本轮先 fetch 确认 PR 远端仍 `215aadf`；使用已测试工作副本的冻结源码（371 文件核验一致）。现有数据库需 visibility 映射，仅运行快照采用主线 `c00a0ae` 已提交的 `server/model/video.py`，没有迁移或引入其他主线声纹逻辑。当前配置为 gpt-5.6-luna/high、Refine 继承，现有模型绑定、并发 4/4/4、重试 2 次，显式 prefill；投影仅排除解析不消费的 Chat query_hook。v18 是 gpt-6-luna/low，ASR/参考池重新生成，非单变量实验。复用现有服务实现定点运行，API 未部署本轮代码。
- **剩余问题：** 当前复核工具已真实接入，但 B0 编码前准入、Prepare 对错名的可采纳归因，以及 Refine 归因只读/受限合并仍未闭环。下一步不能仅锁住 Under 的错名；本轮不改阈值、encoder 或临时调整流程。
- 证据：[简洁 HTML 与时间线](../../outputs/verification/ep04-full-rerun-20261021/report.html)、[验收结论](../../outputs/verification/ep04-full-rerun-20261021/report.md)、[最终评价](../../outputs/verification/ep04-full-rerun-20261021/rerun-evaluation.json)、[逐阶段归因](../../outputs/verification/ep04-full-rerun-20261021/attribution-audit.json)。outputs 被 Git 忽略，证据仍需从主工作区只读引用。

#### 2026-10-21：Understanding 声学复核工具已接入，沿用无 VAD 矩阵规则

- **实现范围：** 在基于远端 `215aadf` 的隔离 worktree `outputs/worktrees/ep04-segmentation-20261020` 实现。本轮已重新 fetch 并核对远端；代码尚未提交、推送或部署；接入交付时未重启、迁移或重解析，随后固定 EP04 的 v19 验收见上条，未触碰主 MyGO 系列。主工作区只同步本条 B12 与原目录 TRD，不覆盖其他工作线代码。
- **工具统一入口：** `agent/video_parse_v2/tools.py` 集中 Understanding／Refine／Prefix／Voiceprint 的工具声明和组装，沿用共享 Canvas Tool 协议；移除原 `prefix/tools.py` 重复包装，`voiceprint.py` 保留 Agent 编排。原 Refine 的 compare、看帧和完成检查、Prefix 零工具路径和计数保留；未新增注册表框架。
- **接入行为：** `analyze_speaker_consistency(start_ms,end_ms)` 返回复核范围、支持统计、未覆盖范围和只读证据；宿主在 Understanding 调用前扫描合法原 ASR cue，任务有冻结 speaker encoder 才启用。`transcribe_segment(start_ms,end_ms)` 使用任务固定 ASR 模型／输入模式，对 Chunk 内指定范围重新转写，返回新的只读 ASR，不覆盖原 ASR、不自动改事件。ASR mode=none 时不提供该工具；没有模型绑定不回退到全局默认配置。
- **算法保持：** CAM++ 192 维、1 秒完整窗口、400ms 步长；左右至多 6 窗、至少 3 个有效窗对、反差 0.10／短侧 0.15。运行与离线回放共用 `server/constants/parse.py`，纯算法未改；不启用 VAD、不聚类、不预设人数。自身／重叠／重复 PCM 的统计排除保留。完全恒定 PCM 为现有 encoder 不支持的输入，整段显式返回 `analysis_status=unavailable`、不可编码窗和未评分范围；不删窗、不造向量、不认证为单人。其他模型／媒体故障仍走失败路径。
- **职责与证据：** 按用户最新要求撤销 Understanding 完整 cue 区间锁，允许在 Chunk 内拆分和校正时间；不以候选、局部重 ASR 或精确 cue 匹配作为写权限／完成门禁。原 ASR 仍只读，Chunk 越界校验和批次原子性保留，Prefix 时间保护不变。提交沿现有 `detail.evidence` 保存原观察、`tool_evidence` 与必要的 `dialogue_provenance`；exact/subrange/overlap 仅是时间对应，明确 `content_verified=false`，不是内容或人物认证。
- **调用与回收：** 分析直接解码 `task_input.original_source_uri`，避免 `source_media_ref` 指向 proxy 的混淆；每次 Understanding 共用原 PCM、一个延迟加载的 encoder 及按 PCM 摘要缓存的向量。工具临时文件归当前 Chunk lease，取消时仍等待实际阻塞调用退出后再释放。回执每次返回最新 Canvas revision，缓存不冻结旧 revision；局部 ASR 的相对时间只加一次绝对偏移。
- **代码验证：** 十组共 **1319 passed、0 failed、1 条第三方弃用警告，33.54 秒**：engine/parsev2 280、agent/video_parse_v2 366、Runtime 新测试 9、media 56、voiceprint 115、worker 220、queue 131、repository 125、artifact 12、model_calls 5。覆盖音频／视频 ASR、原片选择、缓存、恒定窗不足、取消期间资源保留、只读证据和拆分权限；媒体测试含真实 FFmpeg PCM 逐字节核对。独立审查发现的原片来源、回执版本及恒定窗错误路径均已修复并回归。
- **真实样本回放：** 新工具运行路径已对原来的 12 条／231 窗完成真实原 MP4 裁取与 CAM++ 推理：完整 PCM 摘要、所有窗口向量与冻结基线相同，最大逐分量差 0；旧算法字段逐项一致，24 次重复工具调用未新增编码。第 11 条全程爱音的误报仍保留，第 12 条立希→爱音仍正确提示；按现有人工分组为混人范围 6/7、其余对照候选 1/5。总墙钟 13.828 秒含媒体处理；范围有历史重叠版本、四条普通对照缺逐段精标，不能当泛化准确率。此次使用离线 lease／task 绑定和已有范围 cue，未调用生成式 ASR／Understanding、数据库或重解析。见 [真实路径离线验收](../../outputs/verification/ep04-understanding-tools-20261021/report.md)。
- **仍未验收／后续：** 上述离线交付时尚未运行生成式 Understanding 全集验收；随后 v19 实测已完成，结果见上条，人物归因闭环仍未通过。模型 pipeline COMPLETE 不证明人物归因正确。B0 收集／编码前的准入、Refine 归因只读和受限跨 Chunk 合并属于后续工作，本轮没有实施，也没有改 B0 算法／阈值／encoder。TRD 保留原历史正文并同步 main，设计目录仍被 Git 忽略，不随 PR 自动推送。

#### 2026-10-19：排查链路、音频区间漂移与当前结论

**固定验收对象与统计边界**

- 仅针对隔离 EP04 系列 `bdbf28ae-5404-58e4-8c98-699433499ffc`、视频 `0019c16c-a7b7-5f6c-8275-5019869c1207`。用户确认约 `47.4–48.7s` 的「祥ちゃんも悪くない」是**长崎爽世**，约 `293.5–296.0s` 的「長崎そよちゃんと…」是**千早爱音**。真值指目标台词，不代表包含它的十几秒长片段全由此人发声。
- 下表时间均为原视频绝对秒；新证据字段使用整数 `start_ms/end_ms`。历史 `start_seconds/end_seconds` 在当前 V2 实际存毫秒，不能再次乘除 1000。top1/gap 是声学排名及 cosine 分差，不是正确概率。
- 版本号指实际解析任务版本，不把离线算法、Refine replay 或裁剪变体另算生产版本。失败任务只能说明已保存部分；任务 COMPLETE 也不能替代人物正确性验收。尚无全片人工真值，不能认定“除了两句都对”。

**排查如何转向，哪些结论不能混在一起**

1. 最早围绕声源质量排查：参考人物错标、稀有人物参考不足、单质心与多参考、聚类方案，之后选择 CAM++／B0 trimmed centroid。旧 v11 声库确实把爱音目标句收进祥子参考、爽世目标句收进灯参考；这些不是假问题，但优化参考并没有校验目标音频是否包含多个人。
2. 后来修 Refine 的证据供给与决策：预先评分、flag/prefill、强制逐冲突记录、完成门禁、label_doubtful、tail 检验、top1 lead 提示与重叠窗口反馈。这解决“模型可能完全不调声音工具”和“声音正确但未采用”的部分流程缺口，却仍默认一个 DIALOGUE 可以用一个 speaker 表达。把候选排名改对、把结果填入 Canvas、最终提交正确，是三个不同验收点。
3. 用户听出约 `45–48s` 和 `292–297s` 可能有尾音、短促「啊？」、交替说话，提出裁掉末尾对照。实际做了固定尾裁实验，但选用的是 v12 原始短区间；这两条短区间的 top1 本来已正确。此前用这个结果回答后续版本的长区间问题，属于**输入基线未对齐**，不能据此说之前的错误不存在或已修复。
4. 继续回溯 v11 起每轮的 ASR、首次 Understanding、声纹 artifact 和最终事件，才发现目标所指已从约 1～2 秒短句扩大为 3.6、7.2、11.4、13.3 秒。评分对象变了，参考池与 Understanding 标签也随每轮变化；相同 B0 算法并不意味着实际比较了同一音频、同一声库。
5. 当前有直接证据的主输入缺陷是：**ASR 有时直接跨说话人合并；Understanding 又会把本来分开的 ASR cues 合成一个长 DIALOGUE；B0 对长事件整体编码，Refine 往往只改这一整段的单一姓名或 null。** 因而排查重点应先保护音频分人边界，再评估人物匹配。固定裁尾没有稳定改善，不等于已证明短语气词、重叠或毫秒级边界不重要。

**v11～v18 实际任务索引**

| 版本 | task_id | 本轮状态／证据适用范围 |
| --- | --- | --- |
| v11 | `0f681291-4f52-4254-a3be-91c1ee835c3c` | COMPLETE；旧 VoiceprintAgent 参考池，以下声学排名为后续离线复核 |
| v12 | `cc57cf42-4c69-427d-b6a0-e9523f22bc8e` | COMPLETE；B0 初次生产接入的比较基线 |
| v13 | `9dd66201-3b6a-45b8-a2f6-dcd0d5513f74` | PARSING_FAILED；24 个 Chunk ASR 均因 `403 AccountOverdueError` 失败，无 Understanding／Refine 可评 |
| v14 | `58288805-6e51-4f4d-9fee-21e38104114e` | PARSING_FAILED；已有局部评分／Refine，不作为全片成功样本 |
| v15 | `080731f4-ac5c-49ed-9caa-3921212f0ceb` | PARSING_FAILED；同上 |
| v16 | `54371266-091e-418d-af3f-8740f85e53f4` | PARSING_FAILED；爽世所在 Chunk 未贡献准备评分 |
| v17 | `9bb8aa31-8217-4c67-9b86-19bc8782348a` | PARSING_FAILED；已有局部评分／Refine |
| v18 | `b8d48ae8-e352-48e4-b9b5-a7444c4ca036` | 此前终态数据库核对为 COMPLETE；准备 artifact 可复核。目录中的 `latest.json` 是较早 PARSING 快照，不能单独证明终态 |

**爽世目标句：原始 ASR → 实际评分区间 → 人物结果**

| 版本 | 原始 ASR 区间（秒） | Understanding／评分区间（秒） | top1 / gap | 已保存人物或限制 |
| --- | --- | --- | --- | --- |
| v11 | 47.5–48.8 | 47.5–48.8（离线复核） | 祥子 / 0.121401 | 最终灯；旧池复核，不能冒充生产 B0 调用记录 |
| v12 | 47.4–48.7 | 47.4–48.7 | 爽世 / 0.062722，CONFLICT | Understanding 爱音 → 最终爽世，`refine_edit` |
| v13 | 无有效结果 | 无 | 无 | ASR 失败，不造分数 |
| v14 | 47.3–48.8 | 47.3–48.8 | 爽世 / 0.104821，CONFLICT | Understanding 爱音 → Refine null；理由 `label doubtful, speaker unresolved` |
| v15 | **35.4–48.7** | **35.4–48.7，13.3s** | 爱音 / 0.198523，ACCEPT | Understanding 灯 → 爱音，`voice_default`；ASR 已跨前文合并 |
| v16 | 留存 ASR 38.7–48.7 | **无该句的准备评分** | 无 | 对应 Chunk 失败；后来留存长事件不能倒推为本轮声纹输入 |
| v17 | **45.2–48.8** | **45.2–48.8，3.6s** | 爱音 / 0.132242，band=null | Understanding／最终爱音；长段包含前一句与爽世目标句 |
| v18 | **35.3–48.5** | **35.3–48.5，13.2s** | 爱音 / 0.091359，band=null | Understanding／最终爱音；未拆出爽世目标短句 |

**爱音目标句：原始 ASR → 实际评分区间 → 人物结果**

| 版本 | 目标 ASR cue（秒） | Understanding／评分区间（秒） | top1 / gap | 已保存人物或限制 |
| --- | --- | --- | --- | --- |
| v11 | 293.5–296.0 | 293.5–296.0（离线复核） | 爱音 / 0.151062 | 最终祥子；声学复核正确不代表 Refine 当时采用 |
| v12 | 293.5–296.0 | 293.5–296.0 | 爱音 / 0.063694，CONFLICT | Understanding／最终祥子；top1 正确，决策未纠正 |
| v13 | 无有效结果 | 无 | 无 | ASR 失败 |
| v14 | 293.4–296.0 | **288.8–296.0，7.2s** | 爱音 / 0.135117，CONFLICT | Understanding 睦 → 爱音；姓名纠正不能证明长区间单人 |
| v15 | 293.5–295.3 | **291.3–299.0，7.7s** | 爱音 / 0.037342，NO_VOICE | Understanding／最终 null；另有 295.4–299.0 事件，与长段重叠 |
| v16 | 293.6–296.0 | 293.6–296.0 | 爱音 / 0.033992，NO_VOICE | Understanding／最终 null；说明区间合并不是全部错误的唯一原因 |
| v17 | 293.9–295.3 | **289.6–296.8，7.2s** | 爱音 / 0.105198，CONFLICT | Understanding null → 爱音；仍把多个短应答合成一句 |
| v18 | 293.4–295.7 | **284.3–295.7，11.4s** | 爽世 / 0.010109，NO_VOICE | Understanding 睦 → 最终 null；不能当作爱音短句本身的评分 |

v11 分数来自旧声库离线质心复核；v12 起表中为各任务自己的准备结果。各轮参考池并未冻结，不能把跨轮分差变化全部归因于区间。另用同一份 v11 数据离线运行 B0 时，两句 top1 分别为爽世／爱音，gap 为 `0.081574/0.119693`；这是算法对照，不是新一轮生产解析。

依据：[v11 逐句复核](../../outputs/verification/speaker-agreement-20261018/v11-lines.jsonl)、[旧参考池快照](../../outputs/verification/speaker-algo-20261018/source-snapshot.json)、[算法对照](../../outputs/verification/speaker-algo-20261018/report.md)、[v12 实际接入验收](../../outputs/verification/speaker-b0-integration-20261019/actual-test-summary.md)、[v13 失败](../../outputs/verification/speaker-voice-decisions-20261019-v13/run-failure-report.md)、[v14 终态报告](../../outputs/verification/speaker-voice-loo-luna-20261019-v14/terminal-voice-report.md)、[v15 报告](../../outputs/verification/speaker-voice-ranking-retry-20261019-v15/ranking-voice-report.json)、[v16 报告](../../outputs/verification/speaker-voice-lead005-20261019-v16/ranking-voice-report.json)、[v17 报告](../../outputs/verification/speaker-voice-feedback-20261019-v17/ranking-voice-report.json)、[v18 首次 Understanding](../../outputs/verification/speaker-voice-feedback-c4-20261019-v18/original-known-cases.json)、[v18 声纹索引](../../outputs/verification/speaker-voice-feedback-c4-20261019-v18/manifest.json)。

**谁合并了区间：已定位到生成层，不能笼统归罪 Refine**

- **ASR 已经合并爽世侧。** v15 原始模型输出就是 `35.4–48.7s`；v17 是 `45.2–48.8s`；v18 是 `35.3–48.5s`。这些范围不是声纹程序、数据库或 Refine 后来扩大的。v17/v18 的首 Chunk 视频内容 SHA 和规范化请求 SHA 相同，ASR 返回边界仍不同，属于相同输入下实际生成结果不稳定；这不证明具体是哪种模型内部机制导致。
- 当前 ASR 使用豆包多模态视频转写 `doubao-seed-2-0-lite-260428`，Prompt 虽已要求按声音分 `speaker1/2`、换人拆条，但不是经过独立验收的专用 diarization／强制对齐模型。`engine/common/transcription.py` 的后处理只转换时间、裁范围、过滤与排序，Server 只加 Chunk 偏移；未找到 Python 主动拼接 ASR 行的逻辑。视频抽样 `fps=1` 不能解释为音频只能精确到一秒。
- **Understanding 又扩大爱音侧。** v18 ASR 目标仍是 `293.4–295.7s`，但 `before_any_refine=true` 的首次 Understanding 已保存为 `284.3–295.7s`。v17 原 ASR 分开的 `289.6–290.1`「うん」、`291.5–292.7`「長崎？」、`293.9–295.3` 目标句、`295.4–296.8`「あ…」，被 Understanding 合为 `289.6–296.8s`。这是 Refine 前就存在的跨 cue 合并；生成的 description 中的人物名也不是真值。
- **B0 忠实评分了错误粒度。** 准备阶段读取 Understanding DIALOGUE 的完整 start/end。当前直接编码上限为 30 秒，上述所有长段均落在整段单向量路径，没有内部换人切分；来源 cue IDs 只用于追溯和排除，不能把整段还原成目标短句。整段 top1 即使是某个真实参与者，也不能表示每一句都由其说出。范围调整后旧声音证据失效的机制，无法防止准备前就已合并的范围。
- **Refine 决策仍是另一层未完成项。** v12 爱音句已是正确 top1，最终却保留祥子；另有 v15 `212.8–214.8s` 短应答，较早提交的 seq3 窗口记录 `voice_top1`／爽世，reason 却写“未达置信改写门槛，故撤回改名并保留原理解”，与其决策含义矛盾；较晚提交的 seq1 窗口又记录 null，reason 为“爽世领先原标签爱音仅0.082，低于0.10阈值；短促重复肯定语音不足以可靠确认。”后者虽填了 `evidence_type=contradiction`，文字仍未指出具体反证。结构化一致性通过不等于 reason 内容有说服力。窗口能看到 scope、更新来源与已有 reason，不代表这些信息构成禁止后写的锁；现有事件／字段合并也不等于人物共识投票。必须按真实提交先后核对，不能按窗口空间顺序假定“全部上下文已达成人物共识”。此例与两个固定验收句分开记录，不拿生成标签充当人工真值；原始记录见 [v15 逐窗口决策 TSV](../../outputs/verification/speaker-voice-ranking-retry-20261019-v15/ranking-voice-window-records.tsv)。

依据：[v17/v18 原始 ASR 对照](../../outputs/verification/speaker-voice-feedback-c4-20261019-v18/upstream-v17-v18-asr-comparison.json)、[v17 首次 Understanding](../../outputs/verification/speaker-voice-feedback-20261019-v17/understanding-snapshot.json)、[v18 首次 Understanding](../../outputs/verification/speaker-voice-feedback-c4-20261019-v18/understanding-snapshot.json)、[ASR 生成及规范化](../../engine/common/transcription.py)、[转写适配](../../gateway/transcription.py)、[声纹准备／直接编码](../../server/service/video_parse.py)、[媒体切片](../../server/service/video_parse_media.py)、[声纹时长常量](../../server/constants/parse.py)。

**裁尾实验：实际支持什么，不支持什么**

- 实验固定 v12 artifact（SHA256 `d74a30b5a3fcc60475dd48cb256ff188fb3eb12e1f5066dbc9b528a0ae14edbd`）、CAM++192/L2 encoder 和每组引用集合，排除目标自身、同 cue、重叠与重复 PCM。包含两条 v12 原句、用户近似范围、v17 宽范围及六个候选同人对照，共 **12 组 × 5 个尾裁长度 = 60 个变体；57 个评分、3 个因不足 400ms 明确不可编码**。原始 PCM、向量、top3 重现及组内 kept reference IDs 不变断言通过；12 组完整音频各额外重复编码一次，每组两次向量逐分量一致。

| 对照 | 完整结果 | 裁尾结果 | 能得出的结论 |
| --- | --- | --- | --- |
| 爱音 v12，293.5–296.0s | 爱音，gap 0.063694 | 裁 1s 后仍爱音，gap 0.064896；向量 cosine 0.937040 | 固定裁尾未带来预期的大幅改善 |
| 爽世 v12，47.4–48.7s | 爽世，gap 0.062722 | 裁 0.5s 后仍爽世，gap 降至 0.027858；裁 1s 后剩 300ms，不可编码 | 裁短可削弱证据；不能盲目统一裁一秒 |
| 候选同人对照 | 六条按预定条件选择，未经人工确认 | 可裁 1s 的四条 cosine 为 0.796348–0.983656；全部 28 个可评分控制变体保持原 top1 | 向量变化本身不足以证明多人；此处不是人工准确率 |
| 同一 v12 来源池，爽世短句与宽区间 | 47.4–48.7s top1 爽世 0.767560 | 45.2–48.8s top1 爱音 0.794759，gap 0.076922 | 短／长范围排名不同；按各自完整范围排除后引用数为 314／313，**不是仅改变波形的严格单变量对照**，也不要与 v17 自己池的 gap 0.132242 混用 |

尚未人工逐毫秒标注或专门移除中间语气词，固定截掉末尾未必碰到真正干扰；缩短也改变音素、时长及参考可靠性。因此本轮结论是“不能靠固定裁尾或向量漂移判断换人”，而非“已排除短语气词影响”。相较于该未证实假设，数秒至十几秒跨 cue 合并已有直接上游证据，应优先处理。

依据：[实验报告](../../outputs/verification/speaker-tail-crop-20261019/report.md)、[协议](../../outputs/verification/speaker-tail-crop-20261019/protocol.json)、[完整 TSV](../../outputs/verification/speaker-tail-crop-20261019/results.tsv)、[对照试听页](../../outputs/verification/speaker-tail-crop-20261019/listening.html)。首次实验因 ffmpeg 输入侧 `-to` 与生产输出侧 `-t` 参数不一致触发 `AssertionError: control_soyo_1300`；已对齐提取方式、保留断言并完整复跑通过，失败日志也保留，未通过弱化检查制造重现。

**已做但未部署：撤掉 Understanding 改台词区间的权限**

- 用户已明确要求只允许 Refine 调整台词时间。隔离工作树 `/private/tmp/seekino-understanding-asr-bounds-20261019` 基于 PR #5 分支 tip `504b9b4`，有四个未提交文件：`timelineEvent_canvas.py`、`prompts.py`、`test_context_canvas.py`、`test_framework.py`。不是主工作区已部署的修复。
- 宿主检查 Understanding 新 DIALOGUE 必须对应本 Chunk 一条有效 ASR cue 的完整区间，既有 DIALOGUE 时间只读；试图合并、拆分、扩大或绕过类型修改会得到可恢复工具反馈，不能只靠 Prompt。Refine 保留时间调整能力，原声音证据随区间变化失效；无可用 ASR 的既有回退路径保留。
- 前一轮代码验证记录：新增 34 个用例，要求的八组共 **1155 passed、0 failed**（engine 218、agent 329、voiceprint 115、worker 220、queue 131、repository 125、artifact 12、model_calls 5）。本次文档维护未重跑；单测不证明真实人物归因正确。**尚未 commit/push、重启或重新解析，未验证真实音频效果。该限制只能防止 Understanding 二次合并，不能修复 ASR 本来已合并的 cue。**

**下一步工具职责与论文依据：尚未实现或实测**

需要的是对音频做 **speaker segmentation／diarization** 的独立工具：判断区间内是否单人、何时换人、何处重叠。必须同时覆盖先后换人 `A→B→A` 和同时两人发声；只做 overlap detection 会漏掉依次说话。输出应保留匿名 speaker 活动区间、切换／重叠位置、方法及不确定性，而非直接承诺“这是爽世”。整段 `compare_speaker` 回答“更像谁”，不能代替“是否混了多个人”。

| 论文／官方实现 | 与本问题直接相关的能力 | 边界 |
| --- | --- | --- |
| Bredin & Laurent，2021，[End-to-end speaker segmentation for overlap-aware resegmentation](https://arxiv.org/abs/2104.04045) | 在局部音频中联合表示语音活动、换人及重叠，可用于已有分段的重新切分 | 约 16ms 是输出步长，不是边界准确率保证；未证明 EP04 语气词可稳定分出 |
| Plaquet & Bredin，2023，[Powerset multi-class cross entropy loss for neural speaker diarization](https://arxiv.org/abs/2310.13025) | 将无声、单人、多人同时活动的组合设为类别，改进局部说话人活动预测 | **不是将两个人的声纹向量混成一个新人物模板**，也不是音轨分离或真名识别；仍需全局关联及本域验证 |
| [pyannote Community-1 官方模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1) | 可本地运行的完整匿名分人候选，支持下载后离线运行，适合作为旁路对照 | 首次需取得模型访问权限；保留 regular 输出的重叠信息，exclusive 输出不能证明本来只有一人；本轮未下载、运行或验收 |

建议先离线测实际失败的 v15/v17/v18 **完整长区间**，同时保留 v12 原始短句与人工确认的普通单人对照；人工分别标记发声边界、匿名同人关系、姓名和重叠，再看换人漏检、单人误报、短应答与边界误差。随后才在同一冻结 CAM++／B0 参考池中比较切分前后排名，继续排除目标自身／同 cue／重叠／重复 PCM，避免把参考池变化误当分段收益。不得只测两个目标或只看标签一致率后宣称全片准确。

若验证有效，保护参考池的程序检查必须发生在 B0 收集／编码之前；只给 Refine 一个可选工具，既可能不调用，也无法撤销已经混入声库的长音频。Understanding 可以读取声学证据并命名，时间调整权限仍遵守用户要求留给 Refine；确认混人的整段不能直接作为单人物参考。具体模型、阈值、分段与文本对齐方式待离线结果决定，本项尚未增加生产接线、Worker 或调度阶段。详细研究笔记见 [本地论文与实现调研](../../outputs/verification/speaker-diarization-research-20261019/report.md)。

**尚未解决与交接要求**

- ASR 匿名分组与换人边界不稳定，Understanding 限权尚未部署，专门分段模型还没在两个目标上跑过；现有整段 B0 分数不能作为混合音频里某一句的身份证明。
- 参考错名、短语音／声态变化、稀有角色与参考数量仍有误差；纠正区间后也不能预设 B0 必然正确。已有正确 top1 却被 Refine 保留错名／写 null，以及重叠窗口覆盖，需单独按动作与提交链验收。
- 后续每次报告至少携带 task/version、原 ASR cue、首次 Understanding、实际编码范围、参考 artifact checksum、top3/gap、逐窗口 decision/reason 和最终来源，不能再只拿台词文本或一个时间点跨版本比较。人物效果验收以人工听音为准，不能把“八组测试通过”或“解析 COMPLETE”说成两个经典错例已稳定修好。
- 本次只维护本文件，保留下方历史。链接中的 `outputs/verification/` 属于被 Git 忽略的本地证据，未随文档自动加入 PR；文档落盘、证据可复现、纳入提交及生产部署是四件事。本次无生产改动、API／数据库写入、重启、重解析、迁移、commit、push 或 merge。

#### 2026-10-20：用户重新明确 Understanding 负责原子台词，Refine 负责跨 Chunk 衔接

- **新的职责目标已确认，尚未实现：** 用户明确由Understanding在提交前完成Chunk内的换人检查、必要的局部重ASR与拆分，向后交付单人发声的原子台词；随后B0消费这些音频区间。就台词分段职责而言，Refine检查跨Chunk是否需要衔接、去重或合并，不再承担Chunk内部混人长cue的主要拆分工作。此决策取代此前“台词区间调整留给Refine”的边界。
- 原始ASR继续作为只读来源证据，不能把“一条原ASR cue必须对应一条完整DIALOGUE”继续作为输出约束。`7ea879b`的防扩大保护需改为有依据的cue内子段和来源／授权范围校验，保留原子批次及拒绝无依据合并扩大等保护；当前代码与prompt仍执行旧限制，不代表新职责已经生效。
- 待实施的出口约束：已发现混人且尚未处理的cue不能作为已原子DIALOGUE交给B0／Refine；原文、音频和未解决残余仍需保留，不能靠丢弃内容制造“拆干净”。人物名未知不等于混人；同时发声的PCM也不会因拆成两条文字而变成单人音频。完成信号和无检测告警均不等于声学原子性已验证。
- 干净分段解决的是整段混声向量的问题，使B0排名更适合作为人物归因依据；参考错名、近分及短音频质量仍是独立条件，暂不改变现有encoder、B0算法或阈值。跨Chunk合并应保留原子来源，不能将A→B→A跨中间B合成一个连续A区间；改变音频范围继续使旧声音证据失效。
- 本轮仅核对调用链并更新本节职责记录，未修改生产逻辑、重新ASR、重解析、部署、重启、迁移、提交或推送。

#### 2026-10-20：补全声学变化工具设计与 Prepare→Refine 归因边界

- 用户进一步明确：Refine拿到准备阶段的归因后不得随意改名或置null，就DIALOGUE而言只处理跨Chunk衔接／合并。该约束需要Canvas和提交检查实施，不能只靠prompt；当前实现仍允许后续窗口重新决策，尚未收紧。
- 原TRD为[agent/video_parse_v2/design/voiceprint-trd.md](../design/02-agent-harness/voiceprint-trd.md)，受`.gitignore`的`**/design/`规则忽略。最新职责、算法和分批实现设计已按用户要求同步回main主工作区的原目录，并与隔离worktree同名副本保持一致；历史原文完整保留。此次仅合入相关设计与本节记录，保留主工作区其他修改；该TRD仍不随PR推送，本条提供可追踪索引。
- 首批设计沿用CAM++／1秒窗／400ms步长，比较时间矩阵的左右内部与跨侧相似度，排除自身／共享PCM／重复PCM的虚假支持，不聚类、不预设人数。连续块变化和短尾差异均可成为重ASR待复核范围；短尾缺少内部非重叠配对时明确支持不足，不能再次按最小成簇数量丢掉。算法和阈值尚未验收，现有矩阵不等于自动定位已成功；单embedding无法独立确认同时发声。
- 接入目标是在Understanding模型调用前程序扫描，Agent展开候选并复用冻结ASR局部重转写，提交前处理已知混人及残余；B0在收集／编码前检查资格。原ASR只读，有新局部ASR证据时允许在授权Chunk内校正旧cue时间，不能机械锁死错误原范围。
- 当前B0是排名／分档artifact，只有`prefill+ACCEPT`预填，其余可能保留Understanding旧姓名。因此不能直接锁住当前speaker就称为高置信归因；还须明确准备阶段可采纳归因与未决的交付。现有`merge_refinement_content`是并发三方合并，不是两条台词的合并工具；正式合并还缺源事件保留／去重和证据继承合同。本次仅整理设计，未改生产逻辑、B0／encoder／阈值，未运行模型、部署、重解析、提交或推送。

#### 2026-10-20：矩阵变化算法已实现，离线粗筛有信号但尚不能判断台词纯净

- **最新人工更正：第 12 条（1201.7–1208.5s）开头立希、随后爱音，算法正确发现原记录混人；第 11 条（738.7–744.7s）全程爱音，用户指出截断与较大语气变化，属于误报。** 第 12 条移出单人对照。未加 VAD 按新分组为混人 6/7、其余对照候选 1/5；50% VAD 为 4/7、1/5，25% 为 6/7、1/5，75% 为 3/7、0/5。其余四条普通候选仍仅有一般反馈，不声称完整人工误报率。此例支持算法用于 Understanding 复核纠错；触发后仍须确认拆分。详见 TRD 第 8 节，独立保存人工标注，未改原评分／参数或重跑模型。

- 用户授权先实现并实测算法，随后明确只需实验结论、不要求精确定位。新增`engine/parsev2/speaker_change.py`、`evals/speaker/speaker_change.py`及对应测试，均在隔离worktree；两个Understanding Agent工具尚未注册，生产流程未接入。算法复用原CAM++／1s窗／400ms步长，不聚类、不预设人数。
- Foote原论文的Gaussian checkerboard相关作为对照保留；masked median与短侧支持分支是本地扩展。主设置在回放前冻结为左右最多6窗、最少3个有效窗对、完整两侧反差0.10／短侧0.15，未改变B0阈值。已排除自身、共享音频和重复PCM的虚假配对支持。
- 共16个EP04输入、244窗：复用62旧窗、新编码182窗及14次首窗重复，重复逐分量差0。另选6条6–10秒普通台词作对照，编码前冻结选择规则与artifact摘要，PCM与原artifact一致；采用用户“其他基本都对”的泛化反馈，不冒充逐段精确转人／重叠标注。
- **初始选样分组记录：6个历史混人范围中5个触发复核，原选普通对照的6条中2条触发；其中第12条现已确认混人、提示正确。** 用户圈出的两条13秒长段均捕捉到末尾差异，但v17爽世短范围未触发，v18爱音目标附近仍缺候选。历史6范围来自两个场景的多个重叠版本，触发率不能当作独立泛化准确率；普通对照的额外复核也仍需回听。4条短输入／短对照无候选不能当单人认证。
- 预先列出的短侧门槛敏感性：0.10时混人5/6、普通4/6触发；主0.15为5/6、2/6；0.20为4/6、1/6。保留漏检与额外触发的取舍，未挑结果替换主设置。结论是可继续验证为局部重ASR的辅助提醒，不能作为自动切分或B0单人准入证明；同时发声及真实A→B→A音频仍未验收。
- 新增49个算法语义测试（含1s／400ms A→B、A→B→A和短尾），完整`tests/engine/parsev2`为267 passed、0 failed。独立非调用算法的复算覆盖228主扫描、96组敏感性结果、103个Foote点，最大误差5.5511e-16。首次实测前已修复并回归“低支持尾峰被升级成完整支持峰”的review发现。
- [实验结果与证据](../../outputs/worktrees/ep04-segmentation-20261020/outputs/verification/ep04-speaker-change-20261020/run-v1/report.md)保留全部样本、参数／代码摘要、PNG和JSON；最新实现状态与结论已同步main原目录的[voiceprint-trd.md](../design/02-agent-harness/voiceprint-trd.md)。本轮未重ASR、重解析、部署、重启、迁移、commit、push或合并PR，未改主工作区其他代码。
- 后续按用户要求提供 [12 条矩阵 HTML](../../outputs/verification/ep04-speaker-change-20261020/matrices-12.html)：6 个历史合并输入加 6 条普通长台词，每条保留矩阵数值与原音频。主工作区和隔离副本均有可独立离线打开的文件。TRD 第 3 节补齐当前实际扫描、0.10／0.15 分支、连续点合并和支持不足处理；此次仅展示冻结结果，未重新编码或更改参数。

- 后续按用户“停顿可能造成误判”的假设，复用本地 pyannote 官方 VAD 对 12 条做窗口过滤对照；保留原时间网格／邻域／CAM++／反差门槛，仅可选 mask 排除配对。主设置语音占比≥50%：历史触发 5/6→3/6、普通 2/6→2/6；第11条误报与第12条正确提示均未过滤任何窗（该句前面的5/6、2/6保留初始选样分组口径，最新分组见本节首条），爱音部分候选因支持不足消失，不能当作修好。25% 为 5/6、2/6；75% 为 2/6、1/6。新增 13 个 mask 语义测试，engine 280 passed。见 [VAD 对照记录](../../outputs/verification/ep04-speaker-change-20261020/vad-mask-report.md) 与 TRD 第 7 节；未接生产或重解析。

#### 2026-10-17 历史状态：v5 曾通过爽世单例

多参考修复后重启服务，关闭 Prefix/Compact 的第四集 v5 完整解析与索引通过；真实 Refine 将目标从高松灯纠正为长崎爽世，MySQL 与 ES 均已核验。匿名 ASR 在该轮虽分出目标短句，仍与爱音前文使用同一标签；全片人物准确率与完整逐次工具读写审计未验收。以下保留当时的排查与结果，不以单例通过覆盖后来的复现。

#### 背景与固定问题

- 用户确认 MyGO 第 4 集约 `00:47–00:49` 的「祥ちゃんも悪くない／也不是小祥的错」应是 **长崎爽世** 所说。旧任务 `ac0a6822-79e1-4a23-944b-2c1090603e27` 的正式数据库却记录 `speaker=高松燈`，旧 Refine `updates=[]`。本项以用户纠正为此 case 的标准，未新增独立人工听音标注。
- 旧 ASR `35.4–48.7s` 把爱音的前文和目标短句合并，且没有 `speaker_id`。不能把正式 Timeline 的错误姓名当作 ASR 已直接识出“灯”，错误出现在基于候选转写与画面的后续归因。
- 用户只要求按音色区分 `speaker1/speaker2`，不要求 ASR 认识背景人物真名；编号作用域为单个输入片段，跨 Chunk 相同编号不代表同一人。真实人物匹配由理解／复核结合证据完成，不能仅凭人物背景名单强行填名。

#### 已实施 ASR 契约与第一次真实复测

- `15a67a0` 已接通匿名标签：候选输出 schema 要求可空 `speaker_id`；Prompt 明确同声复用编号、换人拆条、不确定留空；规范化与 `detail.evidence.asr` 保留标签。兼容历史无标签结果，不通过补一个字段伪造正确音色分组。
- 新建独立系列 `bdbf28ae-5404-58e4-8c98-699433499ffc`（「MyGO 第4集 · ASR说话人复核」），视频 `0019c16c-a7b7-5f6c-8275-5019869c1207`；复制原背景文字与 9 张图片，只上传第四集。源 SHA256 `50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`，时长 `1420127ms`；原 MyGO 13 集、原目标内容版本与更新时间不变。
- 任务 `a94be537-6385-4ba9-b99b-ca63477f5982` 的 ASR 请求真实包含新 Prompt 和 schema，实际返回模型为豆包 `doubao-seed-2-0-lite-260428`；Understanding／Refine 配置 `codex_local / gpt-6-astra`。该轮删除 fallback、设置自动重试为 0，避免失败反复调用；没有将标准人物名塞入模型输入。

| 环节 | 第一次真实复测结果 | 判断 |
| --- | --- | --- |
| 原始 ASR | `35.5–48.7s` 仍合并为 `speaker1`；`53.2–55.2s` 的「誰も悪くないんだよ」为 `speaker3` | 标签已输出，目标换人边界仍未拆出，音色分组未通过 |
| 首次 Understanding | 目标句 `47.0–48.7s`、`speaker=null`；描述称画面仍在爱音身上，不能凭画面确定声音归属 | 避免旧版错填灯，但未识出爽世 |
| 首窗口 Refine | 可读 `[0,3)` 三个 Chunk，输入包括后续爽世出现及「谁都没有错」；仅更新 `seq2`，没有修改 `seq0` | 不是窗口太窄；已提供文字前后文，没有完成归因修正 |
| 最终数据库 | 目标首段仍为 v1，内容与已采集初版一致，`speaker=null` | 没有程序自动清空姓名的证据，也没有正确人物落库 |

首次理解接收抽样图片、ASR 文字和背景，没有原音频；Prompt 要求缺少可靠对应时保留 `null`。这是模型输出本身，不是后端把“爽世”降级为未知。该轮 Refine 只有 `get_context_canvas / write_context_canvas` 和完成信号，纯文字复核没有按需看帧或重新听音；其提示也未明确聚焦未解决 speaker、换人边界和声音分组冲突。存在足够文字前后文不等于能够证明模型逐项核验过它。

#### Chat 答对与 ASR 修复必须分开验收

- 旧问题是“这里说到突然退出的人是指谁？”，锚点 `42.977s`。旧回答“丰川祥子”主结论本来正确，问题在附带声称“灯回应小祥没有错”。新系列新会话答对退出者且不再误归灯，但没有指出目标说话人，不能计为音色识人通过。
- 独立追问“00:47 左右说‘小祥也没有错’的是谁？”未包含标准人物名，Chat 回答“应该是长崎爽世”，明确属于连续剧情推断、未经音色／口型独立确认。Query／Evaluator 各两次调用 `inspect_video_frames`，同组 4 帧为 `48.025/50.075/52.125/54.175s`，3 次命中缓存；因此不能把最终答对单独归因于 ASR。
- 两次都是新会话，没有重放旧 EP01 多轮历史。样本只覆盖单集单句，且存在模型随机性、背景简体化和上下文差异，不据此推导 13 集总体准确率，也不以此为依据直接全量重解析。

#### Trace 现状与缺口

- 原始 ASR 的 request、完整 SSE、终态可恢复，目标新 call 为 `48daa7a3-59fd-4bb4-bc44-582292f72b91`；采集器实际捕获 24 个首次 v1，另存最终数据库、Refine delta、输入版本、ModelUsage 及执行计时。
- 覆盖目标的 Refine call `4352e881-ccb0-4f97-a08d-2890eb3bbe84` 在 `21:01:19–21:02:26 +0800` 成功；模型 trace 只有 `request/completed`，没有完整 Canvas 逐次读取、写入参数及回执。只能确认没有提交目标修正，不能断言“精读后判未知”或“完全没读”；日志也不记录模型隐藏推理。
- 待补的可观测性属于 Gateway／Canvas 调用链：关联 task、interval、lease、call 与工具输入范围、revision、结果／错误，限制输出体积并排除媒体 base64 和凭据。不能把新增抽帧起止日志描述为已补齐全部 Canvas 审计。

#### Refine 通用看帧接线与第二次复测

- 当前代码已新增 [通用工具](../../agent/utils/video_frames.py)，由 [Refine 宿主](../../server/service/video_parse.py) 绑定原视频、当前授权时间范围和取消／租约检查；复用既有采样、ffmpeg 抽帧、`ToolImageArtifact` 图片传输，不依赖 Chat 会话／召回状态。
- 工具仍叫 `inspect_video_frames`，接受原视频绝对毫秒 `start_ms/end_ms` 和 `frame_count=1–4`，返回实际图片及采样位置、摘要；超出授权范围明确拒绝。临时图片编码入内存后清理，不登记长期 URI，不新增长期图片存储或数据库字段。
- [Refine Prompt](../../agent/video_parse_v2/prompts.py) 已要求回查未知归属、换人边界和 ASR 冲突，按需看附近画面；静帧不含音频，不得声称音色确认，不确定时仍可保留 `null` 并说明限制。
- 本地相关回归 161 项通过；同一第四集真实 ffmpeg 抽取两帧并成功编码为工具图片，0 模型调用。[抽帧记录](../../outputs/verification/refine-frame-tool-20261016/real-frame-smoke.json)。这只证明工具传输和边界，不证明 Astra 实际会调用、会修正目标，或所有回执已完整可追踪。
- 用户已授权在该独立系列重新解析第四集。API 已重启为 PID `12172`，健康检查、Parse Consumer 和资料库就绪，无数据库迁移，也没有中断在途解析；Astra 真实小探针成功，耗时 `9.303s`。
- `2026-10-16 22:19:51 +0800` 一次调用已有 reparse 接口，HTTP 200 创建任务 `9406af9e-4ee0-4787-bb9e-609656b19186`、解析 version 2；仍使用上面的独立 Series／Video／同一源 SHA256，未重新上传或覆盖原 MyGO。冻结配置仍为 24 个 Chunk、60s／180s、ASR Ark Lite、理解及 Refine Codex/Astra、无 fallback、`max_retry_count=0 / maxAttempts=1`。
- 新轮身份、接口响应和源码快照保存到 [本轮身份](../../outputs/verification/mygo-ep04-refine-frames-20261016/identity.json)、[启动响应](../../outputs/verification/mygo-ep04-refine-frames-20261016/reparse-response.json)。下方记录真实终态与已提交的局部语义结果，不以接口受理、抽帧 smoke 或局部完成当作整集通过。

##### 第二次复测的最终结果与中断背景

- `22:23:54 +0800` API PID `12172` 进入正常停机并取消 10 个在途执行（8 Refine、2 Understanding）；`22:24:03` 新 API PID `30878` 启动，同一任务以 lease 2 接续，没有新建第三轮解析。触发该次重启的来源未知，用户也不清楚；不能把取消记录当作供应商故障，也不能把含此次中断的时长当作无中断性能 A/B。仅记录事实，不再据此推测操作者。
- 本轮 ASR call `bbbf4393-77cf-479c-af56-4935dd5c3eb9` 出现重叠重复：`35.3–48.7s / speaker1` 长段仍含目标句，另以同音异写「咲ちゃんも、悪くない」在 `47.7–49.1s / speaker2` 再列一条；后句 `53.2–55.4s` 为 `speaker3`。不是清晰、正确的换人拆条，音色归组仍未通过。
- 首次理解把 `47.7–49.1s` 目标句再次写成 `speaker=高松灯`，与上轮未知不同；终态首段仍为相同内容 v1，没有被 Refine 纠正。它是本轮错误归属，不能因为新增工具或实际调用图片就判为改善。
- 首窗口 `cf247fcc-ddb5-491b-a220-72f1f867b0b2` 已确认真实执行取帧。停服前第一次请求虽然从 `47.7s` 开始，均匀采样最早落在 `49.237s`，已晚于目标句结束，不能声称拍到了说话时画面；恢复后实际又抽取 `48.050s / 48.750s` 及 `48.400s`，这次才覆盖目标发言区间。
- `22:27:25` 捕获该窗口已提交的 delta，仅更新 `seq2`，未改目标 `seq0`；后续首段快照的增加来自 detail／索引数据变化，语义内容仍为 v1、`speaker=高松灯`。**目标时段图片已成功抽取并编码，宿主供图通路可用，该归属仍未纠正**；本轮没有保存原生图片回执，不能进一步断言模型逐帧核验并采纳了哪些内容。工具可用性与语义修正效果须分别报告。
- 主任务于 **22:30:56.950 +0800** 进入 **PARSING_FAILED**，理解 **13/24**、Refine **8/22**、索引 **4 个 Chunk**完成；任务墙钟 **11:05.220**，API 有效执行统计 **10:54.161**，`processingTimingComplete=false`。计时账本不完整，不能把统计值当作完整连续处理时长，也不能用本轮与无中断首轮计算工具加速／减速比。API PID `30878` 在任务失败后仍健康。
- 失败子任务为 `seq10、13–18` 共 7 个，根因均为 `worker lease expired`，与停机撤销的理解执行批次吻合。冻结 `max_retry_count=0` 使租约回收触发失败预算耗尽；主任务的 `draining after a terminal failure` 是子任务终止后收尾信息，不是供应商根因。[失败明细](../../outputs/verification/mygo-ep04-refine-frames-20261016/failure-details.json)。停机恢复的租约边界另见 B11，本轮未修代码或自动重新投递。
- 已持久化 ModelUsage 按唯一 call ID 统计为 **62 条：52 completed、10 failed**；10 条 failed 对应停机取消，未发现应据此认定的供应商故障。另 7 个受中断的 ASR 诊断调用有 request，其中部分收到 SSE，但缺少终态用量账本，不在这 62 条中，也不能把未知用量当作 0。Refine 取帧日志为 **141 次 started／141 次 success／0 次 failed**，涉及 8 个窗口；重启和反复核验可形成重复，单凭次数不能断言死循环，也不能把工具图片成功当作人物归属正确。
- 本轮 **未运行新的 Chat 复现**：整集没有完成，避免用未完成／混合版本数据做端到端质量结论。上轮答对的 Chat 保留为上轮证据，不追认成此次看帧接线后的结果。
- 已核对原 MyGO 13 个视频 ID 不变且全部 COMPLETE，原任务和内容在本次测试开始后均无修改；原 EP04 目标任务、内容的版本／更新时间／正文均保持，上轮独立 parse v1 仍 COMPLETE。新失败任务的父／子／Interval lease owner 均已清空，`active_model_calls={}`，没有记录中的本地运行任务；断连旧请求的远端执行或计费无法由数据库证明停止。[保留与收尾检查](../../outputs/verification/mygo-ep04-refine-frames-20261016/original-preservation-check.json)。
- 证据：[任务终态](../../outputs/verification/mygo-ep04-refine-frames-20261016/final-task.json)、[终态首段](../../outputs/verification/mygo-ep04-refine-frames-20261016/final-seq0.json)、[模型用量](../../outputs/verification/mygo-ep04-refine-frames-20261016/final-model-usage.json)、[取帧日志](../../outputs/verification/mygo-ep04-refine-frames-20261016/frame-inspection-events.log)、[停机明细](../../outputs/verification/mygo-ep04-refine-frames-20261016/interruption-summary.json)、[版本采集](../../outputs/verification/mygo-ep04-refine-frames-20261016/version-watch/capture-summary.json)。

#### 第三次复测：开启 PrefixContextAgent，目标仍未识别，已停止持续追踪

- 用户明确授权开启 PrefixContextAgent，仅对同一独立系列的第四集重解析。`2026-10-16 22:53:38 +0800` 唯一一次 reparse POST 返回 HTTP 200，创建任务 `c0c6356e-5066-4d7e-a7b3-e7aea563ccc1`、parse version **3**。Series、Video、上传会话及源 SHA256 保持上述身份；前两轮记录保留，不重新解析原 MyGO。
- 本轮冻结配置将 `compact_enabled=true`，新增 `models.prefix_context` 绑定 `codex_local / gpt-6-astra`，纯文字、180 秒、无 fallback；Prefix 预算为 Series 摘要 8000 字符、Video 摘要 6000 字符、最多 6 次 Canvas 工具调用。ASR、理解、Refine 和 Embedding 沿用上轮配置，不把开启 Prefix 当作切换了 ASR 模型。
- API PID 仍为 `30878`；提交前健康、无活动解析或 Chat，没有重启服务、数据库迁移或生产代码修改。该设置只冻结在本轮任务，未改默认 YAML；[只读预检](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/preflight.json)、[本轮身份](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/identity.json)、[实际提交响应](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/reparse-response.json)、[冻结配置](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/requested-parse-config.json)。
- **首段前序摘要仍为空。** 本系列只有 EP04，没有生成或导入 EP01–03 的摘要；目标台词在 `seq0`，其前面没有本集已解析 Chunk。因此不能预期本轮 Prefix 从不存在的前序摘要直接改善目标句，不能把背景资料、后续 Refine 上下文与已生成 Prefix 混称同一输入。
- 验收方案原定同时核对主任务终态、Prefix 真实模型调用／提交、游标推进 **24/24** 及新版本索引发布；主任务 `COMPLETE` 单独不足以证明 Prefix 链路完成。用户随后要求停止持续追踪、只检查目标 Chunk，故下方只报告已核实的目标结果与已观察状态，不补造全轮 Prefix 的最终验收。

| 环节 | 本轮目标句「祥ちゃんも悪くない／也不是小祥的错」的实际结果 |
| --- | --- |
| 原始 ASR | `35.3–48.8s / speaker1` 长段仍合并目标句，未正确拆出换人边界；本轮没有第二轮的重复独立短句 |
| 首次理解，内容 v1 | `47.1–48.8s / speaker=null`；描述说明画面仍是爱音，ASR 合并使画外接话者不能可靠确定。这发生在 Prefix 前，不能归为 Prefix 的改善 |
| Refine 后，内容 v2 | 仍为 `null`；新增说明：爱音抬头后爽世出现并接着说「誰も悪くないんだよ」，上下文提示可能是来人接话，但静帧与冲突 ASR 不足以确认画外声音 |
| Prefix 提交后 | 内容 **v2→v2**，persons、timeline 及整段正文完全不变；只写入 Series 摘要 **126** 字符、Video 摘要 **282** 字符，两类摘要继续保留目标归属未知 |

- **目标人物识别未通过。** 用户要求停止追踪后的一次直接数据库查询确认，该句仍为 `speaker=null`，没有识别为用户已确认的长崎爽世。Prefix 已实际执行并提交摘要，不能解释成开关未生效；也不能把“避免错归灯”当作成功识出了爽世。[首次理解](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/first-seq0.json)、[Refine 后](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/after-refine-seq0.json)、[Prefix 输入](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/prefix-input-seq0.json)、[Prefix 后](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/prefix-after-seq0.json)、[正文差异核对](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/prefix-seq0-effect.json)。
- 本轮明确给出的保留理由是 **ASR 合并／分组不准、画外声音缺少直接核验依据**；开启 Prefix 没有补充原音频或首段前序摘要。现有 Prefix 按游标单向处理，后续本集摘要不会自动触发首段重审。不能据此推导“有真实前序剧情摘要也无效”，该条件本轮没有测试。
- 主解析曾于 `23:13:24 +0800` 被观察为 `COMPLETE`，理解 **24/24**、Refine **22/22**、索引 **24/24**；当时 Prefix 尚未全部完成。[主流程首次完成快照](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/main-first-complete.json)。后续 Prefix 的正文修改会使状态短暂回到 `INDEXING`，需重新发布对应内容版本，不应把主流程首次完成当作所有后台工作结束。
- 附加质量风险：已观察的 `seq3` 中，Prefix 将 **9 条已具名对话改为 `speaker=null`**，保留台词与时间并修改描述，触发正文版本递增和重索引。这证明修改链路可用，但可能过度保守；该段未建立逐条人工标准答案，不能将修改数量计为准确率提升。[已观察的 Prefix 修改汇总](../../outputs/verification/mygo-ep04-prefix-enabled-20261016/prefix-effect-summary.json)。
- 用户要求后已停止只读采集器，实际为 SIGINT／exit 130，没有续采；采集摘要中的默认 `max_duration` 退出标签未能区分这次人为停止，不能据此认定解析或 Prefix 超时。停止的是追踪，真实解析／Prefix 服务任务未被取消，也未追加新的 Chat 或其他模型实验。整轮后台终态、24/24 Prefix 及最终索引一致性**未继续核验**；后续诊断以现存目标阶段快照为依据，不继续后台追踪。

#### 2026-10-17：声纹准备与 Refine 声音比对接通

- 正式声纹位于本次 `video_parse_task.detail.voiceprint={ready,voiceprintList}`；视频、任务归属由父行确定，不按 series 或跨视频共享，不进入 ES。每个人物保存实际模型/revision、逐片段 192 维向量、来源 Chunk/版本/事件指纹、原视频绝对毫秒、PCM SHA-256 和选择依据。Engine 只定义 Schema；Gateway 执行 CAM++ CPU 推理。
- 声纹默认关闭。开启时，提交 Understanding 的 Chunk 达到 `ceil(chunk_count/2)` 即固定本次来源与 speaker 名单并调用 `InvokeVoiceprint`；剩余 Understanding 继续执行，原 90% 批次开放规则不变。四个工具为名单查询、只读 Canvas、登记、完成；完成须有效登记人数严格超过名单一半，空名单允许零参考，NPC 可跳过并返回原因。非空 speaker 或自报 confidence 本身不能替代身份依据，首版不做同人异名合并。
- Refine 的实际原子领取增加 `interval_ready && voiceprint.ready`；未就绪维持 WAITING，不占槽或扣失败。比较工具适用于 null 及有姓名但冲突的台词，返回 cosine/候选差值/来源及实际时长，排除自身、重叠音频、重复 PCM 与不相容模型。工具不写 timeline，仍由原 Canvas 修改或保留 null。后续 EP04 v7 追踪发现旧来源指纹包含中文翻译：仅改翻译也会使参考失效，并使至少 9 轮 Refine 在合并前被拒绝。按用户确认，准备后的 speaker 和音频区间直接视为正确静态成果，读取及提交不再对照 timeline 重验；参考元数据只保留用于追溯，event 合并冲突日志继续保留。
- 父协调器复用原租约、模型准入、用量和取消。准备期间的真实异常、超时和 active 崩溃受持久化有限预算约束，首次准备事务失败也计预算；正常取消/等待不计。Python 3.12 的自定义阻塞取消异常超时路径已单独验证：保留执行槽直到线程真实退出，避免误记为正常取消。另已用真实测试数据库覆盖取消先于 begin 事务绑定的竞态，事务真实退出后再次 stop，再由 Consumer 释放，正常取消不误扣失败。部分登记不放行，成功准备幂等跳过，人工 retry 重置本阶段失败预算。新增可空配置字段关闭时省略序列化，保持旧上传幂等指纹。
- 自动化验证分两组：Agent/Repository/Queue/Worker/Upload/Gateway **540 项通过**，配置及媒体 **338 项通过**；数据库用例为测试隔离环境，尚未进行真实 MySQL 多 Consumer 锁竞争验收。未改 ASR Prompt，未重启服务、迁移、运行真实生成 Agent、写回 speaker 或重解析任何一集。
- 真实音频验证复用 [声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)，只取 `sequence_no<12` 的 16 条固定参考，每人 4 条，目标不入参考。使用本次 `_embed_speaker_segment` 从同一原视频直接 downmix 到 16k mono PCM16，再经 Gateway 调用官方固定 CAM++；17 次实际提取 completed，输出均为有限、归一化 192 维向量，临时 WAV/租约标记清理通过。此处声道为 mono downmix，与原实验的首声道协议不同，不将数值差异解释成准确率提升。
- 目标 `47100–48800ms`、实际 1700ms 的候选为：长崎爽世 **0.82694462**、高松灯 **0.51958897**、千早爱音 **0.44891524**、椎名立希 **0.39888920**；第一/二差值 **0.30735565**。相似度不是概率；这些样本的身份依据沿用实验清单/用户授权，不能外推整集准确率，也不等于真实 Refine 已正确识别并写回爽世。
- 原视频 SHA-256 `50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`；目标 PCM SHA-256 `8587a4c56232d357e5b1c3d6e9ee2560dd378bfefe51c943ec15a484fb3b9f08`。本次临时验证输出 `/tmp/seekino-voiceprint-smoke-20261017/result.json` 包含每条向量、来源、实际模型 revision、候选及调用 ID，文件 SHA-256 `a8ec8321961faf0f5ac3736b9a8da85fb218d13478ec709931a783ab4e3518b2`，目标 call ID `dc7d49db-2c41-4562-8bcc-cc5d458a7c92`。临时依赖/官方源码/权重仅在 `/tmp`，未同步项目主环境；临时目录可能被系统清理，固定来源与协议仍见清单及 [声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)。

#### 2026-10-17：重启后的 EP04 VoiceprintAgent 实测

- 已安装 speaker 可选依赖到项目 `.venv`，在 ignored `outputs/local-models/campplus/` 部署官方固定源码和权重，配置 CAM++ CPU Gateway source/binding，重启 API/Web 后通过原 reparse 接口仅提交隔离第四集 v4：`a99b4af4-4b66-43e5-90dc-b04b525b616a`。MySQL/ES 未重启，没有迁移、commit/push。完整冻结配置、部署 SHA、时间线及数据基线见 [VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)。
- 本轮真实观察到 Understanding 恰好 12/24 提交即启动准备；固定 12 块、5 人名单。Agent 依据已提交 ACTION/EMOTION 与独立 ASR 轮次登记爽世 414900–416400ms、立希 597100–600000ms、灯 536200–540000ms，各一条。准备完成 3/5 人，11:48:29 提交 ready；参考部分登记期间全部 Interval 仍 WAITING，ready 后才开始领取，此时 Understanding 20/24。目标 null 没有被登记为参考。
- 目标本轮边界 `47700–48700ms`，实际 1000ms。真实 compare_speaker 候选为爽世 **0.433279**、灯 **0.411900**、立希 **0.394218**，前两名差 **0.021379**。Refine 明确消费该结果，因差值小及参考覆盖不足保留 null；Prefix 后仍 null，seq0 v2 已索引。链路实际执行通过，目标正确识别未通过。参考集合、目标裁切与此前 1700ms／多参考独立实验不同，不能直接拿旧高分替代本轮结论。
- 实测发现准备阶段一条真实 speaker_embedding failed（37.050s，冻结超时 30s）被 Codex 通用工具反馈路径交给模型，Agent 重试后仍提交 ready，绕过宿主失败预算。账本未保存原始异常类型，不把推断的 TimeoutError 当作直接回执。已仅在准备宿主保存首次真实错误、阻止后续 finish，并保留正常取消和 pending 资源所有权；相关 worker **107 项**、声纹仓库/队列 **144 项**回归通过。补丁与失败账本保留在实验目录。此修复尚无修复后独立完成整集解析的实测证据。
- 运行期间日志另出现 11:53:03 API 2197 开始关闭、11:57:24 新 API 27365 启动；本 Agent 未在解析期间发起该重启，日志无法确定发起者。12:01:59，9 个 Interval 以 `worker lease expired` 失败，冻结 `max_retry_count=0`，最终 12:03:55 主任务 **PARSING_FAILED**：Understanding 24/24、Refine 8/22 提交、9 失败/5 WAITING，索引 1/24、Prefix 游标 1。重叠窗口曾因版本冲突重算，但未发现单次调用突破 600s 的证据；外层仍有固定 1800s 时限。该中断使整集验收失败，不能归为 CAM++ 人物识别错误。
- 新父租约保留相同的 ready 和三条正式声纹，未重新调用准备 Agent／登记，提供了真实重启后幂等恢复证据。收尾确认本任务活动租约与模型占用均为 0，临时媒体目录不存在。原系列 13 条视频、13 条任务、312 条正文的版本、时间及规范内容 SHA-256 全部未变；当前 API 健康。统一账本保留 287 次成功和 1 次失败 CAM++ 调用，成功耗时中位数 85ms、P95 1193ms；此计时含初始化/锁等待，进程中断的未完结调用不在统计中，不当作纯推理 benchmark 或准确率。

#### 2026-10-17：单参考代表性不足，多参考采集目标与提前完成问题

- **状态：多参考采集与登记门禁已修复，408 项相关回归通过；真实 Agent 选样质量待单独验收。** 离线实验样本未写进正式声纹库。现有 `CharacterVoiceprint.references` 已经能在同一 speaker 下保存多条独立向量，登记实现会向相同 speaker/model/revision 的人物记录追加；Refine 也已对多参考做单位化、均值和质心再归一化比较。缺口主要在采样策略、有效样本计数及完成条件，而非缺少多向量存储能力。
- **根因：人物覆盖目标与单人物参考充分度混在一起。** 修复前 `get_speaker_list` 仅返回人物/chunk 和总体覆盖数，没有每个人的已登记数、目标和缺额；Prompt 与 `finish_voiceprint_preparation` 只要求具参考的人物数严格超过名单一半。实际 5 人名单中登记 3 人、每人 1 条便可结束，缺少继续为每个人物收集独立参考的目标。原 `skip_reasons` 仅返回零参考人物的理由，1–4 条时没有缺额说明机制。
- **爽世选段实验：** 固定同一 `47700–48700ms` 目标，原急切追问参考仍为 **0.433279**；温柔感谢样本为 **0.729227**，困惑询问为 **0.673273**，普通协商为 **0.500743**。同场景平静劝解为 **0.771343**，但共享“悪くない”措辞，不能独自证明跨场景表现。完整片段与固定首 1.5 秒的 9 组对照中，7 组完整得分更高、2 组更低，不能认为时长越长越好。情绪分类来自解析描述、尚未独立听音标注；文本、停顿、场景和背景仍有混杂因素，不能断定差异完全由情绪造成。见 [声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)。
- **三人均衡多参考实验：** 爽世、灯、立希各 5 条、每人来自 5 个不同 chunk，均在初始 Understanding v1 已具名；目标所在 chunk、重叠音频和重复 PCM 排除，保留各自原登记作 baseline。固定目标不变，30 次 CAM++ CPU 提取全部成功；以下为完整参考的结果，分数均为 cosine、不是身份概率。

| 聚合方式 | 长崎爽世 | 高松灯 | 椎名立希 | 第一、二名差 |
|---|---:|---:|---:|---:|
| 原登记单条参考 | 0.433279 | 0.411900 | 0.394218 | 0.021380 |
| 每条与目标比较后等权平均 | 0.617295 | 0.464499 | 0.371158 | 0.152796 |
| 按时长加权平均（每条权重上限 4 秒） | 0.640495 | 0.459412 | 0.374714 | 0.181083 |
| 现有归一化质心比较 | 0.744100 | 0.549815 | 0.435815 | 0.194285 |

- 统一每条参考首 1.5 秒后，等权平均仍为爽世 **0.506174**、灯 **0.406279**、立希 **0.340700**，前两名差 **0.099895**。灯增加参考后得分也会上升，但本目标上仍与爽世拉开差距。这解决了只给爽世增加参考、其他人保持单参考的不对等口径；不能由单个已知目标估计整体准确率，且爽世候选池来自此前已评分实验，不属于完全盲测。清单、逐条音频/向量/权重及来源 SHA 见 [声纹基础实验退役摘要](design-experiment-cleanup.md#声纹基础实验)。这两个实验目录均被 Git 忽略，链接指向本地实验成果。
- **聚合公式不能只比较数字大小。** 单位目标/参考下，逐条余弦均值为 `A=t·mean(u_i)`；现有归一化质心为 `B=A/||mean(u_i)||`。因此质心法数值更高包含缩放因素，不等于准确率更高；时长加权仅是预先固定的实验对照，也不是质量或人物置信度。本轮各方法排名一致，不能据此确定普遍最优算法。
- **当前采集规则：** 每个 speaker 下保留多条参考；以 **5 条合格、独立参考作为每人采集目标，不设每人登记硬上限**，原“首版成本上限”已移除，仍受阶段有限工具/时间预算约束。数值仅定义于 `server/constants/parse.py::VOICEPRINT_REFS_PER_SPEAKER`，Agent 消费工具返回的 `target_refs_count`。不足 5 条允许完成，应说明固定授权来源不足或阶段预算接近耗尽等原因；不把 5 条作为 ready 的硬门槛，不用低质量/不确定样本凑数。人物身份可靠、单人有效发声和清楚边界优先，再尽量覆盖不同场景、台词和说话状态；不设置必须凑齐几种情绪的指标。跨 chunk 优先，但同 chunk 的不同独立台词仍可使用。
- **独立来源与区间门禁：** 原实现仅对完全相同的来源/区间/fingerprint/PCM 做幂等。精确重复请求直接返回已有登记、不重做截音/embedding；同一 `source_ref` 的另一子区间即使不交叉也不能再登记为独立 case，只有显式替换才允许改为更准确子区间。对本轮全部人物的已登记区间检查交叉，采用绝对毫秒半开区间，`new_start < old_end && old_start < new_end` 即拒绝，首尾相接允许。宿主在提取前预检、持久化时原子复核；不同来源截取后 PCM 摘要完全相同也在最终登记拒绝，不增加声学查重框架。返回 `message/retry_hint` 引导 Agent 换独立、非重叠台词；这类反馈消耗工具预算，不计准备执行失败。参考目标按有效独立来源计，失效来源不继续计数；不扩大到同人异名合并。
- **工具与收口：** `get_speaker_list` 返回每人的 `refs_count/target_refs_count` 及 `references=[{source_ref,start_ms,end_ms}]`，Agent 能对应 sources 避开已登记台词，不接收向量。`register_voiceprint` 可对同人多次追加，回执包含 `added`、该人物的参考进度和来源元数据。Agent 达到人物覆盖后继续补充多参考，在达到目标、可用来源耗尽或接近本阶段预算时、预算真正耗尽前调用完成工具。`finish` 要求名单内所有不足目标者（含零参考）提供非空 `skip_reasons`；缺原因返回 `ready=false`、`missing_reason_speakers`、`reference_counts`、目标及提示。说明齐全且 **具有效参考的人物数 > speaker_list_size/2** 才可 ready，空名单可零参考结束；理由不绕过多数门禁，也不额外建立“来源耗尽”审计框架。模型、截音、数据库失败及真实预算耗尽仍真实上抛，不能伪装成“收不够参考”。
- 来源继续限于达到半数 Understanding 时固定的授权快照，预算/取消/恢复复用现有准备阶段；不为凑 5 等全部 chunk，不因后续更多台词触发无界补采或整轮 Refine。本轮调整采集策略、工具进度和完成检查，保持现有多参考存储及质心聚合；是否切换逐条均值/权重另按更多目标的验证结果决定。实现当时尚未进行第四集重解析，后续真实 v5 结果见下节；全片准确率仍未统计。

- **本次代码验证：** `.venv/bin/python -m pytest -q tests/server/test_video_parse_voiceprint.py tests/server/test_video_parse_worker.py tests/agent/video_parse_v2/test_voiceprint.py tests/server/test_video_parse_queue.py tests/server/test_video_parse_repository.py tests/agent/video_parse_v2/test_framework.py --tb=short`：**408 passed**，覆盖独立来源、区间/PCM 去重、上限与幂等、缺额说明、Refine 准入以及既有失败/取消/恢复。新增 MySQL 重叠登记与第 5 名额竞争两例已通过 collect-only；当前未配置专用 `SEEKINO_TEST_DATABASE_URL`，未执行真实 MySQL 竞争，也未初始化测试库。`git diff --check` 与本轮独立 hypnos-review 通过。该代码修改阶段没有重启、迁移、重解析或 commit/push；后续按用户授权重启与复测见下节。


#### 2026-10-17：关闭 Prefix/Compact 后，第四集 v5 多参考端到端通过

- 按用户指令重启 API/Web 后，仅通过 reparse 提交隔离第四集新任务 `dd55671d-a84b-4628-bedb-4007a4f4aa9c`、version5。相对 v4 请求只关闭 `compact_enabled`、清空 `prefix_context` 及其模型绑定；声纹96次工具/600秒、CAM++ CPU/30秒、并发10、max_retry_count=0等保持原值。没有人工登记样本或注入目标身份。详细报告、请求/响应与快照见 [VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)。
- 15:13:19.484（UTC+8）恰好12/24 Understanding提交时准备启动，固定seq0–9、13、18和5人名单。实际Agent为爱音、祥子、立希、爽世、灯各收集5条，共25条；独立source_ref和存储PCM摘要各25、无区间交叉、同模型192维单位向量。目标音频未登记。25条已登记但ready=false时22窗口仍WAITING；15:15:45.062首次观察ready，15:15:46.277首个Refine领取；采样无提前领取。
- 本轮目标为47500–48800ms（1300ms）。ASR单独分条但匿名speaker仍与前文爱音相同，首次Understanding v1错归高松灯。Refine真实compare两次一致：爽世0.778768、祥子0.613801、灯0.505230，前两名差约0.164967；cosine不是概率。15:19:43.348提交v2改为长崎爽世，description明确引用声音证据；15:19:54.076目标chunk已索引。终态直接读ES，日文原句和中文译句的speaker均为长崎爽世。
- 15:47:40.798整集COMPLETE，约38分44秒：Understanding24/24、Refine22/22、当前正文版本索引24/24，全部窗口retry_count=0。Prefix lease_version/cursor均为0且无对应模型调用；父子租约、模型占用及capacity已清，临时媒体无残留，API/Consumer健康。原系列13视频、13任务、312正文的版本/时间/规范SHA全部不变，无迁移或commit/push。
- 统一账本1126条均completed，CAM++975次（准备25、Refine950），耗时中位44ms、最大2991ms，包含初始化和锁等待；不能把整集38分钟归于CPU声纹推理。Refine生成54次，对应22窗口正式提交，模型completed不等于成果提交。终态25条参考来源仍全部有效。
- 验收边界：爽世参考含53.2–55.2秒同场景且共享“悪くない”的近邻台词，本轮目标1.3秒而v4为1秒，不是单因素或跨场景泛化实验；没有人工试听校准全部25个身份标签，不能据此估计全片准确率。现有diagnostics不持久化完整Tool回执，缺额理由也不单独落库；本轮全员满5，无缺额理由需要验收。证据来自正式参考、真实比较日志、Canvas写回及ES，不宣称完整工具轨迹已恢复，也不宣称本轮发生并验证了崩溃恢复。

#### 2026-10-17：静态声纹与强制 null 提示复测，v8 流程完成但目标仍错

- 仅在隔离第四集运行任务 `8ef2a23b-def7-4ff3-905d-36cee63ce70f`，Prefix/Compact 关闭。ASR 提示明确无法可靠区分匿名声音时必须 `speaker_id=null`，Understanding 明确身份无可靠依据时必须 `speaker=null`；不改 nullable schema，不注入目标答案。提示实参回归 47 项及独立 hypnos-review 通过。详见 [VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)。
- 19:16:08.468–19:46:17.381（UTC+8）COMPLETE，24/24 Understanding、22/22 Refine、24/24 当前版本索引。墙钟 30:08.913，比 v7 的 43:22.011 少 13:13.098（30.48%）；Refine 54→37 轮、刷新 32→15 次。本轮使用用户当前配置 `gpt-5.6-sol`，v7 为 `gpt-6-astra`，且提示及声纹策略有变化，不能将收益单独归因于去除参考校验。15 次拒绝为 13 次事件修改冲突、2 次结构冲突，没有声纹来源失效拒绝。
- 声纹准备在 12/24 块时启动，正式参考仍为 5 人各 5 条，ready 后不变、无提前领取。VoiceprintAgent 生成耗时 389.539 秒，准备 25 次 CAM++ 编码累计仅 3.914 秒；准备与 Understanding 部分重叠，全部 Understanding 完成后到 ready 仍约等待 2 分 35 秒。原 13 视频/13 任务/312 正文未变，源文件摘要不变，终态租约、模型占用与临时文件已清理。
- **目标识别未通过：** ASR 将 35.6–48.6 秒合成 `speaker1`，目标「咲ちゃんも悪くない」混在末尾；Understanding 将整段归千早爱音。seq0 首轮因 chunk2 的 177200–178000ms 事件双方同改 speaker/description 被拒绝，第二轮只更新 seq1/2；最终 chunk0 及 ES 仍错归千早爱音。全任务 270 条成功比较、seq0 20 条中，覆盖目标 47–49 秒的成功声音比对为 0；不能归因为 CAM++ 错认，也不能排除成功日志前被校验拒绝且未记录的工具尝试。下一步需区分混合接话拆分与具名错误归属核验的缺口，不以继续堆 null 提示替代验证。
- 本轮 ASR 12/353 段为 null；初始 Understanding 43/308、最终 Timeline 31/314 条 DIALOGUE 为 null。这是弃权条目数，不是准确率；本轮没有证明目标修复或全片人物识别改善。

#### 2026-10-17：v9 参考错标与完成前组内一致性检查

- v9 `56498828-8926-4ad7-beb4-b8a22d2b0b49` 的 25 条正式参考中，47.6–48.8 秒「祥ちゃんも悪くない」被登记为灯，用户已确认实际为爽世。组内逐条 cosine 均值为 0.378263，去掉它后其余灯参考内部均值为 0.659323，gap=0.281059；与爽世五条均值为 0.715700。这说明非空 speaker、画面可见及自报 0.96 不能替代独立身份依据。完整矩阵见 [VoiceprintAgent 退役摘要](design-experiment-cleanup.md#voiceprintagent-历次迭代)。
- 当前修复在 `finish_voiceprint_preparation` 同一事务读取未 ready 的参考，按 speaker/model/revision 检查组内一致性；不增 runtime 成果副本或缓存，不重新调用模型。实验初值为至少 5 条独立参考、核心均值 >=0.60、候选对核心均值 <=0.50、gap >=0.18，唯一来源为 `server/constants/parse.py`。少样本不阻止原收口，不强制每组删最低；显著异常返回 ready=false、来源/区间、均值、gap 及修正提示，不静默删条或改姓名。
- 准备工具由原四个增至五个：新增 `unregister_voiceprint(source_ref)` 幂等单条撤销；`register_voiceprint` 可用 `replace_source_ref`，新 embedding 成功后事务替换，含同来源更精确子区间，失败保留旧条。无可靠替代时可撤销后以 `skip_reasons` 说明不足 5 条，具参考人数仍须严格过半；理由不能绕过仍存在的异常。ready 后继续按静态成果使用，不因 timeline 后续编辑重验或重跑。
- 本次执行 `.venv/bin/python -m pytest -q tests/server/test_video_parse_voiceprint.py tests/agent/video_parse_v2/test_voiceprint.py tests/server/test_video_parse_worker.py tests/server/test_video_parse_queue.py tests/server/test_video_parse_repository.py tests/agent/video_parse_v2/test_framework.py --tb=short`：**463 passed，23.56 秒**，仅既有 Starlette 弃用警告。覆盖完成门禁、阈值和编码器分组、显式替换/撤销及回滚、旧租约/来源保护、预算/取消与真实失败保留；此前 408 项及各轮部署/验证事实仍保留其原日期和合同。
- 实际导入 `_voiceprint_consistency` 回放 v9 原 25 条向量，与独立核算一致返回灯 47.6 秒、立希 487.1 秒、祥子 293.9 秒三条可疑参考，报告在 `/tmp/seekino-voiceprint-consistency-replay.json`，没有写数据库或新增仓库实验文件。回放不是泛化准确率，后二条仍无独立真值；真实 MySQL 并发、实际 Agent 重选及全片准确率尚未验收。本轮没有服务重启、迁移、重解析或 commit/push。

验收分别检查：新 ASR 是否真的在换人处分条；首次与最终 Timeline 是否纠正为爽世或有依据保留未知；Refine 是否实际取得图片、提交了哪些事件；最终索引／Chat 是否使用新版本。即使 Chat 答对，也保留 ASR 和数据库未修复的判定。相邻窗口重算见 B07，不在本项顺带修改调度。

依据：[第一次完整报告](../../outputs/verification/mygo-ep04-speaker-review-20261016/report.md)、[结构化对照](../../outputs/verification/mygo-ep04-speaker-review-20261016/comparison-summary.json)、[最终首段](../../outputs/verification/mygo-ep04-speaker-review-20261016/final-seq0.json)、[ASR 契约](../../engine/common/transcription.py)、[解析宿主](../../server/service/video_parse.py)、[原系列保留检查](../../outputs/verification/mygo-ep04-speaker-review-20261016/original-preservation-check.json)。

### B13：解析启动负载尖峰、Codex 重复加载 MCP 与任务列表过量读取

排序日期：2026-10-18。

- 状态：2026-10-18 MCP 实例开关已实现，并完成 bundled Codex 无推理启动/恢复验证；任务列表轻量投影尚未实施。未重启现有服务，真实解析负载收益尚未验收。历史现场任务为隔离第四集 v10 `6ec42661-5938-4857-b7d0-d4a9d2eb77e2`；接手后只读详情已确认 COMPLETE、70/70 单元完成、lastError=null，不能沿用此前 PARSING 状态。

#### 已确认的运行现场（UTC+08:00）

| 时间 | 请求/采样 | 结果 |
| --- | --- | --- |
| 23:06:50–51 | `/api/jobs/video-parse/list` | HTTP 200，13,183.6 / 13,966.2 ms |
| 23:07:01–02 | 同上 | HTTP 200，9,113.7 / 10,237.2 ms |
| 23:07:16 | `/api/series/get` | HTTP 200，24,314.9 ms |
| 23:08:05–11 | 解析任务列表 | 已回落至 377.5 / 474.4 ms |
| 23:09:16 | `/healthz` / 单任务详情 | HTTP 200，16.0 / 60.2 ms |

- 系统 18 个逻辑 CPU，load average 一度 166.42，随后回落至 105.10；后次采样 CPU 约 67% idle，表示此前有尖峰，不能把历史 load 当作当时仍满载。内存压缩约 14 GiB、swap 使用为 0；本轮日志未发现 OOM、MemoryError、线程池耗尽、FFmpeg 失败或实际 ERROR/CRITICAL，不能据此诊断为 OOM。
- API PID 19140 连同其子进程采样共约 101 个进程：10 个 Codex 模型进程、各 10 套 bytedcli/组件文档/Chrome DevTools MCP 等。累计 RSS 约 11.4 GiB，包含共享页重复统计，不是独占物理内存精确值。API 自身采样 RSS 约 450–920 MiB。
- 23:09:16 本轮仍 PARSING、last_error=null、父租约有效；仅 1/24 Understanding 已提交，声纹准备尚未达到半数 Chunk 的启动条件，22 个 Refine 均 WAITING。尖峰出现在 Understanding 启动期间，不能归因于新加的声纹组内矩阵检查。数据库当次 processlist 未见查询堆积或等锁。

#### Gateway：会话与进程绑定，空表未真正关闭 MCP

- `CodexSession.open()` 为每个会话创建 CodexClient，并执行 start/initialize/thread_start；SDK 的 client.start 实际启动本地 app-server 进程。close/interrupt 直接关闭整个 client，因此当次 10 路并发对应 10 个进程，是项目适配器现状，不是 Codex 协议要求一线程一进程。Supervisor 保留/恢复原生 threadId 也不等于复用常驻 app-server。
- 排查时运行参数确有 `mcp_servers={}`，但仍启动全局配置中的 bytedcli、node_repl、motorComponentDoc、chrome-devtools。禁用设置没有达到预期；不能只靠 Prompt 禁用或断言存在空表参数来验收。Codex 配置存在逐表合并语义，应核对项目实际 bundled binary/SDK 的有效配置，不能拿 Homebrew Codex 版本替代运行版本。
- 用户确认的配置边界：解析中的 Understanding/Refine/Voiceprint 等有界模型调用由上层在创建模型实例时传 `mcp=False`；Supervisor 是否保留 MCP 同样由上层装配明确选择。Gateway 只接收、透传和执行实例参数，不根据 Supervisor 身份选择策略，不新增 YAML/source 策略，不修改用户全局 Codex 配置，也不将 MCP 开启扩展为默认开放 shell/浏览器等全部原生能力。
- 单 app-server 承载多个原生 thread 是可评估方向；项目当前只有单会话回调/通知绑定，取消还依赖关闭进程。共享前必须解决按 thread/turn 路由、工具回调隔离、单会话超时取消和进程退出影响面，不能仅把 client 改成单例。该生命周期调整应与先关闭额外 MCP 的修复分别核定范围。

#### MCP 修复与验收进展（2026-10-18）

- 代码：模型创建/build 入口新增实例级 `mcp: bool = False`，严格校验后沿 CodexAgentModel → CodexSession 传递，生成 fallback 沿用本次参数。V2 解析共用创建处显式 False；Server 装配处 Query/Evaluator/Chat 显式 False，Codex Supervisor 显式 True；Gateway 没有角色策略分支，现有配置、任务快照和会话取消机制不变。
- 执行：False 在同一已初始化 app-server 上通过 `config/read` 读取有效服务器集合，再为 thread/start、thread/resume（含 rollback 后恢复）逐项覆盖 `enabled=false`；读取失败或形状异常会关闭会话并报错。True 不覆盖 MCP enabled 状态。两者均保留原生工具禁用、read-only sandbox 和 approval never，宿主动态工具继续可用。
- 真实运行验证：使用项目 bundled binary/SDK 0.156.1，在现有全局配置下创建独立无推理 thread；bytedcli、chrome-devtools、computer-use、motorComponentDoc、node_repl 全部报告 `runtimeStatus=disabled`、工具数 0，进程树未见对应 MCP 命令，宿主动态工具调用成功。该次 open 约 1.32 秒；这是单次启动记录，不是前后性能对比。
- 隔离验证：临时 CODEX_HOME 中配置一个 enabled/required 的受控本地 MCP 和一个 disabled MCP，用历史注入持久化测试 thread 后跨进程恢复。False 的 start/resume 均没有 MCP 启动标记；True 的 start/resume 均能连接原本启用的服务器并发现工具，原本 disabled 的服务器始终没有启动。共 0 个推理 turn，未启动整套全局 MCP，也未修改全局配置。
- 回归：`.venv/bin/python -m pytest tests/gateway/test_codex_{client,model,supervisor}.py tests/gateway/test_{model,agent}_factory.py tests/server/test_bootstrap.py tests/server/test_video_parse_worker.py -q`，536 passed，1 条 Starlette/AnyIO 依赖弃用警告。`git diff --check` 通过；按实施前快照完成 hypnos-review，未发现需修正的行为或职责边界问题，已补 rollback 恢复仍携带禁用表的断言。
- 验证边界：参数传递、非法值、fallback、启动/恢复/rollback、动态工具、取消及上层装配有相关回归；真实 runtime 证据不替代新解析任务在服务中的负载验收。现有服务未重启，本轮未重跑解析、迁移或 commit/push。列表读取量/时延改造、共享 app-server 和前端请求截止时间仍待后续核定。

#### 任务列表：需要轻量统计，却加载完整成果 JSON

- `ParseJobRepository._video_parse_tracking()` 使用 `select(ChunkParseTask, ChunkParseContent, ...)` 加载完整 ORM 实体，只为判断成果存在性、内容版本、索引完成情况及 detail 中少量提交租约/耗时标记，却连带传输并反序列化正文、ASR/索引 detail 和 embedding。
- 当次工作区列表覆盖 23 个任务、501 份 Chunk 成果；content 4,697,554 bytes、detail 16,484,904 bytes，合计约 20.2 MiB/次数据库读取，尚不含父任务及 Interval detail。这不是浏览器响应大小。统计长度的聚合 SQL 约 51.8 ms，未单独 profile 数据传输、JSON 解码和 ORM 成本，不能将 13 秒全部归于这一处。
- 前端 TasksPage 每 5 秒调用 listAllVideoParseTasks，每页 100 条、串行读完全部分页才展示结果；requestJson 缺少请求截止时间，挂起时可能一直显示加载。任务页本身没有视频预览；旧 `/api/videos/content` 的 206 burst 不应直接归因于任务页。
- `project_video_parse_task()` 仅过滤 `_worker_runtime_v2`，当前父任务 voiceprint 向量也会进入列表/详情公开 detail；页面不使用这些向量。是否调整公开 detail 需核对现有消费者，保持必要的声纹元数据和兼容边界。
- MySQL 8.4.11 的只读 EXPLAIN 已核对：Chunk 成果连接通过 `uq_chunk_parse_content_task` 为 eq_ref；全量列表部分扫描与选择范围有关，没有证据表明缺少关联索引是主因。先将 SELECT 投影到存在标记、版本、必要 JSON 叶子及小字段，避免读取完整正文/detail。普通覆盖索引不能自动覆盖 JSON 叶子；是否使用生成列/函数索引或冗余统计字段应在窄查询实测后另议，本轮未执行 DDL。

#### 接手方向与验收

1. MCP 实例参数修复已完成上述独立运行验证；下一批按项目门禁核对任务列表轻量投影的最小修改树。共享进程和前端超时分别说明必要性，避免一起重做框架。
2. 新代码接入现有服务后的解析负载仍待验收；独立无推理会话已核对有效配置、MCP runtime 状态和进程树，不能只靠配置字符串，也不默认启动全部真实 MCP 或消费模型预算。
3. 列表改造保持进度、索引版本、拓扑、耗时完整性与旧成果兼容口径；验证不会加载正文/大 detail/向量，比较同数据量下的时延和读取量，检查分页/取消。需要覆盖索引时先给 EXPLAIN 和收益依据，不能直接加迁移。
4. 如另行实施共享 app-server，必须证明一会话卡住、取消或超时不影响其他会话，工具回调/用量/历史不会串线。不要自动重启、迁移、全量重解析或 commit/push；当前声纹修复和其他工作区脏改动均须保留。

依据：[运行日志](../../outputs/local-services/api.log)、[Codex 会话生命周期](../../gateway/codex/client.py)、[Agent 会话创建](../../gateway/codex/agent.py)、[Supervisor](../../gateway/codex/supervisor.py)、[模型工厂](../../gateway/agent_model/factory.py)、[任务统计查询](../../server/repository/job.py)、[接口投影](../../server/api/jobs.py)、[任务页](../../web/src/features/tasks/TasksPage.tsx)、[前端请求](../../web/src/api/client.ts)；[官方 app-server 协议](https://learn.chatgpt.com/docs/app-server)、[官方 MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。系统/SQL/HTTP采样均为只读，两个负担放大点已确认，各自对尖峰的贡献尚未隔离测量。

### C16：构图检索缺少共同的帧级候选图库，接续 Event 级检索实验

排序日期：2026-10-18。

日期：2026-10-17；更新：2026-10-18。类型：实验数据覆盖不足与外部开发依赖。状态：**MyGO 13 集旧 Event 索引补建及全量验收完成，待按 Event 取帧扩充共同构图候选图库**；当前保留已有实验，不在 17 张样本上继续据排名选择模型或融合权重。

#### 当前卡点与已有证据

- 本地 PCP＋检测框图实验只有 17 张选图；人物框共同候选池为 13 张，排除查询自身后 12 张。缺少足够丰富的构图和人物布局，无法区分“图库没有合适画面”与“存在但模型没有排上来”。
- 俯视房间实测中，现有 Chat 检索 MyGO 13 集，返回第 7 集 `04:06.559–04:09.100`、`05:32.000–05:42.300` 两段休息室画面。第一段较贴近斜俯拍、人物靠右下与左侧大片地板的关系；第二段复用同机位但变成三人。这些候选不在旧 17 图图库中，不能据本轮结果判定 Chat 整体优于 PCP／人物框向量。
- 当前正式视觉索引编码的是视频 Chunk；本轮 Chat 走“参考图理解 → 文字查询 → Chunk 召回 → 原片取帧核验”。现有关键帧图片或取帧记录不等于已建立可检索的逐帧向量库。

#### 已确认的接续方向

1. **先确认 Event 级检索就绪及目标数据可检索。**由另一工作线交付实现，本构图实验不重复实现 Event 存储、索引或工具。2026-10-18 用户进一步授权本工作线调用现有补建入口，为 MyGO 13 集补齐 Event 索引；需要能够取得命中 Event 的身份、视频来源和原视频毫秒时间范围。
2. **通过 Event 检索定位更细的候选范围，再取得对应关键帧。**优先复用另一线交付的关键帧；若只提供 Event 和时间范围，则接现有原片取帧能力。保留 Event／视频身份、帧时间及图片路径，按镜头和画面去重；Event 不保证等于一个镜头，必要时取多帧核对。
3. **在同一批 Event 候选关键帧上接续构图实验。**对同一图库提取原图 embedding、PCP 类别分布、人物框图 embedding，比较单路与可调权重融合；分别评议构图类别，以及人物位置、大小和排列。原图 embedding 不替代人物框图特征。
4. **区分候选召回与构图排序。**先确认相似画面是否进入 Event 候选集，再评价构图特征是否把它排上来；候选池外的画面不能被当作排序模型失败。Chat 发现的例子作为候选线索，经人工核对后使用，不直接当作完整标准答案集。

依赖边界：早期 [Event 细粒度检索 TRD](../design/02-agent-harness/event-retrieval-trd.md) 的仅词法方案已成为历史记录；当前以 [Sub-Chunk 检索 TRD](../design/02-agent-harness/sub-chunk-trd.md) 和实际代码为准，独立 Event 索引包含文本、视觉向量及抽帧来源元数据。当前视觉向量由每 Event 最多 3 帧聚合，临时 JPEG 不持久保存，也没有建立逐帧 PCP／人物框图特征库。因此“Event 检索完成”表示可以接续定位和取帧，不能自动标为构图相似检索已完成。

恢复实验的条件：目标视频的 Event 检索可用，命中可追溯到原片时间并取得对应帧，形成相较 17 图更丰富且各方法共用的候选集；随后用固定查询和人工评议检查召回、排序及人物布局差异。当前不预先选定最佳模型、权重或宣称全库效果。

2026-10-18 运行记录：复用旧 Timeline，通过 `request-es-index-rebuild --with-events` 补建，13/13 集 COMPLETE、312/312 Chunk 覆盖、7,103 个 Event 可检索，包含 16,535 个帧采样记录（不等于独立画面数）。逐个核验 ES 身份／版本、文本投影、向量合同、抽帧时间与完整集合；所有原正文 ID／版本／哈希保持不变。全量“俯视房间”和“舞台演奏”两条 Event 视觉通道检索通过，覆盖 312 个来源 Chunk，partial=false、rejected=0；俯视查询召回第 7 集 `248600–249100ms` 的候选，落在先前 Chat 找到的范围内，但本轮没有以此宣称构图排序准确率。详见 [构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)。

性能补充：这轮功能补建暴露的历史写入放大为约 45 分钟 **20.61 万个 UPDATE_ROWS 事件、44.106 GB binlog**，严格按事件时间统计全天为 47.786 GB。Claude 已完成缓存批量保存、无变化聚合与配套扫描修复；完整计数、batch 教训、实施范围和七天留存措施见 **B14**。上述数字属于修复前基线，修复后的真实负载降幅未在本轮复测。

依据：[构图研究归档摘要](design-experiment-cleanup.md#构图模型与图库)、[Chunk／Event 视觉编码](../../server/service/video_parse_index_coordinator.py)、[Event 检索链路](../../server/service/video_event_retrieval.py)。实验目录沿用本地 ignore 规则；源码实现由另一工作线负责，本工作线维护构图实验与本轮获授权的数据补建、验收记录。

### C17：单集台词定位仍耗时两分半，串行角色与重复交接占主要开销

排序日期：2026-10-18。

日期：2026-10-18。优先级：高。类型：Chat 端到端延迟、重复整理与观测粒度问题。状态：**真实 trace 已核对，优化尚未实施**。用户要求先记录问题并交给新 Agent；本轮未修改生产代码、Prompt、模型配置或执行拓扑。

#### 固定样本、结果与证据

使用 Seekino 本机运行的默认 Chat，在独立 `mygo-ep04-speaker-review-20261016` Series（`bdbf28ae-5404-58e4-8c98-699433499ffc`）发送两条中文、存在意思偏差的问题；没有给 Agent 日文原句、说话人、目标时间或 Event ID。测试时该 scope 有 24 个 Chunk、430 个 Event，且带有 EP04 素材元数据，所以这是单集定位测试，不能当作跨全集找集数的验收。

| 样本 | 问题中的记忆表达 | 结果 | Conversation / Assistant |
| --- | --- | --- | --- |
| B，drift | “这又不关你的事，你少掺和” | 09:08–09:09 立希对爱音说「部外者は黙ってて。」；视频卡 09:00.000–09:14.200，说明并非逐字对应 | `ae105562-4b64-4067-b482-c4871beebfe0` / `cff08757-4a11-4f0c-bd18-1fc02595bf9f` |
| C，premise | “不懂音乐就别来指点”，原话可能记偏 | 20:03.2–20:08.7 爱音挤兑立希不懂吉他和弦；视频卡 20:01.7–20:11.2，明确纠正原句没有“不懂音乐”和“别指点” | `a33927a7-362a-45ee-824a-bf213fe64398` / `1b65aa2b-7582-484a-af0d-3c9a759de38e` |

两轮均完成，Query 自主使用 `search_video_events`，没有 Chunk `retrieve` 回退。首次可见文字分别为 7.3754 / 8.079 秒；目标 Chunk 的原始 transcript 在约 22 / 25 秒已读取，最终仍在 156.105 / 154.078 秒完成。获得文本不等于模型已完成理解或已足够交付，不能把后续时间全部判为无效。

2026-10-24 已将本轮输入、历史完整回答、阶段计时、审核反馈和关键工具失败摘要迁移到 [MyGO benchmark](../../evals/benchmark/query_capabilities/README.md#历史结论event)，B/C 的机器可读记录见 [baseline.jsonl](../../evals/benchmark/query_capabilities/baseline.jsonl) 中的 chat-event-drift／chat-event-premise。原始 SSE、完整日志、原生 rollout 和帧文件已退役；下文保留当时的发现与适用边界，不再提供逐字节日志取证。

#### 端到端耗时拆解

| 阶段 | B | C |
| --- | ---: | ---: |
| Supervisor 启动、理解并委派 | 9.2 s | 9.8 s |
| Query 第 1 轮 | 56.0 s | 29.4 s |
| Evaluator 第 1 轮 | 37.5 s | 24.4 s |
| Query 第 2 轮 | 无 | 32.5 s |
| Evaluator 第 2 轮 | 无 | 18.7 s |
| 内层 Chat 整理回答 | 31.1 s | 24.5 s |
| Supervisor 最终读取、改写与提交 | 约 22.3 s | 约 14.7 s |
| 服务端整轮 | **156.105 s** | **154.078 s** |

- **底层搜索很快。** B 的 Event 检索 1 次，731 ms；C 共 3 次，671 + 919 + 668 = 2,258 ms。Embedding 已包含在检索工具执行时间内，不再叠加。B 抽帧 4 次成功，Query 1 次、Evaluator 3 次，后 3 次命中缓存；实际只有 4 张独立图片。C 没有抽帧，不能将其描述为重新核验过音画。
- **主要时间在模型角色与角色内请求往返。** B 的 Query / Evaluator / Chat 工具执行合计分别为 1.284 / 0.105 / 0.022 秒，阶段却分别约 56 / 37.5 / 31 秒；C 对应工具合计为 2.308 / 0.035 / 0.020 秒。工具 duration 求和可能包含同一模型轮内的多个调用，并非严格独占墙钟；差额包含模型处理与等待、网络、SDK、启动和编排，不能全部称为“推理时间”。
- **父子耗时不能重复累计。** B / C 的 `search_video_graph` 124.619 / 129.762 秒包住 Query、Evaluator、Chat；外层 Supervisor SDK 运行时间也包住该工具。原生三次约 30 秒 `wait` 正在等同一个后台子流程，不是额外增加 90 秒。

#### 已确认的机制与仍待量化的贡献

1. **简单定位仍走完整拓扑。** Supervisor → Query → Evaluator → Chat → Supervisor。`continue` 回 Query，`complete` 必进内层 Chat；内层回答只作草稿，Supervisor 再核对文字 Timeline、对白、范围和审核，自己输出最终文字、提交视频。独立核验有其必要性，但 Chat 与 Supervisor 的两层整理是否都需要、交接是否过于间接，值得优化；不能直接撤销 C12 已确认的 Supervisor 终审与范围修订权。
2. **同一份证据重复导航与读取。** B 有 42 次业务工具调用，其中 33 次 Canvas 读取；C 有 39 次，其中 31 次读取。B 同一 transcript 被 Query、Evaluator、Chat、Supervisor 四个角色读取。工具调用数不等于模型请求数；原生 Supervisor 中存在一次 `exec` 顺序 await 多个工具的情况，不能仅凭同秒日志说成并行或每项各需一次模型请求。
3. **C 额外一轮由播放范围与附带主张引出。** 第一次 Evaluator 已确认核心事实，只因从 20:00 起播、依赖跨 Chunk 的未完整前句而要求修订；它也给了“从完整台词 20:01.7 开始并删去首句依赖”的局部方案。Query 仍补检索前块，最后采用局部方案，之后再跑完整 Evaluator；两阶段合计 51.227 秒。不是 ES 搜索了 51 秒，也不能承诺只删第三次检索就省 51 秒。Query Prompt 只强制首轮真实检索，后轮并未强制重搜。
4. **子角色会话独立启动。** 本次 Codex adapter 每次 Query / Evaluator / Chat invocation 使用新 ephemeral session / app-server；B / C 分别有 4 / 6 次 SDK 角色运行（含外层 Supervisor），不是其内部模型请求总次数。启动未单独打 span，尚不能量出应归因多少秒，也不能直接以复用会话替代角色／上下文隔离。
5. **单集 scope 不等于短上下文。** 两轮 Supervisor 初始业务 Prompt 约 16,037 / 16,015 字符，其中 `series_context` JSON 13,207 字符、`importantExtraData` 12,834 字符；Query / Evaluator 每轮也携带完整 series context。Supervisor 原生单次模型输入约 22.7K–28.2K tokens。几十万的 usage 是 SDK 多次请求累计且多数命中缓存，不能当成单个超长 Prompt，也不能把 native `world_state` 状态快照当成模型输入。背景可能有成本，不等于全部无用，更没有单独测得其延迟贡献。

#### 与性能修复一并保护的质量问题

- C 第二次 query 写入“立希打断爱音”的未经证实方向，真实证据却是爱音回敬立希。没有设置 speaker 硬过滤，后续精读纠正了方向。只能确定假设出现在 query 串，不能从缺失的模型转录断言其来自某段背景知识；与 C14 的假设偏移问题关联。
- 两轮最终用“你记得的是这段”指认来源，虽然解释了非逐字匹配，但没有排除其他近义场景；应区分“有支持的近义候选”和“唯一来源”。
- C 第一次全量 trace 中核心 Event 融合 rank=22，在回源后 matched 集合位置为 20；首屏只有 14 卡，Query 没有翻页，不能说它已看到目标。第二次 rank=11 才进入首屏，随后精读。底层召回、工具实际展示、成功读取和最终选择须分开统计。
- 另做的固定中文查询对照使用 0.65 / 0.35 / 0 权重，与自然 Agent 自主选择的参数不同；存在语义已召回但 RRF 排名被词法弱相关候选压低的情况，不将其混为“工具慢”或直接据三句改全局权重。具体 ID / 分数见 [独立检索分析](../../evals/benchmark/retrieval_quality/baseline.jsonl)。

#### 待评估方向、职责与验收

- Chat 编排负责评估是否减少 Supervisor 场景下内层 Chat 的重复润色，或更直接、有界地交接获批草稿与必要来源；保留独立 Chat 调用能力、Supervisor 终审与合法时间修订权。不预先选定“删 Chat”或自动信任子图为最终方案。
- Query / Evaluator 负责区分需要新证据的修订和已有证据足够的局部修订；核心对白完整即可支持问题时，不为了可有可无的前文扩大主张、触发重搜。不得以追求速度直接跳过证据审查或放行跨界片段。
- Gateway / 观测负责按现有日志与 usage 增补必要的单请求启动、发起、首个可用输出、结束、工具等待时间，区分 SDK 角色运行与内部模型请求；先确认现有埋点，避免新增表或复制日志系统。正文／工具展示与模型上下文继续解耦。
- 工具易用性另见 C18。先复用现有 batch、直接 section、可省略 end 的能力，不能以“缺 batch”为前提另起协议。
- 用相同素材与问题做改前／改后对照，记录模型绑定、scope / 索引版本、并发、阶段／请求／工具次数、错误恢复、首反馈与最终耗时；必要时顺序复跑以区分并发影响。保护说话人、原句纠偏、事件时间、完整且合法的播放范围、无结果正常说明、取消／失败／超时、流式展示与保存一致性。实际加速须真实模型验收，不能只靠 mock 或承诺省去阶段时长的全部。

证据边界：两轮在 2026-10-18 14:55–14:58（UTC+08:00）并行执行，没有同问题 Chunk-only 全流程或严格缓存／负载控制；不能据此宣布 Event 加速或形成稳定 P95。此 scope 的索引快照不代表当前全库覆盖；C16 的 MyGO 13 集补建由另一工作线维护。Query / Evaluator / Chat ephemeral 完整转录未保留，不能补造 prompt 或内部推理。当前先完成记录，不重跑、回填、重启、迁移或部署。

依据：[Runtime](../../agent/chat/video_search/runtime.py)、[Query](../../agent/chat/video_search/query.py)、[Evaluator](../../agent/chat/video_search/evaluator.py)、[内部 Chat](../../agent/chat/video_search/chat.py)、[Supervisor](../../agent/chat/supervisor.py)、[输入上下文](../../agent/chat/video_search/context.py)、[Codex adapter](../../gateway/codex/agent.py)、[Codex session](../../gateway/codex/client.py)、[B 运行诊断摘要](../../evals/benchmark/query_capabilities/baseline.jsonl)、[C 运行诊断摘要](../../evals/benchmark/query_capabilities/baseline.jsonl)。

### C18：Context Canvas 已具备 batch，但读取模式与范围语义容易误用

排序日期：2026-10-18。

日期：2026-10-18。优先级：高。类型：通用 Tool 可用性、参数合同与角色使用问题。状态：**能力已有、误用已复现，接口／提示优化尚未实施**。不能将“Agent 未正确使用”写成“没有 batch”，也不能据两轮就断言全部耗时都由接口设计造成。

#### 现有原理与能力

Context Canvas 是**执行期、位于模型消息之外的宿主状态**，以 collection → reference 保存条目，业务适配层把条目投影为 named sections 和目录。模型通过 Tool 按权限读取或修改宿主状态，不是把整份 Canvas 追加到每轮模型消息，也不是专门的 MySQL history table。

- 通用核心在 `agent/utils/context_canvas.py`：锁、快照／拷贝、revision、只读 collection / entry、原子 batch、角色与 batch_id 幂等；宿主／业务流程负责正式持久化，Canvas Tool 不直接决定业务数据库写入。
- `agent/utils/context_canvas_tools.py` 负责有界读写通道、角色可读集合、参数模式、分页和详情读取；Chat 的 retrieval / draft / approved / review / render 等业务 section、草稿校验和读审计在 Chat 适配层。Parse 也使用该通用能力，调整工具须核对两类消费者，不能把 Event、台词或 Supervisor 规则塞回 utils。
- 写入已支持带 `expected_revision`、唯一 `batch_id` 和 operations 的原子批次，整批失败无部分生效；本轮问题主要在读取，不默认改写写入合同。

| 读取模式 | 参数 | 范围与返回 |
| --- | --- | --- |
| collection 目录 | 省略 reference / references，section 默认 overview | 用 cursor 分页；不接受 start/end 范围 |
| 单个条目 overview | reference + section=overview | 返回 section 名与长度；不接受非默认字符范围 |
| 单个 section 正文 | reference + 明确 section | start/end 是字符偏移，end 不包含；省略 end 自动取实际长度，预算内可整段读取 |
| 多条目同 section 批读 | references=[...] + 明确的非 overview section | 同一 collection、多个 reference、同一个 section；start/end 是 references 的条目下标，省略 end 取列表长度；不使用 cursor |

工具说明已明确：**已知 section 可以直接读，不必先 overview**。现有 batch 不能在同一次请求列出“同一 reference 的多个不同 section”或任意混合 collection / section；不能因此说完全没有批量能力。单次模型往返内可以执行多个单项 Tool，它与 Canvas references batch、真正并行执行是三件事。

预算仍有区别：目录 `page_items` 按条数和 UTF-8 字节分页；单 section `range_text` 按字符／字节检查，超大范围要求缩小，不静默冒充完整；批量详情 `range_items` 对整个 JSON 回执再限制 8,000 o200k_base tokens，并结合字符／字节预算，返回 truncated / complete_end / msg（截断说明），未完整项不算已经精读。不能把“所有工具都限制 8,000 tokens”当作现状。

#### 两轮真实误用与可确认原因

- **B 没有使用 references batch。** Chat 先读过 overview，仍把 draft / selection_reason / evidence.transcript / evidence.metadata 四个 section 都按 `[0,8000)` 读取，实际长度为 359 / 1663 / 1866 / 1724，四次 INVALID_RANGE；随后按正确长度重读。end 本可省略。
- B 的 Supervisor 已知 review 为 847 字符，先漏写 section 却填 end=1200，命中默认 overview 的范围拒绝；再用 section=review、end=1000，仍越界；第三次才成功。两次失败服务执行仅 14 ms，额外成本来自错误反馈后继续请求模型，不能把整个 22 秒末段都归因于错误。
- **C 仅尝试一次 references batch，且失败。** Evaluator 传 collection=retrieval、references=[0044…]、section=overview、start=0、end=20，因 batch 不接受 overview 返回 INVALID_READ；即使换正确 section，一项列表也不能 end=20。Chat 另猜了 section=catalog，违反目录应省略 reference、使用默认 overview 的规则；两次错误均恢复。
- **真实可用性问题是多模式共用字段。** reference / references / 均省略切换不同操作，同一 start/end 在字符与条目下标之间切换，overview 又有额外组合限制。已有说明与错误反馈未让这些样本稳定使用正确方式，不能只责怪 Agent “没看说明”；也尚不能断言只改 Prompt 或放宽一个范围规则就足够。
- 日志同秒有多个单项调用，不代表它们并行。Supervisor 原生代码存在同一 exec 内顺序 await 多个调用；Query / Evaluator / Chat 的原生转录缺失，不能凭秒级日志补造并发方式或模型请求数。

#### 待评估优化与边界

1. **先核对模型实际收到的 schema、说明、角色 Skill 与错误回执。** 通用工具和 Supervisor 自定义说明须一致，强调已知 section 直接读、短内容省略 end、batch 的条目语义与续读方式；不要未检查就复制另一套 batch 工具。
2. **让常见正确读取少填参数。** 评估目录／详情／batch 的表达是否应更明确，避免同名范围字段切换单位；若调整 schema，先给最小方案、调用方影响和兼容策略，不预设必须拆多个 Tool、加入新 DTO 或新增业务“证据包”。已知 section、完整小段与预算受控续读应成为清楚的主路径。
3. **保留有界读取与证据完整性。** 不能为减少报错取消租户／角色权限、revision、只读来源、预算／截断标识、读审计或真实来源校验；若考虑自动收敛范围，须明确何时整段完整、何时部分返回、如何续读，不能静默截断后标记已完整核验。
4. **通用能力与业务职责解耦。** 通用模式、预算与读写机制只维护一份；Chat 决定哪些获批材料足够支持用户主张，Parse 决定事件正文与修改规则，Canvas 不理解这些业务含义。不要以优化延迟为由新建 table、扩大为全仓 Canvas 重写或改动 Event 索引／前端消息 Schema。

#### 接手验收

- 直接读取已知 section、单条省略 end、现有多 reference 同 section batch 都可用；未知 section／reference、目录与详情模式误用有清晰可恢复反馈。批读 end 是条目下标，单读 end 是字符偏移的现合同，若改变必须同步所有消费者与测试。
- 覆盖目录分页、超长单 section、批次完整前缀／截断／续读、token 与字节上限、只读／权限、revision 与原子写入重放；未完整项不能通过读审计。保护 Chat 与 Parse 两个消费者，定向回归，不借此全仓重构。
- 真实 Agent 用上述 B / C 等同 scope 问题验证是否消除 INVALID_RANGE / INVALID_READ，并减少不必要 overview、重读和模型请求；记录模型轮次与工具调用分开，既看首反馈／总耗时，也核对最终说话人、记忆纠偏和合法完整播放范围。不以假模型能按 schema 调用证明真实模型一定会用好。
- 本轮只记录问题；模型、背景裁剪、角色合并、读取协议或预算变化均未实施。优化效果和节省秒数等待具体方案与真实对照，不将 154–156 秒承诺为一定可压缩到某个时长。

依据：[通用 Canvas](../../agent/utils/context_canvas.py)、[读取参数与批读分支](../../agent/utils/context_canvas_tools.py)、[预算与范围](../../agent/utils/tool_output.py)、[Chat Canvas 适配](../../agent/chat/video_search/video_search_canvas.py)、[角色工具装配](../../agent/chat/video_search/tools/registry.py)、[Chat 工具适配](../../agent/chat/video_search/tools/canvas.py)、[读审计](../../agent/chat/video_search/tools/shared.py)、[Parse Refine 提示](../../agent/video_parse_v2/prompts.py)、[通用 Canvas 测试](../../tests/agent/utils/test_context_canvas.py)、[B 工具错误摘要](../../evals/benchmark/query_capabilities/baseline.jsonl)、[C 工具错误摘要](../../evals/benchmark/query_capabilities/baseline.jsonl)。

### B07：视频解析成本高昂且耗时超过片长数倍

排序日期：2026-10-17。

优先级：高。类型：解析性能、单位经济性与失败止损问题。状态：2026-10-12 已取得真实整集完成的耗时收敛里程碑；历史问题、已实施修复与仍待优化的边界按下列日期分别记录。

#### 2026-10-30：Understanding 直接 JSON 与输出上限落地

状态：TODO（代码与隔离回归完成，运行服务加载与实模效果待验）。dev 已去除 Understanding Canvas 工具循环，证据直接输入，宿主校验后原子写 Canvas；Refine 保留工具。默认及实际本机 YAML 的视觉主模型/fallback 已由 8,192 改为 30,000，旧任务快照不被覆盖。相关回归 **380 passed**，未重启、未调用实模，不宣称已量化提速。精简字段/SHOT 迁移仍是独立剩余事项。见 [TRD §3.3](../design/03-product-reassessment/video-parse-performance-trd.md#33-接入职责与失败路径2026-10-30-已实现直接-json) 与 [devlog](devlog.md#devlog-parse-direct-json-20261030)。

#### 2026-10-29：Karpathy OCR 配置偏差与失败恢复

状态：TODO（修复已部署，真实解析推进验证中）。本批原快照错误开启独立 OCR；已按用户原定关闭 OCR 的要求移除本机默认绑定，并通过正式 reparse 服务为 8 集创建 v2 快照，保留原视频与旧版本。另修 provider token 校准向下失效造成的新证据反复释放，以及父任务停止后子任务仍等待媒体并续租的问题。659 项定向测试通过；完整解析和索引成功仍需运行证据。详见 [TRD §3.6](../design/03-product-reassessment/video-parse-performance-trd.md#36-正式调用与恢复修复)、[devlog](devlog.md#devlog-karpathy-parse-recovery-20261029)。

#### 2026-10-29：补足多片段质量证据，身份缺证与模型优劣分开验证

状态：TODO（质量证据扩展；不重开已结束的协议选型）。用户指出，47 秒画外音在 K3 和豆包均失败，当前模型输入没有音频或可靠说话人身份，不能将此归因为豆包 non-thinking 质量低，也不能证明串行无法接 Refine。错误事实保留，音频/身份补证、模型配置、thinking 开关和串并行调度分别验收；匿名声音分组不自动等于具名角色，补证可前置或后置。

本任务 Agent 已将更长、多元样本计划补入 [主 TRD §4.7](../design/03-product-reassessment/video-parse-performance-trd.md#47-多片段质量验证计划分开模型能力身份补证与调度)：建议 6 个新连续 2 分钟片段、12 个 60 秒 Chunk，覆盖 6 类场景，首轮两模型一次筛查建议 24 次，先冻结样本/独立金标及预算。具体素材窗口、金标和后续消融预算尚未冻结；本次未新增调用或改生产。用户随后纠正架构基线：应比较携带实际前文、无强制后置 Refine 的轻量串行 workflow，与现有并行理解及其 Refine；独立调用按顺序发出不是前者。24 次模型筛查不回答完整架构优劣，该阶段另计完整成本及长序列质量。已知身份问题继续关联 [B12](#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属)，完整纠偏见 [devlog](devlog.md#devlog-parse-quality-attribution-20261029)。

#### 2026-10-29：协议选型结束，采用精简 JSON，质量优先

本轮协议选型：**DONE（研究与决策）**。用户已决定“精简了schema，但是不换xml，因为收益不明显”，并明确不为解析效率盲目追求单 Chunk 30 秒；接受既有可靠并行解析策略及其更高人力成本，认为足以满足当前效率需求。完整决定见 [主 TRD §1.4](../design/03-product-reassessment/video-parse-performance-trd.md#14-最终取舍精简-schema继续-json质量优先)、[决策 devlog](devlog.md#devlog-parse-protocol-decision-20261029)。

后续采用精简 JSON 与 SHOT 方向，不再自动续跑 XML 或为 30 秒目标裁剪视觉输入。两轮数据和失败保留，不把用户的工程取舍写成新测得的并行加速或质量通过。**生产接入与 B07 其余问题仍为 TODO**：由解析模块维护者落实新合同、宿主提交及下游兼容，核验既有并行策略下的内容质量、整集到索引可见耗时、完成率、恢复和成本；不以协议决策完成代替实施完成。

下列实验段落保留各自结束时的判断；其中“下一步”建议由本次最终取舍收敛，不再自动触发新的协议/输入预算实验。

#### 2026-10-29：精简字段并新增 SHOT 后，XML 仍未获得替换依据

状态：TODO。按用户意图取消 participants、visible_persons 与根级 persons，新增 SHOT；用相同字段的精简 JSON/XML 再执行 8 次。全部复用首轮图片、本地 Whisper 转写及背景，OCR 关闭，没有新增 ASR/OCR 调用。见 [第二轮报告](../report/agent/video_parse_v2/parse-compact-shot-20261029.md)、[主 TRD §4.6](../design/03-product-reassessment/video-parse-performance-trd.md#46-第二轮实际结果精简有局部收益xml-没有整体优势)、[devlog](devlog.md#devlog-parse-compact-shot-20261029)。

K3 JSON/XML 尝试耗时中位数为 118.52/144.57 s（XML +21.97%），豆包为 44.41/52.50 s（+18.21%，JSON 含一次合同失败）。8/8 completed，合同有效 JSON 3/4、XML 4/4；58 项定向测试、223/223 证据一致性检查通过。字段精简减少了 JSON 正文生成负担，但 XML 没有同字段性能优势，K3 前置等待仍大。

名单缺失不再计错；八份均有 SHOT 和具名对白，已知 47 秒爽世画外音仍为 0/8 正确，最快 JSON 还有跨人合并。保留精简合同方向，暂不切换 XML；下一步由本任务 Agent 完善主体/分段、镜头动作分工、画面文字，再冻结视觉输入预算实验。生产 Canvas tools、SHOT/旧数据/索引兼容及整集恢复尚未接入验证，不能关闭 B07。

#### 2026-10-29：单块协议对照完成，暂不切换到 XML

状态：TODO。EP04 原视频已恢复并核对为旧 SHA；本地 Whisper large-v3-turbo Q5 转写完成，本轮依用户要求关闭独立 OCR。K3 low、豆包 2.1 Pro disabled 各执行 JSON/XML 两次，共 8 次；完整结果见 [报告](../report/agent/video_parse_v2/parse-protocol-20261029.md)、[主 TRD §4.4](../design/03-product-reassessment/video-parse-performance-trd.md#44-2026-10-29-实际结果与取舍) 和 [devlog](devlog.md#devlog-parse-protocol-20261029)。

K3 尝试耗时中位数 103.22→100.17 s（−2.96%），未达探索门槛；豆包 98.73→64.68 s（−34.49%）包含两组各一次校验失败，且 XML 存在 speaker/participants 遗漏、跨人台词合并，不能作同质量提速。四份 XML 均为合法 XML，换行无解析问题；一份违反单主类型合同。两协议各 3/4 通过领域/范围校验，但画面文字、分类及已知爽世画外音身份仍不合格。缓存差异和小样本限制保留。

24 项离线测试、44/44 证据一致性检查通过；没有生产接线、数据库提交或整集验收。下一步由本任务 Agent 结合用户 Schema 意图收紧身份/分段合同，再做同字段精简 JSON 和视觉输入预算对照；不继续无上限重复当前合同。观察准备约 5.33 s 只代表模型已加载的一次本地 ASR，不能据此关闭 B07 或永久删除 OCR。

#### 2026-10-28：Understanding 输出协议与新机复测准备

状态：TODO，生产 Understanding 仍使用 Canvas tools。[解析性能主 TRD](../design/03-product-reassessment/video-parse-performance-trd.md) 已按 common principle 明确影响因素、计时/质量口径、事件包装 XML 与直接 JSON 对照及宿主 Canvas 接入边界。前序实验 decoder 的 24 项离线测试通过，只支持本地转换与内存往返；没有新模型或整集成绩。用户将在下载后提供原视频，旧验证输入与原文尚未恢复。下一步由本任务 Agent 校验素材、冻结观察与模型参数，先完成 8 次有界协议探索；正式替换仍需质量、性能及下游集成证据。历史约 31 秒不能视为本项完成，FFmpeg 常驻/warm-up 也未被证实为根因。决策与验证边界见 [devlog](devlog.md#devlog-parse-performance-20261028)。

#### 2026-10-17：Refine 引入 event 粒度三方合并

- EP04 多参考声纹任务 `dd55671d-a84b-4628-bedb-4007a4f4aa9c` 完整墙钟 38:43.518；22 个 Refine 窗口生成 54 次，32 次快照刷新。32 轮占 Refine 并行累计生成时间的 64.4%，不是可直接承诺的整集降幅。准备 Agent 回合 148.086 秒，与剩余 Understanding 重叠；Refine 950 次 CAM++ 调用累计 66.113 秒，不能将整集耗时归因于编码推理。
- 保留重叠窗口并发。提交时将输入原文 Base、数据库最新正文 Current 和本轮结果 Proposed 做三方合并：不同 Chunk／event 的独立修改保留，相同 event 的相同结果直接接受，正文未变不重复增加版本；同一 event 的不同结果仍刷新快照。
- 双方 timeline 都变化且结果不同时，只对唯一且顺序一致的 `(event_type, start_seconds, end_seconds)` 对应事件合并；新增、拆并、重排、改时间／类型或重复锚点无法可靠对应时保守冲突。persons 保持整列表三方判断，不扩展到同一 event 内的字段合并。
- 基准内容必须匹配输入指纹；未升版改正文与旧租约仍拒绝。声纹准备后按可信静态成果使用，timeline 编辑不再造成来源失效或提交拒绝。先验证独立 Proposed，再校验合并结果；并发组合导致 Schema 不成立时按快照冲突处理，所有成员及阶段推进仍原子提交。
- 定向回归共 585 项通过、1 项因专用 MySQL/ES 未配置跳过；包括 event 合并、真实冲突回滚、相同结果不重复升版、原始快照透传及现有声纹／索引门禁。EP04 24 份真实正文经新参数序列化后指纹一致；未执行真实 MySQL 多连接合并竞争实验。
- 取舍：普通上下文变化不再一概让独立修改重算，不保证模型读取依赖的语义串行一致性。当前没有再次重启或整集重解析，尚无合并后的真实耗时对照；不宣称这 32 次全部可以自动合并。

#### 2026-10-16：EP04 已完成，但重叠 Refine 仍反复刷新快照

- 隔离任务 `a94be537-6385-4ba9-b99b-ca63477f5982` 最终 `COMPLETE`，理解 24/24、Refine 22/22、索引 24/24；有效处理耗时约 14:41，起止墙钟约 14:45。语义结果并未通过说话人归属验收，详见 B12。
- 139 个唯一完成 call ID 包含 24 ASR、24 Understanding、43 Refine、24 文本 Embedding、24 视频 Embedding；22 个滑动窗口产生 21 次输入快照刷新，与 21 次额外 Refine 调用对应。
- 最慢窗口 `seq17` 覆盖 Chunk 17–19，`21:05:18–21:12:56 +0800`，执行日志 `elapsed_ms=457916`。同一 lease 内完成 5 次 Astra invocation，4 次因成员版本／内容指纹变化重新生成，`retry_count=0`；不是供应商错误重试，也不受 `max_retry_count=0` 自动阻止。
- 提交快照校验是在防止旧结果覆盖，不能删除它消除表面冲突。待处理仍为共享成员的 Refine 调度／写入策略，保留不重叠并发；本轮未修此逻辑。对单次 invocation 的 timeout 也不能当作整窗口的总上限。
- Codex 记录来自 `codex_thread_total`，包含缓存输入；不能把其累计 Token 直接当作新增付费 Token 或账单金额。

依据：[本轮报告](../../outputs/verification/mygo-ep04-speaker-review-20261016/report.md)、[逐次调用与冲突](../../outputs/verification/mygo-ep04-speaker-review-20261016/latency-summary.json)、[快照校验](../../server/repository/video_parse.py)、[Refine 重算循环](../../agent/video_parse_v2/refine/agent.py)。

#### 2026-10-12：整集解析耗时收敛里程碑，MYGO EP03 完整完成

状态：有界正文批读与索引版本幂等策略已实施，真实整集的理解、Refine、索引发布均完成，当前长耗时主路径已显著改善。此处的“解决”对应本次任务正常完成及下列已实施行为，不表示所有并发重算、短错误或语义质量问题都已消失。

##### 最终运行证据与时间口径

- Parse Task：`20b05d4c-becf-4fce-b744-0d3ac8f2f8e1`，MYGO EP03；父任务 `attempt_count=1`，终态 `COMPLETE`。理解 **24/24**、Refine **22/22**、索引 **22/22**，完成单元 **68/68**。索引完成意味着当前成员版本已通过发布／查询可见性校验并持久提交，不只是 SDK 返回 completed。
- 此前卡在索引的 EP01（`d470e300-9813-4c19-a62e-f52503e2e1ae`）与 EP02（`5584368a-3d77-4fad-b11b-3b8c3f9e249b`），当日 14:49 的已采集快照也确认均为 `COMPLETE`、索引 22/22。它们保留历史失败执行，时长完整性仍为 false，不能把旧累计值当作本次单轮完整基准。
- 开始：`2026-10-12 14:45:27.792 +0800`；完成：`15:13:43.821 +0800`。墙钟 **28 分 16.029 秒**；全链有效执行区间并集 **28 分 13.979 秒**（`1693979ms`），账本 `complete=true`、所有执行已闭合，页面按整秒显示 **28 分 13 秒**。
- 仅 Understanding 与 Refine 的执行区间并集为 **26 分 59.496 秒**。用户提到的“26 min”与这一口径对应；它不等于含 Index 的全链耗时。Index 在解析结束后增加的尾段为 **1 分 14.483 秒**。
- 本轮冻结模型为 `gpt-6-astra`、`reasoning_effort=low`，原生 invocation deadline 仍为 600 秒；视觉输入为 `ordered_frames`，最多 32 帧、max_height=720。没有通过扩大 timeout、自动重试或 fallback 预算来解释这次收敛。
- 用户回顾的整集体验为约 **90 分钟 → 异常样本约 110 分钟 → 优化后仍有错误的约 37 分钟 → 本轮解析主体约 26 分钟**。这些包含不同集数、执行轮次、错误重试及记录完整性；不能当成同片、同配置的严格 A/B 加速比。本轮完整有效时长以 **28 分 13.979 秒**为准，也不要与下面 2026-10-09 的“90 秒视频耗时 37 分钟”样本混用。

| 本轮阶段 | 执行记录 | 阶段时间区间并集 | worker 累计工作时间 |
| --- | --- | ---: | ---: |
| 完整 chunk（含 ASR、理解及宿主执行） | 24，全部 completed | 16 分 27.492 秒 | 52 分 04.593 秒 |
| Refine lease | 22，全部 completed | 19 分 22.296 秒 | 51 分 01.657 秒 |
| Index（含 embedding、媒体准备与 ES 发布） | 24：22 completed、2 failed 后恢复 | 3 分 08.736 秒 | 8 分 14.165 秒 |

阶段之间并行交错，**三个阶段并集不能直接相加**；worker 累计量也不是整集墙钟时间。

本轮 ModelUsage 共 **169 条，全部 completed、0 failed**：

| 调用 | 次数 | 单次平均 | 单次最长 |
| --- | ---: | ---: | ---: |
| ASR | 24 | 27.763 秒 | 43.439 秒 |
| Codex Understanding invocation | 24 | 96.058 秒 | 117.180 秒 |
| Codex Refine invocation | 42 | 72.849 秒 | 104.083 秒 |
| 文本 embedding | 37 | 0.206 秒 | 0.534 秒 |
| 视频 embedding | 42 | 7.400 秒 | 9.587 秒 |

Codex invocation 包含整个 native agent 执行，不等于一次底层采样。Refine lease 平均为 139.166 秒、最长 370.275 秒，包含窗口内多次 invocation；不能与上表单 invocation 的平均值混用。Embedding 本身为亚秒／秒级，Index 总时长还包含非模型工作，不能把索引拖尾都归因于 embedding 模型慢。

##### 已实施的解决方案

1. **Canvas 支持有界批量读取正文。** `get_context_canvas` 接受 `references=[...]` 与明确 section，按零基 `start/end` 项范围读取（end 不包含），批读不使用 cursor；已知正文 section 可直接读取，省去每个事件先 overview 再 content 的双重调用。整个 JSON 回执限制为 **8,000 o200k_base tokens**，预算不足时裁剪后续项并返回明确 warning／truncated／complete_end，要求缩小读取范围；简略项不能当作已完整核验。写入继续复用既有原子批次，保留 revision、issued references 和提交快照校验。减少逐项调用也减少同步 PostToolUse 的触发次数；没有关闭或修复托管 hook 内部实现。
2. **索引以身份、版本、完整性和可见性决定完成。** 对当前正确版本的 ES 文档直接验证、复用，跳过重复媒体构建与 embedding；缺失或较旧版本才发布，较新版本拒绝旧目标覆盖。修正了“同版本缓存数值差异 → 重写相同 external version → 失败重放”的决策路径，同时保留租约、SQL 当前版本和 ES 外部版本防护。精度保存与差异来源研究独立保留，不将新 JSON 编码、重新生成向量或推进 generation 作为所有任务恢复的前提。
3. **总有效时长覆盖完整业务链。** 使用同一 execution_timing 账本记录 Understanding、Refine、Index 的实际执行区间，取并集；失败执行计入，排队、停机和等待重试排除。Index 全量完成或部分发布释放租约后，已观察到的计时样本仍可安全闭合；未观察到的历史执行不补造，记录不全时返回 `complete=false`。ModelUsage 保留单次模型调用起止，独立 EMBD 时长栏仍未在任务页展示。
4. **索引阶段进度权重改为 5%。** 理解与 Refine 在其余 95% 内按完成单元分摊；当前固定拓扑的公式为 `95 × (completedChunks + completedRefines) / 46 + 5 × indexedIntervals / 22`。理解与 Refine 全提交为 95%，索引 1/22 为约 95.2%，全部提交为 100%。原始完成单元计数仍为 68/68，百分比不再等于完成单元数除以 68。5% 是固定展示权重，不是 ETA 或精确的模型耗时比例。

##### 尚未解决的边界

- **Refine 并发快照冲突仍存在。** 本轮记录 **20 次冲突，涉及 10 个窗口**；42 次 Refine invocation 对应 22 个完成窗口与 20 次冲突重算。当前领取逻辑尚未排除共享成员的有效 Refine lease，不能记录为已实现互斥调度或已消除重复核验。此次没有模型超时，不代表未来不会再次超过 600 秒。
- **本轮并非零错误。** 两次 Index 执行分别在 96ms、47ms 后以 `VideoParseIndexCoordinationError` 失败，各恢复一次后完成；它们不是模型 timeout。最终 last_error 已清空，日志未保留具体错误消息，不能补写更细原因。
- 同版本缓存并发、重复 embedding 与批读实际调用次数的进一步优化仍需各自证据；托管 hook 内部等待原因、旧 Pro 空输出问题未被本次成功解决。完整完成与可检索状态也不替代人物、对白、动作等语义质量的独立验收。

本轮权重调整的离线验证：后端 Job Repository **32 passed**，TasksPage **18 passed**，覆盖 95% 解析／5% 索引、持久成果计数、未知拓扑及完成后 100%。没有新增真实模型调用、任务重试、服务重启、数据库迁移、commit 或 push；上述 EP03 运行证据来自本轮只读核验的既有任务。

依据：该 Parse Task 的最终 API 快照、ModelUsage／execution_timing 一致快照，以及 `outputs/local-services/api.log` 中 `14:45:27–15:13:43 +0800` 的记录；[Canvas 批读工具](../../agent/utils/context_canvas_tools.py)、[Refine 读取指引](../../agent/video_parse_v2/prompts.py)、[索引版本策略](../../server/service/video_parse_index_coordinator.py)、[ES 就绪验证](../../server/storage/elasticsearch.py)、[执行计时](../../server/service/video_parse.py)、[计时持久化与快照提交](../../server/repository/video_parse.py)、[进度权重](../../server/model/job_progress.py)、[任务汇总](../../server/repository/job.py)。下方历史条目及 B08 中“尚未恢复”的描述保留其采集时点，真实恢复与新集完成的最新结果以上述日期条目为准。

#### 2026-10-11：Refine 单项读取反复等待原生 PostToolUse，耗尽整次执行预算

优先级：高。状态：已定位一个超时样本的主要等待环节，待修复；托管 hook 内部为何耗时数秒仍未确认。以下为当日已采集的运行证据，不表示当前任务实时状态，也不替代后面的历史模型与 Pipeline 结论。

##### 现象与实际执行路径

- Parse Task：`d470e300-9813-4c19-a62e-f52503e2e1ae`；Video：`e709a2d8-aabb-509e-b323-e7f9aa7018b2`。本轮实际使用 `codex_local / gpt-6-astra`、`reasoning_effort=low`；Refine 跟随 Understanding 模型绑定。
- Gateway 对 native adapter 直接执行 `CodexAgentModel.generate()`，走 Codex 原生 app-server agent；Server 传入 `tool_harness="deepagents"` 不代表本轮经过 DeepAgents 图或其 hook。
- 当前 `request_timeout_seconds=600` 作为整个 native invocation 的 deadline，覆盖会话启动、模型生成、工具调用、工具后的 hook 等待和结果返回。到期由 Seekino 终止 session，并报 `Codex Agent execution deadline expired`；不是一次供应商 HTTP 请求必然等待了十分钟。
- 已观测到的主要模式是逐项读取之间的同步等待：Seekino 已将成功工具回执交给 Codex，调用工具的 Agent 脚本仍要等 PostToolUse 完成，才能拿到结果并继续下一次读取。

```text
Agent 脚本请求 get_context_canvas(item 1)
    → Seekino 执行读取，将成功结果交给 Codex
    → Codex await PostToolUse hook
    → hook 完成后，结果才交回调用脚本
    → 脚本请求 item 2，重复上述过程
    → 整次 invocation 达到 600 秒 deadline，尚未形成持久提交
```

##### 完整超时样本与耗时口径

样本为 Refine `[19,22)`、`lease_version=2`，invocation `37723952-ebd2-4143-af64-db313743b4f7`。时间为 `2026-10-11 +0800`；原始采集于 `16:48:33` 核实覆盖整次 invocation，native 日志后续滚动不能视作仍可完整重放。

| 指标 | 已采集结果 |
| --- | --- |
| invocation 起止与耗时 | `16:10:06.460–16:20:06.436`，`599.976 s`，最终超时 |
| 成功事件读取 | 127 次：64 次 overview、63 次 content；涉及 64 个唯一事件，最后一个只读到 overview |
| 相同路径重复读取 | 0；该样本不是反复读取同一对象的模式 |
| 已闭合 hook | 126 段，时间并集 `481.954 s`，占整次 invocation 的约 `80.3%` |
| event 回执中段 | 首末成功回执跨度 `483.459 s`；其中闭合 hook 并集 `481.954 s`，其余合计 `1.505 s`，尚未细分读取、传输与推进 |
| 写入与提交冲突 | 采集范围内未观察到写入或 snapshot-conflict；该次超时发生在持久提交前 |

`481.954 s` 是 hook/started → hook/completed 生命周期区间的并集，不是 Canvas 执行时间、handler CPU 时间或已证实的网络等待。另有末尾未闭合 hook，未计入该值；整次执行其余约 118 秒尚未完整分解，不能直接标为模型推理时间。读取属于业务核验，不能把整次 600 秒都命名为“无效时长”。

##### 源码与本机配置确认的机制

1. **工具回执与脚本取得结果之间存在同步 hook。** 已安装 SDK 与 bundled CLI 为 `0.156.1`；对应官方 `rust-v0.156.1` 源码中，registry 先 await 业务工具，再 await PostToolUse，最后才返回结果。code-mode 内部工具调用也经过这条路径。PostToolUse 可以追加反馈或阻止原始结果继续到达脚本，因此同步方式会等待其决定；不是工具一返回就立即恢复脚本。
2. **这类动态工具默认串行，范围是同一个执行 runtime。** DynamicToolHandler 没有覆盖 `supports_parallel_tool_calls`，默认值为 false；同一个 code-mode turn worker 的 nested calls 共享 ToolCallRuntime，其写锁覆盖工具 dispatch 和后面的 PostToolUse 等待。不是按工具名称分别建锁，也不是四个独立 Refine process 共用全局锁。仅把单项调用改为 `Promise.all` 不会减少 hook 次数，在该版本的同一 worker 中仍受串行门禁约束。
3. **当前存在匹配全部工具的同步托管命令。** 本机 `/etc/codex/requirements.toml` 启用 hooks，PostToolUse 的 matcher 为 `.*`，命令为 CorpLink Resources 下的 `corplink-bridge`，timeout 为 5 秒，未设置 async，按同步方式执行。Codex 的命令 runner 启动外部进程、传入事件 JSON，并等待输出与进程结束；5 秒是命令执行超时配置，不是固定 sleep，也不是整个 Refine 的预算。采集中的通用 hook 事件缺少逐条 handler 关联，不能声称 126 段已逐一证明均属于这个命令。
4. **读取契约放大调用次数，写入已有批次能力。** `get_context_canvas` 已支持目录 list/分页，但单次正文读取只接受一个 reference；工具说明引导先 overview，再读取具体 section。样本中 64 个事件由此产生 127 次成功读取。`write_context_canvas` 的 timeline 写入已经支持 `operations[]`，不能记录为读写都只能逐项，也不需要据此重写整个 Canvas。

##### 尚未确认的原因与独立问题

- CorpLink 安装资源只有编译后的 `corplink-bridge`，没有可读 handler 实现。当前只能把等待定位到 Codex 的同步 hook 生命周期；其内部是否等待 IPC、其他系统服务、网络、队列或进程启动／退出，均未证实，不能凭名称断言是在做内容审核或模型调用。
- 没有证据证明该样本是“大量结果已经计算完，再堵在一个全局返回队列”。观测到的是前一次读取完成后等待 hook，再继续下一项；源码另确认同一 runtime 中非并行动态工具的执行门禁。
- 其他窗口已独立证实存在共享成员被相邻 Refine 修改、提交版本／fingerprint 冲突，然后在同一 lease 内重新核验的重复工作。该机制继续成立，但不能解释本样本在提交前耗尽预算；同样不能把这里的 `80.3%` 外推到全部 Refine 超时或整集墙钟时间。
- invocation 超时后临时 Canvas 未持久提交，既有队列重领时重新创建 Canvas 和原生 thread，无法直接续接此前读取进度。提交冲突后重新 invocation 也会重新获得单次 deadline，因此一个 Refine lease 可以超过十分钟。SDK completed 不等于成果已持久提交。

##### 待处理方向与验收

- 优先在现有读取契约上支持多个 references 或按 member 的有界正文批量读取；已知 section 时可直接读取正文，保留 issued references、revision、输出大小预算与分页，减少实际工具调用和 hook 触发次数。不能用无界全量输出代替分页，也不能把 `Promise.all` 当作同等修复。
- 补齐 task／interval／lease／invocation／tool call 与 hook handler 的关联、起止、回执、错误及超时状态、Canvas revision 和最后有效动作；区分业务读取、hook 生命周期与 handler 内部阶段，不记录模型私有推理、credentials 或媒体 base64。托管 handler 内部延迟需要其处理方的执行证据。
- 调度层另行评估领取 Refine 时避开正在执行的共享成员窗口，保留不重叠并行和提交快照校验；不能删除版本校验掩盖冲突，也不强制无需修改的 Refine 制造更新。
- 先以 fake provider + 真实 Canvas／queue／repository 离线验证批读结果和边界、调用次数下降、无修改正常完成、已提交成果不重复执行，以及不重叠窗口并行。批读后的真实耗时与质量尚未验证，不承诺仅修这一处就能整集完成；总 deadline、自动重试及 fallback 调整需独立评估。

依据：[诊断 HTML 与样本明细](../../outputs/reports/refine-timeout-20261011.html)、本机 `outputs/local-services/api.log`、原采集的 `/Users/bytedance/.codex/logs_2.sqlite` 中 `codex_app_server::outgoing_message` 记录，以及 ModelUsage／execution_timing 快照；[Canvas 读取工具](../../agent/utils/context_canvas_tools.py)、[timeline 批次写入](../../agent/video_parse_v2/timelineEvent_canvas.py)、[Codex adapter](../../gateway/codex/agent.py)、[Refine agent](../../agent/video_parse_v2/refine/agent.py)、[领取逻辑](../../server/mq/video_parse_queue.py)、[提交校验](../../server/repository/video_parse.py)。原生机制以对应 release 的官方源码为依据：[registry 的 tool → PostToolUse → 返回](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/registry.rs#L624)、[dynamic handler](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/handlers/dynamic.rs#L82)、[默认并行设置](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/tools/src/tool_executor.rs#L112)、[执行锁范围](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/parallel.rs#L181)、[code-mode 共享 runtime](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/code_mode/delegate.rs#L432)、[命令 hook runner](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/hooks/src/engine/command_runner.rs#L193)与 [OpenAI 官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)。

#### 2026-10-09：ParseV2 的 90 秒视频耗时 37 分钟

状态：功能及相对 ASR 的秒级时间链路已通过；人物名单校验已按用户明确规则缩小至 `DIALOGUE`。理解/精修输出预算与思考控制已完成源码、兼容性、离线和同片真实通路验证，约100.3秒完成；ASR 保持原设置。视觉事件输出变少，完整语义质量未验收。失败恢复、总时限和退避配置仍待处理。以下37分钟耗时与失败记录对应修复前冻结源码。

本次使用同一 90132ms 原片、原默认配置，在隔离 namespace `seekino_asr_reparse_20261009_081508_test` 重跑 task `d3046375-9d70-4a8d-972d-98fd6429f519`。服务运行开工时 `c8c5d2c + dirty` 的 539 文件冻结源码，模型、fps、重试和 fallback 没有临时调整；后续并行工作区变化未加载到该服务，以下根因严格对应本次运行。

**并行已经生效，但最慢 Chunk 的连续失败重试决定了总耗时。** 两段几乎同时开始，第二段约 9 分 57 秒完成，第一段因三次主模型失败及恢复花了 30 分 46 秒。唯一 Interval 需要两段均成功，随后才执行跨段精修；增加并发槽位不能缩短同一 Chunk 内依次发生的失败尝试。

| 关键路径 | 耗时 | 实际结果或依赖 |
| --- | ---: | --- |
| 第一段首次 Pro 理解 | 574.969 秒，约 9 分 35 秒 | Provider 返回 JSON，程序人物引用校验失败 |
| 第一段第二次 Pro 理解 | 653.048 秒，约 10 分 53 秒 | Provider 返回 `length` 不完整终态 |
| 第一段第三次 Pro 理解 | 397.302 秒，约 6 分 37 秒 | Provider 返回 `content_filter` 不完整终态 |
| 同轮 DeepSeek fallback | 7.276 秒 | 成功提交第一段 |
| 第一段 ASR、退避及准备 | 约 213.454 秒 | ASR 执行三次，另有两次退避 |
| 跨段精修模型 | 343.738 秒，约 5 分 44 秒 | 等待两段成功后执行并提交 |
| 其余领取、阶段交接、发布及轮询 | 约 23.211 秒 | 两段向量和 ES 发布完成 |
| **总墙钟时间** | **2212.998 秒，36 分 53 秒** | **08:16:08.318–08:53:01.315 UTC，最终 COMPLETE** |

第二段的理解与第一段首轮重叠，不能把全部模型请求耗时相加作为墙钟时间。完整关系是 `max(两段各自耗时) + 跨段精修 + 索引发布`，再计入准备与交接。第一段三次 Pro 失败合计约 27 分 5 秒；当时校验规则下均没有被接收的完整业务结果。

本轮 SQL/API 为 **14 次调用、443098 Token、missing0**，三次失败保留 **167520 Token，占本轮约 37.8%**；其中 failure output 为 85314、reasoning 为 75101。Token 按供应商实际用量保存，业务校验或供应商终态失败不会撤销已消耗的 Token。本节不估算金额，不混入前轮 99 条账本或下述 ParseV1 费用快照。

#### 三次长调用为何最后失败

1. **首轮是完整 JSON 的业务引用失败。** `persons` 已登记 `Kaname Raana`，但 `timeline[17]`、`[22]`、`[29]` 的 `visible_persons` 分别写成 `Kaname Raana (hand)`、`Kaname Raana (hand/guitar)`、`Kaname Raana (guitar)`。模型把身体/道具附注放进了人物引用字段，顶层 validator 在第一处未知引用立即抛错；供应商的 JSON Schema 结构约束没有保证跨字段引用一致。只在内存副本修复第17条再校验，仍暴露第22条，所以不能描述为“只有一个错误”。本次没有更改 raw 或历史结果，未证明其余内容都高质量。
2. **第二轮是供应商输出预算耗尽。** 实际 output32768，其中 reasoning29016、其余3752；供应商返回 `length`，没有可供业务接收的完整成功 JSON。Responses 的输出上限同时计入回答和思维链，思考占掉约88.5%的预算；不是程序拿到完整合法结果后随意拒绝。依据：[官方 Responses 参数说明](https://docs.volcengine.com/docs/ark/create-model-responses-api?lang=zh)。
3. **第三轮是供应商内容过滤终态。** 实际 output19829，其中 reasoning19581；Provider 返回 `content_filter`，程序按失败处理并调用已有 DeepSeek fallback。没有完整 raw JSON 用来判定具体触发内容，不能把它追认为本地人物校验失败或猜测过滤原因。

三次都在请求结束后才能确认最终结果。本次可定位到远程请求耗时和大量 reasoning Token，但没有供应商内部指标来分摊排队、推理与生成时间，不能把全部耗时都声称为本地校验或网络等待。

#### 本次确认的执行策略缺口

- **该冻结轮尚未接入 Ark 思考控制。** 五次 Pro 请求（理解与精修）output140100，其中 reasoning122954，约87.8%。当时 Ark 多模态适配器不接收或发送 `thinking` / `reasoning`，主路也未显式设置结果输出上限。下节源码已接入控制；实际关闭使用 `thinking.type=disabled`，不能仅在配置中填写字段就声称已控制推理预算，具体质量影响须同片验证。[官方说明](https://docs.volcengine.com/docs/ark/deep-thinking?lang=zh)
- **理解失败重跑整个 Chunk。** ASR 结果仅在单次 graph 内存中，重新领取失败 Chunk 后重跑 ASR、媒体准备与理解。本次两次额外 ASR 合计74.30秒，没有阶段成果复用。
- **fallback 只接住 `ContentFilterError`。** 人物引用校验失败和 `length` 不进入备用路径，而是先重试整个 Chunk；第三次 `content_filter` 才在同一次尝试中切换到 DeepSeek。程序缺少按结果错误类型选择定向修复、阶段重试或 fallback 的恢复策略。
- **600秒不是请求总墙钟上限。** 该值传给 SDK 网络 timeout，SSE 循环没有独立总 deadline；本次653.048秒请求没有在600秒结束，最终仍为 `length`。后续实现总时限时须沿用既有 cancellation/client close，并等待线程收尾，不能只取消 asyncio 等待而让底层请求继续消费 Token。
- **任务退避配置没有用于 Chunk consumer。** 快照为初始1秒、最大30秒；实际 consumer 使用 MQ 初始30秒、最大900秒，因此两次退避为30/60秒，加调度准备后间隙35.69/66.34秒。Prefix 的任务级退避与 Chunk consumer 的队列级退避不能混称为同一有效策略。

#### 人物引用校验修复与离线验证（2026-10-09）

用户明确：只有 `event_type=DIALOGUE` 时，人物引用需要匹配当次 `persons`；其余六种事件不受人物名单限制。仅看到吉他或手部不能据此判断人物归属。实现已在所有事件共用的时间排序检查后，仅对 `DialogueEvent` 执行名单校验；非对白的 `participants` / `visible_persons` 原文保留，不删括号、不补人物、不推断物体归属。对白的 `speaker=null`、非空 speaker 属于 participants、人物名单引用，以及所有事件的结构、时间和顺序检查继续生效，静态 JSON Schema 未变。

回归验证：新增六种非对白事件 × 两个人物引用字段的12项原文保留/JSON往返用例，另有1项混合事件排序用例；13项在旧实现下均失败。修改后 `.venv/bin/pytest -q tests/engine/parsev2 tests/agent/video_parse_v2/test_framework.py` 为82项通过，`.venv/bin/pytest -q tests/engine/test_index_projection.py` 为17项通过，合计99项通过；独立只读审阅通过。

使用首轮调用 `84a7dc7a-7031-48f0-b241-d1966e833328` 的未修改 raw JSON 离线重放：38条事件通过完整 `VideoParseResult` 校验及 `[0,60000)` 时间范围检查；第17、22、29条均为 `ACTION`，原附注及全部正文/时间完整保留。原始证据文件内容不变，没有新模型调用或数据库写入。验证记录为 `/private/tmp/seekino-dialogue-persons-20261009/raw-replay-verification.json`；本项结果说明旧名单阻断已解除，不代表重新完成真实全流程或确认模型描述中的人物归属。

#### 理解/精修默认预算与思考控制修复（2026-10-09）

用户明确批准仅执行理解和精修接线，ASR 保持原设置。`models.vision` 新任务默认为 `max_output_tokens=65536`、`reasoning_effort=none`，经 Gateway 配置、Server profile 和固定 V2 任务快照进入实际 Ark 请求；未单独配置的 refine 继续复用 understanding binding。Vision 配置校验允许更大的显式预算，默认值与最大可配置值分开。没有调整 ASR、Chat、备用模型、并发、重试或数据库 Schema。

Ark 结构化与流式路径均处理控制值：`none` 显式发送 `thinking={"type":"disabled"}`；`None` 完全省略控制参数，保留旧任务原行为；其它支持的值发送 `reasoning.effort`。旧固定任务不会回读新默认值。新增 Server Vision 可选字段未设置时，仅省略其 snake/camel 序列化键，保护旧显式上传请求的幂等指纹，其它 nullable 字段保持；V2 任务的原有 reasoning 字段仍正常持久化。OpenAI Vision 对不支持的非 `none` 值在 YAML 边界拒绝，备用模型保留独立配置。

离线验证覆盖配置传播、旧快照恢复、实际 SDK 请求参数、refine 继承与上传幂等兼容：`.venv/bin/pytest -q tests/gateway/test_config.py tests/gateway/test_model_config_v2.py tests/gateway/test_models.py tests/gateway/test_usage.py tests/server/test_config.py tests/server/test_video_parse_worker.py tests/server/test_upload_service.py tests/engine/parsev1/test_model_sources.py tests/engine/parsev2 tests/agent/video_parse_v2/test_framework.py --tb=short`，**573 passed**；仅有既有 Starlette/AnyIO 弃用警告。修复后的独立只读源码复核通过。

第一次实测 namespace `seekino_parse_budget_20261009_121056_test`、task `75501ba3-6d8d-410c-a653-bc2dc134c4b6` 在 ASR 供应商连接阶段即失败；恢复时数据库只保留4条 failed candidate_transcript，tokens 均缺失，父任务仍为 PARSING。进程及临时目录已消失，不能把该轮作为性能或端到端通过证据，也不能将缺失用量当零。已恢复原 MySQL/ES 数据目录；按原第6集680秒起点与相同 ffmpeg 参数重建素材，大小9999051、SHA256 `ddbecf396cb32193e99c5168428adf97b6cdd6627103d534a69dc0da36f2e4b5`、精确时长90.131708秒与原记录完全一致，取整为90132ms。恢复后的新一轮证据保存到持久目录 `outputs/verification/vision-controls-20261009-resumed/`。

恢复后 task `f8e59ff7-1c05-4ec4-b788-c742353aa6eb`、namespace `seekino_parse_budget_resume_20261009_132118_test` 使用 `29b8051 + dirty` 的542文件冻结源码、原 Series 元数据和字节相同的素材。新旧 profile 与固定任务快照只差 Vision 的两个控制值；没有请求级配置覆盖。父任务、两 Chunk 和唯一 Interval 全部 COMPLETE，Chunk/Interval retry均0。提交到轮询观测终态为 **100.285秒**（13:22:03–13:23:44 UTC），其中 coordinator 观测时长95.470秒；轮询时间与处理时间分别保留，数据库 `processing_duration_ms=0` 不用作有效耗时。

| 同片观测 | 修复前冻结轮 | 本轮 |
| --- | ---: | ---: |
| 总墙钟时间（含终态轮询） | 2212.998秒 | 100.285秒 |
| 模型调用 | 14 | 10 |
| Total Token | 443098 | 193985 |
| 失败调用 / Token | 3 / 167520 | 1 / 27885 |
| 主路理解/精修 reasoning Token | 122954（5次） | 0（3次均明确报告0） |

本轮墙钟减少约95.5%、Token减少约56.2%，是一次实测对照，不保证未来请求时长，也不是只切换一个参数的因果实验：源码还包含已完成的人物校验及宿主改动，模型输出和精修工作量不同。两段仍并行：ASR远程请求分别37.468/18.540秒；第一段 Ark理解17.965秒后返回 `content_filter`，同次尝试的既有 DeepSeek fallback 成功，理解合计25.261秒；第二段 Ark理解32.736秒成功。精修远程请求3.048秒，索引发布16.392秒。本轮没有 `length`、人物校验失败或整个Chunk重跑。

实际 SDK 白名单证明三次 Ark理解/精修均发送65536和 `thinking.type=disabled`，ASR两次仍省略这两项控制。10条SQL用量均来自供应商，input187142、output6843、total193985，API逐项一致、missing0；内容过滤失败的27885 Token同样保留。ASR reasoning分别418/311，四次embedding未报告reasoning，因此全局reasoning聚合仍为null，不将未知当0。10个used call ID与账本集合相同，active调用与全部执行租约均0；model_usage八列保持原设计。两内容version1/generation1、Interval input/indexed_versions、ES两文档及四个1024维有限float32向量全部对应。两媒体DTO的0–60000/60000–90132ms范围、字幕/事件边界和播放READY通过；真实Range请求HTTP206，bytes0–1023/9999051。

ASR原始17/11条秒制估计全部精确等于独立计算的 `round(seconds*1000)`、片段裁剪及一次offset后的实际返回；第二段首条4.5–5.1秒正确存为64500–65100ms。最终28句对白均与ASR原文标准化对应，27句起止一致，1句开始提前400ms、结束一致；第二段11句横跨25632ms，未重现26ms压缩。这里只验证程序换算及最终结果与观察到的ASR一致，不认证音轨毫秒真值。

**本轮不作高质量解析验收。** 精修raw为 `{}`、output仅2 Token，意味着没有内容修改提案，不能与旧343.738秒的精修工作量等同。旧SQL当前内容为第一段14对白+1场景、第二段13对白+14动作+1场景；本轮为第一段17对白、第二段11对白+1动作。第二段非对白事件由15条变为1条，视觉信息输出明显更少；事件数量本身不能判定真值或归因于单独关闭reasoning，但必须作为质量对照保留。字幕、统计、存储、检索发布与播放通路通过，不等于人物/动作/场景描述全部准确完整。

前端兼容已按追加批准的三文件修改树补齐：Vision最大输出输入框与JS校验均为262144，接受新65536默认值；ASR及备用模型仍限制32768。新增7项预算边界回归，`npm test -- src/features/uploads/upload-types.test.ts src/features/uploads/UploadProvider.test.tsx`，23项通过；`npm run typecheck`通过。前端不新增reasoning编辑控件，取得的有效配置通过原有对象复制和请求路径保留该附加字段。

独立终态复核31/31项通过，结论仅为参数、统计、秒级时间、存储索引及播放通路通过，保留上述质量资格。已对本轮隔离API PID14346核实身份后发SIGTERM，9622关闭；恢复的3306/9200依赖与新旧测试数据保留。未执行commit、push或业务库迁移；新增测试库按现有Schema初始化，不改变model_usage八列设计。

收尾时当前ROOT出现并行ORM类及仓储引用改名，相关源码和共享测试中的改动完整保留。本轮配置及四个模型控制生产文件与实测冻结版字节相同；最新工作区在这些并行改动后再次运行相同离线命令，573项通过（11.78秒）。真实模型、SQL/API和播放验收仍以13:21冻结的542文件为证，不把后来的整个ROOT版本追认为同一次实测。

本轮本地证据：[配置核对](../../outputs/verification/vision-controls-20261009-resumed/default-verification.json)、[实际请求与最终账本审计](../../outputs/verification/vision-controls-20261009-resumed/audit-result.json)、[ASR时间审计](../../outputs/verification/vision-controls-20261009-resumed/asr-timestamp-audit.json)、[同片耗时及质量资格](../../outputs/verification/vision-controls-20261009-resumed/performance-comparison.json)、[媒体验证](../../outputs/verification/vision-controls-20261009-resumed/media-verification.json)。源代码、输入素材与原始payload保存在同一持久输出目录；旧临时目录证据已消失，本轮旧耗时沿用已记录的历史文档，旧SQL快照只读恢复，未补造历史raw或更改旧账本。

#### 同片轻推理对照与精修覆盖回归（2026-10-09）

用户要求理解和精修改用轻推理对照。新轮只复制上一轮 `none` 的542文件冻结源码，将测试副本 `models.vision.reasoning_effort` 改为 `low`；其余541文件字节相同。视频仍为同一SHA256、9999051字节、90132ms，Series prompt元数据、65536输出预算、ASR、备用模型、并发和重试配置均保持；普通上传无请求级覆盖，ROOT默认仍为 `none`。两轮实际ASR分句与文字存在差异，因此只证明配置单项变化，不能宣称逐字prompt相同的严格A/B或仅由reasoning造成全部差异。

本轮 namespace `seekino_parse_budget_resume_20261009_135022_test`，task `5940fbfb-439f-4a66-b066-68d8bc97c91d`；13:56:47–14:07:48 UTC提交至终态轮询 **661.334秒**。之前的沙箱本地连接拒绝发生在提交前，没有创建第二个任务或消耗模型Token，不计入解析墙钟。父任务、两Chunk和唯一Interval均COMPLETE，Chunk/Interval retry均0；这只表示执行通路结束，**本轮语义质量验收失败**。

| 同片对照 | 关闭推理 none | 轻推理 low |
| --- | ---: | ---: |
| 总墙钟时间（含终态轮询） | 100.285秒 | 661.334秒 |
| 模型调用 | 10 | 10 |
| 总Token | 193985 | 232369 |
| 主路理解/精修reasoning Token | 0 | 37332 |
| 失败调用 / Token | 1 / 27885 | 1 / 35474 |
| 精修生成阶段耗时 | 3.073秒 | 454.095秒 |
| 理解初稿事件总数 | 29 | 36 |
| 最终保存事件总数 | 29 | **2** |

low耗时约为none的6.59倍，Token增加约19.8%。两段理解仍并行：第一段主路129.374秒后内容过滤，既有DeepSeek备用模型接续，理解合计136.672秒；第二段主路161.829秒成功，理解合计161.855秒。精修远程454.069秒、完整生成阶段454.095秒，索引17.710秒；没有length或整个Chunk重跑。三次真实Ark请求均发送 `reasoning.effort=low`、65536，供应商reasoning分别6731、6638、23963；最后一项来自精修，占精修24253输出Token的大部分。ASR设置不变，reasoning仍真实报告477/311，embedding未知字段继续为null；reasoning属于output的组成部分，不重复累加到total。

**精修将完整时间线覆盖成局部修改，必须先修复该契约问题。** low初稿第一段17事件（10对白、7场景），第二段19事件（11对白、4动作、2情绪、1场景、1画面文字）。精修只返回两段各一条修改参与者的对白，未携带其余事件。现有提示要求仅返回变化的成员/字段，但未明确强调一旦提供timeline必须给完整替换数组；Engine与Repository按整字段替换执行，结构、人物和时间校验均放行。两段最终version2各1条对白，36条事件降为2条，净减少34条事件、19条对白；SQL、API与ES都保存了这个缩减结果。精修有更新、版本一致或任务COMPLETE不能作为内容可用的依据。none轮精修为空，保留29条，未触发这个覆盖路径；问题属于已有精修协议/校验缺口，不能归因于低推理参数本身，也与秒转毫秒无关。

离线使用未修改的冻结Engine重放本轮raw精修提案，现有校验接受两份单条timeline，`apply_chunk_update`得到1+1条，精确等于最终SQL；没有新增模型调用或数据库写入。应先明确并落实精修timeline的全量替换或事件级修改语义，防止局部纠正丢弃未修改事件；不以人物登记表或简单数量阈值代替更新契约。

六张代表帧10/35/55/65/75/85秒已独立核对：low初稿增加部分场景和可见人物覆盖，但35秒画面没有其声称可见的白发少女，且「やらないの？」比本轮ASR提前4.2秒。精修没有证明修正该问题，该事件随其余内容被替换掉。none的对白description也含视觉观察，不能把独立ACTION/SCENE数量直接等同视觉理解准确率；音箱开/关等动作单凭代表帧不能裁决。最终ASR秒到毫秒程序转换仍正确，精修丢失的对白属于内容覆盖失败，不能靠换算通过代替验收。

用量与工程证据复核34/34通过：10个唯一call_id与预算used集合一致、missing0，input186595/output45774/total232369与API逐项相等，失败用量保留；全部执行租约释放，两文档最终version2及索引回执对应，四个1024维有限float32向量一致，Range播放206。顶层验收明确为 `failed_semantic_quality`，不将这些工程检查宣传为解析质量通过。仅启动本轮隔离API和初始化现有Schema的独立测试资源；API PID27755已核对身份后停止，MySQL/ES与新旧测试数据保留。ROOT生产源码/默认配置、八列model_usage和并行工作区改动保持；本轮只追加本问题文档与忽略目录内测试证据，未commit或push。

本轮证据：[准备单变量核对](../../outputs/verification/vision-controls-low-20261009/independent-preparation-review.json)、[统计与终态审计](../../outputs/verification/vision-controls-low-20261009/final-reviewed.json)、[独立内容对照](../../outputs/verification/vision-controls-low-20261009/none-low-independent-quality-comparison.json)、[精修覆盖离线重放](../../outputs/verification/vision-controls-low-20261009/refine-replacement-replay.json)、[ASR时间换算](../../outputs/verification/vision-controls-low-20261009/asr-timestamp-audit.json)。原始素材、manifest、SDK请求、raw响应、版本与索引快照保存在同一持久目录，none轮证据未改。

#### 处理方向与验收条件

- 理解/精修的思考策略与输出预算接线及同片真实请求核对已通过，关闭思考与65,536预算已生效；视觉事件减少与精修空提案必须纳入质量对照，尚未完成高质量语义验收。
- 人物名单约束仅适用于 `DIALOGUE`，源码修复及离线验证已完成；后续真实联调核对非对白观察可直接保留，对白归属不确定时允许 `speaker=null`，所有事件仍须满足结构、时间范围和任务/lease 边界。观察到吉他、手部或附注不触发人物名单阻断，也不自动清洗名字或推断人物归属。
- 按 schema 引用失败、输出预算耗尽、内容过滤、网络故障分别确定阶段恢复和 fallback；复用有效 ASR 成果所需的身份、配置、provenance 与租约边界必须保持，不能为了省 Token 把不完整/过滤结果当成成功。
- 使总请求耗时和 Chunk 退避遵循真正有效的配置；以能持续输出超过总预算的流式故障验证取消、用量保留、线程收尾及不再启动后续调用，不新增逐阶段高频计时表。
- 用同一90132ms原片报告墙钟、调用次数、reasoning/总Token、失败用量及并发重叠；定向修复应保留已完成工作，重试不再无区别重复 ASR。字幕/动作时间、人物引用、内容覆盖和播放范围需要共同验收，结构通过不能代替原片语义质量评测。

用户提出“约15分钟完成高质量解析”的预期。**在修复前冻结轮去掉失败重试、沿用该轮首次理解和精修耗时，曾粗估约16分钟**（理解约10分钟、精修约6分钟、准备和索引），该历史估算不是本轮100.3秒的实测结果。最新默认配置未保证固定时限，也没有由完成时长证明高质量；视觉信息变少和空精修已留证。既有验收只确认新Parse通路与相对ASR的秒级时间一致性，未认证毫秒音轨真值或全部人物/动作/对白语义；ASR输出秒、程序保存毫秒的修复继续有效。

依据：[模型适配器](../../gateway/ark/multimodal.py)、[SSE 终态处理](../../gateway/ark/sse.py)、[引用校验](../../engine/parsev2/schemas.py)、[Chunk Workflow](../../agent/video_parse_v2/understanding/workflow.py)、[宿主与 fallback](../../server/service/video_parse.py)、[Consumer 退避](../../server/mq/video_parse_consumer.py)、[最新运行证据索引](../report/server/model-token-usage-stages-evidence.md)。原始证据 `/private/tmp/seekino-asr-reparse-20261009/` 的 `source/`、`environment.json`、`stage-observations.jsonl`、`understanding-schema-diagnosis.json`、`state-latest.json`、`audit-parse-timing-final.json` 与 `acceptance-final.json`；装配/持久层有后续ROOT变化，根因以该冻结源码及实际调用记录为准。

#### 2026-09-23：ParseV1 历史实测数据与统计口径

以下实测、结构性原因、处理方向和验收条件保留该日期的 ParseV1 语境，不把历史“Understanding 按 Context 串行”或价格统计套到上面的 ParseV2 运行。

以下数据取自 2026-09-23 的本地开发库 `series_parse_job`、`parse_usage_attempt`、`parse_model_call`，共 7 个 Job、17 个 Attempt 和 675 次已记录模型调用；EP07 仍为 QUEUED 且没有消耗，未列入下表。表中“Worker 耗时”是 Consumer 使用单调时钟累计的活跃处理时间，不是视频时长；“已知预估费用”按每次调用保存的本地人民币价格快照计算。它只是当前配置单价估算中已经能够计价的部分，不等同于 Provider 最终账单，也不包含赠金、资源包或折扣；未计价调用和不完整 Attempt 会让配置单价估算缺少一部分，不能据此反推实际账单下界。

| 样本 | 结果 | 源片长 | Worker 耗时 | 相对片长 | Attempt | 已记录调用 | Total tokens | 已知预估费用 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EP01／`4770ed42` | 成功 | 23.84 min | 81.54 min | 3.42× | 6 | 117 | 3,585,909 | ¥15.40 |
| EP02／`d57d8145` | 成功 | 23.67 min | 74.52 min | 3.15× | 1 | 96 | 3,051,231 | ¥14.76 |
| EP03／`e8ab3ef3` | 成功 | 23.67 min | 77.74 min | 3.28× | 2 | 119 | 3,465,478 | ¥15.47 |
| EP04／`0a45ce3d` | 成功 | 23.67 min | 至少 99.55 min | 至少 4.21× | 3 | 139 | 4,219,958 | 已知 ¥14.39；48 次调用未计价 |
| EP05／`2d24ee44` | 成功 | 23.67 min | 97.20 min | 4.11× | 1 | 104 | 3,054,408 | 已知 ¥12.89；48 次调用未计价 |
| EP06／`757b6064` | 失败，进度 17% | 23.67 min | 至少 29.48 min | 至少 1.25× | 4 | 100 | 2,921,862 | 已记录 ¥4.62，账本覆盖不完整 |

- 4 个计时完整的成功样本均约 23.7 分钟，但活跃处理耗时为 74.52–97.20 分钟，即片长的 3.15–4.11 倍。EP04 的计时不完整，但已累计至少 99.55 分钟和 4.21 倍片长。
- 5 个成功样本合计 118.508 分钟源视频，已知 Worker 耗时至少 430.542 分钟，聚合后至少为实时播放时长的 3.633 倍；已记录 575 次模型调用和 17,376,984 tokens。
- EP01–EP03 的调用与价格覆盖完整，单集预估费用为 ¥14.76–¥15.47，均值 ¥15.21，约为每源视频分钟 ¥0.64。5 个成功样本的已计价调用合计 ¥72.91，但 EP04、EP05 各有 48 次 Embedding 调用未计价，因此不能把 ¥72.91 当作完整总费用。
- EP06 最终只到 17% 且没有产出可用结果，却已记录 100 次调用、约 292 万 tokens 和 ¥4.62 已知费用。其已记录调用数已超过一次无重试成功的 EP02，说明页面进度不能作为资源消耗的近似值。
- 合并成功样本与 EP06 后，当前已执行 Job 共记录 675 次调用、20,298,846 tokens 和 ¥77.5359478 已计价调用预估费用；完整的配置单价估算还缺少 96 次 Embedding 调用，且该小计仍不是实际账单。
- 2026-09-23 的本地 Artifact 快照还显示：`outputs/server/artifacts/development/jobs` 中 6 个已运行 Job 的工作目录约 4.5 GiB；`outputs/server/artifacts/development/media` 中排除原视频后的派生 Chunk、Analysis Window 和关键帧约 4.4 GiB。发布实现使用文件复制，工作目录与稳定媒体目录中的派生文件是两份独立副本。这属于本机转码、I/O 和磁盘放大，不能与上表的模型费用混算。

EP01 的各 Attempt 跨越了隔夜和人工恢复间隔，所以 `completed_at - started_at` 约 23.8 小时，不能描述成连续计算耗时。排除该样本后，另外 4 个成功 Job 的端到端墙钟时间为 74.5–107.6 分钟，仍明显超过源片长。

#### 已确认的结构性原因

1. **单集天然产生大量远程调用。** 默认按 60 秒切片并在相邻切片两侧各保留 3 秒重叠。约 23.7 分钟视频形成 24 个 Chunk；没有 fallback 和重试时，每个 Chunk 仍至少执行 1 次 Lite 候选转写、1 次 Pro 视频理解、1 次文本向量和 1 次视频向量，即理论最低 96 次模型调用。
2. **主要阶段串行。** ASR 只有 4 路有界并发，Pipeline 等全部待处理 ASR 完成后才进入 Understanding；Understanding 为演进 Context 按 Chunk 串行。文本向量全部完成后才串行执行视频向量。同一服务实例只装配一个 Parse Consumer，Job 之间也逐个处理。按 EP02 的 Job 日志和调用顺序近似拆分，ASR 约 29.9 分钟、主视觉理解约 39.3 分钟、Embedding 约 2.9 分钟、本地处理与阶段交接约 2.5 分钟；这只是日志反推值，当前没有持久化的逐阶段正式指标。
3. **Pro 视频理解是当前首要金额来源。** 在无重试且全部调用可计价的 EP02 中，24 次 `video_timeline` 占 45.30% tokens，却贡献 ¥12.44，即该集预估费用的 84.27%；24 次视频向量调用贡献 11.32%，Lite 候选转写贡献 4.22%，文本向量贡献 0.19%。该比例只代表此实测样本，但足以定位本轮优化的第一对象。
4. **媒体准备和上传重复。** 每个 Chunk 依次生成 Analysis Window、Canonical Chunk 和关键帧；ASR、Pro 理解和视频向量还会分别读取并内联编码视频。一次 24-Chunk Attempt 至少涉及 48 次片段转码和 24 次独立关键帧提取，当前没有按源内容与解析配置复用整套准备结果。
5. **重试会重做已经成功但未持久化的工作。** ASR 没有独立 durable checkpoint。Understanding 在中途失败后，下一 Attempt 只恢复已提交的连续 PARSED 前缀，其余已经在内存中完成的 ASR 会重新调用；媒体准备也会写入新的 `lease-*` 目录并重新生成全部窗口。实测候选转写调用从干净样本的 24 次放大到 EP01 的 41 次、EP04 的 63 次和失败 EP06 的 91 次。
6. **fallback 继续放大长尾。** 一个典型 66 秒窗口触发内容审核 fallback 后，会在已发生的主 Pro 请求之外，再增加 3 次 `neutral_visual_evidence` 和 1 次 `fallback_video_timeline`；本地日志已出现这一 1+4 调用路径。主请求单次超时上限为 600 秒，fallback 单次为 1200 秒，当前没有整个 Job 的总耗时、总调用或总费用预算。

#### 处理方向

1. 先补低频结构化指标或 Metrics，分别统计 media prepare、ASR、understand、text embed、video embed 和 index 的耗时、调用数与费用；不要恢复 B04 已删除的逐 Chunk／逐 Stage 高频写库表。
2. 优先完成重试止损：为 ASR 增加可恢复的 Chunk 级持久化结果，以内容哈希、解析配置和模型 provenance 作为复用条件；恢复时复用已发布媒体，避免重新转码、上传和计费。为 Job 设置明确的总耗时、总调用和总费用预算。
3. 以同一批视频做质量对照，评估减少 Pro 重复输入和调用数量的方案，包括降低视觉采样、合并相邻窗口、先筛选再调用 Pro；任何降本方案都必须同时检查时间线和 Evidence 质量，不能只看费用。
4. 在不破坏 Context 顺序的前提下，将独立的 Embedding 和 fallback 分组改为有界并发或流水处理；Job 级并发需要同时验证 Provider 限流、内存占用和 Qdrant 写入边界。
5. 对最终成功或失败的 Job 做引用审计后，清理无 Checkpoint 引用的旧 Lease 和重复派生媒体；价格快照缺失时必须明确显示“未计价／下界”，不能显示成完整总费用。

#### 验收条件

- 用固定视频集同时报告源片长、端到端墙钟时间、活跃 Worker 时间、Attempt、模型调用、tokens、已知费用、未计价调用和用量覆盖状态，并形成可重复的优化前基线。
- 普通成功、Understanding 中途失败重试和 Worker 中断恢复都不再重复调用已经持久化的 ASR，也不重新生成内容与配置一致的媒体文件。
- 优化后的同批样本在 Worker 时间、墙钟时间和每源视频分钟费用上均有可重复的下降，同时 Timeline、Transcript 和检索 Evidence 的质量回归通过。
- 所有调用都有价格快照，或在 API／UI 中明确标记未知费用；失败 Job 达到预算后可解释地终止，不再让重试无限线性放大资源消耗。

依据：[pipeline.py](../../engine/parsev1/pipeline.py)、[transcribe.py](../../engine/parsev1/transcribe.py)、[understand.py](../../engine/parsev1/understand.py) 与 [embed.py](../../engine/parsev1/embed.py) 的阶段顺序和并发边界；[video_chunk_parse.py](../../engine/parsev1/video_chunk_parse.py) 的主理解与 fallback；[analysis_windows.py](../../engine/parsev1/analysis_windows.py)、[video_processing.py](../../server/service/parsev1/video_processing.py) 与 [artifact.py](../../server/storage/artifact.py) 的媒体准备、Lease 目录和复制发布；[consumer.py](../../server/mq/consumer.py) 的 Job 消费；[parse_usage.py](../../server/service/parse_usage.py) 与 [parse-model-usage.md](../parse-model-usage.md) 的价格快照和费用口径；[config.yaml](../../config.yaml) 的当前模型与 fallback 配置；本地开发库和 `outputs/server/logs/development/api.log` 的 2026-09-20 至 2026-09-23 运行记录。

### C14：台词检索存在假设偏移、Chunk 级语义表征与融合截取缺口

排序日期：2026-10-17。

优先级：高。类型：检索质量、Embedding 表征粒度、假设与事实边界、审核覆盖及追踪缺口。状态：已对比真实成功／失败样本，完成 89 组真实 RAG 组合测试及 8 次 Embedding 隔离对照；确认本样本的短句对整段向量匹配排名偏低，且部分目标在底层通道命中后被融合与最终截取排除。初始人物／场景联想的具体来源未确定，尚未修复。本项只记录问题与待验收方向。

**问题归纳：近义台词单独编码相近，不保证所在 Chunk 会被召回。**当前用查询短句对比整分钟多事件文本的单一向量，目标台词的匹配信号可能不足以让整个 Chunk 进入语义候选；即使词法已命中，偏重语义的 RRF 仍可能将其排出最终 topK。Agent 后续加入未经验证的人物／场景条件，是另一个需要独立处理的搜索方向问题。三者应分别验证，不能只修 Prompt、增加检索次数或调大全局 topK 就认定已解决。

#### 现象与可复核样本

以下运行均发生于 `2026-10-16`，时间为 `+0800`，Tenant 为 `local-default`；本项于次日整理。两轮为不同 Conversation，失败轮没有收到成功轮的历史答案。

| 项目 | 成功样本 | 失败样本 |
| --- | --- | --- |
| 用户问题 | `真是高高在上呢 在哪里出现过？` | `真实高高在上呢，这谁说过吗` |
| Conversation | `d0c244a7-daf6-4cd1-9aca-afc5449cd26b` | `57fedc8b-cf83-4413-854b-7a2c56a65543` |
| Assistant Message | `b9f2af2d-1142-45ab-9362-22d5e0f758a1` | `53696eb7-6f35-44d5-92e7-351fc566c64b` |
| 完成时间／处理统计 | `20:13:22`／`134787 ms` | `23:58:10`／`178908 ms` |
| Query 检索 | 3 次，累计 18 条、去重 18 个 Chunk | 13 次，累计 88 条、去重 62 个 Chunk |
| Query 精读 Transcript | 8 个 Chunk | 6 个 Chunk |
| Evaluator 补检索 | 无 | 1 次、5 条；与 Query 合并后去重 63 个 Chunk |
| 正确 Chunk | 第 3 次检索第 1 名进入候选，最终提交 1 个视频项 | 全部 14 次检索返回的候选均未包含正确 Chunk，最终视频项为 0 |

正确证据为第 13 集 Chunk `ad1c7419-c11c-5280-9e2e-89456355bfe7`，所属 Video 为 `e6a02c10-7b93-5af9-b21a-9e431bd130e6`。Transcript 在 `54600–56000 ms` 记载豊川祥子对若葉睦说「いいご身分ですわね。」，中文译文为「真是居高临下啊」。最终播放器范围为 `37500–60000 ms`，覆盖前后对话。用户记忆中的中文措辞与证据译文不同，需要核验近义表达。

两轮实际 `model_usage` 均记录 Supervisor／Query／Evaluator／内部 Chat 使用 `gpt-6-astra`，Embedding 为 `doubao-embedding-vision-250615`；检索策略均为 `query-adaptive-three-channel-rrf-v2`，日志中的 ES index 名均为 `seekino_video_chunks_local_20261010_development`。失败轮比成功轮多选一个 EP04 复核 Series，但仍包含正确的第 13 集。以上排除了本样本中“换了模型”与“第 13 集不在 Scope”这两种解释；同名 index 不证明两个时点的全部文档／版本完全一致。

#### 未经证实的前提最早出现在哪里

1. **Supervisor 未将目标台词预先归属爱音／爽世。**失败轮原始 Supervisor prompt 与 `search_video_graph` 实参要求核验可能的台词、说话人和语境，没有把这两个人物或第 10 集指定为答案。用户输入也未提供人物、集数或场景。不能把现象记成“Supervisor 先幻觉，再把错误背景传给 Query”。
2. **Query 在第二次检索首次加入人物。**第一次 query 为 `真是高高在上呢 说话 居高临下`。`23:55:37–23:55:42` 先读取首批候选 `2180f859…`、`59169f19…` 的 transcript／text，`23:55:46` 第二次检索改为 `长崎爽世 千早爱音 真是高高在上 居高临下 普通人`。最早可观察到的变化发生在 Query：人物词在候选精读后进入后续搜索条件。这能确认约束增加的时点，不能单独确定最初联想的来源，也不能证明模型已把该人物当作答案。
3. **后续多次扩写同一假设。**实际 query 包括 `长崎爽世在家里对千早爱音说高高在上，爱音说要结束自己开始的乐队`、`第10集 爽世 爱音 客厅 谈话 16分钟`、`长崎爽世 千早爱音 在家中 结束乐队 ANON TOKYO`。人物、场景、集数和时间均超出用户输入，尚无目标台词的直接证据。后续仍有不带人物的日语检索和语义改写，不能声称搜索完全锁死在一个方向。
4. **审核接受了未核实结论，没有纠正召回缺口。**Evaluator 精读相关候选并额外检索一次后，以“已覆盖中文语义及多种日语表达、未核实到目标台词”为由返回 `complete`。Supervisor 最终明确表示无法确定说话人，没有将错误人物归属写成事实；本轮可确认的问题是搜索假设持续影响召回，以及审核对搜索覆盖的判断不足。正确 Chunk 没有进入任何返回候选，不能归为 Evaluator 筛掉正确片段。

提出可撤回的搜索假设本身合理；需要防止把候选相关性、作品背景或模型联想当成已验证前提，并据此不断增加约束。该样本呈现“首批候选 → 人物／场景假设 → 后续搜索继续围绕该假设”的轨迹，存在候选锚定和确认偏差的表现。**具体由首批候选、模型既有知识还是 Character Bible 背景触发最初联想，现有 Trace 无法区分各自贡献，尚不能确定。**不得把推测写成已经定位的内部认知根因。

**背景输入补核：实际包含关系与行为信息。**失败轮原始 Supervisor prompt 的两个 Series 均有 `characterBible=[]`，但 `importantExtraData.content` 各自传入完整的 `12581` 字符 Markdown Character Bible，包含 9 条 `relation_notes` 和 9 条 `behavior_notes`，不能只看空数组就判断没有背景关系。实际输入包括“若叶睦与祥子是青梅竹马、与爽世同班”“爱音与立希容易发生言语冲突”“爽世对旧乐队和丰川祥子有强烈情感牵引”等；`SEEKINO_RUNTIME_CONTEXT_START/END` 标记之外的完整正文也在 prompt 内。现代码的 `load_video_context()` 保留选定 Series，`project_inputs()` 直接带入 `series_context`，没有在 Chat 路径只提取运行时标记段或过滤关系／行为字段。背景里未出现本次目标台词、“第10集／16分钟”或“ANON TOKYO”这些具体检索条件，因此“有关系背景”是已确认输入事实，“该背景导致本次错误假设”仍未证明；原始完整 Query prompt 的历史留档限制不变。依据：[背景原文件](../../assets/mygo_background_referrence/characters.md)、[背景装配](../../server/service/agent_chat.py)、[Query 输入投影](../../agent/chat/video_search/context.py)及上述 Supervisor 原始 rollout 第 9 行。

#### 为什么之前的成功也不能证明检索稳定

成功轮前两次中文语义检索也未找到正确片段；第三次使用 `上から 偉そう`，`top_k=5`，词法／语义／视觉权重为 `0.85/0.15/0`，才把正确 Chunk 排到第 1。失败轮尝试了 `上からだね`、`上から目線`、`ずいぶん偉そうだね` 和 `上から`，未使用相同组合。

历史词法命中日志指向相邻台词「祥が壊れそうだから。」，而非目标台词本身。只读 ES `_mapping`、`_analyze` 与单文档 `_explain` 确认：当前 `cjk` analyzer 将 `上から 偉そう` 分为 `上か／から／偉そ／そう`，相邻台词共享 `から／そう`；目标句「いいご身分ですわね。」与该查询的分词没有交集。当前单文档匹配解释来自这两个公共字串，解释分数与历史分数略有不同，不能宣称完整复现历史排名。

因此，该次成功带有相邻对白公共字串帮助召回整个 Chunk 的偶然成分。Query 精读后取得了目标台词的直接证据，最终答案有据；这不能证明系统已经稳定理解中文记忆台词与日语近义表达。两轮都未调用 `inspect_video_frames`，本次对比未证明抽帧是成功／失败的决定因素。

#### 机制缺口与待处理方向

- **Query：区分已知约束与搜索假设。**用户明确给出的角色、场景、集数可以约束检索；从候选、背景或模型联想加入的条件应保持可撤回。连续候选不能直接确认目标台词时，重新执行不依赖该人物／场景的搜索，改变独立假设，而非只增加措辞和重试次数。优先复用既有 Query／通用 Retrieval Skill 与 Canvas，不先增加假设表或新的 Agent 层。
- **Evaluator：审核搜索覆盖与退出条件。**现有 Prompt／Skill 已要求审查意图和检索策略，缺口在实际执行效果，不能简单记成“完全没有审核要求”。审核未确认答复时，应检查检索是否反复依赖同一未经证实的前提，以及是否做过解除该前提的补查；无法证明覆盖时保留检索限制，不能仅按查询次数、候选数量或语言种类认定充分。
- **Supervisor：保持最终事实和覆盖责任。**背景、候选与 Query 草稿都不能直接成为事实；对未确认结果检查内层限制并清楚说明。本轮未观察到 Supervisor 最先引入错误人物，不以本问题为由擅自新增循环调度层或重写会话管理。
- **检索层：评估公共日语字串对排序的影响。**现有 `cjk` 与宽松 `match` 可因公共字串带回候选。需用台词近义、译文差异及困难负例评估召回率和误召回，不直接增加硬性词项门槛，否则可能丢掉这次成功所依赖的候选。
- **追踪：保留可复核的实际输入与决策输出。**本轮日志足以对比检索词、权重、候选与工具读取，但 Query／Evaluator 使用 ephemeral thread，当前本地 `capture_model_diagnostics` 仅为 Parse 落原始诊断，未留两轮完整内部 prompt。需要复用现有 Trace／日志路径记录脱敏的模型可见输入、角色与版本、query 改写、真实工具结果和审核结论，使后续能判断背景如何进入检索；不得假装已复原历史缺失内容。本项不新建持久化表。

#### 2026-10-17 真实 RAG 组合测试补充

- 直接调用正式 `build_retrieve_tool.invoke → VideoParseRetrievalService.search`，完成主矩阵 71 组及权重／final topK 对照 18 组，全部无执行错误。使用真实 MySQL／ES 与已配置的 Ark Embedding；未重跑 Supervisor／Query／Evaluator 模型，未修改生产策略。完整参数、原始通道候选、工具分页、正式 Evidence 与离线融合审计见[实验报告](../../outputs/verification/mygo-quote-rag-20261017-003118/REPORT.md)。
- **补充分层结论：历史工具返回里没有正确 Chunk，不等于底层各通道都没召回。**按历史失败首 query `真是高高在上呢 说话 居高临下` 与 `.05/.9/.05` 权重复跑，目标在词法第 1、语义第 17，完整候选融合第 13，超出原 `top_k=8`；只扩大 final K 到 40、保持通道深度 20，实际返回目标第 13。该结果来自当前索引实测，不能冒充历史原始通道留档。
- 受控 query `居高临下` 与已知日语原句 `いいご身分ですわね`，纯词法均把正确 Chunk 排第 1；改为词法 `.1`／语义 `.9` 后，目标仍是底层词法第 1，却融合到第 21，最终 top10 丢失。词法字段分别直接命中目标中文译文与日语原句，因此现象不只由翻译、日语公共字串或人物假设引起。
- 当前 RRF 使用 `weight / (60 + rank)`，不直接使用 BM25 绝对分数。词法第 1 但未进入语义 top20 的目标，在 `.1/.9` 下只得 `.1/61`，低于语义第 20 单通道项的 `.9/80`。已保存 raw candidates 的官方融合离线复算区分了 channel depth、candidate budget 和 final topK；不能把融合截取误报为底层没命中。
- 同词权重 sweep 显示：历史失败首 query 在 `.1/.9/0` 时返回目标第 8，`.3/.7/0` 时第 1；`居高临下` 在 `.1/.9`、`.3/.7` 的最终 top10 均无目标，`.5/.5` 为第 2、`.8/.2` 为第 1。单样本不作为全局改权重依据，应将通道融合与截取一并纳入修复评估，而非只调整背景或 Query 提示。

#### Embedding 表征粒度补核

- 目标 ES 的 `semantic_text` 确实保留「真是居高临下啊」与日语原句，但编码对象是 21 个事件拼接的完整一分钟、1775 字符文本（工作、通话、退勤、到访、弹吉他、乐队对白等），只保存一个 1024 维 semantic 向量；没有单句／单事件向量。查询字符串直接用同一模型编码，与整个 Chunk 向量比较；完整 Character Bible 没有直接拼入该语义文本。
- 新增 8 次真实 Embedding 隔离对照：「真是高高在上呢」与「真是居高临下啊」单独编码的余弦相似度为 `0.811762`；同一 query 对目标 110 字单事件为 `0.443881`，对整分钟文本为 `0.357940`。新编码完整文本的向量与现存 ES 向量逐元素相同，最大绝对差为 0。结果支持本样本的表征粒度不匹配，不能解释成模型完全不认识近义词或中文没进入索引。
- 使用同批已保存向量，对 MyGO 当前全部 312 个 semantic 向量做正式精确余弦排序：目标对「高高在上」为第 208、对「真是高高在上呢」为第 123、对中文原句为第 89；单个已知事件作查询为第 18。默认 ANN top20 与这些结果一致，故本样本低排名也存在于精确排序。单目标对照不是通用准确率；向量模型、语言混合和格式等贡献仍需继续验证。
- 当前采用 `doubao-embedding-vision-250615`、1024 维、Chunk 文本／视频双向量及词法通道；应评估保留 Chunk 表征，同时增加台词／对话窗口／事件级表征并归并回 Chunk 的收益，不能只调整 RRF 或背景 Prompt。完整输入、响应向量、两两相似度与精确排序见 [Embedding 诊断报告](../../outputs/verification/mygo-quote-rag-20261017-003118/embedding-diagnosis/REPORT.md)。本轮只诊断，未修改索引策略或重跑入库。

#### 验收条件

- 使用这两种问法及其近义改写作为固定台词定位样本，复核正确 Chunk 的召回与证据归属；记录多次真实模型运行的结果，单次成功或 fake-model 测试不能证明稳定。
- 对比现有 Chunk 单向量与台词／对话窗口／事件级表征的召回效果及索引、查询成本；结果须关联回正式 Chunk、时间范围、Tenant 和内容版本，不能依赖本样本的人物或台词硬编码。
- 分别记录各通道原始排名、融合排名及最终 topK，核对强词法命中在哪里被排除；在固定样本和困难负例上分别评估表征粒度、融合权重与截取深度的收益，不能将本样本一次调权成功作为全局修复依据。
- 首批候选包含相似语气但错误人物／场景时，Query 能验证并放弃无支持假设；Evaluator 能发现搜索方向偏移，并通过既有 `continue/advice` 要求有效补查。
- 未找到直接证据时仍允许正常完成纯文字答复，但明确“在已检索／已读证据中未核实”的范围；不得扩大成作品不存在该台词，也不把检索假设当作最终事实。
- 真实 trace 能关联用户问题、背景来源、Query 调用、候选及其精读、Evaluator advice 与 Supervisor 最终提交，区分可观察行为与无法确认的内部联想。

依据：本机 [API 日志](../../outputs/server/logs/development/api.log) 中成功样本 `6075–6155`、失败样本 `10967–11133` 的检索／精读／审核记录，尤其 `6117`（相邻台词词法命中）、`6122`（正确 Chunk 返回）、`10987`（首次带人物的 Query）与 `11133`（未确认答复获批）；`GET /api/agent/messages/list` 的两轮 `retrievalTrace/renderBlocks`；只读 `model_usage` 查询（按上述 Assistant Message 的 `run_id`）；失败轮原始 Supervisor rollout `~/.codex/sessions/2026/10/16/rollout-2026-10-16T23-55-12-01a1456c-ae50-7aa2-bee5-0b3dd9b69969.jsonl` 第 9／13 行；[Query](../../agent/chat/video_search/query.py)、[Evaluator](../../agent/chat/video_search/evaluator.py)、[检索 Skill](../../agent/chat/video_search/skills/retrieval/SKILL.md)、[Supervisor](../../agent/chat/supervisor.py)、[ES 词法匹配](../../server/storage/elasticsearch.py)、[本地诊断边界](../../gateway/model_diagnostics.py)。本次未修改运行逻辑，未重跑 Supervisor／Query／Evaluator／内部 Chat 模型；上述 RAG 实验真实调用已配置的 Embedding，未重启服务或迁移数据库。

### B01：Agent 遗留 PROCESSING 永久阻塞对话

排序日期：2026-10-16。

优先级：高。类型：执行生命周期和故障收尾缺失。状态：历史问题已具备部分退出路径；持久截止时间与跨实例过期扫描方案尚未完成。

#### 2026-10-16 已实施与仍缺失的边界

- 当前 Chat 已有手动停止：有效执行设置 cancellation，等待实际终态；本服务内没有执行对象的遗留消息可通过取消接口保存为 `FAILED / AGENT_CANCELLED`。既有 `FAILED` enum 直接复用，没有为取消新增数据库状态。
- 当前配置整轮执行预算为 600 秒，超时映射 `AGENT_EXECUTION_TIMEOUT`；模型错误映射 `AGENT_MODEL_ERROR`，保存失败状态、清空 phase 并更新版本后发布终态。Codex 终止错误通知此前没有及时上抛，修复及验收见 C13。
- 当前源码未见下述 `processingDeadlineAt` 持久字段与独立过期扫描；不能将进程内超时或手动取消说成已实现跨实例自动恢复。下文保留最初诊断和未实施方案，不表示这些字段已部署，也不表示可以自动续接旧模型执行。

依据：[取消与失败收尾](../../server/service/agent_chat.py)、[持久状态](../../server/repository/agent.py)、[消息 enum](../../server/model/agent.py)、[当前配置](../../config.yaml)。

#### 问题是什么

`AgentRepository.start_message()` 先在数据库中保存 USER 和 PROCESSING ASSISTANT，然后 Service 将执行放入 ThreadPoolExecutor。消息状态持久化了，执行任务和控制对象仍只在内存中。

```text
数据库提交 PROCESSING
    → 进程异常退出，内存任务消失
    → 数据库仍是 PROCESSING
    → 下次发送发现该对话已有处理中消息，返回 409
```

还有一个同类触发条件：任务已经结束，但 COMPLETED 和 FAILED 都写库失败；finally 仍移除内存任务，数据库恢复后没有机制补做失败收尾。

这里的 409 门禁本身有必要，它阻止同一 Conversation 同时执行两个 Kick。问题是没有为失去执行的消息提供退出 PROCESSING 的路径。REST / SSE 快照只能读取遗留状态，不能恢复任务或解除门禁。

#### 推荐的最小修复

当前先保证遗留消息能够失败结束、用户能够继续发送；不自动重放同一消息的模型请求。

1. **保存一个固定截止时间。** ASSISTANT 创建时持久化内部字段 `processingDeadlineAt`，表示本次请求必须结束的绝对 UTC 时间。预算从受理开始，覆盖线程池排队和执行，不能每个 Turn 或每次 Heartbeat 重新延长。
2. **补过期收尾。** 后台定期扫描过期的 ASSISTANT PROCESSING；发送请求前也先处理当前 Conversation 的过期消息，且必须发生在幂等消息查找之前，否则旧 Key 仍会提前返回 409。状态变为 FAILED，清空 phase、递增公开 version、写入明确的超时错误，保留已经保存的正文和证据。
3. **在事务中确认过期。** 只更新仍为 PROCESSING 且截止时间已到的消息。已经完成／失败的消息不能再修改；未到截止时间的有效执行仍保留 409 门禁。多个 API 实例同时扫描只应产生一次终态变化。
4. **将写入保护与截止时间对齐。** 当前 checkpoint / complete 已有行锁与 PROCESSING 检查，fail 也只改变 PROCESSING，不需要凭空重做一套状态体系。checkpoint / complete 还需要检查有效截止时间，避免截止已过、扫描尚未执行时迟到结果仍提交 COMPLETED。普通异常 fail 仍可终结 PROCESSING；过期收尾另要求截止时间已到，不能给普通 fail 错加“尚未超时”的限制。
5. **同步停止迟到输出。** Service 将剩余预算传给 Harness，排队结束后不能让 kick 重新获得完整 180 秒。progress / evidence / delta 发布前检查同一截止时间；本进程收尾时触发 cancellation。确认 FAILED 已提交后才发失败事件，状态无法确认时沿用 interrupted 和快照恢复。

这项修复保持同一 messageId 只执行一次，也不将终态改回 PROCESSING，因此不需要为它引入 executionVersion、自动接管或 LangGraph 执行恢复。原 Idempotency-Key 仍返回原失败消息；用户的新请求使用新 Key，在同一 Conversation 中继续。

#### 实现时必须注意

- 不能在服务启动时把所有 PROCESSING 一律改为 FAILED；其他 API 实例可能仍在正常执行。判定依据是持久化截止时间，不能只看当前进程的 `_executions`。
- 截止时间使用一致 UTC 时间源，多实例部署优先用数据库时间判定；获得行锁后重新读取当前时间，不能用等待锁之前的 now 判断截止。`monotonic()` 用于进程内剩余预算，不能持久化后跨进程比较。
- 当前 complete / fail 先锁 Message，再锁 Conversation。发送前的过期收尾应使用独立短事务并遵循相同锁顺序，提交后再进入 start_message；不能在持有 Conversation 锁时反向锁 Message。
- 仅有 SQL 保护不足以停止 SSE：当前 delta 约每秒才持久化一次，必须同时约束每次输出。写入被拒绝时读取已有终态，不再尝试保存 chunks 覆盖它；前端现有终态拒绝迟到增量的逻辑可复用。
- 历史 PROCESSING 没有截止时间，需要受控升级：停止并确认旧 API 执行进程退出后，将遗留处理中消息失败收尾，保留内容，再启用新版字段。不能在旧实例仍工作时盲目回填并判死。
- 取消为协作停止，阻塞的 Provider 请求仍依靠有限网络 timeout；失败收尾解除对话阻塞，不代表能够立刻硬杀线程或停止所有底层计算。

#### 修改归属

| 位置 | 拟修改内容 |
| --- | --- |
| server/model/agent.py | 内部截止时间字段和迁移要求 |
| server/repository/agent.py | 过期终结方法，沿用终态写保护并补截止判定 |
| server/service/agent_chat.py | 发送前收尾、定期扫描、取消与输出停止、已有终态处理 |
| agent/chat/video_search/runtime.py、agent/chat/common/state.py | 接收剩余预算，保持 Harness 独立于数据库／HTTP |
| server/api/app.py、bootstrap.py、config/agent.py | 扫描任务的启动／关闭与配置装配 |

#### 验收条件

- 保存 PROCESSING 后模拟进程退出，截止后另一实例将其置为 FAILED，同一对话可以发送新消息。
- 未过期任务不被另一个实例或定期扫描误杀；同一 Conversation 仍只能有一个有效 Kick。
- 完成与过期扫描并发时只产生一个终态；旧执行不能覆盖失败状态或继续发布正文。
- 排队耗尽预算的任务不再开始新的模型调用；旧幂等 Key 不会偷偷重跑模型。
- 终态写库暂时失败后，数据库恢复时扫描能够完成失败收尾。

依据：[agent_chat.py](../../server/service/agent_chat.py) 的 start_message / _execute / close；[agent.py](../../server/repository/agent.py) 的 start_message / checkpoint_assistant_message / complete_assistant_message / fail_assistant_message；[runtime.py](../../agent/chat/video_search/runtime.py)、[state.py](../../agent/chat/video_search/state.py)。

### B11：阶段超时后阻塞调用未退出，重试可能与旧执行重叠

排序日期：2026-10-16。

优先级：P2。状态：2026-10-14 源码核对与离线最小复现确认，待独立设计执行隔离；本次整理提交不扩大运行时重构范围。当前未测得真实供应商的重叠调用数量或额外费用。

#### 2026-10-16：EP04 复测遇到停机后租约回收失败

- B12 的第二轮复测在一次未知来源的正常停机后恢复，但 7 个 Chunk（`seq10、13–18`）以 `worker lease expired` 失败，冻结配置 `max_retry_count=0`，租约过期的失败计数耗尽可执行预算，主任务最终 `PARSING_FAILED`。这和“Provider 明确报错后重试耗尽”不同，停机造成的取消调用也不能算供应商故障。
- 该批旧子任务保持 lease 1，未被新进程重领，与停服时 7 条“blocking work still running”撤销记录在数量和执行批次上吻合；撤销日志未带 execution ID，不能逐行直接关联。已提交 13 个 Chunk 和 8 个 Refine 保留，但未提交执行没有全部恢复。主任务 lease 2 已领取不等于所有子任务已完成交接，最终 FAILED 也不是看帧工具报错。
- 源码链为：取消阻塞调用产生 `BlockingCallCancelled(execution_pending=True)` → Consumer `_release_cancelled()` 只在 completion 上挂延迟归还回调 → 真实阻塞线程退出后才归还租约。工作线程为 daemon，整个 API 进程退出时不能依赖其继续运行并触发回调；旧租约因此可能遗留。新进程 `reap_expired_chunks() → _reap_children() → _fail_child()` 把 retry 从 0 加到 1，因 `1 > max_retry_count=0` 置 FAILED，父任务记录 failure_status 并等待其余执行排空后失败收尾。旧任务事实与该代码链吻合；没有逐回调存活日志，不补造线程最后退出时刻。
- 回收后 `leaseExpireTime` 被清空，采集器没有保存这 7 条子租约的中间到期快照，因此只确认走了 `worker lease expired` 路径，不声称掌握每条租约的精确到期时间。[失败子任务与源码链明细](../../outputs/verification/mygo-ep04-refine-frames-20261016/failure-details.json) 保留了各自起始和失败落库时刻。关联源码：[取消归还](../../server/mq/video_parse_consumer.py)、[阻塞线程](../../server/utils/execution.py)、[过期回收与失败计数](../../server/mq/video_parse_queue.py)。
- 这是 B10 的计划停服次数语义和本项取消／实际退出／租约回收边界的真实反例。需在明确执行资源已退出及租约可归还的前提下，让计划停服不消耗业务失败预算，并覆盖取消期间已领取但尚未进入模型调用的任务；本轮只记录，没有扩改 Consumer／Queue。完整终态与证据见 B12。

#### 问题与已确认边界

- `ExecutionCalls.run()` 取消等待时，若阻塞线程仍未退出，会抛出带 `completion` 的 `BlockingCallCancelled`。它表示等待已取消，不表示底层 HTTP／SDK 调用、媒体处理或远端计算已经停止。
- Consumer 的阶段 deadline 分支取消任务后，用 `gather(return_exceptions=True)` 收尾，再抛普通 `TimeoutError`，丢失上述 pending 标记；实时理解 `_run_base()` 则捕获该异常后直接 `retry_chunk()`。两条路径都可能在旧调用仍运行时进入重试。
- Queue 的 `_retry_child()`／`_fail_child()` 会清空子任务 lease，并将未耗尽预算的任务改回待领取状态；`_phase_capacity()` 按当前 `RUNNING`／`REFINING` 记录统计名额。新执行因此可能与仍存活的旧阻塞调用重叠，实际运行中的工作数量可能超过配置的阶段并发值。
- **已有保护仍有效：** 旧执行提交必须通过任务身份、owner、lease version、阶段及期限校验；失效 lease 的迟到结果不能作为当前成果提交，索引仍受内容版本与代次约束。`ExecutionCalls` 的延迟清理和媒体工作区锁也会保留旧调用仍在使用的客户端／文件。
- **这些保护不等于外部调用停止。** lease fence 不能撤销已发出的请求，不能保证新旧调用不重叠，也不能保证供应商不重复计费；不能把“没有旧结果覆盖”报告成“没有重复执行”。

#### 离线复现证据

使用真实 `RealtimeUnderstandingService._run_base()` 与 `run_blocking()`，以受控线程模拟不响应取消的 Provider，queue／repository 使用替身；设置 50ms execution deadline，并在观测后主动释放线程。结果为 `provider_started=True`、`provider_exited_at_retry=False`、`run_base_returned_while_provider_running=True`。独立 Consumer 复现也确认 deadline 后已调用 `retry_chunk()`，而 `ExecutionCalls.pending` 仍为 `True`。

本次复现没有真实模型请求、开发数据库写入或服务重启；只证明取消、重试和线程退出的先后关系，不构成真实重复扣费的账单证据。相关现有离线回归 207 项通过，不能替代上述并发边界验收。

#### 待处理方向

1. 先明确阶段并发限制约束的是可领取任务，还是实际仍在运行的执行；将 lease 状态、调用实际退出、远端取消确认分别观测，不用同一个“已停止”状态替代。
2. 评估可取消客户端及可中断的独立执行进程，保留完成／取消回执和资源所有权。终止本地进程仍不保证供应商远端计算停止；支持异步任务或幂等键时，应保存供应商任务 ID 并查询、取消或复用，无法确认的远端执行明确标记为未知。
3. **只延迟本地 `retry_chunk()` 不足以修复。** deadline／lease expiry 的 reaper 仍可能回收并重新派发；领取、回收、正常停服及 pending execution 的处理必须采用一致规则，同时避免为等待一个无法退出的调用而无限阻塞恢复。
4. 保留旧结果提交校验及有界失败预算；记录旧执行尚未退出时的恢复决策、实际并发、重叠时长和重复调用用量。更换 Redis 或其他消息中间件不会自动停止旧线程／远端请求，也不会自动消除重复计费。

#### 验收条件

- 用可控 Provider 分别覆盖正常响应取消、忽略取消、迟到成功和迟到失败；同时走 Consumer 与实时理解入口，验证 deadline、手动停止、正常停服和 lease expiry 的结果。
- 设置阶段并发为 1，在旧调用仍运行期间触发重试与 reaper；以调用开始／实际退出事件计数验证所选并发策略，而非仅检查数据库中 `RUNNING` 的数量。若允许有限重叠，必须有明确上限与可观测记录。
- 旧执行迟到返回仍不能提交；媒体／客户端在实际使用结束前不能被清理；任务恢复不会因无限保留占位而永久停滞。
- 验证本地子进程与受管资源已退出；对远端取消是否成功、是否继续计费单独核对供应商回执及用量，无法证明时保留未知状态，不声称 exactly-once 或零重复费用。

依据：[阻塞执行与延迟清理](../../server/utils/execution.py)、[Consumer deadline 与取消交接](../../server/mq/video_parse_consumer.py)、[实时基础解析](../../server/service/realtime_understanding.py)、[重试／阶段容量／过期回收](../../server/mq/video_parse_queue.py)、[执行提交校验](../../server/repository/video_parse.py)、[媒体工作区锁](../../server/service/video_parse_media.py)。本项补充 B10 的恢复边界，不将队列中间件迁移作为前置修复。

### C13：Codex 已报告终止错误但未及时上抛，阶段累计时长也不能当墙钟

排序日期：2026-10-16。沿用原“Chat 联调问题（2026-10-16）”章节日期。

归类说明：终止错误传播已修复；本条同时记录的计时口径问题仍未修复，因此整体保留在 TODO。

优先级：高。状态：终止错误传播已在 `dd622aa` 提交并 push，API 随后重启；真实 Chat 复测已完成。耗时口径差异及跨实例遗留状态仍独立保留，不能由本项推导全部生命周期问题已解决。

- 先前约 10 分钟 Chat 样本在 Codex 初始连接即收到代理 `CONNECT 403`，Query／Evaluator 和检索工具尚未开始，不能归因于召回 Chunk 太多。旧链路等待最终 turn 事件，未消费结构化终止错误；SDK 的连接重试又可持续到外层 600 秒预算。没有证据是 Astra 额度不足，也未确定代理拒绝的具体策略。
- 修复在 Codex client 始终订阅 `ErrorNotification`，按 thread／turn 匹配保留错误；`will_retry=false` 或结构化认证 401／403 及时失败，保留可重试错误语义。关闭 `features.unbounded_connection_retries`，Agent 等待循环调用 `raise_if_failed`，关闭受管 transport 后沿既有异常链上抛，不解析任意 stderr 文本伪造终态。
- `CodexAgentModelError → AgentModelInvocationError → AGENT_MODEL_ERROR → Repository FAILED` 沿用已有 Chat 收尾；清空 phase、递增版本并持久提交后发布失败事件。无需新增数据库 enum；若失败落库也失败，则发 `answer.interrupted` 要求读取快照，不能假装已持久失败。B01 的跨进程遗留收尾仍为另一边界。
- 离线用真实 SDK 通知替身及 Codex Adapter／Supervisor／ChatService 验证“收到 fatal、没有最终 completed”时，在原 600 秒预算内不足 3 秒即可保存 FAILED；早到错误、其它 thread／旧 turn、503 可重试错误均有回归。实际提交树回归为后端 1808 passed / 56 skipped，前端 398 passed，typecheck／build 通过；这是前次分模块提交的验证，不是本次真实任务的调用次数。
- 随后的 EP04 连通探针与两轮真实 Astra Chat 成功，说明当时可调用；未通过修改代理绕过控制，也不能据成功反推已定位或修复旧代理策略。

**另一个可测问题是计时口径。** 第一次 EP04 复现问题的 `timing.totalDurationMs=131795`（约 02:12），但 `timing.startedAt → 持久化 updatedAt=197289ms`（约 03:17）。前者为阶段累计值，不能用于宣称用户只等待了两分钟；差额尚未逐段归因，不能直接称为模型推理。针对说话人的第二问分别为 `157659/157662ms`，两种口径约一致。后续应补齐未落入 stage 的等待与编排区间，在页面清晰区分处理统计与请求墙钟；本轮仅记录，没有修改计时实现。

依据：[Codex 通知处理](../../gateway/codex/client.py)、[等待与异常上抛](../../gateway/codex/agent.py)、[Chat 失败收尾](../../server/service/agent_chat.py)、[消息终态持久化](../../server/repository/agent.py)、[两轮 Chat 与计时](../../outputs/verification/mygo-ep04-speaker-review-20261016/comparison-summary.json)、[逐工具记录](../../outputs/verification/mygo-ep04-speaker-review-20261016/chat-replay-original/tool-timeline.json)。

### B10：正常停服没有解析任务交接，恢复消耗重试且整集收尾受调度阻塞

排序日期：2026-10-14。

优先级：高。类型：V2 解析生命周期与恢复调度缺陷。状态：诊断已确认；10:20 已通过现有接口恢复第 7、8 集的调度资格，解析生命周期／调度代码尚未修复，本轮没有重启 API。最初记录时间：2026-10-14 02:28–02:31（`+0800`）；10:13–10:22 的新现场见下方补充。这是解析阶段问题，与 B09 上传完成事务的死锁分开处理。

#### 02:28 历史任务快照与当时的重启判断

本机 MySQL 的 13 个未删除、非测试 VideoParseTask：

| 视频 | 数据库状态 | 本次核验的执行情况 |
|---|---|---|
| 第 1、2、3、4、6 集 | COMPLETE | 已完成，不因重启重做 |
| 第 5 集 | PARSING | 唯一活跃整集租约，owner=`local-consumer-10201`，attempt=1/max=3；02:28 快照有 10 个 COMPLETE、4 个 UNDERSTANDING、10 个 QUEUED Chunk，4 个在途 Chunk 的 retry 均为 0/max=2 |
| 第 7、8 集 | PARSING | 主任务无 owner，last_error=`worker lease expired`，attempt=1/max=3；已完成 Chunk 分别 8/24、19/24，等待整集调度恢复后继续派发 |
| 第 9 集 | INDEXING | 主任务无 owner，last_error=`worker lease expired`，attempt=1/max=3；24/24 Chunk 和 22/22 Interval 已 COMPLETE，整集成功状态尚未收尾 |
| 第 10–13 集 | PENDING | 尚未开始，attempt=0/max=3 |

按该快照，重启一次不会仅因上述活跃任务的重试预算直接耗尽而失败，但不能描述为无损、立即续跑：第 5 集尚未提交的在途 Chunk 会重做，并消耗重试；已提交结果保留。快照会随运行变化，执行重启前必须重新读取在途状态，不能把这组次数当作永久保证。

02:31 的待领取主任务顺序为第 8 → 9 → 7 → 11 → 10 → 12 → 13 集；第 5 集仍占据唯一主调度名额。如果此时再中断第 5 集，回收会把它的 next_execute_time 推到当时的“现在 + 30 秒”，可能排在这些已有任务之后。日志显示上一进程在 01:15:22 开始 shutdown，新进程 10201 在 01:43:13 启动；这些是既有记录，本轮没有操作重启。

#### 已确认的原因

1. **API 与解析 Consumer 在同一进程，重启 API 同时中断解析。** Consumer 虽在独立线程运行，生命周期仍由 API lifespan 管理。
2. **正常停止直接取消在途任务，没有归还 Video／Chunk／Interval 租约。** `run_forever()` 的 finally 调用 cancel + gather，三个执行入口对 CancelledError 仅重新抛出。新进程依靠 reap_expired 处理旧租约；当前默认租约 300 秒、失败退避 30 秒，还要等待调度名额。Prefix 的正常取消已有主动归还逻辑，是例外。
3. **计划停服被当作失联，消耗失败预算。** Video 重新领取增加 attempt；过期时 attempt 已达 max 则停止自动恢复。Chunk／Refine／Index 过期直接增加 retry，超过冻结配置中的 max_retry_count 则失败。当前业务任务上限分别是 3 次领取、2 次重试；反复中断可以耗尽预算，已有成果并不会因此被删除。
4. **持久化边界是完成一个 Chunk，不是完成一次 ASR／模型调用。** ASR／OCR／Canvas 在理解期间只存内存；整段理解结束才提交。取消清理本租约临时媒体，所以未提交 Chunk 的媒体准备和 ASR／理解需要重做。已提交 Chunk、Refine 结果、索引回执会保留，不能说重启必然整片从头解析。
5. **整集恢复及成功收尾共用唯一主调度名额。** 一个 Video handler 一直持有名额直到整集完成。领取按 next_execute_time、create_date、id 排序，不按恢复优先。子任务可以跨视频继续领取，但未派发或需要重新派发的 Chunk 依赖各自父调度；Interval 索引提交只更新子任务，不归并父成功状态，过期回收也不补成功归并。这解释了第 9 集子任务全完成却仍显示 INDEXING：重新领取父任务后可直接收尾，无需重跑已经完成的内容。

#### 证据边界与后续验收要求

- **系统记录确认了停服前的两次自动睡眠。** 只读 `pmset -g log`：00:16:34 进入 Idle Sleep，00:21:02 唤醒（268 秒）；01:02:59 进入 Idle Sleep，01:14:30 唤醒（691 秒）。第 8 集 00:21:43、第 9 集 01:14:30 的主任务租约丢失均在相应唤醒后，且早于 01:15:22 停服。应用同时存在约 267／688 秒的墙钟与 monotonic 执行耗时差，与系统暂停吻合。睡眠期间无法续心跳，而数据库租约按 UTC 墙钟到期；这些已有失联不能归因于后来的服务重启。第一段睡眠虽小于 300 秒，叠加入睡前已消耗的心跳周期仍可能越过租约期限；未拿到入睡前逐次心跳时间，不声称精确还原了该段到期瞬间。
- 已有单元测试覆盖过期接管、旧 owner 隔离、部分成果复用等；没有找到“真实在途任务 → SIGTERM → 新进程恢复 → 不扣计划停服重试”的完整验收。本轮只读测试源码，没有运行真实模型或中断业务任务。
- 修复首先应让计划停服具备明确交接：停止领取、在受控期限内完成或取消在途工作、以当前租约版本归还可续跑任务，并与真实执行失败的重试计数分开。还需要让已完成子任务的整集成功收尾不被长时间运行的另一个 Video 阻塞。ASR 等细粒度成果复用是独立的重做成本优化，不能替代停服交接。

#### 10:13–10:22 现场：服务存活，但恢复等待没有明确状态和手动入口

本轮按用户要求执行 `./start.sh status` 和 `./start.sh start`。MySQL、API／Parse Consumer、Web、Elasticsearch 均在运行；启动流程复用原进程，API PID 仍为 10201，未中断第 13 集的在途执行。`/healthz` 返回 HTTP 200，`parseConsumer.status=running`、`failure=null`；直连 API 与 Web 代理的任务列表一致。B09 的上传事务修复仍只是磁盘代码，本轮没有通过重启加载。

| 视频 | 10:13 现场 | 本轮处理与 10:21 验证 |
|---|---|---|
| 第 1–6、9 集 | COMPLETE，共 7 集 | 已完成成果保留 |
| 第 7 集 | PARSING_FAILED，attempt=3/max=3，主租约已清空，last_error=`worker lease expired`；8/24 Chunk、2/22 Interval COMPLETE | 10:20 调用现有 `retry-parse` 返回 200，原 task ID／version=1 保持，max_attempts 补到 4；上述成果数量不变，等待整集恢复调度 |
| 第 8 集 | PARSING_FAILED，同样因主任务租约失效耗尽 3 次领取；19/24 Chunk、9/22 Interval COMPLETE | 同样返回 200，原任务不变、max_attempts=4；已完成成果数量不变，10:21 已有两个 Chunk 进入 UNDERSTANDING |
| 第 10–12 集 | PARSING，但主任务 owner／expiry 均为空，attempt=2/max=3，last_error 同为租约过期；已完成 Chunk 分别为 4／0／10 | 已到可领取时间，仍等待整集协调器；已有可执行子任务不一定完全停止，例如第 12 集有一个 Interval 新完成。不能把“主任务无 owner”等同于“所有子任务无人执行” |
| 第 13 集 | PARSING，有效 owner=`local-consumer-10201`；24/24 Chunk COMPLETE，正在精修 | 10:21 精修结果达到 20/22，另外两个 Interval 为 REFINING；主租约持续续期。Consumer 没有整体停摆 |

10:24 最后复核：四项服务状态仍正常，第 13 集已从 PARSING 推进到 INDEXING；第 7、8、10–12 集仍无主任务 owner。以上恢复等待尚未全部解除。

10:21 主任务的到期候选顺序为第 11 → 10 → 12 → 7 → 8 集。`VideoParseConsumer.run_once()` 仅在 `_video_tasks` 为空时领取下一个整集任务；第 13 集仍持有唯一协调器名额，因此其他集即使 `next_execute_time` 已到，也不会立即领取主租约。已派发的部分子任务仍能跨视频执行；需要重新派发的 Chunk 则依赖父协调器。这是代码中的调度策略限制，不是发现了新的 MySQL 死锁，也不能从“服务健康”推导出“每个任务都在推进”。

**今晨租约失效另有新的睡眠证据。** `pmset -g log` 显示 02:49:14 进入 Clamshell Sleep（合盖），之后反复 Maintenance Sleep／DarkWake，直到 09:41:21／09:41:27 才记录完整 Wake；本轮不能称为 Idle Sleep。第 8 集第三次主任务失败在 08:49:38，恰逢 08:41:02 起睡眠 516 秒后的 DarkWake；第 7 集第三次失败在 08:54:53，前面先睡 252 秒、短暂唤醒后又睡 38 秒，最后于 08:54:52 DarkWake。第 11、10、12 集的主任务失败分别在 09:07:00、09:31:19、09:39:55，也紧邻短暂唤醒。结合 DB 的 `worker lease expired`，这些记录强烈支持反复睡眠导致续租中断；尚未逐条取得最后一次成功 heartbeat，不声称每个失败都只有这一个原因。应用日志里的阶段标签 `prepare` 覆盖整集协调器，不表示故障发生在上传或尚未解析。

上述主任务失败日志均为 `retry_recorded=false`：当前 owner／租约已不符合续排写入条件，不能把日志中计算出的 retry_at 当成成功重新排队。09:41 完整唤醒后另外出现 Ark API 连接失败／DNS `gaierror`，且 `retry_recorded=true`；这是模型调用的网络故障，须与睡眠时的租约失效分开记录。本轮没有修改系统睡眠设置，也没有通过反复重启来尝试恢复。

**手动入口缺口已同时由页面、接口和源码确认：**

- 第 7、8 集在本轮恢复前，任务页确实有“重试解析”按钮，列表 `canRetry=true`。`POST /api/uploads/retry-parse` 以 `uploadSessionId + tenantId + expectedJobVersion` 恢复最新失败任务；它保留已完成成果及执行历史，为父任务补足至少一次领取额度。它不建立新解析版本，也不等于抢占当前整集协调器。
- 第 10–12 集仍是 PARSING，列表同时返回 `canRetry=false`、`canReparse=false`。前端据此隐藏操作，后端也只允许失败终态走 retry；直接调用接口不能绕过。当前没有为“已接受、非终态、主租约已释放、等待恢复”提供专门的手动继续入口。
- 同一条记录却仍返回“视频解析失败，请重试”的公共错误，任务卡显示“正在解析画面与对白”。第 7、8 集续跑成功后也暂时保留旧 `last_error`，直到父任务再次领取时才清空。当前页面混淆了正在执行、恢复排队、最终失败，无法帮助用户判断是否需要操作。
- 上传弹窗的“重试”调用上传恢复／`completeUpload`，不是解析 `retry-parse`；已经上传成功的解析卡住问题不能靠重复完成上传来修复。

**待实施的恢复入口必须与调度一起设计。** 对最新任务校验版本、删除状态、失败意图及父／子／Prefix 租约；正在执行时返回明确原因，已经排队时返回幂等的排队结果，确需恢复时保留完成产物并唤醒有界恢复调度。不能仅以 `lease_owner=NULL` 就清空子任务或强制重做，也不能只是新增一个把状态改成 PARSING 的按钮——这两集调用现有 retry 后仍需等待父协调器，已证明“重新排队”与“马上续跑”不是一回事。页面需要直接展示恢复等待原因、下一次调度时间和可执行操作；验收须覆盖“另一个长任务仍运行时，中断任务也能在有界时间内恢复”，并保持模型调用并发限制。

本轮另修复了一个启动脚本的小问题：Web 页面品牌已是 Seekino，旧探针硬编码 HTML 必须包含 `seekino`，错误拒绝复用正常的 5173 端口。`scripts/local_services.py` 改为检查 Vite 客户端与 `/src/main.tsx` 入口，随后仍验证 Web 的资料库 API 代理；修改后 `./start.sh start` 成功。此探针错误与解析调度缺陷分别处理。现有启动脚本测试为 24 passed／8 failed；8 个失败都来自 `_model_config()` 测试替身缺少当前配置函数使用的 `normalized_models`。在测试进程内恢复旧探针后，仍得到同样的 8 个失败，未修改这些无关测试或配置代码。

补充依据：[手动续跑接口](../../server/api/uploads.py)、[续跑状态校验和成果保留](../../server/repository/upload.py)、[操作能力投影](../../server/api/jobs.py)、[任务页按钮](../../web/src/features/tasks/TasksPage.tsx)、[上传恢复入口](../../web/src/features/uploads/UploadProvider.tsx)、[启动探针](../../scripts/local_services.py)；本机 10:13、10:21 MySQL 快照、10:20 两次续跑 HTTP 200 与 10:22 健康／任务列表检查。这里记录的是恢复资格与已观察到的子任务推进，不代表六个未完成视频已经解析完成。

#### 10:43–10:47 新现场：已恢复调度，但模型账号欠费导致失败

本次再次检查时，问题已变化，不能继续把所有失败解释为租约或排队：

- 10:43 第 13 集已 COMPLETE，第 12 集已经领取第三次主租约；第 10、11 集陆续出现失败 Chunk。它们父任务的 `VideoParseTask is draining after a terminal failure` 只是发现子任务失败后停止派发的结果。
- 读取失败 Chunk 的原始 `last_error`，第 10、11、12 集均为 HTTP 403、`AccountOverdueError`，供应商明确返回 `account has an overdue balance`。10:47 再检查，第 7 集四个失败 Chunk 也都是相同欠费错误；这些 Chunk 的 retry_count 均为 3。
- 10:47 第 8、13 集已 COMPLETE；第 7、10、11、12 集均为 PARSING_FAILED，任务列表 `canRetry=true`。失败任务目前已有安全的手动重试入口。本轮没有继续对欠费任务重试、切换模型或重启服务。
- 第 8 集在 10:43 已有 24/24 Chunk 和 22/22 Interval COMPLETE，但父任务仍为 PARSING；10:47 才通过第四次领取完成父状态收尾。说明自动成功归并缺口仍在，只是本次最终轮到了它。

**即时恢复顺序：** 如果沿用现有模型，先恢复实际使用的 Ark 账号服务，再通过任务页“重试解析”或原 `retry-parse` 恢复第 7、10、11、12 集。保留已完成成果和原任务版本，不需要重新上传或整集重新解析。服务重启不能修复账号欠费。若要切换模型，需要先确认可用来源及对应阶段；第 10 集冻结配置中 ASR 和 Embedding 来源均为 `ark_default`，理解来源为 `codex_local`，仅更换理解模型不会替换 Ark ASR／Embedding。

当前 `VideoParseConsumer._run_chunk()` 对所有 Exception 走同一 `retry_chunk()`，队列每次增加 retry_count，所以欠费错误也会耗尽自动重试额度。当前生成模型 fallback 只接收 `ContentFilterError`，transcription／embedding 则没有对应 fallback，不能据“配置了备用模型”断言欠费可自动绕过。后续需要给欠费／认证等人工干预错误明确分类和可操作提示，避免按临时网络错误反复重试；本轮尚未修改该机制。

**具体失败模型已由请求 Trace 确认。** 第 10、11、12 集失败调用的 operation 均为 `candidate_transcript`（ASR 转写），provider=`ark`、source=`ark_default`、request.model=`ep-20260911212241-gg8bm`，响应均为 403／`AccountOverdueError`。同一端点在当日 10:37:56 的成功响应明确返回 model=`doubao-seed-2-0-lite-260428`，因此可定位为 ASR 使用的豆包 Seed 2.0 Lite 所属 Ark 账号返回欠费；这些调用不是本地 Codex 理解调用，也不是 Embedding。连接来源配置使用 `ARK_API_KEY`。失败证据：[EP10 请求／错误](../../outputs/model-traces/b2fac332-22cd-4769-9563-9cad42f02534/events.jsonl)、[EP11 请求／错误](../../outputs/model-traces/b5d9df7e-bbb5-44ab-a586-6242de1e564c/events.jsonl)、[EP12 请求／错误](../../outputs/model-traces/4bc3bc1c-1db3-4533-9ecc-b374f1d1800a/events.jsonl)；端点模型名依据：[紧邻的成功请求／响应](../../outputs/model-traces/7de40feb-965d-4554-951e-c9f59f51d290/events.jsonl)。上述判断使用实际请求与响应，没有拿当前 YAML 的新模型名替代历史调用证据。

**页面为何只报“视频解析失败，请重试”：错误上报链存在两层信息丢失。** Chunk 的 last_error 保存了欠费原文，但 `_aggregate_video_task()` 只归并状态／failure_status，没有将失败子任务的根因传到父任务；父协调器随后因 failure drain 退出，`retry_video()` 将父 last_error 写成 `VideoParseTask is draining after a terminal failure`。`project_video_parse_task()` 又完全不使用错误内容，只要 last_error 存在就生成固定 `PROCESSING_FAILED` 文案。`TasksPage` 原样显示这段后端 message，前端没有进一步吞掉欠费信息。

这处展示缺陷需要沿后端错误链修复：保存和保留可识别的供应商错误码、实际失败阶段与调用模型，避免后续收尾异常覆盖首要根因；API 将已知欠费错误映射为明确的公开文案，未知异常才使用通用提示，保留内部原文不直接透出的边界。现存失败任务也应能从失败子任务补出根因，不能要求用户再次触发欠费。现有 `Failure.code/message` 和前端展示已足够承载准确提示，不必为此新增按钮。验收须覆盖“ASR 欠费 → 子任务失败 → 父任务收尾 → 页面仍显示账号欠费”，同时保留敏感原文不泄露的测试；当前投影测试 `test_video_parse_task_projection_hides_internal_failure_detail` 将所有错误固定泛化，需要同步调整预期。此修复尚未实施。

最小独立代码修复可先补无主父任务的成功归并：扩展现有 Queue 周期收尾，在新的事务中锁定符合条件的父任务，复用既有归并规则；全部子任务完成时直接完成父任务，不再为了收尾领取一次主租约。保持失败优先、在途子任务保护和旧租约隔离；候选筛选及 limit 不能使完成任务长期排在不会变化的等待任务之后。这一补丁与账号恢复分别处理，多父协调器、停服交接和页面等待展示随后按独立范围实施。

依据：[Consumer 异常处理](../../server/mq/video_parse_consumer.py)、[重试计数与周期归并](../../server/mq/video_parse_queue.py)、[Fallback 触发条件](../../gateway/model_factory.py)、[模型阶段约束](../../gateway/config.py)；10:43–10:47 本机数据库的失败 Chunk、冻结配置及任务列表只读快照。供应商账号实际余额未直接查询，欠费判断依据是其明确返回的错误码与错误消息。

#### DB Queue 与 Redis 的判断（2026-10-14 补充调研）

**当前建议：先修复任务生命周期与恢复语义，保留 MySQL 队列；将 API 与解析 Worker 拆成独立进程。Redis 可作为后续消息分发优化，但不是本次脆弱性的必要修复条件。** 本段为待实施方案，尚未调整解析架构或迁移 Redis。

需要区分三件事：队列负责“哪项工作该交给哪个 Worker”；任务状态机负责“中断后从哪里继续、什么时候算失败”；进程部署决定“重启 API 会不会同时停止 Worker”。换 Redis 主要改变第一件事，后两件事仍要由应用实现。

| 当前问题 | 所属机制 | 只换 Redis 能否解决 |
|---|---|---|
| 重启 API 同时取消解析 | API 与 Worker 共进程 | 不能；独立 Worker 使用 MySQL 队列同样能隔离 API 重启 |
| 正常停服不归还租约，等待过期 | Worker 停止与交接流程 | 不能；Redis pending 消息也需要明确的恢复／接管逻辑 |
| 计划停服、睡眠消耗失败预算 | 领取次数／失败次数混用 | 不能；Redis 投递次数也不应直接当业务失败次数 |
| ASR 成功后中断仍重做 ASR | 片段内中间成果未持久化 | 不能；队列不保存内存中的解析成果 |
| 第 9 集全部子任务完成，父仍 INDEXING | 成功归并依赖唯一主调度名额 | 不能；要调整归并触发和恢复调度 |
| 空闲轮询、任务量增大后的 DB 调度负担 | 消息发现与分发 | Redis Streams 的阻塞读取和消费组可能有帮助；本轮没有吞吐／锁等待基准证明这里是瓶颈 |
| 电脑自动睡眠，Worker 无法续心跳 | 执行宿主暂停 | 不能；同机 Redis 不能让睡眠中的 Worker 工作，异机接管仍需要可访问的媒体和正确的旧执行隔离 |

MySQL 本身并不排斥队列表：官方明确将 `SKIP LOCKED` 用于多会话访问队列表时避开已锁记录。Seekino 的领取逻辑已经使用它；当前还拥有“创建业务记录和待执行任务可在同一数据库事务提交”的便利。需要修复的是我们在其上实现的停止、重试和调度规则，不能由这次故障推导出 MySQL 队列必然不可靠。[MySQL 锁定读取](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)、[当前领取代码](../../server/mq/video_parse_queue.py)。B09 的 Video 外键／Series 事务锁顺序也是独立问题：换成 Redis 分发任务不会消除这些业务 SQL。

#### 本地 RagFlow 实际怎么做

核对对象是同级 `../ragflow` 的本地源码，HEAD=`313ca90f6abd7682fe8523e16fd67b3653a3fa84`，核对时该工作区干净；不是对所有 RagFlow 发布版本或线上配置的概括。它的 Python 和 Go ingestion 路径必须分开看。

**Python 路径使用 Redis 协议 Streams，当前 Compose 对应服务是 Valkey。** `queue_product()` 用 XADD 入队，`queue_consumer()` 用 XREADGROUP 消费，`RedisMsg.ack()` 用 XACK 确认。启动时先读取同名 consumer 的未 ACK 消息，再取新消息；业务 Task、进度和 retry_count 仍保存在关系数据库，解析成果也由业务存储承接。API 与 task executor 有独立启动进程，这种部署隔离值得借鉴。[Redis 封装](../../../ragflow/rag/utils/redis_conn.py)、[任务接收与执行](../../../ragflow/rag/svr/task_executor.py)、[TaskService](../../../ragflow/api/db/services/task_service.py)、[启动脚本](../../../ragflow/docker/entrypoint.sh)。

但该 Python 路径并不自动提供我们缺少的全部能力：

- 未 ACK 回放仍会经过 `TaskService.get_task()` 并增加数据库 retry_count；前三次领取可执行，第四次领取读到 `retry_count >= 3` 时标记失败并放弃。`TE_RUN_MODE=0` 的重构执行器也共用这层领取和 ACK 逻辑。
- 当前可达消费路径没有找到 XCLAIM／XAUTOCLAIM 的跨 consumer pending 接管；`get_pending_msg()`／`requeue_msg()` 有定义但未找到调用。失联 Worker 清理删除注册与心跳，不等于已将其消息转给其他 consumer。不能把“同名 Worker 重启可回放”说成“任意 Worker 都能自动接管”。
- 普通执行异常会记录失败后走 ACK；协程 CancelledError 则跳过尾部 ACK，留下 pending。有限等待在途完成只在启用 `MAX_TASKS_PER_WORKER > 0` 的退出流程中执行，默认值是 0，不能概括为默认优雅停服都会等待任务完成。

**本地 Go ingestion 的现行队列是 NATS JetStream，已经不是 Redis 队列。** `InitMessageQueue()` 只装配 NATS；KVrocks 中虽然保留 Redis Streams helper，但没有发现 Go 业务调用。消息主要携带 task_id/task_type，业务状态仍查数据库。值得参考的是它明确区分“停服中断”和“用户取消”：停服保留非终态，交给 broker 再投递；终态通常 ACK，未终态 NACK；Stop 先停取新任务，再取消执行并有期限地等待清理，超时留下未确认任务。长任务仍需心跳续期。[MQ 装配](../../../ragflow/internal/engine/global.go)、[Go 任务执行与停止](../../../ragflow/internal/ingestion/service/ingestion_service.go)、[消息合同](../../../ragflow/internal/common/task.go)。

这仍不等于模型调用中间位置可继续：Go pipeline 明确从图入口重放，由组件缓存决定复用哪些已有成果；另有已写 DB、尚未确认入队任务的启动补发。**可借鉴的是停服语义、消息确认与业务终态的关系、结果复用和补发机制，而不是仅复制一个 Redis 依赖。** [Pipeline 重放边界](../../../ragflow/internal/ingestion/pipeline/pipeline.go)、[持久任务补发](../../../ragflow/internal/service/ingestion_task_service.go)。

#### Redis 能额外提供什么，以及增加什么成本

Redis Streams 消费组提供消息分摊、待确认记录和阻塞等待；XPENDING 可查看已投递但尚未确认的消息，XCLAIM／XAUTOCLAIM 可用于转移符合条件的未确认消息。它可以减少用业务 DB 高频轮询发现任务的需要，并为更多独立 Worker 提供统一消息分发能力。多 Worker 本身不是 Redis 独有能力，当前 DB 领取也有并发隔离；是否值得迁移应看领取延迟、调度 SQL 成本和实际扩展需求。[XREADGROUP](https://redis.io/docs/latest/commands/xreadgroup/)、[XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)。

如果后续采用 Redis，应选择具备确认／恢复语义的 Streams 消费组或经过验证的任务框架；普通 Pub/Sub 断连时会丢消息，不适合作为持久任务队列的唯一来源。[Redis Pub/Sub 投递语义](https://redis.io/docs/latest/develop/pubsub/)。还必须设计以下边界：

1. **数据库与消息发布的一致性。** “任务已在 MySQL 提交，进程在 XADD 前退出”会留下没有消息的任务。可采用与业务任务同事务保存的待投递记录，再由发布者重试发送；或以 DB 待执行状态为依据做可重放的投递和补偿扫描。不能只写 `commit(); xadd()` 就宣布可靠。
2. **允许消息重复，成果提交必须幂等。** “成果已提交，XACK 前退出”会重复投递。新 Worker 应先读任务版本和完成状态，已完成则确认跳过；未完成才领取执行。Redis 的消息 ownership 不会替旧进程撤销已经发出的模型请求，仍需保留任务版本／租约校验和索引版本保护，拒绝旧执行迟到提交。这是跨 DB、模型和索引写入边界的设计要求。
3. **Redis 自身的持久化和运行维护。** 需要配置并验证重启恢复、持久化、容量及清理策略。RDB 是定时快照，AOF 的刷盘策略也有不同的丢失窗口；不能把内存队列当成天然比现有 DB 更耐故障。任务可由 DB 补投时，可降低消息层丢失对业务的影响。[Redis 持久化](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。

Seekino 虽已有 `redis>=5,<7` 客户端依赖，目前 Redis 接入属于 cloud auth 的 OTP／限流／查询并发控制；local 模式不创建该客户端。它不是已经部署好的解析队列，迁移仍需独立的队列配置与生命周期管理，不能把认证 Redis 的职责直接混进解析队列。[当前 Redis 接入](../../server/caller/redis.py)、[认证配置](../../server/config/auth.py)。

#### 建议分批解决与验收

以下是待实施的职责划分，不代表本轮已经改代码或授权迁移。先完成不依赖新中间件的修复，每批单独验证；不把 Redis 迁移作为这些修复的前置条件。

| 批次 | 具体目标与归属 | 必须通过的验收 |
|---|---|---|
| 1：停服交接与次数语义 | Consumer 停止领取并冻结新派发，继续维持在途租约心跳；在有限期限内让在途步骤提交或取消，确认本地执行及其受管子进程已退出；之后按 owner + lease_version 条件归还未完成任务。Queue 区分计划停服、失联恢复与真实执行错误，正常停服不扣业务失败预算 | 有在途 Chunk／Refine／Index 时正常停启，保留已提交成果，不因计划停服耗尽预算；归还后立即具备领取条件；旧 owner 的迟到提交被拒绝 |
| 2：成功收尾与恢复调度 | Repository／Consumer 让子任务完成后的成功归并不再依赖唯一整集名额；加入有界的恢复调度，兼顾已有任务与新任务，保持当前并发预算 | 一个视频长时间运行时，另一个子任务全完成的视频能及时变为 COMPLETE；中断的第 7／8 集能继续派发；新任务也不会永久饿死 |
| 3：独立解析 Worker | 复用现有业务装配和 DB Queue，提供独立 Worker 进程入口，API 不再默认拥有该 Worker 的生命周期；启动脚本分别管理 PID／退出与健康状态 | 只重启 API 时 Worker PID、在途执行和失败预算不变；单独重启 Worker 时满足第 1 批；同机源码变更仍应遵守任务配置／代码版本边界 |
| 4：片段内成果复用 | 在语音识别等昂贵步骤成功后保存完整结果，按原视频身份、片段范围、步骤配置／模型及任务版本校验后复用；沿用现有持久存储，不依赖临时目录 | ASR 已成功、理解未完成时中断，恢复跳过 ASR，仅重做未完成理解；输入／配置变化不误用旧结果；模型用量记录不因复用重复记账 |

第 1 批有两个不能漏掉的细节：一是 `claim_video()` 每次领取都会增加 attempt_count，**只增加 release、却保留原来的领取与 max_attempts 判定，仍会因正常停启耗尽预算**；必须一起明确执行历史与失败余额的口径。正常交接不回退已发生的模型用量／调用预算，每次重新领取仍递增 lease_version。二是不能在旧执行还可能写入时先归还租约；本地执行无法在期限内停止时，不主动清空租约，应停止续租并退出或终止旧进程，由租约过期接管兜底。已发出的远端请求可能仍在执行，接管后仍须以租约版本和索引版本拒绝迟到写入，不能降低版本校验来强行续跑。失联恢复也应有独立的次数／速率限制和告警，避免把确定性崩溃变成无限重放。

独立 Worker 目前没有正式常驻入口：`tests/manual/server_mq_once.py` 只是单次消费的测试入口，不应直接当生产 Worker 启动。未来需要修改进程装配与启动脚本，这一批应单独评估；同机独立进程也不能消除整台电脑睡眠。[现有装配](../../server/main.py)、[本地服务脚本](../../scripts/local_services.py)、[单次测试入口](../../tests/manual/server_mq_once.py)。

共同验收还应包括：强制退出或暂停超过租约后可接管；旧进程恢复不能覆盖新结果；在“结果提交成功但确认／响应丢失”的窗口重复执行不会重复发布；日志区分 shutdown／lease_expired／model_error，并能看到剩余重试、恢复等待和重复调用成本。停机测试应先在隔离数据库与可控模型替身中执行，不拿当前 13 个真实视频做破坏性演练。只有在上述行为正确后，仍测得队列 SQL 开销或领取延迟显著、或者需要更复杂的分布式消息分发，才单独评估 Redis Streams／其他 broker。

依据：[API 生命周期](../../server/main.py)、[Consumer 停止／领取／心跳](../../server/mq/video_parse_consumer.py)、[租约回收与重试预算](../../server/mq/video_parse_queue.py)、[持久结果提交与整集归并](../../server/repository/video_parse.py)、[整集调度](../../agent/video_parse_v2/runtime.py)、[Chunk 理解提交边界](../../agent/video_parse_v2/understanding/workflow.py)；本机 MySQL 的上述只读快照，以及 `outputs/server/logs/development/api.log` 的既有记录。

#### 2026-10-14 专用ASR替代调研

状态：已核对官方价格与接口文档，尚未接入或对 MYGO 音频做质量实测。范围仅为日语对白转写及时间戳，不替换画面理解／Refine。以下国内方案价格为人民币常规按量价格，不含免费额度、资源包折扣或额外存储／流量。

**现有 Lite 用量审计，不能与充值金额直接画等号。** 按当前 13 个 VideoParseTask ID 关联本地 `outputs/model-traces/*/events.jsonl`，找到 377 次 `candidate_transcript` 请求：280 次 provider completed 且有完整 usage，94 次 error（73 次欠费 403、15 次连接错误、4 次 ReadTimeout、2 次 ArkTimeout），3 次没有完整终态／usage。以下只计算 280 次成功返回的用量，未获得用量的请求不计入估算，但不据此承诺供应商没有计费；也未查询供应商余额、整账号账单或其他项目消费。

| 已返回的 ASR 用量 | Token 数量 | 按普通在线、输入 ≤32K 的官方刊例价估算 |
|---|---:|---:|
| 非音频输入（视频／文字） | 6,876,544 | 0.6 元／百万，约 4.13 元 |
| 音频输入 | 103,642 | 9 元／百万，约 0.93 元 |
| 输出（包含推理 Token） | 395,787 | 3.6 元／百万，约 1.42 元 |
| 合计 | 输入总计 6,980,186；输出 395,787 | **约 6.48 元，属有记录调用的估算，不是供应商账单** |

280 次成功调用均在 32K 输入档内、cached_tokens=0。输出中 reasoning_tokens=218,031，约占输出 55.1%，已经包含在输出总数内，不能重复收费估算；请求中未传 thinking／reasoning 关闭参数，实际响应确有推理用量。同一批任务中存在 30 次重复成功 ASR 调用，主要集中于第 1、2 集，不能把所有重复请求或所有 403 都算成付费重做。2026-10-13 23:00 后的 179 次成功调用按同一口径约 4.13 元；2026-10-14 当天的 155 次约 3.59 元。[普通在线计价及分模态费率](https://docs.volcengine.com/docs/ark/model-pricing?lang=en&redirect=1)

全部 377 次 ASR 请求都使用 `input_video`、fps=1；当前 `VideoASRAgent` 将视频窗口送给通用多模态模型，即便业务只要求转写。ASR 成果仅在整个 Chunk Understanding 完成时一起提交，中途失败会重新转写。这分别带来视频输入开销和重复调用开销；更换专用音频 ASR 与增加 ASR checkpoint 是两项不同的降本措施。[视频转写输入](../../engine/common/transcription.py)、[片段内成果提交](../../agent/video_parse_v2/understanding/workflow.py)

**同批次 Embedding 的已记录费用更高。** MySQL `model_usage` 中，关联上述 13 个任务的成功 Embedding 均为 `ark / doubao-embedding-vision-250615`，按不同 call ID 记录；成功视频 Embedding 为 449 次，图像输入 17,377,605 Token、文本输入 245,204 Token；成功纯文本 Embedding 为 400 次、文本输入 722,738 Token。按官方图像 1.8 元／百万、文本 0.7 元／百万估算，合计 **31.9572484 元，约 31.96 元**，其中图像输入约 31.28 元。另有 1 次失败视频 Embedding 缺少 usage，未计入估算。2026-10-13 23:00 后的 Embedding 按同一口径约 22.62 元。还存在较早的 Pro 视觉理解调用，本段并非整个账户的账单核对；目前只能判断这批已记录用量中 Embedding 比 ASR 更贵，不能据此解释全部充值去向。换 ASR 不会减少检索索引的 Embedding 费用。[方舟向量模型官方价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=en&redirect=1)

DB 同时确认这 13 集总时长为 18,465,233 ms，即 **5.12923 音频小时**；当前 312 个 Chunk 的 window 长度总和等于原片段长度总和，没有实际重叠，不应把默认 overlap 配置当作本批费用成因。上述 30 次额外成功 ASR 均为同一 Chunk、相同媒体 SHA／接入点在不同 lease_version 下重新调用，估算约 0.70 元，确有浪费但不是全部消费的大头。按完整音频只转写一遍、无重试／额外精修计算，下面三个国内方案整批约为 Paraformer **1.48 元**、Qwen3 **4.06 元**、豆包录音 2.0 **4.10 元**；这是全量一次转写的对照预算，不能与当前尚未全部完成、且含重试的 Lite 估算直接相减作为实际节省。

| 候选 | 常规单价与服务地域 | 日语与时间戳 | 非实时文件输入限制／调用方式 |
|---|---|---|---|
| 阿里百炼 `paraformer-v2` | 华北 2（北京）：0.00008 元／秒，折合 **0.288 元／音频小时** | 支持日语；句级和字／词级毫秒时间戳 | ≤12 小时、≤2 GB；公网可访问音频 URL，HTTP 异步提交／查询 |
| 阿里百炼 `qwen3-asr-flash-filetrans` | 华北 2（北京）：0.00022 元／秒，折合 **0.792 元／音频小时** | 支持日语；默认句级，`enable_words=true` 开启字级，日语在字级支持名单内 | ≤12 小时、≤2 GB；DashScope 异步文件转写，不是同名实时或 Chat 接口 |
| 火山豆包录音文件识别模型 2.0，`volc.seedasr.auc` | 火山引擎国内语音服务：**0.8 元／音频小时** | 指定 `audio.language=ja-JP`；`show_utterances=true` 返回句／词级毫秒时间戳 | <5 小时、<512 MB；音频 URL，`/api/v3/auc/bigmodel/submit` 提交后查询 |

表格依据：百炼 [Paraformer 模型信息](https://help.aliyun.com/zh/model-studio/paraformer-v2)、[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[语言及音频规格](https://help.aliyun.com/zh/model-studio/asr-model)、[非实时语音识别／时间戳](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)；火山 [计费说明](https://docs.volcengine.com/docs/DoubaoVoice/Billinginstructions-21?lang=zh)、[录音文件标准版 API](https://docs.volcengine.com/docs/DoubaoVoice/LargemodelrecordingfilerecognitionstandardversionAPI?lang=zh)、[产品规格](https://docs.volcengine.com/docs/DoubaoVoice/ProductOverview-6?lang=zh)。核对日期：2026-10-14。

价格和版本须分开看：

- 百炼 `paraformer-8k-v2` 面向中文电话音频，不能替代本项目日语转写；`paraformer-v2` 才是此处候选。百炼官方将 Paraformer 定位为较早一代模型，因此低价只构成测试理由，不能直接证明动漫对白效果足够。[模型及语言](https://help.aliyun.com/zh/model-studio/asr-model)
- `qwen3-asr-flash` 的 OpenAI 兼容 Chat 接口不返回时间戳；本项目应比较 `filetrans`。更新的 `qwen-audio-3.1-asr-flash-filetrans` 已按输入／输出 Token 计费，不能套用上述每秒价格。[时间戳接口](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)、[价格](https://help.aliyun.com/zh/model-studio/model-pricing)
- 火山旧大模型标准版 2.3 元／小时、极速版 4.5 元／小时列在计费页的历史模型价格中；不要把它们当成 2.0 标准版价格。`volc.bigasr.auc_turbo` 是另一资源：同步返回，≤2 小时／100 MB，支持 `ja-JP`；不能仅因“极速”就套用 0.8 元单价。[计费说明](https://docs.volcengine.com/docs/DoubaoVoice/Billinginstructions-21?lang=zh)、[极速版 API](https://docs.volcengine.com/docs/DoubaoVoice/recording-file-recognition-lite-http?lang=zh)
- 免费额度单列：北京 `paraformer-v2` 当前为每月发放 10 小时、当月有效；`qwen3-asr-flash-filetrans` 为 10 小时、按开通／发布等规则起算 90 天有效。它们不代表常规调用免费；火山资源包也是预付费而非免费额度。[百炼额度及规则](https://help.aliyun.com/zh/model-studio/model-pricing)

接入边界与建议：

1. 先用相同的短样本比较 Paraformer、Qwen3 和豆包 2.0，覆盖日语人名、重叠对白、背景音乐、长静音及歌曲；检查漏字／幻觉、时间戳偏移和真实耗时。先验证便宜的 Paraformer 是否够用，再决定是否选择约 0.8 元／小时的两种方案；尚无本项目实测质量排名。
2. 这些专用 ASR 使用各自的语音 HTTP／SDK 协议和结构化转写结果。需要适配鉴权、提交／查询、错误状态及 Cue 时间戳转换，不能只把现有 Chat 模型 ID 改成 ASR 名称；火山语音服务的开通和密钥也应独立核对。
3. 异步供应商任务 ID 和已成功的转写结果应持久保存；重启后先查询／复用原任务，避免重新提交同一音频。结果仍按视频身份、音频范围和模型配置校验后交给理解阶段，和本项片段内成果复用一起设计。
4. 本轮不更换模型、不重跑视频、不调用付费 ASR；实际节省比例应以当前任务的 ASR 用量审计和音频时长为基准，不能把理解／Refine 的成本算进 ASR 对照。

另一个接入成本较低的云候选是 Groq `whisper-large-v3-turbo`：官方价 **0.04 美元／音频小时**，支持多语种、`verbose_json` 句段时间戳及 OpenAI Audio Transcriptions 协议；`whisper-large-v3` 为 0.111 美元／小时。可用账号和网络需实际核对，未将美元按假设汇率换算成人民币。[Groq 官方模型与价格／协议](https://console.groq.com/docs/speech-to-text)。本地 Whisper 可避免按调用支付 API 费用，但需要本地计算资源及 adapter／兼容服务，当前项目没有直接的本地 Whisper 执行入口。[Whisper 官方实现](https://github.com/openai/whisper)

**切换边界：** Seekino 已有 `openai_audio` 适配器，可发送准备好的 16kHz 单声道 WAV 并读取 `verbose_json.segments`；兼容服务可复用这条通路。只有 `json.text` 的响应会退化为整个 Chunk 一个时间范围，不能当作等价的句段字幕。国内上述异步接口仍需适配。已有失败任务的 `retry_parse()` 明确保留 ASR 冻结配置，只刷新理解／Refine，因此修改 YAML 后按原按钮重试不会切换这批任务的 ASR；需要单独定义保留完成 Chunk、只对未完成部分更新 ASR 的恢复策略，并保留配置与成果来源记录。[现有音频适配器](../../gateway/transcription.py)、[时间戳规范化](../../engine/common/transcription.py)、[重试冻结配置](../../server/service/upload.py)

### B08：索引版本策略、Embedding 存储边界与 JSON 数值精度

排序日期：2026-10-12。

优先级：高。类型：索引更新策略、向量持久化职责与精度研究。状态：2026-10-12 已实现按身份／版本确认索引完成；独立 embedding 字段尚未实施。用户根据精度影响研究选择本次不推进精度修复，仅调整索引策略；此前工作区的数值编码改动保留原状，不计入本次实现或部署结论。

#### 2026-10-12 确认的三项问题

1. **索引按身份与版本决定更新。** 可信身份为 tenant／series／video／parse task／chunk，版本为 content_version 与 index_generation。相同版本、文档完整且 query-visible 时直接确认完成，跳过重复构建和 embedding；版本较低或文档缺失才发布，较高版本阻止旧任务覆盖。同版本尚未可见时确认可见性，不以向量数值差异触发重写。原 `is_ready`、`verify_ready` 与异常读回的完整 source 相等判断违背此策略，数值尾差只是暴露问题的一个触发条件；维度及数值合法性校验和数据审计与更新决策分开。SQL receipt 的身份／版本仍需正确。
2. **独立 `embedding JSON` 字段承接缓存。** 将 `detail._index_embeddings_v2` 的完整 envelope（content_version、receipt、representations）移至 `ChunkParseContent.embedding`；detail 保留 evidence、scope、compact。字段拆分解决职责和读写粒度，不能宣称仅新增原生 JSON 列就避免浮点往返误差。具体数值编码、历史数据兼容及迁移执行尚待落实，本次记录不授权迁移或重启。
3. **研究精度机制、影响及差异来源。** 已确认本机 MySQL JSON 转换对部分数值产生一个 binary64 ULP 的往返差异；需要区分十进制解析、二进制存储及输出格式化，量化 cosine／排序的影响，并核对 ES 原始 `_source` 与内部向量表示。既有对比是 SQL 缓存读回值与 ES `_source`，没有原始模型响应、缓存及 ES 三端的同一次调用关联，不能表述为“模型内存原值被 ES 改坏”或“模型不稳定”。数百维差异的来源保持未定位，源码允许并发覆盖只是候选路径。

当前判断修正：优先解决版本幂等语义。身份／版本／完整性／可见性正确的 ES 文档可直接认可，纠正旧缓存数值或推进 generation 不是所有任务恢复的前置条件。精度保存和来源追踪分别作为持久化与诊断问题研究。

本次实现：ES 前置 readiness、写入后的验证和异常实时读回均以身份／版本及必需字段完整性判定，取消正文与向量逐值相等。Coordinator 在构建媒体／embedding 前确认已有版本；同版本可检索时跳过重建，必要时轻量补 receipt，并在完成前复核；同版本未可见时按现有规则重试，更高版本拒绝旧目标覆盖。外部版本防护和租约／SQL 当前内容版本校验保留，未迁移数据库、恢复真实任务或重启服务。

本次验收：存储／Coordinator 单元回归 99 项通过；Worker 索引相关回归 7 项通过；真实 ES 8 项集成回归通过（含同版本尾差、迟到版本、部分发布恢复），测试仅使用随机隔离测试索引并清理，没有外部模型调用。真实 MYGO 任务尚未用新代码重跑，不能以测试通过宣称这两集已经恢复。

#### 本轮研究结论与边界

- **解析机制已用同版本源码实验隔离。** MySQL 8.4.11 官方 bundled RapidJSON 默认解析对故障小数产生一个 binary64 ULP 的差异，full-precision flag 对这些输入恢复参考值；binary JSON 仍以八字节 double 保存。输入解析即可解释尾差，不需要假设向量降成 float32 或读出时固定截断。当前 MySQL 离线，实验针对官方解析器，不冒充本轮服务器逐函数调试。
- **本例数值影响很小，严格比较影响很大。** 已复现坐标的绝对差约 `1.36e-20`，相对差约 `1.30e-16`，转换到 float32 后位相同。五组现有 1024 维向量的有限扰动实验中，cosine 最大变化约 `1.11e-16`；没有据此担保全部检索或所有近乎同分排序不变。尾差会改变逐值相等／哈希结果，不能成为同版本发布的更新条件。
- **当前 ES 原始 source 保存路径已只读核对。** ES 为 8.19.22，目标索引 `index.mode=standard`、`index.mapping.source.mode=STORED`，default／final pipeline 均为 `_none`，mapping 无 source 过滤，两路向量为 1024 维 hnsw。原始 `_source` 与内部 float32 向量索引是独立路径，不能拿内部表示推断原始 source 被改。当前配置快照不补齐历史调用来源，也不证明所有历史时刻配置相同。
- **归因边界保持不变。** 当前已比较的是 SQL 缓存与 ES source；没有证明同一次模型响应的内存值被 ES 改成另一组值。客户端 dumps／loads 对四个故障数值保留原位模式；数百维大差仍需追踪原始响应、实际发布 payload 与读回结果的调用身份。

完整机制、官方源码链接、独立解析复现和数值影响见 [MySQL JSON 向量精度研究（2026-10-12）](../research/2026-10-12-mysql-json-vector-precision.md)。该研究轮次仅记录与研究；随后版本策略已按用户指示实施，字段迁移及真实任务恢复仍未执行。

#### 已采集现象与失败机制

- 排查中的 MYGO EP01 Parse Task 为 `d470e300-9813-4c19-a62e-f52503e2e1ae`。理解 24/24、Refine 22/22 已完成，17:31:06 的索引日志记录 `ElasticsearchBulkWriteError`，17:31:23 主任务进入 INDEXING_FAILED。当时快照为 24 个 Chunk COMPLETE、22 个 Interval 中 4 COMPLETE／17 INDEXING／1 INDEXING_FAILED，ES 有 20/24 个文档。这些是已采集快照，不表示当前实时状态。
- 对已存在 ES 文档的完整 source 比对，身份、内容版本、文本及其他字段一致；部分向量坐标有 binary64 末位差。例如缓存为 `-0.00010395050048828124`，ES 为 `-0.00010395050048828125`，绝对差 `1.3552527156068805e-20`，恰好 1 ULP。本机 MySQL 8.4.11 的只读 JSON 转换已复现数值变化，Python JSON 往返则保持原值。另有 4 个文档的向量数百维不同、最大差异约 `0.0068`；来源尚未关联到原始模型响应，不能据此归因于模型或并发。
- 发布代码直接使用构建出的内存 artifact，后续重放优先读取 SQL 缓存。MySQL JSON 数值往返的已复现尾差可以解释部分缓存／ES 差异，但这条代码路径不证明历史 ES `_source` 改变了模型原始响应。此前把缓存对比结果表述为模型内存与 ES 的差异过于确定，应以上述三端关联证据边界为准。

```text
ES 已有 ID／版本 V、向量 A
    → 同版本缓存恢复出 A′，哪怕只差 1 ULP
    → is_ready 比较完整 source，返回 false
    → 以 external 版本 V 重写已有文档
    → 同版本写入冲突；inspect_exact 仍判定 false
    → 原 bulk 异常重新抛出，缓存重放再次走同一路径
```

已用真实 coordinator／storage 逻辑与内存客户端构造复现：首轮发布成功，仅修改同 receipt／版本缓存中的一个坐标为相邻 binary64 值，随后连续三轮均为 `is_ready=false → bulk item 409 → inspect_exact=false → 原异常抛出`，没有重新编码。**构造客户端的 409 验证了确定性机制，不是历史 bulk item 错误的原始证据。** 原日志未保留 item status／error，历史 409 仍属根据代码与快照作出的推断。

#### 已实施的精度修复与验收范围

- 通用 JSON 编解码放在 [server/utils/json.py](../../server/utils/json.py)，[StringEncodedJSON](../../server/model/types.py) 只负责 SQLAlchemy 绑定与结果转换。物理列仍为 JSON；写入时将目标数值 payload 编码为 JSON 字符串，读取时由 Python 解码，避免数据库再次解析其中的浮点数字。业务内存、DTO、Engine 及 ES／Qdrant 请求继续使用数字数组／权重映射。
- Parse V2 只编码 `ChunkParseContent.detail._index_embeddings_v2.representations`，覆盖 dense text／video 向量和 sparse weights；外层 content_version、receipt、index_generation 及其他 detail 字段保持原生结构。编码前复制数据，不修改仍要发布到 ES 的内存结果。
- Legacy `VideoChunkEmbedding.text_vector`／`vision_vector` 同样使用字符串编码。ORM 与列查询读取时自动恢复数值，IndexSync 的发布快照和 source_revision 计算不需要业务层适配；旧数字数组／对象仍可读取，重新写入后使用新格式。
- 本批 **310 项离线测试通过**，覆盖字符串物理存储、故障坐标 `float.hex()` 精确保留、V2 dense／sparse 恢复、旧格式兼容，以及 legacy 重复导入的 source_revision／generation 稳定。尚未进行实时 MySQL／ES 联调，没有重启服务、修改历史缓存或恢复该任务。已发生精度变化的旧数字不能通过再次字符串化还原为原始值。

#### 独立待处理项与验收条件

| 问题 | 已确认边界与影响 | 待处理与验收 |
| --- | --- | --- |
| 同版本缓存并发覆盖候选路径 | 相邻 Interval 可为共享 Chunk 编码；`save_index_embeddings()` 对相同 receipt／版本的 representations 使用 `update`，后保存结果可覆盖前结果，各自仍使用本地 artifact 发布。源码存在这条路径，但尚未证明它造成了历史数百维差异，也未证明模型同输入重复请求会产生不同结果。 | 关联 call_id／输入指纹／结果标识及缓存、发布记录，先确认真实来源；同版本发布按版本幂等策略处理，缓存生命周期问题独立核对。 |
| Refine／Index 共用 retry_count | Interval 的 Refine 失败计数在成功转入 INDEXING 后保留。采集中的 Interval 16 已有两次 Refine 超时，随后第一次 Index 失败即达到 `3 > max_retry_count=2`，进入 INDEXING_FAILED。 | 明确重试额度是否按阶段分配，验证前阶段失败后仍具有约定的 Index 重试机会；不通过删除异常或放松 source 校验掩盖失败。 |
| 原始 bulk item 错误日志缺失 | `ElasticsearchBulkWriteError` 保存 failures／status_codes，但消费者主要记录异常类型与摘要，无法从当次日志确认 item 是版本冲突还是其他 ES 错误。 | 记录 task／interval／lease、失败文档 ID、item status 与 ES error type／reason；故障注入能还原逐项原因，不输出向量正文或凭据。 |
| 既存缓存与任务索引恢复 | 新编码不能恢复已经发生舍入的原始值；INDEXING_FAILED 状态和缺失文档不会因部署新代码自动恢复。旧逻辑下 `request_index_rebuild()` 仅重置状态和 retry_count，保留缓存，不能改变错误的 source 比较策略。 | 先实施按身份／版本／完整性／可见性判断的策略，再确认全部目标可检索、Interval 与 Task 正常完成；已有相同正确版本无需因缓存尾差强制重算或推进 generation。精度或来源修复只针对独立证实的数据问题，不作为全部任务恢复的前置条件。 |

依据：[缓存保存与索引 receipt](../../server/repository/video_parse.py)、[索引协调与缓存恢复](../../server/service/video_parse_index_coordinator.py)、[ES external version 与完整 source 校验](../../server/storage/elasticsearch.py)、[Interval 重试计数](../../server/mq/video_parse_queue.py)、[消费者错误日志](../../server/mq/video_parse_consumer.py)、[模型 JSON 类型回归](../../tests/server/test_model_json.py)、[V2 Repository 回归](../../tests/server/test_video_parse_repository.py)、[Legacy IndexSync 回归](../../tests/server/test_index_sync_repository.py)，以及 `outputs/local-services/api.log` 中上述时间段记录和当日只读 MySQL／ES 比对结果。

### C09：真实 provider 的工具列表仍暴露默认 general-purpose task

排序日期：2026-10-11。沿用原“Chat 联调问题（2026-10-11）”章节日期。

优先级：待确认。状态：已观察到，独立问题，本次未修复；不阻断 C08 的这次真实 Chat 执行。

- 当前代码为 `seekino_supervisor` 注册禁用 general-purpose SubAgent 的 harness profile，wrapper 的 `model_name` 使用 `seekino_supervisor:<configured-model>`；真实适配器追踪仍报告实际 provider，例如 DeepSeek／Ark。profile 匹配路径是否符合预期仍待独立复核，不能直接把猜测写成根因。
- 本次真实回归的模型 operation／工具列表包含 `read_file,task,search_web,search_video_graph,understand_current_video`，而已有 fake-model 测试预期不包含 `task`。这里的 `task` 是 SDK 默认 general-purpose 工具，不是已注册的视频业务 SubAgent；工具列表包含它不代表本轮实际调用了它。
- 待办归属：`gateway/agent_model` 的 SDK harness 配置适配。先以真实 provider 适配器复核 profile 选择与最终绑定工具，再确定最小修复；保留当前两个业务工具和专业工作流，不借此迁移整个 SubAgent 架构。
- 验收：真实 DeepSeek／Ark 适配器与 fake-model 都不再暴露被明确禁用的默认 `task`，普通视频工具调用、会话 checkpoint 和错误传播仍通过。

依据：[deepagents.py](../../gateway/agent_model/deepagents.py) 的 harness profile 注册与模型包装、[test_deepagents.py](../../tests/gateway/test_deepagents.py) 的工具列表断言，以及 C08 真实 Message 的模型用量记录。

### C10：实时理解已返回，但相邻 Chunk 证据仍缺失

排序日期：2026-10-11。沿用原“Chat 联调问题（2026-10-11）”章节日期。

状态：已观察到，片段解析根因未独立定位，本次未修复。

- 既有 mygo Video 为 `e709a2d8-aabb-509e-b323-e7f9aa7018b2`，全量 Parse Task 为 `d470e300-9813-4c19-a62e-f52503e2e1ae`；联调前任务 API 已返回 `PARSING_FAILED`。
- C08 的实时工具成功返回 `partial=true`。回答明确说明 `0–60 s`、`60–120 s`、`120–180 s` 三个相邻片段解析失败，缺少完整前因与后续证据；本轮仍取得暂停画面观察并完成上下文汇总与最终回答。
- 本轮没有独立逐 Chunk 查询或复现其解析错误，不能仅凭整轮 `COMPLETED` 认定三个片段成功，也不能把既有 Parse 失败归为 C08 已修复的 checkpoint 问题。
- 待办归属：基础视频解析 runtime／Server 解析任务恢复链路。按上述 Parse Task 与目标 Chunk 核对现有失败记录及模型／媒体阶段，分别定位原因；后续验收应确认缺失片段恢复后的证据覆盖与来源。未调用全量解析重试／重置接口，未扩大本次代码修复范围。

依据：联调前 `GET /api/jobs/video-parse/list` 的任务状态、C08 的工具日志及真实回答；[realtime_understanding.py](../../server/service/realtime_understanding.py) 的既有 Chunk 读取与部分证据返回语义。既有全量解析成本／恢复问题仍按 B07 的历史记录分别处理，不套用其旧根因作为本轮结论。

### C01：草稿引用未召回的 Evidence 导致回答失败

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

- 状态：故障原因已定位，校验和失败路径仍待改进。此前 Chat 日志中的 `Draft Item must reference retrieved Evidence` 来自 `AgentWorkspace.upsert_draft_item()`：Query Agent 提交的 `video_chunk_id` 不在本轮已召回的 Evidence 集合中。这里的 Evidence ID 是视频证据片段的 `video_chunk_id`，不是日志或请求的追踪 ID。
- 影响：该错误抛出 `AgentModelInvocationError`，本次 Assistant 消息被标记为 FAILED，页面显示“这次对话未能完成，请稍后重试。”；不是服务进程 panic。合法性校验必须保留，但目前未知 ID 没有像非法时间范围那样返回可供 Agent 修正的 Tool 反馈。
- 已做：`update_draft_tem` 已统一更名为 `update_draft_item`（代码、Query Skill 与测试，提交 `b41b1f5`）。工具调用现在记录 `Query`／`Evaluator`／`Chat` 角色、对话与消息 ID、工具名、调用 ID、参数摘要、结果摘要或错误、耗时；自由文本用长度与哈希代替原文。业务日志持续写入 `outputs/server/logs/development/api.log`，并按大小轮转。
- 待办：明确未知 Evidence ID 的可恢复 Tool 错误语义，给 Agent 返回可操作的纠正信息；验证失败后能继续检索或改用已召回 ID，同时不能放行未检索证据。`read_trieve_item`、`get_draft_catelog` 也存在拼写异常，目前仍是实际 Tool 名，尚未更名，改名时须同步 Tool、Skill 和回归测试。

依据：[video_search_canvas.py](../../agent/chat/video_search/video_search_canvas.py) 的 `upsert_draft_item`；[tools/](../../agent/chat/video_search/tools/) 的 Tool 定义与 [tool.py](../../agent/chat/common/tool.py) 的调用追踪；[agent_chat.py](../../server/service/agent_chat.py) 的失败收尾；[query/SKILL.md](../../agent/chat/video_search/skills/query/SKILL.md) 的工具清单。

### C02：多次“回答未完成”仍需逐条归因

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

- 状态：页面现象由用户提供的记录确认，具体失败并非同一种。原问题“爱音和灯是怎么认识的？谁是咕咕嘎嘎？”连续出现约 8 秒、29 秒、29 秒的失败，后来一次约 1 分 17 秒完成；追问“为什么没解释清楚？”也先经历约 8 秒失败，随后约 31 秒完成。仅靠页面通用文案和耗时，不能把这些失败全部归因于 C01。
- 同期日志线索：同一会话在 10:41 检索阶段约 8 秒、10:45 与 10:52 审核阶段各约 29 秒失败，错误类型均为 `GraphRecursionError`；16:22 另有检索阶段约 8 秒的 `AGENT_MODEL_ERROR`。17:09 还出现 `read_trieve_item` 游标不匹配，最终以 `PROCESSING_FAILED` 结束。页面摘录没有 `message_id`，这些日志尚未与摘录中的每次失败逐一关联。工具名拼写异常本身也不能据此判定为游标错误的根因。
- 待办：用 `conversation_id`／`message_id` 对齐页面失败与 `agent_execution_failed`、`agent_tool_failed`；分别排查模型错误、游标误用和 Evidence ID 校验。对游标错误检查 Agent 是否把某个证据的 `next_cursor` 用于另一证据，及 Tool 返回的纠错信息是否足够；复测失败后的继续发送。

依据：`outputs/server/logs/development/api.log` 中 2026-09-21 10:41、10:45、10:52、16:22 和 17:09 的失败记录；[tool.py](../../agent/chat/common/tool.py) 的 Tool 追踪；[agent_chat.py](../../server/service/agent_chat.py) 的公开错误映射。

### C03：“怎么认识的”被回答成后续组队过程

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

- 状态：回答质量缺陷，尚未完成根因定位。首个成功回答把第 1 集约 11:00–21:00 的追人、摔倒、创可贴、卡拉 OK 邀请当作“相识”的完整起点。用户继续追问后，系统才检索到第 1 集约 04:00 的校外偶遇、04:24 后的教室邻座再碰面、约 05:02 的自我介绍。按后续提供的片段，11:27 起是认识后的组队接近，首答遗漏了更早的接触。
- 同一问题还包含“谁是咕咕嘎嘎？”。首答只称在“第 1–3 集范围”未找到，并推测可能是别的译名或粉丝梗；用户选择的是 MyGO，不能凭这次未命中断言全系列没有，也需要核查为何回答自行收窄到第 1–3 集。该称呼所指目前仍未核实。
- 待办：复盘首轮 Query 检索词、候选排序、精读与 Evaluator 放行记录，确认 04:00 片段是未召回、被忽略还是在审核时被遗漏。对“初次／最早／怎么认识”类问题检查时间线完整性，对包含多个子问题的提问逐项核验；未找到时准确说明实际检索范围和限制。回归用此对话验证首答能区分“首次碰面、正式自我介绍、后来组队”。

依据：用户提供的本次对话页面记录；[query/SKILL.md](../../agent/chat/video_search/skills/query/SKILL.md) 与 [evaluator/SKILL.md](../../agent/chat/video_search/skills/evaluator/SKILL.md) 的检索及审核流程。04:00 等情节来自后续答案所展示的视频片段，仍需在回归时独立核验。

### C05：用户可见回答混入 Agent 流程描述

排序日期：2026-09-21。沿用原“Chat 联调问题（2026-09-21）”章节日期。

- 状态：页面摘录中的成功回答末尾出现“已按获批草稿整理完成”“三个视频块分别对应”等内部整理说明，首答还说“正好用满本次上限”。这些句子没有回答用户的剧情问题，也把草稿审批和视频块上限暴露到正文中；当前仅凭页面文本不能断定是哪个 Agent 阶段生成的。
- 待办：按消息 ID 对照 Chat Agent 输出、服务端正文组装和前端渲染，确认该内容的来源；让最终回答只保留用户需要的结论、依据和必要的不确定性说明，并用这段对话做回归。

依据：用户提供的本次对话页面记录；[chat/SKILL.md](../../agent/chat/video_search/skills/chat/SKILL.md) 的最终回答流程；[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx) 的消息展示。

### B02：上传提交成功后，临时目录清理失败导致接口误报失败

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

优先级：中。类型：事务完成后的错误边界缺陷。状态：已复现，待修复。

#### 问题是什么

`UploadService.complete()` 完成校验、合并、发布原视频和探测，然后 `complete_upload()` 提交 Video、ParseJob 与 COMPLETED 上传状态。提交以后，Service 直接调用 `delete_upload_staging()`；该方法执行 `shutil.rmtree()`。

```text
Video + ParseJob + COMPLETED 已提交
    → 删除临时上传目录出现 PermissionError / OSError
    → 异常传到 API，返回错误
    → 用户看到失败，实际上传与任务已经成功
```

本轮离线故障注入结果：首次 complete 返回 `403 PERMISSION_DENIED`；查询数据库时上传已 COMPLETED 且 jobId 存在；再次 complete 返回 200。数据没有丢失，但首次结果误导用户，临时目录也遗留。

根因不是业务事务提交失败，而是附属清理操作的失败改变了已经成立的业务成功结果。

#### 推荐的最小修复

只在提交完成后的清理边界捕获 `OSError`（包含 PermissionError / FileNotFoundError），记录安全的 warning，随后返回原 `completed`。校验、合并、发布、探测或提交失败仍沿用现有失败路径，不扩大捕获范围。

```python
try:
    self._artifact_store.delete_upload_staging(session_id)
except OSError as exc:
    _logger.warning(
        "Could not remove completed upload staging",
        extra={
            "upload_session_id": session_id,
            "job_id": completed.job_id,
            "error_type": type(exc).__name__,
        },
    )
return completed
```

业务状态和错误字段保持已提交的成功结果，不调用 release_completion，不重新合并、不重新发布、不创建第二个 Job。遗留目录可后续单独补幂等清理重试或清理命令；不能为重试清理而重新触发解析。

修改归属：`server/service/upload.py` 的提交后清理位置。`LocalArtifactStore` 仍负责实际目录删除，不全局吞掉所有存储错误。

#### 验收条件

- 清理抛出 PermissionError / OSError 时，首次 complete 仍返回 200 和真实 Video / Job ID。
- 上传保持 COMPLETED；同请求重复 complete 仍复用相同 Video / Job。
- warning 能定位 uploadSessionId 和 jobId；原视频与 Job 工作目录不被清理。
- 清理成功路径不变；提交前的校验／合并／发布错误仍失败，不能因本修复被吞掉。

依据：[upload.py](../../server/service/upload.py) 的 complete；[artifact.py](../../server/storage/artifact.py) 的 delete_upload_staging；[errors.py](../../server/api/errors.py) 的 PermissionError 映射。

### B05：Parse Agent 边界被模型档位和 fallback 隐式耦合

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

优先级：高。类型：解析编排与恢复边界不清。状态：已按算法 Pipeline 与生产持久化重新划分，数据契约迁移待后续处理。

#### 问题与根因

旧编排先把 Lite／Pro 这样的 provider tier 当成流程角色，ASR、视频理解和内容审核 fallback 的职责彼此渗透；随后又把完整阶段拓扑放进 `VideoProcessingService`，导致 Server 同时拥有 Job 生命周期和算法流程，Engine 退化成一组无法从目录看出顺序的 Agent。前者无法判断一次视频会调用哪些能力，后者则让算法演进被 Lease、Repository 和 Artifact 细节包围。

#### 已实施方案

- Engine 以显式 `ParsePipeline` 拥有 `transcribe → understand → embed → persist` 顺序。ASR 阶段跨 VideoChunk 有界并发并按序交付；Understanding 阶段按序演进 Context；Embedding 阶段构造检索文档并生成 Text/Vision 向量。
- `persist` 是算法 Pipeline 的提交端口，不是 Engine 内的数据库实现。Server 实现 `ParsePersistence`，在当前 Lease 下完成 Parse Artifact、Checkpoint 和 MySQL 事务；Pipeline 返回后的 Qdrant 派生索引同步仍由 Server Job Adapter 负责。
- Server 的 `VideoProcessingService` 只承担 Job Adapter：加载任务与已提交前缀、校验原视频、管理 Lease/Fencing、提供生产 `ParsePersistence`，并把最终业务 ID 返回给 Consumer。它不再定义 ASR、理解和 Embedding 的算法拓扑。
- Engine 契约只使用算法 DTO、路径和 `sequence_no`，不引用 `ParseJobLease`、Server Checkpoint DTO、Repository、Artifact URI 或 Index Service；Server 将 Pipeline 输出映射到稳定业务 ID 和持久化模型。
- provider、model 与 tier 只保留为配置和 provenance，不决定拓扑。内容审核 fallback 只属于 understand 阶段，复用 transcribe 阶段交付的同一份 Transcript；显式空 Transcript 不会触发补跑。
- Pipeline State 只是当次算法执行内存。异常仍由 ParseJobConsumer 和 MySQL Queue 发起下一 Attempt；MySQL Checkpoint、已提交 Artifact 与 Lease/Fencing 是跨 Attempt 的唯一恢复真相。

#### 尚未迁移的契约与已知恢复缺口

- 本轮**没有**把 ASR raw DTO 迁移为 `start_time` / `end_time` 字符串，也没有新增“原始 ASR”与“经理解校正后的 canonical transcript”两套独立类型和存储。当前 `TranscriptAnalysis` / `TranscriptCue` 仍沿用 `start_ms` / `end_ms` 整数契约；这项数据模型重构需单独设计和迁移。
- 生产 ASR 阶段当前没有独立 durable checkpoint，结果只保存在本次 Pipeline State。Understanding 失败后，下一 Attempt 会恢复已经持久化的连续 PARSED 前缀，但会为剩余窗口重新执行 ASR；即使上一次已经在内存中完成这些 Transcript，也无法跨 Attempt 复用。

依据：[pipeline.py](../../engine/parsev1/pipeline.py) 的算法阶段顺序；[transcribe.py](../../engine/parsev1/transcribe.py)、[understand.py](../../engine/parsev1/understand.py)、[embed.py](../../engine/parsev1/embed.py) 和 [persist.py](../../engine/parsev1/persist.py) 的阶段契约；[video_processing.py](../../server/service/parsev1/video_processing.py)、[consumer.py](../../server/mq/consumer.py) 与 [job.py](../../server/repository/job.py) 的 Lease、Checkpoint 和恢复边界。

### B06：缺少可复用的结构化 Context Canvas 与受限 Patch 框架

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

优先级：中。类型：Agent 结构化状态编排重复建设。状态：待修复，当前暂缓实施。

#### 问题与背景

Chat Agent 已经维护一份结构化 Workspace／Canvas，并通过 Tool 逐步修改草稿、证据和时间范围。Parse 若要让 VideoUnderstandingAgent 校正 ASR 结果，也会出现同一种需求：ASR 先把原始证据写入每个 Chunk 的 Context，Understanding 只能修改被授权的特定 bullet，完整 Chunk 校验通过后再生成 Artifact、Checkpoint 和最终业务事实。

这不是某个 Prompt 的局部问题，而是一类重复出现的 Agent 基础设施：结构化上下文、字段级写权限、Tool Patch、校验、版本、差异记录、临时草稿与持久化快照。目前 Chat 与 Parse 各自手写类似机制，会重复实现状态模型、Tool 参数、路径白名单、错误处理和恢复语义；继续复制会让两套行为逐渐分叉。

当前 Parse Pipeline 已满足“生产 ASR 不单独写业务库，Understanding 完成后才通过 Server `ParsePersistence` 提交 Chunk Parse Artifact／Checkpoint，全部 Embedding 完成后再统一导入业务事实”。缺口不在落库时机，而在 Context 和 Agent 输出契约：Understanding 可以重新生成 Timeline Event 时间，却不能受控地修改或产出 canonical Transcript；原始 ASR 与理解后的事件时间仍可能并存且不一致。

#### 当前取舍

- 本轮只纠正 Parse Pipeline 的代码所有权，不为这一点同时重构 raw／canonical Transcript 数据契约。后者会波及 Prompt、Tool、Checkpoint 兼容、持久化、召回和历史数据语义，仍需单独设计。
- 当前 Chat Query／Evaluator 可以在召回后根据 Evidence 选择更准确的最终播放区间，能够覆盖主要用户路径；它只负责本次答案的范围选择，不会修复已经持久化的 ASR 或 Event 数据。
- 保留现有生产执行方式：ASR 跨 Chunk 有界并发，全部 ASR 完成后 Understanding 按 Context 顺序串行；不在本 TODO 中顺带改造成 Chunk 级流水线。
- 原始 ASR 证据不应被 Agent 原地覆盖。未来应将 raw evidence 与 Agent 生成的 canonical state 分开，最终持久化哪一层必须由显式契约决定。

#### 目标框架

- 定义通用 Canvas Document：由稳定 `block_id`、明确 block 类型、revision 和 metadata 组成；业务字段仍由各领域自己的 Schema 定义，框架不理解 Anime、Transcript 或 Chat 等领域含义。
- Tool 只能执行白名单操作，例如读取 block、替换指定 bullet、追加受控条目或提交草稿；每个 Agent 显式声明可修改的 block／字段，禁止任意对象写入和任意 JSON Path Patch。
- Raw Evidence block 只读；Canonical／Draft block 可按 Policy 修改。一次 Tool Patch 必须先在副本上完成结构校验和领域校验，再原子提交；失败不能留下部分修改。
- 每次修改记录 revision、调用方、目标 block 和结构化 diff，支持审计、乐观并发检查、重放测试以及 Artifact／Checkpoint 恢复。
- 区分运行期 Draft 与 Durable Snapshot。Agent 只操作当前 Draft；业务流程在明确提交点保存完整、已验证的 Snapshot，而不是让 Tool 直接写数据库。
- Chat 与 Parse 只实现薄的领域适配器，共用 Canvas、Patch、Policy、Validation 和 Revision 核心；不能把某一侧的 Workspace 类型直接泄漏到另一侧。

#### TODO

- [ ] 盘点 Chat Workspace／Tool 的现有更新动作，以及 Parse `Pipeline State`、`ContextSnapshot`、Chunk Artifact 的状态修改点，整理真正共有的最小操作集。
- [ ] 定义最小 Canvas／Block／Patch／Policy／Revision 协议，先写状态转换和非法 Patch 的纯单元测试，不先接模型。
- [ ] 明确 Parse 的 block：不可变 `raw_asr`、可修改 `canonical_transcript`、`timeline_events`、`context_before` 与 `context_after`；决定 raw ASR 是否只保留在 Artifact 中。
- [ ] 明确 ASR raw 时间字段契约。若采用 `start_time`／`end_time`，不得继续把它们伪装成已经规范化的毫秒；无效或未知时间不能导致整条可用文本被丢弃。
- [ ] 为 VideoUnderstandingAgent 提供受限 Tool，只允许修改 canonical block 的指定 bullet；完成后生成不可变 Parsed Chunk，再沿现有 PARSED Checkpoint 路径提交。
- [ ] 将 Transcript 字面召回的时间真相切换到 canonical Transcript，或明确改用已校正的 DIALOGUE Event；不能继续静默混用 Lite ASR 时间与 Understanding Event 时间。
- [ ] 选择一个边界清楚的 Chat 更新流程作为第二个接入方，验证共享的是框架而不是复制另一套业务模型。
- [ ] 补齐 Tool 越权、Schema 失败无部分写入、revision 冲突、Chunk 重试恢复、旧 Artifact／历史行兼容及 raw evidence 不可变回归测试。

#### 验收条件

- Agent 只能修改 Policy 明确授权的 bullet；越权字段、未知 block 和过期 revision 均被稳定拒绝。
- 同一次 Tool 调用要么提交完整合法的新 revision，要么不改变 Context，不存在半更新状态。
- Raw Evidence 始终可追溯且不可被覆盖；Canonical 数据的来源、版本和最终持久化时机明确。
- Chat 与 Parse 至少各有一个真实流程使用同一套 Canvas/Patch 核心，且没有复制两份路径校验、revision 或 diff 实现。
- Parse 失败重试能够从最后一个已提交的完整 Chunk Snapshot 恢复，不依赖进程内共享可变对象。

### C07：收集并治理 Tool／Context 链路的其他异常终止

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 状态：待盘点。当前已有统一 Tool 结果封装和部分可纠正错误反馈，但仍存在 Tool 次数、输出大小、草稿／证据校验、Context 预算、模型调用与阶段超时等可能结束整轮的路径。不能将所有中断一概视为应移除的限制；证据一致性和真实服务故障需要保留明确的失败语义。
- [ ] 逐项列出 Query、Evaluator、Chat 中从 Tool 调用或 Context 处理走到整轮 FAILED 的路径；对齐实际日志的 `message_id`、Tool 调用 ID、触发条件、错误类型和页面表现，标明哪些是 Agent 可修正输入、哪些是数据一致性保护或真实依赖故障。
- [ ] 对可修正的 Tool 参数、未知条目、读取范围、Catalog 游标和输出过大错误，统一返回有错误码、纠正提示及必要有效范围的受限结果，使 Agent 能在当轮缩小请求或重试；对必须终止的错误保留清晰的阶段、原因和日志，不用通用 Tool 错误掩盖根因。
- [ ] 按盘点结果逐项修复并补回归：合法的多次检索及重复片段可继续完成；超范围读取能缩小后重试；无效证据不能进入最终答案；Context 溢出、模型失败和超时各有可定位的终止原因。

依据：[tool.py](../../agent/chat/common/tool.py) 的 Tool 包装和 [shared.py](../../agent/chat/video_search/tools/shared.py) 的调用预算；[tool_output.py](../../agent/chat/common/tool_output.py) 的输出限制；[runtime.py](../../agent/chat/video_search/runtime.py) 与 [context.py](../../agent/chat/video_search/context.py) 的执行及 Context 预算；[agent_chat.py](../../server/service/agent_chat.py) 的失败收尾。

### F02：前端允许 PARTIAL，后端只接受 READY

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 类型：确定的前后端契约缺陷。
- 现状：SearchPage 的 `isSearchable()` 同时允许 READY / PARTIAL；Agent 的范围解析与基础检索都严格要求 READY。
- 复现：显式选择 PARTIAL，或 Workspace 只有 PARTIAL 时空选发送，均返回 `409 SERIES_NOT_READY`，没有执行模型或检索。
- 影响：页面显示可检索、允许发送，实际请求一定失败。
- 修复方向：前端按当前后端契约只允许 READY；已选动画失效后仍能取消选择，禁止发送且不能静默扩大为全部动画。正式开放 PARTIAL 是另一项业务功能，需要同步设计可检索 Video 范围。
- 依据：[SearchPage.tsx](../../web/src/features/search/SearchPage.tsx)、[agent_chat.py](../../server/service/agent_chat.py)、[video.py](../../server/repository/video.py)。

### F04：刷新后缺少恢复未完成上传的完整交互

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 类型：功能缺失。
- 现状：上传队列与源 File 对象只在页面内存中。后端保存缺段信息，资料库可以列出 pendingUploads，但页面没有重新选取源文件、绑定已有 uploadSessionId 并恢复上传的入口。
- 影响：当前页面中的暂停／继续可用，页面刷新后的恢复体验尚未交付。
- 修复方向：从服务端读取未完成上传，重新选择并验证源文件身份后绑定已有 Session；按 missingSegmentNumbers 续传，不能只比较文件名和大小就认定内容一致。
- 依据：[UploadProvider.tsx](../../web/src/features/uploads/UploadProvider.tsx)、[LibraryPage.tsx](../../web/src/features/library/LibraryPage.tsx)。

### F05：任务页将轮询描述成 SSE

排序日期：日期待确认。原文及所属章节未明确该条日期，暂列本块末尾。

- 类型：确定的展示文案缺陷。
- 现状：TasksPage 的任务列表使用 jobs/list 每 5 秒轮询，工具栏却写着“SSE 实时更新”。上传面板订阅指定 Job SSE 是另一条已实现路径。
- 修复方向：文案准确说明定期刷新。不要为匹配文案增加当前未实现的 Workspace 级 SSE。
- 依据：[TasksPage.tsx](../../web/src/features/tasks/TasksPage.tsx)。

### 2026-10-30：任务页混淆当前与历史解析版本

状态：TODO（实现、21 项任务页回归、构建与独立真实 API 预览通过，待合并部署）。旧版本不可重试是既有权限约束，但列表缺少版本区分；新增当前／历史／全部版本筛选，默认只显示最新版本，历史显示替代说明。删除仍是上传聚合粒度，本次不删除任何记录或视频。设计与证据见 [TRD](../design/03-product-reassessment/task-history-filter-trd.md)。
