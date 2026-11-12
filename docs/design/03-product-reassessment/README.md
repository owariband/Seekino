# 03 · Product Reassessment：从产品目标复审已有设计

整理日期：2026-10-28（Asia/Shanghai）。归类依据：文档最初要解决的问题、正文日期、MDV 版本元数据及 Git 历史；文件修改时间不作为提出时间。

阶段归属与当前有效性分开记录。跨阶段修订的文档保留一份正文；表中“后续”说明演进，不把旧文中的“当前”“已完成”解释为今天的状态。实施状态由 [管理工作台](../../dev/workbench.html) 维护，问题分析见 [problems](../../dev/problems.md)，决策和验证见 [devlog](../../dev/devlog.md)。

## 本阶段的提出背景

本阶段的转向至少可以追溯到 [2026-10-23 的 reflection](../../dev/reflection.md)。字节二面的直接反馈让 hypnos 重新看待其作为产品 owner 的职责：最初想做的是“本地素材自动解析＋Codex 剪视频一条龙”，实际推进中却不断扩展技术和功能，交付期限、外部反馈与整体收益没有得到同样的重视。同行同样在做剧本和分镜，却能收敛复杂度，这个对照成为 hypnos 重新审视 Seekino 的触发点。10 月 23 日是反思记录日期，原文没有单独确认面试当天的日期。

### 当时的个人处境：接近半年实习后的外部反馈与调整

hypnos 于 **2026-04-27 入职实习**，到这次面试复盘时已接近半年。此前是一边实习、一边独立与 AI 协作开发项目，产品、策略、后端都在冷启动，尚未形成稳定的研究、反馈与经验积累方式。入职日期由 hypnos 在 2026-10-28 补充；对这段处境的解释来自 reflection 和 [整体规划复盘](../../dev/seekino-trd.md)。

这次调整的重点是把知识、架构与工具重新放回用户问题中衡量。10 月 24 日的 reflection 将判断收敛为“用户体验＋团队成本”：看部署与使用是否方便，衡量 Token、工具错误、耗时和上下文收益的综合变化，保留有用的工程骨架，减少偏离制作目标的复杂度。同时开始重视主动找研究、尽早接触外部反馈，以及边学习、边设计、边记录文档。

| 记录时点 | 当时的个人与项目背景 | 对后续工作的影响 |
|---|---|---|
| 2026-10-23 | 二面反馈后，回看最初的本地素材解析与剪辑目标，以及功能扩张、交付延期和反馈不足 | 从“继续加能力”转向明确最小交付点，量化收益并寻求真实外部反馈 |
| 2026-10-24 | 继续反思部署复杂度、表达和文档积累；发现已有研究可帮助更清楚地定义任务 | 用“用户体验＋团队成本”审视方案，把研究和工程设计对齐到实际问题 |
| 2026-10-24 的时间安排 | [OKR](../../dev/OKR.md) 记录 10 月 26 日离职、10 月 27 日回成都并迁移数据的安排 | 重新估算月底剩余时间和上线预期；个人换机也使交接与证据保存成为必要工作 |
| 2026-10-25–26 | [整体规划](../../dev/seekino-trd.md) 与 [换机交接](handoff_to_new_pc.md) 继续整理剧本、分镜、自动剪辑、解析性能和 Sandbox | 形成解析质量与速度、叙事召回、作品交付三条复审主线 |
| 2026-10-28 | 回到这些材料，按个人经历与项目发展重新分期 | 将历史动机、外部反馈、技术转向和未完成验证记录到阶段 README |

因此，10 月 28 日是本次命名与归档整理的日期，10 月 23–26 日的记录解释了此前已经发生的视角转变。阶段之间仍有重叠，本次整理没有复跑实验或重新验收生产实现。

## 文档时间与提出背景

| 文档 | 时间归属与证据 | 提出时的视角与背景 | 本次整理时的阅读边界 |
|---|---|---|---|
| [首页明暗主题切换](theme-transition-trd.md) | 2026-11-09，线上观看首页的封面与页面换色不同步 | 让画布与预览画面协调变亮或变暗，保留正片色彩 | [PR #80](https://github.com/owariband/Seekino/pull/80) |
| [parse-media-link-lease-trd.md](parse-media-link-lease-trd.md) | 2026-11-07，198 帧交付逐个触发 chunk 续租 | 清单就绪后与交付前各校验一次 | [PR #75](https://github.com/owariband/Seekino/pull/75) |
| [parse-ready-dispatch-trd.md](parse-ready-dispatch-trd.md) | 2026-11-07，micrograd 的索引在 refine 结束后继续约 39 分钟 | 提交后按 ID 派发、有限等锁与轻量收尾；记录同一素材的真实索引对照 | [PR #70](https://github.com/owariband/Seekino/pull/70) |
| [parse-output-drafts-trd.md](parse-output-drafts-trd.md) | 2026-11-07，SSE 逐事件续租与模型正文草稿需求澄清 | 内存累积正文，每 1,024 新字节或退出时保存独立调用草稿；续租继续由周期心跳驱动 | PR #64；隔离验证与真实解析效果分别验收 |
| [docker-web-assets-trd.md](docker-web-assets-trd.md) | 2026-11-06，CPU 构建暴露发布 Logo 软链接缺失 | 保留构建目录关系、限制发布资源输入，并验证 main 部署来源 | 设计与证据见正文；当前状态见工作台 DEPLOY-01 |
| [cloud-guest-without-smtp-trd.md](cloud-guest-without-smtp-trd.md) | 2026-11-06，SMTP 留空导致整个 cloud API 无法启动 | 游客启动与邮件能力分离，未配置邮件时明确拒绝邮箱认证 | 当前状态见工作台 DEPLOY-02 |
| [cloud-user-docs-trd.md](cloud-user-docs-trd.md) | 2026-11-10，Cloud 访客看不到两版区别、部署方式与 GitHub 入口，比赛需要真实用户却缺少转化落点 | 站内 `/docs` 使用文档与顶栏 GitHub 入口；API 文档迁到 `/api/docs` | 初稿 |
| [apple-style-refresh-trd.md](apple-style-refresh-trd.md) | 2026-11-10，推广前界面不够简约高级：圆体标题、等宽大写小标签、绿灰色调 | Apple 式视觉层：SF / 自托管 Inter、中性色板、磨砂顶栏、胶囊按钮；不改布局 | 配色的后续恢复见下一项，原始取舍保留 |
| [restore-celadon-palette-trd.md](restore-celadon-palette-trd.md) | 2026-11-12，中性色板替换青瓷底色，整站观感偏离此前配色 | 从 PR #90 前的源码恢复背景、表面、文字和 Hero 遮罩，保留字体与交互改进 | [PR #127](https://github.com/owariband/Seekino/pull/127)；验收证据见正文 |
| [model-discovery-feedback-trd.md](model-discovery-feedback-trd.md) | 2026-11-10，“获取模型列表”结果不可见，whisper.cpp 必然 404 | 内联结果与模型 chip、防重复；无列表的服务给出中性说明 | 初稿 |
| [sticky-docs-trd.md](sticky-docs-trd.md) | 2026-11-10，欢迎卡片与文档准确但抓不住首次访客 | 按 SUCCESs 重写，`/docs` 拆为快速开始等多页 | 初稿 |
| [smtp-login-availability-trd.md](smtp-login-availability-trd.md) | 2026-11-07，邮件未配置仍展示验证码表单，用户填写后才知道不可用 | 由账号服务传递可用能力，空配置展示游客模式和未开放说明 | 当前状态见工作台 DEPLOY-04；依赖 #62，随 #63 发布 |
| [cloud-docker-deploy-trd.md](cloud-docker-deploy-trd.md) | 2026-11-06，简历筛选需要公开演示，但云服务器上没有任何服务 | 用 Docker 在 4C8G 服务器部署发布后的 `main`，迁移演示数据，经公网 HTTPS 提供观看、检索与问答 | 初稿；实施与验收待进行 |
| [chat-stream-snapshot-coalescing-trd.md](chat-stream-snapshot-coalescing-trd.md) | 2026-11-05，长回答流式交付途中被判为慢消费者并断开 | 合并缓冲中相邻的未发送快照，持续读取的客户端不再收到中断 | 初稿；实验待测 |
| [chat-frontend-performance-trd.md](chat-frontend-performance-trd.md) | 2026-11-12，聊天输入、正文和思考更新反复解析历史回答，终态先于 EOF 时误显示连接中 | 保留流式合同与 Kino 行为，隔离渲染并验证状态、滚动和浏览器性能 | [PR #126](https://github.com/owariband/Seekino/pull/126)；执行状态见工作台 CHAT-PERF-01 |
| [series-background-ownership-trd.md](series-background-ownership-trd.md) | 2026-11-05，系列概览讨论中发现简介、背景资料与人物资料归属不清 | 系列只保留“简介 + 一份背景资料”由用户维护，`character_bible` 转为只读兼容，Agent 使用显示名称 | 初稿；实验待测 |
| [chat-streaming-trd.md](chat-streaming-trd.md) | 2026-11-01，课程 QA 撤回错误 FTT 后独立承接 | 模型正文在生成过程中交付，首字计量能关联实际请求、增量和浏览器展示 | PR #25 直接合入 dev，不依赖 #18；隔离验证与真实试点、性能和部署分开判断 |
| [Chat 模型重试提示](chat-model-retry-status-trd.md) | 2026-11-07，local 观看问答 | 等待期间显示模型重试次数 | [PR #71](https://github.com/owariband/Seekino/pull/71) |
| [chat-text-delta-stream-trd.md](chat-text-delta-stream-trd.md) | 2026-11-05，已有检索结果后正文每秒约 9 字到达页面 | 正文以增量事件交付、按间隔持久化，正文速度回到供应商水平 | 已实现，演示环境验收通过（2026-11-05）；正文 228–311 字/秒，9 回合最长 112 秒 |
| [chat-lazy-conversation-trd.md](chat-lazy-conversation-trd.md) | 2026-11-07，打开对话页即在服务端创建对话，空“新对话”在记录中堆积 | 草稿态，首次发送或首次附件时才创建 | 实现完成、本机验证通过；待合并 |
| [chat-answer-shape-trd.md](chat-answer-shape-trd.md) | 2026-11-05，回答含过程播报、长段与完成后的工具行 | 回答先结论、短段列表，完成后只留正文与视频片段 | 初稿；九门禁对照待测 |
| [chat-reasoning-display-trd.md](chat-reasoning-display-trd.md) | 2026-11-05，长回合 50–110 秒期间用户看不到系统在想什么 | 实时转发 Supervisor 思考增量，回答上方默认折叠展示 | 初稿；依赖 #51 |
| [chat-card-event-boundaries-trd.md](chat-card-event-boundaries-trd.md) | 2026-11-05，学习路线类视频卡常用整分钟或半句起止 | 视频卡起止落在讲解句子或事件边界 | 初稿；离线回放 68→79/104，门禁对照待测 |
| [deepseek-web-search-trd.md](deepseek-web-search-trd.md) | 2026-11-02，Chat 更换模型后搜索仍走未固定 backend 的 DDGS | 免 key DDGS 与 DeepSeek API 独立配置，明确请求、费用和错误合同 | 直接向 dev 集成；接口接入与回答、引用质量分别验收 |
| [management-workbench-trd.md](management-workbench-trd.md) | 2026-11-02 统一看板；2026-11-03 升级为管理工作台 | 以分类和看板连接分散的问题、PR、TRD 与 devlog；后续加入里程碑、课程闭环地图与验收门槛，服务周四课程闭环验收 | 单文件离线维护；历史 DONE 沿用原验收范围，PR 信息、课程进度与费用按采集时点阅读 |
| [parse-task-query-recovery-trd.md](parse-task-query-recovery-trd.md) | 2026-11-03，完整课程积累大任务 detail 后查询 1038 | 独立后端查询修复与 Consumer 失败定位 | Draft #30，已整合 #18；排序修复与同 v11 恢复已验证，整课仍在推进 |
| [watch-answer-delivery-trd.md](watch-answer-delivery-trd.md) | 2026-11-04，课程 QA 的 Q01–Q03 仍因失败视频卡、超长回答与内部术语未通过 | 实时证据改用时间码链接交付，限定句数与用户可见术语 | 实现与单测完成；真实模型复测待执行 |
| [handoff_to_new_pc.md](handoff_to_new_pc.md) | 2026-10-26 的多任务交接，含 10-24–25 探索 | 换机前保存真实停点、尝试过程、未完成项与后续理由 | 跨阶段交接；保留三条主线和环境差异，不将局部探针当作完整产品交付 |
| [parse-output-xml.md](parse-output-xml.md) | 2026-10-26 协议设计稿 | 在保留有效信息的前提下降低解析输出负担 | 交接时尚未正式接入；须同时验证内容质量、时间定位与整集耗时 |
| [video-parse-performance-trd.md](video-parse-performance-trd.md) | 2026-10-28 建立；11-01 补 Kimi 推理耗时与帧时间配对续跑 | 解析异常长的因素拆解、完整/精简 JSON 与 XML 对照、SHOT 候选及宿主 Canvas 接入边界 | 解析性能主入口；最终采用精简 JSON、不换 XML，质量优先并沿用既有并行策略；10-30 dev 已接入直接 JSON 与 30k，精简合同迁移及实模验收待完成 |
| [local-text-embedding-trd.md](local-text-embedding-trd.md) | 2026-10-29，课程 P0 与本地运行需求 | 本地 Qwen 文本向量和可关闭的视觉索引，保留 OCR/VLM | 实验与兼容性分别验证，不把本地编码通过视为全课程质量通过 |
| [parse-pause-resume-trd.md](parse-pause-resume-trd.md) | 2026-10-30，单集优先解析 | 持久暂停、继续、租约隔离和实际阶段显示 | 已实操释放非目标任务容量，整集完成另行验收 |
| [model-network-failure-trd.md](model-network-failure-trd.md) | 2026-10-30，Karpathy 连接异常 | SDK 分类、节点重试、可诊断日志 | 区分连接异常、网关超时与整集闭环验收 |
| [kimi-streaming-trd.md](kimi-streaming-trd.md) | 2026-10-30，长 Chunk 504 与历史流式成功记录 | 正式 Kimi 生成入口接入 Chat SSE，收齐后校验和执行工具 | 单独验证代码接入、流式接口兼容性、真实素材效果与部署 |
| [model-streaming-trd.md](model-streaming-trd.md) | 2026-10-31，默认 stream 被 profile / 入口写死，temperature 缺少创建时配置 | 通用模型配置、默认 stream=true / temperature=0、Kimi 固定温度、独立 fallback、共享协议接收与 Parse 快照 | 承接 Kimi 接入修复；隔离 HTTP 验证与真实部署、供应商效果分开验收 |
| [local-whisperx-asr-trd.md](local-whisperx-asr-trd.md) | 2026-10-29 本地 ASR；10-30 故障续版与 10-31 存储／GPU 测试 | 视频级 ASR／Summary 持久成果、Task／成果归类及 Turbo＋Metal 本地测试接线 | 存储与恢复隔离验证通过；Turbo＋Metal 正式 Gateway 短样本与完整 116 分 20 秒 ASR 均通过，业务库迁移、独立对齐及完整课程闭环待验 |
| [cloud-public-mcp-trd.md](cloud-public-mcp-trd.md) | 2026-11-11，本机剪辑 Agent 取云端公开素材 | 同一 MCP SDK 挂载到 Cloud，按发布范围检索，下载有时限和区间限制的 MP4 | 游客身份、工具发现与真实取片分别验证 |
| [mcp-access-trd.md](mcp-access-trd.md) | 2026-10-28，外部 Agent 素材库与演示需求 | 将首要交付收敛为 localhost MCP，Cloud 保留 Chat 与后续轻量 HTML 作品 | 区分协议与隔离测试、真实 Codex 连接、真实素材到作品；不把历史 Sandbox 探针当作已交付能力 |
| [剪辑双召回主设计](editing-skill-style-reference-trd.md) | 2026-11-05 开题，同一 PR 交付设计和代码 | 编辑从剧本找表演与剪辑手法，凭证据取回原片 | [需求、实现与验收 #50](https://github.com/owariband/Seekino/pull/50) |
| [本机 MCP 接口与整体验收](mcp-edit-references-trd.md) | #50 接口部分 | 由同一客户端查表演、查手法、取两段原片 | [可复跑测试](../../../tests/server/test_editing_mcp_flow.py) |
| [代表片段与全片风格卡](edit-reference-profile-trd.md) | #50 解析部分 | 从原片生成手法依据 | 同一功能的解析合同 |
| [解析结果用途](parse-result-profile-trd.md) | #50 上传部分 | 同库正片与剪辑参考的配置及读取隔离 | 同一功能的上传合同 |
| [《Weekend Study》宣传片剧本](editing-promo-weekend-study.md) | #50 试点成片，片主 2026-11-10 批准 | 一条 prompt 经 MCP 取材、本地抠像渲染 | 剧本、实测数字与占位项；成片待制作 |
| [动画表情素材召回](anime-material-recall-trd.md) | 2026-11-08，五集盲评漏选爱音反应 | 剪辑 Agent 按画面表情找素材 | 单独核验语义排名、全量翻页与原片画面。 |
| [上传视频自动判型](parse-pre-hook-router-trd.md) | 2026-11-09，CPU 普通上传需要自动选策略 | 沿用 Parse V2 及显式覆盖 | [#82](https://github.com/owariband/Seekino/pull/82) |
| [公开观看、对话与逐集检索](public-course-readiness-trd.md) | 2026-11-10，追加 E08 导致七集已就绪课程消失 | 显式可见性、逐集资料依赖和撤回校验 | [系列分享 §3.7](../02-agent-harness/series-sharing-trd.md#37-首版公开条件整个系列都已就绪) |
| [剪辑素材的运镜描述与回放](clip-camera-reference-trd.md) | 2026-11-10，镜头平移被看反或按画面内容拆断 | 同一发布事件提供中文描述、来源与可执行镜头 | [实测](../../report/agent/video_parse_v2/clip-camera-reference-20261110.md) |
| [按证据下载连续原片](mcp-source-clips-trd.md) | #50 媒体部分 | 编辑自选入出点，服务端返回一段 MP4 | [同一应用的双路取片回归](mcp-edit-references-trd.md#6-结果验收与闭环) |

## 本次讨论确定的复审问题

下表来自 2026-10-28 本次会话及用户提供的另一会话总结。它记录复审方向与拟取得的证据，不是新完成的实验结论或已经批准的架构替换。

| 主线 | 从产品视角重新问什么 | 交接材料记录的停点 | 应取得的验证交付 |
|---|---|---|---|
| 解析能力与速度 | 剧本、问答和剪辑真正消费哪些信息；如何在同样质量下更快 | 媒体准备优化、零工具理解与 XML 单块对照已有记录；正式接入和质量合格的代表片段/整集基线仍缺 | 解析质量检查表、分阶段计时与整集基线 |
| 素材理解与叙事召回 | Event 事实如何支持完整剧情、镜头顺序与剪辑入出点；哪些召回通道有实际收益 | 已有 CineAgents 阅读和 Event 合同对照；仍需真实片段验证叙事与证据的关系 | 固定解析版本和证据预算的召回通道消融、可核验叙事片段 |
| Sandbox 创作闭环 | 用户能否从需求最终拿到可预览、可下载的作品 | 旧 Mac 的隔离 FFmpeg 探针通过；模型制作、修错、产物验证、Chat 交付及 Linux 容器仍需验证 | 用户能获取成片的完整制作闭环 |

### 当前讨论的判断原则

1. 解析先建立“同样质量下更快”的基线。内容正确性与时间定位分别验收，单个 60 秒 Chunk 的历史成绩不代替整集结果。
2. 召回以 Event 找线索，按需读取上下文、复看原片，创作时再组织剧情段落。Event-first 仍需比较纯语义、词法加语义、当前三通道的收益；不能直接推出只保留文本相似度。
3. Sandbox 分别验证会话状态、隔离执行与作品文件持久化。已有 checkpoint 或执行探针不能代替制作、修错、发布、预览和下载。

2026-10-28 后续范围调整：用户明确优先实现 localhost MCP，以 10 月 30 日早上前的 Codex 实际演示为目标。复杂剪辑交给外部 Coding Agent；Cloud 的微型 Sandbox 收敛为后续 HTML 作品保存与隔离预览。主合同见 [MCP 接入 TRD](mcp-access-trd.md)，前述 Sandbox 研究与历史停点保留。

这些判断的精确实现和验收口径后续再形成专项文档。本 README 不同步维护任务完成状态。

## 被重新审视的早期材料

| 背景材料 | 为什么仍留在原阶段 | 当前复审入口 |
|---|---|---|
| [product.md](../02-agent-harness/product.md) | 正文标注于 2026-09-20，是早期定位草案 | 本地优先、目标用户、运行形态与产品价值是否仍一致 |
| [分层解析思路](../02-agent-harness/produce_future_1.md)、[陪看与按需解析](../02-agent-harness/agent_harness_refactor.md) | 原始提出日不详，属于架构完善期的产品探索 | 实际使用场景、等待成本及信息需求 |
| [目标 Query 与检索需求](../02-agent-harness/multimodel-retrieve-trd.md) | 2026-10-13–17 的需求与研究记录 | 哪些用户问题值得优先验证，怎样判定答案可用 |
| [Sub-Chunk / Event 合同](../02-agent-harness/sub-chunk-trd.md)、[视觉检索](../02-agent-harness/visual-retrieval-trd.md) | 已形成的技术设计基础 | 各通道、粒度和上下文机制是否带来真实收益 |
| [Parse Harness](../02-agent-harness/video-parse-harness-trd.md)、[Engine](../02-agent-harness/video-parse-engine-trd.md)、[Canvas](../02-agent-harness/context-canvas-design.md) | 记录任务、领域和上下文的技术演进 | 哪些复杂度仍有必要，哪些工作可以减少 |
| [本地 Bridge](../02-agent-harness/codex-bridge-trd.md)、[Local/Cloud](../02-agent-harness/local-cloud-trd.md) | 运行形态与责任边界的历史及持续修订记录 | Sandbox 接线、部署与作品交付 |

相关原始调研仍在研究目录：[OmniScript](../../research/2026-10-24-omniscript.md)、[Supervisor / Sandbox](../../research/2026-10-25-supervisor-sandbox-langsmith.md)、[本地 Sandbox / DeepAgents](../../research/2026-10-26-local-sandbox-deepagents.md)。本次归档不更新外部论文或工具的事实判断；部分旧实验材料已缺失，复跑前按原交接核对输入与证据。

[返回设计总索引](../README.md) · [报告索引](../../report/README.md)

- [任务历史版本筛选](task-history-filter-trd.md)：2026-10-30，默认当前版本、历史追溯与操作权限说明。

- [独立对话页布局](chat-sidebar-layout-trd.md)：左侧历史与居中草稿，状态见[工作台](../../dev/workbench.html) CHAT-LAYOUT-01。
- [聊天页顶栏与公开范围](chat-header-scope-trd.md)，2026-11-08。游客视角下的紧凑导航、选择器空态与独立聊天侧栏。
- [个人模型服务](personal-model-connections-trd.md) 记录生效 YAML 的管理接口、凭证掩码、cloud 权限和会话选模。依赖设置工作区 #72。
- [模型注册与用途](model-registry-roles-trd.md) 延伸 #74，将同一组注册模型接入聊天、解析和检索。
- [游客观看问答的执行预算](guest-chat-turn-budget-trd.md) 记录第五次模型调用中断的复现，以及按 IP 计数的独立验收。
- [观看页 Kino 入口与悬浮陪看窗](watch-kino-entry-trd.md) 记录桌面浮窗、停靠、全屏和手机抽屉的实测。
- [视频内文字查找](watch-text-find-trd.md) 记录当前一集的台词、画面文字和播放器毫秒跳转。[PR #105](https://github.com/owariband/Seekino/pull/105)
- [片内画面查找](watch-visual-find-trd.md) 记录单集出镜、场景查询和独立开关。
- [片内智能搜索](watch-smart-find-trd.md) 记录可选的网络梗解释、同义词匹配和单集验收。
- [设置工作区](settings-workspace-trd.md) 记录会话内打开设置、设备偏好、管理员配置和设计样稿对照。
- [对话回答模型与输入区](chat-composer-models-trd.md) 记录会话级模型选择、附件位置和侧栏调宽。状态见[工作台](../../dev/workbench.html) CHAT-COMPOSER-02。
