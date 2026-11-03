# 整片 ASR Prompt 与 Refine JSON 对照（2026-11-01）

关联：[性能 TRD](../../design/03-product-reassessment/video-parse-performance-trd.md)、[PR #18](https://github.com/owariband/Seekino/pull/18)、[devlog](../../dev/devlog.md#devlog-prompt-refine-ab-20261101)。

## 问题、方案与比较范围

用户允许完整 ASR 对白超过名义 Chunk，要求 Prompt 简单且不附底层实现名；同时比较 Refine Canvas 工具与直接 JSON 编辑。现有严格 Chunk 校验与完整 ASR cue 形成冲突，模型多次猜测句内时间。实验通过现有 Gateway 调用真实 Kimi，使用已保存的真实输入，不写正式任务、不重启服务。

固定 Understanding 输入：同一首块 189 张 timestamp 配对图、22 条整片 ASR cue、当前课程背景、全片声源目录与现有 VideoParseResult Schema。旧 Prompt 与简化 Prompt 各新跑一次。候选不改变图像、采样、模型与并发，允许完整对白跨 Chunk，由后续 Refine 去重、补漏、衔接。图片 SHA、顺序、detail 和 189 个时间标签核对相同。

固定 Refine 输入：历史任务 613f40e2-7bcb-4e38-9ae4-9ad1021afb51 的前三块当前成果，版本 2/1/1、共 47 个事件，其中 29 个非对白事件。两组使用相同 evidence 和当前背景；共同去掉 cue.words，保留原始证据快照，精修以整条 ASR 时间为依据。冻结输入 SHA256 为 0c789b12b7fc7f30a6643a882f1716eed151750a7cbbb0ee5a67ede5626f0999。

Canvas 组使用现有 get_context_canvas、write_context_canvas、CanvasCompletion 和 DeepAgents；直接 JSON 组一次接收完整内容与证据，返回三个成员的完整 JSON。两组共同职责为对白去重/补漏/衔接/有依据的身份归属，保留无关视觉事件，均不提供额外取帧或声纹工具。因此这是相同精修任务的接口对照，不是旧生产全部职责与工具的整体前后比较。三块输入也不是本轮新 Understanding 的连续输出。

四组顺序执行，每组一次、600 秒总期限，Kimi k3.1-pro、stream=true、30000 output tokens，无自动重试/fallback；模型配置相同。配置中的 reasoning_effort=none 没有阻止真实 reasoning_content 返回，不能当作关闭 thinking。固定执行顺序为旧 Understanding、新 Understanding、Canvas Refine、JSON Refine；没有随机化、交叉顺序或多次重复，不推断稳定吞吐或尾分位。

## Understanding：对白完整性改善，但没有提速

| 指标 | 本轮旧 Prompt | 本轮简化 Prompt |
| --- | ---: | ---: |
| 总耗时 | 272.583 s | 318.592 s（+16.88%） |
| 全部事件 | 14 | 36 |
| DIALOGUE | 6 | 22 |
| 归一化英语词序列 | 输入 197，输出 183 | 输入 197，输出 197，序列完全相同 |
| 最后跨界对白 | 丢失尾部 14 词；最后对白止于 59670 ms | 保留 59670–61180 ms cue |
| 已命名对白 | 6/6 Andrej Karpathy | 22/22 Andrej Karpathy |
| 首 SSE 前 / reasoning 流 / JSON 流窗口 | 采集缺失 | 23.646 / 223.528 / 71.336 s |
| input / output / reasoning tokens | 采集缺失 | 232615 / 12162 / 7346 |

旧 Prompt 本次漏掉“now this is just one example of a prompt people have come up with”共 14 个归一化英语词；新 Prompt 完整保留，尾 cue 超过名义 60000 ms。新结果基本为每条 ASR cue 建一个 DIALOGUE，增加了描述、人物字段和事件记录；其他视觉事件也有拆分变化：ACTION 3→7、ON_SCREEN_TEXT 3→6、EMOTION 1→0。更多事件不等于更好质量，画面事件没有人工金标。

H1 的提速假设在这次样本中未得到支持。不能把耗时增加全部归因于事件数，也不能称消除了所有反复推理；候选仍有约 223.5 秒可见 reasoning 窗口。保留用户要求的完整对白合同，不将这份完整候选模板直接标为性能优化成功。当前提示没有给出相邻 cue 的稳定合并偏好，事件碎片化是具体待修问题，不追加更多并行模型路线。

旧 Prompt 对照首次调用的诊断 context 缺少 task id，未落盘 SSE/CoT；usage observer 又因 MappingProxyType 不能 deepcopy 而失败。墙钟与最终输出完整，token、首 SSE 与分段数据不可用，表中按缺失记录。后续三组已修好实验采集并核对 provider usage；没有为了补数据重跑旧组，也没有拿上一轮 311 秒历史调用代填。

## Refine：直接 JSON 减少多轮模型交互

以下为本次对照汇报的原表，按原有字段、数字、单位与格式保留。每组一次，同一组三个 Chunk；更高精度与诊断指标继续保留在后表和原始 evidence。

| 指标                            | Canvas 工具组      | 直接 JSON 组   |
| ----------------------------- | --------------- | ----------- |
| 总耗时                           | **593.7 秒**     | **247.0 秒** |
| 模型请求次数                        | 8 次             | 1 次         |
| Canvas 读取                     | 10 次            | 0           |
| Canvas 写入尝试                   | 4 次，其中 1 次被校验拒绝 | 0           |
| 累计 input tokens               | 314,671         | 11,307      |
| 累计 output tokens，包含 reasoning | 22,559          | 10,360      |
| 其中 reasoning tokens           | 11,588          | 5,663       |
| 最终事件数                         | 45              | 45          |

### 补充精度与诊断数据

| 指标 | Canvas 工具组 | 直接 JSON 组 |
| --- | ---: | ---: |
| 总耗时 | 593.679 s | 246.963 s |
| 本次耗时变化 | 基线 | −346.716 s，−58.40% |
| 模型请求数 | 8 | 1 |
| Canvas 工具 | 10 次成功读取；4 次写入尝试，3 次成功 | 0 |
| 工具参数错误 | 1 次写入被拒绝 | 0 |
| 累计 input tokens | 314671 | 11307 |
| 累计 output tokens（含 reasoning） | 22559 | 10360 |
| 累计 reasoning tokens | 11588 | 5663 |
| provider 报告 cached input | 247040 | 未报告 |
| reasoning 流窗口累计 | 421.218 s | 172.933 s |
| 最终事件 / DIALOGUE | 45 / 16 | 45 / 16 |
| 无关视觉事件保留 | 29/29 | 29/29 |
| Schema、源视频范围、三个成员集合 | 通过 | 通过 |

Canvas 组有一次写入被工具参数校验拒绝：三条 DIALOGUE 的 speaker 不在 participants 中。模型继续修订人物名单、拆分写入并回读确认，最后完成。最长一次请求为 287.723 秒，其中 reasoning 流 263.047 秒；下一轮又用 138.066 秒，其中 reasoning 流 104.793 秒。不是本地工具执行花了几分钟：八次模型请求窗口累计 593.382 秒，相对总耗时的剩余约 0.297 秒，仅可作为本地工具/轮间开销的差额估计。

JSON 组首 SSE 前 3.725 秒、reasoning 流 172.933 秒、正文流 70.262 秒；一次输出仍需约四分钟。结果支持减少 Canvas 读写规划与往返，但不能承诺下一次稳定节省同样比例。输入 token 下降 96.41% 是本次各请求累计值，包含 Canvas 历史消息重复发送；缓存计费、单轮上下文与模型输出也不同，不能据此直接换算费用下降。

### 内容与边界核对

两组都完成了同样的主要修复：

- 补回第一块遗漏的“sort of replying to it / now this is just one example”，保留完整 50400–61180 ms 对白，并移除下一成员重复开头。
- 保留 113520–121660 ms 的完整序列续写说明，移除下一成员重复的“completes the sequence with the outcome”。
- 保留 178450–181440 ms 的 ASR 尾句，没有因为第三块止于 180000 ms 而截断。
- 29 个非对白事件的类型、正文、描述与时间均与原输入相同；人物标签按课程背景调整。最终三个成员事件数均为 16/15/14，16 条对白 speaker 均为 Andrej Karpathy。

两份对白不是逐字相同：JSON 组沿用原成果中的“in a local park / a branch on the tree”，Canvas 组进一步改回 ASR 的“in the local park / a branch of a tree”。这是 JSON 没有完成的逐字 ASR 校正，不能说两组质量完全相同。两组还保留原有 ChatGPT 等转写修订，归一化词数均为 547，而去重后的原 ASR 为 550；已保存完整序列 diff，不能把词数比当作人工准确率。未重新听原音或逐帧制作金标，不保证这些原有修订全部正确。

对本次已知漏句、重复与无关事件保留检查，两组均通过；这不覆盖多说话人、视觉重新判定、复杂长区间或整课质量。直接 JSON 的完整替换也存在意外删改事件的风险，本例未发生不代表不需要正式宿主校验。

## 取舍、接入边界与验证

下一项实施应收敛为 Refine 直接 JSON：模型只接收事实和精修目标，正常路径一次返回完整结果；失败时由宿主提供具体 Schema 修复反馈，仍受现有有限重试与总期限约束。本实验没有触发或验证 JSON 修复重试，不据此增加新的无限循环。保留整片 ASR 白话合同，同时修正 Understanding 容易退化为“一条 cue 一个完整事件”的表述；不扩大图片、并发或模型路线。

正式实施中的完整 JSON 门禁属于 Refine 业务侧，复用现有领域 Schema、工具参数校验与错误反馈；Server 仍负责成员身份、源视频时间范围、租约、内容版本和事务提交。这里的 gate 不是 gateway/ 目录，不把本次业务门禁加入模型调用基础设施。取消模型侧 Canvas 工具不等于取消这些提交条件，也不要求删除其他节点使用的 Canvas。生产当前仍在 Agent、Canvas、Server/prefix 校验名义 Chunk/Interval 范围；实验仅放宽内存副本到 181440 ms，尚未统一这些正式路径。

离线检查通过：四份结果 Schema/视频范围、Refine 成员集合、四组模型配置、Refine 冻结输入、189 张图及时间标签一致性、候选完整 197 词、两组关键边界修复与 29 个非对白事件保留。没有为纯实验和文档变更运行无关业务回归；本轮未修改生产代码。

实验结束只读回查：最新任务 c3970da1-53b7-4cf5-9bb2-15bfa01e9947 仍暂停，lease_version=2、进度 1/117，三类任务活跃租约均 0；ASR 请求计数 3→3，新增转写 0；三个来源 Chunk 的版本、content 和 detail 均未改变。本轮没有服务重启、API 任务续跑或解析成果写库，不能称正式接口、117 Chunk、索引检索、回看追问已通过。

## 证据入口

- [汇总指标与完整 diff](evidence/prompt-refine-ab-20261101/analysis.json)、[离线核对](evidence/prompt-refine-ab-20261101/validation.json)、[运行状态核对](evidence/prompt-refine-ab-20261101/environment-check.json)。
- [共同 Refine 输入](evidence/prompt-refine-ab-20261101/refine-input.json)、[Canvas 最终结果](evidence/prompt-refine-ab-20261101/refine-canvas/result.json)、[JSON 最终结果](evidence/prompt-refine-ab-20261101/refine-json/result.json)。
- [旧 Understanding](evidence/prompt-refine-ab-20261101/understanding-control/result.json)、[新 Understanding](evidence/prompt-refine-ab-20261101/understanding-candidate/result.json)、[候选 Prompt](evidence/prompt-refine-ab-20261101/understanding-candidate.txt)。
- [证据清单与 hash](evidence/prompt-refine-ab-20261101/manifest.json)、[重现范围](evidence/prompt-refine-ab-20261101/README.md)。原图、完整 trace、原始 words 与实验脚本保存在本机 outputs/verification/video-asr-api-recovery-20261031/prompt-refine-ab-20261101/，不将含媒体的大型日志重复纳入文档。
