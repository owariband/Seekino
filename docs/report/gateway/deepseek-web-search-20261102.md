# DeepSeek 网页搜索接入验证（2026-11-02）

[PR #28](https://github.com/owariband/Seekino/pull/28) 直接面向 `dev`，主设计为 [TRD](../../design/03-product-reassessment/deepseek-web-search-trd.md)。只替换 `search_web` 的供应商，不包含 PR #18 的 ASR 或 PR #25 的正文流式实现。

## 接入与离线回归

`Supervisor → WebSearcher → POST https://api.deepseek.com/v1/tools/search` 已在真实页面执行。首版 Server 注入 DeepSeek 配置，曾移除 DDGS 及其 lxml/primp 锁定项；同日后续修正已恢复原依赖，支持用户选择，见文末。网页查询限 128 字符，视频查询仍为 500。保留 URL 校验、去重、摘要预算与既有引用规则；配置/额度错误不可立即重试，408/429/5xx 和传输错误标为可重试。

独立 `dev@56e79777` 基线的适配器、配置、Supervisor、Bootstrap、本地启动器回归 **432 passed，26.33 秒**，1 条既有 Starlette/AnyIO 弃用警告。首轮有两个既有完整响应测试桩未显式关闭 stream；补齐 `stream=False` 后通过，未因此引入 #25 代码。工具说明补充官方站点过滤后，Supervisor **137 passed**；该次缓存目录写入被本机权限拒绝，不影响断言。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest \
  tests/gateway/test_web_search.py tests/gateway/test_config.py \
  tests/agent/chat/test_supervisor.py tests/server/test_bootstrap.py \
  tests/server/test_local_services.py -m 'not live_agent' -q --tb=short
```

## 真实页面样本

独立实例：API `19626`、Web `15176`，专用空库 `seekino_deepseek_search_20261102_test` 及同前缀 ES 索引。主工作区配置和其他实例保持原状。模型为 `deepseek-flash / low`，实际使用 7 次生成调用；测试代码由 `900ad97c` 加[源码 SHA](evidence/deepseek-web-search-20261102/verification.json) 标识。

固定问题：“请先简短说明你的查询计划，然后联网搜索 Python 官方文档，用三点解释 asyncio.sleep 与 time.sleep 的区别，给出官方来源链接。”每组一个新会话；第二组只给工具说明增加 `site:` / `lang:` 用法。供应商缓存与检索结果变化不可控，这不是稳定性能对照。

| 项目 | 首轮（未提示站点过滤） | 补充过滤说明后 |
| --- | --- | --- |
| DeepSeek Search 成功次数 | 4/4 | 5/5 |
| 单次 Search 耗时（ms） | 463.9、551.7、302.6、355.6 | 418.2、342.8、838.8、380.6、379.2 |
| 每次有效结果数 | 5、5、5、5 | 3、4、3、5、5 |
| 模型调用数 | 3 | 4 |
| Server 整轮耗时（s） | 12.859 | 13.765 |
| 查询是否限定官方域名 | 否 | 五次均使用 `site:docs.python.org` |
| 最终引用 | 2 处未核实链接，无直接官方文档链接 | 1 个官方文档 URL 正常显示，仍有 3 处未核实链接 |
| 回答质量 | 未通过 | 未通过 |

另有一次直接 Search 探针：HTTP 200，约 400 ms，5 个结果。两轮页面加探针共 10 次非空成功 Search，按[官方费率](https://api-docs.deepseek.com/guides/search/#billing)估算 **$0.0075**，不含模型生成、不是账单。刷新后回答及来源保留，浏览器错误日志为空。7 次模型调用合计 input **42,456**、output **2,911** tokens；缓存输入 31,104、reasoning 902 分别是其子集。没有重跑 DDGS 作 A/B，也没有在 #28 计量 FTT。

## 失败案例与结论边界

首轮使用普通关键词，命中旧邮件列表与第三方资料；回答把 asyncio 的运行方式限定为必须在 async def 内 await，并混用了旧版 time.sleep 的信号行为。官方文档列出多种[协程执行方式](https://docs.python.org/3/library/asyncio-task.html#coroutines)，而 [time.sleep](https://docs.python.org/3/library/time.html#time.sleep) 在信号处理器不抛异常时会继续等待。第二轮找到了官方页，仍把 time.sleep(0) 泛称为空等；该说法忽略 Windows 的让出时间片行为。两个失败样本均保留，未靠挑选成功回答验收。

仅有摘要时，模型仍说过“核对原文”，引用了不在允许结果中的 URL。现有 URL 严格匹配和中文标点提取问题也未在本次更改。**供应商替换与配置接入已验证，回答和引用质量没有验收通过**；[C24](../../dev/problems.md#deepseek-web-search-c24) 保持 TODO；该轮试点后 PR 暂留 Draft，后续集成决定见本文末节。下一步应逐条对照工具返回 URL、模型引用及过滤结果，修复证据使用问题后再验收；不以搜索 API 的成功率替代问答质量。

脱敏记录：[请求与计时](evidence/deepseek-web-search-20261102/provider.log)、[源码/查询/用量/样本](evidence/deepseek-web-search-20261102/verification.json)、[JUnit](evidence/deepseek-web-search-20261102/backend.xml)。完整页面回答和截图保留在独立 worktree 的 `outputs/verification/deepseek-web-search-20261102/`。

## 配置选择修正：保留免 API key 接入

本机选择 DeepSeek 不应成为所有用户的强制配置。默认恢复 `provider: ddgs`，原 DDGS 9.16.0 依赖和锁定项不升级；`provider: deepseek` 显式启用官方 API。DDGS 不读取 API key，即使环境已有 DeepSeek key 也不会自动使用；任一路失败均不跨供应商回退。DDGS backend 可设 auto 或具体支持的后端，配置错误时明确失败。

工具 Schema 从实际所选服务获取上限，DDGS 500、DeepSeek 128，视频查询仍为 500；Server 装配及启动器 key 变更检查同步覆盖。上述命令额外加入 `tests/agent/chat/test_query_hook.py`、`tests/server/test_agent_chat_service.py`，结果 **604 passed / 1 skipped，39.20 s**。跳过项仍缺少专用 MySQL 测试配置，既有 Starlette 弃用警告不影响断言。未引入 Chat 流式代码或修改 PR #25。

同一公开查询 `asyncio.sleep site:docs.python.org`，结果上限 5、超时参数 8 秒：

| 显式选择 | 适配器可用的 API 凭据 | 单次耗时 | 结果数 |
| --- | --- | --- | --- |
| DDGS / auto | 无 | 2.714 s | 5 |
| DeepSeek | 配置的 DeepSeek key | 0.588 s | 5 |

这两次是入口探针，各一个样本，不用于宣称稳定效果或总体提速。新增一次 DeepSeek Search 估算 $0.00075，不含此前调用；没有新增生成模型调用或重评答案质量。当前专用实例显式保留 DeepSeek，主工作区配置不变。

新证据：[探针结果](evidence/deepseek-web-search-20261102/provider-choice-probes.json)、[修正源码与回归记录](evidence/deepseek-web-search-20261102/provider-choice-verification.json)。


## 2026-11-02：与 Chat 流式联合验证及 dev 集成

对 #25 `04eecafd` 和 #28 `8ea667c0` 的联合代码完成后端 1086 passed / 1 skipped、前端 159 passed，typecheck/build 通过。跳过项为缺少 `SEEKINO_TEST_DATABASE_URL` 的可选 MySQL 检查；本轮没有新增真实模型调用。GitHub 无远端 CI check，这些结果来自本机隔离验证。

#25 已先合入 dev（`2cd27caa`）；#28 同步最新 dev，仅处理 Supervisor import 与三份文档插入冲突，保留双方内容。合并后的全部 Python/前端实现与测试逐文件核对，和上述已验证联合副本一致。此次只同步集成状态和旧依赖表述，未修改已记录的失败结果。验证清单见 [dev-integration-review.json](evidence/deepseek-web-search-20261102/dev-integration-review.json)。

本轮决定交付流式计量和搜索配置能力；课程 Q02/Q03 复测、回答及引用质量继续跟进，合入 dev 不代表这些验收或主环境部署完成。
