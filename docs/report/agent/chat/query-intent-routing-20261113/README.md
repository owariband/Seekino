# Chat 前置意图判断三路径对照：2026-11-13

Cloud 已恢复为 **JEV 分类 → 按需 DeepSeek Flash 改写 → Supervisor**。本地同条件 27 个完整 turn 均正常完成。Supervisor 直接判断比 JEV＋改写器的平均首段正文时间减少 13.2%、平均完成时间减少 7.8%，但总 token 增加 19.4%；本批不能支持“去掉前置层就整体更优”。DeepSeek 合并决策组的平均耗时与总 token 最少，但出现了错误来源陈述，平均性能不能代替质量验收。

这是一段真实 12.012 秒 MyGO 素材上的有界实验。A／B 的 9 次标签均正确，且 6/9 对追加后的模型输入完全相同，因此几例回答或工具差异不能直接归因于分类器。当前结论是保留三路证据、维持已授权的 Cloud JEV 配置，暂不据此把 Cloud 改为直接判断。

关联：[TRD](../../../../design/03-product-reassessment/query-intent-routing-comparison-trd.md) · [PR #145](https://github.com/owariband/Seekino/pull/145) · [管理工作台](../../../../dev/workbench.html) `CHAT-INTENT-01` · [C19 历史问题](../../../../dev/problems.md)。

## Cloud 为什么实际调用 DeepSeek

Cloud `/data/config/cloud/models.yaml` 同时保留 JEV 参数及 `models.query_hook=deepseek-flash/high/256`。启动装配把这个可选 binding 作为 `decision_model` 注入，QueryHook 据此选择合并决策图；只要它存在，JEV 分类与独立改写图就不会执行。不是 Supervisor 运行中自主选用了 DeepSeek，也不是保留 JEV endpoint 就等于正在使用 JEV。

本次在确认没有处理中的 Chat 后，仅删除 `models.query_hook`，保留所有 JEV 与改写器设置。删除前后 YAML 的语义比较确认没有其他配置变动。原文件保存在 Cloud 同目录的 `models.yaml.before-jev-20261113T100259Z.bak`，权限为 600；原 SHA 为 `5f85d0fdb687bd59834c485454c9a5fba5000f520f5f21fcf810a808659c6af4`，新 SHA 为 `fef4055318dbded9dce779716e71167d5f73957294f9877fc26278927ad4a9da`。

API 容器 `seekino-local-api-1` 已重启并通过健康检查。通过其真实 HTTP API 新建普通游客测试会话，发送无私密内容的反馈请求，整轮 `COMPLETED`、4.690 秒。该消息的实际日志为：`query_hook_route status=completed action=respond duration_ms=1533.3`。这证明运行实例调用了 JEV，不只是磁盘配置改对。收尾又通过公开 HTTPS `/healthz` 核对为 HTTP 200、`status=ok`、parseConsumer=running，具体时间保存在同一收据中。[配置与新建消息收据](cloud-config-switch.json)不含配置正文、会话凭据或旧聊天。

当前分类模型为 `typesafe/jev-1.14`，实际响应版本 `typesafe/jev-1.14-20261014`；检索改写器仍是既有 `deepseek-flash/high/256`。本次没有发布 Cloud 镜像；其镜像仍为 `seekino-api:main-a0fb93d9`。动态 85% compact 已经 [#144](https://github.com/owariband/Seekino/pull/144) 集成到 `dev`，本次三组均使用该策略，不能把 Cloud 配置切换写成 Cloud 已部署新 compact 代码。

## 实验条件和指标

源码为 `dev` 基础上的实验提交 `7aafbc34`，生产行为文件无本轮修改。A 复用原 Cloud 前置配置，但三组全部在本机相同 `dev` 实现中运行，避免混用 Cloud A 与本机 B／C。版本、17 个关键源码哈希、配置哈希、样本与顺序见 [protocol.json](protocol.json)。

| 路径 | 前置层 | 后续共同条件 |
| --- | --- | --- |
| A `deepseek_decider` | DeepSeek Flash／high／256，一次输出 action 与检索候选 | 相同当前 Supervisor prompt、历史、工具、素材、Flash／high／8192 |
| B `jev_rewriter` | JEV 1.14 分类；retrieve／mixed 再用 Flash／high／256 改写 | 同 A |
| C `supervisor_direct` | 无 QueryHook；Supervisor 接原始消息 | 同 A |

实际 runtime 三组均发现 context size **1,048,576**，85% 触发值 **891,289**，尾部 keep **24,000**，输出／安全预留 **8,192／4,096**。本批最大单次 DeepSeek 输入仅 15,293 token，未观察到 compact；所以这里比较的是意图策略，不能将结果归于本轮发生了压缩收益。

测试数据库为 `seekino_intent_20261113_test`，Chunk／Event 使用独立 `*_test` 索引。只复制一个已发布公开 MyGO 片段及必要解析事实：1 个 Chunk、6 个 Event；未复制旧聊天、个人模型或凭据。复制后只将发布收据的索引名称重绑定到实际目标，内容、版本与编码合同保持不变。[fixture 收据](fixture-receipt.json)记录了复制范围。embedding 使用原片段的 `doubao-embedding-vision-251215`／1024 合同，避免错用当前 Qwen 索引。两条真实检索入口都通过正式回源校验。

六类样本为：反馈不检索、只解释已有句子、停止检索并压缩一句、先回应反馈再检索、含指代的新检索、保留原话及否定约束的检索。前三例复用已提交意图用例；后三例在相同意图结构下改为可核实的台阶夜谈。每类三组各一次，再复测反馈、混合、原话三类，共 27 turn，每组 9 个。首轮轮换组顺序，复测反转 case 顺序。每个 turn 都是新会话、真实服务／模型／检索，运行至自然终态，非工具替身或一次模型续推。[冻结样本](../../../../../evals/benchmark/intent_routing/three_path_20261113_cases.json)中的 expected 不传入模型。

**FTT** 从调用 `start_message(stream=True)` 前计时，至首个非空用户可见正文事件；reasoning、状态、空工具快照均不计。**TT** 从同一起点至自然终态及提交完成。这是服务层交付测量，未含浏览器网络与 DOM 渲染，也不是 provider 原生 TTFT。本批首次正文均为 `answer.snapshot`，没有首次 `provider_delta`，因此更准确地说是“首段可见正文时间”，不能声称已测到 token 级首字流。

服务按 YAML 所选模型逐轮构建 runtime，采样覆盖实际实例；只包装启动时的默认实例会漏计。本轮记录完整回答、实际 hook 输出、工具结果与阶段用量。三组 Supervisor prompt、工具 schema、历史正文的 9 个三元组逐一一致；A／B 追加后的模型消息有 6 对一致、3 对候选词不同。见 [input-audit.json](input-audit.json)。

## 时间、用量与工具对照

下表每组均为 9 个完整 turn，失败和无正文均为 0。输入／输出／总 token 覆盖分类、改写、Supervisor 和 embedding 的全部 provider 用量；reasoning 是 output 子集，不能再相加。未报告 p95／p99 或显著性。

| 指标 | A DeepSeek 前置决策 | B JEV＋改写器 | C Supervisor 直接判断 |
| --- | ---: | ---: | ---: |
| 完成 | 9/9 | 9/9 | 9/9 |
| FTT 平均／中位（秒） | 6.099 / 4.994 | 8.886 / 7.416 | 7.713 / 6.872 |
| TT 平均／中位（秒） | 8.319 / 9.560 | 10.404 / 11.488 | 9.588 / 8.102 |
| TT 合计（秒） | 74.874 | 93.635 | 86.289 |
| 每 turn 前置平均（秒） | 1.452 | 2.701 | 0，无前置调用 |
| input token | 230,188 | 251,037 | 299,718 |
| output token | 8,144 | 9,602 | 11,508 |
| total token | 238,332 | 260,639 | 311,226 |
| 已报告 reasoning token | 3,427 | 4,357 | 5,888 |
| Supervisor provider 请求数 | 24 | 27 | 31 |
| 所有工具调用数 | 15 | 17 | 20 |
| 检索工具调用数 | 10 | 8 | 9 |

C 相对 B：FTT 平均减少 **13.2%**、TT 平均减少 **7.8%**，总 token 增加 **19.4%**。C 相对 A：TT 平均增加 **15.2%**、总 token 增加 **30.6%**。但 C 的 TT 中位数低于 A，说明“平均最短”不等于每种 query 都快。A／B／C 的首轮六类平均 TT 为 7.821／10.406／9.672 秒，复测三类为 9.316／10.400／9.419 秒；样本构成与随机波动都需考虑。

| 阶段 | A：调用数／总 token | B：调用数／总 token | C：调用数／总 token |
| --- | ---: | ---: | ---: |
| 合并决策 | 9 / 5,864 | 0 | 0 |
| JEV 分类 | 0 | 9 / 6,647 | 0 |
| 独立改写 | 0 | 5 / 2,778 | 0 |
| Supervisor | 24 / 232,196 | 27 / 250,969 | 31 / 310,952 |
| embedding | 10 / 272 | 8 / 245 | 9 / 274 |

JEV 分类平均 1.680 秒，中位 1.370 秒；其中一次总阶段耗时 4.021 秒。改写只在 5/9 turn 执行，平均 1.832 秒。`router_timeout_seconds=3` 使用 httpx 网络阶段 timeout，不能写成整个 route 总耗时必定不超过 3 秒；本轮没有证据显示它发生重试。A 的合并决策平均 1.450 秒。A／B 的 action 均为 9/9 正确，B 改写 5/5 成功，未发生失败开放，因此性能差不能用“超时跳过工作”解释。

三组总用量均完整，正式共 132 次 provider 调用、810,197 token。DeepSeek 已报告的未缓存 input 分别为 **33,692／28,807／32,948**；JEV 和 embedding 没有相同 cache 明细。虽然 C 总 token 更多，不能据此认定费用同比增加，也不能把较少总 token 写成账单节省。本轮未取完整供应商账单。完整分项和逐 turn 指标见 [metrics.json](metrics.json)。

## 输出内容与交付问题

逐字回答全部保存在 [outputs.md](outputs.md)，逐条人工审阅及时间／卡片依据在 [reviews.json](reviews.json)。以下评分只反映固定样本，非独立盲评或总体正确率。

| 检查 | A | B | C |
| --- | ---: | ---: | ---: |
| 主要意图行为：非检索不新增检索、检索实际取证、mixed 两项均答 | 9/9 | 9/9 | 9/9 |
| mixed 在检索前已公开实质回应 | 2/2 | 1/2 | 1/2 |
| 只压缩成一句话 | 0/1，给了两个版本 | 1/1 | 1/1 |
| 严格人工审阅：pass / partial / fail | 6 / 2 / 1 | 8 / 1 / 0 | 8 / 1 / 0 |

具体差异：

- **反馈场景。** 三组都能回应“你说话怎么这么无趣”，没有重新检索。A 一次把历史中未核验的“第13集定位”再次说成确实查到；复测又把“高高在上”错误定位到本次台阶夜谈开头。B／C 没有该错误。这个片段的六个 Event 不包含该台词或冲突。A／B 这些 turn 的后续模型输入相同，因此不能据此声称 JEV 分类更准确；它暴露的是后续生成与证据边界问题。
- **停止检索并压缩一句。** B／C 都只给一句；A 虽未检索，却给两种版本和额外说明。动作标签正确不能保证格式遵循。
- **混合任务。** 首轮 A 在 5.851 秒先回应吐槽，再于 7.211 秒开始检索；B／C 都先检索，直到 17.868／13.734 秒才出现正文。复测三组都做到先回应，说明顺序不稳定，不能按最终正文中“先回应”几个字判通过。
- **视频卡。** 首轮 B／C 均将同一 Chunk 分两次 `submit_video_item`，模型声称给了两段；现有合同按 Chunk ID 原地更新，最终只剩后一次的 **3084–12012ms** 卡片，开场原话没有留在最终卡片里。源码 [submit_video_item](../../../../../agent/chat/supervisor.py) 明确为更新语义。这是现有工具选择／交付合同的问题，本轮未改实现。复测三组都一次提交完整片段，卡片与说明一致。
- **指代与原话。** 三组都解析了“第一种”为台阶夜谈；所有原话用例最终卡片为 **0–2300ms**，与已发布原句一致。指代用例覆盖了燈 3100–8900ms 的回应，未把“不解释检索过程”误当成停止检索。

前置层只占总 token 的约 2.5%（A）／3.6%（B）；主要用量来自后续重复携带工具与证据的 Supervisor 请求。C 省掉前置，但本轮需要 31 次 Supervisor 请求，B 为 27，A 为 24，另有更多证据读取。这个现象解释了本批“少一个前置调用却不一定更快”，但其中生成随机性与缓存差异未被消除，不能将全部变化归于路由。

## 验证、排除批次与复跑

在正式比较前，保留了基础设施探针和 16-turn 环境诊断。探针发现默认 runtime 监控会漏计实际会话实例，并补齐新测试库 checkpoint 表。环境诊断中 5 个 turn 因继承 generation=2 与原素材 generation=1 不符而被一致性校验拒绝，连续三个失败后停止；随后只修正隔离启动环境，预检 Event／Chunk 两条入口，再全量重跑原先同一份 27 turn。**不是只挑失败例重跑，也未改变样本或 Prompt。**这些诊断数字不混入正式比较，原文件全部保留。

相关代码：[harness](../../../../../evals/benchmark/query_intent_compare.py) · [统计脚本](../../../../../evals/benchmark/query_intent_compare_summary.py)。已有 profile 参数按 `protocol.json` 配置为三份本机私有 YAML；凭据仍从现有私有环境注入，不复制到命令、报告或版本库。

```bash
python -m evals.benchmark.query_intent_compare \
  --cases evals/benchmark/intent_routing/three_path_20261113_cases.json \
  --configs-dir /private/tmp/seekino-query-intent-private-20261113/configs \
  --output /private/tmp/seekino-query-intent-private-20261113/controlled --live

python -m evals.benchmark.query_intent_compare_summary \
  /private/tmp/seekino-query-intent-private-20261113/controlled \
  --out /private/tmp/seekino-query-intent-private-20261113/controlled-summary.json
```

所需环境为 `SEEKINO_ENVIRONMENT=test`、新测试库、两个测试索引、`SEEKINO_ELASTICSEARCH_INDEX_GENERATION=1`、local auth，以及原片段的 embedding 模型／维度。服务构建不启动 parse consumer，并跳过与本次无关的删除重整。harness 默认 `--plan` 不发请求；`--live` 才运行。每轮 300 秒观察上限，本批无超时。原始 provider 用量、hook、工具与事件日志保存在上述私有本地目录。

已完成验证：脚本编译；离线 27-turn 排程；两条真实检索与回源；实际 runtime 参数及输入一致性；确定性统计与主 Agent 独立复算；27 个自然终态；独立 SQL 对照 27/27 正文与 `COMPLETED` 一致，测试库无 `PROCESSING`，见 [persistence-audit.json](persistence-audit.json)。测试库的其他 assistant 行包括注入的历史和环境诊断，不能当作额外正式生成数。

本任务启动的 MySQL、ES 和 embedding 已按自有 PID 停止，原始数据、测试库和全部结果保留，见 [cleanup.json](cleanup.json)。Cloud API 继续使用 JEV。没有修改生产算法、调低思考强度、重建原索引或迁移源数据库。

## 结论和验证边界

**已完成** Cloud 配置纠正和本轮三路完整比较。**未证明**任一路径在全部业务上最优，也不支持仅凭省去前置层就推广 Supervisor 直接判断。A／B 都正确分类，B 的固定串行开销更高；C 对简单一句话任务更快，在本批平均上却增加后续请求和总 token。B／C 的混合交付仍有一次顺序和卡片问题，A 有一次明确错误归属；选择方案必须连同这些失败看。

后续优先核对同一 Chunk 多次提交的提示／返回语义，以及重复检索、重复读取相同 cursor 的收敛规则；它们属于 Supervisor 工具交付职责，不能靠更换 Router 自动解决。本轮只记录问题，不扩大为生产改造。若要评估降低 rewriter effort、不同 compact 时机或删除前置层，需要另外固定其余条件再测。

未覆盖：整季或大索引、长对话、接近 85% 窗口、附件／回放 bypass、实际 Query 子 Agent 被选用的回合、浏览器渲染／播放验真、并发和完整账单。语义质量依据已发布解析结果与卡片合同，未对原片另做逐帧标注。缓存为 provider 自然状态，探针会预热，记录 cache 并轮换顺序不能代替冷缓存实验；样本每类只有一至两次，生成差异保留为观察。
