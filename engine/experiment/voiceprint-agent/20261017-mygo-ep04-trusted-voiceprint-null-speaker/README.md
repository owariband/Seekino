# 第四集：可信静态声纹与未知说话人保留 null 复测

日期：2026-10-17；叙述使用 Asia/Shanghai，JSON 采集时间使用 UTC。

状态：v8 已完成，任务 `8ef2a23b-def7-4ff3-905d-36cee63ce70f`。流程验收通过，目标台词识别验收失败：最终仍错误归属千早爱音。

## 最终结果

运行时间为 19:16:08.468–19:46:17.381（UTC+8），墙钟 **30:08.913**。Understanding 24/24、Refine 22/22、当前正文版本索引 24/24，全部窗口 retry_count=0。Prefix Compact 关闭且无对应调用；429 条模型用量均 completed。

| 指标 | 上一轮 v7 | 本轮 v8 |
|---|---:|---:|
| 整集墙钟 | 43:22.011 | 30:08.913 |
| Refine 生成轮数 | 54 | 37 |
| Refine 快照刷新 | 32 | 15 |
| Refine 单轮生成中位数 | 258.324 秒 | 205.715 秒 |
| Refine 生成累计时间（并行相加） | 14267.299 秒 | 7942.154 秒 |
| VoiceprintAgent 生成调用 | 94.829 秒 | 389.539 秒 |
| 准备阶段 CAM++：次数 / 累计调用耗时 | 25 / 3.635 秒 | 25 / 3.914 秒 |
| Refine CAM++：次数 / 累计调用耗时 | 889 / 59.192 秒 | 270 / 14.894 秒 |

本轮比 v7 少 **13:13.098，约 30.48%**，但同时存在生成模型、Prompt 和静态参考策略差异，不是严格 A/B。并行累计时间不能直接从墙钟中扣减。15 次冲突由 13 次 `event_changed` 和 2 次 `timeline_structure_changed` 组成，没有声纹来源失效拒绝。结构化日志已可定位窗口、Chunk、基准/当前版本和冲突字段；它只记录每轮首次拒绝原因，不恢复已丢弃的完整 Proposed。

声纹在 12/24 Understanding 提交时启动，固定 seq0–9、13、18 的来源快照。最终为 5 人各登记 5 条、共 25 条，均为同模型/revision 的有限 192 维单位向量；来源、区间和 PCM 摘要无重复或交叉，目标 47–49 秒不在参考内。ready 后到终态参考未变化，没有提前领取 Refine。参考身份仍来自 Agent 选择，没有逐条人工试听校准；5 条参考也不等于覆盖 5 种情绪。

VoiceprintAgent 的生成调用耗时 **6:29.539**，25 次编码累计仅 **3.914 秒**；准备变慢不能归因于 CPU 向量计算。全部 Understanding 在约 19:25:43 提交，ready 在约 19:28:18 提交，随后开始 Refine，因此此处在 Understanding 完成后额外等待约 **2 分 35 秒**；前面的准备与 Understanding 重叠，不能把整个 6 分 29 秒视为新增关键路径。首条参考约在 VoiceprintAgent 启动 251 秒后出现。现有用量口径包含模型及工具往返，不能进一步断言等待全部来自某一种内部步骤。

## 目标失败路径与 null 结果

用户确认的「祥ちゃんも悪くない／也不是小祥的错」说话人是长崎爽世。本轮原始 ASR 把 **35.6–48.6 秒**合成一条 `speaker1`，末尾识别成「咲ちゃんも悪くない」。初始 Understanding 将整段归为千早爱音，既没有拆出接话，也没有保留 null。

覆盖目标的 seq0 Refine 共生成两轮，分别为 319.626 秒和 171.252 秒。第一轮因 chunk2 的 `177200–178000ms` 事件双方同时修改 speaker/description 被拒绝；第二轮成功结果只更新 seq1/2，没有修改 seq0。最终 chunk0 仍为 v1，数据库及当前版本 ES 中目标均仍落在归千早爱音的长段内。不能把别处的真实并发冲突解释为声纹保护阻止了目标修正。

全任务有 **270 条成功声音比较记录**，其中 seq0 窗口有 20 条；按 `start_ms < 49000 && end_ms > 47000` 扫描，覆盖目标的成功记录为 **0 条**。本轮没有目标的 CAM++ 比对结果，不能说 CAM++ 错认人物。现有 metadata-only 轨迹不能排除在参数/来源校验阶段已被拒绝、尚未产生成功比较日志的工具尝试。证据见 [目标失败追踪](target-failure-trace.json)。

| 本轮持久化阶段 | 总条数 | speaker 为 null |
|---|---:|---:|
| 原始 ASR | 353 | 12（speaker_id） |
| 首次观测 Understanding | 308 条 DIALOGUE | 43 |
| 最终 Timeline | 314 条 DIALOGUE | 31 |

这些计数说明提示允许弃权，不能证明人物识别准确率提高。目标本轮仍暴露混合发言没有拆开、错误具名台词未得到有效声音核验的问题；仅加强 null 提示没有解决这个案例。

## 终态验收与证据

- [终态验收](final-verification.json)：24/22/24 成果、ES 当前版本、参考静态一致性和任务资源回收通过，目标正确身份及独立边界验收为 false。
- [原系列保留检查](original-preservation-check.json)：原 13 条视频、13 条任务、312 条正文的版本、时间及规范内容 SHA-256 全部不变。隔离第四集源文件 SHA-256 不变。
- [参考来源审计](reference-audit.json)：25 条来源可追溯、任务/video 隔离和模型一致性通过，不替代人工身份标注。
- [耗时对照](latency-comparison.json)：冻结配置、调用账本、并行口径、冲突原因与重复比较统计；[当前 ES 文档](es-documents.json)保留索引证据。
- 终态活动租约与模型占用为 0，capacity 已释放，临时媒体目录无残留，API/Consumer 健康。未进行迁移、commit/push 或原 13 集重解析。

## 本轮条件

- 视频 `0019c16c-a7b7-5f6c-8275-5019869c1207`，隔离系列 `bdbf28ae-5404-58e4-8c98-699433499ffc`，第四集；源 SHA-256 `50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814`。
- 代码基点 `25bda94` 加工作区内本任务修改：Refine 冲突原因日志、声纹准备后不再对照 timeline 作废参考，以及 ASR/Understanding 的强制 null 提示。实际文件摘要见 `source-sha256.json`，不能把 HEAD 当作所有被测代码。
- 当前 `config.yaml` 的生成模型由用户改为 `gpt-5.6-sol`。本轮 Understanding/Voiceprint 使用当前生成绑定，Refine 继承 Understanding；不改用户配置文件。ASR、CAM++、索引、24 个 60 秒 Chunk、22 个三 Chunk 窗口、三阶段并发 10、失败预算 0、声纹工具预算 96 等沿用 v5/v7 请求。
- Prefix Compact 关闭：`compact_enabled=false`、`prefix_context=null`、`models.prefix_context=null`。不人工注册参考、不注入目标正确答案。
- ASR 无法可靠分辨匿名声线时，必须输出 `speaker_id=null`，保留听清的原语台词；Understanding 无可靠人物归属证据时，必须输出 `speaker=null`，不能按对话顺序、出现/提及人物或剧情补名。两字段职责不同。
- 正式声纹准备成功后，登记的 speaker、向量和音频区间是本任务的可信静态成果；timeline 后续文本、人物、时间或事件删除不作废参考，也不阻止提交。仍按原任务/video 隔离和 ready 准入，比较排除实际音频重叠及重复 PCM。

## 服务与执行边界

用户已在本轮前重启 API。新增提示在 Python 导入时加载，因此在提交本轮解析前还需要 API 加载本轮新代码；只重启 API，Web/MySQL/Elasticsearch 保持原进程，不初始化 Schema 或运行迁移。

首次只读 preflight 显示 API 健康、无活动解析租约，但有一条正在执行的 Chat；等待其正常结束，不取消或中断它。重启及请求记录分别保存为 `restart-*.json` 和 `reparse-*.json`。

11:14:07 UTC 首次观测现有 Chat 已结束，随后仅重载 API。旧 API 对 SIGTERM/SIGINT 未在脚本限定时间退出，按同一管理脚本升级 SIGKILL 后启动新 PID 91385；未中断活动解析或 Chat。Web 仍复用 PID 17842，MySQL/ES 未重启。提示文件修改早于 API 重载，实际请求与预览完全一致；见 `run-start.json`。等待现有 Chat 的时间不计入 v8 解析墙钟。

`control.py` 通过现有 `/api/uploads/reparse` 仅提交一次此隔离视频，数据库连接只读；`collector.py` 从本目录响应自动绑定 task，采集状态、版本、原始 ASR、目标台词变化、模型用量及轮转冲突日志。`verify.py` 按静态声纹契约核对终态、ES 当前版本、临时文件清理和原系列 13 集未变，不重新实施 timeline 指纹门禁。

## 验收口径

1. 区分 ASR 匿名标签与 Understanding 人物名，记录初始/最终 null 数量、目标台词归属变化；null 数量本身不代表准确率。
2. 核对 ready 前不领取 Refine，准备后参考保持静态，Voiceprint 与 Refine 的真实阶段耗时及临时音频清理。
3. 统计实际 Refine 生成轮数、快照刷新和结构化拒绝原因，核对全部 24/22/24 成果及最终 ES 写回。
4. 原系列 13 集保留运行前基线，结束后比对视频、任务和内容记录，不自动重解析。

对照成功的 v7 墙钟 43:22.011，也可参考 v5 的 38:43.518。但本轮同时更换生成模型、加强未知说话人提示并移除动态声纹失效条件，不能把总时差单独归因于某项修复；模型输出和选样仍有随机性。代码接通、耗时和人物识别分别验收。

v7 归属基线见 `baseline-attribution-counts.json`：352 段 ASR 中 1 段匿名标签为 null；首次观测的 24 份 Understanding v1 共 379 条 DIALOGUE，其中 90 条 speaker=null；最终共 367 条、66 条 null。这是持久化条目计数，不是跨 Chunk 去重后的独立发言数，也不是准确率指标。

提示实参回归：`tests/engine/common/test_transcription.py` 与 `tests/agent/video_parse_v2/test_framework.py` 共 47 项通过，hypnos-review 通过；声纹/合并链路此前相关 292 项通过。本目录被 Git 忽略，保留本地实验，不 force-add 原始快照、向量、日志或音频。
