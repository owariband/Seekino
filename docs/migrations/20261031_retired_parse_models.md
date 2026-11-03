# 旧 Job／Checkpoint 退役迁移（2026-10-31）

关联：[主 TRD](../design/03-product-reassessment/local-whisperx-asr-trd.md)、[Draft PR #18](https://github.com/owariband/Seekino/pull/18)。入口：[`scripts/migrate_retired_parse_models.py`](../../scripts/migrate_retired_parse_models.py)。代码已移除旧模型；本说明不代表真实库已迁移。

## 变化与数据边界

| 对象 | 迁移操作 |
|---|---|
| `series_parse_job`、`video_chunk_checkpoint`、`parse_usage_attempt` | 删除退役表及其中旧执行／检查点／用量记录 |
| `upload_session.job_id` | 移除旧列、索引和外键，上传记录保留 |
| `parse_model_call` | 删除 `job_id IS NOT NULL` 的 V1 调用；移除该列及旧约束／索引／外键 |
| 现行模型调用身份 | `video_task_id`、`execution_kind`、`execution_task_id` 改为必填，保留 V2 行和内容 |
| Video、V2 Task／成果、原片与封面 | 不删除或改写 |
| 旧 ArtifactStore `jobs/` 目录 | 脚本不读取或删除；运行代码不再维护该旧目录 |

新库只注册 22 张表，不需退役迁移。已有库若残留上述旧表／列，启动校验会拒绝并提示脚本，不能靠 `init-db` 自动删除。HTTP 响应移除 `jobId`／`latestJobId`，前后端须同步升级；`videoParseTaskId`、现行任务版本与租约参数保持。没有 V2 Task 的视频显示未解析，不通过旧 Job 状态恢复就绪。

## 执行顺序

1. 核对工作树、数据库 URL／环境和当前服务控制者；备份数据库与配套媒体。先只读预览旧表行数、旧引用数量及现行用量身份是否完整。
2. 停止 API／Consumer 等写入者。先完成[视频成果迁移](20261031_video_parse_content.md)，保留停写窗口，再执行本脚本；不允许旧、新版本并行写库。
3. 受控执行后核对返回结果，旧表与旧关联均应为空，`current_usage_identity` 为 `true`。检查 V2 行与备份快照一致，并运行现行 `init-db` 结构校验。
4. 配套前后端同步上线，再由原控制服务启动。旧任务不会因此自动重跑 ASR。

```bash
# 默认只读；使用核对过的配置，勿在命令行粘贴数据库凭据。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_retired_parse_models

# 已备份且所有写入者停止后执行。
uv run --no-sync --env-file /absolute/path/to/verified.env python -m scripts.migrate_retired_parse_models --apply --writers-stopped
```

脚本拒绝未知外键指向旧表、Job／V2 双重身份以及缺失／非法的现行用量身份；先核对来源，不能猜测补值。MySQL DDL 会隐式提交，因此迁移不是整体事务；各步按现有结构判断，支持中断后重入。再次执行已完成的迁移无变更。回退必须使用对应版本的数据库备份并协调代码版本，不能只回退代码后继续写入；旧记录删除后不能由本脚本重建。

## 隔离验证与部署状态

2026-10-31，在临时 MySQL 8.4.13 中构造升级前 25 表及 V1／V2 数据：只读预览、停写门禁、非法现行身份无写入拒绝、旧库启动门禁、迁移后全部 V2 行保留、DDL 中断重入、22 表当前校验与重复执行均通过。另 36 项现行 MySQL 回归通过；Server 全量 2,726 项通过（120 项外部集成未配置而跳过），最后清理再移除 1 项旧 ID 用例并复验现行 11 项，最终保留 2,725 个通过项，前端应用类型检查与 153 项相关测试通过。

临时实例禁用 TCP，仅使用独立目录和 Unix socket，验证后已关闭。真实开发库和历史文件未清理，未重启现有服务、运行真实 ASR、commit／push 或部署。原始快照与日志在 `outputs/verification/retired-parse-models-20261031/`（Git 忽略）；永久测试中不再保留退役模型 fixture。
