# 1. 问题与现状

需求：Understanding 保留独立语音段，将跨段合并判断交给 Refine。日期：2026-11-14。工作分支：`fix/understanding-dialogue-boundaries`；PR：创建后回填，目标分支 `dev`。当前执行状态统一见[工作台 B12](../../dev/workbench.html)。

Understanding 根据 ASR、画面与背景生成 DIALOGUE；B0 随后使用这些 DIALOGUE 的音频区间准备声纹。若 Understanding 将原本独立的语音段合成一个大区间，声纹提取可能混入不同发言，后续逐句纠错也失去原来的分段依据。

当前 [Understanding Prompt](../../../agent/video_parse_v2/prompts.py)要求核对每条 ASR、允许拆开有证据的混人 cue，并保留跨 Chunk 的完整 cue，但没有明确禁止合并独立 cue。Refine 已负责跨 Chunk 去重和连续性修复，却没有明确承接这项合并职责。这里的“合并音频”指扩大 DIALOGUE 的音频时间范围，并非拼接媒体文件。

本次在 Agent Prompt 中明确两阶段职责：Understanding 保留各独立语音段，Refine 根据证据判断能否合并。保留 ASR 噪声清理、措辞修正、有依据的多人 cue 拆分及跨 Chunk 完整 cue；不把每个 cue 无条件复制成事件。本次不改变声纹算法、调度、Schema 或提交事务。此前 EP04 的 Refine 输入膨胀另行处理，不能由本次职责调整推定已解决。

# 2. 影响因素、假设与评价指标

核心假设：明确禁止 Understanding 跨独立 cue 合并，可以减少进入声纹计算的混合区间；真实模型遵从程度与人物归属改善均须另验。WhisperX 自身已混人的 cue 仍需拆分，禁止合并不代表输入边界必然正确。

| 指标 | 口径、基线 | 目标 | 验证方法与证据 |
| --- | --- | --- | --- |
| 阶段职责 | 原 Prompt 未明确禁止跨独立 cue 合并 | Understanding 明确禁止；Refine 明确承担有依据的合并 | 对照 Prompt diff 及实际构造的输入，已核对 |
| 既有时间与证据合同 | 完整 cue 可跨 Chunk；原始 ASR 保留词级对齐 | 保持原行为，相关测试通过 | Understanding / Refine 既有测试 |
| 真实模型分段 | 本次未获得同输入重跑基线 | 独立语音段在 Understanding 后仍独立；混人 cue 可有据拆分 | 后续 EP04 整集首轮 Understanding 与原始 ASR 对照，待测 |
| 人物归属及成本 | 本次未测 | 不用测试通过代替人物准确率；记录实际 token、耗时 | 后续整集验证，待测 |

# 3. 改造方案与建模

复用既有完整 JSON 工作流，只修改 `agent/video_parse_v2/prompts.py` 的阶段策略并同步模块说明。

1. Understanding 对各独立 ASR cue／发言分别保留 DIALOGUE，不因相邻、同人、同话题或叙事连贯而连接其音频范围。独立段的合并交给 Refine。
2. 保留现有例外：证据支持时拆开混人 cue；修正噪声、措辞或时间；不因句号或 Chunk 边界机械切分一个完整 cue。未知身份继续为 null。
3. Refine 可以在证据支持同一人的连续发言时考虑合并；保留不同说话人／不确定的话轮边界。改变音频范围后，原逐句声纹分数仍只对原范围有效，不可移用于合并后的范围。

数据流仍为：原始 ASR → Understanding 独立 DIALOGUE → 既有 B0 声纹准备 → Refine 有据合并／去重 → 原事务提交。该修改是模型指令约束，不新增宿主硬校验，也不保证所有模型输出必然遵从。

实现顺序为 TRD 首提交、DeepSeek Flash 按明确范围修改 Prompt、主 Agent 复核 diff、运行相关既有测试、同步结果与 PR。没有接口或存储迁移；历史已保存的 Understanding 不会自动重写。

# 4. 对照实验与结果分析

基线为本分支起点 `8f8dd5be`；修改前后使用相同的 Prompt 构造器与既有测试。优先核对禁止合并指令是否与混人拆分、跨 Chunk 完整 cue 和 Refine 声纹范围约束一致，再运行针对性回归。

| 比较项 | 修改前 | 修改后 | 判断与证据 |
| --- | --- | --- | --- |
| Understanding 跨独立 cue 合并职责 | 未明确禁止 | 已在 Prompt 明确禁止 | 实际构造的 Prompt 与 diff 核对 |
| Refine 有据合并职责 | 仅泛化说明去重、修复连续性 | 已明确合并条件及应保留的话轮 | 实际构造的 Prompt 与 diff 核对 |
| Understanding／Refine 回归 | 本轮未单独运行基线 | 90 passed，0.97s | 既有隔离测试，未新增提示原句断言 |
| EP04 真实分段、身份与成本 | 未重跑 | 未重跑 | 不由静态检查推导 |

在独立工作树中复用本机 Python 3.12 环境运行：

```bash
/Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest tests/agent/video_parse_v2/test_understanding_json.py tests/agent/video_parse_v2/test_refine_direct.py tests/agent/video_parse_v2/test_framework.py -q -m 'not live_agent'
```

生产代码仅新增两段 Prompt 字符串。测试覆盖原始 ASR 证据、完整 JSON 提交、跨 Chunk 完整台词、Refine 音频范围变化后的来源处理及恢复合同；没有使用真实模型，不能证明语义遵从或人物身份效果。DeepSeek dsh 因缺少凭据未执行，OpenCode 重试被自动审批拒绝向外部模型发送私有源码；本次实现和测试由主 Agent 在本机完成。

# 5. 试点验证与方案取舍

这是小范围 Prompt 策略修改，采用既有隔离 Harness 测试核对接线与数据合同，不新增独立试点，也不为本次修改启动或部署服务。真实效果仍需后续 EP04 整集 1fps 对照，不使用片段试点替代整集验收。

真实整集验证前需先处理此前独立发现的 Refine 输入上下文超限；Prompt 职责调整不会缩小该证据载荷。验证时比较首轮 Understanding 独立段、B0 音频范围和 Refine 最终结果，区分上游分段与下游合并。任何实测退化应保留失败证据并调整策略。

# 6. 结果验收与闭环

Prompt 职责已接入，相关既有测试 90 项通过。验收分两层：现有合同回归完成，再以整集真实输出判断独立段是否被保留、人物归属是否改善。前者不能代替后者。采用该阶段职责调整；尚未部署服务，也不会自动改写历史任务成果。

待补 PR 链接；真实整集效果、耗时与 token 继续标记未验证。后续先处理独立的 Refine 载荷问题，再通过整集 1fps 对照验证分段与身份；当前状态只在[工作台](../../dev/workbench.html)维护，相关历史问题保留在 [B12](../../dev/problems.md#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属)。
