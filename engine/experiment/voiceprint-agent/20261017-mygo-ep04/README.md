# EP04 服务重启后的 VoiceprintAgent 实测

日期：2026-10-17。本文时间使用 Asia/Shanghai（UTC+8）；JSON 采集时间使用 UTC。

本轮实际执行了 Understanding → VoiceprintAgent → CAM++ 登记 → Refine 声音比对 → Prefix。目标「祥ちゃんも悪くない／也不是小祥的错」在 Understanding、首个 Refine 和 Prefix 后均为 `speaker=null`。链路确实调用并消费了声音证据，目标人物识别未通过。

整集于 **12:03:55 进入 `PARSING_FAILED`**，采集器以 `failed_and_drained` 正常结束。24/24 Understanding 完成，8/22 Refine 提交，9 个窗口因运行中的服务中断而租约过期失败，5 个保持 WAITING；索引 1/24，Prefix 游标为 1。整集功能验收未通过。

## 任务与部署

- 隔离测试 series：`bdbf28ae-5404-58e4-8c98-699433499ffc`。
- video：`0019c16c-a7b7-5f6c-8275-5019869c1207`，第四集。
- 本轮 task：`a99b4af4-4b66-43e5-90dc-b04b525b616a`，version **4**；上一轮 version 3 保留。
- 通过现有 `/api/uploads/reparse` 入口提交一次，返回 HTTP 200；请求/响应见 `reparse-request.json`、`reparse-response.json`。24 个 Chunk、22 个滑动 Interval；原系列 13 集没有加入本次请求。
- 源视频 SHA-256：`50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`。
- Gateway 新增 `campplus_local` source 和 `speaker` binding；CAM++ 直接走 CPU adapter。`config.yaml` 及冻结任务的 speaker timeout 均为 30 秒，CPU threads 为 6。冻结配置见 `requested-parse-config.json`，配置前后见 `gateway-config-before.yaml`、`gateway-config-after.yaml`。
- 正式模型为 `iic/speech_campplus_sv_zh-cn_16k-common`、revision `v1.0.0`、`campplus_cn_common.bin`（28,036,335 字节），输出 192 维。权重 SHA-256：`3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8`。
- 官方 3D-Speaker 实现 commit：`065629c313eaf1a01c65c640c46d77e61e9607b4`。源码和模型部署在被忽略的 `outputs/local-models/campplus/`，通过 `.env.local` 的 `PYTHONPATH` 装配官方实现。没有复制网络代码到业务目录。
- 使用 `uv sync --frozen --extra speaker` 安装可选依赖到项目 `.venv`，实际包括 torch/torchaudio 2.8.0、NumPy 2.2.6、SciPy 1.15.3；包清单前后快照见 `runtime-packages-before.json`、`runtime-packages-after.json`。未根据上游旧 requirements 覆盖项目主依赖；本次安装没有改 lock 文件。
- 本轮沿用 v3 的 ASR、Understanding、Refine、Prefix 和 Index 绑定，新增 Voiceprint 与 speaker embedding；Prefix 继续开启，`max_retry_count=0` 保持原配置。Voiceprint 生成模型为纯文本 Codex，执行超时 600 秒、工具上限 96。目标正确身份没有作为提示注入。

重启前等待已有 Chat 执行自然结束，且确认没有活动解析租约。旧 API 在 SIGTERM 和 SIGINT 后仍未退出，复核无活动任务后，对已验证身份的旧 API 进程组强制结束；Web 正常退出。通过现有本地服务模块启动新 API/Web，API PID 2197、Web PID 2340，健康检查确认 Parse Consumer 正常。MySQL、ES 未重启，没有运行迁移或 schema 初始化。见 `restart-before.json`、`restart-after.json`；不要把这次重启描述为全程优雅退出。

## 真实阶段证据

| 时间 | 事实 |
|---|---|
| 11:41:25 | 提交 v4 解析 |
| 11:43:59.530 | 首次 seq0 Understanding v1，目标 `47700–48700ms`，speaker=null |
| 11:46:37.276 | 恰好 12/24 个 Understanding 提交，Voiceprint 准备 active；22 个 Interval 均 WAITING |
| 11:48:16 | 登记爽世参考 |
| 11:48:26 | 登记立希、灯参考；此时已有 3 条参考但 ready=false，Interval 仍全部 WAITING |
| 11:48:29 | 完成工具成功提交 ready=true，registered=3、required=3 |
| 11:48:29.841 | 首次采集到 ready=true，此时 Understanding 为 20/24 |
| 11:48:30.134 | seq0 Interval 开始领取；晚于准备提交，早于全部 Understanding 完成 |
| 11:48:47 | 24/24 Understanding 已提交 |
| 11:49:09 | 目标台词实际调用 compare_speaker |
| 11:52:37.806 | seq0 Refine 提交，正文 v1→v2，目标仍 null |
| 11:53:03–11:57:24 | 已运行 API 开始关闭，随后出现新 API；详见运行中断证据 |
| 11:53:05.792 | 旧 API 等待连接结束期间，Prefix 后 seq0 仍 v2，目标不变 |
| 12:01:59 | 九个未提交窗口被回收为 worker lease expired |
| 12:03:55.391 | 主任务 PARSING_FAILED，租约和活动模型占用均回收 |

固定来源为已经提交的 12 块：seq `[0,1,2,3,4,5,6,7,8,9,13,18]`，均 v1。达到半数时按已提交来源固定集合，并非机械取 seq0–11。后续 Understanding 提交没有扩大本次来源集合。

固定 speaker 名单为：丰川祥子、千早爱音、椎名立希、长崎爽世、高松灯；登记 3 人，满足 `registered_speaker_count * 2 > speaker_list_size`。每人仅一条参考，没有为了此次验收人工增加已知声纹。

| 人物 | 参考区间（绝对毫秒） | 时长 | 已提交证据与 ASR 核对 |
|---|---:|---:|---|
| 长崎爽世 | 414900–416400 | 1.5 秒 | ACTION 描述祥子离开、爽世留在后方向她说话；本句追问转学；独立 speaker4 轮次，相邻句不进入截音 |
| 椎名立希 | 597100–600000 | 2.9 秒 | ACTION 描述立希撑桌后坐回，向灯解释在意她没来练习；独立 speaker3 轮次 |
| 高松灯 | 536200–540000 | 3.8 秒 | EMOTION 描述灯低头收肩后说演出没唱好；独立 speaker3 轮次 |

这些选择依据与实际来源记录吻合，目标 null 台词未进入参考。此项是 Understanding/ASR 证据审计，没有额外人工试听来校准首个身份标签；Agent 填写的 confidence 也不是经过校准的概率。

## 目标比对与写回

目标本轮为 **47700–48700ms，实际 1000ms**；PCM SHA-256：`ae7f3c555512e14fd62673cb38f2f0d4984155f3a405da394db98c3bc500824d`。

| 候选 | Cosine 相似度 |
|---|---:|
| 长崎爽世 | 0.433279 |
| 高松灯 | 0.411900 |
| 椎名立希 | 0.394218 |

前两名差值 **0.021379**。相似度不是人物身份概率，不能把 0.433279 解读为“43% 是爽世”。

Refine 的 v2 描述明确引用 1000ms、相似度和较小差值，结合缺少爱音参考等因素保留未知。因此，这次不能归因于工具未装配，也不能声称已正确识别爽世。Prefix 保留了该决定，目标最终的已观察版本仍为 null。

此前固定 16 条参考、每人 4 条、1700ms 目标的独立宿主实验得到爽世 0.82694462、灯 0.51958897；其参考集合、人物覆盖和目标裁切都与本轮不同，不能用这两个数字直接比较准确率，或把差异归因于单一因素。来源见 `engine/experiment/campplus/20261017-mygo-ep04/manifest.json`；旧临时宿主结果路径及 SHA 记录在 `docs/dev/problems.md` 的 B12。

## 实测发现与局部修复

统一 `model_usage` 账本记录一条真实失败的准备编码调用：`b54d1dee-7814-48c0-8a04-84b904d84ead`，stage=voiceprint、operation=speaker_embedding，11:47:38.794–11:48:15.844，37.050 秒。冻结超时为 30 秒。Agent 随后重试，并最终提交 ready=true，持久化阶段失败数仍为 0。见 `preparation-usage-interim.json`。

Codex 通用工具协议把异常转成失败回执交给模型，原 Voiceprint 宿主没有保留真实错误，导致模型内部重试绕过宿主有限失败预算。账本没有保存原始异常类型/文本；“编码超时”由 Agent 自述、耗时与源码推断，真实调用失败及其后仍成功 ready 则有直接账本和快照证据。

只在 `server/service/video_parse.py` 的准备宿主增加首次真实错误保存及完成提交保护，并在既有 `tests/server/test_video_parse_worker.py` 增加直接回归：

- 登记、截音、模型、名单或完成数据库回调真实失败后，阻止后续 finish，生成返回成功或泛化错误也不能覆盖原始原因。
- 登记与完成共享准备锁，避免失败结果尚未处理时并发完成；取消路径读取短锁，不等待原生音频推理锁。
- 正常取消、参数纠正反馈保留原语义；真实失败后的 pending 取消保留执行资源所有权，并标记实际失败交给已有有限预算流程。
- ready 已成功提交后的通知失败只记录日志，数据库仍是准入依据。

生产增量 +79/-27；测试 +273/-1。精确本轮补丁见 `error-fence-service.patch`、`error-fence-worker-tests.patch`，用于区分此前已有的工作区修改。

验证：worker 整文件 **107 passed**，声纹仓库与队列 **144 passed**；移除冗余锁后，预算/并发/失败后取消定向回归 **5 passed**；`git diff --check` 通过。worker 有一条既有 Starlette 弃用提示。

**v4 的声纹准备发生在 PID 2197 的修复前实现中。运行中服务又被其他来源关闭并启动新进程，续跑没有重新准备声纹；因此这轮不能验证修复后的准备失败路径。以上修复由回归测试验证，尚无修复后独立完成整集解析的证据。**

## 运行中断、恢复与最终核验

API 日志在 **11:53:03** 记录 PID 2197 开始关闭，11:53:04 等待连接结束，**11:57:24** 记录新 PID 27365 启动。这次关闭不在本 Agent 的解析执行操作中；现有日志不能识别发起者，也不能确定旧 PID 的精确退出时间。证据见 `runtime-interruption-audit.json`，包含日志行号和原/新父租约快照 SHA。

12:01:27.975 首次观察到父租约从 `local-consumer-2197 / lease1` 变为 `local-consumer-27365 / lease2`。新协调器看到已提交的 ready，保留同一正式声纹对象，未重新生成、登记或触发一整轮声纹准备。本轮只有 1 次准备生成、3 次登记、1 次成功完成提交，提供了真实重启后跳过已完成准备的证据。

12:01:59，seq `[1,4,5,8,9,11,12,13,14]` 的 Interval 以 `worker lease expired` 进入 PARSING_FAILED，retry_count 均为 1。原冻结 `max_retry_count=0`，失败预算耗尽。剩余一个执行窗口结束后，主任务于 12:03:55.391 收口为 PARSING_FAILED。父任务 last_error 为空，具体失败依据来自子 Interval 错误；处理计时标记不完整，不能用其 `processing_duration_ms=0` 表示没有实际执行。

| 核验项 | 结果 |
|---|---|
| Understanding | 24/24 已提交 |
| Voiceprint | ready=true，3/5 人，3 条参考 |
| Refine | 8 COMPLETE、9 PARSING_FAILED、5 WAITING |
| 当前版本已索引 Chunk | 1/24，包含目标 seq0 |
| Prefix | seq0 已完成，游标 1；全片未完成 |
| 目标最终已观察正文 | v2，speaker=null |
| 声纹 ready 前领取 | 采样未观察到，原子准入回归通过 |
| 活动解析租约/模型占用 | 本任务均为 0 |
| 临时媒体 | 本任务 work/video-parse-v2 目录不存在，无残留 WAV 或租约标记 |
| 原系列保留 | 13 视频、13 任务、312 正文版本/时间/规范 JSON SHA-256 全部不变 |
| 服务健康 | 12:05:49 API status=ok，Parse Consumer running |

正式参考均为有限 192 维向量，L2 norm=1。完整检查见 `final-verification.json`、`original-preservation-check.json` 和任务目录的 `summary.json`。

统一账本实际保存了 **288 条 CAM++ 调用结果：287 completed、1 failed**。其中准备阶段为 3 次成功、1 次失败，Refine 阶段为 284 次成功；日志包含 284 次 compare_speaker 结果。成功调用耗时最小 12ms、中位数 85ms、P95 1193ms、最大 9462ms。此计时包括可能的模型初始化、锁等待和 CPU 推理，不是严格隔离的纯推理 benchmark；进程中断且未写结束账本的调用不在统计中。首次失败为 37.050 秒，不能用成功调用中位数掩盖冷启动失败。

本轮同时验证了声音证据链路可执行、成功准备可在新租约下恢复；目标正确识别和完整解析成功均未通过。后续若研究准确率，应控制参考数量/身份依据/时长和目标裁切再做对照，不能直接把此前固定样本分数替代本轮 Agent 结果。

本次源码/配置与记录范围如下；快照及模型部署产物单列在实验与 ignored outputs 中，未纳入提交：

```text
AniVAG/
├── [M] config.yaml                         # 本次仅增加 CAM++ source 与 speaker binding，12 行
├── [M] .env.local                          # ignored，仅追加官方实现 PYTHONPATH，不复制凭据
├── server/
│   └── service/
│       └── [M] video_parse.py              # 首次真实失败保护，增量 +79/-27
├── tests/
│   └── server/
│       └── [M] test_video_parse_worker.py  # 实际失败/并发/取消回归，增量 +273/-1
├── docs/
│   └── dev/
│       └── [M] problems.md                 # 只补 B12 实测结果，保留并行 C14
└── engine/
    └── experiment/
        └── voiceprint-agent/
            └── 20261017-mygo-ep04/
                ├── [A] README.md
                ├── [A] watch.py
                ├── [A] error-fence-service.patch
                ├── [A] error-fence-worker-tests.patch
                └── [A] runtime-interruption-audit.json
```

此外本目录保留请求/部署/配置/包清单/数据基线及最终核验 JSON，任务子目录保留运行快照和最终账本。它们属于本次实验数据。已有 Chat、C14、Gateway 接入等工作区改动没有被归为本次源码修复；本 Agent 没有执行 commit/push。

收尾时共享仓库发生了本次工具操作之外的提交：12:08:11 的 `36dee83` 包含解析实现及本次局部修复，12:08:14 的 `e906b60` 包含问题文档；当时 HEAD 已由开测基线 e33ba85 前进到 e906b60。随后检查 staged 为空，Git 状态只显示 `.claude-tmp/`。这不代表测试启动时使用 e906b60，实测仍以 `source-sha256.json` 和生命周期快照为准。新 `.gitignore` 的 `**/experiment/` 规则已忽略本实验目录，报告和数据目前为本地成果，没有被这些提交纳入。原 TRD 在上一阶段已维护，其 design 目录同样仍被忽略，本轮没有强制纳入 Git。上面的 A/M 描述本次动作，不等于收尾时的未提交文件状态。

## 采集与复现边界

只读采集命令：

```sh
.venv/bin/python -u engine/experiment/voiceprint-agent/20261017-mygo-ep04/watch.py \
  --task-id a99b4af4-4b66-43e5-90dc-b04b525b616a \
  --max-seconds 5400 --poll-seconds 0.5
```

采集器通过本地环境取得连接，SQL 查询限制为 SELECT，任务/video/series/SHA 受固定校验；环境凭据没有复制到实验目录。任务目录保留 `progress.jsonl`、来源快照、逐版本 Chunk/Interval、`target-history.json`、正式 `voiceprint.json`，退出时导出终态及统一用量账本。`parse_model_call` 为旧表，本轮调用以 `model_usage` 为准。

采样不能保证捕获每个中间版本；first_observed_ready 是观察时间，不是事务时间。ready 前领取检查是采样证据，并辅以原子准入回归测试。重叠 Interval 因正文版本变化重新生成，600 秒是单次生成期限；外层另有从领取起算的 1800 秒总期限，续租及快照重算均不延长它。模型 completed 也不等于窗口已经提交。本轮声纹数据使用的是官方 CPU 路径，耗时不能全部归给 CAM++。

重启时源码 SHA 清单见 `source-sha256.json`，基准 Git HEAD 为 e33ba85、当时工作区有未提交修改。部署、包清单、冻结配置和原系列数据基线均单独留档。本 Agent 没有执行迁移、commit 或 push；收尾时共享 Git 状态的外部变化见上文。
