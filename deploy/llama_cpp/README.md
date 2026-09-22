# llama.cpp direct-video runtime

该 runtime 保留为 anivag 本地 Qwen3-VL 的直接视频 A/B 实验入口。Gateway 仍保留
HTTP adapter，但当前正式 `parse-video` fallback 已改为官方 DeepSeek Flash 的分组抽帧
链路，不会读取本目录配置，也不负责启动或停止本进程。

“直接视频”表示 llama.cpp 在 Server 内部调用 FFmpeg 解码、采样并构造视频时序输入；
Qwen3-VL 不会读取音轨。Engine 仍会提供候选 Transcript。

## 前置条件

- 使用最新 upstream llama.cpp，并确认 `llama-server --help` 包含 `--video-fps`；
- 构建必须启用视频支持；Ollama.app 当前内置二进制为 `MTMD_VIDEO=OFF`，不能复用；
- `ffmpeg` 与 `ffprobe` 必须位于 PATH，或用 `--video-ffmpeg-dir` 指定目录；
- 准备匹配的语言模型 GGUF 与 multimodal projector GGUF。

当前机器的 Ollama store 已有一组可用于 A/B 的 Qwen3-VL-8B Q4_K_M 主模型和
Q8_0 projector。首次验证可从 Ollama manifest 解析真实 blob；验证通过后应复制或建立
稳定语义路径，不能把 content-addressed hash 固化进业务配置。

## 启动

以下路径均为占位符，替换为本机稳定绝对路径：

```bash
/absolute/path/to/llama-server \
  -m /absolute/path/to/qwen3-vl-8b-q4_k_m.gguf \
  --mmproj /absolute/path/to/mmproj-qwen3-vl-8b-q8_0.gguf \
  -c 65536 \
  --video-fps 0.5 \
  --video-timestamp-interval 2000 \
  --video-ffmpeg-dir /opt/homebrew/bin \
  --alias anivag-qwen3-vl-direct \
  --host 127.0.0.1 \
  --port 8080
```

`--video-fps 0.5` 是 Server 全局参数。进行历史 A/B 复现时，必须与实验调用侧声明的
FPS 一致。不要使用默认 4 FPS：66 秒窗口会从约 33 帧上升到
约 264 帧，既无法和现有基线公平比较，也可能显著扩大视觉 token 和内存占用。
Gateway 只能核对调用侧声明，无法从 Server 反查真实启动参数，因此应把上述启动命令
作为部署契约固定下来；修改任一侧时必须同步修改另一侧。

只读健康检查：

```bash
curl -sS http://127.0.0.1:8080/health
```

## 与正式链路的关系

当前 `config.yaml` 不再提供 `local_fallback`，Engine CLI 也不再选择 llama.cpp。若要继续
做 direct-video A/B，应在独立诊断脚本中显式构造 `LlamaCppMultimodalModel`，并使用新的
output directory，不能把实验结果混入正式 checkpoint。

## 验证顺序

1. 先用短视频确认 Server 的 `input_video` 与 JSON Schema 输出正常；
2. 再用 episode 7 的完整 66 秒 VideoChunk 验证超时、内存与 Schema；历史产物文件名仍为 `segment-0010`；
3. 固定同一 0.5 FPS、候选 Transcript、Context、Prompt 和 Schema，与 Ollama 33 帧
   路径比较事件召回、时间误差、人物/字幕质量与延迟；
4. A/B 通过前，不把该 provider 设为整集默认降级。

Gateway 对视频发送 raw Base64 `input_video.data`，不发送 `data:video/...` 前缀，
以兼容受该格式解析问题影响的 llama.cpp 构建。
