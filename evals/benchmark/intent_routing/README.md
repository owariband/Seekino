# Intent Routing · 意图路由与改写

从 `evals/query_router` 移入的组件评测：**13 个 respond/retrieve/mixed 路由场景 + 1 个 noul API smoke**。它只测前置分类／query 改写，不包含检索、最终回答与播放卡交付。完整的“高高在上／说话无趣”对话回归见 [chat_behavior](../chat_behavior/README.md)。

下文保留 2026-10-20 的协议、历史结果和对应脚本。历史 GPT 名称不作为未来模型测试默认值；生成式对照按 [统一约定](../README.md) 使用 Kimi。`chat_rewriter.py` 仍是历史 Codex 专用适配，不是当前 Kimi 的运行入口，本次只迁移 import 路径。

## Jev 分类实验

通过 `POST https://openrouter.ai/api/alpha/decisions` 调用 `typesafe/jev-1.14`。
Jev 返回分类与概率，不生成 rewritten query。本实验独立运行，不连接业务数据库、
不执行 state 中提到的工具，也不修改线上 Chat。

接口依据：[OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)。

## 运行

在仓库 `.env.local` 配置 `OPENROUTER_JEV_API_KEY`，不提交密钥。沿用项目的 uv 环境加载方式：

```sh
# 只校验样本、请求构造；不读凭证、不发送请求。
uv run --no-sync python -m evals.benchmark.intent_routing.openrouter_jev --dry-run

# 只运行用户给的 noul 例子，验证账号与接口可用性。
uv run --no-sync --env-file .env.local python -m evals.benchmark.intent_routing.openrouter_jev \
  --only smoke_user_noul

# 完整实验：1 条 noul + 13 条中文路由，每例一次。
uv run --no-sync --env-file .env.local python -m evals.benchmark.intent_routing.openrouter_jev
```

`--model`、`--timeout`、`--cases`、`--only`、`--out` 可覆盖实验参数；
不同凭证变量名通过 `--api-key-env` 选择。密钥仅放入 Authorization Header。
输出默认保存在 `outputs/verification/jev-<UTC时间>.jsonl`；指定文件已存在时拒绝覆盖。
HTTP、网络或响应解析错误会保存已完成结果并停止，不自动重试。
退出码 1 表示调用错误或分类与参考标签不一致，详情见结果文件。

## 判定口径与结果

- `respond`：直接回应、对助手的反馈、根据已有信息解释或修改回答。
- `retrieve`：查视频／台词／场景／补充证据；检索后的正常回答也归此类。
- `mixed`：独立的非检索任务与新的检索任务同时存在。

上下文和当前 query 放进 `state`，分类标准放进 `questions.route`，
参考标签 `expected` 只在本地评分，不发给模型。对话是合成测试材料，集数不代表真实检索结果。
原始请求、响应、实际模型版本、概率、耗时及 OpenRouter 返回的 `usage.cost` 均保存在结果中。
费用缺失时单独计数，不推断免费；耗时是客户端端到端耗时，包含网络与供应商处理时间。
`noul >= 0.5` 仅用于给冒烟样例评分，不作为真实工具授权策略。

这些样本检查此前出现过的意图混淆及指代／否定边界，不是独立大规模测试集。
小样本的准确率、概率或置信度不能证明生产质量；实际启用还需评估延迟、错误处理与独立样本。

## 2026-10-20 实测

使用 `OPENROUTER_JEV_API_KEY`，实际模型返回 `typesafe/jev-1.14-20261014`。
单条连通测试成功后，完整执行固定样本；未根据返回结果修改问题或参考标签。

| 项目 | 结果 |
|---|---|
| 用户提供的 noul 示例 | 连通测试返回 0.07，低于本实验 0.5 判定阈值 |
| 中文三分类 | 13/13：respond 4/4、retrieve 5/5、mixed 4/4 |
| 13 条路由端到端耗时 | 中位 529.79 ms，最小 512.39 ms，最大 1709.06 ms |
| 完整 14 条实验费用 | OpenRouter 报告 $0.000382410 |
| 加上先前单条连通测试 | 共 15 次成功调用，报告费用 $0.000398538 |

原始文件位于仓库的忽略目录，包含完整请求／响应与用量：

- `outputs/verification/jev-20261020T142436456703Z.jsonl`：单条连通测试。
- `outputs/verification/jev-20261020T142451741630Z.jsonl`：完整实验。

“真是高高在上呢 哪里出现过”判为 retrieve，分配给该选项的概率为 0.79；
接续的“那又咋了，你说话怎么这么无趣啊”判为 respond，概率为 0.98。
这些是模型返回的分数，不是对生产准确率的估计。当前只完成独立 API 接入与实验，
尚未将分类结果接入 Chat Supervisor，也未实现 query rewrite。

## 本地 Qwen3-4B 改写实验（2026-10-20）

本机此前没有 4B 权重，已有的是 Qwen3-0.6B 和 Qwen3-VL-8B。
此次下载 [Qwen 官方 GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF)，使用
Q4_K_M（4B 参数、4-bit 量化），没有做任务微调。权重文件 2,497,280,256 字节，
revision `bc640142c66e1fdd12af0bd68f40445458f3869b`；
SHA256 `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` 已核对。

复用 `gateway.local.llama_cpp`，读取先前真实 Jev 的 13 条 decision，不再次调用云 API。
模型仅生成 `current_task`、`retrieval_query`，代码保留 Jev 的 action 并渲染固定 append。
使用 llama.cpp `b10809-5266f24da`，GPU offload 99、4 线程、4096 context、单 slot、
非思考模式；请求 temperature=0、最多256输出 tokens，并启用 JSON Schema 约束解码。
这不是无约束生成的 JSON 格式成功率。

| 实验 | 字段约束通过 | 全部13条耗时中位数 | 范围 |
|---|---:|---:|---:|
| 第一版：规则 + JSON 输入 | 10/13 | 993.46 ms | 643.01–1590.68 ms |
| 第二版：简化规则，历史在前，当前消息与动作在后 | 13/13 | 876.79 ms | 526.45–1492.54 ms |

第一版3条 respond 仍生成检索词，其中核心反馈样例被改成查找上一轮台词。
第二版修正了该样例，输出“回应用户对助手说话方式的评价”且 query=null。
但第二版仍有两条明确语义错误：

- “真是高高在上呢 哪里出现过”只提取出“哪里出现过”，丢失真正待查台词。
- “你觉得我是在问动画剧情吗？我是在说你刚才的回答”被改为“判断用户是否在询问动画剧情”，误解反问。

第二版 mixed 指代样例的任务说明仍停在“先回答第二件，继续查找第一件”，
没有明确其中回应反馈的目标；检索词已识别雨中等人。不能将13/13字段通过当作语义全对。
两版来自相同小样本，第二版参考第一版失败后修改，不是独立测试集。
只检查重写产物，没有调用 Supervisor 验证最终回答效果；两版耗时不含模型加载，
包含前缀缓存复用，单次顺序执行，不能据此泛化其他机器或稳定性能收益。

完整模型输入、原始响应、append、token用量及服务配置在：

- `outputs/verification/rewriter-qwen3-4b-20261020T144057Z/`：最初2条试跑，遇到 respond 约束失败后停止。
- `outputs/verification/rewriter-qwen3-4b-baseline-20261020T144215Z/`：第一版完整13条。
- `outputs/verification/rewriter-qwen3-4b-v2-20261020T144412Z/`：第二版完整13条。

共28次本地推理，无新增云模型调用。临时测试进程均已停止；权重和结果保留在 outputs。
`results.jsonl` 中 `semantic_review=pending` 指程序不自动做语义评分，上述结论来自人工式逐条审阅。

复跑时先在一个终端启动本机模型：

```sh
llama-server \
  -m outputs/models/Qwen3-4B-GGUF/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf \
  --alias seekino-qwen3-4b-rewriter --host 127.0.0.1 --port 18841 \
  -ngl 99 -c 4096 -np 1 -t 4 --seed 42 \
  --chat-template-kwargs '{"enable_thinking":false}' --no-webui --offline
```

在另一个终端执行，完成后关闭前面的测试服务：

```sh
uv run --no-sync python -m evals.benchmark.intent_routing.local_rewriter \
  --jev-results outputs/verification/jev-20261020T142451741630Z.jsonl
```

`--dry-run` 仅校验保存的 Jev 输入，不启动服务或模型调用；`--only` 可选择样例。
`--prompt` 可指定实验规则文件；模板文字见 `rewrite_prompt.txt`。
当前 runner 要求上游 Jev 结果成功、完整，遇到缺失 response/answer 的文件会拒绝执行。

## 8B 与当前 Chat 最低推理档对照（2026-10-20）

继续使用上面的第二版提示词和同一份真实 Jev decision，未修改 prompt、样本或 action。
本机已有的是 **Qwen3-VL-8B-Instruct Q4_K_M**，不是纯文本 Qwen3-8B。
本轮读取现有 Ollama GGUF，用同版 llama.cpp、4096 context、4 线程、单 slot、
temperature=0、256 输出上限和 JSON Schema 约束，只加载语言部分，不加载视觉 projector。
服务启动 2.600 秒，未计入逐请求耗时。它是另一套权重，不能把差异全归因于 4B → 8B。
模型定位见 [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B)
与 [Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct) 官方说明。

Chat 对照读取当前 supervisor binding：`codex_local / gpt-5.6-sol`，仅在内存中设置
`reasoning_effort=low`、关闭 fallback、60 秒超时，无工具。low 是本机 Codex 模型目录
公布的最低档；SDK enum 能接受 none 不代表当前入口支持。没有修改运行配置。
Codex adapter 的输出预算只参与上下文预检，没有 API 生成输出硬上限；
因此不能声称和本地的 256 tokens 硬上限相同。

### 固定评分与结果

评分在检查新输出前固定，4B 也按同一标准重评：

- 完整通过：两个字段合起来保留本轮目标与有效限定；不要求历史摘要或复述全部证据。
- 部分遗漏：主目标正确，但遗漏“仅据某句”“先后顺序”或本可消解的指代。
- 明显错误：错误目标／指代、把反馈当剧情、遗漏 mixed 独立诉求或捏造事实。

| 模型 | 完整通过 | 部分遗漏 | 明显错误 | 成功请求耗时 |
|---|---:|---:|---:|---|
| Qwen3-4B Q4_K_M，既有 v2 结果 | 6/13 | 5/13 | 2/13 | 中位 0.877 s，0.526–1.493 s |
| Qwen3-VL-8B-Instruct Q4_K_M | 10/13 | 3/13 | 0/13 | 中位 1.936 s，1.291–2.246 s |
| gpt-5.6-sol，Codex low | 前2条通过 | 未完成全组 | 无完整结论 | 两条分别 9.234 s、6.031 s |

本地两组字段契约均 13/13 通过，不能代替语义评分。
8B 修复了 4B 把“哪里出现过”当检索词、把反问反馈当剧情问题的两处错误。
剩余遗漏：一例将特定“她们”泛化成“剧中角色”，两例将“先……再……”弱化成“并”。
4B 的“她们和好”候选词较短，但 task 保留了对象，按统一口径计完整通过。
8B 的“第二件先回答”已解析两项内容且保留回应优先级；允许后台同时查找，不扣顺序分。

Chat 第3例收到 HTTP 429，冷却后对未完成样本做一次有限重试仍为 429，随即停止。
不能把 2/2 成功改写称为整组或生产 100% 正确率。更早有一次调用后写用量失败
（dataclasses.asdict 不能 deepcopy mappingproxy），已修复；该次输出与用量未落盘，
排除出对照但可能已消耗账号额度。共尝试5次 Chat 生成，保留2条成功产物、2条429记录，
另1次记录失败。没有新增 Jev 调用。

两条成功 Chat 调用输入分别 12,047／12,121 tokens，输出46／32 tokens，
缓存输入均为0；使用当前 Codex harness，包含其基础上下文及每例启动 session/app-server。
这不是精简直接 API 调用的成本／速度。SDK 未报告账单金额，不用 API 标价推算实际扣费。

按样本把之前 Jev 实测时长与本次8B时长相加，中位约2.481秒；只是跨次记录的阶段和，
不是已接入 Chat 的端到端测量。13条均为已用过的边界样本，提示词曾参考4B失败调整，
没有独立留出集；单次顺序调用、包含前缀缓存，未测GPU服务器、并发或下游最终回答。
本次8B与云对照在短时间内并行运行，也不是严格控制硬件负载的基准。

### 产物与复跑

- `outputs/verification/rewriter-qwen3-vl-8b-20261020T145926Z/`：13条输入、原始响应、append、用量及启动参数；临时服务已关闭。
- `outputs/verification/rewriter-qwen3-vl-8b-20261020T145721Z/`：首次启动未就绪，未产生推理样本，测试进程已关闭。
- `outputs/verification/chat-rewriter-sol-low-complete-20261020.jsonl`：2条成功及首次429。
- `outputs/verification/chat-rewriter-sol-low-remaining-20261020.jsonl`：有限重试的429及原因。
- `outputs/verification/chat-rewriter-sol-low-20261020T1500Z.jsonl`：早期记录失败说明。
- `outputs/verification/rewriter-comparison-20261020.json`：统一语义评分、统计及限制。

当时复用 Codex Chat 的独立入口（不会连接业务数据库；以下只做 dry-run）：

```sh
uv run --no-sync python -m evals.benchmark.intent_routing.chat_rewriter \
  --jev-results outputs/verification/jev-20261020T142451741630Z.jsonl \
  --dry-run
```

脚本的历史行为是去掉 `--dry-run` 后调用 Codex 生成，支持 `--only` 和 `--out`，失败保存后停止；新测试不应直接重放该 GPT 配置。2026-10-20 当次只增加评估入口与结果记录，没有把改写接入实际 Chat。

## 纯文本 Qwen3-8B 与固定模板填槽（2026-10-20）

用户已另行下载官方 `Qwen/Qwen3-8B`，revision
`b968826d9c46dd6066d109eabc6255188de91218`。本次检查下载 manifest 的11个文件大小、
5个 safetensors 分片及399个张量索引，未再次下载模型。
从原 BF16 权重用[同版官方转换器](https://github.com/ggml-org/llama.cpp/blob/5266f24da/convert_hf_to_gguf.py)
生成 BF16 GGUF，再由现有 `llama-quantize` 生成 Q4_K_M。
转换依赖通过 `uv run --frozen --no-sync --with ...` 临时装配，不修改项目依赖或 `.venv`。
Q4 文件5,027,783,584字节，SHA256
`a0aa87c95df6a10c37d905f8514cabab12880f448c163a7235f5a1e1b235f6b5`。
原始权重及Q4副本保留；测试完清理了本次生成的16.39GB BF16 GGUF中间文件。
转换、量化及模型加载均不计入下列逐请求耗时。

### 先按旧职责测试

同一份 v2 提示词、13例、非思考模式、llama.cpp版本和运行参数：

| 精度 | 完整通过 | 部分遗漏 | 明显错误 | 中位耗时 |
|---|---:|---:|---:|---:|
| 纯文本8B Q4_K_M | 5/13 | 7/13 | 1/13 | 1.866秒 |
| 同权重 BF16 | 6/13 | 7/13 | 0/13 | 3.676秒 |

Q4仍将明确反馈改为“确认用户是否在询问动画剧情或对回答风格提出反馈”。
BF16去掉了剧情分支，但仍擅加“确认用户意图”，并将原台词截成“高高在上呢”。
因此旧任务的问题不能全部归因于量化。这里保留旧评分供追溯，不据此认定8B不足或必须换更大模型。

### 用户纠正职责后的填槽实验

此前虽然固定了 action，但仍让小模型自由生成整句 `current_task`，扩大了它的职责。
本实验改为：**Jev返回类别，代码选择固定模板，8B仅填写检索目标。**

| action | 程序固定的处理方式 | 8B职责 |
|---|---|---|
| respond | 仅回应当前消息，不新增视频检索；已有上下文仍可用 | 不调用 |
| retrieve | 查视频证据，回应检索诉求 | 只填 `retrieval_query` |
| mixed | 回应与检索均处理，回应内容和顺序遵循原消息及上下文 | 只填 `retrieval_query`，不混入回应部分 |

原始用户消息完整保留，随后 append 程序生成的模板和候选检索词；
模型不能输出 `current_task` 或修改 action。模板只针对本轮，不评价上轮检索，也不约束后续轮次。
8B的 schema 只有 `retrieval_query: string | null`；null 留给 Supervisor 处理未明确的目标。
未修改真实 Chat，也未调用 Supervisor 验证最终回答。

沿用13例，其中4条respond检查跳过调用，9条检查模型填槽。
新评分预先固定，只评目标、原话和明确指代，不再要求填写回应内容或执行顺序。
候选词允许简洁，原消息和历史仍交给Supervisor；不能把这套分数与旧两字段任务当成同一准确率比较。

| 项目 | 结果 |
|---|---|
| 4条respond代码路径 | 全部跳过模型，检索槽为空；不计入8B命中数 |
| 9条模型输出 | 8条完整通过、1条部分遗漏、0条明显错误；字段约束9/9 |
| 9次模型调用耗时 | 中位1.151秒，范围0.924–1.426秒 |
| 输出token总数 | 164 |
| 剩余遗漏 | “真是高高在上呢”仍被改为“高高在上 台词 出现位置”，原台词截短 |

正确案例包括“第二种”补为“天台争吵，争吵开始前发生了什么”，
mixed只提取“她们第一次登台的片段”或“雨中等人的场景”，不重新解释用户的吐槽。
按样本相加此前Jev耗时：含模型填槽中位约1.734秒，纯回应代码路径约0.535秒；
这是跨次阶段耗时相加，尚未做实际链路的端到端测量。
样本已用于之前试验，提示参考过已知错误，不是独立留出集；尚未测试目标GPU和并发。

### 记录与复跑

- `outputs/models/Qwen3-8B-GGUF-local/b968826d9c46dd6066d109eabc6255188de91218/`：Q4权重、转换来源及日志。
- `outputs/verification/rewriter-qwen3-text-8b-20261020T151723Z/`：旧两字段Q4结果。
- `outputs/verification/rewriter-qwen3-text-8b-bf16-20261020T151906Z/`：旧两字段BF16结果。
- `outputs/verification/template-qwen3-text-8b-20261020T152519Z/`：固定模板填槽结果。
- `outputs/verification/rewriter-text8b-report-20261020.json`：两个任务各自的逐条评分与统计。

启动同参数的本机 llama-server 后，运行：

```sh
uv run --no-sync python -m evals.benchmark.intent_routing.template_rewriter \
  --jev-results outputs/verification/jev-20261020T142451741630Z.jsonl \
  --model seekino-qwen3-text-8b-rewriter
```

可加 `--dry-run` 验证13例的9次模型请求／4条代码路径，不执行生成。
本次共35次本地生成（13+13+9），无新增云API调用；三个临时测试服务均已关闭。
