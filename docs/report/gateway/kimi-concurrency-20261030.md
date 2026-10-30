# Kimi Code API 10 路并发实测

测试时间：2026-10-30 10:47:30–10:47:59（Asia/Shanghai）。范围：当前开发配置的 Kimi Code 账号、`k3.1-pro`、短文本代码题；不代表长视频、多模态或持续负载验收。对应问题 [B21](../../dev/problems.md#b21kimi-code-api-10-路并发能力未验证)，过程见 [devlog](../../dev/devlog.md#devlog-kimi-concurrency-20261030)。

## 结论

同一 Key 连续两轮 10 路并发均成功：20/20 返回 HTTP 200，JSON 请求标识与计算答案逐一正确，结束原因全部为 `stop`。零 429、零网络错误、零自动重试。串行对照 3/3 成功，总计 23 个请求，API 返回用量合计 6,484 tokens。

这证明当前账号在本次测试时能够接受 10 路短请求；不能据此证明服务端同时分配 10 个推理实例、账号上限恰为 10、整季解析可以提速 10 倍或持续 10 路长上下文同样稳定。

## 方法与配置

- 从 `.env.local` 和 `config/local-dev-config.yaml` 读取当前 source、Key 与模型，端点为 `https://api.kimi.com/coding/v1/chat/completions`；不在报告或原始证据保存密钥。
- 复用 Gateway 的 `completion_options(profile="kimi", reasoning_effort="none")`，实际请求仅带 `temperature=1`，没有发送关闭 thinking 的参数。模型响应名称均为 `k3.1-pro`。
- 使用 OpenAI Async SDK，`max_retries=0`，单次 90 秒超时，`max_completion_tokens=1024`，JSON 输出；无 fallback、无工具循环。SDK 使用自身真实默认客户端标识。
- 合成题为执行 `sum(x*x for x in range(i, i+5))`，返回请求标识与整数答案；每轮题目及预期值完整保存在证据中，没有传业务素材。
- 顺序执行 3 次串行对照，然后通过异步 Barrier 同时释放 10 个请求，完成后重复一轮。HTTP 连接池上限 20，记录 request hook 到完整响应结束的在途区间。没有并发超过 10 的探索或持续压测。
- 未进入 Seekino Server/Gateway 的完整 Agent 编排，没有写业务库、变更解析任务、重启服务或调整长期并发配置；用量保存在本次独立证据中，不进入产品用量页。

## 测量结果

| 组别 | 请求数 / 并发 | 成功 | 整组耗时 | 单请求耗时中位数 | 单请求范围 | 峰值在途 | 发出跨度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 串行对照 | 3 / 1 | 3/3 | 14.306 s | 4.665 s | 4.413–5.226 s | 1 | 串行，无同时发出 |
| 并发第一轮 | 10 / 10 | 10/10 | 6.582 s | 5.126 s | 3.304–6.577 s | 10 | 5.957 ms |
| 并发第二轮 | 10 / 10 | 10/10 | 7.646 s | 4.717 s | 3.321–7.637 s | 10 | 7.549 ms |

整组时间从创建这一组任务到全部结果写出；单请求时间从 Barrier 释放到完整响应解析完成，包含网络与可能的供应商排队时间。串行与并发组题数不同、连接有预热影响，不能直接用整组耗时比声称业务加速倍数。只有两轮，不报告 p95/p99 或长期成功率。

原始证据：[逐请求结果与用量](../../../outputs/verification/kimi-concurrency-20261030/results.json)、[HTTP 在途事件](../../../outputs/verification/kimi-concurrency-20261030/events.json)、[测试脚本](../../../outputs/verification/kimi-concurrency-20261030/probe.py)。脚本 SHA-256 保存在结果文件；已有结果存在时脚本拒绝覆盖。

执行命令：

```bash
UV_CACHE_DIR=/private/tmp/seekino-uv-cache uv run --no-sync --env-file .env.local \
  python -u -m outputs.verification.kimi-concurrency-20261030.probe
```

## Seekino 配置与供应商限制的区别

测试进程读取的配置为 Understanding/Parse 并发 2、Refine 并发 10、Index 并发 1、Video 并发 1、Summary 并发 1、Chat kicks 并发 4。它们是业务执行槽位，不等于供应商账号的统一请求上限；旧任务还会使用各自冻结的配置快照，未审计正在运行任务的实际槽位。

`sources.codex_local.max_concurrency: 10` 只控制 Codex 本地适配器，不能解释为 Kimi 已配置 10 并发。`max_model_calls=100` 是当前保留的任务配置值，源码明确累计调用数不限制 admission，不能拿它当账号并发上限。本次未修改任何这些值。

截至本次核对，[Kimi 官方介绍](https://www.kimi.ai/zh-hans/resources/kimi-code-introduction)（页面标注更新于 2026-10-10）写明最多 30 个并发请求；[会员权益文档](https://www.kimi.com/code/docs/kimi-code/membership.html)说明所有设备和 API Key 共享配额、存在 5 小时滚动频控，并区分新旧套餐规则。介绍页与现行会员规则的周额度表述不同，本报告不据介绍页推定当前账号套餐或剩余额度。实际在途达到 10 且全部成功是本次账号能力的证据，官方上限并不保证任何负载下的延迟与持续吞吐。
