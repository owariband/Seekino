# Refine 画外音提示与 EP04 low 整集验证

2026-11-15；分支 `fix/refine-offscreen-voice`，[PR #170](https://github.com/owariband/Seekino/pull/170)，依赖 #167。设计见[TRD](../../../../design/03-product-reassessment/refine-offscreen-voice-trd.md)，当前状态统一见[工作台 B12](../../../../dev/workbench.html)。

**本轮 DeepSeek Flash low 主链完成，两个固定目标姓名 2/2 正确。** 两句的首次 Understanding 已正确，Refine 保留了姓名和一致的描述，没有发生正确身份被改错；不能算作预填纠错，也不能由此推导整集所有人物已正确。

## 改动与运行条件

仅在 Refine Prompt 增加一段：允许画外音、先声后像（未见其人先闻其声）和反应镜头；可见、张口或旧描述中的发言归属不能单独推翻 ACCEPT 声纹默认。保留受支持的视觉动作，同时复核描述中的说话人推断，证据不足保留 null。原有基于证据更正权限、B0、encoder、阈值、预填及提交规则保持。

用户最初请求 medium；[DeepSeek 官方](https://api-docs.deepseek.com/api/create-response/)说明 medium 映射 high，随后明确选择 low。本轮 Understanding、继承的 Refine 和同模型 fallback 均为 **deepseek-flash / low**。任务 v4 `df165ebc-540a-4a9d-bf84-b7469501564b`，1fps，整集1420127ms、24块；提交时间为2026-11-15 00:01:32（UTC+8）。

独立执行副本基于提示提交 `ceee38ea`，沿用上轮 `45017773` Understanding 分段和 `c777ef72` 媒体目录修复。直接回归379 passed；组合回归434 passed，11.73秒。371个运行源码文件前后SHA一致。ASR复用同一166条全片WhisperX结果，SHA-256 `02e0d35d94491a960109c58c287fe92ed5e54584d63d486fe22d41bbae8593a0`；Understanding、参考池、B0和Refine重新生成。

## 两句实际结果

| 目标句 | 首次 Understanding／本轮 B0 | Refine 与最终结果 |
| --- | --- | --- |
| 48.000–49.800s「祥ちゃんも悪くない」 | 長崎そよ；top1爽世0.726104，gap0.092380、band=null，不触发自动预填 | 实际输入、返回、最终均爽世；source=understanding，**姓名PASS** |
| 292.500–295.499s「長崎…長崎そよちゃんと…」 | 千早愛音；top1爱音0.815731，gap0.037056、NO_VOICE，不触发自动预填 | 实际输入、3次相关成功返回、最终均爱音；source=understanding，**目标句姓名PASS** |

爽世目标对应实际调用 `997c7cb9-cc9d-45ee-ab56-582a5d2fef5c`，输入和返回一致。最终描述为“画面外传来長崎そよ的声音，她替豊川祥子说话，认为祥子也没有错”，visible_persons=[]；没有再将发言归于灯。爱音描述为“千早愛音看着長崎そよ，迟疑地念出她的名字”，与姓名一致。这些是模型保存的描述，未额外认证所有画面人物和边界。

45次输入中，有目标摘录的共6次：爽世1次，爱音5次（3次返回、2次RefineInputChanged）；已逐项保存。DeepSeek Flash worker通过`delegate`的OpenCode后端核对最小目标摘要，主Agent复核实际输入/返回、最终提交及机械检查。worker将无自动预填时的输入姓名称为“预填”时不采纳该术语；本轮两个目标都没有触发自动预填。

## 整集与成本指标

| 指标 | v3 high | v4 low＋新提示 |
| --- | --- | --- |
| 固定姓名正确数 | 爽世错、爱音对，1/2 | 爽世对、爱音对，2/2 |
| 主链完成 | 24 Under／22 Refine／24当前索引 | 相同，ES 24 Chunk／631 Event |
| B0行／有效编码 | 337／336 | 309／308 |
| 实际Refine输入／返回留证 | 49／23 | 45／24；另17次异常、4次无outcome记录 |
| 首次版本实际预填 | 95条，含69 CONFLICT | 112条，含67 CONFLICT；零违例 |
| 原身份额外分数 | 47条 | 44条 |
| 新提示和low送达 | 无新提示；high | 45/45请求均有新段且绑定low，均无参考池原始资料 |
| 从创建到完成 | 23分59.848秒，含手动触发标准恢复 | 24分27.473秒，增加27.625秒；本轮无手动恢复，存在自动重试与尾部等待 |
| 解析／Refine／repair已报告input | 1,669,990 tokens | 1,635,786 tokens |
| 已报告output | 1,266,475 tokens | 961,838 tokens |
| 其中reasoning | 972,602 tokens | 684,623 tokens |
| cached input | 1,275,008 tokens | 236,800 tokens |

reasoning已包含于output，不重复相加。以上只汇总返回的provider usage，未回报的失败／取消用量不计入；不估算账单。模型档位、提示、Understanding、边界、参考池、缓存及重试历史均不同，不能将差异全部归因于新提示或宣称low稳定更快／更省。

运行中观察到两次连接中断后的标准重试及两次JSON修复。业务结果门禁保持；主链最终COMPLETE。observer异常15次为RefineInputChanged、2次ModelFailureError；4个输入没有outcome文件，不能据此推断主链结束后仍在运行，也不编造取消原因。模型用量记录的completed与observer返回属于不同层级，不混算。辅助摘要截至指标留证有2次失败，未纳入本次身份验收，完成状态未认证。

## 验收边界与留证

本轮固定两句姓名及描述一致性通过，新段和low实际送达通过，主链及索引通过。两句首次理解已对，因此没有复现v3“旧描述与正确预填冲突”的同一冻结输入，不能证明该单一反例已因提示而稳定消失。2/2也不能认证整集所有台词、混人区间、声纹纯度或时间边界。

[live-run.json](live-run.json)保存配置、来源、实际输入检查、目标分阶段结果、相关调用和指标。原始控制脚本、首次理解、完整实际请求、返回及worker摘要留在本机 `outputs/verification/ep04-offscreen-flash-low-20261114/`。准备目录沿用11-14后缀，实际整集运行日期为UTC+8的11-15。旧v1/v2/v3状态及v3的24份内容指纹不变；本机专用API19731已加载新提示，CPU/prod未访问。
