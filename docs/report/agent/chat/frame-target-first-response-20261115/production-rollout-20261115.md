# 2026-11-15：Chat 最小正式发布与 CPU/Cloud 上线

[main PR #180](https://github.com/owariband/Seekino/pull/180) 已合并为 `f7823c8d`，从最新main `d0844554` 选取20个运行文件。代码来自dev #171、#178、#179；旧模型继承发布候选#172由本次覆盖并关闭。内部设计、devlog、看板、评测与测试文件没有进入main，main忽略规则保持；本地发布树810路径／10资产及GitHub release-files通过。

## 用户交付与最小范围

正式行为包括单时间点原片取帧、用户时间／本轮播放锚点、同循环先确认再取证、JEV-only模板与按需Query、父子模型控制和动态容量一致、工具schema及原生task入口修复、JEV连接复用与关闭生命周期。3份SKILL.md由产品运行时加载，是工具指令；不能当开发材料删除。

server/service/agent_chat.py只向main加入videoId／seriesId两行，没有顺带发布反馈功能、数据库结构或前端。候选配合独立验证工作树运行1088 passed／1 skipped，42.72s，一条既有Starlette警告；它包含先前部分回归，不累加唯一测试数。

## 实机预检与部署

Cloud与CPU对应同一个API实例。部署从在线message-feedback-74cb3c96镜像覆盖19个与main精确一致的文件，agent_chat在原线上文件上只补两行，保留既有反馈方法；Gateway配置对内部embedding和document_base_urls的兼容继续沿用。镜像是有明确兼容差异的runtime overlay，不是全量main镜像。

候选以相同非root用户、配置、媒体和网络在独立临时容器装配，SQL强制只读，关闭启动清理，不运行Parse Consumer。Supervisor与Query均为Flash/high/8192，实际policy相同：context1048576、output8192、margin4096、trigger891289、keep24000。原片1056000ms返回1张JPEG，跨Series范围拒绝，Parse任务数不变，生成0次；实际PTS未知。

准备中修正了构建文件／父目录权限和预检stdin。首次切换又因把容器内模型路径当宿主bind源被Docker拒绝；旧API仍健康、启动时间未变。随后按实际bind源修正Compose插值，核对环境与全部挂载一致后才重试。没有把这些失败写成成功部署。

最终运行镜像为`seekino-api:chat-frame-f7823c8d-20261115`，image ID `sha256:118dae687799072e15258e1d268b00ef18f3411cc662312837b60b4eaee9f988`。API启动于`2026-11-15T06:17:31.243694003Z`，healthy；切换操作17.136s，该时间不是精确停机或Chat耗时。Web、embedding、MySQL、ES、Redis五个容器ID未变。切换时近期PROCESSING0、活动Parse／Chunk／Refine0，旧PROCESSING2未作为在途任务处理；未做数据迁移或索引重建。原镜像与原Compose文件保留，可回切。

## 公网真实探针

HTTPS健康200，新游客创建会话并经CSRF校验完成SSE至answer.completed。使用非私密公开样例问题：

> 请看看这部视频17:36这一帧，简单描述你实际看到的画面。

| 指标 | 本次单条公网观测 |
| --- | ---: |
| 首可见正文FTT（目标确认） | 5.281s |
| 服务端TT | 7.725s |
| 客户端TT | 8.807s |
| JEV／Supervisor／改写调用 | 1／2／0 |
| 实际工具调用／错误 | 1／0 |
| input／output／total token | 34,630／386／35,016 |
| 其中reasoning／cached input | 144／19,072 |

首段为“我先查看你指定的 17:36 这一帧画面。”。只读核查该新会话原生checkpoint与用量，实际仅get_video_frame(1056000)，没有用1056558ms播放点替代；模型请求工具列表含该单点工具、不含原生task，独立改写与合并决策均0。回答对浅棕长发、闭眼微笑和暗处灯带的主要描述与原图相符，并承认单帧不能证明动作和对白；仍以眼睛配色辅助猜身份，而闭眼图不支持该细节，不能据此认定完整视觉质量已通过。

线上本次生成成功，说明此前402是当时观测，不能继续称当前模型不可用；原T02长历史指代仍未按最终代码重放。单条公网新会话也不能代替三条历史对照、近满窗、Query收敛、并发或p95，不把5.281s与本地三条均值直接比较。

## 配置、证据与边界

上线后active模型和agent-runtime哈希与切换前相同，JEV-only、rewriter关闭、Flash/high/8192与85%策略保持。完整白名单收据和逐调用token见[production-rollout-20261115.json](production-rollout-20261115.json)；不发布身份／CSRF、消息ID、原始checkpoint、配置正文或凭据。前述较早三组指标仍按各冻结版本阅读，见[本报告入口](README.md)。

当前待验收项只在[工作台](../../../../dev/workbench.html)CHAT-FRAME-FIRST-01维护：正式发布与部署已完成，T02原长历史和完整质量仍待验证。
