# Kimi 3 low 整课复验（2026-11-02）

[PR #18](https://github.com/owariband/Seekino/pull/18) · [评审看板](pr18-kanban.html) · [主 TRD §4.24](../../design/03-product-reassessment/video-parse-performance-trd.md#424-11-02显式-kimi-3-low-的整课复验计划)

**结论：显式 K3 low 接线完成，真实整课未跑通。** 新 v8 通过正式 reparse 启动，20:27:44–20:35:32（Asia/Shanghai）运行 **467.434 秒，7 分 47 秒** 后失败；30 个 Understanding 请求没有任何 SSE，0/117 Understanding、0/115 Refine、0/117 索引。没有自动再开整课。

## 问题、改动与配置

原视觉工厂拒绝非 none 的 effort，适配器也没有传递该字段；v7 的实际请求是 k3.1-pro 且省略 effort。修复 `gateway/model_factory.py`、`gateway/openai/vision.py` 和工具 Chat 请求构造，使 Kimi profile 的 low/high/max 进入实际请求；标准 OpenAI 继续拒绝未支持的显式档位，旧 null/none 保持省略。

初始用例复现 ValueError，修复后 **452 项定向回归通过**，覆盖真实 SDK 配离线 HTTP、直接 JSON、两种工具 harness、stream 开关、备用模型独立 effort、配置与 100 秒首 SSE。代码 `2aaf9fa9` 已推送原 PR，隔离 API PID 26165 / 19621 已重启加载。

| 配置 / 身份 | 本轮值 |
| --- | --- |
| 完整课程 | Let's build GPT，116 分 20 秒，117 Chunk / 115 Refine 区间 |
| 新任务 | `2eef6cec-f494-407c-8939-cf415de1336e`，v8 |
| 旧任务 | `60bede4d-39c6-48ef-a4a6-a57e7b9a6e24`，v7，保留失败证据 |
| 模型 | **k3**，不是 k3.1-pro；官方模型/effort 合同见[模型说明](https://www.kimi.com/code/docs/en/kimi-code/models.html) |
| 实际出站 | 全部 30 请求为 `reasoning_effort=low`、`stream=true` |
| 绑定范围 | Understanding、继承它的 Refine、备用模型、Summary；隔离 YAML 的其他 Kimi 角色也统一，不修改主工作区默认配置 |
| 并发 / 首 SSE | Understanding 10、Refine 10、Index 1；首 SSE 100 秒，原总期限保留 |
| 音频 | ASR/分组与 v7 持久正文逐项一致；新 ASR=0，未调用分组模型 |
| 其他条件 | 完整图片、Prompt、重试与租约不变；未修正已知跨 Chunk 时间合同 |

## 真实失败分解

- 首批 sequence 0–9 各调用三次（首次与两次有限重试），共 30 次；21 次触发 `No first SSE event within 100 seconds`，9 次因父任务最终失败而取消。没有后续 Chunk 被重新扩展派发。
- 21 次期限实测为 **100.002–100.161 秒**，记录阶段均为 `waiting_for_headers`；其含义是尚未取得 HTTP 响应头，不能直接断言正在上传还是服务端排队/预填充。
- **0 个 SSE、0 份模型 JSON、0 份供应商 usage**。没有 429、504 或 Schema 拒绝的供应商响应；API 最终错误为 `PARSE_NODE_FAILED`：视频理解失败：模型请求超时。
- 本轮 Refine 没有启动，不能归因为 Refine 冲突或修正循环。30 个并发请求耗时不能相加成课程耗时；课程从开始到失败为 467.434 秒。
- 失败 usage 缺失不代表零 token 或免费。每 Chunk 的图片 data URL 合计 14.5–28.4 MB，首批十路 222,598,817 字节；这不是服务器确认接收的字节数，重试会重新构造这些输入。

## 最小连通性与传输对照

整课失败后只增加两次有界生成对照，均不混入课程进度/耗时指标：

1. 同环境只读 `GET /coding/v1/models` 在 **0.975 秒**返回 200，列表含 k3；此请求不生成内容。
2. 无图片的 K3 low 小请求在 **10.276 秒**返回合法 `{"summary":"ok"}`，返回模型 k3，182 input / 49 output tokens。说明该组合基本可调用；不能由小请求成功证明大视觉请求稳定。
3. 单路原始 Chunk 0 输入对照：实际请求体 14,548,052 字节，结果 `failed`，实际耗时 100.023 秒。请求体从第 1.416 秒开始上传。到 100 秒截止仍没有 send_request_body.complete，而是 send_request_body.failed；本样本停在上传阶段，不能解释为模型推理已运行 100 秒。无法仅凭此区分本机上行、网络链路或服务端接收。错误为 `ModelFailureError: No first SSE event within 100 seconds`。此对照只保留原始图片、文本、Schema 与 K3 low，改成并发 1、最多 512 输出 token，并在首 SSE 关闭；不提交业务成果，不能代表完整 Understanding 耗时或质量，用量未返回时仍未知。

## 与旧基线的比较与边界

| 指标 | v7，k3.1-pro / 省略 effort | v8，k3 / low |
| --- | --- | --- |
| Understanding 完成数 | 10 | 0 |
| 前十块 Understanding 阶段中位 | 264.465 秒 | 无成功样本，不计算 |
| 首 SSE | 中位 43.368 秒，最大 66.665 秒 | 全部无首 SSE；21 次 100 秒期限，9 次取消 |
| Understanding 输入 / 输出 | 2,434,057 / 84,920 | 30 次 usage 均缺失，未知 |
| reasoning tokens | 68,116 | 未取得，未知 |
| Refine / 索引 | 0 / 0 | 0 / 0 |
| 新 ASR | 0 | 0 |
| 最终失败 | 7 次额度 429、1 次断连（Refine 请求） | Understanding 首响应期限耗尽 |

模型版本、effort、网络时段以及首 SSE 策略与 v7 不同，不能做单因素提速或降本结论。采用参数透传修复，保留失败成果和有界重试；下一步应根据单路传输证据定位大视觉请求等待，确认可运行条件后再决定课程恢复，不继续盲目重开。Understanding 时间合同、JPEG 截头来源、声学对齐、人工质量和完整检索回看追问仍未闭环，B07/B20 保持未完成。

## 可复核证据

- [摘要与身份](evidence/kimi3-low-20261102/summary.json)、[逐请求记录](evidence/kimi3-low-20261102/request-telemetry.json)、[100 秒观测与图片体积](evidence/kimi3-low-20261102/first-sse-observed.json)。
- [正式 API 终态](evidence/kimi3-low-20261102/latest.json)、[部署版本](evidence/kimi3-low-20261102/deployment.json)、[音频正文校验](evidence/kimi3-low-20261102/audio-reuse.json)。
- [小请求](evidence/kimi3-low-20261102/small-probe.json)、[原 Chunk 单路传输](evidence/kimi3-low-20261102/large-probe.json)、[模型列表连通性](evidence/kimi3-low-20261102/models-connectivity.json)。
- 完整原始 trace、实际配置和控制/采集脚本保留在本机 `/Users/hypnos/workspace/agent/Seekino/outputs/verification/lets-build-gpt-k3-low-v8-20261102`，不提交凭据、媒体或模型思考正文。
