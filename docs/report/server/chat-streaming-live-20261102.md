# Chat stream=true 真实运行：2026-11-02

**正文已经能在模型完成前显示，但当前 Kimi 链路仍有较长的首次等待和成批输出。** 本轮从 [PR #25](https://github.com/owariband/Seekino/pull/25) 的 worktree 启动真实 API/Web，以 Chrome 提交两条问题，没有模拟模型或 HTTP。需求见 [TRD](../../design/03-product-reassessment/chat-streaming-trd.md)。

## 运行条件

运行时代码 `10da2dca28d668a7d8022f83d15cd8d3587b81be`，分支 `fix/chat-streaming-ftt`；Deep Agents / Kimi `k3.1-pro`。五次实际供应商请求均为 `stream=true`。配置复制自本机既有模型配置，SHA256 为 `74cc763706fad8909d4a5285c8f91b3e75db24f26848963963ef980365858a89`。

API `http://127.0.0.1:19625`，Web `http://127.0.0.1:15175/search`。复用已有测试 MySQL/ES 进程，单独新建 `seekino_chat_streaming_20261102_test` 数据库及同名前缀的 Chunk/Event `_test` 索引；媒体目录独立且为空。没有连接开发/生产业务库，也没有重启原服务。API/Web 在交付时继续运行。

Chrome 真实页面使用 1440×1000 视口，两条请求在同一新会话顺序发送。测试窗口约 15:32–15:36（Asia/Shanghai），不是此前课程 Q02/Q03 的同条件复测；同一台机器还运行上游测试服务，未控制外部负载。每题仅一个样本，不计算分位数或前后提速比例。

## 实际效果

| 用例 | 首个模型正文到客户端 | 页面首次显示 | 整轮完成 TT | 观察 |
| --- | ---: | ---: | ---: | --- |
| 矩阵乘法长解释，要求八段并给例题 | 27.211 s | 27.227 s | 61.594 s | 最初是 Markdown 前缀 `**`；第一段可读文字在 32.811 s 记录到。模型完成前有 15 次可见字数增长，最终正文 2030 字符 |
| 先说明计划，再搜索 Python 官方文档并解释 | 13.234 s | 13.248 s | 145.758 s | 首段是“我会……”进度说明；四次模型调用、三次搜索之后才完成实质回答 |

现有核验器对两条样本均返回 `valid=true`：请求、原始正文 delta、服务端发送与浏览器完成前展示可以关联。但它不区分 Markdown 格式前缀和可读文字，因此长回答的 27.211 s 只能按**首个正文 token（含格式符）**解释，不能说用户此时已经看到讲解。工具样本也不能用进度提示时间代替实质答案时间。

长回答的持续增长核验为 true；工具样本首轮只有一次完成前展示，`continuous_growth_verified=false`。该样本证明 session 在提示、工具、后续推理之间继续运行，不能单独证明平滑逐字输出。

![长回答在模型尚未结束时已经显示到第三段](evidence/chat-streaming-20261102/long-answer-growing.png)

## 仍然存在的等待

长回答在 Gateway 接收到的原始正文 delta 中有七段超过一秒的空档，分别约为 5.548、5.312、3.878、4.168、4.897、6.259、3.528 秒。页面相应成批增长。这些空档已存在于 SDK 解码后的接收记录，不能归因于 Server 的 50 ms 合并；本轮没有进一步区分供应商、网络代理、压缩或 SDK 缓冲。

工具场景第一次搜索耗时 12.037 s，成功返回空列表；第二次 2.133 s，返回五个结果；第三次 7.091 s，返回 `WEB_SEARCH_FAILED`。Agent 保留过程说明并继续完成回答。最后一次模型调用耗时 73.014 s，正文直到该调用的 72.793 s 才开始到达，随后约 0.220 s 内收完；stream=true 在这个短输出上仍表现为接近整段出现。

配置 `reasoning_effort=none` 不代表本次没有推理生成：长回答用量中仍记录 638 reasoning tokens。记录不足以把首次等待全部归因于推理，也不能证明改流式会缩短模型计算或工具耗时。

## 内容、持久化与成本

长回答最终持久正文与供应商完整正文逐字一致，矩阵例题抽查计算正确；两轮刷新后均能恢复。浏览器 pageerror 为 0。页面仍把 LaTeX 数学表示作为普通文字显示，这属于既有展示限制，本轮没有修改数学渲染。

五次模型调用共记录 input 25,343、output 4,328、total 29,671 tokens；reasoning 是 output 的子集，不另加总。三次真实搜索中的空结果和错误保留在证据中。没有进行价格换算或成本改善比较。

## 复核入口与结论边界

脱敏 [summary.json](evidence/chat-streaming-20261102/summary.json) 包含运行来源、每次调用、用量、工具结果、浏览器增长时间及原始 trace 哈希；[长回答浏览器事件](evidence/chat-streaming-20261102/long-answer-browser.json)和[工具场景浏览器事件](evidence/chat-streaming-20261102/tool-loop-browser.json)保留原始计时。

完整输入、配置、供应商事件、消息、截图和启动/浏览器脚本只保留在本 worktree 的 `outputs/verification/chat-streaming-live-20261102/`，不提交密钥或完整模型上下文。复跑核验：

```bash
python scripts/verify_chat_streaming_trace.py \
  outputs/verification/chat-streaming-live-20261102/long-answer/browser.json \
  outputs/verification/chat-streaming-live-20261102/model-trace/7fe67092-96f4-4d64-99c5-abe3b70f74e5/events.jsonl
```

本轮确认真实长回答已去除上层整包交付等待，MySQL 持久快照和真实页面都已接入。普通 Chat 首轮试跑通过；课程观看页、Q02/Q03 同条件重复、原片依据质量与性能目标仍未验收，C23 保持 TODO，PR 保持 Draft。下一步先明确格式前缀与可读正文的计量展示，再核对接收端成批到达的原因，继续课程样本验证。
