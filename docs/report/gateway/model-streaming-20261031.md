# 通用模型 stream 配置修复记录（2026-10-31）

关联：[TRD](../../design/03-product-reassessment/model-streaming-trd.md)、[PR #22](https://github.com/owariband/Seekino/pull/22)。本次修复属于 PR #20 合并后的后续修正，保留同一 PR #22 交付。

## 问题与改动

此前 `GatewayClient` 只有 Kimi 视觉入口启用流式，Chat / Structured 和 DeepSeek Responses 仍有固定非流式路径。创建模型时无法统一决定传输方式。修复增加严格布尔 `ModelBinding.stream`，默认 true、显式 false；旧 Agent 配置投影和主模型 / fallback 均保留各自值。

共享接收器移至 `gateway/streaming/chat.py` 和 `gateway/streaming/responses.py`，各自处理协议。Chat Completions 复用 SDK 解码及 delta 合并，验证终止 choice，保留推理内容、工具参数和最后 usage；Responses 保留终止事件、序号和最终正文一致性验证。完整响应继续进入既有 Schema、finish reason 和工具合同，半截参数不能执行工具。不新增自动重试，也不因出错自动切换传输方式。

工厂、Agent Loop、Structured、Deep Agents、视觉 JSON/工具轮次、本地 Ollama/llama.cpp 和 Ark 视频转写接入上述能力。独立 Query Hook 使用 `rewriter_stream`。原生 Codex/Claude 不发送生成 HTTP stream 参数，显式 false 会被拒绝；独立音频、WhisperX、向量、声纹协议不受此传输开关控制。

Server `ParseConfig.from_defaults → to_video_parse_v2 → 持久快照 → VideoParseRuntimeService._invoke_multimodal → GatewayClient` 现在保留开关及 fallback 值；ASR 的生成式视频路径也透传。新任务保存有效值。旧快照缺字段时默认 true，但序列化不增添该字段，因此历史幂等指纹不变。新快照带有新字段，旧版本未必可读取，回滚必须评估任务恢复兼容性。

前端 HTTP SSE 仍是业务事件链：Deep Agents 完成模型轮次后调用 `handle_text`，Chat runtime 发布答案快照，Server SSE 发送 `answer.snapshot`。本次改变模型 HTTP 传输，不声称新增前端逐 token 展示。

## 归因证据

| 提交 | 已确认行为 | Git 身份与证据边界 |
| --- | --- | --- |
| [1ab13600](https://github.com/owariband/Seekino/commit/1ab13600)（2026-09-15） | 引入结构化 Agent 的 `streaming=False` | 作者/提交者 Hypnos，无 Agent 署名，不能由 Git 身份认定用户亲自决策 |
| [862c2699](https://github.com/owariband/Seekino/commit/862c2699130c80c7660797a9c07ed4ce80c2ab3a)（2026-09-17） | Chat 从 `stream_text` 转为完整响应工具循环，同时加入 `stream=False` 和测试断言 | 作者/提交者 Hypnos；提交说明关注工具轮次完整性，未找到用户明确要求关闭模型流式的记录 |
| [6bf7e6e5](https://github.com/owariband/Seekino/commit/6bf7e6e5)（2026-09-23） | 拆分适配器时保留 false，Responses SSE 收集器移入 Ark 目录 | 继承既有实现，不能据此推定最初决策人 |
| [71a1343e](https://github.com/owariband/Seekino/commit/71a1343e3cf24ddcc51d23c1faab2d667b749cbc)（2026-09-24） | 新增 OpenAI 视觉适配器时固定 false | 作者/提交者 yyu03，无 Agent 署名 |
| [f82fabc2](https://github.com/owariband/Seekino/commit/f82fabc2959c8d0e27ed6a3a45868df16a83b34c) / PR #20（2026-10-30） | 仅为 Kimi 打开 stream，并新增其他 profile 仍为 false 的测试 | 本会话 Codex 实施，不能把这个范围错误归责于用户或供应商 |

Git 可以定位引入点，不能在没有额外记录时证明更早是哪个 Agent 或谁作了产品决策。接收完整模型轮次与 HTTP 流式并不冲突，本修复在共享收集器中组装后继续原有工具校验。

## 隔离验证

环境：分支 `refactor/model-streaming`，工作树 `/private/tmp/seekino-model-streaming-v2`，Python 3.12，复用项目已安装依赖。模型请求使用真实 SDK 加 `httpx.MockTransport`，测试凭据为固定占位字符串。Langfuse 集成测试仅绑定本机回环 HTTP 服务；没有新调用真实模型，也没有发送用户素材或修改任务数据库。

覆盖矩阵：

- 4 个 profile / provider × 默认、true、false × JSON、Loop、Structured、Supervisor，48 项真实 SDK 请求断言。
- 主模型 / fallback 反向开关、同名 Ark 模型的独立 fallback、Ark 视频转写、本地生成与文本流、Query Hook、严格布尔校验，合计 79 项新工厂和传输测试。
- 原协议故障集继续验证断流、EOF、截断、错误顺序、取消、超时和工具执行边界。完整响应旧测试明确传入 false，只验证兼容分支。
- Parse 13 项新增配置案例，以及已有配置 / OCR / Harness 输入与 runtime 创建回归合计 397 passed。
- 隔离 HTTP / SQLite 验证 `GET /api/uploads/parse-config → POST /api/uploads/create → GET /api/uploads/get`，true / false 与 fallback 反向值均保存并返回；与上传服务的持久化、幂等和恢复回归合计 113 passed。

执行命令（`python` 为项目 `.venv/bin/python`）：

```bash
python -m pytest tests/gateway -m 'not live_agent' -q --tb=short
python -m pytest tests/server/test_parse_model_streaming.py tests/server/test_parse_ocr_config.py tests/server/test_config.py tests/agent/video_parse_v2/test_input.py tests/server/test_video_parse_worker.py::test_canvas_channel_preserves_output_budget_and_uses_valid_policy -q --tb=short
python -m pytest tests/server/test_api.py::test_parse_config_route_returns_defaults_and_rejects_false_frame_cap tests/server/test_upload_service.py -q --tb=short
npm --prefix web run typecheck
```

Gateway 最终全量结果：**1345 passed、5 failed、1 skipped、1 deselected，41.01 秒**。Parse 相关回归 **397 passed，8.88 秒**（1 项依赖弃用警告）。相关模型配置/适配器聚焦回归另有 236 passed；Query Hook、usage 和早期传输矩阵回归 186 passed，以上与全量测试有重叠，不相加。

Parse HTTP / 上传服务回归 **113 passed，10.83 秒**（同一项依赖弃用警告）；前端同步 Parse 配置的可选 stream 字段，`npm --prefix web run typecheck` 通过。CI 当前只有面向 main 的人工 dispatch，本 PR 目标 dev，未将本地验证冒充 GitHub CI。

全量中的以下 5 项在未改业务代码的 `dev` / `580df6cd` 同环境逐项复现（5 failed，1.96 秒），因此没有把它们改成通过或删除断言：

1. `test_offline_otlp_payload_headers_hierarchy_and_time`：Langfuse 缺少预期 `x-langfuse-ingestion-version` header。
2. `test_default_model_migration_preserves_asr_embedding_agent_and_parse_strategy`：旧模型配置迁移等价断言与当前配置不一致。
3. `test_independent_supervisor_keeps_agent_models_and_environment_overrides_separate`：期待 Codex，当前默认 Ark。
4. `test_codex_supervisor_without_independent_binding_requires_the_agent_source_to_match`：依赖已过时的 Codex 默认配置。
5. `test_fallback_media_is_checked_against_the_active_context_budget`：既有 context 预算用例用尽模拟响应；原分支 StopIteration，本分支 SSE 中表现为由 StopIteration 导致的 RuntimeError。

原始日志保存在本地 `outputs/verification/model-streaming-20261031/`，不含真实模型 payload 或凭据。

## 验收边界

已验证的是配置到正式创建入口的传递、模拟 HTTP 上的协议正确性和相关回归。未验证其他供应商的实际线上 SSE 兼容性、真实任务数据库提交、整集质量、延迟分布或成功率。本次未重启服务、未修改正式任务。历史 Kimi 3 fps / 189 帧的单 Chunk 流式成功证据仍见 [PR #20 报告](kimi-streaming-20261030.md)，不重复消费之前的一次性素材授权。

流式减少生成期间长时间等待完整响应的风险，但不会减少输入 token；在首个事件前、上下文超限或供应商故障时仍可能失败，不能承诺“stream=true 就不会断连”。

## Temperature 配置补充（2026-10-31）

用户要求业务创建模型与 YAML 均可选择温度，提供默认值，Kimi 固定为 1。本补充继续在 PR #22，先提交设计 `27602542`，再实现与验证；没有新建 PR。基线 `2e0fb989` 的 `ModelBinding(temperature=0.2)` 返回 `extra_forbidden`，多数请求适配器固定为 0，部分视觉路径省略该参数。

新增 `ModelBinding.temperature`、旧 Agent / fallback 对应字段和独立 `rewriter_temperature`：默认 0.0，接受 [0, 2] 的有限数值，拒绝字符串、布尔、null、NaN 和无穷。Kimi profile 在解析绑定时归一为 1，底层 Chat / Vision 适配器仍执行固定约束。固定值放在轻量 constants 模块，配置导入不会额外加载 Agent / LangGraph runtime。

覆盖 Chat、Structured、Deep Agents Supervisor、视觉 JSON/工具、Responses、Ark 视频转写和本地生成；主模型与 fallback 分别保留值，同名 Ark 模型也能选择不同温度的 fallback。Server Parse profile / HTTP 配置 / 任务快照 / runtime 保留该字段；旧快照缺字段时按 0 读取，序列化不增添字段。默认配置来源的 Kimi 快照是有效值 1；直接提交的快照保存调用方参数，创建时仍由 Kimi 规则归一为 1。

兼容边界：默认 0 延续多数原有调用行为，不能解释为质量最优值；此前省略温度的普通 Vision / Ark Responses 现在明确发送 0，Kimi Vision 明确发送 1。供应商仍可拒绝其他不支持的值。原生 Codex / Claude 不支持温度，非默认值明确报错，默认值不传给 CLI；独立音频、向量和声纹不发送生成温度。新任务格式带有 temperature，回滚旧代码须检查快照兼容性。没有增加网页控件。

| 指标 | 温度补充前 | 温度补充后 | 结论 |
|---|---|---|---|
| 内部 / YAML 显式温度 | 字段被拒绝 | 56 项配置验证通过 | 默认、有效范围、旧 Agent 投影、Kimi 主模型 / fallback 归一通过 |
| 工厂与最终请求矩阵 | 79 项 stream 验证 | 226 passed，7.66 秒 | 180 组五入口组合及 fallback、本地、转写、改写均保留温度 |
| Gateway 全量 | 1345 passed，5 failed | 1548 passed，5 failed，1 skipped，1 deselected；45.15 秒 | 增加 203 项通过，失败仍为上面列明的 5 项基线 |
| Parse 配置 / runtime | 397 passed | 425 passed，8.81 秒 | 保存、恢复、旧快照形状及正式模型构造通过 |
| HTTP / SQLite 上传 | 113 passed | 113 passed，9.34 秒 | 同一接口用例新增主模型、ASR、fallback 温度保存与取回断言 |
| 前端类型检查 | 通过 | 通过 | 新增可选 number 字段，无新 UI |
| 真实模型质量 / 部署 | 本轮未测 | 本轮未测 | 无真实请求、服务重启或正式任务变化 |

范围与上文 stream 验证使用同一 Python 3.12 环境；以下聚焦测试与全量测试重叠，数字不相加：

```bash
python -m pytest tests/gateway/test_model_streaming.py -q --tb=short
python -m pytest tests/gateway/test_model_config_v2.py -k temperature -q --tb=short
python -m pytest tests/gateway -m 'not live_agent' -q --tb=short
python -m pytest tests/server/test_parse_model_streaming.py tests/server/test_parse_ocr_config.py tests/server/test_config.py tests/agent/video_parse_v2/test_input.py tests/server/test_video_parse_worker.py::test_canvas_channel_preserves_output_budget_and_uses_valid_policy -q --tb=short
python -m pytest tests/server/test_api.py::test_parse_config_route_returns_defaults_and_rejects_false_frame_cap tests/server/test_upload_service.py -q --tb=short
npm --prefix web run typecheck
```

原始日志位于 `outputs/verification/model-temperature-20261031/`：`gateway-final.log`、`parse-config-runtime.log`、`http-upload.log`、`typecheck.log`。首次沙箱全量额外出现 6 项回环监听权限失败及 2 项视频转写构造断言未更新；保留为 `gateway.log`。最终全量允许离线测试监听 127.0.0.1，转写用例增加显式温度及传递断言后，仅剩原有 5 项失败。测试不依赖真实供应商或业务数据库。
