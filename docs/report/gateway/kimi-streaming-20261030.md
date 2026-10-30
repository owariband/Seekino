# Kimi 正式生成入口流式接入验证

日期：2026-10-30，Asia/Shanghai。基线：`dev` / `df83c3f9`；变更：`fix/kimi-streaming` / [PR #20](https://github.com/owariband/Seekino/pull/20)。设计入口：[TRD](../../design/03-product-reassessment/kimi-streaming-trd.md)。此记录验证代码接入和单张合成图接口兼容性，尚未验证长视频 Chunk 成功或服务部署。

## 接入与自动化验证

工厂按 `profile: kimi` 为 `OpenAIVisionModel` 启用 `stream=True` / `stream_options.include_usage=True`；直接 JSON 和共用视觉工具轮次调用 `gateway/openai/streaming.py`。复用 OpenAI SDK `ChatCompletionStreamState` 累积 content、reasoning 与 tool_calls；补充终止 choice 检查、流关闭、控制与预算检查、读取错误分类，保留末尾 usage。工具循环在完整结果校验后执行，跨轮透传 Kimi reasoning。

环境：Python 3.12.16、OpenAI 2.54.0、httpx 0.28.1。在独立 worktree 使用项目原有虚拟环境执行：

```bash
python -m pytest tests/gateway/test_kimi_streaming.py tests/gateway/test_openai_media.py tests/gateway/test_model_factory.py tests/gateway/test_deepagents_multimodal.py tests/gateway/test_multimodal_agent.py tests/gateway/test_usage.py tests/gateway/test_model_diagnostics.py -q
```

结果：281 passed、1 failed（5.78 秒）。失败为 `test_fallback_media_is_checked_against_the_active_context_budget`：Ark → DeepSeek 模拟 fallback 未在预期位置触发输入预算异常，最终 `StopIteration`。同一用例在未修改的 `dev` / `df83c3f9` 上单独复跑也失败；本次未改变该 Responses 路径或扩大修复范围。新增 Kimi 用例全部通过，相关直接入口四文件最近一次为 138 passed，后续新增 4 项也通过于七文件运行。

## 一次真实合成图请求

2026-10-30 22:59:05 发起正式 `GatewayClient.create_generative_model('vision').generate` 调用，配置来自本机开发 YAML，地址 `https://api.kimi.com/coding/v1`，模型 `k3.1-pro`，max tokens 30000、60 秒超时、SDK 重试 0。输入是自行生成的 128×64 红蓝两色 PNG 和测试提示词；不包含用户视频、业务数据库或真实任务写入。首次探针运行在 HTTP 调用前因误读句柄属性退出，实际只发出一次请求。

| 项目 | 实测 |
| --- | --- |
| 首个 SSE 分片 | 5.520 秒，自调用开始计 |
| 完整结果 | 7.431 秒 |
| 分片数 | 110 |
| Schema 校验 | 通过，marker = `KIMI_STREAM_OK` |
| 描述 | 图片左半部分是红色，右半部分是蓝色。 |
| Token 用量 | input 242、output 122、total 364；reasoning 84 包含在 output 中 |
| Provider request ID | 未提供可识别值（null） |

[原始结果 JSON](kimi-streaming-20261030-synthetic.json) 随报告保存。更完整的本地诊断与探针在实施 worktree 的 `outputs/verification/kimi-streaming-20261030/`，属于忽略的本地产物，不承诺其他 checkout 自动具备。

## 尚未验证

MyGO EP04 原 0–63 秒长 Chunk 仍待流式复测。先前两次真实素材授权已用于 1 / 0.5 fps 非流式请求，未追加外传。该小图结果不能说明流式消除了视觉预处理等待、上游首片前 504、context 压力或内容质量问题。HTTP 无事件期间由现有读取超时限制；持续产生分片时检查总预算。未修改 FPS/推理参数、未自动重试真实素材、未重启服务或更改任务状态。
