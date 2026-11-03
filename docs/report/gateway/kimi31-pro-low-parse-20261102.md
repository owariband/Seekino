# Kimi 3.1 Pro low 整课对照（2026-11-02）

[PR #18](https://github.com/owariband/Seekino/pull/18) · [评审看板](pr18-kanban.html) · [性能 TRD §4.28](../../design/03-product-reassessment/video-parse-performance-trd.md#428-11-02kimi-31-pro-low-模型对照与额度终止) · [K3 low 基线](kimi3-low-parse-20261102.md#v9-empty-results)

**结论：已新建 v10 并真实运行完整课程任务，K3.1 Pro low 的前 20 份 Understanding 响应未复现空 timeline／单字人物名；整课因五小时额度耗尽停止，未完成验收。** 23:22:55–23:29:45（Asia/Shanghai）实际运行 410.168 秒，18/117 Understanding、5/115 Refine、3/117 索引，新 ASR=0。当前没有继续解析或自动重试。

## 实验身份与控制条件

| 项目 | 本轮事实 |
| --- | --- |
| 课程 | Let's build GPT，116 分 20 秒，117 Chunk / 115 Refine 区间 |
| 新任务 | `af389a51-8050-4d86-a33f-d3b44dc22a04`，v10；正式 `/api/uploads/reparse`，expectedJobVersion=9 |
| 旧任务 | `d7c13ca5-14f5-4201-8855-a2b94f07e68c`，v9 保留原失败与成果 |
| 源码 / API | `e2d4acaf`，worktree `full-video-asr/Seekino`，隔离 API19621，PID35460 |
| 模型与档位 | **k3.1-pro / reasoning_effort=low / stream=true**；54 次出站全部核对，26 个完整响应的 model 均为 k3.1-pro |
| 路由 | Kimi 直连，NO_PROXY 包含 api.kimi.com；实际 10 个 Kimi TCP 连接直达远端443，未连接本地7897 |
| 保持条件 | Understanding/Refine/Index 并发10/10/1、3fps/720px、全量189–198张图、原 Prompt/Schema/业务门禁、输出上限30000、100秒首SSE期限与有限重试 |
| 音频 | 从 v9 校验复用 ASR 与独立分组；两份 JSON 及摘要均相同，新增转写0，无新分组推理 |

冻结 ParseConfig 仅修改 Understanding 及其 fallback 的模型名；Refine 继承该绑定。隔离 YAML 同步 vision、vision_fallback、summarize 为新模型／low，其他角色未改；没有改主工作区默认配置或业务代码。重启前核对旧 API 的 worktree、PID 与三类活跃租约为0，其他服务未重启。来源：[冻结配置](evidence/kimi31-pro-low-20261102/frozen-run.json)、[部署](evidence/kimi31-pro-low-20261102/deployment.json)、[音频复用](evidence/kimi31-pro-low-20261102/audio-reuse.json)。

23 个带图请求覆盖20个不同 Chunk，均与 v9 对应输入核对。原消息不逐字相同：`all_speakers.scope_id` 和 ASR 的 `speaker_scope_id` 必然引用新任务。仅将旧／新任务 ID 替换为同一占位、排除模型名并按媒体哈希比较后，请求全部相等，图片哈希与 Schema 全部一致。两轮执行时间不同，不能排除供应商负载或服务状态变化。来源：[输入与响应核对](evidence/kimi31-pro-low-20261102/model-comparison.json)。

## 原始内容对照与校验

以下取两轮相同 sequence 0–19 各自首个完整的带图 Understanding 响应，未用文字修正替代首轮结果；只判断明确的空内容／异常人物名，不等于完整人工质量验收。

| 指标 | v9 K3 low | v10 K3.1 Pro low |
| --- | --- | --- |
| 对应 Chunk 样本 | 20 | 20 |
| 空 timeline | 2（sequence 0、11） | 0 |
| 单字／占位 persons | 4（`-`、`S`、`安`、`和`） | 0；20份均为 Andrej Karpathy |
| sequence 0 | 0事件、0对白、41字符 | 16事件、7对白、7617字符 |
| sequence 1 | 1事件、0对白、319字符 | 14事件、11对白、7346字符 |
| sequence 7 | 1事件、1对白、293字符 | 9事件、5对白、3606字符 |
| sequence 11 | 0事件、0对白、37字符 | 11事件、7对白、4270字符 |

v10 每份含6–17事件、4–13对白，全部 finish_reason=stop，原始 SSE 内容拼接与最终响应相等。18份通过现有结构／时间门禁并落库；另外两份 sequence12、15 分别有10／17事件，但 `timeline must be ordered by start_seconds`。离线仅稳定排序后两份都通过现有结构及时间校验；**没有修改线上行为或补写数据库**。

Refine 完整返回6次，其中5次通过并提交；1次因 `members.0.content: Extra inputs are not permitted` 被拒绝，多带的是空 `content: {}`，不是空 timeline。三份格式错误的修正请求全部带原 JSON 与具体错误提示，其中两份 Understanding 不重复上传图片；修正调用遇到额度429，未得到新响应。此次不能宣称“没有格式问题”，也不能归因为“未把原JSON发回”。[逐请求离线核对](evidence/kimi31-pro-low-20261102/terminal-audit.json)。

## 停止原因、耗时与请求数

首次额度拒绝发生在 **23:27:58 CST**，供应商明确返回：

```text
HTTP 429
error.type = exceeded_current_quota_error
You've reached your 5-hour usage limit.
```

这不是100秒首SSE超时，也不是普通瞬时并发限流。后续仍按当前通用服务异常策略退避重试，首个 terminal 事件是 Refine 的 `child_retry_budget_exhausted`，之后 Understanding 也收到同类429，父任务撤销租约后出现的 lease lost 属于终止清理。最终 API 状态为 PARSING_FAILED，泛化 lastError 没有呈现具体额度原因；额度错误分类仍在 B07/T03 待处理。

| 口径 | 本轮 |
| --- | --- |
| 新建请求至任务结束实际用时 | 410.168秒（6分50秒）；不是全课完成用时 |
| 持久状态 | Understanding18/117、Refine5/115、Index3/117 |
| 成功阶段耗时中位数 | Understanding105.488秒（n=18）；Refine125.662秒（n=5，含阶段内刷新） |
| 完整模型请求耗时中位数 | Understanding106.927秒（n=20）；Refine66.761秒（n=6，含1次结构无效响应） |
| 已收到SSE的首事件中位／最大 | Understanding43.219／67.059秒；Refine3.509／4.938秒 |
| Kimi请求总数 | 54：26完整返回、20额度429、8被新快照替代取消；无其他传输错误 |
| Understanding请求 | 25：20完整返回、5额度拒绝 |
| Refine请求 | 29：6完整返回、15额度拒绝、8更新取消 |
| 更新取消退出 | 8次，中位29ms，范围18–111ms；未记录提交冲突 |

上述成功阶段、完整模型响应与整个任务实际用时是不同口径，不能互相替代；并发请求时长不相加当课程用时。来源：[终态](evidence/kimi31-pro-low-20261102/terminal.json)、[请求汇总](evidence/kimi31-pro-low-20261102/request-summary.json)。

## 用量与结论边界

| 具备usage的完整请求 | input tokens | output tokens（含reasoning） | reasoning tokens |
| --- | ---: | ---: | ---: |
| Understanding20次 | 4,879,680 | 46,294 | 19,023 |
| Refine6次 | 63,926 | 23,463 | 1,977 |
| 合计26次 | **4,943,606** | **69,757** | **21,000** |

Understanding 平均每次243,984 input tokens，占已知输入的98.7%；该输入包含全量图片及文本，未做供应商图文token拆分。low 已显式生效于请求字段，但不会减少图片数，也不等于零reasoning。另28次拒绝／取消缺少usage，不能记成零；这只是本次任务已知用量，不是全账号账单，不能由此反推五小时全部额度由本轮消耗。

本轮支持“相同课程输入下，K3.1 Pro low 没有重现 K3 low 的空／极短输出”这个局部结论。尚不能确定 K3 模型本身、low实现、结构输出模式或供应商当时服务状态中哪一项造成此前异常；也没有系统人工核对所有对白覆盖、视觉事件与时间准确率。保留 K3.1 Pro low 的本次冻结配置和成果，额度未恢复不继续重试；恢复同一v10应复用既有ASR与已提交Chunk，无需再开整课版本。

完整课程、原视频50–60%用时目标和检索→出处→回看→追问仍未验收；内容门禁、Understanding稳定排序、跨Chunk时间规则与额度分类没有因换模型自动修复。报告未引入新PR、新代理、新业务代码或额外模型探针。

完整原始请求／响应及进程日志仅保留在本机 `outputs/verification/lets-build-gpt-k31pro-low-v10-direct-20261102/`；提交的证据只含配置、哈希、计数、校验错误与脱敏错误链，不含凭据、原始图片或模型推理正文。
