# 已提交任务派发和索引收尾对照

2026-11-07，macOS arm64、本机 MySQL 8.4.13。改前代码为 `e3255fb1`，最终实现为 `0941b20b`（[PR #70](https://github.com/owariband/Seekino/pull/70)）。[TRD](../../../../design/03-product-reassessment/parse-ready-dispatch-trd.md) 记录预先定义的目标。

## 业务基线

micrograd v5 的任务 `68602d50-8da7-4d68-982e-0550ce7f8e6c` 有 146 块、144 个窗口，understand、refine、index 都允许同时运行 10 个。整集从开始到完成用了 90 分 04.884 秒，最后一个 refine 后约 39 分钟才收尾。历史诊断记录了 26,821 个候选行次与 454 次成功领取（含再次领取）。4,520 次索引模型调用累计 191.445 秒。子任务详情共有 104,550,235 字节，其中索引向量缓存 101,877,643 字节。原始记录在本地 `outputs/verification/parse-output-drafts-live-20261107/` 的 `comparison.json`、`final-integrity.json` 和 `db-snapshots.jsonl`。

## 相同已完成数据的对照

同一个只读测试库，交替顺序运行五轮。比较 146 块的 ID、媒体顺序、阶段、状态、租约、版本和正文存在性，五轮逐项相同。计时包括 SQL、传输和 Python 转换。原始样本单位为毫秒。

| 读取方式 | 五轮样本 | 中位数 |
| --- | --- | ---: |
| 完整子任务、正文、详情及 DTO 拷贝 | 1747.42、1442.57、1458.50、1393.23、1429.18 | 1442.57 |
| 修复后生产入口的状态投影 | 7.58、3.88、4.49、3.87、4.57 | 4.49 |

[查询对照脚本](benchmark_progress.py) 使用 `SEEKINO_TEST_DATABASE_URL`。从该版本仓库根运行 `PYTHONPATH=. python docs/report/server/mq/parse-ready-dispatch/benchmark_progress.py <task-id>`。

## 固定调度负载

专用 Unix socket 与测试库，同样的 146 块、144 个滑动窗口、批大小 10、零派发间隔。按三个阶段轮流填充至 10 份工作，再次使用真实的 MySQL 成果提交和租约入口。使用固定理解结果及模拟发布回执，隔离数据库开销。阶段依赖、最终状态相同。候选按当前领取阶段的任务 ORM 实体实际载入数计，SQL 按数据库驱动执行次数计。每轮均成功领取 146 / 144 / 146 次。

| 指标 | 改前 | 改后 |
| --- | ---: | ---: |
| 被载入的 understand / refine / index 候选 | 10,731 / 10,720 / 787 | 146 / 144 / 146 |
| 该试验的全部 SQL | 14,867 | 12,881 |
| 三轮从首次领取到整集完成的秒数 | 10.486、13.122、11.343 | 4.828、5.518、6.755 |

[固定负载脚本](benchmark_queue.py) 只接受专用 `seekino_ready_benchmark_test`。用同一支脚本分别将 `PYTHONPATH` 指向两个 checkout，改后传入 `--dispatch`。三轮中位数从 11.343 秒降到 5.518 秒。这里的总耗时只度量该负载。

## 本地 embedding 和 ES 试点

选取同一个 micrograd v5 已提交的正文和内容版本，用新的子任务、空向量缓存及每次独立的 ES Chunk/Event 物理索引。从队列领取开始计时，到整集持久状态完成且最后一个索引 handler 退出结束。用正式 `VideoParseRuntimeService.index`、本机 `seekino-qwen3-embedding-0.6b`（文本、1,024 维）和 ES 实际写入、查询可见校验。进程收尾允许与下一份工作重叠，并发按数据库中处于 RUNNING 的 index 租约采样。此环境单独运行索引，已提交的理解和 refine 成果作为输入。

| 前 12 块，冷索引 | 用时（秒） | 采样有效租约峰值 | Chunk / Event 文档 |
| --- | ---: | ---: | ---: |
| 原版，有领取探针 | 44.751 | 4 | 12 / 224 |
| 修复版，有领取探针 | 17.642 | 10 | 12 / 224 |

首次试验保留了已知工作遇忙父行立即跳过的行为，用时 53.154 秒。同批原版为 45.018 秒。按 ID 领取降低了候选成本，但空槽仍会等到下一次轮询。加入有界等锁后，同一批的第一次复验为 19.315 秒，15 次立即跳过后再等锁均成功。表中复验原版有 21 次父行忙而跳过，修复版 10 次遇忙的父行均在等待内取得。等待的 MySQL session 变量限定在这次父行查询并在退出时还原。硬上限、暂停、子任务状态与版本继续在同事务复核。

同一批 12 块的用量从实际的 `model_usage` 按 `run_id` 核对，均为 420 次成功的文本编码、21,246 个输入 token。Gateway 从模型请求发出到返回的平均用时为 27.06 ms 和 68.97 ms。并发提高时单次请求需要排队，但这批工作完成得更早。按源块开始时间和事件序号逐份比对 ES，文本、时间、版本、事件类型和词法字段一致，420 个 1,024 维向量的最大逐元素差为 0。

146 块完整冷索引两次分别用时 **347.069 秒和 332.441 秒（5 分 47 秒、5 分 32 秒）**，4,520 次本机模型请求均成功（231,352 个输入 token，单次均值 42.61 ms）。有效租约采样峰值 10，146/146 个块的已索引版本与当前内容版本一致，两次剩余子租约均为 0，ES 可见 146 个块、2,307 个事件。历史 v5 在最后一个 refine 后约 39 分钟仍有索引在途，两个计时覆盖的工作量不同。

[逐轮数值和 ES 对照](pilot-results.json) 包含任务 ID、耗时、租约采样和领取探针。原始试点输出在本地 `outputs/verification/parse-ready-dispatch-20261107/`，脚本 `index_pilot.py`，包括较慢的第一版。试点使用独立父任务和新的 ES 索引。原视频、原任务和原索引只读。整集包含 ASR、understand、refine 的新总时间与 DeepSeek 费用仍待相同条件实跑。


## 回归与交付边界

714 个定向用例通过，覆盖队列、提交、worker、索引协调器、ASR 与 Agent 解析合同。真实 MySQL 单独运行两个集成文件，30 个用例通过，包括六个消费者竞争、超时后的再次领取、连接池设置还原、失败与暂停、旧租约、摘要回滚与恢复。扩大回归时，Cloud Chat 夹具缺少 `max_query_chars`，Refine 单独构造的 runtime 夹具缺少 `_refine_subscription`。在未修改的 `e3255fb1` 分别复现了 8 和 6 个失败参数用例。

正式部署、从 ASR 开始的全流程用时及其 DeepSeek 费用待测。此次试点只向本机 embedding 端点发送模型请求。


复跑回归使用仓库的 Python 3.12 环境。以下两个入口分别使用 SQLite fixture 和专用测试 MySQL。第二个入口需要设置 `SEEKINO_TEST_DATABASE_URL` 和 `SEEKINO_ISOLATED_MYSQL_SOCKET` 指向自己的隔离实例。

```sh
python -m pytest -q tests/server/test_video_parse_repository.py tests/server/test_video_parse_queue.py tests/server/test_video_parse_worker.py tests/server/test_video_parse_results.py tests/server/test_full_video_asr.py tests/server/test_video_parse_index_coordinator.py tests/server/test_parse_ready_dispatch.py tests/agent/video_parse_v2
python -m pytest -q tests/server/test_video_parse_mysql_integration.py tests/server/test_video_parse_results_mysql.py
```
