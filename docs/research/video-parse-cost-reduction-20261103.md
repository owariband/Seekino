# 视频解析降成本论文调研：输入预算、按需观察与模型内部压缩

日期：2026-11-03（Asia/Shanghai）。范围：核对一手论文、正式会议记录、作者代码和 Seekino 当前源码，寻找降低视频解析费用的候选方向。本文是调研依据，不是实施 TRD、论文复现或效果验收；本轮未调用收费模型、修改业务配置、重启服务或迁移数据。

## 对 Seekino 的判断

**当前优先研究“减少送入 API 的重复帧、分配分辨率预算、必要时补看”，模型内部 token 剪枝留作自托管路线。** TimeChat-Online 值得读，但它不能作为图片预处理插件，原样放到任意闭源视频 API 前面。已确认的主推荐包括 CVPR / ICCV 2025 和 ICML 2026 正式论文；下文把质量收益、输入预算、推理速度和实际账单分开说明。

项目中可核对的依据：

- [config.yaml](../../config.yaml) 的仓库默认值为 `video_parser.video_fps: 3.0`；这不是对当前运行服务有效配置的查询。
- [frame_sampler.py](../../engine/common/frame_sampler.py) 的 `plan_frame_timestamps()` 根据 FPS、时长和帧数上限均匀安排整数毫秒时间点。
- [video_parse.py](../../server/service/video_parse.py) 的 `understand_chunk()` 在 `ordered_frames` 路径把 `media.frames` 中每张图片转成 `ImageInput`，并附原视频毫秒时间；`native_video` 路径把 FPS 交给 `VideoInput`。因此改变外部选帧与改变模型内部 token sequence 是两个不同接入点。
- [2026-10-29 协议实验](../report/agent/video_parse_v2/parse-protocol-20261029.md) 冻结了 60 秒、180 张 1280×720 JPEG、3 FPS、`detail=high` 的输入；单次 input 约 22万～24万 tokens。该实验直接调用 Provider，未经过完整生产解析链路，也没有实际账单，不能把所有 input tokens 都称作 image tokens。

以上证据支持优先调查视觉输入规模，但没有证明它占当前总账单的比例。ASR、Understanding、Refine、输出长度、重试、缓存命中和实际模型单价仍须分别计量。

## Change Blindness 与 TimeChat-Online 的边界

Change Blindness 通常译为“变化盲视”：场景确实发生变化，但观察者未能察觉。经典 flicker 实验在原图与修改图之间插入短暂空白；局部运动提示被遮蔽后，人可能反复看图仍找不到很大的变化。一旦把注意力转到相应位置，又会觉得变化明显。它不等于画面没变，也不能推出未注意到的变化不重要。原作者页面提供解释、演示和 1997 年原论文入口。[Rensink：Change Blindness](https://www2.psych.ubc.ca/~rensink/flicker/)。

TimeChat-Online 的启发是减少重复视觉表征的处理。其 DTD 模块比较相邻帧对应局部 token 的特征相似度，删除冗余 token；不是仅做整帧去重或场景切换检测。官方代码使用自定义 `Qwen2_5_VLForConditionalGeneration`，并把 `drop_method`、`threshold` 等参数交给 `model.generate()`，需要模型内部接入。[作者代码与 ACM MM 2025 标注](https://github.com/yaolinli/TimeChat-Online)、[原论文](https://arxiv.org/html/2504.17343v1)。

原论文 Table 4 的一个**不额外 SFT 的 DTD 实验**：Qwen2.5-VL-7B 在 StreamingBench 从 73.7 降至 72.0，删除 82.5% visual tokens，相对保留约 97.7% 的准确率。这里不是 97.7% 的绝对准确率，也不是 API 账单降低 82.5%。完整 TimeChat-Online 模型另有训练流程，不能把模块免 SFT 的实验概括成整个系统无需训练。[Table 4](https://arxiv.org/html/2504.17343v1)。

## 优先阅读的论文与可复用思路

### 1. Q-Frame — ICCV 2025

**对现有 API 最值得先读。** 用 CLIP 类图文匹配模型估计问题相关性，以 query-aware frame selection（QFS）挑帧，另有 multi-resolution adaptation（MRA）分配分辨率。它在 API 外决定输入，省去修改大模型内部的要求。[正式会议页](https://openaccess.thecvf.com/content/ICCV2025/html/Zhang_Q-Frame_Query-aware_Frame_Selection_and_Multi-Resolution_Adaptation_for_Video-LLMs_ICCV_2025_paper.html)。

论文 Table 2 中，GPT-4o 同样输入 8 帧，LongVideoBench 从 53.3 到 58.6，Video-MME 无字幕从 61.9 到 63.8。作者由于闭源预处理未知，**只在 GPT-4o 测了 QFS，没有验证 MRA 的 API 成本**。Qwen2-VL 的多分辨率实验则是另一组：Table 6 中 8 张高分辨率帧为 2265.1 tokens，4+8+32 张混合分辨率帧为 2344.8 tokens，近似预算、实际约增加 3.5%；不能写成严格等 token 或节省账单。[论文 §4.1、Tables 1/2/6](https://arxiv.org/html/2506.22139v2)。

对 Seekino 的推断：可借鉴“重要区域看细、其余保持时间覆盖”。但离线建库没有未来用户的 query，需要另定覆盖目标，不能直接用一次问题把其他内容筛掉。

### 2. Adaptive Keyframe Sampling（AKS）— CVPR 2025

**适合研究低帧数下怎样保住信息。** 它在视觉编码器之前选帧，兼顾问题相关性和时间覆盖，避免只选一堆相似的高相关画面；用预训练 BLIP / CLIP 评分，不调下游 MLLM 权重。[正式会议页](https://openaccess.thecvf.com/content/CVPR2025/html/Tang_Adaptive_Keyframe_Sampling_for_Long_Video_Understanding_CVPR_2025_paper.html)。

论文 Table 1：LLaVA-Video-7B 都用 64 帧，LongVideoBench val 从 58.9 到 62.7，Video-MME 从 64.4 到 65.3，评测不使用字幕。该结果是固定帧数的质量提升，并非直接减少 64 帧或实测 API 费用下降；前置候选抽帧与图文评分也有成本。[论文 §4、Table 1](https://arxiv.org/html/2502.21271v1)。

对 Seekino 的推断：覆盖约束比“全视频只取相似度最高的 N 帧”更值得移植；先设置每个时间区间的最低观察量，再探索把余量给变化与细节。具体采样率尚待验证。

### 3. VideoTree — CVPR 2025

**适合借鉴“先粗看、相关或不确定的部分再展开”。** 方法先按视觉特征聚类，只对代表帧生成描述，然后按问题相关性扩展树节点，最后让 LLM 基于描述回答。[作者项目页及会议标注](https://videotree2024.github.io/)。

Table 3 在 EgoSchema 500 题验证子集、相同 GPT-4-1106 与 LaViLa captioner 下：LLoVi-best 为 180 条 captions、24.8 秒、61.2%；VideoTree-fast 平均 13.6 条 captions、7.8 秒、63.6%。但 LLoVi-fast 只有 3.9 秒、57.8%，因此不能笼统说它比所有基线更快。这是 QA 实验的时间和描述数量，不是完整素材建库的费用。[论文 §4、§5.2、Table 3](https://arxiv.org/html/2405.19209v3)。

对 Seekino 的推断：可研究粗解析后对遗漏风险高的区间补看；不要先昂贵地描述全部帧，再把描述删掉，否则主要视觉成本已经发生。原方法按 query 展开，不能保证覆盖未知检索需求。

### 4. VideoBrain — ICML 2026

**适合研究模型怎样决定是否补帧。** 以直接看帧的 VLM 为控制者，配合语义检索与区间均匀采样两种工具，在信息不足时补看。正式 PMLR 论文集已确认录用，不仅是 arXiv 投稿。[正式会议页](https://proceedings.mlr.press/v306/zou26h.html)。

论文基于 Qwen3-VL-8B-Instruct，初始 16 帧、最多 5 轮；Table 2 的 LongVideoBench 从基线 32 帧、45.2% 到平均 22.3 帧、53.3%。四个长视频测试集平均帧数分别减少约 30%～40%。但策略经过 SFT 与 GRPO，文中报告 8×H20、约 472 GPU 小时训练；帧数减少也不包含全部多轮推理和工具成本。[论文 §3、§4、Table 2](https://arxiv.org/html/2602.04094v1)。

对 Seekino 的推断：先借鉴“少量输入→检查缺口→限定范围补看”的流程。直接复制训练路线并不一定符合当前预算，也不能用普通提示词实验冒充论文复现。

### 5. FrameFusion — ICCV 2025

**面向可控制推理内部的模型。** 浅层先合并相似 token，再按重要性裁剪；需要读取及修改中间表征。其正式 venue 是 ICCV 2025。[正式会议页](https://openaccess.thecvf.com/content/ICCV2025/html/Fu_FrameFusion_Combining_Similarity_and_Importance_for_Video_Token_Reduction_on_ICCV_2025_paper.html)。

论文 v2 把 relative token budget 定义为解码开始时各层平均 KV 序列长度相对原输入的比例。30% 预算下，Table 1 的 LLaVA-Video-7B 跨表内指标平均分从 65.9 到 64.8。§5.4 在 128 条 Video-MME 视频、LLaVA-Video 7B/72B、32～256 帧、分别 1/4 张 A100-80GB 上报告 1.6～3.6 倍端到端加速。早期版本的速度范围不同，应注明版本。[论文 v2 §5、Table 1](https://arxiv.org/html/2501.01986v2)。

对 Seekino 的推断：适合后续自托管成本比较。把图片涂黑、变灰或发送 CLIP 向量，不等价于此类内部压缩，也不会自动获得其加速比。

### 6. DyCoke — CVPR 2025

**补充关注长输出阶段的成本。** 训练自由的跨帧 token 合并，加解码阶段动态 KV 裁剪；需要修改推理内部，不能原样控制闭源 API。[作者代码与会议标注](https://github.com/KD-TAO/DyCoke)。

Table 3 在单张 A6000、LLaVA-OV-7B、32 帧、MVBench 下：单例延迟 2.30→1.49 秒，GPU 内存 34→24 GB，准确率 58.81→59.56。此表只输出一个 token；长描述另看 Table 4，32 帧 VideoDC 的解码延迟从 42→31 ms/token，同时评分从 3.30→3.20（`K=0.7`）。因此不能把单 token QA 的 1.54 倍加速直接推广到 Seekino 的长 JSON 输出。[论文 Tables 3/4](https://arxiv.org/html/2411.15024v3)。

### 7. StreamingTOM — CVPR 2026

**作为实时观看方向的后续阅读。** 它同时处理两个位置：在进入 LLM 前给每帧设因果 token 预算；在 LLM 后用 4-bit 量化记忆和按需取回控制活动 KV cache。作者项目页与正式 CVF 论文条目确认 CVPR 2026。[正式会议页](https://openaccess.thecvf.com/content/CVPR2026/html/Chen_StreamingTOM_Streaming_Token_Compression_for_Efficient_Video_Understanding_CVPR_2026_paper.html)、[作者项目页](https://yige24.github.io/StreamingTOM/)。

它减少的是本地模型 prefill、记忆和延迟压力。对当前离线 API 解析，优先级低于外部选帧；其具体压缩倍数不应换算为 API 费用倍数。本轮未复现实机效果。

## 已讨论方法与其他候选的定位

| 方法 | 本轮核对到的定位 | 对当前降费用问题的影响 |
| --- | --- | --- |
| QueryStream | [ICLR 2026 正式论文集](https://proceedings.iclr.cc/paper_files/paper/2026/hash/0b17d256cf1fe1cc084922a8c6b565b7-Abstract-Conference.html)；围绕 query 的 token 过滤与回应触发 | 在线助手可研究；在无 query 的素材入库阶段，不能直接按某次问题的相关性丢掉其他事实 |
| LongVU | [ICML 2025 正式论文集](https://proceedings.mlr.press/v267/shen25j.html)；含 DINOv2 相似帧筛除和内部时空压缩 | 会议应写 ICML 2025；不是 ICLR 2025。可作自托管与混合压缩补充阅读 |
| ColorTrigger | [作者论文](https://arxiv.org/html/2603.22466v1)；低功耗灰度采集与动态 token 路由 | 灰度文件的体积、传输、电力与远端 API token 账单是不同指标；本轮不把它计入已核验顶会推荐名单 |

## 候选验证顺序与费用口径

以下是基于源码与论文的工程推断，尚未执行。

1. **先量成本构成。** 用同批素材拆出 ASR、Understanding、Refine、失败重试、缓存命中和输出；保留 Provider 的实际 usage 与账单。缓存 input 通常是 input 的子集，不能重复累加。
2. **先建立简单输入预算基线。** 在相同片段、Prompt、模型版本、输出要求下比较当前均匀采样与更少帧，明确质量随预算变化的曲线。这比先实现复杂选择器更容易判断收益来自哪里。
3. **再验证覆盖约束下的相似帧筛除。** 留住时间覆盖、镜头边界附近和短动作候选；以 AKS / Q-Frame 的思想分配预算，分别记录选择器成本与节省的远程输入。整帧 embedding 相似不能保证小物体、表情、字幕和手部动作没有变化。
4. **最后验证按需补看。** 先粗解析，再针对具体缺口读取原片；复查必须有次数和范围上限。仅用模型“很自信”作为停止条件，不能证明没有漏事件。

建议冻结静态对话、快速动作、字幕/OCR、短暂人物出入、相似背景中的小动作等片段，评价人物与对白归属、事件漏检、时间边界、画面文字，以及后续检索能否回到正确原片。既要看平均质量，也要看关键短事件是否被系统性删掉。QA 分数保持并不证明完整事件解析质量保持。

可以用下面的概念式防止账算错：

`总成本 = 选帧/特征计算 + ASR + 首轮模型调用 + 所有补看与 Refine 调用 + 失败重试 + 存储与自托管资源`

每次 API 调用再按供应商的未缓存输入、缓存输入、输出等规则计算。**减少输入图片、减少内部 visual tokens、降低 FLOPs、加快 wall-clock、减少实际支付金额必须分别报告。** 分辨率下降是否减少计费 tokens，取决于实际 Provider 的 resize / tokenization / detail 合同，须用 usage 验证；没有实测时只写候选收益。

## 本轮验证边界

已做论文与会议归属核对、关键实验表阅读、项目源码及历史实验记录核对。未复现论文、运行收费模型、测量当前服务账单、修改采样策略或证明任何方案已降低 Seekino 的解析费用。本文仅新增调研笔记，保留工作区其他已有修改。
