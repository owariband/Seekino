# 日语动画的说话人分离与角色归属研究（2026-10-16）

结论：**音色可以为画外说话人的角色归属提供证据，但需要先有可靠的角色声音样本，以及允许拒绝匹配的判定。** 单独加入 diarization，只能得到“哪些时间由同一声音说话”；它不会自动知道这个声音叫高松灯。建议先评估本地 Community-1 的匿名说话人时间线，再用已确认的角色样本做 embedding 匹配。WhisperX 可帮助对齐对白；TalkNet 类音视频检测适合补充“屏内谁正在说话”，不能包办画外音归属。

这是方案研究，未安装依赖、运行新模型、修改业务或重解析视频。下文区分一手资料事实、本项目已观察事实和工程建议；**没有任何日语动画准确率已经验证的结论**。外部资料于 2026-10-16 核对，引用官方主仓、主模型卡或原论文；`main` 链接是动态版本，不构成部署版本锁定。

## 先拆清楚五个问题

| 能力 | 回答什么 | 不直接回答什么 |
| --- | --- | --- |
| ASR | 某段声音说了什么，以及对应时间 | 是哪个角色说的 |
| Speaker diarization | 什么时候换了声音、哪些片段属于同一匿名说话人 | 匿名 `speaker_0` 对应哪个角色 |
| Speaker embedding | 把一段语音表示成可比较的声音特征向量 | 单凭向量得到姓名或角色名 |
| Speaker verification / identification | verification 比较两段是否同一说话人；identification 在已注册样本中判断身份，或返回未知 | 保证所有短句、重叠语音都能成功认人 |
| Active speaker detection（ASD） | 给定画面中的人脸轨迹及同步声音，判断此人此时是否在说话 | 没有出镜的说话人是谁；该人脸叫什么名字 |

这些边界分别对应 [pyannote 的匿名时间线输出](https://github.com/pyannote/pyannote-audio)、[ECAPA 的语音到向量方法](https://www.isca-archive.org/interspeech_2020/desplanques20_interspeech.html)、[SpeechBrain 的两段语音验证接口](https://speechbrain.readthedocs.io/en/latest/API/speechbrain.inference.speaker.html)、[pyannote 的注册后身份匹配流程](https://www.pyannote.ai/blog/speaker-identification-system-recurring-meetings) 和 [TalkNet 原论文](https://arxiv.org/abs/2107.06592)。不要把 diarization（按说话人分段）与 source separation（把混合声音拆成独立音轨）混用。

“听起来像”应落成一个有来源的相似度结果。角色名来自经过确认的 `声音样本 → 角色` 绑定；在没有样本时，最多先积累匿名声音身份。这个限制对画内、画外都成立。

## 本次 AniVAG 的实际缺口

本段依据本地源码、已落盘模型请求/响应及同轮数据库核查，不是根据外部工具文档推测现有系统。

- EP4 被调查 chunk 的 ASR 实际模型响应标记为 `doubao-seed-2-0-lite-260428`，输入包含有声 MP4。原始输出 11 条 cue，字段仅时间与文本，没有 speaker。35.4–48.7 秒的一条 cue 同时包含爱音的长句和随后回应，并出现“祥ちゃんも悪くない、咲ちゃんも悪くない”的重复/错字。见 [完整原始请求与事件](../../outputs/model-traces/6da385d8-7a90-406d-8e35-d4f3c9243059/events.jsonl)、[未截断输出预览](../../outputs/model-traces/6da385d8-7a90-406d-8e35-d4f3c9243059/output-preview.json)。这里的时间是该次原始模型响应中的秒；不能直接当作项目内部整数毫秒。
- 归一化合同 `ASRCue` 也只有 `start_seconds/end_seconds/text`，并不携带 speaker；跳过理解阶段时，程序明确生成 `speaker=None`。见 [observations.py](../../engine/parsev2/observations.py)。
- 该解析任务的理解模型快照为 `codex_local/gpt-6-astra`，视觉输入为 ordered frames（1 fps、最多 32 帧、高 720）；理解阶段消费图片和文字、通过 Canvas 读取 ASR，没有收到音轨，再生成 timeline 的 speaker 字段。原始 ASR 的说话人边界问题与后续角色命名错误是两个环节，不能都归结为 ASR 已认错角色。见 [解析模型装配](../../server/service/video_parse.py)、[解析输入合同](../../agent/video_parse_v2/input.py)。
- 本轮核查未发现当前解析链实现了独立 diarization 或 speaker embedding。现有 `speaker` 因而不是“声纹匹配已经核验”的结果。

同轮 SQL 审计进一步确认：

- understanding 实际调用 `83faa2da-d825-4122-8394-06c735b6b4e9` 为 `codex/gpt-6-astra`，2026-10-14 01:48:39–01:50:16 成功，未发生 fallback。
- 初始 `chunk_parse_content` v1 就已把相关对白归为 `高松燈`，错误不是到 Chat 才新产生的。
- `detail.evidence.asr` 仍保存上述 11 条 ASR，只做秒转毫秒及边界裁剪；可区分原始转写和理解模型生成的 speaker。

审计提取见本机临时文件 [/tmp/anivag-ep4-asr-audit.json](/tmp/anivag-ep4-asr-audit.json)。该临时文件不作为长期研究资料依赖，原始 ASR 留存位置见上方仓库链接。
由于 Codex 这次原始响应及逐次 Canvas 写操作未保存，**只能定位到初始 understanding 产物已有错误、且缺少独立声学校验，不能确定模型内部为何选中了该角色**。
“只收到图片和文字”是已核实的输入事实，不是对内部推理过程的完整解释。

**工程判断：** 先解决长 ASR cue 跨说话人边界、声音身份没有记录的问题，比只给理解模型增加“别认错人”提示更直接。仅按时间把整条 13.3 秒 cue 贴给一个角色，仍可能把其中另一人的短回应一起归错。

## 候选能力及实际取舍

| 方案 | 一手资料确认的能力 | 在本项目中的位置与限制 |
| --- | --- | --- |
| pyannote Community-1 | 本地运行，输入按 16 kHz 单声道处理；提供常规 diarization 和 exclusive diarization；可给定或限制说话人数；权重下载后支持离线使用 | 本地匿名说话人分段候选。当前画面有两个人不等于整段音频只有两个说话人，不能据此硬设人数。模型卡的学术基准不是动漫验收结果。[官方模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1) |
| pyannote Precision-2 | 官方资料说明有说话人数控制、重叠语音处理、exclusive 模式及置信度信息；主仓示例通过 pyannoteAI 服务运行 | 用于理解产品能力差异，不作为本次本地试验默认依赖，也不把它称为当前最新版本。[官方说明](https://www.pyannote.ai/blog/precision-2)、[官方调用示例](https://github.com/pyannote/pyannote-audio#precision-2-premium-speaker-diarization) |
| SpeechBrain ECAPA-TDNN | 可提取 embedding、比较两段声音；公布模型在 VoxCeleb1/2 上训练；模型卡明确不保证其他数据集效果 | 做独立的角色注册/匹配候选。不是 ASR，也不负责输出角色名字。VoxCeleb 的成绩不能换算成本地动画识别率。[模型卡](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) |
| WhisperX | VAD、ASR、强制对齐，并可结合 pyannote 把时间段或词附上匿名 speaker ID；当前主线有日语对齐模型配置 | 适合评估对白边界与声纹时间线衔接。不是独立角色识别模型；原文错字不会因为对齐就必然变正确。[原论文](https://arxiv.org/abs/2303.00747)、[日语配置](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py) |
| TalkNet / TS-TalkNet | TalkNet 判断屏内人脸是否发出当前声音；TS-TalkNet 研究以预注册 speaker embedding 补充音视频线索 | 第二阶段辅助角色绑定与检查。TalkNet 需要人脸检测/跟踪；缺脸、背影、画外旁白不能仅靠它命名。TS-TalkNet 也不等于已解决动漫识别。[官方实现](https://github.com/TaoRuijie/TalkNet-ASD)、[TS-TalkNet 论文](https://arxiv.org/abs/2305.12831) |

Community-1 已包含 speaker embedding 相关组件；其模型卡引用 WeSpeaker，而不是要求用户另装 ECAPA 才能完成 diarization。当前 WhisperX 的包装也可返回 speaker embeddings。因此试验时应先检查现成输出是否适合注册匹配，**不要默认串联两套 embedding 模型**；ECAPA 可以作为独立对照。不同编码器的向量不能直接混比。[Community-1 组件说明](https://huggingface.co/pyannote/speaker-diarization-community-1#citations)、[WhisperX diarize.py](https://github.com/m-bain/whisperX/blob/main/whisperx/diarize.py)

Exclusive diarization 为每一时刻选一个说话人，便于和转写对齐，不能证明原音频没有重叠。若用于字幕，应同时保留常规 diarization 的重叠信息，避免把这种展示投影变成“现场只有一人说话”的事实。前半句是 [官方模式定义](https://www.pyannote.ai/blog/precision-2)，后半句是本项目的工程建议。

## 为什么动画需要单独验证

**短句。** 说话人验证研究专门设置短时长、跨语言试验，不能把长句成绩外推到“嗯”“诶”等音节。[短时长 ECAPA 相关研究](https://www.isca-archive.org/interspeech_2021/thienpondt21_interspeech.html)。建议按有效语音时长分桶测试；短句可参考同一匿名 speaker 的其他清晰轮次，但不能不经分段就拼接相邻声音，可能拼进另一人的发言。

**表演声线。** 日语动画语音的一手研究观察到职业声优会使用局部气声、紧声、粗糙音等变化。[日本动画语音研究](https://www.isca-archive.org/speechprosody_2026/ishi26_speechprosody.html)。由此提出待验证假设：同一角色的耳语、哭喊、平静说话可能拉开向量距离，相似角色声线也可能靠得很近。不能把“一个声优”与“一个虚构角色”当作恒定一一映射。

**BGM、音效和重叠。** 官方声纹注册建议使用单人、无音乐、无其他声音的清晰样本。[注册建议](https://www.pyannote.ai/blog/speaker-identification-system-recurring-meetings)。工程上应保存原音轨，先用清晰对白建样本；降噪/人声分离只做可比较的试验分支，不能预设一定提升身份匹配。音乐里的歌声和角色台词也应分开评估。

**日语文本与时间。** WhisperX 主线显式处理日语无空格文本、选择日语对齐模型；README 同时说明重叠语音和 diarization 有局限。[日语实现](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py)、[官方限制](https://github.com/m-bain/whisperX#limitations-)。需要验证角色名、短回应和说话人切换边界，不只检查中文字幕看起来通顺。

**动画画面。** TalkNet 官方评估来自 AVA、TalkSet、Columbia 等资料，不能作为本项目动画准确率依据。其 FAQ 指出短于一秒、小脸、侧脸表现存在问题，且前置人脸检测失败时无法输出 ASD。[官方 FAQ](https://github.com/TaoRuijie/TalkNet-ASD/blob/main/FAQ.md)。动画人脸、口型循环及画外台词的适配是本项目需验证的推论；不要用“脸在画面里”替代“此人正在说话”。

## 建议的最小本地验证路径

以下是设计建议，尚未实现，也不是已确认的库接口或数据库 Schema。

1. **先建立一集级别的匿名 speaker 时间线。** 优先对整集音频或有充分重叠的连续窗口做 diarization，再映射回现有 chunk。逐个 chunk 独立得到的 `speaker_0` 不能直接当成同一个角色；跨文件连续身份需要注册样本或独立匹配。[官方对单文件分段与跨文件身份的区分](https://www.pyannote.ai/blog/speaker-diarization-for-ai-dubbing-and-localization)
2. **分段与转写对齐独立检查。** 保留原 ASR cue，再对齐细粒度字/词时间与 speaker turn。本次长 cue 若跨两位说话人，应切成多段或标记混合/不确定。WhisperX 当前实现按重叠时长选择主 speaker，并可对齐到词；只有整段时间时不能恢复其中每个词的精确归属。[官方实现](https://github.com/m-bain/whisperX/blob/main/whisperx/diarize.py)
3. **建立小规模、已确认的角色样本集。** 每个角色挑选若干不同场景、单人清晰发声片段，保留原视频 ID、绝对毫秒范围、确认方式和样本版本。初期可试每角色 3–5 段、每段约 3–10 秒；这些是试验参数，不是论文保证的最低时长。样本可以是原视频时间引用，embedding 单独保存，无须复制长期音频文件。
4. **做可拒绝的身份匹配。** 对多个已确认样本聚合匹配；同时检查第一名得分、与第二名差距、片段时长、重叠/音质和样本支持数量。门槛由本地留出集选定，不直接照搬 SpeechBrain 示例的默认 `0.25`。余弦得分不是校准后的身份概率。[接口定义](https://speechbrain.readthedocs.io/en/latest/API/speechbrain.inference.speaker.html)
5. **保留 unknown 与证据分歧。** 没有足够声音证据时，speaker 可保持匿名或未知；视觉支持角色 A、声音支持 B 时，记录冲突，交由 Agent 查询上下文或说明不确定。不得用本轮模型猜测的角色名自动回灌为高置信度注册样本。
6. **最后才加屏内发声辅助。** 若本地样本证明 TalkNet 类方法对动画有效，可用同步发声和已确认外观帮助注册声音。画外音继续依靠声音样本和上下文，不把没检测到嘴动解释为角色没说话。

角色声音样本的绑定应按作品及配音音轨隔离。跨集同角色可以复用；换配音、声优更替或角色表演模式明显变化，应新增版本或多个原型。此为工程建议；本轮没有证明单个聚类中心足以覆盖这些情况。

最小可追溯结果至少要区分：音频范围、匿名 speaker ID、候选角色及得分、是否拒绝、原样本引用、编码器版本和匹配版本。Agent 消费“声音匹配证据”，最终叙述仍需结合事件内容；声纹只能支持“谁说的”，不能单独证明“被谈论的人是谁”。

## 验收而不是凭听感选模型

建议先标注约 100–200 条跨集对白，分出注册集、阈值校准集和留出测试集，避免同一句或其近邻切片同时出现在注册与测试中。覆盖短句、普通句、BGM、交叠、画外音、侧脸/背影及情绪变化；纳入不在注册表里的角色，验证系统能拒绝匹配。数量是首次验证的建议规模，不构成统计显著性保证。

| 要回答的问题 | 建议记录的结果 |
| --- | --- |
| 声音边界是否正确 | diarization 的漏检、误检、speaker confusion；固定是否计入重叠及边界容忍设置 |
| 是否把句子错误合并 | 跨 speaker 的 cue 数、错误边界附近的字/词归属 |
| 角色名是否可信 | 接受匹配后的准确率、错误接受率、拒绝率，分时长/音质/角色统计 |
| 跨 chunk 是否稳定 | 同一角色被拆开、不同角色被合并、ID 在边界切换的次数 |
| 值不值得接入 | 本机整集耗时、内存/显存、追加模型成本，以及对现有问题的改善 |

DER 的错误组成可对照 [pyannote 官方说明](https://www.pyannote.ai/blog/precision-2)。角色名准确率需要额外标注，因为 diarization 的匿名标签可以与真值做置换匹配，低 DER 不等于已经认对角色名。

目前推荐停在这个验证范围：**本地 Community-1 分段 + 可选现成 embedding/ECAPA 对照 + 人工确认角色样本 + 对齐和拒绝匹配评估**。不据论文排行榜直接升级全量解析，不要求先重跑已解析的全部视频，也不把增加 TalkNet、换 ASR、重做 Canvas 绑成同一个改动。
