# 聊天页重复解析与连接状态：前端性能验收

2026-11-12。PR [#126](https://github.com/owariband/Seekino/pull/126)，分支 `perf/chat-frontend`，目标 `dev`。设计见[主 TRD](../../design/03-product-reassessment/chat-frontend-performance-trd.md)，当前执行状态见[工作台](../../dev/workbench.html)的 `CHAT-PERF-01`。本报告分别记录隔离性能对照、本地冒烟和 13:43（UTC+8）的公网发布验收；隔离对照数据不代表真实用户延迟已经达标。

## 问题与改动

输入、正文和思考过程共用页面级渲染，历史 Markdown 随状态更新重新执行。连接提示又把“没有 PROCESSING 消息”当作尚未受理，导致可信终态先于响应关闭时重新显示连接中，并误唤醒 Kino。

本次在现有 Search feature 内增加消息与 Markdown 的缓存边界，稳定 Context 和缺省数组；用受理状态区分连接提示，停止按钮只对应真实 PROCESSING，传输收尾期间发送按钮保留但禁用；复用幂等 key 阻止旧请求的晚到对账覆盖新请求。滚动合并到每帧最多一次，观察容器及直接子元素的实际尺寸，用户上滚后停止跟随，回到底部后恢复。

实现由 OpenCode 的 DeepSeek Flash worker 完成，Codex 复核并执行测试、生产构建和浏览器对照。没有改模型、后端、业务 API 或权限投影；没有增加依赖。Kino 保留基线已有的 6/10/15 fps 和 `happy`、`sleep`、`leap`，不重写引擎，也没有给正文增加人为逐字延迟。

## 对照条件

- 基线：`1adac5d3`；方案：实现提交 `1392bfa8`。两份生产构建使用相同锁文件和依赖。
- Chrome `157.0.8103.51`，1440 × 900，CDP CPU 降速 4 倍。基线和方案串行运行，测量时没有并行运行前端测试。
- 每场景 1、20 或 60 条完成态回答，固定 Markdown；侧栏只有一个 fixture 会话。输入预热 5 次，统计后续 30 次。
- 独立 Node HTTP 服务每 33 ms 发送一次 SSE，共 100 个等长正文增量；每秒发送一次整个会话快照。所有接口都是隔离 fixture，不连接业务数据库或模型。
- 输入时间是合成 input 分发到下一 rAF 观察；正文时间是 SSE 接收到既有 DOM 观察回调。两者都不声称精确测得物理绘制、真实 INP 或供应商 FTT。
- 每个场景保留一组最终成对回放。p95 只描述这批样本，不推断生产环境尾部。早期探索记录保留在本地原始证据目录，不混入最终表。

结构化结果见[配套 JSON](chat-frontend-performance-20261112.json)；原始命令输出、初次探索、浏览器 trace 和失败截图在 `outputs/verification/chat-frontend-performance-20261112/`。

## 实测结果

先用真实 `SearchPage`、真实 `react-markdown` 和模拟 API 的 jsdom 探针确认重复计算是否消失。20 条历史回答的结果如下；计数不等于浏览器耗时。

| 操作 | 基线 Markdown 执行次数 | 方案执行次数 |
| --- | ---: | ---: |
| 输入一个字符 | 20 | 0 |
| 仅更新 reasoning | 20 | 0 |
| 更新一条回答 | 20 | 1 |

浏览器表中的每格为 **p50 / p95，单位 ms**。

| 历史回答数 | 输入：基线 | 输入：方案 | SSE 到 DOM：基线 | SSE 到 DOM：方案 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 15.0 / 16.6 | 15.9 / 16.9 | 10.9 / 20.2 | 9.8 / 18.2 |
| 20 | 16.8 / 20.7 | 15.8 / 16.4 | 25.8 / 34.7 | 10.5 / 18.4 |
| 60 | 36.6 / 38.6 | 15.4 / 16.0 | 35.3 / 38.8 | 11.0 / 18.4 |

60 条历史下，输入 p95 下降约 59%，SSE 到 DOM p95 下降约 53%；流式期间显示帧间隔 p95 从 50.0 ms 降到 16.7 ms。1 条历史的输入没有测出收益，p95 增加 0.3 ms，接近一帧等待尺度；不把所有场景写成同比提速。两版在本轮输入和流式窗口中均未记录到超过 50 ms 的 long task，不能据此宣称“消除了长任务”。

Kino 的三次 progress 到状态属性更新，在 60 条历史下分别从 43.5 / 38.8 / 35.9 ms 降到 2.9 / 2.4 / 2.3 ms。这里只有每场景三次切换，不能计算有意义的尾部结论；状态属性也不是物理绘制时间。收尾观察窗口内，基线三个场景均出现误唤醒 `look`，方案均未出现。

三个场景都验证了快照先完成、POST 终态先于 EOF、两秒收尾、手动上滚和回到底部：

| 行为 | 基线 | 方案 |
| --- | --- | --- |
| 已完成消息下面显示连接中 | 三个场景均出现 | 三个场景均未出现 |
| 终态收尾仍显示无效停止 | 三个场景均出现 | 无停止按钮，发送按钮禁用 |
| EOF 后保留草稿并可提交 | 通过 | 通过 |
| 上滚时保持阅读位置、回底部恢复跟随 | 通过 | 通过 |
| SSE 增量到 DOM 观察数量 | 每场景 100 / 100 | 每场景 100 / 100 |
| 请求次数、页面异常、未覆盖 API | 每场景 1 次、0、0 | 每场景 1 次、0、0 |

收益主要出现在已有较多历史文本的更新路径。当前没有证据要求再增加 delta 缓冲，因此保留直接增量交付。代价是一个局部滚动 hook、消息缓存边界和尺寸观察；Search 页面 JS 从约 96.64 KB 增至 98.10 KB，gzip 从 30.99 KB 增至 31.43 KB，未增加运行依赖。

## 正确性与回归

- `npm --prefix web run typecheck` 和 `npm --prefix web run build` 通过。
- 前端完整回归 33 个文件、658 项通过。新增测试覆盖 completed/failed 先于 EOF、可信快照先到、interrupted 恢复、旧请求晚到、新请求也已结束、滚动跟随、观察器清理及 Markdown 执行次数。
- `chat-streaming.spec.ts`、`chat-layout.spec.ts` 共 7 项：6 项通过。公开范围提示的位置断言失败；同一 Chrome 下，未修改的 `1adac5d3` 也在同一行失败，`status.x = 268`，要求至少 `357.609375`。本 PR 未改这项布局或测试期望，不能记为浏览器全通过。
- 原卡片选择仍使用页面已核对的 `selectedHit`，没有通过缓存旧消息的少数字段冻结媒体或权限。时间码、卡片、草稿、手机历史和模型设置的现有行为按上述单测及浏览器范围回归。

公开范围提示属于现有 `CHAT-HEADER-01` 的布局合同，需要另行核对；本次只记录证据，没有新建第二个 PR。真实模型回答质量、线上网络、媒体较多或带大体积检索 trace 的历史、很长的会话侧栏及真实手机未验证。

## 本地部署冒烟（2026-11-12 补验）

按本地试用要求，在 `http://127.0.0.1:5173/search` 启动生产构建，代理到 `9621` 的 API；前后端源码均为 `1ea15e78`。复用既有开发 MySQL、ES 和三条 Series，API 健康状态为 ok。该轮连接实际服务与模型，与上面的隔离性能对照分别记录。

首次复用的其他试验后端无法连接其本地模型。切回正常开发库后，Kimi 默认模型返回月度额度耗尽；DeepSeek 选项又沿用了查询改写的 256 token 上限，日志确认 `finish_reason=length`、`completion_tokens=256`。这些失败保留在本地证据中。随后为本次 API 使用独立的临时配置，把 agent / supervisor 绑定为 DeepSeek Flash、启用流式、输出上限 8192；查询改写预算和原配置文件保持原值，临时配置未提交。

通过真实页面发送一条无需视频检索的通用知识问题：收到 100 个 `answer.delta`、79 次正文 DOM 增长观察，最终 `answer.completed`；观测到 1 次 `answer.model.completed`，请求终止耗时约 4.59 秒。结束后无“正在连接助手”及停止按钮，页面 JS 异常为 0；点击 Kino 后观测到 `jump`。这些验证了本机入口可用，不代表回答质量、所有动作的物理绘制或线上性能已经验收。没有独立供应商 trace，因此不认证 FTT。

原始过程保存在 `outputs/verification/chat-frontend-performance-20261112/local-chat-smoke.json`、`local-chat-completed.png`，初次不可用、Kimi 额度和 256 token 截断分别保留。运行 PID 与日志在 `outputs/local-services/chat-frontend-preview.pid.json`、`chat-frontend-api.pid.json` 及同名前缀日志，临时模型配置为 `/private/tmp/seekino-chat-frontend-local-runtime.yaml`。该轮仅做本机部署；后续公网发布见下一节。

## 公网前端发布（2026-11-12 13:43，UTC+8）

公开入口为 [Seekino 聊天页](https://seekino.hypn0s.cloud/search)。线上前端原为 main `49ea0590`，本次在该基线上仅应用 `SearchPage.tsx`、`useAgentConversation.ts`、`useThreadFollow.ts` 三个运行文件，逐文件与 PR #126 的已验证实现核对 SHA-256。没有发布 dev 的其他差异，也没有推送或合并 main；仍沿用同一个需求 PR。

发布目录独立安装 main 锁定依赖，类型检查、132 项现有聊天回归和生产构建通过；发布树检查核对 816 个已跟踪路径、28 个发布资产。最终 dist 再通过 1/20/60 条历史的固定 SSE 回放，每场景 100 个增量；连接终态、发送禁用、上滚保持、返回底部及 Kino 收尾护栏均通过。首轮误从仓库根目录启动 Vitest，未加载 jsdom，改用 `npm --prefix web test -- ...` 后通过，未修改断言。

服务器接收 1.55 MB 静态包，验证包哈希及 48 个文件哈希，在原 Nginx 镜像上叠加 dist，形成 `seekino-web:chat-perf-1392bfa8-20261112`。镜像 ID 为 `sha256:9d8ea2407970398d5363d4d36ab7d9f5e423a3537a24be3e2403e64bf0665c93`。保留原有十份 Compose 组合，再追加仅指定 Web 镜像的覆盖文件；只重建 Web，API 的容器 ID、启动时间和镜像均与发布前一致。Nginx 配置检查、Web/API 健康状态与运行中 index 哈希通过。首轮传输中断后，以带保活的 SSH 重传同一包并重新校验，未使用残缺文件发布。

公网 Chrome 再核对 index 与 SearchPage 脚本哈希，确认实际提供新产物。一个新游客会话连续两轮真实聊天均完成，分别收到 42 / 8 个正文增量、42 / 8 次终态前正文 DOM 增长观察，单次请求终止约 7.56 / 3.53 秒；两轮都没有残留连接提示或停止按钮，且分别等到 Kino 回到 sleep。页面 JS 异常为 0。初次采证把匿名访问 `/api/auth/me` 的 401 一并计为接口失败；`AuthProvider.readIdentity` 明确捕获该状态并创建游客，后续两轮已正常完成，因此将这一条保留为预期游客引导响应，非预期接口失败为 0。原始分类记录保留，不重复消耗模型调用。这些仍不是物理绘制、认证 FTT、真实手机或长媒体历史的验收。

原始上线回执、浏览器记录与截图在 `outputs/verification/chat-frontend-performance-20261112/production-deployment.json`、`production-smoke.json`、`production-chat.png`；发布候选回放为 `release-browser.json`。服务器发布与回滚材料位于 `/home/ubuntu/seekino-cloud-runtime/releases/chat-perf-1392bfa8-20261112/`，旧镜像 `seekino-web:main-49ea0590` 保留，`rollback.py` 复用原 Compose 组合，仅恢复 Web。后续 main 正式发布需包含这三个文件的补丁。

## 复跑与交付边界

在相同依赖下分别构建基线与方案，再从本分支调用同一个回放脚本。下面的 dist 路径替换为对应构建目录；`--expect-fixed` 只用于方案，检查连接提示、发送禁用、滚动、误唤醒和 100 个增量的 DOM 观察。

```bash
npm --prefix web ci
npm --prefix web run build
SEEKINO_TEST_BROWSER_CHANNEL=chrome node web/tests/chat-frontend-performance.mjs /absolute/baseline/dist /tmp/chat-before.json 1,20,60
SEEKINO_TEST_BROWSER_CHANNEL=chrome node web/tests/chat-frontend-performance.mjs /absolute/candidate/dist /tmp/chat-after.json 1,20,60 --expect-fixed
```

浏览器回放需要允许本机 loopback 监听。前端全量中的远端代理测试同样需要该能力；受限沙箱中的 EPERM 不是业务接口失败，不应修改测试绕过它。

本次采用前端状态、渲染和滚动修复，交付于唯一 PR #126。代码、隔离对照、本地真实聊天和公网两轮冒烟已完成；前端已上线，真实设备、长媒体历史与很长会话侧栏仍需复测。
