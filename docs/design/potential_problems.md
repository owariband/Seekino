# 开发中的潜在问题与验收缺口

本文件记录开发过程中发现的观察与缺口，不替代已有 TRD、阶段地图或验收合同。只有可复现结果才能将“待验证”升级为“已证实缺陷”；本地替身回归不作为真实 MySQL、Elasticsearch 或模型效果证明。

## VP2-001｜manual retry 与 Worker 成果提交的锁序观察

- 记录日期：2026-10-02；归属：Server 解析 pipeline；验收阶段：07B（08A 的前置门槛）。
- 状态：2026-10-05 隔离真实 MySQL 已复现 `1213 Deadlock`；2026-10-06 完成锁序修复与用户确认的自然收尾语义，并通过本项真实 MySQL 复验。
- 首次观察与复现：`retry_parse()` 原先 Chunk → Interval → parent，而 `commit_chunk_result()` / Refine 提交存在 Interval → Chunk 的方向。两个真实用例在生产 SQL 实际取得行锁后用 Event 控制交错，复现循环等待；当时全文件为 17 passed / 2 failed。不是测试伪造的加锁路径，也不是已发生的生产事故。
- 当前修复：手动 retry 统一为 Interval → Chunk → parent；另外记录持久失败意图、停止该视频新派单，让已领取 Chunk / Refine / Index 正常提交或实际回收后，再发布整集失败。自然收尾不能代替一致锁序；已有活跃租约检查发生在取锁之后。
- 验证：在 `tests/server/test_mysql_integration.py` 使用独立连接和已取得行锁的 Event 同步，覆盖 retry 与 Worker 领取／提交竞争、有限等待、整笔回滚及成果／租约不被部分重置。不以 Barrier 同时起跑或 SQLite 通过替代锁竞争证明。
- 补充验证：增加 Chunk / Refine / Index × 有效租约 / 已到期 owner 的 6 个真实场景，在真实成果 UPDATE 后、事务提交前暂停；并发执行完整回收与收口，必须保持运行态，提交后保留成果并最终失败。原两条锁序竞争用例不弱化。
- 历史环境阻塞：2026-10-06 首次以此前隔离的 `anivag_v2_local_20261005_test` 配置尝试全 25 个 MySQL 用例，localhost:3306 拒绝连接。10 个失败 / 15 个 setup errors 均为建连失败，没有执行锁竞争；该次不算修复复验结果。
- 本次真实复验：用户要求继续测试后，仅启动项目已有 MySQL 实例，确认版本为 8.4.11、隔离级别为 REPEATABLE READ；不启动 API / Worker，不初始化目录或迁移业务数据。原 2 个 retry / 提交锁循环场景与新增 6 个提交收口场景串行定向运行 3 轮，分别为 8 passed（3.15s / 3.21s / 3.17s）；完整 MySQL 集成文件为 25 passed（4.37s），没有跳过或 `1213 Deadlock`。
- 同版最终回归：Queue、Repository、Upload、Worker、普通 reparse API 和真实 MySQL 集成合并运行，149 passed（12.62s），1 条已有 Starlette deprecation warning。竞争夹具的 `mysql-v2-*` Series 查询为 0，测试库仍为原 21 张表；不据此证明其他全库夹具或业务数据验收。
- 处理边界：本轮没有新增生产或测试代码修改，只补本条证据；未调用付费模型、未 commit / push。MySQL 保持运行，其他服务未启动。本项修复在覆盖的真实竞争场景中未再复现死锁，不承诺系统所有路径永不死锁，也不代表 07B / 08A 全部必需项通过；当前阶段标记不移动。

## VP2-002｜正式解析产出到 Search／删除恢复的组合验收缺口

- 记录日期：2026-10-02；归属：Parse V2 Agent／Server；验收阶段：07B／08A。
- 状态：本地组合回归已补并通过；真实环境、Chat／CLI 演示与产品质量门槛仍未通过。现有生产入口已实现，不是功能缺失结论。
- 依据：`tests/server/test_video_parse_worker.py` 已跑真实 Agent／Runtime／Queue／Repository 并产生 receipt，但使用 SQLite、替身 ES／媒体／模型；现有 V2 retrieval 与 Search API 测试分别使用 Repository／Store 或 retrieval 替身。尚缺正式成果与 receipt 直接进入正式 retrieval hydrate、Search HTTP 和删除恢复的组合证据。
- 潜在影响：单个组件通过可能遗漏跨入口的 task／content version／generation／receipt 不一致、未 COMPLETE 提前开放、删除失败后错误回退等问题。
- 验证：复用既有 Worker 测试入口，串起上传、索引单项失败／恢复、正式检索与 HTTP、reparse 后拒绝旧任务、删除清理失败及重试；媒体与模型均为替身，不产生付费调用。完整窗口与唯一短窗口两种配置均通过；receipt 六字段 provenance 与正式 Search 命中身份一致，恢复仅补失败索引项，不重算 embedding／解析，删除恢复后精确清空对应 ES 文档。
- 证明限制：本地组合仅验证调用链与持久不变量；同版真实 MySQL／ES／模型环境及产品质量门槛未通过时，07B／08A 不签收。

## VP2-003｜Prefix 失效后再次推进的 committed marker／ABA 观察

- 记录日期：2026-10-02；归属：Server Prefix Repository／Queue；验收阶段：07B。
- 状态：本地组合不变量已验证，未发现旧 Worker 获得写入权；不替代真实 MySQL 验收。
- 观察：`invalidate_series_prefixes_from()` 保留 `prefix.committed_leases`；本轮开始时已有回归证明失效后旧 heartbeat／retry／commit 被拒绝，但缺“旧提交 → 失效 → 新链游标再次越过旧位置”的独立序列证据，现已补齐本地覆盖。
- 验证：在 `tests/server/test_video_parse_repository.py` 同时检查历史 committed 识别、当前执行租约及旧 heartbeat／retry／commit／release 的影响，并保留正常同一链跨游标的历史提交识别。
- 本轮结果：旧提交后的检测为 True，失效归零后为 False，新链游标再次越过旧位置后为 True；旧 heartbeat／retry／release 均为 False，commit 抛出 `VideoParseLeaseLostError`。每次旧操作后立即比较完整 parent detail／update_time 与两块 Chunk 的正文、版本、detail／update_time，保护新 lease 到期时间、调度时间和 compact，不允许后续操作掩盖污染。新链仍可继续正常提交；该文件直接回归 `16 passed`，增强快照后的最终同版结果见证据索引。
- 处理边界：历史提交标记可见不等于当前写入有效；不得仅凭 marker 保留清理历史或重做架构。按完整序列与 Runtime 宿主影响决定是否需要修复。

## VP2-004｜SQLite 并发 Worker 不能替代行锁验收

- 记录日期：2026-10-02；归属：Server 测试环境；验收阶段：07B／08A。
- 状态：本地 SQLite 调试已观察到限制；不是 MySQL 生产故障结论。
- 观察：两个 Chunk Worker 在 SQLite 上并行提交同一 Interval 时，双方可能读取同一旧 `ready_mask`，最终只留下其中一个位，两个 Chunk 已 COMPLETE 但 Interval 仍 WAITING。Repository 的 `FOR UPDATE` 在 MySQL 上用于序列化这些行，SQLite 不执行该行锁语义。
- 处理：本地产品组合用例使用一个 child 执行槽、启用 SQLite FK 并设置有限等待，验证调用链和恢复不变量；真正并发证明仍交由隔离 MySQL 的独立连接用例。没有为迁就 SQLite 修改生产事务、bitmap 或增加另一套通知机制。

## VP2-005｜短视频唯一短窗口被 manual retry 误判为不相容

- 记录日期：2026-10-02；归属：Server Upload Repository；阶段：07B／08A。
- 状态：本地组合回归已复现并完成最小修复；完整窗口／唯一短窗口组合回归 `2 passed`。上传层错误／缺失窗口的直接回归与阶段全量另在同版证据索引登记。
- 复现：20 秒视频、10 秒 Chunk、30 秒 Interval，由正式上传和 Worker 建立合法 `[0,2)` 短窗口。单项索引失败持久化为 INDEXING_FAILED 后，HTTP `/api/uploads/retry-parse` 返回 409。
- 原因：`_parse_preparation_complete()` 使用 `max(0, chunk_count - interval_size + 1)` 得到零窗口，并要求每个窗口成员数等于完整 interval_size；与 Engine `plan_intervals()` 的唯一短窗口合同不一致。
- 修复：Server 准备校验直接消费已有 Engine `plan_intervals()`，按规划的实际 start／count 校验成员和完整性；不修改 Engine、Schema、任务身份或已确认短窗口规则。
- 验证：上述完整上传／失败／HTTP retry／恢复／正式 Search 组合场景必须通过，且完整滑窗及原有 retry／reparse 回归继续通过；真实环境和模型质量仍不由该本地结果推出。

## VP2-006｜同一个 Runtime 跨 event loop 重用的生命周期观察

- 记录日期：2026-10-02；归属：Server Runtime／Consumer；阶段：07B／08A。
- 状态：本地调试已观察到跨 loop 重用失败；是否影响真实服务生命周期尚未验证，不写成生产重启故障。
- 观察：组合用例最初对同一个 Consumer／Runtime 执行 close 后再 start。Consumer 创建新 event loop，但 Runtime 的 `_change_events` 保留绑定旧 loop 的 `asyncio.Event`，再次等待同一任务时报 `is bound to a different event loop`，任务落入 INDEXING_FAILED。
- 当前处理：正式 HTTP retry 在持续运行的同一个 Worker loop 内验证；正常应用重建时会重新装配 Runtime。测试不直接清空私有事件缓存，也不为绕过该观察修改生产生命周期。
- 后续核验：确认复用实例的 start／close／start 或多次 `run_once_sync()` 是否属于正式支持的调用合同；若纳入，补独立生命周期回归后做最小 Runtime 修复。跨 loop 重用的观察不能与当前同 loop retry 通过混为一谈。

## VP2-007｜真实 ES mapping 默认分词器被 Server 校验误拒绝

- 记录日期：2026-10-05；首次真实复现：2026-10-04；归属：Server ES storage；阶段：07B／08A。
- 状态：已证实并完成最小修复；本批真实 ES 6 个用例已通过，不代表整个阶段通过。
- 复现：ES 8.19.22 按 Server 自身 `_mapping()` 成功创建索引后，普通 mapping 回读省略与 `analyzer` 相同的 `search_analyzer`；`ensure_index()` 的逐键比较报 `index field 'lexical_fields' differs`，6 个真实 ES 用例均停在 fixture 初始化。字段查询 `include_defaults=true` 证明实际 `analyzer`／`search_analyzer` 都是 `cjk`。
- 修复：只在 `server/storage/elasticsearch.py::_mapping_contains()` 对实际／预期均为 text、预期要求 search_analyzer、实际缺该键且存在 analyzer 的情形，用局部浅拷贝补实际 analyzer，再复用原递归比较。显式不同值／null、缺失或错误 analyzer、非 text、nested／dynamic／向量约束继续拒绝；不修改返回对象、预期 mapping、Engine 合同或索引 generation。
- 直接回归：新增 12 个场景；旧实现 `3 failed / 9 passed / 25 deselected`，修复后 Storage／CLI ES／Config／Index Coordinator／Retrieval 五文件定向集 `262 passed`（5.30s）。
- 真实验证：已安装的 ES 8.19.22 在独立临时目录、localhost:19200 测试节点上执行 `tests/server/test_elasticsearch_integration.py`，`6 passed`（17.07s）。项目正式 `start_elasticsearch()` 用实际开发维度 1024／1024 连续校验两次通过；只操作隔离空验证索引，所有自建测试索引已清理。
- 证明边界：真实的是 ES mapping／查询／版本／恢复行为；Repository 场景仍用 SQLite，媒体／模型仍为替身。不证明真实 MySQL 锁竞争、真实模型效果或上传产品整链。本批没有恢复业务 ES :9200、启动 Consumer、迁移、业务索引重建、付费调用、commit／push；当前位置仍是 08A，02 合同待评审。
