# 完整提示词同步后的EP04双组复测

> 2026-11-15验收更正：本轮共享ASR输入经过未经要求的重新聚类，115条原非空ID变null。以下生成记录保留历史事实，不能作为复用旧B0方案的验收。当前修复和离线证据见[恢复旧B0方法](b0-restoration-report.md)；本次没有新增生成模型调用。

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)，[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)。初次启动提交`dbdf75794930fb04c0a304d86ee959a400c34b59`，充值后续跑提交`f549b9c99aca120e43458bcae4483af8d8ac525a`。证据为[逐调用与阶段结果](prompt-synced-comparison.json)、[提示词预检](prompt-synced-preflight.json)、[核验摘要](prompt-synced-verification.json)与[中断及恢复记录](prompt-synced-recovery.json)。

**完整Prompt已同步、提交并核实实际投递；A／B均完成24/24 Understanding、22/22 Refine。充值后只继续B的14个未完成区间，新增15次Refine生成入口调用，全部返回并通过区间提交。两组开头仍误归高松燈，约292.5秒爱音台词最终均归千早愛音，人物质量尚未通过。** 本轮GPU未启动且未访问，CPU/prod与数据库未访问，没有部署Server。

## 同步与实际投递

此前45017773保存在PR #164，后续#165→#167→#170→#174→#176没有带入；之前通过的执行副本以本地补丁包含两句，因此旧结果不能作为完整既有Prompt回归。本次仅原样回补“Understanding不合并独立ASR cue／utterance”“合并交给Refine并保持不同／未确定说话人边界”，保留既有CONFLICT预填、原人物评分、画外音／未见其人先闻其声提示，没有另调新措辞。

完整prompts.py SHA为`68324f3aaf9b1245f1926e311a2746fb0068d19e4f530d989ad72747d6cda4d0`，整个解析Agent的.py／.txt文件与之前通过的执行树一致；实际渲染Refine指令前缀SHA为`37a9c0634d3a20522e4c985531b485c7bed30ed40c60da103a789343bd95dc99`，与之前通过的实际请求逐字相同。174项相关测试通过（0.77秒）。用上轮真实缺规则的Understanding和Refine请求做负例，都在模型调用前被拒绝，额外模型调用0次。

宿主要求Agent和执行脚本先形成干净Git提交；每次模型调用逐一检查必需规则、Prompt源码SHA和指令前缀并留证。本轮共113个生成入口（A48、B65，含修复及历史失败）全部通过投递核验，包含充值后15个入口。源码dbdf7579已推送到当前PR，修复已保存在Git。

## 整集结果与人物观察

同一EP04、ASR与1001窗初轮声学缓存，整集1fps、DeepSeek Flash low、temperature0、CAM++ CPU8。A仅匿名speaker ID，B在16/24真实提交时冻结名字参考并更新；两组各24份初始请求的图片SHA和文本集合相等。48份Understanding是本轮各自新生成的答案；本次续跑全部复用，没有新增Understanding或ASR，原ASR SHA不变。

| 组 | Understanding | Refine | 开头“不是小祥的错哦”（应为長崎そよ） | 约292.5秒台词（应为千早愛音） |
| --- | ---: | ---: | --- | --- |
| A：仅音频匿名预填 | 24/24 | 22/22 | 高松燈，错误 | 千早愛音，正确 |
| B：增加2/3具名更新 | 24/24 | 22/22 | 高松燈，错误；正确爽世预填被回改 | 两个最终表达均千早愛音，正确 |

B在16块时启动声纹准备，7.284秒后19块时就绪，第一次Refine在19块时发起，比最后Understanding提交早39.305秒。冻结参考175表达、154具名，对313表达评分。后8块只提供查询，未补充冻结姓名参考。

B开头top1爽世0.773162、gap0.096341、CONFLICT；speaker已经预填爽世，模型返回灯，完成整集后仍为灯。爱音台词后半声纹top1千早愛音0.772443、gap0.085733、CONFLICT，曾在已完成区间返回若葉睦，剩余区间续跑后最终归千早愛音。前半1秒片段top1为爽世、仅0.313873、gap0.080337，也分为CONFLICT，错误预填最终被Refine纠正为爱音。分差与预填不能当作校准的人物概率，也不能据开头单例将全部top1硬锁为正确答案。

本轮已确认完整Prompt实际送达，开头回改仍然发生。两组各只跑一轮，不能据此估计失败概率；只有上述两处具备人工确认的人物真值，全片身份准确率、声音分组纯度与端到端提速仍未验收。

## 中断与充值后恢复

B首次完成8个区间`[0,1,3,9,12,15,18,21]`。未提交的14个区间中，13个返回DeepSeek官方API余额402；第6区间返回max_output_tokens，部分正文19488字符。两类故障分别保留，不将输出限制归为余额故障。原进程退出后没有循环重试。

充值确认后使用[resume-pending-refine.py](resume-pending-refine.py)，仅恢复`[2,4,5,6,7,8,10,11,13,14,16,17,19,20]`，全部提交成功；不重跑原先8个成功区间，不扩充冻结16块，不重跑ASR或Understanding。B的24份初始Understanding SHA逐一与中断检查点相同，原A组结果保持原样。

本次恢复耗时323.731秒，新增15次Refine生成入口（14个区间，含一次修复），全部返回；没有再出现余额402或输出上限。第6区间在原模型、low档位及输出配置下通过，没有提高输出预算。历史14次失败仍留在调用台账：B累计65个入口，51次返回、14次历史错误；A累计48个入口全部返回。入口计数不是完整Provider账单，token与费用未完整采集。暂停与续跑使B耗时不适合与A作整集提速比较，323.731秒仅表示此次恢复耗时。

全部原始请求、返回、两组48份Understanding、B冻结16块与缓存评分、成功区间收据和最终版本保留在`outputs/verification/audio-first-prefill-prompt-synced-20261115/`。实际启动脚本为[live-ab.py](live-ab.py)，采集入口[collect-live-ab.py](collect-live-ab.py)使用`--a-run-name audio-first-prefill-prompt-synced-20261115`。私有媒体、向量与凭据不随Git提交。当前状态见[工作台](../../../../dev/workbench.html)。
