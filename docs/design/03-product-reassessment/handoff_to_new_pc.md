background：
由于离职，公司电脑需要换成是我个人的mac，但是codex的session和workspace强绑定，所以我们会丢失历史的session
所以让各个关键codex agent记录其task到本file

# 维护原则

分为以下几个模块
1. 目前执行的任务（例如研究understanding异常耗时，为什么别人1min解析只需要30s~40s，我们的耗时来源于什么。。？——这只是一个参考），该任务期望解决的问题
2. 提出的各类研究、解决方案；以及各类解决方案尝试实验效果的对比，一路上究竟探索了哪些方案，过程是如何的？最新的探索进度是？
3. 接下来期望要继续做的，且对要解决问题是有依据有意义的事情是？为什么？
4. 对于如何做好这个任务的感悟？



# 交接

## Understanding 耗时、EP04 对照实验与 XML 输出协议

交接时间：2026-10-26，Asia/Shanghai。本节由负责 Understanding 耗时与 Schema 对照实验的 Agent 记录。定位快照：分支 `dev`，HEAD `c8ee5261ee57308ac7617c748c2d0adc32c74344`；工作区另有其他任务的未提交改动，HEAD 不代表本地所有代码及文档。

**先读这一段：当前已经验证“无需工具、一次送入帧与 ASR/OCR、直接输出解析结果”能够运行，也完成了三模型的新 XML 单 Chunk 实验；没有完成新 XML 的生产接入，没有用新 XML 重跑整集 EP04。用户最后将任务切换为先整理本交接，刚才的接入工作只做到只读调用链调查，没有开始代码修改或新的模型请求。下一位 Agent 不要误报已切换，也不要自动续跑已被暂停的整集实验。**

### 1. 当前任务、期望解决的问题与执行边界

#### 1.1 为什么研究这个问题

用户从面试交流中了解到：对方使用约 3 FPS 输入、Chunk 之间串行处理，1 小时视频约 30～40 分钟处理完。我们此前对约 23 分 40 秒的 EP04，即使 4 并发，媒体 + OCR + Understanding 也曾耗时约 28 分钟。因此希望定位成本来自媒体准备、ASR/OCR、输入处理、思考、工具交互还是结果生成，尽量达到每个 60 秒 Chunk 在约 30～40 秒内完成理解，同时保留可用的剧本信息。

对方的耗时是**用户转述的线索，不是已复现基准**。没有对方的完整 Prompt、实际模型版本、输出字段、Chunk 长度、缓存、预处理计时范围及质量验收，不能断言对方一定靠关闭思考、分专家、内部并行或特定模型实现。用户曾怀疑是 Doubao 2.0 non-thinking；我们没有证实。

产品需要的是服务于剧本的事件解析，而非仅输出摘要：人物、对白原文与有证据的译文、动作、情绪、关系变化、画面文字、参与人物与可见人物都可能影响后续问答、名场面推荐和分镜理解。不能靠删掉这些信息或错误归属来满足速度目标。

#### 1.2 必须保留的实验口径

- **全片与单块不同**：EP04 长 `1420127 ms`，按 60 秒切成 24 块。多数后期实验只使用第一块 `[0,60000)`，不能据此承诺整集或一小时视频的稳定性能。
- **用户纠正过串行定义**：正确的目标是先并行完成整集 ASR 与全部 OCR，等待所有证据就绪，再逐 Chunk 串行 Understanding；不是每块执行 ASR→OCR→Understanding 后才开始下一块。最初误解的串行运行已中止，不能作为该协议的结果。
- **3 FPS 不等于原视频每一帧**：单块实验送入 180 张 1280×720 采样图，全部发送；整集历史实验另有前后各 3 秒视觉上下文，因此总图片引用数、首块帧数不同。
- **后期的“理解耗时”**包括本地请求组装、图片编码与上传、服务等待、推理、输出与校验，不包括已完成的 ASR/OCR、抽帧、Refine、索引。
- **整体耗时要单独测**：共享 ASR 的旧耗时与新实验相加，只能叫阶段合计，不能写成新鲜重跑全过程的连续墙钟时间。
- **质量门槛尚不完整**：已检查结构、时间范围、部分人物与字幕，但没有全片人工金标。事件数、对白条数、具名比例都不是准确率。

#### 1.3 已完成与未完成

| 项目 | 当前状态 | 不能误认为 |
| --- | --- | --- |
| 整集媒体准备、一次 FFmpeg 批量抽帧 | 已进入生产代码；有整集独立基准与相关测试记录 | 已证明端到端一定更快 |
| Whisper 整集转写及不同解码参数 | 已有真实调用和结果；后期多数理解实验复用证据 | 每次模型对照都重新跑过 ASR |
| 原 Canvas 链路的整集 4 并发基线 | 24/24 块完成，保存结果与调用数据 | 已包含 Refine、索引、数据库提交 |
| “ASR/OCR 全部就绪→串行 Understanding” | 有脚本，未取得这一路径的完整有效整集对照 | 已经证明串行不可行或有完整串行总耗时 |
| 无工具单块 JSON、不同模型/detail、专家分支 | 已实测；成功、失败和质量问题分别保留 | 所有更短的时间都是成功解析 |
| 可读 XML v1；无缩进分行 XML v2 | 已实测，v2 三组均可解码 | 生产 Understanding 已改用 XML |
| XML 接入正式链路、EP04 新格式重跑 | 尚未开始实施；只读调查后转为写交接 | 有新整集成绩，或已删除 Canvas/改动数据库 |

本节交接的当前迁移目标以文件头的“个人 Mac”为准，前期讨论中的 Windows 计划不能直接当作现在的环境假设。

### 2. 研究方式、方案演变与实验对比

#### 2.1 先看 ASR：整集处理与更大模型不自动等于可靠

最早尝试整集 Whisper，目的是一次获取完整台词证据，再让 Understanding 使用。早期结果说明 ASR 参数本身会造成严重内容差异：

| ASR 方案 | 整集推理耗时 | 观察 |
| --- | ---: | --- |
| large-v3-turbo，默认整集 | 33.16 s | 117 个 cue，存在大量“音楽”和重复台词，内容退化 |
| large-v3-turbo，`max_context=0` | 35.77 s | 404 个 cue，重复退化明显缓解；不等于已达到准确转写 |
| large-v3，默认整集 | 167.83 s | 出现同一句大量重复，较大模型也会失败 |
| large-v3，`max_context=0` | 81.59 s | 333 个 cue，仍需核对文字和边界 |
| large-v3，日语、beam 5 / best-of 5、`max_context=0` | 123.50 s | 保留作进一步对照；不把 cue 数当准确率 |
| 10 月 24 日后续基线：large-v3 F16，同上并开启 token timestamps | 151.01 s | 音频提取 1.92 s、模型加载 4.18 s，合计 157.11 s；后续媒体对照复用这一份 |

这些时间对应不同真实运行，不能混写为同一次“满配 Whisper”。证据见 [ASR 对比](../../../outputs/verification/ep04-whisper-full-first-20261022/comparison.json) 与 [后续整集 ASR 实际参数和结果](../../../outputs/verification/ep04-serial-vs-parallel-20261024/asr.json)。源视频 hash、模型 hash、PCM hash、命令及请求参数都在文件里。

#### 2.2 串行协议纠正与整集媒体准备

最初的并行基线得到 24 块有效结果，媒体 + OCR + Understanding 共 **1706.91 s，28:26.91**。随后用户澄清：串行组必须先完成全部 ASR/OCR，才开始逐块理解。旧串行尝试的排除原因保存在 [中止记录](../../../outputs/verification/ep04-serial-vs-parallel-20261024/interrupted-wrong-protocol.json)。

修正方案的脚本为 [frontloaded pipeline.py](../../../outputs/verification/ep04-frontloaded-serial-20261024/pipeline.py)：ASR 与 4 并发 OCR 先完成，然后串行理解，并带最多前两块结果。该目录当前仅有准备脚本/冻结源及预检失败日志，没有完整结果和终态 summary。预检发现代码已不同于并行基线，见 [source drift 日志](../../../outputs/verification/ep04-frontloaded-serial-20261024/preflight-source-drift.log)。**不要为了续跑而简单删掉 hash 断言；应明确新基线或冻结实际代码。**

后来优先改造媒体路径：一个 FFmpeg 进程批量抽整集帧，整集音轨解码一次，Chunk 复用资产；音频 ASR + 有序图片路径不再转码无人消费的 Chunk MP4。不需要让已经退出的 FFmpeg 常驻，收益来自批处理与减少重复解码。

| 10 月 24～25 日整集对照 | 旧的逐块准备 | 整集准备后 4 并发 |
| --- | ---: | ---: |
| 媒体 + OCR + Understanding 墙钟 | 1706.91 s | 2138.22 s |
| 加共享 ASR/提取/加载的阶段合计 | 1864.02 s | 2295.32 s |
| 整集媒体准备 | 旧方案逐块交错执行 | 24.32 s |
| OCR 平均调用 | 49.16 s | 102.32 s |
| Understanding 平均调用 | 159.87 s | 245.86 s |
| 完成块数 | 24/24 | 24/24 |

因此当次端到端反而慢 **25.27%**，不能宣布整集准备已实现整体加速。媒体重复工作减少是独立的工程收益；模型阶段同时变慢，其原因尚未隔离。旧媒体准备累计 1520.82 s 是重叠工作累计，不能从墙钟时间直接扣除。两组采样计划相近但像素不完全一致：旧帧来自转码片段，新帧来自原视频，最大时间差约 17 ms。

该次代码改动涉及 `engine/common/frame_sampler.py`、`server/service/video_parse_media.py`、`server/service/video_parse.py` 及对应测试。历史验证记录为相关用例 208 passed，随后媒体专项 46 passed；有重叠，不能相加，也不是本次交接重新运行的测试。[完整报告与冻结源码入口](../../../outputs/verification/ep04-episode-media-20261024/report.md)。

#### 2.3 去掉 Agent 工具回合，先建立最小理解调用

新的诊断方式：把同一个 Chunk 的全部图片、19 条 ASR、31 条 OCR、同一人物背景一次送入，禁用模型工具、MCP 与 fallback，直接输出完整业务结果。用户说的这次“纯 ReAct”经确认实际指这个**零工具直接 Schema**条件，不是继续执行 ReAct 工具循环。

| 单 Chunk 方案 | 总耗时 / 首正文 | 结果与限制 |
| --- | --- | --- |
| 历史 Canvas 工具流程，两轮参考 | 170.73 s、252.63 s | 历史有 4 并发、189 帧及工具交互，不能视为只变工具的一变量实验 |
| Luna high，零工具直接 JSON | **78.60 / 63.87 s** | 结构与时间通过；正文输出约 14.73 s，4 次重连通知 |
| DeepSeek Flash high，8192 上限 | 52.53 s 后未完成 | 8192 token 全用于 reasoning，没有 JSON，不能算加速 |
| DeepSeek Flash none | **12.21 / 4.00 s** | 结构通过；仅对白和画面文字，没有独立场景、动作、情绪事件，未证明同质量 |
| Doubao 2.1 Pro disabled，detail=high | **63.63 / 21.39 s** | 结构通过，21 个事件；17 条对白中 14 条 speaker 未知，内容仍有缺口 |

Luna 78.60 s 中会话初始化约 **1.27 s**，单纯移除进程启动无法解释几十秒差距。重连通知出现的时间不是累计重连耗时，不能用 `78.6 − 最后一次通知时间` 估算“无网络问题时”的速度。

最初还遇到请求 Schema 兼容性：Codex 不接受判别联合的 `oneOf`，实验改为互斥 `anyOf`、移除 discriminator；DeepSeek strict 要求全部 properties 进入 required。均保留原领域模型做最终校验，失败记录单独存放，未混入成功耗时。不能把这些实验兼容处理误认为已修进全部生产 Gateway。

证据：[直接 JSON/Luna](../../../outputs/verification/ep04-chunk0-direct-schema-20261025/report.md)、[DeepSeek Flash](../../../outputs/verification/ep04-chunk0-deepseek-direct-20261025/report.md)、[豆包关闭思考](../../../outputs/verification/ep04-chunk0-doubao21-direct-20261025/report.md)。

#### 2.4 降低图像 detail：降低输入成本，但碰到结构化输出异常

用户指出输入已经是 720p。需要区分：**原图尺寸是 1280×720；API 的 detail 是另一项图像处理参数，二者不是同一个开关。** 本轮没有缩图、删帧。

| Doubao 2.1 Pro 同图试验 | 耗时 | 实际状态 |
| --- | ---: | --- |
| high 参考 | 63.63 s | 通过结构，内容有缺口 |
| 仅改 low，首次 | 32.56 s | 人名、台词、描述退化为单字；形式校验通过也不可用 |
| 同参数 low 复跑 | 34.38 s | 同样单字化，另有 speaker/participants 冲突 |
| low，再去掉请求 Schema 的 27 个 `minLength: 1` | 55.85 s | 完整文字恢复，但多余字段、时间排序导致原文校验失败 |

输入 token 从 247113 降到 105633，约减 57.3%；这不等于耗时减少 57%。流式文本与最终文本一致，未发现本地拼接丢字。去掉 minLength 后恢复长字符串是服务端结构化输出兼容性的线索，**尚未查明根因，不能归结为 low 让画面看不清**，因为 ASR 中已有文字也单字化了。[完整记录](../../../outputs/verification/ep04-chunk0-doubao21-low-direct-20261025/report.md)。

#### 2.5 从按枚举拆专家，到按证据与工作量拆少量分支

第一版按七类事件拆七个模型调用，每路都接收完整 180 帧和证据。所谓“专家”只是同一模型的不同职责 Prompt，没有训练模型。

- 单模型对照 16.83 s，但大量格式错误，不合格。
- 七路整体 30.42 s，同样失败：两路 TPM 429，其他路有错误字段、没有真正台词或职责混乱。唯一形式合格的画面文字分支把普通对白字幕全写成 ON_SCREEN_TEXT。
- 五个返回内容的专家请求已用约 52 万输入 token，重复读图明显增加；缓存很多，不能直接换算成费用倍数。

随后按用户认可的方案改成：**DIALOGUE 与 VISUAL 并行 → AFFECT 读取前两者事件 → 程序拼接排序**。对白接收 60 张 1 FPS 辅助帧 + ASR/OCR；视觉接收全部 180 帧 + OCR；情绪关系不再读图。程序合并只有毫秒级，模型关键路径才是主要等待。

| 证据分支方案 | 对白 | 视觉 | 后置情绪关系 | 完整链路 | 单模型对照 |
| --- | ---: | ---: | ---: | ---: | --- |
| 豆包 disabled，JSON mode | 39.66 s | 44.79 s | 9.87 s | **54.67 s** | 146.71 s 后截断且内容异常，不能算有效基线 |
| Kimi K3.1 Pro low，30000 上限 | 124.80 s | 147.25 s | 42.13 s | **189.39 s** | 191.27 s，现有领域校验通过但有同值重复 JSON 键 |

豆包前一轮 strict JSON Schema 分支曾失败，换 `json_object` 后才得到表中结果；仍有跨分支人物冲突、把普通切镜当新 SCENE、SUNWAY 误读等。Kimi 不支持本次所要求的 non-thinking，用户选择了 **low**；其 `json_object` 请求被拒绝后，用默认文本输出完成，不能写成“关闭思考的 Kimi”。Kimi 分支比单模型只短约 1.9 s，没有明显收益。

下一层依赖决定关键路径：`max(对白,视觉)+情绪关系`。拆更多路不会自动把时间平均摊开，还引入限流、重复读图、跨分支证据冲突与后续纠错成本。

证据：[七枚举失败](../../../outputs/verification/ep04-chunk0-enum-experts-20261025/report.md)、[豆包证据分支](../../../outputs/verification/ep04-chunk0-evidence-branches-20261025/report.md)、[Kimi 分支](../../../outputs/verification/ep04-chunk0-kimi31-evidence-20261025/report.md)。

#### 2.6 从完整 JSON 转向可读 XML v1

用户提出减少重复 JSON 内容，随后明确要求保留完整人名，而不是 P0/P1、缩写字段；希望接近 XML/MyBatis 的可读表达。v1 将 `persons` 汇总、重复 description 等机械内容交给程序，正文为“姓名：台词”，保留时间、参与者、可见人物和原文/译文。

最初想使用 Doubao 2.0 Pro，但请求 `doubao-seed-2-0-pro-260215` 返回 404 `InvalidEndpointOrModel.NotFound`，没有生成结果，也没有继续发 JSON 对照。其 1.339 s 是请求拒绝耗时，不是模型解析速度。[访问失败记录](../../../outputs/verification/ep04-chunk0-doubao20-xml-20261025/report.md)。

随后 JSON/XML 两组均使用普通文本输出 + 本地校验，避免只有 JSON 受到服务端严格 Schema 约束。模型/证据相同，但派生字段、Prompt、分段粒度也有变化，所以比较的是输出协议，不能解释为“尖括号比花括号快”。

| 同模型 JSON 与 XML v1 | JSON 总耗时 | XML 总耗时 | 原始结果有效性与限制 |
| --- | ---: | ---: | --- |
| Kimi low | 152.45 s | **116.05 s** | XML 通过；JSON 时间线乱序。对白 18 vs 7，粒度和归属不同 |
| 豆包 disabled，首次 | 59.84 s | 31.01 s | JSON 重复键；XML incomplete 截断，不能算完整成功 |
| 豆包 disabled，完整复测 | 58.08 s | **30.35 s** | 两者服务端 completed，但 JSON speaker 不在 participants，XML 语言/角色重复 |
| Luna high | **58.61 s** | 68.82 s | JSON 领域通过；XML 缺属性、语言角色冲突、乱序；两者都有内容问题 |

正文 token / 输出阶段确实可能缩短：Kimi 4390→1445 token、71.10→23.70 s；豆包复测 3153→1854 token、53.64→25.14 s；Luna 2563→1729 token、14.93→10.13 s。但 Luna 首正文前等待 43.66→58.65 s，推理 token 1409→2483，总时间反而增加。输入缓存和内容不同仍是混杂因素。

证据：[Kimi XML v1](../../../outputs/verification/ep04-chunk0-kimi31-xml-20261025/report.md)、[豆包与 Luna 综合报告](../../../outputs/verification/ep04-chunk0-luna-xml-20261025/report.md)。历史报告有“可以根据 speaker 补 participants”的诊断讨论；**后续用户已否定把缺失参与者隐藏在自动补齐中，以 v2 规则为准。**

#### 2.7 当前选定的 XML v2：无缩进、标签/正文分行

设计稿：[parse-output-xml.md](parse-output-xml.md)。用户明确要求的可读形式如下，例子只说明语法：

```xml
<timeline>
<dialogue start="0" end="4200" participants="千早爱音,高松灯" visible="高松灯">
千早爱音：你其实很想组乐队吧
<original>本当はバンドやりたいんじゃないの？</original>
</dialogue>
</timeline>
```

当前决定：

1. 无缩进，但不是把所有内容压在一行；事件开始标签、正文、结束标签分别换行，补充子节点各自另起一行。
2. 使用完整人名；对白直接“姓名：台词”。未知说话人明确写“未知说话人”，内部映射 null。
3. **participants 是完整语义参与者，包含已确认说话人和交谈对象，不是“除说话人外的其他人”。** visible 是独立的可见人物列表；画外说话人仍是 participant，未必 visible。程序不得为了过校验自动补 speaker。
4. 任务级提供主文本语言、角色和原文语言。EP04 示例为 `zh-Hans / translation / ja`，不能在生产里对所有视频硬编码日语。正文无中文证据时保留原文，用 lang/role 覆盖默认值，不编造译文。
5. 类型来自标签，persons 从明确人物字段汇总；description 来自正文或 note；程序稳定排序。根不再重复 unit，时间仍是原视频绝对整数毫秒。
6. XML 只是模型表达层，内部继续转换成 `VideoParseResult`；数据库和检索结构没有因为这份设计改成 XML。

#### 2.8 最新六次调用：2026-10-26

同一 180 帧、ASR/OCR/背景；三家各跑一次旧 XML v1、当前 XML v2。Kimi、Luna 新→旧；豆包旧→新；同模型内串行，不同模型同时跑。没有额外模型修复或选择性覆盖失败样本。

| 模型 | 旧 XML 总耗时 | 当前 XML 总耗时 | 旧 / 新首正文 | 旧 / 新正文阶段 | 旧 / 新推理 token |
| --- | ---: | ---: | ---: | ---: | ---: |
| Kimi low | 351.19 s | **129.90 s** | 308.43 / 89.38 s | 42.75 / 40.51 s | 7385 / 1687 |
| 豆包 disabled | 41.32 s | **31.34 s** | 12.85 / 6.44 s | 28.47 / 24.89 s | 0 / 0 |
| Luna high | 69.90 s | **75.84 s** | 56.14 / 63.23 s | 13.74 / 12.56 s | 1730 / 2489 |

本次旧 Kimi 与三个新结果通过解码和领域校验；旧豆包因排序失败，旧 Luna 因缺属性失败。三个新结果均通过无缩进和分行检查，没有已具名 speaker 被遗漏于 participants。新协议明确让程序排序，原始新输出仍分别有 4/6/3 处相邻倒序；不要据此声称模型排序已经稳定。

这轮不能宣称 Kimi 稳定快 63%：相同旧 Prompt 上一轮只用 116.05 s，本轮变为 351.19 s；当前新格式 129.90 s 甚至慢于那次旧格式。豆包新请求命中 91960/104052 个输入缓存 token，旧请求缓存为 0；约 10 秒总差距中，6.4 秒出现在首正文之前。Luna 两次各有 4 次 reconnect，缓存均为 0。三者正文阶段都只再少了约 1～4 秒。

内容抽查：

- Kimi 新输出有 16 条对白、7 个动作，SUNWAY/ESP 都保留，但 12/12 非对白事件省略 participants，虽然不少正文和 visible 已识别人物；结构通过并不证明参与信息完整。
- 豆包新输出 17 条对白、9 个 scene，ACTION/EMOTION 均为 0，很多动作/情绪写进了 scene；能在约 31 秒返回可解码结果，但不等于按类型检索的信息完整。
- Luna 新输出把长崎爽世认成椎名立希；48.4 秒之后的“誰も悪くないんだよ”又归给千早爱音，54.179 秒帧与人物背景支持该画面人物为爽世；还漏了“她都没告诉你们理由”，把 SUNWAY 写成 SUNDAY。
- 三组新输出都未保留 59880 ms 开始的“はい”。原 ASR 延续到 61360 ms，只有 120 ms 与本块相交，需下一块一起验收，不能直接当作整句漏检率。

最新入口：[总报告](../../../outputs/verification/ep04-xml-v2-three-models-20261026/report.md)、[精确时延与 tokens](../../../outputs/verification/ep04-xml-v2-three-models-20261026/comparison.json)、[原文结构与排版校验](../../../outputs/verification/ep04-xml-v2-three-models-20261026/validation.json)、[逐条对白与内容检查数据](../../../outputs/verification/ep04-xml-v2-three-models-20261026/quality-review.json)。

#### 2.9 Luna 为什么“越来越慢”：目前能说到哪里

不能把不同 Prompt/协议下的 78.60、58.61、68.82、69.90、75.84 s 串成同配置单调退化曲线；其中混有直接 JSON、XML v1/v2、结构约束和内容差异。

已有证据支持：XML 正文阶段更短，但本次首正文前的等待增加，足以抵消输出节省。它混合了上传、排队、预填充、推理及重连，现有数据无法精确拆分。推理 token 增加是线索，不是 XML 本身必然慢的证明。

用户怀疑 Prompt 未同步、存在 JSON/XML 上下文冲突。已检查：XML Prompt 没附旧 JSON Schema，未强制 JSON 输出，每组新建会话。最新运行的 base/developer instructions 相同，返回 instruction_sources 为空，实际 turn effort=high；观察到的系统 userPromptSubmit hook 只有 17/30 ms，entries 为空，未看到额外 JSON 输出要求。不能把“未观察到”扩大为能查看服务端全部隐藏上下文。证据：[runtime-audit.json](../../../outputs/verification/ep04-xml-v2-three-models-20261026/runtime-audit.json)。

另外两个必须留下的参数事实：

- Luna 的 thread 默认可显示 max，但实际 turn 请求为 high；不要只看线程默认值就说跑错思考档位。
- 用户要求输出上限 30000。Kimi/豆包出站请求已验证生效；既有 Codex 适配器未转发 max_output_tokens，也未转发 image detail，因此**不能声称 Luna 使用 30000 上限或 detail=low**。它只接收到相同的 720p 图片，图像处理由服务端决定。

### 3. 接下来有依据、值得继续做什么

#### 3.1 恢复任务时先明确最小接入范围

用户在转为写交接之前，要求“直接换这个新 Schema，重新解析 EP04”。当时按沿用旧整集模型链路理解为 Luna high 的 EP04 测试，但尚未确认最终重跑是新鲜 ASR/OCR、复用证据、串行还是原 4 并发，也没有开始调用。接手后应基于这一真实停点继续，不能假装整集实验已经在后台运行。

只读调查已发现，正式路径不是单改一句 Prompt 就能迁移：

| 入口 | 当前行为 | XML 接入必须处理的职责 |
| --- | --- | --- |
| [understanding/workflow.py](../../../agent/video_parse_v2/understanding/workflow.py)，`_understand` | 建 Canvas、登记 ASR/OCR，最后取 `canvas.result` | 明确 XML 结果怎样进入既有提交边界，防止模型返回了结果但 Canvas 仍为空 |
| [prompts.py](../../../agent/video_parse_v2/prompts.py)，`build_canvas_understanding_prompt` | 要求读写 Canvas、返回完成凭证 | XML 模式须同步提供实际证据和新输出说明，不能残留旧工具完成要求 |
| [video_parse.py](../../../server/service/video_parse.py)，`understand_chunk`（当前约 995 行） | 组媒体输入，调用 CanvasCompletion + Canvas tools | 接收原始文本、解码、领域/范围校验，再交付结果；保留现有媒体边界 |
| [model_factory.py](../../../gateway/model_factory.py)，`GenerativeModel.generate` | 接受 Pydantic result_type，并预检 JSON Schema | 当前不是通用任意文本输出入口，不能硬塞 XML 后宣称已兼容 |
| [codex/agent.py](../../../gateway/codex/agent.py)，`generate` / `_run_structured_invocation` | 构造 strict JSON outputSchema，再 `model_validate_json` | 在 Gateway 所属层明确文本生成/解码边界；不要把 Codex 私有 SDK 逻辑散落到业务层 |
| [schemas.py](../../../engine/parsev2/schemas.py)、[validation.py](../../../engine/parsev2/validation.py) | 领域对象、人物关系及时间合法性 | 继续复用；字段名 start_seconds/end_seconds 实际是整数毫秒 |

本次没有给出已批准的最终修改树，也没有选择最终 Gateway 实现。下一位 Agent 应先确定最小必要方案。优先只替换 Understanding 的模型表达/返回边界，保留 Refine、Prefix、声纹、存储、索引的业务结构；不能借 XML 改造删除整套 Canvas 或跨层重构。

实验解码器是 [compact_protocol.py](../../../outputs/verification/ep04-xml-v2-three-models-20261026/compact_protocol.py)，其 `checked` 辅助仍固定校验 `[0,60000)`；它是**第一块实验代码，不能原样拷入生产解析其他 Chunk**。迁移时需参数化允许范围和语言默认值，禁止 import `outputs/verification` 作为生产依赖。运行脚本引用旧实验目录/冻结源码也必须梳理，不能把历史快照悄悄当当前代码。

#### 3.2 推荐的下一轮验证顺序与理由

1. **先做离线接入验证。** 用保存的原始 XML 验证任意绝对时间范围、中文全名、双语默认与例外、正文换行、转义、未知 speaker、缺失参与者、重复语言角色、截断和越界。只允许已声明的确定性汇总/排序，不能修掉语义错漏后假装模型成功。测试实际 commit 路径拿到结果，且下游领域结构不变。
2. **固定模型和输入再跑代表性 Chunk。** 首块有大量特写，不能代表全片；至少覆盖对话密集、动作/切镜密集、跨 Chunk 话轮与场景变化。设置一次明确的质量门槛，避免对新 Prompt 一上来就付费重跑整集。当前 v2 的非对白 participants、省略/错分 ACTION、Luna 人物身份错误都需要列入验收。
3. **完成 EP04 24 块一次可追溯重跑。** 复用已有证据时只报告 Understanding 延迟；若目标是串行整片处理，应按用户纠正后的“ASR 与全量 OCR 并行→就绪屏障→串行 Understanding”执行，并另记包含预处理的真实墙钟。历史 4 并发数据只能作为有明确限制的参考。
4. **进一步判断 Schema 收益时做同模型重复对照。** 交替旧/新顺序，记录缓存、重连、实际输出内容量；必要时固定一份目标事件内容，只比较 JSON/XML 的表达输出，以区分格式开销与自由理解差异。不要把这个序列化微基准当成视频理解效果。
5. **有了有效基线再优化关键路径。** 目前有依据关注首正文前等待、实际 thinking 档位与通道重连；正文重复内容已减少，继续删缩进的可挖空间不大。没有同模型干净连接对照前，不估算“网络正常时一定只需多少秒”。

上述是交接后的建议顺序，不表示这些实验已经执行，也不自动恢复本轮已停下的模型调用。正式实施仍遵循本仓库 change-gate/hypnos-code，尊重已有用户授权；必要的较大范围变更应给出具体修改树，不把普通实现选择反复抛给用户。

#### 3.3 后续必须记录的指标

| 维度 | 至少记录什么 | 原因 |
| --- | --- | --- |
| 输入一致性 | 视频/图片/证据 hash，帧数/FPS/尺寸/重叠范围，Prompt 版本 | 防止不同输入被当作 Schema 单变量 |
| 实际配置 | 返回模型 ID、出站 thinking/effort、输出上限、detail、服务端输出模式、工具/fallback | 配置文件有值不代表 SDK 真正转发 |
| 分阶段时延 | 媒体、ASR、OCR、请求发出、首 SSE、首 reasoning、首正文、最后正文/终态、校验、整个流水线 | 找关键路径，不能只记总秒数 |
| 调用与资源 | 外层 turn、可观测 HTTP 次数、重连/重试/429、input/cache/output/reasoning tokens | 一个 Agent 调用可能包含多轮；重复输入和缓存影响速度/成本 |
| 完整性 | completed/incomplete/error、终止原因、XML/JSON 原文、结构/范围通过与否 | 截断、单字输出和错误早退不能算成功提速 |
| 内容质量 | 台词与翻译覆盖、说话人、参与/可见人物、动作与场景粒度、OCR 修正、跨块衔接 | Schema 通过不能发现爽世被认成立希等问题 |
| 统计口径 | 样本数、成功率、各阶段中位数及尾延迟；失败另列 | 一次样本不能宣布稳定收益或 SLA |

失败和重试的费用/耗时仍属于真实使用成本。当前没有核算实际账单，不按跨供应商 token 数简单推算金额，也不默认无 usage 的失败请求免费。

#### 3.4 新电脑需要留下的资产与复现入口

**`outputs/verification/` 和 `outputs/local-services/main-model-config.yaml` 已确认被 Git 忽略，clone 不会自动带走。** 本文件本身当前是未跟踪文件，也尚未提交/push；写好交接不等于远端备份已经完成。新电脑路径会变化，文内使用相对链接；旧脚本中绝对路径要按 manifest 映射。

| 资产 | 需要保留什么 | 作用 |
| --- | --- | --- |
| [parse-output-xml.md](parse-output-xml.md) 与本交接 | 当前设计规则、参与者语义、更改停点和对比结论 | 新 Agent 无会话也能判断已做/未做 |
| `outputs/verification/ep04-xml-v2-three-models-20261026/` | 完整目录：脚本、Prompt、协议、原文、result、summary、usage、审计和报告 | 最新六次调用及实验解码器，约 0.64 MiB |
| `outputs/verification/ep04-chunk0-direct-schema-20261025/` | 尤其 inputs.json、frames.json 和 180 张 frames；也保留原始/失败记录 | 其他单块实验共用的真正输入，整个目录约 15.26 MiB |
| Kimi XML、Luna XML、豆包 XML 首次和复测四个 20261025 目录 | benchmark/xml_codec、协议、原文、失败记录、summary/report | v1 历史对照与最新脚本依赖；这四个目录合计约 1.16 MiB |
| detail/Flash/七枚举/证据分支/Kimi 分支实验目录 | 至少报告、协议、Prompt、脚本、summary、usage、原始输出、质量检查及失败子目录 | 解释为什么走到当前方案，避免重交同样的试错成本 |
| `ep04-serial-vs-parallel-20261024/`、`ep04-episode-media-20261024/`、`ep04-frontloaded-serial-20261024/` | asr.json、protocol、result/timing/media、usage、冻结源码和中止/预检记录 | 整集基线、采样方式、正确串行方案尚未完成的证据 |
| `ep04-whisper-full-first-20261022/` 等 ASR 记录 | background.json、实际请求/解码参数、归一化转写、模型/媒体 hash | 人物背景、ASR 输入及历史参数差异 |
| 原始 EP04 MP4 | 从上述 asr.json 的 source_path 定位，保留原字节与 SHA-256 | 全片重跑必要输入；不依赖公司机原绝对目录名 |
| 有效模型配置与认证 | 私有保存 `.env.local` 及有效本地模型配置；Codex 认证在新机器按正常方式恢复 | Git 中 config.yaml 未必是旧基线的生效配置；本文件不记录密钥内容 |

最新 v2 + 共享单块输入 + 四个 XML 历史目录合计约 **17.1 MiB**，不是必须搬走全部 MySQL/binlog 或模型权重才可保留这项研究。其他分支/整集证据与原视频另计；上表没有声称已创建 zip 或上传任何文件。公共模型和临时帧可按参数重建，但已付费模型原文、失败记录和证据关联最值得优先留下。

关键 hash，供换机检查：

- 原始 EP04：`50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`。
- 共享单块 inputs.json：`977574433db3c20627552bd50660d102b648e43fde392f57b8ed276d24b7f6fc`。
- 共享 frames.json：`b823ffc8f982d2307df9b10b8b8593b50c5d7aec5dae9264603a848c7372f077`；还需逐帧 hash，不只比较清单文件。
- 最新旧 XML prompt：`6296c0fa84a9d22a41e3c21c7b5ab958b57e9ef26eaddaf0b48a228b87e682b9`；新 XML prompt：`1de9f4aec8a1c9001500cf2ed7ef49ad8bf7d9784949c8f3ad84c397bac79a72`。

旧整集基线使用的有效配置路径是 `outputs/local-services/main-model-config.yaml`：Understanding 为 `codex_local/gpt-6-luna/high`，OCR 为同模型 medium，3 FPS、720 高度、60 秒块、前后 3 秒视觉上下文、并发 4。不能直接读现在根目录 config.yaml 就断言那是历史实测参数；该文件在交接时还有其他任务的修改。

最新实验的 `analyze.py` 只分析已有输出、写本地汇总，不调用模型；`benchmark.py --preflight` 是本地检查，但会读取环境和所需素材；`benchmark.py --provider ...` 会真实调用模型。历史脚本多数有防重复执行保护，**不要为了读取报告直接运行 benchmark，也不要覆盖旧目录重跑**。查明冻结源、输入路径和配置后，另建有唯一标识的实验目录。

### 4. 做好这个任务的经验与需要纠正的方法

1. **先把问题建模为具体流水线，再找瓶颈。** “视频解析慢”混合了抽帧、转写、文字识别、视觉理解、结构生成、纠错、索引。先画清依赖和计时边界，才知道应该优化媒体、模型调用次数、输入量还是正文长度。曾经把用户的串行定义理解错，提醒我们必须先用简洁阶段顺序核对实验协议。
2. **先保留一个合格且可重复的基线。** 多轮测试同时改变模型、思考、输出模式、Prompt、图像 detail、事件粒度和缓存，虽帮助探索，却不足以归因。下一轮应少改变量，用同时间段重复对照，完整记录失败，避免只追逐更短的单个数值。
3. **更快必须绑定质量门槛。** 32 秒单字输出、31 秒截断、9 秒对白专家只输出一句说明，都不是成功。能解析 XML/JSON 也不代表正确：已发生人物错认、画面文字错误、参与名单缺失。用具体证据点和跨块验收，不能以事件数或具名比例替代准确率。
4. **模型做判断，程序做机械工作。** 排序、标签到类型映射、人物汇总、默认语言和内部字段派生适合代码；XML 的价值主要是减少重复表达、方便阅读，不是标签天然有性能魔法。不能把“机械处理”扩大成默默补齐不可靠人物关系。
5. **配置要查实际出站请求。** high/low 在不同模型间不是相同算力；Kimi low 仍思考，Luna 输出上限和 detail 未转发。参数存在于 YAML 或日志标题里不足以证明生效，需以请求、返回模型、usage 和终态一起确认。
6. **别用推测填补不可观测阶段。** 首正文前不全是思考，重连通知不等于网络耗时；输入 token 不是全是图像 token；缓存 token 比例不是账单。对方的 30～40 分钟只能当目标线索，不能反推其内部架构，也不能因为暂时没复现就认定只能并行。
7. **实验先形成可迁移证据，再考虑整体重构。** 生产 Canvas 还服务于纠错和持久化，单块零工具实验只证明一种 Understanding 入口可行。保持领域对象兼容、小范围接入、再跑代表性数据，比直接删大批设计更容易判断收益和回退。
8. **交接要区分设计、实验、生产和部署。** 本次新 XML 是设计稿 + 本地实验，不是已部署功能；整集媒体准备是已有代码改动，但其一次总耗时没有改善。记录原始输入、版本、失败与未决事项，比留下“已经优化好”的结论更有价值。

本节维护范围仅为 Understanding 性能、相关 ASR/媒体对照及模型输出协议。产品定位、Video-to-Script/镜头论文、迁移分支整理等其他 Agent 的任务应各自补充到本文件，避免将本节推测覆盖为整个项目的确定结论。

## CineAgents：素材理解、叙事召回与可执行剪辑闭环

记录：2026-10-26；负责会话：`/root`。本节交接的是换机讨论之前的论文研究与 Event 设计判断。已完成论文/补充材料核对和本地实现对照；尚未实现 CineAgents、修改 Event 合同或完成重剪实验。下文明确区分用户已确定的目标、论文披露、代码现状和待验证建议。

### 1. 当前任务及期望解决的问题

用户希望先跑通 **24 分钟动画解析 → 可召回的素材证据 → 可执行剪辑剧本 → 剪出视频**。研究论文的目的，是找到能补齐这个闭环的方法，而不是按论文新颖度选择架构。

用户已经明确的范围：

- **零预置角色库暂时不解决。**之后可以给 Agent 配联网搜索工具，预填写角色资料并下载参考图。本轮不要再把动画人物冷启动、InsightFace/SOLIDER 的适用性当作主阻塞；这也不代表联网预填方案已经实现。
- 输入既可能是完整原片，也可能是别人已经剪好的视频。后者的背景、人物和原作可能未知，但镜头编排、剧本逻辑与视听风格仍需要解析，作为后续重剪或生成视频的 reference。
- 需要回答到实现层面：素材理解看哪些维度；各阶段真正输入/输出的 Schema 是什么；叙事召回如何检索、如何检查遗漏；“落实到镜头”如何变成能执行的剪辑操作。
- 用户最后追问的是：**“我们的 event 逻辑是否应该改变成他们那种？”**尚未达成“替换现有 Event”的决定，也没有对应代码实施授权。

下一位 Agent 不需要从八篇论文排名重新讲起，应接着解决 Event 的职责与闭环缺口。

### 2. 已探索的方案、证据、效果与最新进度

#### 2.1 探索过程与方向修正

| 阶段 | 做过什么 / 得到了什么 | 当前应如何使用 |
| --- | --- | --- |
| 八篇论文初筛 | 起点是 CineAgents、CutClaw、EditDuet、LAVE、MM-StoryAgent、MovieAgent、VideoGen-of-Thought、VideoDirectorGPT 的任务适配比较。 | 这是研究路线，不是八套均已复现的候选实现。CineAgents 聚焦叙事组织；CutClaw、EditDuet 暂留作后续选段与时间线参考。 |
| 深读 CineAgents | 核对正文及补充材料中的 Agent prompt、JSON 示例、grounding 反馈、消融和成本。 | “主干参考”不等于完整工程方案，也不意味着其 Schema 优于我们的 Schema。 |
| 用户指出信息不足 | 用户明确反馈“看完后学不到任何东西”“他们的 schema 比我们的粗糙”。 | 后续说明必须给出真实字段、转换过程、失败案例和已有实现差异，不能只重复 Agent 名称和流程图。 |
| 回到本地 Event 对照 | 阅读当前 Parse Schema、Event 索引与检索链路，区分细粒度观察事件和剧情段落。 | 最新建议是保留现有事实层，先验证是否需要叙事组织层；这仍是建议，不是用户已批准的改造。 |

#### 2.2 CineAgents 实际披露到了哪一层

以下是论文材料中的接口与行为概括，不是我们设计的新合同。主要依据为[正文 §4](https://arxiv.org/html/2604.10456v1)及[作者版论文补充材料图 S6–S9](https://pub-e7bc7e5ed67b401a8e9e587c3dee22b6.r2.dev/publications/2026/ECCV26_CineAgent.pdf)。

| 环节 | 披露的输入 / 输出及处理方式 | 不能据此推定的能力 |
| --- | --- | --- |
| Script Agent | 接收已经预处理好的 Shot 摘要：`shot_id / summary / characters_present / dialogue{speaker,text}`；将 Shot 分组成 Event，再抽象 Story 和角色 Profile。 | Script Agent 的 prompt 明确从已有摘要组织叙事；不能把这个步骤当成直接分析原始视频的完整实现。 |
| Narrative Memory | 示例包含 `shots`、`events[{event_id,event_summary,shots}]`、`story_summary`、`character_profiles[{character_id,profile_summary}]`。 | 没有因此获得严谨的因果边界规则、完整来源置信度合同或可直接照搬的细粒度解析 Schema。 |
| Director / Blueprint | 根据需求及记忆摘要提出分阶段故事描述，并接收 Orchestrator 的修改反馈。 | 创作计划不是已经发生的原片事实。 |
| Orchestrator | 按 Blueprint 阶段寻找相关 Event，再取其关联 Shot；判断这些素材能否支撑该阶段。成功输出有序 `final_shot_sequence`；失败返回具体阶段的问题，要求修改计划。 | “语义搜索”这一描述没有交代足以直接复现的向量模型、top-k、阈值等完整检索实现；“足够支撑”也不是形式化的无遗漏保证。 |
| Editor | 按已确定的 Shot 顺序组装，并输出封面、配乐、转场、字幕样式等操作。 | 示例没有构成完整的帧级时间线、音轨和精确入出点合同。 |

这里“落实到镜头”的具体含义是：**将“我要表现某段故事”的文字意图，变成素材中真实存在且按目标顺序排列的 Shot ID。**对 Seekino 来说，之后仍须把 Shot/证据引用解析成来源视频、入出点和输出时间线，才能交给渲染器。

论文值得借用的是“计划必须被素材支持，失败就改计划”的约束。检索到了语义相近的事件，不等于人物、动作、因果或结局就正确。对于“如何判断遗漏”，当前只能确认其使用阶段 grounding 和反馈；不能把它表述为已解决解析漏识别或召回漏检。

已核对的论文自报结果：Table 1 中完整系统 F1 为 64.13，去掉对白匹配为 61.17、层级记忆为 58.04、迭代规划为 59.52；完整系统时序正确率仍为 52.09%。§6.3 报告可复用预处理平均约 1 小时 / 14 美元，后续指令约 6 分钟 / 0.62 美元。**这些均为论文自己的实验，未在 Seekino 复现，也不能换算成 EP04 的成本与耗时承诺。**[论文实验部分](https://arxiv.org/html/2604.10456v1)

#### 2.3 我们的 Event 与他们的 Event 不是同一种粒度

当前代码核对入口：

- [Parse Schema](../../../engine/parsev2/schemas.py)：`VideoParseResult = persons + timeline`。Timeline 类型包括 `SCENE / ACTION / DIALOGUE / EMOTION / RELATION_CHANGE / ON_SCREEN_TEXT / OTHER`。
- 公共字段为 `start_seconds / end_seconds / description / participants / visible_persons`；Dialogue 另有 `speaker / text_variants` 及程序记录的 `speaker_source`。**当前代码的时间值实际是原视频绝对整数毫秒、半开区间；不要按字段名再乘 1,000。**部分旧设计文档仍写整数秒，恢复工作时以当前代码及有效合同核对，不在本任务顺手改文档。
- [Event 索引投影](../../../engine/rag/indexing/events.py)：身份来自 `(parse_task_id, video_chunk_id, version, timeline_index)`；从正式 Timeline 派生检索内容，不把另一套剧情摘要当成新的权威原始事实。
- [Event 检索服务](../../../server/service/video_event_retrieval.py)与[融合/核验算法](../../../engine/rag/retrieval/events.py)：已有词法、语义、视觉通道，融合后批量回源，核验来源版本及 Timeline 条目，再返回 Chunk Evidence；trace 保留候选、预算截断、剔除原因和最终选择。代码存在不代表当前数据集索引覆盖及召回质量已验收。
- 有效设计入口为 [Sub-Chunk / Event 最终 TRD](../02-agent-harness/sub-chunk-trd.md)；[旧 Event TRD](../02-agent-harness/event-retrieval-trd.md)包含已被替代的历史方案，不能照着其中的旧 MySQL Event 表方案开发。

用一个**仅用于说明粒度的假设例子，非 EP04 实测事实**区分：一个剧情段落“争执后离开”，可能关联若干镜头，也包含“说出拒绝台词”“表情变化”“转身走出”等多条 TimelineEvent。论文 Event 负责把这组素材作为故事段落组织起来；现有 TimelineEvent 负责保存可定位的具体观察。

| 备选方案 | 已知收益 / 风险 | 当前状态 |
| --- | --- | --- |
| 用论文 Event 替换现有 TimelineEvent | 字段更少，但会混淆剧情组织与动作/对白等事实记录，影响已有索引与回源合同。 | 不建议直接替换；未做实验或迁移。 |
| 完全保持现状，直接检索细粒度 Event | 可复用现有实现，是必须保留的比较基线；跨镜头叙事完整性仍需验证。 | 代码已有；本研究未跑针对性对照。 |
| 保留 TimelineEvent，按需组合为叙事段落/创作阶段 | 有机会补足“前因—行动—结果”的组织，并保留原始证据。可先由创作任务临时组织，证明需要后再考虑缓存或索引。 | 当前推荐的实验方向；名称、字段、持久化位置均未定稿。 |

检索覆盖有两个不同问题：索引是否覆盖当前正式数据，可检查 receipt、来源版本及失败原因；剧情是否漏掉重要事实，要对照原视频和人工标注。前者通过不代表后者无遗漏。本地已有的检索 trace 能帮助定位候选截断或剔除，但不能独自证明解析完整。

#### 2.4 当前证据与实验边界

- 本轮完成的是阅读与代码对照，没有在 EP04 上运行新的 CineAgents 解析、叙事分组 A/B、召回评测或剪辑渲染；没有自测耗时、成本或质量分数。
- 此前对作者页面与实验室页面的检查未找到已确认的官方实现仓库；这只是当时的搜索结果，不应写成“官方一定没有代码”。需要复现时再核对[作者页面](https://shuchenweng.github.io/)及[实验室发表列表](https://camera.pku.edu.cn/publication)。
- 旧机器的临时核对材料为 `/tmp/seekino-cineagents-evidence-20261025.md`、`/tmp/seekino-cineagents-supplement-source.txt`，后者是带来源位置的文本摘录，非完整论文或代码。**新电脑不能依赖这些临时路径存在。**关键结论、Schema 与公开来源已收进本节；需要更多 prompt 细节时按原文图号重新获取。
- 对本任务而言，[Shot 研究记录](../../research/2026-10-24-shot.md)和[镜头构图 Schema 设计](../02-agent-harness/shot-composition-schema.md)可作为后续对接入口；不能仅凭文档存在认定镜头解析与裁剪映射已接通。

### 3. 接下来值得继续做的事及原因

以下是建议顺序，尚未实施；用户当前只授权维护本交接文档。

1. **先用一个真实片段回答“Event 是否要改”。**选 EP04 中含前因、动作和结果的一段，对齐现有 Timeline、实际 Shot 边界及用户剪辑目标。交付同一素材在两种粒度下的具体数据示例，指出哪个问题是现有检索无法回答的。先验证缺口，避免因论文使用相同名词而重建事实层。
2. **比较现有检索与叙事分组的实际效果。**固定同一解析缓存与查询集：细节查询、跨镜头因果查询、需要重排的剪辑请求。基线直接调用已有 Event/Chunk 检索；实验组在相同证据上组织段落，再选镜头。逐项记录必要证据是否齐全、误召回、来源可追溯性、额外模型调用/耗时/成本；将失败归因到解析漏识别、索引未覆盖、候选截断或选段错误。实验前确定人工核验样本，不能用模型自评证明“无遗漏”。
3. **补齐最小执行链，而不是先扩充 Schema。**从已有素材引用导出一个可审查的剪辑计划，明确来源版本、片段入出点、输出顺序与持续时间，以及本次确实需要的声音处理。先确认仓库已有编辑/渲染接口再适配；用短片验证源范围合法、目标时长、对白是否被截断、输出是否可播放。只有真实消费方需要时才固化 `CompiledScript` 字段，不能把 Shot ID 数组称为完整时间线。
4. **单独验证 reference 视频的解析。**选一个已剪辑、背景未知的短视频，记录实际呈现的镜头顺序、时长、转场、声音与画面关系，以及各段在成片中的作用。保留“画面/对白直接支持”与“创作解读”的区别；不要由蒙太奇自行补全原作时间线。再把可迁移的编排要求落到目标素材，缺素材时返回明确缺口，或进入用户允许的视频生成分支。CineAgents 的原片记忆不能被宣称已经覆盖这种 reference 理解。
5. **闭环证据明确后再决定结构调整。**若分组显著改善跨镜头召回，再讨论派生叙事层的缓存、版本失效和索引；若失败主要在细节漏识别或入出点，优先修实际失败环节。CutClaw 的音乐/选段与 EditDuet 的时间线操作，待对应需求出现时继续核对，不同时引入全部 Agent。

恢复本任务时的首个交付应是：**基于一个真实片段的“现有 Event → 可选叙事分组 → 命中证据 → 目标镜头顺序/入出点”对照，并据此回答是否需要改 Event。**若 EP04 素材或解析缓存未随项目迁移，先定位可用数据，再说明缺口；不要编造已经验证的例子。

### 4. 做好这个任务的经验

- 研究必须拆到输入、处理、输出、消费方和失败条件。用户已经否定只讲角色分工、宏观流程和论文排名的回答方式。
- 论文 Schema 粗，不意味着没有可借鉴的约束；我们的 Schema 细，也不能直接证明叙事召回完整。应比较实际任务是否完成，以及哪个字段被谁消费。
- 把“解析观察”“叙事组织”“创作意图”“执行计划”区分开。可保留不同粒度的引用关系，避免创作计划反写成源视频事实，也避免为分层而新增重复事实源。
- 对未知背景的 reference，先解释成片如何组织信息，再讨论可迁移的风格和结构；可观察的剪辑顺序不自动等于原作事件的时间或因果顺序。
- 对“有没有遗漏”给出可核验范围与失败归因，不能用“Agent 检查过”“grounded”或非空检索结果代替证据覆盖评测。
- 保留用户已经作出的取舍：角色库问题后置，优先闭环。Director、Orchestrator、Critic 可先作为职责和校验阶段，不必对应多个独立 Agent 或额外模型调用。


## 本地 Sandbox 接入与创作闭环验证（更新：2026-10-26）

### 1. 目前执行的任务，以及期望解决的问题

**当前任务是让 Seekino 的 Agent 获得本地隔离执行能力，快速完成一次视频或创作 workflow：准备素材 → 模型写制作代码 → 在 sandbox 内执行 → 检查并交付可预览作品。** 目前已经验证本地 sandbox 能执行视频处理，尚未接通 Kimi / DeepAgents 的创作循环，也没有正式接入 Chat hook。

用户最看重的是尽快验证实际效果及接入复杂度，并且必须考虑 local mode。当前讨论沿用 Kimi `k3.1-pro` 与 DeepAgents；模型调用、检索、数据库和会话状态继续留在 Seekino 宿主，制作代码交给隔离执行环境。统一 DeepAgents 生态的收益应体现在复用工具和工作流，不应变成必须注册云服务或自行维护整套 VM 平台。

本 Agent 更早负责 Parse 的 ReAct / Deep Agents / Codex 同 chunk 对照。用户之后明确表示暂不纠结 Parse 的 harness、Canvas、compact 或 pipeline 架构，优先推进 sandbox 与创作闭环。**不要在恢复本任务时自动重启 Parse 重构，也不要把 sandbox 接入变成更换模型或 Agent harness 的工程。**

当前可写职责是本任务的研究、最小验证和后续获准的执行后端接线；Parse、部署体系重构及其他 Agent 的工作不属于本任务。此前未修改 sandbox 相关生产代码、数据库 Schema、默认模型配置或前端，也未为此重启产品服务、commit 或 push。本次换机交接只维护本节。

### 2. 已提出的方案、探索过程、实验对比与最新进度

#### 2.1 已归档背景：Parse harness 对照

完整实验见 [devlog 的固定锚点](../../dev/devlog.md#devlog-parse-harness-ab-20261025)，入口为 [evals/parse_harness.py](../../../evals/parse_harness.py)。当时冻结 MyGO EP04 chunk 0 的 60 秒视频、180 张图、19 条 ASR、31 条 OCR 和背景，只运行 Understanding，不经过 Refine / Prefix / 索引或数据库提交。

- 同模型对照是 LangChain ReAct 与当前 Parse Deep Agents profile，均用 Kimi `k3.1-pro / low`。主实验每组两次，首轮真实请求除 timeout 外完全相同；扩大实验上下文预算后，两组均结构完成 2/2、字幕锚点覆盖 15/15，且 **0 次 compact**。Deep Agents 平均耗时约 162.54 秒，ReAct 约 218.43 秒；样本很小、后续工具轨迹不同，不能据此证明 Deep Agents 的 compact 更好或框架本身必需。
- Codex + Kimi 可以连通并在诊断合同下读写 Canvas，但工具调用与最终结构化完成合同没有同时满足。按用户要求另跑原 GPT 配置基线；它与 Kimi 不是同模型对照，不能用来证明纯 Codex harness 的质量收益。
- 约 47 秒已确认应为长崎爽世的对白，在本轮各完成路径中都没有被可靠解决。Understanding 未接收原始音频，且未跑现有 Refine / 声纹链路，因此不能直接归因于 SDK，也不能声称完整解析链路一定修不好。
- 原 128k 配置下，固定媒体输入本身已超预算；ReAct 诊断发生 12 次 compact、0 条事件写入、至少约 2.49M token 后人工中止。这是预算与无进展循环的证据，不是有效质量完成样本；未实跑同预算 Deep Agents，不能替它补造失败结果。
- 后续讨论过单次结构化 Understanding、减帧、多专家和 Refine 声纹复核，但本 Agent 没有实施这些改造。用户已把当前优先级转向 sandbox，以上仅作历史与证据保留。

原始输入、trace、结果和评分材料在 Git 忽略的 [parse-harness-kimi-20261025](../../../outputs/verification/parse-harness-kimi-20261025/) 及 [ep04-chunk0-direct-schema-20261025](../../../outputs/verification/ep04-chunk0-direct-schema-20261025/)；详细复跑命令见 devlog。

#### 2.2 Sandbox 方案如何收敛

初始参考是 [Supervisor / LangSmith 研究](../../research/2026-10-25-supervisor-sandbox-langsmith.md)。当时发现现有 SDK 已有 `SandboxClient` 和 `LangSmithSandbox`，所以过早把最快接入理解为 LangSmith 托管 VM，并要求配置 API key。用户指出 local mode 约束后，才补查本地 OS sandbox。**旧研究中“先接 LangSmith”的推荐已不作为当前主线；它的 SDK 核对可以参考，较大的生产接入修改树没有执行。**

| 方案 | 实际研究 / 尝试 | 当前判断与证据边界 |
| --- | --- | --- |
| LangSmith 托管 Sandbox | 查了官方文档、已安装 SDK 和认证配置；未创建云 VM、未上传素材 | 是可选远程执行方案，不是本地执行的前提。没有 LangSmith API key 不阻塞当前本地验证 |
| 自托管 LangSmith | 官方提供企业部署路径，涉及 Kubernetes、支持 KVM 的节点等 | 不适合作为本次个人 Mac 快速验证的起点；没有实施 |
| DeepAgents `LocalShellBackend` | 核对当前源码，命令直接在宿主执行 | 有 shell 工具但没有隔离；只设置 `cwd` / `root_dir` 不能当成本地 sandbox 已接好 |
| Codex 独立 sandbox 命令 | 核对 `../codex` 源码及已安装 CLI，并在本机完成权限与 FFmpeg 探针 | **目前唯一已实测通过的本地路线**。复用执行隔离，不进入 Codex 模型循环；适合作为下一步 PoC 入口，尚非正式产品选型 |
| Anthropic Sandbox Runtime（SRT） | 核对官方博客、独立 `srt` CLI / JS 库、权限和平台文档 | 可由 Python 子进程薄适配，不要求 Claude Agent SDK；尚未安装或实测。若产品不希望依赖 Codex CLI，可进一步验证 |
| NVIDIA OpenShell | DeepAgents 官方已提供 `OpenShellSandbox`；官方资料有本地 gateway 与 Docker / Podman / MicroVM 路线 | 已有生态适配，不必默认自研全套 backend。需要额外 runtime，尚未安装或实测；需要独立 Linux 工具链时再评估 |

当前结论是：本地进程隔离、容器、VM 都是可选实现，sandbox 不等于云 VM。Codex sandbox、Codex harness、GPT 模型是不同层，能单独复用执行隔离，同时保留 Kimi 和 DeepAgents。详细官方链接、版本差异和适配边界见 [本地 Sandbox 研究](../../research/2026-10-26-local-sandbox-deepagents.md)。

#### 2.3 已跑通的本地验证

实测宿主为原公司的 macOS，CLI 为 `codex-cli 0.163.0`；用于核对的相邻源码仓库 `../codex` checkout 为 `5eea8d0dd`。该版本的命令是 `codex sandbox [OPTIONS] -- COMMAND...`，宿主系统选择后端，不要直接照搬旧网页的 `macos` / `linux` 子命令。

macOS 后端使用 Seatbelt / `/usr/bin/sandbox-exec`；当前 Linux 源码默认 bubblewrap + seccomp，Landlock 是旧路径。独立 CLI 入口加载策略后直接启动进程，不需要进入 Agent loop。[命令入口](../../../../codex/codex-rs/cli/src/debug_sandbox.rs)、[macOS 实现](../../../../codex/codex-rs/sandboxing/src/seatbelt.rs)、[Linux 说明](../../../../codex/codex-rs/linux-sandbox/README.md)。

探针设置显式权限 profile：系统最小读取、工具和 Python runtime 只读、源视频只读、制作工作区可写、合成敏感目录不可读、关闭网络；保留 `--include-managed-config`。没有模型调用，也未使用 LangSmith 服务。

| 检查 | 结果 |
| --- | --- |
| 允许工作区内写文件并回读 | 通过 |
| 工作区外合成文件写入 | `EPERM`，宿主核对原文件未变 |
| 显式禁止的合成文件读取 | `EPERM` |
| 连接临时 localhost listener | 宿主对照连接成功，子沙箱连接 `EPERM` |
| 沙箱内 FFmpeg 处理已有视频 | 退出码 0，产物非空 |

产物为 [preview.mp4](../../../outputs/verification/local-sandbox-20261026/work/preview.mp4)：H.264、640×360、2.961292 秒，`ffprobe` 元信息和 FFmpeg 完整解码均通过。此次微型探针记录约 0.235 秒，不能外推成完整创作任务的延迟或 SLA。

踩过的两个环境问题也已解决：当前 Agent 工具本身在 Seatbelt 内，直接再嵌套会报 `sandbox_apply: Operation not permitted`；通过正常执行授权入口，由宿主启动本次显式受限的子沙箱后成功。第一次显式白名单漏了 uv Python 标准库目录，补充该 runtime 的只读权限后通过；不需要扩大到整个用户目录。遇到同类问题先核对启动层和运行时依赖，不要直接关闭隔离。

**验证边界：**这证明的是上述具体路径的权限与执行行为。Codex 仍有系统临时目录等平台基础许可，不能写成“所有工作区外目录均不可写”；localhost 拒绝也不是所有外网路径的审计。尚未验证 Kimi 自主制作、DeepAgents 文件传输、失败修复、并发隔离、取消和进程回收，以及 Chat 的作品发布。

#### 2.4 现有项目接线点

恢复工作时先读 [gateway/agent_model/deepagents.py](../../../gateway/agent_model/deepagents.py)：当前 Supervisor 后端固定为 `StateBackend()`，`FilesystemMiddleware` 只开放 `read_file`，并且丢弃传入的 `session_context`。仅安装 sandbox runtime 或换一个类名，不会自动获得可写工作区和 `execute`。

当时安装的 `deepagents 0.7.23` 中，`BaseSandbox` 实际要求 `execute()`、`upload_files()`、`download_files()` 和 `id`，其余多项文件工具可复用；官方页面“只需 execute”是简化描述，应以恢复后锁定版本的抽象接口为准。适配还要负责工作区、权限策略、超时 / 取消及文件交接，文件传输不能绕开工作区边界。

若使用 `CompositeBackend`，`execute()` 委派给 default backend；仅把 sandbox 挂到 `/workspace/` 而 default 仍是 StateBackend 不够。现有会话历史和摘要文件跟随 checkpoint 的职责应单独保留，不能因创作目录接入就随意迁走。相关代码：[会话合同](../../../gateway/agent_model/contracts.py)、[Supervisor](../../../agent/chat/supervisor.py)。以上是接线调查，没有实施生产改造。

### 3. 接下来有依据、值得继续做的事情

#### 3.1 先把可复现证据带到新 Mac

**只 clone 仓库不够：`outputs/` 被 Git 忽略；截至本次交接，本交接文件和本地 Sandbox 研究笔记本身也仍是 untracked。** 用户需要在旧电脑退还前明确保存这些未跟踪文件及所需素材。本 Agent 没有代为 commit、复制到新机或迁移凭据。

| 优先级 | 需要保存 / 恢复的内容 | 原因 |
| --- | --- | --- |
| 必需 | 本交接文件、[本地 Sandbox 研究](../../research/2026-10-26-local-sandbox-deepagents.md) | 恢复任务依据、当前结论及尚未完成的边界 |
| 必需 | 整个 `outputs/verification/local-sandbox-20261026/` | 包含 `probe.py`、实际 `command.json`、`result.json`、合成权限对照文件与 `work/preview.mp4`；Git 不会带走 |
| 必需（原样复跑） | `outputs/verification/parse-chunk-harness-ab-20261024/frozen-media/visual.mp4` | 探针使用的约 5.8 MB、60 秒输入；没有它，原探针不能复跑。也可显式更换为新测试视频并标注输入变更 |
| 按需 | `../codex` 源码，或可重新获取的 checkout `5eea8d0dd` | 用于复核实现；执行探针只需要合适版本的 CLI，不要求编译源码 |
| 历史保留 | `outputs/verification/parse-harness-kimi-20261025/`、`outputs/verification/ep04-chunk0-direct-schema-20261025/` 及其引用的冻结媒体 | 仅用于恢复原 Parse 对照，不是当前 sandbox PoC 的依赖 |

新机按仓库锁文件重建 Python 环境，安装 / 确认 Codex CLI、FFmpeg 与 ffprobe；先检查 `codex --version` 和 `codex sandbox --help`。探针是旧机验证脚本：FFmpeg 路径写为 `/opt/homebrew/bin/`，需按新机实际安装位置核对；Python runtime 路径由运行解释器推导。`command.json` 含旧机绝对路径，只是证据，不应原样用作新机生产配置。

从新机仓库根目录、能够启动 Seatbelt 的宿主终端运行：

```bash
.venv/bin/python outputs/verification/local-sandbox-20261026/probe.py
```

脚本会重写同目录的探针结果和测试产物，复跑前保留旧机结果副本。预期是 5 项检查通过，并生成可解码的视频。这个步骤不需要 LangSmith API key，也不需要 Kimi 凭据；进入模型创作验证时才通过正常配置渠道恢复模型认证，不在交接文档写密钥。实际模型配置由 `SEEKINO_MODEL_CONFIG` 或仓库 `config.yaml` 选择，恢复后需确认绑定而不是沿用失效的绝对路径。

#### 3.2 接通最小 DeepAgents 创作实验

已有证据说明本地隔离和 FFmpeg 可以工作，下一步真正未知的是模型能否使用这组能力完成创作，而不是再做一轮供应商功能表。

1. 优先以已经验证的 Codex 独立 sandbox 入口做薄适配，在独立实验入口接入 DeepAgents 的文件与 `execute` 工具。先固定一份素材、一个任务目录、一个明确交付格式；不要同时建设多供应商调度或改 Parse。
2. 用 Kimi `k3.1-pro` 跑一个小型视频 / 创作 workflow。例如根据素材生成短片及本地预览页，由模型实际写代码、执行命令、读取错误并修复。具体创作模板仍待实现；当前 FFmpeg 探针是确定性命令，不能冒充这一步已完成。
3. 记录实际工具轨迹、模型用量、墙钟耗时、失败与重试次数；宿主核对输出文件并解码 / 预览。至少验证一次执行错误能反馈给模型且正确结束，不把模型说“完成”当作交付证据。
4. 实验通过后再把能力接进现有 Supervisor / 创作 hook，处理当前只读工具白名单、会话工作区复用、超时取消、历史 checkpoint 与作品发布。验收是用户能从产品入口发起并拿到作品，普通检索仍正常；不要求同时做新编辑器或完整 VM 平台。

正式产品是否继续依赖 Codex CLI、改用独立 SRT，或需要 OpenShell / VM，尚未定案。判断依据应是目标用户安装成本、所需工具链、跨平台行为及执行边界；当前最快 PoC 与最终发行方案可以不同。如果无需 Codex CLI 的分发成为明确要求，再对 SRT 跑同样探针；如果需要统一 Linux 环境，再评估 OpenShell，避免同时引入多个变量。

### 4. 做好这个任务的经验与提醒

- **先从用户的闭环和部署约束出发。** 用户要的是 local mode 能制作作品；先围绕 LangSmith 云账号展开、再补解释本地部署，浪费了沟通。以后先确认代码实际在哪执行，再谈供应商。
- **分清模型、harness、工具后端、隔离运行时。** DeepAgents 可以继续驱动 Kimi；Codex 的独立 sandbox 可以只承担执行。生态统一不等于所有组件必须来自一个托管产品。
- **先查现成实现和本机源码。** 用户指出 `../codex` 后，很快找到独立命令并实测；Anthropic SRT 和 DeepAgents / OpenShell 也已有方案。不能把“自己还没查到”写成“本地不存在”或直接启动自研隔离层。
- **让证据分层。** 文档有能力、类能导入、静态命令成功、Agent 自主制作成功、产品闭环完成是五个不同阶段。当前到静态命令成功；后续交接必须继续标清，不能把探针通过写成创作功能已接好。
- **控制变量，保留失败。** Parse 的跨模型比较不能证明 harness 因果；本地探针遇到的嵌套 Seatbelt 和 Python 只读依赖问题应留下，而不是扩大权限后仅记录成功。
- **迁移不能只保存结论。** 输入、可执行脚本、版本、真实命令和结果要一并保留，尤其是 Git 忽略的素材；新机路径变化属于恢复步骤，不应误判为方案失效。
- **把复杂度用于下一个尚未验证的问题。** 现在该验证 DeepAgents + Kimi 能否真正交付作品，而不是继续争论 DeepAgents 是否“重”、要求 LangSmith 认证或展开整个 Parse 重构。
