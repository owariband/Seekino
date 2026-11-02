# 1. 问题与现状

Chat 的 `search_web` 目前由 `gateway/web_search.py` 调用 `DDGS.text`，没有指定 backend，也没有记录实际搜索供应商。把主模型切到 DeepSeek 后，网页检索仍走这个旧入口，无法确认搜索来源和费用。此前普通 Chat 试跑还出现搜索空结果、失败及为获取具体文档而反复查询；这些现象不能单独证明 DDGS 是原因，但需要换成明确、可核验的官方服务继续验证。

本需求将网页搜索切换为 DeepSeek 官方 Search REST API，保留 Agent 的 `search_web(query)`、标题/URL/摘要结果与现有引用校验。涉及供应商请求、配置装配、查询长度及错误反馈，不新增网页抓取工具或修改视频检索。先确认官方接口与当前 key 可用，再做接入和真实页面验收。

分支 `feat/deepseek-web-search`；[PR #28](https://github.com/owariband/Seekino/pull/28) 直接向 `dev` 集成。网页搜索替换不依赖 ASR PR #18 或 Chat 流式 PR #25；此前 DeepSeek low 测试仅作为问题证据，不构成代码或合并依赖。2026-11-02 初稿；当前只有接口与源码调查，未实施、未实测新搜索。

# 2. 影响因素、假设与评价指标

官方 [Search 指南](https://api-docs.deepseek.com/guides/search/)明确支持复用 DeepSeek API key；搜索是独立 HTTP 请求，不需要把 Flash 换成 Pro。关键差异是 query 最长 128 Unicode 字符，当前网页与视频共用的参数类型允许 500 字符，必须分开约束。

| 指标 | 现状与假设 | 验收标准与测法 |
| --- | --- | --- |
| 实际供应商 | 当前直接 DDGS，模型切换不影响搜索 | 真实工具调用命中 `/v1/tools/search`，日志标识 DeepSeek；不自动回退 DDGS |
| 查询与结果合同 | query≤500，结果≤20，摘要≤600 字符 | 网页 query≤128，视频仍≤500；标题/URL/摘要字段、去重、URL 安全校验和字节预算保持 |
| 失败可解释性 | 旧 WebSearchError 全部被标为可重试 | 空 results 是成功无结果；401/402 不建议立即重试，429/超时/5xx 标可重试；密钥不入日志 |
| 时间、调用次数与费用 | 历史工具样本受模型、搜索、历史影响，不能直接作 A/B | 固定 Python 官方文档题记录 Search 次数、耗时、结果与整轮耗时；性能探索，不预先承诺提速 |
| 来源与回答质量 | 曾有未返回 URL、摘要被当原文及 URL 误拦 | 真实来源可核对，不能把摘要声称为全文；保留失败案例，区分接口接入与答案质量 |

# 3. 改造方案与建模

调用链保持 `Supervisor search_web → Gateway WebSearcher → DeepSeek Search → WebSearchHit`。Server 从既有 YAML 配置装配 WebSearcher；Gateway 处理鉴权、HTTP 与响应校验，不接入业务会话或数据库。

1. 在现有 Gateway 配置增加独立 `web_search` 配置，默认 DeepSeek 官方地址及 `DEEPSEEK_API_KEY` 环境变量名，与主模型选择解耦。缺 key 只让搜索明确失败，不阻塞无搜索的普通 Chat。
2. 以 `POST /v1/tools/search` 传 `query/count/need_content=false`。保留当前结果数量与摘要预算，优先取用于模型阅读的 `summary`，缺少时用 `snippet`；正文未获取时不冒充原文。移除 DDGS 直接依赖。
3. 网页独立参数类型使用 128 字符上限，不截断查询或 `site:` 等操作符，不缩短视频查询。保留 WebSearchError，但携带是否可重试；记录供应商、HTTP 状态、请求标识、耗时和结果数量，不记录 key、正文或完整 query。
4. 保留既有工具调用预算、超时与引用处理。官方 Fetch 是另一种工具，当前替换无需增加它；原有 URL 标点误拦单独记录，不借此扩大链接允许范围。

验证先做单次官方 API 探针，再覆盖适配器/配置/工具参数，最后在专用测试库用 DeepSeek low 经真实页面搜索。最多先做 5 次直接 Search 探针及 3 轮 Chat，失败优先定位，不无界重试；未使用 DDGS 重新调用作对照，采用已有运行证据并说明差异。

# 4. 对照实验与结果分析

| 比较项 | 旧实现证据 | 新实现计划 | 当前判断 |
| --- | --- | --- | --- |
| 供应商与请求 | DDGS.text，无显式 backend | 核对官方 endpoint、Bearer、count 与实际 JSON | 待测 |
| 空结果 / 失败 | 原测试区分空数组和异常，Agent 全标 retryable | Mock HTTP 覆盖 200 空/畸形响应、401/402/429/5xx/timeout | 待测 |
| 查询边界 | 网页与视频共用 500 | 网页 128/129 边界，视频 500 保留 | 待测 |
| 真实搜索质量与耗时 | 原 Python 文档题多次搜索，见 #25 运行报告 | 同题固定 DeepSeek low，记录实际返回 URL、摘要、次数和耗时 | 待测；单次不外推总体质量 |

原始响应仅存独立 worktree 的 outputs/verification，脱敏摘要与命令按 Gateway 实施报告保存。每条完成结论关联实际版本、配置与证据。

# 5. 试点验证与方案取舍

由本需求在现有专用 MySQL/ES 测试数据、独立 API/Web 上执行试点，先确认没有正在运行的用户请求，再切换本实例代码。页面保持 DeepSeek low，以普通文档检索验证实际工具接入；不启动视频解析或迁移数据。

若 key 或 Search 权限不可用，保留错误与配置修复入口，不回退到 DDGS。若接口可用但答案或引用未达标，分别报告接入结果与质量缺口。官方成功且有结果的 Search 每次约 $0.00075，实际费用按官方规则与运行记录估算，不冒充账单。

# 6. 结果验收与闭环

初稿阶段。接口、配置、参数边界、错误分级及真实产品入口全部验证后，才能确认供应商替换完成；性能与通用问答准确率另按样本范围报告。PR 创建后补链接，实施后更新同一 TRD、问题状态、实验记录及证据，不以代码存在代替运行已接入。
