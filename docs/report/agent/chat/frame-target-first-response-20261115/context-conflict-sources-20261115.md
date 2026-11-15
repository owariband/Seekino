# 2026-11-15：Chat 的 context 来源冲突与额外调用链

本文记录各类上下文具体来自哪里、在什么范围内不一致，以及如何影响工具合法性、取证目标、答案和耗时。对应[完整失败映射](diagnostics-before.json)、[同环境回放](README.md)、[逐调用用量](usage.jsonl)和[上线验收](production-rollout-20261115.md)。任务状态只由[工作台](../../../../dev/workbench.html) `CHAT-FRAME-FIRST-01` 维护。

这里的 context 包括模型能看到的文字、历史与工具结果，也包括宿主实际执行调用时的状态和预算。它们有四种不同问题：指令的条件／优先级不清；模型可见历史与执行状态不同步；工具合同不一致；证据范围被扩大。不能一概写成“两段 prompt 互相否定”。模型内部如何权衡没有直接证据；本文只从已发出的调用、反馈、状态和源码说明可观测链路。

## 1. Context 来源与各自应管的事

| 来源 | 放进来的信息 | 应有的范围 |
| --- | --- | --- |
| 当前用户消息 | 本轮目标、明确时间、纠正、先后顺序、长度限制 | 决定本轮要解决什么；明确指定优先于默认锚点 |
| 当前播放数据 | videoId、seriesId、positionSeconds（实际整数毫秒） | 本轮冻结的默认播放位置，不代表已核验画面 |
| 原生历史／checkpoint | 旧问题、回答、工具结果、Chunk ID、cursor、旧播放位置 | 用于指代与已知事实；不自动继承本轮工具句柄和当前来源版本 |
| JEV 与当前轮模板 | respond／retrieve／mixed 分类和处理说明 | 决定处理动作，不产生完整计划，也没有独立复杂度字段 |
| Supervisor／Query Skill | 工具路线、是否委派、证据标准、输出方式 | 在统一边界内组织执行，不能另造相反的参数或能力合同 |
| 工具 schema、描述与宿主实现 | 可传参数、条件必填、时间单位、抽样方法、可用性检查 | 告诉模型能执行什么，并与实际校验一致 |
| 本轮宿主状态 | 检索命中、已授权回源证据、frame receipt、预算剩余 | 决定这一次调用是否合法；不会因历史提到某个ID就自动成立 |
| 检索／Realtime／原图／背景 | 区间事件、暂停帧、生成的视觉摘要、角色设定 | 各自只支持对应时间、粒度与来源范围内的主张 |
| 模型与运行配置 | 当前Supervisor选择、agent全局默认、容量元数据、字符保护 | 决定实际模型和context生命周期，不能仅凭相同模型名推定控制一致 |

## 2. 具体发生在哪些来源之间

| 编号与性质 | 来源 A ↔ 来源 B | 具体不一致 | 已见后果与证据边界 |
| --- | --- | --- | --- |
| CF01：目标与优先级 | 当前用户时间／本轮播放 ↔ 历史回答和旧播放位置 | 本轮验收T02默认点为1058310ms，历史点为1037256ms；模型仍取旧点周边。T03明确时间码按1056000ms验收，Realtime却冻结到1056558ms。 | 工具可以合法但取错目标，进而补查、重新解释，甚至答案仍围绕旧场景。参数偏离能直接核实；是否因某段历史而选择旧点属于高层归因，不是CoT实录。[E1](#e1)[E3](#e3) |
| CF02：句柄生命周期 | 历史中“C真实存在、曾读成功” ↔ 本轮retrieval_calls重新建立 | 常规检索读／区间取帧路径先查本轮集合；历史工具结果并不会恢复该集合。既有大纲回源等显式例外另算。 | T02同一模型回复并发读C和抽C，均UNKNOWN_ITEM。不是看到第一条错误还故意重试；也不是视频不存在。触发恢复检索及后续模型轮次。[E1](#e1)[E2](#e2) |
| CF03：跨工具能力范围 | Realtime返回C且available=true ↔ read／inspect要求对应本轮注册 | Realtime证据可用，不等于C已成为检索工具可调用的条目。同一个ID、一个笼统“可用”，实际对应不同能力。 | T03在Realtime返回C后再inspect，仍UNKNOWN_ITEM；有效retrieve若只返回其他4个片段，也不会注册C。后一次继续读C还有未遵守正确错误反馈的因素。[E1](#e1)[E2](#e2)[E3](#e3) |
| CF04：参数合同 | Provider JSON schema ↔ Python条件校验；旧prompt参数说明 ↔ 实际函数 | 首次retrieve无cursor时需要query／top_k／channel_weights，旧schema却将各字段呈现为可选。Supervisor还描述section／end，但read实际只收ID和cursor。 | T03一次执行前参数校验失败已观察；只知至少一个初始必填字段缺失／null，不能还原是哪一个。section／end错误没有出现在这六次失败中，单列静态风险。[E1](#e1)[E4](#e4) |
| CF05：精度承诺与能力 | 单时间点目标 ↔ 区间均匀采样；“exact时间戳”说明 ↔ 请求seek位置 | 缩小区间不自动保证采到指定点；T03成功区间采样包含1056187ms等，却没有请求1056000ms。旧Realtime观察prompt还称宿主给exact frame timestamps。 | 工具成功不等于精确时点已核验；视觉摘要再声称与用户时间相符，会把未核验对齐包装成工具证据。此项解释成功后的质量缺口，不是3次抽帧UNKNOWN的直接原因。[E1](#e1)[E3](#e3)[E5](#e5) |
| CF06：证据范围与来源 | Chunk区间事件／历史手部画面／背景设定 ↔ 当前单帧可见事实 | 区间里发生过某动作，不证明暂停帧恰好显示该动作；稀疏静帧不证明动作连续；人物背景也不能补足图中不可见细节。 | 旧回放扩大到持续牵手及未测时间轴偏移；上线新探针仍在闭眼图上提眼色。后者缺证是直接可见的，但眼色究竟来自背景还是模型先验无法确定；先验也不等于一份已定位的外部context。已有静帧限制仍被违反，不能全部归为缺prompt。[E1](#e1)[E5](#e5)[E8](#e8) |
| CF07：路线与委派条件 | JEV处理模板 ↔ Supervisor路线；Supervisor不同策略段之间 | JEV的retrieve被旧模板叫“简单检索”，但动作分类不等于复杂度。Supervisor一处在记忆不准且未找到原句时要求必须Query，另一处规定有大纲的路线／对比跨章节不委派；同时适用时缺优先级。 | 前一对主要是职责重复与维度混淆，不是已证字面互斥；后一组属于条件重叠风险。三条有效回放Query调用0，不能把它写成六次错误的直接原因。[E6](#e6) |
| CF08：交付节奏 | 对较早可见回应的需求 ↔ 旧Supervisor全局不写过程说明 | 旧版会直接调用工具，通常等取得证据才开始正文。旧mixed已允许先给实质回应；旧JEV并未要求先确认。 | 主要影响FTT和“闷头查”的感受，不足以单独解释TT或推理工作量。新目标确认和旧禁令是不同版本，不能倒拼成曾同时生效的冲突；若只新增确认而保留总禁令才会打架。[E6](#e6) |
| CF09：有效配置分叉 | 当前Supervisor binding／动态容量 ↔ Query读取全局agent和旧静态预算 | 相同模型名仍可能是Supervisor 1048576／891289、Query 64000／48000；Query没有跟随同一容量解析与当前选择。 | 旧Cloud T01两个Query共9次本地上下文替换；不能把全部次数归于同一个阈值，是否直接造成分页重读也没有单因素证据。实际预算耗尽和无可用partial报告是更直接的失败链。本轮三条Query0，不能用它们证明该旧Query路径已验收。[E7](#e7) |
| CF10：工具集与架构策略 | 业务query_video和禁通用子Agent的配置 ↔ SDK实际注入的task工具及说明 | 旧profile注册名与实际模型标识未匹配，意外暴露另一条委派路径，模型能看到的可选动作比业务策略规定的多。 | 属于真实静态接线问题；三条有效回放task调用0，不能量化其耗时影响。已修正注册匹配，线上探针的请求工具列表不含task。[E8](#e8) |

六次旧失败应按直接原因计：**5次UNKNOWN_ITEM主要落在CF02／CF03，1次参数校验落在CF04**。CF01、CF05、CF06解释了部分“调用成功但效果差”。CF07、CF10不能混入六次已发生错误的原因统计。

配置层还要区分两个阶段：前期共享64000／48000静态策略没有随模型实际容量调整；后来Supervisor已动态解析到1048576／891289，而Query仍沿用小窗口。模型声明、服务元数据、runtime默认与最终装配值不是同一个来源，应核对有效结果。上述数字限当时DeepSeek API路径，native harness另有上下文所有权。

CF02／CF03也不是要求取消注册或授权校验。新单点工具按允许的tenant／Series、视频和原片来源校验，绕开为了取一个已知时点而先检索Chunk的前置；历史ID并没有被批量变成本轮可用成员。

## 3. 从不一致到慢请求的可见链路

**失败后返工。** 历史C仍在模型context中，本轮集合没有C → 读／抽帧被拒 → 错误反馈进入下一次模型输入 → 模型改搜或换工具 → 若新结果仍无C却继续用C，再次失败。每次依赖前一结果的恢复都可能增加一次模型决策及工具等待，新的反馈和证据又使后续输入变长。并行发出的两次工具失败不等于两次新增模型轮次，不能按错误数直接乘单次API耗时。

**目标漂移但不报错。** 本轮播放点、旧画面和明确时间没有就近统一 → 模型选到另一个合法时刻 → 原片工具正常出图 → 用区间事件或旧事实补解释 → 可能扩大搜索、重复取帧，最终仍未回答本轮目标。收紧提示候选的T02就是工具0错误、目标仍未通过，四次取帧都在旧点周围。

**工具结果变成过强的事实。** 没按用户时间执行的工具图 → 视觉摘要生成“与所指时间相符” → Supervisor接到看似权威的工具文字 → 把区间／附近图推成单点主张。摘要本身仍是模型产物；不能拿它当宿主已做过数值对齐或来源核验。

**交付晚与完成慢是两个量。** 工具已定位候选不代表已精读到足够证据；后续合法核对不能全部算浪费。但定位后继续无关扩搜、失败恢复、重复读页都会延长关键路径。旧的不写过程说明又使这一段没有正文，所以FTT接近最后回答。新第一轮目标确认改善的是可见等待；TT下降还要看工具和模型轮数是否减少。

可粗略按“前置决策＋串行模型轮次＋工具依赖关键路径＋交付开销”理解TT，不能把所有并发工具时长直接相加。所谓“思考增加”，在当前证据里更适合表述为**总生成轮数、总reasoning token和重复处理的context增加**；没有逐原因的内部思考难度或耗时分解。

## 4. 数据支持什么，不能支持什么

对照采用同一Flash/high/8192、相同原历史和环境；这里比较a5084c26与收紧提示ab18d93d，每组3条。统计来自[metrics.json](metrics.json)和[usage.jsonl](usage.jsonl)，不是新实验。

| 可观测量 | 同环境基线 | 收紧提示候选 | 能说明什么 |
| --- | ---: | ---: | --- |
| 生成调用 | 22 | 11 | 模型往返轮数减半；不代表每个差额都由失败重试造成 |
| reasoning token总量 | 5161 | 2719 | 总量减少47.3%，已包含在output，不重复相加 |
| 每次生成平均reasoning token | 234.6 | 247.2 | 平均反而增加5.4%；不能声称每次都思考更少 |
| 全阶段总token | 1,040,456 | 494,202 | 包含JEV、生成和embedding，减少52.5% |
| 工具错误／尝试 | 4／22 | 0／11 | 调用合法性改善；与更早6／25是不同试次 |
| 核心业务通过 | 1／3 | 2／3 | 零工具错误仍不等于目标、证据和答案全正确 |
| 平均首可见正文FTT | 23.939s | 7.840s | 包含短确认；候选只有2／3轮真的先确认 |
| 平均取证后正文开始 | 23.939s | 14.384s | 已排除确认及重复进度，不保证正文所有主张正确 |
| 平均服务端TT | 27.656s | 16.295s | 组合改造后缩短41.1%，没有逐项收益拆分 |

计算为`reasoning总量 / 实际生成次数`，只使用reasoning上报非空的22和11条生成记录。总reasoning下降与次数减半相符，单次均值略增；这些均值由不同生成调用组成，不是同一调用的耗时对照。现有证据支持减少往返和总工作量，不支持把收益解释为单次推理强度变低。high可能影响单次耗时，但本轮没有low／high对照，不能分配贡献。

旧a5084c26已经写有贪心原则，而且本轮三条未用Query；不能把这轮收益写成“从实际运行的固定多Agent流水线首次切换为贪心”。这些试次也没有观察到原生摘要事件变化，不能用这组数字证明压缩收益。JEV连接复用随后才集成，同样不能归入这些较早候选的测量收益。

## 5. 应单列的风险与非context原因

- **旧参数指引和条件委派：** section／end、Query Skill历史ID措辞、submit失败后“先read”的提示，存在前提不清或与实际入口不符；本次六次错误没有走这些路径，不据此补造已发生的原因。
- **cursor复制：** 新基线另一次INVALID_EVIDENCE_CURSOR中，后续入参与刚返回的next_cursor不同，解码后fingerprint不同。能确认没有逐字复用，不能确定是抄写、拼接还是错用旧值，更不能笼统说宿主游标失效或压缩导致。
- **时间单位：** positionSeconds／若干证据*_seconds实际为毫秒，submit的start_seconds／end_seconds则为秒，确有误读风险；这些已查时间偏移不能一概归因秒毫秒换算错误，T03更直接是目标选择和采样点不同。
- **字符保护与token窗口：** 200000字符保护与891289 token触发值是两种约束，可能前者先触发，不能把它们当同单位阈值相减。这不等同于父子配置分叉；修复后仍保留字符保护。
- **循环与预算：** 旧Cloud T01两个Query共45次生成、5次cursor错误、2次Event参数错误，反复读页与恢复耗尽共享100次工具预算；子任务无可用partial后，Supervisor恢复也被同一耗尽预算拒绝，196.567s失败。不能仅凭同时9次压缩就断言压缩是唯一原因，本轮Query0试验未覆盖这条控制流。
- **未生效文本：** 关闭的rewriter／合并决策prompt虽仍在源码，本轮并未进入该产品调用链，不能按“源码里存在”就计作上下文冲突或前置开销。
- **网络与可用性：** JEV连接冷启动、供应商402属于独立开销／故障；本次上线新探针已成功生成，旧402按当时记录解释。不能把所有等待都算context conflict。
- **排查视角：** Gateway执行前校验产生模型能看到的ToolMessage，却绕过业务handler日志；24次／5错因此漏成25次／6错中的一项。失败参数又被归一化为{}，这属于取证缺口，不证明模型原始调用为空。

## 6. 已改与待验证的界限

已上线的修正包括：明确用户时间／本轮锚点优先、就近附本轮播放数据；提供带授权与来源校验的单点工具；对齐schema条件必填和实际参数；JEV处理说明被Supervisor沿用、复杂度仍按目标和缺口决定；Query跟随Supervisor控制；清理旧总禁令后允许短目标确认；关闭意外task入口。

其中，**最终时间选择仍依赖模型遵循说明**，并未新增一个宿主从自然语言强制解析全部时间目标的机制；常规历史ID也仍需满足对应工具的本轮前提。现有证据不是“所有context已合成一个不可错的状态”。

上线新会话已实际先确认、只get_video_frame(1056000)、0工具错误，首响应5.281s、服务端TT7.725s；它不含原T02长历史，不能替代该残留验收。取对图之后，角色身份等主张仍应只按图片可见内容与明确标注的背景来源表达，不能用合法率掩盖证据越界。

后续排查继续按三层分别验收：**参数／权限／句柄合法；取证视频和时间正确；答案受证据支持且完成用户诉求。** 本文不增加实现、服务配置或独立任务看板。

## 7. 可复核来源

- <a id="e1"></a>**E1**：[六次错误与质量缺口](diagnostics-before.json)、[各次回放与指标](README.md)、[原生口径说明](../local-history-replay-20261115/README.md)。C是同一真实片段的匿名别名，原始聊天／checkpoint不发布。
- <a id="e2"></a>**E2**：旧[a5084c26 Supervisor每轮集合](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L241)；[retrieval_hit本轮检查](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/video_search/output.py#L387)。
- <a id="e3"></a>**E3**：旧[Realtime调用query未参与构造](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L952)；[固定播放位置与原问题](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/realtime_understanding/runtime.py#L92)。
- <a id="e4"></a>**E4**：旧[可选字段与条件校验](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/video_search/tools/retrieval.py#L38)；[read参数](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/video_search/tools/retrieval.py#L68)；[旧section／end指示](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L1175)。
- <a id="e5"></a>**E5**：旧[区间采样](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/server/service/video_frame_reader.py#L85)；[区间工具不测PTS及静帧限制](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/video_search/tools/frames.py#L111)；[Realtime exact措辞和背景约束](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/realtime_understanding/runtime.py#L154)。
- <a id="e6"></a>**E6**：旧[JEV模板](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/query_hook_prompts.py#L39)；[必须Query条件](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L1173)；[既有贪心与不委派条件](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L1226)；[mixed既有前置回应](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L1219)；[旧不写过程说明](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/agent/chat/supervisor.py#L1259)。
- <a id="e7"></a>**E7**：[模型配置分叉与实测边界](../../../gateway/agent_model/chat-model-config-inheritance-20261115/README.md)、[更早静态／动态窗口对照](../../../gateway/agent_model/chat-context-compaction-20261113/README.md)；修复前[父子binding分开](https://github.com/owariband/Seekino/blob/505a3925471555cb353f9b873d3383e6e72cb82d/server/bootstrap.py#L215)、[Query静态控制](https://github.com/owariband/Seekino/blob/505a3925471555cb353f9b873d3383e6e72cb82d/server/bootstrap.py#L370)。
- <a id="e8"></a>**E8**：旧[profile注册入口](https://github.com/owariband/Seekino/blob/a5084c266798916d455f9fcc6d1a0ccee107ac43/gateway/agent_model/deepagents.py#L61)；[上线收据](production-rollout-20261115.json)、[主张边界与实际工具验证](production-rollout-20261115.md)。
