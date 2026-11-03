# Kimi Parse 推理耗时与逐帧时间配对试验（2026-11-01）

工作分支 `fix/video-asr-recovery`，沿用 [PR #18](https://github.com/owariband/Seekino/pull/18)。本报告关联[解析性能主 TRD](../../design/03-product-reassessment/video-parse-performance-trd.md)与[ASR 恢复 TRD](../../design/03-product-reassessment/local-whisperx-asr-trd.md)。时间为 Asia/Shanghai。

## 2026-10-31 流式基线

完整 Let's build GPT：116 分 20 秒、117 Chunk。隔离正式 API 任务 `613f40e2-7bcb-4e38-9ae4-9ad1021afb51`，`kimi_coding / k3.1-pro`，3 FPS、最大高度 720、前后各 3 秒视觉上下文、30,000 output tokens，Understanding/Refine 并发各 10。Gateway 已取入 dev `580df6cd` 中合并的 Kimi streaming 实现；PR #18 HEAD 仍为 `67223c16`，包含未提交实现，不能以 HEAD 独立代表运行源码。

19:41–20:00 共 140 个 stream=true 请求，25 个 Understanding、115 个 Refine；20:00:43 按用户要求暂停。14 个 Understanding 请求成功，结合之前已提交的 6 个，保留 20/117 成果；只有 1/115 Interval 提交完成。本轮 ASR 新转写 0。未完成整课索引、检索、回看和追问验收。

| Understanding，14 个成功样本 | 阶段含校验/提交 | 其中模型请求 |
| --- | ---: | ---: |
| 最快 | 85.231 s | 84.665 s |
| 中位数 | 320.173 s | 319.612 s |
| 平均 | 335.174 s | 334.626 s |
| 最慢 | 563.118 s | 562.509 s |

阶段不含此前整片 ASR、批量媒体准备或调度排队。每次只有一轮无工具 JSON 请求，含 198 张 detail=high 图、243,681–244,056 input tokens。14 次共 144,985 output tokens，其中 provider 上报 reasoning_tokens=123,355（85.1%）。不能从冻结配置 reasoning_effort=none 推断关闭了推理：当前 Kimi 出站请求没有关闭 thinking 的参数。

| 客户端流事件分段 | 首片前 | 推理流阶段 | 正文/工具参数阶段 | 本地工具/轮间 | 总计 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Understanding 平均请求 | 41.324 s | 265.291 s | 27.829 s | — | 334.626 s |
| Understanding 最慢 11:00–12:00 | 40.860 s | 495.023 s | 26.624 s | — | 562.509 s |
| Refine 06:00–09:00，6 轮后失败 | 61.652 s | 485.082 s | 47.315 s | 6.517 s | 600.578 s |

推理流阶段按首个 reasoning_content 到首个正文/工具 delta 计时；这是客户端接收窗口，包含可能的网络停顿，不是服务端纯 GPU 时间。空分片与舍入带来少量差额。失败末轮没有完整 usage，缺失不记为 0；不将并发请求耗时之和当整片墙钟。

API 确实返回并记录 reasoning_content。抽样行为摘要：慢 Understanding 在枚举相似帧、匹配末尾时间数组、校验事件与说话人；慢 Refine 在反复取局部帧、读取完整 ASR 和调整边界。原始推理文本只作为诊断线索；没有受控对照证明它的全部分析均属浪费。

## Refine 超时与额外重算

| 视频区间 | 请求轮数 | 最后一轮预算 | 故障 |
| --- | ---: | ---: | --- |
| 00:00–03:00 | 1 | 599.924 s | 0 SSE 后 ReadTimeout |
| 06:00–09:00 | 6 | 85.494 s | 持续 SSE 被总期限截断 |
| 05:00–08:00 | 9 | 12.636 s | 剩余预算内未收到首片 |
| 07:00–10:00 | 7 | 129.567 s | 持续 SSE 被总期限截断 |
| 02:00–05:00 | 10 | 5.500 s | 剩余预算内未收到首片 |

模型 request_timeout_seconds=600 同时被 model_factory、tool session 和 DeepAgents 用作整个 generate/Agent 的 deadline；每轮 HTTP 得到 min(剩余时间,模型超时)。外层 Refine 节点预算 1800 秒仍存在，内层先结束。fallback 1200 不负责这类超时，当前工厂只对 content_filter 放行 fallback。五次不是五个 HTTP 请求都独立卡了十分钟，也没有 HTTP 504。

按 tool_call_id 去重有 10 个 INVALID_READ、6 个 UNKNOWN_SECTION、9 个 truncated 回执。06:00–09:00 例没有工具错误，仍执行 12 次读 Canvas、17 次取帧，累计 64 张图，取帧本地耗时仅 6.153 秒，未完成 Canvas 写入。

另两个 Interval 完成模型分析后因共享 Chunk 从 version 1 变成 2、timeline_structure_changed 而刷新 snapshot 重做：04:00–07:00 已耗 577.375 秒/11 轮；01:00–04:00 已耗 472.561 秒/13 轮。03:00–06:00 用 338.825 秒/7 轮成功提交。峰值 20 个在途请求（Understanding 10+Refine 10）；没有 429 证据，不推定服务端限流。

暂停造成的 20 个租约取消异常不计为模型故障。零 SSE 的一次与透传代理 1200 秒 ReadTimeout 在开始时间上相差 0.252 秒，但没有上游 request_id/首字节证据，不能进一步定位服务端或网络；随后 retry 第一轮 3.572 秒收 SSE、28.766 秒完成。

## 2026-11-01 本次唯一改动与试验计划

用户授权保留全量图片，只将 Understanding ordered_frames 及视觉 fallback 从“所有图+末尾数组”改为逐图 `TextInput(timestamp_ms=<绝对毫秒>) → ImageInput`，最后保留同一业务 Prompt。帧内容、数量、顺序、FPS、overlap、分辨率/detail、ASR、模型、stream、推理配置、Schema、并发、期限与 Refine 策略不变。不引入分页工具，不缩短 Chunk。

沿用同一 worktree、PR、数据库和任务；重启退出的专用服务后通过正式 control API 继续。记录源码本轮 diff、实际请求的时间标签与图片一一配对、198 图以及与相同 Chunk 旧请求图 SHA 列表的一致性；ASR 行/SHA 必须不变且新增转写为 0。检查首片、reasoning 流、正文、usage、提交、Refine 与失败。观察窗口最多 15 分钟；若再现 Refine 600 秒超时则暂停保留现场，避免同类故障继续重试。任务自然 COMPLETE/FAILED 也结束观察。

这次是保存已有 20 个成果后的真实续跑，新成功 Chunk 与旧成功样本不同，不能声称严格 A/B 或把均值变化全归因于时间标签。没有人工金标/重复同片对照，时延与质量结论只作探索。

已有跨来源、重叠边界、直接 JSON、Refine 工具与 SSE 定向回归 **54 passed**（2.90 秒，Starlette deprecation 一条）。真实服务加载与本轮试验结果如下。

## 2026-11-01 实际续跑结果

09:23:05 恢复同一任务，测试 API PID 2908、端口 19621，源码来自 PR #18 worktree；09:33:08 通过正式 control API 暂停，观察 603.084 秒。首次检测到的两条 Refine model_timeout 均发生在 09:33:07，按预先约定停止，没有再开启其他实验。暂停后持久租约为 22，09:37 再查仍 paused=true，保留 Understanding **30/117**（新增 10）、Refine **3/115**（新增 2）、Indexed **1/117**。已暂停的是任务；API 与隔离依赖继续保留，便于检查。

20 个新 Understanding 请求（sequence 20–39）均实际携带 **198 张图片、198 个相邻 timestamp_ms 标签**，stream=true、无工具。首批 10 个与旧运行中相同 Chunk 的图片 SHA 列表和采样时刻逐项一致，非 input 请求参数与业务 Prompt 也相同；后 10 个没有相同 Chunk 旧请求可比，但全部通过配对/范围/数量检查。保持 3 FPS、720 上限、前后各 3 秒 overlap。

首批 sequence 20–29（视频 20:00–30:00）全部完成结构校验并提交；后 10 个启动较晚，在观察截止时没有完整响应/usage，作为截断样本保留，不计成成功或独立模型故障。成功样本的时延如下，阶段口径包括该 Chunk 模型调用及校验/提交，不包括此前整片 ASR、批量媒体准备和调度。

| 成功 Understanding 对比 | 10-31 旧格式，n=14 | 11-01 相邻配对，n=10 |
| --- | ---: | ---: |
| 阶段 min / p50 / mean / max | 85.231 / 320.173 / 335.174 / 563.118 s | 301.432 / 378.931 / 380.706 / 453.842 s |
| 模型请求 p50 / mean | 319.612 / 334.626 s | 377.810 / 379.851 s |
| 首 SSE 前，平均 | 41.324 s | 174.702 s |
| reasoning 流窗口，平均 | 265.291 s | 179.634 s |
| JSON 输出窗口，平均 | 27.829 s | 25.511 s |
| reasoning / output tokens | 123,355 / 144,985（85.1%） | 57,533 / 73,898（77.9%） |
| input tokens / 请求 | 243,681–244,056 | 244,011–244,230 |

这批样本的推理流窗口较短，但首片等待更长，总请求平均仍为 **6 分 20 秒**；没有观察到整体提速。首片等待包含客户端发送、网络、供应商排队/预填充等不可分离部分，不能直接称为 CoT 或 GPU 计算时间。两批成功样本来自不同视频范围、日期与并发现场，且有观察截断，不能把窗口变化或总耗时变化归因于标签，也不能从最长值下降宣称长尾改善。首个成功请求（20:00–21:00）301.012 秒，其中首 SSE 79.961 秒、首正文 279.071 秒，验证了新格式正式链路可运行。

本轮累计 109 条请求 trace：20 Understanding、89 Refine。两条触发暂停的 Refine 分别为 01:00–04:00 的第 9 轮（最后预算 36.412 秒、该轮无 SSE，整段 599.906 秒），02:00–05:00 的第 8 轮（最后预算 103.906 秒、该轮无 SSE，整段 599.932 秒）。04:00–07:00 第 13 轮又在暂停后约 1.6 秒返回 model_timeout（整段 600.130 秒），不计入触发停止前的两次；其 retry_recorded=false。另 5 条 trace 为明确的取消/租约撤销错误，其余无终结 trace 按截断处理，不把它们叠加成模型故障。没有 HTTP 504 证据。

新增成功 Refine 为 00:00–03:00（Agent 270.851 秒，阶段 271.174 秒）与 09:00–12:00（Agent 595.181 秒，阶段 595.460 秒）。这些 Refine 使用之前已生成的早期 Chunk 成果；**不能把它们的成功或超时当作新时间配对对 Refine 的效果对照**。本次没有改精修工具或总预算，也没有以重跑旧 Chunk 来消除这一边界。

本轮 ASR **新增转写 0 次**；原 video_parse_content 行 `5f9af428-31e5-49dc-9508-b83afc06025f`、2,476 段正文及 SHA `1910697d9ea39ce5c671a3be13327ebda4518dd310c61152f1f9877adcf597c4` 均不变。正式任务成功从持久成果恢复，本轮未启动 Whisper 推理服务。已完成的是输入改动、定向测试及一次真实续跑观察；输入配对保留，性能收益和人工质量没有验收结论，完整课程闭环未完成。B07/B20 仍 TODO；未 commit/push。

## 证据入口

- [Understanding 阶段统计](evidence/kimi-parse-trace-20261101/understanding-latency-statistics.json)
- [140 次模型调用流时序](evidence/kimi-parse-trace-20261101/model-latency-breakdown.json)
- [Refine 执行、重试与冲突日志](evidence/kimi-parse-trace-20261101/refine-investigation-server-events.log)
- 宿主原始证据：`/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/streaming-full-parse/`。
- [本轮汇总、最终暂停与持久成果](evidence/kimi-parse-trace-20261101/timestamp-pairs-final-metrics.json)、[10 个成功模型请求](evidence/kimi-parse-trace-20261101/timestamp-pairs-summary.json)。
- [20 个真实请求的配对与完整性检查](evidence/kimi-parse-trace-20261101/timestamp-pairs-request-audit.json)、[10 个相同 Chunk 的输入外参数/业务 Prompt 对照](evidence/kimi-parse-trace-20261101/timestamp-pairs-input-only-audit.json)。
- [本轮 Server 执行及失败日志](evidence/kimi-parse-trace-20261101/timestamp-pairs-server-events.log)、[54 项定向测试](evidence/kimi-parse-trace-20261101/timestamp-pairs-tests.xml)、[本轮代码差异](evidence/kimi-parse-trace-20261101/timestamp-pairs-source.patch)。
- 本轮原始证据：同根目录 `timestamp-pairs-20261101/`，包含逐请求流事件、源码之前快照、进程与 API 控制回执、最终状态及离线分析脚本。原始推理全文不复制进 TRD。
