# Event 查询内存热修复与任务排序补丁：生产部署记录

日期：2026-11-14（Asia/Shanghai）。热修复基于已合并 `dev` 的 [PR #154](https://github.com/owariband/Seekino/pull/154)，任务排序补丁为 [PR #159](https://github.com/owariband/Seekino/pull/159)，设计见 [TRD](../../../../design/02-agent-harness/event-scope-task-sort-trd.md)；当前状态见[工作台](../../../../dev/workbench.html) `RAG-QUERY-MEM-01`。本报告只保留公开镜像、聚合采样与只读探针，不含访客身份、凭据或私有配置；发布证据保存在 Cloud 发布目录 `releases/event-memory-sort-2bebd9af-20261114/`。

## 部署范围

PR #154 的 Event 查询有界内容读取已合入 `dev`（`8f0f14d3`），但 CPU 生产 API 仍运行旧 main 镜像（`a0fb93d9`），真实素材库继续走 ES 召回前的全量正文与完整 detail 读取路径。

- **第一次热修复（2026-11-14 00:38 UTC+8）：** API 镜像 `seekino-api:hotfix-event-memory-8f0f14d3`。在现有生产镜像上只覆盖四个后端运行文件：`searchDTO.py`、`repository/video_parse_chunk.py`、`repository/video_parse_retrieval.py`、`service/video_parse_read.py`。
- **最终热修复（2026-11-14 00:50:20 UTC+8）：** API 镜像 `seekino-api:hotfix-event-memory-2bebd9af`，`sha256:66b296c20370a52240c76fe9fb401748bdc1470ca1e0e34a1e46810d2244f9e6`，健康检查通过；在第一次镜像上仅替换 `server/repository/video_parse_retrieval.py`。
- 整个 Compose 的环境变量、挂载和其他服务逐项比较未变，只有 `api.image` 变化；旧镜像保留用于回滚。
- 没有合并或发布 main，没有修改 agent、gateway、prompt、模型配置、Schema、ES 索引重建或媒体资源；DeepSeek Flash 的 agent 与 supervisor `reasoning_effort=high` 保持原值。

## 第一次真实游客 Chat

从 `auth/guest` 进入，选择公开课程素材库，问题为「attention 是怎么让 token 互相交流的？请检索课程，给出相关原片时间段。」，`topK=5`。实际发生四次 `search_video_graph` 工具调用，最终 9 条命中覆盖 E07，HTTP 200，整轮 41.487 秒。

对 API 的 Python 进程（不是 docker-init）每 200 ms 采样：

| 观测 | 数值 |
| --- | ---: |
| 请求前 RSS | 297.40 MiB |
| 请求及结束后 15 秒观察窗口内最高 RSS | 377.88 MiB（+80.48 MiB） |
| 请求结束 RSS | 375.51 MiB |
| 空闲 15 秒后 RSS | 375.63 MiB（相对请求前 +78.23 MiB） |
| 宿主可用内存最低值 | 3,311.17 MiB |
| swap / OOM | 0 / 0 |

这是一轮真实 Chat，不是旧镜像与新镜像的配对对照；历史记录中的 GiB 级整轮 Chat 数据来自其他工作负载，不能与本次单轮采样相减，也不能据此宣称整轮 Chat 内存降低 3 GB 或某个百分比。

## 完整范围与任务排序补丁

热修复上线后，对八部课程的完整 Event 查询范围做只读验收，`list_video_tasks` 报 MySQL 1038：`Out of sort memory`。查询同时选择 `parse_config` 与 `detail._event_index_target_v1`，再按视频、版本和任务 ID 排序，JSON 投影被 filesort 一并携带。

[PR #159](https://github.com/owariband/Seekino/pull/159) 先在原过滤和排序条件下只查询任务 ID，再按这些 ID 读取元数据且不再带 `ORDER BY`，最后按原 ID 顺序生成 DTO。没有提高 MySQL 全局排序缓冲，代价是多一次轻量数据库往返。

回归覆盖 `tests/server/test_public_course_readiness.py`、`test_video_retrieval_service.py`、`test_video_event_retrieval.py`，结果 **61 passed, 1 skipped**，另有一条 AnyIO 弃用警告。主 Agent 使用项目既有 `.venv` 执行 `uv run --no-sync python -m pytest`，通过 `UV_PROJECT_ENVIRONMENT` 指定该环境。

## 只读隔离探针

候选代码在生产容器的独立只读进程中运行，不修改服务进程，覆盖八部课程：

| 阶段 | 聚合结果 |
| --- | --- |
| 范围解析 | 8 个视频、880 个 Chunk 身份；5 次查询；0 个正文 ORM 实体；37.73 ms；RSS +0.50 MiB |
| 候选回源 | 5 个候选；68.04 ms；累计 RSS +4.89 MiB；完整 detail 加载 0 |
| 真实 ES 词法 Event 召回 | 查询 `attention tokens communicate`；8 个 Event、5 个 Chunk、覆盖 8 个视频；`partial=false`；862.42 ms；RSS +28.50 MiB；完整 detail 0 |

探针使用 SQL 捕获和 ORM load 监听确认完整 detail 未加载；`compact` 与 `evidence.camera_hints` 小字段保留由公开课程回归用例验证。该探针不包含 embedding 与 LLM 生成，不等同端到端 Chat。同一探针早前冷启动为 320.83 ms、RSS +0.38 MiB，暖缓存与冷启动的差异不作为优化收益。

## 最终镜像的真实验收

最终镜像直接运行原探针，无临时替换模块：8 视频、880 个 Chunk 身份全部取得；范围阶段 37.22 ms、RSS +0.50 MiB、正文 ORM 0；回源 5 块 69.40 ms、累计 RSS +4.95 MiB、完整 detail 0。真实 ES 词法召回 8 Event / 5 Chunk，覆盖 8 视频且 `partial=false`，612.03 ms、RSS +28.60 MiB、完整 detail 0。

同一个 API Python 进程连续接受两个新游客会话，期间没有重启。每 200 ms 采样，观察窗口包含请求和结束后 15 秒，记录的是整个 API 进程 RSS，不能把全部变化精确归因于某个 Python 对象。

| 查询 | HTTP / 状态 | 请求耗时 | 请求前 RSS | 窗口内最大增量 | 请求结束 RSS | 结束后 15 秒 RSS | 相对请求前净增量 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| attention / token 交流 | 200 / COMPLETED | 33.641 s | 289.56 MiB | 86.57 MiB | 372.76 MiB | 376.13 MiB | 86.57 MiB |
| 反向传播 / micrograd backward | 200 / COMPLETED | 97.811 s | 380.88 MiB | 40.36 MiB | 419.67 MiB | 419.92 MiB | 39.04 MiB |

两个窗口内宿主可用内存最低分别为 3,129.82 / 3,206.86 MiB，swap 均为 0，OOM 计数增量均为 0。第二条问题与第一条不同，耗时不能用来比较代码版本。验收结束时镜像健康，重启计数 0，`OOMKilled=false`。

发布目录保存 `deployment.json`、`guest-memory-summary.json`、`guest-memory-samples.json` 和 `rollback.py`；第二轮位于其 `smoke-backprop/` 子目录。第一版发布目录 `releases/event-memory-8f0f14d3-20261114/` 保留首轮证据；其最初 `deployment.json` 采到了 docker-init，已补说明，Python 内存以 guest 采样为准。第二次重启后的 Python RSS 287,596 KiB 仅用于记录启动状态，重启下降不作为修复收益。

## 验证边界

- **已验证：** 两次镜像部署；最终镜像健康；Compose 差异仅 `api.image`；首版一次及最终版同进程连续两次真实游客 Chat 完成；最终版完整 Event 范围、候选回源与 ES 词法召回；任务排序补丁 61 项回归通过。
- **未验证：** 同素材、同配置的旧镜像与新镜像整轮 Chat 对照；长时间运行和多用户并发内存曲线；任何百分比或“整轮 Chat 降低 3 GB”的结论。
- **口径区分：** 游客 Chat 是 JEV 整轮链路（含 Supervisor、工具编排和模型生成）；隔离探针只测 Event 服务与 ES 词法召回，不包含 embedding 与 LLM，两者不能直接换算。
