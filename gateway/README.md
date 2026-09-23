# anivag Gateway

模型配置版本 2 的来源、各环节绑定及第三方 ASR/视觉接入说明见
[模型配置](../docs/model-config.md)。下文旧版 `agent_model` 配置仍受支持；
在版本 2 中对应 `models.agent`，同一环节不要同时填写两种格式。

Gateway 是 anivag 的模型调用边界。它只负责把统一的多模态输入转换成
Provider 请求，并把严格 JSON Schema 输出校验成调用方指定的 Pydantic 模型。

## 目录边界

- `harness/`：Provider 无关的 Agent 协议、上下文治理、Tool Loop 与错误；其中
  `factory.py` 是选择具体 Adapter 的组装入口；
- `openai/`：OpenAI 兼容协议及其 profile，DeepSeek 通过这里接入；
- `ark/`：Ark SDK、原生 Responses SSE、多模态、Agent 和 Embedding；
- `codex/`：Codex app-server 会话与 Agent Adapter；
- `claude_code/`：Claude Agent SDK Adapter；
- `local/`：Ollama 与 llama.cpp 多模态 Adapter；
- 根目录的 `multimodal.py`、`embedding.py`、`media.py`、`schema_normalization.py`
  提供共享协议和纯工具，`config.py` 只描述来源与模型选择。

业务层依赖共享协议并由 `harness.factory.AgentModelFactory` 选择 Adapter。模型名、
Provider 来源和连接协议分别配置，避免为某个具体模型复制一套运行框架。

## 多模态模型接口

`gateway.multimodal` 提供三种路径型输入：

- `TextInput(text)`：文本；
- `ImageInput(path, detail="high")`：本地 JPEG、PNG 或 WebP 图片；
- `VideoInput(path, fps)`：本地 MP4、MOV 或 AVI 视频。

四个实现共享 `MultimodalModel.generate(...)` 接口：

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
降级策略和 checkpoint 都由上层负责。

```python
import os

from pydantic import BaseModel

from gateway.config import load_config
from gateway.multimodal import ImageInput, TextInput
from gateway.openai.multimodal import DeepSeekMultimodalModel


class Description(BaseModel):
    description: str


deepseek = load_config().video_parser.fallback.deepseek
model = DeepSeekMultimodalModel(
    deepseek.model,
    base_url=deepseek.base_url,
    api_key=os.environ[deepseek.api_key_env],
    reasoning_effort=deepseek.reasoning_effort,
    max_output_tokens=deepseek.max_output_tokens,
    request_timeout_seconds=deepseek.request_timeout_seconds,
)
result = model.generate(
    [
        TextInput("只描述图片中可见的内容"),
        ImageInput("/absolute/path/to/frame.jpg"),
    ],
    schema_name="description",
    result_type=Description,
)
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
`profile` 处理不同 API 方言。Query、Evaluator、Chat 与 Supervisor 共用所选 source；
`ANIVAG_AGENT_QUERY_MODEL`、`ANIVAG_AGENT_EVALUATOR_MODEL` 和
`ANIVAG_AGENT_CHAT_MODEL` 仍可覆盖各角色的模型名。

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

Gateway CLI 只保留 Embedding 冒烟命令：

```bash
uv run --env-file .env.local python -m gateway.cli \
  --config config.yaml embed-video \
  --video /absolute/path/to/video-chunk.mp4 \
  --output outputs/gateway/video-chunk.embedding.json

uv run --env-file .env.local python -m gateway.cli \
  --config config.yaml embed-query \
  --query "惊世一跪发生在哪里？" \
  --output outputs/gateway/query.embedding.json
```

Ark Base64 内联媒体的保守上限为 45 MiB。更大的输入应由上层改走文件上传与复用流程。

旧的 Gateway `parse-video` 子命令与 parser 类已移除；视频解析统一从
`python -m engine.parse.cli parse-video` 进入，避免 Gateway 重新承担 Prompt、抽帧和领域结果归一。
