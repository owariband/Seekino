# 1. 问题与现状

需求：Refine 只接收已有逐句声纹预填摘要，全片参考资料保留在宿主。日期：2026-11-14。分支：`fix/refine-voiceprint-prefill-context`；PR：[#165](https://github.com/owariband/Seekino/pull/165)，目标 `dev`。当前状态统一见[工作台 B12](../../dev/workbench.html)。

全片 DIALOGUE 既是待判定对象，也大量用作其他台词的声纹参考。宿主 B0 已完成比较并预填可接受的身份；Refine 再接收整个参考池及逐候选引用清单，对完成当前台词审核没有必要，还会反复复制这些数据、挤占上下文。

源码确认了两个不同视图：[ParseContextCanvas._apply_voiceprint](../../../agent/video_parse_v2/timelineEvent_canvas.py) 已将评分绑定到具体事件，保留区间、候选姓名、分数、参考数量及冲突状态，并排除参考 ID／来源详情；[build_refine_prompt](../../../agent/video_parse_v2/prompts.py) 却序列化了完整 `member_evidence.voiceprint`，包括 `samples`、`reference_ids` 等内部追溯数据。Server 的正式模型调用使用该 Prompt，不会另外需要参考池。

本次修复模型输入投影，复用现有逐句预填与绑定逻辑。全片参考资料继续用于宿主计算及留存；不新增共享参考目录、模型工具或声纹重算，不改算法、预填条件、Schema、任务恢复及提交事务。此前 [Understanding 不合并独立段的 PR #164](https://github.com/owariband/Seekino/pull/164) 独立交付，本次无合并顺序要求。

# 2. 影响因素、假设与评价指标

决定性因素是原始追溯数据进入了模型上下文，而不是参考关系本身不合法。假设已有绑定摘要足以保持当前 Refine 对预填身份及冲突的审核；真实人物质量仍需后续整集验证。

| 指标 | 基线 | 目标／护栏 | 测量方法 |
| --- | --- | --- | --- |
| 参考资料进入模型输入 | 当前完整序列化 `samples` 和候选引用清单 | 不出现参考池、来源明细、引用 ID 数组；原始宿主资料不变 | 构造真实 Prompt、检查 JSON 并对照原快照 |
| 逐句预填及审核信息 | Canvas 已有有效区间绑定和精简候选 | 同一条台词的身份、分数、冲突状态和数量保持；无匹配的评分不注入 | 现有 Harness 测试及针对性回归 |
| 输入字符数与增长 | 合成 prefill 扩展样本 352,933 字符，参考资料扩展增加 350,006 字符 | 同一评分摘要下，参考资料增长不再增加 Prompt | 修改前后同样本比较，不将字符当 token |
| 真实人物归属、耗时及 token | 本轮未测 | 后续整集验证，不以流程通过代替质量 | EP04 整集 1fps，待执行 |

# 3. 改造方案与建模

在 Agent 的模型输入组装层，将原始 `voiceprint` 与其他证据分开处理：其他证据沿用既有投影；声纹只输出 Canvas 已绑定、仍有效的逐句摘要，并保留对应成员及事件位置、原音频区间。使用现有候选摘要，不重新计算、不改变排名、不截断候选。

数据流保持为：全片参考池 → 宿主 B0 评分 → Canvas 按事件区间绑定并预填 → Refine 读取预填草稿及逐句摘要 → 原校验／事务提交。全片参考池及引用关系不进入模型 JSON；原始快照仍保留，便于内部追溯与范围变化处理。

先用相关回归证明旧 Prompt 会带入参考池，再修改投影、运行相关测试和同输入字符对照。生产代码仅涉及 `agent/video_parse_v2/prompts.py`；不改 Server、Engine 或 Gateway，不重启服务。

# 4. 对照实验与结果分析

基线为 `16d32ff4`。使用既有 `_voice_snapshot` 和可重现的参考池扩展样本，经真实 `build_refine_prompt` 比较；只改变输入投影，身份与评分保持。新增回归需在修改前失败、修改后通过，并检查原始证据未变。

| 对比项 | 修改前 | 修改后 | 证据／判断 |
| --- | --- | --- | --- |
| 参考池与逐候选 ID 数组 | 进入 Prompt，10 个对应合同检查失败 | 不进入 Prompt，仅有效绑定摘要 | 针对性回归通过，原快照未变 |
| 合成 prefill 扩展样本 Prompt | 352,933 字符 | 2,814 字符 | 缩减 99.2027%；非 EP04／token |
| 合成样本的参考资料增长增量 | flag／prefill／off 均增加 350,006 字符 | 三种模式均增加 0 字符 | 同一评分下完整 Prompt 相等 |
| 预填、时间范围及提交合同 | 针对新投影合同的 10 个检查失败 | 相关四组 167 passed，0.86s | 既有回归及新增测试 |
| EP04 实模效果与成本 | 本轮未测 | 本轮未测 | 不从离线结果推导 |

测试命令：`/Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest tests/agent/video_parse_v2/test_context_canvas.py tests/agent/video_parse_v2/test_refine_direct.py tests/agent/video_parse_v2/test_framework.py tests/agent/video_parse_v2/test_understanding_json.py -q -m 'not live_agent'`。

字符对照通过真实构造器及同一快照测量，复跑方法与原始数据见[报告](../../report/agent/video_parse_v2/refine-voiceprint-prefill-context-20261114/README.md)。flag、prefill、off 的身份行为不变；ACCEPT／CONFLICT／NO_VOICE／REVIEW 的有效摘要、跨成员隔离、不匹配区间不注入及原始证据不变均有回归覆盖。

# 5. 试点验证与方案取舍

使用隔离 Harness 测试和真实输入构造器替代单独片段试点；不为本次修复部署或启动服务。Prompt 缩减与逐句预填合同可在本机确定，真实人物判断仍需 EP04 整集 1fps 验收，不能据此宣称整集已成功恢复或身份全部正确。

离线验证已消除参考资料扩展对模型输入的放大；代价是模型不再读取宿主的参考来源明细。原始数据仍在宿主留存，模型无需重做全片声纹比较。真实请求的成本与人物判断变化仍需整集复验。

# 6. 结果验收与闭环

已实现输入投影并通过 167 项相关测试，模型只接收已绑定的逐句预填摘要，完整参考资料仍在宿主。合成对照验证参考资料增长不再增加 Prompt，采用该修复。真实模型效果、EP04 恢复和部署未执行，不随离线测试完成。

交付见 [PR #165](https://github.com/owariband/Seekino/pull/165)；下一步为独立验证环境的整集 1fps 复验，核对真实请求预算及人物归属。当前状态见[工作台](../../dev/workbench.html)，历史问题见 [B12](../../dev/problems.md#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属)。
