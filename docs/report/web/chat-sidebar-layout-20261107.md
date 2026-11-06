# 独立对话页布局验收（2026-11-07）

需求：[TRD](../../design/03-product-reassessment/chat-sidebar-layout-trd.md)；[PR #67](https://github.com/owariband/Seekino/pull/67)；状态：[工作台](../../dev/workbench.html) `CHAT-LAYOUT-01`。

## 本机视觉与真实数据检查

预览为本分支的 Vite 5190，后端沿用本机 19641。使用新的 Chrome 上下文，读取真实历史、切换一条已有回答、返回新对话，检查亮暗主题，再调整到 390×844 打开/关闭历史抽屉。没有发送真实模型消息或删除历史。

| 检查 | 实测结果 |
| --- | --- |
| 1440×1000 左栏 | x=0，y=76，width=248，height=924 px |
| 居中草稿输入 | x=464，y=452，width=760，height=106 px |
| 历史读取 | 139 条真实记录，窄列表独立滚动；旧空会话仍保留 |
| 正式对话 | 阅读旧回答，底部输入约 58px 高，与正文对齐 |
| 真实请求计数 | 浏览页面、切换已有会话、返回草稿全过程 `conversations/create = 0` |
| 手机关闭 | Escape 后抽屉关闭，焦点回到历史入口；亮主题截图无水平溢出 |

截图保存在本机 `outputs/verification/chat-sidebar-layout/20261107/`；其中真实历史截图不提交公开仓库。

## 自动化检查

- `npm --prefix web run typecheck`：通过。
- `npm --prefix web test`：585 passed，另 8 个代理测试因沙箱禁止监听端口（EPERM）未执行。授予本机网络权限后单独执行 `src/test/remote-proxy.test.ts`，8 passed；合计 593 个测试通过。
- `npm --prefix web run build`：通过。
- `chat-layout.spec.ts` 两个隔离浏览器用例：1440/1920px 宽屏输入最大 760px、侧栏折叠保留草稿、390px 无水平溢出、手机焦点限制、Escape/遮罩关闭及焦点返回，均通过。
- `chat-streaming.spec.ts`：首发切换布局后 textarea 仍是原节点、键盘焦点保留；流式正文在模型完成前增长；初始 create=0、首发 create=1、连续新建两次仍为 1，均通过。

浏览器发送测试使用 API mock，不代表真实模型、真实流式耗时或回答质量。真实历史查看与模拟发送分开记录。执行时内嵌浏览器不可用，改用已有 Chrome 的 Playwright channel；没有安装新浏览器。

## 集成边界

本分支基于已合入 #66 的 dev，保留无操作不创建会话。观看页 embedded 使用原历史弹层与输入交互；Kino 工作区的 UI 修改保持独立，联合验证结果在 TRD 中追加。未发布远端、未清理旧记录、未运行真实模型。
