# DeepSeek Flash 整课费用与恢复验证（2026-11-03）

[PR #18](https://github.com/owariband/Seekino/pull/18) · [看板](pr18-kanban.html) · [性能 TRD §4.29](../../design/03-product-reassessment/video-parse-performance-trd.md#429-11-02deepseek-flash-整课费用对照计划)

**2026-11-03 11:34 CST 快照：同一 v11 正在解析，尚未完成整集。** 已提交90/117 Understanding、64/115 Refine、58/117索引，当前无终止错误，新增转写0。[进度原始快照](evidence/deepseek-flash-20261103/progress-snapshot.json)与[费用快照](evidence/deepseek-flash-20261103/cost-snapshot.json)分别保留采集时间。

任务 `93d60de9-b0ab-4aa6-aeea-342935e237b2`，课程 Let's build GPT，116分20秒／117 Chunk。当前代码 `9b0eb280`、API19621/PID5212；模型 `deepseek-flash`、Responses、`reasoning.effort=none`、stream=true、直连。全量图片、10/10/1并发和模型绑定不变；第六段执行纠正了此前漏接的完整ASR对白Prompt与时间权限，因此各段不混为单一不变Prompt的对照。ASR和说话人分组复用，新增转写0。

## 已定位的协议缺口

1. Responses没有接上共用100秒首SSE期限及完整请求诊断，现已补齐。策略不是Kimi专用；本轮覆盖两条解析入口，不代表整个Gateway所有工具循环都已覆盖。
2. strict Schema要求对象required列全声明属性，旧投影遗漏带默认值的speaker_source。首轮3请求在生成前400拒绝；适配器已补投影，业务Schema未变。相同Schema单次文本探针2.390秒通过，成本约0.002755元，单列而不当作课程质量样本。
3. 第二次执行在184.524秒后因响应前断连退出：APIConnectionError被包装成没有reason的DeepSeekModelError，绕过了Understanding已有的网络重试。Chat与Responses现共用SDK网络失败分类，保留400等永久错误。
4. 非法JSON被适配器包装后没有进入Refine原文反馈。现在保留ValidationError的原始JSON和字段／行列位置，交由现有有限纠错；配置中的显式fallback保留。

协议、模型、Understanding及Refine定向346项回归通过。HTTP/HTTPS首SSE期限和异常合同最终111项通过；曾有一项首次SDK事件解码超过0.5秒的测试预算，现只在“首事件解除期限”的成功用例留出2秒冷启动余量、正文延后2.5秒验证解除，生产100秒期限未改。整片对白权限贯通另602项回归通过。

## 当前费用与耗时口径

截至费用快照，185个有usage调用估算 **¥21.63012760**；另57个调用缺少usage，包含执行中、取消和错误记录，费用未知，不能记零。已知输入12,189,246、输出547,587、缓存输入1,511,040、reasoning 0。缓存是输入子集，reasoning是输出子集。费用按同一任务全部v11尝试的实际usage和请求开始时间价表归集，不包含旧Kimi任务、本地硬件成本或单列探针，也不等于账号所有消费。上午和夜间费率不同，见官方[价格规则](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。当前不是一集的最终价格。

第二次执行成功阶段中位：Understanding12.865秒（19份）、Refine14.819秒（6份）。第三次执行继续产出至30理解、17Refine、7索引后进程退出。此处只是已完成样本；不能用较短输出或结构通过代替人工内容覆盖／准确率验收。凌晨至上午的离线间隔不当作模型耗时，重启分段保存证据。

完整原始证据位于本机 `outputs/verification/lets-build-gpt-deepseek-flash-v11-20261103/`，前五段执行分别保存在attempt-1至attempt-5，根目录是第六段进程恢复。报告将在终态补充费用、质量检查和完整链路验证。

## 11:04 越界失败与既定合同接通

纯排序修复65c2aa24后，sequence30通过，继续到31理解／28Refine／28索引。sequence31输入的ASR原始cue为[1917740,1920300)，模型对白保留其结束时间，但Understanding仍按Chunk[1860000,1920000)拒绝，错误是`timeline event falls outside the authorized media time range`。之前已决定“整片ASR对白可跨Chunk”，本次是正式接入遗漏，不属于网络异常或模型自造300ms。

不采用固定容差或强行裁剪；9b0eb280将宿主原片范围贯通到Prompt、Canvas及独立提交校验。视觉范围保持；非法原片时间仍在原纠错预算内返回完整候选和具体位置。原始完整ASR成果没有变化，继续同一v11保存的结果。本轮之后不能再宣称所有Prompt保持不变，完整课程和人工质量仍待验。


## 分段执行

| 执行段 | 加载代码 | 结果与边界 |
| --- | --- | --- |
| 1 | 79f96edc | 64.036秒、3次400，0理解；strict Schema投影缺失可选字段。 |
| 2 | aeaa028e | 184.524秒，19理解／6Refine／2索引；响应前断连未正确分类为可重试错误。 |
| 3 | 540c94ff | 继续至30理解／17Refine／7索引后进程退出；未取得精确退出时刻，不把离线时间算作模型耗时。 |
| 4，10:56恢复 | 540c94ff | 原存储接续过期租约，30理解／24Refine／10索引；sequence30三份逐字相同候选排序错误，2次原文纠错未恢复。 |
| 5，11:02恢复 | 65c2aa24 | 稳定排序使sequence30通过，31理解／28Refine／28索引；sequence31原始cue多出300ms触发Understanding遗留门禁。 |
| 6，11:16恢复 | 9b0eb280 | 完整对白权限接通，原cue真实提交；继续同一v11，没有新建解析版本。 |

当前进度JSON中的成功阶段统计跨多个执行段；模型请求时长与刷新事件只统计当前进程段。失败及取消单列，不累计并行请求时长冒充整课耗时。第六段修正了Prompt中的矛盾合同，各段不能视为单一不变Prompt的模型对照。

## 完整对白的真实提交

11:29 CST真实核对：sequence31首次新输出原样保留[1917740,1920300)，没有因这300ms再发纠错；MySQL正文version=3、timeline_index=9保留同一时间，ES已索引并匹配同一task／chunk／version／timeline_index。实际出站Prompt包含整片对白规则且没有旧全事件Chunk限制。

- [真实请求核对](evidence/deepseek-flash-20261103/dialogue-live-request.json)：调用5433b9ba-0e6c-4e14-868a-5d8c257faa6f，Flash none，首次正常请求，无纠错。
- [MySQL与ES身份核对](evidence/deepseek-flash-20261103/dialogue-live-index.json)：正文保留31:57.740–32:00.300；ES保存检索投影及身份，可信时间按该身份回源MySQL，不在ES另存一份时间。
- [三份排序失败样本核对](evidence/deepseek-flash-20261103/sequence30-sort-audit.json)：只稳定排序即通过原结构和时间门禁；没有更改事件内容或数据库。

## 验证边界

本次602项回归覆盖完整cue经Canvas／事务原样提交、原片／视觉越界拒绝、旧租约拒绝及实时理解的旧范围；78项排序检查包含在相关覆盖中，不把重叠测试相加。Gateway完整范围此前为1624通过、5失败、1跳过、1deselected，五项失败在未修改上游2a0aef06同环境复现，不标全绿。

早期输出抽查发现部分Understanding有ASR却仅输出ACTION，已检查的若干Chunk在Refine后补回对白；不能用非空timeline或速度替代完整覆盖／人工准确率验收。完整117块解析／索引、最终内容覆盖、检索回看追问及单集最终费用仍待完成。

本报告只发布精简证据；完整原始日志、model-trace、逐调用用量保留在上述本机目录，不提交凭据、账号余额、base64图片或模型推理正文。
