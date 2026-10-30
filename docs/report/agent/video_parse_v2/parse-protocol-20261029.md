# EP04 单 Chunk：直接 JSON 与事件包装 XML 对照

日期：2026-10-29（Asia/Shanghai）。关联 [B07](../../../dev/problems.md#b07视频解析成本高昂且耗时超过片长数倍)、[解析性能 TRD](../../../design/03-product-reassessment/video-parse-performance-trd.md)。

**决定：保留 XML 研究候选，暂不替换生产协议。** K3 的尝试耗时中位数下降 2.96%，未达到预设的探索门槛 10%；豆包下降 34.49%，但包含失败调用、缓存差异和明显内容退化，不能计为同质量提速。完整 JSON 也有质量及领域校验问题，本轮没有选出可直接上线的方案。

本轮只运行 Understanding 协议实验。宿主提供固定图片、本地 Whisper 转写和人物背景，模型一次输出，无 Canvas tools、无 Refine、无数据库/索引提交。实验直接调用 provider Responses API，**未经过本地 Seekino API 的生产解析链路**。生产模型配置、Canvas 流程和业务数据未切换。

## 输入、参数与复现

| 条件 | 实际值 |
| --- | --- |
| 原视频 | `assets/library/sources/mygo/[Nekomoe kissaten][BanG Dream! It’s MyGO!!!!!][04][1080p][JPSC].mp4` |
| SHA-256 | `50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`，与旧记录相同 |
| 范围与帧 | `[0,60000)` ms，180 张 JPEG，3 FPS、1280×720、detail=high；字节、顺序及标称采样时间两组相同 |
| ASR | localhost whisper.cpp，large-v3-turbo Q5，日语、max_context=0、best_of=5、beam_size=5、6 threads，Metal/Accelerate |
| OCR | 用户选择关闭独立 OCR；两组均 `ocr=null`，由 Understanding 从图像读取字幕及画面文字 |
| 人物背景 | 既有资料中的外貌事实及简体人名；去掉旧工具、相对秒制说明，不注入质量检查答案 |
| Kimi | 请求及返回均 `k3`，`https://api.kimi.com/coding/v1`，reasoning effort=low；不是 non-thinking |
| 豆包 | 请求及返回均 `doubao-seed-2-1-pro-260628`，Ark `/api/v3`，thinking.type=disabled；四次 reasoning tokens 均为 0 |
| 输出 | 普通文本，无 provider JSON Schema 约束；JSON 使用完整领域 Schema，XML 使用事件包装合同，均本地校验及稳定排序 |
| 预算及顺序 | 每模型 JSON→XML→XML→JSON，串行独立会话；每次 30000 output tokens 上限、600 s 总时限、SDK 重试为 0；共 8 次 |
| 环境与代码 | Apple M5（Metal 日志确认）；Python/SDK/OS 版本见 environment.json；Git HEAD、dirty diff 摘要、源码与输入 hash 见 protocol.json，三个相关源码快照保存在证据目录 |

JSON 与 XML 的语义任务相同，但字段要求及表达不同：XML 省略派生字段，并允许省略部分属性；因此比较的是整个输出协议，**不能把差异全部归因于 XML/JSON 语法**。本轮没有同字段精简 JSON 对照。

实验脚本：[parse_protocol_benchmark.py](../../../../evals/parse_protocol_benchmark.py)。代码不供生产模块导入。原始证据目录：[parse-protocol-20261029](../../../../outputs/verification/parse-protocol-20261029/)，包括媒体/观察 hash、两个完整 Prompt、逐次 SSE、原文、terminal、usage、归一化结果或错误、质量检查表、源码快照。

已执行命令（`media` 创建新目录；`observations` 拒绝覆盖冻结观察；`benchmark` 保留已有尝试）：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evals.parse_protocol_benchmark \
  --phase media --out outputs/verification/parse-protocol-20261029 \
  --video 'assets/library/sources/mygo/[Nekomoe kissaten][BanG Dream! It’s MyGO!!!!!][04][1080p][JPSC].mp4'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evals.parse_protocol_benchmark \
  --phase observations --out outputs/verification/parse-protocol-20261029
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evals.parse_protocol_benchmark \
  --phase benchmark --out outputs/verification/parse-protocol-20261029
```

`observations` 需要先启动本地 Whisper，实际参数保存在 [whisper-launch.json](../../../../outputs/verification/parse-protocol-20261029/whisper-launch.json)。权重 SHA-256 为 `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`，whisper.cpp commit 为 `284029c07012d2000941a08d38a31ffb71f4734e`。没有修改全局 ASR binding。

## 全部调用结果

计时从本地请求组装开始，到完整响应、解码及领域/时间校验结束；失败记录到校验报错结束。首正文不包含 reasoning。正文耗时为首个到最后一个 output_text delta。ASR、抽帧、编译和模型加载不计入 Understanding。

| 按执行顺序 | 总耗时 s | 首正文 s | 正文 s | input / cached tokens | output / reasoning tokens | 协议及领域校验 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| K3 JSON-1 | 111.80 | 77.41 | 34.29 | 222420 / 0 | 3349 / 29 | 通过 |
| K3 XML-1 | 98.34 | 75.54 | 22.64 | 219977 / 219392 | 3246 / 1194 | 通过 |
| K3 XML-2 | 101.99 | 75.96 | 25.94 | 219977 / 219977 | 3054 / 1049 | 通过 |
| K3 JSON-2 | 94.65 | 61.93 | 32.67 | 222420 / 222420 | 3033 / 30 | 通过 |
| 豆包 JSON-1 | 131.33 | 70.14 | 60.73 | 239949 / 0 | 4137 / 0 | 通过 |
| 豆包 XML-1 | 46.60 | 14.02 | 32.57 | 237430 / 236344 | 1923 / 0 | 通过 |
| 豆包 XML-2 | 82.75 | 61.42 | 21.33 | 237430 / 0 | 1398 / 0 | 失败：最后一个 event 同时含 action、dialogue |
| 豆包 JSON-2 | 66.13 | 19.26 | 46.78 | 239949 / 0 | 3064 / 0 | 失败：已知 speaker 不在 participants 中 |

八次 provider 均 completed、无截断或传输失败；**JSON 语法 4/4、XML 语法 4/4 合法，协议/领域最终均为 3/4 通过**。XML 的失败不是换行或标准解析器的问题，而是违反冻结的“一个 event 只有一个主类型”合同。JSON 失败也未自动补人物。没有修复后隐藏失败或追加调用。

原始值：[measurements.csv](../../../../outputs/verification/parse-protocol-20261029/measurements.csv)；聚合：[analysis.json](../../../../outputs/verification/parse-protocol-20261029/analysis.json)。变化率为 `(XML−JSON)/JSON`。

| 探索统计，每组 n=2 | JSON | XML | 变化与判断 |
| --- | ---: | ---: | --- |
| K3 全部尝试耗时中位数 | 103.22 s | 100.17 s | −2.96%，未达 10% 探索门槛；内容也未达标 |
| K3 正文时段中位数 | 33.48 s | 24.29 s | −27.46%；首正文中位数却由 69.67 增至 75.75 s |
| K3 非 reasoning 输出 token 中位数 | 3161.5 | 2028.5 | −35.84%；reasoning 中位数 29.5→1121.5，总 output 只由 3191→3150 |
| 豆包全部尝试耗时中位数 | 98.73 s | 64.68 s | −34.49%，两组各含一次失败，不能作有效结果提速 |
| 豆包正文时段中位数 | 53.76 s | 26.95 s | −49.87%，伴随合并台词、身份/分类字段遗漏 |
| 豆包输出 token 中位数 | 3600.5 | 1660.5 | −53.88%，不代表保留了相同信息 |

缓存无法控制：K3 首次 JSON 无缓存，其余接近全命中；最后一对均全命中时 XML 101.99 s、JSON 94.65 s，XML 仍慢 7.76%。豆包仅第一次 XML 命中约 23.6 万 token，第二次同 Prompt XML 却无命中；两次 JSON 均报告无命中，首正文仍相差约 51 s。不能从单次差异确定缓存、上传、排队或预填充各占多少。n=2 不报告 p95/p99，不宣称稳定降幅。

输入规模仍约 22万～24万 token，协议改动只减少约 1.0%～1.1% 的输入 token。本次证据更支持继续区分视觉输入预算与正文生成阶段，不支持将首正文等待全算作 thinking，也不支持持续压缩标签一定能降低总耗时。

八次尝试总耗时约 733.60 s；provider 用量共 input 1839552、output 23204、total **1862756 tokens**，其中 reasoning 2302 已包含于 output，cached input 898133 已包含于 input。失败调用用量保留。未取得实际账单，不换算金额。

## 内容质量与兼容边界

调用前冻结的 [检查表](../../../../outputs/verification/parse-protocol-20261029/quality-checkpoints.json) 覆盖连续场景、三人身份、低头/闭眼动作、爽世入场、SUNWAY 文字、约 54 秒台词、字幕分类、合法范围。输出由 Codex 对照代表帧及原文检查，**不是独立盲审，也不是穷尽金标**。逐次记录见 [quality-review.json](../../../../outputs/verification/parse-protocol-20261029/quality-review.json)。

| 发现 | 证据与影响 |
| --- | --- |
| K3 XML 的人物参与字段遗漏 | 两次分别有 13/30、11/28 个 event 的 participants 为空，主要是非对白事件；其中正文已明确命名人物。JSON 两次为空的数量均为 0。不能简单由 visible 补 participants，两者语义不同 |
| 豆包 XML-1 身份严重缺失 | 10 条对白全部没有 speaker，15 个 event 全部没有 participants；正文已经多次写出人物在说话。未知身份在 Schema 中合法，因此结构通过没有拦住语义退化 |
| 豆包跨说话人合并 | XML 两次都将 34～48 s 的多句台词合成一条，包含已确认属于另一人的短句；JSON-2 也存在类似合并。不能用对白数量下降解释为格式压缩 |
| 文字覆盖不稳定 | K3 JSON-1 将 SUNWAY 和 ESP 输出为 ON_SCREEN_TEXT；K3 JSON-2 只保留 ESP；K3 XML 两次均没有 ON_SCREEN_TEXT，其中一次仅在场景正文保留 SUNWAY。豆包两次 XML 均漏 SUNWAY；JSON-2 在描述中保留 SUNWAY SUNWAY/ESP，但该结果领域失败 |
| 分类仍需完善 | K3 多次按切镜生成 SCENE；豆包有将相机运动写成 ACTION、将低头/闭眼只放在对白描述中的情况。不能只数 ACTION/SCENE 判定理解完整 |
| 已知画外音身份未解决 | 复核时查到旧项目中用户已确认：47～49 s「祥ちゃんも悪くない／也不是小祥的错」属于长崎爽世，见 [B12 原记录](../../../dev/problems.md#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属)。本轮八份原文均未正确归属该句，错认、留空或并入爱音台词；这一历史检查是评审时补查，未倒填到事前检查表或 Prompt |

约 54 秒「誰も悪くないんだよ」与约 47 秒目标句是两句不同台词，前者认对不能替代后者。当前 Whisper 转写全部 speaker_id=null，Understanding 没有原始音频或后续声纹/Refine 证据；本轮不验证那些链路，也不能归因于 XML 单一因素。

两组都能从图像读出双语字幕，并将 ASR 的「クラシック」纠正为 CRYCHIC；这支持继续尝试无独立 OCR 条件，但画面文字遗漏说明**尚不能确认 OCR 可永久去掉**。PaddleOCR、DeepSeek-OCR-2 仍是独立选型候选，本轮均未调用或安装。

七种事件、绝对毫秒、排序与旧 Canvas 对象的转换已有 24 项离线测试通过；本轮六份有效结果可解码为领域对象。尚未验证宿主写 Canvas 后的持久化、Refine/Prefix、检索、取消与失败恢复。单 Chunk 结果不能作为整集加速或上传到可检索的端到端成绩。

## ASR 与证据核验

本地 ASR 从原视频前 60 秒直接提取 PCM，得到 18 条日语转写。模型已加载条件下，音频提取＋请求＋归一化约 **5.33 s**，其中提取 **0.33 s**，请求与少量处理约 **5.00 s**。权重下载、编译、模型加载不在此计时内；冷启动精确耗时未测，不能当 0。一次结果不证明长期吞吐或 Q5 精度等同完整精度。

转写仍有 CRYCHIC 等专名误识别、偏宽的时间段；ASR 只作为双方相同的观察证据。未做音频逐句真值标注，不报告精确时间误差或完整对白字符错误率。媒体准备早期生成过未使用的低帧率 MP4，媒体记录中的 3.21 s 包含该步骤；没有将它与各次 Understanding 拼成新鲜端到端耗时。

- 定向测试：`tests/evals/test_parse_output_protocol.py` **24 passed**，覆盖排版/转义、严格拒绝、排序、领域及时间范围、Canvas/Refine 内存往返。
- 冻结证据离线复核：[audit.json](../../../../outputs/verification/parse-protocol-20261029/audit.json) **44/44**，包括源码/快照/输入 hash、八次流文本＝terminal＝raw、有效结果重解码一致、两个既有失败原样重现。这是证据一致性检查，不是 44 个质量通过项。
- 所有原文与失败保持不变，无人工修复后提交、无无限重跑、无生产写入。

## 下一轮收敛方向

以下保留首轮结束时的判断。后续用户已确认删除名单并增加 SHOT，同字段复测结果见[第二轮报告](parse-compact-shot-20261029.md)；不再向新合同施加本节的 participants 要求。

1. 先收紧候选合同的身份与分段表达：已知 speaker/participants 必须明确输出；未知需要明确表达，不能因属性可省略而丢掉整批身份。保持 visible 与语义参与者分开，禁止宿主猜测补人。
2. 使用同语义字段的精简 JSON 对照，以区分“字段压缩”与“XML 语法”的贡献；质量检查需覆盖画外音、画面文字、动作和跨人台词拆分。先修共同质量问题，再扩大模型调用。
3. 对首正文等待单独做视觉输入预算实验，固定协议及检查表比较 FPS/分辨率等；缓存继续记录，不能承诺帧数减少即等比提速。
4. 达到可用质量及稳定收益后，再接入“模型直接输出 → 宿主校验 → Canvas.write → 原提交边界”，验证代表片段、相邻 Chunk 和整集。B07 保持未闭环。
