# 模型配置

真实模型评测约定（2026-10-25）：后续生成式模型测试使用 Kimi K3.1 Pro（`k3.1-pro`），不再调用 GPT 系列，包括测试链路中的子任务、视觉理解、摘要、fallback 和额外 judge。历史 GPT benchmark 保留作只读对照；向量、ASR 和既有专用意图路由保持各自配置。开跑前核对实际生效配置及环境覆盖，运行后核对真实调用模型。该约定不自动切换正在运行的业务服务。

`config.yaml` 的版本 2 把连接信息放在 `model_sources`，把模型定义放在 `models`。业务向 Gateway 传模型引用；Gateway 解析配置、校验能力并创建适配器。`agent_runtime` 保留运行参数，`video_parser` 保留采样和降级策略。密钥仍写在 `.env.local`，不要写进 YAML。

配置存储仍是本地 YAML，通过 `SEEKINO_MODEL_CONFIG` 选择文件，未设置时读取仓库 `config.yaml`。修改文件后重启服务以加载新配置。已有任务的正常续跑和 API `retry` 都保留任务保存的模型与解析配置快照。要切换已有视频的解析模型，须调用 `reparse` 并显式传入新的 `parseConfig`；可先通过 `GET /api/uploads/parse-config` 读取服务当前加载的默认配置。未提供 `parseConfig` 的 `reparse` 沿用最近一次任务的配置快照，仅将索引 generation 更新为当前生效版本。连接定义的变更仍需重启 Gateway 所在服务。这里没有引入数据库模型管理、HTTP CRUD 或云端配置。

本机开发配置使用 `config/local-dev-config.yaml`（2026-10-29 从 `local-kimi.yaml` 更名），由 `.env.local` 的 `SEEKINO_MODEL_CONFIG` 指向。它是完整开发环境配置，各角色可选择不同供应商；Kimi 只是其中部分角色的当前选择，不属于文件作用域。新名字不改变已选择的模型。

本机独立 OCR 当前关闭：不要在这份开发配置的 `models` 中添加 `ocr` 绑定，除非明确要重新启用。默认 Parse profile 以该绑定是否存在决定 OCR 开关；已创建任务保留自己的 `ocr_enabled` 快照，改 YAML 或普通重试不会更新旧任务，改变配置应创建新的解析版本。

## 模型定义与默认引用

`agent`、`vision`、`transcription`、`embedding`、`rerank` 是现有业务使用的默认引用。`models` 也接受任意命名引用，例如 `document_reader`；引用名称不决定供应商和协议。

```yaml
config_version: 2
model_sources:
  local_codex:
    adapter: codex_app_server
    provider: codex
  ark_default:
    adapter: ark
    provider: ark
    api_key_env: ARK_API_KEY
models:
  vision:
    source: local_codex
    model: gpt-6-astra
    kind: generative
    supports_vision_input: true
    request_timeout_seconds: 600
    max_output_tokens: 8192
    reasoning_effort: low
    context_window_tokens: 128000
    max_input_chars: 400000
  embedding:
    source: ark_default
    model: doubao-embedding-vision-250615
    kind: embedding
    dimensions: 1024
    video_fps: 1.0
    request_timeout_seconds: 600
  rerank:
    source: local_codex
    model: gpt-6-astra
    kind: rerank
    request_timeout_seconds: 600
    max_output_tokens: 8192
```

这是模型部分示例；完整配置仍需保留 Agent、转写及采样设置。`source` 引用连接定义，`model` 指定该连接的模型或 endpoint。`kind` 声明创建能力，支持 `generative`、`transcription`、`embedding`、`speaker_embedding`、`rerank`；省略时为普通生成，默认转写、向量和重排引用按各自能力归一。重排复用结构化生成实现，不要求供应商提供原生 `/rerank` 接口。

普通生成支持文本；接收图片的模型必须声明 `supports_vision_input: true`，且连接适配器支持图片输入。该声明由配置维护者根据模型能力设置，不会从模型名字猜测。为兼容已有版本 2 配置，保留引用 `vision`、`vision_fallback` 未声明该字段时默认允许视觉输入；任意新引用默认为文本。`fallback` 可内联另一份模型绑定，其来源、模型、超时、推理和预算分别保存。

## Supervisor harness 与独立模型

顶层 `supervisor_harness` 选择 `deepagents` 或 `codex`，与 `models.supervisor` 一起在 Server 启动时加载。修改 harness、模型或连接配置后重启 Server；`agent_runtime` 中已有的运行预算仍按原规则用于新消息。

仓库默认配置选择 `deepagents`：Supervisor、视觉、转写和 embedding 使用 Ark API，检索 Agent 和重排使用 DeepSeek API。可选的 `kimi_coding` 和 `codex_local` 来源已经声明，未选中时无需准备对应凭据或登录；查询改写默认关闭，不依赖本机模型权重。

Kimi 继续使用现有 OpenAI 兼容适配器和 `profile: kimi`。在同一份有效 YAML 中将 `models.supervisor.source` 改为 `kimi_coding`、`model` 改为 `k3.1-pro`，保持 `supervisor_harness: deepagents`，并设置 `KIMI_API_KEY`。其他生成式角色也可独立选择相同来源；选择 API 模型名不会自动替换转写或 embedding。

`profile: kimi` 的 `GatewayClient.create_generative_model()` 自动启用 Chat Completions 流式接收，适用于直接 JSON 和视觉工具轮次，无需新增 YAML 开关。它不改变 FPS、模型、reasoning 参数或业务重试预算；流收齐后沿用结果校验。其他 profile 保持原协议。实现与验证见 [Kimi 流式接入 TRD](design/03-product-reassessment/kimi-streaming-trd.md)。

需要 Codex Supervisor 时，在同一份 YAML 中替换以下字段；其他预算字段可沿用原值：

```yaml
supervisor_harness: codex
models:
  supervisor:
    source: codex_local
    model: gpt-5.6-sol
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 600
    context_window_tokens: 128000
    context_safety_margin_tokens: 2048
    auto_compact_tokens: 90000
    compact_target_tokens: 60000
    max_input_chars: 400000
```

保留现有 `models.agent` 即可继续为视频 Query、Evaluator 和 Chat 使用原来的 API 模型。`models.supervisor` 的来源、模型、推理和预算独立保存，不接受 `SEEKINO_AGENT_CHAT_MODEL` 或 `SEEKINO_AGENT_FALLBACK_MODEL` 覆盖。显式 `reasoning_effort` 优先，否则使用 `agent_runtime.supervisor_reasoning_effort`。模型绑定的超时、输出和上下文预算与已有 Agent 运行限制共同生效；例如本轮截止时间仍受 `agent_runtime.execution_timeout_seconds` 和 `supervisor_timeout_seconds` 约束。Codex 的 `max_output_tokens` 用于预算预留，不保证协议硬截断。

使用 Deep Agents 时将 `supervisor_harness` 改为 `deepagents`，并将 `models.supervisor` 设为 OpenAI 兼容或 Ark API 模型，例如沿用 `models.agent` 中的 DeepSeek 与 Ark 回退绑定。该路径由 Deep Agents 管理模型和工具循环；Codex 路径由原生 Codex runtime 执行同一 Supervisor 业务工具。Codex harness 要求 `codex_app_server` 来源，不接受 `fallback`；Deep Agents 只接受 API 来源及单层 Ark API 回退，配置错配明确报错，不自动切换 harness。

### Deep Agents checkpoint 持久化

Cloud Chat 使用 `langgraph-checkpoint-mysql[pymysql]==3.0.0` 和现有 `mysql+pymysql` 数据库。Server 创建共享 `PyMySQLSaver`，`conversation_id` 直接作为 `thread_id`，不需要 `pi_host`、独立 Node 进程或 Conversation↔pi-session 映射。`pi_host` 和 `supervisor_harness: pi` 已退役，旧配置会明确校验失败。

checkpoint 只保存已确认 turn：每轮先在内存执行，最终 SQL 消息提交成功后写入完整 graph state；失败、取消和关闭不删除上一份已确认状态，显式删除 Conversation 才删除 checkpoint。该机制用于下一轮恢复，不表示进程崩溃后自动续跑有副作用的工具。历史 Pi JSONL 不删除，旧 Conversation 没有 checkpoint 时仍按 SQL 展示历史冷启动，不承诺旧 tool 上下文无损迁移。

首次部署或升级需显式初始化 Schema，API 进程不会自动执行迁移。完整 Docker 启动链的 `initialize` 服务会先运行这一入口，成功后才启动 API；宿主机运行时手动执行：

```bash
python -m server.cli init-db
# 也可在环境评审后执行 docs/migrations/20261024_deepagents_checkpointer.sql
```

用户级 BYOK 的配置管理和授权注入不随 checkpoint 自动提供。MySQL checkpoint 写入、存储增长、retention、真实重启及 compact 恢复需按环境验收。

旧配置未声明 `supervisor_harness` 时默认使用 Deep Agents；未声明独立 `models.supervisor` 时保留原先的 Agent source、Chat 角色模型及其环境覆盖。已有模型子任务的配置与冻结任务快照保持原有规则。本次选择入口不包含 ACP 完整会话、网页 Agent/目录选择或本机文件任务接线。

## DS + 第三方 ASR

在现有 `model_sources` 中增加一个来源：

```yaml
  asr_default:
    adapter: openai_audio
    provider: your_speech_provider
    base_url: https://your-speech-service.example/v1
    api_key_env: ASR_API_KEY
    response_format: verbose_json
```

把 `models.transcription` 改为：

```yaml
  transcription:
    source: asr_default
    model: your-asr-model
    request_timeout_seconds: 180
```

在 `.env.local` 设置 `ASR_API_KEY`。保留 `models.agent` 中的 DeepSeek 设置即可。使用 Ark 多模态向量时仍需要 `ARK_API_KEY`。

`openai_audio` 调用 `POST /audio/transcriptions`。Engine 用 FFmpeg 把每个分析窗口转换为单声道、16 kHz WAV 后上传，临时音频在成功或失败后自动清理。`verbose_json` 支持 `segments[].start/end/text`，时间单位为秒，进入项目后乘以 1000 并舍入为整数毫秒，限制在窗口范围内；已有 `start_seconds` / `end_seconds` 字段名保留。接口仅支持 JSON 文本时，设置 `response_format: json`；这时只有整个窗口级别的时间范围，没有句级时间戳。只兼容聊天接口的服务不能作为该 ASR 来源。没有音轨或抽取失败会报错，不会伪造空转写。

## 本地 WhisperX ASR 与对齐

需要本地低调用成本及字幕字词时间时，推荐可选的 `adapter: whisperx`。项目直接在 Gateway 进程中调用 **WhisperX 3.8.6 的转写和强制对齐**；无需启动兼容 HTTP 服务。WhisperX 内部使用 faster-whisper，用户不需要另写一套 faster-whisper 接线。原 Ark 和 `openai_audio` 仍可选择；未选本地来源时不导入 WhisperX、不加载权重。安装与本地计算仍有磁盘、内存和耗时成本，效果与速度需按自己的素材验证。

准备依赖及权重（由用户主动执行；服务不会自动下载）：

```bash
uv sync --extra whisperx
# 已使用 CAM++ 时保留两项：uv sync --extra speaker --extra whisperx
uv run --no-sync python -m nltk.downloader punkt_tab

# 当前本机部署使用完整 large-v3；CPU/int8 的速度和精度仍需按素材评估。
# ASR 必须为 CTranslate2 目录，不能填写 whisper.cpp 的 ggml-*.bin。
uv run --no-sync hf download Systran/faster-whisper-large-v3 --local-dir /opt/seekino/models/whisperx/large-v3
uv run --no-sync hf download facebook/wav2vec2-base-960h --local-dir /opt/seekino/models/whisperx/align-en
# 根据素材语言补充，仅需下载会使用的语言：
uv run --no-sync hf download jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn --local-dir /opt/seekino/models/whisperx/align-zh
uv run --no-sync hf download jonatasgrosman/wav2vec2-large-xlsr-53-japanese --local-dir /opt/seekino/models/whisperx/align-ja
```

Whisper large-v3 本身支持多语言，只下载一套；本接入采用的 wav2vec2 对齐模型有各自语言的字/音素词表，因此按英语、中文、日语分别准备。2026-10-29 本机权重已落在 `outputs/local-models/whisperx/`，固定 revision 和 SHA-256 见[部署验收](report/gateway/local/whisperx-deployment-20261029.md)。

以上 `/opt/seekino/models/whisperx` 需替换成用户可写、服务可读的稳定模型目录；`punkt_tab` 需安装到服务运行用户的 NLTK 数据路径。ASR、按语言选择的对齐模型是两套权重，下载 Whisper 权重本身不会提供强制对齐能力。VAD 使用该版本包内的 pyannote checkpoint；本接入不启用说话人 diarization，不需要 diarization 的 HF token。系统仍需项目既有的 FFmpeg。

若服务用户的默认缓存目录不可写，在 `.env.local` 中把 `NLTK_DATA` 指向已安装的 NLTK 数据目录，`TORCH_HOME` 和 `MPLCONFIGDIR` 指向服务可写目录。本机已使用上述模型目录下的 `nltk_data`、`torch-cache` 和 `matplotlib`；不需要修改第三方包或禁用 NLTK 的网络安全检查。

在当前使用的完整 YAML 中配置来源，并替换 `models.transcription`：

```yaml
model_sources:
  whisperx_local:
    adapter: whisperx
    provider: local
    device: cpu              # CUDA 环境可设 cuda；当前不支持 MPS
    compute_type: int8       # CPU: int8 / float32；CUDA 可用 float16
    batch_size: 1            # 本机 16 GB 内存；显存充足时再实测调大
    cpu_threads: 6
    # language: en          # 不填则自动识别，识别出的语言必须有对齐模型
    alignment_models:
      en: /opt/seekino/models/whisperx/align-en
      zh: /opt/seekino/models/whisperx/align-zh
      ja: /opt/seekino/models/whisperx/align-ja
models:
  transcription:
    kind: transcription
    source: whisperx_local
    model: /opt/seekino/models/whisperx/large-v3
    request_timeout_seconds: 600
```

该绑定不使用 API key、`fps`、推理档位或输出 Token 上限。只下载英语对齐模型时，可只保留 `en` 并设置 `language: en`；自动识别得到未配置的语言会明确失败。路径不存在、依赖缺失、对齐加载或推理失败均明确报错，不静默返回“已对齐”的转写。单段对齐无法定位的文本仍保留，字词时间为未知。

Server 按原有正式解析链路准备 16 kHz、单声道 PCM16 WAV；Gateway 输出 WhisperX 秒单位结果；Engine 转为整数毫秒并裁剪到窗口；Server 再加原视频偏移。`transcript[].start_seconds/end_seconds` 沿用项目历史名称，实际仍为毫秒；新增可选 `language` 和 `transcript[].words`，字词包含 `text/start_ms/end_ms/score`，两个时间可同时为 `null`。中文、日文的词元可能是单字。无声输入保留空转写；本接入的 `speaker_id` 为 `null`，声纹和人物身份仍走既有独立链路。

字词时间仅保留有对齐分数的声学定位，WhisperX 为未对齐词插值补出的时间不作为精确证据；句段时间可能仍是上游回退范围。完整结果随现有不可变 ASR evidence 写入 Chunk detail，无需数据库迁移。旧模型响应不增加空的 `words` 字段。字幕的行长、换行、停留时间及 SRT/WebVTT/播放器展示仍需在消费层基于这份证据实现，不由 Gateway 决定。

同一 Gateway 的同一 source/model 共享并串行使用加载后的 ASR 与各语言对齐模型；关闭单个 Chunk 句柄不卸载权重，关闭 Gateway 后释放引用。调用沿用 usage、取消与超时控制；原生推理无法强行中断，运行中的取消或超时在模型返回后拒绝结果。默认 `uv sync` 不安装本地 ML extra；之后运行其他 `uv` 命令时应保留所需 extra 或用 `--no-sync`，避免移除已安装的可选依赖。

实现与测试边界见[WhisperX 接入设计](design/03-product-reassessment/local-whisperx-asr-trd.md)。安装指引按[WhisperX 固定版本](https://github.com/m-bain/whisperX/tree/v3.8.6)和[对齐实现](https://github.com/m-bain/whisperX/blob/v3.8.6/whisperx/alignment.py)核对；不将协议测试视为真实模型质量验收。

### 整片 ASR 的配置与兼容

新建 WhisperX 解析任务默认冻结 `models.asr.scope: video`：先转写完整原片音轨，再由原 Chunk ASR 节点读取与窗口相交的完整 cues。时间已经是原片绝对毫秒，不重复加 Chunk 偏移；Understanding 的输出仍受本段可写范围约束。上传解析配置可显式选择 `asr.scope: chunk` 回退逐块转写。未指定 scope 的历史任务仍按 chunk 执行，普通重试不会改变冻结配置。

仅为观看问答新建的按需任务仍冻结 `scope: chunk`；它不启动整片协调器。完整解析恢复时，在当前租约的 ASR 与媒体都准备好之前不会领取新的 Chunk，避免排队等待提前耗尽 Chunk 的执行时限；准备错误和准备租约过期受既有 Prepare 重试预算约束。

整片模式仅支持音频 Provider，且要求开启 Understanding。缓存属于当前进程内的视频协调器执行，退出后释放；重启恢复会重新转写，但保留已提交 Chunk。整片结果没有持久 checkpoint。需为完整音轨配置足够的转写请求超时；整片 ASR 完成前尚无第一个 Chunk 理解结果。识别质量、长音频峰值内存和端到端速度仍需真实对照，本次未启动全量解析。

## 本地声音向量

声音身份特征使用独立的 `speaker_embedding` 能力。以下片段分别合入现有的
`model_sources` 与 `models`；`speaker` 是显式创建时使用的引用，不会自动让解析任务
启用声纹。路径是部署示例，使用前替换为本机稳定存储位置。

```yaml
model_sources:
  campplus_local:
    adapter: campplus
    provider: local
    checkpoint_path: /opt/seekino/models/campplus/campplus_cn_common.bin
    cpu_threads: 6
    max_audio_seconds: 30
models:
  speaker:
    kind: speaker_embedding
    source: campplus_local
    model: iic/speech_campplus_sv_zh-cn_16k-common
    request_timeout_seconds: 30
```

`campplus` 与 Ollama Adapter 并列，进程内使用 CPU／FP32；本批没有 MPS 或 CUDA
推理路径。输入为宿主准备的 16 kHz、单声道 PCM16 WAV，接口限制为 400 ms 至
`max_audio_seconds`，不自动截视频、重采样或保存音频。输出为 192 维 L2 归一化向量，
并返回 `model`、`encoder_revision`、`duration_ms` 和 `dimension`。与参考集合的
比较、人物标签和 video/task 归属留在上层；相似度不是身份概率。

部署时安装可选依赖：

```bash
uv sync --extra speaker
```

普通 `uv sync` 不安装 torch、torchaudio 或 SciPy。`speaker` 锁定实验验证过的
torch／torchaudio 2.8.0、NumPy 2.2.6、SciPy 1.15.3，PyYAML 复用主依赖。NumPy
也被现有检索依赖使用，因此共享锁文件将其从 2.5.3 调整为 2.2.6；其他既有包版本
保持不变。PyPI 的 torch 在 Linux 上会附带 CUDA 相关条件依赖，macOS 不安装这些包；
本 Adapter 始终使用 CPU。

官方 [3D-Speaker](https://github.com/modelscope/3D-Speaker/tree/065629c313eaf1a01c65c640c46d77e61e9607b4)
在固定 commit 下没有 Python 包安装元数据。将原始源码部署到项目外的稳定目录，
通过服务启动环境的 `PYTHONPATH` 提供 `speakerlab`；无需复制网络实现，也无需安装
上游完整训练依赖或 `modelscope`。以下是部署者按实际目录执行的准备命令：

```bash
SPEAKER_HOME=/opt/seekino/models/campplus
mkdir -p "$SPEAKER_HOME"
git clone https://github.com/modelscope/3D-Speaker.git "$SPEAKER_HOME/3D-Speaker"
git -C "$SPEAKER_HOME/3D-Speaker" checkout 065629c313eaf1a01c65c640c46d77e61e9607b4
curl -fL 'https://www.modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/campplus_cn_common.bin' -o "$SPEAKER_HOME/campplus_cn_common.bin"
export PYTHONPATH="$SPEAKER_HOME/3D-Speaker${PYTHONPATH:+:$PYTHONPATH}"
```

checkpoint 固定官方 revision `v1.0.0`，大小 28,036,335 字节，SHA-256 为
`3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8`。
Adapter 首次加载时校验权重及实际导入的 `DTDNN.py`、`layers.py`、`processor.py`
三个官方实现文件的 SHA-256。缺失、错误权重、源码不可导入或版本不匹配会真实报错，
不会静默下载。`encoder_revision` 包含权重身份、实现指纹与 FBank／L2 预处理版本。

调用 `create_speaker_embedding_model("speaker")` 或
`create("speaker_embedding", "speaker")` 获得句柄，再用 `embed_audio(path)` 提取。
首次调用加载模型，同一句柄后续复用并串行执行；宿主复用句柄并按既有生命周期关闭。
`cpu_threads` 在首次加载时设置 PyTorch 进程级 intra-op 线程数；同进程各来源应统一
设置，同一 Gateway 配置中不同的线程设置会明确拒绝；不配置 inter-op 线程数。
超时、取消及调用预算复用现有调用控制，PyTorch
原生前向开始后只能在返回时检查取消或超时，不能承诺硬停止计算；本地调用无供应商
token 计费。VoiceprintAgent、登记事务及 Refine 准入已接通；`config.yaml` 提供
`campplus_local` 来源与 `speaker` 绑定，声纹准备仍需在解析任务配置中显式启用。
部署前须按本机位置配置权重路径，并完成上述可选依赖与官方源码准备。

## 第三方画面理解

增加一个独立的视觉来源：

```yaml
  vision_default:
    adapter: openai_vision
    provider: your_vision_provider
    base_url: https://your-vision-service.example/v1
    api_key_env: VISION_API_KEY
    response_format: json_schema
    token_parameter: max_tokens
```

然后修改模型选择：

```yaml
  vision:
    source: vision_default
    model: your-vision-model
    supports_vision_input: true
    max_output_tokens: 8192
    request_timeout_seconds: 180
```

该适配器调用带图片的 Chat Completions。Engine 自动使用有序抽帧路径，把已有 ASR 转写与画面一起交给视觉模型，不要求第三方支持视频上传。`video_parser.video_fps` 控制抽帧频率，当前默认 3 fps；Parse V2 按实际片段时长完整取帧，旧 `video_parser.max_frames_per_window` 仅为配置兼容保留，不再限制解析帧数。

如果服务不支持 `json_schema`，可以选择 `json_object`；输出仍必须通过项目结构校验。支持的 token 参数为 `max_tokens` 和 `max_completion_tokens`。不完整、被过滤或结构不合法的响应不会当作成功结果保存。

`models.vision_fallback` 可以引用支持视觉生成的来源，例如 `openai_vision`、`profile: deepseek`、Ark 或本机 Codex。`video_parser.fallback` 继续控制开关、分组和抽帧参数。它仍然只在明确的内容过滤错误时触发，不会把任意网络错误变成降级。显式 `models.vision.fallback: null` 禁用该绑定的回退。

## 能力边界

| 创建能力 | 已实现的协议与输入方式 |
| --- | --- |
| 生成、结构化重排 | `ark`、`openai`、`openai_vision`、`codex_app_server`、`claude_sdk` |
| 视觉生成 | Ark 可接收视频；`openai`、`openai_vision`、Codex 使用有序图片；还需模型声明允许视觉输入 |
| 转写 | `ark` 视频转写、`openai_audio` 专用 ASR |
| 多模态向量 | Ark 多模态向量协议；配置来源可独立选择 Ark 连接 |
| 声音向量 | `campplus` 进程内 CPU 推理；宿主提供 16 kHz 单声道 PCM16 WAV |

能力注册和供应商适配器属于 Gateway，业务不维护供应商白名单。`openai_vision` 和普通 `openai` Chat 协议不支持 `reasoning_effort` 的非 `none` 设置；DeepSeek profile、Ark 和 Codex 保留各自实现的推理控制。`openai_audio` 不接受生成 token 限额。模型输出预算用于上下文规划；Codex app-server 不保证通过协议硬性截断到此 token 数。

`models.embedding` 保存 `dimensions`、`video_fps`、`input_mode`、可选 `query_instruction` 和 `encoder_model`。Ark 的 `ep-...` 是请求路由，不是向量模型身份，必须声明实际 `encoder_model`；使用直接模型名时，另写的 `encoder_model` 必须与其一致。旧配置的 endpoint 环境覆盖会保留原模型作为 encoder 身份。查询与索引必须使用兼容向量空间；更换实际 encoder 模型需要重新生成索引。

### 本地文本向量与视觉向量开关

`input_mode: multimodal` 是默认值，生成 Chunk 视频向量与 Event 图片向量。
设置 `input_mode: text` 后，只生成文本向量并使用词法/文本召回，索引阶段不再抽帧或
调用图片/视频编码；ASR、OCR、VLM 理解由各自配置控制，不随此开关关闭。
模式和查询指令会冻结到任务，已有任务不会因修改 YAML 自动改变。

本地直接使用 llama.cpp 时，使用以下来源与绑定（合入同一份模型配置对应节点）：

```yaml
model_sources:
  llama_embedding_local:
    adapter: llama_cpp_embedding
    provider: llama_cpp
    base_url: http://127.0.0.1:18843
models:
  embedding:
    kind: embedding
    source: llama_embedding_local
    model: seekino-qwen3-embedding-0.6b
    dimensions: 1024
    input_mode: text
    query_instruction: Given a question about a machine learning course, retrieve relevant lecture passages that answer the question.
    video_fps: 1.0
    request_timeout_seconds: 120
```

在准备好本机原生程序和 GGUF 后，从仓库根目录执行
`uv run --no-sync python scripts/run_local_embedding.py`。默认读取
`outputs/tools/llama.cpp/llama-server`（Windows 为 `llama-server.exe`）和
`outputs/local-models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf`；
可通过 `--binary`、`--model` 指定路径。启动器不安装依赖、不下载模型、不启动 ASR。
程序在前台运行，Ctrl-C 停止；运行期间模型保持加载，没有实现自动空闲卸载。
本次只在 Mac ARM64 验证；其他系统须准备对应原生程序和所需库，不代表已完成跨平台验收。

适配器使用 `/v1/embeddings`，base_url 填服务根地址，不加 `/v1`；启动器固定
`--embedding --pooling last`、1 个槽位和 4096 token 上下文。超长输入报错，
需要更长输入时显式增大 `--context-size`，同时核对内存。查询指令、协议和归一化
进入独立 encoder revision，不能直接混用 Ollama/Ark 已有向量。
本次已在 Ollama 端口关闭时完成真实编码，不依赖 Ollama daemon。

本地 Qwen 示例（合入现有配置的对应节点；Ollama 需先运行并下载模型）：

```yaml
model_sources:
  ollama_local:
    adapter: ollama_embedding
    provider: ollama
    base_url: http://127.0.0.1:11434
models:
  embedding:
    kind: embedding
    source: ollama_local
    model: qwen3-embedding:0.6b
    dimensions: 1024
    input_mode: text
    query_instruction: Given a question about a machine learning course, retrieve relevant lecture passages that answer the question.
    video_fps: 1.0  # 保留兼容字段；text 模式不执行视频编码
    request_timeout_seconds: 120
```

Ollama 不需要 API Key，使用原生 `/api/embed`。文档直接编码，查询增加配置的
`Instruct: ...\nQuery: ...`，指令与归一化规则进入 encoder revision；响应模型、维数、
有限非零向量均需验证，超长输入明确拒绝截断。该适配器仅支持文本；开启视觉向量需
选择支持图片和视频的编码器，不能仅把 Qwen 文本绑定的模式改成 multimodal。

本地适配器不输出模型 sparse 向量，`SEEKINO_RETRIEVAL_SPARSE_WEIGHT` 必须为 `0`
（默认值）；这不影响 BM25 词法检索。不兼容设置会在启动或上传时明确拒绝。
`init-es-index --config <配置文件>` 自动根据生效 `input_mode` 初始化 mapping；
可选 `--text-only` 只用于断言配置一致，不会覆盖配置强行切换模式。

同一物理索引固定编码与模态合同。首次从 Ark 多模态切换本地文本模型，应使用新的
`SEEKINO_ELASTICSEARCH_INDEX` 与 `SEEKINO_ELASTICSEARCH_EVENT_INDEX`，再显式生成
对应向量；不能混用旧向量或只切查询模型。上传接口会拒绝与活动索引模式、模型或查询
指令不一致的任务配置，因此这是部署/素材库级设置，并非任意单次上传的 UI 开关。
方案与实验边界见 [本地文本向量 TRD](design/03-product-reassessment/local-text-embedding-trd.md)。

来源名（如 `asr_default`）只是用户定义的标识；`adapter` 决定协议，`provider` 标识供应商。云端连接 URL 沿用原来的 HTTPS 要求，本地 Ollama 另允许 loopback HTTP；不允许 URL 中包含用户名、密码、查询参数和片段。供应商专用 WebSocket 或任务轮询 ASR 不属于 `openai_audio`。

## 旧配置兼容规则

| 原配置 | 新配置 |
| --- | --- |
| `agent_model` / `agent_chat` | `models.agent` |
| `video_parser.transcript_model` | `models.transcription` |
| `video_parser.video_model` | `models.vision` |
| `video_parser.fallback.deepseek` | `models.vision_fallback` |
| `multimodal_embedding` | `models.embedding`（`fps` 改为 `video_fps`） |

使用 `models` 时必须写 `config_version: 2`。可以逐个环节迁移，但同一环节同时提供新旧模型字段会明确报错，不静默覆盖。旧解析和向量配置仍使用隐式 `ARK_API_KEY`，不会选择第一个 Ark 连接或误用 Agent 的自定义 Ark 来源。只有 `ark_default` 等于隐式连接时复用该名称，否则归一到固定 `legacy_parse_ark_source`。旧配置未定义独立重排模型时，Gateway 将其归一为 Agent 模型；新配置应显式填写 `models.rerank`。

`GatewayConfig.resolve_model_binding(ref)` 和 `resolve_embedding_binding(ref)` 返回完整绑定，保留所有显式超时、输出、推理、上下文和回退参数。传入已冻结绑定时不会重新应用环境覆盖。`model_dump()` 的 `normalized_models` 保存归一结果，可直接加载回原配置。

现有环境覆盖集中在 Gateway resolver：转写 `SEEKINO_TRANSCRIPT_MODEL`，视觉 `SEEKINO_VIDEO_MODEL`，Agent 优先 `SEEKINO_AGENT_CHAT_MODEL` 再 `SEEKINO_DEEPSEEK_CHAT_MODEL`，Query/Evaluator 使用对应 `SEEKINO_AGENT_QUERY_MODEL` / `SEEKINO_AGENT_EVALUATOR_MODEL`，Agent 回退使用 `SEEKINO_AGENT_FALLBACK_MODEL`，重排使用 `SEEKINO_RERANK_MODEL`。向量默认引用使用 `SEEKINO_VIDEO_EMBEDDING_MODEL` / `SEEKINO_VIDEO_EMBEDDING_FPS`，视觉回退开关使用 `SEEKINO_FALLBACK_ENABLED`。空白模型覆盖忽略；覆盖模型名不会切换来源和协议，任意自定义引用不继承 Agent 默认模型覆盖。

预算归一遵循以下规则：显式绑定字段优先；缺省超时为旧 Parse 的 600 秒；支持输出预算的生成协议缺省取 `agent_runtime.max_output_tokens`，再取旧 Agent 的输出预算。文本上下文、余量、输入字符数和压缩阈值继承 `agent_runtime`；若旧配置没有对应字段，使用兼容值 128000 / 2048 / 400000。声明视觉输入的绑定缺省使用 128000 token、2048 余量和 400000 字符，避免旧视觉 65536 输出预算受到文本 64000 窗口限制。

可用输入预算为上下文减去主模型及各级回退中最大的输出预算，再减安全余量。视觉绑定或没有全局压缩参数的配置，压缩触发值和目标值分别按可用输入预算的 90% 与 75% 归一。resolver 要求 `0 < compact_target_tokens < auto_compact_tokens < 可用输入预算`，不合法时明确报错。转写和向量协议不额外添加不支持的输出限额。

启动脚本按默认模型来源、独立 Supervisor 的主模型及回退、已启用的解析降级收集密钥，不要求未选中的第三方来源密钥。本机 Codex 来源没有 API key 变量要求；Ark 和 DeepSeek 连接仍分别使用它们声明的变量。密钥摘要用于检测启动配置变化，不输出密钥内容。

兼容性基线保存在 `tests/gateway/fixtures/legacy-config.yaml`；配置测试保护默认迁移、任意引用、能力校验、独立重排/向量绑定、预算完整保留及旧任务冻结规则。

### 解析输出预算（2026-10-30）

仓库默认配置与本机 `config/local-dev-config.yaml` 的 `models.vision`、`models.vision_fallback` 均设 `max_output_tokens: 30000`。Understanding 使用直接 JSON；Refine 仍用工具，未指定独立模型时复用 Understanding 的冻结绑定。该值是生成上限，增加它不等于加速；Chat/OCR 等角色不随之改值。配置在服务启动时加载，已有解析任务保留创建时的快照；普通重试不会把旧 8,192 改成 30,000，采用新配置应使用既有新解析版本入口。
