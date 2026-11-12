# 给你的 Agent 一个记得所有画面的素材库

在我们的剪辑测试里，Claude Code 通过 Seekino 在第 4 集里找出 Anon 咬牙切齿的那 1.3 秒，核对来源，再把原片片段取回自己的工作区。你只说要什么，它去找。

## 接入

1. **获取 Cloud 只读凭据**

   在 Cloud 站点复制命令，到剪辑终端运行。游客可读取已发布的平台系列，凭据到期后重跑这一步。

   ```sh
   SEEKINO_CLOUD='https://your-seekino-cloud.example'
   SEEKINO_MCP_TOKEN=$(curl -fsS -X POST "$SEEKINO_CLOUD/api/auth/mcp-token" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])') && export SEEKINO_MCP_TOKEN
   ```

2. **Claude Code 接入 Cloud**

   使用 Claude 时运行，然后从这个终端打开 Claude Code。

   ```sh
   claude mcp add --transport http seekino_cloud "$SEEKINO_CLOUD/mcp" --header 'Authorization: Bearer ${SEEKINO_MCP_TOKEN}'
   ```

3. **Codex 接入 Cloud**

   使用 Codex 时运行，然后从这个终端打开 Codex。

   ```sh
   codex mcp add seekino_cloud --url "$SEEKINO_CLOUD/mcp" --bearer-token-env-var SEEKINO_MCP_TOKEN
   ```

4. **同时使用本机素材**

   在宿主机启动本地 API，再为正在使用的客户端添加第二个服务。

   ```sh
   claude mcp add --transport http seekino_local http://127.0.0.1:9621/mcp
   # 或
   codex mcp add seekino_local --url http://127.0.0.1:9621/mcp
   ```

5. **像交代同事一样说**

   例如：“在 Seekino 里找出 Anon 咬牙切齿的镜头，核对来源后把片段存到 clips/。”

## 选片、风格和 series 共享模板工具

- `list_series` — 看看素材库里有哪些系列。
- `retrieve` — 用一句描述检索片段，返回原片与时间范围。
- `browse_events` — 按时间顺序浏览一集里发生了什么。
- `read_evidence` — 核对原片上下文。给出起止毫秒后，可取得该区间的 MP4 下载地址。

- `find_edit_templates` 检索带原片区间的剪辑技法和可见 series 的共享模板。用 `get_style_card` 查看整片风格和例证。
- `get_edit_template` 读取指定或默认版本的代码、来源与反馈。`list_edit_template_versions` 列出版本和派生关系。
- `save_edit_template_version` 指定 `series_id` 保存新版本，`append_edit_template_note` 追加反馈或本机渲染检查，`set_default_edit_template_version` 明确选择默认版本。

## 使用剪辑 Skill 与 series 共享模板

模板、版本与反馈归属 series。任何有权查看该 series 的人都能读取，不受作者或游客身份影响。换一枚有效游客凭据仍能找回同一公开 series 的模板。创建模板时传入 series_id，已有版本按模板 ID 读取。

写入沿用 series 的编辑权限。Cloud 编辑者可登录后从账号菜单选择“复制剪辑写入凭据”，或通过当前会话 Cookie、CSRF 请求头和 `POST /api/auth/mcp-token?scope=personal` 领取写入凭据。本机沿用本地权限。

在 MCP 上用 `resources/read` 读取 `skill://seekino-editing-materials/SKILL.md`，也可以用 `skills/list` 和 `skills/get` 发现完整文件清单。要在剪辑机上同时给 Claude Code 和 Codex 安装指引与本机分层脚本，可以运行下面的命令。

```sh
npx skills add https://github.com/owariband/Seekino/tree/main/server/skills/seekino-editing-materials -a claude-code -a codex -g -y
```

## 镜头库与运动参考

下面两份 MCP 正文由 Seekino 按固定上游版本改写为中文，保留原有操作规则。资源中提供原文版本、原文与改写正文的 SHA-256，以及修改说明；安装命令取得对应版本的上游完整包。

MCP 提供 `skill://motion-showreel/SKILL.md`，用于代码驱动的图形、文字、界面动画和支持的人物分层合成。完整安装包含风格参考、starter、时间线和逐帧制作脚本。它原生要求先在工作区预览、确认后导出，除非用户明确免除预览。

```sh
npx skills add https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel -a claude-code -a codex -g -y
```

MCP 还提供 `skill://video-shotcraft/SKILL.md` 及原 Apache-2.0 许可证。在剪辑机上完整安装后，Agent 可以读取其镜头卡、demo、模板和工作台，供剪辑时参考。

```sh
npx skills add https://github.com/Vincentwei1021/video-shotcraft/tree/6700db2ba80572ebc777b99d72049679fed9840c -a claude-code -a codex -g -y
```

Seekino 剪辑指引提供镜头运动的参考和建议。Agent 根据镜头意图、节奏与素材，在剪辑工程里自行编写速度、位移和形变代码。例如，钟形速度及其积分形成的 S 形位移，可以作为慢起、加速、落定的参考；具体时长、快段、对称性与落点由镜头代码决定，也可以复用或调整上游辅助函数。

拉伸、拖影等也可按镜头需要编写，用瞬时速度和方向控制强度，再回看实际画面调整。制作与渲染在本机完成。喜欢这套镜头库，欢迎给 [video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) 点一个 Star。

motion-showreel 来自 [ConardLi/garden-skills](https://github.com/ConardLi/garden-skills)，使用 MIT，MCP 分发中保留原版权、许可证和固定版本。它提供动画与合成参考，常规原片选取与裁剪沿用 Seekino 剪辑指引。觉得有帮助，欢迎给上游项目 Star。

## 接下来

- [接下来要做什么](./roadmap.md)
