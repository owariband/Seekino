# 500ms NO_VOICE阈值：复用缓存后的整集Refine结果

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)，[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)。源码80bec738。证据：[实际结果](b0-500ms-result.json)，[1000ms基线实跑](b0-live-report.md)。

**NO_VOICE时长下限从1000ms改为500ms；本轮22/22 Refine完成，爱音段已正确，但开头仍被Refine从爽世回改为爱音。**

仅修改server/constants/parse.py的VOICEPRINT_B0_NO_VOICE_DURATION_MS。编码下限400ms、ACCEPT时长1500ms、gap门槛、旧B0算法与已确认Prompt均保持原样。999ms爱音台词由NO_VOICE变为CONFLICT，按现有Canvas规则预填千早愛音。

| 实际人物结果 | 1000ms基线B组 | 500ms本轮B组 |
| --- | --- | --- |
| 开头，应为長崎そよ | 長崎そよ，正确 | 千早愛音 |
| 约292.5秒台词，应为千早愛音 | 前半爱音、后半睦，后半错误 | 千早愛音 |

开头实际送入Refine的speaker为長崎そよ，模型返回千早愛音；声纹候选及分档与1000ms轮次相同。500ms已解除999ms台词的时长阻断，但Refine回改正确预填的问题仍然存在。本轮结果不作挑选或再次重跑。

本轮复用同一份ASR、24份初始Understanding、169条冻结参考与313条缓存评分，不重新编码或聚类。全片7条表达的分档改变，其中3条进入CONFLICT；全部22个Refine重新运行。24份Understanding文件SHA保持一致，新增Understanding、ASR和声纹编码均为0。

同一DeepSeek Flash low、temperature0，使用既有非流式恢复入口；生成入口24次，其中结果修复2次，状态{"returned": 24}。所有实际调用均核对完整Prompt。1000ms轮次主要使用流式，本轮使用非流式，且生成模型存在波动，因此不把本轮两处正确外推为稳定率或全片准确率。

原1000ms结果完整保留。本轮原始调用和返回位于outputs/verification/audio-first-prefill-b0-500ms-20261115；未新增单元测试。GPU、CPU/prod和数据库未访问，Server未部署。
