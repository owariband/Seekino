# Refine 直接 JSON 正式接入与真实提交验证（2026-11-01）

本次采用既有对照中有效的无工具完整 JSON 路径，已接入 PR #18 工作树并恢复 API。真实三 Chunk 区间经正式 Consumer、租约心跳、Kimi、业务门禁及 MySQL 提交，425.011 秒完成；模型请求 1 次、门禁修正 0 次、额外 ASR 0 次。该结果支持正式接线可执行，不证明整课已完成或稳定达到历史 247.0 秒。

- 分支：`fix/video-asr-recovery`；[Draft PR #18](https://github.com/owariband/Seekino/pull/18)，目标 `dev`；HEAD `67223c16dbba2a89d811f4e8a677870b22309b90` 加未提交工作区修改。
- 主设计：[性能 TRD §3.9](../../design/03-product-reassessment/video-parse-performance-trd.md#39-11-01-refine-完整-json-提交正式接线)。
- 历史对照：[原报告](prompt-refine-ab-20261101.md)及[用户要求原样保存的数据表](evidence/prompt-refine-ab-20261101/refine-comparison-original-table.md)。原表、原报告和原 devlog 数字未改。

## 问题、实现与取舍

旧 Refine 让模型反复读取和写入 Canvas，增加模型请求、重复上下文及参数核对。原同条件对照支持去掉这些交互。本次模型一次收到完整成员内容、相同 ASR/OCR/媒体观察及背景，直接返回每个成员的完整 `persons/timeline`。没有模型侧 Canvas、临时取帧或临时声纹工具，合法 JSON 本身就是完成结果。

业务门禁位于 Refine Agent/Engine，校验 Schema、人物引用、每个成员恰好出现一次及授权时间，再计算实际变化；只有结构/成员错误进入有限修正，保留同一总截止时间。Server 沿用成员、内容版本、指纹、租约、原子提交和冲突合并。冲突只刷新 Refine。宿主保留原 voiceprint prefill/flag 与 speaker_source 规则，模型不能提交来源字段作为事实。gateway/ 本轮源码 hash 没有变化。

整片 ASR 的完整 DIALOGUE 可跨名义 Chunk/Interval，仍不得超出源视频；视觉事件维持原范围。该合同贯通 Refine 输入、本地校验、最终事务及 Prefix。当前 Understanding 的初始时间门禁和 Prompt 未采用此前变慢的候选。没有 DDL、新表、历史数据迁移或全库重解析。

## 运行环境与方法

目标素材仍是 Let's build GPT，全长 6,979,873 ms（约 116 分 20 秒），共 117 个 Chunk。本次只验收已有首三个 Chunk 的一个 Refine 区间，不重新播放或解析整课。

接手检查时原 API 及隔离依赖进程已退出；根据原进程记录恢复已有 MySQL/ES/embedding/HTTPS 透传代理，未重新初始化数据库。工作树 API 使用原隔离库恢复于 `http://127.0.0.1:19621`，PID 5207，healthz 的 Consumer 状态为 running；三个原任务全部保持暂停。

为保留旧运行证据，从 `seekino_video_asr_recovery_20261031_test` 一致性导出到新库 `seekino_refine_direct_20261101_test`。仅在副本恢复历史任务的 accepted/执行状态、重置目标区间完成回执，其余区间设为远期；不启动副本的后台全任务调度。原业务库的任务与成果表整体 fingerprint 前后相同。

在副本中执行 `VideoParseConsumer._run_interval` → `VideoParseRuntimeService.refine` → `IntervalRefine.InvokeRefine` → 真实 Kimi → 原事务提交。使用生产心跳和重试入口，没有直接把实验 JSON 填入数据库、没有模型替身。目标 task=`613f40e2-7bcb-4e38-9ae4-9ad1021afb51`，interval=`854924e7-0952-4f03-b940-dd072a67aeb6`，lease=10。

成员输入版本 2/1/1，共 47 个事件；内容、观察证据、背景与前一轮 A/B 完全相同。生产 Prompt 和内部 Schema 已增加通用成员、宿主来源及视频范围合同，因此这是接入试点，不是另一次严格同条件 A/B。模型为 `k3.1-pro`，`stream=true`，输出预算 30,000，沿原请求超时 600 秒；不改变并发或全量图片策略。

验收准备中修正过脚本日志进程名、测试排程日期（MySQL TIMESTAMP 范围）、Chunk 序号关联和历史 accepted 标志。均发生在首个模型请求之前；只有一次实际 Refine 请求。原始日志完整保留。

## 耗时、token 与内容结果

指标统计一个三 Chunk Refine 区间。前两列来自历史实验，第三列含当前宿主及事务提交；仅第三列经过正式持久提交。请求次数和输入 tokens 衡量重复交互；事件检查衡量已知补漏/去重及无关事实是否保留。不能据此推算整课时延或实际账单。

| 指标 | 历史 Canvas 实验 | 历史直接 JSON 实验 | 本次正式接线试点 |
| --- | ---: | ---: | ---: |
| 总耗时（秒） | 593.679 | 246.963 | 425.011 |
| 实际模型请求 | 8 | 1 | 1 |
| 模型侧 Canvas 读/写尝试 | 10 / 4 | 0 / 0 | 0 / 0 |
| input tokens | 314,671 | 11,307 | 11,384 |
| output tokens（含 reasoning） | 22,559 | 10,360 | 15,176 |
| reasoning tokens | 11,588 | 5,663 | 10,479 |
| 最终事件数 | 45 | 45 | 45 |
| 新增 ASR 转写 | 0 | 0 | 0 |

本次比历史 Canvas 总耗时短约 28.41%，但比历史直接 JSON 多约 72.09%。不能承诺稳定缩短 58.40%。新旧 JSON input 只差 77 tokens；可见推理 tokens 从 5,663 到 10,479，且流式窗口从 172.933 到 342.772 秒。首次 SSE 从 3.725 到 12.393 秒；正文窗口约 70.262 到 69.671 秒。新增时间主要落在可见推理输出窗口，没有 Canvas 往返、门禁反复修正或新媒体计算。流式窗口是客户端观察，不能把它全部等同于服务端纯计算或排除所有网络影响。

本次真实请求 18:52:47–18:59:52（Asia/Shanghai）：首 SSE 前 12.393 秒、可见 reasoning 窗口 342.772 秒、JSON 正文窗口 69.671 秒；模型传输窗口 424.857 秒、正式阶段 425.011 秒，本地/事务等差额约 0.155 秒。[详细模型计量](evidence/refine-direct-integration-20261101/model-metrics.json)。

成果为 16 DIALOGUE 与 29 个非对白事件，合计 45。已知漏句补回一次、两处边界重复移除；`50400–61180`、`113520–121660`、`178450–181440` ms 的跨界对白保留。29 个视觉事件的类型、正文、时间全部保留，允许人物数组按背景统一标签。实际 Kimi 完整结果与数据库正文完全一致。没有人工听音金标，不宣称准确率或所有隐含语义均已验收。[逐项核对](evidence/refine-direct-integration-20261101/quality-checks.json)、[模型完整结果](evidence/refine-direct-integration-20261101/model-result.json)。

区间 COMPLETE，retry_count=0，完成回执 1 条，三个成果版本 2/1/1 → 3/2/2；新进程重新读取后仍完成且不能再次领取。新增 model_usage 1 条。`parse_model_call` 是活跃调用占位，完成后释放，因此其行数差 0 不是“没有请求”；调用次数以 trace 与用量记录核对。[提交及恢复结果](evidence/refine-direct-integration-20261101/live-result.json)。

## 回归、证据与未验范围

相关回归两次最终运行合计 **516 passed**：Agent/Engine/Server 门禁、成员拒绝、来源、超时不提交、冲突刷新、租约、Refine/Prefix 跨界事务、冻结配置、实际消费者/持久化和上传到检索链路（测试中模型与 ES 为替身）。首次扩展回归暴露上传删除用例的异步清理断言时序：Consumer 停止后，ExecutionCalls 仍可延迟释放阻塞媒体线程。测试现在按既有合同有界等待真实清理完成，再断言无遗留；生产清理代码未改。早期失败日志保留，最终 514 + 2 项通过。

原 API 健康，原任务暂停；全局转写计数 3→3，原成果不变。没有恢复整个 117 Chunk 调度，也没有在此次真实试点执行 ES 索引、回看或追问，因此 B07/B20 仍 TODO。跨界事件在真实检索投影/回源中的覆盖仍需整课试点。采用这次已完成的直接 JSON 接线，继续验证运行波动和整课链路，不把服务加载、单区间完成或单次 token 改善写成产品闭环完成。

本机完整原始目录：`/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/refine-direct-integration-20261101/`。其中 run_real_refine.py 和 verify_real_result.py 为实际脚本；依赖上一轮 control.py、已保存配置、真实数据和本机服务，不是独立可移植脚本。source-database.sql、原始事件流及凭据配置保留本机，不纳入报告证据。只归档结果/计量/校验摘要；[范围核对](evidence/refine-direct-integration-20261101/scope-audit.json)保留 Gateway 未改与原表 hash。

本轮没有 commit、push、合并、另建 PR 或主开发库迁移；API 运行的是上述工作树未提交实现。

## 后续：10 路整课续跑启动（2026-11-01 19:12，Asia/Shanghai）

单区间交付后，继续完整 Let's build GPT 的请求已授权。先经控制接口恢复 v3，确认两项音频缓存命中；发现 v3 为此前单块实验冻结 Understanding=1 后，经明确选择切回10路，通过正式 reparse 创建 v4：`2a531c4f-2c73-4cc2-adaf-5156edc26236`。只改变 parse_concurrency 1→10，其他冻结配置逐项一致；旧 v3 暂停保留。本轮需重做此前已完成的1块 Understanding，整片 ASR 与说话人分组正文经身份检查跨版本复用，逐份相等、新增转写0。

首轮10个实际 k3.1-pro 请求均 stream=true 且已收到SSE，状态PARSING、未暂停、lastError=null；总117 Chunk，115个滑动Refine区间。配置为Understanding10、Refine10、Index1。该时点10块执行中、107块排队，不能写成整课完成。[启动证据](evidence/refine-direct-integration-20261101/full-course-started.json)、[音频复用核对](evidence/refine-direct-integration-20261101/full-course-audio-reuse.json)。

只读采集脚本每60秒从该任务持久计量保存进度与分布：Understanding/Refine/Index成功执行分别统计n、median、min、max；模型请求时延单列，失败、暂停和进行中样本不混入成功中位数，也不混入旧实验。原始目录为 `/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/lets-build-gpt-resume-20261101/`，`latest.json` 保存最近统计、`progress.jsonl` 保存变化。采集脚本不修改任务或发起模型调用；解析由API Consumer持续推进。此段为启动记录，最终中位数、终态和整课索引/问答结果待后续核验。


<a id="refine-feedback-repair-20261101"></a>
## Refine 错误反馈与排序续录（2026-11-01）

两份原始完整响应 e34cbb4d-b0ba-41d2-84c8-f5a24ec8ea6f、e5ea65f5-61d6-4182-8389-f4b8e288443d 均因排序拒绝；实际第二次请求含 guide，却没有被拒 JSON。第一次所在区间 416.023 秒完成；第二次修正有效后遇到 timeline_structure_changed，600.106 秒耗尽后任务级重试。请求失败不等于区间终态失败。

本次代码以 Refine 专用接收类型保留完整结果，保留原对外 Schema，再在业务门禁稳定排序、严格校验并提交。修正携带最近失败正文与同一份数据对应的错误位置，语法错误保留原文，不累计旧失败。回归 519 passed（1 项既有弃用警告）；两份真实输出共 6 成员、79 事件仅调整排列即通过 Schema，未改字段、未调用模型。没有新的实模提速或事务提交结论。

本轮新增验证证据：outputs/verification/video-asr-api-recovery-20261031/refine-feedback-repair-20261101/，其中 replay.json 是真实输出回放结果，validation.json 是测试命令及结果记录，before/ 与 change-manifest.json 记录本轮边界，源码与脚本保留复核入口。已有 Canvas/JSON 对照原表没有改动。

当前 API19621 的长任务未重启，本次新行为尚未在其进程加载；后续切换后需分别记录修正、版本冲突和真实提交结果。模型并发/超时、数据库 Schema、ASR配置、已提交成果和 Gateway 实现均未因本次修改改变。
