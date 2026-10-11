# AniVAG 本机 Codex Bridge：原 5 批基线索引

日期：2026-10-10。范围依据：[TRD 第 11 节](../design/codex-bridge-trd.md#bridge-checkout)及用户已确认的 localhost、通用 Bridge、项目根目录默认值和 Conversation/session 绑定。

**适用范围更新：** 用户已确认 local 默认 Supervisor 改用 Deep Agents SDK，并允许 Codex/Claude ACP Bridge。本索引和既有 SVG/场景的 37 文件、P01–P05 仅对应此前 Codex Bridge 基线，不覆盖新增目标。当前优先批次为 [TRD 第 12.1 节 D01](../design/codex-bridge-trd.md#supervisor-first-batch)，两类 SDK 的最新 44 文件范围以 [TRD 第 13 节](../design/codex-bridge-trd.md#bridge-full-scope)为准；本次未重画地图、迁移其标记或登记实现通过。以下进度和预算均按原 ACP 基线阅读。

主交付：[阶段图 SVG](agent-bridge-stages.svg)；[可编辑 Excalidraw 场景](agent-bridge-stages.excalidraw)。每个图中阶段节点包含功能、预计改动、用户验收和 Agent 指示。本文件提供文件分配、详细引用及证据索引，不代替阶段节点或 TRD。

当前责任方为接入 Agent（本会话 `/root`），负责通用 `gateway/bridge` 及首版必需的 Agent/Server/Web 接线。共享文件仅修改 Bridge 接线部分，保留并行工作区改动。上游适配器维护与本机数据库执行列外部 TODO。

**原 ACP 基线当前位置：▶ P01 待执行阶段。** 原 TRD/check-out 已交付；P01–P05 的实现、接线、离线验证和真实运行验收均未开始。此前 plan-map 授权仅覆盖规划产物；当前新增目标与开工范围以最新 TRD/check-out 为准。

字段 review 前置：用户要求模型字段先交由其 review。[字段评审稿](../design/codex-bridge-trd.md#bridge-field-review)包含持久 runtime 绑定、DTO/VO、活动/交互和快照合同，尚未定稿。P02/P04 涉及字段的实现待 review；P01 的通用 SDK 验证可独立进行。该 review 要求来自用户，不是自动新增的审批流程。

## 批次与真实依赖

| 批次 | 交付目标 | 首次涉及文件 | 执行依据 | 状态 |
| --- | --- | ---: | --- | --- |
| P01 | 通用 ACP Client 与本机 Codex 兼容验证 | 5 | TRD 第 3–6、8、[12 节](../design/codex-bridge-trd.md#bridge-first-batch) | 当前待执行；未实现、未验收 |
| P02 | 后端 runtime/session 绑定、基本执行、交互回复和停止 API | 13 | TRD 第 2、4–6、8、10 节；[会话合同](../design/codex-bridge-trd.md#bridge-session) | 待字段 review、待执行 |
| P03 | 共享视频工具与 MCP 接入 | 6 | TRD [第 7 节](../design/codex-bridge-trd.md#bridge-mcp)、第 10 节 | 待执行 |
| P04 | 网页选择、工具/计划/交互、快照与刷新恢复 | 11 | TRD 第 8–10 节；[恢复合同](../design/codex-bridge-trd.md#bridge-recovery) | 待字段 review、待执行 |
| P05 | 全链路真实验收、必要修正与运行说明 | 2 | TRD [第 14 节](../design/codex-bridge-trd.md#bridge-validation) | 待执行 |

P01 → P02；P02 → P03、P04；P03、P04 → P05。P03 与 P04 可以在后端合同完成后并行开发；完整视频场景验收依赖 P03。P02 必须先提供可由开发客户端验证的交互回复和停止合同，不能把待审批任务的结束路径拖到网页实现后。

P01 的版本/权限/问询/恢复真实兼容验证是后续真实接入的门槛。验证未通过时可以继续不依赖该结果的设计、替身与离线测试；不能标记真实产品接入通过。需要上游补丁时另核对范围，不把 provider 逻辑放回聊天业务层。

## 全量修改量

按此前 Codex 首版的逐文件基线，旧范围预计 **37 个唯一文件**。以下保留原预算供比对；最新完整范围已由 [TRD 第 13 节](../design/codex-bridge-trd.md#bridge-full-scope)更新为 44 文件：

| 分类 | 文件数 | 预计行数 |
| --- | ---: | --- |
| 生产逻辑（含前端） | 20 | 新增 1,300–2,150；删除/搬移 200–370 |
| 依赖声明与锁文件 | 2 | 锁文件以实际解析为准，未纳入代码行数预算 |
| 直接回归测试 | 13 | 新增 850–1,250 |
| 实施说明与 TRD | 2 | 文档行数另计 |
| 合计 | **37** | **新增代码 2,150–3,400；删除/搬移 200–370** |

预计 A 8、M 29、D 0、R 0。新增与删除/搬移合计约 2,350–3,770 行，不含文档、锁文件。这是根据现有调用链与 TRD 核对的预算，实施后以实际 diff 更新；不将锁文件解析结果或不同阶段的共享文件复改重复累加。

已有 `conversation.detail` ORM、内部读取和迁移 SQL 的准备工作属于此前交付，不重复计入这 37 个文件。实际旧库迁移尚未执行。

此前 plan-map 交付单独计 **4 个文件：新增 SVG、场景和索引，修改 TRD 增加路线引用和稳定锚点**；当时不扩大 37 文件实施范围。本轮只维护 TRD/索引两份文档，已有阶段图保持原样。

## 文件分配索引

以下将 37 个文件按首次涉及阶段唯一分配。后续必要复改仍受完整修改树和对应节点职责约束。各批展开为可执行 check-out 时，再核对当时源码并展示该批逐文件树。

### P01：5 文件，生产 2 / 依赖 2 / 测试 1

- `pyproject.toml`（M）
- `uv.lock`（M）
- `gateway/bridge/__init__.py`（A）
- `gateway/bridge/client.py`（A）
- `tests/gateway/test_bridge.py`（A）

保持此前已展示的第一批范围。直接复用 SDK 的协议类型、管道和会话接口。真实验证使用隔离目录与用户自己的 Codex，不能以替身证明文件或恢复语义。

### P02：13 文件，生产 8 / 测试 5

- `agent/chat/bridge.py`（A）
- `server/bootstrap.py`（M）
- `server/repository/agent.py`（M）
- `server/service/agent_chat.py`（M）
- `server/model/dto/agentDTO.py`（M）
- `server/model/vo/agentVO.py`（M）
- `server/api/agent.py`（M）
- `server/router/agent.py`（M）
- `tests/agent/chat/test_bridge.py`（A）
- `tests/server/test_bootstrap.py`（M）
- `tests/server/test_agent_chat_service.py`（M）
- `tests/server/test_api.py`（M）
- `tests/server/test_cloud_agent_runtime.py`（M）

后端可独立用 HTTP/开发客户端验收。首次 session 绑定必须提交成功后才能 prompt；同会话 Agent/cwd 固定，幂等包含绑定，不自动重放可能已写文件的任务。

### P03：6 文件，生产 4 / 测试 2

- `agent/chat/supervisor.py`（M）
- `agent/chat/common/video_tools.py`（A）
- `server/api/agent_mcp.py`（A）
- `server/api/app.py`（M）
- `tests/agent/chat/test_supervisor.py`（M）
- `tests/server/test_agent_mcp.py`（A）

复改 P01 的依赖声明/锁文件以声明 MCP 直接依赖；复改 P02 的装配、Bridge 与 Service 接线及直接回归，以关联当前执行上下文。共享视频工具搬移必须保持旧缓存、预算、证据、图像、取消及用量语义，不另造视频工具实现。

### P04：11 文件，生产 6 / 测试 5

- `web/src/api/contracts.ts`（M）
- `web/src/api/client.ts`（M）
- `web/src/features/search/useAgentConversation.ts`（M）
- `web/src/features/search/SearchPage.tsx`（M）
- `web/src/features/search/AgentProgress.tsx`（M）
- `web/src/features/search/search.module.css`（M）
- `web/src/api/client.test.ts`（M）
- `web/src/features/search/useAgentConversation.test.ts`（M）
- `web/src/features/search/SearchPage.test.tsx`（M）
- `web/src/features/search/AgentProgress.test.tsx`（M）
- `tests/server/test_sse_events.py`（M）

复改 P02 的执行记录、DTO/VO、API/router 及直接回归，完成快照、重新订阅和待交互恢复。快照与增量必须原子交接或补发；浏览器按 sequence 去重，断流不重发 prompt。沿用现有当前连接、CSRF 和 scope cancellation。

### P05：2 文件，文档 2

- `gateway/README.md`（M）
- `gateway/bridge/design/codex-bridge-trd.md`（M）

首触只有两份实施文档；生产和测试必要修正发生于前四批文件，已包含全量代码预算。按真实失败修复，不预设扩大文件范围。实施期间每批都同步本地图/索引的状态与证据，规划产物的维护单独统计。

## 外部 TODO 与验收限制

| TODO / 责任方 | 当前状态与完成条件 | 阻塞范围 |
| --- | --- | --- |
| 上游 ACP/适配器能力 / 上游维护者；接入 Agent 负责只读核对与实测 | 固定组合未实测；记录本机 Codex、问询、原生工具及恢复结果；如不兼容需上游修复或另核对补丁 | 阻塞真实接入验收；独立设计与离线验证可继续 |
| 个人权限完整继承 / 上游维护者 | 当前没有完整 inherit 模式；P01 核对生效模式、writable roots、network 和 trust 差异；不默默切 full-access | 不把有限模式标成完整继承；有实际权限冲突时阻塞依赖该权限的验收 |
| 本机旧库迁移 / 本机运行维护者 | [SQL 已准备](../../../docs/migrations/20261010_agent_conversation_detail.sql)，尚未执行；实际字段及受控读写通过才关闭 | 阻塞 P02 存量库联调，不阻塞 API/Repository 合同设计 |

原基线排除了 Claude、云端/Pi、远程主机、完整文件管理器及上游适配器补丁。本次 Claude 已纳入最新 TRD 的通用 ACP 接入；其余仍在当前范围外，不用旧地图推定新增授权。

## 证据记录

| 证据 | 2026-10-10 状态与适用范围 |
| --- | --- |
| 原范围核对 | 此前 Codex TRD 基线为 37 个唯一实施文件；首次分配 5/13/6/11/2；不包含最新 Deep Agents/Claude 目标 |
| 前置 detail | ORM、内部读取及旧库 SQL 已准备；不代表旧库已执行迁移 |
| P01–P05 | 全部未实施；离线测试与真实运行验收未执行 |
| 地图交付 | 使用临时目录的 `@excalidraw/excalidraw@0.18.1` [官方 exportToSvg](https://docs.excalidraw.com/docs/@excalidraw/excalidraw/api/utils/export) 完成真实矢量 SVG 导出，保留可编辑场景；项目依赖不变 |
| 地图核验 | 场景 68 个原生元素、5 阶段各四项、仅 P01 当前标记；21 个本地引用及锚点有效；SVG XML、矢量图元、卡片文字边界与浏览器中文渲染检查通过，无位图/裁切/重叠；独立节点接手审查通过。仅证明规划交付，不证明 Bridge 运行成功 |
| 字段 review | 已补 TRD 第 6.1/6.2、8.1、9.1 节评审稿，P02/P04 标记待用户 review；未实施模型字段、API 或数据库变更 |

后续执行完成每一批时，同步节点和本索引中的实现、局部验证、真实验收维度，链接可复现实验与实际版本；未通过项保留待验收，迁移当前标记时移除旧标记。
