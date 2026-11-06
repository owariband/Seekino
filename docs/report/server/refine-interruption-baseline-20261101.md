# Refine 内容更新与中断候选：旧实现基线（2026-11-01）

采集：2026-11-01 21:56:22 Asia/Shanghai。关联 [性能 TRD §1.9–6.6](../../design/03-product-reassessment/video-parse-performance-trd.md)、[PR #18](https://github.com/owariband/Seekino/pull/18)。本报告只记录一次历史运行及只读复核，不是新方案对照结果。中断重发未开发，未重启或重试任务，无新增模型请求。

## 1. 实际环境与可复核入口

- 课程：Let's build GPT，6979873 ms（约116分20秒）；117 Chunk，115个三块重叠Refine区间。
- v4任务：`2a531c4f-2c73-4cc2-adaf-5156edc26236`；视频：`cd7c4da8-051c-5c6a-b95f-9fdaffefbeee`。
- 隔离API端口19621，PID5207，11-01 18:49:36启动；MySQL13361 / `seekino_video_asr_recovery_20261031_test`；ES19231，测试索引前缀`seekino_recovery_`；Kimi代理18846保持pass。
- Kimi `k3.1-pro`，Refine继承Understanding，190/190个Refine出站请求`stream=true`；Understanding10 / Refine10 / Index1；配置执行期限1800s，现代码以绑定600s取min，因此同一Refine尝试各轮共用600s；单请求fallback也受剩余期限约束。
- 当前进程已加载直接JSON Refine，未加载后来完整错误反馈和稳定排序；没有内容更新中断。源码位于`/Users/hypnos/.codex/worktrees/full-video-asr/Seekino`，HEAD `67223c16`加未提交修改。没有完整启动源码快照，HEAD不是精确运行版本；[修复前后哈希](evidence/refine-interruption-baseline-20261101/feedback-change-manifest.json)只帮助核对相关文件，不弥补这一缺口。

| 固定证据 | 内容 |
| --- | --- |
| [baseline.json](evidence/refine-interruption-baseline-20261101/baseline.json) | 查询时点、任务状态、配置、ASR哈希、阶段分布和已上报用量 |
| [stage-entries.json](evidence/refine-interruption-baseline-20261101/stage-entries.json) | 持久阶段尝试计时；含终止后残留的running项 |
| [model-requests.json](evidence/refine-interruption-baseline-20261101/model-requests.json) | Understanding/Refine逐请求ID、状态、时间、usage；embedding汇总保留在baseline |
| [refine-attempts.json](evidence/refine-interruption-baseline-20261101/refine-attempts.json) | 按interval/lease关联请求、冲突和失败；不发布全文输入或推理正文 |
| [trace-summary.json](evidence/refine-interruption-baseline-20261101/trace-summary.json) | 全窗口错误、冲突及共现分类 |
| [derived.json](evidence/refine-interruption-baseline-20261101/derived.json) | 含失败等待的区间耗时、请求轮数分布与统计方法 |
| [source-manifest.json](evidence/refine-interruption-baseline-20261101/source-manifest.json) | 本地原日志/190份Refine trace的字节数和SHA256 |

本地完整原始证据在 `/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/`，本轮采集脚本、查询快照及完整分析在其 `refine-interruption-baseline-20261101/`。采集脚本仅GET/SELECT；trace分析复用 `refine-failure-trace-20261101/analyze.py`，另存结果，不覆盖20:06历史分析。持续追加的原日志按manifest记录的字节数截取前缀核对SHA，本轮194个来源前缀均通过校验。仓库仅归档紧凑证据；模型原文、媒体、凭据不随本报告复制。

## 2. 任务进度与耗时

任务状态`PARSING_FAILED`，非手动暂停；19:12:25.373开始，20:51:27.825结束，共5942.452s（99分2.452秒）。查询时调用占用表为0行，没有仍在执行的模型请求；计时中的9条Understanding和10条Refine running是未写完终态的残留，不能解释为仍在运行。`processingDurationMs=4995872`同时标记`processingTimingComplete=false`，所以不使用它代表总时长。

| 阶段 | 完成数 | 成功尝试中位数 | 最小–最大 | 样本范围 |
| --- | --- | --- | --- | --- |
| Understanding | 81/117 | 272.885s | 160.158–575.501s | n=81完成尝试 |
| Refine | 53/115 | 247.989s | 127.514–582.478s | n=53完成尝试 |
| Index | 47/117 | 3.441s | 1.709–5.134s | n=47完成尝试 |
| Refine首次开始至最终提交 | 53个成功区间 | **820.336s** | 160.551–2050.847s | 同一区间所有尝试及之间等待；未成功者另列 |
| Refine失败尝试 | 39次 | 600.781s | 35.213–603.810s | 有完整阶段终态的失败；另10次租约丢失计时未收尾 |

共63个区间启动过，53个完成，10个没有完成；其余52个未开始。102次尝试中，44次有1个模型请求、30次有2个、26次有3个、2次有4个，总计190。失败完整计时21775.025s，成功16270.210s，失败占两者总和57.23%；这些并行累计时间不等于实际经过时间，不是CPU/GPU利用率。没有采集这一轮本机RSS、GPU利用率或供应商算力，不能从token/时延推算资源百分比。

ASR共2476 cues，正文重算SHA与存储SHA相等：`1910697d9ea39ce5c671a3be13327ebda4518dd310c61152f1f9877adcf597c4`；本任务新增转写0。它说明这轮下游失败没有让已保存音频成果丢失，不证明整课解析或人工准确率通过。

## 3. 拒绝、冲突、超时与最终停止分开计数

| 观察层次 | 数量 | 实际含义 |
| --- | --- | --- |
| Refine模型请求 | 190：102 completed、88 failed | 请求完成不代表业务提交；完整输出也可能因Schema被记录failed |
| Schema拒绝请求 | 39：34排序、5额外content字段 | 有完整输出，可修正；不是39个最终失败区间 |
| 提交冲突 | 49次，全部timeline_structure_changed | 旧快照与最新/候选的时间线锚点无法可靠对齐 |
| 超时尝试 | 36次 | 22次同时Schema与冲突，7次仅冲突，7次仅Schema；会消耗共享600s |
| 连接异常尝试 | 3次 | RemoteProtocolError，服务断开未返回响应；网络/供应商责任未定位 |
| 末批租约丢失 | 10个Refine尝试 | 日志RuntimeError: Video Parse V2 worker lease was lost；调用记录为取消 |

最终失败区间`6a57475e-8513-4400-b4d4-9e0fc74f8f2f`，起始sequence43、3成员、retry_count3，数据库错误`worker lease expired`。同一末批Understanding也报告lease lost，任务lastError为`PARSE_NODE_FAILED`。**租约为何未能维持尚未定位**；本报告不把它说成504、机器休眠或某一次冲突的直接后果。应另验环境/心跳链路，候选中断方案仍需保留现有租约终止规则。

可定位案例：区间`7bd552aa-3cf6-48ec-a7dd-a90886d522ef`的lease1两份合法结果分别在约407.759s与192.058s模型调用后触发冲突；第三次请求仅剩0.024127s预算，尝试600.081s超时。该次没有Schema错误。seq29的一处结束时间base1800000ms、current1805480ms、proposed1800260ms；现有以时间为锚点的merge无法安全对齐。lease2后来188.494s成功。另一区间`d57a2a2b-14fe-48c9-b15c-6675dd38e942`先排序修正、再冲突、最后600.106s超时。具体请求ID与冲突上下文保留在逐尝试证据。

## 4. 用量及覆盖率

| 阶段 | 全部请求 | 有provider usage | 已知input | 已知output | 其中reasoning |
| --- | --- | --- | --- | --- | --- |
| Understanding | 108 | 81（75.0%） | 19,806,788 | 606,095 | 472,798 |
| Refine | 190 | 141（74.2%） | 1,558,955 | 1,295,947 | 715,243 |

Refine已知cached input570468，是input子集；reasoning也是output子集，不能重复求和。Refine49次、Understanding27次失败/取消没有usage，真实总token和账单未知；不将缺失作0。模型请求成功耗时中位数Refine224.681s（n=102）、Understanding271.882s（n=81），它们不包含全部重试等待，不应替代上表区间有效结果时长。拒绝接收类型的后续变化也会改变request completed的意义，A/B以业务门禁和提交结果为准。

## 5. 对照适用范围与文件核对

本轮只冻结旧实现；后续A/B的两组都应包含此前未部署的完整反馈/排序修复，再比较是否增加内容更新通知、上下文续接和请求中断。固定同一初始内容和配置，记录全部取消/失败、输入版本和usage覆盖率，详细预算与护栏以主TRD §2.7/4.16为准。此历史运行不是干净的单变量控制组，也未完成最终质量验收。

远端PR18仍Draft/open，HEAD `174160a94af97e7d700a492660b382c3ef010234`，提交信息`Merge branch 'dev' into fix/video-asr-recovery`（11-01 20:32:39）；它比本地`67223c16`多17个提交。本地已有业务实现未commit/push，不得把远端合并上游的动作当作这些改动已发布。后续应先整合远端与本地，再推原分支；本轮文档评审不代表候选实现或服务部署获批。
