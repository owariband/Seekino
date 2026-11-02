# Chat 正文流式交付与 FTT 采证：2026-11-01 隔离验证

需求见 [Chat 流式 TRD](../../design/03-product-reassessment/chat-streaming-trd.md)，跟进 [PR #25](https://github.com/owariband/Seekino/pull/25)。早期实验基于含 #18 的版本；2026-11-02 已将本需求独立整理到 dev，历史测量仍按原版本解释。本报告验证实现与模拟链路；没有调用真实模型、访问课程数据库、重启共享服务或部署。

## 业务变化与证据边界

Deep Agents 的公开正文现在可在单次模型响应结束前进入持久快照。普通正文首段立即发布，后续快照每 50 ms 合并；工具参数仍收齐并验证后执行。已展示正文的调用若断流，保留部分内容并失败，不拼接另一个 fallback；正文到达前仍可按原配置回退。非流式、补发与恢复快照不伪造实时增量来源。

浏览器收到 SSE 不足以判定 FTT 有效。`answer.snapshot.textStream` 关联供应商调用；`answer.model.completed` 区分模型轮次结束与整轮持久终态。浏览器分别记录接收、可见 DOM 观察和 TT，单独浏览器记录中的 `fttMs` 保持 null。离线校验器核对原始请求确为 stream=true、原始正文 delta、调用身份、服务器完成前发送、浏览器模型完成事件前可见正文，才输出有效 FTT。空包、内部推理、工具参数不计首字。

## 验证结果

| 验证 | 结果 | 支持的结论 / 限制 |
| --- | --- | --- |
| Gateway、Deep Agents、Supervisor、Server、trace 定向回归 | 589 passed，1 skipped | 模拟 SDK、SQLite、会话与工具合同回归；跳过项需要独立 `SEEKINO_TEST_DATABASE_URL`，不代表 MySQL 真实环境通过 |
| 新增真实 SDK 摘要隔离用例 | 1 passed | 压缩摘要的 SSE 不进入公开正文；保留图片准备与压缩原有顺序 |
| 前端 API、计时、会话和页面单测 | 159 passed | 接收 / 展示计时分离、非流式不产生有效 FTT、重复和迟到事件处理 |
| TypeScript / 生产构建 | 通过 | 新事件与可选来源字段的静态兼容性 |
| Chrome 页面测试 | 1 passed | 真实页面在模拟模型完成事件前两次增长，完成后正文一致；供应商数据来自测试，不能填写真实模型 FTT |
| 真实 Kimi / Ark 课程用例 | 未执行 | FTT 基线、实际速度、内容质量与部署仍待试点 |

首次浏览器运行因 Playwright 缓存缺少 `chromium_headless_shell-1243` 未启动，后改用本机 Chrome 的独立测试配置通过。未安装额外浏览器。后端初轮有两个既有整包测试桩未声明 `stream=False`，已把它们显式标为非流式合同测试；真实流式另由 HTTP SDK SSE 用例覆盖。

### 正反例

| 用例 | 改造前 / 对照 | 本次观察 |
| --- | --- | --- |
| 暂停供应商终止包 | after_model 才发布；禁用正文观察器的 stream=true 对照仍收齐缓冲 | 供应商仍阻塞时，已能读取 PROCESSING 的持久正文；继续生成后最终文本逐字一致 |
| 显式 stream=false | 整包完成 | 无 `textStream`，FTT 为空 |
| 服务器整包缓冲 / 浏览器等模型完成后才呈现 | 不能作为端到端流式 | 校验器拒绝数值并输出具体原因 |
| reasoning、role、工具参数 | 非公开正文 | 不触发正文首字，完整工具参数才执行 |
| 网页 URL 跨多个 delta | 逐片校验会破坏链接或泄漏未核实 URL | 累积后按原规则核验；正文不重复，来源列表继续保留 |
| 输出后断流、正文前失败、取消、订阅断开 | 终态 / 重试语义不得混淆 | 部分正文保留；已输出不回退拼接；正文前失败可按独立配置 fallback；取消持久 FAILED；断开订阅不重跑 |

### 可复跑命令

本次在独立 worktree 执行，使用主工作区已经安装的 Python 3.12 环境和前端依赖；未执行依赖同步。主体后端命令：

```bash
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest \
  tests/gateway/test_deepagents.py tests/gateway/test_model_streaming.py \
  tests/gateway/test_model_diagnostics.py tests/agent/chat/test_supervisor.py \
  tests/agent/chat/test_streaming_delivery.py tests/server/test_agent_chat_service.py \
  tests/server/test_chat_streaming_trace.py -m 'not live_agent' -q --tb=line
npm --prefix web test -- src/api/chatDelivery.test.ts src/api/client.test.ts \
  src/features/search/useAgentConversation.test.ts src/features/search/SearchPage.test.tsx
npm --prefix web run typecheck
npm --prefix web run build
npm --prefix web run test:e2e -- tests/chat-streaming.spec.ts --reporter=line
```

最后一条默认使用项目 Playwright Chromium；本次以临时配置将同一项目的浏览器 channel 设置为 `chrome`，其他配置不变。该本机覆盖配置不进入产品提交。

原始材料：[验证清单](evidence/chat-streaming-20261101/validation.json)、[后端 JUnit](evidence/chat-streaming-20261101/backend.xml)、[模拟浏览器采证](evidence/chat-streaming-20261101/browser-simulated.json)、[模型完成前截图](evidence/chat-streaming-20261101/before-model-completion.png)。浏览器样本仅作链路和展示证据，任何其中的毫秒数都不是实际模型性能。

## 真实试点采证方法与下一步

在明确版本的隔离实例设置 `SEEKINO_CHAT_TRACE_DIR`，开始测试前在浏览器收集事件：

```javascript
window.chatDeliveryEvidence = [];
window.addEventListener('seekino:chat-delivery', event => {
  window.chatDeliveryEvidence.push(event.detail);
});
```

保存同一请求的事件数组为 `browser.json`，以其中 `source.callId` 找到服务端私有 trace 目录，执行：

```bash
uv run --no-sync python scripts/verify_chat_streaming_trace.py \
  browser.json /path/to/chat-trace/CALL_ID/events.jsonl
```

浏览器原始证据只含身份、计时、状态和可见字数；服务端 opt-in trace 包含模型输入、输出和关联上下文，应按既有本地私有诊断合同保存。只有校验器返回 `valid=true` 才能引用 `ftt_ms`；`continuous_growth_verified=false` 的短回复不能单独验收长回答持续交付。失败 / 取消的候选接收时间仍保留，但缺少完整证据时不得改填成功指标。

先选一条足够长的课程解释，核验真实增量与页面同时增长，再按 PRD 的 Q02/Q03 固定历史、播放位置、模型、缓存和负载，每组至少三个真实样本建立基线。原始错误 FTT 已撤回，不能用 9.173 / 29.576 / 27.231 秒计算收益；TT 与内容质量分开比较。FTT 性能目标在探索基线之后、正式对照之前确定。C23 保持 TODO，责任由本 PR 持续承接。

<a id="boundary-review-20261102"></a>
## 2026-11-02：模型事件与 Server 交付职责调整

`ModelTextDelta` / `observe_model_text` 移至 Gateway 的通用事件模块，Supervisor 不再引用 Chat Completions 协议或控制发布频率。Server 的 `ChatSnapshotCoalescer` 负责写库前合并，业务边界、失败和取消刷新暂存前缀；模型完成后的刷新不携带实时来源。Chat 诊断目录由 Server 通过 `model_diagnostic_scope` 注入，Parse 原有诊断合同保留。

| 验证 | 实际结果 |
| --- | --- |
| 无 Chat / Server 的 Gateway 并发订阅 | 两次 SDK 请求分别收到自身增量，完整结果保持一致 |
| 快速连续正文 | Agent 无需等待 50 ms；Server 首段立即保存，窗口内仅保留最新快照，下一次满足间隔的增量正常交付 |
| 模型完成、断流、取消 | 暂存正文保存；完成后刷新无实时标记，失败终态保留前缀 |
| 工具循环 | 工具开始前前缀已保存，工具完成后的下一次模型首段立即交付 |
| 定向后端回归 | **596 passed / 1 skipped，22.13 秒**；跳过 MySQL 专项，缺少 `SEEKINO_TEST_DATABASE_URL` |

本轮命令在前述后端回归范围增加 `tests/gateway/test_model_events.py`，其余七个文件相同，使用 `-m 'not live_agent' -q --tb=line`。源码指纹与命令见[验证清单](evidence/chat-streaming-20261101/boundary-validation.json)，原始结果见[JUnit](evidence/chat-streaming-20261101/boundary-backend.xml)。11-01 的前端、浏览器记录仍是该日期的证据；本轮没有修改前端或重跑浏览器，没有真实模型调用或部署，FTT / TT 仍未测。

## 独立 dev 基线复核（2026-11-02）

基于 `dev` 的 `56e79777` 只重放 Chat 需求提交，不再携带 PR #18 的 ASR、解析或数据库迁移。PR #25 直接以 dev 为目标。相关后端 747 passed / 1 skipped（27.03 秒），前端四个相关测试文件 159 passed，类型检查与构建通过；跳过项为未配置 `SEEKINO_TEST_DATABASE_URL` 的 MySQL 专项。新增重放不改变旧实模记录的运行版本，本轮没有实模调用。

后端命令为上述主体命令补充 `tests/gateway/test_model_events.py`、`tests/gateway/test_agent_model.py` 与 `tests/gateway/test_kimi_streaming.py`；前端命令不变。本地 JUnit：`/private/tmp/seekino-pr25-dev-backend.xml`。
