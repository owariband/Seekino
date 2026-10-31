# Pyannote 与 CAM++ 身份归因的能力及重复建设边界

日期：2026-10-31（北京时间，UTC+8）。核对范围：Pyannote 4.0.7 / Community-1、WhisperX 3.8.6、CAM++ 官方实现，以及本工作树的调用链与历史记录。本轮只调查，未运行推理、下载权重、修改生产代码或变更 PR。

后续处置：用户于同日决定搁置 Apple WhisperX 适配，保留现有 CAM++ 使用，暂不推进 Pyannote 替换或新实验。本文保留能力事实与候选分析，不是当前实施计划；决定见[主 TRD](../design/03-product-reassessment/apple-gpu-asr-alignment-trd.md)。

**判断：应该把现成的全片 diarization 作为基线，不能因为它不直接输出角色名，就默认保留一整套自有分段、聚类和逐句归因。** CAM++ 本身是复用的声音特征模型；重复建设风险主要在围绕它补出来的通用算法与编排。与此同时，作品人物、可信参考、拒绝归因和证据持久化仍是 Seekino 的业务职责。现有证据不足以宣布 CAM++ 路线优于 Pyannote，也不足以宣布换用 Pyannote 已能解决动漫身份归因。

## 上游已有能力

| 所需能力 | 官方实现与边界 |
| --- | --- |
| 全片匿名 speaker、换人边界 | Community-1 对音频做局部分段与特征提取，再跨窗口聚类、重建全片时间线。ASR 分窗不阻止这条独立流程；它不需要先运行 WhisperX ASR。见 [Pyannote 4.0.7 的 apply / clustering / reconstruct](https://github.com/pyannote/pyannote-audio/blob/4.0.7/src/pyannote/audio/pipelines/speaker_diarization.py#L489)。 |
| 重叠语音 | 同时返回普通 diarization 与 exclusive diarization；后者限制每时刻一个 speaker，便于文字时间对接，不能拿它证明原音频没有重叠，也不是将混合声源拆成音轨。见 [官方模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1#exclusive-speaker-diarization)。 |
| 可复用声音特征 | `DiarizeOutput.speaker_embeddings` 提供每个 speaker 的特征；WhisperX 的 `return_embeddings=True` 可导出按 speaker 标签索引的字典。因此不必默认再给所有句子编码一遍 CAM++。见 [输出定义](https://github.com/pyannote/pyannote-audio/blob/4.0.7/src/pyannote/audio/pipelines/speaker_diarization.py#L59)、[WhisperX 封装](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/diarize.py#L82)。 |
| 将匿名声音对应到已知人物 | 开源 Community-1 的 `apply` 接口没有注册人物参考库的参数；可在其 embedding 之上做应用层匹配。源码中利用 `annotation` 对齐参考标签是评测辅助，不是声纹注册识别。见 [接口及参考标签处理](https://github.com/pyannote/pyannote-audio/blob/4.0.7/src/pyannote/audio/pipelines/speaker_diarization.py#L659)。 |
| 成品声纹注册与身份识别 API | 商业 pyannoteAI 已有 `/voiceprint` 与 `/identify`，可传入带名字的参考声纹、匹配阈值等；官方示例使用 `precision-2`。不能把这套服务能力说成开源 Community-1 的本地现成功能，也不能说整个 Pyannote 产品没有身份识别。见 [voiceprint](https://docs.pyannote.ai/api-reference/voiceprint)、[identify](https://docs.pyannote.ai/api-reference/identify)。 |

CAM++ 官方 `infer_sv.py` 的基本职责是将音频编码为 embedding、比较两段音频相似度；common 模型配置为 192 维。3D-Speaker 项目另有完整 diarization 配方，这也说明“CAM++ encoder”和“完整 speaker pipeline”不是同一层的比较对象。[CAM++ 官方推理入口](https://github.com/modelscope/3D-Speaker/blob/main/speakerlab/bin/infer_sv.py)、[官方能力与配方](https://github.com/modelscope/3D-Speaker#overview-of-content)

## 本地调用链与历史依据

当前工作树的正式 B0 路径启用时，在 [server/service/video_parse.py](../../server/service/video_parse.py#L633) 从 Understanding 的 `DIALOGUE` 收集片段及已有人物标签，对整个区间做 CAM++ 编码；随后按已有姓名聚合、去除离群参考、比较质心，输出候选与分档。它是基于人物标签的匹配，**并不是先对整片做无监督 speaker diarization**。匹配与拒绝规则见 [voiceprint_matching.py](../../engine/parsev2/voiceprint_matching.py#L122)。该文件中的 `cluster_samples` 被评测和测试调用，正式 B0 不走这条聚类路径；本轮未核验运行中服务的启用配置或部署版本。

历史记录不能支持“已经比较过 Pyannote，所以必须自研”的结论：

- [2026-10-16 研究](2026-10-16-speaker-attribution.md) 已明确建议先用 Community-1 建立全片匿名时间线，并优先复用其 embeddings，不默认串联两套 encoder。
- [10-17 CAM++ 实验摘要](../dev/devlog.md#devlog-history-27) 证明固定 4 人、24 条参考的闭集样本可以匹配；CPU encoder 中位 27.06 ms。但一段 13.5 秒混合音频对爱音为 0.9117、爽世为 0.5761，已经说明“整段最像谁”不能表达其中的换人。这些都不是与 Pyannote 的对照。
- [历史分段与声纹 TRD](../design/02-agent-harness/voiceprint-trd.md) 记录了 CAM++ 1 秒窗 / 400 ms 步长的变化扫描实验；其所在历史隔离工作树、是否上线应按当时证据理解，当前工作树没有 `speaker_change.py`，不能直接写成当前生产能力。[10-21 devlog](../dev/devlog.md#devlog-history-06) 仍将独立 diarization 记为研究候选，没有项目 DER 或生产收益。

上述历史判断依据当前跟踪的文档摘要；部分摘要引用的 `outputs/` 原始研究材料在此次核对位置已不可用，本轮未重新审计全部原始产物。当前没有可引用的项目同条件对照证明 Pyannote 更好或更差。

## 对当前选型的含义

用户进一步提出“预先对全片 ASR”和“在现有 CAM++ 使用层引入 Pyannote”两条路径。这里须固定语义：纯 ASR 与 diarization 可以独立推进，不是互相替代；如果第一条指的是全片转写连同 diarization 一起前置，则与第二条的差别是说话人处理的执行时机。无论在前置阶段还是后续声纹阶段执行，全片 speaker 关联都应覆盖完整音轨，不能按业务 Chunk 独立编号后直接合并。当前记录需求边界，不把候选接入位置或替换效果写成已验收。

可优先验证的组合是 `whisper.cpp 转写及持久化 + Pyannote 全片匿名时间线 + Seekino 人物绑定`，不要求保留 WhisperX 的前置 VAD/ASR 流程。先通过全片 speaker 分组，再为每组选择可靠代表片段进行人物绑定，有机会减少逐句命名和重复编码；这属于工程假设，需实测后决定替换范围。

保留的业务工作包括：参考片段与人物的可信绑定、作品/音轨范围、unknown 与冲突处理、时间证据、版本及持久复用。若更换 embedding 模型，参考和目标必须使用同一编码空间；既有 CAM++ 向量和阈值不能直接搬到新模型。是否继续用 CAM++ 做额外身份匹配，应在固定分段、固定参考集上比较其增益后决定，而不是预先叠加两套 encoder。

运行成本仍需验证：Community-1 官方说明默认 CPU、支持 CUDA，并提供进度 hook；固定版本在本机 Apple MPS 的兼容性、吞吐与结果尚无本轮证据。此前停止的是 WhisperX 的 CPU VAD，不能直接作为 Community-1 完整 diarization 的耗时测量。[设备与进度说明](https://huggingface.co/pyannote/speaker-diarization-community-1#processing-on-gpu)

后续若恢复实验，应先选包含 A→B→A、短回应、混合长 cue 和重叠语音的有界样本，分别评估 speaker 分段/聚类错误、人物归因错误和等待成本；不能用主要为单人讲解的整课跑通代替多人归因验收。本轮没有执行该实验，也没有将任何候选标记为可部署。
