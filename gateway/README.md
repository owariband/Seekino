# anivag Gateway

模型配置版本 2 的来源、各环节绑定及第三方 ASR/视觉接入说明见
[模型配置](../docs/model-config.md)。下文旧版 `agent_model` 配置仍受支持；
在版本 2 中对应 `models.agent`，同一环节不要同时填写两种格式。

Gateway 是 anivag 的模型调用边界。除把统一输入转换成 Provider 请求并校验结构化
输出外，`agent_model/` 还提供 Provider 无关的 Agent Model 协议、上下文预算／换窗、
Tool Loop、Adapter 选择和 Provider fallback。它不拥有 Video、Query、Speaker、
Conversation 或 Parse Job 等领域语义。

## 目录边界

- `agent_model/`：Provider 无关的 Agent Model 协议、上下文治理、Tool Loop 与错误；
  `deepagents.py` 接入内部 Supervisor 的 Deep Agents SDK，`factory.py` 负责组装；
- `openai/`：OpenAI 兼容协议及其 profile，DeepSeek 通过这里接入；
- `ark/`：Ark SDK、原生 Responses SSE、多模态、Agent 和 Embedding；
- `codex/`：Codex app-server 会话与 Agent Adapter；
- `claude_code/`：Claude Agent SDK Adapter；
- `local/`：Ollama、llama.cpp 多模态 Adapter，以及进程内 CPU CAM++ Adapter；
- 根目录的 `multimodal.py`、`embedding.py`、`speaker_embedding.py`、`media.py`、`schema_normalization.py`
  提供共享协议和纯工具，`config.py` 只描述来源与模型选择。

业务层通过 `model_factory.GatewayClient` 获取模型。`create(kind, ref)` 按外部配置
创建 `generative`、`embedding`、`speaker_embedding`、`rerank`、`transcription` 五类模型，也提供对应的
`create_*_model()` 类型入口。视觉理解使用生成接口和显式图片能力声明；来源适配器
支持图片，并不表示该来源下的每个模型都能理解图片。

`create_agent_factory()` 保留现有 Agent 的循环和 Supervisor 接口。模型名、来源、
连接、预算和回退从配置解析，供应商实例创建与关闭均在 Gateway 内完成。Engine
和 Server 保留抽帧、转写提示词、时间戳归一、检索候选身份与任务状态等业务职责。

当前配置由本地 YAML 提供。未来云端配置存储将向同一配置合同提供有效定义；
数据库、租户权限和配置管理 API 属于 Server 配置管理，不进入模型工厂。

```python
from pydantic import BaseModel

from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput


class Description(BaseModel):
    description: str


client = GatewayClient(load_config())
model = client.create_generative_model("vision")
try:
    result = model.generate(
        [ImageInput("/absolute/path/to/frame.jpg"), TextInput("描述可见内容")],
        schema_name="description", result_type=Description,
    )
finally:
    model.close()
    client.close()
```

## 声音向量接口

`speaker_embedding` 提取说话人的声音特征，与检索使用的多模态 `embedding` 分开。
`adapter: campplus` 在当前进程通过 PyTorch 执行官方 CAM++，使用 CPU／FP32，
输出 192 维 L2 归一化向量；不经过 Ollama，也无需另起推理服务。

```python
client = GatewayClient(load_config())
model = client.create_speaker_embedding_model("speaker")
# 统一入口也支持 client.create("speaker_embedding", "speaker")。
try:
    result = model.embed_audio("/absolute/path/to/prepared-audio.wav")
    vector = result.vector
    encoder_revision = result.encoder_revision
    duration_ms = result.duration_ms
finally:
    model.close()
    client.close()
```

输入必须是宿主准备的 16 kHz、单声道 PCM16 WAV，时长至少 400 ms，且不超过
source 的 `max_audio_seconds`。结果同时包含 `model` 和 `dimension`；版本标识包含
固定权重 SHA-256 与预处理版本，便于检查向量是否来自兼容空间。400 ms 是接口
输入限制，不表示该长度足以确认人物。

创建句柄时不导入 torch、不加载权重；首次提取按需加载，同一句柄后续复用，
并发调用在实例内串行执行，关闭时释放模型引用。`cpu_threads` 在模型首次加载时
通过 PyTorch 设置进程级 intra-op 线程数，同进程的 CAM++ 来源应使用一致设置；
同一 Gateway 配置中不同的线程设置会明确拒绝。
调用复用既有模型调用记录、预算及取消检查，不虚构供应商 token 用量。取消和
超时在等待与推理前后检查，已进入 PyTorch 原生前向的计算不能强制中断。

Gateway 校验音频并提取向量。来源授权、从视频截音和清理临时音频、video/task
隔离、参考登记、排除自我及重叠比对、相似度比较和最终 speaker 写回由宿主负责。
VoiceprintAgent 与 Refine 已通过宿主接入此入口；声纹准备按解析任务配置显式启用，余弦相似度不是身份概率。
依赖、官方源码和权重部署说明见[本地声音向量配置](../docs/model-config.md#本地声音向量)。

## 多模态模型接口

`gateway.multimodal` 提供三种路径型输入：

- `TextInput(text)`：文本；
- `ImageInput(path, detail="high")`：本地 JPEG、PNG 或 WebP 图片；
- `VideoInput(path, fps)`：本地 MP4、MOV 或 AVI 视频。

底层适配器共享 `MultimodalModel.generate(...)` 接口：

- `ArkMultimodalModel` 支持文本、图片和视频，通过 Ark Responses SSE 返回结构化结果；
- `DeepSeekMultimodalModel` 支持文本和按输入顺序排列的图片，通过 DeepSeek 官方
  Responses API 返回结构化结果。它不接受 `VideoInput`，调用方应先抽帧；
- `OllamaMultimodalModel` 支持文本和按输入顺序排列的图片，通过 Ollama 的
  OpenAI-compatible API 返回结构化结果。它不接受 `VideoInput`，调用方应先抽帧；
- `LlamaCppMultimodalModel` 支持文本、图片和视频，通过 upstream llama.cpp Server
  的 OpenAI-compatible API 返回结构化结果。视频以 raw Base64 放入
  `input_video.data`，不依赖 Server 读取本机文件路径。

Gateway adapter 内部负责文件检查、MIME 判断、Base64/Data URL 编码、Provider
payload、网络请求和结果 Schema 校验。Prompt、视频时长探测、抽帧、事件归属、
业务降级和 checkpoint 由上层程序负责；模型来源之间的 Provider fallback 属于
`agent_model/` 的执行协议。

```python
from pydantic import BaseModel

from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput


class Description(BaseModel):
    description: str


client = GatewayClient(load_config())
model = client.create_generative_model("vision_fallback")
try:
    result = model.generate(
        [
            TextInput("只描述图片中可见的内容"),
            ImageInput("/absolute/path/to/frame.jpg"),
        ],
        schema_name="description",
        result_type=Description,
    )
finally:
    model.close()
    client.close()
```

直接视频模式使用同一个接口：

```python
from gateway.local.llama_cpp import LlamaCppMultimodalModel
from gateway.multimodal import TextInput, VideoInput


model = LlamaCppMultimodalModel(
    "anivag-qwen3-vl-direct",
    base_url="http://127.0.0.1:8080",
    max_output_tokens=8192,
    request_timeout_seconds=1200,
    expected_server_video_fps=0.5,
)
result = model.generate(
    [
        VideoInput("/absolute/path/to/video-chunk.mp4", fps=0.5),
        TextInput("只依据视频画面返回结构化描述"),
    ],
    schema_name="description",
    result_type=Description,
)
```

`expected_server_video_fps` 是调用侧对 llama.cpp Server 启动参数 `--video-fps` 的声明；
后者是 Server 全局配置，不是逐请求配置。Gateway 会拒绝 `VideoInput.fps` 与该声明
不一致的请求，但它无法从运行中的 Server 反查真实启动参数。部署命令必须保证二者
一致，不能把这个调用侧检查理解成 Server 配置探测。

这里的“直接视频”仍是 llama.cpp 在服务内调用 FFmpeg 解码和抽帧，不是 Qwen
直接读取 MP4/H.264 字节流，也不包含音轨理解。llama.cpp 官方仍将多模态 Server
标记为 experimental；direct-video 先用于同一 VideoChunk A/B。生产降级配置使用
DeepSeek Flash：Engine 按 30 秒分组抽帧，先生成中性画面证据，再用同一模型结合
Lite ASR、compact 和背景资料整理最终 Schema。协议与启动参数见
[llama.cpp Server 文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

模型名、服务地址和采样参数由仓库根目录 [`config.yaml`](../config.yaml) 提供；
本机密钥或个人 Endpoint 写入已忽略的 `.env.local`。不要在 adapter 中固化部署地址。

## Chat Agent 模型来源

`model_sources` 描述连接方式，`agent_model.source` 选择其中一个来源。`adapter`
决定协议实现，`provider` 只用于 provenance、用量和价格归属，OpenAI 兼容协议的
`profile` 处理不同 API 方言。Query、Evaluator、Chat 与内部 Supervisor 共用所选 source；
`ANIVAG_AGENT_QUERY_MODEL`、`ANIVAG_AGENT_EVALUATOR_MODEL` 和
`ANIVAG_AGENT_CHAT_MODEL` 仍可覆盖各角色的模型名。

内部 Supervisor 默认通过 `AgentModelFactory.build_deep_agent()` 使用 Deep Agents
0.7.23。SDK 负责模型／工具循环、摘要和工具结果卸载；Gateway 将已有 OpenAI 兼容
或 Ark Adapter 的单次请求接入 SDK，继续使用配置中的模型、推理参数、超时、fallback、
用量及追踪。视频 Query／Evaluator／Chat 的审核流程仍作为业务工具执行。

SDK 使用 `StateBackend` 虚拟文件和进程内 `InMemorySaver`。一个 Conversation 对应
一个 thread；配置重建共享同一状态存储。只暴露虚拟 `read_file` 以读取卸载内容，关闭
默认通用 subagent。业务工具每轮保留原始问题和范围，串行执行以复用核验结果。AniVAG
成功保存最终回答后才确认 SDK 状态；失败、取消或保存失败会清理该 thread，下一轮
从数据库中已完成的历史恢复。删除 Conversation 同时清理 SDK 状态。重启进程后也从
数据库恢复文本历史，虚拟文件和中间工具状态不会持久化。

当前内部 Supervisor 要求 OpenAI 兼容或 Ark API source。下方 Codex／Claude 配置
描述已有原子 Agent Adapter；把它们作为当前 Server 的全局 source 会在 Supervisor
组装时报配置错误。用户自带 Codex／Claude 的完整本机会话计划通过 ACP Bridge 接入，
该路径的设计见 [Bridge TRD](bridge/design/codex-bridge-trd.md)，本批尚未实现。

DeepSeek 使用 OpenAI Adapter 的 DeepSeek profile，并显式配置 Ark fallback：

```yaml
model_sources:
  deepseek_api:
    adapter: openai
    provider: deepseek
    profile: deepseek
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
  ark_default:
    adapter: ark
    provider: ark
    api_key_env: ARK_API_KEY

agent_model:
  source: deepseek_api
  model: deepseek-flash
  max_output_tokens: 8192
  request_timeout_seconds: 180
  fallback:
    source: ark_default
    model: doubao-seed-2-0-lite-260428
```

Codex 复用当前系统用户已有的本机 Codex 登录，不需要配置 API Key：

```yaml
model_sources:
  codex_local:
    adapter: codex_app_server
    provider: codex
    max_concurrency: 4

agent_model:
  source: codex_local
  model: gpt-6-sol
  max_output_tokens: 8192
  request_timeout_seconds: 180
```

需要指定非默认 Codex 可执行文件时，在 `codex_local` 下添加 `codex_bin`。

Claude 的本机订阅模式复用当前系统用户已有的本机登录：

```yaml
model_sources:
  claude_local:
    adapter: claude_sdk
    provider: claude_code
    auth_mode: local_subscription
    max_concurrency: 4

agent_model:
  source: claude_local
  model: claude-sonnet-4-6
  max_output_tokens: 8192
  request_timeout_seconds: 180
```

Claude API Key 模式把 `auth_mode` 改为 `api_key`，并添加
`api_key_env: ANTHROPIC_API_KEY`；程序会从该字段指定的环境变量读取密钥。
需要指定非默认 Claude 可执行文件时，在 source 下添加 `cli_path`。

`local_subscription` 会在 Claude 子进程中显式清空继承到的 API Key、Auth Token、
自定义 Base URL 和云厂商路由开关，防止本机环境变量把订阅登录静默改成 API 计费。
`api_key` 模式同样会清空相互冲突的认证方式，只注入 `api_key_env` 指定的值。
Claude 调用使用 `--no-session-persistence`，不会把问题、工具结果和回答写入个人
`~/.claude/projects` 历史。每个审核回合启动新的 CLI 进程，并在下一轮提示中显式携带
原始任务、上一轮可见回答和审核反馈；隐藏推理不跨回合保留，Agent 可以通过 AniVAG 工具
重新读取规范工作区。认证目录保持不变，因此本机订阅模式可继续读取既有登录凭证。

Codex app-server 与 Claude Agent SDK 当前都没有暴露可由 AniVAG 设置的硬输出 token
上限。它们配置中的 `max_output_tokens` 用于 AniVAG 的上下文输出预留；实际调用仍由
请求超时、模型调用次数和 provider 自身的输出上限约束。DeepSeek 的同名配置会直接
传给模型 API。

CLI provider 会在启动进程前检查初始提示、工具 Schema 和结构化输出 Schema 是否能放入
`agent_runtime` 的上下文硬预算。Codex 还会加入按当前锁定 SDK 实测并向上取整的基础输入
预留。Codex/Claude 内部后续工具回合的 transcript 不对 AniVAG 暴露，因此这项预检不执行
伪压缩，也不把内部续轮描述成可由 Context Canvas 精确治理。

修改 `model_sources`、`agent_model`、认证方式或可执行文件等模型连接配置后，需要重启
Server。`ANIVAG_AGENT_FALLBACK_MODEL` 只覆盖已经配置的 fallback 模型，不会创建或猜测
fallback source。当前支持 OpenAI → Ark 与 Ark → Ark；Codex 和 Claude 不会隐式切换到 Ark。

## 多模态 Embedding

`DoubaoMultimodalEmbedder` 保留文本与视频向量能力。文本查询和 VideoChunk 使用同一模型，
进入同一向量空间。Embedding 不返回 VideoChunk 内部时间戳，也不替代上层的视频理解流程。

## 依赖与验证

以下命令均在仓库根目录执行：

```bash
uv sync
uv run pytest tests/gateway
```

Embedding 手动冒烟入口位于 `tests/manual/gateway_embedding.py`，从仓库根目录运行：

```bash
uv run --env-file .env.local python -m tests.manual.gateway_embedding \
  --config config.yaml embed-video \
  --video /absolute/path/to/video-chunk.mp4 \
  --output outputs/gateway/video-chunk.embedding.json

uv run --env-file .env.local python -m tests.manual.gateway_embedding \
  --config config.yaml embed-query \
  --query "惊世一跪发生在哪里？" \
  --output outputs/gateway/query.embedding.json
```

Ark Base64 内联媒体的保守上限为 45 MiB。更大的输入应由上层改走文件上传与复用流程。

旧的 Gateway `parse-video` 子命令与 parser 类已移除；视频解析统一从
`python -m engine.parsev1.cli parse-video` 进入，避免 Gateway 重新承担 Prompt、抽帧和领域结果归一。

## Langfuse 模型观测

`tracing` 默认关闭。Server 使用 `LangfuseTracing(config.tracing)` 单例，Parse worker、Chat
和检索通过 `tracing.scope(context)` 关联模型调用与业务 ID。构造器不读取
密钥、不启动线程或进程，首次启用的范围才初始化。缺失后端地址或密钥、初始化和导出故障
只产生通用日志，模型结果与 ParseUsageLedger 继续保留。

```yaml
tracing:
  enabled: true
  base_url: https://your-langfuse-host.example
  public_key_env: LANGFUSE_PUBLIC_KEY
  secret_key_env: LANGFUSE_SECRET_KEY
  environment: development
  release: your-revision
  queue_size: 2048
  export_timeout_seconds: 5
  close_timeout_seconds: 5
```

`base_url` 也可通过 `LANGFUSE_BASE_URL` 提供；配置中显式值优先。地址应指向现有
Langfuse 实例，密钥仅从指定环境变量读取。当前桥接以 Unix socketpair 和 `pass_fds`
实现进程隔离，支持本项目的 macOS / Linux 运行环境。

桥接使用独立的 OpenTelemetry 1.45.0 `TracerProvider` 和 Langfuse 官方 OTLP/HTTP
接口 `/api/public/otel/v1/traces`，以 HTTP Protobuf、Basic Auth 和
`x-langfuse-ingestion-version: 4` 发送。不修改进程全局 TracerProvider，也不安装未消费的
Langfuse SDK。SDK 4.17.0 的隔离探针证实 OTel `force_flush(timeout_millis)` 不约束阻塞
exporter 的整体等待，因此生产实现由自有进程执行 HTTP，主进程的
`SimpleSpanProcessor` 只进行序列化和有界队列的非阻塞入队，不执行网络请求。
官方字段依据见 [原生 OpenTelemetry 接入](https://langfuse.com/integrations/native/opentelemetry)。

根 span 表示执行范围；Generation / Embedding 在真实模型请求开始时创建，在终态结束，
耗时来自 Gateway 的开始 / 结束时间。业务范围失败时根 span 标记通用 `ERROR`，原错误
继续传播；模型调用失败也只导出通用状态，不导出错误正文。普通 Provider 请求标记
`provider_request`；Codex / Claude SDK 聚合记录标记 `sdk_turn_run` 和实际 usage 来源。
有效的 32 位十六进制 `trace_id` 与 16 位 `parent_span_id` 可继承远端父节点；业务层的
任意字符串 `run_id` 仅用于关联。`session_id`（或 `conversation_id`）写入 session。
`asyncio.to_thread` 自动继承当前上下文；自行创建的线程需显式传递上下文。

仅允许导出工作区、系列、视频、任务、会话和执行 ID、阶段、分类、重试及 lease 等关联
字段。提示、回答、媒体、工具正文和凭据不进入 input / output。Token usage 未知时省略
`usage_details` 并标记 `usage_status=unknown`；明确的零值保留。标准 usage 只写入
`input` / `output` / `total`，缓存、推理和模态子集保存在 metadata，避免与 inclusive
总量重复计数。`usage_subsets_semantics` 说明推理属于 output、其余当前子集属于 input，
各子集可能互相重叠。人民币费用和币种只写入 metadata，并可携带 `ledger_call_id`；
不把 CNY 写入 USD `cost_details`。ParseUsageLedger 继续作为业务账本。

宿主关闭时应先停止并等待模型工作，再调用 `tracing.close()`。关闭先停止接受新记录，
在 `close_timeout_seconds`（最小 0.2 秒）内尝试排空；截止时终止并回收自有 HTTP 子进程，
停止 IPC 线程。OS 调度可能使墙钟略超配置，未在截止内退出的资源会记录 error。
顺序重复 close 不再执行；并发重复 close 立即返回，第一次 close 负责收尾，报告在首次
收尾结束后可读。`close_report` 各计数含义如下：

- `exported`：获得成功 HTTP 响应且未被 OTLP `partial_success` 拒绝的单 span 传输数。
- `failed`：HTTP / 网络失败、响应无法解析或 collector 明确拒绝的传输数。
- `dropped`：已知的队列满、超大记录或关停后入队拒绝数。
- `unconfirmed`：已接受入队但没有确认响应的记录数；其中可能已被后端接收。
- `forced`：截止时使用了进程终止；`worker_exited` / `ipc_thread_exited` 分别说明资源状态。

配置帧通过专用 IPC 首帧发送，不占业务队列。队列满会丢弃观测并发出通用告警，导出
失败不会重跑模型。HTTP 确认只说明传输被接受，不保证后端查询可见、可靠投递或 exactly-once；
失败请求也可能已被后端处理，不能把 `failed` / `unconfirmed` 解读为确定未入库。

离线字段与生命周期验证运行：

```bash
uv run pytest tests/gateway/test_tracing.py tests/gateway/test_langfuse_integration.py
```

真实后端验证默认跳过。获得具体项目写入授权并设置三个 `LANGFUSE_*` 环境变量后，可
显式设置 `ANIVAG_LANGFUSE_INTEGRATION=1`，单独运行
`tests/gateway/test_langfuse_integration.py::test_real_langfuse_v2_observations_read_back`。
该入口只发送合成记录，不调用模型，再通过
`GET /api/public/v2/observations` 的 `core,basic,model,usage,metadata,metrics,trace_context,io`
字段组检查父子节点、模型、session、usage、时间和正文省略。需要 Langfuse Cloud 或
自托管 v4；此轮未向真实后端发送数据。
[官方 v2 Observations 查询合同](https://langfuse.com/docs/api-and-data-platform/features/public-api)
说明字段组、时间范围和版本兼容性。
