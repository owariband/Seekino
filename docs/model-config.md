# 模型配置

`config.yaml` 的版本 2 把连接信息放在 `model_sources`，把各环节使用的模型放在 `models`。`agent_runtime` 保留运行参数，`video_parser` 保留采样和降级策略，`multimodal_embedding` 整节保留原有的模型、维度和采样配置。密钥仍写在 `.env.local`，不要写进 YAML。

仓库默认模型、降级开关、模型参数和密钥变量均未改变。原来的配置文件无需迁移，仍可以通过 `ANIVAG_MODEL_CONFIG` 使用；`start.sh`、本地数据库启动和可选 Docker 启动的使用方式不变。

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

在 `.env.local` 设置 `ASR_API_KEY`。保留 `models.agent` 中的 DeepSeek 设置即可。视频画面理解和多模态向量默认仍使用 Ark，所以这套组合仍需要 `ARK_API_KEY`。

`openai_audio` 调用 `POST /audio/transcriptions`。Engine 用 FFmpeg 把每个分析窗口转换为单声道、16 kHz WAV 后上传，临时音频在成功或失败后自动清理。`verbose_json` 支持 `segments[].start/end/text`，时间单位为秒，进入项目后按当前时间线契约转换为整数秒（起点向下取整、终点向上取整）并限制在窗口范围内。接口仅支持 JSON 文本时，设置 `response_format: json`；这时只有整个窗口级别的时间范围，没有句级时间戳。只兼容聊天接口的服务不能作为该 ASR 来源。没有音轨或抽取失败会报错，不会伪造空转写。

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
    max_output_tokens: 8192
    request_timeout_seconds: 180
```

该适配器调用带图片的 Chat Completions。Engine 自动使用有序抽帧路径，把已有 ASR 转写与画面一起交给视觉模型，不要求第三方支持视频上传。`video_parser.video_fps` 控制抽帧频率，`video_parser.max_frames_per_window` 控制每窗口上限（默认 32）。

如果服务不支持 `json_schema`，可以选择 `json_object`；输出仍必须通过项目结构校验。支持的 token 参数为 `max_tokens` 和 `max_completion_tokens`。不完整、被过滤或结构不合法的响应不会当作成功结果保存。

`models.vision_fallback` 可以引用 `openai_vision` 来源，也可以引用现有 `profile: deepseek` 来源，后者保留原来的 DeepSeek Responses 调用。`video_parser.fallback` 继续控制开关、分组和抽帧参数。它仍然只在明确的内容过滤错误时触发，不会把任意网络错误变成降级。

## 能力边界

| 环节 | 支持的来源 |
| --- | --- |
| `models.agent` | 现有 `openai`、`ark`、`codex_app_server`、`claude_sdk` 适配器 |
| `models.transcription` | `ark` 视频转写、`openai_audio` 专用 ASR |
| `models.vision` | `ark`、`openai_vision` 有序图片理解 |
| `models.vision_fallback` | DeepSeek profile、`openai_vision` |
| `multimodal_embedding`（保留原格式） | 原有 Ark 多模态向量实现，继续使用 `ARK_API_KEY` |

本次不改造向量来源，`models.embedding` 不属于有效配置。文本 Embeddings 接口不能直接替代当前视频向量接口。接入非 Ark 多模态向量供应商还需要实现对应协议，且查询与索引必须使用兼容向量空间；更换模型需要重新生成索引。本次没有改变这条检索语义。

来源名（如 `asr_default`）只是用户定义的标识；`adapter` 决定协议，`provider` 标识供应商。连接 URL 沿用原来的 HTTPS 要求，不允许用户名、密码、查询参数和片段。供应商专用 WebSocket 或任务轮询 ASR 不属于 `openai_audio`。

## 旧配置兼容规则

| 原配置 | 新配置 |
| --- | --- |
| `agent_model` / `agent_chat` | `models.agent` |
| `video_parser.transcript_model` | `models.transcription` |
| `video_parser.video_model` | `models.vision` |
| `video_parser.fallback.deepseek` | `models.vision_fallback` |

使用 `models` 时必须写 `config_version: 2`。可以逐个环节迁移，但同一环节同时提供新旧模型字段会明确报错，不静默覆盖。旧解析和向量配置仍使用隐式 `ARK_API_KEY`，不会误用 Agent 的自定义 Ark 来源。

现有模型名称环境变量及显式任务参数继续生效；它们只覆盖模型名称，不切换协议。新配置未指定超时、token 限额时，保留原有运行参数。新解析模型指定独立超时后使用该环节超时；显式任务 token 限额优先于模型配置。配置来源或模型变更后重启服务。

启动脚本按选中的模型来源及已启用的解析降级收集密钥，并始终保留原有多模态向量所需的 `ARK_API_KEY`；不会要求未选中的第三方来源密钥。仓库默认配置仍需要原来的 `DEEPSEEK_API_KEY` 和 `ARK_API_KEY`。密钥摘要用于检测启动配置变化，不输出密钥内容。

兼容性基线保存在 `tests/gateway/fixtures/legacy-config.yaml`；测试对新旧默认配置的所有设置做比较，并用两种格式运行服务端环境变量和全局设置编辑测试。
