# 100 秒首 SSE 期限与 Kimi Parse 用量核对（2026-11-02）

> PR #18 评审入口：[HTML 小看板](pr18-kanban.html)。按 TODO / Progressing / Done 展示同一 PR 的交付范围与证据；状态快照以页面时间为准，并与 B07 / B20 同步维护。

分支 `fix/video-asr-recovery`，[PR #18](https://github.com/owariband/Seekino/pull/18)。设计入口：[模型网络故障 TRD](../../design/03-product-reassessment/model-network-failure-trd.md#11-02-first-sse-result)。基线 HEAD `d60b1bf1`，本次变更见同一 PR；未另建需求或 PR。

## 问题、策略和适用范围

旧 Chat SSE 接收器在得到 SDK chunk 后才检查总期限。等待响应头、没有 SSE 的阻塞读不能及时检查；只发 SSE 注释心跳还会不断刷新网络读超时。当前 Parse 的 Understanding 和直接 JSON Refine 都经 `OpenAIVisionModel.generate` 无工具入口，因此在该入口接入 100 秒首事件期限，并由共享 Chat 接收器解除计时。

- 从发起本次 SDK 请求开始，100 秒没有解码得到首个 SSE 即关闭独占 HTTP/1 连接，以 `MODEL_TIMEOUT` 返回原有节点有限重试；HTTP 响应头或注释心跳不算首事件。
- role、reasoning、正文等首个有效 SSE 均计入；收到后解除启动计时，既有总期限和后续读预算继续生效。更短的调用总预算优先。
- HTTPX trace 在响应头前记录连接句柄，计时器唤醒阻塞读；不创建一个留在后台继续请求模型的工作线程。所有期限计时器在调用退出前回收。取消仍保留调用取消原因，不伪装为可重试超时。
- 生成适配器显式使用独占 HTTP/1 客户端，不关闭共享 HTTP/2 socket。非流式、工具循环、Responses 和其他供应商协议本轮未扩展。不是修改前端 SSE，也不改变租约、JSON 门禁、任务重试上限或 ASR 成果。

## 本地验证

用真实 OpenAI SDK、HTTPX 与本地 HTTP / HTTPS 服务验证；HTTPS 使用临时自签证书及本地信任，没有供应商请求。首事件期限缩放到 0.25 秒，原总期限缩放到 1–2 秒，避免为验证逻辑等待 100/600 秒。

| 场景（每个对照各 1 次） | 旧接收方式，原预算 1 秒 | 新首事件预算 0.25 秒 | 连接释放 |
|---|---:|---:|---|
| 不返回 HTTP 头 | 1.164 秒 | 0.270 秒 | 两组均观察到关闭 |
| 有 HTTP 头，无 SSE | 1.023 秒 | 0.271 秒 | 两组均观察到关闭 |
| 持续注释心跳，无 SSE | 1.501 秒，由测试主动 EOF 结束 | 0.273 秒，自主超时 | 新组观察到关闭 |

心跳基线 1.501 秒不是自然超时：原实现超过 1 秒仍阻塞，测试在 1.5 秒主动结束服务器；不能把该数值写成原实现的等待上限。正式配置的 100 秒常量在源码中，以上是缩放测试，不是 Kimi 实测耗时。所有对照均返回 `model_timeout`。[对照记录](evidence/first-sse-timeout-20261102/local-deadline-comparison.json)。

回归命令（仓库 worktree，Python 3.12，`/Users/hypnos/workspace/agent/Seekino/.venv/bin/python`）：

```bash
python -m pytest tests/gateway/test_first_sse_deadline.py tests/gateway/test_kimi_streaming.py tests/gateway/test_openai_media.py tests/gateway/test_model_streaming.py tests/gateway/test_model_diagnostics.py tests/server/test_video_parse_model_calls.py tests/agent/video_parse_v2/test_framework.py -q
python -m pytest tests/gateway/test_first_sse_deadline.py -q
```

首轮 391 passed / 11.76 秒；扩展 HTTPS 后首事件文件 20 passed / 9.75 秒，合并去重 401 个用例。覆盖无头/无事件/心跳、首 role/reasoning 后继续生成、原总预算更短与仍然有效、非流式不变、取消语义、旧连接关闭后才进入下一次请求、SDK 没有额外重试及超时 trace。既有节点回归确认最多三次尝试和 ASR 只执行一次。没有跑全仓测试，也没有真实 Kimi 付费重试。

## 用量：不是显式 low，且完整重开重复付出了视觉输入代价

v7 `60bede4d-39c6-48ef-a4a6-a57e7b9a6e24` 的实际请求是 `model=k3.1-pro`、`stream=true`，没有 `reasoning_effort`，也没有关闭 thinking。冻结 `reasoning_effort=none` 在此生成路径仅表示省略；`gateway/model_factory.py` 还会拒绝非 none 的视觉生成绑定。不能把它说成 K3 low 或无推理。

[Kimi Code 模型配置](https://www.kimi.com/code/docs/en/kimi-code/models.html)确认 K3.1 Pro 支持 low/high/max，不能关闭 thinking。该表写默认 high，而同期[Overview](https://www.kimi.com/code/docs/en/)写默认 max；原始响应未回显实际 effort，本轮只确认没有显式 low，不替供应商推断实际档位。本次没有顺带更换模型或修改任务冻结配置，显式 effort 贯通仍待处理。

| v7 的 10 次 Understanding | 实测 |
|---|---:|
| 图片 | 第一块 189，其余各 198，共 1,971 张 |
| 每请求文本长度 | 14,378–15,080 字符；不是 token 数 |
| 累计 input tokens | 2,434,057 |
| 累计 output tokens，含 reasoning | 84,920 |
| 其中 reasoning tokens | 68,116，约占 output 的 80.2% |
| Schema 修复 / 模型重试 | 0 / 0 |
| 首 SSE 最大等待 | 66.665 秒 |

全量图片是大输入的直接来源，但返回 usage 未拆分 image/text/cache tokens，不能给出准确图片占比或缓存扣费比例。已确认的 Understanding 跨 Chunk 时间规则冲突会让模型反复考虑边界，见[既有调查](../server/lets-build-gpt-clean-parse-20261102.md#v7-quota-terminal)；本轮没有因果对照，不能把全部 reasoning 都算成无效推理。100 秒门槛不会降低这十次首事件正常请求的 token 消耗。

今天三轮完整解析尝试在上海时间 15:13–17:59 发起请求，均在截图指示的 14:24–19:24 五小时窗口内：

| trace 范围 | 请求数 | 有 usage / 未拿到 usage | 已上报 input | 已上报 output（含 reasoning） |
|---|---:|---:|---:|---:|
| v5 | 47 | 22 / 25 | 4,901,377 | 158,456 |
| v6 | 53 | 24 / 29 | 4,924,424 | 186,501 |
| v7 | 18 | 10 / 8 | 2,434,057 | 84,920 |
| 合计 | 118 | 56 / 62 | 12,259,858 | 429,877 |

每个 call_id 只计一份最后 usage，不把 SSE usage 与完整响应重复相加。未返回 usage 的取消/失败请求不记作零 token，无法核实供应商扣额；这也不是全账号账单。完整重开复用了音频，却重做 Understanding，因此多轮重复输入放大了消耗。输入计数包含图片，未证明相同图片是否命中供应商缓存。[用量证据](evidence/first-sse-timeout-20261102/token-audit-summary.json)；完整逐请求汇总保留在本机 `outputs/verification/first-sse-timeout-20261102/token-audit.json`。

截图的 Code 五小时 100% 与月度总量 81% / Code 39.47% 是不同口径，不能直接换算成本。超时关闭本地连接也不保证上游立刻停止计费；这项修复首先减少无首事件的等待，不承诺降低已完成的正常请求开销。

## 部署和未完成事项

已核对原 API PID 20256、worktree、控制脚本和隔离库；三个旧运行态任务均暂停，父/Chunk/Interval 活动租约均为零。通过原控制脚本重启 API 为 PID 22571，仍监听 `127.0.0.1:19621`，`/healthz` 返回 200。[部署记录和实现文件 SHA-256](evidence/first-sse-timeout-20261102/deployment.json)。MySQL、ES、音频和任务数据未迁移；没有调用 retry/reparse 或真实模型。

v7 仍是额度耗尽后的失败版本，10/117 Understanding、0/115 Refine；本轮不将已失败的旧结果写成恢复成功。真实供应商的 100 秒策略收益、显式 low 选型效果、Prompt 边界冲突修正与完整课程检索问答仍待验证。
