# anivag Web

anivag 的正式 Web 前端。视觉基于已确认的 A「青瓷札记」，通过同一套功能组件连接
FastAPI 正式数据。前端不内置 Series、视频、任务或检索结果；API 不可用时只展示明确的
连接错误与空态。

## 技术栈

- React 19 + TypeScript 7：严格类型下实现交互与 API 契约。
- Vite 8：开发代理、热更新与生产构建。
- React Router 8：问画面、资料库、解析任务、设置四个一级路由。
- TanStack Query 5：Series、Job 等服务端状态的缓存、刷新与错误边界。
- CSS Modules + CSS Variables：A「青瓷札记」视觉、响应式布局与日/夜主题。
- Lucide React：统一且可访问的线性图标。
- Vitest + Testing Library：业务逻辑与主流程组件测试。
- Playwright：预留浏览器端验收测试入口。

上传没有引入通用上传面板。anivag 使用自定义分片号、`Content-Range`、SHA-256、
`Idempotency-Key` 和服务端恢复点；将这段协议收敛在 `UploadProvider` 中，比在通用库上
叠加适配层更短，也更容易保证进度语义准确。

## 目录边界

```text
src/
├── app/       # 路由、主题、工作区上下文与全局壳
├── api/       # HTTP 边界与最小前端契约
├── features/  # 按 search/library/uploads/tasks/settings 纵向组织
├── shared/    # 真正跨功能复用的格式化与小型 UI
├── styles/    # 全局 token 与基础排版
└── test/      # 单测环境
```

功能目录可以依赖 `api/` 与 `shared/`，`api/` 不依赖 React。跨功能状态只放在
`app/` 或对应 Provider 中，页面内部状态保留在页面内，避免形成万能 store。

主要数据流：

```text
anivag HTTP API
       ↓
WorkspaceProvider ── Series 选择与运行模式
       ↓
Feature pages ───── Search / Library / Tasks / Settings
       ↑
UploadProvider ───── 3 文件并行、3 分片 worker、暂停恢复、Job SSE
```

## 已实现页面

- 问画面：Agent 对话、结构化视频片段窗口、逐条选择理由、原始证据、上下文与原视频跳时播放。
- 资料库：Series 创建/选择、Episode 状态、上传入口。
- 传输案台：批次总进度、每个视频独立进度、最多 3 文件并行；刷新页面后可重新选择同名同大小的原文件，只补服务端缺失分片，合并中的会话可直接继续合并。
- 解析任务：阶段时间线、进度、Chunk checkpoint、用量、错误与自动重试状态；以 UploadSession 为整任务删除粒度，FAILED 主任务可手动重试并沿用检查点。
- 设置：白天/黑夜主题、API 连接信息与数据边界说明。

## 启动

推荐 Node 24 LTS：

```bash
npm install
npm run dev
```

开发环境默认通过 Vite 代理连接 `http://127.0.0.1:9621`。跨域或独立部署时，可以在
`web/.env.local` 指定：

```bash
VITE_API_BASE_URL=http://127.0.0.1:9621
VITE_TENANT_ID=local-default
```

`VITE_API_BASE_URL` 留空时使用同源地址；`VITE_TENANT_ID` 未设置时遵循服务端默认值
`local-default`。连接失败不会生成本地替代数据。

## Release / Debug 展示模式

同一套构建按请求头选择展示模式。后端公开接口 `GET /api/system/runtime-config`
仅在 `x-use-ppe` 的值精确等于 `1` 时返回 `{"displayMode":"debug"}`，其他情况均
返回 release。响应使用 `Cache-Control: private, no-store` 和 `Vary: x-use-ppe`。
网关需同时向页面和后续 API 请求注入该头；前端不会自行补头，也不使用环境变量、
URL 参数或本地存储启用 debug。

工作区从当前资料来源的 API 读取模式。首次加载、接口失败或返回值未知时使用
release；切换来源会取消旧请求，避免旧来源的 debug 结果影响新页面。

- Release 保留状态、整体进度、总耗时、回答与视频证据、用量概况及正常操作。
- Debug 额外展示解析切片 / 理解 / Refine / 完成单元、执行阶段明细、模型与预算
  设置、内部连接信息及调用用量明细。

Debug 中保存的 Chat 参数不会进入 release 请求，release 使用服务端默认限制。
上传在 release 使用默认解析配置。日志、模型用量记录和权限判断不随展示模式改变。

验收时分别通过网关注入与移除 `x-use-ppe: 1`，确认技术明细出现与隐藏，普通上传、
Chat、错误提示和任务操作仍可使用。直接访问本地开发服务且不注入 header 时默认为
release；也可用 `curl -H 'x-use-ppe: 1' http://127.0.0.1:9621/api/system/runtime-config`
检查后端判定。

本机资料不要求账号登录。若要在 localhost 登录远端账号、浏览远端公开或个人资料，
在 `web/.env.local` 增加固定的远端服务地址，然后重启 Vite：

```bash
ANIVAG_REMOTE_API_URL=https://your-anivag-server.example
ANIVAG_REMOTE_COOKIE_NAME=anivag_auth
```

`ANIVAG_REMOTE_COOKIE_NAME` 必须与远端的 `ANIVAG_AUTH_COOKIE_NAME` 一致，默认均为
`anivag_auth`。未配置地址时只使用当前服务。环境变量优先于 `.env` 文件；远端目标必须
为 HTTPS origin，不带路径或凭据，独立的本机测试服务可以使用 loopback HTTP 地址。

开发代理自动提供 `/remote/api` 和 `/remote/healthz`，同时注入
`VITE_REMOTE_API_BASE_URL=/remote` 和远端 origin。它检查浏览器请求来源，再向固定服务
转发，隔离本机与不同远端的会话 Cookie，并透传 CSRF、分片上传、视频 Range 和 SSE。
远端会话保留 HttpOnly、SameSite，路径限定为 `/remote`；HTTP loopback 开发入口去掉
Secure 属性，上游 HTTPS 校验保持开启。登录、退出和远端失联均不改变本机资料归属。
切换资料来源不会上传本机资料；本次没有实现资料同步。

这个代理仅供 Vite 开发联调。生产构建不会把 `ANIVAG_REMOTE_API_URL` 自动转换为
`/remote`；生产环境应单独部署同源代理，并配置 `VITE_REMOTE_API_BASE_URL`。若服务
返回同上游的绝对 `/api` 媒体地址，配置 `VITE_REMOTE_API_ORIGIN` 可将它映射到该代理。
实际验证码登录还需要远端身份服务、Redis 和 SMTP 正常运行。

验证：

```bash
npm run typecheck
npm test
npm run build
npm run test:e2e
```
