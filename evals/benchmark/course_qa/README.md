# 课程闭环 QA

主标准见 [课程 QA TRD](../../../docs/design/03-product-reassessment/course-qa-trd.md)。本目录保存固定用例和采证工具；当前问题状态只维护在 `docs/dev/problems.md`，每轮质量结论需要人逐项核对原片。

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
