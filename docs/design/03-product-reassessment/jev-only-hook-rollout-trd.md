# 1. 问题与现状

需求：将已测的 JEV-only hook 作为默认路径，完整记录实验用例、用量、耗时和质量，并更新 Cloud 与 CPU server。日期：2026-11-13（UTC+8）；工作分支：`fix/jev-only-hook-default`；目标：`dev`，运行文件另从最新 `main` 建发布分支；[PR #155](https://github.com/owariband/Seekino/pull/155)。

前两轮实验分别测了 JEV＋改写器和 JEV-only。旧表没有代表纯 JEV，后续又只突出完成数与耗时，缺少直接可读的 query 和质量判据，无法据此判断实际收益。新一轮 27 turn 的模型、输入和用量已经冻结；质量是随后由 Codex 主 Agent 逐条补评，不能写成预先冻结的盲测。现有 `QueryHookConfig.rewriter_enabled`、根配置、本地开发和 Cloud 示例仍开启可选改写器，后续部署可能重新带回不需要的串行调用。

本次复用既有 JEV 路由和 Supervisor，不改分类模板、检索算法或视频卡片合同。文档记录全部已知指标、六条不同 query 及相关历史、重复规则、逐条输出和扣分依据；默认开启的 hook 采用 JEV-only，仍保留显式关闭 hook 和显式启用其他既有路径的能力。两台部署的模型、配置路径、进程和版本先只读核实，不把历史部署说明当当前事实。

上游：JEV-only 同条件实验 [PR #151](https://github.com/owariband/Seekino/pull/151)，证据见[原报告](../../report/agent/chat/query-intent-jev-only-20261113/README.md)；动态模型容量及 85% compact [PR #144](https://github.com/owariband/Seekino/pull/144) 已在 `dev`，进入发布时须核对 `main` 是否具备所需运行代码。

# 2. 影响因素、假设与评价指标

默认值与显式 YAML 都决定后续部署；可选 `models.query_hook` 会选择 DeepSeek 合并决策并绕开 JEV，单独关闭改写开关不足以切换这条路径。线上配置不能仅检查磁盘值，还要检查实际调用。Supervisor 固定为这次选定的 DeepSeek Flash/high/8192；Parse、转写、embedding、索引与媒体数据保持原合同。

| 指标或护栏 | 现有证据／基线 | 本次验收 |
| --- | --- | --- |
| 默认 hook 装配 | rewriter 默认及示例为 true | hook 启用时默认 JEV-only；关闭 hook 的旧配置仍可加载 |
| 实验记录完整性 | 新表共 27 turn；6种 query，3种重复 | 保留 FTT、TT、input/output/total、请求数、完成数、失败与质量口径，query和历史可直接查看 |
| 质量 | 事后补评 A/B/C 完整通过为7/9、7/9、4/9；实际任务选择9/9、9/9、8/9 | 保留部分通过与失败明细，C忽略一句过程说明时5/9的敏感性边界，不扩大成线上总体正确率 |
| 线上路径 | 当前待核实 | 备份后无合并决策绑定，rewriter=false，真实 JEV 路由成功，无独立改写请求 |
| Supervisor及容量 | 对照组为Flash/high/8192、动态85% | 实际生效模型、effort、预算和容量策略可核对；不以写入配置代替生效 |
| 运行与数据护栏 | 部署版本、处理中请求、健康与索引合同待核实 | 只更新所需服务；无迁移、删除、重建索引；保留回退备份，已有处理中工作不被随意终止 |

# 3. 改造方案与建模

默认链路为：原始消息与近期历史 → JEV `respond/retrieve/mixed` → 原文加既有固定处理说明 → 同一 Supervisor。独立改写器不创建、不调用；保留其显式 opt-in 能力，测试继续覆盖该分支。类默认关闭 rewrite，仓库默认示例显式 false；删除默认示例中未使用的改写绑定，并更新对应说明。是否需要更新依赖默认开启改写的测试，以实际读取和回归结果判断。

模型配置和创建仍分别由 Gateway、Server 装配承担，不新增路由算法或另一套模型配置入口。线上改动使用当前生效 YAML，关闭 rewrite、移除合并决策绑定，设置／核对选定 Supervisor 和动态 compact；安全备份原文件及哈希，凭据不进入 Git。若当前镜像缺少 `auto_compact_ratio`，发布只选取上游必要运行文件，不能把整个 `dev` 合入 `main`。

内部 devlog、TRD、实验与验收材料留在 `dev`。发布分支从最新 `origin/main` 创建，仅选择正式用户安装、运行、升级与验证需要的文件，沿用 `main` 的 ignore 规则。用户本次已授权合入 `dev/main` 和服务更新；发布合并仍按既有 PR 边界执行。

# 4. 对照实验与结果分析

不重新运行已冻结的 27 turn，不从历史表抽取有利列拼接新表。将已有新旧实验独立记录，原始字符串、数据和哈希保持。按 humanizer-zh 编辑说明性正文，保留所有独立指标、限定、失败和归因；把事后质量评阅与原始采样区分。

已把六条query、完整历史、27条逐字回答与扣分原因写入[补充评阅](../../report/agent/chat/query-intent-jev-only-20261113/query-quality-review.md)，全量指标补入[原devlog](../../dev/devlog.md#devlog-chat-intent-jev-only-20261113)。原始usage与结果保持，未重跑被测模型。默认值、三份示例和注释已修正，显式opt-in与合并决策能力保留；没有修改Server优先级或Parse路由。

QueryHook／Gateway／Bootstrap／Parse路由相关回归最终285项通过，1条既有Starlette弃用警告。首次Bootstrap有24项失败，因为旧断言依赖启动时创建改写模型；改为断言JEV-only不创建该模型后，按同一范围重跑通过。容量／工厂／配置发现四文件为262通过、3项失败，失败均为历史默认模型fixture的断言；已在修改前的`96d43e20`工作树复现同名3项失败，未顺带修改业务模型。命令和结果见[回归收据](../../report/agent/chat/query-intent-jev-only-20261113/default-rollout-checks.json)。发布选取后继续验证必要运行文件和配置加载。

# 5. 试点验证与方案取舍

两台部署先确认实际服务拓扑、挂载、镜像与活动请求，备份后逐个切换，必要时只重启拥有该配置的 API。用新建、无私密内容的反馈与检索请求覆盖真实 JEV 和 Supervisor，记录路由、独立改写次数、终态和用量；线上探针与27turn对照分开统计，不能拿少量探针宣布线上性能排名。

若 JEV、配置解析或健康失败，恢复该实例的备份并核对恢复结果；不对不相关服务进行重启或数据修复。SSH／网络不可用时，继续完成仓库与发布材料，记录具体连接缺口，服务器变更不能写成已完成。

# 6. 结果验收与闭环

当前已补齐实验记录并通过默认JEV-only的285项相关回归；dev/main发布及线上切换尚未完成。现有SSH固定节点不可用，独立TCP出口和GPU跳板均未取得CPU SSH banner；GPU侧到CPU的22/443端口可建立TCP，Cloud的TLS握手仍超时。尚未写入、重启或修改远端配置，连接问题不能记为部署完成。

交付分别记录文档、默认路径、dev/main、Cloud/CPU生效和真实探针的证据。实际部署收据、回退备份与剩余限制在执行后补入本TRD与devlog；唯一当前状态由工作台`CHAT-JEV-DEFAULT-01`维护。

大索引、长历史、并发、长期延迟分布与完整费用仍未由原始小样本实验覆盖。视频卡片范围和既有内容错误是原实验保留的问题，不在本次 hook 默认值与部署范围内修复。
