# 恢复旧B0方法：原ASR ID建库与全量查询

最新实模结果见[复用旧B0后的整集A/B](b0-live-report.md)：两组均24/24理解、22/22 Refine；B开头爽世正确，爱音后半仍被999ms的NO_VOICE门槛挡住。

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)，[主TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)，[工作台](../../../../dev/workbench.html)。本次修复同一实验链路的接线，没有修改Engine算法、Server行为或已确认Prompt。原A/B输入偏离了复用既有方法的要求，其请求与返回保留作历史证据，不能作为所要求方案的验收。

## 问题与偏差

此前prepare-audio.py把全部ASR区间切为1秒窗，将原speaker ID全部丢弃为label=None，再聚为749个新组。1001个窗口中57个来自原null cue。汇总回长cue时，任意多个组使整个cue变null；原166条ASR中130条有ID，前处理后仅17条有ID，115条从有ID变null。细分turn另存文件却没有传给Understanding。两组A/B共享此输入，均不代表“以原speaker ID建参考，再重判全部音频”的实现。

原ASR文件未改，SHA仍为`2543d152519b6452d7b0f227db328894127a12fe48990dc45643c88d2e5750e7`。此前报告的整集生成入口数和回改现象保持其实际输入下的历史事实，不能据此将失败归因为旧B0或完整Prompt的效果。

## 修复范围

- ASR阶段直接复用`VideoParseRuntimeService._embed_voiceprint_sample`与`_score_voiceprint_sample`，沿用B0长句编码、trim、余弦评分、目标／同cue／同PCM／重叠排除和原阈值。没有新聚类、投票或阈值。
- 原非空speaker ID作为匿名参考标签；只有有效向量进入参考池。全部166条cue都作为查询，原null只计算查询向量，不进入参考池，也不将查询预测追加为新参考。
- ACCEPT／CONFLICT按现有规则预填top1；证据不足和过短查询保留原ID并记录原因，原非空ID不会因汇总变null。原文本、词级对齐与时间不变。
- 2/3理解后的参考直接使用当时冻结的有效具名表达，逐表达调用同一个B0评分入口，再走现有Canvas；移除将名字绑定到新增1秒窗的支路。参考中的null和无向量项被排除。
- live-ab.py改用独立的`asr-id-b0-v1`产物，拒绝旧聚类产物。prepare-audio.py、verify-results.py的默认目录为`audio-first-prefill-asr-id-b0-20261115`，新结果不覆盖旧实验。

## 本次实际验证

证据：[ASR整集摘要](b0-asr-summary.json)、[旧基线复现与接线核验](b0-restoration-verification.json)。CPU8；本次生成模型调用0、ASR调用0，GPU／CPU-prod／数据库未访问，Server未部署。

| 指标 | 原ASR | 错误重聚类输入 | 恢复B0后的输入 |
| --- | ---: | ---: | ---: |
| cue总数 | 166 | 166 | 166 |
| 非空speaker ID | 130 | 17 | 133 |
| null | 36 | 149 | 33 |
| 原非空ID变null | — | 115 | 0 |

恢复后的参考池为6个原speaker ID下的122条有效cue，null参考0条；130条查询达到400ms编码下限，其中8条原null查询；全部166条查询都有评分或未评分原因。3条原null获得预填，其余保留null，不能为追求覆盖率强填。首次CPU准备7.706秒，其中编码6.633秒；同批缓存再运行0次编码。该时间仅为本机音频准备，不是整集生成链路提速。

原v4基线309条候选、gap、band、conflict及reason逐项完全复现。107项相关测试通过（0.81秒）。基于上一轮保存的24份理解和向量，构造24份修复后的Understanding请求，重评分313条表达，具名参考154条；旧聚类产物在进入实验宿主前被拒绝。该回放的开头Refine Prompt为51157字符，原错误输入为80000字符；输入与参考策略均变化，长度变化不能单独用作因果结论。

实际Canvas检查：47.5–49.5秒top1長崎そよ，CONFLICT，gap0.080725，预填speaker为長崎そよ；292.5–293.5秒NO_VOICE，保留千早愛音；294.5–296.5秒top1千早愛音，CONFLICT，gap0.101781，预填千早愛音。

本次没有新增Understanding或Refine调用。313条表达使用的是错误输入下已有的模型答案，只验证旧算法复用、证据排除和预填接线；不能宣称新Understanding已归对、Refine已停止回改或整集人物质量通过。原ASR长cue仍可能混人，匿名ID也不是人物真值。

## 复核入口

```bash
PYTHONPATH=. uv run --no-sync python docs/report/agent/video_parse_v2/audio-first-prefill-evaluation-20261115/verify-results.py --data-root /path/to/Seekino
uv run --no-sync python -m pytest tests/engine/parsev2/test_voiceprint_matching.py tests/agent/video_parse_v2/test_framework.py -m 'not live_agent' -q
```

verify-results.py只读取本机已有数据并构造请求／Canvas，不配置Gateway、不发模型请求、不启动编码。prepare-audio.py在没有新缓存时使用本地CAM++ CPU编码；其结果与历史原始文件分别保存。完整Prompt文件保持已确认的`68324f3aaf9b1245f1926e311a2746fb0068d19e4f530d989ad72747d6cda4d0`。
