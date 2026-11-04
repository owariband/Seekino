# 解析任务管理试点（2026-11-04）

- 对应 TRD：[parse-task-control-trd.md](../../../design/03-product-reassessment/parse-task-control-trd.md)；PR [#36](https://github.com/owariband/Seekino/pull/36)。
- 环境：分支 `fix/parse-task-control`（da207663 起）的 API 跑在课程库的**一次性克隆** `seekino_task_control_20261104_test`（13361）上，ES 索引与媒体根目录独立（`seekino_task_control_*_20261104_test`、`outputs/verification/parse-task-control-20261104/library`），租约 30 s / 心跳 5 s。克隆前先在克隆库内暂停 micrograd，课程库原任务未受影响（核对：原库 720608b1 仍为 PARSING、租约属 course-loop-dev-20261104）。
- 未调用任何付费模型：运行中删除与暂停均在本地整集语音转写（ASR）阶段完成；素材为 micrograd 第 600–780 s 的 3 分钟片段。

## 结果

| 检查 | 修改前（dev 152fce8e） | 修改后 | 证据 |
| --- | --- | --- | --- |
| 可删除覆盖 | 历史 0/11，运行中 0/1 | 历史 11/11（`canDeleteVersion`），运行中 `canDelete=true` | 列表 API 枚举 |
| 默认视图 | 13 张卡片（全部状态 × 当前版本筛选） | 进行中 1 张；全部 2 张，历史版本收进卡片 | `01-default.png`、`02-history-open.png` |
| 单个 / 批量删除历史版本 | 无入口 | 浏览器中删除 GPT v1，再对两个视频“删除全部历史版本”；API 仅剩 2 个当前版本，无错误提示 | `walk2.mjs` 输出 |
| 运行中删除 | 拒绝（UploadStateError） | 确认后 216 ms 卡片消失；ASR 执行在 5 s 内因租约失效取消（`ModelCallCancelledError`）；首次续删在租约未过期时静默跳过，52 s 后完成清理（会话 `DELETION_PENDING` 清除，Video 与任务行删除） | `05-running-delete-confirm.png`、api.log `upload_delete_deferred` → `upload_deleted` |
| 暂停 / 继续 | — | 暂停立即释放租约（owner=None），继续后 8 s 内重新领取；随后运行中删除返回 200 并从列表移除 | `ctl.py` 输出 |
| 任务列表耗时 | 4.6 s / 次（页面每 5 s 轮询） | 0.19–0.80 s；仓储层 2.5 s → 0.23 s，13 个任务的进度与计时输出逐字节一致 | `perf:` 提交说明 |
| 回归 | tests/server 2830 passed / 14 failed | 2842 passed / 同样 14 个既有失败，无新增；web 571 passed，typecheck、build 通过 | 命令输出 |

列表慢的原因：进度统计读取了每个 Chunk 的完整 `ChunkParseContent.detail`，其中含索引 embedding，课程库平均 351 KB / 行，每次轮询约 170 MB。改为只投影需要的三个字段。

## 未验证

- 运行中删除只在 ASR 阶段实测；Chunk 理解 / Refine / Index 阶段的 fencing 由单测覆盖（`tests/server/test_upload_service.py`、`test_parse_control.py`），未做付费实测。
- 课程库正式服务（19641）仍运行 497a35cf，未部署本分支。
