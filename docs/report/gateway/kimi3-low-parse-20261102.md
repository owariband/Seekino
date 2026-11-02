# Kimi 3 low 整课复验（2026-11-02）

[PR #18](https://github.com/owariband/Seekino/pull/18) · [评审看板](pr18-kanban.html) · [主 TRD §4.24](../../design/03-product-reassessment/video-parse-performance-trd.md#424-11-02显式-kimi-3-low-的整课复验计划)

**最新结论（11-02 23:03 CST）：已按正式 reparse 启动直连 v9，随后在 14 分 38 秒后失败。** 19/117 Understanding、8/115 Refine、3/117 索引，ASR/分组复用且新转写为 0。终止事件是 sequence 27 返回 0–1 ms 的 OTHER，超出其 27:00–28:00 授权范围；同时首次生成空 timeline 在直连中复现。当前已停止，完整课程仍未通过。详见[直连全课与空结果调查](#v9-empty-results)。

**较早结论（11-02 22:14 CST）：采用 Kimi 直连，不保留本轮专用代理规则。** 同一份 189 图、14.55 MB 请求显式禁用环境代理后，第 2.577 秒完成客户端发送、54.477 秒收到首 SSE，HTTP 200、返回 k3；不经过 7897。专用规则已撤回，API19621 已恢复 Kimi 直连。只测到首事件，没有再次启动整课；完整输出质量与十路稳定性仍未验收。详见[直连复测与最终取舍](#direct-retest)。

**较早结论（11-02 21:56 CST）：K3 图片输入可用，已修正代理绕过规则并实际恢复同一个 v8；整课仍未通过。** 代理重试运行 453.009 秒，提交 9/117 Understanding、完成 2/115 Refine、索引 0/117，最终仍因首 SSE 超时停止；新 ASR=0。抽查发现空 timeline 和单字人物名已提交，结构完成数不能当作质量通过数。详见[本轮代理与内容复核](#proxy-retry)。

**较早结论（11-02 20:46 CST）：显式 K3 low 接线完成，真实整课未跑通。** 新 v8 通过正式 reparse 启动，20:27:44–20:35:32（Asia/Shanghai）运行 **467.434 秒，7 分 47 秒** 后失败；30 个 Understanding 请求没有任何 SSE，0/117 Understanding、0/115 Refine、0/117 索引。没有自动再开整课。

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


<a id="proxy-retry"></a>
## 图片输入、实际代理出口与原 v8 恢复（11-02 21:56 CST）

### 问题与传输证据

K3 的官方[模型说明](https://www.kimi.com/code/docs/en/kimi-code/models.html)列有图片/视频输入；小文本成功不足以证明课程图片请求可用。真实单图请求获得了与课程画面一致的浏览器/文字描述；完整图片请求的直连对照停在上传。因此本轮分别检查图片能力、客户端传输和 Clash 实际路由，不以“本机已开代理”推断模型流量走了代理。

两层配置让 `api.kimi.com` 直连：隔离 API 的 `NO_PROXY` 显式包含该域名；绕过这一环境变量、显式连接本地代理后，Clash 的 GeoIP CN 规则仍选 `DIRECT`。本地控制器的 `/connections` 给出了实际规则和出口，不能仅凭 `HTTP_PROXY` 存在断言已使用代理。

| 有界对照 | 请求体 / 输入 | 客户端发送观测 | 首 SSE / 完整返回 |
| --- | --- | --- | --- |
| K3 low 单张真实课程图，直连 | 72,581 字节 | 第 0.273 秒完成发送 | 2.756 秒 / 21.490 秒；合法 JSON，描述与画面相符 |
| 原 Chunk 0，curl 直连 | 14,548,052 字节 / 189 图 | 100 秒仅上传 2,489,980 字节，24,898 B/s | 无 HTTP 响应，无 SSE |
| 原 Chunk 0，显式本地代理但仍命中 DIRECT | 同模型、文本、图片和 Schema | 100 秒仍未完成发送 | 无 SSE |
| 10 张均匀取样原图，直连 | 822,945 字节 | 第 8.227 秒完成发送 | 18.040 秒；收到首事件即关闭 |
| 原 Chunk 0，`api.kimi.ai` 经既有代理 | 14,548,052 字节 | 第 1.678 秒完成发送 | 100 秒无头 / 无 SSE；未采用该端点 |
| 原 Chunk 0，`api.kimi.com` 显式 AI 分流 | 14,548,052 字节 / 189 图 | 第 1.167–1.958 秒发送，约 0.791 秒 | **42.445 秒**；HTTP 200、返回模型 k3，首事件即关闭 |

大请求对照只将输出上限设为 512，并在首 SSE 关闭，以控制诊断费用；正式任务仍为 30,000。curl 与 SDK 的序列化字段顺序不同，解码后的 JSON 逐项相同；两个 SDK 大请求的原始 SHA 相同。10 图仅为诊断，正式解析没有减少推图。`send_request_body.complete` 表示客户端发送完成，不单独证明上游已完整接收；HTTP 200/SSE 是后续独立证据。以上各一次、不同时间/工具，不估计长期带宽、p95 或稳定提速倍数；缺失 usage 不计零。

### 采用的配置与部署范围

- 保持官方 `https://api.kimi.com/coding/v1`、k3 / low / stream=true。仅调整 #18 隔离 API 启动环境：`HTTP_PROXY` / `HTTPS_PROXY` 为 `http://127.0.0.1:7897`，`NO_PROXY` / `no_proxy` 只保留 `localhost,127.0.0.1,::1`。
- 在 Clash 当前订阅的自定义 prepend 规则增加 `DOMAIN,api.kimi.com,🤖 AI服务`，位于 GeoIP 之前，使用已有 AI 服务组。运行时连接证明选中了该组和新加坡出口；保留规则模式与原有自定义规则。当前路由已复核，自定义规则已落盘；没有为验证持久性额外重启 Clash。
- 核对本 worktree 服务及租约后，仅重启隔离 API19621，PID 29305，装载 HEAD `88283d79`（业务改动为 `2aaf9fa9`）。数据库、ES、本地音频服务和主工作区配置不变。启动与采集脚本位于本机 `outputs/verification/lets-build-gpt-k3-low-v8-proxy-20261102/`。
- 使用正式 retry 接口恢复同一个 v8，未新建版本；模型配置文件摘要不变，保留 10/10/1 并发、原 Prompt、全量图片、100 秒首 SSE、既有总预算、重试和租约。ASR/分组校验复用，新 ASR 为 0。

### 正式重试的结果与边界

本次从 retry 请求到失败为 **21:23:26.535–21:30:59.544，453.009 秒（7 分 33 秒）**。父任务的 `startTime` 仍为较早首次启动时刻，不能把中间调查时间算进本次解析。真实出站共 41 次，全部 k3 / low / stream=true。

| 本次重试 | Understanding | Refine |
| --- | --- | --- |
| 已提交阶段 | 9/117 | 2/115 |
| 模型请求 | 29 | 12 |
| 收到 SSE / 完整返回 | 12 / 12 | 12 / 5 |
| 未完整返回原因 | 7 次首 SSE 期限、1 次响应前断连、9 次父任务失败取消 | 4 次成员更新取消、3 次父任务失败取消 |
| 有首 SSE 样本中位 / 最大 | 48.369 / 73.655 秒，n=12 | 6.823 / 17.955 秒，n=12 |
| 已完成阶段耗时中位 | 182.878 秒，n=9 | 54.621 秒，n=2 |
| 已返回 usage 的 input / output / reasoning | 2,209,356 / 15,086 / 4,526 | 44,724 / 8,934 / 3,672 |

阶段耗时包含该次阶段内修正/重试；仅对已完成样本统计，不能相加当整课耗时。24 次请求没有 usage，费用未知；探针调用另算。Refine 有 4 次更新通知和快照刷新，旧请求退出 0.016–0.018 秒；这证明局部取消链路响应，不证明整课冲突浪费已解决。

最终 sequence 2 在两次首 SSE 期限与一次响应前断连后耗尽有限重试。任务错误为 `PARSE_NODE_FAILED`：`视频理解失败：模型请求超时。本次解析已停止派发，请检查后手动重试。` 没有 429 或 504。代理修复使原来完全无响应的输入取得真实结果，但没有消除这次十路运行中的全部启动失败；尚无传输分段证据将剩余超时细分为排队、预填充或链路等待。

### 内容抽查与继续运行的门槛

已提交不等于内容合格。sequence 0 只返回一条 0–1000 ms 的 SCENE，`persons=["И"]`、描述为 `"А"`，没有对白。sequence 1 首次返回 7,494 字符、人物为 Andrej Karpathy；门禁拒绝原因是 `timeline must be ordered by start_seconds`。修正请求**已经包含完整的原 JSON、排序错误提示及“保留其他内容”指令**，但模型返回 `{"persons":["Y"],"timeline":[]}`，仍通过现有门禁。它不能归因为“这次没有带回原 JSON”。当前 9 个持久成果中 sequence 1、7、8 的 timeline 为空，另有单字人物名；部分其他 Chunk 有正常课程对白，不能将所有结果一概判为正常或异常。

这些短输出的正式请求上限仍为 30,000、finish reason 为 stop，并非诊断探针的 512 上限截断。已保存原输出、修正输入与持久版本的对应证据；**尚未隔离出极短错误输出的具体原因，不把它断言为代理改写、模型不支持图片或 low 档位必然退化。** 现有内容覆盖门禁有缺口；先对这一固定失败样本复核模型响应与门禁，再决定如何恢复整课，不用更快的无内容结果证明提速。

尝试停止扩大试跑时任务已经自然失败，未执行暂停操作；当前 `PARSING_FAILED`、可 retry，没有在途采集进程，也没有再次 retry/reparse。原 9 个成果保留为失败证据，未覆盖原始 ASR、未发布成完整索引。B07/B20 和 Draft PR #18 均保持未完成；全课耗时目标、人工质量与检索回看追问仍未验收。

### 本轮证据

[探针及字段身份核对](evidence/kimi3-low-20261102/proxy/probe-results.json)、[实际路由与落盘范围](evidence/kimi3-low-20261102/proxy/routing.json)、[重试终态与统计口径](evidence/kimi3-low-20261102/proxy/run-summary.json)、[逐请求时间与错误](evidence/kimi3-low-20261102/proxy/request-telemetry.json)、[持久正文抽查与修正前后样本](evidence/kimi3-low-20261102/proxy/content-sanity.json)。完整媒体、原始请求、Clash 含凭据备份只保留本机忽略目录，不提交 Git；证据不包含供应商思考正文。


<a id="direct-retest"></a>
## 直接访问复测与最终取舍（11-02 22:14 CST）

本次按明确的直连选择复测，测试客户端设置 `trust_env=False, proxy=None`，仍请求官方 `https://api.kimi.com/coding/v1`。原 Chunk 0 的 189 图、文本、Schema、k3 low、512 诊断输出上限和 100 秒首 SSE 期限不变；请求体 14,548,052 字节，原始 SHA 与此前代理 SDK 探针相同。仅执行一次，到首 SSE 关闭；没有用小文本替代原始视觉输入，也没有再次重开整课。

| 观测点 | 本次直连 | 前次 AI 代理探针 |
| --- | --- | --- |
| TCP 目标 | api.kimi.com:443 | 127.0.0.1:7897 |
| 客户端开始发送正文 | 1.832 秒 | 1.167 秒 |
| 客户端完成发送正文 | **2.577 秒** | 1.958 秒 |
| 正文发送阶段用时 | **0.745 秒** | 0.791 秒 |
| 首 SSE | **54.477 秒** | 42.445 秒 |
| HTTP / 返回模型 | 200 / k3 | 200 / k3 |

这次直连能传完同一份大输入并收到模型事件，说明早先 100 秒只上传 2.49 MB 的现象**不能推导为直连天然不可用，也不足以证明必须增加代理**。两路各一次且时段不同，12 秒首 SSE 差值不构成稳定性能优劣结论；两次都未等待完整 JSON，不能验证语义质量。

最终采用直接访问：撤回本轮增加的 Kimi 精确分流规则，运行时和当前订阅自定义规则均核对移除，其他规则与基础配置保留；没有切换全局模式或更换节点。隔离 API 启动环境恢复 Kimi 的 NO_PROXY 排除，核对零活动租约后仅重启该 API（PID 31574）。既有 `/healthz` 正常，HTTPX 对 Kimi URL 选择直接 `ConnectionPool`，不经过本地代理端口；模型配置、业务代码和失败任务未改。

之前的代理试跑记录仍作为历史证据保留，当前任务继续 PARSING_FAILED，9 个 Understanding / 2 个 Refine / 0 索引。空 timeline 与修正后内容丢失没有因连通性通过而解决，后续验收应围绕固定失败样本。此次只验证当前直连条件和撤回临时分流，不追加有偿测试。[直连、撤回与 API 恢复证据](evidence/kimi3-low-20261102/proxy/direct-retest.json)。


<a id="v9-empty-results"></a>
## 直连 v9 完整课程与空结果调查（11-02）

### 正式启动与终态

按重新解析指令，经 `/api/uploads/reparse` 从 v8 新建 **v9 `d7c13ca5-14f5-4201-8855-a2b94f07e68c`**。旧失败成果保留，Chunk 结果重新生成；冻结 ParseConfig 与 v8 完全相等，k3 / low / stream=true、10/10/1 并发、100 秒首 SSE、完整图片与 Prompt 均保留。隔离 API PID 32447，装载 `55228d6d`，实际出站 TCP 连接 Kimi 的 443，无 7897 连接；ASR 与独立分组 JSON/摘要逐项相等，新 ASR=0。

22:25:58.445–22:40:36.186（Asia/Shanghai），任务开始至终态为 877.741 秒；从 reparse 请求发出计为 **877.879 秒，14 分 38 秒**。最终 19/117 Understanding、8/115 Refine、3/117 Index。成功阶段中位为 Understanding 173.372 秒（n=19）、Refine 87.419 秒（n=8），这些样本包含不合格正文，不能据此宣称提速或质量达标。

真实 Kimi 请求 76 次：Understanding 49 次（26 完整返回、15 传输错误、8 取消）；Refine 27 次（13 完整返回、7 成员更新取消、7 父任务失败取消）。Refine 有 7 次定向更新，旧请求退出 0.015–0.028 秒。请求时长不可相加当课程耗时；诊断对照另计，缺少 usage 的调用不视为免费。

本次终止错误明确为 `ValueError: timeline event falls outside the authorized media time range`。sequence 27 的范围是 `[1620000,1680000)` ms（27:00–28:00），模型却返回一条 `[0,1)` ms 的 OTHER，正常 finish=stop。宿主正确拒绝该事件，随后父任务停止并撤销兄弟租约；后续 lease lost 是这次终止的连带结果。**本次致命事件不是合理 ASR 跨界 330 ms，也不是 504；它是模型返回了与当前视频位置不符的极短内容。** 当前任务自然失败，未再次 reparse。

### 空 timeline 到底是什么

`timeline` 保存 Chunk 的对白、画面、动作等事件。非空课程输入却得到 `timeline: []`，意味着本次理解没有产出任何事件；它与 ASR 原文丢失是不同问题，底层 ASR 仍完整。

| 固定样本 | 实际输入与输出 | 已证实的事实 |
| --- | --- | --- |
| v8 sequence 1 初次 | 21 条非空 ASR；22 事件，其中 21 条对白，7,494 字符 | 因 start_seconds 排序被拒绝 |
| v8 sequence 1 修正 | 原 Prompt + 全部 ASR + 完整被拒 JSON + 排序提示 | 模型返回 `{"persons":["Y"],"timeline":[]}`，31 字符，随后被接受 |
| v8 sequence 6/7 初次 | 24/21 条非空 ASR、198 图 | 未进入格式修正就已返回空 timeline |
| v9 sequence 0 初次，直连 | 22 条非空 ASR、189 图 | 返回 `{"persons":["-"],"timeline":[]}`；再次复现 |
| v9 sequence 27 初次 | 27:00–28:00 的课程输入 | 返回 0–1 ms 的单个 OTHER，导致正式任务失败 |

逐事件拼接供应商原始 SSE 的 `delta.content`，与最终 response content 一致；所查短输出 finish_reason=stop，正式 max_tokens=30000。不是 SDK 拼接漏字、应用层主动清空、音频输入为空或诊断探针 512 输出上限造成的截断。直连同样出现，不能将该语义错误仅归因于本地代理。

### 为什么门禁没有拦住空内容

1. `VideoParseResult.timeline` 定义为普通 list，没有最小长度；黑场、空输入等情况需要合法空结果。Schema 验证只遍历现有事件，空数组自然通过。现行时间范围校验也只遍历事件，无法拒绝空数组。
2. Understanding 未把“输入有明确 ASR、输出却没有对白/事件”纳入业务结果检查；文本中有“只有无事件时才可为空”的要求，但代码没有对应约束。
3. 宿主稳定排序目前仅在 Refine 接入，Understanding 仍把纯排序问题交回模型。反馈**确实包含完整原 JSON 与错误提示**；模型没有遵守“只改非法字段、保留其他内容”，而门禁也未核对修正前后的内容丢失。
4. Understanding 的有限修正循环仅捕获结构 `ValidationError`；时间边界检查在循环之后，触发 `ValueError` 会直接终止阶段。sequence 27 因此没有进入带原 JSON 的业务错误修正。

离线重放确认空数组同时通过 Schema 与时间门禁。对 v8 sequence 1 稳定排序可在保留全部 22 事件的情况下通过结构校验，但仍有从 59,670 ms 起的 ASR 对白超出 60,000 ms 的授权边界，后续范围门禁仍会拒绝。说明排序遗漏、内容覆盖和既有跨 Chunk 时间合同需要分别处理，单加排序不能宣称整课已修好。

### 严格 JSON 模式对照与边界

为了检查格式约束是否直接造成空输出，仅使用 v8 sequence 1 的同一份文本修正输入，不发图片、不提交成果；k3 low、temperature=1、30,000 输出上限、直连和输入内容保持，切换 `json_schema.strict`。

| 有效对照 | strict=true | strict=false |
| --- | --- | --- |
| 完整返回用时 | 23.027 秒 | 36.397 秒 |
| 首 SSE | 1.510 秒 | 4.831 秒 |
| 输入 / 输出 / reasoning tokens | 6,843 / 2,039 / 22 | 6,843 / 2,060 / 43 |
| 事件数 / 人物 | 22 / Andrej Karpathy | 22 / Andrej Karpathy |
| 结构 / 后续时间门禁 | 结构通过；仍含跨界对白 | 结构通过；仍含跨界对白 |

最初两次诊断因脚本误用 `mark_first_event()`、实际接口为 `received()`，在首 SSE 后 1.670/1.015 秒中止；它们是诊断脚本失败，未计入有效对照，usage 未知。修正脚本后才得到上表，因此本次诊断总调用 4 次、有效比较 2 次；没有掩盖或把前两次计为模型失败。

两组均能保留 22 事件，**不支持“关闭 strict 即可修复”的结论**。本轮没有调整正式格式、模型、图片或业务代码。可确认的机制是“模型有时返回不足的内容，而现有业务门禁接受了空结果”；为什么相同模型有时产生这种极短输出，尚未隔离出确定诱因，不能直接归为 low 档位、网络、严格解码或某一条 Prompt。

下一项收敛到 Understanding 的业务结果门禁：复用宿主稳定排序，对有明确音频证据却整份空掉/无对白的结果提供具体反馈，并让可修正的时间错误进入原有有界修正，同时保留源视频时间、租约与事务合同。具体空内容/噪声边界仍需设计与验证；不全局禁止所有合法空列表，不擅自补造模型未确认的事件。完整课程与质量继续由 B07/B20 跟踪。

证据：[v9 身份与终态](evidence/kimi3-low-20261102/v9/summary.json)、[音频复用](evidence/kimi3-low-20261102/v9/audio-reuse.json)、[原始响应与输入抽查](evidence/kimi3-low-20261102/v9/output-audit.json)、[门禁离线重放](evidence/kimi3-low-20261102/v9/gate-replay.json)、[格式模式对照及脚本错误](evidence/kimi3-low-20261102/v9/schema-mode-comparison.json)、[致命时间边界](evidence/kimi3-low-20261102/v9/terminal-time-boundary.json)。完整 trace 和候选 JSON 保留在本机 `outputs/verification/lets-build-gpt-k3-low-v9-direct-20261102/`，不提交媒体、凭据或思考正文。
