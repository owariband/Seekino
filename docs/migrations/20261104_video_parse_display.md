# Video Parse 展示元数据列迁移（2026-11-04）

关联：[展示读源切换 TRD](../design/02-agent-harness/video-display-source-trd.md)。实现入口为 [`scripts/migrate_video_parse_display.py`](../../scripts/migrate_video_parse_display.py)。

## 数据去向与范围

| 数据 | 迁移前 | 迁移后 |
|---|---|---|
| 展示元数据列名 | `video_parse_content.summary` | `video_parse_content.display`，`ALTER TABLE ... RENAME COLUMN` |
| 展示结构 | `{"submission", "cover_uri", "frame_receipts"}` | 扁平 `{title, title_kind, description, episode_order, cover_uri, evidence, frame_receipts}` |
| 展示审计键 | `detail.summary` | `detail.display` |
| 标题、简介、封面 URI 的当前展示值 | `video` 表三列（展示读取方） | 改由 `video_parse_content.display` 提供，`video` 三列原位置保留但不再读写 |
| 历史封面 | 旧成果 `cover_uri` 为空、`video.cover_uri` 有值 | 按每个 Video 最新一条展示成果补齐，不覆盖已有封面 |

每个 `video_parse_task` 至多一条成果行。展示读取取每个 Video 最新（`video_parse_task.version` 最大、未删除）且 `display` 非空的成果行；没有成果时沿用既有默认回退。`submission.cover` 不再单独保存，其帧证据仍保留在 `evidence` 与 `frame_receipts`。

## 执行顺序

1. 核对工作树、控制服务、数据库 URL 与环境；备份 MySQL 业务数据和配套媒体。先预览计数，预览不执行 DDL 或数据更新。
2. 停止旧 API/Consumer 等写入者；新旧代码不能并行写展示值。
3. 使用目标环境执行迁移。DDL（列改名）与数据事务分开：MySQL DDL 会隐式提交；数据改写可回滚，脚本可重入。标题或简介与 `video` 三列不一致时预览报告冲突，`--apply` 会报错并停止，不覆盖任何展示值。
4. 保持停写，运行现有 `init-db` 结构检查确认通过，再用新代码启动控制服务。

下列命令在目标工作树执行；环境文件是示例，应选已经核对的实际配置，不在命令行粘贴数据库凭据：

```bash
# 只读预览；读取 SEEKINO_DATABASE_URL 与 SEEKINO_ENVIRONMENT。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_video_parse_display

# 写入者已停止、备份已完成后执行。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_video_parse_display --apply --writers-stopped
```

新代码启动校验要求 `video_parse_content` 存在 `display` 列且不存在旧 `summary` 列，否则拒绝继续并提示先执行本迁移。

## 历史数据与回退边界

- 迁移只重命名列、转换旧结构、补齐历史封面，不删除 `video.display_name / description / cover_uri` 三列；删除属于不可逆操作，另行确认。
- 标题或简介与 `video` 三列不一致时不覆盖，报错停止，需先核对来源再处理。
- 回退应停止新写入者并协调代码与数据库备份版本；列已改名，不能通过删表“回退”。
