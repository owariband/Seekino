# M5 上 GPT 课程的 Metal ASR 实测

日期：2026-10-30，Asia/Shanghai。用户要求核实本机 GPU、内存和真实转写速度。关联 [B20](../../../dev/problems.md#b20本地-whisperx-正式接入与字幕时间证据)。本轮没有修改正式模型配置、重启 API 或写入解析结果。

随仓库保存的[指标、参数及哈希摘要](whisper-metal-benchmark-20261030.json)供 PR 审阅，不包含原课程正文、音频或凭据。后文忽略目录中的原始大文件只在本机保留；它们不是已经发布的测试 fixture 或生产 checkpoint。

## 硬件与运行条件

`system_profiler` 确认 MacBook Pro、Apple M5、10 核 GPU、10 核 CPU（4 性能＋6 能效）、16 GB 统一内存、Metal 支持。`hw.memsize` 为 17,179,869,184 bytes。GPU 与 CPU 共用内存，没有独立显存；本轮 Metal 日志的建议工作集为 12,713.12 MB（源码按十进制 1e6 换算），不是空闲内存或可独占额度。测试前系统 swap used 为 6,536.50 MiB，不能据此量化 ASR 被换页拖慢多少。

本机正式 WhisperX 使用 CTranslate2 4.8.4、完整 large-v3、CPU/int8、batch 1、6 线程，并执行强制对齐。CTranslate2 的预编译 GPU 路径为 NVIDIA CUDA，无法通过配置 `mps` 调用 M5 Metal；见[官方硬件支持](https://opennmt.net/CTranslate2/hardware_support.html)。因此使用已有 whisper.cpp 测试 Metal，并另外做同模型 CPU 对照。它不是“同一 WhisperX 模型开关 GPU”的对照。

## 协议与结果

输入为《Let's build GPT》原片 `[0,60000)` 和 `[1200000,1500000)` 毫秒，重新提取 16 kHz 单声道 PCM。权重 `large-v3-turbo-q5_0`，SHA-256 `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`；英语、beam 5、best-of 5、max_context 0、temperature 0、temperature_inc 0，输出 verbose_json 与 token timestamps。无 VAD、无声学强制对齐。独立服务仅监听 `127.0.0.1:18844`；GPU/CPU 顺序执行，仅 CPU 组增加 `--no-gpu`。

| 后端与样本 | 音频长度 | 请求耗时 | 相对实时速度 | 归一化校验 |
|---|---:|---:|---:|---|
| Metal，课程开头，首次请求 | 60 s | 3.760 s | 15.96 倍 | 通过，22 cues／237 words |
| Metal，20:00–25:00 | 300 s | 12.992 s | 23.09 倍 | 通过，104 cues／992 words |
| Metal，课程开头，重复请求 | 60 s | 3.474 s | 17.27 倍 | 通过，22 cues／237 words |
| CPU，同一开头、同模型参数 | 60 s | 17.196 s | 3.49 倍 | 通过，22 cues／237 words |

独立服务启动至可连接：Metal 1.019 s、CPU 0.418 s，未包含在请求耗时中。Metal 日志确认 `use gpu = 1`、`Apple M5`；CPU 日志确认禁用 GPU。每秒采样的服务进程峰值 RSS：Metal 892,534,784 bytes（约 0.83 GiB）、CPU 974,143,488 bytes（约 0.91 GiB）；RSS 不等于专用显存占用，也不是精确峰值 GPU 分配量。两个测试进程均已退出，正式 API PID 40888 保持运行。

同模型、同音频的 Metal 请求约快 4.57–4.95 倍，CPU/GPU 转写全文相同；segments 含时间和概率，整体 JSON 不完全相同。四份输出均无空词和反向词时间，均通过实际 `normalize_transcription`，没有清洗后冒充原始结果通过。没有人工逐字金标或声学边界标注，不能声称 WER、对齐质量或全片漏识别已通过。

按 5 分钟样本线性外推，116 分 20 秒整集约需 5.04 分钟纯转写；这仅是估算，不含强制对齐、不代表新鲜整集实测，也不保证长音频退化或不同负载下的表现。当前证据证明本机存在明显更快的转写执行路径，不能把正式 CPU large-v3 的时长当作硬件能力上限。

## 正式任务同时发生的进展

GPT v5 `650f7c9c-1e69-43b9-ac63-12bd355ede1e` 于 14:42:41 完成原 WhisperX 整片 ASR，日志计时 4,591.557 s，即 76 分 31.557 秒。14:44:02–14:44:14 出现十个不同 Understanding execution 及十个模型调用开始记录；随后 API 返回 PARSING、117 个 Chunk、`activeExecutions=10`。本轮基准开始于该次 ASR 完成之后；没有用 GPU 结果替换它。完整 Understanding、Refine、索引和检索回看追问仍待验收。

## 证据与复现

脚本：[gpu_benchmark.py](../../../../outputs/verification/gpt-full-demo-20261030/gpu_benchmark.py)。原始证据位于本机忽略目录 `outputs/verification/gpt-full-demo-20261030/gpu-benchmark/`：`protocol.json`、`results.json`、`summary.json`、`events.jsonl`、CPU/Metal 服务日志、资源采样、原始转写和原音频片段、`production-status.json`。模型和输入哈希已记录。重复运行会覆盖同名输出，应先选择新的输出目录。

未进行业务代码修改，无需运行无关单元测试。本轮验证是四次真实本地模型请求及现有归一化代码校验，不是正式 Gateway／全片闭环验收。后续接入需保留当前字词证据和强制对齐合同，验证长音频、错误恢复与持久化；不要把独立基准误报为正式配置已经切换。

## 15:08 后续：整集 GPU 实测与正式任务失败

用户要求验证全量。独立脚本 `outputs/verification/gpt-full-demo-20261030/gpu_full_episode.py` 对 6,979.873 秒原音轨使用相同 Turbo Q5、Metal 和解码参数，单路完整转写；15:03:05 开始请求，15:07:59 返回。音轨提取 5.854 秒、模型服务启动 0.821 秒、完整转写请求 **293.578 秒（4 分 53.578 秒）**，三阶段合计 **300.253 秒（约 5 分钟）**；相对实时 23.78 倍。服务每两秒采样的峰值 RSS 2,551,758,848 bytes（约 2.38 GiB），服务已正常退出。

返回 2,476 条 cues、24,088 个字词时间项、111,219 个文本字符。首条开始为 0 ms，末条结束为 6,979,640 ms；实际 `normalize_transcription` 全量通过，无空词和反向词时间。原始与规范化结果完整保存在 `gpu-full-episode/raw.json`、`normalized.json`，其 SHA-256 和每分钟覆盖审计见 `coverage-audit.json`。这是全片转写和结构校验成功，不包含强制对齐、人工 WER/CER 或整集 Understanding/索引验收。

原正式任务于 14:49:19 收到 Kimi 504；有限节点重试期间继续遇到连接问题，14:51:49 终止异常链为 `APIConnectionError → ConnectError → gaierror: [Errno 8] nodename nor servname provided, or not known`。最终 PARSING_FAILED，保留 8/117 个已提交 Chunk。后续只读 DNS 检查已能解析 `api.kimi.com`，无授权请求 /models 返回 401，说明检查时 DNS/HTTPS 可达，不能证明生成请求已稳定。没有证据把本次 DNS 失败归于之前的 Clash 关闭操作，也没有重启 API 或重新派发会再次执行 CPU ASR 的任务。证据：`kimi-failure-1451.json`。

恢复缺口在源码 `VideoParseRuntimeService.prepare` 的 finally：协调器退出即清理 `_video_transcripts`，不只是 API 重启才丢失。8 个已提交 Chunk 的 evidence 不能替代完整 ASR checkpoint。此次 GPU 输出落盘只保障实验结果保留，正式任务尚不会自动读取；需要在 Server 持久层按源视频、模型/解码配置和内容版本管理视频级 ASR 产物，成功落盘后才开放下游任务，并在新租约重试时校验复用。

当前 Kimi ordered-frames 路径只发送图片与文本，无原音频。VLM 可利用代码/课件校正术语，但不能把无法听音核实的补全视为源片事实。小模型质量及 10 路本地 ASR 吞吐未测；现有 WhisperX runtime 和 whisper.cpp HTTP 服务均用锁串行保护模型，不能将 10 个提交请求视作 10 路实际推理。下一步应优先接入可恢复的视频级 GPU ASR，再针对不确定片段做局部重识别与证据保留，而非依赖 VLM 覆写原始转写。
