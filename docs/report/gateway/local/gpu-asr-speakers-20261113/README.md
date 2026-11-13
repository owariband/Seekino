# GPU WhisperX 声源分组部署与 EP04 验证

日期：2026-11-13。[PR #146](https://github.com/owariband/Seekino/pull/146)，分支 `fix/gpu-asr-speakers`。[TRD](../../../../design/03-product-reassessment/cloud-whisperx-diarization-trd.md)，当前执行状态由工作台 `ASR-GPU-01` 维护。

## 结论与范围

GPU 接口已接通 CUDA 分组并返回 CPU 可消费的 `speaker_id`。**原问题尚未解决：EP04 约 47–49 秒的短句仍被 community-1 合并到爱音所在声源，约 53–56 秒的爽世句属于另一声源。** 不是字段交接造成此次残余错误；原始 diarization turn 已合并了该处发言。密集采样没有改善，因此不采用。

最新执行要求是直接处理整集 EP04，CUDA / FP16、thread=8；该整集处理已经完成，不再进行短样本试点。最终结果详见 [整集 CUDA/thread=8 摘要](full-cuda-thread8-summary.json)。

旧 EP04 任务、ASR、正文和索引没有改写，也没有启动生产重解析。CPU 三处兼容/作用域改动只存在修复分支，未发布 main、未部署或重启 CPU。CPU SSH 随后的两次代理连接握手失败，直连 22 端口超时；不将离线标准化验证当作线上端到端通过。

## 环境与方法

- GPU：RTX 4090，24 GiB；WhisperX 3.8.6；ASR large-v3、float16、batch 16；ASR 与对齐沿用旧 runtime。
- community-1 revision：`3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`。复用此前已下载的 snapshot，逐文件 SHA-256 验证后上传，分组 segmentation/embedding 实际设备均为 CUDA。
- 同一 EP04 先取 `[0,90000)` ms WAV，再取整集 1420.178 秒。均 PCM16 / 16 kHz / mono，不强制人物或声源数。
- 最终整集请求的 ASR 与分组 CPU threads 均为 8，OMP/MKL 同为 8；首次部署后的其他记录使用默认 6 threads，属于此前诊断证据。
- 90 秒基线与候选按 HTTP 表单 `diarize=false/true` 调用；整集候选使用默认参数。密集对照仅将分组窗口步长改为 0.2 秒，不改线上默认。

## 实测

| 请求 / 实验 | ASR 秒 | 分组与交接秒 | 实际总秒 | 声源数 | 带标签句段 / 全部句段 | 目标句隔离 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 最终整集 CUDA/FP16/thread=8，重启后冷加载 | 30.195 | 13.498 | 44.163 | 6 | 130 / 166 | 未通过 |
| 90 秒基线，首次冷启动 | 13.840 | 0 | 13.962 | 0 | 0 / 3 | 无分组 |
| 90 秒默认分组，ASR 已热 | 1.785 | 1.641 | 3.465 | 2 | 6 / 9 | 未通过 |
| 整集默认分组，ASR 已热 | 17.490 | 12.247 | 30.110 | 6 | 130 / 166 | 未通过 |
| 90 秒密集分组，复用音轨、无 ASR | 不适用 | 3.655 | 3.655 | 6 | 仅记录 turns | 未通过 |
| 整集密集分组，复用音轨、无 ASR | 不适用 | 56.821 | 56.821 | 17 | 仅记录 turns | 未通过 |

首次基线及最终 thread=8 请求包含加载，而部分记录已热，不能据此推定分组或线程调整的速度收益。密集实验只计分组，不与包含 ASR 的整集总时长作同口径比较。不同请求的编号不可直接互认。样本量不足以报告 p95、系统准确率或长期稳定性。最终整集请求以 0.5 秒间隔采样全卡资源，已用显存峰值 9476 MiB、GPU 利用率峰值 100%；这是全卡采样值，不是进程独占或精确峰值。

整集默认原始 turns 在 `44.699–48.631` 秒仍是 `SPEAKER_04`，后续 `53.002–54.740` 秒是 `SPEAKER_03`。上游字词对齐还把目标末字「い」拉到 `48.324–53.908` 秒；多数重叠分配会产生单字切段。这份分组不能当作这句人物身份的可信约束。密集采样保留了相同的错误合并，且整集组数由 6 增至 17，放弃该参数。

## CPU 接入与验证

真实整集响应通过当前 Engine 标准化及 `validate_asr`：166 cues、130 个非空标签。90 秒有/无分组的顶层原文完全相同，展开后的原始字词、时间和分数完全相同。标签只是附加声音证据。

分支中的 CPU 最小调整：原生 `speaker` 回退、显式 `SEEKINO_ASR_SCOPE`（Docker 同步透传）、整片原生标签的 task scope 与完整 catalog。默认不设置环境变量时保留已有自动策略，历史冻结任务不变。scope 元数据只附加在已验证持久 ASR 的缓存副本上，不改数据库 schema。

定向测试两组分别为 447 passed 和 38 passed，合计 485；后一组保留已有 Starlette 弃用警告。未执行真实 CPU 新任务、姓名映射、Refine、重索引或检索回看。

## 内部声纹核验的实际接入

先前已读的 EP04 冻结配置没有 `voiceprint`、`voiceprint_mode`，按 `ParseConfig` 合同等价于关闭。当前代码的 `compare_speaker` builder 和比较方法在正式路径中没有调用方，只在定义和单测出现；直接 JSON Refine 不临时调用工具，而通过 `voiceprint_mode != off` 时宿主注入的预计算评分消费声纹证据。此次任务没有开启该链路，因此不能表述为 Agent 有工具却自行拒绝使用。后续 CPU 只读重查因 SSH 不通未完成，不声称已经核对最新工具调用日志。

这与 pyannote 无姓名聚类不同：内部声纹核验需要可信单人参考音频及对应人物，再对目标声段比较。当前数据提示应优先复核既有声纹准备/评分接线，而非继续靠更多聚类参数替代身份验证；该方向尚未在本次任务中启用或实测。

## 证据与回退

可审阅摘要：[summary.json](summary.json)、[目标片段证据](target-evidence.json)、[模型摘要](model-manifest.json)。完整响应与对照保存在本机忽略目录 `outputs/verification/gpu-asr-speakers-20261113/`，以及 GPU `/root/seekino-gpu/e01-runtime/ep04-*.json`；原始媒体不提交。

GPU 当前 wrapper 是 `/root/seekino-gpu/whisperx_server.py`，runtime 在 `/root/seekino-gpu/seekino/gateway/local/whisperx.py`；旧版保存在 `/root/seekino-gpu/e01-runtime/speaker-deploy-backup-20261113/`。新日志 `whisperx-speakers-20261113.log`，部署文件 SHA-256 记录在本次本机 `speaker-deployment-20261113.json`。本次未建立主机重启后的自启动保证。

下一步应针对此动画声源的错误合并及字词时间拉长寻找独立质量方案，再决定是否接入正式 CPU 新任务；单纯打开分组或调密采样已不能消除该失败案例。
