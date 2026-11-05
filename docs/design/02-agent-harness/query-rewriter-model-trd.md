# 1. 问题与现状

**查询改写模型 TRD。状态：已实现，演示环境验收通过（2026-11-05）。** 分支 `feat/query-rewriter-model`，PR [#47](https://github.com/owariband/Seekino/pull/47)。关联：Query Hook 现有实现（`agent/chat/query_hook.py`、`gateway/query_hook.py`、`gateway/config.py` 的 `QueryHookConfig`）。无合并顺序依赖。

## 背景与触发场景

每轮 Chat 前，Query Hook 先由 Jev（OpenRouter `typesafe/jev-1.14`）判断本轮是直接回应、检索还是混合（`respond` / `retrieve` / `mixed`），再由本机 Qwen3-8B（llama.cpp，`127.0.0.1:18841`）把用户消息改写成检索目标 `retrieval_query`，附加到 Supervisor 的输入。2026-11-05 发布验收时按用户要求开启 Query Hook。

## 实际与期望行为

期望：Jev 路由后由可用的模型改写用户消息，使 Supervisor 的检索与回答更贴合场景；改写可独立开关；模型来源走统一的模型配置。

实际：

- 改写器写死为 llama.cpp 端点与模型名（`QueryHookConfig.rewriter_base_url`、`rewriter_model`；`gateway/query_hook.py` 用 `LlamaCppMultimodalModel` 调用）。本机没有 Qwen3-8B 权重，也没有运行 18841 服务；`deploy/README.md` 列出的 `outputs/models/Qwen3-8B-GGUF-local/` 在当前机器不存在。
- 实测（19641，`dev` `978be570`）：`event=query_hook_route status=completed action=retrieve duration_ms=2070.0`，随后 `event=query_hook_rewrite status=fallback duration_ms=10.5 error=Rewriter failed (ConnectError)`；LOOP-08 的 SE/LP 五轮均为路由完成、改写回退。
- 改写没有独立开关：`enabled` 同时控制路由与改写。
- 本地主工作区有未提交的“默认开启 Jev”配置改动（`config.yaml`、`config/local-dev-config.yaml`、`.env.example`、`deploy/cloud/*.example*`、`deploy/docker/env.example`、`tests/gateway/test_config.py`），未同步到 origin。

## 负面影响

8B 本地模型需要用户没有的算力；改写永远回退，等于只开了一半功能，还白耗一次连接尝试。

## 本次边界

解决：改写器改为通过模型配置绑定（如 `models.query_rewriter` 选择 `deepseek-flash`）调用；新增 `query_hook.rewriter_enabled` 独立开关；移除写死的 llama.cpp 改写端点与 Qwen 默认值；仓库默认开启 Jev 路由。不解决：Jev 本身的替换；带播放上下文或附件的轮次（现有逻辑跳过 Hook）。必须保持：Hook 失败一律回退到原始消息；`decision_model`（`models.query_hook` 单模型决策）路径不变；总超时语义不变。

# 2. 影响因素、假设与评价指标

| 因素 | 假设 | 验证 |
| --- | --- | --- |
| 改写模型可用性 | H1：deepseek-flash 在 4 秒内完成单次结构化改写 | 19641 上 SE/LP 五个问题记录 `query_hook_rewrite` 耗时与状态 |
| 改写对回答的影响 | H2：改写后检索目标更具体，回答不变差 | 同题开关改写前后对比工具调用数、耗时与回答（小样本，记录为观察） |

| 指标 | 基线 | 目标 / 护栏 |
| --- | --- | --- |
| 改写成功率 | 0 / 5（ConnectError 回退） | ≥ 4 / 5 |
| 改写耗时 | 10.5 ms 失败 | ≤ 4 秒（`rewriter_timeout_seconds`） |
| 关闭改写时行为（护栏） | — | 只路由、不调用改写模型 |
| Hook 失败回退（护栏） | 回退原消息 | 不变 |

# 3. 改造方案与建模

```text
用户消息 → Jev 路由（respond / retrieve / mixed）
  respond → 原样
  retrieve / mixed → rewriter_enabled ? 改写模型（models.query_rewriter）生成 retrieval_query : 不改写 → 附加模板
```

- `QueryHookConfig` 增加 `rewriter_enabled`；删除 `rewriter_base_url`、`rewriter_model`、`rewriter_stream`、`rewriter_temperature` 等 llama.cpp 专用字段，改写参数随模型绑定配置。
- Server 装配时按 `models.query_rewriter` 创建生成模型并注入 Query Hook；未配置绑定或开关关闭时不调用改写。
- 仓库配置：`query_hook.enabled: true`，`rewriter_enabled: true`，`models.query_rewriter` 指向 `deepseek-flash`（与 Chat 相同来源）；吸收本地主工作区未提交的 Jev 默认开启改动。

# 4. 对照实验与结果分析

计划：单元测试覆盖开关、绑定缺失、改写失败回退；19641 上以 SE01、SE02、SE04、LP01、LP02 各跑一次，记录路由与改写的状态、耗时，并与 2026-11-05 改写回退的同题记录（`outputs/verification/course-loop-dev-20261104/gates/loop08-1.json`）对照。

**测试（提交 `feb025ed`）：** 全量 `tests/agent tests/server tests/gateway -m "not live_agent"` 20 failed / 5303 passed，失败集合与 `dev` `978be570` 完全一致。

**演示环境（19641，`dev` + #46 + #47 集成构建；v11 模型配置增加 `models.query_rewriter`（deepseek-flash，`reasoning_effort: none`）并移除 llama.cpp 字段，原配置备份 `model-config.yaml.bak-20261105-pre-rewriter`；记录 `outputs/verification/course-loop-dev-20261104/gates/``loop08-*.json`）：**

| 指标 | 基线 loop08-1（Qwen3-8B 不可用） | 本方案 loop08-2…7 |
| --- | --- | --- |
| 改写成功率（`action=retrieve` 回合） | 0/4（ConnectError 回退，5–34 ms） | 14/14 |
| 改写耗时 | — | 388–1493 ms，中位约 680 ms |
| 路由 `action=respond` 回合（LP01） | 不改写 | 不改写（行为不变） |

改写对回答质量的影响未单独对照：同一时段的回合耗时主要受 Supervisor 模型生成与 `query_video` 委派影响（见 [outline-first-delegation-trd.md](outline-first-delegation-trd.md)），改写本身占用不到 1.5 秒。

# 5. 试点验证与方案取舍

以演示环境实测代替独立试点；若改写使回答变差或总耗时显著上升，关闭 `rewriter_enabled` 回到仅路由。

# 6. 结果验收与闭环

**原问题验收：** 改写不再依赖本机 Qwen3-8B，演示环境 14/14 回合改写成功；`rewriter_enabled` 可单独关闭改写而保留 Jev 路由。

**最终决定：** 采用。部署环境需提供 `models.query_rewriter` 绑定所需的 `DEEPSEEK_API_KEY`；旧配置中的 `rewriter_base_url` / `rewriter_model` 等字段会被拒绝，需同步删除。

**剩余事项：** 改写前后的检索命中与回答质量对照（需固定样本的离线评测）；本机 `dev` 检出合并后需丢弃重复的 Jev 默认配置修改再 `git pull --ff-only`。
