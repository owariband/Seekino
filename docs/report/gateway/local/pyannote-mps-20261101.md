# GPT 整集 VAD 与 pyannote Apple GPU 实测（2026-11-01）

关联：[ASR 主 TRD](../../../design/03-product-reassessment/local-whisperx-asr-trd.md)、[B20](../../../dev/problems.md#b20本地-whisperx-正式接入与字幕时间证据)、[PR #18](https://github.com/owariband/Seekino/pull/18)。本轮是同片离线组合验证；没有新跑 Whisper，没有修改旧任务的冻结输入或写入业务库。

## 问题与条件

正式 whisper.cpp Turbo 转写已持久化，但 2,476 段 speaker_id 全为 null。用户要求验证 VAD 和说话人分组能否在 Mac 执行，避免将 faster-whisper 无 Metal 后端误推成所有音频模型都不能用 Apple GPU。此次不把声源聚类当成人名识别，也不把 token 时间当成声学强制对齐。

设备为 Apple M5、10 核 GPU、16 GiB 统一内存。原片是《Let's build GPT》，业务时长 6,979,873 ms、117 Chunk；提取的 PCM16/16 kHz/单声道音轨为 6,979.941625 秒。torch/torchaudio 2.8.0、pyannote.audio 4.0.7、WhisperX 3.8.6。官方 community-1 权重固定 revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`，下载 26.409 秒，登录凭据不进入实验记录。

采用 WhisperX 使用的 pyannote Pipeline，直接传入已解码 waveform，绕过本机 TorchCodec/FFmpeg 动态库不匹配。MPS 运行时 segmentation 与 embedding 实际设备均为 mps；`PYTORCH_ENABLE_MPS_FALLBACK=0`，无不支持算子的静默 CPU 回退。聚类及部分调度仍是 CPU 工作，不声称整个 Pipeline 全在 GPU。6 CPU threads、segmentation/embedding batch 均为 32，单模型任务，未设 num_speakers/min_speakers/max_speakers。

## 验证顺序与结果

先用同一开头 120 秒分别运行 CPU 和 MPS。预设继续条件为双方自动得到一个说话人、20 ms 采样的 exclusive 语音区间不一致率不超过 0.5%。实际不一致为 0/6,000；这仅检验设备输出一致性，不是人工 diarization accuracy。通过后完整 116 分 20 秒仅跑 MPS，设 1,200 秒观察上限。

| 项目 | CPU 120 s 预检 | MPS 120 s 预检 | MPS 整集 |
| --- | ---: | ---: | ---: |
| 单调时钟推理耗时，秒 | 62.864 | 11.607 | 426.240（排除休眠） |
| 单调时钟含初始化总耗时，秒 | 66.714 | 14.722 | 429.427（排除休眠） |
| regular 自动声源簇数 | 1 | 1 | 2 |
| OS 进程峰值 RSS，GiB | 2.645 | 0.868 | 1.679 |
| 1 s 采样 MPS tensor 峰值，GiB | — | 0.677 | 0.981 |
| 1 s 采样 MPS driver allocation 峰值，GiB | — | 3.412 | 3.699 |

整集 6,979.941625 秒音轨的 pyannote MPS 按排除休眠的单调时钟计，推理 426.240 秒，含初始化共 429.427 秒；机器中途休眠，含休眠日历总耗时约 1718.468 秒。自动得到 2 个匿名声源簇，未强制说话人数；第二簇仅 9 个重叠小段、合计 1.266 秒，exclusive 输出仅一个主簇，未人工回听确认为第二个人。2,476 条原转写中 2474 条按直接时间重叠获得标签，2 条保持未知。 整集 regular 分组 1455 段、exclusive 分组 1446 段，标签为 `SPEAKER_00, SPEAKER_01`。所有最终时间有限且正向，最终声纹向量有限；短样本 CPU/MPS 都曾出现 NumPy clustering matmul 的 divide/overflow/invalid 警告，未将其抹掉或宣称库无数值风险。本轮没有人工标注参考，不能报告 DER，也不能据单次运行推定并发上限、长期稳定性或尾延迟。

本轮阶段计时必须区分两个时钟。macOS `time.monotonic()` 实现为 `mach_absolute_time()`，休眠期间不前进；库的 TimingHook 使用 `time.time()`。`pmset` 确认 10:49:37 Idle Sleep、11:11:14 完全唤醒，中间一次短 DarkWake。两口径差 1,289.041 秒与该休眠窗口一致。因此 7 分 9 秒仅表示非休眠执行时间，含休眠等待实际约 28 分 38 秒；不能把库的 1,715.281 秒推理时间判成计时 bug。此次没有为补数字再次重跑整集，后续完整性能复测应在同一命令外使用 `caffeinate -i` 防止空闲休眠，不修改整机长期设置。

逐批日志的单调时钟阶段计时（秒）：

```json
{
  "segmentation": 20.05,
  "embeddings": 403.455
}
```

其中声纹提取 403.455 秒，占非休眠推理约 94.7%；语音分段 20.050 秒，剩余约 2.735 秒为人数/聚类/重建等步骤。库的原始日历时钟计时保留如下（包含中途休眠）：

```json
{
  "total": 1715.281344652176,
  "segmentation": 20.049181938171387,
  "embeddings": 1692.4968020915985
}
```

RSS 是操作系统的进程驻留口径；MPS tensor 是活跃张量分配，driver allocation 还含后端缓存/分配。统一内存下三者口径有重叠，不能相加为独立 RAM+VRAM，也不是整机内存压力。本轮未测 GPU 利用率、功耗或并发上限。时间为单次样本；模型权重/文件缓存不冷。

此前独立全片 VAD 已完成：CPU 6 threads、batch 32、1,472,749 参数，提取 3.135 秒、加载 0.189 秒、推理 268.170 秒，总计 274.551 秒；输出 288 个合并区间，OS 峰值 RSS 1.297 GiB。测量脚本首轮曾因缺少 psutil / NumPy 整数序列化失败，修复后完整重跑，失败保留于摘要。该 VAD 没有用于重新裁剪并运行现有 Whisper；pyannote diarization 内部另有 segmentation，不能将这些独立实验数字相加冒充已接入的统一 Pipeline。

## 结果合并与验证边界

使用安装版本的 `whisperx.assign_word_speakers`，基于 exclusive 分组按直接时间重叠补 cue 的 speaker_id；fill_nearest=false。跨多个声源的 cue 数为 0。原文、cue 毫秒、全部 token/word 时间逐字段一致；既有 `validate_asr` 完整校验通过。speaker 标签代表匿名声源簇，不声明与画面人物或姓名对应。

实验产物 `asr-with-speakers.json` 保存于原 outputs 目录，原 `committed-asr.json` 字节不变。合并摘要记录源文件/分组/结果 SHA256；2,476 段及 token 时间沿用 whisper.cpp，声学强制对齐仍未执行。未覆盖旧 video_parse_content，正式任务仍读取原 speaker_id=null 的冻结成果。新的组合阶段还需要显式的配置/成果身份、失败恢复与正式 API 接线；后续应以同一 Chunk 的输入/输出对照验证标签是否实际减少 Understanding 归属歧义，不将局部可运行当成完整课程闭环。

证据：[CPU/MPS 对照](evidence/pyannote-mps-20261101/diarization-device-comparison.json)、[整集结果](evidence/pyannote-mps-20261101/full-result.json)、[进度与资源](evidence/pyannote-mps-20261101/full-events.jsonl)、[合并摘要](evidence/pyannote-mps-20261101/speaker-merge-summary.json)、[Schema 验证](evidence/pyannote-mps-20261101/schema-validation.json)、[VAD 摘要](evidence/pyannote-mps-20261101/vad-summary.json)。[实测脚本](evidence/pyannote-mps-20261101/run_diarization.py)记录本机路径和复跑参数；不会自动将实验结果导入正式任务。
