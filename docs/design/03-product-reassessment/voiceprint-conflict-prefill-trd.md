# 1. 问题与现状

需求：声纹 CONFLICT 先预填人物，再交 Refine 审核。日期：2026-11-14。分支 `fix/voiceprint-conflict-prefill`，PR 创建后回填。依赖 [#165](https://github.com/owariband/Seekino/pull/165) 的精简声纹输入，基线 `4f5238eb`；堆叠 PR 先指向该分支，合并顺序为 #165 → 本修复，最终集成 dev。当前状态统一见[工作台 B12](../../dev/workbench.html)。

EP04 v2 的 294.500–296.500s 台词在 Understanding 中标为若葉睦，声纹 top1 是千早愛音 0.7573，gap 0.05397、band=CONFLICT。实际 Refine 输入仍为睦，最终未纠正。[真实记录](../../report/agent/video_parse_v2/refine-voiceprint-prefill-context-20261114/live-run.json)证实：旧 Canvas 只在 ACCEPT 时替换姓名，且前三候选没有原身份睦的分数。

该门槛从 B0 首提交 `356677a9` 就存在，本轮输入精简没有新增它。它把高置信采纳与待审核草稿混为一谈，未落实“先置换 top1，保留原身份与评分，再让 Agent 审”的职责。本次修复该行为，不改旧任务、声纹算法或数值门槛，不处理 Understanding 跨 cue 合并及辅助摘要失败。

# 2. 影响因素、假设与评价指标

| 指标 | 基线 | 目标与验证 |
| --- | --- | --- |
| CONFLICT 草稿身份 | 保留 Understanding 名字 | 有可用候选且未被 Refine 明确编辑时先写 top1；真实 EP04 冻结回放验证 |
| 原身份评分 | 原身份不在前三则缺失 | 保留前三＋有依据的原身份候选；没有参考时不编造分数 |
| Agent 权限与恢复 | 可纠正／置 null；既有 refine_edit 不被覆盖 | 两类预填均保持相同编辑与来源合同 |
| 输入体积与留存 | #165 逐句精简摘要 | 不恢复 samples、reference_ids、src；原始证据不变 |
| 最终人物准确性 | v2 两个固定目标 1/2 | 本次离线验证只证明输入置换；未重跑则不得宣称新终态正确 |

# 3. 改造方案与建模

1. Agent Canvas 的 prefill 条件接受 ACCEPT／CONFLICT，继续要求候选存在、区间已绑定，并保留 refine_edit；flag、off、NO_VOICE 和无候选行为不变。
2. Engine 在现有全量排名计算后保留前三；若原 label 有真实评分且不在前三，再附上该候选。排名、gap、分档、encoder 和排除规则不变，不重新算一次评分。
3. Server 接受第四个候选时只允许它是原 speaker；保留既有参考关系、排序、分数、来源版本和提交校验，兼容旧三候选 artifact。
4. Refine 继续收到已置换 speaker、understanding_speaker、全部保留候选的精简分数及 conflict 标记。分档供审核，不自动指示恢复原身份；Agent 可有据纠正或置 null。

流程为 Understanding 原名 → 宿主评分 → prefill 写 top1 → Refine 审核 → 原事务提交。只修改实际持有这些策略的层，不新增配置、模型工具或迁移。

# 4. 对照实验与结果分析

先更新直接回归并在旧实现复现失败，再修复。测试覆盖 CONFLICT 预填到完整 Refine JSON 提交、显式更正、NO_VOICE／空候选、原候选保留及第四候选准入。用保留的 EP04 313 个真实向量重新执行相同 B0 评分和真实 Prompt 构造器，不调用外部模型，不改写旧结果。

| 比较项 | 修改前 | 修改后 |
| --- | --- | --- |
| 真实目标进入 Refine 的 speaker | 若葉睦 | 冻结真实窗口重建为千早愛音 |
| 真实目标原身份评分 | 不在前三，未提供 | 若葉睦 0.512934；爱音 0.757265，仍 CONFLICT |
| 相关合同回归 | 选定 12 项中 3 失败 | 379 passed，5.30 秒 |
| 全片评分稳定性 | 313 个已编码目标 | 前三／gap 不变，63 行补回原身份评分 |
| 新整集最终身份 | 未执行 | 未执行，不能由离线输入推导 |

# 5. 验证范围与方案取舍

本次采用相关隔离测试和冻结的整集声纹 artifact 回放验证数据流，不以片段实模试点代替整集。若加载到本机空闲测试 API，记录新源码和进程；旧 v2 保留。真实 Agent 最终是否保留正确身份仍需后续整集验证，不通过提高阈值或注入真值伪造通过。

# 6. 结果验收与闭环

实现和冻结输入回放完成，证据见[报告](../../report/agent/video_parse_v2/voiceprint-conflict-prefill-20261114/README.md)与[精简数据](../../report/agent/video_parse_v2/voiceprint-conflict-prefill-20261114/replay.json)。采用先写候选、后审冲突的职责，保留不确定性与 Agent 权限。代码尚未加载到服务，新整集 Agent 终态未执行，不能认定整集归因达标。原始声纹资料只留宿主，CPU/prod 未访问。
