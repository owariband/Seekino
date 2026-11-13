# JEV-only 默认配置生产上线报告：2026-11-14

本报告记录 JEV-only 默认值从代码合并到 Cloud API 实际上线的过程，以及上线后的真实健康、对话和检索 hook 探针。收据中的时间均为 UTC；部署完成对应本地 2026-11-14 00:25:03（UTC+8），成功的探针结束于本地 00:27:55。27 turn 基准仍是 2026-11-13 的实验，本次没有重跑。

关联：[上线 TRD](../../../../design/03-product-reassessment/jev-only-hook-rollout-trd.md) · [dev PR #155](https://github.com/owariband/Seekino/pull/155) · [main PR #157](https://github.com/owariband/Seekino/pull/157) · [原始脱敏收据](production-rollout-20261114.json) · [兼容补丁](production-embedding-compatibility.patch) · [实验指标与质量评阅](README.md)。

## 发布与拓扑

dev PR #155 以 `9a18c6fb` 合入，main PR #157 以 `8a86883f` 合入。main 此次发布仅改动 19 个正式使用与验证所需文件，devlog、TRD 和评测材料没有进入发布分支；GitHub 的 `release-files` 检查通过，本地发布树检查覆盖 808 个路径和 8 个资产，也通过。

Cloud API 实际运行在 CPU 主机 `seekino-server` 上，容器为 `seekino-local-api-1`。之前把它描述成两台独立部署的服务并不准确：这里只有一个 API 实例。

部署镜像为 `seekino-api:jev-only-8a86883f-compat`。它在上一版 `seekino-api:main-a0fb93d9` 运行时上覆盖 5 个文件，保留原有依赖和其他服务代码，构建耗时 3.084 秒，没有网络或依赖变更，5 个覆盖文件逐一核对哈希，当前配置可以正常加载。四个覆盖文件与 main 一致；`gateway/config.py` 额外保留了该部署原有的内部 embedding URL 校验和 `document_base_urls`，这是兼容差异，不能把镜像说成与 main 逐字节相同。

线上 Compose 的 embedding 地址是内部 HTTP 端点 `http://embedding:18845`，现有配置还带有 `document_base_urls`；main 的对应校验不接受该内部地址，schema 也不识别这个字段。完整 main 候选在停止 API 之前因此未通过配置校验，从未激活。实际镜像沿用上一运行时已有的专用地址校验和字段支持，没有在本次新增 embedding 路由能力。

后续复核发现，00:37:52（UTC+8）另一项 Event 内存修复接续部署了 `seekino-api:hotfix-event-memory-8f0f14d3`。最新镜像中的上述 5 个文件哈希与本次 overlay 一致，active、seed、runtime 三份配置的哈希也都一致；API healthy，启动和后续容量日志仍为 1048576／891289／24000。因此本次 00:25 的切换与下文探针对应原兼容镜像，最新运行状态另由收据中的 `latest_runtime_after_parallel_event_memory_hotfix` 记录。本轮保留该后续部署，没有重放模型来评测它的内存修复效果。

## 配置与切换

活动配置 `/data/config/cloud/models.yaml` 已更新，种子配置 `/etc/seekino/models.yaml` 同步更新；`/data/config/cloud/agent-runtime.yaml` 的字节哈希未变。活动文件哈希为 `547729bafeb922a0e1f0c25916996513f40b1f507a0e4373ce51943ce48067d2`，种子文件为 `91728e8fdbc6903089187d35a3ddf81a75e0ca57055e0b0ff5b9cfed40d1429f`，runtime 保持 `98c16c9e11e844b04f5da524594029dabc2cba41a7dbcd83aa0c37b7c8217990`。

active 与 seed 都满足：hook 开启、独立 rewriter 关闭，配置里没有 `models.query_hook` 或 `models.query_rewriter` 绑定。JEV 配置名为 `typesafe/jev-1.14`，实际调用记录为 `typesafe/jev-1.14-20261014`。Supervisor 为 DeepSeek Flash、reasoning high、输出预算 8192、`auto_compact_ratio: 0.85`。上线日志中的实际容量为 context 1048576、触发 891289、keep 24000；keep 是最近消息尾部的保留目标，不是整段压缩后请求的大小。没有实现 50% 异步 compact。

切换前已准备备份，实际文件为：

- active：`/data/config/cloud/models.yaml.before-jev-only-20261113T162445Z`
- seed：`/etc/seekino/models.yaml.before-jev-only-20261113T162445Z`。收据计划里的 `/seed/models.yaml` 对应宿主 `/etc/seekino`，以实际备份路径为准。
- runtime：`/data/config/cloud/agent-runtime.yaml.before-jev-only-20261113T162445Z`

备份只在远端保留，凭据和配置正文没有导出或提交。收据中的 `config_backup_plan.activated=false` 是准备阶段的快照；运行核查后的 `config_plan_activated=true` 才是最终状态。

API 的停止、重建和 readiness 合计 17.891 秒。这是部署操作耗时，不是精确测量的用户停机时间。其他 5 个容器的 ID 和启动时间未变，没有数据库迁移、清理或索引重建。

重启前存在 2 条旧的 PROCESSING 记录，都早于本次启动；本次新增处理中为 0，活动或到期 Parse 任务为 0，运行中的 Chunk 为 0，Refine 区间为 0。未来的 Parse PENDING 2037 没有租约。旧记录和该 PENDING 均未改动，也没有做修复。

## 上线后的真实探针

公网 HTTPS `/healthz` 返回 200，Parse Consumer 为 running。使用新建游客身份完成了经认证的会话创建和 SSE 请求，最终进入 `answer.completed`，验证了公开入口、游客鉴权和 SSE 终态；没有测试注册账号登录。

HTTP 对话使用的非私密 query 是：

> 请不要检索视频，只用一句话确认：配置检查完成。

回答为“配置检查完成。”客户端观测 FTT 3.616601065 秒、TT 4.749729216 秒；服务端总耗时 3.273 秒，其中 QUEUED 3258 毫秒、RESPONDING 15 毫秒。FTT 按公网 HTTP 客户端首个非空可见正文计算，排除 reasoning、状态和工具事件。这次口径包含公网传输，和 27 turn 实验的服务层 FTT 不同，不能拿单次简单探针与基准平均值比较或排名。

本次对话共 1 次 JEV 调用和 1 次 Supervisor 调用，独立改写和合并决策调用均为 0。合计 input 16775、output 43、total 16818 token；Supervisor 实际为 input 16139、output 5、total 16144，缓存 16000，供应商上报 reasoning 0。high 档位来自生效配置核对，不能从这次简短回答的 reasoning 用量反推档位。调用记录里那串注册工具名只表示本轮可用的工具集合，不代表实际执行了 13 个工具。

第一次 HTTP 尝试其实已经完成回答，但采集 SQL 因 `usage` 是未加引号的保留字而失败。随后用同一条非私密 query 重跑了一次并采用修正后的采集；第一次的客户端 FTT/TT 没有保留。这是采集脚本问题，不是服务或模型失败，也不并入实验结果。

最终回源核对确认两次尝试均为 `COMPLETED`、JEV 均为 `respond`。第一次正文为“配置检查完成，未检索任何视频。”，第二次为“配置检查完成。”；没有把两次措辞当成完全相同的输出。两次的服务端完成时间分别为 4.310 秒与 3.273 秒，只有第二次保留了完整客户端计时和调用收据。

这两次路由日志在后续镜像替换前已读取，保存为收据的 `pre_replacement_probe_log_capture`。之后按最新容器启动时间再次读取日志时，两条旧探针已不在该容器的时间窗口内；SQL 中的两条终态和用量仍可核对，不能把新容器的空日志窗口当作探针没有执行。

第一次的用量随后从该条自建消息补齐：JEV 与 Supervisor 各 1 次，input 16775、output 48、total 16823，独立改写与合并决策均为 0。两次 HTTP 加下述一次检索 hook 共 5 次 provider 调用，input 34181、output 129、total 34310 token，均与原 27 turn 分开统计。

## 检索 hook 探针

另一条探针的 query 是：

> 请帮我找一下视频里讲反向传播的片段。

它只验证真实 JEV hook：路由为 `retrieve`，耗时约 2.156 秒，整段 hook 2.169980187 秒，独立改写以 `disabled` 跳过、耗时 0。仅有 1 次 JEV 调用，input 631、output 38、total 669 token。原始 query 被保留，没有生成候选检索改写。

这条探针在 `retrieve` 之后没有继续执行完整生产检索，也没有再调用 Supervisor 处理检索结果。因此它不能证明完整片库检索、召回或最终回答通过，只能证明 JEV hook 在真实配置下完成了路由。

## 回归与发布范围

dev 上 QueryHook／Gateway／Bootstrap／Parse 路由的定向回归为 285 项通过，1 条既有 Starlette 弃用警告。main 发布回归为 543 项通过、3 项既有的默认模型 fixture 失败；容量组为 262 项通过、同样 3 项失败。容量组和 dev 定向集合与 main 回归范围有重叠，不能把这些数字相加成“总测试数”，3 项失败也没有在本次修复。

本次没有新的 27 turn 运行，也没有新增基准模型调用。已有六条 query、27 条输出、质量评分和逐项指标仍按原实验记录阅读。

## 限制与未验证范围

- 一次健康检查、一次非检索对话和一次检索 hook 探针只覆盖上线接线，不是性能或业务质量基准。
- 完整生产库检索、近满窗上下文质量、并发和长期延迟都没有重新测量，不能据此给出性能排名。
- 部署镜像是保留既有运行时与配置适配的 5 文件 overlay，不是完整 main 镜像；该兼容差异需随镜像保留。
- 旧的 PROCESSING 记录和未来 PENDING Parse 未处理；备份只在远端，恢复时需要按上述路径核对。
- `CHAT-COMPACT-01` 仍为 verifying。Cloud 已加载 85% 动态策略，但完整业务交付、近满窗质量、并发与长期延迟尚未验收。

本次完成的是默认值、dev/main 发布和 Cloud/CPU 实例实际生效这一段闭环；C19 全场景及其他任务不随本次上线关闭。
