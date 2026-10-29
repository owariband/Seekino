# Seekino 项目 Agent 指引

你是参与 Seekino 持续建设的项目 Agent。先理解用户要解决的产品问题，再结合当前实现、设计与证据推进工作；交付需要说明问题改善了什么、如何验证，以及还剩什么未验证。

## 1. 项目目标与上下文

Seekino 将视频转成可检索、可回看、可追溯来源的素材库。主要业务链路是素材导入、视频解析、索引构建、自然语言检索、片段回看与观看问答；结果需要关联原视频及时间范围。

剧本与分镜理解、Sandbox 和基于素材的创作是持续演进方向。区分产品目标、候选方案、已实现能力和已验证效果，不能把规划或历史实验直接写成当前可用功能。

- 首次进入项目，阅读 [项目 README](README.md) 与 [文档分工](docs/README.md)。涉及产品定位或整体架构时，再读 [整体规划与复盘](docs/dev/seekino-trd.md) 和 [产品复审背景](docs/design/03-product-reassessment/README.md)。
- 开始具体任务先检查 `git status --short`，识别已有修改；从 [设计索引](docs/design/README.md) 定位相关主设计，再读对应模块 README、源码和测试。无需每次通读全部历史材料。
- 文档中的“当前”“已完成”按其日期、版本和验证环境理解。发现设计、实现或运行状态不一致时，用源码、测试与最新运行证据核对，并明确差异。

## 2. 架构职责与代码入口

| 模块 | 职责与入口 |
| --- | --- |
| [gateway/](gateway/README.md) | Provider 协议、模型配置与适配、通用 Agent Model 能力、调用控制和用量记录；不拥有 Video、Query、Speaker 等业务语义。 |
| [engine/](engine/README.md) | 领域算法与合同；`parsev2/` 管解析 Schema、时间校验与规划，`rag/` 管索引投影、召回融合与证据核验，`common/` 提供共享媒体能力。生产任务、事务和租约属于 Server。 |
| [agent/video_parse_v2/](agent/video_parse_v2/README.md)、`agent/chat/` | 解析与 Chat 的 Prompt、推理、Context Canvas 和工具编排。Parse Harness 通过宿主注入的 runtime 执行业务操作，不直接持有 Server Repository 或 Gateway Client。 |
| [server/](server/README.md) | FastAPI / CLI、身份权限、上传、配置装配、持久化、媒体与可靠执行。HTTP 调用方向为 `router → api → service → repository/storage`，业务状态与归属判断留在 Service 和事务边界。 |
| [web/](web/README.md) | React + TypeScript + Vite 前端；`src/api/` 管 HTTP 边界，`features/` 按业务组织，`shared/` 保存真正复用的组件。连接真实 API，不能用虚构业务数据掩盖连接失败。 |
| `tests/`、[evals/benchmark/](evals/benchmark/README.md) | 自动化验证与效果评测。测试通过、真实模型可运行和业务质量达标分别报告。 |

修改前先确认行为归属，优先在实际执行该策略的层修复。不要因跨层调用较多而复制算法、增加包装层，或顺带删除相邻模块的契约。

## 3. 必须理解的项目契约

- **正式链路：** Upload-to-Search 使用 V2、MySQL 与 Elasticsearch。MySQL 保存业务事实、任务和版本，ES 保存派生检索投影；不从旧设计中恢复 V1、Qdrant 或独立队列架构。
- **时间单位：** 视频时长、Chunk / Event 边界和帧位置使用整数毫秒，部分字段仍名为 `*_seconds` / `*Seconds`。Timeline 区间为左闭右开；配置时长、超时、租约和 FPS 按各自合同解释，不能凭字段名统一换算。见 [Engine 时间约定](engine/README.md#媒体时间单位) 与 [Timeline Schema](engine/parsev2/schemas.py)。
- **恢复与并发：** 正式任务由 Server 的持久状态和进程内 Consumer 推进。修改 Parse / Refine / Index 时，核对租约、版本、幂等、失败恢复和提交条件；不能把内存状态或任务通知当作持久完成事实。
- **检索与证据：** 检索候选需要在可信授权范围内查询，并按对应任务和内容版本回源核对。相似度分数、向量命中或模型推断不等于已经核实的原片事实。Event / Sub-Chunk 以 [主 TRD](docs/design/02-agent-harness/sub-chunk-trd.md) 为设计入口。
- **配置与数据：** 模型、来源和调用配置从既有配置入口解析，见 [模型配置](docs/model-config.md)。已有向量库的模型、维度和编码合同变更可能需要重建，不能只改查询端。开发、测试和生产数据遵循现有物理隔离校验。
- **本地资产：** 宿主机持久媒体默认放在 `assets/library/`，由 `SEEKINO_ARTIFACT_ROOT` 配置；`assets/` 下未跟踪素材由 Git ignore 排除，已跟踪发布资产保留；媒体与 MySQL 配套备份。原生数据库默认保存到 `data/mysql/`、`data/elasticsearch/`，底层文件不提交 Git，但不属于缓存，备份方式见 [data/README.md](data/README.md)。历史 `outputs/` 和显式配置仍可能包含真实媒体、数据库、配置及验收材料，切换默认路径不代表旧资产已迁走；部署与恢复按 [部署说明](deploy/README.md#persistent-media-root) 执行。

## 4. 需求、TRD 与文档维护

**每次新增 TRD，必须完整阅读并遵循 [TRD Principle](docs/design/common/trd-principle.md)；实质修订已有 TRD 时，同样按规范补充本次涉及的内容。** TRD 以 `# 1. 问题与现状` 开头，清晰呈现六个 key process；初稿写问题、假设与验证计划，执行中补证据，闭环时回到原问题验收。详细要求维护在 principle 中，不另建一份竞争规范。

- 保留用户提供的原始大架构、关键流程、术语与论述。用户要求“完善”“补充”时，在原结构上添砖加瓦；未经明确要求，不以同义概括替换原文，不擅自删除关键段落或重组全文。
- 新设计放在 `docs/design/` 对应发展阶段，按 [设计索引维护约定](docs/design/README.md#维护约定) 更新阶段 README 与主题入口；跨阶段修订保留一份主设计，历史方案明确关联后续入口。
- [problems.md](docs/dev/problems.md) 是问题 TODO / DONE 的唯一当前状态入口；[devlog.md](docs/dev/devlog.md) 保存决策、实验、收益与代价，沿用“问题与现状／方案候选／方案验证／指标对比”四段结构。
- TRD 保存现行设计、关键决策与验收依据，通过链接关联问题状态和实验记录；不要在 TRD、索引和报告中另建独立维护的任务看板。历史结论保留日期、证据和验证边界。
- 技术修改按 [文档维护职责](docs/README.md) 同步相关设计、问题和 devlog。调研放 `docs/research/`，实施报告放 `docs/report/<实际模块路径>/`，原始实验与验收证据保留在对应目录。
- 没有实测的数据标为预期或待测。实现完成、测试通过、真实效果达标与部署完成分别说明，不能互相代替。

## 5. 工作方式与修改边界

- 根据用户请求与会话上下文区分调查和实施；仅要求调查时保持只读，已授权目标内自主完成必要修改与验证，不重复索取已有授权。
- 大改动先简述方案和影响范围；不按行数、文件数或预先列出的文件树机械要求再次确认。目标扩张、尚未授权的破坏性操作或重大兼容性取舍需要用户决定时，说明具体影响和建议，再确认；不依赖该决定的已授权工作继续推进。
- 保持改动最小：定位时确认实际问题，实施前逐文件核对必要性，交付前检查 diff 是否有越界改动、顺手清理或格式化噪声。保留用户和其他 Agent 的已有工作。
- 搜索代码与文档优先使用 `rg` / `rg --files`。先读实际调用点和相关测试，再决定实现；发现旧路径失效时从设计索引与当前源码定位，不把旧名称直接写入新代码。
- 根据任务选用项目技能：调研看 [research](.agents/skills/research/SKILL.md)，需要压力检验方案时看 [grilling](.agents/skills/grilling/SKILL.md)，素材短片工作看 [seekino-short-video](.agents/skills/seekino-short-video/SKILL.md)。编码偏好在相关实现或审查中按需使用可用的 `hypnos-code`，纯文档修改无需自动加载。仅按适用条件使用，不因技能存在而扩大任务。
- 服务重启、真实数据迁移或清理、commit 和 push 依照任务已有明确授权执行；文档中的计划和示例命令不构成操作授权。
- 默认使用中文沟通，保留代码和接口的原始名称。先说结论，再说明依据、变化与验证；遇到阻塞写清缺口和下一项可执行动作。

## 6. 验证与交付

Python 版本与依赖以 [pyproject.toml](pyproject.toml) 和锁文件为准，前端命令以 [web/package.json](web/package.json) 为准。现有项目使用 Python 3.12、uv 和 Node.js 24+。

按改动选择相关验证，先运行直接覆盖本次行为的测试，再根据跨模块影响扩展范围。以下是仓库根目录下的常用入口，不要求每次全部运行：

```bash
# 后端：按实际模块或具体文件缩小范围；示例为 Engine 测试
uv run --no-sync python -m pytest tests/engine -m "not live_agent"

# 需要覆盖后端整体回归时
uv run --no-sync python -m pytest -m "not live_agent"

# 前端：按改动选择类型检查、测试和构建
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
```

真实数据库、ES、Redis、浏览器和模型验证须先核对测试环境与所需配置；排除 `live_agent` 不代表其他测试一定没有外部依赖。真实模型调用按当前任务授权和预算执行，不能通过连接开发或生产数据替代隔离测试环境。

纯文档变更检查结构、链接和改动范围，无需运行无关业务测试。交付时说明结果、相关验证与未验证项，以及实际存在的兼容性或数据影响；报告详略匹配改动，目录树仅在有助于理解时提供。不把未执行的检查写成通过。项目职责或稳定工作约定改变时，同步更新本文件及其引用入口。
