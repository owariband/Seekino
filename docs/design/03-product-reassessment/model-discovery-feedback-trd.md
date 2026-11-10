# 1. 问题与现状

需求：模型服务的“获取模型列表”结果可见、可直接添加，且对不提供列表的服务给出中性说明。提出日期：2026-11-10（Asia/Shanghai）。分支：`fix/model-discovery-feedback`（基于 `feat/apple-style-refresh`，沿用其样式）；PR：待创建后回填。合并顺序：`feat/cloud-user-docs` → `feat/apple-style-refresh` → 本分支。

**触发。** hypnos 反馈设置页的“获取模型列表”像占位按钮，点了什么也没拿到。

**排查结论（2026-11-10，本机 dev API 实测）。** 前后端都已实现，不是占位：按钮调用 `POST /api/agent/model-connections/discover`（[client.ts](../../../web/src/api/client.ts)），后端经 [model_settings.py](../../../server/service/model_settings.py) 调用 [model_discovery.py](../../../gateway/model_discovery.py)，按协议请求 `/models`、`/api/tags` 等接口并处理分页。

| 连接 | 实测结果 |
| --- | --- |
| deepseek_api | 200，2 个模型 |
| kimi_coding | 200，6 个模型 |
| llama_embedding_local | 200，1 个模型 |
| whisper_cpp_local（openai_audio） | 400，“模型列表接口返回 HTTP 404” |

“像占位”的原因有两个：

1. **成功结果几乎不可见。** 获取成功后，界面只多出一个收起的“选择模型”下拉框：没有数量、没有列表、没有确认提示；列表还包含已注册的模型，选中后会产生重复行。
2. **whisper.cpp 必然失败。** 后端把 `openai_audio` 标记为可获取（[yaml_chat_models.py](../../../server/service/yaml_chat_models.py)），但 whisper.cpp 服务没有 `/v1/models`，总是返回 404，并以错误形式展示。

**本次边界。** 只改获取结果的展示、重复添加的防护、404 的处理和 `openai_audio` 的可获取标记。不改保存接口、不改其他协议的获取逻辑，也不改模型配置格式。

# 2. 影响因素、假设与评价指标

| 指标 | 类型 | 验收标准 | 测量方式 |
| --- | --- | --- | --- |
| 结果可见 | 结果 | 获取后在按钮下方直接显示“获取到 X 个模型，Y 个已注册”和模型 chip | 组件测试；浏览器截图 |
| 一键添加 | 结果 | 点击新模型 chip 后加入模型行，chip 变为已注册 | 组件测试 |
| 防重复 | 结果 | 已在模型行中的 ID 不再作为可添加项 | 组件测试 |
| 无列表的服务 | 结果 | 模型列表接口返回 404/405/501 时显示中性说明，而不是错误 | 后端单测；组件测试 |
| whisper.cpp | 结果 | `openai_audio` 连接不再显示获取按钮 | 后端单测；组件测试 |
| 既有行为 | 护栏 | 其他协议的获取、保存与现有测试不变 | pytest；vitest |

# 3. 改造方案与建模

- **后端：** `discover_models` 在首个请求返回 404、405 或 501 时返回 `None`，表示“服务不提供模型列表”；其他失败仍按原样报错。`DiscoveredModels` 增加 `listAvailable`（默认 `true`）。`openai_audio` 从可获取协议集合中移除。
- **前端：** 获取结果改为内联展示：一行摘要，加上 chip 列表。新模型 chip 可点击添加，优先填入空行；已注册的模型以勾选样式展示、不可点击。新模型多于一个时提供“全部添加”。`listAvailable: false` 时显示中性说明。用户新建连接时，`openai_audio` 与 `ark` 一样不显示获取按钮。

# 4. 对照实验与结果分析

对照修改前的实测（第 1 节表格），修改后用同一组连接在本机重测并截图，结果回填本节。

# 5. 试点验证与方案取舍

取舍：OpenAI 官方的音频接口本身支持 `/models`，移除 `openai_audio` 后，这类连接也需要手动填写模型 ID（如 `whisper-1`）。考虑到当前实际使用的音频服务是本机 whisper.cpp 和 GPU 上的 WhisperX，这个代价可以接受；即使其他服务也不提供列表，404 的中性说明同样兜底。

# 6. 结果验收与闭环

实施后回填。
