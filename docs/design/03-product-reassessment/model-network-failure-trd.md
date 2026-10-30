# 1. 问题与现状

需求：模型连接异常分类、有限重试与可诊断日志。2026-10-30，[PR #13](https://github.com/owariband/Seekino/pull/13)。

10-30 15:08 关联续版：#13 已合入 dev，以下原始提交阶段的部署描述保留历史口径。GPT v5 本次先在 14:49:19 收到 504，节点有限重试后于 14:51:49 以 `APIConnectionError → ConnectError → gaierror` 终止，停在 8/117 Chunk。后续 DNS/HTTPS 探测可达，不证明长请求稳定，也没有证据将本次归因于此前 Clash 关闭。分类和重试已执行，供应商/DNS 的剩余不确定性仍由本需求跟进。

这一故障同时触发整片 ASR 协调器退出并清理内存，使此前 76 分 32 秒的计算不能在新协调器中复用。该后果由 [ASR 主设计续版](local-whisperx-asr-trd.md)承接：视频级持久成果、正式 GPU 接线及下游失败后的零重复 ASR 验收。新跟进不把持久化当成修复网络，也不把再次可连接当成整集成功。详见[同一实测报告](../../report/gateway/local/whisper-metal-benchmark-20261030.md)。

Karpathy v3 任务在直接 JSON 理解链路失败。OpenAI SDK 的 APIConnectionError、APITimeoutError、HTTP 504 原样穿透 Gateway；节点重试只识别 Python/httpx 异常或 MODEL_TIMEOUT。最终被呈现为“执行异常”，并停止整集派发。正式节点终止日志没有异常链，无法区分网络断连与服务端超时。

2026-10-30 实测：EP1 Chunk 0 的模型请求为 12:01:45–12:06:57（311.88 秒），返回 nginx 504；969 秒是含 ASR 的 Chunk 总耗时。EP4/5 四个不同起点请求均在 12:14:22 连接失败，EP7/8 四个请求在 12:22:44 连接失败，未达到 600 秒请求阈值。共同连接中断是假设，无法由缺失的底层异常链判定责任端。四个已成功请求耗时 183–296 秒，输入 232510–243436 tokens，仍有 reasoning tokens。错误重试不能解决输入规模与服务性能问题。

# 2. 影响因素、假设与评价指标

| 指标 | 基线 | 验收 | 方法 |
|---|---|---|---|
| SDK 连接/超时/504 分类 | execution_failed | 分为连接异常、请求超时、服务不可用 | MockTransport 产生真实 SDK 异常 |
| 暂态错误有限重试 | 未命中 | 遵循现有节点次数和执行期限 | 工作流回归 |
| 认证/坏请求/取消 | 不应盲目重试 | 仍不重试 | 负例验证 |
| 失败可诊断性 | 缺少 cause | 脱敏异常链、状态码、请求 ID | 日志测试 |
| 完整视频可检索 | 0 | 至少 1 集 | 正式任务观察，待验收 |

# 3. 改造方案与建模

Gateway 将无工具视觉请求的 SDK 暂态错误转换为通用 ModelFailureError，保留 cause。API 超时先于连接错误分类；504 单独标为上游网关超时。429、500、502、503 为暂态服务异常。Agent 按通用 reason 判断重试，不依赖 Provider SDK。直接 JSON 路径接上既有 opt-in model_diagnostics，逐请求保存异常链。Server 显示明确中文错误并复用已有脱敏异常链记录，终止事件不伪装成重试事件。

流程：Provider 异常 → Gateway 分类 → 节点有限退避 → 成功提交或耗尽后终止 → 安全提示与诊断日志。取消优先于连接异常。保留租约、版本、幂等、既有 deadline 与终止后停止派发合同；不迁移数据、不改并发或图像采样。

# 4. 对照实验与结果分析

先用真实 SDK + MockTransport 复现不同错误，验证单次调用不在 SDK 内暗中重试；再测节点判定、API 提示和终止日志。2026-10-30 隔离回归 233 passed（19.18 秒）：`tests/gateway/test_openai_media.py`、`tests/gateway/test_model_diagnostics.py`、`tests/agent/video_parse_v2/test_framework.py`、`tests/server/test_video_parse_queue.py`。新增 MockTransport 覆盖断连、连接拒绝、读超时、429/500/502/503/504 和 400/401；trace 验证保留 RemoteProtocolError、504 状态码且不包含密钥。节点成功恢复/耗尽验证最多 3 次、ASR 只运行一次。以上真实耗时只用于诊断，不据此估计 p95/p99 或长期成功率。

# 5. 试点验证与方案取舍

用户授权重启解析。EP1 已于 12:43:07 通过正式 retry API 恢复，沿用 v3 和已有提交。其余失败任务暂不同时启动。先测试修复，再部署观察；如果连接继续失败，用新异常链定位网络或服务端。重试会增加耗时与模型费用，仍受原有限预算约束。

# 6. 结果验收与闭环

当前：调查与修复实现完成，233 项相关回归通过；正式部署与整集闭环待验证。不能把模型接口可访问、离线测试通过或一个 Chunk 成功当成整集完成。下一步：创建独立 PR、部署并观察 EP1。
