<p align="center">
  <img src="assets/readme/seekino-idle.svg" width="96" alt="Seekino" />
</p>

<h1 align="center">Seekino</h1>

<p align="center">
  <strong>视频里的 Ctrl+F</strong><br />
  用一句台词、一段描述或一张截图搜视频。点开结果，原片就从那一刻开始播放。
</p>

<p align="center">
  <a href="https://seekino.hypn0s.cloud">试用 Seekino Cloud</a> ·
  <a href="#本地部署">部署自己的素材库</a> ·
  <a href="https://seekino.hypn0s.cloud/docs">使用指南</a> ·
  <a href="README.en.md">English</a>
</p>

## 先试一次

> self-attention 是在哪一集哪一分钟讲的？

公开课 *Neural Networks: Zero to Hero* 共 8 集、14.6 小时。Seekino 可以跨集回答这类问题；而在观看页，你还能直接定位到某一秒。

**在视频里找一句话。** 按 `⌘F` / `Ctrl+F`（或点播放器右下角的放大镜），输入记得的台词或画面文字。结果带着本集位置出现，点一下，播放器就停在那一秒。

<p align="center">
  <img src="assets/readme/demo-find.gif" width="860" alt="观看页按 ⌘F 输入 most important，结果定位到第 7 集 62:00" />
</p>

**看到一半有问题，就地问。** 打开播放器旁的 Kino，问「刚才这段在讲什么？」它知道你看到哪里，回答里附一张能回看的片段卡。

<p align="center">
  <img src="assets/readme/demo-ask.gif" width="860" alt="播放器旁向 Kino 提问，回答附带第 7 集 61:00–61:56 的片段卡" />
</p>

你也可以在 Cloud 上问开头那句，让 Seekino 跨集定位；回答里同样带着可以点开的片段卡。公开课程和动画可以直接以游客身份体验。

## 主要功能

- **用台词、描述或截图找镜头。** 不知道是哪一集，也可以搜「下雨天有人在门口等」。如果手头只有一张截图，可以把静态图片发给助手。找到的片段带有视频名和起止时间，方便回看前后发生了什么。
- **挑自己需要的那段课。** 先看系列概览和单集大纲，再选一段细看。播放页按 `⌘F` 查台词；看不懂时在播放器旁问 Kino，不必截图或抄时间码。
- **建立自己的素材库。** 视频按系列上传，解析、建好索引后就可以反复搜。在任务页看进度，失败后可以重试；用量页单独列出已记录的 token 和能估算的 API 费用。
- **给剪辑助手取原片。** 接入 MCP 后，可以让 Claude Code 或 Codex 按描述选片、核对出处，把选中的一段取到剪辑机。有系列编辑权限时，还可以把动效代码、参考片段和修改意见一起存回系列，下次接着用。

更多界面实拍（2026-10-25）：[素材库](assets/readme/screenshots/library.jpg) · [检索结果](assets/readme/screenshots/search-results.jpg) · [观看问答](assets/readme/screenshots/watch-chat.jpg)

## 本地部署

想搜自己的视频，就在电脑上部署 Seekino。默认用 Docker 运行，宿主机无需安装 Python、Node.js 或下载模型权重。

1. 安装并启动 Docker Desktop，或安装 Docker Engine 和新版 Compose 插件。Windows 请选择 WSL 2 和 Linux 容器。
2. 首次安装时获取代码，复制环境文件。如果已有 `.env.docker`，请编辑原文件。

   ```bash
   git clone --depth 1 --single-branch --branch main https://github.com/owariband/Seekino.git
   cd Seekino
   cp deploy/docker/env.example .env.docker
   ```

3. 在 `.env.docker` 填写火山方舟的 `ARK_API_KEY` 和 DeepSeek 的 `DEEPSEEK_API_KEY`，确认账号已开通所选模型。如果也要使用 Jev 判断问题是否需要检索，再填 `OPENROUTER_JEV_API_KEY`。视频保存在本机；使用远程模型时，相关音视频、帧或文本会发给供应商，费用由你的模型账号承担。
4. 在仓库根目录启动服务：

   ```bash
   docker compose --env-file .env.docker up --build -d --wait
   ```

打开[本地素材库](http://127.0.0.1:5173)，创建系列，传一段你熟悉的短视频。等解析和索引完成，搜一句你记得的台词，点开结果听听是不是那一句。

日常停止用 `docker compose --env-file .env.docker stop`；`down -v` 会删除数据卷。切换模型、修改端口、备份、旧库升级和 Windows 验证范围见[本地配置指南](deploy/local-setup.md)。服务启动后可查看 [API 文档](http://127.0.0.1:5173/api/docs)和[运行状态](http://127.0.0.1:5173/healthz)。

<a name="宿主机开发可选"></a>

<details>
<summary>可选：在宿主机运行 API 和网页</summary>

准备 Python 3.12、uv、Node.js 24+、npm 和 ffmpeg / ffprobe。首次运行时复制 `.env.example` 为 `.env.local`，填好模型密钥；已有文件请保留。在仓库根目录运行：

```bash
uv sync --locked
npm --prefix web ci
docker compose --env-file .env.local up -d --wait mysql elasticsearch
./start.sh
```

`./start.sh` 使用已检出的 `main` 工作区；使用 `dev` 时运行 `./start.sh --branch dev`。状态与停止命令、全原生安装和旧数据恢复见[部署说明](deploy/README.md)。

</details>

## 接入剪辑助手

要让 Claude Code 或 Codex 使用公开素材，按 [Cloud MCP 接入指南](https://seekino.hypn0s.cloud/docs/agents)在终端领取游客只读凭据，并添加对应客户端。要使用自己的素材，先完成本地部署，再为所用客户端添加本机地址（二选一）：

```sh
# Claude Code
claude mcp add --scope user --transport http seekino_local http://127.0.0.1:9621/mcp

# Codex
codex mcp add seekino_local --url http://127.0.0.1:9621/mcp
```

连接后可以直接交代：「在素材库里找爱音咬牙切齿的镜头，核对原片，把那一段存到 `clips/`。」Cloud 和本机可以同时连接；Cloud 游客只能读取已发布的公开素材，每次最多取 60 秒。

## 整体架构

视频入库时，Seekino 把画面和对白整理成带时间的事件。搜索时用这些事件定位，再按来源视频和时间范围取回片段。

![Seekino 整体架构：视频解析与索引、按问题检索、回到原片核对](assets/readme/figures/fig-overview.png)

<!-- 技术博客发布后，在这里补充链接。 -->

## 许可与致谢

当前仓库未声明 Seekino 整体的许可证。随项目分发的第三方组件各有许可：

- [video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) 提供镜头配方和制作方法，代码和文档使用 [Apache-2.0](server/skills/video-shotcraft/LICENSE)。
- [motion-showreel](https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel) 提供代码动画与分层合成脚本，使用 [MIT](server/skills/motion-showreel/LICENSE)。
- 界面字体 Inter 使用 [SIL Open Font License 1.1](web/public/assets/fonts/Inter-LICENSE.txt)。

遇到问题或有改进建议，可以到 [GitHub Issues](https://github.com/owariband/Seekino/issues) 反馈。
