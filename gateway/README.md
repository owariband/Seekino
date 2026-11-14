# Seekino Gateway

模型连接和角色在仓库根目录的 [config.yaml](../config.yaml) 中配置。Docker 中导入配置、设置提供商密钥、切换 Kimi 或 Codex 的步骤见[本地配置指南](../deploy/local-setup.md)。已有视频索引的模型与维度必须和查询端一致。

## Chat Agent 模型来源

`model_sources` 描述连接方式，`agent_model.source` 选择其中一个来源。`adapter`
决定协议实现，`provider` 只用于 provenance、用量和价格归属，OpenAI 兼容协议的
`profile` 处理不同 API 方言。Chat 的 Query 子任务复用当前选中的 Supervisor 模型配置，
包括来源、模型、主备、思考档位与调用控制；切换会话模型时也随之切换。
旧配置没有独立 `models.supervisor` 时，以 `models.agent` 及已有 Chat 环境覆盖作为整个
Chat 的默认配置。旧独立 Query／Evaluator 模型覆盖仍可被兼容入口读取，
`SEEKINO_AGENT_QUERY_MODEL` 不再单独控制 Chat 子任务的模型。

API Agent 的 Deep Agents、structured 和工具循环复用同一模型容量解析入口及缓存。
在 Supervisor 绑定启用 `auto_compact_ratio` 后，Chat 父子使用相同动态窗口和主备安全预算；
字符数、超时、输出和工具次数保护继续生效。原生 runtime 仍自行管理上下文。

内部 Supervisor 支持 `supervisor_harness=deepagents` 和 `codex`，按配置选择。
Deep Agents 路径通过 `AgentModelFactory.build_deep_agent()` 使用 SDK；SDK 负责模型／工具
循环、摘要和工具结果卸载。Gateway 将已有 OpenAI 兼容或 Ark Adapter 的单次请求接入 SDK，
继续使用配置中的模型、推理参数、超时、fallback、用量及追踪。视频 Query／Evaluator／Chat
的审核流程仍作为业务工具执行。

一个 Conversation 对应一个 thread；配置重建共享同一状态存储。每轮的中间 checkpoint 仅
保存在内存，最终回答经 SQL 确认后才将 messages、compact 和 StateBackend `files` 的完整
状态写入持久 saver。只暴露虚拟 `read_file` 以读取卸载内容，关闭默认通用 subagent。失败或
取消只丢弃本轮临时状态，显式删除 Conversation 才清理持久 checkpoint。旧 Conversation
没有 checkpoint 时保留原有 SQL 冷启动逻辑，不把文本投影等同于原生工具历史。

Deep Agents 路径要求 OpenAI 兼容或 Ark API source；Codex Supervisor 路径要求
`codex_app_server` source，已有原生 thread 恢复，但当前每次 invocation 仍创建并关闭
app-server 进程。下方 Codex／Claude 配置还可服务已有原子 Agent Adapter。以上是当前
代码能力；完整本机 Bridge 的目录、权限、交互与恢复接入仍按独立验收项推进。

DeepSeek 使用 OpenAI Adapter 的 DeepSeek profile，并显式配置 Ark fallback：

```yaml
model_sources:
  deepseek_api:
    adapter: openai
    provider: deepseek
    profile: deepseek
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
  ark_default:
    adapter: ark
    provider: ark
    api_key_env: ARK_API_KEY

agent_model:
  source: deepseek_api
  model: deepseek-flash
  max_output_tokens: 8192
  request_timeout_seconds: 180
  fallback:
    source: ark_default
    model: doubao-seed-2-0-lite-260428
```

Codex 复用当前系统用户已有的本机 Codex 登录，不需要配置 API Key：

```yaml
model_sources:
  codex_local:
    adapter: codex_app_server
    provider: codex
    max_concurrency: 4

agent_model:
  source: codex_local
  model: gpt-6-sol
  max_output_tokens: 8192
  request_timeout_seconds: 180
```

需要指定非默认 Codex 可执行文件时，在 `codex_local` 下添加 `codex_bin`。

Claude 的本机订阅模式复用当前系统用户已有的本机登录：

```yaml
model_sources:
  claude_local:
    adapter: claude_sdk
    provider: claude_code
    auth_mode: local_subscription
    max_concurrency: 4

agent_model:
  source: claude_local
  model: claude-sonnet-4-6
  max_output_tokens: 8192
  request_timeout_seconds: 180
```

Claude API Key 模式把 `auth_mode` 改为 `api_key`，并添加
`api_key_env: ANTHROPIC_API_KEY`；程序会从该字段指定的环境变量读取密钥。
需要指定非默认 Claude 可执行文件时，在 source 下添加 `cli_path`。

`local_subscription` 会在 Claude 子进程中显式清空继承到的 API Key、Auth Token、
自定义 Base URL 和云厂商路由开关，防止本机环境变量把订阅登录静默改成 API 计费。
`api_key` 模式同样会清空相互冲突的认证方式，只注入 `api_key_env` 指定的值。
Claude 调用使用 `--no-session-persistence`，不会把问题、工具结果和回答写入个人
`~/.claude/projects` 历史。每个审核回合启动新的 CLI 进程，并在下一轮提示中显式携带
原始任务、上一轮可见回答和审核反馈；隐藏推理不跨回合保留，Agent 可以通过 Seekino 工具
重新读取规范工作区。认证目录保持不变，因此本机订阅模式可继续读取既有登录凭证。

Codex app-server 与 Claude Agent SDK 当前都没有暴露可由 Seekino 设置的硬输出 token
上限。它们配置中的 `max_output_tokens` 用于 Seekino 的上下文输出预留；实际调用仍由
请求超时、模型调用次数和 provider 自身的输出上限约束。DeepSeek 的同名配置会直接
传给模型 API。

CLI provider 会在启动进程前检查初始提示、工具 Schema 和结构化输出 Schema 是否能放入
`agent_runtime` 的上下文硬预算。Codex 还会加入按当前锁定 SDK 实测并向上取整的基础输入
预留。Codex/Claude 内部后续工具回合的 transcript 不对 Seekino 暴露，因此这项预检不执行
伪压缩，也不把内部续轮描述成可由 Context Canvas 精确治理。

修改 `model_sources`、`agent_model`、认证方式或可执行文件等模型连接配置后，需要重启
Server。`SEEKINO_AGENT_FALLBACK_MODEL` 只覆盖已经配置的 fallback 模型，不会创建或猜测
fallback source。当前支持 OpenAI → Ark 与 Ark → Ark；Codex 和 Claude 不会隐式切换到 Ark。

### 云端 Deep Agents 与 MySQL checkpoint

在完整的云端模型配置中选择 `supervisor_harness: deepagents`，并为聊天及实际执行的模型
角色绑定 API source。本地 Codex / Claude 配置不需要因此改变。

```yaml
supervisor_harness: deepagents

models:
  agent:
    source: deepseek_api
    model: deepseek-flash
  supervisor:
    source: deepseek_api
    model: deepseek-flash
    supports_vision_input: false
    max_output_tokens: 8192
    request_timeout_seconds: 300
```

cloud 模式在构造外部资源前要求 Deep Agents，并拒绝任何被选择的 Codex / Claude 模型
绑定及 fallback。`pi_host` 和 `supervisor_harness: pi` 已不再是有效配置。

`conversation_id` 直接作为 LangGraph `thread_id`。每轮先在内存中执行，最终 SQL 消息
提交成功后才将完整 graph state 写入 MySQL；失败、取消和关闭不会删除上一份已确认
checkpoint，显式删除 Conversation 才删除对应 checkpoint。没有 checkpoint 的旧 Conversation
继续按既有 SQL 展示历史冷启动，不承诺历史 Pi JSONL 中的 tool 上下文无损迁移。

checkpoint Schema 只通过显式初始化或迁移创建，不在 API 启动时自动变更数据库：

```bash
python -m server.cli init-db
# 或按环境评审后执行 deploy/migrations/20261024_deepagents_checkpointer.sql
```

## 网页搜索

`WebSearcher` 根据独立 `web_search.provider` 选择 DDGS（默认，无 API key）或 DeepSeek 官方 Search API。
Server 注入配置，Chat 模型选择与搜索供应商互不绑定；保留统一 `WebSearchHit(title, url, snippet)`。
DDGS 查询限 500 字符，DeepSeek 限 128，Agent 工具 Schema 使用对应上限。两条路径共用 URL 校验、
去重与摘要预算；保留 DDGS 依赖，只有选择 DeepSeek 时才读取搜索 key，不自动跨供应商回退。
日志记录所选 provider/backend、请求 ID、可用的 HTTP 状态、耗时与结果数，不记录凭据或查询正文。
DDGS 的 auto 由库选择后端；配置写错或指定了已禁用后端时明确失败，避免库静默改回 auto。
