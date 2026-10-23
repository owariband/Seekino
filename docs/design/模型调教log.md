# AniVAG 视频理解模型调教日志

更新时间：2026-09-13（Asia/Shanghai）

## 1. 这份日志记录什么

这份文档记录 Episode 7 第 11 个待解析 VideoChunk 从“Doubao 原生视频被审核截断”，到抽帧降级、备胎模型选型、本地 Qwen 调教、“两阶段视觉证据 → Pro 修正”实验、Qwen 直接视频/主动抽帧 A/B，以及最终选定“Qwen 视觉证据 → 火山方舟 DeepSeek 文本整理”的降级路线。

它不是模型排行榜，而是一份可复核的工程故障史。文中会明确区分：

- **事实**：由日志、JSON 产物或代码直接支持；
- **推断**：由受控实验支持，但仍不能证明唯一原因；
- **待验证**：下一步设计，尚未达到正式质量门槛。

## 2. 固定测试对象与正式状态

- Episode 7，`sequence_no=10`，即第 11 个 VideoChunk。
- Canonical VideoChunk：整集绝对时间 `[600000, 660000)` ms。
- AnalysisWindow：整集绝对时间 `[597000, 663000)` ms，片长 `66000ms`。
- Canonical VideoChunk 在窗口内的相对范围：`[3000, 63000)` ms。
- 当时按历史文件名已完成 `segment-0000`～`segment-0009`，进度为 `10/24`，累计 `258` 个正式事件。
- 当时的历史产物路径 `segments/segment-0010.json` 尚不存在。
- 本文所有第 11 个 VideoChunk 实验均写入 `diagnostics/`，都标记 `formal_checkpoint=false`，没有改写正式 Context chain。

这条边界非常重要：**Schema 能解析不等于质量过关，诊断成功也不等于正式 VideoChunk 成功。**

## 3. 第一阶段：Doubao 原生视频为什么卡住

### 3.1 完整 66 秒原生视频

最初把完整 AnalysisWindow MP4 直接交给 Doubao Pro。旧的非流式请求在生成末尾返回 HTTP 400：

```text
OutputTextSensitiveContentDetected
```

这是供应商明确返回的输出内容审核错误。现有错误里没有审核类别、命中文字区间或具体帧号。

因此可以确认：

- 请求不是因为本地 MP4 无法读取而失败；
- 模型已经进入输出生成或输出审核阶段；
- **不能确认究竟是哪张画面、哪句台词或哪段模型措辞命中了审核。**

### 3.2 把视频二分

为了缩小范围，把 66 秒窗口二分为两个 33 秒片段：

| 范围 | 结果 | 证据 |
|---|---:|---|
| `[0,33000)` | 同类 HTTP 400 | `diagnostics/segment-0010-half-0.error.json` |
| `[33000,66000)` | 成功，22 events | `diagnostics/segment-0010-half-1.parse.json` |

后半段成功结果包含：ACTION 7、DIALOGUE 12、EMOTION 1、SCENE 2，共 12 条非空 `text`。

二分说明“前半段更容易复现”，但它仍不能定位某一帧。二分产物也不能直接拼成正式 VideoChunk，因为那会破坏完整 AnalysisWindow、跨边界理解和事件唯一归属语义。

### 3.3 改成 SSE 后看到了什么

随后用相同 Pro、相同 Prompt 对前半段做 SSE 重放：

- 收到 `2358` 个 `response.output_text.delta`；
- 收到 `response.completed`；
- 拼出 `4932` 字符完整 JSON；
- 成功输出里仍包含此前怀疑过的“乐奈，你几岁开始弹吉他的”相关内容。

这证明该句不是一个稳定、必现的触发器。SSE 的价值是让 Gateway 知道：模型输出了多少字符、在哪种终止状态结束、是否拿到了 response id。SSE **不会绕过审核**，供应商也没有通过 SSE 返回精确的违规帧、类别或文本 span。

受控推断：失败更可能与当次输出措辞、审核时机或审核非确定性有关；证据不足以把原因归到某张动画画面。

## 4. 第二阶段：继续使用 Doubao，但把视频改成有序抽帧

### 4.1 15 帧第一次尝试

- 15 张均匀帧，时间为 `2200, 6600, ... 63800ms`；
- 耗时 `188.929s`；
- 模型确实返回了内容，但有 13 个事件出现 `end_ms <= start_ms`；
- Pydantic 拒绝，未形成可用 Schema。

证据：`diagnostics/segment-0010.frames-15.error.json`。

### 4.2 15 帧 v2

调整帧序列 Prompt 与时间约束后，以相同 15 帧重试：

- 耗时 `172.710s`；
- Schema 成功；
- 15 个 observed events，14 个 midpoint-owned events；
- ACTION 5、DIALOGUE 7、EMOTION 1、SCENE 2；
- 8 条非空 `text`。

它识别出了“对不起”“那个……”“都弹什么样的乐曲呢”“好厉害呀”“不记得了”“Live 呢。谢谢你，小灯。”“能开始了吗”“爱音！”等字幕/对白候选。

证据：`diagnostics/segment-0010.frames-15-v2.parse.json`。

结论：显式有序多图可以作为 Doubao 原生视频审核故障的可运行降级形式。但单轮 15 帧只能证明“能跑、能得到较好的结构”，不能证明召回率和时间精度已经达到正式质量门槛。

### 4.3 把密度提高到 33 帧 / 0.5 FPS

为了减少稀疏采样漏事件，增加到 33 张图：

- 帧时间为 `1000, 3000, ... 65000ms`；
- 耗时 `361.989s`；
- Ark SSE 已生成 `4094` 字符；
- 随后以 `content_filter` / `OutputTextSensitiveContentDetected` 终止；
- Schema 未完成，正式 checkpoint 未写。

证据：`diagnostics/segment-0010.frames-33.error.json`。

这一次确认了审核不是“图片一提交就拒绝”，而是在已经生成大量结构化文本之后终止。但它仍没有告诉我们是哪一帧、哪一类或哪段文字触发。

这一阶段的矛盾是：

- 15 帧比较容易过审，但视觉召回可能不够；
- 增加到 33 帧后证据更密，审核又在输出末端截断；
- 单纯调 FPS 无法同时保证质量和过审。

## 5. 为什么开始调查 VideoRAG、MiniCPM 与 Qwen

### 5.1 VideoRAG 实际做了什么

VideoRAG 的视觉阶段也不是把完整 MP4 原样交给一个模型后直接得到最终答案：

- 视频先按 30 秒分段；
- rough caption 每段抽 5 帧；
- fine caption 每段固定抽 15 帧；
- MiniCPM-V 2.6 负责对帧序列做 caption；
- transcript 同时进入上层流程；
- ImageBind 生成视觉向量；
- GPT-4o / mini 等文本模型再负责实体、图谱和问答。

所以 VideoRAG 的经验真正指向的是“**短视觉上下文 + 分阶段处理**”，而不是“MiniCPM 一步生成严格毫秒级 Timeline”。它的目标是检索与问答，也与 AniVAG 的最终事件 Schema 不同。

### 5.2 MiniCPM 为什么没有立刻照搬

VideoRAG 原代码使用的 MiniCPM-V 2.6 BNB NF4/CUDA 路径不能原样搬到当前 Mac。后续已经改用 GGUF Q4_K_M + Ollama/Metal 完成同条件 A/B；它虽然更快，但发生严重跨帧涂抹和重复，质量明显低于 Qwen。模型随后按用户要求从本机删除，诊断结果仍保留，详见第 13 节。

### 5.3 为什么先装 Qwen3-VL-8B

当前 M5 Pro、48GB 统一内存，已安装：

- Qwen3-VL-8B Instruct `Q4_K_M`；
- `Q8_0` 视觉 projector；
- 本地文件合计约 `5.78GB`；
- 通过 Ollama/Metal 运行，模型名为 `anivag-qwen3-vl:8b-q4-64k`。

选择它是因为它已有多图、OCR、anime 与时序能力信号，而且当时不需要继续下载。

### 5.4 “能不能直接传 MP4”不是 Mac 能力问题

| 运行方式 | 应用能否直接传 MP4 | 本项目状态 |
|---|---:|---|
| Ollama + 当前 Q4 GGUF | 否 | 必须由 Engine 抽帧后传图片 |
| Transformers + qwen-vl-utils | 是 | 需要另一套 BF16/PyTorch 依赖，未安装 |
| upstream llama.cpp server + GGUF | 是 | 已安装 `llama.cpp 0.4.0` 并完成第 11 个 VideoChunk 真实 A/B |

macOS/Apple Silicon 并不缺 Unix 或 GPU 支持。差异来自运行时有没有接好视频解码、采样与 metadata 预处理。当前 Ollama 内置 llama-server 是 `MTMD_VIDEO=OFF`，所以这是包装层能力差异，不是“只有 Windows/Linux 能跑”。

项目已经为 `llama_cpp` direct-video 路径接好 Gateway 接口，并完成真实 MP4 调用。受控 A/B 证明它没有优于 Engine 主动抽帧，还暴露了跨视频 prompt cache 污染与短尾段失控生成问题，因此不选作正式 fallback 输入方式，详见第 14 节。

## 6. Gateway / Engine 分层调整

这次排查也暴露了命名与职责问题，最后收敛为：

- Gateway 只封装模型调用协议：Ark、Ollama、llama.cpp；
- 模型地址、端口和默认模型放 `config.yaml` / 环境变量，不散落硬编码在业务算法中；
- Engine 负责 VideoChunk 证据解析、抽帧、Prompt、回退编排、时间映射、去重和 Context；
- 上游已经生成的 VideoChunk 属于业务事实，Engine 不重复切分；诊断实验需要的 30 秒分组只是模型输入分组，不改变 Canonical VideoChunk。未来断点续传的 `Segment` 只表示字节范围，也不参与这条时间轴。

当前主要落点：

```text
packages/gateway/src/gateway/models.py
packages/gateway/src/gateway/config.py
engine/parse/video_chunk_parse.py
engine/parse/video_parse.py
engine/parse/prompts.py
engine/parse/schemas.py
```

## 7. 本地 Qwen 调教过程

### 7.1 33 张图一次性发完

第一次把已有 33 张、0.5 FPS 图片一次性送给 Qwen：

- 耗时 `332.354s`；
- Schema 首次成功；
- 34 events 全是 SCENE；
- DIALOGUE 0，非空 `text` 0；
- 只有 14 种唯一 description，多余精确重复 20 条。

它把前十几秒机械复读成“乐队表演/准备”，漏掉大量硬字幕、对话、观众与喝水等事件。这条结果不能进入正式 checkpoint。

证据：`diagnostics/segment-0010.qwen3-vl-8b-q4.frames-33.parse.json`。

事实结论：本地模型绕过了云端供应商审核，且 33 图能在 64K 上下文内返回合法 JSON；但 **Schema 成功 ≠ 语义可用**。

### 7.2 严格复刻 VideoRAG fine：15 × 3

VideoRAG fine 的真实口径是每个 30 秒输入组固定 15 帧。66 秒窗口因此拆为：

- `[0,30000)`：15 帧，0、2、……、28 秒；
- `[30000,60000)`：15 帧，30、32、……、58 秒；
- `[60000,66000)`：15 帧，60、60.4、……、65.6 秒。

尾部 6 秒仍固定 15 帧，等效密度为 2.5 FPS。三组是三次独立 Qwen 请求，不是 45 图一次发完。

结果：

| 指标 | 33 图单请求 | VideoRAG 15×3 |
|---|---:|---:|
| 总图片 | 33 | 45 |
| wall time | 332.354s | 219.354s |
| events | 34 | 37 |
| DIALOGUE | 0 | 14 |
| 非空 text | 0 | 14 |
| 唯一 description | 14 | 31 |
| 精确重复 surplus | 20 | 6 |

三组各耗时 `76.717 / 75.808 / 64.413s`，全部首次通过 Schema。虽然总图片更多，墙钟反而约快 34%。字幕/OCR 恢复明显，包括“好好弹”“乐奈你呢”“我在很多地方弹奏”“谢谢你，小灯”“不好意思，大家久等了”“加油”和歌词等。

但最终 Timeline 仍有严重问题：

- 人物身份和说话者错配，把爱音大量认成乐奈；
- 前 14 秒仍有复读；
- 把台下观众台词归给台上角色；
- 场所判断错误；
- `[58000,60000)` 出现信息空洞；
- 尾段 umbrella event 与细事件重叠；
- 个别描述自相矛盾。

证据：

- `diagnostics/segment-0010.qwen3-vl-8b-q4.videorag-fine-groups.parse.json`
- `diagnostics/segment-0010-videorag-fine-groups/`

受控推断：在当前 Qwen 8B Q4 + Ollama + Prompt 下，缩短单请求视觉上下文是质量恢复的重要变量。它不能证明模型本体优于或劣于尚未实测的 MiniCPM。

### 7.3 把完整业务 Context 再塞回 Qwen

下一轮保持相同 15×3 图片，却把正式 SeriesBible、窗口规则、历史 Context 和完整业务 Prompt 交给 Qwen：

- 第 1 组 `29.651s`，Schema 成功，但 15 events 全为 SCENE、0 text、只有 2 种唯一 description；
- 第 2 组 `29.943s`，输出出现非法时间区间，Pydantic 拒绝；
- 按“一次明确失败即停止”，第 3 组没有调用。

证据：`diagnostics/segment-0010-videorag-fine-groups-business-context-v2/`。

事实结论：为 Ark/Doubao 设计的完整业务 Prompt/Context 不能原样套给当前 8B Q4 本地路径。

合理推断：人物资料、历史 Context、严格时间线、证据规则和最终 Schema 同时压给小模型，超过了它在当前调用形态下的有效指令承载能力。现有实验不能精确分离是哪一段 Prompt 单独导致退化。

## 8. 两阶段实验：Qwen 负责“看见”，Doubao Pro 负责“理解与归一”

### 8.1 假设

为了降低 Qwen 的上下文负担，本轮不再让它生成最终 `VideoTimeline`。它只能输出：

- 支撑观察的 Frame 编号；
- 不含真名的外观、服装、乐器和站位描述；
- 当前帧直接可见的动作或状态；
- 原样硬字幕；
- 只有画面证据明确时才给匿名 `visible_speaker` 候选。

Qwen 不接收 SeriesBible、历史 Context、候选 ASR、Canonical ownership 规则，也不计算毫秒或 event type。Engine 根据 Frame 编号做确定性时间映射。Doubao Pro 第二阶段只接收文本化观察、空候选 ASR、SeriesBible 与前一个 VideoChunk 的 Context，再输出既有 `VideoTimeline`；它不接收 MP4 或图片。

诊断脚本：`/private/tmp/anivag_qwen_neutral_doubao_refine_segment0010.py`。

### 8.2 Qwen 中性视觉阶段结果

仍采用 VideoRAG 15×3 分组：

| 组 | 范围 | 耗时 | observations | 结果 |
|---|---:|---:|---:|---|
| 0 | `[0,30000)` | 215.230s | 15 | Schema 首次成功 |
| 1 | `[30000,60000)` | 184.656s | 14 | Schema 首次成功 |
| 2 | `[60000,66000)` | 163.509s | 15 | Schema 首次成功 |
| 合计 | 66 秒 | 563.395s | 44 | 全部落盘 |

中间证据质量统计：

- 44 条观察；
- 19 条非空 `hard_subtitle`；
- 28 种唯一 `visual_fact`；
- 16 条重复事实 surplus；
- 绝大多数 subjects 保持匿名；
- 1 条把字幕中的“小灯”泄漏进 `visual_fact`；
- 仍会写“字幕显示其正在讲述一段回忆”等超出纯视觉证据的推断；
- 对 `visible_speaker` 仍明显过度自信；
- 尽管 Prompt 要求合并相邻相同事实，它仍接近逐帧输出，尾段重复尤其明显。

阶段性结论：

- **成立**：移除身份资料、毫秒计算、event type 和最终 Timeline 负担后，三组 Schema 稳定性恢复，字幕/OCR 召回也比 Qwen 直接输出最终 Timeline 更好；
- **不成立**：这并没有让 8B Q4 自动变成可靠证据抽取器，重复、过度说话者归因和轻度推断仍需后处理或更强模型修正。

证据：`diagnostics/segment-0010-qwen-neutral-then-doubao-pro/group-00.neutral.json`～`group-02.neutral.json`，以及 `merged-neutral-evidence.json`。

### 8.3 Doubao Pro 纯文本修正结果

第二阶段确认没有发送视频或图片，只发送：

- 上述 44 条文本化中性观察与 Engine 时间映射；
- 空候选 ASR；
- SeriesBible runtime context；
- `contextBeforeCompact`；
- 最终 `VideoTimeline` JSON Schema。

结果：

- 运行 `332.527s`；
- 已产生 `3443` 个 SSE partial output characters；
- 随后仍以 `content_filter` / `OutputTextSensitiveContentDetected` 终止；
- 最终 Schema 未完成；
- 没有重试；
- 没有按当时的历史文件名写出 `segment-0010.json`。

证据：`diagnostics/segment-0010-qwen-neutral-then-doubao-pro/doubao-refiner.error.json`。

三次 Qwen 与一次 Pro 的模型阶段累计耗时约 `895.922s`，即约 14 分 56 秒。

这是本轮最重要的新证据：**把视频改写成文本候选并不能保证 Doubao Pro 通过输出审核。** 问题不只可能来自输入画面；候选字幕、最终事件叙述或模型生成措辞本身也可能触发审核。由于 Ark 仍没有返回类别、文本 span 或最终 partial JSON，本轮依然不能把错误精确归因到某一句。此前 SSE 同内容又曾成功，因此也不能把某条台词写成稳定触发器。

### 8.4 两阶段策略的准确结论

这轮不是全盘失败，也不是已经跑通：

- Qwen 的任务拆分方向有效：中间 Schema 稳定，OCR 增加，身份胡猜减少；
- Qwen 中间结果仍需去重、限制推断与校验说话者；
- Doubao 文本修正没有绕开原来的审核问题，因此没有最终质量结果；
- 当前不能把两阶段实现直接接入生产 fallback；
- 所有 Qwen 中间产物都已保存，下一种 refiner 可以直接复用，不必再次耗费 9 分多钟重跑本地视觉阶段。

## 9. 降级路径标记：不用 `fallfail`，使用 `is_fallback`

用户需要的是：降级虽然完成，但质量明显下降，未来有更好模型时应进入“重新生成”队列。`fallfail` 容易被理解成“fallback 自己失败”，与真实语义相反。

最终字段为：

```python
is_fallback: bool = False
```

它放在 `VideoChunkParseResult`，而不是 Gateway、`VideoParseProvenance` 或 `VideoChunkCheckpoint` 顶层：

- 它直接表达整个 VideoChunk 是否由降级路径生成；
- 会随 VideoChunk 进入 checkpoint 和最终 `VideoParseResult`；
- provenance 继续只记录“由谁、用什么输入、为什么 fallback”；
- 不提前引入没有客观评分体系的 `quality_status` 枚举。

当前自动设置规则：

```python
is_fallback = (
    evidence.provenance is not None
    and evidence.provenance.fallback_reason is not None
)
```

不根据 `provider != "doubao"` 判断，否则主动进行的本地模型 A/B 也会被误标。原因已经由 `video_parse.fallback_reason` 和 `primary_*` 诊断字段保存，不重复增加 reason 字段。

向后兼容：

- 字段是非 Optional 的 `bool = False`；
- 旧 checkpoint 缺少字段时，Pydantic 自动补 `False`；
- 曾使用 `needs_regeneration` 的 checkpoint 会在读取时迁移为 `is_fallback`，新序列化结果只写新字段；
- 新 fallback checkpoint 会显式持久化 `true`；
- 不通过校验器强制绑定 provenance，未来人工验收或成功重生成后可以显式清零。

实现文件：

- `engine/parse/schemas.py`
- `engine/parse/video_parse.py`
- `tests/engine/test_parse_pipeline.py`

Engine 当前测试：`16 passed`。

未来真正做 regenerate 功能时还有一个重要边界：当前 CLI 把连续存在的 checkpoint 视为 completed。新字段本身不会自动触发重跑。若修复某个中间 VideoChunk 会改变 Context，应从最早被修复的 VideoChunk 起重建后续 Context chain，不能静默覆盖单个 JSON 后继续复用旧上下文。

## 10. 全部实验总表

| 路径 | 输入策略 | 耗时 | 结构结果 | 主要问题 |
|---|---|---:|---|---|
| Doubao 原生 66s | MP4 | 未完整保存 | 审核失败 | 无类别/span/frame |
| Doubao 前半 33s | MP4 | — | 审核失败；后续 SSE 重放又成功 | 审核不稳定 |
| Doubao 后半 33s | MP4 | — | 22 events | 只适合诊断，不能拼成正式 VideoChunk |
| Doubao 15 帧 v1 | ordered images | 188.929s | Schema 失败 | 13 个非法时间区间 |
| Doubao 15 帧 v2 | ordered images | 172.710s | 15 observed / 14 owned | 召回与时间精度未达门槛 |
| Doubao 33 帧 | ordered images | 361.989s | 4094 chars 后审核失败 | 提高密度再次被截断 |
| Qwen 33 图单请求 | ordered images | 332.354s | 34 SCENE，0 DIALOGUE | 语义塌缩、20 条重复 |
| Qwen VideoRAG 15×3 | grouped images | 219.354s wall | 37 events，14 DIALOGUE | 身份、说话者、时序仍错 |
| Qwen 15×3 + 完整业务 Context | grouped images | 59.594s 后停止 | 第 1 组退化，第 2 组 Schema 失败 | 小模型被复杂任务压垮 |
| Qwen 中性证据 15×3 | grouped images | 563.395s sum | 44 observations，19 字幕 | 16 条重复、过度归因 |
| MiniCPM-V 2.6 中性证据 15×3 | grouped images | 150.429s sum | 48 observations，30 个非空字幕 | 仅 9 种视觉事实、6 种字幕；39 条重复 surplus，跨帧涂抹严重 |
| Doubao 文本修正 | text evidence only | 332.527s | 3443 chars 后审核失败 | 两阶段仍未得到最终 Schema |
| Qwen/llama.cpp 主动抽帧 15×3 | grouped images | 191.376s | 2/3 组 Schema 成功 | 类型错分；6 秒尾组 Schema 失败 |
| Qwen/llama.cpp 直接视频 15×3 | grouped MP4 | 425.177s | 2/3 组 Schema 成功 | 中段复读；尾组耗尽 8192 tokens；默认 prompt cache 会串视频 |

## 11. 当前工程结论

一路走下来，边界已经比最初清楚得多：

1. Doubao Pro 不是“看不懂动画”，核心阻塞是原生视频、高密多图乃至纯文本修正都出现过不可解释的输出审核终止。
2. SSE 提升了可观测性，但只知道终止阶段、response id 和 partial output 长度；它没有定位具体违规画面。
3. 本地 Qwen 没有供应商审核问题，却暴露了小模型的注意力、指令承载、身份消歧和时间线生成问题。
4. 照 VideoRAG 分组是有效优化；真正起作用的是缩短单请求视觉上下文，而不是一个神奇的全局 FPS 数字。
5. Qwen 更适合作为 OCR / 高召回候选证据提取器，不适合在当前 8B Q4 形态下一步生成正式 Timeline。
6. “Qwen 只负责看见、强模型负责理解、Engine 负责证据与时间”仍是合理架构方向，但当前 Doubao 不能充当稳定 refiner，因为纯文本阶段也会被审核截断。
7. 在找到稳定 refiner 或明确人工验收前，任何这类降级产物都应保留 `is_fallback=true`，不伪装成与主模型同质量。
8. Qwen 的直接 MP4 路径没有带来整体质量收益；正式 fallback 选择 Engine 可控抽帧，避免把采样和跨请求缓存正确性交给当前 llama.cpp video wrapper。
9. 文本 refiner 正式改选用户已在火山方舟开通的 `deepseek-v4-flash-ga-260731`。该选择已经写入配置，但真实第 11 个 VideoChunk 调用尚未执行，不能提前宣称质量通过。

这段劳累之旅可以压缩成一句话：

> 我们先被一个不告诉命中画面、类别或文本位置的审核错误卡在第 11 个 VideoChunk；把视频二分、换成 SSE、从 15 帧加到 33 帧，终于确认审核并不稳定，却仍不知道它究竟看到了什么。换成本地 Qwen 后审核消失了，质量却塌了；再照 VideoRAG 拆成 15×3，字幕和事件活了过来，人物身份又串了；把人物资料塞回去，小模型又被上下文压垮；最后让 Qwen 只负责看、Pro 只负责修，Qwen 稳住了，Pro 却连纯文本都再次被审核截断。

## 12. 已选路线的实施门槛

路线已经从“继续枚举模型”收敛为第 15 节的 Qwen + DeepSeek fallback，但以下事实必须在接入时守住：

1. 先复用已经落盘的 Qwen 中性观察验证 DeepSeek refiner，不重跑视觉阶段；
2. DeepSeek 只接收文字证据、`contextBeforeCompact`、SeriesBible、候选 ASR 与时间边界，不再接收 MP4 或图片；
3. SeriesBible 和历史 Compact 只能消歧，不能独立创造当前 VideoChunk 事件；
4. DeepSeek 返回结果必须经过 Schema、时间范围、重复率和事件归属校验；未过质量门不得写正式 checkpoint；
5. 成功写入的降级结果必须保留完整 provenance，并设 `is_fallback=true`；DeepSeek 自身失败时只保存诊断，不生成伪成功 VideoChunk；
6. 首次第 11 个 VideoChunk 验证仍执行“一次明确失败即停止”，不静默重试，不把换供应商写成“必然无审核”。

## 13. MiniCPM-V 2.6：VideoRAG 底模并没有救下第 11 个 VideoChunk

2026-09-13 已按上一节第 5 项完成 A/B。下载的是适合当前 Mac/Ollama 的官方 GGUF，
不是 VideoRAG 原仓库的 CUDA/bitsandbytes INT4：

```text
hf.co/openbmb/MiniCPM-V-2_6-gguf:Q4_K_M
```

下载与运行资源：

- Ollama 模型磁盘大小 `5.7 GB`；测试后磁盘仍有约 `503 GiB` 可用；
- 模型是 Q4_K_M、7.61B，视觉 projector 约 504M，context 32,768；
- 15 图请求期间 `ollama ps` 显示已加载大小 `6.7 GB`、`100% GPU` 层放置；
- 当前 48GB M5 Pro 扛得住，且不需要换 Windows/Linux；`100% GPU` 是模型层放置，
  不是瞬时 GPU 利用率或“吃满整机内存”。

测试没有加入人物资料、历史 Context 或 ASR，复用 Qwen 中性证据实验的三组 15 帧、同一
匿名证据 Schema 与同类 Prompt。三组都第一次返回合法结构：

| 组 | 范围 | 耗时 | observations | 非空字幕 |
| --- | ---: | ---: | ---: | ---: |
| 0 | `[0,30000)` | 46.235s | 16 | 13 |
| 1 | `[30000,60000)` | 58.698s | 16 | 16 |
| 2 | `[60000,66000)` | 45.496s | 16 | 1 |
| 合计 | 66 秒 | 150.429s | 48 | 30 |

速度很漂亮：相对 Qwen 中性证据的 563.395 秒快约 3.75 倍。可惜内容明显更差：

- 48 条 observation 只有 9 种唯一 `visual_fact`，重复 surplus 达 39；
- 30 个非空 `hard_subtitle` 只有 6 种唯一文本，所谓高字幕数主要是把同一句复制到很多帧；
- 三组都对 Frame 15 多生成一次 observation；
- group 0 明明发生舞台全景、人物近景、麦克风和水瓶等镜头变化，却把 16 条视觉事实全部
  写成同一句“观众观看乐队表演”；
- group 0/1 丢掉双语字幕的日文部分，group 2 又把歌词“肩落”错成“肢落”；
- group 1 对全部 16 条都填写说话者，并把不同人物和镜头压成少数模板。

作为对照，Qwen 的 44 条 observation 有 28 种唯一事实、18 种唯一字幕，重复 surplus 为
16。Qwen 当然仍有身份错认、过度说话者归因和轻度推断，但在这次公平测试中，视觉变化和
字幕信息量都显著优于 MiniCPM。

这次结果解释了“为什么 VideoRAG 选它，我们却觉得它很废物”：VideoRAG 让 MiniCPM 生成
召回候选窗口的粗/精 caption，链路里还有 transcript、ImageBind 和文本 LLM；AniVAG 此处却
要求它从 15 帧中抽出不串帧、可追溯的细粒度证据。两种任务的质量门槛不同，不能从
VideoRAG 的工程选型推出 MiniCPM 比 Qwen 更适合我们的 Schema。

结论：MiniCPM 只作为已测低成本基线保留实验记录，不接入生产 fallback，也不写正式 checkpoint；其本地模型文件已经按用户要求删除。本次没有调用 Doubao、没有模型失败重试。证据位于：

```text
packages/engine/outputs/ep07-full-60s/diagnostics/segment-0010-minicpmv26-neutral-videorag-fine-groups/
packages/engine/outputs/ep07-full-60s/diagnostics/segment-0010.minicpm-v2.6-q4.neutral-videorag-fine-groups.parse.json
```

## 14. Qwen 主动抽帧与直接视频 A/B

为了只比较输入方式，使用同一份 Qwen3-VL-8B Q4、同一个 llama.cpp runtime、同一中性
`VideoTimeline` Prompt/Schema，不提供人物资料、历史 Context、候选 ASR 或音轨。66 秒窗口两组
都按 `[0,30s)`、`[30,60s)`、`[60,66s)` 串行调用；前两组 0.5 FPS，尾组 2.5 FPS，
从而让每次请求都具有 15 个视觉样本。先完整运行主动抽帧组，再完整运行直接 MP4 组。

| 子组 | 主动抽帧 | 直接 MP4 |
| --- | --- | --- |
| `[0,30s)` | 65.026s；10 events；Schema 成功 | 61.640s；11 events；Schema 成功 |
| `[30,60s)` | 72.430s；14 events；Schema 成功 | 69.241s；15 events；Schema 成功 |
| `[60,66s)` | 53.920s；Schema 失败 | 294.296s；耗尽 8192 输出 tokens |
| 合计 | 191.376s；2/3 成功 | 425.177s；2/3 成功 |

质量不能只看 event 数：

- 第一组直接视频的 DIALOGUE 类型比抽帧正常，但仍有说话者、乐器和时间归属误判；
- 第二组抽帧能覆盖喝水、上台、观众应援、眼睛特写与乐器准备等变化；直接视频却把多句字幕
  挤进最初 2 秒，随后连续 14 次输出“小灯”，15 个事件只剩 3 种描述；
- 尾组两边都失败，说明对 6 秒短尾段强行固定 15 帧会制造大量近重复视觉输入；直接视频还
  进入长输出循环，成本明显更差；
- 两个 30 秒成功组的 prompt tokens 都约 14.3K，直接传 MP4 没有节省模型视觉上下文；
  llama.cpp 的所谓视频输入本质仍是服务端解码、抽帧和注入时间信息。

还发现一个独立的正确性问题：llama.cpp 默认 prompt cache 曾把两个不同 MP4 判断为相同
前缀，第二次请求复用 14,320 个旧 token，只重新计算 1 token，结果与上一个输入窗口逐字段相同。
关闭 `--cache-prompt` 后才得到真正的第二个输入窗口结果。因此任何 direct-video 实验都必须使用
`--no-cache-prompt` 或全新进程；污染结果不计入上表。

正式策略不照抄“每组永远 15 帧”，而是保留 VideoRAG 的短上下文收益，采用：

```text
模型输入子组时长 <= 30 秒
frame_fps = 0.5
max_frames = 15
```

所以 66 秒窗口应得到 `15 + 15 + 3` 帧，而不是对最后 6 秒强行升到 2.5 FPS。这里的子组
只是一次模型调用的输入分组，不改变上游生成的 Canonical VideoChunk，也不新增生产切片身份。

证据目录：

```text
packages/engine/outputs/ep07-full-60s/diagnostics/segment-0010-qwen3vl8b-input-ab/
```

## 15. 正式选择：Qwen 视觉 fallback + 火山方舟 DeepSeek 文本 refiner

### 15.1 决策

保留 Doubao Pro 原生视频作为主路径。只有它明确返回 `ContentFilterError` 时，才进入一次
受控降级：

```text
Doubao Pro 原生视频
  -> 明确 content_filter
  -> AnalysisWindow 按 <=30 秒分为模型输入子组
  -> Engine 以 0.5 FPS、每组最多 15 帧主动抽帧
  -> 本地 Qwen3-VL-8B Q4 提取匿名视觉事实、硬字幕和说话者候选
  -> Engine 根据帧时间确定性映射、去重并保留证据
  -> 火山方舟 DeepSeek V4 Flash 结合当前证据、contextBeforeCompact、SeriesBible 和候选 ASR
  -> 输出既有 VideoTimeline Schema
  -> Engine 校验、换算绝对时间、执行 midpoint ownership 与保守去重
  -> 写入 checkpoint，并标记 is_fallback=true
```

Qwen 被正式选为视觉 fallback，不是因为它已经能独立生成高质量最终 Timeline，而是因为
同条件下它比 MiniCPM 保留了更多视觉变化和字幕信息，并且 Engine 主动抽帧比 llama.cpp
直接视频更可控。Qwen 阶段仍坚持不给 SeriesBible、历史 Compact 和人物真名，避免把 8B
模型重新压回已验证会退化的复杂任务。

DeepSeek 是 fallback 内部的第二阶段文本 refiner，不是另一个视觉模型。它必须把 Qwen
内容视为待核验证据，把 SeriesBible 与 `contextBeforeCompact` 视为消歧材料；不能仅凭背景
资料补造当前画面中不存在的事件，身份不充分时仍保留匿名外观描述。

### 15.2 火山方舟模型配置

用户已经在火山方舟开通 DeepSeek 支持，使用其 OpenAI-compatible 地址和明确模型 ID：

```yaml
video_parser:
  fallback_refiner:
    provider: ark
    model: deepseek-v4-flash-ga-260731
    base_url: https://ark.cn-beijing.volces.com/api/v3
    api_key_env: ARK_API_KEY
    max_output_tokens: 8192
    request_timeout_seconds: 1200
```

配置只保存环境变量名称，不保存 API Key。该配置已能被 `GatewayConfig` 严格加载；截至本次
记录，Engine 尚未消费 `fallback_refiner`，也尚未对 DeepSeek 发起第 11 个 VideoChunk 真实请求。
因此当前准确状态是“架构与模型已选、配置已登记、调用链待实现和验证”，不是“DeepSeek
fallback 已经跑通”。

### 15.3 `is_fallback` 语义

即使 DeepSeek 成功整理并通过基础质量门，这条结果仍来自主模型审核失败后的降级路径，
因此必须满足：

```text
video_parse.fallback_reason = content_filter
is_fallback = true
```

同时保存 Doubao 的错误 code/response id/partial output 长度、Qwen 模型与抽帧参数、DeepSeek
模型 ID 和两阶段耗时。不能仅把最终 provider 写成 DeepSeek 后丢失“为何降级、视觉证据由谁
生成”的链路。未来更强模型重生成成功后，再显式清除 `is_fallback` 并从该 VideoChunk
起重建后续 Context chain。

## 16. 路线再次收敛：官方 DeepSeek Flash 同时负责视觉证据与最终整理

第 15 节是当时的阶段性决定，随后已被真实 A/B 覆盖。用户开通官方 DeepSeek
`deepseek-flash` 后，我们对同一个第 11 个 VideoChunk 完整跑通：

```text
Lite ASR
  -> AnalysisWindow 按 <=30 秒分组
  -> 0.5 FPS、每组最多 15 帧
  -> 官方 DeepSeek Flash 匿名视觉证据
  -> Engine 映射帧编号到毫秒、相邻精确去重
  -> 同一个 DeepSeek Flash 结合 Lite、SeriesBible、contextBeforeCompact 整理
  -> VideoTimeline
```

66 秒窗口实际使用 `15 + 15 + 3 = 33` 帧。真实诊断结果：

| 阶段 | 耗时 | 结果 |
|---|---:|---|
| Doubao Lite ASR | 41.154s | 20 cues |
| DeepSeek 三组视觉取证 | 12.738s | 28 个合并后观察、38 条唯一字幕行 |
| DeepSeek 文本整理 | 6.612s | 26 events，其中 20 DIALOGUE |
| 端到端 | 约 60.857s | Schema、排序、范围与对白证据检查通过 |

它相对本地 Qwen 视觉阶段约 415 秒的实测明显更实用，但不能把速度误写成“视觉质量全面
更强”。人工复核仍发现：个别显著字幕没有进入最终 Timeline、50～54 秒部分视觉观察被
整理阶段舍弃、人物归一仍不稳定，原始视觉阶段也出现过把水瓶/泵头识别成其他物体的错误。
因此这条链仍是 fallback，不是与 Pro 等价的主路径。

正式配置最终统一为一个 `video_parser.fallback`：抽帧策略在 Engine，模型 endpoint、Key
环境变量名和请求参数在 Gateway config；视觉与 refiner 共用官方
`https://api.deepseek.com` 的 `deepseek-flash`。此前分散的 `local_fallback`、
`deepseek_vision` 和 `fallback_refiner` 配置已删除。Ollama/llama.cpp adapter 仍保留作
A/B 工具，但不再由正式 `parse-video` CLI 选择。

生产实现遵守以下边界：

- 只有 Doubao Pro 明确抛出 `ContentFilterError` 才进入一次 fallback；网络、Schema 或
  DeepSeek 自身错误不会触发循环重试；
- 主任务可继续使用 `--asr-mode none`；fallback 被触发后才为当前窗口按需跑 Lite，已有
  非空 Lite 结果则复用；
- 视觉请求只接收当前组图片和 Frame 编号，明确隔离 ASR、人物资料与历史 Compact；
- 最终 refiner 才接收 Lite、映射后的视觉证据、SeriesBible 和
  `contextBeforeCompact`；
- DeepSeek 产生不属于 Lite/硬字幕证据集合的非空对白，或事件超出窗口边界时，Engine
  拒绝写 checkpoint；
- provenance 保存 Lite、视觉、refiner 模型，完整抽帧时间、分组帧数、分组时长与每组
  上限；恢复时参数不一致会拒绝混用旧 checkpoint；
- 成功降级的 VideoChunk 由 `fallback_reason=content_filter` 自动得到
  `is_fallback=true`。

真实诊断证据位于：

```text
packages/engine/outputs/ep07-full-60s/diagnostics/segment-0010-lite-asr-deepseek-flash-full-fallback/
```

## 17. 第七集正式整集续跑结果

2026-09-13 使用上述生产代码从已有 `10/24` checkpoint 继续跑第七集。恢复保持
`asr-mode=none`，因此不重算前十个 VideoChunk；若 Pro 失败，才在 fallback 内部按需调用 Lite。

最意外但重要的结果是：曾经多次被审核卡住的第 11 个 VideoChunk，这一次 Doubao Pro 原生视频请求
直接成功，输出 26 个归属事件和 20 条对白。因为没有明确 `ContentFilterError`，生产规则
正确地没有调用 DeepSeek，也没有用降级诊断结果覆盖质量更高的 Pro 结果。这再次证明供应商
审核具有非确定性；我们仍不知道此前究竟命中了哪一帧或哪段生成文本。

整集最终结果：

| 指标 | 结果 |
|---|---:|
| Canonical VideoChunk | 24/24 |
| 总时长 | 1,420,108 ms |
| 最终 owned events | 468 |
| DIALOGUE / ACTION / SCENE / EMOTION / OTHER | 272 / 119 / 50 / 26 / 1 |
| observed / overlap 不归属 / owned 去重 | 534 / 65 / 1 |
| `is_fallback=true` | 0 个 VideoChunk |
| 全局排序、时间范围、midpoint ownership | 全部通过 |
| ContextSnapshot 按 VideoChunk 交接 | 24 个连续 |
| 完全相同的事件键 | 0 组 |

前十个 VideoChunk 来自早期 checkpoint，尚未按 VideoChunk 持久化 `video_parse` provenance；后 14 个都明确记录
`provider=doubao`。这不影响事件时间线和 Context 链，但做供应商统计时不能把前十个的
`provider=None` 擅自算成新的模型调用。

最终文件：

```text
packages/engine/outputs/ep07-full-60s/pipeline.parse.json
```

这次运行证明的是“整集解析、断点续跑、Context 传递、边界归属与 checkpoint”完整跑通；
它还不能证明检索召回有效。当前仓库只有单条 video/query embedding adapter，尚未实现
`VideoChunkRetrievalDocument` 构造、双路 TopK、RRF、CandidateVerifier，也没有独立人工标注的
`query -> relevant time range` 金标。直接从当前解析结果反向生成问题与标签会形成自证循环，
不能称为召回评测。下一步应先准备独立人工 query fixture，再对 Text、Vision 与 RRF 分别
报告 Hit@K、Recall@K 和 MRR，并按 `is_fallback` 分层。

本轮生产实现后的验证结果为 Engine `27 passed`、Gateway `36 passed`。

## 18. 整集人工审计：结构成功不等于语义召回成功

最终产物随后补做了逐 checkpoint 与聚合文件审计。CLI 原先在强类型
`VideoParseResult` 外层临时塞入未声明的 `models` 字段，导致下游按正式 Schema 加载
`pipeline.parse.json` 时被 `extra=forbid` 拒绝。现已将模型选择与 fallback 参数纳入
`VideoParseModels` / `VideoFallbackModels`，旧结果缺少 `models` 时仍兼容为 `None`；重写
后的整集文件可直接验证并 round-trip。对应代码提交为 `4722c48`，最终 Engine 测试为
`28 passed`。

结构和时间轴方面是可靠的：24 个 VideoChunk/Transcript checkpoint 都可加载，Canonical
VideoChunk 连续覆盖 `[0, 1420108)`，468 个最终事件全部有界、排序并满足 midpoint 唯一归属，
Context handoff 与 snapshot hash 可按 VideoChunk 复算。事件时间并集覆盖 98.49%，18 个空隙合计
21.5 秒，最大 3 秒。

但人工语义审计发现以下会影响真实召回的问题：

1. 第 11 个 VideoChunk（`sequence_no=10`）虽由 Pro 成功返回，仍把“乐奈，你呢”之后的回答
   归给高松灯，又把“乐奈，你几岁开始弹吉他”的“不记得了”归给千早爱音；这会污染
   “谁说了什么”查询。
2. `888000–941000` 的连续第一人称舞台发言也随镜头切换被错误归给不同成员，暴露出
   “当前镜头人物 = 当前说话人”的系统性偏差。
3. 全片 `RELATION_CHANGE=0`。包括爽世因《春日影》爆发、团队关系明显破裂的名场面，
   也只被拆成 EMOTION 与 DIALOGUE；按事件类型做关系召回会漏。
4. 后半段 `window_summary` 吸收了 AnalysisWindow overlap 中属于相邻 VideoChunk 的事件。
   因此 `VideoChunkRetrievalDocument` 只能索引 owned events / `self_compact`，不能直接索引 window summary、
   contextBefore 或 contextAfter。
5. 标准名不稳定：简体查询名与日文汉字/假名形式混用；事件 Schema 又没有独立
   `characters` / `speaker` 字段。精确实体检索必须做 alias 归一，不能只依赖 description
   自由文本。
6. seq23 把画面上的“为什么 #8”写成“本集标题”，实际是下一集预告标题，会给第七集
   标题查询制造假阳性。
7. 272 条 DIALOGUE 全都有 text，但所有 transcript checkpoint 都是
   `transcript_source=none`，没有独立 ASR 金标，所以 100% 字段填充率不能被称为台词
   recall 100%。

结论：当前产物已适合进入检索实验，不能直接宣称检索有效。首轮 raw retrieval smoke 应
严格使用 canonical 无 overlap 视频与 owned text，分别报告 Text、Vision、RRF；speaker、
关系与边界查询必须放进人工 fixture，才能让上述缺陷真实暴露在指标里。

## 19. 第七集真实召回冒烟：能做候选召回，但等权融合不能做最终排序

2026-09-13 从原片人工标注 12 条 `query -> time range`，没有从解析文本反向造题。查询覆盖
精确/近似对白、纯视觉动作、人物别名、说话人与镜头主体错位、跨 60 秒边界、关系冲突和
下一集预告。Vision 候选重新裁成 24 个 canonical 无 overlap VideoChunk；Text 候选只读取当前 VideoChunk
`self_compact` 与 owned events，严格排除 `window_summary`、before/after Compact。

真实 Ark embedding 使用 1024 维、1 FPS，结果如下：

| 通道 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR |
|---|---:|---:|---:|---:|---:|
| Text | 58.3% | 91.7% | 91.7% | 100.0% | 0.747 |
| Vision | 25.0% | 58.3% | 58.3% | 100.0% | 0.441 |
| Text/Vision 等权 RRF | 41.7% | 75.0% | 91.7% | 100.0% | 0.623 |

结论应分两层：

1. 作为 raw candidate generator，当前方案有效：12/12 查询都能在 Top10 找到相关 VideoChunk。
2. 作为最终排序还不够：等权 RRF 反而被弱一些的整块 VideoChunk Vision 向量拖累，Top1 只有 41.7%。

最难的“乐奈说自己在很多地方弹奏”在 Text、Vision 都排第 8、RRF 第 7。正确台词虽然存在，
但解析 description 把回答者写成高松灯，证明上游人物归因错误会真实伤害召回，而不是只影响
展示。VideoChunk Vision 对拥抱、哭喊、手机等画面有效，对对白和片尾文字查询明显更嘈杂。

用同一 12 条样本做离线诊断时，Text:Vision=`3:1` 的 RRF 可到 Hit@1 75.0%、MRR 0.845；
这是同集 in-sample 调参，不能直接定为生产默认。下一步应扩充 held-out 查询，再决定通道权重，
并补 CandidateVerifier。当前整集仍是 0 个 fallback VideoChunk，因此这轮
不能代表 fallback 召回质量。

标注与完整排名：

```text
packages/engine/evals/ep07.recall-smoke.json
packages/engine/evals/ep07.recall-smoke.md
packages/engine/outputs/ep07-recall-smoke/recall-eval.json
```
