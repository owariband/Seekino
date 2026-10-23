# 第四集多参考声纹复测：关闭 Prefix/Compact

日期：2026-10-17；本文时间为 Asia/Shanghai（UTC+8），JSON 采集时间为 UTC。

**最终 COMPLETE：Understanding 24/24、Refine 22/22、当前版本索引 24/24。目标「祥ちゃんも悪くない／也不是小祥的错」由首次 Understanding 的高松灯，经真实声音比对改为长崎爽世；数据库正文与 Elasticsearch 日中台词字段均已核实。**

本轮验证了这个已知目标及完整执行链路，不是全片人物准确率统计。

## 任务、配置与重启

- 隔离系列：`bdbf28ae-5404-58e4-8c98-699433499ffc`。
- 视频：`0019c16c-a7b7-5f6c-8275-5019869c1207`，第四集。
- 新任务：`dd55671d-a84b-4628-bedb-4007a4f4aa9c`，解析版本 **5**；通过现有 reparse API 提交一次，未复用 v4 的 ready 或参考。
- 执行时间：15:08:57.280–15:47:40.798，约 **38 分 43.5 秒**。
- 重启前无活动解析租约或 PROCESSING Chat。旧 API 27365、Web 2340 均在 SIGTERM 后退出；新 API 36765、Web 36791 健康。MySQL/Elasticsearch 未重启，无表初始化或迁移。
- 相对 v4 请求只改三处：`compact_enabled=false`、`prefix_context=null`、`models.prefix_context=null`。模型自身的 auto_compact 上下文策略保留。
- Voiceprint 工具上限 96、准备 600 秒；CAM++ CPU 单次 30 秒；并发 10、阶段执行 1800 秒、max_model_calls=100、max_retry_count=0 均保持原值。
- 原视频实物 SHA-256：`50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`，与登记身份相符。关键源码和模型配置的 SHA 在运行前后保持一致；开测 HEAD 为 e906b60，使用工作区中已修复且未提交的代码，不把 HEAD 当作全部实际源码。
- 没有注入目标人物答案、人工登记离线样本或中途重启。所有 25 条由实际 VoiceprintAgent 选择。

## 声纹准备与门禁

| 采样时间 | 事实 |
|---|---|
| 15:13:19.484 | 恰好 12/24 Understanding 已提交，开始准备 |
| 15:15:44.543 | 25 条已登记但 ready=false，22 个 Interval 仍 WAITING |
| 15:15:45.062 | 首次观察 ready=true，5 人各 5 条 |
| 15:15:46.277 | 首个 Refine 领取，晚于 ready |
| 15:16:31.353 | 全部 24 个 Understanding 提交 |

来源固定为 seq0–9、13、18，不随后续 Understanding 增长。采样未观察到 ready 前领取；采样时间不等于事务提交时间。

| 人物 | 参考条数 | 不同 chunk 数 |
|---|---:|---:|
| 千早爱音 | 5 | 5 |
| 丰川祥子 | 5 | 4 |
| 椎名立希 | 5 | 4 |
| 长崎爽世 | 5 | 5 |
| 高松灯 | 5 | 5 |

25 个独立 source_ref、25 个不同存储 PCM 摘要，无区间交叉；均为同一 CAM++ revision 的有限 192 维单位向量。逐条选择依据、版本、时间和模型见 [参考审计](reference-audit.json)。终态重新核验来源后，5 人仍各有 5 条有效参考。目标47500–48800ms没有登记为参考。

本轮均达到目标，无不足人数；没有以本次真实运行替代缺额、重复、交叉、取消等负向自动化测试。

## 目标声音证据与写回

本轮 ASR 将目标单独切成 **47500–48800ms，1300ms**，但匿名标签仍与前面的爱音发言相同。首次 Understanding v1 将其错归高松灯，描述明确为画外回应。

Refine 在 15:16:27 和 15:16:34 对此区间实际调用比较两次，日志结果一致：

| 候选 | Cosine（日志六位小数） |
|---|---:|
| 长崎爽世 | 0.778768 |
| 丰川祥子 | 0.613801 |
| 高松灯 | 0.505230 |

前两名差约 **0.164967**；相似度不是身份概率。目标 PCM SHA-256：`fd6d0fec3bfa51a60165f71497c16b50bc882cef9ba7c5ca9c8237633a5347be`。实际日志摘录见 [target-comparisons.json](target-comparisons.json)。这里只保存日志中的候选，不把它当成完整 Tool 回执。

15:19:43.348，seq0 Refine 提交，正文 **v1 → v2**，speaker改为长崎爽世。新描述明确引用爽世余弦0.779、领先0.165、灯0.505，说明判断消费了声音证据。15:19:54.076 已观察到 seq0 phase3/COMPLETE、indexed_content_version=2。终态直接读取 ES 的日文原句及中文译句，speaker均为长崎爽世，24篇文档版本与 MySQL 当前正文一致，见 [ES 文档](es-documents.json)。本轮未通过 Chat 问答替代数据库和索引验收。

## 终态、用量和保留检查

- 全部 24 个 Chunk 完成且索引版本匹配，22 个 Interval 全部 COMPLETE，retry_count均为0；无失败窗口。
- Prefix lease_version=0、cursor=0、没有 Prefix 阶段模型调用。collector的 `prefix_done=true` 表示关闭时无需等待，不表示运行过该Agent。
- 父任务 lease已释放、capacity=false，子租约及模型 active 占用均为0；本任务临时媒体目录没有残留文件。服务健康，Parse Consumer running。
- 统一账本 **1126 条全部 completed**：CAM++ **975 次**（准备25、Refine950）；Refine生成54次，对应22次正式窗口提交，completed调用数不等于提交数。
- CAM++账本耗时最小12ms、中位44ms、P95 194ms、最大2991ms；包含初始化、锁等待和CPU推理，不是纯推理benchmark。整集耗时不能归给CAM++。
- 原系列 **13视频、13任务、312正文** 的版本、时间及规范正文SHA全部未变，见 [保留检查](original-preservation-check.json)。
- 无迁移、commit/push，没有自动重解析原有13集。

完整终态核验见 [final-verification.json](final-verification.json)，运行快照和账本见 [dd55671d-a84b-4628-bedb-4007a4f4aa9c/summary.json](dd55671d-a84b-4628-bedb-4007a4f4aa9c/summary.json)。

## 验收边界

爽世参考包含53.2–55.2秒同场景的「誰も悪くない」，与目标共享措辞；本轮目标为1.3秒，上一轮v4为1秒。再加上Agent选择、初始归属与Prefix配置变化，本轮不能单独量化多参考或任一配置的因果收益，也不能称独立跨场景泛化验证。没有人工试听校准全部25个首标签，1秒叫名等短样本仍需谨慎理解。

现有Codex diagnostics未持久化完整工具输入输出；`skip_reasons`也不单独落库。本轮全员达到5条，因此没有缺额理由需要验收。保留正式参考、真实比较日志、Canvas写回正文及ES读取来证明本目标的证据链，不声称完整逐步工具轨迹已恢复。没有中途崩溃或超时，因此本轮不宣称新增的在线恢复验收。

## 复现与采集

```sh
uv run --no-sync --env-file .env.local python engine/experiment/voiceprint-agent/20261017-mygo-ep04-multiref-no-prefix/control.py preflight
uv run --no-sync --env-file .env.local python engine/experiment/voiceprint-agent/20261017-mygo-ep04-multiref-no-prefix/control.py restart
uv run --no-sync --env-file .env.local python engine/experiment/voiceprint-agent/20261017-mygo-ep04-multiref-no-prefix/control.py submit
```

`submit`在本目录已有响应时拒绝重复提交；以上是本轮实际操作顺序，不应直接重复执行重启或覆盖基线。SELECT-only采集器复用上一轮watch.py，仅增加可选output-dir参数，保持旧默认路径；本轮参数task-id=dd55671d-a84b-4628-bedb-4007a4f4aa9c、max-seconds=5400、poll-seconds=0.5。`control.py verify-original`只读核对原系列；`verify.py`只读核对本轮DB导出、ES、原视频哈希、健康和本地资源，均不运行DDL。本实验目录被Git忽略，记录为本地成果。
