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


## 首批 Refine 提交与真实更新通知（15:24观察）

15:22:18与15:22:40，起点为sequence 2和5的两个Refine区间分别成功提交，成功执行耗时243.468秒、239.436秒。这些提交触发8次相邻旧请求取消；逐一关联trace确认错误为`ModelCallCancelledError`，取消后的8个下一代请求全部带有`Content update notice`和实际changed_versions。旧连接退出37–73毫秒，旧调用结束到下一代请求开始133–160毫秒。对应区间仍为lease_version=1、retry_count=0，没有把这8次更新当作业务重试耗尽；它们被底层`model_usage.status=failed`记录，统计时必须与真正失败分开。

[首批更新证据](evidence/lets-build-gpt-clean-parse-20261102/first-refresh.json)保存提交区间、每代调用、取消和新请求时间、实际更新通知。两次提交影响有重叠，部分区间从generation 1更新到2后又更新到3，8次取消不等于8个独立区间。此时已证实的是提交后的定向通知、真实连接退出和新快照提示接续；这些被刷新的请求尚未完成，不能据此宣称已消除重算或节约多少解析总时间。

15:24:00的进度为Understanding 19/117、Refine 2/115、已索引Chunk 0/117，经过11分36.280秒；10个Refine仍在执行，任务PARSING、lastError=null、新ASR仍为0。索引需要相关重叠区间都提交，不能把单个Interval的COMPLETE当成对应Chunk已经可检索。旧v4的99分钟失败终态与此处部分完成时点不作直接提速比例比较。


## 终态：Understanding 未提供格式修正机会（15:24:15）

v5最终PARSING_FAILED，经过711.418秒（11分51.418秒），完成Understanding19/117、Refine2/115、Index0/117，新转写0。此前15:24进度是停机前快照，后台已停止继续派发；[终态](evidence/lets-build-gpt-clean-parse-20261102/terminal.json)保留完整口径。

第一原始故障是sequence_no=13（13:00—14:00），call `9510e634-df2d-4275-9ffb-5b600e132115`：318.573秒返回完整结果，6个ACTION事件出现`text_variants: []`，在`timeline.3/5/7/9/13/16.ACTION.text_variants`触发`extra_forbidden`。[原始错误](evidence/lets-build-gpt-clean-parse-20261102/understanding-failure.json)。该请求有7080个SSE事件，首SSE60.756秒；不是504或网络未返回。

Understanding在Gateway解析时直接按VideoParseResult拒绝，Agent只有网络类重试，没有保存完整错误输出并提供字段位置的格式修正流程；第一次ValidationError就触发全视频停止。随后7个Understanding与10个Refine被撤销是连带结果，不是17个独立根因；先前8次共享成员更新取消仍单独归类。被刷新Refine尚未完成，因此整课冲突优化收益未验收。

下一步在同一PR18补齐Understanding业务结果门禁：请求仍携带规范Schema，先保留完整返回值，校验错误时用原JSON和精确路径做有限文本修正，不重新发送图片、抽帧或ASR，不放宽Schema；修正与首次生成共用原600秒请求窗口及既有最多2次修正，租约和最终提交门禁保持。先重放本次真实输出，再验相关测试与新运行版本；v5保留失败，不在其上混入新代码继续计时。


## Understanding 反馈修复与真实输出重放

原Schema保持不变；模型返回由`UnderstandingModelResponse`完整保留，Agent校验后才能进入原有时间、Canvas和事务门禁。非法字段或JSON语法错误携带最近完整候选和精确路径做文本修正，最多max_retry_count次，初次生成与修正共用请求期限。Gateway没有业务改动；ASR、抽帧、图片注入与并发配置均保持。

相关Agent、Worker真实接线、音频恢复及Refine更新回归390项通过。用v5 sequence13保存的真实响应重放业务门禁，通过正式Server修正方法发起1次真实Kimi文本请求，限定原600秒扣除已消耗319.450秒后的280.55秒；没有改写失败任务或提交重放结果到数据库。实际66.501秒完成，input7190、output2293（其中reasoning202），21个事件及所有合法内容逐项一致，只有6个非法空text_variants被删除，严格Schema通过。新ASR/抽帧均0，无图片输入。[重放摘要](evidence/understanding-schema-repair-20261102/live-summary.json)。

这是单次真实错误修正成功，不能替代全课稳定性或人工语义准确率。v5保留失败；后续新版本单独运行计时，不将本次66.5秒相加后宣称v5成功。
