# AniVAG Engine

Engine 承载 AniVAG 自有的领域算法编排；外部模型请求仍由
`packages/gateway` 负责。

当前解析代码按业务层级拆分：

- `video_parse.py`：整集/整批 Segment 的顺序编排、Context、ownership、去重与恢复；
- `segment_parse.py`：单个已有 Segment 的 Prompt、候选 ASR 融合、模型选择与审核降级；
- `frame_sampler.py`：本地降级所需的帧时间规划与 FFmpeg 抽帧；
- `prompts.py`：Transcript 与事件时间线的证据约束；
- `analysis_windows.py`：仅供当前本地整集实验物化临时 AnalysisWindow，不是生产上传切段。

`ParsePipeline` 会：

1. 消费上传链路已经持久化的规范 Segment 与对应 AnalysisWindow；
2. 并发预生成可独立计算的 Lite 候选 Transcript；
3. 按 Segment 顺序串行调用 Pro，并传入上一段的 ContextSnapshot；
4. 把窗口内相对时间换算为整集绝对时间，并按事件中点唯一归属；
5. 将已归属事件压缩为有界 `ContextSnapshot`，交给下一段辅助消歧。

当前 `parse-video` CLI 为了继续完成第七集实验，仍会从本地完整视频规划并物化
临时 AnalysisWindow。正式上传链路不会在解析阶段再次切 Segment，也不会引入
`segment_split.py`。

`contextAfter` 只来自当前及更早规范 Segment 的已确认事件，不会吸收
AnalysisWindow 后置重叠区里的未来事件。

## 运行

在仓库根目录执行：

```bash
uv sync --project packages/engine

uv run --project packages/engine --env-file .env.local \
  anivag-engine --config config.yaml parse-video \
  --video '/absolute/path/to/episode.mp4' \
  --segment-duration-ms 60000 \
  --context-overlap-ms 3000 \
  --max-height 720 \
  --asr-concurrency 4 \
  --series-bible-file /absolute/path/to/characters.md \
  --output-dir packages/engine/outputs/episode
```

候选 Transcript 会先写入 `transcripts/segment-XXXX.json`；每完成一段 Pro
解析，命令再写入 `segments/segment-XXXX.json`。使用相同参数和输出目录重跑时，
Pipeline 会验证模型、窗口和上下文链，然后从第一个未完成 Segment 继续。全部完成
后写入 `pipeline.parse.json`。Segment 文件包含每段的
`context_before_compact → self_compact → context_after_compact`，便于直接检查
上下文是否真的逐段传递。

如果候选 ASR 暂时不可用，可显式传入 `--asr-mode none`。该模式会为每段保存
带 `transcript_source=none` 的空候选检查点，再由 Pro 读取视频音频、画面与硬字幕；
最终产物不会把这一降级路径伪装成 Lite 结果。

当 Ark 明确返回输出内容审核错误且本地降级已启用时，Engine 只降级一次：按配置
抽取有序帧，再通过 Gateway 的 Ollama 多模态模型接口调用本地 Qwen。网络错误、
Schema 错误或本地模型错误都不会触发循环重试。

`local_fallback.provider` 也可以显式切为 `llama_cpp`。此时 Engine 不再自行抽帧，
而是把同一个 AnalysisWindow MP4 作为 `VideoInput` 交给 Gateway；llama.cpp Server
在进程内解码并按 `frame_fps` 对应的 `--video-fps` 采样。Qwen3-VL 仍不能读取音轨，
所以 Engine 会继续传入候选 Transcript，并使用仅允许画面/硬字幕作为视觉证据的
Prompt。当前 direct-video 路径只用于 A/B，默认配置仍是 `ollama`：

```yaml
video_parser:
  local_fallback:
    enabled: true
    provider: llama_cpp
    model: anivag-qwen3-vl-direct
    base_url: http://127.0.0.1:8080
    frame_fps: 0.5
    max_frames: 33       # Ollama 模式使用；direct-video 模式忽略
    max_height: 720      # Ollama 模式使用；direct-video 模式忽略
    max_output_tokens: 8192
    request_timeout_seconds: 1200
```

恢复检查会核对实际 provider、model 与 input mode。切换 `ollama/ordered_frames` 和
`llama_cpp/video` 做实验时应使用新的 output directory，不能混用已有 checkpoint。
独立 Server 的启动参数与 A/B 顺序见
[`deploy/llama_cpp/README.md`](../../deploy/llama_cpp/README.md)。
