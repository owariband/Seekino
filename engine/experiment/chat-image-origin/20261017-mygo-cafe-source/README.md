# 咖啡店局部截图：Chat Agent 出处定位

2026-10-17。**本次最终交付通过**：Chat 回答《BanG Dream! It's MyGO!!!!!》第 1 集约 17:33，并提交一张视频卡，窗口 17:32.300–17:36.100。独立直接查看两张原片帧，确认用户截图对应全景前景中两位顾客、桌面杯具、椅子及琴包所在区域。

## 问题与输入

只向全新会话发送「这张图片出自哪一集？」和[用户原图](images/image-01.png)，范围限定 MyGO。原图 630×412、343737 字节，上传及回读 SHA 一致；没有补充人物名、咖啡店名称、场景描述、台词、猜测集数或此前实验结果。模型随后自行生成的搜索描述不属于用户输入。

复用 [既有 runner](../20261016-mygo-image-origin/run.py)，没有修改脚本；其会话标题沿用“MyGO 六图出处实验”，实际只有 1 张附件。没有改生产代码、配置或重启服务。执行前 API/进程内 Consumer 正常，MyGO 13/13 READY；总执行和 Supervisor 1200 秒，Query/Evaluator/Chat 各 480 秒，客户端 1500 秒。

| 项目 | 结果 |
| --- | --- |
| 时间（北京时间） | 2026-10-17T14:12:21.535428+08:00 至 2026-10-17T14:16:18.531977+08:00 |
| 会话 / 助手 | `3c2542c6-37e5-433a-8249-c553cbf966df` / `edd707f0-1c43-4175-b3bc-a1f6ca9b7c83` |
| API / 模型 | PID 27365 / gpt-6-astra |
| 客户端 / 持久化耗时 | 236.994 / 236.821 秒 |
| 完整 search_video_graph | 209.216 秒，1 次，实际下发 1 张图 |
| 最终消息 | answer.completed / COMPLETED，lastError=null |
| 最终提交 | 1 次 submit accepted，1 张视频卡，1 个 hit，视频状态 READY |
| Chunk | `0c19d787-d299-5c26-805c-dfa974af0408` |

最终消息原文：

> 我会在《It's MyGO!!!!!》中核对这张咖啡店画面的出处。
>
> 这张图片出自 **《BanG Dream! It's MyGO!!!!!》第 1 集，约 17 分 33 秒处**。
>
> 它是 RiNG 咖啡店全景镜头的局部裁切：背对镜头、穿青绿色上衣的长发女生，对面穿粉色上衣的短发女性，以及右侧的深色乐器包，都与核验画面一致。
>
> 时间以当前片源为准，不同版本可能略有偏移。

## 如何找到

起初它猜测人物为长崎爽世，随后改用普通顾客、服装和琴包的外观描述，并尝试中文/英文及不同通道。**前五次检索都没有返回最终目标**。第六次改为“RiNG 咖啡厅 全景 许多客人 桌边聊天 顾客”，目标首次出现并排第一，再核对全景中的前景局部。

下表为持久化 retrievalTrace 中的实际原词，权重顺序为 lexical/semantic/visual。所有 query 都是文字；没有把上传原图直接 embedding 后检索。

| 次数 | query | 权重 | Top K |
| --- | --- | --- | ---: |
| 1 | 咖啡店桌边 长崎爽世 背对镜头 浅棕色长发 对面粉色上衣短发女性 旁边深色乐器包 | 0.05/0.15/0.8 | 8 |
| 2 | 咖啡店 顾客 背对镜头 棕色长发青绿色上衣灰裙 对面短棕发粉色短袖翻领上衣女性 黑色长琴包靠椅 茶杯 | 0/0.05/0.95 | 5 |
| 3 | 咖啡厅背景顾客 两名女性 粉色短袖衬衫 短棕发 绿色上衣长发女生 背影 椅子旁黑色吉他包 | 0.1/0.6/0.3 | 8 |
| 4 | 咖啡厅里多名普通顾客坐着喝茶聊天，背景有穿粉色Polo衫的棕色短发女性与穿青绿色上衣的长发女性，桌边立着深色吉他软包 | 0/1/0 | 12 |
| 5 | Two background female customers sitting opposite at cafe table, short brown bob hair woman in pink polo shirt smiling, long light brown hair woman teal top back facing camera, dark guitar gig bag leaning on chair foreground | 0/0/1 | 8 |
| 6 | RiNG 咖啡厅 全景 许多客人 桌边聊天 顾客 | 0.1/0.8/0.1 | 10 |

最终目标在第六次检索的 textRank=1、visionRank=5、lexicalRank=13，融合后第一。因此这次主要经历了回到场景全景语义、再核对局部的路线，不能解释成初次局部视觉查询直接命中。

## 核验与耗时

- retrieve 6 次全部成功，共 51 个候选出现、24 个独立 Chunk；合计 2.714 秒，平均 452.333 ms。
- Query 取帧 17 次、Evaluator 3 次，共实际检查 15 个不同 Chunk；77 帧次、61 个唯一 SHA、4 次缓存命中。
- Query 有 RANGE_TOO_LARGE 和 INVALID_RANGE 各一次，均为 Canvas 读取反馈，之后恢复。其他角色无这类错误。旧取帧次数/上下文预算拦截均为 0。
- 持久化阶段：QUEUED 11.221 秒，RETRIEVING 141.397 秒，EVALUATING 33.651 秒，RESPONDING 50.552 秒；最后一项含 Supervisor 最终交付，不能称为内部 Chat 的纯推理耗时。
- 直接查看了 1053250、1055750 ms 的全幅原帧，二者前景区域与用户局部图对应。保存原字节于 [verified-frames](run1/verified-frames/)，来源、请求点、SHA 和方法见 [visual-review.json](run1/visual-review.json)。未生成或处理新的裁剪图。

原图只占完整画面的一部分，本轮确认相符镜头与近似时间，没有测量唯一精确输入帧、解码 PTS、几何裁剪框或全部重复出现位置。最终结构化消息有 READY 视频证据，但本轮没有打开浏览器实际点播。

## 产物与复现

- [manifest.json](manifest.json)：实际问题、唯一输入、原始字节和 SHA。
- [runtime.json](runtime.json)：执行前后健康、配置、PID 和源码指纹；本轮均无漂移。
- [run.json](run1/run.json)、[run.sse](run1/run.sse)、[run.log](run1/run.log)：原始请求、回答、事件和时间窗日志。
- [observations.json](run1/observations.json)、[trace.log](run1/trace.log)：按本会话隔离的统计和日志。
- [validation.json](run1/validation.json)：上传 SHA、源帧 SHA、最终交付、配置一致性与敏感模式检查。

```sh
uv run --no-sync --env-file .env.local python -u \
  engine/experiment/chat-image-origin/20261016-mygo-image-origin/run.py \
  --manifest engine/experiment/chat-image-origin/20261017-mygo-cafe-source/manifest.json \
  --output-dir /tmp/mygo-cafe-source-repeat \
  --log-file outputs/server/logs/development/api.log --timeout 1500
```

输出目录须全新。此实验创建聊天、附件和模型调用，未执行图片向量直接检索、生产索引改造、数据库迁移、服务重启、commit 或 push。新增 13 个实验文件，修改主题索引 1 份；生产代码、配置和测试均为 0。目录沿用既有 ignore 规则，普通 Git diff 不反映实验产物。原工作区其他变更未触碰。

```text
AniVAG/
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [M] README.md
            └── 20261017-mygo-cafe-source/
                ├── [A] README.md
                ├── [A] manifest.json
                ├── [A] runtime.json
                ├── images/
                │   └── [A] image-01.png
                └── run1/
                    ├── [A] run.json
                    ├── [A] run.sse
                    ├── [A] run.log
                    ├── [A] observations.json
                    ├── [A] trace.log
                    ├── [A] visual-review.json
                    ├── [A] validation.json
                    └── verified-frames/
                        ├── [A] source-1053250.jpg
                        └── [A] source-1055750.jpg
```
