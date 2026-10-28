# Canvas 工具设计研究：可执行修改、准确读取与失败恢复

研究日期：2026-10-22。依据：当前工作区源码，以及 Anthropic 2025-09-11 发布的工具设计文章。
本轮未改生产代码、未做迁移，也未运行新的真实模型对照实验。
下文区分已实现事实、源码暴露的限制和设计建议；建议接口名称仅作讨论，不代表已经实现。

**推荐先补齐 Parse 草稿的显式事件删除与可修复错误反馈，再改善事件定位、读取参数和结构化输出，最后用真实任务评测调整预算。**
当前通用 Canvas 主要服务 Parse/Summary；Chat 已改用检索与证据读取工具，不能用旧 ChatCanvas 的失败轨迹描述当前接口。[Chat 注册表](../../agent/chat/video_search/tools/registry.py)

## 原文提供的原则及其边界

Anthropic 主张围绕任务选择职责清晰的工具，返回相关上下文，明确输入输出，并用真实任务、可验证结果和留出集迭代。
原文也建议合理分页、过滤、范围选择与截断，并让错误响应说明修正方法；命名和返回格式的收益需要针对模型测量。[原文](https://www.anthropic.com/research/writing-tools-for-agents)
因此，本文不会把原文解释为无限返回、删除所有限制或增加完整 CRUD 就必然更好。
以下方案是针对本仓库的工程推论，不是 Anthropic 对 Seekino 的评测结论。

## 当前实现：哪些能力已有，哪些路径缺失

| 实现事实 | 证据 | 对本次设计的含义 |
| --- | --- | --- |
| Parse 支持新增、patch、set 事件，以及人物/事件数组修改，但无事件删除操作 | [ParseOperation 与 dispatcher](../../agent/video_parse_v2/timelineEvent_canvas.py)，50–108、341–445 行 | 数组的 `remove` 只能删除数组元素，不能删除事件 |
| `_validate_draft` 要求全部初始事件 reference 仍存在，提示词也明文禁删 | [校验](../../agent/video_parse_v2/timelineEvent_canvas.py)，518–533 行；[提示词](../../agent/video_parse_v2/prompts.py)，109、124 行 | 只增加一个删除分支或只改提示词都不完整 |
| 原子写已校验 revision、按 role/batch_id 与操作指纹幂等，并在提交前检查只读集合 | [ContextCanvas.atomic_batch](../../agent/utils/context_canvas.py)，122–191 行 | 删除草稿不需要绕开现有提交机制，也不应开放修改源证据 |
| `reference` 与 `references` 分别触发单项/批量读取；`start/end` 分别表示字符/项下标 | [读取参数与执行](../../agent/utils/context_canvas_tools.py)，20–32、58–164 行 | 同一参数名承载两种单位，部分互斥规则仅在调用后报错 |
| `section` 默认 overview，批量读取却要求显式非 overview section | [读取分支](../../agent/utils/context_canvas_tools.py)，70–94、132–152 行 | 已知目标也可能先调一次目录或遭遇参数错误 |
| Event 的 `start_seconds/end_seconds` 实际是绝对毫秒；PatchEvent 同名字段缺少时间单位 description | [事件 Schema](../../engine/parsev2/schemas.py)，30–46 行；[EventFields](../../agent/video_parse_v2/timelineEvent_canvas.py)；[提示词](../../agent/video_parse_v2/prompts.py)，94 行 | 提示词反复解释不能消除工具参数名本身的误导 |
| 事件目录返回 reference、member_ref、event_type，缺少时间、人物和事件摘要 | [Parse catalog](../../agent/video_parse_v2/timelineEvent_canvas.py)，305–315 行 | 模型难以仅凭目录定位要改的具体事实 |
| Parse/Summary 把结构化内容先转 JSON 字符串，再放入工具响应；单项支持字符切片 | [Parse sections](../../agent/video_parse_v2/timelineEvent_canvas.py)，318–328 行；[Summary sections](../../agent/video_summarize/state.py)，143–145 行 | 完整对象也需要二次理解转义文本；字符切片可能拆开 JSON |
| 通用参数错误只保留前四个字段位置，丢弃具体错误类型与原因 | [validation feedback](../../agent/utils/tool.py)，101–110 行 | 模型可能只看到 `Invalid arguments: arguments`，无法据此修正 |
| Summary 的 chunks/background 已只读，另有读取覆盖统计及独立提交工具 | [SummaryCanvas](../../agent/video_summarize/state.py)，122–160 行；[Summary tools](../../agent/video_summarize/tools.py) | 不应为追求接口对称给 Summary 加草稿写权限 |

批量读取、分页 cursor、截断标记、范围读取、revision、幂等、原子性、只读保护都已经存在。
本次研究讨论它们是否容易正确使用，不将它们包装成待新增功能。[通用读取](../../agent/utils/context_canvas_tools.py)、[状态机制](../../agent/utils/context_canvas.py)

## P0：允许明确删除草稿事件，保留原子修改语义

建议增加最小显式操作，例如 `remove_event(event_ref=...)`，只作用于当前角色允许编辑的事件草稿。
删除和 patch/add 共用一次 `write_context_canvas` batch；删除未被指明的事件仍然不允许。
遗漏条目继续表示不修改，空 operations 也不能被解释为清空 timeline。
不为删除额外引入 mandatory reason、人工确认、软删除或 tombstone 系统；本次没有证据证明这些机制必要。
需要同步调整 operation schema、dispatcher、初始 reference 存活校验和提示词，而非只改其中一处。
已有 source/time authority、只读 evidence/background、role、revision 和整批回滚继续生效。[Parse 校验](../../agent/video_parse_v2/timelineEvent_canvas.py)、[只读检查](../../agent/utils/context_canvas.py)

删除之后，同 batch 引用已删除对象如何处理应有唯一规则：建议明确失败并回滚整批，而非静默跳过。
合并两个重复事件可表达为更新保留项并删除冗余项；无需为每种业务动作再造一套基础 CRUD 工具。
若支持删除本 batch 新建项，应通过已有确定性新增身份规则或返回引用实现，不能临时猜测列表位置。
新增身份由 Canvas/member/key 确定，删除后同 key 重加会涉及引用复用；需明确这一局部语义并验证旧引用行为，不扩成持久化软删除体系。[新增分支](../../agent/video_parse_v2/timelineEvent_canvas.py)，358–381 行
持久化层并非无条件禁止缩短 timeline：当前内容未变时可接受 proposed；发生并发结构变化时会报冲突。
因此要保留并发冲突处理，不能把“支持明确删除”写成“结构变化总能自动合并”。[merge_refinement_content](../../engine/parsev2/validation.py)，158–204 行

## P0：错误必须告诉模型如何修复本次调用

参数校验建议返回受长度限制的字段路径、错误类型、具体原因和一个适用修正方向；不要回显完整敏感输入。
例如不合法的数组元素应指出 `operations[0].values` 期望的类型，而非只说检查 schema。
未知 reference 应明确其 collection，并提示从当前目录取已发放引用；不要猜测最接近的事件并代替修改。
未知 section 可直接返回当前可用 section，减少为修复错误再次读 overview 的往返。
revision 冲突可透传已有 `current_revision`，并要求复核受影响对象后重试；不能偷偷替换 revision 后强行写入。
这些是错误呈现改进，不能吞掉校验或把失败改写为成功。[错误包装](../../agent/utils/tool.py)、[CanvasRevisionConflict](../../agent/utils/context_canvas.py)

## P1：目录可定位，身份保持稳定

事件目录建议增加绝对毫秒范围、speaker（允许 null）和短事件描述；按需补人物、member/version。
目录应足以回答“我要读哪条”，完整事实仍从详情读取；摘要不能成为未读原文的替代证据。
目前 reference 是带 Canvas 身份的 hash；可以评估 Canvas 内稳定短 handle，但必须由宿主维护映射。
不能使用可变 timeline 列表下标替代身份，否则删除、排序或插入后，同一个编号可能指向另一事件。
短 handle 的有效范围和过期条件应明确；底层仍保留原始 reference 与 member/version 关联。[身份构造](../../agent/video_parse_v2/timelineEvent_canvas.py)，165–177 行
若目录仍过大，优先试 member、时间区间、事件类型过滤；新过滤字段是否值得加入由实际定位任务决定。

## P1：读取参数与输出结构一起简化

优先试验单一 `references=[...]` 详情入口；已知对象默认读取 content，overview 保留为显式目录信息。
这是候选方案：若单项与批量确有不同任务价值，也可保留两入口，但必须在 schema 中表达互斥条件。
字符切片参数改用明确单位，例如 `char_start/char_end`；项分页用 cursor 或明确 item 参数，不复用 start/end。
Event 修改工具的时间字段建议统一暴露 `start_ms/end_ms`，明确为原视频绝对毫秒；宿主转换到既有存储字段。
这与读取参数消歧属于同一方向：会调整 Agent 工具 schema，但可以由宿主适配已有 HTTP/存储合同，不要求全库字段改名或数据库迁移；方案尚未实施。[当前时间合同](../../engine/parsev2/schemas.py)
尽量让 schema、工具描述和运行时校验表达同一合同，避免 schema 看似接受、执行才发现禁止的参数组合。
完整详情建议返回 JSON 对象，例如 `data`，而不是再编码到 `content` 字符串；明确文本切片才返回 text。
批量的 collection、revision、section 可放到公共元数据；每项保留所需引用与数据，去掉纯重复字段。
可比较 concise/detail 两种投影，但 concise 仍须提供完成下一步操作必需的引用，不能省掉可操作身份。
结构化返回必须同时调整 Summary 的“已读完整项/范围”统计，不能因格式变化把截断内容误计为已读。[record_read](../../agent/video_summarize/state.py)，147–160 行

阶段能力也应通过 schema 收敛：非 Prefix 不暴露 `set_compact`，不可读取的 collection 不放进枚举。
当前所有 ParseOperation 都含 compact，执行时才拒绝非 Prefix；建议把“不属于该阶段”前移到工具定义。
这不意味着每个阶段重写一套 Canvas，也不意味着把所有检查都移到 Prompt。[compact 分支](../../agent/video_parse_v2/timelineEvent_canvas.py)，418–425 行

Understanding 当前分开读取 ASR、OCR、background 与 member/persons，而通用 batch 仅覆盖同 collection/section。[阶段要求](../../agent/video_parse_v2/prompts.py)，97–101 行；[批量读取](../../agent/utils/context_canvas_tools.py)
可评估在现有 Canvas 上增加 member/chunk 工作视图，一次聚合相关证据、当前事件摘要、可写范围和 revision；优先扩现有 view，不为每字段新增工具。是否减少调用需实测。

## P2：预算服务于完成任务，不能变成猜谜

区分传输字节边界与模型上下文预算；重叠的条目、字符、token 限额应合并或推导，避免互相覆盖。先核对配置与实际执行是否一致。
优先按完整条目分页；单条内容过大时，提供可继续读取的明确范围及已完整返回的边界。
模型应知道哪些项完整、哪些未返回、怎样继续；不能靠反复扩大 end 试探上限。
既有 `truncated`、`complete_end` 与建议范围可复用；不要再造第二套互相矛盾的完整性标记。[读取与输出控制](../../agent/utils/context_canvas_tools.py)、[tool_output](../../agent/utils/tool_output.py)
预算是否合适看任务完成率和总成本；单次返回更少但多十次调用，未必更节省。
本轮不建议无限返回、不删除全部限制，也不把文章中的某个 token 数当项目通用最优值。

## 验证方式：先验证合同，再测模型是否更会用

第一层是确定性用例：显式删除、遗漏保留、原子回滚、幂等重放、只读拒绝、版本冲突、分页完整性和参数单位。
它们证明工具执行合同，不证明模型能选择正确事件或减少幻觉。
第二层使用固定源证据和人工裁定目标，构造下列真实任务；同一输入比较旧版与候选版，不强制唯一工具路线。

| 任务 | 应核验的结果与故障 |
| --- | --- |
| 删除重复 event | 指定冗余事件消失，正确事件保留；统计误删与未删 |
| 修正 speaker | 目标 speaker 被正确修改，时间、原文和无关事件保持符合任务要求 |
| 同 batch 合并事件 | 更新与删除全部成功或全部回滚，幂等重放不重复新增 |
| 未知 reference 恢复 | 找到有效目标后完成任务，不猜 ID、不修改另一条事件 |
| 大证据跨页阅读 | 必需证据被完整读取，截断没有被误认为结束 |
| 并发 revision 变化 | 正确重读并复核，或返回冲突；不覆盖另一方变化 |

主指标是结果正确性、证据支持与误删/遗漏；辅指标是成功前工具次数、参数错误与恢复次数、token、总时延。
单次调用成功不能替代最终任务正确，工具调用更少也不能抵消误删或证据遗漏。
按任务难度、事件数、证据长度和并发情况分桶；固定同一模型与同一 3 fps 输入，记录 Prompt、工具 schema、预算与数据版本。
同一组输入做重复运行，报告样本数与波动；先在开发集改工具，再用未参与调参的留出集确认收益。
评测依据最终状态、工具参数/返回和可见过程；不把模型对自己行为的解释当作成功证据。

## 推荐实施次序与本轮结论

1. P0：补显式删除合同及精确错误反馈，配套最小确定性回归；继续复用原子写与只读保护。
2. P1：目录补定位信息，消除读取参数歧义，试验结构化详情和稳定短 handle；逐项比较，避免一次混改。
3. P2：以真实任务成本调整分页预算，做模型 A/B 和留出验证，决定哪些候选接口值得进入生产。

当前证据足以定位接口缺口，不能给出“成功率提升多少”的结论。
本轮仅完成研究文档；没有删除任何实际 Event、修改 API/数据库合同、重跑解析或宣称真实模型效果已经改善。
