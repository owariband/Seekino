# Video Parse 成果表迁移（2026-10-31）

关联：[主 TRD](../design/03-product-reassessment/local-whisperx-asr-trd.md)、[Draft PR #18](https://github.com/owariband/Seekino/pull/18)。实现入口为 [`scripts/migrate_video_parse_content.py`](../../scripts/migrate_video_parse_content.py)。

## 数据去向与范围

| 数据 | 迁移前 | 迁移后 |
|---|---|---|
| 完整 ASR | 协调器进程内，退出即丢失 | `video_parse_content.asr`，MySQL JSON；准备时校验并加载 |
| 匿名声源分组（11-01 增量） | 无正式持久成果 | `video_parse_content.diarization`，独立 JSON；不改原 ASR |
| Summary 完整提交与帧回执 | `video_parse_task.detail._worker_runtime_v2.summarize.result/frame_receipts` | `video_parse_content.summary` |
| Summary 调度、租约、重试、完成与发布回执 | 任务运行状态 | 原位置保留 |
| 标题、简介、封面 URI 的当前展示值 | `video` | 原位置保留，后续与 Summary 成果在同一事务更新 |
| 封面图片、原片 | ArtifactStore | 原位置保留 |
| Chunk 事件与 ES 投影 | 现有 Chunk 成果与索引 | 原位置保留 |

成果 ORM 与 Chunk／Interval 同置于 [`server/model/parse_content.py`](../../server/model/parse_content.py)，元数据 DTO 位于 [`parseContentDTO.py`](../../server/model/dto/parseContentDTO.py)。任务定义位于 `server/model/task.py`；单纯模型归类不增加数据库迁移；同日后续退出旧 Job／Checkpoint 另需执行[退役迁移](20261031_retired_parse_models.md)，不能仅执行本说明即启动新代码。每个 `video_parse_task` 至多一条成果行，字段为 `id`、唯一外键 `video_task_id`、`asr`、`diarization`、`summary`、`version`、`detail`，另有公共 `create_time/create_date/update_time/update_date`。不是每个视频一行：新解析版本有自己的任务和成果，Retry 沿用原任务。

ASR 正文是既有归一化结构 `{transcript, language?}`；`*_seconds` 字段继续遵循项目整数毫秒合同。`detail.asr` 保存输入／模型／来源配置指纹、Schema、正文摘要及完成审计。来源身份独立于租约；当前租约下完成的 ASR 可以被后续合法租约复用。局部模型与对齐权重按内容哈希识别，首次校验有磁盘读取成本；远程 API 只能识别声明的来源配置和模型名，不承诺核实供应商内部权重。

11-01 增量：已迁移过旧版成果表的数据库仍需追加可空 `diarization JSON` 列。
同一迁移入口在预览中报告 `add_diarization_column`，停写后 `--apply` 幂等添加；
不会复制实验分组或修改原 ASR。`detail.diarization` 保存独立媒体／权重／参数身份、
正文摘要和租约审计。新启动检查要求此列存在，即使当前不启用分组也须迁移。

## 执行顺序

1. 核对正在使用的工作树、控制服务、数据库 URL 与环境；备份 MySQL 业务数据和配套媒体。先预览候选数量，预览不执行 DDL 或数据更新。
2. 停止旧 API/Consumer 等写入者；新旧版本不能并行写 Summary，否则旧运行状态可能重新出现正文。
3. 使用目标环境执行迁移。建表与数据事务分开：MySQL DDL 会隐式提交；数据搬迁可回滚，脚本可重入。已存在且不同的 Summary 会报错并回滚数据更新，不覆盖冲突成果。
4. 保持停写，完成[旧模型退役迁移](20261031_retired_parse_models.md)。核对两次迁移计数、正文／证据和任务状态，运行现有 `init-db` 结构检查，再用原控制服务启动新代码。

下列命令在目标工作树执行；环境文件是示例，应选已经核对的实际配置，不在命令行粘贴数据库凭据：

```bash
# 只读预览；读取 SEEKINO_DATABASE_URL 与 SEEKINO_ENVIRONMENT。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_video_parse_content

# 写入者已停止、备份已完成后执行。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_video_parse_content --apply --writers-stopped
```

已有业务库缺少该表时，新代码启动校验会明确拒绝继续。不能用直接 `create_all` 代替迁移历史 Summary。空的新测试库按现有初始化路径创建完整 Schema。

## 历史数据与回退边界

- 历史 ASR 未持久保存，不能凭 Chunk 局部 evidence 或 Turbo 实验文件回填；该脚本不会运行模型，也不会自动重试任务。
- 旧 Summary 没有保存当时发布的封面 URI。迁移保留原 submission 与帧回执，新行的 `cover_uri` 为 `null`，并记录 `legacy_cover_uri_unavailable`。当前 Video 封面不变，不从可能已被后续解析或人工编辑覆盖的值反推历史事实。
- 任务状态、版本、租约与原时间戳保留；只移除已成功搬迁的 `result/frame_receipts` 两个键。
- 回退应停止新写入者并协调代码与数据库备份版本。新产生的 ASR 只有新表保存，不能通过删表“回退”；旧代码没有读取持久 ASR 的能力。旧、新 Summary 冲突需先核对来源再处理。
- ASR 与 Summary 分列、按阶段更新，但仍属于同一行；若数据库使用 `binlog_row_image=FULL`，写 Summary 的行事件可能仍包含 ASR。这里减少的是高频任务状态更新对成果的牵连，并未测得 binlog 缩减比例；不要把成果反复累计改写。

## 已执行验证与部署状态

2026-10-31：746 项相关后端回归通过；独立 Python 子进程从文件数据库恢复 ASR、禁止构造 Provider 的检查通过；独立 MySQL 8.4.13 的 15 项迁移／恢复／原子提交检查通过。MySQL 测试以临时目录、Unix socket 和独立 `_test` 库运行，禁用 TCP，实例已关闭。

同日第一轮模型归类后，25 张表的字段、约束和索引与重构前一致（后续退役变为 22 张，见退役迁移说明）；独立进程迁移入口与 15 项隔离 MySQL 检查再次通过。Server 全量回归及两处过期断言的修正记录见 [devlog](../dev/devlog.md#devlog-video-parse-content-20261031)，原始证据位于 `outputs/verification/task-model-refactor-20261031/`。

恢复测试包括协调器失败、Understanding 终态失败后的正式 Retry 持久路径、新 runtime／数据库连接、独立进程、暂停继续、旧租约迟到、身份不符与损坏正文。计数模型在首次运行转写一次，恢复新增转写为零；2／10 Chunk 的区间合同仍受覆盖。使用模拟模型，不代表真实模型质量或课程闭环验收。

本轮代码位于 `fix/video-asr-recovery` 工作区，未部署。开发 MySQL 当前未运行，尚未执行开发库回填；未重启现有 API，未触发旧课程 CPU ASR。原始记录保存在 `outputs/verification/video-parse-content-20261031/`（Git 忽略）。

定向回归可运行 `tests/server/test_video_parse_results.py`、`test_video_parse_content_migration.py`、`test_full_video_asr.py` 和现有 Summary 测试。真实 MySQL 用例为 `tests/server/test_video_parse_results_mysql.py`：仅当显式设置 `SEEKINO_ISOLATED_MYSQL_SOCKET` 指向 `/private/tmp/seekino-vpc-mysql-*` 临时实例时执行，否则跳过；测试只创建／删除独立随机 `_test` 数据库。
