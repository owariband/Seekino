# Let’s build GPT 新版本全课解析对照（2026-11-02）

> 11-02 18:01终态：v7因Kimi五小时额度耗尽停止，515.164秒、10/117理解、0/115 Refine、0索引，新ASR0。末节记录终态、阶段指标及Understanding跨Chunk对白合同未接通的问题；此前启动记录按当时时点保留。

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


## v6：加载 Understanding 修复后新建全课（16:06）

API已从同一worktree的`f3a3d43e0f3c91cfa4c3743cad684564bc521d52`重启，通过正式reparse在16:06:15新建v6 `72d0cdc1-f7f4-453e-b64e-8480239ab243`。完整117块和115区间重新运行；冻结ParseConfig与v5逐项一致，官方直连源配置也相同。v5保持PARSING_FAILED，没有在失败记录上混入修复后时间。

2476条ASR和说话人分组再次校验复用，分别与v5正文相等，新增转写0。[v6启动证据](evidence/lets-build-gpt-clean-parse-20261102/v6-startup.json)。新进程API19621，日志、trace和30秒只读采集独立放在 `/Users/hypnos/workspace/agent/Seekino/outputs/verification/lets-build-gpt-clean-parse-v6-20261102/`；查看`latest.json`或最终`terminal.json`，不把v5的`latest.json`作为新版本状态。

v6仍以完整解析/索引及58分10秒至69分48秒为目标，尚未完成。与v5的差异是新增有限Understanding格式修正；与v4比较时还包含此前Refine/排序/取消等改动，不能全归因为一项优化。检索、出处、回看、追问需要完整成果之后另行验收。


<a id="v6-image-failure"></a>
## v6 终态与图片截头追查

v6于16:20:18.741进入PARSING_FAILED，实际经过842.076秒（14分02.076秒），Understanding20/117、Refine3/115、Index1/117，新ASR0。processingDurationMs=788010且计时不完整，不能用它替代实际起止用时。[终态证据](evidence/lets-build-gpt-clean-parse-20261102/v6-terminal.json)。

首个原始错误来自sequence28的请求`cf203b92-2ea5-493b-a2ab-af91c18b57cf`：16:19:56.933保存请求，16:20:14收到400 `unsupported image format: application/octet-stream`，没有SSE或模型JSON。这不属于此前的JSON格式错误或Refine冲突。

与v4成功请求`e6c1a2eb-a0ad-4d8d-a9ab-e2fe6527d6c6`逐字节比较，198帧中的197张相同；28:16.500的图片由100451字节变成100446字节，恰好缺少开头`ff d8 ff e0 00`五字节，余下内容完全一致。同一坏图两次真实请求0.808/0.564秒稳定返回同一个400，正常相邻图5.155秒收到200和首SSE。用当前正式sampler从原视频重新抽取目标及相邻帧，8.817秒完成，目标图与v4正常图片逐字节一致，正式Base64编码和诊断保存/恢复不改变数据。该本地抽取限制到28:17，不能视为一次完整解析试跑。

这确认了失败请求的坏数据和直接拒绝原因，未确认截头发生的具体程序操作。最近Understanding修复改模型输出门禁，Refine修复改JSON快照/连接取消，时间标签改文字与图片的配对；抽帧、硬链接、读取/编码和诊断保存文件没有本PR独有修改。原临时帧已随任务收尾清理，缺少各阶段文件指纹，不能指认FFmpeg、并发、网络或其他进程为截头根因。[复现及来源证据](evidence/lets-build-gpt-clean-parse-20261102/v6-image-investigation.json)。

<a id="v7-fresh-run"></a>
## v7：保持实现与配置的新整课尝试

17:53:05通过正式POST /api/uploads/reparse新建v7，Consumer于17:53:06.036开始执行，任务`60bede4d-39c6-48ef-a4a6-a57e7b9a6e24`。v6已失败且无活动租约，保留历史记录、不再续跑；其他三个旧版本保持暂停。API归属、工作目录和零活动租约核实后，仅重启PR18的API为本轮隔离日志，MySQL/ES/embedding与原素材保持。

HEAD `6f7c38fb`，业务实现仍为`f3a3d43e`；与v6冻结ParseConfig逐项一致，Understanding/Refine/Index并发10/10/1，完整117Chunk和115滑动Refine区间从头执行，图片重新生成。2476条ASR和独立说话人分组正文与v6分别相等，两份完成元数据均记录reused_from_task_id=v6，新转写0。这是音频缓存复用条件下的完整下游解析，不包含冷启动ASR推理耗时；本地风扇没有明显负载不能替代调用记录，本轮记录已明确没有重算模型。

额外启动只读轮询，保留目标帧在整集目录、Chunk链接和实际请求中的哈希及异常原字节，不修改图片、不拦截模型请求。17:53:24首次观察、17:54:02 episode.json发布后观察均为100451字节且能解码，SHA与v4正常图一致。它是轮询快照，不能保证捕获每个瞬间，也尚未证明sequence28的实际发送或完成正常。

17:56:55请求快照：已启动10个Understanding，10个已收到SSE，错误0个；具体首SSE与阶段进度见[启动证据](evidence/lets-build-gpt-clean-parse-20261102/v7-startup.json)。单次启动不证明偶发截头已修复，完整解析/索引与问答仍待验。

原始材料：`outputs/verification/lets-build-gpt-clean-parse-v7-20261102/`中的run、audio-reuse、progress、request-telemetry、frame-audit以及model-trace。采集到任务终态结束，并释放本轮caffeinate；网络时间窗口与v6不同，不按首批时延宣称整体提速。


<a id="v7-quota-terminal"></a>
## v7 终态、阶段表现与尚未解决的 Prompt 冲突

18:01:41.200任务进入PARSING_FAILED，实际515.164秒（8分35.164秒），10/117 Understanding已提交，Refine0/115、Index0/117。最早Refine错误出现在17:58:26：Kimi返回429、`exceeded_current_quota_error`，明确提示五小时用量额度耗尽。八次请求覆盖四个区间，其中四次是追加重试；七次额度拒绝耗时0.387–0.462秒，一次连接被提前断开耗时36.872秒，均没有SSE或模型JSON。首个区间在第三次请求后耗尽原两次重试预算，18:00开始全任务收尾，其余已在执行的Understanding到18:01:41完成。不能把八次失败解释为Refine冲突重算、长推理或600秒超时。

[完整统计与Prompt核验](evidence/lets-build-gpt-clean-parse-20261102/v7-outcome-analysis.json)区分阶段执行和模型请求耗时，以下仅汇总本轮十个完成样本，不将并行请求相加作为整课耗时。

| 指标 | 本轮实测 |
| --- | --- |
| Understanding成功阶段 | n=10，中位264.465秒，最短213.470，最长458.682 |
| Understanding模型请求 | n=10，中位263.853秒，最短212.939，最长457.951 |
| 首SSE等待 | n=10，中位43.368秒，最大66.665 |
| 首SSE后至请求结束 | 配对样本中位212.195秒；包括持续生成及传输，不能全算纯GPU计算 |
| Understanding格式修正/模型重试 | 均0，十个请求首次通过 |
| 每次input/output/reasoning tokens中位 | 244480.5 / 7955 / 6156；input包含全量视觉输入 |
| 十次总input/output/reasoning tokens | 2434057 / 84920 / 68116；reasoning占output约80.2% |
| 说话人 | 69条DIALOGUE均为Andrej Karpathy，无null；仅证明本批命名一致 |
| Refine成功样本 | 0；不能计算有效推理中位数或宣称冲突收益 |
| ASR/说话人重算 | 0；持久成果继续保留 |

Prompt并未全部落实此前的简化方案。实验脚本`candidate_understanding`允许完整ASR对白跨Chunk，但正式`build_understanding_prompt`仍要求每个DIALOGUE是逻辑完整话语，同时每个事件必须在Chunk范围；共享Prompt和Server后缀再次施加同样限制，输入仍去掉words。Understanding的时间校验和Canvas也没有接入Refine所用的独立dialogue_bounds，不能只修改一行文字就声称贯通。

这在十份真实请求中均能核对：ASR保留跨边界cue，却无词级时间。首块允许范围0–60000ms，末条ASR是59670–61180ms；可见推理反复讨论截断、合并或省略，最终DIALOGUE结束设为60000，未保留该cue末尾五个词。sequence6也在同样的边界问题上推敲。不能据此量化有多少秒完全由冲突造成，也不能断言最终整课永久丢词，因为后续Refine没有取得结果；但足以否定“Understanding跨Chunk对白策略已完整接入”的结论。

目前已接通：全片声源/课程背景、时间标签配图、直接JSON Refine、原JSON与字段错误反馈、宿主稳定排序、共享成员更新通知。当前十个Understanding没有Schema错误，并不构成格式修复路径的新真实样本；本轮没有任何Refine成功提交，也无法新验证冲突通知。五小时额度错误仍被归为通用model_service_unavailable并按30/60秒退避，这是错误分类与恢复策略的缺口。首SSE短超时策略尚未实现，本轮没有600秒无首包样本。旧坏图已正常重抽，但仅执行到sequence9，未重新发送sequence28，截头根因仍未定位。

本轮仅查询与补证据，未切模型、追加模型请求或恢复任务。接口canRetry=true，已完成十块仍保留；额度恢复后可针对同版本恢复，没必要单为额度错误再从零新建整集。供应商未给出精确重置时间，不能将五小时窗口解释为从报错时刻再等五小时。完整课程成功、58–70分钟目标与问答闭环仍未验收。
