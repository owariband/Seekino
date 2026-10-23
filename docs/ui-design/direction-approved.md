# AniVAG · 设计方向确认

- 确认日期：2026-09-14
- 正式方向：A · 青瓷札记
- 用户选择原话：「用A版本UI,然后gitignore一下我们的UI-design」
- Gate 状态：已通过；后续正式前端以 A 版为唯一视觉主干。

## 已展示的三个真实方向

- A · 青瓷札记
  - 原型：`design-demos/direction-a-celadon-journal.html`
  - 日间截图：`screenshots/direction-a-light.png`
  - 夜间截图：`screenshots/direction-a-dark.png`
  - 并行上传截图：`screenshots/direction-a-upload-light.png`
  - 任务中心截图：`screenshots/direction-a-tasks-light.png`
- B · 影像标本馆
  - 原型：`design-demos/direction-b-moving-image-archive.html`
  - 日间截图：`screenshots/direction-b-light.png`
  - 夜间截图：`screenshots/direction-b-dark.png`
- C · 时间书签
  - 原型：`design-demos/direction-c-time-bookmark.html`
  - 日间截图：`screenshots/direction-c-light.png`
  - 夜间截图：`screenshots/direction-c-dark.png`
- 汇总评审页：`review.html`
- 三方向汇总截图：`screenshots/directions-contact-sheet.png`

## A 版正式设计约束

- 使用左侧窄书脊导航、中央 Agent 对话与候选、右侧证据播放器的三段骨架。
- 检索本质是连续 Agent 对话；每个候选都说明「命中事实、证据来源、仍需核验」，不展示私有思维链或伪造置信度。
- 全局持续展示 2–3 个并行上传会话；每个视频拥有独立进度、状态与操作，上传进度和后续建库进度分开表达。
- 日间主题采用带纸张温度的青瓷色，夜间主题采用墨绿黑；青绿色只集中用于动作、状态与时间证据。
- 视觉继续保持干净、简洁、文艺、清新，不回退为通用后台卡片墙。

## 范围说明

本文件只确认设计方向并作为正式开发的 Gate；本次确认动作本身不代表已经开始编写前端。`docs/ui-design/` 是本地设计工作区，按用户要求整体加入 `.gitignore`。
