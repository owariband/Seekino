# CONFLICT 预填与原身份评分回放

2026-11-14；分支 `fix/voiceprint-conflict-prefill`，[PR #167](https://github.com/owariband/Seekino/pull/167)，依赖 [PR #165](https://github.com/owariband/Seekino/pull/165)。设计见[TRD](../../../../design/03-product-reassessment/voiceprint-conflict-prefill-trd.md)，当前状态见[工作台 B12](../../../../dev/workbench.html)。

旧 Canvas 只预填 ACCEPT，CONFLICT 保留 Understanding 名字；B0 又只导出前三，因此原身份落在前三之外时没有对照分数。修复后两类分档均可先填 top1，原身份有真实评分则一并保留；Refine 仍有审核、更正和置 null 权限。

直接覆盖 Canvas／Refine、B0 算法和 Server 原子发布的隔离测试 **379 passed，5.30s**。修改前选定的 12 项回归中 3 项失败，分别证实 CONFLICT 草稿、完整 Refine 提交和原身份候选丢失。执行命令：

```bash
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest \
  tests/agent/video_parse_v2 \
  tests/engine/parsev2/test_voiceprint_matching.py \
  tests/server/test_video_parse_voiceprint.py -q --tb=short
```

冻结输入取自本机整集 v2 `7504e1bb-2958-4b1d-9d77-407b829e93c9`，包含 314 行、313 个声纹向量和 24 份首次 Understanding。保持模型、encoder、参数和参考池不变，重算所有可编码目标，逐条断言前三及 gap 与原 artifact 相同；随后使用真实 Canvas／Prompt 构造器重建覆盖目标的原始 Refine 窗口。原始 artifact SHA-256 与调用关联见 [replay.json](replay.json)。完整资料留在本机 `outputs/verification/ep04-prefill-context-20261114/private/`；回放脚本为其中 `replay-conflict-prefill.py`。

| 检查项 | 修改前 | 修改后 |
| --- | --- | --- |
| 整集前三及 gap | 原评分 | 313/313 完全一致 |
| 原身份不在前三而有评分 | 未提供 | 63 行补回 |
| 294500–296500ms 草稿 | 若葉睦 | 千早愛音 |
| 对照评分 | 千早愛音 0.757265；睦缺失 | 千早愛音 0.757265；若葉睦 0.512934 |
| gap／分档 | 0.053973／CONFLICT | 不变；conflict=true 保留 |
| 原始参考池／引用 ID 进入模型 | #165 已移除 | 仍未进入 |
| 新 Agent 最终身份 | 未运行 | 未运行 |

DeepSeek Flash 通过 `delegate` 的 OpenCode 后端仅读最小评分摘要，确认草稿置换和 NOT_RUN 边界；主 Agent 复核了原始向量重算断言、实际输入关联和源文件不变。摘要中的第四项是为比较附上的原身份，不代表其在全量人物中排名第四。

上述离线验证证明输入路径已经按 top1 预填并提供原身份对照，不能据此声称新整集归因全部正确、Agent 会保留爱音、token／费用降低或解析耗时改善。该阶段没有模型调用、数据库写入或服务重启；后续真实整集见下。旧 v2 保留，CPU/prod 未访问。

## 整集真实验证（2026-11-14 后续）

**主链路完成，两个固定目标姓名仍为 1/2 正确。CONFLICT 预填合同通过，人物效果未通过。** 任务 v3 `cc181952-3212-43cf-815c-ef9c0b9d07ba`，DeepSeek Flash high，视觉 1fps，prefill。Refine 继承同一模型绑定；实际模型用量记录确认 Understanding／Refine 都为 `deepseek-flash`。新执行副本基于 `a84ff818`，保留上轮 `45017773` 分段 Prompt 与 `c777ef72` 媒体目录修复；组合回归 434 passed，8.32 秒。

通过本机标准 reparse 创建新版本，旧 v1/v2 均保留。复用同一份 166 条整集 WhisperX ASR，SHA-256 `02e0d35d94491a960109c58c287fe92ed5e54584d63d486fe22d41bbae8593a0`；重新生成 Understanding、参考池和 B0。详见 [live-run.json](live-run.json)。

首轮 19 块成功后，一个 Understanding 请求因 `max_output_tokens` 耗尽 50,000 token 上限失败，4 个兄弟 Chunk 随父任务失败被取消。该上限包含推理与可见输出，见 [DeepSeek 接口定义](https://api-docs.deepseek.com/api/create-response/)。随后通过标准 retry-parse 恢复**同一 v3**，保留 19 块，只重试 5 块；模型、fps、输出预算和数值阈值均未改变。24 份首次 Understanding 全部在任何 Refine 提交前保存。

| 目标句 | 首次 Understanding | B0 与实际 Refine 输入 | 最终结果 |
| --- | --- | --- | --- |
| 47.500–49.000s「祥ちゃんも悪くない」 | 高松燈，错误 | top1 長崎そよ 0.744782、原高松燈 0.506480，gap 0.171333、ACCEPT；输入已预填爽世 | 返回改为高松燈并提交，source=refine_edit，**FAIL** |
| 288.500–295.499s 含「長崎そよちゃん…」 | 千早愛音，目标句姓名已正确 | top1 爱音 0.838045、第二爽世 0.836356，gap 0.001689、NO_VOICE；不触发预填 | 最终爱音、source=understanding，**目标句姓名 PASS**；6.999 秒整段单人及边界未认证 |

开头实际调用 `ee0e43f2-932d-4ded-8dad-6bb5e2b70114` 的输入 `speaker=長崎そよ`，返回却恢复为 `高松燈`。输入描述仍写“高松燈抬起头，替祥子说话”，与预填姓名矛盾；这是实际观察到的冲突，尚不能认定为模型改错的唯一原因。没有手动改写描述、锁定真值或介入本轮推理。爱音句首次 Understanding 已正确，不能把它计作 CONFLICT 纠错。上轮 v2 是开头正确、爱音错误；本轮错误目标换了，准确数量没有提高。重新生成了理解与参考池，不是冻结输入的单因素因果实验。

| 运行／输入指标 | v3 实测 |
| --- | --- |
| 端到端用时 | 23 分 59.848 秒，含失败与人工触发标准恢复；从 task create_time 到 UTC finish_time |
| 主链路 | 24/24 Understanding、22/22 Refine、24/24 当前版本索引；ES 24 Chunk／689 Event |
| B0 | 337 行、336 个有效编码样本 |
| 实际输入预填 | 337 条首次版本对白全覆盖；95 条预填，其中 69 条 CONFLICT 全部写 top1；75 条与原身份不同 |
| 原身份对照 | 47 条实际请求附上前三之外的原身份评分 |
| 实际 Refine 调用 | 49 次输入、23 次返回；21 次 RefineInputChanged、5 次 ModelFailureError；最终 22 窗口提交完成 |
| 参考资料进入模型 | 49 次均无 samples、reference_ids、src 或 vector；预填检查零违例 |
| 解析与 Refine 已报告 token | input 1,669,990；output 1,266,475，其中 reasoning 972,602；cached input 1,275,008；含 JSON repair，未把 reasoning 再加到 output |
| 不变性 | 371 个运行源码文件 SHA 一致；旧 v1 状态、v2 状态及 24 份内容指纹不变 |
| 辅助摘要 | Kimi 3 次失败，独立问题未修；主链路 COMPLETE 不表示所有辅助能力成功 |

token 仅汇总返回的 provider usage，失败／取消调用可能有未返回的用量，不据此估算账单。95 次预填和 top1 一致性均不是身份准确率，两个目标之外没有整集人物真值。

DeepSeek Flash worker 通过 `delegate` 的 OpenCode 后端核对冻结的最小目标摘要，主 Agent 复核实际请求／返回、终态与机械检查。原始控制脚本、首轮失败、49 次输入／返回和 worker 日志留在本机 `outputs/verification/ep04-conflict-prefill-flash-high-20261114/`；正式服务与 CPU/prod 未访问。本机专用 API 19731 已加载新代码，待后续处理“正确预填被 Refine 改错”及描述冲突。
