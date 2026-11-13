# 1. 问题与现状

需求：最新 dev 的 EP04 整集 1fps 内部声纹验证。日期：2026-11-13。分支：`docs/ep04-voiceprint-1fps-validation`，PR 创建后回填，目标 dev。运行源码基线为 `0893780aafa3fc8deab5e7784533c52ef87fcc52`（本次开始时最新 origin/dev）；不混入未合并的 GPU 修复分支代码。

CPU 旧正式 EP04 将约 47–49 秒「祥ちゃんも悪くない／也不是小祥的错」归给高松燈，人工已确认应为长崎爽世。GPU WhisperX 整集 CUDA/FP16/thread=8 能返回匿名声源，仍把目标短句与爱音合并。旧正式任务未启用内部声纹准备。现有 dev 的 B0 `prefill` 可以在完整 Understanding 后计算声纹候选，将支持的候选交给 Refine；是否能跑通并纠正该句，需要用整集实测回答。

本次只运行已实现能力与记录证据，不预填目标真值，不改写原正式任务。优先使用独立数据库、ES 索引与媒体命名空间，完整处理 EP04，不做短片段试点。主声纹合同见 [voiceprint-trd.md](../02-agent-harness/voiceprint-trd.md)，GPU 基线见 [PR #146](https://github.com/owariband/Seekino/pull/146)。

# 2. 影响因素、假设与评价指标

| 指标 | 基线 | 本次验收 |
| --- | --- | --- |
| 执行链路 | 旧任务关闭 voiceprint | 冻结 1.0 fps、prefill、真实 CAM++ encoder；Understanding、声纹准备、Refine 完成，索引状态单列 |
| 声纹实际执行 | 旧任务没有准备成果 | ready manifest、artifact/checksum、候选/排除依据及 Refine 输入可回源 |
| 目标归属 | 旧正式结果高松燈，GPU 匿名分组仍错误合并 | 对照首次理解、声纹 top1/band、Refine 前后和最终 speaker，正确答案为长崎爽世 |
| 数据与来源护栏 | 旧版本保留 | 不把真值喂给模型；真实失败、空候选、被 Refine 改错均保留，不以 COMPLETE 或非空率代替准确率 |
| 成本与恢复 | 待测 | 保存实际模型、token、耗时与重试；不重启已有生产服务，不覆盖其库/索引 |

完整声纹参考池的质量和 Refine 是否消费证据是关键因素。当前 B0 是预计算评分路径，不能把它表述为模型临时调用 compare_speaker。

# 3. 执行方案与职责

运行边界明确为本机：API / Consumer、测试 MySQL / ES、CAM++ 与验证产物均放本机，仅通过 SSH 访问 GPU 模型服务。禁止连接或修改 CPU/prod server。此前误在 CPU 建立的准备目录与源码压缩包不再访问，临时转发已关闭；未创建测试库、容器或启动解析任务。

运行最新 dev 的独立 API / Consumer，复用 GPU WhisperX CUDA/FP16/thread=8，视觉输入 ordered_frames / 1.0 fps，按既有配置选择 Understanding/Refine 模型并记录。配置 `voiceprint_mode=prefill` 与已验证 revision 的 CAM++ speaker embedding；声纹编码按既有 CPU/FP32 合同执行、8 线程，不擅改算法或阈值。

通过正式 Series / Upload / Parse 配置入口提交完整 EP04，使用已有背景与人物资料，不提供目标句答案。固定视频 SHA、源码 SHA、有效配置与数据库/索引身份。首次理解、B0 artifact、Refine 输入/输出与最终正文分别保存。若环境或代码失败，先保存失败与最小原因，不把修过的代码冒充原始 dev 结果。

# 4. 整集执行与结果分析

2026-11-13 已经通过本机标准上传与重试入口执行整集任务 `368fec7e-b1f3-42b3-9ed6-fd0766be471b`。GPU 停机造成的连接拒绝、恢复时遗漏 `NLTK_DATA` 的启动错误均保留为运行失败记录；恢复后整集 ASR 已成功持久化，冻结 1 fps / `prefill`。本机 22:26:55（UTC+8）核实 166 条 ASR、130 条非空 speaker_id、36 条未标注、六个匿名标签。

成功输出的 cue 5（33251–48324 ms，`SPEAKER_04`）包含爱音长段与爽世目标大部分，cue 6（48324–59553 ms，`SPEAKER_03`）又从目标末尾「い」开始。**CUDA WhisperX 的目标分组未正确隔离，返回 speaker ID 不构成人物身份验收。**可版本化[实测证据](../../report/gateway/local/whisperx-cuda-speakers-20261113.json)、[B12](../../dev/problems.md#problem-b12-whisperx-cuda-20261113)与 [devlog](../../dev/devlog.md#devlog-whisperx-cuda-speaker-attribution-20261113)保留失败事实；44.163 秒是此前独立 GPU 整集 ASR 测量，不是本机整集解析耗时。

完整声纹/Refine 验收继续由 DS worker 追踪。本条尚不判定其修正效果；报告仍需列出 24 个 Chunk 和覆盖窗口状态、声纹 ready 与条目数、目标句在各阶段的 speaker/候选/分数/分档，以及实际耗时、模型调用和失败原因。没有全片真值时仅验收已确认目标，不扩展为全片准确率。

# 5. 整集验证与取舍

以完整 EP04 的一次真实解析回答问题，不采用短样本试点。保留原正式版本和本次独立任务。若能够跑通但目标仍错误，分别给出两项结论，并定位错误在参考池、评分、预填还是 Refine。运行完成不构成生产推广决定。

# 6. 验收与闭环

本次需要明确回答：1）开启内部声纹后能否跑通；2）目标短句是否正确。本次已确认 ASR 实际返回 speaker ID 且目标分组仍错误；两项内部声纹验收尚未在本条完成。阶段性问题与 devlog 已同步，当前执行状态在工作台维护；源码功能、测试、真实运行和效果分别报告。
