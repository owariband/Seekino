# WhisperX Python 接缝与《Let's build GPT》整课验证

日期：2026-10-31，北京时间 UTC+8。关联 [主 TRD](../../../design/03-product-reassessment/apple-gpu-asr-alignment-trd.md)、[B24](../../../dev/problems.md#b24-apple-gpu-asr)、Draft PR #21。本报告验证隔离原型；正式 Gateway、依赖锁和运行配置尚未接入，未操作业务数据库或解析任务。

同日后续决定：用户要求搁置 PR #21，将精力收回产品闭环。报告保留短样本成功与整课中止证据，不继续运行 VAD/MPS/强对齐或 Pyannote 替换实验；不将原型测试通过写成正式功能完成。

## 问题与现状

用户提出保留 WhisperX VAD、Cut & Merge 和强制对齐，只把音频窗口的 faster-whisper 调用换成已有 whisper.cpp 模型服务，初版接受 batch_size=1。此前将通用后端框架、进程管理、全部解码参数兼容及上游化一起计入最小接入，进而估计工作量偏大，没有先验证实际耦合。

本轮先回答小补丁能否跑通完整链路、旧路径是否改变，再按用户指定使用《Let's build GPT》完整音轨测一次总耗时与输出合同。声学对齐的人工边界误差、识别准确率和产品检索闭环不由“模型返回成功”代替。

## 方案候选与实际改动

在 WhisperX 3.8.6 的隔离副本内给 `load_model` / `FasterWhisperPipeline` 增加可选 `segment_transcriber`。未提供回调时保留原 CT2 路径；提供回调时绕过 CT2 初始化、tokenizer 和 Mel/batch 解码，逐窗口发送 waveform 并把文字放回原 VAD 时间。共用 VAD 加载、切分合并、结果回填、进度与原对齐 API，没有修改 C++。

实际核心补丁为 `asr.py` 新增 69 行、删除 31 行，净增 38 行；HTTP 调用适配器 44 行。测试与实验脚本另计。原型要求显式语言、transcribe、batch_size=1，不支持的 CT2 选项明确拒绝，不伪造 avg_logprob。VAD/align 使用 CPU，cpp 使用 Metal；不以此要求先实现通用设备路由。

本机运行复用固定 whisper.cpp 二进制和 Turbo Q5_0 权重，为实验启动独立回环地址服务；服务加载一次并复用所有窗口。全课原始转写保存后释放 cpp，再加载对齐器，减小同时驻留的资源。共享虚拟环境不打补丁，正式配置不切换。

## 方案验证

### 短样本与旧路径回归

已有英文原片 12 秒片段重复两次，中间加 2 秒静音，得到 26 秒输入。chunk_size=12 下，原 Pyannote VAD/Cut & Merge 得到 3 个窗口，各发一次 cpp 请求；逐一核对 waveform SHA-256 与原音频窗口相同。对齐得到 84 个词，均有分数和有效时间，第二段音频仍保持 14 秒后的偏移，并通过现有 Engine 毫秒归一化。

- 13 项定向测试通过，23.78 秒（含依赖导入）：包含 6 项原版/修改版默认路径对照，以及窗口传递、无声和不支持选项。
- 新路径从依赖导入后计时 7.010 秒，含临时服务启动、VAD、ASR、对齐及资源回收；不是解释器冷启动基准。
- 原版和修改版均使用同一 CPU/int8 large-v3、batch_size=1 和同一 26 秒输入实跑：文本、avg_logprob、VAD 窗口与进度完全一致；两次对照合计 73.246 秒，含子进程启动和导入。
- 原安装目录 asr.py 的 SHA-256 与实验前一致。证据：[短样本报告](../../../../outputs/verification/whisperx-cpp-spike-20261031/REPORT.md)、[核心补丁](../../../../outputs/verification/whisperx-cpp-spike-20261031/asr.patch)、[回归结果](../../../../outputs/verification/whisperx-cpp-spike-20261031/legacy-regression.json)。

### 整课实验条件

复用 2026-10-30 从 `07 - Building GPT.mp4` 提取的完整音轨，时长 **6979.941625 秒（116 分 20 秒）**，16 kHz / mono / PCM16。执行前校验音频、模型、二进制和原型代码的内容哈希。

| 对象 | 固定条件 |
|---|---|
| 音频 SHA-256 | `6d12244a2696b4cd9262715bf35febaed5509013c6be5bb3bdc84a154dc6143d` |
| WhisperX | 3.8.6 隔离副本，原型 asr.py SHA-256 `cfeb6de6700b2f110c544af93339237b467240332fa984f1cde8f456d614955b` |
| whisper.cpp | commit `284029c07012d2000941a08d38a31ffb71f4734e`，使用现有 whisper-server |
| ASR | large-v3-turbo Q5_0 / Metal，英语，逐窗口请求；beam/best_of 5、temperature 0、无温度回退、max_context 0、6 线程、Flash Attention |
| VAD | 原 Pyannote / CPU；WhisperX 30 秒 Cut & Merge；cpp 自身 VAD 关闭 |
| 对齐 | 现有英语 wav2vec2-base-960h / CPU；原 WhisperX align；无声学分数的插值时间按现有 Gateway 规则移除 |
| 资源 | Apple M5 / 16 GiB；每 5 秒采样 Python 与自有 cpp 进程 RSS/CPU、系统 swap |
| 执行范围 | 一次整课探索性验证；独立资产目录，不写正式任务，不重跑旧 CPU 整课 |

原始 ASR 分阶段保存，每个窗口的音频哈希、文字和耗时另有 JSONL。预先检查输入身份、窗口对应、时间轴、末段覆盖、毫秒归一化和进程回收；人工质量阈值仍待标注，不把本轮直接当成质量验收。协议见[实验计划](../../../../outputs/verification/whisperx-cpp-gpt-full-20261031/experiment-plan.md)。

## 指标对比与边界

**整课实验于 12:18 按用户要求停止。** 总运行 411.221 秒（约 6 分 51 秒，含依赖导入），停止时仍在原 Pyannote 的 CPU VAD，VAD 已运行约 403 秒且尚未返回。没有发出 ASR 请求，没有进入 alignment，也没有生成整课字幕。中断栈在 `PyanNet → SincNet → torch.conv1d`；这证明停止时在 CPU 模型计算，不是 cpp 请求等待。完整 VAD 耗时及剩余时间未知，不用 26 秒样本外推完整课 ETA。

临时 cpp 进程已 terminate + wait 回收。原始执行记录保留 KeyboardInterrupt/退出 130；[处置记录](../../../../outputs/verification/whisperx-cpp-gpt-full-20261031/disposition.json) 区分用户主动停止与模型故障，[中断栈](../../../../outputs/verification/whisperx-cpp-gpt-full-20261031/failure.txt) 和资源记录均保留。不能把这次整课验证写为通过，也不会自动重启或切换设备重跑。

用户进一步明确关注“全视频范围的说话人归因”。本原型只执行 VAD、ASR 与强制对齐，没有执行 WhisperX 的独立 `DiarizationPipeline/assign_word_speakers`。词时间对齐与跨窗口说话人一致性是不同指标；现有短样本证明的接口可运行性不能替代身份归因收益验证。实际先需要区分视频级结果复用、ASR 内部短窗、全片 diarization 及匿名说话人到业务人物的映射；不因为“整片一次调用”就宣称模型具有全片上下文或人物识别能力。

历史同音频、同 cpp 二进制与 Turbo 权重的纯 ASR 为 182.360 秒。新流程增加 WhisperX VAD、逐窗口请求和强制对齐，而且关闭 cpp 自身时间 token 解码；两者不是单因素性能对照。旧 full large-v3 CPU 全流程 4591.557 秒还涉及模型及量化差异，不能直接计算“同质量加速比”。

本轮尚不证明 CUDA 回归、自动语言、多语种、MPS、异常取消、长时间稳定运行、正式 Gateway/Server 接线、持久恢复或 Upload-to-Search 闭环。原始文字、字幕和音频留在 Git 忽略的验证目录，报告只提交必要指标与证据入口。
