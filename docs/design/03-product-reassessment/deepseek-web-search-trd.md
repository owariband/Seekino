# 1. 问题与现状

Chat 原本通过 `DDGS.text` 提供无需 API key 的网页搜索，却没有独立配置来选择搜索服务。本机已有 DeepSeek 额度并希望使用其官方搜索，但这不应让其他用户被迫配置付费 API。首版 PR 将 DDGS 移除、默认绑定 DeepSeek，错误地把本机选择变成了产品要求。需要保留免 key 接入，同时让有 API 的用户显式选择服务。此前的搜索失败与引用问题仍保留为验证案例，不将它们归因于整个 DDGS 库。

本需求让用户在无需 API key 的 DDGS 与 DeepSeek 官方 Search API 之间显式选择，保留 Agent 的 `search_web(query)`、标题/URL/摘要结果与现有引用校验。默认兼容 DDGS；本机测试选择 DeepSeek 是部署配置，不是产品强制要求。涉及供应商请求、配置装配、各自查询长度及错误反馈，不新增网页抓取工具或修改视频检索。2026-11-02 修正此前移除 DDGS、只允许 DeepSeek 的方案；两种路径分别验证，不能凭 key 是否存在自动切换。

分支 `feat/deepseek-web-search`；[PR #28](https://github.com/owariband/Seekino/pull/28) 直接向 `dev` 集成。网页搜索配置不依赖 ASR PR #18 或 Chat 流式 PR #25；此前 DeepSeek low 测试仅作为问题证据，不构成代码或合并依赖。2026-11-02 更新：两种配置已实现并验证，DeepSeek 真实页面接入通过；回答及引用质量仍待验收。联合回归后决定将搜索配置能力合入 dev；#25 先行合入后，本分支同步最新 dev 处理文本冲突，这不是新的功能依赖。

# 2. 影响因素、假设与评价指标

官方 [Search 指南](https://api-docs.deepseek.com/guides/search/)明确支持复用 DeepSeek API key；搜索是独立 HTTP 请求，不需要把 Flash 换成 Pro。关键差异是 query 最长 128 Unicode 字符，DDGS 与视频查询原先允许 500 字符；网页工具 Schema 必须按所选 provider 约束，不能把 API 限制施加给免 key 路径。

| 指标 | 现状与假设 | 验收标准与测法 |
| --- | --- | --- |
| 选择与默认行为 | 原来仅 DDGS，首版错误强制 DeepSeek | 省略配置为免 key DDGS；显式 DeepSeek 才读取 key、调用官方 endpoint；不自动切换 |
| 查询与结果合同 | query≤500，结果≤20，摘要≤600 字符 | DDGS query≤500、DeepSeek≤128，视频仍≤500；标题/URL/摘要字段、去重、URL 安全校验和字节预算保持 |
| 失败可解释性 | 旧 WebSearchError 全部被标为可重试 | 空 results 是成功无结果；401/402 不建议立即重试，429/超时/5xx 标可重试；密钥不入日志 |
| 时间、调用次数与费用 | 历史工具样本受模型、搜索、历史影响，不能直接作 A/B | 固定 Python 官方文档题记录 Search 次数、耗时、结果与整轮耗时；性能探索，不预先承诺提速 |
| 来源与回答质量 | 曾有未返回 URL、摘要被当原文及 URL 误拦 | 真实来源可核对，不能把摘要声称为全文；保留失败案例，区分接口接入与答案质量 |

# 3. 改造方案与建模

调用链保持 `Supervisor search_web → Gateway WebSearcher → 配置选择的 DDGS / DeepSeek → WebSearchHit`。Server 从既有 YAML 配置装配 WebSearcher；Gateway 处理鉴权、HTTP 与响应校验，不接入业务会话或数据库。

1. 独立 `web_search.provider` 默认 `ddgs`，可选 `deepseek`。DDGS 读取 backend、不读取 key；DeepSeek 使用官方地址与配置的 key 环境变量名。本地启动器仅在选择 DeepSeek 时检测搜索 key 变化。缺 key 只让已选择 DeepSeek 的搜索失败，不阻塞其他 Chat。
2. 以 `POST /v1/tools/search` 传 `query/count/need_content=false`。保留当前结果数量与摘要预算，优先取用于模型阅读的 `summary`，缺少时用 `snippet`；正文未获取时不冒充原文。DDGS 保留原依赖与 `title/href/body` 映射，两种服务共用清洗和预算。
3. Gateway 暴露所选服务的查询上限，Agent 据此构造参数 Schema：DDGS 500、DeepSeek 128；不截断搜索操作符，不缩短视频查询。工具说明提示按需使用 `site:`。保留 WebSearchError，但携带是否可重试；记录供应商、HTTP 状态、请求标识、耗时和结果数量，不记录 key、正文或完整 query。
4. 保留既有工具调用预算、超时与引用处理。官方 Fetch 是另一种工具，当前替换无需增加它；原有 URL 标点误拦单独记录，不借此扩大链接允许范围。

验证先做单次官方 API 探针，再覆盖适配器/配置/工具参数，最后在专用测试库用 DeepSeek low 经真实页面搜索。最多先做 5 次直接 Search 探针及 3 轮 Chat，失败优先定位，不无界重试；首轮页面未重跑 DDGS 对照；配置修正后额外各做一次 DDGS / DeepSeek 探针，验证两条路径可用，不用单样本证明总体效果。

# 4. 对照实验与结果分析

以下先保留首版 DeepSeek 接入的历史实验，再补配置选择的验证。

| 比较项 | 旧实现证据 | 本次结果 | 判断 |
| --- | --- | --- | --- |
| 供应商与请求 | DDGS.text，无显式 backend | 探针及两轮页面共 10 次官方 Search，均 HTTP 200、有结果 | 实际入口已接通，无 DDGS 回退 |
| 参数、错误与配置 | 网页/视频共用 500；错误全部 retryable | 432 项相关回归通过；含 128/129 边界、视频 500、空/畸形响应、401/402/429/5xx/timeout 与 key 变更 | 离线合同通过，真实故障分布未测 |
| 真实耗时 | 旧模型/历史/结果不同，不构成同条件 A/B | 两轮 Search 分别 4/5 次，单次 0.303–0.839 s；Server 整轮 12.859/13.765 s | 各一个探索样本，不外推提速或尾延迟 |
| 来源与回答 | 旧样本存在无效引用和过度断言 | 增加过滤说明后出现可点击官方文档；仍有 3 处未核实链接及不准确表述 | 质量未通过，不能关闭全部验收 |

第一次页面样本未使用站点过滤，第二次仅补工具说明后五次均使用 `site:docs.python.org`。两次均保留；同题两轮不是稳定效果评测。7 次模型调用共 input 42,456 / output 2,911 tokens；10 次 Search 估算 $0.0075，不含生成、非账单。本需求独立于流式改动，未计量 FTT。

源码版本、原始 HTTP 日志、查询、用量、错误断言和质量核对见[实施报告](../../report/gateway/deepseek-web-search-20261102.md)。完整回答仅保存在本地 outputs/verification；没有改写之前 #25 的测量记录。

配置选择修正后：相关回归 **604 passed / 1 skipped**（可选 MySQL 检查缺配置），含默认 DDGS 不读取 key、有 key 不自动切换、两种查询上限与双向不回退。相同查询各一次真实探针：DDGS 未向适配器提供任何 key，2.714 s 返回 5 条；显式 DeepSeek 0.588 s 返回 5 条。这只确认两种入口可运行，不证明稳定速度或质量优劣。

# 5. 试点验证与方案取舍

已在独立 API 19626 / Web 15176、新建专用测试库和索引完成两轮页面试点，模型为 DeepSeek Flash low。原流式页面继续保留；未改主环境配置、执行视频解析或迁移既有数据。本实例显式选择 DeepSeek；仓库默认仍为 DDGS。配置修正不扩大为通用回答质量已达标。

若 key 或 Search 权限不可用，保留错误与配置修复入口，不回退到 DDGS。若接口可用但答案或引用未达标，分别报告接入结果与质量缺口。官方成功且有结果的 Search 每次约 $0.00075，实际费用按官方规则与运行记录估算，不冒充账单。

# 6. 结果验收与闭环

免 key 默认、显式 API 选择及各自参数边界已验证，用户无需购买 API 即可保留原搜索方式。本需求测试实例显式选择 DeepSeek，真实页面接入证据保留原运行版本。答案仍有引用与事实错误，整体质量验收未通过；[C24](../../dev/problems.md#deepseek-web-search-c24) 保持 TODO。2026-11-02 决定通过 PR #28 将已验证的搜索配置能力集成到 dev；保留失败样本和后续质量验收，不宣称主环境已部署。

下一步由本需求逐条比对工具返回 URL、模型引用及过滤结果，验证摘要使用与不准确断言。未取得证据前不将 API 成功或单次耗时写成质量、性能达标。
