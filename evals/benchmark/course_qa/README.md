# 课程闭环 QA

主标准见 [课程 QA TRD](../../../docs/design/03-product-reassessment/course-qa-trd.md)。本目录保存固定用例和采证工具；当前问题状态只维护在 `docs/dev/problems.md`，每轮质量结论需要人逐项核对原片。

具体测试题维护在 TRD 的 [QA 测试用例集](../../../docs/design/03-product-reassessment/course-qa-trd.md#qa-case-set)：各组先写业务目标，再列 query、预期结果、工作量、实际结果、FTT、TT / 操作耗时和检验效果。实时问答、局部证据追问、全课检索分开评分；“工作量”说明需要读取的证据和执行的动作，不代表统一的响应时限。已有 Q01–Q03 原题和历史结果保留，新增用例是否具备素材、是否已执行均在表中注明。

Q 表示 query。`cases.json` v2 将 W01/W02/W03 更名为 Q01/Q02/Q03，每题保留 `legacy_id`；问题和预期没有改动。历史原始事件、截图与页面快照仍使用 W 编号，新运行使用 Q 编号。[首字追踪](../../../docs/report/server/course-qa-20261101.md#ftt-trace) 复用旧日志，记录完整模型调用与首段整包正文等待；历史 FTT 标注已撤回，真实流式 FTT 未采集。

**每条问答必须分别报告真实流式 FTT 和 TT。** 具体口径以 [Chat 流式 PRD](../../../docs/design/03-product-reassessment/chat-streaming-prd.md) 为准：先核对实际模型请求流式、供应商正文 delta 及端到端实时转发，再记录客户端首正文；TT 仍到整轮完成事件。当前 `browser.mjs` 只采客户端事件，没有供应商增量与页面绘制计时，单独运行它不能证明真实流式 FTT。旧三轮的 9.173/29.576/27.231 秒改称首段整包正文到达耗时，FTT 字段为空并注明非流式。固定进度、心跳、工具状态和推理不算正文；模型进度正文须标明类型，实质回答及来源齐备时间另列。失败/取消保留终止耗时，不能当成功 TT。

**先按主 TRD 第 2–4 节确定业务任务、测试范围与要改变的优化决策，再选择工具。** 当前 `cases.json` 和 `browser.mjs` 只覆盖观看后的三轮，不能代表目标找课、学习顺序、七集建库或公开部署已经验收。主指标是限时有来源任务完成、拿到可继续操作的结果所需时间、整条路径累计系统等待和可用课程覆盖；工具耗时、缓存命中与调用次数用于诊断。出处题必须等可核对的具体来源出现才算可用，不能以进度提示或提前出现的泛泛解释计分。

下一批素材先独立核对概念、代码/公式和缺证等代表题的原片依据；一门就绪课程即可验证目标/基础约束及找片段，两门相关课程就绪即可验跨课组织。Q01–Q03 保留为调试回归，并保留新位置作为验收样本。按基础/派生缓存、是否必须看图及后台负载分组，确认对照能回答“改哪一环才能更快完成用户任务”，再追加同条件调用；完整范围和当前缺口不在本目录另建状态表。

FTT 采证缺口不阻断内容、工具决策与 TT 验收。`realtime-boundaries.json` 固定 RT04/RT05；`http_cases.py` 为每题创建独立 QA 会话，使用真实 HTTP SSE 记录完成时间和持久答案，始终将 FTT 标为未采集。先用 `snapshot.py` 核对隔离库、运行来源与已有 Chunk，再执行：

```bash
python evals/benchmark/course_qa/http_cases.py --api http://127.0.0.1:19621 --cases evals/benchmark/course_qa/realtime-boundaries.json --out outputs/verification/course-qa/<new-run>/http --live
```

此固定套件发送两次新问题，会产生模型费用；每题 240 秒期限，异常尝试取消后停止后续问题，取消回执仍需核对终态。它是 HTTP 内容／TT 验证，不代替浏览器交互、流式 FTT 或完整 CoT 采证。使用项目 Python 3.12 环境，输出目录必须全新。[首轮两题结果及记录边界](../../../docs/report/server/course-qa-reasoning-boundaries-20261101.md) 已回填主 TRD。

`cases.json` 对应 PR #18 的隔离《Let's build GPT》素材，不指向开发库。视频时间是整数毫秒。`review` 是评审依据，不发送给模型。改变素材需同时更新 ID、位置与评审事实，并生成新版本；不能改题后覆盖旧结果。

先用 `snapshot.py --help` 查看只读指纹采集参数。它要求测试库名以 `_test` 结尾，使用本机独立 MySQL，保存 Chunk 0/1/2 和任务状态。前后快照应使用不同的新目录。源码指纹只标识磁盘内容，运行进程是否加载它仍需部署证据。调用前另行记录当前 ASR 请求数及并发解析负载。

前端连接被测 API，端口必须独占。以下命令在 QA worktree 根目录运行，需已有 Web 依赖与本机 Chrome：

```bash
SEEKINO_HTTP_PORT=19621 npm --prefix web run dev -- --host 127.0.0.1 --port 15173 --strictPort
# 只检查真实页面、媒体及侧栏，不发送用户问题。
node evals/benchmark/course_qa/browser.mjs http://127.0.0.1:15173 outputs/verification/course-qa/<new-run>/browser
# 确认 warm_base_precondition、模型授权与费用预算后执行三轮真实问题。
node evals/benchmark/course_qa/browser.mjs http://127.0.0.1:15173 outputs/verification/course-qa/<new-live-run>/browser --live
```

页面使用真实 HTTP、真实视频和正常的表单提交，不拦截/伪造业务返回。工具通过复制响应流采集 SSE 单调时间，并保存实际请求、播放位置、浏览器错误、截图、时间点点击和刷新前后正文。采证本身会有少量开销，不是负载压测。`--live` 会新建 QA 会话、最多发送三次用户问题并产生模型费用；失败停止继续发送，240 秒时请求取消，取消是否完成需查看持久状态。每次使用全新输出目录，避免混淆事件。

人工审核需要检查：事实是否对应原片；后续讲解是否冒充当前已经讲到；每个时间点对应哪条陈述；同会话指代是否保持；回看定位与连续播放是否正常；刷新后消息和终态是否一致。`answer.snapshot` 可早于终态展示正文，首个实质回答应从事件和页面内容标记，不能只取首条事件或进度说明。首问与追问、冷暖缓存分别统计，小样本不报告尾部分位数。

`reliability.py --help` 是真实 HTTP 补充入口：读取本轮浏览器已保存的请求，对原 QA 消息做完成重放和 key 冲突验证；新建一个有 `[course-qa]` 标识的会话测试 PROCESSING 重放、取消、继续及未 READY 范围。必须显式 `--live`，最多再发送三条新问题；其中未 READY 查询预期在模型调用前拒绝。取消合同为 `FAILED/AGENT_CANCELLED`，不是不存在的 `CANCELLED` 消息状态。失败即中止，保留中间结果，不能把取消回执当作模型已停。

失败案例、HTTP 幂等/取消补充、配置摘要及最终判断保存在各轮报告。原始响应与截图留在忽略目录；不把密钥、媒体或签名 URL 提交到 Git。该工具不是自动放行器，也不启动解析 Consumer、不重启服务、不切换模型。
