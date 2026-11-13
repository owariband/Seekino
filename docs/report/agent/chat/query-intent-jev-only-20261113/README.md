# 关闭独立改写器后的 Chat 三路径复测：2026-11-13

本次 B 明确为 **JEV 分类 → Supervisor**。三组均关闭独立改写器，重新运行全部 27 个完整 turn，每组 9 个，没有沿用旧表的 A／C。B 的平均 TT 为 9.49 秒、总 token 为 257239；C 的平均首段正文早 0.13 秒，平均 TT 多 2.5%、总 token 多 20.0%。这个小样本支持继续保留 JEV-only 作为对照，尚不足以证明任何路径普遍更优。

关联：[同一 TRD 第 7 节](../../../../design/03-product-reassessment/query-intent-routing-comparison-trd.md#7-关闭独立改写器的同条件复测) · [PR #151](https://github.com/owariband/Seekino/pull/151) · [devlog](../../../../dev/devlog.md#devlog-chat-intent-jev-only-20261113) · [管理工作台](../../../../dev/workbench.html) `CHAT-INTENT-01`。旧 [JEV＋改写器实验](../query-intent-routing-20261113/README.md)独立保留，不能代表本次 B。

同日补充：六条query、完整历史、27条逐字回答和评分依据已整理为[按query排列的评阅](query-quality-review.md)与[结构化评分](reviews.json)。默认值、dev/main发布及Cloud/CPU切换另由[上线TRD](../../../../design/03-product-reassessment/jev-only-hook-rollout-trd.md)和[PR #155](https://github.com/owariband/Seekino/pull/155)记录；下文“不改Cloud”限定原实验时点。

## 重新实测的结果

| 指标 | A：DeepSeek 前置决策 | B：JEV 单独分类，改写器关闭 | C：Supervisor 直接判断 |
| --- | ---: | ---: | ---: |
| 前置层平均耗时 | 1.40 s | 1.67 s | 0 |
| FTT 平均／中位 | 7.07 / 6.23 s | 6.73 / 5.97 s | 6.60 / 4.81 s |
| TT 平均／中位 | 9.99 / 10.28 s | 9.49 / 8.29 s | 9.72 / 10.39 s |
| 全阶段 input token | 274684 | 248674 | 297434 |
| 全阶段 output token | 10250 | 8565 | 11356 |
| 总 token，9 turn 合计 | 284934 | 257239 | 308790 |
| Supervisor 模型请求数 | 29 | 27 | 32 |
| 独立改写模型请求数 | 0 | 0 | 0 |
| 完成数 | 9/9 | 9/9 | 9/9 |

FTT 从服务开始执行到首个非空用户可见正文事件，排除 reasoning、状态和工具事件；27 条首次正文事件均为 `answer.snapshot`，不是 provider 原生 TTFT，也没有计浏览器网络与渲染。TT 到整轮自然终态和持久化完成。三组都无整轮失败或超时，但 A 有一次前置结构化决策 `ValidationError`，随后按既有规则跳过提示追加，Supervisor 正常完成；其耗时和 token 全部保留。见 [frontstage-failures.json](frontstage-failures.json)。

全阶段包含前置判断、Supervisor 和检索 embedding；共 129 次 provider 请求，128 次 completed、1 次 failed，input／output／total usage 均完整，合计 850963 token。reasoning 是 output 的子集，不重复相加。缓存、reasoning 对不提供字段的服务保留未知，不能把总 token 比例当费用比例。没有调用 `query_video`，本批 `agent_chat` 请求全部来自 Supervisor，没有 Query 子 Agent 的混计。

## 关闭改写器和条件一致性的证据

三份隔离 profile 都设置 `query_hook.rewriter_enabled=false` 并移除 `models.query_rewriter`，实际 `retrieval_slot` 请求全部为 0。B 有 9 次成功的 `query_hook_route`，没有 `query_hook_decision`；原始消息保留，也未追加候选检索词。A 保留原有一次合并判断 `action + retrieval_query` 的职责；本次关闭的是独立改写器，没有偷偷改变 A 的决策 schema。C 不执行前置层。见 [零改写审计](no-rewriter-audit.json)与[配置差异](profile-diff.json)。

使用 dev 基础上的冻结提交 `0bc42b6734f183b5dad12446d70a24396c0999d8`。对照上一轮的 17 个关键生产文件，源码哈希一致；本轮只增加评测装配的 `--jev-only` 模式及验证，保留历史协议复跑能力。Supervisor 固定 `deepseek-flash / high / 8192`、temperature=0，共用 prompt、工具 schema 和角色／正文历史；九组三方输入审计全部一致，只有前置处理结果按实验路径变化。见 [protocol.json](protocol.json)、[input-audit.json](input-audit.json)。

三组实际 runtime 都从模型能力取得 context=1048576，compact 阈值为 891289（85%），keep=24000，输出预留 8192、安全余量 4096；旧 YAML／runtime fallback 的静态值不等于本次生效策略。短历史未观察到 compact，本实验不能再次证明长上下文压缩收益。

沿用同一份[六类样本](../../../../../evals/benchmark/intent_routing/three_path_20261113_cases.json)，包含反馈、已有证据解释、停止检索、混合任务、指代、保留原话；前三类各一轮，反馈／混合／原话检索再重复一次。每个 case 三组轮换，第二轮按既定反序运行，不按已看到的结果挑选样本。每轮观察上限 300 秒、并发为 1。一个正式前集成探针不并入表格。

素材仍为一段已发布的 MyGO EP04 原片 8–20 秒、总长 12.012 秒短片，一条 Chunk、六个 Event；使用隔离 `seekino_intent_20261113_test` 和两个 `*_test` ES 索引、generation=1、原有 `doubao-embedding-vision-251215 / 1024` 编码合同。没有复制 Cloud 私密历史。Event 与 Chunk 检索都预检回源成功，未启动解析 Consumer，未迁移来源库。见 [fixture-snapshot.json](fixture-snapshot.json)。

## 输出内容与实际代价

以下为Codex主Agent对正文、工具和正式Timeline的逐条核对，没有独立人工盲评。原实验先保留问题明细，同日再补做完整／部分／失败的逐条评阅。query和expected在运行前冻结，细化评分及边界判定属于事后明确。完整27条原文及最终卡片在[outputs.md](outputs.md)，包含query和历史的对照见[query-quality-review.md](query-quality-review.md)。

| 质量口径 | A DeepSeek前置 | B JEV-only | C 直接判断 |
| --- | ---: | ---: | ---: |
| 实际任务选择正确 | 9/9 | 9/9 | 8/9 |
| 完整交付通过 | 7/9（77.8%） | 7/9（77.8%） | 4/9（44.4%） |
| 完整／部分／失败 | 7 / 2 / 0 | 7 / 2 / 0 | 4 / 4 / 1 |

任务选择看是否正确回应／检索及混合任务先答后查；完整交付还要求主要结论、明确指令和最终卡片满足要求，部分通过不折算半分。C第020条的扣分仅为一句多余过程说明，若忽略此项则为5/9（55.6%）。A有一次前置ValidationError，故有效标签是8/9；9/9是下游实际任务行为。每组9turn只有6种query，这些结果不能作为模型总体准确率。

- C 第 002 条在纯表达反馈中调用了 `get_series_overview` 和 `search_video_events`，仍有额外检索；其余五条反馈样本未检索。C 第 006 条给出一句后又追加解释和索取文本，未严格遵守“压成一句”。A／B 对应压缩样本都只返回一句。
- 六条混合任务均先有正文回应反馈，再进行检索；部分回答最终又重复了一遍同样的认错说明，不能用更早的 FTT 等同于更简洁的交付。
- 六条原句检索的最终正文都保留“因为《春日影》是首好歌”，但 B 第 017 条把远景镜头和燈回应的先后描述反了，JEV-only 同样不等于内容已经全部正确。
- A／B／C 都能找到短片，但同 Chunk 多次提交仍会更新为后一次范围。例如第 009／010／011／014／022 条最终卡片不再覆盖开头原话，第 014 条也漏了燈回应；第 011 条正文却声称两段合并为全片。该既有卡片合同问题未在本次修改。

结构化提取与解释性观察见 [behavior-checks.json](behavior-checks.json)。这些差异同时包含生成随机性和工具合同影响，不能全归因于前置分类器。B 本批比 C 少五次 Supervisor 请求，直接跳过分类层并没有稳定减少后续工作。

## 验证范围与复跑入口

独立从原始 result／usage 复算与汇总一致；27 条终态正文均与 SQL 一致，测试库无 PROCESSING，见 [persistence-audit.json](persistence-audit.json)。原始文件哈希见 [raw-provenance.json](raw-provenance.json)，不含凭据的汇总见 [metrics.json](metrics.json)。编译、离线 27 步计划、旧协议聚合兼容检查、源码冻结及输入一致性均通过；没有运行无关的全仓测试。

复跑使用 [harness 说明](../../../../../evals/benchmark/query_intent_compare_README.md)的 `--jev-only` 模式；需要私有模型认证和保留的隔离素材，配置正文及 raw JSONL 留在本机私有目录，不提交 Git。正式原始目录为 `/private/tmp/seekino-query-intent-jev-only-20261113/controlled`。本轮启动的 MySQL／ES 已按自有 PID 停止，数据和原始运行材料保留，见 [cleanup.json](cleanup.json)。

这是 9 turn／组、单一短片、串行的小试点，provider 缓存自然变暖，未重复到足以建立显著性。整季、大索引、长历史、附件／回放、Query 子 Agent、并发、浏览器播放与实际费用尚未验证。此次没有变更或重启 Cloud，不能用本地零改写结果宣称 Cloud 已关闭改写器；C19 全场景亦未关闭。
