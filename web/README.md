# AniVAG Web

AniVAG 的正式 Web 前端。视觉基于已确认的 A「青瓷札记」，通过同一套功能组件连接
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

上传没有引入通用上传面板。AniVAG 使用自定义分片号、`Content-Range`、SHA-256、
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
AniVAG HTTP API
       ↓
WorkspaceProvider ── Series 选择与运行模式
       ↓
Feature pages ───── Search / Library / Tasks / Settings
       ↑
UploadProvider ───── 3 文件并行、3 分片 worker、暂停恢复、Job SSE
```

## 已实现页面

- 问画面：Agent 对话、候选片段、逐条选择理由、原始证据、上下文与原视频跳时播放。
- 资料库：Series 创建/选择、Episode 状态、上传入口。
- 传输案台：批次总进度、每个视频独立进度、最多 3 文件并行、暂停/继续/重试。
- 解析任务：阶段时间线、进度、Chunk checkpoint、错误与自动重试状态。
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
VITE_WORKSPACE_ID=local-default
```

`VITE_API_BASE_URL` 留空时使用同源地址；`VITE_WORKSPACE_ID` 未设置时遵循服务端默认值
`local-default`。连接失败不会生成本地替代数据。

验证：

```bash
npm run typecheck
npm test
npm run build
npm run test:e2e
```
