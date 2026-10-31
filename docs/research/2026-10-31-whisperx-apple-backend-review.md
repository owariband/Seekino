# WhisperX Apple ASR 后端与文章事实核查

日期：2026-10-31。问题：在 Apple Silicon 上保留 WhisperX 的 VAD、Cut & Merge、原音频时间窗口与强制对齐，只将其中 ASR 后端换成 whisper.cpp，能否降低本地开发成本；文章中对 batching、硬件要求及上游扩展能力的判断是否准确。

同日后续决定：用户已搁置 [PR #21](https://github.com/owariband/Seekino/pull/21)，停止 Apple WhisperX 改造及上游提案推进。以下保留当时的源码事实和候选方案，不作为当前实施计划；项目保留可选 WhisperX 体验，Apple 暂走 cpp 简单整片转写，现有 CAM++ 使用不变。最终取舍见[主 TRD](../design/03-product-reassessment/apple-gpu-asr-alignment-trd.md)。

**结论：保留 WhisperX pipeline、只替换 ASR backend 的方向成立；需要拆出目前与 faster-whisper 耦合的后端接缝。** 项目固定的 WhisperX 3.8.6 没有可直接注入任意 ASR 的统一接口。它已有独立 align API，但“整段 whisper.cpp 转录后只调用 align”只是另一种组合，不能作为用户要求的完整 pipeline 替换方案。本文据用户澄清采用“原 VAD/Cut & Merge → 持续驻留的 cpp 后端识别各窗口 → 原时间窗口 → WhisperX 强制对齐”作为研究目标，不先改 CTranslate2 或 cpp 推理内核。

ASR 后端、VAD 执行设备与对齐设备须分别理解；不能把 Metal 字符串传给 PyTorch VAD/aligner。上游提案可以围绕此边界准备，但现有相关 PR 尚不能当作已合入能力。本报告只记录资料、源码核对与候选设计，不声明本机接入已经实现或效果达标。

本次阅读了用户提供的文章原文，未改写其个人叙述。核查使用官方仓库、原论文及项目源码；未安装依赖、启动服务、执行模型或发送外部评论/PR。与本报告并行进行的实现、测试和实机验证应由对应 TRD 与验收记录报告。

## 1. 版本基线

| 对象 | 核对版本 | 本报告使用方式 |
| --- | --- | --- |
| Seekino 依赖 | WhisperX 3.8.6、faster-whisper 1.2.1、CTranslate2 4.8.4、torch/torchaudio 2.8.0 | 以本工作区 [uv.lock](../../uv.lock) 为准，不能用上游 main 代替已安装合同 |
| WhisperX main | `5d3678b8535698a2826a5db9eb3e4105aef85dd9`，提交日期 2026-10-22；包版本写作 `3.8.7rc1` | 检查未来上游提案的基线；本次不建议随接入自动升级。[commit](https://github.com/m-bain/whisperX/commit/5d3678b8535698a2826a5db9eb3e4105aef85dd9)、[pyproject](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/pyproject.toml) |
| whisper.cpp | 本地 checkout `284029c07012d2000941a08d38a31ffb71f4734e` | 核对 CLI、HTTP、Metal 选择与时间单位，不把所有后续版本一概而论。[固定源码](https://github.com/ggml-org/whisper.cpp/tree/284029c07012d2000941a08d38a31ffb71f4734e) |
| faster-whisper 官方性能表 | README 快照 `9e472ae9de8bb374552f531f752654554b76b2d6` | README 保存的对照实际使用 faster-whisper 1.1.0、whisper.cpp 1.7.2 等旧版本，不是本轮重跑。[固定 README](https://github.com/SYSTRAN/faster-whisper/blob/9e472ae9de8bb374552f531f752654554b76b2d6/README.md#benchmark) |

文章贴出的 `FasterWhisperPipeline.transcribe(..., interleaved_context=False)` 与本次查到的 main 一致。该功能来自 [PR #1474](https://github.com/m-bain/whisperX/pull/1474)，于 2026-09-26 合入；项目固定 [3.8.6 的 transcribe](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L196) 没有这个参数。main 的交错上下文包含分流、跨批次 token 上下文、首批重跑和最终排序，不只是普通 batch_size 的另一种写法。[main transcribe](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/whisperx/asr.py#L259)

main 还加入可选 Numba 和 torchaudio 缺失时的 Hugging Face 对齐模型回退；这些改动同样不等于项目 3.8.6 已有能力，也不等于已经存在 Apple ASR 后端。[main alignment](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/whisperx/alignment.py)、[main 依赖](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/pyproject.toml)

## 2. 文章中的关键判断

| 原意 | 核对结果与建议表述 | 依据 |
| --- | --- | --- |
| WhisperX 是 ASR、VAD 分片与字词时间对齐的 pipeline | 成立；还可选说话人分离。本文关心 ASR→对齐，不因此引入 diarization | [原论文 v2](https://arxiv.org/abs/2303.00747v2)、[3.8.6 README](https://github.com/m-bain/whisperX/blob/v3.8.6/README.md) |
| VAD 在做“音频清洗” | 若作为宽泛说法可以理解，但技术上应写“检测语音区间并切分/合并”。它不会把说话期间的背景音乐、噪声或重叠人声净化掉 | [3.8.6 ASR 对原 waveform 按区间切片](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L212) |
| 强制对齐让文字与时间“严格对齐” | 它根据给定文本和声学证据估计时序；输出可能失败、回退或插值。它不保证 ASR 文本正确，也不证明幻觉文字确实被说出 | [3.8.6 alignment 的失败回退与插值](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/alignment.py#L209) |
| 已发表的对齐算法比依赖 VLM 更值得试 | 对“给定文字的发声时间”使用声学对齐是匹配问题的方法选择；论文发表不保证在我们的语言、音乐或重叠语音中收益明显，VLM 的视觉/语义工作也不会因此自动消失 | [原论文的任务与实验边界](https://arxiv.org/abs/2303.00747v2)、[现有三语实测限制](../report/gateway/local/whisperx-deployment-20261029.md) |
| faster-whisper 使用 CTranslate2，Mac 原路径使用 CPU | 对当前项目成立。CTranslate2 4.8.4 的设备解析识别 `cpu/cuda/auto`，没有 `mps`；不能仅把配置改成 mps 就获得 Apple GPU ASR | [CT2 4.8.4 devices.cc](https://github.com/OpenNMT/CTranslate2/blob/v4.8.4/src/devices.cc#L19)、[WhisperX Mac 示例](https://github.com/m-bain/whisperX/blob/v3.8.6/README.md#usage--command-line) |
| batch_size 带来“指数级”加速 | 官方表能证明特定配置下数倍加速，不能证明指数规律；更大的 batch 也增加内存占用 | 见下节固定 benchmark |
| whisper.cpp “不存在 batch” | 需缩窄：它没有可直接等同于该 WhisperX pipeline 的多音频片段 batch_size 合同。内部 token/beam 批处理、音频切块并行和多音频 batch 是不同概念 | [内部 whisper_batch](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/src/whisper.cpp#L478)、[whisper_full_parallel](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/include/whisper.h#L614) |
| GPU 必须大于 8 GB、硬盘必须大于 40 GB | 不应写成通用最低要求。显存取决于模型、精度、batch 和同时常驻的阶段；磁盘还取决于 wheel、权重、缓存、容器与素材保留策略 | [官方 benchmark 的 8 GB GPU 实验](https://github.com/SYSTRAN/faster-whisper/blob/9e472ae9de8bb374552f531f752654554b76b2d6/README.md#large-v2-model-on-gpu)、[WhisperX 内存调整建议](https://github.com/m-bain/whisperX/blob/v3.8.6/README.md#technical-details-) |
| 用远端 GPU 意味着重传 10+ GB 原视频 | 若远端只做 ASR/对齐，可传本地抽出的音频；只有把整套视频解析搬去远端，才另外涉及原视频传输 | [WhisperX 音频 API](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/audio.py)、[Seekino 音频输入合同](../../gateway/README.md) |

“声素模型”可以保留为对论文术语的转述；实现层应明确是语言对应的 CTC 声学模型及其字符/标签字典，不能假设任何语言都能用同一个英语对齐器。3.8.6 对未知字符存在 wildcard 处理，因此 README 关于数字完全不能对齐的旧描述也不能无条件照搬；wildcard 得到时间并不意味着所有数字、符号的读法已经正确理解。重叠语音、错误转录和不匹配的语言仍须在素材上验收。[3.8.6 模型选择及 wildcard](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/alignment.py)

## 3. batching 的收益与可比性

官方 README 的同一组 GPU 对照是 13 分钟音频、large-v2、beam size 5、RTX 3070 Ti 8 GB、CUDA 12.4：

| faster-whisper 配置 | 转录耗时 | VRAM（保留上游 MB 单位） | 根据表格计算的变化 |
| --- | --- | --- | --- |
| fp16 | 63 s | 4525 MB | 单条基线 |
| fp16，batch_size=8 | 17 s | 6090 MB | 约 3.71 倍速度，显存增加约 35% |
| int8 | 59 s | 2926 MB | 单条基线 |
| int8，batch_size=8 | 16 s | 4500 MB | 约 3.69 倍速度，显存增加约 54% |

来源：[faster-whisper 固定 benchmark](https://github.com/SYSTRAN/faster-whisper/blob/9e472ae9de8bb374552f531f752654554b76b2d6/README.md#large-v2-model-on-gpu)。倍数与百分比是本报告对表格的算术计算，不是 Seekino 实测。同页的 whisper.cpp fp16+Flash Attention 是 65 s、4127 MB；这不能推出最新 Metal 版本在 Apple 上一定比某个 CUDA batch 配置慢多少。

从实现看，batch 把多个切片的特征堆叠后交给同一个模型批次，吞吐收益可以来自更充分的硬件利用和共享调度成本。这是解释该机制的推断，不是指数复杂度结论。原论文报告 VAD Cut & Merge 允许并行批次并改善其测试集效果，不能泛化成任意数据不损质量。官方性能比较建议也要求控制 beam size、输出文字量/WER、CPU 线程数等变量。[WhisperX 3.8.6 特征堆叠](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L175)、[论文摘要](https://arxiv.org/abs/2303.00747v2)、[官方比较要求](https://github.com/SYSTRAN/faster-whisper/blob/9e472ae9de8bb374552f531f752654554b76b2d6/README.md#comparing-performance-against-other-implementations)

whisper.cpp 的 `whisper_full_parallel(..., n_processors)` 使用独立状态切分音频并开线程，头文件还提示切块边缘的识别质量可能下降；它不是把 faster-whisper 的 `batch_size` 改一个参数就能复现。源码内部亦有 token/beam 的 `whisper_batch`，所以“完全没有 batch”过宽。另有 [PR #3485](https://github.com/ggml-org/whisper.cpp/pull/3485) 讨论共享上下文音频 batch，本次查询为 open、未合入；不能以 PR 标题或内部结构名推定有生产可用的多音频批推理。[并行实现](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/src/whisper.cpp#L7982)

CTranslate2 4.8.4 在 macOS 提供 ARM64 wheel，`WITH_ACCELERATE` 是其构建选项，但这不等于 Metal/MPS 设备支持。当前上游硬件文档已经涉及 AMD ROCm，说明“所有 GPU 永远只能是 NVIDIA”也不是稳健表述；本需求的准确边界是：项目所用 CTranslate2 路线不能直接把 ASR 放到 Apple MPS 上。[4.8.4 安装/构建说明](https://github.com/OpenNMT/CTranslate2/blob/v4.8.4/docs/installation.md)、[当前上游硬件快照](https://github.com/OpenNMT/CTranslate2/blob/78b1e1770fd3c28dbd125bbe586d90ae45e611b8/docs/hardware_support.md)

## 4. 完整 pipeline 的最小后端接缝

3.8.6 的 `load_model(..., model=...)` 参数不是通用 backend 注入点。它接受并继续使用 faster-whisper/CT2 风格的模型对象；换入只会调用 cpp 的对象，会在后续属性和方法访问处失败。需要分开以下职责，而不是复制整份 `transcribe()` 或伪造 CT2 对象：

| 当前耦合点 | 源码行为 | 建议归属 |
| --- | --- | --- |
| `transcribe()` 前半段 | 按 VAD 类型调用 `preprocess_audio`、VAD、`merge_chunks`，保留原音频 start/end；按窗口裁出 waveform | 共用的 WhisperX 编排，保留原 VAD 与 Cut & Merge 算法 |
| `Tokenizer` 与语言识别 | 访问 `model.hf_tokenizer`、`model.model.is_multilingual`；`detect_language()` 使用 mel、`encode()` 与 CT2 检测方法 | ASR backend 的语言/分词能力 |
| `preprocess()` | 读取 `model.feat_kwargs`，计算 log-mel 并补齐 | faster-whisper backend；cpp 接收 waveform 后走自己的特征计算 |
| `get_iterator()` / `_forward()` | Torch DataLoader 堆叠特征，调用 `generate_segment_batched(..., tokenizer, options)` | faster-whisper 的批处理执行；其他后端按自身能力运行 |
| 文本回填与对齐 | 把每个识别结果放回原 VAD 时间窗口，再由 `align()`使用原 waveform 对齐 | 共用的结果与对齐编排 |

来源：[3.8.6 Pipeline 构造、特征与 forward](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L108)、[VAD 与窗口切片](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L196)、[load_model](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L315)。上述“建议归属”是本报告的架构建议。

可提议一个小的波形接口：后端接收 16 kHz mono waveform 窗口及已解析的语言/task/选项，返回与输入一一对应的 text 和实际存在的可选分数；共享层掌握窗口 ID、顺序、start/end。原 faster-whisper 默认仍批量运行，cpp 初版可以顺序消费同一组窗口，不假装具备同样的批推理性能。对齐前的 `{language, segments[{start,end,text}]}` 中，时间来自 WhisperX 的窗口，不能被 cpp 自己生成的时间段覆盖。[上游现有窗口回填](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L277)

WhisperX 的 `Pyannote.preprocess_audio/merge_chunks` 等实现已经能分别调用，可复用这些组件，不重写 cut/merge 算法；但在 Seekino 自己调用这些组件，也不能声称“原 FasterWhisperPipeline 未改动就支持新后端”。若目标是让原 `whisperx.load_model()/transcribe()` 入口切 backend，需要针对上游或固定 fork 的接口重构；若先在 Seekino 组合现有阶段，也须明确属于宿主编排并证明语义等价。两者都应围绕共享阶段与后端边界实施，而不是复制 500 多行源码。[Pyannote 阶段接口](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/vads/pyannote.py#L233)

### 4.1 常驻 cpp 执行与窗口语义

最小候选是持续驻留的 whisper-server：加载权重一次，按 WhisperX 已合并的窗口接收 WAV 请求，返回窗口文本。cpp 的 tokenizer、mel、encoder/decoder 留在 cpp；WhisperX 保留 VAD、Cut & Merge、窗口归属和强制对齐。此路径无需每个窗口重新启动 CLI/加载模型，也不需要复制 CT2 的 batch 内核。该固定 server 使用共享模型与请求锁，逐窗口 HTTP 调用不能描述成真正的 GPU 多音频 batch。[server 模型与请求入口](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/examples/server/server.cpp)

窗口文本请求可用 `response_format=json` 及 `no_timestamps=true`、`token_timestamps=false`，由共享层持有时间；是否使用 verbose_json 只为额外诊断，不是对齐必需。应关闭 cpp 自己的 VAD，避免又切一遍已由 WhisperX 确定的窗口；不要启用 translate。该版本 server 默认 `no_context=true`，每次推理清除前文状态，有利于维持 3.8.6 默认独立窗口语义；共享进程跨文件时仍需验证没有 prompt 或状态泄漏。[请求选项与输出](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/examples/server/server.cpp#L480)、[上下文重置](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/src/whisper.cpp#L7103)

语言应按整次输入确定，然后传给每个窗口；不要让每个短窗口各自 auto 检测而改变语言策略。原 3.8.6 使用音频最初 30 秒检测语言，cpp backend 需要独立实现对应能力或要求调用方指定语言。cpp 默认 en；`language=auto` 是转录自动识别，`detect_language=true` 则检测后立即返回，可用于单独检测步骤，不能误当正常转录请求。server 的语言名是全名，要转换到 WhisperX 的语言代码。[WhisperX 语言检测](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/asr.py#L302)、[cpp 检测分支](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/src/whisper.cpp#L7004)

这一方案仍须解决模型实例身份和生命周期。请求 model 名称不能替代服务实际加载权重的核验；可以评估受管理的固定二进制/权重进程，或为已有服务提供可核验实例信息。本次不据此决定最终部署形态。固定 server 已注册 `abort_callback` 检查 HTTP 连接是否关闭，因此存在协作式中断机制；必须实测断开、后端停止、后续请求和资源释放，不能把客户端抛出超时直接当作原生推理已经结束。[协作取消](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/examples/server/server.cpp#L991)

“整段 whisper-cli/whisper-server → 直接 align”的方式只能验证两个库的输出接缝，绕过了用户要求保留的 WhisperX VAD/Cut & Merge。对每个窗口启动一次 CLI 则带来重复加载，因此不作为当前主方案。CLI JSON 的 `result.language` 为代码、`offsets` 为毫秒等事实仍可用于独立诊断，但不应驱动本需求的窗口设计。[CLI 输出合同](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/examples/cli/cli.cpp#L624)

### 4.2 设备、结果与资源合同

VAD 与 alignment 是 PyTorch 能力，ASR 的 cpp Metal 选择不能传给它们。第一轮可以保持原 VAD 模型与 CPU 执行，独立验证 alignment 的 MPS；VAD 本身是否迁到 MPS另行实测，不因 ASR 后端变化偷偷切换其模型/算法。3.8.6 alignment 前向后把 emission 转回 CPU，trellis/backtrack 仍在 CPU，因此 mps 不是整条强制对齐全 GPU 的保证。[VAD 加载](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/vads/pyannote.py#L21)、[对齐设备处理](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/alignment.py#L80)

Metal 必须核对实际后端选择。`use_gpu=1`、JSON systeminfo 含 MTL 或二进制有 Metal 支持只代表请求/能力；`whisper_backend_init_gpu: using MTL0 backend`、设备名与模型缓冲区分配日志能提供更直接的运行证据。若显式请求 Metal，应避免悄悄接受 CPU 回退；日志也不应被解释成每个算子都在 GPU 上执行。[后端选择源码](https://github.com/ggml-org/whisper.cpp/blob/284029c07012d2000941a08d38a31ffb71f4734e/src/whisper.cpp#L1300)

`align()`使用原 16 kHz mono waveform 和窗口秒时间，返回时不包含 language，共享运行时须保存它。项目毫秒与原视频偏移继续按上层合同转换；保留本地权重、Punkt 预检及未对齐词处理。cpp token probability 不是 alignment score，不得填造；后端不支持的 suppress_numerals、hotwords、interleaved_context 等选项也不应静默忽略。[独立入口与 Schema](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/schema.py)、[3.8.6 alignment](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/alignment.py)、[现有 Gateway runtime](../../gateway/local/whisperx.py)

更换执行路径不会自动减掉依赖：3.8.6 的 pyproject 仍声明 faster-whisper、CTranslate2、PyTorch 等。第一轮保留版本固定，先证明完整窗口链路和结果合同；依赖瘦身可以在有明确收益时另做。[3.8.6 依赖](https://github.com/m-bain/whisperX/blob/v3.8.6/pyproject.toml)

如果只把音频送远端，16 kHz、mono、PCM16 的数据率是 32,000 byte/s；116.33 分钟约 223 MB（十进制，未含很小的 WAV 头），这是按格式计算的估计，并非本轮上传实测。压缩音频还可更小。远端整套开发环境依然有配置与维护成本，但不必把原始视频体积直接当作远端 ASR 的必传体积。[音频格式依据](../../gateway/README.md)、[WhisperX 16 kHz 音频入口](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/audio.py)

## 5. 上游 backend/device 提案的现状

以下是 2026-10-31 的 GitHub API 状态快照。搜索范围包括 WhisperX 的 backend、mlx、mps、whisper.cpp、align_device、alignment_device、router 和相关 device PR；搜索结果有边界，不声称覆盖所有外部 fork。

| 一手记录 | 查询状态 | 能说明什么 |
| --- | --- | --- |
| [Issue #109：M1 GPU](https://github.com/m-bain/whisperX/issues/109) | open | Apple GPU 是已存在的需求，但历史报错不能替代当前版本验证 |
| [Issue #345：MPS / whisper.cpp](https://github.com/m-bain/whisperX/issues/345) | closed | [维护者说明](https://github.com/m-bain/whisperX/issues/345#issuecomment-2545031708)为重复 #109 而关闭；不是功能完成 |
| [PR #1185：MLX backend](https://github.com/m-bain/whisperX/pull/1185) | closed，merged=false | 没有进入 main；PR 自述的性能不作为本项目证据 |
| [PR #1401：Qwen3 ASR / ForcedAligner](https://github.com/m-bain/whisperX/pull/1401) | open，未合入 | 已有人提出显式 `--asr_backend`，但该提案还同时更换 aligner，不能当作 cpp adapter 已存在 |
| [PR #1373](https://github.com/m-bain/whisperX/pull/1373)、[PR #1476](https://github.com/m-bain/whisperX/pull/1476)：diarize_device | 均 open、未合入 | 支持把阶段设备拆开的动机；它们处理 diarization，不等于独立 alignment device 已交付 |
| [PR #1483：非 CUDA 设备自动发现](https://github.com/m-bain/whisperX/pull/1483) | open、未合入 | 作者明确未做完整 ASR E2E。识别到 PyTorch 加速器不意味着 CTranslate2 能执行该设备 |
| [PR #1474：interleaved context](https://github.com/m-bain/whisperX/pull/1474) | merged，2026-09-26 | 对应文章所贴新代码；与 Apple backend 路由是不同工作 |

在查到的 main 固定提交中，CLI 仍只有共享 `--device`，`transcribe.py` 仍从 `whisperx.asr` 导入 `load_model`，对齐和 diarization 复用该 device；没有实际接入 cpp/MLX 的后端路由。因此可以准备上游改进提案，但不能把一个新的 `auto` 设备选择函数当作全部问题的修复。[main CLI](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/whisperx/__main__.py)、[main 实际调用链](https://github.com/m-bain/whisperX/blob/5d3678b8535698a2826a5db9eb3e4105aef85dd9/whisperx/transcribe.py)

建议未来提案控制在两个独立接口：

1. **ASR backend 合同：** 保持默认 faster-whisper，先从通用 VAD/Cut & Merge 编排中分离其 tokenizer、mel、CT2 批处理。后端消费共享层给出的 waveform 窗口并返回对应文本/语言，时间归属由共享层维护。所有后端复用原 VAD 与窗口，不再要求它们伪装具有相同的 batch、compute_type、上下文滚动功能；不支持的显式选项应报错或清楚限定。
2. **alignment device 合同：** 允许对齐设备覆盖原 device，未设置时保持既有行为。显式验证 MPS 可用性和具体模型前向；ASR 的 CPU/CUDA/Metal 与对齐的 CPU/CUDA/MPS 分开表达。先不把 diarization 变更混入本次范围。

这属于候选设计，不是上游承诺，也不是已创建的 PR。Seekino 的 `infra/inference/speech/` 可承接本地模型组合与生命周期、由 Gateway 接入；是否通过固定上游 fork 完成 backend 接缝，或先复用阶段组件做宿主编排，应在 TRD 明确，不能混称已经改造原 pipeline。后续上游化时去掉项目业务依赖，带上同版本回归、真实模型与失败场景证据，再向维护者说明与 #1401 等提案的关系。本次未提交 issue、评论、上游 PR 或发布包。

## 6. 证据和未验证边界

本报告已完成原文、项目锁文件、固定版本接口、main 调用链、官方 benchmark 和上述 GitHub 状态的静态核查。文章的主要决策依据可以保留：先获得本机可用的 ASR→强制对齐闭环，再评估远端开发环境；无需为这一步先重写 cross-attention 或多音频批处理内核。

实现验收仍需分别证明：同一音频、同一 VAD 模型和参数产生同一组 Cut & Merge 窗口；每个 cpp 文本准确回填原窗口，空结果/失败不造成错位，且整条链路确实进入对齐与正式产品入口；旧 CPU/CUDA 配置兼容；超时/取消后无晚到结果提交且所持资源可回收；缺失权重或语言资源不临时下载；真实语言、静音、短片段与重叠语音的文本覆盖、未对齐比例和时间边界；CPU/MPS 对齐的总耗时、内存与质量差异。完整耗时应包含 VAD、窗口编码/传输、ASR、对齐和需要的加载成本，不能直接套用整段 cpp ASR 的历史数字。测试通过、原生模型可运行和业务质量达标应分别记录，不把上游论文、PR 自报或单次 ASR 耗时当作全部验收。

-codex
