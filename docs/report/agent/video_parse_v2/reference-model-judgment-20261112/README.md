# 剪辑参考模型观察接纳验证 · 2026-11-12

原问题见 [TRD](../../../../design/03-product-reassessment/reference-model-judgment-trd.md) 与 [PR #139](https://github.com/owariband/Seekino/pull/139)。实现提交 `76baa67d`，基线 `5c7a68ac`。本报告记录截至本次检查的证据，不承担独立任务看板；当前状态见工作台 EDIT-REFERENCE-02。

## 问题改善与保留边界

首轮真实素材任务的 4/8 失败暴露了逐段观察中的候选切点约束和 PickNote 补尾缺口。本次删除以下限制，不对原始模型中文或动作时间做投影、排序、裁剪或重写：

| 原限制 | 修改后行为 |
| --- | --- |
| 动作步骤只允许检测切点，且必须连续铺满片段 | 每步只需是本源片段内的正向区间；接受空步骤、缺口、重叠和独立动作顺序 |
| 风格例证只能落在片段起点或检测点 | 接受所引用片段 [start_ms, end_ms) 内的原片整数毫秒 |
| 有观察就必须有风格信号 | 允许如实返回空 signals |
| Refine 必须复述每个段且恰好一次 | 宿主保留原观察；只应用实际更新，重复更新按最后一次处理，按宿主来源恢复完整结果 |
| role、framing、enter 只能使用封闭枚举 | 保留模型的文字描述，字段与类型不变 |
| 单条描述 600 字、动作 90 步、槽位/信号/例证 8 条等模型描述门槛 | 删除描述性上限，调用仍受现有输出 token 和执行预算限制 |
| 新 PickNote 的槽位只能是对象 | 与既有 Note 一致，接受结构槽位或文字槽位 |
| PickNote 缺最后一个根右括号直接重试失败 | 复用现有窄范围补尾处理，再执行完整结构与来源验证 |

保留来源 ID、原生帧钟、源区间、Chunk/Video 归属、媒体槽位类型、宿主测量数量合同、最多 63 个 Chunk 的持久窗口、基本 JSON 结构和真实存储/执行边界。Server 的鉴权、租约、版本、发布对账和全局失败恢复策略没有修改。旧 StyleCard 读取合同保留。

## 同输入冻结回放

[样本文件](../../../../../tests/engine/parsev2/fixtures/reference_model_outputs.json)包含九次动作分段失败和三次 P5R 不完整 JSON 响应；保留模型原文及宿主测量，不包含媒体、凭据或无关日志。每例经真实 `observe_segments` 入口执行，使用同一条冻结响应、attempts=1 和本地空白帧像素，不发起新模型调用。

| 指标 | 修改前 | 修改后 | 变化与边界 |
| --- | --- | --- | --- |
| 动作分段接纳 | 0/9 | 9/9 | +9；不改动作时间或文字 |
| PickNote JSON 接纳 | 0/3 | 3/3 | +3；仅缺外层闭合符可以恢复 |
| 合计接纳 | 0/12 | 12/12 | +12；不是新模型完成率 |
| 新模型调用 | 0 | 0 | 离线对照没有模型费或实模生成耗时 |

原始对照为 [baseline.json](baseline.json) 与 [fixed.json](fixed.json)。样本的原始文本和步骤时间由测试逐字段比较；固定文件摘要保存在 fixed.json。

## 已执行的回归

| 命令 | 实测结果 | 证明范围 |
| --- | --- | --- |
| `python -m pytest tests/engine/parsev2 tests/server/test_mcp_edit_references.py -q` | 215 passed，1.81 s | 领域、时间/来源反例、旧格式及 MCP 回源相关行为 |
| `python -m pytest tests/engine/parsev2/test_edit_reference.py tests/engine/parsev2/test_reference_model_judgment.py -q` | 31 passed，0.23 s | 主 Agent 最后一次精确复跑，包含 12 条真实响应及修复边界 |
| `SEEKINO_TEST_ELASTICSEARCH_URL=http://127.0.0.1:19469 python -m pytest tests/server/test_editing_mcp_flow.py -q -rs` | 3 passed，25.95 s | 正式上传 → 自动解析 → 全片 Refine → 索引 → MCP 查询、风格卡与证据回看 |

执行 Python 为项目既有 Python 3.12 虚拟环境。完整集成验证使用新建的独立 Elasticsearch 8.19.22 集群 `seekino-reference-test`（19469，临时数据目录）和独立 SQLite 临时库，没有连接 development 素材索引；模型和向量响应由测试注入。新增两个集成参数覆盖完全未更新与部分重复更新，且包含非检测点/不铺满的动作、自由描述、长摘要、字符串槽位和非检测点风格锚点。旧链路同时覆盖原片下载、公开/撤回权限及来源回查。

## 与并行 dev 整合后的复核

整合 `2ec52ea4`（新增 MCP/Skill 创作参考文案，相关生产代码的逻辑未改变）后，215 项 Engine/相关回归再次通过，用时 1.68 s。完整链路组本轮为 2 passed、1 failed（17.66 s）：两个新增剪辑参考用例通过，既有正片用例在索引阶段报告 `Event index target changed during execution`。对该单项定向重跑为 1 passed（14.23 s）。首次失败保留在记录中；错误位置属于未修改的正片 Event 目标校验，本次未扩展为该并发路径的修复或长期稳定性结论。

## 证据边界

12 条失败输出现在可以通过解析入口，说明报告中的过度拒绝已被消除；这不能证明模型描述都正确，也不能从一次离线结果推断长期批量稳定。正式 development 的八支视频仍属于另一轮真实运行记录；部署、采用 50000 新配置的任务结果及实际业务质量须分别记录。当前代码验证未改写或删除旧视频和已提交成果。
