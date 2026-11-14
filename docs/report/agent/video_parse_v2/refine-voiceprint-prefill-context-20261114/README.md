# Refine 逐句声纹摘要输入对照（2026-11-14）

设计见[本次 TRD](../../../../design/03-product-reassessment/refine-voiceprint-prefill-context-trd.md)，执行状态见[工作台 B12](../../../../dev/workbench.html)。

[measurement.json](measurement.json) 保存真实 Prompt 构造器对合成输入的修改前后对照。基线为 `16d32ff4`；样本来自既有测试 `_voice_snapshot`，固定人物、候选分数与音频区间，只扩大参考样本元数据、来源明细及引用 ID。此数据不是 EP04 整集回放，也不代表 token、模型延迟或人物准确率。

在仓库根目录及项目 Python 环境中复跑：

```bash
PYTHONPATH=. uv run --no-sync python docs/report/agent/video_parse_v2/refine-voiceprint-prefill-context-20261114/measure.py
```

测试入口为 `tests/agent/video_parse_v2/test_context_canvas.py`；相关 Context Canvas／Refine direct／framework／Understanding JSON 四组共 167 passed，0.86s。新合同在旧代码上对应的 10 个检查均失败，修改后通过。原始宿主快照保持不变，flag／prefill／off 模式的参考资料扩展不再使 Prompt 增长。真实 EP04、供应商上下文预算和部署未在本次运行。


## 整集真实验证（2026-11-14）

标准 reparse 新任务 `7504e1bb-2958-4b1d-9d77-407b829e93c9`（v2）于 UTC+8 13:04:39–13:24:02 完成，提交至完成 **19 分 23.134 秒**。恢复昨天的本机 Docker 测试库／ES，API 运行本次投影修复及 #160 媒体工作目录、#164 Understanding Prompt；组合回归 222 passed。按正式缓存复用相同的 166 条 GPU CUDA WhisperX ASR，重新执行全部 1fps Understanding。旧 v1 失败任务保留。详见 [live-run.json](live-run.json)。

- Understanding 24/24、Refine 22/22、当前版本索引 24/24；B0 314 行、313 次完成编码，另有短片段无有效样本；ES 按本任务回源为 24 Chunk、673 Event。
- 41 次实际 Refine 输入均未携带参考样本／引用 ID；23 次返回、17 次 RefineInputChanged、1 次 DeepSeekModelError，最终 22 窗全部提交。实际 Prompt 为 32,481–86,296 字符，未再出现 ContextWindowOverflow。
- 同一真实首窗（序号 0/1/2）、同一 B0／原始证据：旧投影 1,152,947 字符，修复后 52,546 字符，且与实际捕获 Prompt **逐字相同**，缩减 95.4425%。这是可比的 Prompt 字符对照，不是 token 或费用降幅。

| 已确认目标 | 首次 Understanding | B0 | 最终结果 |
| --- | --- | --- | --- |
| 47.500–48.324s「祥ちゃんも悪くない」 | 長崎そよ | top1 長崎そよ 0.6016，NO_VOICE | 長崎そよ，正确；并非声纹自动预填纠正 |
| 294.500–296.500s「長崎そよちゃんと…」 | 若葉睦 | top1 千早愛音 0.7573，CONFLICT | 若葉睦，错误；Refine 未采用正确候选 |

两个已有真值目标为 1/2 正确，不能称全片人物归属通过。Understanding 分段另有 11 个时间包含候选；主 Agent 按“独立 cue 不在 Understanding 合并”的规则复核到至少一处违规：200.088–205.711s 的一个 DIALOGUE 合入 4 条源 cue 的措辞。即使它们是同句碎片，也未获本阶段合并例外；其余候选不强行计为已证实违规。Prompt 单独约束尚未满足严格分段要求。

模型账本还记录 3 次 VideoSummaryCompletion 失败，本轮未修该附带摘要问题；主解析终态仍为 COMPLETE。`processing_duration_ms=0` 本次不可用，耗时采用实际创建／完成时间差。完整本地留证位于 `outputs/verification/ep04-prefill-context-20261114/`，原始素材位于独立 `assets/library/verification/ep04-prefill-context-20261114/`；私有配置与完整参考池不提交。服务保留运行，没有访问或部署 CPU/prod。
