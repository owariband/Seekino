# 完整EP04真实Understanding／Refine两组对照

2026-11-15；[PR #176](https://github.com/owariband/Seekino/pull/176)、[TRD](../../../../design/03-product-reassessment/audio-first-prefill-evaluation-trd.md)、[工作台](../../../../dev/workbench.html)。原[离线回放](README.md)仅验证声学表示、评分和Canvas；本报告补充新模型实际消费输入的结果。证据：[主对照](live-comparison.json)、[核验摘要](live-verification.json)、[恢复执行的B组](recovered-live-comparison.json)、[同提示词不同返回](same-prompt-repeat.json)。

**两组均完整完成24个Understanding和22个Refine，但两组开头均误归高松燈，爱音介绍段最终均为千早愛音。** B组确实在16/24时开始准备，18/24时发起Refine，提前约80.95秒与剩余Understanding重叠；正确声纹预填仍被low模型回改。执行通过、提前触发通过与人物归因达标分别判断。

## 比较对象与输入控制

| 组 | Understanding输入 | Refine输入 | 实际完成 |
| --- | --- | --- | --- |
| A：音频先算一次 | 缓存ASR＋第一阶段匿名speaker ID | 新理解与原ASR；不提供2/3具名更新 | 24/24 Understanding，22/22 Refine |
| B：再加2/3具名更新 | 与A相同图片、ASR及提示词，独立调用模型 | 本轮最早16块人名锚点绑定预计算音频窗，逐表达预填及评分 | 24/24 Understanding，22/22 Refine |

完整EP04、1fps、DeepSeek官方Flash low、temperature=0；CPU CAM++为8线程。复用原166条ASR和第一阶段1001窗缓存，无ASR请求，GPU未启动且未访问，CPU/prod、数据库及旧任务未访问或改写。第一阶段749个匿名组仍未认证稳定身份，只有17/166条cue可填单一ID。

宿主复用正式ChunkUnderstandingWorkflow、IntervalRefine、Gateway与Canvas，以独立本机文件保存提交，未接入Server租约、事务或索引。两组各自的24份首次理解来自本轮新模型调用，没有导入旧Understanding答案或另一组答案。主对照24份初始请求按图片SHA和文本构造的集合相等；输出边界仍不同，不能据此宣称两组所有差异都由2/3更新引起。

B组冻结的0起算sequence_no为`[6, 1, 5, 3, 7, 2, 9, 4, 0, 8, 11, 12, 10, 13, 18, 15]`，按实际完成顺序而非视频前16块。冻结收据与本轮原始提交顺序和内容SHA逐一核对；182条冻结参考表达中158条有名字。后8块只补查询，不补参考姓名。绑定前排除查询自身、同ASR cue与重叠锚点，沿用B0评分及实际Canvas合同。

## 两个已知目标的真实流转

| 目标 | A首次理解 | A最终 | B首次理解 | B预填 | B最终 |
| --- | --- | --- | --- | --- | --- |
| 开头“不是小祥的错” | 高松燈 | 高松燈，错误 | 高松燈 | 長崎そよ，正确 | 高松燈，错误，refine_edit |
| 爱音介绍，约292.5–295.499秒 | 先爱音、后半误写爽世 | 千早愛音，正确 | 千早愛音 | NO_VOICE未自动改名 | 千早愛音，正确 |

A的开头范围为[48000,50000)，B为[47400,50800)；两组均覆盖已确认的[48000,49800)目标。爱音段在两组中均被拆为多条，具体区间保留在主对照JSON。两个检查点分别是已确认的人物名称，不是全片身份真值；不能外推整集准确率或随机失败概率。

B组开头实际送入Refine的候选：

| 人物 | 相似度 |
| --- | ---: |
| 長崎そよ | 0.764492 |
| 千早愛音 | 0.679857 |
| 棕发少女 | 0.620871 |
| 高松燈 | 0.592243 |

分差0.084635，band=CONFLICT。实际Refine输入speaker已经是長崎そよ，原理解高松燈及其分数也保留在逐句摘要中；模型返回高松燈，host提交为refine_edit。该错误发生在获得并预填正确候选之后。相似度与分差均不是身份概率。旧描述与新speaker的冲突仍存在；本轮没有独立消融确定模型回改的内部原因。

另一个恢复B组开头的相似度为爽世0.783359、原人物高松燈0.542816，也预填为爽世后被回改。它的两次Refine文字输入SHA完全相同：`5836086e0e196242b882a9a0023873d09d39761ab8392d7456a60df476c0d1f6`；同一Flash low、temperature=0分别返回長崎そよ和高松燈。这是实际不稳定案例，不是两次重复便能估计的失败率，见[原始调用ID与摘要](same-prompt-repeat.json)。

## 提前触发与调用成本

| 指标 | A | B主对照 |
| --- | ---: | ---: |
| 新Understanding初次生成 | 24 | 24 |
| Understanding文本修复 | 2 | 2 |
| Refine生成入口调用，含修复 | 22 | 23 |
| 已提交区间收据 | 22 | 22 |
| 声纹逐表达评分 | 无具名更新 | 316 |
| 具名准备开始时理解提交 | — | 16/24 |
| 具名准备就绪时理解提交 | — | 18/24 |
| 首次Refine发起时理解提交 | 本轮恢复时24/24 | 18/24 |

具名准备约12.188秒，第一次Refine比最后一个Understanding提交早80.950934秒。316条评分分档为ACCEPT34、CONFLICT86、null84、NO_VOICE112；分档及改名数不代表身份准确率。

B主对照从实验任务开始到完整结束713.047秒，不含先前抽帧准备；A包含实验宿主中断和恢复，仅最终Refine恢复段462.919秒，二者不能直接作为加速比。两组执行条件和生成结果有波动；本轮只证明提前重叠确实发生，未证明生产端到端提速。调用数是被采集的生成入口，Provider内部请求、token与费用未完整采集。

## 恢复、额外运行与未通过项

实验宿主的两项错误均修正并保留尝试：声纹收集参数顺序不匹配；随后以原始60秒成员边界处理已扩大的scope。后者按正式Server合同修正展开member_bounds、固定write_bounds与consulted scope并集，用实际失败快照通过Canvas及完整结果门禁。重叠区间以完整成员集合互斥，保留48份首次理解原文件SHA。原中断Refine入口A16次、恢复B10次另行归档。

A从本轮24份新理解完成最终22区间；恢复B也完成24/22，另有26次Understanding入口和24次Refine入口记录，其中一次生成错误经修复后通过。为了验证真实16/24触发，修正宿主后再次完整执行B主对照。该主对照在查看新B输出前已[固定选择](primary-case-selection.json)，不按人物结果挑选；恢复B仅作为恢复与不稳定证据。额外B运行的成本保留，没有假装只付出两组最初调用。

132项相关Understanding／Refine／Canvas测试通过（1.76秒）；原ASR SHA保持不变，24份初始请求一致，每组24份首次理解和22份区间收据核对。生产代码、Server、GPU与CPU/prod均未修改。DS辅助协议核对被自动审批拒绝，本轮由主Agent直接核对原始JSON。

当前判定：执行链路和2/3触发可用性已在本机实验宿主验证；阶段1匿名分组和最终身份质量未通过，尚未部署Server。需要继续处理Refine覆盖正确预填的条件，以及参考名字和匿名分组质量；不能仅因top1正确或准备提前就宣称low稳定识别。

## 证据与复现

原始结果分别保存于`outputs/verification/audio-first-prefill-live-ab-20261115/`和`outputs/verification/audio-first-prefill-live-b-gate-20261115/`，包含私有请求／返回、原始Understanding、冻结16块、声纹向量和最终内容；媒体、凭据与向量不随Git提交。

入口为[实跑脚本](live-ab.py)、[恢复Refine](resume-live-refine.py)、[采集对照](collect-live-ab.py)。沿用原报告的本地venv、模型部署和PYTHONPATH；新实验可用`live-ab.py --data-root <根仓库> --run-name audio-first-prefill-<唯一运行名> --cases A_audio_only B_two_thirds`。配置凭据按标准环境加载；脚本拒绝覆盖已有manifest。采集本次主对照增加`--b-run-name audio-first-prefill-live-b-gate-20261115`。再次模型运行会新增实际调用，不是只读复验。
