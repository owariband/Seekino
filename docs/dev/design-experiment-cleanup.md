# Design 和 Experiment 清理清单与探索结论

盘点日期：2026-10-24。依据：`5630e87` 加当前工作区内容。范围为主工作区的 `**/design/`、`**/experiment/`，不包含 `outputs/`、`.claude/worktrees/`、`.claude-tmp/` 内的副本。路径均从仓库根目录起算，体积为文件逻辑字节。

本文件按轮次保存清理范围和探索结论。**当前去向以[第四轮整体退役记录](#第四轮旧实验整体退役构图研究仅留本地归档)为准**：旧声纹、检测与出处定位实验仅留摘要；构图数据迁入被忽略的本地归档。前三轮中的保留例外、原位恢复入口和文件数均为当轮历史状态，不再代表当前目录仍存在。以下至“第二轮”之前为首轮执行记录。**已于 2026-10-24 按用户“全部delete掉”的授权，删除以下全部候选：88,113 个文件、2,099,309,067 字节（约 1.955 GiB），并移除 38 个空目录。** 对于已经退役、无需再复算的探索，本文件替代其旧代码、逐轮日志和中间产物；仍被脚本读取的输入、回归样本、选型对照和当前合同继续保留。

## 清理范围

| 类别 | 已删除文件数 | 体积 | 已落实的保留边界 |
| --- | ---: | ---: | --- |
| 明确冗余的空文件、导出副本及缓存 | 101 | 5.144 MiB | 保留 MDV 原件；不包含图表正文或规格源文件 |
| 声纹任务过程快照 | 87,894 | 1,986.939 MiB | 放弃完整状态演变回放，保留下面列出的终态和审计例外 |
| 本轮补充的旧版本与旧轮次 | 149 | 10.079 MiB | 已处理受影响的报告链接；保留脚本硬依赖 |

第三项与第二项有 31 个文件、0.105 MiB 重叠，来自 v6 失败运行。新增独立删除为 **118 个文件、9.974 MiB**。所有类别去重后实际删除 **88,113 个文件、约 1.955 GiB**，其中 17 个受 Git 跟踪，其余 88,096 个是未跟踪或被忽略的本地产物；机器输入和审计例外已排除。体积为逻辑文件大小，不等同于文件系统实际释放的物理空间。

### 明确冗余文件

| 范围 | 数量 | 保留说明 |
| --- | ---: | --- |
| `docs/design/technical_solution.md` | 1 | 0 字节空文件 |
| `docs/design/technical_solution copy.mdv.reference.working.md` | 1 | 与 MDV Reference 历史版本 `v_0b6632b1a51f0e7b7269a8eabb328367` 相同，仅末尾换行不同。原 MDV 完整校验通过 |
| `docs/design/diagram/*.visual-check.*`、`docs/design/diagrams/*.visual-check.*` | 36 | 只删截图与验收报告；保留交互 HTML 正文和架构／时序规格 JSON |
| 盘点范围内的 `.DS_Store` | 16 | Finder 元数据 |
| 盘点范围内的 `__pycache__` 文件和 `.pyc` | 47 | Python 缓存 |

`technical_solution copy.mdv.document.working.md` 不在清单内。它包含独立的第 14–17 章工程补充，与 MDV 当前正文及已保存 Document 版本均不同。`.mdv-assets/` 也不随导出副本一起清理。

### 已清理的旧版本与旧轮次

为缩短路径，下面使用这些前缀：

- **VC**：`engine/experiment/speaker-clustering/20261018-mygo-ep04-voiceprint-agent/`
- **VP**：`engine/experiment/voiceprint-agent/`
- **IMG**：`engine/experiment/chat-image-origin/20261016-mygo-image-origin/`
- **GALLERY**：`engine/experiment/composition-models/20261018-shot-gallery/`

| 编号 | 准确删除范围 | 文件数 / MiB | 结论与已落实的处理 |
| --- | --- | ---: | --- |
| N01 | `VC/startup-failure-01/`、`VC/startup-failure-02/` 全部文件 | 15 / 0.356 | 环境与启动失败，已总结并删除。没有发现保留脚本读取其中数据，旧运行 manifest 的来源描述属于历史记录 |
| N02 | `VC/superseded-run-01/`、`VC/superseded-run-02/`，每个目录排除 `status.json`、`usage.json` | 20 / 1.195 | 两轮宿主接线问题已经由最终轮次替代。四个排除文件仍被 `VC/report.py:153` 读取；其余旧脚本、候选快照及 trace 已删除，报告和生成脚本中的保留说明已同步 |
| N03 | `VP/20261017-mygo-ep04-event-merge-no-prefix/` 全目录 | 51 / 0.579 | v6 在解析开始阶段因网络代理失败，随后由配置相同的 v7 重跑替代。v7 README 的旧目录说明已指向本总结；不能把该失败当算法反例 |
| N04 | `IMG/runs/chat-02.{json,log,sse}`、`chat-03.{json,log,sse}`，以及 `chat-04/`、`chat-05/`、`chat-06/`、`chat-07/` 全目录 | 49 / 4.620 | 早期链路和预算试验。保留原图、manifest、runner 和最终成功的 chat-08/09；已更新 IMG README、chat-08/09 README 对旧轮次的链接 |
| N05 | `VP/20261017-mygo-ep04-event-merge-no-prefix-retry/` 中的 `trace-current-readonly.json`、`trace-latency.json`、`trace-latency-report.md` | 3 / 2.779 | 运行中途快照和分析，终态数字已有替代。保留 `latency-comparison.json`、`trace-evidence.json`、最终 README；README 和 devlog 对中途报告的链接已指向本总结 |
| N06 | `docs/design/video-parse-v2-architecture.excalidraw`、`docs/design/video-parse-v2-architecture.svg` | 2 / 0.450 | 明确使用已撤销的 Interval INDEXING 设计；没有发现有效外部引用。架构决策已总结，旧图已删除 |
| N07 | `docs/design/agent-loop-code-example.py` | 1 / 0.005 | 示例导入的 `server.agent`、`server.agent.state` 以及文中旧测试路径均已不存在。已移除旧运行命令，两份 Agent 文档及 transfer 的说明已指向总结 |
| N08 | `agent/video_parse_v2/design/architecture_draft.md`、`agent/video_parse_v2/design/refactor_1.md`、`engine/parsev2/design/refactor_1.md` | 3 / 0.066 | 早期方案与 2026-09-27 阶段快照，已非当前入口；未发现有效外部引用。已删除旧稿，保留当前两份 TRD、声纹 TRD 和仍被阶段图引用的评审文档 |
| N09 | `GALLERY/embedding-run-01.log` 至 `embedding-run-04.log`，以及 `embedding-run-04-receipt.json` | 5 / 0.029 | 分批填充向量缓存的进度与一次限流失败记录；最终第五轮已完成。已删除早期进度，保留第五轮日志／receipt、最终矩阵、行映射、图库和校验文件 |

N02 的四个例外合计仅 4,131 字节。保留它们即可继续生成现有最终报告；如果要求旧目录完全消失，则必须先调整 `report.py` 的历史成本表读取逻辑，不能仅删除文件。

N04 的两个分组 manifest 含历史 `run_output` 值，分别指向 chat-06/07。现有 `run.py` 不消费该字段，实际强制使用新的 `--output-dir`，所以它不是输入读取依赖；仍应保留历史含义，不把清理后的新路径伪装成原始运行路径。

Git 迁移 manifest 中的旧路径是保存时点的历史清单，不应为了消除文本命中而改写。真正要处理的是仍供读者点击的入口和脚本读取。

### 声纹过程快照及例外

已删除 VP 下这些实验的指定任务目录中的 `tasks/`、`intervals/` 文件，排除下面三份审计证据：

| 实验目录 | 任务目录 | 排除前体积 |
| --- | --- | ---: |
| `20261017-mygo-ep04/` | `a99b4af4-4b66-43e5-90dc-b04b525b616a` | 80.27 MiB |
| `20261017-mygo-ep04-event-merge-no-prefix/` | `14e7bc7d-4166-4fad-b7df-8469b08fa3d5` | 0.11 MiB |
| `20261017-mygo-ep04-event-merge-no-prefix-retry/` | `3b9bb6d7-8f2c-4ce3-bb6f-2a2a0aa8228b` | 798.92 MiB |
| `20261017-mygo-ep04-multiref-no-prefix/` | `dd55671d-a84b-4628-bedb-4007a4f4aa9c` | 715.81 MiB |
| `20261017-mygo-ep04-trusted-voiceprint-null-speaker/` | `8ef2a23b-def7-4ff3-905d-36cee63ce70f` | 391.92 MiB |

v7 的 `merge-audit.json` 直接引用该任务 `intervals/` 下的三份文件，必须保留：

- `seq003-COMPLETE-db58842541ad9323.json`
- `seq002-COMPLETE-c62df11dc1e5e7c0.json`
- `seq004-COMPLETE-5186d9320b5ec7b0.json`

除单独按 N03 退役的 v6 外，各轮 `final-*.json`、`summary.json`、`progress.jsonl`、`voiceprint-events.log`、`target-history.json`、`chunks/`、`sources/` 和审计文件继续保留。特别是 `target-history.json` 引用的 Chunk 快照、`trace-evidence.json` 引用的来源快照不在清理范围。现有耗时比较读取终态和日志；清理后失去的是完整的逐次任务／Interval 状态回放。

## 需要留下的探索结论

### 声纹宿主接线和旧准备方案

N01 的失败来自未加载项目环境、UV 缓存权限、Codex 状态库访问限制。后续使用正确环境和正常访问权限启动。模型调用名额从 20 调到 65，是适配 Gateway 为每次工具回调预留名额的计数方式；工具上限 64、时限 900 秒没有改变。这些结果只能说明运行环境和宿主预算接线，不能评价声纹算法。

N02 第一轮重复呈现弱身份标签，Agent 将同源信息误当独立佐证；第二轮移除重复字段后，又因为缺少生产人物背景而零登记。最终轮补齐背景并保持不重复人物身份字段。两轮旧执行分别为 78.34 秒、54.66 秒；最终轮 149.47 秒，登记 15 条、5 人，未达到 11 人名单中至少 6 人的覆盖门槛。最终 `coverage_incomplete`，未发布为正式声库。

仅在五人草稿库中排第一不能证明识别改进：补入缺失竞争人物后，爽世目标的逐条均值仍排第二。候选多样性或参考条数增加没有在本样本证明必然更好。弱标签、同一来源的重复描述、画面中有人张嘴都不能替代独立身份核验。参见保留的 [聚类与选源退役摘要](#聚类与选源)。

旧 75% 准备门槛和 VoiceprintAgent 选源路线已被后续 B0 接入合同取代；WavLM、K-means、medoid 仍属于后续实验，不能据“没有接入生产”宣称算法已被证明无效。当前决定以 [voiceprint-trd.md](../design/02-agent-harness/voiceprint-trd.md) 为准。

### v6 网络失败与 v7 性能结论

N03 的 v6 遇到 `ArkAPIConnectionError → ProxyError:403 Forbidden`，原因是服务继承沙箱代理环境。Understanding 0/24、声纹未准备、Refine 未执行；它没有提供合并算法的效果或耗时证据。宿主环境重启后，同配置 v7 完成运行。

N05 中途分析不能代替最终耗时：v7 为 43:22.011，v5 为 38:43.518，v7 多 4:38.493，约增加 12.0%，整集提速验收未通过。最终追踪识别了翻译修改触发声音参考指纹变化的问题；至少九轮满足相应拒绝的充分条件，累计生成跨度不是可直接节省的墙钟时间。其余刷新原因没有完整原始工具轨迹，不能全部归因于同一种冲突。保留的 [VoiceprintAgent 退役摘要](#voiceprintagent-历次迭代)、`trace-evidence.json` 和终态结果承载这些结论。

### 六图出处定位的迭代

N04 使用同一组六张原图探索图片透传、候选核验和执行预算，不能当作视觉向量模型的独立准确率评测。

| 轮次 | 当时观察 | 留下的结论 |
| --- | --- | --- |
| chat-02 | 134.599 秒；仅以原始短问题检索，六图均未确认，无视频卡 | Supervisor 收到图片不等于检索角色实际使用图片 |
| chat-03 | 533.244 秒；15 次 retrieve、12 次成功取帧，遇到取帧额度和上下文预算拦截；给出未核验集数，无卡 | 图像透传改善了检索行为，但 COMPLETED 不等于定位成功；不能把未验收写成 0/6 准确率 |
| chat-04 | 未再触发旧取帧／上下文拦截，但首轮 Query 到达 480 秒阶段截止 | 阶段预算与总预算分别影响任务能否完成 |
| chat-05 | 600.788 秒总超时；部分子搜索成功，但没有最终逐图交付 | 子工具找到候选不等于 Supervisor 完成交付；并行提示不自动带来端到端成功 |
| chat-06 | 三图组 600.675 秒失败；三个子搜索成功，最后返回约在第 595.9 秒 | 需要给最终组织答案和提交卡片留出预算 |
| chat-07 | 三图组 600.659 秒失败；最后一图已有相符原帧和草稿，尚未完成审核 | 候选、审核通过和最终交付是不同阶段 |
| 保留的 chat-08/09 | 同分组在 1200 秒预算下分别用 706.510、789.081 秒完成，每组交付三张卡 | 当时延长预算解决了这两个组的交付截断，不能据此宣称检索速度或泛化质量提高 |

最终六张卡对应集数为 5、5、1、7、13、4，覆盖独立核对的同镜头原片位置；没有完整唯一帧或所有重复位置真值。原始输入、两个分组 manifest、runner、[图片检索退役摘要](#图片向量与出处定位) 和 chat-08/09 的结果及核帧材料保留。

### 过时架构图和代码示例

N06 的图明确写着 `Interval: REFINING → INDEXING → COMPLETE`，把索引执行、重试和 Prefix 更新后的重索引交给 Interval。当前合同为 Interval 仅 Refine、同一 ChunkTask 承担 Parse/Index；这不是两张都有效的架构图。保留的依据为 [Parse Agent TRD](../design/02-agent-harness/video-parse-harness-trd.md) 和 [Engine TRD](../design/02-agent-harness/video-parse-engine-trd.md)。

N07 示例演示使用假模型执行两轮 Query/Evaluator、传递 advice、复用证据和回调，不执行真实 Provider／SQL／HTTP。它引用的 `server.agent` 和 `tests/server/test_agent_runtime.py` 已不存在，不能继续称为当前可运行示例。需要学习现有行为时，从 [video_search/runtime.py](../../agent/chat/video_search/runtime.py) 与 `tests/agent/chat/` 开始；示例中“证据复用和确定性输出校验”的意图可以保留，旧导入路径和接口不再保留为合同。

N08 三份历史文档保留的有用结论是：Engine 负责解析 Schema 与检索投影，Harness 消费 Schema 和工具；Server 负责持久化、任务、租约与恢复；阶段代码存在不等于真实运行验收通过。旧 Proposal/Patch/Canonical 路线、旧阶段接线状态及未实现清单已不适合作为当前建设清单。当前边界以两份 TRD 和实际代码为准。

`docs/design/refactor_2.md`、`refactor_3_es_qdrant.md`、`engine/parsev2/design/trd.md` 同时含当前修订和历史方案，而且仍被其他材料引用，不纳入整文件清理。特别是旧独立 build/publication、整版发布和 Interval INDEX 的内容可以归纳为已撤销决策，但不能据此连同文档中其他有效约定一起删除。

### 图库向量的分批运行

N09 的早期轮次先完成 2,360 个唯一图像向量，再复用缓存扩到 5,117 个。第四轮目标为 8,120 个，完成 8,118 个，两个请求在四次尝试后仍因 HTTP 429 限流失败。第五轮最终完成 12,389 个唯一图像向量，失败为零，并生成矩阵与行映射。

结论是按图像内容哈希复用已完成缓存、重试未完成项；早期进度文件不承担最终状态。第五轮 receipt、`embeddings.npy`、`embedding_rows.jsonl`、`gallery.jsonl`、`validation.json` 和原图继续保留；后续 shot-tags 的采样及排序直接使用它们。

## 不因未采用而直接删除的材料

| 材料 | 未采用或旧版本的事实 | 仍然保留的原因 |
| --- | --- | --- |
| `composition-models/20261019-shot-tags/pilot/cheap*`、`primary_b*`、两组 smoke 及对应 raw | cheap 模型没有入选；primary_b 是重复测量 | `pilot_report.py`、`validate_delivery.py`、`audit_full.py` 和中断核对仍读取它们，用于重算选型依据 |
| 同目录 `full/interruption.json`、`full/missing.jsonl`、原始 full manifest、未完成调用记录 | 全量运行被用户停止 | 定义已完成子集和未完成范围；没有它们就容易把探索子集误当全库完成 |
| 同目录 `features/runner_snapshots/*.py` | 是旧代码快照 | 固定特征来源版本，不能用当前脚本替代已发生推理时的代码 |
| `speaker-encoder/.../wavlm_large/`、`speaker-clustering/...wavlm-kmeans/` | 未接入该轮生产 | 后续聚类、参考库核对和 VoiceprintAgent 仍读取其 manifest、向量、runner 和固定选源数据 |
| `composition-models/20261017-first-pass/` 的模型结果 | 有些模型没有成为后续主方案 | 上层分析／报告直接读取 SAMP 等结果，layout-raster 和 shot-tags 还读取其模型记录；不能按整个日期目录删除 |
| `chat-image-origin/20261017-mygo-teacup-detail-source/run1/`、`run2/` | 两轮均未通过定位验收 | 第二轮虽完成回答，引用原帧仍不匹配，真实出处未确认；是未解决问题的失败样本，不存在可替代它的成功轮次 |

上述路径中的 `...` 只用于说明保留类别，不能作为删除选择器。

便宜打标模型的淘汰结论可以写进本总结，但现阶段还不能替代原始对照：三组 pilot 各 200 个样本最终 schema-valid；primary 两次自身一致性的景别 κ=0.784、角度 κ=0.888，cheap 对 primary 的对应 κ=0.497、0.673，未达到预先规定的 0.684、0.788 门槛，所以选 `gpt-5.6-sol`。这证明当时的一致性门槛未通过，不证明准确率低多少。详见保留的 [Shot-tags 归档摘要](#shot-tags-标注与排序)。只有连同旧对照的复算／验收入口一起退役，才适合只保留总结。

## 第一轮实际执行与验证

删除前已展开并固定完整清单，逐个核对文件类型、大小、修改时间和保留例外；删除使用准确路径，未对全部 `run*`、`old*`、`raw/` 或 JSON 做统一清理。Git 迁移 manifest 和 MDV 内的历史引用保持原貌。

- **删除核对**：88,113 个候选全部不存在；仅移除了由本次删除形成的 38 个空目录。
- **保留核对**：盘点范围内除本次有意更新的文档和报告脚本外，75,573 个保留文件均存在，大小和修改时间与执行前一致；40 个关键文件的 SHA-256 一致，覆盖 MDV 原件、独立 Document 导出、图表资产、成本表输入、合并审计例外和最终结果。
- **引用核对**：10 个入口／报告／生成脚本已更新；15 处新增的本地链接及标题锚点有效，这些文件中没有继续指向已删除文件的 Markdown 链接。历史目录树均作为当时记录保留，不冒充当前文件清单。
- **报告核对**：`VC/report.py` AST 解析通过，结构比较确认只改了三段报告文字；两次 superseded 执行及最终执行的三行成本表，均可从保留的 status／usage JSON 重算并与报告一致。
- **能力边界**：已删除轮次不能再逐条回放；保留摘要记录原实验条件、淘汰原因、后续替代及数值限制。本次没有重新调用模型或验证算法效果，也没有运行生产全量测试。

除本文件外，实际修改的入口如下；具体删除文件由上面的准确范围和三份审计例外定义。

```text
Seekino/
├── transfer.md
├── docs/
│   ├── design/
│   │   ├── agent-loop-references.md
│   │   └── agent-chat-delivery-contract.md
│   └── dev/
│       ├── design-experiment-cleanup.md  # 统一清单、探索结论和执行记录
│       └── devlog.md                    # 仅替换一处旧报告链接
└── engine/experiment/
    ├── voiceprint-agent/20261017-mygo-ep04-event-merge-no-prefix-retry/
    │   └── README.md
    ├── chat-image-origin/20261016-mygo-image-origin/
    │   ├── README.md
    │   └── runs/
    │       ├── chat-08/README.md
    │       └── chat-09/README.md
    └── speaker-clustering/20261018-mygo-ep04-voiceprint-agent/
        ├── report.md
        └── report.py                     # 仅同步报告文字
```

既有工作区改动保持原样，包括本次开始前已删除的 `docs/dev/perf_plan.md` 和 `docs/dev/problem_backup.md`；它们不计入本次 88,113 个删除文件。没有修改生产代码、配置或数据库，没有 commit 或 push。

## 第二轮：MyGO benchmark 整理与删除

同日按用户“帮我整理一下，然后就删掉多余 file”的授权，将两次 MyGO 评测收敛到当时的 `evals/mygo`。2026-10-25 又按能力拆分到 [evals/benchmark](../../evals/benchmark/README.md)，下方链接指向现入口，原文件数／体积／修改树仍描述 10-24 的清理。第一轮保留的这两个评测目录及旧总报告现已退役；下面为本轮新增删除，不与上面的 88,113 个重复计数。

| 删除范围 | 文件数 | 原 Git 状态 |
| --- | ---: | --- |
| `engine/design/mygo-chat-eval-20261015/` | 393 | 全部未跟踪 |
| `engine/design/event-retrieval-eval-20261018/` | 138 | 129 个未跟踪、6 个已跟踪、3 个被忽略 |
| `engine/design/mygo-chat-eval-20261015.md` | 1 | 已跟踪 |
| **合计** | **532** | **7 个已跟踪、522 个未跟踪、3 个被忽略** |

实际删除 **68,188,954 字节（约 65.03 MiB）**，移除 64 个空目录。新 benchmark 四个文件合计 324,757 字节；加一份离线测试共 332,568 字节，约 325 KiB。四个文件承接以下内容：

- 用例现分为 [Chat cases](../../evals/benchmark/query_capabilities/cases.json) 与 [检索 cases](../../evals/benchmark/retrieval_quality/cases.json)：合计仍为 26 个，即 13 个 Chat 用例与 13 个检索配置；保留输入、scope、播放上下文、多轮关系、10 条去重参考事实和评分要求。
- 观测现分为 [Chat baseline](../../evals/benchmark/query_capabilities/baseline.jsonl) 与 [检索 baseline](../../evals/benchmark/retrieval_quality/baseline.jsonl)：合计仍为 36 条，即 13 条 Chat 与 23 条检索观测；保留完整最终回答、实际播放卡、精简候选、排名与耗时、已有用量、历史判断及必要诊断。
- [run.py](../../evals/benchmark/run.py)：默认离线校验，支持历史回放及显式真实运行；新运行只产生一份 JSONL，写入被忽略的 `outputs/verification/`，已有输出拒绝覆盖。
- [README.md](../../evals/benchmark/query_capabilities/README.md)：保存实验背景、操作方法、历史结论、评分和数据前提。Chat 的覆盖／上下文交接问题，以及 Event 就绪条件、候选曝光与最终交付的区别，都在这里归纳。

旧日志、重复 SSE、全部候选正文、原生 rollout、图片、源码快照和展开报告已删除，不能再进行原始全链路取证。源路径与 SHA-256 仅保留为历史来源说明，不是当前文件依赖，也不能用于恢复已删除内容。固定事实不代表穷尽金标；人工评审要求继续保留，不把任务完成或来源检查通过等同于回答准确率。

旧 premise 用例曾错误沿用 09:08 的立希目标。本轮依据独立候选原文，将对应参考修正为 EP04 20:03.2–20:08.7 爱音谈和弦的台词；历史旧排名仍以 `legacy_target_*` 留存，当前来源检查使用修正后的参考。这项修正及适用边界见 benchmark 的历史结论。

### 实际修改树

`[A]` 表示本轮新增，`[M]` 表示本轮修改已有工作区文件，`[D]` 表示本轮删除。目录下数百个本地产物按上表的完整目录范围列示；下树单列七个已跟踪的删除文件。

```text
Seekino/
├── evals/mygo/
│   ├── [A] README.md
│   ├── [A] cases.json
│   ├── [A] run.py
│   └── [A] baseline.jsonl
├── tests/evals/
│   └── [A] test_mygo_benchmark.py
├── docs/
│   ├── design/
│   │   └── [M] visual-retrieval-trd.md
│   └── dev/
│       ├── [M] devlog.md
│       ├── [M] problems.md
│       └── [M] design-experiment-cleanup.md
└── engine/design/
    ├── [D] mygo-chat-eval-20261015.md
    ├── mygo-chat-eval-20261015/             # 全部 393 个本地产物已删除
    └── event-retrieval-eval-20261018/       # 全部 138 个文件已删除
        ├── [D] README.md
        ├── [D] EP04.md
        ├── [D] EP04-ZH.md
        ├── [D] analyze_chat.py
        ├── [D] run_chat.py
        └── [D] run_retrieval.py
```

本轮新增 5 个文件、修改 4 个既有工作区文件、删除 532 个文件。`devlog.md`、`problems.md` 与视觉检索 TRD 中的旧入口已更新，历史描述同步标明证据已压缩。Git 迁移 manifest 中的旧路径保留历史含义；没有改写生产代码或现有 `.gitignore`。

### 验证与当前 diff

- 删除前逐个验证 532 个源文件的类型、大小、修改时间与 SHA-256；36 条历史观测逐条核对原回答、播放区间、检索候选及耗时，10 条参考事实保留来源说明。删除后确认两个目录及旧总报告均不存在，新文件哈希保持一致。
- 删除后再次运行默认离线校验、历史回放和 `tests/evals/test_mygo_benchmark.py`：26 个用例可读取，36 条观测可评分，**14 项测试通过**，无真实 API 调用。测试覆盖多轮关系、请求字段、已知漏例、时间／scope／说话人核对、失败退出与输出覆盖保护。
- 历史回放为 32/36 次执行完成、28/36 次通过已知来源检查；四次索引未就绪、三次 Chunk 漏召回及一次 T07 已知漏例继续作为失败基线，**这些数字不是整体准确率**。新的真实模型与在线检索评测尚未执行。
- 依赖扫描覆盖代码、测试、文档和被忽略的实验材料，未发现保留脚本继续读取两个旧目录。剩余旧路径字符串属于来源元数据、迁移历史或本清理记录；新增本地文档链接及锚点已校验，修改文件通过空白差异检查。
- 完成时 `git status --short --untracked-files=all` 为 **74 个路径：16 个修改、28 个删除、30 个未跟踪**。其中已跟踪文件的删除仍会出现在 diff；其余还包含既有代码改动、图表／文档及新增 benchmark，不能把剩余数量直接当作待删废料。并行出现的工作区改动保持原样，没有 commit 或 push。

## 第三轮：密集实验数据的本地 ZIP 归档

同日按用户“超多文件类型请压缩为 zip，然后我们不 push”的授权，把前面盘点出的六类密集数据转成可恢复的本地归档。**本轮没有废弃更多实验信息**：72,501 个散文件全部装入六个 ZIP，逐文件验证后才移除散文件，并清理 21,116 个空目录。两份已跟踪的报告／源码原样保留，不放入 ZIP，避免解压覆盖它们。

下面路径均相对 `engine/experiment/composition-models/`；MiB 为逻辑字节除以 1,048,576。

| 归档文件 | 保存的原文件数 | 原体积 MiB | ZIP 体积 MiB |
| --- | ---: | ---: | ---: |
| `20261019-shot-tags/raw.zip` | 34,789 | 521.459 | 90.230 |
| `20261019-shot-tags/rankings.zip` | 180 | 274.481 | 28.720 |
| `20261019-shot-tags/pilot.zip` | 4,389 | 38.811 | 7.966 |
| `20261019-shot-tags/features.zip` | 8,307 | 34.647 | 16.247 |
| `20261018-shot-gallery/frames.zip` | 12,447 | 863.082 | 865.478 |
| `20261018-shot-gallery/embeddings.zip` | 12,389 | 49.907 | 30.258 |
| **合计** | **72,501** | **1,782.387** | **1,038.900** |

原数据 1,868,967,898 字节，ZIP 共 1,089,365,355 字节，节省 779,602,543 字节，约 **743.5 MiB**。整个 `experiment` 的文件数从 **74,972 降到 2,478**，含六个 ZIP 和一份归档目录清单；总逻辑大小约 **1.502 GiB**。图片使用 ZIP STORED 保留原 JPEG，不做有损重编码；包头及逐文件清单使图片包比原图片总和略大，收益主要是减少散文件。其余数据使用 ZIP DEFLATE level 6；这里的数字不同于之前只读测量的 tar.gz level 1。

### 保留与恢复边界

- `shot-tags/pilot/report.md` 和 `shot-tags/features/runner_snapshots/7dfe04142bcfa692611e78a106bea75291581f9c566c7f9f36a3bcea5d899a0a.py` 的 SHA-256 与归档前一致，仍保持原路径。
- 总向量矩阵 `embeddings.npy`、`embedding_rows.jsonl`、图库 manifest、OP/ED 标注、最终 tags、顶层报告、脚本及其余实验继续保持展开状态。每图向量也保存在 ZIP 中，本轮没有借“重复”丢弃任何向量。
- [构图本地归档与恢复说明](#本地归档与恢复) 记录每个 ZIP 的 SHA-256、大小、原文件数、恢复目录与保留例外。各 ZIP 内的 `_archive_manifests/<目录名>.json` 保存全部原文件路径、SHA-256、大小、权限和纳秒修改时间。
- [构图本地归档与恢复说明](#本地归档与恢复) 给出 Python 标准库解压命令。旧链接和实验脚本继续描述解压后的原布局；需要图片、原始调用、排名或特征缓存时先恢复相应归档。尚未恢复时不能把缓存缺失当成需要重新调用模型。
- 解压时先核对 ZIP 的 SHA-256；只在归档数据原路径尚未被新运行占用时恢复原位，否则先解压到独立目录。标准 ZIP 解压可恢复内容与目录布局；精确纳秒时间等附加元数据另保存在清单中。
- 六个 ZIP 和目录清单都命中已有 `.gitignore:15` 的 `**/experiment/`，没有修改忽略规则、执行 `git add -f`、commit 或 push。这些是本地保存，未建立异地备份；它们不会出现在普通 clone 中。

### 实际修改树与验证

```text
Seekino/
├── docs/dev/
│   └── [M] design-experiment-cleanup.md
└── engine/experiment/composition-models/
    ├── [A] archives-20261024.json                 # 本地忽略，归档摘要及 SHA-256
    ├── 20261018-shot-gallery/
    │   ├── [M] README.md                        # 恢复说明
    │   ├── [A] frames.zip                       # 承接 12,447 个散文件
    │   └── [A] embeddings.zip                   # 承接 12,389 个散文件
    └── 20261019-shot-tags/
        ├── [M] README.md                        # 恢复说明
        ├── [A] raw.zip                          # 承接 34,789 个散文件
        ├── [A] rankings.zip                     # 承接 180 个散文件
        ├── [A] pilot.zip                        # 承接 4,389 个散文件
        └── [A] features.zip                     # 承接 8,307 个散文件
```

本轮新增 7 个被忽略的归档／清单文件、修改 3 个文档，移除的 72,501 个散文件均未受 Git 跟踪。精确成员路径完整保存在各 ZIP 的逐文件清单中；没有删除已跟踪文件。验证覆盖：

1. 固定准确清单，检查所有成员都是普通文件、没有符号链接，且均被 Git 忽略；保留所有已跟踪例外。
2. 建包后重新读取全部 72,501 个数据成员，通过 ZIP CRC 和 SHA-256 检查，成员清单无遗漏、无重复；所有归档成功才进入散文件清理阶段。
3. 删除前再次核对每个原文件的类型、大小、修改时间、文件身份与 SHA-256，并对六个包做实际解压抽查，共 **17 个样本文件**内容一致。
4. 删除后确认全部原散文件已移除、六个 ZIP 的 SHA-256 不变、两份保留报告／源码哈希不变；没有遗留 `.partial` 归档。新增文档链接与恢复锚点、修改文档的空白差异检查通过。

本轮没有修改生产代码、数据内容、数据库或运行配置，也没有重跑模型；验证针对归档完整性和可恢复性。其余并行工作区变更保持原样。

## 第四轮：旧实验整体退役，构图研究仅留本地归档

2026-10-24，按用户对八类实验“该总结的总结，该删除的删除”的授权执行。前面三轮中的“继续保留”“必须保留”描述的是当时保留旧实验复算能力的边界，本轮已撤销该要求；下列状态优先于历史记录。生产代码和正式测试未发现对 `engine/experiment/` 的读取依赖；实验之间的依赖随整组退役处理。

本轮处理 **2,478 个文件、1,612,352,476 字节**：其中 **1,807 个文件、205,106,176 字节（195.604 MiB）总结后直接退役**；**671 个文件、1,407,246,300 字节**进入一个本地 ZIP 后移除原文件。归档包含构图目录全部 660 个文件，以及它引用的 10 张来源图片和 1 份冻结标注；不包含其余旧声纹、检测、图片定位实验。**已完成删除与检查，原 `engine/experiment/` 目录已不存在，另清理 132 个空目录。**

| 原类别 | 原文件数 | 原大小 MiB | 本轮去向 |
| --- | ---: | ---: | --- |
| `speaker-clustering` | 485 | 31.713 | 全部退役，包括 439 段 WAV、试听网页及旧聚类／选源结果 |
| `voiceprint-agent` | 942 | 73.499 | 全部退役，保留下面的迭代结论 |
| `speaker-encoder` | 98 | 6.798 | 全部退役，保留模型对照和适配器核验结论 |
| `campplus` | 74 | 8.560 | 全部退役，保留固定参考池和聚合方式的结论 |
| `grounding-dino` | 122 | 46.972 | 114 个退役；7 张来源图片和 1 份标注随构图归档 |
| `image-embedding` | 24 | 20.413 | 全部退役，保留小样本对照的适用边界 |
| `chat-image-origin` | 73 | 24.007 | 70 个退役；3 张来源图片随构图归档 |
| `composition-models` | 660 | 1,325.698 | 全部本地归档，含 shot-gallery、shot-tags 和已有六个 ZIP |

保留的工程入口是 [MyGO benchmark](../../evals/benchmark/query_capabilities/README.md)、当前设计合同及 `outputs/verification/` 下的正式验收材料。没有用这些旧实验生成新的伪真值或准确率，也没有把未解决图片案例自动升级为有标准答案的 benchmark。退役目录的完整日志、试听、逐条矩阵和复算脚本不再提供；下列摘要保存的是当时的观察和限制。

### 声纹基础实验

来源：原 `campplus/20261017-mygo-ep04`、`20261017-soyo-emotion`、`20261017-balanced-multiref` 及 `20261018-diarization-research.md`。

- CAM++ 四人各六条参考的固定小样本中，单参考、双参考和留一排名分别匹配原标签 20/20、16/16、24/24；参考标签来自当时的 Timeline，只有特定目标有用户确认，不能当整集人工真值或未知人物识别准确率。模型为官方 CAM++ 中文 16 kHz checkpoint，192 维，未训练。
- 同人与异人余弦分数有明显重叠：同人最低约 0.420，异人最高约 0.721。“超过 0.7 即确认身份”不成立。混合发言长段会偏向占时长较多的人，裁剪边界与是否换人必须先核对。
- 同一爽世目标 47.7–48.7 秒，与不同参考的分数可从 0.433279 到 0.729227；相近场景高分还混有同一句话因素，不能全部归因于情绪。九组裁剪中七组长段更高、两组更低，时长与分数并非单调关系。
- 三人各五条固定参考中，爽世逐条均值 0.617295、归一化质心分数 0.744100。单位向量下均值为 `t·μ`，质心分数还除以 `||μ||`，因此两种聚合的数值和人物排序不能直接混比。AHC／谱聚类材料是调研候选，没有被这批实验完成验证。

### 声纹模型与适配器

来源：原 `speaker-encoder/20261018-mygo-ep04-model-comparison` 及其 `wavlm_official_pinned`。

- 同一批 47 段 PCM 比较 CAM++、ERes2NetV2、两版 ReDimNet2 和 WavLM；v11 与旧 balanced 参考池隔离。v11 爽世难例的逐条均值排名：CAM++／ERes2NetV2 第 4，两版 ReDimNet2 第 1，WavLM 第 5；换成 balanced 池后各模型均第 1，说明参考集合影响很大。
- 两版 ReDimNet2 难例领先幅度仅约 0.0269／0.0077，且只有两个独立台词得到用户身份确认，不能据此宣布生产选型胜出或计算全量准确率。
- WavLM 使用同一权重和 47 段音频在官方旧版环境复测：最大向量差约 `3.30e-6`，585 对余弦最大差约 `4.59e-6`，人物排序未变。适配器数值差异不能解释该难例的错误排名。权重来自镜像，未证明与不可用的微软下载逐字节相同；旧环境复测也不是完整训练环境复现。
- 本轮只移除实验目录。`outputs/local-models/` 等目录中的模型权重和环境不在清理范围，不能由实验代码退役推断生产模型已切换。

### 聚类与选源

来源：原 `speaker-clustering/20261018-mygo-ep04-wavlm-kmeans` 与 `20261018-mygo-ep04-voiceprint-agent`。

- 24 个 Chunk、378 条 ASR cue 切成 436 个短窗，另有 3 个留出版本，共 439 段音频；226 个窗参与拟合。在 L2 向量上运行普通欧氏 K-means，尝试 K=2–12；不是球面 K-means，也没有 PCA／白化。自动选择 K=2 时爱音与爽世目标落入同簇，轮廓系数不能替代人物分离质量或真实人数。
- K=8 选源后，用相同六人物候选池复核：爽世逐条均值 0.529157 仍低于乐奈 0.529823；质心分数则为爽世 0.593479、乐奈 0.529823。只展示有利聚合会掩盖失败，不能认定问题已解决。
- VoiceprintAgent 最终轮用时 149.47 秒，登记 15 条、5 人，未达到 11 人名单中至少 6 人的覆盖门槛，`ready=False`，没有发布正式声库。Agent 当时依赖候选资料和图片，没有完成独立听音身份验收；画面张嘴、台词提到某个人名均不足以确定说话人。
- 五人草稿库中爽世第一，补回缺失竞争人物后逐条均值仍落后乐奈；旧库中爱音本来已第一。增加候选或参考条数没有在本样本证明必然改善。试听与旧报告整体退役，生产声纹合同仍以 [voiceprint-trd.md](../design/02-agent-harness/voiceprint-trd.md) 为准。

### VoiceprintAgent 历次迭代

来源：原 `voiceprint-agent/` 下 v4–v11 的终态报告、来源快照及执行／跨组审计。以下是历史观察，不是当前服务状态。

- v4 暴露调用超时被重试掩盖、Refine／Index 未完整完成等链路问题；v5 完成运行并把目标从灯修正为爽世。v6 因代理连接失败未进入有效算法比较；同配置重跑 v7 用时 43:22.011，v5 为 38:43.518，慢约 12%，未通过整集提速验收。翻译变化触发声音参考指纹变化是已确认的部分刷新原因，不是所有刷新的完整归因。
- v8 更快但目标人物仍错；35.6–48.6 秒 ASR 跨说话人，不宜把整段向量直接当短句身份。v9 把 47.6–48.8 秒爽世台词登记到灯，本组留一均值约 0.378，爽世组约 0.716，暴露参考身份污染。
- v10 出现爱音登记为祥子的参考；与另一候选余弦约 0.652 仍不能证明同一身份。v11 在 18/24 个 Chunk 后启动，得到 6 人 31 条参考，存在两条已知错标；爽世六条中的四条来自同一 Chunk，来源覆盖仍不充分。
- v11 跨组审计中，30 条可做本组留一比较，25 条本组最高、5 条别组更高；这是与现有登记的一致性，不能报为 25/30 识别准确率。低分是复核信号，不等于错标。旧 75% 准备门槛、聚类／Agent 选源实验与后续 B0 合同分开看待，生产行为由当前代码和 TRD 决定。

### GroundingDINO 检测

来源：原 `grounding-dino/` 中 MyGO 六图、四档分辨率、大厅结构和水族馆四组实验。

- 本机六图测试中 MPS 流程中位约 784.81 ms／张、CPU 1999.66 ms／张；没有完整人工标注框，不能转成 AP 或通用检测准确率。
- 360p／480p／720p／1080p 来源图进入模型后都被处理到约 1333×750，因此降低源图分辨率没有证明能降低推理成本。480p／720p 人物框几何较稳定，360p 出现额外框；这是预测一致性，不是独立真值评测。
- 大厅样例的普通物件、建筑结构、直接上下区域提示分别得到 9、17、0 个框。固定提示失败不代表模型永远无法描述上下结构；梁的矩形框也不等于构图分界线。
- 水族馆先冻结助手给出的六条线与两个人物框再跑检测，避免看答案后改标注；但助手标注不是专家真值。DINO 的梁框没有提供线段方向与端点。供构图研究使用的来源图和该标注已随本地归档保留，其余报告、绘图和检测输出退役。

### 图片向量与出处定位

来源：原 `image-embedding/20261017-mygo-cup-crop`、`20261017-mygo-anon-crop`、`chat-image-origin/` 中六图、咖啡桌和茶杯案例；六图逐轮预算问题亦见前文。

- 六图分组在 600 秒预算下未完成交付，延长到 1200 秒后两组分别用 706.510、789.081 秒完成，共六张有原片位置核对的卡。解决的是这些任务的交付截断，未证明检索更快或全片召回可靠。
- 咖啡桌裁剪图经扩大描述范围找到 EP01 约 17:33；茶杯第一次 1200 秒超时，第二次虽完成却给出不匹配的 EP02 11:38.5，真实出处仍未解决。不同轮次模型／环境有变化，不构成严格 A/B。失败结论保留，原图未另立新 benchmark。
- 当时 Chat 走图片描述、文本检索和候选帧核验链路，并非已接入直接图像向量检索。Doubao 小池中茶杯裁剪检索整图为 7 个候选第一，余弦约 0.364、领先约 0.032；反向整图检索裁剪却排末尾。爱音脸部与整图约 0.705，与另一爱音图约 0.676，难以单靠小差值确认同一帧。
- 这些对照缺少全片困难负例与独立标注；余弦不是概率，双向检索也不对称。保留为链路、裁剪和误匹配风险的结论，原始输出／日志退役；三张构图来源图仅作为构图归档的输入保留。

### 构图模型与图库

来源：归档内 `composition-models/` 的 first-pass、layout-raster、person-box-fusion、Chat／Event 构图、Event 回填及 shot-gallery 实验。

- 17 张动漫图对照 PCP、SAMP、CLIP、DINOv2、MUSIQ 等表示，并结合检测框／线段。十个探索性三元组中 PCP fc7 为 8/10、CLIP 整图为 7/10；样本小且带主观构造，不是构图准确率。图像质量、构图类别与空间结构表征不可混为同一能力。
- 纯线条表示有可用迹象，所有框栅格化后做全局向量没有稳定优势；融合在部分例子有效，尚无通用最优配方。PCP＋人物框仅 13/17 图可用，缺框不等于图中无人，手设融合权重未经过独立调参验收。
- Event 回填完成 13 集、312 个 Chunk、7,103 个 Event，记录 16,535 次采帧；这是采样记录数，不是唯一图片数。内容 ID／版本／SHA 未变证明回填边界，不证明构图检索效果。Chat、Event 和小离线池的候选集合不同，不宜直接比较排序胜负。
- 共享图库包含 5,898 个 shot、12,447 个关键帧、12,389 张唯一 JPEG、5,585 个复用组，并映射 7,103 个 Event；保存了 1024 维 Doubao 向量、行映射、稳定 ID、时间、SHA 和 OP／ED 标注。这些是最有后续复用价值的研究数据，已整体归档；不是生产启动依赖。

### Shot-tags 标注与排序

来源：归档内 `composition-models/20261019-shot-tags/` 的 pilot、full、features、exploration 与排名材料。

- Pilot 做了三轮各 200 张，主模型两次标注在景别／垂直角度上的 κ 约 0.784／0.888；便宜模型与主模型约 0.497／0.673，未达当时预设门槛，未选用。主模型一致性不等于人工语义正确率。
- 全量标注按用户要求中止，最终覆盖 4,015 个关键帧、2,889 个 shot、3,964 张唯一 JPEG；数据明显偏 EP01–07，不是完整 13 集。离线特征已覆盖 8,147 个目标帧／8,092 张唯一图片，与 VLM 标注完成度是两回事。
- 25 个 Query、10 个替代裁剪，M0–M5 六种方法共 150 组排名；735 对经去重形成 718 个盲评对。没有人工判断，`score.py` 未执行，因此不能声称标签、几何或融合方法提高了检索质量。Depth Anything／M6 没有运行，不属于已完成结果。
- 标注、向量、人工复核材料、脚本、运行配置和已有密集数据 ZIP 一并保留本地。后续恢复时先识别哪些结果已完成，避免把中止轮次当完整数据集或重复调用模型补缓存。

### 本地归档与恢复

归档：[composition-experiments-20261024.zip](../../outputs/archives/composition-experiments-20261024.zip)，**1,221,285,035 字节（约 1.137 GiB）**。校验与成员说明：[composition-experiments-20261024.manifest.json](../../outputs/archives/composition-experiments-20261024.manifest.json)。两者命中原有 `/outputs/` 忽略规则，不进入普通 `git add`／push／clone。本轮没有新增忽略规则，也没有建立异地备份；在其他机器上只能看到摘要，需自行取得此归档才能复查构图原始数据。

ZIP SHA-256：`7f6fd60a6f01e055691489c6ab0bb7be6f83321525284bebd079a5b0f8ab1b47`。

外层 ZIP 按原仓库相对路径保存 671 个文件，另附 `_archive_manifests/composition-experiments-20261024.json`，记录每个文件的大小、SHA、权限和纳秒时间。已有六个密集数据 ZIP 原样放入外层，SHA 保持不变，继续保存前轮归档的 72,501 个原始散文件。图片不做有损重编码，大部分体积来自已压缩 JPEG，归档主要解决散文件和 Git 污染问题。

在仓库根目录运行下列命令，会先核对 SHA，再解压到一个新建临时目录，不覆盖当前工作区：

```python
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

archive = Path("outputs/archives/composition-experiments-20261024.zip")
catalog = json.loads(archive.with_suffix(".manifest.json").read_text())
checksum = hashlib.sha256()
with archive.open("rb") as source:
    for block in iter(lambda: source.read(1024 * 1024), b""):
        checksum.update(block)
assert checksum.hexdigest() == catalog["sha256"]
restored = Path(tempfile.mkdtemp(prefix="seekino-composition-restore-"))
with zipfile.ZipFile(archive) as bundle:
    bundle.extractall(restored)
print(restored)
```

外层解压后，先读 `engine/experiment/composition-models/archives-20261024.json` 及图库／shot-tags README 的 `Local ZIP archive` 部分，再按需解开六个内层包。它们的成员路径相对各自日期目录，例如 `frames.zip` 解到 `20261018-shot-gallery/`，`raw.zip` 解到 `20261019-shot-tags/`；应在尚未被新运行占用的位置恢复。标准 ZIP 恢复内容，精确纳秒时间等元数据另见清单。

这是历史研究材料归档，不是可直接启动的独立环境。原视频、数据库／索引、`.cache` 与 `outputs/local-models` 中的权重／环境未打包，历史脚本含原工作区绝对路径和当时接口，重跑前需核对依赖。当前清理只触及 `engine/experiment/` 和本文列出的文档入口。

### 本轮修改范围与验证

```text
Seekino/
├── docs/dev/design-experiment-cleanup.md      # 汇总退役结论和恢复方法
├── docs/{README.md,dev/{devlog.md,problems.md,OKR.md}}
├── agent/video_parse_v2/design/voiceprint-trd.md
├── engine/design/{shot-tagging-experiment-brief.md,shot-composition-schema.md,多模态理解解析研究.md}
├── engine/experiment/                        # 整体移除原文件及空目录
└── outputs/archives/                         # 本地忽略
    ├── composition-experiments-20261024.zip
    └── composition-experiments-20261024.manifest.json
```

本轮修改上述 9 份文档，新增 2 个被忽略的归档／清单文件，移除 2,478 个原文件和 132 个空目录。原文件中 139 个已受 Git 跟踪；加上前轮已有的 9 个删除，Git 中仍显示 148 条 `engine/experiment/` 删除记录，这是待提交的清理差异，不是文件仍留在磁盘。

- 归档重新读取全部 671 个数据成员，通过 CRC 和 SHA-256 检查，并实际解压四个跨格式／跨目录样本核对内容；六个内层 ZIP 的 SHA 保持不变。
- 删除前对全部 2,478 个文件核对类型、大小、修改时间、文件身份和 SHA，确认没有新文件或并发改写后才逐个移除。删除后确认源目录不存在、归档完整、没有 `.partial` 文件。
- 生产代码、正式测试与配置扫描未发现对退役目录的读取；benchmark 中的 `provenance.retired_path` 是历史来源字符串。默认离线校验读取 26 个用例，历史回放读取 36 条观测，仍为 32/36 次执行完成、28/36 次来源检查通过；**14 项 benchmark 测试通过**，没有调用真实 API。benchmark 文件与原 `.gitignore` 的 SHA 未变。
- 原实验文档链接已改为摘要或归档说明；不再把旧试听、原始矩阵、完整报告链接伪装为仍可访问。迁移 manifest 保留历史路径。新增链接／锚点和本轮文件的空白差异检查通过。
- 没有修改生产代码、运行配置、数据库、索引或模型文件，没有 commit／push。完成时仍有大量 `.research/` 未跟踪材料：`pi-6aedd106` 1,646 个文件、`gondolin-0.12.0` 347 个文件，属于本轮八类实验之外的源码副本，没有一并删除或忽略。

## 第五轮：忽略本地研究副本与临时文件

2026-10-24，用户反馈变更数再次超过 3,000。与第四轮完成时的 2,216 项状态对比，当前 3,370 项新增了 1,154 项：`.research/shotdetect-deps/` 中的 NumPy、OpenCV、scenedetect 等本地依赖占 1,153 项，另有 `.kimi-tmp/parse-deepagents-discussion.txt` 1 项。已跟踪文件的修改／删除状态没有变化，`engine/experiment/` 未重新出现。

确认 `.research/` 和 `.kimi-tmp/` 没有被 Git 跟踪的文件后，在根 `.gitignore` 增加 `/.research/` 与 `/.kimi-tmp/`。保留所有本地研究副本、辅助脚本、依赖和临时文本；正式研究结论仍位于 `docs/research/`，不受规则影响。此次只修改忽略规则及本记录，没有删除这些材料，也没有 commit／push。

验证后 Git 状态从 **3,370 项降到 215 项**：18 个修改、167 个删除、30 个未跟踪。移出变更列表的 3,155 项全部属于上述两个本地目录，逐项确认仍在磁盘；已跟踪文件的状态没有被隐藏，原实验目录仍不存在，归档继续被忽略。本轮修改文件通过 `git diff --check`。剩余删除项中 148 个来自 `engine/experiment/`，属于应提交的清理结果。
