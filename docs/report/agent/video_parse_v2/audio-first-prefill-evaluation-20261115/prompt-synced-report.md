# 完整提示词同步后的EP04双组复测

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)，[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)。本轮实际启动提交`dbdf75794930fb04c0a304d86ee959a400c34b59`，证据为[逐调用与阶段结果](prompt-synced-comparison.json)、[提示词预检](prompt-synced-preflight.json)、[核验摘要](prompt-synced-verification.json)。

**全部已确认Prompt已经同步并提交，174项测试通过。A整集24/22完成；B理解24/24完成，Refine8/22后尚有14个区间未完成：13个返回DeepSeek官方API的402 Insufficient Balance，1个达到输出上限。** 本轮GPU未启动且未访问，CPU/prod与数据库未访问，没有部署Server。

## 同步与实际投递

此前45017773保存在PR #164，后续#165→#167→#170→#174→#176没有带入；之前通过的执行副本以本地补丁包含两句，因此旧结果不能作为完整既有Prompt回归。本次仅原样回补“Understanding不合并独立ASR cue／utterance”“合并交给Refine并保持不同／未确定说话人边界”，保留既有CONFLICT预填、原人物评分、画外音／未见其人先闻其声提示，没有另调新措辞。

完整prompts.py SHA为`68324f3aaf9b1245f1926e311a2746fb0068d19e4f530d989ad72747d6cda4d0`，整个解析Agent的.py／.txt文件与之前通过的执行树一致；实际渲染Refine指令前缀SHA为`37a9c0634d3a20522e4c985531b485c7bed30ed40c60da103a789343bd95dc99`，与之前通过的实际请求逐字相同。174项相关测试通过（0.77秒）。用上轮真实缺规则的Understanding和Refine请求做负例，都在模型调用前被拒绝，额外模型调用0次。

宿主要求Agent和执行脚本先形成干净Git提交；每次模型调用逐一检查必需规则、Prompt源码SHA和指令前缀并留证。本轮98个生成入口（含修复及失败）全部通过送达核验。源码dbdf7579已推送到当前PR，修复不再只存在于运行副本。

## 本轮实际进度与人物观察

同一EP04、ASR与1001窗初轮声学缓存，整集1fps、DeepSeek Flash low、temperature0、CAM++ CPU8。A仅匿名speaker ID，B在16/24真实提交时冻结名字参考并更新；两组各24份初始请求的图片SHA和文本集合相等。没有重新ASR，原ASR SHA不变。

| 组 | 新Understanding | 已提交Refine | 目标观察 |
| --- | ---: | ---: | --- |
| A | 24/24 | 22/22 | 开头仍高松燈错误，爱音介绍最终千早愛音 |
| B | 24/24 | 8/22 | 已完成开头区间仍把爽世预填改回灯；介绍段当前前半爱音、后半若葉睦，尚非整集终态 |

B在16块时启动声纹准备，7.284秒后19块时就绪，第一次Refine在19块时发起，比最后Understanding提交早39.305秒。冻结参考175表达、154具名，对313表达评分。实际Refine指令前缀全部与之前通过轮一致。

B开头top1爽世0.773162、gap0.096341、CONFLICT；speaker已经预填爽世，模型返回灯。爱音介绍后半声纹top1为千早愛音0.772443，也曾预填，当前返回若葉睦。另有1秒片段top1仅0.313873却分为CONFLICT，分差与预填不能当作校准的人物概率。补齐旧规则没有自动消除已观察到的回改；这不构成B整集质量结论。全片身份真值、声音分组纯度和端到端提速仍未验收。

## 中断原因与续跑检查点

A已完整完成。B成功区间索引为`[0,1,3,9,12,15,18,21]`；未提交索引为`[2, 4, 5, 6, 7, 8, 10, 11, 13, 14, 16, 17, 19, 20]`。其中[10, 11, 13, 14, 2, 19, 16, 20, 17, 4, 7, 5, 8]明确返回官方API余额402，第6区间返回max_output_tokens，部分正文19488字符。输出限制不能归为余额故障，Provider token与费用未完整采集，不能据入口数估算费用。原进程已退出，没有循环重试。

两组48份Understanding、B冻结16块及向量、所有成功Refine的最新内容／版本／scope、请求和返回均保留在`outputs/verification/audio-first-prefill-prompt-synced-20261115/`。续跑入口[resume-pending-refine.py](resume-pending-refine.py)已通过`--inspect-only`核对24份B理解和8份成功区间；恢复时跳过已完成工作，只调剩余14个Refine，不重跑Understanding、ASR或成功区间，不补充冻结16块。充值后的实际模型续跑尚未执行；若再次402，该次续跑会阻止后续排队请求继续发给Provider。第6区间维持原模型和输出配置进行有界重试，如再次达到上限则单独报告，不能静默提高预算。

实际启动脚本为[live-ab.py](live-ab.py)，采集入口[collect-live-ab.py](collect-live-ab.py)使用`--a-run-name audio-first-prefill-prompt-synced-20261115`。私有媒体、向量与凭据不随Git提交。当前状态见[工作台](../../../../dev/workbench.html)。
