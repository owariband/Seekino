# 个人模型服务的设置与聊天验收

2026-11-07 在本机 5188 网页、19641 API 和隔离的 MySQL 测试库验证。入口在「设置 → 回答模型」。空配置直接展示接口协议、服务名称、Base URL、API Key、模型 ID 和图片能力。界面样式使用 DeepSeek Flash max 生成的 CSS，表单、路由、协议执行和验收由 Codex 完成。[设计](../../design/03-product-reassessment/personal-model-connections-trd.md) · [PR #74](https://github.com/owariband/Seekino/pull/74)

![本机设置页的个人模型连接表单](assets/personal-model-connections-20261107.png)

| 验收项 | 结果 |
| --- | --- |
| 浏览器正常入口 | Chrome 从无个人连接开始，切换协议时 URL 示例随之更新；填写自定义地址、虚构密钥与两个任意模型 ID。保存、刷新、编辑、选择第二个模型测试连接、选择第一个模型发消息和删除均成功。添加及选模共创建 0 个聊天，首次发送后创建 1 个聊天 |
| 会话执行 | 对四种协议分别通过真实 API 创建测试聊天、发送消息。回环服务核对对应认证、模型和工具声明，四个聊天均以对应模型名持久化回复并完成。测试连接也分别命中同一端点。验收会话及连接均已清理 |
| 协议合同 | Chat Completions 走 /chat/completions，Responses 走 /responses，Anthropic 走 /v1/messages，Gemini 走 /models/{model}:generateContent。逐协议覆盖工具声明、模型发起调用、带调用 ID 或名称回传工具结果和最终文本；兼容测试另覆盖旧 Chat Completions 令牌字段 |
| 数据与归属 | 个人默认只用于新聊天。两个所有者隔离，过期修改返回冲突，修改凭据目的地要求重新输入或清除密钥。读取设置和历史回复均不回显凭据。删除连接后已完成消息仍支持幂等重放 |
| 布局及回归 | 浏览器验证桌面 1280px、手机 320px；手机无横向溢出。独立前端 612 项单测、17 项相关 Chrome 用例及生产构建通过。Kino 联合工作树 620 项前端单测通过。服务及网关相关回归 412 项通过、1 项跳过 |

供应商验证使用本机可控的 HTTP 服务和测试密钥。未对四家厂商逐一使用付费账户验收。自定义回答模型在收到每次模型调用的完整输出后展示文本。
