# AniVAG Gateway

Gateway 是 AniVAG 的模型调用边界。它只负责把统一的多模态输入转换成
Provider 请求，并把严格 JSON Schema 输出校验成调用方指定的 Pydantic 模型。

## 多模态模型接口

`gateway.models` 提供三种路径型输入：

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

from gateway import DeepSeekMultimodalModel, ImageInput, TextInput, load_config


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
from gateway import LlamaCppMultimodalModel, TextInput, VideoInput


model = LlamaCppMultimodalModel(
    "anivag-qwen3-vl-direct",
    base_url="http://127.0.0.1:8080",
    max_output_tokens=8192,
    request_timeout_seconds=1200,
    expected_server_video_fps=0.5,
)
result = model.generate(
    [
        VideoInput("/absolute/path/to/segment.mp4", fps=0.5),
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
标记为 experimental；direct-video 先用于同 Segment A/B。生产降级配置使用
DeepSeek Flash：Engine 按 30 秒分组抽帧，先生成中性画面证据，再用同一模型结合
Lite ASR、compact 和背景资料整理最终 Schema。协议与启动参数见
[llama.cpp Server 文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

模型名、服务地址和采样参数由仓库根目录 [`config.yaml`](../../config.yaml) 提供；
本机密钥或个人 Endpoint 写入已忽略的 `.env.local`。不要在 adapter 中固化部署地址。

## 多模态 Embedding

`DoubaoMultimodalEmbedder` 保留文本与视频向量能力。文本查询和视频片段使用同一模型，
进入同一向量空间。Embedding 不返回片段内部时间戳，也不替代上层的视频理解流程。

## 安装与验证

以下命令均在仓库根目录执行：

```bash
uv sync --project packages/gateway
uv run --project packages/gateway pytest
```

Gateway CLI 只保留 Embedding 冒烟命令：

```bash
uv run --project packages/gateway --env-file .env.local \
  anivag-model --config config.yaml embed-video \
  --video /absolute/path/to/segment.mp4 \
  --output packages/gateway/outputs/segment.embedding.json

uv run --project packages/gateway --env-file .env.local \
  anivag-model --config config.yaml embed-query \
  --query "惊世一跪发生在哪一段？" \
  --output packages/gateway/outputs/query.embedding.json
```

Ark Base64 内联媒体的保守上限为 45 MiB。更大的输入应由上层改走文件上传与复用流程。

旧的 `anivag-model parse-video` 与 Gateway parser 类已移除；视频解析统一从
`anivag-engine parse-video` 进入，避免 Gateway 重新承担 Prompt、抽帧和领域结果归一。
