# 1. 问题与现状

需求：将已测的 JEV-only hook 作为默认路径，完整记录实验用例、用量、耗时和质量，并更新 Cloud 与 CPU server。日期：2026-11-13（UTC+8）；工作分支：`fix/jev-only-hook-default`；目标：`dev`，运行文件另从最新 `main` 建发布分支；[PR #155](https://github.com/owariband/Seekino/pull/155)，发布 [PR #157](https://github.com/owariband/Seekino/pull/157)。

2026-11-14 的部署验收沿用本需求，在 `docs/jev-only-rollout-evidence` 分支、[PR #158](https://github.com/owariband/Seekino/pull/158) 补回实际生效收据与 devlog；内部留痕只进入 dev。

前两轮实验分别测了 JEV＋改写器和 JEV-only。旧表没有代表纯 JEV，后续又只突出完成数与耗时，缺少直接可读的 query 和质量判据，无法据此判断实际收益。新一轮 27 turn 的模型、输入和用量已经冻结；质量是随后由 Codex 主 Agent 逐条补评，不能写成预先冻结的盲测。现有 `QueryHookConfig.rewriter_enabled`、根配置、本地开发和 Cloud 示例仍开启可选改写器，后续部署可能重新带回不需要的串行调用。

本次复用既有 JEV 路由和 Supervisor，不改分类模板、检索算法或视频卡片合同。文档记录全部已知指标、六条不同 query 及相关历史、重复规则、逐条输出和扣分依据；默认开启的 hook 采用 JEV-only，仍保留显式关闭 hook 和显式启用其他既有路径的能力。部署的模型、配置路径、进程和版本先只读核实，不把历史部署说明当当前事实。2026-11-14 上线核查确认：Cloud API 实际运行在 CPU 主机 `seekino-server` 的 `seekino-local-api-1`，是一个实例，不是两台独立部署的服务；最初计划的分别验收按这个实际拓扑执行。

上游：JEV-only 同条件实验 [PR #151](https://github.com/owariband/Seekino/pull/151)，证据见[原报告](../../report/agent/chat/query-intent-jev-only-20261113/README.md)；动态模型容量及 85% compact [PR #144](https://github.com/owariband/Seekino/pull/144) 已在 `dev`，进入发布时须核对 `main` 是否具备所需运行代码。

# 2. 影响因素、假设与评价指标

默认值与显式 YAML 都决定后续部署；可选 `models.query_hook` 会选择 DeepSeek 合并决策并绕开 JEV，单独关闭改写开关不足以切换这条路径。线上配置不能仅检查磁盘值，还要检查实际调用。Supervisor 固定为这次选定的 DeepSeek Flash/high/8192；Parse、转写、embedding、索引与媒体数据保持原合同。

| 指标或护栏 | 现有证据／基线 | 本次验收 |
| --- | --- | --- |
| 默认 hook 装配 | rewriter 默认及示例为 true | hook 启用时默认 JEV-only；关闭 hook 的旧配置仍可加载 |
| 实验记录完整性 | 新表共 27 turn；6种 query，3种重复 | 保留 FTT、TT、input/output/total、请求数、完成数、失败与质量口径，query和历史可直接查看 |
| 质量 | 事后补评 A/B/C 完整通过为7/9、7/9、4/9；实际任务选择9/9、9/9、8/9 | 保留部分通过与失败明细，C忽略一句过程说明时5/9的敏感性边界，不扩大成线上总体正确率 |
| 线上路径 | 上线前待核实 | 备份后无合并决策绑定，rewriter=false，真实 JEV 路由成功，无独立改写请求 |
| Supervisor及容量 | 对照组为Flash/high/8192、动态85% | 实际生效模型、effort、预算和容量策略可核对；不以写入配置代替生效 |
| 运行与数据护栏 | 上线前部署版本、处理中请求、健康与索引合同待核实 | 只更新所需服务；无迁移、删除、重建索引；保留回退备份，已有处理中工作不被随意终止 |

2026-11-14 的实际核查结果：active 与 seed 均 hook=true、rewriter=false，无 `models.query_hook`／`models.query_rewriter` 绑定；Supervisor 为 Flash/high/8192、`auto_compact_ratio=0.85`，日志实际为 context 1048576、trigger 891289、keep 24000。公网健康 200，真实对话中 JEV 与 Supervisor 各 1 次、独立改写 0 次；检索 hook 探针到达 `retrieve` 后未执行完整生产检索。

# 3. 改造方案与建模

默认链路为：原始消息与近期历史 → JEV `respond/retrieve/mixed` → 原文加既有固定处理说明 → 同一 Supervisor。独立改写器不创建、不调用；保留其显式 opt-in 能力，测试继续覆盖该分支。类默认关闭 rewrite，仓库默认示例显式 false；删除默认示例中未使用的改写绑定，并更新对应说明。是否需要更新依赖默认开启改写的测试，以实际读取和回归结果判断。

模型配置和创建仍分别由 Gateway、Server 装配承担，不新增路由算法或另一套模型配置入口。线上改动使用当前生效 YAML，关闭 rewrite、移除合并决策绑定，设置／核对选定 Supervisor 和动态 compact；安全备份原文件及哈希，凭据不进入 Git。若当前镜像缺少 `auto_compact_ratio`，发布只选取上游必要运行文件，不能把整个 `dev` 合入 `main`。

内部 devlog、TRD、实验与验收材料留在 `dev`。发布分支从最新 `origin/main` 创建，仅选择正式用户安装、运行、升级与验证需要的文件，沿用 `main` 的 ignore 规则。用户本次已授权合入 `dev/main` 和服务更新；发布合并仍按既有 PR 边界执行。

# 4. 对照实验与结果分析

不重新运行已冻结的 27 turn，不从历史表抽取有利列拼接新表。将已有新旧实验独立记录，原始字符串、数据和哈希保持。按 humanizer-zh 编辑说明性正文，保留所有独立指标、限定、失败和归因；把事后质量评阅与原始采样区分。

已把六条query、完整历史、27条逐字回答与扣分原因写入[补充评阅](../../report/agent/chat/query-intent-jev-only-20261113/query-quality-review.md)，全量指标补入[原devlog](../../dev/devlog.md#devlog-chat-intent-jev-only-20261113)。原始usage与结果保持，未重跑被测模型。默认值、三份示例和注释已修正，显式opt-in与合并决策能力保留；没有修改Server优先级或Parse路由。

QueryHook／Gateway／Bootstrap／Parse路由相关回归最终285项通过，1条既有Starlette弃用警告。首次Bootstrap有24项失败，因为旧断言依赖启动时创建改写模型；改为断言JEV-only不创建该模型后，按同一范围重跑通过。容量／工厂／配置发现四文件为262通过、3项失败，失败均为历史默认模型fixture的断言；已在修改前的`96d43e20`工作树复现同名3项失败，未顺带修改业务模型。命令和结果见[回归收据](../../report/agent/chat/query-intent-jev-only-20261113/default-rollout-checks.json)。发布选取后核对 main 此次 19 个发布所需文件、发布树 808 个路径／8 个资产与 GitHub `release-files`，均通过；main 回归为 543 通过、3 项同名既有基线失败。该集合与前述 285 项、262 项范围重叠，不能相加，3 项失败未在本次修复。

# 5. 试点验证与方案取舍

部署先确认实际服务拓扑、挂载、镜像与活动请求，备份后切换，必要时只重启拥有该配置的 API。用新建、无私密内容的反馈与检索请求覆盖真实 JEV 和 Supervisor，记录路由、独立改写次数、终态和用量；线上探针与27turn对照分开统计，不能拿少量探针宣布线上性能排名。

2026-11-14 的实际切换按上述边界完成。Cloud API 只更新 `seekino-local-api-1`，其他 5 个容器 ID 和启动时间未变；API stop/recreate 到 ready 为 17.891 秒，这是操作耗时，不是精确用户停机。切换前有 2 条早于本次启动的 PROCESSING，本次新增为 0，活动／到期 Parse、运行中 Chunk、Refine 区间均为 0，未来 PENDING 2037 无租约；这些记录未改动。公网 HTTPS 健康 200，Parse Consumer running；新建游客身份，经认证的会话创建与 SSE 请求到 `answer.completed`，JEV 与 Supervisor 各 1 次、独立改写 0 次。检索 hook 只验证到 `retrieve`，没有执行完整生产检索或后续 Supervisor。详见[生产上线报告](../../report/agent/chat/query-intent-jev-only-20261113/production-rollout-20261114.md)。

若 JEV、配置解析或健康失败，恢复该实例的备份并核对恢复结果；不对不相关服务进行重启或数据修复。SSH／网络不可用时，继续完成仓库与发布材料，记录具体连接缺口，服务器变更不能写成已完成。

# 6. 结果验收与闭环

2026-11-14 已完成默认值、发布和 Cloud/CPU 实例实际生效这一段闭环。dev PR #155 以 `9a18c6fb` 合入，main PR #157 以 `8a86883f` 合入；main 此次发布仅改动 19 个正式使用与验证所需文件，release-files 与本地发布树（808 路径／8 资产）通过。Cloud API 实际运行在 CPU 主机 `seekino-server` 的 `seekino-local-api-1`，使用 `seekino-api:jev-only-8a86883f-compat` 镜像；active 与 seed 均 hook=true、rewriter=false，无 query_hook／query_rewriter 绑定，Supervisor 为 Flash/high/8192、85% 动态 compact，日志为 1048576／891289／24000。公网 HTTPS 健康 200，Parse Consumer running；新建游客身份，经认证的会话创建与 SSE 请求到 `answer.completed`，JEV 与 Supervisor 各 1 次、独立改写 0 次。检索 hook 探针确认 `retrieve` 与禁用改写，但没有执行后续完整生产检索。

发布镜像是上一 `main-a0fb93d9` 运行时上的 5 文件 overlay，保留该部署原有的依赖、内部 embedding URL 校验和 `document_base_urls`；四个覆盖文件与 main 相同，`gateway/config.py` 是兼容差异，不能写成与 main 逐字相同。完整 main 候选在停止 API 前 preflight 失败，从未激活。API stop/recreate 到 ready 为 17.891 秒，是部署操作耗时，不是精确用户停机；其他 5 个容器未重启。切换前 2 条 PROCESSING 早于本次启动，未来 PENDING 2037 无租约，均未修复或改动。

00:37:52（UTC+8）之后，另一项 Event 内存修复将 API 接续更新为 `seekino-api:hotfix-event-memory-8f0f14d3`。最终只读复核确认：本次 5 个 Chat/Gateway 文件哈希、active／seed／runtime 配置哈希保持，最新 API healthy，模型容量日志仍为 1048576／891289／24000；因此本次默认配置仍在生效。原兼容镜像的探针与最新镜像的配置复核分开留痕，没有覆盖后续部署，也不据此验收内存修复效果。

此前 2026-11-13 记录的 SSH 固定节点不可用、独立 TCP 出口和 GPU 跳板未取得 CPU SSH banner、Cloud TLS 握手超时，是当时的连接问题，保留为历史。主机重启后 SSH 恢复，先通过 GPU 跳板、随后直连 CPU 完成核查与切换，不能把旧连接缺口写成现在仍然阻塞。

dev 定向回归 285 项通过；main 回归 543 通过、3 项既有默认模型 fixture 失败；容量组 262 通过、同样 3 项失败。范围重叠，不能相加。没有重跑 27 turn 或新增基准模型调用。工作台 `CHAT-JEV-DEFAULT-01` 按本次默认／发布／配置范围记录完成；`CHAT-COMPACT-01` 保持 verifying，完整业务、近满窗、并发和长期延迟仍未验证。C19 全场景不随本次上线关闭。大索引、长历史与完整费用仍未由原始小样本实验覆盖。视频卡片范围和既有内容错误是原实验保留的问题，不在本次 hook 默认值与部署范围内修复。
