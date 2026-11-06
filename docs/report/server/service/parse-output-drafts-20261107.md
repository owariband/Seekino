# Parse 模型正文草稿与 SSE 续租解耦验证

日期：2026-11-07。基线 `dev` / `416abffa`；工作分支 `feat/parse-output-drafts`；[PR #64](https://github.com/owariband/Seekino/pull/64)；[主设计](../../../design/03-product-reassessment/parse-output-drafts-trd.md)。当前执行状态只在[管理工作台](../../../dev/workbench.html)维护。

## 改善与实际接入

正文原来只在 Gateway 内存中收集。本次由 `bootstrap → ParseUsageService.capture_video → ParseOutputDraftService` 接入，观察 HTTP Chat Completions / Responses 正文，按调用 UUID 独立累积并保存 `parse_model_output_draft`。新增未保存正文达到 1,024 UTF-8 字节时写累计快照，调用退出时保存余量和终态；非流式响应只能在收到完整正文后保存。没有新增计时器，reasoning、工具参数、prompt 不混入正文。

模型生成入口的 `check_control` 从 `manager.check_active` 改为 `manager.raise_if_cancelled`。原前者会沿 `_require_*_active → queue._heartbeat_child` 锁父行并更新子任务租约；后者只检查本地取消。调用接纳、独立周期心跳、工具边界以及正式结果提交继续使用原持久检查。新增草稿表没有父任务外键，保存只访问草稿表。

草稿按 Server 固定的 tenant/task/actor 归属接收，拒绝模型上下文覆盖身份；重试和 refine generation 有独立调用 ID 与相关元数据。数据库保存失败保留正式模型调用，并停止本次调用的中途写入重试，退出时最多再试一次。草稿 `completed` 不能当作整个 Parse 已通过业务校验或已提交。

## 对照实验

测试 `test_real_sse_checks_draft_thresholds_and_lease_access_counts` 使用真实 OpenAI SDK、HTTP MockTransport、隔离 SQLite 和正式 Server 草稿作用域，喂入 65 段正文（64 × 32 字节 ASCII + 3 字节中文）。两组都启用同一草稿实现，只替换旧/新控制回调，隔离回调对租约访问次数的影响。

| 指标 | 旧持久检查回调 | 新本地取消回调 |
| --- | ---: | ---: |
| 模型接收路径额外持久检查 | 69 | 0 |
| 调用接纳检查 | 1 | 1 |
| 显式模拟周期心跳检查 | 1 | 1 |
| 总持久检查 | 71 | 2 |
| 草稿保存快照（字节） | 1,024 / 2,048 / 2,051 | 1,024 / 2,048 / 2,051 |
| 正式收集到的正文 | 相同 2,051 字节 | 相同 2,051 字节 |

69 包括逐 SSE 事件检查和接收器入口/退出检查；不能把它直接写成 69 个 token。该实验测量函数引发的持久检查次数，不是线上 SQL 延迟、持锁时长或吞吐基准。草稿保存三次中，前两次状态为 `streaming`，最后一次为 `completed`；未满阈值时数据库没有提前出现新前缀。

## 验证结果与复跑

使用仓库已有 Python 3.12 虚拟环境。最终 **941 passed，64 warnings，25.68 秒**；告警来自已有 SQLite expression index reflection 和 Starlette/AnyIO 弃用提示。原始日志在实施 worktree 的 `outputs/verification/parse-output-drafts-20261107/pytest.txt`，未将运行输出提交 Git。

```bash
python -m pytest \
  tests/server/test_parse_output_draft.py tests/server/test_parse_model_streaming.py \
  tests/server/test_video_parse_model_calls.py tests/server/test_database.py \
  tests/server/test_base_model.py tests/gateway/test_sse_proxy.py \
  tests/gateway/test_model_streaming.py tests/gateway/test_model_events.py \
  tests/gateway/test_kimi_streaming.py tests/agent/chat/test_streaming_delivery.py \
  tests/server/test_chat_streaming_trace.py tests/server/test_parse_usage.py \
  tests/server/test_video_parse_worker.py tests/server/test_video_summarize_service.py \
  tests/server/test_video_summarize_persistence.py tests/server/test_bootstrap.py \
  -m 'not live_agent' -q
```

覆盖 1,023/1,024 字节边界、中文跨阈值、超大单增量、空正文、退出余量、失败/取消前缀、持久失败后不逐 token 重试、并行线程调用、嵌套作用域及租户/任务归属、不可回退或替换前缀、独立表 SQL、重复建表和 MySQL LONGTEXT/no-FK DDL。Gateway 测试覆盖流式、非流式、仅终态正文以及断流/不完整响应；Server worker 和 Summary 回归覆盖既有取消、租约、提交与失败恢复行为。

首次回归发现非流式新增观察会被 Chat 当成实时增量。修复位于 `SupervisorRuntime.accept_text_delta`：`before_completion=false` 走原有完整正文交付，不生成伪实时来源；两条既有非流式/回退用例保持原断言并通过，未将失败测试改成新行为。初版新测试还有 JSON 字节预期和 Summary fixture 错误，已按真实输出修正后重新执行。

## 部署与验证边界

新增表通过显式 `init-db` 创建；本轮只验证隔离 SQLite 和 MySQL DDL，没有修改真实运行库、重启服务或恢复暂停的解析任务。没有测量 MySQL 实际锁冲突占比、真实 refine 活跃数、整集用时、模型费用或质量，因此不承诺 10 路并发或 60 分钟完成。

未暴露通用正文事件的 SDK/CLI 路径不支持中途草稿，工具参数与 reasoning 不在本功能范围。硬杀进程只保留最后成功快照；新增草稿不用于自动续写或正式结果恢复。本轮没有新增 UI/API 展示入口或自动清理任务；每次写入会重写该调用累计正文，其单调用写入放大需在真实负载中测量。
