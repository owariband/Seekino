# 音频先算与2/3理解后具名预填：完整EP04回放

**当前更正（2026-11-15）：此前新增的固定窗重聚类偏离了复用旧方法的要求，已移出执行链。参见[恢复旧B0方法与离线验证](b0-restoration-report.md)。旧A/B输入使115条原非空ID变null，不能作为所要求方案的验收；本次生成模型调用0。下文保留历史实验口径。**

2026-11-15；分支 `perf/audio-first-prefill-evaluation`，[PR #176](https://github.com/owariband/Seekino/pull/176)。依赖[#174](https://github.com/owariband/Seekino/pull/174)，[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)，状态见[工作台](../../../../dev/workbench.html)。

本文依据[第一轮统计](audio-first-summary.json)、[具名预填对照](prefill-comparison.json)与[验证及成对比较](verification.json)，均为离线测量；ASR复用已有结果，旧Understanding按提交顺序回放。

2026-11-15追加的两组新Understanding／Refine实际调用，单独记录在[整集真实A/B对照](live-ab-report.md)。以下原离线数据和manifest保留原实验口径，不代替该次新模型结果。 后续[完整Prompt同步复测与充值恢复](prompt-synced-report.md)中两组均完成24份Understanding和22个Refine，开头身份仍未通过；该报告为本需求最新实模证据。

## 1. 实际测试范围

本条链路只测“整段视频音频先行 + 复用旧 Understanding”的离线流程，不包含新的模型解析，也不包含标准解析链路的实际加速测量。

- ASR：沿用已完成的 166 条 cue，总时长 989710 ms，未重新识别。
- 阶段 1：在 Understanding 之前，对整集已就绪音频进行计算；固定 CAM++ 编码器（CPU，8 线程），按 1000 ms 切窗、尾窗合并到 ≥400 ms，共 1001 个窗口；匿名凝聚聚类，余弦距离阈值 0.35。不使用名字或 Understanding 边界。ASR整集已经就绪，因此可读取整集音频；没有使用后续Understanding信息。
- 短 cue：36 条 <400 ms 被跳过，合计 4462 ms，占 989710 ms 的 0.45%（占 cue 数 21.7%）。
- 阶段 2：按真实 Understanding 提交时间取最早16个Chunk的人名锚点（200条具名表达，不是视频顺序前16块），再单独取全部24块（277条具名表达）；打分前排除查询自身、重叠与同一 ASR cue 的锚点引用。四种模式各自对全部 309 条表达做离线 Canvas 检查（308 条有向量、1 条过短）。
- 合成检查：`synthetic_wrong_label_prefill` 是往副本里注入错误名字，用来验证预填契约，不是真实失败。

阶段 1 实测耗时：

| 项 | 秒 |
|---|---:|
| 解码 | 1.249 |
| 编码（1001 窗口，8 线程） | 15.501 |
| 聚类与匿名预填 | 0.135 |
| 合计 | 16.886 |

### 阶段 1 的分组是碎片化的

| 指标 | 数值 |
|---|---:|
| 匿名组数 | 749 |
| 窗口数 | 1001 |
| 只含 1 个窗口的组 | 626 / 749 |
| 只含 1 个匿名 ID 的 cue | 17 / 166 |
| 含未知或混合组的 cue | 149 / 166 |

749 是匿名组数，不是 749 个人：平均每组 1.34 个窗口，83.6% 的组只有单个窗口；cue 层面只有 17/166 落到单一匿名 ID，149/166 为未知或混合（混合 cue 保留 null 聚合 ID，细分声音区间另行保存）。这说明当前分组很碎，尚未认证稳定身份，**阶段 1 的匿名分组尚未通过可用性/身份质量验收**，也没有被当作任何身份真值使用。

### 查询侧音频缓存只覆盖很小一部分

| 项 | 数值 |
|---|---:|
| 表达查询数 | 308 |
| 命中阶段 1 精确区间缓存 | 6 |
| 命中既往重复表达 | 4 |
| 实际原生编码调用 | 298 |
| 查询准备耗时 | 8.320 s |
| 同一批查询重跑的编码调用 | 0 |
| 与原始 PCM 完全相同的查询 | 5 / 308 |

共享整段 PCM 后，以“精确表达区间”为键只能命中 6 条，再加 4 条重复表达命中，其余 298 条仍要原生编码。因此查询侧有 8.320 s 的增量开销，不能记作 0；只有同一批查询重跑时才是 0 次编码。同时 303/308 条查询的 PCM 哈希与原查询不同，仅 5 条相同。

## 2. 实测对比

四种模式均覆盖全部 309 条表达（band：ACCEPT／CONFLICT可预填，null未满足自动预填分档；NO_VOICE是规则分档，可由时长、候选不足或分差触发，并不等于没有声音）：

| 模式 | Chunk数 / 具名表达数 | 引用样本数 | ACCEPT | CONFLICT | null | NO_VOICE | 实际预填 | 草稿改说话人 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| exact_expression_full（控制） | 24 / 277 | 308 | 45 | 67 | 116 | 81 | 112 | 78 |
| exact_expression_two_thirds | 16 / 200 | 216 | 75 | 72 | 94 | 68 | 147 | 88 |
| audio_window_two_thirds | 16 / 200 | 387–408 | 54 | 75 | 102 | 78 | 129 | 84 |
| audio_window_full | 24 / 277 | 540–561 | 35 | 65 | 123 | 86 | 100 | 73 |

与旧保存基线相比：

| 模式 | top1 变化 | band 变化 | 评分 + Canvas 秒 |
|---|---:|---:|---:|
| exact_expression_full（控制） | 0 | 3 | 1.914 |
| exact_expression_two_thirds | 44 | 77 | 1.286 |
| audio_window_two_thirds | 53 | 88 | 2.761 |
| audio_window_full | 34 | 76 | 3.704 |

两个2/3模式不可混用：`audio_window_two_thirds` 的引用池是 387–408，实际预填 129、改说话人 84；`exact_expression_two_thirds` 的引用池是 216，实际预填 147、改说话人 88。前者引用来自整段共享 PCM 上的音频窗口，后者引用来自精确表达区间。

目标个体只有两个有人工确认：

| 目标 | 期望 | top1（四模式） | 真实 band | 真实预填来源 | 合成错误标签修正 |
|---|---|---|---|---:|---|
| opening_soyo | 長崎そよ | 均为 長崎そよ | null / null / null / NO_VOICE | understanding | 3/4 |
| anon_introduction | 千早愛音 | 均为 千早愛音 | NO_VOICE / null / null / null | understanding | 3/4 |

顺序为 exact_expression_full / exact_expression_two_thirds / audio_window_two_thirds / audio_window_full。真实数据下两个目标原本就是正确名字，四个模式里 top1 仍是原名字；因为 band 为 null 或 NO_VOICE，这两个目标的自动预填没有触发，预填来源仍是旧 Understanding 名字。这只说明“保持正确”，不是本次 Agent 新增的准确率。合成检查共 8 例（4 模式 × 2 目标），6 例为 CONFLICT 并被语音默认名覆盖，2 例为 NO_VOICE 而保留注入的错误名；其中两个2/3模式各 2/2，仅是“CONFLICT 时用语音默认名覆盖”的契约证明，不代表真实身份正确率。

控制组（`exact_expression_full`，在共享PCM上重新编码精确表达区间）top1 变化 0、band 变化 3，说明链路基本可复现；它只给出音频提取路径改变后的控制参考，不能证明身份正确性。共享整段 PCM 使 303/308 条查询哈希改变，但哈希变化不等于结果变化。

## 3. 解读

- 阶段1的输入是已就绪整集ASR和音频，不读Understanding姓名或表达边界；匿名分组可利用整集声音，但尚未验证对新Understanding的实际帮助。
- 碎片化是当前主要问题：1001 个窗口被切成 749 个组，626 个组只有 1 个窗口，cue级仅17/166得到单一匿名ID。以现有形态，匿名分组不足以直接当身份使用，也未通过可用性/身份质量验收。
- 锚点变多不等于预填变多：从 16块的200条具名锚点增加到全部24块的277条时，两种模式的 ACCEPT 都下降（audio_window 54→35，exact_expression 75→45），实际预填也下降（129→100，147→112），更多结果落入 null/NO_VOICE。原因未验证，这里只记录方向。
- 草稿说话人变化数（78 / 88 / 84 / 73）不等于“改对了”——没有整集身份真值，不能声称这些改动都正确，也不能把 749 个匿名组当作人数。
- 音频复用收益有限：共享整段 PCM 只有 6+4 条命中，298 条原生编码，8.320 s 是实际增量，只有重跑同批查询才为 0。阶段 1 的 16.886 s 与查询准备 8.320 s 合计约 25.2 s（不含各模式评分与 Canvas），这些是实际付出的计算量，不能当作加速。
- 没有整集加速结论：旧 Understanding 是回放，Canvas 是离线检查，均不是标准解析链路的实际测量，不能据此推断端到端加速。

## 4. 未验证与下一步

未验证：

- 整集身份真值：只有 2 个目标有人工确认，749 个匿名组的纯度、以及任何一次说话人变化是否正确，都没有验证。
- 新 Understanding/Refine 行为：本次回放旧提交，没有测预填如何影响后续模型解析、草稿质量或端到端时延。
- 在线/流式场景：没有测未知未来标签（歌曲/重叠标记固定）、VAD、词级对齐，也没有测混合 cue 的处理质量。
- 阶段 1 匿名分组的可用性/身份质量验收：未通过，不表述为已可用。
- 整集加速：无 live 测量，16.886 s 与 8.320 s 只是离线单项耗时，不能合成加速比。

后续待确认项（本文不对实现架构做决定）：

- 做整集身份标注，按组和按 cue 评估匿名分组纯度与碎片率。
- 用新的 Understanding/Refine 实跑一遍，比较预填前后草稿差异，并区分“保持原本正确名字”与“修正错误名字”。
- 单独测量标准链路时延与编码成本，包括缓存命中率与重跑成本。
- 明确 NO_VOICE 的处理口径：当前会保留已有名字，也会保留合成注入的错误名。


本轮保留两阶段分离的方向，但第一轮1秒短窗＋默认聚类未通过稳定身份预填的效果验收，未接入Server。第二轮具名评分与实际Canvas预填已在全部309行通过合同检查；未认证这些改名都正确。2/3短窗参考补齐到全参考后，43条top1、68条分档改变；同一16块锚点下，短窗参考与精确表达参考相差25条top1、75条分档。晚到参考必须有评分版本与补映射合同，不能默默替换已消费结果。

脚本未修改Server、API配置或数据库事实，也未访问GPU及CPU/prod。DS Flash按精简指标起草报告，主Agent已按源码和实际artifact修正分档含义、锚点数量与控制组向量来源；没有将只读worker对批量音频的“未来信息”猜测当作已证实泄漏。

## 复现

本地依赖根仓库的venv、已部署的3D-Speaker及CAM++权重；私有媒体、ASR、查询向量不随Git提交。先执行[音频准备](prepare-audio.py)，再执行[具名评分及Canvas检查](evaluate-prefill.py)，最后执行[验证](verify-results.py)。需要显式提供共享数据根目录，第二轮必须在第一轮之后运行。

```bash
export PYTHONPATH=.:/Users/hypnos/workspace/agent/Seekino/outputs/local-models/campplus/3D-Speaker-065629c313eaf1a01c65c640c46d77e61e9607b4
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/audio-first-prefill-evaluation-20261115/prepare-audio.py --data-root /Users/hypnos/workspace/agent/Seekino --source "$EP04_MEDIA"
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/audio-first-prefill-evaluation-20261115/evaluate-prefill.py --data-root /Users/hypnos/workspace/agent/Seekino
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/audio-first-prefill-evaluation-20261115/verify-results.py --data-root /Users/hypnos/workspace/agent/Seekino
```

`EP04_MEDIA`设为本机既有原视频的绝对路径。原视频SHA为`50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`，ASR SHA为`2543d152519b6452d7b0f227db328894127a12fe48990dc45643c88d2e5750e7`。工作目录为本实验worktree根目录。重跑第一轮验证完整缓存命中、0次编码；第二轮同批查询重跑也为0次编码，证据见[缓存证明](cache-proof.json)与预填对照。报告中的耗时是单次本机测量，不构成p50/p95或标准服务加速比。
