# 本地 whisper.cpp Turbo＋Metal 接线验证（2026-10-31）

关联：[主 TRD](../../../design/03-product-reassessment/local-whisperx-asr-trd.md)、[PR #18](https://github.com/owariband/Seekino/pull/18)。代码在 `fix/video-asr-recovery`，HEAD `67223c16`＋当前未提交改动；没有运行业务库迁移或课程任务。

## 实际配置与调用

主工作区 `.env.local` 仍指向 `config/local-dev-config.yaml`，只将 transcription 默认切至 `whisper_cpp_local`／`large-v3-turbo-q5_0`，添加对应 loopback 音频来源及客户端占位 key。旧 WhisperX 来源保留，未选中；其他模型／解析并发配置未修改。新 whisper.cpp 任务默认 `scope=video`，旧冻结任务不变。

调用经过 `GatewayClient.create_transcription_model()`、既有 `openai_audio`、`POST /v1/audio/transcriptions`、Engine 毫秒归一化和持久 ASR Schema 校验。whisper.cpp 的 `verbose_json` 实际兼容该路径，未写新协议包装层。

```bash
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python \
  /Users/hypnos/.codex/worktrees/full-video-asr/Seekino/scripts/local_whisper.py \
  --config /Users/hypnos/workspace/agent/Seekino/config/local-dev-config.yaml \
  --binary /Users/hypnos/workspace/agent/Seekino/outputs/tools/whisper.cpp/build/bin/whisper-server \
  --model-dir /Users/hypnos/workspace/agent/Seekino/outputs/local-models/whisper \
  --require-metal
```

该实例监听 `127.0.0.1:18842`，仅加载一份模型，processors=1、CPU threads=6、flash attention、max-context=0、beam-size=5、best-of=5、无温度 fallback、language=auto。`--require-metal` 从实际 backend 日志拒绝 CPU／非 Metal 回退。控制记录为主工作区 `outputs/local-services/whisper/runtime.json`，日志同目录 `server.log`；按记录校验进程身份后终止 launcher，launcher 会关闭自己的 whisper-server。没有新增 launchd 自动启动任务。

## 真实短样本结果

输入来自同一完整《Let's build GPT》音轨的前 60 秒，16 kHz 单声道 WAV；不是新的整集 benchmark。

| 指标 | 本轮实测 |
|---|---|
| 实际后端 | 日志确认 Apple M5、`using MTL0 backend` |
| 模型 | large-v3-turbo Q5_0，574,041,195 字节 |
| Gateway 调用耗时 | 2.8558495 秒；模型已加载后开始计时 |
| 输出 | 22 段；语言 english；句段／token 时间归一化与持久结果校验通过 |
| 采样峰值进程 RSS | 838,090,752 字节，约 0.781 GiB；41 个样本 |
| 采样峰值进程 CPU | 51%，仅本次采样，不能作为整集负荷上限 |
| WhisperX 导入／强制对齐 | 均未发生 |

RSS 是进程驻留内存，不能当作独立显存；短样本峰值不替代整集的 2.525 GiB 历史采样。前日同片 Full／Turbo 对照仍以[原报告](whisper-metal-full-vs-turbo-20261030.md)为准；不从本轮短样本推算整集时间或并发上限。

- 权重 SHA-256：`394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`。
- binary SHA-256：`2611df3c12ce338832690e6fa8d5d5b8739e2b54f89fceec1fc134bcfec46ccc`。
- 60 秒音频 SHA-256：`7a9e67aa3de0032e015b2ba6dec883a0a8527322e761ed0d04a82762d09b74b7`。

原生 server 参数见[官方 server 文档](https://github.com/ggml-org/whisper.cpp/blob/master/examples/server/README.md)，本机实际命令与源码已核对。WhisperX 的 batch_size 是另一条推理路径的参数，此处未传入；processors 和 threads 不等同于它。token 时间不等于声学强制对齐。

## 同日补充：完整 116 分 20 秒 ASR

用户指出目标是完整课程，因此在 60 秒接线检查后，追加同一集整片调用。没有用短样本外推全片结果。

完整 6,979.941625 秒（约 116 分 20 秒）音频经同一个正式 Gateway 请求完成，耗时 **178.696 秒（约 2 分 59 秒）**，返回 **2,476 段**、24,085 个词／token 条目，117／117 个分钟窗口有文本，归一化和持久成果 Schema 校验通过。采样峰值 RSS 为 **1,647,525,888 字节（约 1.534 GiB）**。

| 项目 | 完整音频实测 |
|---|---|
| 输入 | 111,679,066 帧，16 kHz 单声道，223,358,396 字节 WAV；时长 6,979.941625 秒 |
| 模型／后端 | 同一 large-v3-turbo Q5_0、同一 Metal／MTL0 实例；WhisperX 未加载 |
| 端到端请求耗时 | 178.695775 秒；含本地 HTTP 上传、推理和回包，开始计时时模型已加载、音频已提取 |
| 时间覆盖 | 第一段从 0 ms 开始，最后一段到 6,979,720 ms；117／117 分钟窗口有文本 |
| RSS | 353 次采样，峰值 1,647,525,888 字节，约 1.534 GiB |
| CPU | 采样峰值 579.1%，macOS 口径约为 5.79 个逻辑 CPU 的瞬时用量；GPU 推理仍由实际 Metal 日志确认 |
| 强制对齐／业务解析 | 未执行强制对齐、Kimi、Refine、索引或业务库写入 |

117 个分钟窗口的转写覆盖不等于 117 个 Chunk 已完成理解或索引；Schema／时间校验不等于人工准确率验收。这次使用已有完整 WAV 和已加载模型，不能写成包含提取与冷启动的整课耗时。RSS 是进程驻留采样，受内存管理和运行状态影响；本轮峰值与前日 2.525 GiB 均保留，不据此推导并发上限。

完整输入 SHA-256：`6d12244a2696b4cd9262715bf35febaed5509013c6be5bb3bdc84a154dc6143d`。归一化文件 SHA-256：`51b02564a45a6a37746d0c2fca19310afb6f41727ecf2fe2cecf7c5d299f1a89`。原始／归一结果、分钟覆盖、请求与资源记录位于证据目录的 `full-course/`，实验结果未写入旧 GPT v5 任务或冒充其 checkpoint。

## 回归与部署边界

381 项直接回归通过；额外 Gateway 配置 61 passed／3 failed。三个失败是默认 Agent／Supervisor 与旧 fixture 的断言，在无改动 HEAD `67223c16` 隔离导出中同样复现。最后调整 Metal 日志识别后，3 项启动保护复跑通过。没有为使测试通过而改动无关默认配置。

两次首次启动探测因等待 C++ stdout 缓冲的 listening 行超时，均未执行模型请求且停止了自建进程。健康判断改为实际 Metal stderr＋`/health` 后通过；失败日志一并保留。

本地配置和 Whisper 服务已启用，完整 116 分 20 秒 ASR 已通过上述调用；API／Consumer 未启动、真实数据库未迁移、旧任务未 Retry，117 Chunk 后续理解／索引／回看追问未验收，独立对齐未实现。HTTP 侧按声明来源／模型建立持久身份，实际权重／binary／启动参数靠本次控制记录核验；通用 HTTP 接口本身不证明内部权重身份。服务模型或解码参数变化须新建解析版本。

原始证据：工作树 `outputs/verification/whisper-metal-activation-20261031/`（Git 忽略），包含 `smoke.json`、raw／normalized JSON、资源样本、启动命令／控制记录、测试 XML、未改 HEAD 的失败复现和启动探测失败记录。没有 commit／push。

后续范围纠正：用户要求的业务恢复验收已改用本 worktree 完整 API／Consumer 和独立 MySQL／ES 执行，真实进程中断、恢复及发现的修复见[解析 API 实机报告](../../server/video-asr-api-recovery-20261031.md)。本报告的短样本／整集数据只证明当时的 Gateway 转写调用。
