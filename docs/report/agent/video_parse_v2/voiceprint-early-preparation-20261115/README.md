# 声纹提前准备：2/3参考池、ASR ID与窗口放行对照

2026-11-15；分支 `perf/voiceprint-early-preparation`，[PR #174](https://github.com/owariband/Seekino/pull/174)。依赖[#170](https://github.com/owariband/Seekino/pull/170)，设计见[TRD](../../../../design/03-product-reassessment/voiceprint-early-preparation-trd.md)，当前状态见[工作台](../../../../dev/workbench.html)。

**本轮完成整集冻结实验，没有修改Server门禁、重启API或启动新一轮Agent解析。** 结果支持提前计算与后绑姓名的方向；不支持把16块参考直接称为全参考等价，也不支持把WhisperX ID硬当人物。本轮没有实测整集提速。

## 数据与可比条件

使用EP04 v4 `df165ebc-540a-4a9d-bf84-b7469501564b` 的24份首次Understanding、309条DIALOGUE、308个192维CAM++向量、166条ASR和22个窗口的真实时间记录。首次理解均在任何Refine提交前留存，基线两句最终姓名2/2，不代表全片真值。源artifact SHA-256为 `c3ca8d50c4e4f27ca4701cfc7557704d3ca9fd57e8431c9ef3e1fa9f409eb642`。

用当前Engine重新计算全参考309行，逐行断言candidates、gap、band、conflict、reason与原artifact完全相同，再改变参考池。16块按**实际提交时间最早的16/24**选取，不按视频前16块，不按216/308样本比例替代Chunk门槛。模型、向量、查询、阈值和排除规则保持一致；所有查询仍用全片确定的song／overlap标记，因此这是参考池单因素对照，不能认证未知未来标记的在线早期Refine。

## 2/3参考池映射全部表达

| 指标 | 全参考基线 | 最早16块参考 |
| --- | --- | --- |
| 查询 | 309行 | 同309行 |
| 可用编码样本 | 308 | 216，70.1% |
| 其中有名字、可进入参考筛选 | 277 | 200；无名样本仍被现有算法排除 |
| 初次理解标注名字种类 | 9 | 6；缺ういちゃん、マナちゃん、海鈴，未认证其真实身份 |
| top1变化 | 对照自身0 | 44/309，约14.2%；不是44条错误 |
| 分档变化 | 对照自身0 | 77/309，约24.9%；不是准确率变化 |
| ACCEPT／CONFLICT | 45／67 | 76／71 |
| NO_VOICE／null | 82／115 | 69／93 |
| 爽世目标top1 | 長崎そよ0.726104，gap0.092380、null | 長崎そよ0.726801，gap0.102202、null |
| 爱音目标top1 | 千早愛音0.815731，gap0.037056、NO_VOICE | 千早愛音0.814602，gap0.077271、null |

两句top1保留，只能说明此次冻结候选未变，未运行新的Agent终态。候选空间和参考质心都变了，ACCEPT从45增至76不能直接认作更准；爱音top1略降、第二名爽世分数下降更多，使gap扩大就是具体反例。[完整统计](frozen-comparison.json)。本次纯缓存向量评分约1.5秒／1.1秒，不包含音频编码，也不是整集加速指标。

## ASR后先计算：可复用范围与ID限制

166条ASR中有6个非空speakerId、36条缺失ID。当前缓存3467个字词带时间与score，没有字词speakerId。ASR长cue可能覆盖多个后续表达：已知爽世目标48.0–49.8s横跨SPEAKER_04与SPEAKER_03，爱音目标292.5–295.499s又处于SPEAKER_04。ID不能直接保证一个真实人物；不能用首次Understanding标签的混合度冒充声学纯度。

现有“精确区间→向量”合同下，仅50/308个已编码表达与某条ASR cue的起止完全一致；22个表达重叠多个非空ID，9个样本没有匹配到缓存cue。提前计算ASR cue向量并不能让其余不同区间的向量直接免算；其中还包含过长混声cue，需先处理可靠音频分段。此结论不否定提前准备PCM／特征或新的匿名声学分段方案。

另用现有 `score_clusters` 做完整308查询对照：按ASR ID的最大时间重叠固定分6组，295个样本归组、13个未分配；命名采用≥3独立cue、初始标签票纯度≥0.6，排除目标／同cue后投票和重算质心。分别允许全片名字锚点及仅16块名字锚点，两种情况都只有**8/308个查询产生具名候选，两句目标均弃权**。[分组结果](asr-id-grouping.json)。

这只测试了一种固定ID分组与保守命名规则，使用的仍是Understanding区间向量；不是ASR阶段实现，也不能否定独立于WhisperX ID的匿名声学聚类。初始标签票纯度不是身份真值或声纹校准概率。

## 时序与可提前范围

| v4实际时间／成本 | 数值 |
| --- | --- |
| 第16块提交 | 2026-11-15 00:06:25.522（UTC+8） |
| 第18块提交（旧75%阈值） | 00:06:33.744；比16块晚8.222秒 |
| 第24块提交 | 00:09:28.776；比16块晚183.254秒 |
| 完整声纹准备 | 39.462秒，包含裁音频、编码及评分 |
| 准备完成代理时刻 | 00:10:08.265，按start＋elapsed计算，未含最后发布开销 |
| 乐观窗口放行提前量 | 22个窗口为39.489～183.281秒；不是整集提速实测 |

[时序回放](release-timing.json)假设16块时启动，参考准备仍花原完整准备39.462秒；窗口自己的Understanding必须完成，且晚到查询向量被假定立即可用。未计晚到查询编码、song／overlap未来变化、资源争用、证据更新、重试或模型响应变化。真实最终完成还受模型长尾影响，不能拿早3分钟直接当作任务省3分钟。

## 决定与接入边界

1. **保留提前计算方向。** 音频区间和encoder固定时，向量计算不需要人物名字；可在ASR后对可靠音频段计算，或在每个Understanding提交时按实际表达区间异步计算。缓存应绑定源媒体、精确区间和encoder合同，名字与分档后绑定。
2. **2/3作收集启动条件，不直接截断最终参考。** 16块已有200个带名样本，但晚到参考应允许补齐；只提前编码、保留全片最终评分可以重叠计算，仍保留全片门禁，不能称Refine已提前放行。
3. **真正并行Refine需窗口就绪和参考版本。** 窗口自己的理解与对应音频证据齐后才可放行；评分绑定参考版本，晚到人物／参考变化后的补映射及过期结果如何处理须进入提交合同。否则会消费旧分数或反复重跑。
4. **ASR ID仅作线索。** 匿名声学计算与后续人名锚点分开，可靠分段及区间映射先验证；不将混声长cue或固定WhisperX ID直接升级为人物身份。

DS Flash worker仅核对精简指标，主Agent复核源码门禁、原始数据与实际Engine回放；未采纳将名字代理当真实纯度、将放行估算称近零／已实现端到端收益的推断。当前API仍是已验证的low链路，Server、数据库事实及旧任务未改写，CPU/prod未访问。

## 复现

需要本机保留的v4私有输入，以及 `outputs/verification/voiceprint-early-preparation-20261115/private/asr.json`。脚本不访问模型或数据库；只读输入，结果写实验outputs目录，向量与媒体不随Git提交。源码见[参考池对照](compare-frozen.py)、[ID分组对照](compare-speaker-ids.py)、[放行时序](replay-release-timing.py)。在本实验worktree根目录执行：

```bash
PYTHONPATH=. OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 /Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/voiceprint-early-preparation-20261115/compare-frozen.py --data-root /Users/hypnos/workspace/agent/Seekino
PYTHONPATH=. OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 /Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/voiceprint-early-preparation-20261115/compare-speaker-ids.py --data-root /Users/hypnos/workspace/agent/Seekino
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python docs/report/agent/video_parse_v2/voiceprint-early-preparation-20261115/replay-release-timing.py --data-root /Users/hypnos/workspace/agent/Seekino
```
