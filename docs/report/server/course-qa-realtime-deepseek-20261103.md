# 课程 QA：实时模型绑定与 DeepSeek 看帧请求失败

**DeepSeek Flash 支持图片；本次看帧先被请求的 JSON Schema 拒绝，补齐字段后又遇到返回格式校验失败。课程页面的视觉链路尚未通过。** 本轮没有修改产品代码或旧任务配置。

[QA TRD](../../design/03-product-reassessment/course-qa-trd.md) · [脱敏指标和原始证据哈希](evidence/course-qa-realtime-deepseek-20261103.json) · [此前 Chat 复测](course-qa-deepseek-20261103.md)。问题状态见 C26/C27；C22 的重复取证继续复现。

## 实时模型为什么仍跟随旧解析任务

现有 `observe_frames()` 读取最近 V2 任务的 `parse_config`，`_derive()` 再把 `config.models.understanding` 传给 `generate_realtime()`。已有 Chunk 的身份、版本与缺失补算需要复用原任务合同，但本轮临时看帧没有覆盖基础解析结果，不需要因此强制沿用其模型。该绑定从 2026-10-10 的初版就存在。

[原设计](../../design/02-agent-harness/realtime-parse-trd.md) 要求基础补算保持冻结配置，以供后续正式解析复用；当前实现把这项约束也用于临时视觉调用。新的实时模型应由当前配置装配，并进入派生缓存身份；已有任务和 Chunk 继续按原版本读取。这是待修复的配置职责问题，本次未实施产品解耦。

本轮仍运行联合提交 `1deff37c`。QA 启动入口仅代理实时宿主的 `generate_realtime()`，把模型绑定换为 `deepseek_api / deepseek-flash / low / stream=true / temperature=0`，无 fallback；提示、图像、Schema、期限控制仍走原代码。全局视觉配置同时设为 DS，但单改 YAML 对已有任务不起作用。该临时入口是模型试验条件，不是正式功能；原数据、冻结配置和基础 Chunk 未修改。前后源码、配置及 Chunk 指纹一致，完整入口哈希保留在证据中。

## 浏览器三连问

沿用 Q01→Q02→Q03、01:30 暂停点、已有基础 Chunk，新建会话。FTT/TT 使用同一前端单调时钟并通过供应商正文、Server 来源和模型完成前 DOM 增长核验。

| 用例 | FTT | TT | 首段与实际结果 |
| --- | ---: | ---: | --- |
| Q01 | 1.263 秒 | 11.026 秒 | 首段“我先”为进度；实时图像请求 HTTP 400，回答依赖已有文字 |
| Q02 | 4.009 秒 | 5.354 秒 | 首段“1”为编号前缀；三条解释后另附来源行，格式与来源质量仍需分别评判 |
| Q03 | 2.690 秒 | 8.590 秒 | 首段“我先”为进度；再次看帧 HTTP 400，视频卡两次提交失败 |

两次图像请求确实发往 DeepSeek `/responses`，但返回 400；Chat `/chat/completions` 正常。三题完成不代表看帧成功，以上 TT 不作为成功视觉问答的速度基线。Q01 还把 01:30 暂停点表述为“约 01:25”，混淆了暂停位置与对白起点；Q03 能区分原话和解释，但仍暴露工具错误。浏览器刷新与截图已保存，本轮未重跑取消和崩溃用例。

## HTTP 400 的具体原因

官方文档明确说明 [DeepSeek Flash 支持图像输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)，包括 Responses API。其 [错误码说明](https://api-docs.deepseek.com/zh-cn/quick_start/error_codes/) 将 400 归为请求体格式错误，需根据具体错误消息修改请求；不能把状态码直接解释为模型没有视觉能力。

浏览器路径只保留了 `DeepSeekModelError` 和 HTTP 状态，原始错误体未进入这条适配器的 trace。随后用同一暂停帧、原适配器、`VisualObservation` Schema 做定向复现，收到：

```text
HTTP 400 / invalid_request_error
Required properties must match all properties in the object
```

`VisualObservation` 声明 `summary` 和 `captions`，其中 `captions` 有默认空列表，所以生成的 Schema 只有 `required: ["summary"]`。Gateway 同时发送 `strict: true`，没有把该 Schema 转换为供应商要求的形式。此次失败发生在请求合同校验阶段，没有取得视觉生成结果。

对照使用相同图像、提示、模型和参数，仅把根对象改为 `required: ["summary", "captions"]`：400 消失。第一次对照约 6.131 秒后进入本地校验错误；第二次保存了完整响应事件，约 7.040 秒收到正常 `response.completed`，文字描述识别出 LearnGPT、树叶新闻示例和右下角讲师画面，已与原帧核对。

但两次响应都带 Markdown JSON 围栏；原适配器直接 `model_validate_json(output_text)`，因此本地 `ValidationError` 再包装为 `DeepSeekModelError`。仅在离线评审中去除围栏后，第二份结果通过原 Schema 校验。这不是产品已修好，也不代表全部视觉描述正确：响应对屏幕其他软件的用途还包含推测。

## 本轮结论

需要分别修复实时模型选择（C26）和 DeepSeek 结构化输入／输出兼容（C27），再用真实页面复测。不能靠改旧任务、关闭来源守卫或把部分文字答案当作完整视觉结果来放行。当前 QA 运行入口仍保留 DS 模型试验，尚未加入 Schema 修正；没有继续发送相同的失败课程请求。原始完整响应、图片和日志仅保留本机忽略目录。
