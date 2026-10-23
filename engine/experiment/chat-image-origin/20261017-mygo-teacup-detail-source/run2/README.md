# 茶杯局部截图复测：全部 GPT 绑定切换为 GPT-5.6 Sol

2026-10-17。**模型切换与服务重启完成；本轮服务正常交付，但出处定位未通过独立视觉验收。** Chat 用时 18:27，回答第 2 集约 11:38.5 并提交一张卡；它明确声称匹配的 698500 ms 原帧与用户图不符。不能把 COMPLETED、Evaluator 放行或视频卡产生视为定位成功。

## 输入与执行环境

复用[同一 manifest](../manifest.json)、[用户原图](../images/image-01.png)和[既有 runner](../../20261016-mygo-image-origin/run.py)，新建独立会话，仅发送「这张图片出自哪一集？」，范围为 MyGO。原图 282×214、70032 字节，SHA-256 为 `09c08c81fb240f65f2f7a4bfa274bed258c79d92ac57411a28e13121be7d5cab`，上传并回读一致。没有提供集数、台词、场景提示或首轮结果；根 Agent 的独立核帧没有反馈给运行中的 Chat。

用户最初写 `gpt-5.6-solar`，核对本机可用列表后，用户明确确认采用 **`gpt-5.6-sol`**；官方模型名参见 [OpenAI 模型文档](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。本次只修改 `config.yaml` 中以下三项，其他模型及参数保持原值：

| 活动绑定 | 修改前 | 修改后 | 消费方 |
| --- | --- | --- | --- |
| models.agent | gpt-6-astra | gpt-5.6-sol | Query、Evaluator、内部 Chat |
| models.supervisor | gpt-6-astra | gpt-5.6-sol | Supervisor |
| models.vision | gpt-6-astra | gpt-5.6-sol | 新解析任务及相关视觉理解 |

角色环境变量没有覆盖这三项。没有修改 Codex 全局默认值、DeepSeek/Ark/Embedding/Speaker、历史任务快照或历史实验输出；重启前没有待运行或运行中的解析任务，也没有 PROCESSING 聊天。

API 于 18:54:31 正常终止旧进程并启动 PID 80339，18:54:37 就绪；Consumer 仍在 API 进程内。Web 随后重启为 PID 84658，启动记录与新配置一致；未重启数据库或 ES。实验开始前 MyGO 13/13 READY，总执行与 Supervisor 1200 秒、各内部阶段 480 秒、客户端 1500 秒。

**此前讨论的 Evaluator `stop` 修复尚未实施。** 本轮验证的是换模型后的现有链路，不是停搜修复验收。与首轮之间工作区存在其他历史变更，不能把两轮差异全部归因于模型；本轮执行期间记录的源码与配置指纹保持一致。

## 最终交付与验收

| 项目 | 结果 |
| --- | --- |
| 时间（北京时间） | 18:55:33.872–19:14:00.982 |
| 会话 / 助手 | `2e1daa99-a22c-4d77-9995-a6ee47d2bd4e` / `a6fe4cdd-9342-4c8b-a73a-4da263b17456` |
| 客户端 / 持久化耗时 | 1107.097 / 1106.902 秒 |
| 子搜索耗时 | 1041.348 秒 |
| 执行终态 | FINISHED / answer.completed / COMPLETED，lastError=null |
| 最终提交 | 1 次 submit accepted，1 个 hit，1 张视频卡 |
| 声称的出处 | 第 2 集约 11:38.5，Chunk `2ecbd6cd-3935-58f4-b709-b7cff16b2788` |
| 卡片范围 | 694700–707800 ms，即 11:34.7–11:47.8 |
| 独立视觉验收 | **FAILED_VISUAL_MATCH**；真实出处仍未确认 |

最终正文中的关键主张为：

> 这张图出自《BanG Dream! It's MyGO!!!!!》第 2 集，约在 11:38.5。
>
> 截图中的白色茶杯、浅黄色茶水、杯碟，以及画面下沿千早爱音的红粉色头发，都与原片吻合。
>
> 已核对完整 13 集，匹配结果明确。

根 Agent 直接查看了实际交付给 Evaluator 的 698500 ms 原帧，并按回执核对原始 JPEG SHA。该帧显示粉发角色、灰色制服外套、绿色条纹领带、手持白杯与偏棕色液体，前景是浅粉色长方形物体；用户图中的杯口描边、黄色液体、杯柄／杯身比例、红色圆弧前景及深色格纹背景没有在这张帧中对应。另一轮无历史上下文的独立看图复核也判为不匹配。

候选原帧保存为 [source-698500.jpg](reviewed-frames/source-698500.jpg)，SHA 为 `5574acd42c0ea8d4046f75d8e27188538e25ed7688dcbc1cec1ab3d5e74ed2a7`；完整来源与视觉判断见 [visual-review.json](visual-review.json)。没有裁剪、调色、缩放或重编码。该判定否定这张明确引用的原帧与用户图匹配，不证明整个 Chunk、同集其他时间或全作品中不存在目标。

## 实际检索与审核轨迹

所有 retrieve 输入仍为文字，没有把用户图片直接编码后匹配逐关键帧向量。持久化 retrievalTrace 保留了三次实际 query，权重顺序为 lexical/semantic/visual，Top K 均为 10：

| 次数 | 实际 query | 权重 |
| --- | --- | --- |
| 1 | 白色茶杯装浅黄色茶水，放在白色杯碟上；画面前景有红色圆形物体，背景是蓝灰色格纹衣物或布料的特写镜头 | 0.05/0.15/0.8 |
| 2 | 特写镜头：白色茶杯装浅黄色茶水，放在白色杯碟上；画面前景有红色圆形物体，背景是蓝灰色格纹衣物或格纹布料 | 0.02/0.08/0.9 |
| 3 | 白色无明显纹饰的茶杯，杯中浅黄色茶水，白色杯碟，下方前景有红色圆形物体，后方是蓝灰色格纹衣物；特写构图及相邻镜头 | 0.02/0.08/0.9 |

第 2 集候选在首次查询就排第一，第二次排第二、第三次排第三；它被召回不代表画面匹配成立。

| 轮次与审核时刻 | 实际决定 |
| --- | --- |
| 1，19:00:03 | continue。独立取帧后否决第 2 集约 11:37：茶水、背景、前景物体不符，并指出播放范围截断相邻动作 |
| 2，19:05:33 | continue。否决第 12 集约 21:27：茶杯有蓝色叶纹和纹章，红色物体、构图不符 |
| 3，19:09:44 | continue。回到第 2 集，声称 698500 ms 原帧匹配，仅要求扩展播放范围；与第一轮否定方向及实际原帧不一致 |
| 4，19:11:54 | complete。范围扩展为 694700–707800 ms 后放行，同样声称 698500 ms 匹配 |

内部 Chat 生成草稿后，Supervisor 读取 review、来源清单与文字 Evidence，显式提交一张卡并写出确定出处。根 Agent 未修改此原始答案；本报告保留自动流程的误判，不把最终完成状态当作通过。

## 工具覆盖与模型记录

- 成功 retrieve 3 次，30 个候选交付项、13 个不同 Chunk，合计 1.691 秒。
- Query 取帧 9 次、Evaluator 19 次，共 28 次，101 帧交付、66 个不同 SHA、7 次缓存命中，工具合计 8.479 秒。
- **实际取帧只涉及两个 Chunk，分别是第 2、12 集候选。** `searchedVideoIds` 的 13 集表示索引搜索范围，不表示对完整 13 集逐集看过原片。最终“已核对完整 13 集”属于覆盖夸大。
- 四次 Query 与四次审核中，第四轮主要修订已有范围，没有新增 retrieve。流程经历多轮模型与工具往返及重复核验；工具耗时不能等同模型纯推理时长，也不能由剩余时间精确反推各项开销。
- 可恢复工具反馈为 `INVALID_READ` 3、`STALE_REVISION` 1、`INVALID_RANGE` 3：Query 2 次、Evaluator 1 次、内部 Chat 4 次；Supervisor 无此类工具错误。旧取帧／上下文拦截均为 0。
- [model-usage.json](model-usage.json) 中 10 条 GPT 执行记录全部为 `gpt-5.6-sol`，另有 3 条 `doubao-embedding-vision-250615` 记录，没有 Astra。GPT 记录按原生会话／执行聚合，不能称作只有 10 次内部采样；Token 原值保留，不在本实验推算实际账单。

## 结论、验证与运行边界

本轮确认模型替换已贯穿活动 GPT 角色及实际执行，但没有解决小局部图定位质量：相似场景被错误认作同帧，审核先否定后放行，最终答案还夸大了核验覆盖。这是一个“正常完成但视觉匹配失败”的案例。首轮 Astra 为 20 分钟超时且零卡，本轮 Sol 为 18:27 完成一张未通过验收的卡，两轮都没有确认真正出处；不能据此推断模型的一般准确率或性能优劣。

配置验证确认仅三处 model 值变化，其他配置保持原值；本轮未实施 `stop`、未改 Agent 程序或提示词、未迁移数据库、未重写历史任务、未 commit/push。输入／上传 SHA、调用模型、终态、trace 和候选原帧分别核对，结果见 [validation.json](validation.json)。本次没有浏览器实际点播，也没有追加带答案提示的模型重试。

实验结束后，日志另出现 19:15:05 API 开始关闭、19:15:38 新 PID 91385 就绪；本轮根 Agent 未发起这次额外重启。收尾健康请求曾在该间隙连接被拒，重试成功，当前 API/Consumer 正常且配置仍为 Sol。该事件晚于 19:14:00 实验完成，不是本次回答终止原因；详见 [runtime.json](runtime.json)。

## 产物与复现

- [run.json](run.json)、[run.sse](run.sse)、[run.log](run.log)：原始请求、回答、卡片、事件及时间窗日志。
- [observations.json](observations.json)、[trace.log](trace.log)：按会话隔离的工具统计、审核与检索原词。
- [runtime.json](runtime.json)、[model-usage.json](model-usage.json)：模型绑定、重启、执行前后环境和调用记录。
- [visual-review.json](visual-review.json)、[候选原帧](reviewed-frames/source-698500.jpg)：视觉验收失败的原始依据。
- [validation.json](validation.json)：完整性与边界验证。

```sh
uv run --no-sync --env-file .env.local python -u \
  engine/experiment/chat-image-origin/20261016-mygo-image-origin/run.py \
  --manifest engine/experiment/chat-image-origin/20261017-mygo-teacup-detail-source/manifest.json \
  --output-dir /tmp/mygo-teacup-sol-repeat \
  --log-file outputs/server/logs/development/api.log --timeout 1500
```

命令会创建聊天并调用当前配置模型，输出目录须全新。本轮新增 run2 的 11 个文件，修改 `config.yaml`、本实验索引及主题索引共 3 份；原始 manifest、用户图和 run1 原始结果保持不变。实验目录沿用既有 ignore 规则，其他工作区修改不属于本轮。

```text
AniVAG/
├── [M] config.yaml  ← 三个活动 GPT 绑定切换为 Sol
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [M] README.md  ← 更新主题最新结果
            └── 20261017-mygo-teacup-detail-source/
                ├── [M] README.md  ← 关联复测，首轮正文保留
                └── run2/
                    ├── [A] README.md  ← 实测结果及视觉验收失败
                    ├── [A] runtime.json  ← 模型与重启记录
                    ├── [A] run.json  ← 请求和最终交付
                    ├── [A] run.sse  ← 事件流
                    ├── [A] run.log  ← 时间窗日志
                    ├── [A] observations.json  ← 会话统计
                    ├── [A] trace.log  ← 会话日志
                    ├── [A] model-usage.json  ← 请求模型与用量
                    ├── [A] visual-review.json  ← 原帧对照与复核
                    ├── [A] validation.json  ← 产物验证
                    └── reviewed-frames/
                        └── [A] source-698500.jpg  ← 原始候选帧，未作编辑
```
