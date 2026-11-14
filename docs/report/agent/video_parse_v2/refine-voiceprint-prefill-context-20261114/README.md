# Refine 逐句声纹摘要输入对照（2026-11-14）

设计见[本次 TRD](../../../../design/03-product-reassessment/refine-voiceprint-prefill-context-trd.md)，执行状态见[工作台 B12](../../../../dev/workbench.html)。

[measurement.json](measurement.json) 保存真实 Prompt 构造器对合成输入的修改前后对照。基线为 `16d32ff4`；样本来自既有测试 `_voice_snapshot`，固定人物、候选分数与音频区间，只扩大参考样本元数据、来源明细及引用 ID。此数据不是 EP04 整集回放，也不代表 token、模型延迟或人物准确率。

在仓库根目录及项目 Python 环境中复跑：

```bash
PYTHONPATH=. uv run --no-sync python docs/report/agent/video_parse_v2/refine-voiceprint-prefill-context-20261114/measure.py
```

测试入口为 `tests/agent/video_parse_v2/test_context_canvas.py`；相关 Context Canvas／Refine direct／framework／Understanding JSON 四组共 167 passed，0.86s。新合同在旧代码上对应的 10 个检查均失败，修改后通过。原始宿主快照保持不变，flag／prefill／off 模式的参考资料扩展不再使 Prompt 增长。真实 EP04、供应商上下文预算和部署未在本次运行。
