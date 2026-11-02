# Refine 共享成员冲突恢复：实现与本地验证

日期：2026-11-02，Asia/Shanghai。分支 `fix/refine-conflict-recovery`，
[Draft PR #26](https://github.com/owariband/Seekino/pull/26)。
上游为 [PR #18](https://github.com/owariband/Seekino/pull/18) 的 `b01afce9`；首提交 `19e497ea`
只有 TRD，后续实现沿[性能主设计 §3.12–3.13](../../design/03-product-reassessment/video-parse-performance-trd.md)。

本轮完成源码和受控验证，没有重启 API、迁移业务库、重试课程或调用外部模型。
真实 Kimi 并发耗时、token、语义质量和完整课程完成时间均未验收。

## 问题与实际改动

原行为在模型生成期间不接收内容版本变化；提交冲突返回 False，下一轮丢弃候选重新生成。
新行为在读取快照前注册订阅。事务成功退出后返回实际改变正文的成员版本，只通知同视频、
成员相交的其他 Refine；相同内容、仅 scope 变化、回滚和重复版本不触发新生成。

每次请求拥有取消范围，执行级租约、额度和用量仍由原 Manager 管理。更新后先结束旧请求并
等其释放，再读取新快照重发。若响应已完整返回，优先交给原三方合并；确实冲突则将具体
原因、位置和最近完整候选带入新输入。Schema 修正中更新也保留该候选及错误位置。
未完整返回的 JSON 或内部推理状态不伪造为可恢复成果。

所有生成、修正和刷新共用原尝试期限。暂停、租约丢失和全执行取消仍终止任务；底层调用
忽略取消时保留 `BlockingCallCancelled.completion`，由 Consumer 在真实退出后释放资源。
没有新的数据库表、前端/API、DeepAgents、ASR/Understanding 调度或首 SSE 超时策略。

主要入口为 Agent `refine/agent.py`、`refine/input.py`；Server `video_parse.py`、
`video_parse_refine_updates.py`、`video_parse_model_calls.py`；成果事务与 DTO；
通用传输 `gateway/streaming/chat.py`、`gateway/usage.py`。Gateway 不承担业务门禁。

## 为什么额外调整传输取消

可控回调验证通过后，用真实 OpenAI SDK + HTTPX + 本地 HTTP/SSE 发现：仅关闭客户端或流，
不能保证唤醒另一个线程中阻塞的 `recv`。固定在客户端已收到首个 SSE 后发更新，持续数据与
静默流均复现过等待至本次 0.7 秒期限；线程栈停在 HTTPcore 同步读。

修正后先执行后注册的内层流取消回调，再清理客户端；HTTP/1 响应通过连接暴露的 socket
执行 `shutdown` 后关闭流，唤醒读线程。HTTP/2 可能复用连接，不 shutdown 整条连接。
不暴露 socket 或尚未取得响应的适配器仍依赖原取消/期限兜底；本地关闭不证明远端停止计费。

## 对照与验证结果

所有模型输出由受控本地服务生成；这些是传输/状态机验证，不是 Kimi 的语义或速度实验。
HTTP 前后使用同一脚本、同一首 SSE 触发点和 0.7 秒期限；HTTPS 只复核修正后行为。

| 场景 | 修正前 | 修正后 | 观察结论 |
| --- | --- | --- | --- |
| HTTP，首 SSE 后持续返回数据 | 0.70186 s，期限触发时底层未退出 | 0.000710 s，刷新成功 | 旧槽位释放，替代请求完成 |
| HTTP，首 SSE 后静默 | 0.70166 s，期限触发时底层未退出 | 0.001042 s，刷新成功 | 无需等待下一份数据才退出 |
| HTTPS，上述两种场景各 3 次 | 未单测前值 | 6/6 成功，0.000663–0.000977 s | 全部旧调用退出后才重发 |
| 回滚/no-op/不相交/重复版本 | 无专用成员通知 | 确定性检查通过 | 不无差别打断执行 |
| 完整结果与更新同到 | 提交时版本合并 | 优先保留完整结果并尝试原合并 | 避免丢弃已完成候选 |
| Schema 修正中更新、提交冲突 | 冲突后只返回 False | 最近候选、错误位置及最新正文均保留 | 可以在已有输出基础上修改 |
| 迟到/暂停/期限 | 原执行级取消不可继续 | 请求级取消可继续，全执行取消与期限仍终止 | 资源未退出时不提前释放 |

本地数值只证明阻塞读被唤醒，没有稳定 p95/p99 或整集性能结论。取消后新请求仍需模型推理，
可能产生额外 token；是否抵消冲突等待须用真实并发 A/B 评估。

扩展检查覆盖 Agent V2、Server worker/queue/model calls/事务/Summary 和全部 Gateway：

- 初次：2215 passed、11 failed、1 skipped、1 deselected。七项 Langfuse 测试因沙箱禁止绑定本地端口失败。
- 允许本地网络后重跑 Langfuse：7 passed、1 failed、1 skipped；其中六项端口失败恢复通过。
- 最终补充已关闭响应的保护及 8 项边界用例，相关 327 项检查通过。
- 按唯一测试合并：**2229 passed、5 failed、1 skipped、1 deselected**。
- 在未改动上游 `b01afce9` 同环境运行五项残留失败：5/5 复现。分别是 Langfuse ingestion header、
  三项默认配置旧断言和 fallback context。没有跳过、删改这些失败来获得全绿。
- `git diff --check` 通过。没有运行无关前端测试；本轮未改前端。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/agent/video_parse_v2 tests/server/test_refine_content_updates.py \
  tests/server/test_video_parse_model_calls.py tests/server/test_video_parse_repository.py \
  tests/server/test_video_parse_worker.py tests/server/test_video_parse_queue.py \
  tests/server/test_video_summarize_persistence.py tests/gateway -m 'not live_agent'
```

## 原始证据与运行状态

本机证据目录（Git 忽略）：`outputs/verification/refine-conflict-recovery-20261102/`，位于
`/Users/hypnos/workspace/agent/Seekino`；无外部模型凭据。

| 文件 | 用途 |
| --- | --- |
| `verification-summary.json` | 唯一用例汇总、基线失败名单、本次源码 SHA-256 |
| `regression.xml`、`langfuse-network-check.xml`、`transport-final.xml` | 原始测试结果，保留失败 |
| `parent-known-failures.xml` | 上游五项失败对照 |
| `http_cancel_probe.py` | 本地 SDK/HTTPX/SSE 复现；`--tls` 开启本地证书复核 |
| `http-cancel-before.json`、`http-cancel-after.json` | HTTP 前后原值 |
| `https-cancel-probe.json` | HTTPS 六次原值 |

11-02 只读检查没有发现原 PID5207 或 API19621/MySQL13361/Kimi代理18846/ES19231 监听者。
原业务任务没有被本轮改写。旧 v4 的 99 分钟、49 次提交冲突、47/117 索引仍是历史证据；
没有把并发累计请求时间当成可直接扣减的实际完成时间。

## 验收边界与下一步

采用本实现作为真实并发试验候选，PR 保持 Draft。通知只覆盖本 API 的普通 Refine；Prefix、
其他进程和重启后的正确性仍由持久版本/租约保证。完整候选仅保留在当前执行内存，重启后
从已提交成果重建，通知不冒充持久 checkpoint。

下一步仍为 TRD §4.16：固定相同已完成 Understanding/ASR 的隔离副本，对照原提交冲突刷新
与通知/候选续接版本；最多 48 次真实请求或 60 分钟，保留失败和不完整 usage。本轮没有开始
该外部实验，不能宣布 Kimi 全量解析已恢复或达到 58 分 10 秒至 69 分 48 秒的整课目标。
