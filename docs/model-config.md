# 模型配置

真实模型评测约定（2026-10-25）：后续生成式模型测试使用 Kimi K3.1 Pro（`k3.1-pro`），不再调用 GPT 系列，包括测试链路中的子任务、视觉理解、摘要、fallback 和额外 judge。历史 GPT benchmark 保留作只读对照；向量、ASR 和既有专用意图路由保持各自配置。开跑前核对实际生效配置及环境覆盖，运行后核对真实调用模型。该约定不自动切换正在运行的业务服务。

`config.yaml` 的版本 2 把连接信息放在 `model_sources`，把模型定义放在 `models`。业务向 Gateway 传模型引用；Gateway 解析配置、校验能力并创建适配器。`agent_runtime` 保留运行参数，`video_parser` 保留采样和降级策略。密钥仍写在 `.env.local`，不要写进 YAML。

配置存储仍是本地 YAML，通过 `ANIVAG_MODEL_CONFIG` 选择文件，未设置时读取仓库 `config.yaml`。修改文件后重启服务以加载新配置。已有任务的正常续跑使用保存的模型快照；通过 API 显式重试时，Server 会读取当前本地配置，更新理解、精炼模型和视觉输入方式，保留已有 ASR、向量模型和任务拓扑。连接定义的变更仍需重启 Gateway 所在服务。这里没有引入数据库模型管理、HTTP CRUD 或云端配置。

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

仓库默认配置选择 Codex harness：

```yaml
supervisor_harness: codex
models:
  supervisor:
    source: codex_local
    model: gpt-6-astra
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

保留现有 `models.agent` 即可继续为视频 Query、Evaluator 和 Chat 使用原来的 API 模型。`models.supervisor` 的来源、模型、推理和预算独立保存，不接受 `ANIVAG_AGENT_CHAT_MODEL` 或 `ANIVAG_AGENT_FALLBACK_MODEL` 覆盖。显式 `reasoning_effort` 优先，否则使用 `agent_runtime.supervisor_reasoning_effort`。模型绑定的超时、输出和上下文预算与已有 Agent 运行限制共同生效；例如本轮截止时间仍受 `agent_runtime.execution_timeout_seconds` 和 `supervisor_timeout_seconds` 约束。Codex 的 `max_output_tokens` 用于预算预留，不保证协议硬截断。

使用 Deep Agents 时将 `supervisor_harness` 改为 `deepagents`，并将 `models.supervisor` 设为 OpenAI 兼容或 Ark API 模型，例如沿用 `models.agent` 中的 DeepSeek 与 Ark 回退绑定。该路径由 Deep Agents 管理模型和工具循环；Codex 路径由原生 Codex runtime 执行同一 Supervisor 业务工具。Codex harness 要求 `codex_app_server` 来源，不接受 `fallback`；Deep Agents 只接受 API 来源及单层 Ark API 回退，配置错配明确报错，不自动切换 harness。

### Deep Agents checkpoint 持久化

Cloud Chat 使用 `langgraph-checkpoint-mysql[pymysql]==3.0.0` 和现有 `mysql+pymysql` 数据库。Server 创建共享 `PyMySQLSaver`，`conversation_id` 直接作为 `thread_id`，不需要 `pi_host`、独立 Node 进程或 Conversation↔pi-session 映射。`pi_host` 和 `supervisor_harness: pi` 已退役，旧配置会明确校验失败。

checkpoint 只保存已确认 turn：每轮先在内存执行，最终 SQL 消息提交成功后写入完整 graph state；失败、取消和关闭不删除上一份已确认状态，显式删除 Conversation 才删除 checkpoint。该机制用于下一轮恢复，不表示进程崩溃后自动续跑有副作用的工具。历史 Pi JSONL 不删除，旧 Conversation 没有 checkpoint 时仍按 SQL 展示历史冷启动，不承诺旧 tool 上下文无损迁移。

首次部署或升级需显式初始化 Schema，API 启动不会自动执行迁移：

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

## 本地声音向量

声音身份特征使用独立的 `speaker_embedding` 能力。以下片段分别合入现有的
`model_sources` 与 `models`；`speaker` 是显式创建时使用的引用，不会自动让解析任务
启用声纹。路径是部署示例，使用前替换为本机稳定存储位置。

```yaml
model_sources:
  campplus_local:
    adapter: campplus
    provider: local
    checkpoint_path: /opt/anivag/models/campplus/campplus_cn_common.bin
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
SPEAKER_HOME=/opt/anivag/models/campplus
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

`models.embedding` 保存 `dimensions`、`video_fps` 和可选 `encoder_model`。Ark 的 `ep-...` 是请求路由，不是向量模型身份，必须声明实际 `encoder_model`；使用直接模型名时，另写的 `encoder_model` 必须与其一致。旧配置的 endpoint 环境覆盖会保留原模型作为 encoder 身份。文本 Embeddings 接口不能直接替代当前视频向量接口，查询与索引必须使用兼容向量空间；更换实际 encoder 模型需要重新生成索引。

来源名（如 `asr_default`）只是用户定义的标识；`adapter` 决定协议，`provider` 标识供应商。连接 URL 沿用原来的 HTTPS 要求，不允许用户名、密码、查询参数和片段。供应商专用 WebSocket 或任务轮询 ASR 不属于 `openai_audio`。

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

现有环境覆盖集中在 Gateway resolver：转写 `ANIVAG_TRANSCRIPT_MODEL`，视觉 `ANIVAG_VIDEO_MODEL`，Agent 优先 `ANIVAG_AGENT_CHAT_MODEL` 再 `ANIVAG_DEEPSEEK_CHAT_MODEL`，Query/Evaluator 使用对应 `ANIVAG_AGENT_QUERY_MODEL` / `ANIVAG_AGENT_EVALUATOR_MODEL`，Agent 回退使用 `ANIVAG_AGENT_FALLBACK_MODEL`，重排使用 `ANIVAG_RERANK_MODEL`。向量默认引用使用 `ANIVAG_VIDEO_EMBEDDING_MODEL` / `ANIVAG_VIDEO_EMBEDDING_FPS`，视觉回退开关使用 `ANIVAG_FALLBACK_ENABLED`。空白模型覆盖忽略；覆盖模型名不会切换来源和协议，任意自定义引用不继承 Agent 默认模型覆盖。

预算归一遵循以下规则：显式绑定字段优先；缺省超时为旧 Parse 的 600 秒；支持输出预算的生成协议缺省取 `agent_runtime.max_output_tokens`，再取旧 Agent 的输出预算。文本上下文、余量、输入字符数和压缩阈值继承 `agent_runtime`；若旧配置没有对应字段，使用兼容值 128000 / 2048 / 400000。声明视觉输入的绑定缺省使用 128000 token、2048 余量和 400000 字符，避免旧视觉 65536 输出预算受到文本 64000 窗口限制。

可用输入预算为上下文减去主模型及各级回退中最大的输出预算，再减安全余量。视觉绑定或没有全局压缩参数的配置，压缩触发值和目标值分别按可用输入预算的 90% 与 75% 归一。resolver 要求 `0 < compact_target_tokens < auto_compact_tokens < 可用输入预算`，不合法时明确报错。转写和向量协议不额外添加不支持的输出限额。

启动脚本按默认模型来源、独立 Supervisor 的主模型及回退、已启用的解析降级收集密钥，不要求未选中的第三方来源密钥。本机 Codex 来源没有 API key 变量要求；Ark 和 DeepSeek 连接仍分别使用它们声明的变量。密钥摘要用于检测启动配置变化，不输出密钥内容。

兼容性基线保存在 `tests/gateway/fixtures/legacy-config.yaml`；配置测试保护默认迁移、任意引用、能力校验、独立重排/向量绑定、预算完整保留及旧任务冻结规则。
