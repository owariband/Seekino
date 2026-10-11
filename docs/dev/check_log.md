# AniVAG 项目检查记录

检查日期：2026-10-10。检查对象为当前工作区，包含检查开始前已有的未提交修改。

本文件用于逐项 Review。问题保留稳定编号、影响、触发条件、证据及验收建议，避免把测试环境限制误记为代码 bug。首次审查建议未实施；后续用户授权启动前端联调，CHK-022 已按下述记录修复，其余建议仍待 Review。

## 2026-10-10 本机前端联调启动复核

- 一键启动资料库探针已使用当前 `tenantId` 合同，复用 `AuthConfig.local_tenant_id` 并编码查询参数。真实路由回归覆盖默认及自定义 Tenant；`tests/server/test_local_services.py` 为 **32 passed**。
- 现有 Python 环境与锁文件一致，无需同步依赖；前端构建通过，19 个文件、**229 项单测通过**。
- 旧开发库因 `workspace_id` 与旧字段合同被当前 Schema 校验拒绝。按用户选择，新建 `anivag_local_20261010_development`、`anivag_video_chunks_local_20261010_development`，媒体目录为 `outputs/server/local-20261010/artifacts`；旧库、旧索引与媒体均保留，未执行历史数据迁移。
- `.env.local` 已切换到新联调资源，原配置保存在忽略目录 `outputs/local-services/config-backups/20261010-150638-before-frontend-dev.env`，不进入 Git。
- 原生 ES 8.19.22 与 MySQL 8.4 已运行；`./start.sh` 首次启动及再次复用均成功，`./start.sh status` 四项全部运行。API 为 `127.0.0.1:9621`，Web 为 `127.0.0.1:5173`；启动器仍要求提前准备 ES。
- API 直连与 Vite 代理共 **11 个 GET 全部 200**，包括身份、Consumer 健康、Series、V2 任务、Job 列表以及 OpenAPI。报告：`outputs/local-services/frontend-startup-smoke.json`。
- 真实 Chromium 验证 `/watch`、`/library`、`/tasks` 三页，身份与列表正常，Consumer running，页面显示真实空库及 API 已连接；页面 JS 错误、console error 与 alert 均为 0。报告与截图：`outputs/local-services/frontend-ui-smoke/`。
- 本轮验证到本机启动、身份恢复、列表与页面联调，未上传视频或调用真实模型，不能视为当前完整解析→检索→问答的验收。

## 检查结论与验证范围

首次当前工作区审查已完成，覆盖启动入口、依赖与测试、上传与续传、V2 解析、检索问答、播放器、身份与权限、任务恢复及 Engine/Agent/Gateway/Server 分层。共记录 22 条问题；CHK-022 已修复，剩余 21 条待 Review：P1 1 条、P2 16 条、P3 4 条。

结论：当前一键启动及真实前端页面联调已通过，旧库未迁移，本机使用新的独立空库；Consumer 仍有 CHK-005 的恢复问题。实际观看、重试、续传、移动操作及键盘访问仍有明确缺陷，不能认定当前完整流程已跑通且好用。真实外部服务和模型质量的验收边界单独列在文末，不把模拟结果或历史版本实测作为当前全链路通过。下表保留首次审查结果，当前启动状态以上方复核为准。

建议先 Review CHK-005（Worker 恢复），然后检查 CHK-010（续传来源）、CHK-012（额度幂等）、CHK-013（删除后上下文）、CHK-014（无音轨解析），以及 CHK-008/009/017/019/020 的日常操作体验；CHK-001..004 为过时测试合同，需要恢复有效回归保障。

| 检查项 | 状态 | 证据与限制 |
| --- | --- | --- |
| 当前工作区 | 已核对 | 原有 35 个 tracked 文件有修改；本记录在检查开始时是空的 untracked 文件。 |
| Python 自动化测试 | 已执行，存在失败 | 默认全量：2768 passed / 19 failed / 83 skipped，96.37 秒。其中 7 个失败是沙箱禁止测试 HTTP 服务监听端口；沙箱外重跑 tracing 文件为 8 passed / 1 skipped。另有 12 个 API 问答用例失败见 CHK-004。 |
| 前端单元测试与构建 | 已通过 | `npm test -- --reporter=dot`：19 个文件、229 项测试通过，11.14 秒；`npm run build` 通过。`npm ls --depth=0` 未发现依赖缺失/版本失配。浏览器模拟流程另行验证。 |
| 浏览器既有流程 | 已通过，限模拟 API | 沙箱外 `npm run test:e2e -- --reporter=line`：2 项 Chromium 流程通过，1.8 秒。范围为游客初始化、邮箱登录、个人/公共资料切换和退出；不覆盖真实 SMTP、后端鉴权、视频解析、检索与播放。 |
| 播放器与故障体验复现 | 已执行，确认 4 条问题 | 临时 Vitest 检查 5 项通过，分别确认结束重播失效、解析重试失效、续传文件身份校验不足、Storage 读/写异常。Chromium 加载当前生产构建与真实 2 秒 H.264 MP4，确认 CHK-008 和 CHK-011；API 为模拟响应。正常页面无 JS 错误、无未处理的模拟 API 路径；390px 视口无横向页面溢出，不能据此认定完整移动体验已验收。 |
| 观看 Chat 与键盘/移动端体验 | 已执行，确认 4 条问题 | 临时组件检查 3 项通过；当前生产构建 + 真实 MP4 的 Chromium 复现 CHK-017..020。6 次 Tab/Shift+Tab 只在两处循环；页面卸载后一次 focus 增加 4 个请求；媒体丢失时普通文字提问未发出；390×844 下回答设置被底部导航拦截。API 为模拟；未验证真实手机软键盘。报告 `/private/tmp/anivag-audit-ui/frontend-deeper-report.json`。 |
| Python 依赖与锁文件 | 已通过 | `uv sync --locked --dry-run --offline`：Resolved 102 / Checked 97 packages，Would make no changes；未安装或修改依赖。 |
| 现有服务与运行入口 | 当前未就绪 | 沙箱内、沙箱外 `./start.sh status` 均显示 MySQL TCP、API / Parse Consumer、Web、Elasticsearch health 未就绪；`python -m server.cli --help` 正常。未将当前服务未启动直接认定为代码 bug。 |
| 隔离真实 MySQL 8.4 | 已执行，存在失败 | 独立随机端口与临时数据库：139 passed / 2 failed / 2 skipped，14.82 秒；2 个失败见 CHK-001。MySQL / ES 集成脚本路径 `/private/tmp/anivag_audit_integration.py`，运行产物 `/private/tmp/anivag-audit-integration-6m3372nu/`。 |
| 隔离真实 Elasticsearch 8.19.22 | 已执行，存在失败 | 独立临时 data/config/logs 与随机端口：15 passed / 1 failed / 2 errors，14.58 秒。失败见 CHK-002、CHK-003。测试结束后临时 MySQL/ES 进程已停止，数据与日志留存。 |
| 当前合同的隔离复核 | 已通过，使用临时测试副本 | 只在 `/private/tmp/anivag-audit-fixtures/` 更新 nullable、Snapshot.members 与毫秒坐标，保留业务断言。真实 MySQL 用量并发两组通过；ES 部分 bulk 恢复、删除后晚到 writer 隔离、真实 MySQL→ES→Agent 回答持久化三项通过（10.71 秒）。模型、embedding 和索引媒体准备为替身；仓库原用例仍待修复。日志 `/private/tmp/anivag-audit-integration-at3x6lbl/`。 |
| 正式 HTTP 总装配启动 | 隔离环境已通过 | 使用正式 `server.main`、全新 MySQL Schema、真实 ES 地址、local 身份和空任务队列启动。8 个 GET 均 200，health 为 `ok / parseConsumer=running`，身份为 `local-user / local-default`。虚拟模型凭据，未调用模型；不等同于当前原有服务已运行，亦不证明上传到问答的真实模型质量。报告 `/private/tmp/anivag-audit-integration-at3x6lbl/http-smoke-report.json`。 |
| 一键启动器的资料库就绪检查 | 已复现阻断 | 当前真实 FastAPI 路由与 production library_ready/start_applications 联合执行：旧 workspaceId 查询返回 422、tenantId 返回 200；API 健康已就绪仍抛资料库查询失败，不进入 Web 启动。详见 CHK-022；本项使用只读空库替身，未重启原有服务。 |
| 当前模型配置与 Refine 合同 | 离线已通过 | 正式 .env.local 解析与 prepare_service_configuration 成功，当前 V2 默认绑定合法，所需凭据仅检查存在，不打印值、不验证 Provider 认证。历史 raw 的 36 条事件经当前 Canvas 两处局部修改后仍为 36 条（17/19），移除原事件被拒绝；没有模型请求/数据库写入。报告 `/private/tmp/anivag-audit-current-model-evidence.log`。 |
| 代码与体验问题 | 本次审查已完成 | Server、Engine/Agent/Gateway、Web 分别审查；22 条记录区分生产缺陷、过时测试、文档/结构建议，有复现与仅有源码证据的结论分别标明。 |

## 待 Review 问题

### CHK-001 · P2 · MySQL 用量并发测试仍要求全部字段 NOT NULL，未适配身份迁移

- 状态：已在隔离真实 MySQL 8.4 复现；属于测试合同过时，尚无证据表明这里是生产表定义错误。
- 触发条件：配置 `ANIVAG_TEST_DATABASE_URL` 后，运行 `tests/server/test_model_usage_repository.py::test_mysql_real_schema_and_concurrent_call_id_replay` 的两组 MySQL 参数。
- 现象与影响：两个用例都在字段断言处失败，后面的并发同 `call_id` 重放验证根本没有执行；默认不配 MySQL 时会跳过，CI 配 MySQL 时会失败。
- 代码证据：[`tests/server/test_model_usage_repository.py:222`](../../tests/server/test_model_usage_repository.py#L222) 对全部列断言 `nullable == False`；[`server/model/parse_usage.py:41`](../../server/model/parse_usage.py#L41) 已定义允许 NULL 的 `actor_user_id`，与历史记录可缺少 actor 的身份迁移合同一致。
- 复现结果：隔离集成共 `139 passed / 2 failed / 2 skipped`；具体失败保存在 `/private/tmp/anivag-audit-integration-6m3372nu/mysql-tests.log`。
- 建议：按字段分别校验 nullable；保留 `actor_user_id` 允许 NULL 的合同，继续执行原并发重放测试。
- 验收：两组 MySQL 参数通过，且实际走到并发 INSERT / 幂等与冲突校验；历史无 actor 的记录仍可读取。
- 补充复核：临时副本按 `actor_user_id` 分别核对 nullable 后，两组真实 MySQL 并发用例通过；SQLite 两组按原合同跳过。生产表及仓库测试未修改。

### CHK-002 · P2 · 两个真实 ES 恢复/删除测试仍用秒构造毫秒时间字段

- 状态：已在隔离 Elasticsearch 8.19.22 复现；测试 fixture 与当前时间合同不一致。
- 触发条件：配置 `ANIVAG_TEST_ELASTICSEARCH_URL`，运行 `test_real_es_coordinator_recovers_partial_bulk_without_reparsing` 或 `test_real_es_delete_reconcile_fences_an_immediately_late_writer`。
- 现象与影响：两项测试都在 fixture 准备阶段抛出 `VideoParseConflictError: Chunk plans do not match the frozen parse config`；真实 ES 的部分 bulk 恢复与晚到 writer 隔离尚未进入被测部分。
- 代码证据：[`tests/server/test_elasticsearch_integration.py:172`](../../tests/server/test_elasticsearch_integration.py#L172) 配置 `chunk_duration=10` 秒，但 [`184`](../../tests/server/test_elasticsearch_integration.py#L184) 写入 `duration_seconds=20`、[`212`](../../tests/server/test_elasticsearch_integration.py#L212) 使用 `(0,0,10)/(1,10,20)`；这些历史命名的业务字段和坐标当前保存整数毫秒。[`server/repository/video_parse.py:1055`](../../server/repository/video_parse.py#L1055) 将配置秒数乘 1000，再核对冻结的分块范围，正常拦住了不一致 fixture。
- 复现结果：2 个 setup errors，日志 `/private/tmp/anivag-audit-integration-6m3372nu/es-system-tests.log`。
- 建议：将 fixture 的业务时长、分块坐标与后续 timeline/evidence 坐标一起按当前毫秒合同更新；保持配置的 `chunk_duration` 为秒，不删除生产侧冻结配置校验。
- 验收：两项用例真正进入 bulk 失败恢复、删除后晚到 writer 的阶段，并验证索引/业务版本最终一致；不能只让 fixture 不报错。
- 补充复核：临时副本统一时长、分块和事件坐标为毫秒后，两项真实 ES 用例通过，确实完成部分 bulk 失败恢复、原结果复用、删除响应丢失后的 reconcile 和晚到 writer 的 409 校验。

### CHK-003 · P2 · Agent 系统集成测试仍把 IntervalMemberSnapshot 当作列表

- 状态：已在隔离 MySQL + ES 复现；测试未适配包装了成员列表的 Snapshot 返回合同。
- 触发条件：同时配置隔离 MySQL 与 ES，运行 `tests/server/test_agent_system_integration.py::test_agent_runs_over_mysql_and_es_retrieval_and_persists_the_answer`。
- 现象与影响：测试抛出 `TypeError: 'IntervalMemberSnapshot' object is not iterable`，尚未验证真实检索与最终回答持久化。
- 代码证据：[`tests/server/test_agent_system_integration.py:274`](../../tests/server/test_agent_system_integration.py#L274) 获取 `load_interval_members` 返回值，随后 [`277`](../../tests/server/test_agent_system_integration.py#L277) 直接迭代该值；[`server/model/dto/jobDTO.py:249`](../../server/model/dto/jobDTO.py#L249) 定义的 Snapshot 包含任务 ID、起始序号、分块数量和 `members`，不是旧列表。
- 复现结果：1 failed，日志 `/private/tmp/anivag-audit-integration-6m3372nu/es-system-tests.log`。
- 建议：从 Snapshot 的 `members` 读取成员及各成员的内容版本，核对 `commit_interval_refinement` 的当前参数合同，保持生产侧成员身份、版本与指纹校验；同一 fixture 的时长、分块、事件和 Query 选择范围也应统一当前毫秒合同。
- 验收：完整执行 Refine 提交、ES 查询、Agent 证据审核与 SQL 回答持久化，保留检索范围与证据对应断言。
- 补充复核：临时副本更新上述合同后，真实 MySQL + ES 用例通过；当前 Agent Query/Evaluator/Chat Canvas 执行、证据 video block 及最终两条消息的 SQL 持久化断言均完成。模型输出和媒体/embedding 为确定性替身。

### CHK-004 · P2 · 12 个 API 问答回归用例的模型替身仍调用旧 Draft Tool

- 状态：当前完整 Python 测试已复现；根因定位为测试模型替身未适配 Context Canvas 工具合同，不能据此断言真实模型问答必然失败。
- 触发条件：运行 `tests/server/test_api.py` 中会进入 Evaluator 的问答、范围选择、幂等重放、JSON/SSE 和 Accept 协商用例。
- 现象与影响：12 项回归失败，JSON 请求返回 502，流式用例收到失败结果；大多在 Evaluator 阶段，另有证据 fixture 在 Retrieving 阶段失败，日志包含 `AgentModelInvocationError`、`root_error_type=KeyError`。API 问答关键合同目前无法取得有效回归结果。
- 代码证据：[`tests/server/test_api.py:272`](../../tests/server/test_api.py#L272) 的 `_AgentModelStub.run_structured` 仍索引 `get_draft_catelog` 和 `read_draft_item`；当前 [`agent/chat/video_search/evaluator.py:62`](../../agent/chat/video_search/evaluator.py#L62) 要求用 `get_context_canvas` 遍历并精读 Draft。该 API fixture 注入的是 `AgentRuntime`，因此这组失败不属于 Supervisor Tool 名称不匹配。
- 复现结果：全量日志 `/private/tmp/anivag-audit-pytest.log`；失败涵盖 `test_agent_conversation_send_and_list_messages`、`test_same_send_path_supports_live_events_and_idempotent_replay` 等 12 项。
- 建议：更新模型替身的 Canvas 遍历与精读行为，按当前审核合同构造响应；保留 API 用例对范围、幂等与 SSE 的业务断言。
- 验收：12 项原用例通过，且 Evaluator 确实完成所需目录页与条目精读，不通过跳过审核或固定返回 complete 消除失败。

### CHK-005 · P1 · Consumer 轮询遇到一次临时数据库异常后永久停止

- 状态：已用当前生产 `VideoParseConsumer` 和只在首次轮询抛错的 Queue 替身复现；属于运行可靠性问题。
- 触发条件：MySQL 的过期租约扫描或领取任务阶段抛出一次 `OperationalError`，例如数据库短暂重启/连接中断。
- 现象与影响：异常退出整个 Consumer 线程；数据库恢复后不再轮询或自动重启。HTTP 进程仍可存活，后续上传的任务可能停在队列里，直到人工重新启动 Consumer/服务；health 会显示 failed，未发现现有代码中有自动恢复调度。
- 代码证据：[`server/mq/video_parse_consumer.py:160`](../../server/mq/video_parse_consumer.py#L160) 的 Queue 轮询异常没有局部恢复，沿 `run_forever` 退出到 [`143`](../../server/mq/video_parse_consumer.py#L143) 只记录 `_failure`；[`server/bootstrap.py:68`](../../server/bootstrap.py#L68) 仅在服务 start 时调用 Consumer.start。
- 复现：构造 `poll_interval=20ms` 的 Consumer，Queue 第 1 次 `reap_expired` 抛 `OperationalError`，其余调用本可成功；启动后分别等待 250ms、250ms，两次结果均为 `running=False / failure=OperationalError / poll_calls=1`。复现未连接真实数据。
- 建议：对明确可恢复的依赖异常提供有界退避与重新领取；协议/结构异常仍应失败并告警。也可由运行层提供明确的 Worker 自动恢复，而非仅检查线程存活。
- 验收：独立环境里让 MySQL 短暂不可用后恢复；Consumer 能恢复轮询，已有租约/新任务继续处理，且不重复提交、不绕过 fencing；持续异常仍可观察并停止。

### CHK-006 · P2 · CI 没有接入 ES/Redis 的真实集成门禁

- 状态：源码确认的覆盖缺口；当前隔离 ES 执行已暴露 CHK-002、CHK-003，证明默认跳过会掩盖测试合同失效。
- 代码证据：[`.github/workflows/ci.yml:68`](../../.github/workflows/ci.yml#L68) 的 backend job 只配置 MySQL 和 `ANIVAG_TEST_DATABASE_URL`，没有 ES、隔离 Redis 及对应测试 URL；前端 job 只执行单测与 build，没有 Playwright 步骤。
- 影响：正式 V2 所依赖的索引恢复、真实 SQL→ES→Agent 链路和云端 OTP/额度 Lua 测试会跳过；现有浏览器流程即使退化，当前 CI 也无法直接发现。手动 dispatch 的审批策略本身不作为本条问题。
- 建议：在现有审批方式下提供独立 ES/Redis 集成 job 和浏览器 job；明确依赖就绪后需要执行的用例，并防止相关用例全部 skip 仍被标绿。
- 验收：CI 输出真实执行数量，进入 SQL→ES→Agent 与 Redis OTP/额度阶段；运行配置错误或合同失效能够使该门禁失败。

### CHK-007 · P3 · 所有路由页面静态导入，首屏携带完整应用代码

- 状态：维护性/加载性能建议；未测得用户侧加载超时，不作为已复现体验 bug。
- 代码证据：[`web/src/app/App.tsx:3`](../../web/src/app/App.tsx#L3) 静态导入资料库、播放器、检索、任务、用量、设置和上传页面；本次 `npm run build` 产物主 JS 为 647.23kB / gzip 196.86kB，构建器发出大于 500kB 的 chunk 提示。
- 影响：首次只访问观看页也需获取全部页面逻辑；功能继续增加时首屏下载和解析成本会增长。
- 建议：在测量首屏网络与交互耗时后，优先按路由懒加载较少访问的设置/用量/管理页面，避免为消除提示直接调大 warning 阈值。
- 验收：分包后首屏主包和首屏耗时有可比测量，路由切换有加载反馈且没有功能回退。

### CHK-008 · P2 · 视频播放结束后，播放按钮无法重新播放

- 状态：组件检查及当前生产构建的 Chromium + 真实 MP4 均已复现。
- 触发条件：任意可播放视频自然播放到结尾，再点击底部播放按钮、画面中央播放按钮，或使用播放快捷键。
- 现象与影响：界面显示可以播放，但操作没有效果；需要先手动拖动进度条离开结尾，或重新加载页面，才能继续播放。
- 代码证据：[`web/src/features/player/VideoPlayer.tsx:119`](../../web/src/features/player/VideoPlayer.tsx#L119) 的 `resume` 在 `media.ended` 时直接返回；[`159`](../../web/src/features/player/VideoPlayer.tsx#L159) 的 `togglePlay` 将所有暂停后的播放操作交给该函数。结束事件将 UI 切回播放状态，但没有提供重播路径。
- 复现结果：真实 2 秒 MP4 播完后，点击播放前后均为 `paused=true / ended=true / currentTime=2 / duration=2`。临时组件检查中，点击前后 `media.play` 的累计调用次数均为 1。浏览器报告 `/private/tmp/anivag-audit-ui/browser-report.json`，脚本 `/private/tmp/anivag-audit-browser.mjs`；API 为模拟，不依赖真实解析结果。
- 建议：明确结束后的播放行为，支持从头重播；区分用户主动重播与 Chat 暂停后的恢复行为，避免 Chat 恢复意外开始新一轮播放。
- 验收：自然播完后，底部/中央按钮和键盘均能重播；用户手动暂停、跳转和 Chat 中断后的恢复行为仍正确。

### CHK-009 · P2 · 上传面板的解析失败“重试”只重复完成上传，没有重新入队

- 状态：当前 UploadProvider + UploadDialog 已用模拟 API 和任务 SSE 复现；后端完成上传的幂等合同已核对。
- 触发条件：本次上传已完成、内存中仍保留 File，解析任务随后进入 `PARSING_FAILED` 或 `INDEXING_FAILED`，用户在浮动上传面板点击“重试”。
- 现象与影响：按钮将任务短暂显示为处理中，然后重新收到同一个任务的失败状态；解析没有重新入队。任务页已有真正的解析重试入口，本问题限定在上传面板。
- 代码证据：[`web/src/features/uploads/UploadDialog.tsx:120`](../../web/src/features/uploads/UploadDialog.tsx#L120) 对所有带 File 的 failed 任务提供同一个重试按钮；[`UploadProvider.tsx:599`](../../web/src/features/uploads/UploadProvider.tsx#L599) 定义 `retry: resume`。重新运行遇到 `COMPLETED` 会话走 [`276`](../../web/src/features/uploads/UploadProvider.tsx#L276) 的 `completeUpload`；[`server/service/upload.py:522`](../../server/service/upload.py#L522) 对已完成上传只返回原任务身份。真正的重试 API 是 [`web/src/api/client.ts:254`](../../web/src/api/client.ts#L254) 的 `retryUploadParse`。
- 复现结果：发出 failed SSE 后点击重试，`completeUpload` 累计调用 2 次，第二次订阅的任务 SSE URL 与第一次完全相同；再次发出失败快照，按钮又回到“重试”。未调用重新入队 API。临时检查 `/private/tmp/anivag-audit-ui/upload.test.tsx`，日志 `/private/tmp/anivag-audit-ui-tests.log`。
- 建议：按上传失败与解析失败选择对应动作；解析重试使用服务端 capability 和预期任务版本，成功后同步新的任务状态。也可直接引导用户到已有的任务页重试入口，避免提供无效按钮。
- 验收：在面板重试失败解析后，服务端任务真正重新入队，并最终更新面板；版本冲突、不可重试状态和网络重放有正确反馈，已完成上传不重复合并。

### CHK-010 · P2 · 续传仅核对文件名和大小，可以混合两个不同文件的分片

- 状态：前端选择行为已复现；隔离 SQLite + 真实上传仓库、Service、ArtifactStore 已确认混合字节可完成上传。媒体探测使用替身，本复现不证明混合产物一定能解码。
- 触发条件：原文件已有部分分片上传，恢复时选择另一个同名、同大小但内容不同的文件；创建会话时没有预先冻结完整源文件摘要，当前 Web 默认属于这种情况。
- 现象与影响：客户端保留已上传的旧分片，只从新文件发送缺失分片；各分片的 SHA-256 都可正确，却不能证明来自同一个源文件。最终产物可能损坏，也可能仍可解码但包含错误内容，错误不会在续传选择时被拦截。
- 代码证据：[`web/src/features/uploads/UploadProvider.tsx:485`](../../web/src/features/uploads/UploadProvider.tsx#L485) 的 `attachFile` 只检查名称和大小；[`249`](../../web/src/features/uploads/UploadProvider.tsx#L249) 的会话检查同样如此。创建请求 [`230`](../../web/src/features/uploads/UploadProvider.tsx#L230) 没有传支持的 `contentSha256`；[`server/service/upload.py:557`](../../server/service/upload.py#L557) 仅在会话已有摘要时核对完整文件。完成后的摘要由 [`server/repository/upload.py:1350`](../../server/repository/upload.py#L1350) 回填为合并产物摘要，不能补证原文件身份。
- 复现结果：原文件 `abcdefghij`，替换文件 `UVWXYZ1234`，二者名称 `episode.mp4`、大小 10 字节。保留原文件第 0 片 `abcd`，后两片取自替换文件，实际合并为 `abcdYZ1234`；初始会话摘要为空，完成状态为 `COMPLETED`。前端同样接受替换 File 并只上传缺失的 1/2 片。复现脚本 `/private/tmp/anivag-audit-mixed-upload.py`，日志 `/private/tmp/anivag-audit-mixed-upload.log`。
- 建议：冻结可以证明完整源文件身份的摘要/合同，并在恢复时核对；只有名称和大小的历史会话应明确采用能保证身份的恢复策略，不能默认视为同一文件。保留分片校验作为传输完整性保护。
- 验收：同名同大小但不同内容的文件在继续上传或最终合并前被拒绝，真正的原文件可跨刷新续传；缺失片恢复、分片重放与完整文件摘要同时正确。

### CHK-011 · P2 · 浏览器 Storage 不可用或写入失败会导致应用白屏

- 状态：Storage 读取 `SecurityError` 已在当前生产构建 Chromium 复现；写入 `QuotaExceededError` 已在 ThemeProvider 组件检查复现。异常由受控注入模拟浏览器限制，不声称当前普通浏览器默认会报错。
- 触发条件：浏览器策略禁用本地存储，或该 origin 的存储额度已满；初始化主题或保存主题时读取/写入抛错。
- 现象与影响：顶部 ThemeProvider 的异常逃逸，React 根组件无法渲染，整个应用空白且无恢复说明；主题偏好失败阻断了观看和检索等核心功能。
- 代码证据：[`web/src/app/theme.tsx:23`](../../web/src/app/theme.tsx#L23) 和 [`36`](../../web/src/app/theme.tsx#L36) 直接读取/写入 localStorage。[`web/src/main.tsx:39`](../../web/src/main.tsx#L39) 将 ThemeProvider 放在全应用外层。其他未保护存储访问还包括 [`web/src/app/workspace.tsx:52`](../../web/src/app/workspace.tsx#L52) 的初始化及 [`web/src/features/search/useAgentConversation.ts:30`](../../web/src/features/search/useAgentConversation.ts#L30) 的对话身份保存，修复主题一处不能代表完整降级已实现。
- 复现结果：Chromium 注入 `Storage.prototype.getItem` 抛 `SecurityError` 后打开 `/watch`，页面错误为 `Storage disabled`，`#root` 文本为空且子元素数量为 0。组件注入 `setItem` 抛 `QuotaExceededError` 后同样有异常逃逸。报告 `/private/tmp/anivag-audit-ui/browser-report.json`，截图 `/private/tmp/anivag-audit-ui/blocked-storage.png`。
- 建议：将可选浏览器偏好的读写提供默认值和异常降级；对话必要身份保留适当的内存会话与明确反馈，避免无法持久化时反复建立会话或白屏。
- 验收：分别禁用 Storage 读、写，或模拟 quota 满，页面仍可观看/检索，主题与资料选择可在本次会话使用；对话创建幂等及身份切换仍正确。

### CHK-012 · P2 · 云端问答的幂等重放和同 Key 冲突仍消耗体验额度

- 状态：当前 cloud HTTP fixture + 真实 SQLite、AuthService、AgentChatService 已复现；模型为替身，Redis 为按生产 Lua 的预算逻辑计数的替身，未作为真实 Redis 集成结果。
- 触发条件：同一消息的成功请求使用相同 Idempotency-Key 重放，或相同 Key 携带不同正文产生 409；原请求已完成，没有再次执行模型。
- 现象与影响：重放/冲突请求仍扣一次请求额度；网络恢复或重复点击可能耗尽游客体验次数，达到限额后连历史答案重放都返回 429。游客额度按 IP 共享，同 IP 其他游客也受影响。本条是请求预算问题，不是 Token 重复记账。
- 代码证据：[`server/api/agent.py:292`](../../server/api/agent.py#L292)（SSE）和 [`317`](../../server/api/agent.py#L317)（JSON）在 `start_message/send_message` 之前进入 `model_query`；[`server/service/access.py:68`](../../server/service/access.py#L68) 的 Lua 递增预算，[`110`](../../server/service/access.py#L110) finally 只释放并发租约。幂等检查位于 [`server/service/agent_chat.py:569`](../../server/service/agent_chat.py#L569)，已完成重放直接返回原消息。
- 复现结果：游客额度配置为 3，依次原请求、成功重放、同 Key 改正文、再次重放，HTTP 为 `[200,200,409,429]`；运行替身只启动 1 次，预算计数为 3，成功重放的 messageId 相同。脚本 `/private/tmp/anivag-audit-idempotent-quota.py`，日志 `/private/tmp/anivag-audit-idempotent-quota.log`。JSON 已实际执行，SSE 具有同样的源码调用顺序。
- 建议：协同幂等判断、新执行与原子额度准入；历史结果重放和未启动执行的幂等拒绝不消耗新模型请求预算，同时保留滥用限流和真正新请求的并发控制。
- 验收：同 Key 重放不启动模型、不增加执行预算；冲突不扣执行预算，新请求仍受限额及总并发限制；已获准入的异步工作在 HTTP 断开后仍正确释放并发租约。

### CHK-013 · P2 · 删除前序集视频后，后续 Prefix 仍包含被删除内容

- 状态：真实隔离 SQLite Schema、UploadRepository 与 VideoParseRepository 已复现。
- 触发条件：第 1/2 集的 Series Prefix 已生成，删除第 1 集，再领取第 3 集的 Prefix 工作；同样涉及删除中间集后的后续链。
- 现象与影响：被删除 Video 已不存在，但后续集的 `series_prefix_compact` 不失效，新的 Prefix 模型仍继承删除内容，可能把已移除的资料继续作为理解上下文。
- 代码证据：[`server/repository/upload.py:817`](../../server/repository/upload.py#L817) 的 begin 阶段只 fence 被删除 Video 的 V2 task；[`950`](../../server/repository/upload.py#L950) 的 finish 阶段删除 Video 并刷新 Series 状态，没有调用后续 Prefix 失效。对比 [`1563`](../../server/repository/upload.py#L1563) 的 reparse 失效 helper 和 [`server/repository/library.py:234`](../../server/repository/library.py#L234) 的集数修改。后续 [`server/repository/video_parse.py:2828`](../../server/repository/video_parse.py#L2828) 仍从完成任务读取旧 compact，[`server/service/video_parse.py:780`](../../server/service/video_parse.py#L780) 将租约的 Series compact 传给 Prefix 模型。
- 复现结果：真实 `begin_delete_session/finish_delete_session` 后，第 1 集不存在，第 2 集 Prefix cursor 仍为 1；真实 `claim_next_prefix` 返回的第 3 集租约继续包含 `episodes 1..2 INCLUDING SECRET FROM EPISODE ONE`。脚本 `/private/tmp/anivag-audit-prefix-delete.py`，日志 `/private/tmp/anivag-audit-prefix-delete.log`；标记文字为合成上下文，未使用用户资料。
- 建议：在删除 fencing 事务中失效受影响后续 Prefix，并明确后续正在执行的 Prefix 如何取消或版本拒绝；外部清理失败的重放仍保持依赖链正确。
- 验收：删除首集/中间集，覆盖后续 Prefix 已完成/正在执行、ES/文件清理失败后恢复；重新领取和重新生成的上下文不含已删除资料，旧版本无法提交。

### CHK-014 · P2 · 无音轨视频的 V2 媒体准备失败，关闭 ASR 也无法视觉解析

- 状态：真实 ffmpeg + 当前生产 VideoParseMediaService 已复现，尚未进入模型阶段。
- 触发条件：合法可播放的 MP4 只有视频轨，没有音频轨；选择直接视频/抽帧视觉或 OCR，甚至配置 ASR=none。
- 现象与影响：媒体准备无条件提取 WAV，ffmpeg 返回 `Output file does not contain any stream`，V2 解析失败；原视频可播放，不应因不需要的音轨阻断视觉理解。
- 代码证据：[`server/service/video_parse_media.py:160`](../../server/service/video_parse_media.py#L160) 无条件 `_materialize_audio(video,audio)`，[`307`](../../server/service/video_parse_media.py#L307) 的命令输出 WAV。[`server/service/video_parse.py:1041`](../../server/service/video_parse.py#L1041) 的媒体准备不接收 ASR mode；[`495`](../../server/service/video_parse.py#L495) 的 ASR=none 判断发生在媒体准备以后。既有媒体测试使用 command_runner 替身或带音轨媒体，未覆盖无音轨输入。
- 复现结果：ffmpeg 生成 1 秒、`-an` 的 H.264 MP4，调用 `prepare_chunk(...visual_mode='ordered_frames',ocr_enabled=False)`，立即抛上述 RuntimeError。脚本 `/private/tmp/anivag-audit-engine-probes.py`，日志 `/private/tmp/anivag-audit-engine-probes.log`。
- 建议：按阶段需要决定是否提取音频，并明确实际无音轨的 ASR 空结果语义；保留视频和所需媒体的完整性校验。
- 验收：无音轨且 ASR=none 的视觉/OCR视频能完成媒体准备和后续解析；带音轨的 ASR 不回退，音轨存在但损坏有明确错误。

### CHK-015 · P2 · DeepSeek 非工具多模态调用忽略调用级期限，取消不能关闭传输

- 状态：当前 DeepSeek Adapter 通过隔离 Responses/Client 替身复现；V2 OCR 的取消等待路径已核对，未调用真实 Provider。
- 触发条件：DeepSeek 的 `generate` 不带 tools，调用方传更短 `request_timeout_seconds/check_control`，或执行租约/任务取消时 ModelCallTracker 被取消。
- 现象与影响：Adapter 仍使用构造超时，不执行 control callback，也没有关闭在途 HTTP client 的取消回调。V2 OCR 选此分支时，外层 `_run_blocking` 会在取消 manager 后等待线程结束，Worker/临时媒体可能一直占用到请求自然结束或配置超时。晚到结果仍被正确拒绝，本条不是持久化 fencing 绕过。
- 代码证据：[`gateway/openai/multimodal.py:104`](../../gateway/openai/multimodal.py#L104) 接受 control，[`108`](../../gateway/openai/multimodal.py#L108) 接受调用超时，但非工具 [`164`](../../gateway/openai/multimodal.py#L164) 的 track_model_call 内没有 `add_cancel_callback`，[`184`](../../gateway/openai/multimodal.py#L184) 固定使用 `_request_timeout_seconds`。对比 [`gateway/ark/multimodal.py:173`](../../gateway/ark/multimodal.py#L173) 的关闭回调。V2 的 [`server/service/video_parse.py:1018`](../../server/service/video_parse.py#L1018) 在取消后等线程退出，保护媒体释放时序。
- 复现结果：构造超时 480 秒，调用时传 20ms，替身收到 timeout=480、control_checks=0。另一替身请求开始 30ms 后取消，client 原可通过 close 唤醒；实际 client_close_calls=0，约 206ms 后请求自然结束才返回 `ModelCallCancelledError`。脚本 `/private/tmp/anivag-audit-engine-probes.py`、`/private/tmp/anivag-audit-deepseek-cancel.py`，后者日志同名 `.log`。
- 边界：当前 `generate_realtime` 已将剩余期限写入新 binding，再以 bounded 值构造 DeepSeek，并有外层 wait_for；不能把直接 Adapter 的 480/20ms 复现扩大为当前实时请求固定等待 480 秒。
- 建议：Provider 请求使用最小有效期限，非工具分支注册请求级传输关闭回调并消费 control；保留取消后晚到结果校验和正确资源释放顺序。
- 验收：更短调用超时实际传入 Provider；取消能尽快关闭请求并释放 Worker/媒体；未取消请求正常返回，晚到/已取消结果仍不能提交。

### CHK-016 · P3 · 公开课程分页每页扫描全部候选，空检索范围重复扫描

- 状态：当前 LibraryService 与 API 授权范围组装逻辑已用计数替身复现；属于可量化结构/扩展性建议，未测得实际线上超时。
- 触发条件：公开课程较多，浏览器列表翻页，或问答未传显式 seriesIds、需按全部可见公开课程解析 scope。
- 代码证据：[`server/service/library.py:178`](../../server/service/library.py#L178) 每页加载全部 published 候选，逐项 get_series + publication_ready，最后 [`187`](../../server/service/library.py#L187) 在内存切片。[`server/api/agent.py:336`](../../server/api/agent.py#L336) 对空 scope 每 100 条循环调用，每页再次扫描全部候选。
- 复现结果：201 门合成可见课程，获取全公开 scope 执行 3 次全量扫描、603 次课程详情读取、603 次媒体就绪检查。读取数量约为 `N × ceil(N/100)`，尚未测量 SQL/文件实际耗时。脚本 `/private/tmp/anivag-audit-public-pagination.py`。
- 影响：课程增长时，分页不能限制详情装配与文件检查成本，所有公开范围的问答会在发起模型前重复做同样工作。
- 建议：保留公开媒体就绪和可见性判定，使用候选分页、批量装配或一次可见 scope 读取减少重复扫描。
- 验收：公开范围读取数量随课程数量近线性增长；未发布、未完成或媒体丢失课程仍不向游客开放，分页总量和下一页行为正确。

### CHK-017 · P2 · 观看页捕获全部 Tab，键盘焦点陷入播放器与输入框循环

- 状态：当前 WatchPage 组件检查与生产构建 Chromium 均已复现。
- 触发条件：观看页打开 Chat，从输入框或播放器子控件按 Tab/Shift+Tab，希望访问下一/上一控件。
- 现象与影响：焦点只在播放器容器与 Chat 输入框之间循环，键盘无法正常访问音量、倍速、发送、回答设置等子控件，也难以离开这两个区域。
- 代码证据：[`web/src/features/player/WatchPage.tsx:122`](../../web/src/features/player/WatchPage.tsx#L122) 对所有 Tab 调用 preventDefault，没有区分 Shift 或事件目标；[`208`](../../web/src/features/player/WatchPage.tsx#L208) 与 [`262`](../../web/src/features/player/WatchPage.tsx#L262) 在整个播放器 section 和 Chat panel 的 capture 阶段执行。既有面板切换测试不覆盖实际子控件可达性。
- 复现结果：真实 Chromium 连续 6 次 Tab/Shift+Tab 的 activeElement 为 `DIV 视频播放器 → TEXTAREA 向 anivag 助手提问` 循环。组件复现脚本 `/private/tmp/anivag-audit-ui/frontend-deeper.test.tsx`，浏览器脚本 `/private/tmp/anivag-audit-frontend-deeper.mjs`，截图 `/private/tmp/anivag-audit-ui/watch-keyboard-cycle.png`。
- 建议：保留面板快速切换能力，同时限定触发范围或使用不会覆盖普通 Tab 遍历的快捷键；正常子控件保留顺序与反向访问。
- 验收：使用真实播放器与 Chat 控件，正向/反向遍历能访问各按钮、设置和导航，能离开面板循环；Chat 暂停与返回播放仍正确。

### CHK-018 · P3 · 检索页卸载未移除 focus 监听，后台请求随进入次数累积

- 状态：当前 SearchPage 组件检查与生产构建 Chromium 均已复现。
- 触发条件：多次进入并离开检索页或观看 Chat，之后让浏览器窗口重新获得焦点。
- 现象与影响：已卸载页面继续发起回答设置请求，次数随历史挂载增加。结果不会更新已卸载组件，但每次回到窗口仍增加无用网络和服务端读取。
- 代码证据：[`web/src/features/search/SearchPage.tsx:238`](../../web/src/features/search/SearchPage.tsx#L238) 注册 focus；[`239`](../../web/src/features/search/SearchPage.tsx#L239) cleanup 只设置 active=false，没有移除 listener。[`219`](../../web/src/features/search/SearchPage.tsx#L219) 的 refresh 先发请求，仅收到响应后才检查 active。
- 复现结果：组件挂载/卸载 3 次、输入框已经不存在时，focus 仍发 3 个 options 请求。Chromium 挂载 4 次后回到观看首页，`activeSearchCount=0`，一次 focus 将 options 请求从 4 增至 8；报告 `/private/tmp/anivag-audit-ui/frontend-deeper-report.json`。
- 建议：cleanup 移除同一个 listener，并在启动刷新前核对组件仍有效；按既有请求管理方式处理在途请求。
- 验收：反复进入/离开页面后，已卸载页面不发请求；当前页面回到前台仍能刷新设置，不因旧请求覆盖新结果。

### CHK-019 · P2 · 原视频丢失时，观看 Chat 连普通文字提问也无法发送

- 状态：MISSING 媒体已在组件检查和生产构建 Chromium 复现；其他尚未就绪状态只做源码推导，未分别浏览器执行。
- 触发条件：观看页选中原文件已丢失的视频，用户不依赖当前位置，只想进行普通文字聊天或询问已有课程资料。
- 现象与影响：输入框及发送按钮仍显示可用，点击却提示“当前播放位置尚未就绪，请稍后重试”，不发送请求；永久丢失的媒体无法通过等待恢复，观看 Chat 因播放上下文校验全部阻断。
- 代码证据：[`web/src/features/player/WatchPage.tsx:88`](../../web/src/features/player/WatchPage.tsx#L88) 捕获播放位置，媒体不可用时拿不到 Snapshot；[`web/src/features/search/SearchPage.tsx:320`](../../web/src/features/search/SearchPage.tsx#L320) 对所有带 captureExtraContext 的提问无条件要求非空上下文。同一 submit 的普通 Chat 分支已允许不传 extraContext。
- 复现结果：媒体 MISSING 时发送“你好，请用文字介绍本课程”，组件 send=0；Chromium 的发送请求计数为 `1 → 1`，保留输入并显示上述错误。截图 `/private/tmp/anivag-audit-ui/watch-missing-media-chat.png`。
- 建议：明确当前位置提问与普通聊天的可用条件，提供准确的不可播放反馈和文字问答降级；当前位置问题仍需有效位置，不能伪造播放上下文。
- 验收：媒体丢失/准备中/加载失败时普通提问有明确可用路径；依赖当前位置的问题正确提示不可用，视频就绪后正常携带位置与身份。

### CHK-020 · P2 · 手机尺寸观看页的底部导航遮住 Chat 范围与回答设置

- 状态：当前生产构建的 Chromium 390×844 视口已复现；未验证真实手机软键盘。
- 触发条件：手机尺寸打开观看页 Chat，尝试操作输入框底部的范围选择、回答设置。
- 现象与影响：固定导航覆盖 Chat 工具栏；回答设置表面可见却点不到，点击会由下方导航处理。没有横向溢出不能代表页面可操作。短系列名也复现，不能归因于长标题。
- 代码证据：[`web/src/features/player/watch.module.css:67`](../../web/src/features/player/watch.module.css#L67) 的移动观看高度只扣除顶部 64px；[`web/src/app/app-shell.module.css:312`](../../web/src/app/app-shell.module.css#L312) 的 mobileNav 使用 fixed、z-index=30、距底部 10px，观看布局没有预留其占用空间。
- 复现结果：回答设置 summary 的 y 范围为 `792..820`，中心 elementFromPoint 命中移动导航的“解析任务”链接；实际点击等待 1500ms 后超时，报告导航拦截指针。页面宽度与视口同为 390px。截图 `/private/tmp/anivag-audit-ui/watch-mobile-chat-covered.png`。
- 建议：统一预留移动导航及 safe-area 空间，使输入与工具栏始终可操作；按实际移动布局核对面板和键盘行为。
- 验收：手机尺寸真实点击范围、回答设置和发送均成功；覆盖侧栏打开/关闭、横竖屏及软键盘出现，导航与观看控制都可用。

### CHK-021 · P3 · Gateway 仍指导 Server 启用 Langfuse，与当前停装配合同不一致

- 状态：源码与文档交叉核对确认；这是文档过时，停止 Langfuse 总装配是已记录的迁移决定，不列为生产代码 bug。
- 触发条件：按 Gateway README 将 tracing.enabled 配成 true 并提供 Langfuse 环境变量，期待正式 Server 发出 trace。
- 现象与影响：正式 bootstrap 仍不构造 Langfuse，文档承诺的 Parse/Chat/检索 trace 不会因此出现，读者可能浪费时间排查凭据与后端。
- 代码证据：[`gateway/README.md:252`](../../gateway/README.md#L252) 声称 Server 使用 LangfuseTracing 单例，并给 enabled=true 示例；实际 [`server/bootstrap.py:169`](../../server/bootstrap.py#L169) 固定 tracing=None。[`server/README.md:43`](../../server/README.md#L43) 与 [`server/report_docs/model-usage-handoff-20261008.md:109`](../../server/report_docs/model-usage-handoff-20261008.md#L109) 已说明 bootstrap 停止装配、保留独立模块及参数，正式用量迁移到 SQL。
- 证据边界：独立 gateway tracing 测试通过只证明该模块能够单独使用，不能证明正式 Server 注入它；本轮没有调用真实 Langfuse。
- 建议：同步 Gateway 文档，明确正式 Server 当前不消费 tracing 配置，独立模块使用说明与 SQL 用量合同各自准确。
- 验收：各入口文档与当前 bootstrap 一致；读者能够区分 SQL 模型用量和独立 Langfuse 模块，避免把配置 true 误认为正式服务已经启用。

### CHK-022 · P1 · 已修复：一键启动资料库探针的 Tenant 合同

- 状态：已修复并完成真实路由回归、本机首次启动、已有 API/Web 复用与 Vite 代理验证，详见顶部启动复核。以下保留原始问题证据。
- 触发条件：运行 `./start.sh start`，MySQL/ES 与 API 达到就绪，启动器准备检查资料库并启动 Web。
- 现象与影响：API 本身可健康运行，但启动器发出的资料库请求返回 422，抛“API 已监听但资料库查询失败，请查看 api.log。”，未进入 Web 启动；一键启动不能完成。直接正式 server.main 的隔离启动通过不能覆盖此入口。
- 代码证据：[`scripts/local_services.py:318`](../../scripts/local_services.py#L318) 请求 `/api/series/list?workspaceId=local-default`；当前 [`server/api/library.py:113`](../../server/api/library.py#L113) 必须接收 tenantId。启动器在 [`349`](../../scripts/local_services.py#L349) 及 Web 代理检查的 [`370`](../../scripts/local_services.py#L370) 都使用这个 helper，入口 [`452`](../../scripts/local_services.py#L452) 调用 start_applications。
- 复现结果：TestClient 使用当前 create_app、真实路由、local 默认身份和只读空 Library 替身，旧参数 HTTP=422、正确参数 HTTP=200；真实 library_ready=False。令 API 已监听且 /healthz 成功，真实 start_applications 抛上述错误。脚本 `/private/tmp/anivag-audit-start-readiness.py` 已独立重跑，未启动服务或修改数据。
- 建议：就绪检查使用当前 Tenant 身份合同，并将启动 helper 的请求走实际路由做回归，避免仅模拟响应导致别名迁移被漏检。
- 验收：依赖已就绪时首次启动和复用 API 两种路径均能启动 Web，API 直连及 Web 代理资料库检查成功；非法参数或真实依赖故障仍能给出准确失败信息。

## 结构与正确性审查补充

- 静态 import 检查未发现 Engine/Agent/Gateway 反向引用 Server。V2 分块配置以秒、业务坐标以毫秒，planning/采帧/ffmpeg 处转换一致；CHK-002/003 是测试构造合同未更新，不能据此移除生产校验。
- 检索会核对 Tenant、Series、Video、Task、index generation、内容身份与时间范围；词法匹配保留原始来源，相邻上下文保留自己的身份。重排先校验证据和候选完整性，无效输出回退至 RRF。Refine 提交核对成员版本/指纹，Canvas 权限、目录遍历和精读合同也有保护。
- Cloud Tenant 写权限、公开课程媒体就绪、Conversation owner、媒体/证据范围及 SSE 逐轮身份重验均有代码与既有测试保护。云端 bootstrap 强制 Agent 限制，游客运行参数受服务端 cap；未发现这些保护被前端参数直接绕过的证据。
- 删除在外部清理前持久化待恢复标记并隔离被删 Video 的租约，真实 ES 部分失败/删除晚到写入的临时合同复核通过。CHK-013 是后续 Prefix 依赖未失效，应保留已有删除恢复保护。
- 较大 Repository/Runtime 文件本身不直接列为缺陷；当前已量化的结构问题为 CHK-016 的重复全量装配，性能验收仍需实际负载。
- 观看页已接入播放上下文 DTO：当前生产构建用真实媒体发送时，extraContext.playback 包含 series/video/episode 身份，历史名 positionSeconds 的值为 347 毫秒，与暂停位置一致。该浏览器脚本故意返回 400，并出现一次未模拟的消息恢复 GET；只验证发出的合同，不作为真实模型问答成功证据。
- 启动脚本只管理 loopback 依赖及 PID 签名一致的自管进程，既有 MySQL 优先复用，不覆盖非空未初始化目录；服务/Caller close 保持幂等与先停止使用者再释放依赖的顺序，本次未确认额外退出清理缺陷。healthz 主要检查 Consumer 线程，不能用单一 health 成功推断模型、SMTP 或完整业务可用。

## 已有真实模型产物与当前验收边界

- 只读复核 [`vision-controls-20261009-resumed/final-reviewed.json`](../../outputs/verification/vision-controls-20261009-resumed/final-reviewed.json)：历史 90.132 秒样例在约 100.285 秒完成，31/31 工程检查通过；[`audit-result.json`](../../outputs/verification/vision-controls-20261009-resumed/audit-result.json) 留有真实 Provider 用量、SQL→ES 向量一致性及 MP4 Range=206。报告同时明确视觉覆盖减少、Refine 空提案、语义与音轨精度未认证，不能将 COMPLETE 视为高质量验收。
- 历史 low 对照的 [`final-reviewed.json`](../../outputs/verification/vision-controls-low-20261009/final-reviewed.json) 为约 661.334 秒、34/34 工程检查通过，但 overall_acceptance=failed_semantic_quality；旧精修把 36 条事件覆盖成 2 条。当前 [`agent/video_parse_v2/refine/agent.py:19`](../../agent/video_parse_v2/refine/agent.py#L19) 已使用 ParseContextCanvas，模型提交只接受 CanvasCompletion；[`timelineEvent_canvas.py:396`](../../agent/video_parse_v2/timelineEvent_canvas.py#L396) 核对原始事件仍存在，[`341`](../../agent/video_parse_v2/timelineEvent_canvas.py#L341) 由程序生成完整成员更新。已有 [`test_context_canvas.py:32`](../../tests/agent/video_parse_v2/test_context_canvas.py#L32) 保留 36 条事件并验证两处局部纠正，故未把旧 36→2 结果重记为当前 P1。
- 本轮额外用上述历史两块 Understanding 原始 payload 构造当前 Canvas，把旧 Refine 两条局部修正映射为当前 set_event，离线重放退出码 0：`36 → 36`、两个成员仍为 `17/19`；人为移除一条原事件被 _validate_draft 拒绝。脚本 `/private/tmp/anivag-audit-current-model-evidence.py`。这证明当前更新合同保留事件，不证明真实模型会正确调用新工具或其修正内容语义准确。
- 上述历史冻结源码中的 Library API、启动 helper、解析 Service/Repository 等与当前文件 hash 不同，仍有 workspaceId 接口；历史产物证明相应旧版流程实际运行过，不能证明当前工作区 V2→检索→Chat 全链路或新的 Canvas 模型交互质量已经验收。
- 本轮没有新增真实模型请求、SMTP 发信或真实 Redis Lua 集成。当前模型输出质量/时延、手机软键盘，以及浏览器连真实后端的完整上传→解析→检索→问答流程，仍应在隔离资源和明确输入/质量标准下验收；所需检查范围已明确，本次不代替用户执行问题修复。
- 当前有效默认配置已按正式环境加载：V2、Ark ASR lite、Ark 理解 65,536 输出预算/reasoning=none，Refine 复用理解、直接视频输入、OCR/Compact 关闭；Agent 使用 DeepSeek、embedding 使用 Ark 1024 维。所需凭据存在仅代表具备尝试连接的配置条件；认证、账户额度、端点服务状态和实际模型适配仍无本轮请求证据。

## 检查限制与后续验收

- 本次工程基线、运行与体验复现、分层审查及已有实测产物复核已完成；问题修复与外部真实全链路验收尚未执行，后续可按稳定编号逐项 Review。检查覆盖范围不代表证明不存在其他缺陷。
- 新增的临时 UI 检查共 5 项通过，含对已观察到错误行为的断言；“通过”表示复现成立，不表示这些功能正确。临时脚本与日志没有加入仓库测试，未修复生产代码。浏览器预览与 Chromium 检查结束后均已退出。
- 补充观看 Chat 组件检查 3 项及 Chromium 脚本 exit 0，确认 CHK-017..020；浏览器 API 均为替身，视频为真实 MP4。此脚本的消息恢复 GET 未模拟，已记录为验证边界，不误记为产品缺陷。
- 第二次隔离合同复核已完成，正式 `server.main` 的空队列生命周期和 8 个 HTTP GET 通过；MySQL/ES/HTTP 临时进程均已退出。临时副本没有删除业务断言，也没有修改仓库源测试；其通过只能缩小“生产逻辑也坏了”的推断范围，不能让当前仓库原测试变绿。
- 实时解析外层心跳异常曾作为候选检查，但生产 `VideoParseRuntimeService._model_stage` 还有调用 `_require_chunk_active` 的内层心跳与取消保护；仅用简化 Runtime 替身观察到外层退出不足以证明生产任务会失去续约，因此未将此候选记为已确认缺陷。
- 默认 Python 测试完整输出：`/private/tmp/anivag-audit-pytest.log`；离线 tracing 沙箱外复核：`/private/tmp/anivag-audit-langfuse-unsandboxed.log`；前端测试、构建、浏览器日志分别为 `/private/tmp/anivag-audit-web-tests.log`、`/private/tmp/anivag-audit-web-build.log`、`/private/tmp/anivag-audit-web-e2e-unsandboxed.log`。日志在临时目录，重要结论和源码位置已写入本记录；临时文件不会作为长期证据的唯一依据。
- 本轮新增/维护的仓库文件只有 `docs/dev/check_log.md`；原有 tracked 文件修改保留。两次隔离运行的 MySQL/ES 及正式 HTTP 临时进程已停止，经沙箱外探测确认端口关闭（首次 51757/51758，合同复核 55296/55297/55419）；未对原有数据执行迁移或重启，未 commit/push。
- 真实模型、SMTP 发信、数据库迁移以及现有用户数据的变更需要有对应验证证据；没有执行的部分保留“未验证”，不声明跑通。
- 记录中的严重度：P1 为主要流程阻断、数据/权限正确性问题；P2 为明确功能或体验问题；P3 为维护性与覆盖缺口建议。严重度结合触发条件判断。


## 2026-10-10 22:05 Parse 模型诊断补齐

- 已核实运行装配 `server/bootstrap.py` 为 `tracing=None`，本地现有模型用量记录不包含完整请求、回执或工具执行树。过去摘要日志定位的是 Ark completed 空输出，尚未查实供应方内部原因。
- 本轮新增 `gateway/model_diagnostics.py`，接入 `gateway/agent_model/multimodal.py` 与 `gateway/ark/sse.py`：本机开关启用且具有 Parse 任务身份时，按实际 call_id 保存请求正文、工具 schema/历史反馈、SDK 解析事件、回执和异常链；媒体内容寻址复用并校验恢复，凭据脱敏，写入失败保留原执行结果。
- 多轮工具模型请求增加 agent_run_id/agent_round；外层异常携带 provider request_id，使现有 consumer 重试日志能够关联上游请求。保持 Chat/Canvas 的工具语义、数据库合同和任务执行限制。
- 验证：模型诊断、工具请求、SSE 和 Parse Canvas 四文件合计 104 passed；公共模型用量测试 49 passed，合计 153 passed，均为离线测试。生产新增模块及全部 tracked diff 的空白检查通过。
- 已在 ignored `.env.local` 启用 `ANIVAG_MODEL_TRACE_DIR=outputs/model-traces`。确认没有活动解析租约后，仅重启自管 API/Web，保留数据库与 ES；API、Web 代理资料库查询正常。模型用量记录重启前后均 89 条，未自动解析或发出推理请求。
- 限制：诊断从后续请求生效，不能恢复历史未保存的输入；记录 SDK 解析事件，非原始 wire。已反馈给模型的工具历史可恢复，工具在下一轮形成前直接崩溃的独立轨迹未新增；供应方未返回的内部错误原因仍需凭请求 ID 排查。格式与本地读取说明见 `outputs/model-traces/README.md`。
