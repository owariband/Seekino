# EP04 第二轮：取消人物名单、增加 SHOT 后的精简 JSON/XML 对照

日期：2026-10-29，实际调用约 19:50–20:02（Asia/Shanghai）。关联 [B07](../../../dev/problems.md#b07视频解析成本高昂且耗时超过片长数倍)、[解析性能 TRD §3.5](../../../design/03-product-reassessment/video-parse-performance-trd.md#35-第二轮取消名单并分离镜头的精简合同)、[首轮报告](parse-protocol-20261029.md)。

**最终决定：精简 Schema，继续使用 JSON，不采用 XML；质量优先，不再追求单 Chunk 30 秒。** 用户接受既有可靠并行解析策略及其更高人力成本，认为足以满足当前效率需求。本轮协议选型结束，生产接入尚未完成。 相同精简合同下，K3 的 XML 尝试耗时中位数比 JSON 高 21.97%，豆包高 18.21%。豆包 JSON 有一次合同失败，且各组均有说话人错误，因此没有选出可直接上线的方案。每组仅两次；这是本轮观测，不能推出 XML 在所有条件下必然更慢。

## 本轮改变与不变的条件

用户希望取消 `participants`、`visible_persons`，由专门镜头类型保存剪辑所需画面信息。本轮两种格式共同取消逐事件人物名单及根级 `persons`，增加 `SHOT`；保留对白 speaker、原文/译文、明确的动作主体、镜头描述、绝对整数毫秒及左闭右开范围。删掉的名单不再计为质量缺失。

实验使用 [CompactTimeline 与共享解码器](../../../../evals/parse_compact_protocol.py)，由 JSON/XML 归一化为相同对象；JSON 也使用精简字段和语言默认值，不再携带完整生产 Schema。虚构样例已核验语义等价。`SHOT` 保存景别、构图、角度和镜头运动，`SCENE` 表示场景改变，`ACTION` 表示人物动作。没有要求模型强行凑齐所有类型。

| 条件 | 实际值 |
| --- | --- |
| 素材 | EP04 前 `[0,60000)` ms；视频 SHA-256 `50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814` |
| 图片 | 首轮同一批 180 张 JPEG，3 FPS、1280×720、detail=high；逐帧字节、顺序和帧清单 hash 均相同 |
| 观察 | 复用首轮同一次 localhost whisper.cpp large-v3-turbo Q5 的日语转写及人物背景；`ocr=null`，不重新调用 ASR/OCR |
| Kimi | 请求及返回均 `k3`；Coding Responses API，reasoning effort=low；仍非 non-thinking |
| 豆包 | 请求及返回均 `doubao-seed-2-1-pro-260628`；Ark Responses API，thinking.type=disabled；reasoning tokens 全为 0 |
| 请求 | 直接一次输出，零 Canvas tools、普通文本、无 provider JSON Schema 强制通道；两组均本地严格校验 |
| 顺序与预算 | 每个模型 JSON→XML→XML→JSON，独立会话、串行；共 8 次，30000 output tokens 上限、600 s 截止、SDK 重试为 0；无修复/fallback/补跑 |
| 质量检查 | 调用前冻结新版检查表，包括已知约 47 秒爽世画外音；检查表不发送模型，不补答案到 Prompt |

两组只有格式表达及相应说明不同，事实任务与默认值相同；格式说明长度仍有差异，XML 输入比 JSON 多 44 个 K3 token / 43 个豆包 token。对照未控制 provider 缓存、排队或预填充，不能称为冷启动实验。

本轮仍是 **Understanding 协议实验，未经过本地 Seekino API 的完整生产链路**。新增 SHOT 暂存实验 `CompactTimeline`，没有伪装为旧 SCENE，也未声称已兼容生产 Canvas。生产枚举、名单消费、出镜证据条件、旧数据/索引与宿主提交仍需专门接入验证。

## 八次实际结果

总耗时从本地请求组装开始，到完整响应、解码及合同/范围校验结束；失败计到校验报错。首正文排除 reasoning；正文时段为首个到末个 output_text delta。抽帧和 ASR 不在该计时内。

| 执行顺序 | 总耗时 s | 首正文 s | 正文 s | input / cached tokens | output / reasoning tokens | 合同校验 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| K3 JSON-1 | 105.59 | 87.13 | 18.36 | 220057 / 0 | 3499 / 1727 | 通过 |
| K3 XML-1 | 201.36 | 185.64 | 15.65 | 220101 / 219648 | 6664 / 5277 | 通过 |
| K3 XML-2 | 87.78 | 67.57 | 20.18 | 220101 / 220101 | 1551 / 296 | 通过 |
| K3 JSON-2 | 131.46 | 119.20 | 12.19 | 220057 / 220057 | 4602 / 3223 | 通过 |
| 豆包 JSON-1 | 50.81 | 23.77 | 26.94 | 237514 / 235320 | 1543 / 0 | 失败：三条 SHOT 多出 description |
| 豆包 XML-1 | 54.39 | 29.73 | 24.65 | 237557 / 235320 | 1602 / 0 | 通过 |
| 豆包 XML-2 | 50.61 | 21.10 | 29.48 | 237557 / 237557 | 1588 / 0 | 通过 |
| 豆包 JSON-2 | 38.01 | 10.99 | 27.00 | 237514 / 0 | 1529 / 0 | 通过 |

八次均 completed，无传输失败或截断；标准 JSON/XML 语法均合法。实验合同通过率为 **JSON 3/4、XML 4/4**。豆包 JSON-1 在原始 timeline 索引 4、6、9 的 shot 额外生成 `description`，合同仅允许 dialogue 使用此字段；这属于格式合同错误，不等于那三段事实都错。原文未修补，失败未隐藏，耗时也不当作成功交付耗时。

| 探索统计，n=2/组 | JSON | XML | `(XML−JSON)/JSON` |
| --- | ---: | ---: | ---: |
| K3 全部尝试总耗时中位数 | 118.52 s | 144.57 s | +21.97% |
| K3 首正文中位数 | 103.16 s | 126.61 s | +22.72% |
| K3 正文时段中位数 | 15.28 s | 17.92 s | +17.27% |
| K3 非 reasoning 输出 token 中位数 | 1575.5 | 1321 | −16.15% |
| 豆包全部尝试总耗时中位数 | 44.41 s | 52.50 s | +18.21% |
| 豆包首正文中位数 | 17.38 s | 25.41 s | +46.24% |
| 豆包正文时段中位数 | 26.97 s | 27.07 s | +0.36% |
| 豆包输出 token 中位数 | 1536 | 1595 | +3.84% |

豆包 JSON 中位数包含一次无效结果；仅看合同通过样本则 JSON 只有 38.01 s 一次，XML 为 54.39 / 50.61 s，也不能据此推断可靠成功延迟。内容质量另见下文。未报告 p95/p99 或稳定性结论。

**主要等待不在本地解析 XML。** 本地解码/校验仅约 0.16–1.53 ms。K3 XML 两次首正文为 185.64 / 67.57 s，对应 reasoning 5277 / 296 tokens，差异远大于本地解码；虽然都近乎完整缓存命中，整体仍差约 114 s。豆包两种格式正文时段相近，差异主要出现在首正文之前。首正文包含上传、排队、输入处理及可能的 reasoning，服务端分段不可观测，不能全部算成 thinking。

缓存也不是延迟的充分解释：豆包最后一次 JSON 报 cached=0 却最快。本轮支持把后续诊断转向模型输出前等待与视觉输入预算，不支持声称“换协议必然提速”或“缓存保证更快”。

## 精简前后发生了什么

下表是首轮与第二轮的方向比较：相同素材/观察，**共同改动了人物名单、SHOT、字段压缩、镜头/动作分工及 Prompt**，并且缓存与运行窗口不同；不能把全部变化归因于删除两个字段。

| 模型/格式 | 首轮总耗时中位数 → 本轮 | 首轮正文中位数 → 本轮 | 非 reasoning 输出 token 中位数变化 |
| --- | --- | --- | --- |
| K3 JSON | 103.22 → 118.52 s | 33.48 → 15.28 s | 3161.5 → 1575.5，约 −50.2% |
| K3 XML | 100.17 → 144.57 s | 24.29 → 17.92 s | 2028.5 → 1321，约 −34.9% |
| 豆包 JSON | 98.73 → 44.41 s | 53.76 → 26.97 s | 3600.5 → 1536，约 −57.3% |
| 豆包 XML | 64.68 → 52.50 s | 26.95 → 27.07 s | 1660.5 → 1595，约 −3.9% |

精简 JSON 明显减少了正文生成负担；豆包在同样精简字段后，XML 甚至不再节省输出 token。K3 正文虽减少，reasoning/前置等待增长，整体并未加快。**“减少重复输出”有局部收益证据，“XML 是取得该收益的必要条件”没有得到支持。** 无合格全链路基线，因此不称整集优化完成。

## 内容质量：事实覆盖与类型分别检查

同日归因修正：下述画外音失败在 K3 与豆包均出现，且当前输入没有音频或说话人身份证据；它说明当前前置 Understanding 尚不能可靠完成该 case，不单独证明豆包 non-thinking 质量更低、Refine 必须后置或串行不能纠错。更长多场景验证按[主 TRD §4.7](../../../design/03-product-reassessment/video-parse-performance-trd.md#47-多片段质量验证计划分开模型能力身份补证与调度)分离模型、补证与调度。

豆包两轮原文的专项复核见[业务质量问题清单](doubao-nonthinking-quality-review-20261029.md)，保留实际片段、已知真值、业务后果和不能外推的边界。

由 Codex 对照代表帧、冻结观察与原文作非盲复核，逐次保存在 [quality-review.json](../../../../outputs/verification/parse-protocol-compact-shot-20261029/quality-review.json)。没有穷尽金标、独立盲审或精确声学边界，不报告完整准确率。名单缺失不计错；正文中的主体、speaker 和分段仍须正确。

| 检查项 | 本轮发现 | 判断 |
| --- | --- | --- |
| SHOT 与可见主体 | 八份原文都输出 11–15 条 SHOT，包含特写/远景/切镜及人物；125 条原始对白全部有姓名，包含无效 JSON 的 16 条 | 不再出现首轮某份豆包 XML 的整批 speaker 缺失；有姓名不代表认对人 |
| 动作事实 | 闭眼/低头、手部、爽世到来并坐下等主要事实仍在正文，未因删名单而整批消失 | 许多动作仍嵌在 SHOT，只有 5/8 份输出独立 ACTION；事实覆盖与分类分别报告 |
| 已知 47 秒目标 | 用户早已确认「祥ちゃんも悪くない／也不是小祥的错」属于长崎爽世，本轮 8/8 仍错归灯或爱音 | 关键身份目标 0/8 正确；格式变更没有解决画外音 |
| 跨说话人合并 | 豆包 JSON-2 将「因此让你们伤了心…」与上述爽世台词合为爱音的一条，区间 43.000–46.333 s | 最快、合同合法的 38.01 s 结果仍有业务错误，不能直接采用 |
| 54 秒另一句 | 八份原文均把「誰も悪くないんだよ／不应该归咎为谁的错」归给爽世 | 此句识别正确不能替代 47 秒目标 |
| 画面文字 | SUNWAY 在 3/8 份原文保留，ESP 也在 3/8；均嵌入 SHOT，无独立 screen-text | 直接读图可以取得文字，但覆盖不稳定；没有证明独立 OCR 可永久删除 |
| 场景/镜头 | 引入 SHOT 后不再把所有切镜都写成 SCENE，但 K3 JSON-2 仍把爽世视角变化写为 SCENE | 分类有改善方向，尚未统一；豆包 JSON-2 末段写三个人却称“双人中景” |
| 时间 | 七份合同有效结果均满足整数及 Chunk 范围；部分事件沿用 ASR，部分按字幕/帧调整 | 范围合法不能替代剪辑边界精度；最快 JSON 的混合台词区间还早于已知目标 |

已知目标依据：[B12 用户确认](../../../dev/problems.md#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属)。本轮检查表在调用前纳入此目标，与首轮事后补查的口径分开保留。Whisper 的 speaker_id 全为空，Understanding 只收到帧和文字观察，没有原始音频或后续身份核验；本轮不能判断音频/声纹方案的效果。

## 复现、验证与交付边界

原始目录：[parse-protocol-compact-shot-20261029](../../../../outputs/verification/parse-protocol-compact-shot-20261029/)。保留 preflight、环境、完整 Prompt、输入/源码 hash、四份源码快照、逐次 SSE/terminal/raw/result 或错误。首轮产物未覆盖。代码入口：[benchmark](../../../../evals/parse_protocol_benchmark.py)、[新合同](../../../../evals/parse_compact_protocol.py)、[测试](../../../../tests/evals/test_parse_compact_protocol.py)。

已执行（第一条会产生真实模型调用；已有结果目录不用于覆盖重跑）：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evals.parse_protocol_benchmark \
  --phase benchmark --contract compact-shot-v2 \
  --out outputs/verification/parse-protocol-compact-shot-20261029
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/evals/test_parse_output_protocol.py tests/evals/test_parse_compact_protocol.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python \
  outputs/verification/parse-protocol-compact-shot-20261029/analyze.py
```

- **58 项定向测试通过**：旧合同 24 项、新合同 34 项，覆盖 JSON/XML 同语义、换行/转义、未知 speaker、SHOT 保留、旧名单字段拒绝、时间及非法结构。旧 Canvas/Refine 内存测试仅覆盖旧合同，不证明 SHOT 已兼容生产。
- **223/223 证据一致性核验通过**：冻结输入、180 张图片、源码快照、八次 stream/terminal/raw 一致、七份结果重解码一致、一份错误原样复现。这是证据校验数量，不是质量通过项。
- 原始值：[measurements.csv](../../../../outputs/verification/parse-protocol-compact-shot-20261029/measurements.csv)；统计：[analysis.json](../../../../outputs/verification/parse-protocol-compact-shot-20261029/analysis.json)；一致性：[audit.json](../../../../outputs/verification/parse-protocol-compact-shot-20261029/audit.json)。

八次累计尝试耗时约 **720.00 s**。provider input 1830458、output 22578、total **1853036 tokens**；reasoning 10523 已含于 output，cached input 1368003 已含于 input。无额外修复调用；未取得账单，不换算金额。ASR 沿用首轮一次 5.33 s 的已加载测量，不能写成第二次转写或新鲜端到端用时。

用户最终选择精简 JSON 与 SHOT 方向，不采用 XML，不再自动追加协议或视觉预算实验，也不把单 Chunk 30 秒作为验收条件。效率沿用既有可靠并行策略，后续以内容质量、整集交付、恢复和成本评价；本次没有新增并行实测。画外音与跨人分段等质量问题、生产 Canvas tools 去依赖、下游兼容及新合同整集验收仍待完成。B07 中本轮协议选型记为 DONE，其余实施事项保持 TODO；详见 [最终取舍](../../../design/03-product-reassessment/video-parse-performance-trd.md#14-最终取舍精简-schema继续-json质量优先)。
