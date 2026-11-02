# Let’s build GPT 新版本全课解析对照（2026-11-02）

本次继续 [PR #18](https://github.com/owariband/Seekino/pull/18) 的完整课程验收，分支 `fix/video-asr-recovery`。运行代码 `1c3aaa6c2ed40359f56541daacdc8401c941ee52`，API 从 `/Users/hypnos/.codex/worktrees/full-video-asr/Seekino` 启动。本文先记录实际启动与测量合同；完整解析、Refine 冲突收益和业务质量尚未取得结果。

## 问题与比较边界

旧 v4 在 99 分 02.452 秒后失败，最终 81/117 Understanding、53/115 Refine、47/117 Index。它混有旧的格式反馈、共享成员冲突、网络等待和合盖休眠影响，不能继续在同一个任务上重试并把累计时间当作最新修复的性能。具体旧证据见[冲突基线](refine-interruption-baseline-20261101.md)；后续因果排查原始材料保留在本机 `outputs/verification/video-asr-api-recovery-20261031/refine-latency-causality-20261101/`。

本轮通过正式 `POST /api/uploads/reparse` 创建同一视频的 v5；旧任务和成果保留。新版本重新建立全部 Chunk / Interval，Understanding、Refine 和 Index 从头执行，ASR 与说话人分组按媒体、冻结模型及 Schema 身份复用。这是在相同音频缓存条件下比较解析链路；不能算冷启动 ASR 总耗时。

两轮 ParseConfig 逐项一致：Understanding=10、Refine=10、Index=1，k3.1-pro，3fps / 720px / 3秒上下文重叠，117 Chunk、115滑动区间；每阶段超时和重试保持原配置。本轮统一加载完整错误 JSON 反馈、稳定排序与 Refine 内容更新中断修复。供应商源配置仅将历史故障注入代理改为 `https://api.kimi.com/coding/v1` 直连，使取消作用于真实连接；网络时段也不同。因此属于最新实现相对旧运行的整体对照，不能把全部变化归因于 Refine 通知这一项。

## 身份、启动及已核实事实

| 项目 | 实际值 |
| --- | --- |
| 课程 | Let’s build GPT，6,979,873 ms，约116分20秒 |
| 视频 ID | `cd7c4da8-051c-5c6a-b95f-9fdaffefbeee` |
| 旧 v4 | `2a531c4f-2c73-4cc2-adaf-5156edc26236`，保留失败状态 |
| 新 v5 | `87f63b1f-1f7a-4d1f-bb05-fed572790a52` |
| 正式接口创建 | 2026-11-02 15:12:22，Asia/Shanghai |
| Consumer 开始 | 15:12:23.953 |
| API / 隔离数据库 | `127.0.0.1:19621` / `seekino_video_asr_recovery_20261031_test` |
| ASR / 说话人分组 | 15:12:24两项跨版本复用，正文分别与v4相等，新转写0次 |
| 首批 Understanding | 10个真实请求，全部stream=true，全部收到SSE |
| 首批首SSE等待 | n=10，中位43.6525秒，最大65.117秒 |
| 启动证据时点 | 15:14:43；当时尚无完成请求，不能判断阶段中位数和最终速度 |

[启动证据](evidence/lets-build-gpt-clean-parse-20261102/startup.json)保存逐请求时间和音频摘要；[冻结请求](evidence/lets-build-gpt-clean-parse-20261102/frozen-parse-request.json)保留正式接口配置。MySQL、ES、embedding 和 API 均使用已核对归属的独立实验进程；没有重建数据库、恢复旧任务或启动CPU ASR。`caffeinate -i -w <API PID>` 抑制空闲睡眠，采集器到终态释放该进程；这不能保证合盖或系统重启时继续执行。

## 测量与验收

优先判断完整117块是否成功解析和索引，再比较从开始到终态的实际用时。目标仍为原片50%—60%，约58分10秒至69分48秒。失败终态、已完成覆盖范围和停顿分别报告，不把并行请求时长相加当成用户等待时间。

Understanding / Refine / Index按本任务的成功完成执行统计n、median、min、max；失败、取消和进行中单列。记录每次Refine更新命中、旧请求取消等待、刷新代数、事务冲突及最终提交，分析它们发生时其他工作是否仍在推进。网络首SSE等待单列最大值、中位数和未收到SSE的请求；本轮没有增加首SSE超时策略。

只读采集每30秒保存进度。原始运行目录为 `/Users/hypnos/workspace/agent/Seekino/outputs/verification/lets-build-gpt-clean-parse-20261102/`：`run.json`、`environment.json`、`reparse-request.json`、`audio-reuse.json`保存身份与启动事实；`latest.json`、`progress.jsonl`和到终态生成的`terminal.json`保存持久进度；`api.log`、`logs/`、`model-trace/`保留请求与并发证据；`summarize_trace.py`提取首SSE等时间，`request-telemetry.json`是其带时间戳快照。原始Prompt/图片/模型正文保留本地，不放入Git。

本轮不靠删除校验、提高超时或暗中切模型让任务完成；若需要修代码或配置，先记录本版本失败/停止边界，不能把修改前后混成单个成功样本。完整课程索引完成后，检索→出处→回看→追问仍要单独验收；本次启动事实不能替代内容质量或产品闭环。
