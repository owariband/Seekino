# 复用旧B0方法后的EP04整集实际结果

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)，[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)。初次启动源码259be770，API恢复入口5868c43b，最后一个区间非流式恢复6c6d7818。[实际请求、返回与阶段结果](b0-live-comparison.json)，[恢复检查点](b0-live-recovery.json)，[此前接线修复](b0-restoration-report.md)。

**A／B都已完成24个新的Understanding和22个Refine。B纠正了开头爽世台词，A仍错误；约294.5秒爱音台词的后半两组都仍错误，尚未达到两处身份都正确的目标。** 本报告依据实际模型返回，未用离线回放或单元测试代替人物效果。

## 运行条件与完成结果

同一完整EP04、1fps、DeepSeek Flash low、temperature0，ASR复用原缓存。第一轮以原非空speaker ID下的122个有效cue建立6组B0参考，对全部166条cue查询；原null仅作查询，原非空ID丢失0。A只使用第一轮匿名ID，B额外在第16个真实Understanding提交时冻结169条有效具名表达作为旧B0参考，对313条表达评分。没有重新聚类，编码、排除规则、评分、分档与Canvas均复用旧方法；已确认Prompt保持不变。

| 组 | Understanding | Refine | 开头台词，应为長崎そよ | 爱音台词，应为千早愛音 |
| --- | ---: | ---: | --- | --- |
| A：仅ASR ID的B0预填 | 24/24 | 22/22 | 千早愛音，错误 | 前半千早愛音正确；后半若葉睦错误 |
| B：增加2/3具名B0更新 | 24/24 | 22/22 | 長崎そよ，正确 | 前半千早愛音正确；后半若葉睦错误 |

两组各24种初始Understanding请求的文本与图片SHA集合相同。API失败导致原始请求重试次数不同：A35次、B33次，因此包含重试的请求multiset不相等，不将两者混写。两组答案分别新生成，没有复制其他轮次答案或彼此答案。

B在16块时启动准备，8.349秒后仍为16块时就绪；第一次Refine在17块提交时发起，比最后Understanding提交早95.332秒。这是本机实验宿主中的实际阶段重叠，不代表生产Server已接入或端到端提速。

## 两处目标的实际取舍

B开头初始Understanding为千早愛音，声纹top1長崎そよ0.776094，原人物爱音0.631244，gap0.144850、CONFLICT。Canvas预填爽世后，Refine最终保留，speaker_source为voice_default。A开头没有具名声纹更新，最终仍为爱音。

B的294500–295499ms后半台词，声纹top1其实是千早愛音0.737257，原人物若葉睦仅0.059659，gap0.171519。但该表达长度为999ms，低于旧规则no_voice_duration_ms=1000，分档为NO_VOICE，因此没有触发具名prefill，最终仍为若葉睦。这次观察到的后半失败发生在分档／预填门槛，不能写成模型拒绝了一个已生效的爱音预填。本轮没有更改旧阈值或再次调Prompt。

## API中断与续跑

首轮A完成16个Refine、B完成21个；A的1、3、5、6、8、13区间与B的16区间因模型连接失败未提交。复用全部48份Understanding与成功区间，只补这7个区间；A补完6个。B16又遇两次发送请求时对端断开，最后仅该区间显式改为stream=false完成；模型、low档位、temperature、Prompt与输出预算不变，实际binding和恢复历史均留证。该区间远离两处人物目标。

累计生成入口A66次、B62次，共128次，含修复和历史失败；99次返回、29次错误，全部核对完整Prompt投递。最终两组均有22份有效区间提交，当前错误列表为空，历史错误保留。入口计数不等于Provider账单，完整token与费用未采集；因网络中断和恢复，不以本轮墙钟时间作提速结论。

ASR原文件SHA不变，GPU、CPU/prod与数据库未访问，没有部署Server或构建索引。两处人工确认目标不外推为全片准确率或失败概率。当前状态见[工作台](../../../../dev/workbench.html)。
