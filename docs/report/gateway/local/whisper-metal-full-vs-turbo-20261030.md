# whisper.cpp Metal：Full large-v3 与 Turbo 整集对照

日期：2026-10-30。关联分支：`fix/video-asr-recovery`；[Draft PR #18](https://github.com/owariband/Seekino/pull/18)。本次为独立实验，未接入正式解析任务。

## 问题与现状

需要确认 Apple M5 本地运行 full large-v3 的实际耗时、资源负荷，以及相对于 large-v3-turbo 的转写差异。沿用此前同一门 **Let's build GPT** 完整课程；用户已更正“133 分钟”的记忆，实际容器时长约 **116 分 20 秒**。本次从原片重新提取的完整音频为 6979.941625 秒。

此前 [Turbo Metal 实测](whisper-metal-benchmark-20261030.md) 为历史配置下的另一轮实验。本次重新运行两种模型，以本轮受控 A/B 结果计算速度比，不能拿旧 CPU large-v3 + VAD + 强制对齐的 76 分 32 秒直接作为同质量比较基线。

## 方案与实验控制

- 硬件：Apple M5、10 核 GPU、16 GiB 统一内存。
- 运行库：whisper.cpp，commit `284029c07012d2000941a08d38a31ffb71f4734e`；运行日志确认两边 `use gpu = 1`、Metal、Apple M5。
- 权重：`ggml-large-v3-q5_0.bin` 与 `ggml-large-v3-turbo-q5_0.bin`。这里的 Full 指完整 large-v3 架构，**两边都量化为 Q5_0，不是 FP16 测试**。
- 同一完整 PCM16 / 16 kHz / 单声道 WAV，同一二进制、英语、beam 5、best_of 5、temperature 0、关闭 temperature fallback、max_context 0、6 CPU 线程、单进程、flash attention。
- 每个模型新建进程、运行一次，顺序 Turbo → Full。模型先哈希校验，因此磁盘页缓存不冷。模型下载不计入推理时间；共享音频提取耗时 3.089 秒。
- 本次范围是纯转写：无 VAD、无声学强制对齐、无说话人识别；未写入正式任务、未重试历史 CPU 任务、未改变冻结配置。

## 方案验证与指标对比

| 指标 | Full large-v3 Q5_0 | large-v3-turbo Q5_0 |
|---|---:|---:|
| 整集转写请求耗时 | **495.115 秒 / 8 分 15 秒** | **182.360 秒 / 3 分 02 秒** |
| 模型服务启动 | 0.413 秒 | 0.413 秒 |
| 音频时长 / 请求耗时 | 14.10× | 38.28× |
| 进程 RSS 采样峰值 | **3.276 GiB** | **2.525 GiB** |
| GPU 设备利用率中位数 | 91% | 91% |
| CPU 占用中位数 | 13.9% | 12.5% |
| 原始分段数 | 2550 | 2476 |
| 现有 ASR 归一化校验 | 通过 | 通过 |

Full 耗时为 Turbo 的 **2.715 倍**，多约 **5 分 13 秒**；进程 RSS 采样峰值多约 **0.75 GiB**。两次推理进程均已正常退出。

资源按约 2 秒采样。RSS 是进程驻留内存观测，**不是独立 GPU 显存、进程全部物理占用或并发内存预算**；GPU 利用率来自整台设备计数，不是进程独占计数。CPU 使用 `ps %cpu` 口径，100% 约为一个逻辑核忙碌，不能把此百分比理解为全机 CPU 占比。未测功耗和每进程 Metal 内存归属。

系统开始时已有约 6.8 GiB swap；Turbo 期间系统 swap 从 6933.25 降到 5993.62 MiB，Full 期间降到 5116.25 MiB。采样内存压力保持 normal，未触发实验保护阈值。既有 swap、缓存和顺序是环境变量；本轮只证明单路可以完成，**没有测出并发上限**。

### 内部阶段计时

| whisper.cpp 计时项 | Full | Turbo |
|---|---:|---:|
| 音频特征 Mel | 1.832 秒 | 1.535 秒 |
| Encoder | 113.825 秒 | 97.326 秒 |
| Decoder：decode + batchd | **356.407 秒** | **62.294 秒** |
| Token 采样 | 17.702 秒 | 17.387 秒 |

日志确认两边 Encoder 均为 32 层；Full Decoder 32 层，Turbo Decoder 4 层。本轮速度差主要体现在解码计算。计时项不是 GPU 独占时长，也不等于功耗。

## 转写 diff 检查

拼接原始 segment 时保留模型原有空格，避免把跨段的 `embed` / `ding` 人为拆成不同词。之后忽略大小写和标点，使用 `difflib.SequenceMatcher(autojunk=False)` 对齐整集词序列。

- Full 20525 个归一化词，Turbo 20408 个；对齐一致 20020 个。
- 对称词级一致度为 `2 × 20020 / (20525 + 20408)` = **97.818%**。
- 共 **235 组差异**；Full 侧未匹配 505 词，Turbo 侧 388 词。
- 这是两份输出间的一致度，**不是 WER 或准确率**。数字书写、复合词分写和口头重复等也会产生差异；两边共有的错误不会体现在差异率里。

记录了 11 处文本检查意见，其中 8 处附原片截图。以下判断依据原片画面、代码与上下文，**没有逐字听校**；样本按潜在问题挑选，不是随机质量评测，不能据样本胜负计算整集模型排名。

| 位置 / 差异编号 | Full | Turbo | 检查结论 |
|---|---|---|---|
| 01:05 / D0001 | `ChessGPT`；`breaking news` | `chat gpt`；`breaking can use` | 画面同时支持 ChatGPT 和 breaking news：同一处两边各有错误。 |
| 31:58 / D0063 | `if we have the targets` | `if we have no targets` | 原片代码 `if targets is None: loss = None`，Turbo 更符合条件含义。 |
| 38:39 / D0078 | `bigram language model` | `background language model` | 画面为 `BigramLanguageModel`，Full 更准确。 |
| 49:41 / D0101 | `1 1 0 dot product` | `110.product` | 画面为矩阵行 `[1,1,0]` 和矩阵乘法，Full 更清晰。 |
| 53:48 / D0113 | `expo` / `expo 2` | `xbow` / `xbow2` | 画面变量为 `xbow` 和 `xbow2`，Turbo 更准确。 |
| 1:35:55 / D0187 | `an n-dot layer norm` | `nn.layernorm` | 画面正在输入 `nn.LayerNorm`，Turbo 更利于对应代码；口语拼写差异不等同于知识含义反转。 |

Full 与 Turbo 都有技术词、代码标识、数字或否定条件上的问题；现有抽查不支持“Full 在这门课上全面优于 Turbo”。可确认的结果是 Turbo 在本轮快约 2.72 倍，而质量差异需要用真实音频参考转录继续判断。

## 证据与复现入口

完整证据保存在本机主工作区：

`/Users/hypnos/workspace/agent/Seekino/outputs/verification/whisper-metal-full-vs-turbo-20261030/`

- `protocol.json`：原片、音频、模型、二进制 SHA-256 与完整参数。
- `summary.json`、各模型的 `result.json` / `resources.jsonl` / `server.log`：计时与资源采样。
- 各模型的 `raw.json` / `normalized.json` / `transcript.txt`：原始及归一化成果。
- `diff.html`：可搜索、按时间定位、筛选已检查位置、回听音频、打开原片截图的对照页。
- `review.json`、`diff-changes.json`、`diff-statistics.json`、`REPORT.md`：完整检查意见、差异与详细报告。
- `benchmark.py`、`compare.py`：本轮执行和对比脚本。`benchmark.py` 对已有结果有保护，不应为查看结果重复运行。
- `preview.py`：仅绑定 `127.0.0.1:18847`，支持音频 Range 请求。浏览器已验证筛选 D0063 与跳转至约 31:54（差异前 4 秒）。

原始大文件位于 Git 忽略目录，未随本报告入库；后续搬迁机器需同步这些实验资产。

## 验证边界

真实整集纯转写与现有归一化检查已完成；这不等于人工识别质量验收。未测 FP16、强制对齐、多路并发、完整课程解析索引检索闭环或 ASR 持久恢复。token 时间不等于声学强制对齐。实验结果不作为旧 large-v3 冻结任务的 checkpoint，也不代表 PR #18 已接入新功能。
