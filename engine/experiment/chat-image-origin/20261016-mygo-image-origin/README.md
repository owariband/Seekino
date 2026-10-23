# MyGO 六张截图：Chat Agent 出处定位实验

- 日期：2026-10-16 首次执行，2026-10-17 整理并恢复服务复跑。
- 状态：四次六图真实问答、四次三图分组已执行。**最新两组三图均完成最终聊天出处交付**，见 [chat-08：原图 1–3](./runs/chat-08/README.md)、[chat-09：原图 4–6](./runs/chat-09/README.md)。
- 最新结论：按用户要求把总执行、Supervisor 及其模型绑定超时从 600 秒调为 1200 秒，重启 API 后用相同清单、新会话同时重跑。chat-08 耗时 706.510 秒，chat-09 耗时 789.081 秒，均 answer.completed / COMPLETED，分别交付 3 张视频卡；最终逐图集数为 **5、5、1、7、13、4**。六张卡的范围均覆盖本轮直接查看的相符原片帧。两组都实际越过 600 秒并正常结束，旧的 10 分钟限制已解除。
- 范围：这是**同一条问题、六张原图的探索性案例**。chat-02–05 是六图联合输入，chat-06/07 是 600 秒三图分组失败记录，chat-08/09 是 1200 秒三图重跑。没有完整唯一帧或全部重复位置真值，不计算全量准确率或 Chunk Recall@K；不是纯图片向量召回评测。

最新分组结果、耗时、配置和证据保存在 [chat-08](./runs/chat-08/README.md)、[chat-09](./runs/chat-09/README.md)。此前三图总超时见 [chat-06](./runs/chat-06/README.md)、[chat-07](./runs/chat-07/README.md)；六图总超时与并行链路分析见 [chat-05](./runs/chat-05/README.md)，阶段超时及服务中断检查见 [chat-04](./runs/chat-04/README.md)。下文第 3–5 节保留前两次运行的历史比较。

| 原图 | 独立核对的原片出现位置 | 最新分组状态 | 最终聊天交付 |
| --- | --- | --- | --- |
| image-01 | 第 5 集，约 06:14–06:15 | chat-08 完成 | 是，含视频卡 |
| image-02 | 第 5 集，约 17:27–17:28 | chat-08 完成 | 是，含视频卡 |
| image-03 | 第 1 集，约 21:49 | chat-08 完成 | 是，含视频卡 |
| image-04 | 第 7 集，约 20:57–20:58 | chat-09 完成 | 是，含视频卡 |
| image-05 | 第 13 集，约 00:55 | chat-09 完成 | 是，含视频卡 |
| image-06 | 第 4 集，约 13:01 | chat-09 完成 | 是，含视频卡 |

以上为相符镜头的出现位置，截图裁切与字幕范围可能不同；不是唯一精确帧定位。已核帧保存在两组的 verified-frames 目录，核验方法、请求采样时间和 SHA 见各自 visual-review.json。没有把这些人工核对结果补进实验问题。

## 1. 实验问题与验收口径

用户已知截图属于 MyGO，希望只凭图片找到出处。实验提交原问题：

> 这些是出于哪一集?

chat-02–05 的一条消息按原顺序附上六张 PNG；chat-06/07 及 chat-08/09 分别按原顺序附上原图 1–3／4–6，各用一个新会话，第二组的本组图号 1/2/3 对应原图 4/5/6。检索范围通过 `seriesIds` 限定为 `mygo`。不在正文补充人物名、图片描述、字幕转写或猜测的集数；下文用于辨识样本的描述没有额外输入模型。

这里分别检查两件事：

1. **图片输入与实际执行路径**：附件是否保持原字节，模型是否表现出对画面的理解，实际调用的是文字检索、原帧核对，还是直接图片向量检索。
2. **出处定位结果**：逐张返回集数，以及可回到原片核验的时间和 Chunk；匹配画面要由原片确认。无法确认的项应单列。

HTTP 200、`answer.completed` 或模型报出集数，都不单独代表第二项通过。模型自己的回答也不能充当真值。

## 2. 输入、模型与运行口径

| 项目 | 设置或记录 |
| --- | --- |
| 输入 | 用户提供的六张 PNG；原始字节及上传顺序固定 |
| 图像处理 | 未做缩放、裁剪、去字幕、增亮或重编码；图 4 的暗色外观保留 |
| 样本编号 | `image-01` 至 `image-06`；尺寸、SHA-256、字节数见 [manifest](./manifest.json) |
| 检索范围 | `mygo`，系列 ID `836fa9f0-2bf4-50fb-9c64-75526fa1dea9`；运行前服务显示 13/13 集 READY |
| 聊天入口 | 本机 API `http://127.0.0.1:9621`，tenant 为 `local-default` |
| 最终回答模型名 | 八次均为 `gpt-6-astra`；各新运行目录保存进程、有效配置及实验记录 |
| 提交形式 | 创建会话 → 上传本轮六图或三图 → 回读核对 SHA-256 → 发送原问题及对应 `mediaUri` → 保存 SSE 和最终消息 |
| 检索形式 | 模型看图后生成文字 query，调用既有 `retrieve`；`chat-03` 还调用 `inspect_video_frames` 核对候选 |
| 原图向量检索 | 未执行；文字 query 使用 visual 通道，不等于把上传原图 embedding 后搜索 |
| 重复次数 | chat-02–05 各一次六图输入；chat-06/07 在 600 秒下各运行一次；chat-08/09 用相同分组输入在 1200 秒下各重跑一次；没有完全相同状态的重复测量、随机采样或独立无图对照 |

`chat-02` 在只接通 Supervisor 图片输入的代码状态下执行；`chat-03` 在向 Query、Evaluator 等角色统一透传图片后执行。两次使用相同六张原图和问题，但代码与执行路径不同；`chat-03` 使用新会话、新幂等键。下表比较实际观察，不据此认定某个改动单独造成了效果或耗时变化。

## 3. 历史两次问答的结果

| 指标 | [chat-02](./runs/chat-02.json) | [chat-03](./runs/chat-03.json) |
| --- | --- | --- |
| 执行时间（北京时间） | 10-16 22:25:11–22:27:25 | 10-16 23:39:16–23:48:09 |
| 客户端总耗时 | 134.599 秒 | **533.244 秒** |
| 服务持久化总耗时 | 134.133 秒 | 532.219 秒 |
| 聊天终态 | HTTP 200，COMPLETED | HTTP 200，COMPLETED |
| 原图回读校验 | 6/6 张 SHA-256 一致 | 6/6 张 SHA-256 一致 |
| 当前／历史输入图片数 | 6／0 | 6／0 |
| `retrieve` 调用次数 | 1 | 15 |
| 实际检索表现 | 仅用原始短问题，semantic=1、visual=0；召回 1 个 Chunk | 按画面内容组织 query，包含正的 visual 权重；继续读取候选证据和原帧 |
| 原帧检查 | 未记录成功取帧 | 12 次成功，交付 32 帧次；其中 19 个唯一 SHA、5 次缓存命中 |
| 帧检查额度错误 | 未记录 | 6 次 `FRAME_INSPECTION_LIMIT` |
| 检索子图最终结果 | 最终选中 0 个片段 | 第 4 轮 Evaluator 入口出现 `VIDEO_CONTEXT_BUDGET_EXCEEDED` |
| 最终回答 | 六张均未确认 | 报出六个暂定集数，明确核验未完成 |
| 最终视频卡／定位时间 | 无／无 | 无／无 |
| 出处定位验收 | 未通过 | 未通过 |

32 帧次包含重复交付，不是 32 张唯一画面；12 是成功工具调用次数，不是图片数。模型调用次数、token 和费用没有完整记录，不能用工具调用数或配置上限代替。两次耗时仅为单次观测值，不构成稳定的性能对照。

## 4. chat-03 的逐图结果

以下集数直接来自 `chat-03` 的最终回答，是**未核验预测**。所有样本的 `gold_episode`、`gold_timestamp_ms` 仍为空。

| 原图 | 画面辨识说明 | Agent 暂定集数 | 最终 Chunk／时间 | 原片真值核验 |
| --- | --- | --- | --- | --- |
| [image-01](./images/image-01.png) | 粉发人物仰视近景，顶部带中日化妆字幕 | 第 1 集 | 未提供 | 待标注 |
| [image-02](./images/image-02.png) | 教室内三名同学围着背对镜头的粉发人物 | 第 5 集 | 未提供 | 待标注 |
| [image-03](./images/image-03.png) | 夕阳街头，粉发人物张嘴近景 | 第 5 集 | 未提供 | 待标注 |
| [image-04](./images/image-04.png) | 暗色画面中的棕发人物回头 | 第 10 集 | 未提供 | 待标注 |
| [image-05](./images/image-05.png) | 蓝灰发人物、白色无袖衣服的正面近景 | 第 13 集 | 未提供 | 待标注 |
| [image-06](./images/image-06.png) | 泡沫背景中的粉发人物近景 | 第 1 集，待确认 | 未提供 | 待标注 |

最终答案没有给出六张图的可复核位置，因此出处定位未验收。**这不等于已经测出“0/6 集数正确”**；没有真值，就不能判断这些暂定集数的正确率。检索次数也不能反推出 Recall@K。

## 5. `chat-03` 为什么没有完成定位

原始证据：[SSE](./runs/chat-03.sse)、[服务日志](./runs/chat-03.log)、[最终消息和摘要](./runs/chat-03.json)。会话为 `a96920d1-153c-490e-a53c-f4a0275e5c46`，助手消息为 `e4470f04-9a61-4911-9ad9-973ccb020bab`。

1. 服务记录 `current_image_count=6 history_image_count=0`。Supervisor 描述了六张图的具体画面；Query 随后执行按画面内容组织的文字检索，并读取候选原帧。这些是真实运行观察；Query/Evaluator 的供应商请求中各有几张原图，没有独立的输入级 trace 保存。
2. 第一轮 Query 已用完 12 次成功取帧，交付 32 帧次。Query、Evaluator 和后续循环共用取帧计数；额度检查发生在缓存读取之前，已有缓存也会被额度挡住。
3. 首轮 Evaluator 开始就无法自行取帧。总计 6 次额度错误，其中 Query 3 次、Evaluator 3 次；前三轮审核均返回 `continue`，要求进一步核验尚未定位的图片。
4. 第 4 轮 Evaluator 刚进入即被本地上下文检查拒绝；日志第 233 行为 `VIDEO_CONTEXT_BUDGET_EXCEEDED`、phase=evaluator，检索工具历时 485.811 秒。日志没有记录原始异常的实际大小和阈值，无法确定命中的是字符检查还是 token 预检；不能解释为模型服务的调用额度耗尽。
5. Supervisor 得到失败的检索结果后，仍完成了一条带不确定性说明的回答。最终日志是 `video_used=false`、`submitted_video_count=0`，消息没有 `searchResult`；聊天 COMPLETED 与定位任务完成是两件事。

因此，这次执行支持“图片已经参与聊天和候选核验”，但**不足以说明现有视觉向量能稳定定位这六张图**。取帧预算和循环上下文问题也使本轮无法干净地衡量检索算法本身。

## 6. 局限与下一步

- 六张图已有独立核对的同镜头出现位置；仍需补齐唯一帧或重复使用／回忆画面的多个合法位置真值，再进行独立定位评测。
- 后续若要回答“原图 embedding 能否召回现有 Chunk”，应另建直接图片向量检索实验，固定索引、候选排名及真值，避免把当前 Agent 的多步查找当作图片 Recall@K。
- 本案例没有裁剪图、GIF、视频或构图／动作相似性对照，也没有实测历史图片输入。
- 历史运行尚未完整锁定索引数据版本、各角色有效模型配置和预算；`chat-03.json` 的 `source_sha256` 是最终工作区哈希，不能当作运行进程的精确源码快照，详见其 `source_note`。chat-03 的 API PID 为 19340，34957 是该次实验之后重启的 PID。chat-04 运行于新 PID 53308，另存启动源码指纹、有效配置和最终无漂移审计；索引数据仍未完整锁版本。
- 原始 JSON 中的 `COMPLETED`／`FINISHED` 描述执行状态，保留原样；manifest 的最新状态和本报告描述定位验收结果。

## 7. 产物与复现

- [样本清单](./manifest.json)、[六张原图](./images/)：原始字节、顺序、SHA-256 和待补真值。
- `runs/chat-02.*`、`runs/chat-03.*`、[runs/chat-04/](./runs/chat-04/README.md)、[runs/chat-05/](./runs/chat-05/README.md)、[runs/chat-06/](./runs/chat-06/README.md)、[runs/chat-07/](./runs/chat-07/README.md)、[runs/chat-08/](./runs/chat-08/README.md)、[runs/chat-09/](./runs/chat-09/README.md)：每次请求、回答、SSE 事件与服务日志；旧记录没有因复跑而修改。
- [图 1–3 分组清单](./manifests/images-01-03.json)、[图 4–6 分组清单](./manifests/images-04-06.json)：实际样本顺序及组内编号映射；既有 runner 通过 `--manifest` 复用，没有修改脚本。
- [复跑脚本](./run.py)：从当初六图提交脚本整理而来，改为参数化路径、新输出目录和导入保护；这是复跑工具，不冒充当时原封不动执行的脚本快照。

在项目根目录先检查样本，不连接服务：

```sh
.venv/bin/python engine/experiment/chat-image-origin/20261016-mygo-image-origin/run.py --validate-only
```

真实复跑要求本机 API 已启动、MyGO 系列可检索且模型可用；会创建新会话、上传六图并调用配置的模型服务。输出目录必须尚不存在，以免覆盖历史记录：

```sh
.venv/bin/python engine/experiment/chat-image-origin/20261016-mygo-image-origin/run.py \
  --base-url http://127.0.0.1:9621 \
  --output-dir /tmp/mygo-image-origin-retest-01
```

脚本保存新请求、原始 SSE、最终消息、耗时和可用的本机日志。只有收到 `answer.completed` 且助手状态为 COMPLETED 才记录 FINISHED；失败、中断或缺少终态会记录对应状态并非零退出。FINISHED 仍不表示集数正确，脚本不会标注真值；复跑当前服务得到的结果也不保证与历史运行一致。

## 8. 接入阶段的历史记录

| 记录 | 做了什么 | 结果 | 是否计入问答结果 |
| --- | --- | --- | --- |
| [preflight-01](./runs/preflight-01.json) | 检查服务、系列和附件入口 | 当时运行 API 尚无附件路由，未上传、未问模型 | 否 |
| [attachment-trace-01](./runs/attachment-trace-01.json) | 上传六图、回读、尝试发送 | 原图校验通过，但发送被临时 409 拦截；未进入模型 | 否 |

早期接入排障不是召回实验。相关 HTTP 日志保留在 [attachment-trace-01.log](./runs/attachment-trace-01.log)，不再把代码修改树和单元测试数量当成实验结果。

以下第 9 节记录 chat-04 之前的目录整理，彼时只整理文件，没有重新调用模型。最新复跑另见 chat-04；迁移后的旧原始产物保持不变。

## 9. 目录整理与本地验证

沿用同目录 Grounding DINO 实验的主题索引和日期目录结构。本次迁移 17 个已有文件，新增主题索引和复跑脚本，共 19 个文件；没有删除原图或运行证据。README 和 manifest 迁移后修改内容，其余 15 个文件仅迁移、字节不变。

```text
AniVAG/
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [A] README.md                       ← 实验主题索引
            └── 20261016-mygo-image-origin/
                ├── [R] README.md                   ← 原顶层同名 md，迁移并按实验问题重写
                ├── [R] manifest.json               ← 迁移并更新最新记录和定位状态
                ├── [A] run.py                     ← 参数化复跑与离线样本校验
                ├── images/
                │   ├── [R] image-01.png
                │   ├── [R] image-02.png
                │   ├── [R] image-03.png
                │   ├── [R] image-04.png
                │   ├── [R] image-05.png
                │   └── [R] image-06.png
                └── runs/
                    ├── [R] preflight-01.json
                    ├── [R] attachment-trace-01.json
                    ├── [R] attachment-trace-01.log
                    ├── [R] chat-02.json
                    ├── [R] chat-02.sse
                    ├── [R] chat-02.log
                    ├── [R] chat-03.json
                    ├── [R] chat-03.sse
                    └── [R] chat-03.log
```

整理前快照位于 `/tmp/anivag-experiment-style-before/`。当时本地检查通过：15 份原图和原始运行文件 SHA-256 全部保持不变；manifest 当时指向 chat-03，真值字段仍为空；README 相对链接均存在；JSON 可解析；脚本 AST、`--help` 和 `--validate-only` 通过，离线验证结果为 6 张图片、`api_called=false`。chat-03 表中的 15 次检索、12 次成功取帧、32 帧次、19 个唯一 SHA、5 次缓存命中、6 次额度错误均从原日志重算一致。chat-04 完成后 latest_run 更新为 `runs/chat-04/run.json`。

复跑脚本通过 6 个 `httpx.MockTransport` 场景验证：完成、失败、中断、缺终态、助手仍处理、缺助手消息；每例均模拟六图上传和回读，无真实 API 或模型调用。独立复查未发现报告口径或复跑状态处理的阻塞问题。

目录整理时仍为 untracked，因此普通 `git diff --stat` 和 staged diff 不显示这些文件；当时变更量用整理前快照比较。目录整理没有改业务代码、项目依赖、API、数据库或配置，也未重启、迁移、commit 或 push。后续 chat-04 按用户授权重启 API/Consumer 并真实问答，单独记录在对应运行报告中。

## 10. 本轮三图分组产物

chat-06/07 按用户“六张图分两次发”的要求执行；两组各三张原字节、独立新会话，同时运行。分组清单到实际上传的编号映射、6 张原图及 6 张保存原帧的 SHA、JSON 解析、Markdown 链接、终态、敏感模式检查均已验证。终态之后补采没有发现晚到子工具错误。

相对于本轮开始，修改 3 份已有索引／清单（+24/-12 行，不计本节），新增 24 个分组与运行产物，约 3.01 MB。目录原本 untracked，普通 Git diff 无法反映本轮增量；对比副本保存在 `/tmp/anivag-split-before/`。没有 staged 变更，没有修改生产代码、runner、模型配置或超时；未重启、迁移、commit 或 push。

```text
AniVAG/
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [M] README.md
            └── 20261016-mygo-image-origin/
                ├── [M] README.md
                ├── [M] manifest.json
                ├── manifests/
                │   ├── [A] images-01-03.json
                │   └── [A] images-04-06.json
                └── runs/
                    ├── chat-06/
                    │   ├── [A] README.md
                    │   ├── [A] observations.json
                    │   ├── [A] run.json
                    │   ├── [A] run.log
                    │   ├── [A] run.sse
                    │   ├── [A] runtime.json
                    │   ├── [A] trace.log
                    │   ├── [A] visual-review.json
                    │   └── verified-frames/
                    │       ├── [A] image-01.jpg
                    │       ├── [A] image-02.jpg
                    │       └── [A] image-03.jpg
                    └── chat-07/
                        ├── [A] README.md
                        ├── [A] observations.json
                        ├── [A] run.json
                        ├── [A] run.log
                        ├── [A] run.sse
                        ├── [A] runtime.json
                        ├── [A] trace.log
                        ├── [A] visual-review.json
                        └── verified-frames/
                            ├── [A] image-04.jpg
                            ├── [A] image-05.jpg
                            └── [A] image-06.jpg
```


## 11. 20 分钟复跑的变更与验收记录

本轮配置只改三项：`models.supervisor.request_timeout_seconds`、`agent_runtime.supervisor_timeout_seconds`、`agent_runtime.execution_timeout_seconds`，均为 600→1200 秒。模型绑定的超时也必须调整：Supervisor 原生会话包括等待工具的时间，Gateway 按绑定上限取最小值，仅改 Agent 总超时会保留 600 秒限制。没有引入新配置来源或修改业务实现。

相对开工前快照 `/tmp/anivag-20min-before/`：修改 1 个配置、3 个已有实验索引/清单，新增 24 个运行记录/核帧文件，约 3.59 MB；4 个已有文件为 +21/-21 行（不计本节）。生产逻辑代码 0 行、测试文件 0 个。小改动范围未扩大。

运行中另一项工作在 12:08:14 生成提交 `e906b60`，包含当时的共享工作区内容；本 Agent 未执行 commit/push。当前 `git diff --stat` / `--numstat` 对本轮配置为空，staged 为空，实验目录又被现有 ignore 规则排除，因此不能把当前 Git 状态当作“本轮没改文件”。以上增量依据修改前副本及本轮新目录计量，没有回滚或覆盖其他工作。

```text
AniVAG/
├── [M] config.yaml                         ← 三项超时 600→1200；API 重启加载模型绑定
└── engine/
    └── experiment/
        └── chat-image-origin/
            ├── [M] README.md               ← 最新实验状态
            └── 20261016-mygo-image-origin/
                ├── [M] README.md           ← 结果、历史比较和本轮验收
                ├── [M] manifest.json       ← 最新运行及同镜头交付状态；原图/gold 不变
                └── runs/
                    ├── chat-08/
                    │   ├── [A] README.md           ← 原图 1–3 的方法、结果、局限
                    │   ├── [A] observations.json   ← 按会话统计及最终交付核对
                    │   ├── [A] restart.json        ← API 重启实际过程
                    │   ├── [A] run.json            ← 原始请求、事件和持久化消息
                    │   ├── [A] run.log             ← 运行时间窗日志
                    │   ├── [A] run.sse             ← 原始事件流
                    │   ├── [A] runtime.json        ← 配置装配、PID、源码摘要和结束检查
                    │   ├── [A] trace.log           ← 仅本会话日志
                    │   ├── [A] visual-review.json  ← 来源帧、SHA、同镜头核对说明
                    │   └── verified-frames/
                    │       ├── [A] image-01.jpg
                    │       ├── [A] image-02.jpg
                    │       └── [A] image-03.jpg
                    └── chat-09/
                        ├── [A] README.md           ← 原图 4–6 的方法、结果、局限
                        ├── [A] observations.json   ← 按会话统计及最终交付核对
                        ├── [A] restart.json        ← API 重启实际过程
                        ├── [A] run.json            ← 原始请求、事件和持久化消息
                        ├── [A] run.log             ← 运行时间窗日志
                        ├── [A] run.sse             ← 原始事件流
                        ├── [A] runtime.json        ← 配置装配、PID、源码摘要和结束检查
                        ├── [A] trace.log           ← 仅本会话日志
                        ├── [A] visual-review.json  ← 来源帧、SHA、同镜头核对说明
                        └── verified-frames/
                            ├── [A] image-04.jpg
                            ├── [A] image-05.jpg
                            └── [A] image-06.jpg
```

验证包括：2 次真实三图问答均完成，6/6 原图上传回读一致，6/6 保存原帧 SHA 一致且经直接查看，6/6 最终卡片覆盖对应帧；10 份本轮 JSON 可解析，报告相对链接存在；本轮文件敏感模式扫描为 0；相关路径 `git diff --check` 通过。复用 runner、分组清单均与运行时保存的 SHA 一致。未增加/运行单元测试，因为本轮没有业务实现修改。两名独立审核分别核对两组最终持久消息、工具日志、卡片和已有原帧基线，无阻塞发现。

API 仍为新 PID27365、配置保持 1200 秒，Consumer 健康；没有改 API Schema、数据库 Schema、前端或输入协议。没有数据库迁移、解析数据清理、手动重试解析任务。最终结构化消息中视频状态 READY，但本轮没有操作浏览器实际点播；未把这一项写成 UI 播放验收通过。

观察到但本轮未改：Query 1 次 RANGE_TOO_LARGE，内部 Chat 8 次 INVALID_RANGE，Supervisor 2 次 INVALID_RANGE 和 1 次 INVALID_READ，均为 Canvas 文本读取反馈；三个搜索在各自会话内仍按顺序执行；API 优雅退出未完成。它们与本轮最终完成事实分开记录，没有顺手扩展为读取接口、并发或关闭机制重构。
