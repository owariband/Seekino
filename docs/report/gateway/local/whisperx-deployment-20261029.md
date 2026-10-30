# WhisperX 三语部署与 Karpathy 解析启动

记录时间：2026-10-29，Asia/Shanghai。环境：共享 dev 工作区、macOS arm64、16 GB 内存、Python 3.12、MySQL `seekino_development`。本报告记录部署和启动证据，完整系列解析与字幕产品效果仍待验收。状态入口为 [B20](../../../dev/problems.md#b20本地-whisperx-正式接入与字幕时间证据)，设计见 [TRD](../../../design/03-product-reassessment/local-whisperx-asr-trd.md)。

## 运行时与权重

项目 `.venv` 已安装 WhisperX 3.8.6、faster-whisper 1.2.1、CTranslate2 4.8.4、PyTorch/torchaudio 2.8.0。命令为 `uv sync --frozen --inexact --extra whisperx --extra speaker`，保留其他会话已有依赖。ASR 采用完整 large-v3 架构，CPU/int8、batch 1、6 线程；未对比 int8 和 float32 的质量差异，也未验证 CUDA。

| 本地目录 | 上游仓库 | 固定 revision | 推理文件体积 |
|---|---|---|---:|
| `align-en` | `facebook/wav2vec2-base-960h` | `22aad52d435eb6dbaf354bdad9b0da84ce7d6156` | 0.352 GiB |
| `align-zh` | `jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn` | `99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd` | 1.189 GiB |
| `align-ja` | `jonatasgrosman/wav2vec2-large-xlsr-53-japanese` | `cf031e020336460d15a417eba710bbc5bb43be9a` | 1.184 GiB |
| `large-v3` | `Systran/faster-whisper-large-v3` | `edaa852ec7e145841d8ffdb056a99866b5f0a478` | 2.879 GiB |

以上目录在仓库 `outputs/local-models/whisperx/` 下。所有下载文件经过 SHA-256 核验，VAD 使用 WhisperX 自带 checkpoint。NLTK punkt_tab 从官方固定来源取得并核验发布的 SHA-256；没有关闭其代理安全限制。`NLTK_DATA`、`TORCH_HOME`、`MPLCONFIGDIR` 已配置为项目内目录，解决首次实测的默认缓存权限失败。

Whisper 只需一套多语言权重；wav2vec2 对齐器分别使用英语、中文、日语字符/音素词表。本地模型目录与 tokenizer 完整，验证时设置 `HF_HUB_OFFLINE=1`。

## 实际验证

正式 `GatewayClient.create_transcription_model → transcribe → align → Engine normalize_transcription` 的实测结果：

| 语言 | 音频长度 | 单次调用耗时 | 有效时间字词/全部字词 | 样本 |
|---|---:|---:|---:|---|
| en | 12.000 s | 28.30 s | 42/42 | 真实原片 |
| ja | 10.000 s | 13.07 s | 9/9 | 真实原片 |
| zh | 9.336 s | 26.98 s | 38/38 | 合成语音 |

英语为 Micrograd 原片 600–612 秒；日语为 MyGO 第 4 集 45–55 秒；中文为 macOS Tingting 合成语音。英语/中文耗时含进程内首次加载，日语复用 ASR 但首次加载对应 aligner。三次调用均通过语言检测、非空输出、有效字词范围和整数毫秒检查；未测人工边界误差、分位数吞吐或峰值内存。中文把“字幕”识别为“字母”，完整错误保留，对齐成功不能证明转写准确。torchcodec 的外部 FFmpeg 解码器仍有加载警告；当前通路传入预解码 waveform，三语调用成功，不将其报告为通用解码可用。

安装后相关回归：`tests/gateway/test_whisperx.py`、`tests/gateway/test_llama_cpp_embedding.py`、`tests/engine/common/test_transcription.py`、`tests/server/test_text_only_embedding.py`，**75 passed**。此前合同测试结果保留在 [devlog](../../../dev/devlog.md#devlog-whisperx-20261029)。

等待用户指定的「课程闭环进行」完成后，再独立调用本地 llama.cpp embedding，验证英文 document 与中文 query 均为 1024 维、L2 norm 1.0。服务 `127.0.0.1:18843`，模型别名 `seekino-qwen3-embedding-0.6b`，文本模式。

## 开发服务与系列提交

`config/local-dev-config.yaml` 已选择 `whisperx_local` 和 `llama_embedding_local`，其他生成模型仍为原配置。通过 `./start.sh --branch dev start` 重启服务并完成存储初始化；API 9621、Web 5173。为这台机器配置 video concurrency 1、parse concurrency 2、index concurrency 1，WhisperX 共享 runtime 串行执行。

新向量空间：`seekino_video_chunks_development_qwen3_20261029`、`seekino_video_events_development_qwen3_20261029`，generation 2。旧 Ark 索引未删除、旧视频未重跑；旧索引中的向量不会自动出现在新空间，不能把它们直接作为 Qwen3 向量使用。当前为文本 embedding，不产生视觉向量。

Karpathy 8 集原片共约 14.61 小时、2.58 GiB，全部重新核对源文件 SHA-256 后通过分段 Upload API 提交。每集使用稳定幂等键，保存服务返回的冻结配置、upload/video/task ID。Series ID：`1abab59d-f52c-5a2a-a23a-c20b6c2ece09`，租户 `local-default`。首集任务：`1c886312-6543-423c-9038-903095630c5f`。8 个任务均已持久化；首集已经完成整集抽帧，进入前两个 Chunk 的正式解析，WhisperX 日志检测英语成功。**提交和开始不等于全系列完成。**

启动观察中 Kimi 视觉请求先出现 `RemoteProtocolError: Server disconnected without sending a response`，另一次出现 HTTP 504；消费者已按既有策略安排重试。截至该快照，尚无首个 Chunk 内容提交，不能宣称 VLM/索引端到端已通过。该错误属于视觉模型请求，不能用本地 ASR 验证通过来掩盖。保留当前日志和任务状态，完整 VLM/索引链路继续观察。

## 证据与恢复入口

原始证据保存在仓库忽略目录 `outputs/verification/whisperx-deploy-20261029/`，本报告提供可跟踪的结果摘要：

- `model-manifest.json`：仓库、固定 revision、逐文件字节数与 SHA-256。
- `samples.json`、`result-en.json`、`result-ja.json`、`result-zh.json`、`validation-summary.json`：样本、原始输出、规范化结果、耗时。
- `embedding-preflight.json`：独立 embedding 调用检查。
- `karpathy-manifest.json`、`series.json`、`complete-01.json` 至 `complete-08.json`：原片与提交身份。
- `submitted-profile.json`、`tasks-current.json`、`persisted-evidence.json`：配置与运行时快照；执行时间见文件内字段。
- `config-before.yaml`、`env-before-selected.json`：切换前配置和本次改动的非密钥环境项。恢复旧来源时需要成对恢复模型绑定和对应索引；已提交任务仍按冻结配置运行，不能只改 YAML 就声称旧任务已切换。
- `submit_karpathy.py` 可恢复分段上传，校验活动配置与首次快照一致，复用幂等键；`inspect_run.py` 只读核对本系列的 MySQL 状态和已持久化 ASR evidence。

现有进程继续运行。完整系列的识别/检索质量、重启恢复、未对齐词占比与字幕分行/播放/导出未验收。字幕数据基础已接通，完整字幕产品仍需消费层实现。
