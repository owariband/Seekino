# 茶杯极小局部截图：Chat Agent 出处定位

最新复测：[run2：全部 GPT 绑定切换为 GPT-5.6 Sol](run2/README.md)。服务用时 18:27 正常完成并返回“第 2 集约 11:38.5”及 1 张卡，但其明确引用的原帧与用户图不符，独立视觉验收失败；真实出处仍未确认。`stop` 修复尚未实施。

以下正文保留首轮 run1（gpt-6-astra）的记录与当时产物范围。

2026-10-17。**本次未找到出处，最终交付失败。** 仅给出一张 282×214 的茶杯局部图，Chat 在 20 分钟总执行时限到达时，以 `AGENT_EXECUTION_TIMEOUT` 结束；没有集数、定位时间、视频卡或已确认的原片证据。不能据此认定原片中不存在该画面。

## 问题与输入

向全新会话发送「这张图片出自哪一集？」和[用户原图](images/image-01.png)，视频范围限定 MyGO。原图 70032 字节，上传并回读校验 SHA 一致。没有向模型补充人物名、地点、场景描述、猜测集数或此前实验结果。Supervisor 随后自行描述杯子等可见细节，再携带该图调用搜索；这些文字不是 runner 提供的提示。

本轮与[咖啡店截图实验](../20261017-mygo-cafe-source/README.md)使用同一 Chat 入口，但未建立两张上传图来自同一原帧的裁剪关系，不能作为同一原图逐级裁剪的严格对照。两例也不是图片向量直接检索实验。

复用[既有 runner](../20261016-mygo-image-origin/run.py)，没有修改脚本；其会话标题沿用“MyGO 六图出处实验”，实际只有 1 张附件。API/进程内 Consumer 正常，MyGO 13/13 READY；总执行和 Supervisor 为 1200 秒，Query/Evaluator/Chat 各 480 秒，客户端等待 1500 秒。本轮没有改生产代码、配置或重启服务。

| 项目 | 结果 |
| --- | --- |
| 时间（北京时间） | 2026-10-17 14:23:11 至 14:43:11 |
| 会话 | `8016a767-9528-42e2-939f-28bb1ff75cfe` |
| 助手 | `ccc97a8b-cd56-4c90-a0dc-4f9d28afa6c9` |
| API / 模型 | PID 27365 / gpt-6-astra |
| 客户端 / 持久化耗时 | 1200.199 / 1200.067 秒 |
| search_video_graph | 1 次，1189.649 秒，以执行超时失败，实际下发 1 张图 |
| 消息终态 | answer.failed / FAILED |
| 最终结果 | searchResult=null，0 次 submit，0 张视频卡，无确认定位 |

持久化正文仅有开场白：

> 我会在《It's MyGO!!!!!》中核对这张茶杯画面的出处。

最终错误为 `AGENT_EXECUTION_TIMEOUT`，消息为“本次处理超时，已停止生成，请缩小问题后重试。”服务端同一会话日志记录根异常 `AgentExecutionTimeoutError`，发生阶段为 RETRIEVING。不是客户端 1500 秒等待到期，也不是图片上传失败。

## 实际查找过程

Supervisor 生成的完整工具任务描述为：

> 定位附件截图出自哪一集及具体时间。图中是白色茶杯盛黄色茶水、白色碟子与勺子，下方红色圆形物，背景格纹衣服。请核对一致画面。

此后进入 6 轮 Query、5 次 Evaluator 审核。每次审核都因 Draft 为空而返回 continue；没有已选片段可供逐项审核，Evaluator 本轮未独立取帧，也未进入内部 Chat 成文阶段。第六轮 Query 尚在查找时总时限到达。

| 审核时刻 | 结果 |
| --- | --- |
| 14:26:15 | Draft 空，继续查找；提醒不要预先把红色圆形物认作茶壶 |
| 14:29:42 | Draft revision 41 空，继续查找 |
| 14:32:33 | Draft revision 60 空，继续查找 |
| 14:37:45 | Draft revision 96 空；提醒不要依赖未经证实的留学情节假设 |
| 14:41:09 | Draft revision 118 空；提醒不要以未经证实的英国、早餐情节限制召回 |

这些是日志中的审核意见，不能作为画面真实出处或实际检索原词。本轮失败前没有生成持久化 retrievalTrace，运行日志只记录 retrieve 的 query 长度、摘要 hash、通道权重和候选 ID。**47 条新查询的完整文字无法从当前保存的产物恢复**，因此本报告不杜撰逐次搜索词，也不把上面的 Supervisor 描述当作底层 retrieve 原词。

## 工具与耗时

| 指标 | 实测 |
| --- | ---: |
| 成功 retrieve 调用 | 48 次：47 次新 query、1 次游标翻页 |
| 候选交付次数 / 独立 Chunk | 423 / 81 |
| retrieve 合计工具耗时 | 25.402 秒 |
| Query 取帧 / Evaluator 取帧 | 81 / 0 次 |
| 实际取帧检查的不同 Chunk | 24 |
| 图片交付帧次 / 不同图片 SHA | 256 / 131 |
| 取帧缓存命中 | 43 次 |
| 取帧合计工具耗时 | 21.939 秒 |
| 成功 Canvas 读取 | Query 248 次、Evaluator 5 次 |
| 成功 Canvas 读取合计耗时 | 12.301 秒 |

候选交付次数根据各页实际返回的 ID 统计；游标翻页的 result_count 沿用原检索总数，直接相加会多算。缓存命中代表整次取帧结果命中缓存；256 帧次包含重复传输，131 个不同 SHA 也不代表连续覆盖了原视频。

Query 发生 9 次 `RANGE_TOO_LARGE`、1 次 `INVALID_CATALOG_CURSOR`、1 次 `INVALID_RANGE`，均是 Canvas 读取反馈，之后继续执行；最终终止由总执行超时造成。`FRAME_INSPECTION_LIMIT`、`VIDEO_CONTEXT_BUDGET_EXCEEDED` 均为 0。

本轮不是向量库或解码单次调用慢到 20 分钟：可观测工具耗时远小于总耗时，流程经历大量模型与工具往返、重复读帧和未产出 Draft 的审核循环。现有统计不能将剩余时间精确归因到某一次模型推理、排队或图片传输。

## 结论与局限

图片从上传到 Supervisor 搜索子任务的传递正常，模型能识别可见物件；这张缺少人物、文字和大场景线索的局部图，现有文字检索加原片取帧路线未在 20 分钟内定位。上一张咖啡店局部图 3:57 成功，并不意味着任意小物件截图都能同样定位。

本轮没有确认源帧，gold 集数、时间及 Chunk 保持空值；不计算准确率或 Recall@K，不把任意茶杯候选保存为已核验原片，也没有继续延长超时或追加带提示的重试。可观察到的流程问题包括重复取帧和空 Draft 审核持续返回 continue；本轮只记录，不修改运行策略或新增次数限制。

执行前后 API PID、配置 revision 和 247 个被记录源码文件的 SHA 均一致，健康检查正常。本轮未打开浏览器测试错误 UI 或实际点播；终态依据 SSE、持久化消息与服务日志相互核对。

## 产物与复现

- [manifest.json](manifest.json)：唯一输入、原问题、原字节 SHA，无猜测答案。
- [runtime.json](runtime.json)：执行前后服务健康、配置、PID 与源码指纹。
- [run.json](run1/run.json)、[run.sse](run1/run.sse)、[run.log](run1/run.log)：请求、消息、终态、时间窗日志。
- [observations.json](run1/observations.json)、[trace.log](run1/trace.log)：按本会话隔离的工具统计与日志。
- [validation.json](run1/validation.json)：输入、上传、终态、计数、环境一致性及敏感模式检查。

```sh
uv run --no-sync --env-file .env.local python -u \
  engine/experiment/chat-image-origin/20261016-mygo-image-origin/run.py \
  --manifest engine/experiment/chat-image-origin/20261017-mygo-teacup-detail-source/manifest.json \
  --output-dir /tmp/mygo-teacup-detail-repeat \
  --log-file outputs/server/logs/development/api.log --timeout 1500
```

输出目录须全新，此命令会创建新聊天、上传附件并调用模型。新增 10 个实验文件，修改主题索引 1 份，生产代码、配置和测试文件修改为 0。目录沿用既有 ignore 规则，普通 Git diff 不反映实验产物；原工作区其他变更未触碰。未执行数据库迁移、服务重启、commit 或 push。

```text
AniVAG/
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [M] README.md  ← 增加本轮失败案例索引
            └── 20261017-mygo-teacup-detail-source/
                ├── [A] README.md  ← 实验问题、真实结果及局限
                ├── [A] manifest.json  ← 固定原始输入
                ├── [A] runtime.json  ← 执行前后环境记录
                ├── images/
                │   └── [A] image-01.png  ← 用户原图
                └── run1/
                    ├── [A] run.json  ← 请求及最终持久化消息
                    ├── [A] run.sse  ← 原始事件流
                    ├── [A] run.log  ← 执行时间窗日志
                    ├── [A] observations.json  ← 工具与终态统计
                    ├── [A] trace.log  ← 会话日志
                    └── [A] validation.json  ← 产物验收
```
