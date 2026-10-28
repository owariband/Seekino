# Agent Loop 与 Output Node：源码参考

核对日期：2026-09-15。以下结论基于公开源码与明确标注的本地源码；这是参考与设计记录，未修改 Seekino 业务代码。

## 1. 优先看 Google 的完整应用示例：循环出口与前端消费

项目：[google-gemini/gemini-fullstack-langgraph-quickstart](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart)。

源码：[backend/src/agent/graph.py](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart/blob/main/backend/src/agent/graph.py)，重点看 reflection、evaluate_research、finalize_answer 和图连接；[frontend/src/App.tsx](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart/blob/main/frontend/src/App.tsx)，重点看 useStream / onUpdateEvent / thread.messages。

已核对行为：检索结果累积到 state；reflection 返回 is_sufficient / knowledge_gap / follow_up_queries；路由依据材料是否充分和最大循环数，继续检索或进入独立 finalize_answer；该节点生成最终回答并整理引用。前端将节点更新转成活动记录，从 messages 渲染回答。

可借鉴：明确的循环出口、来源数据与最终答案绑定、节点进度与答案消费的区分。

适配边界：它审核检索材料；达到轮数上限也进入 finalize_answer，该节点调用模型生成答案。Seekino 最新方案同样把最终 Chat 放在循环出口，但明确要求每轮 Query 的 draft output 和视频证据接受 Evaluator 审核；事实性结论必须包含非空 `selected_video_ranges`，只有 complete 才进入 Chat，3 次 continue 走失败路径。存在 approved evidence 时，Chat 首次使用 `tool_choice=required` 并至少调用一次 `submit_video_item`，成功提交后恢复 `auto`；确定性 Output Node 只绑定实际提交且已放行的证据。最终逻辑顺序保存在 `renderBlocks`，成功 SSE 在数据库 commit 后原子释放。

## 2. 最贴近两个 Agent 的回边：LangGraph Reflection

项目：[langchain-ai/langgraph-reflection](https://github.com/langchain-ai/langgraph-reflection)。仓库已于 2026-04-01 归档。

源码：[src/langgraph_reflection/__init__.py](https://github.com/langchain-ai/langgraph-reflection/blob/main/src/langgraph_reflection/__init__.py)，重点看 create_reflection_graph / end_or_reflect；[examples/llm_as_a_judge.py](https://github.com/langchain-ai/langgraph-reflection/blob/main/examples/llm_as_a_judge.py)，重点看 call_model / judge_response。

已核对行为：主 Agent 子图执行后进入审核子图；拒绝时审核把意见追加为 HumanMessage，主 Agent 再接收累计 messages；通过时审核不追加意见，条件边结束。RemainingSteps 避免框架步数耗尽时无限返工。

可借鉴：主 Agent / 审核 Agent 子图组合、审核反馈回到生成方、累计对话作为下一轮输入。

适配边界：结束依赖末条消息类型，不是用户定义的 complete / continue；没有独立 Output Node；框架剩余步数不足也可能结束，不代表审核通过。Seekino 把主生成方对应为 Query，使用显式 EvaluatorDecision、LoopTurn 记录与业务 3 轮上限；放行后才进入独立 Chat 自主块生成，保留反馈语义，不照搬结束判定。

## 3. 输出结构与循环运行时：Agno

项目：[agno-agi/agno](https://github.com/agno-agi/agno)。GitHub 元信息核对为未归档，核对时仍有近期提交。

源码：[libs/agno/agno/workflow/loop.py](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/workflow/loop.py)，重点看 max_iterations、end_condition、forward_iteration_output，以及每轮迭代结果的保存；[libs/agno/agno/workflow/types.py](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/workflow/types.py)，重点看 StepInput / StepOutput。

已核对行为：Loop 默认 max_iterations=3；end_condition 读取本轮步骤输出决定停止；forward_iteration_output 默认 false，需要显式启用才将本轮输出接给下一轮。StepOutput 支持结构化 content、媒体集合、success / error 和子步骤结果；Loop 会保留并返回历轮步骤记录。

可借鉴：业务轮数与终止条件单独配置；步骤输出有明确数据结构；每轮输出、媒体与运行记录有明确归属。

适配边界：通用 Loop 不直接保证最终结果来自 Chat，也不保证终止时获得审核通过的答案；仍需应用自己的输出选择与放行规则。Seekino 已使用 LangGraph，可将这些原则落实在现有图内，无需为了参考而切换依赖。

## 4. Seekino 的最小适配

```text
Query（计划 / 检索 / draft output）
  → Prepare Draft：校验真实视频选择，绑定完整 Evidence
  → Evaluator
      complete → Chat：有获批证据时首次 required，至少提交一个视频，之后以 auto 自主编排
                   → Output Node：校验实际提交项并保存有序 renderBlocks → final_output → END
      continue → advice + 原计划 + 历轮记录 → Query
      第 3 轮仍 continue → 失败，不调用最终 Chat

生成期间：Chat 的工具调用、acknowledgement 与 Provider token 只在服务端缓冲
生成结束：Service 在数据库 commit 成功后原子释放 Evidence / 有序 blocks / 兼容 delta / completed
```

- draft_output：本轮 Query 的拟回答事实与选中视频 Evidence；事实性结论必须具有非空 `selected_video_ranges`。LoopTurn 保留计划、完整检索结果、复用来源、审核和意见，下一轮显式读取原计划与历轮记录。
- final_output：最终 Chat Markdown 与其实际提交的获批 Evidence 子集；approved evidence 非空时，该子集也必须非空，顺序由 Chat 自主决定。Chat 不能改变放行的 ID、时间范围或加入未放行证据；Output Node 按调用与正文的逻辑顺序生成 `renderBlocks`，只做确定性校验和封装，不是第四个 Agent。
- evaluator decision / advice：独立控制与反馈数据，不放进 final_output 充当 Chat 正文。
- 模型路由：Query、Evaluator、Chat 默认使用 `config.yaml` 的 `agent_model.model`（DeepSeek Flash）。Provider 请求或模型响应异常时，失败的单次调用在当前消息 / Tool 状态上原位 fallback 到 Ark Doubao Lite；已完成的业务 Tool 与 acknowledgement 保留且不重跑。业务 Tool 错误、取消 / deadline / 预算等控制错误和本地输入校验错误不触发模型降级。
- 视频交付仍使用现有 videoChunkId / videoId / startMs / endMs。浏览器原视频路径为 /api/videos/content；规范片段 HTTP 接口与范围播放验收另行实现。
- 参考项目提供循环或输出机制，未验证其中任何一个具有 Seekino 所需的完整视频解析、证据校验、Conversation 隔离与 MySQL 快照 SSE 闭环。

完整本地设计：[agent-chat-delivery-contract.md](agent-chat-delivery-contract.md)。

历史代码草案已于 2026-10-24 清理，探索结论见[归档总结](../dev/design-experiment-cleanup.md#过时架构图和代码示例)。其中 2026-09-15 的 8 项离线检查仅是草案阶段基线，不是当前回归总数，也不代表现行 HTTP / SSE 交付；当前实现与验证以[最新交付契约](agent-chat-delivery-contract.md)为准。

## 5. DeepSeek Harness：kick → turn → step

本地只读参考：[ReactLoopAgent](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L64)。[kick](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L210) 是 private async kick(): Promise<void>，零参数，内部 while (await this.turn()) {}。[turn](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L246) 负责用户任务的 turn/start / turn/end 与内部 Step；Inbox 还有排队输入时返回 true，所以同一次 Kick 能继续处理多个用户 Turn。[step](../../../deepseek-harness/packages/core/agent-loop/src/agent.ts#L332) 在该项目内部完成 Provider 流与工具执行，没有使用 LangChain 封装这一层。

可借鉴 kick → turn 的主流程组织。Seekino 的业务定义固定为一次 Kick 对应一个用户请求，一轮 Turn 对应一次 Query / Evaluator；Agent 内部 Step 可由所选 SDK 接管。具体数据归属、交付生命周期和术语差异统一维护在 [最新设计第 3 节](agent-chat-delivery-contract.md#3-harness-的-loop--kick--turn)，不直接照搬该项目的 Inbox 驱动器与插件系统。
