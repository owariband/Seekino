# GPT 课程背景与全片说话人上下文验证（2026-11-01）

关联：[主 TRD](../../design/03-product-reassessment/local-whisperx-asr-trd.md)、[上一轮分组试点](video-diarization-api-20261101.md)、[PR #18](https://github.com/owariband/Seekino/pull/18)。本轮是同一 ASR 需求的后续身份上下文修正。

## 问题、修改与来源

上一轮声源分组已进入真实请求，首块 22 条 cue 均为 SPEAKER_00，但最终 6/6 DIALOGUE.speaker=null。两个字段含义不同：ASR 编号表示匿名声源，Timeline speaker 表示被命名的人物；原 ASR 没有被改写成 null。实际 Series 仅有验收用途的简介，important_extra_data=null、character_bible=[]；Prompt 又要求直接人物映射证据，导致模型反复判断是否能给画中画讲师命名。

用户本轮明确提供 Karpathy 课程来源并要求补背景、注入 all_speakers。通过正式 `/api/series/update` 更新当前隔离 Series `34646a50-bdad-5207-837c-5d0e90adcc0f` 的 description 和 existing important_extra_data JSON，保存修改前后快照。课程名和本集标题按 [Karpathy 官方课程目录](https://karpathy.ai/zero-to-hero.html)核对；讲师/主要声音对应按用户提供的课程背景记录，不表述为生物特征身份验证。

背景为 Andrej Karpathy 的《Neural Networks: Zero to Hero》课程，本集《Let's build GPT: from scratch, in code, spelled out.》，由 Karpathy 讲解并演示代码。JSON 背景包含来源、all_speakers 姓名/别名/角色，以及单讲师与主要声源的对应背景；没有将某个任务的 SPEAKER_00 永久写入 Series 当成跨视频身份。

Prompt 在每次 Understanding 显式注入 all_speakers：完整 voice_groups、voice_group_count、scope、scope_id 与背景中的 known_speakers。全片目录不会因当前窗口只出现一个声音就丢掉其他声音；旧 clip 输入明确限定在 input_clip，没有标签时人数为未知。允许采用明确背景中的单讲师/主声源对应，缺少背景或存在矛盾时保留未知。Refine 使用同一口径。原转写、模型、时间合同、Schema 和数据库结构不变。

## 真实接口观察

前一版本 `6cf69603-6708-4d24-bee3-2dff0e858ad3` 保留。核对 API PID 10828、任务暂停且租约排空后，加载 Prompt 代码到隔离 API PID 13664。通过 reparse 接口创建 v3 `c3970da1-53b7-4cf5-9bb2-15bfa01e9947`，保留单路 Understanding、同一完整原片/首 Chunk、189 张 timestamp 配对图、Kimi k3.1-pro、stream=true 和原模型参数；只变化课程背景与身份上下文/Prompt。

两份音频成果均从 v2 经身份与摘要校验后复用。实际 Kimi 请求 `d38de9b9-67e2-4eb1-b45c-15ee8ac42859` 含全片 voice_groups=[SPEAKER_00]、voice_group_count=1、known_speakers=[Andrej Karpathy] 及对应角色/课程背景。输入目录和背景已经在真实 trace 中确认，不能仅以 Prompt 单测替代此接入验证。

首块实际提交成功：persons=[Andrej Karpathy]，17 个事件中的 9 个 DIALOGUE 全部 speaker=Andrej Karpathy。ASR evidence 仍为 22 条 SPEAKER_00，两个音频正文摘要不变，新增转写 0、分组模型加载 0。首块后正式暂停，租约版本 2、无活跃 Chunk 租约；新版本只验证 1/117，旧版本及历史结果保留。

| 指标 | 前一轮：缺课程背景 | 本轮：背景+speaker context |
| --- | ---: | ---: |
| 命名人物 | 右下角男性讲解者 | Andrej Karpathy |
| 对话归属 | 6/6 为 null | 9/9 为 Andrej Karpathy |
| 总事件 / 对话数 | 18 / 6 | 17 / 9 |
| 请求总耗时 | 260.990 s | 311.469 s |
| 请求到首 SSE | 50.781 s | 59.239 s |
| reasoning 流耗时 | 180.503 s | 220.099 s |
| 正文输出耗时 | 29.606 s | 32.113 s |
| prompt tokens | 233,207 | 233,603 |
| reasoning / completion tokens | 5,358 / 6,949 | 6,890 / 8,559 |

两次 189 张图片的内容摘要、顺序和 detail 完全相同，出站参数除 messages 外完全相同；均单路、Kimi k3.1-pro、stream=true。文本输入增加 1,244 字符，Provider prompt tokens 增加 396（约 0.17%）。本次消除了缺少课程背景导致的首块人物归属为空，但没有提速：总耗时增加约 50.479 秒，主要为 reasoning 流增加约 39.596 秒，另有首 SSE 等待增加约 8.458 秒。可见推理采用了已提供的 Karpathy 名称，其余仍处理跨 Chunk 语句边界、事件分组、画面文字和 Schema 自检。

此处不是逐事件金标准确率：两次对话拆分数量不同，9/9 表示本次产出的对话都采用用户提供人物，不表示全课识别准确率 100%。单次模型推理、服务端负载和缓存等仍可能波动，不能据此认定新增 context 导致普遍变慢。决定保留背景与接线，因为用户期望的人物归属已在首块实证；耗时问题继续单独诊断，不重复计算音频。

## 验证范围

43 项 Prompt/分组恢复定向检查通过；扩展到 Parse Agent 模块为 157 项通过（两组有重叠）。新增检查覆盖两个 Chunk 的完整声源目录、带姓名的背景输入、原始 ASR 不变，以及旧 clip 的局部标签/未知人数。数据更新通过正式 Series API 完成并回读核对；没有修改开发库的其他系列。

本轮考察用户给定背景能否支持首块人物归属，不是自动声纹认人测试，不是人工 DER 评测。整集人物归属稳定性、Refine 实跑和 117 Chunk 到检索问答闭环仍待验证。只有一个真实样本，计时不能推导吞吐或尾延迟，新增背景与 Prompt 属于一组改动，不能单独归功于 all_speakers 字段。

证据：[Series 更新内容](evidence/speaker-context-20261101/series-update-request.json)、[真实注入目录](evidence/speaker-context-20261101/injected-speaker-context.json)、[前后时序与归属对比](evidence/speaker-context-20261101/comparison.json)、[输入一致性](evidence/speaker-context-20261101/input-comparison.json)、[首块实际 JSON](evidence/speaker-context-20261101/first-chunk-content.json)、[最终暂停状态](evidence/speaker-context-20261101/final-state.json)。完整 trace、原始结果与更新前后 Series 快照保留在本机 `outputs/verification/video-asr-api-recovery-20261031/speaker-context-20261101/`；观察脚本位于其父目录 `speaker_context_pilot.py`。
