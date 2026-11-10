import type { Locale } from "../../shared/messages";

import { GITHUB_URL } from "./links";

export { GITHUB_URL };
const SETUP_GUIDE = `${GITHUB_URL}/blob/main/deploy/local-setup.md`;
const README_EN = `${GITHUB_URL}/blob/main/README.en.md`;

// Community links arrive later; [xxx] marks every spot still waiting for one.
const COMMUNITY_PLACEHOLDER = "[xxx]";

export interface DocsContent {
  kicker: string;
  title: string;
  lead: string;
  tryCloud: string;
  github: string;
  toc: string;
  sections: {
    versions: { title: string; intro: string; columns: [string, string, string]; rows: [string, string, string][]; note: string };
    usage: { title: string; intro: string; items: { title: string; body: string; where: string }[] };
    deploy: {
      title: string;
      intro: string;
      steps: { title: string; body: string; code?: string }[];
      mcpTitle: string;
      mcpBody: string;
      mcpCode: string;
      more: string;
      links: { label: string; href: string }[];
    };
    roadmap: { title: string; intro: string; status: string; items: { title: string; body: string }[] };
    community: { title: string; intro: string; channels: { label: string; value: string; href?: string }[]; closing: string };
  };
  copy: string;
  copied: string;
}

const zh: DocsContent = {
  kicker: "Seekino 文档",
  title: "视频只解析一次，之后像读文字一样找",
  lead: "Seekino 把视频变成可以检索、回看和追问的素材库。每个结果都带着原片和精确的时间范围，点一下就回到那一刻。",
  tryCloud: "在 Cloud 上试一试",
  github: "GitHub 上给个 Star",
  toc: "本页内容",
  sections: {
    versions: {
      title: "Cloud 与本地版",
      intro: "你现在打开的是 Seekino Cloud：不用安装，直接体验我们已经解析好的样例。想用在自己的视频上，就在自己电脑上部署本地版。",
      columns: ["", "Cloud", "本地版"],
      rows: [
        ["开始方式", "打开浏览器即可", "用 Docker 在自己电脑上启动"],
        ["能看什么", "平台已解析发布的公开样例，如动画和课程", "你自己上传的任何视频"],
        ["上传与解析", "暂未开放", "整片转写、分块并行解析、建立索引，进度可查、失败可重试"],
        ["检索、观看、问答", "支持", "支持"],
        ["账号", "游客即可体验，登录后保存自己的对话", "本机单用户，无需注册"],
        ["模型与费用", "平台提供", "自选 Ark、DeepSeek、Kimi 等 API 或 Codex，费用由你的账户承担，每次调用都记录 token"],
        ["MCP（让 Claude Code / Codex 取素材）", "暂不开放", "支持"],
        ["数据在哪", "平台服务器", "视频和数据库都留在你的电脑上"],
      ],
      note: "Cloud 的服务器规格有限，所以上传、个人资料库和 MCP 目前只在本地版提供。",
    },
    usage: {
      title: "怎么用 Seekino",
      intro: "这几种用法，在 Cloud 的样例上就能先试。",
      items: [
        {
          title: "找回某个画面",
          body: "按 ⌘K / Ctrl K，用一句模糊印象描述画面，比如“在雨里奔跑的那一幕”，也可以上传一张截图。Seekino 返回命中的片段和精确时间范围，点开就从那一刻播放。",
          where: "Cloud · 本地版",
        },
        {
          title: "边看边问",
          body: "观看时打开右侧助手，直接问“刚才这段在讲什么”。助手知道你看到了哪里，回答会附上可以跳转核对的片段。",
          where: "Cloud · 本地版",
        },
        {
          title: "先看大纲，再挑重点",
          body: "解析完成后会自动生成视频大纲和系列概览。116 分钟的课程，先看结构，再只看你需要的那几段。",
          where: "Cloud · 本地版",
        },
        {
          title: "给剪辑找素材",
          body: "把自己的素材库接入 Claude Code 或 Codex。你只说模糊印象，Agent 通过 MCP 找到片段、核对来源、下载原片，你最后审核即可。",
          where: "本地版",
        },
      ],
    },
    deploy: {
      title: "部署本地版",
      intro: "默认部署使用 Docker，模型只调用 API，不需要显卡、本地模型权重，也不需要在电脑上装 Python 或 Node.js。",
      steps: [
        { title: "安装 Docker", body: "Windows 和 macOS 安装 Docker Desktop，Windows 选择 WSL 2 / Linux containers；Linux 安装 Docker Engine 与 Compose 插件。" },
        { title: "获取代码", body: "克隆仓库并进入目录。", code: `git clone ${GITHUB_URL}.git\ncd Seekino` },
        { title: "填写模型凭据", body: "复制环境模板，在 .env.docker 中填入 ARK_API_KEY 和 DEEPSEEK_API_KEY。", code: "cp deploy/docker/env.example .env.docker" },
        { title: "一条命令启动", body: "首次构建需要联网下载镜像和依赖。", code: "docker compose --env-file .env.docker up --build -d --wait" },
        { title: "上传第一个视频", body: "打开 http://127.0.0.1:5173，新建系列并上传一个短视频。解析和索引完成后，就可以检索和问答。" },
      ],
      mcpTitle: "接入 Claude Code / Codex",
      mcpBody: "MCP 只接受本机直连，需要在宿主机上运行 API（按 README 的“宿主机开发”准备环境后执行 ./start.sh）。然后添加 Seekino：",
      mcpCode: "claude mcp add --transport http seekino http://127.0.0.1:9621/mcp\n# 或\ncodex mcp add seekino --url http://127.0.0.1:9621/mcp",
      more: "更多配置：",
      links: [
        { label: "本地配置指南（切换 Kimi / Codex、持久化、Windows）", href: SETUP_GUIDE },
        { label: "项目 README", href: GITHUB_URL },
        { label: "API 文档（本机 /api/docs）", href: "http://127.0.0.1:5173/api/docs" },
      ],
    },
    roadmap: {
      title: "未来计划",
      intro: "以下是正在考虑的方向，还没有排期。你最想先要哪一个，欢迎来交流群告诉我们。",
      status: "计划中",
      items: [
        { title: "Cloud 个人资料库", body: "登录后在 Cloud 上传、解析自己的视频，不用自己部署。" },
        { title: "Cloud 接入 MCP", body: "用访问令牌把 Cloud 上的素材库接入 Claude Code / Codex。" },
        { title: "剪辑参考复用", body: "保存解析过的动效和运镜参考，让 Agent 每次剪辑都能复用之前的经验。" },
        { title: "按类型自动解析", body: "自动识别课程、影视剧、剪辑素材等视频类型，按类型选择解析方式、控制成本。" },
      ],
    },
    community: {
      title: "加入交流",
      intro: "Seekino 还在早期，每一条使用反馈都会直接影响下一步做什么。用得顺手、遇到问题，或者只是想聊聊视频和 AI，都欢迎来。",
      channels: [
        { label: "交流群", value: COMMUNITY_PLACEHOLDER },
        { label: "问题与建议", value: "GitHub Issues", href: `${GITHUB_URL}/issues` },
      ],
      closing: "如果 Seekino 对你有用，在 GitHub 上点个 Star 就是对我们最大的支持。",
    },
  },
  copy: "复制",
  copied: "已复制",
};

const en: DocsContent = {
  kicker: "Seekino docs",
  title: "Parse a video once, then search it like text",
  lead: "Seekino turns videos into a library you can search, replay and ask about. Every result links to the source video and its exact time range, so one click takes you back to that moment.",
  tryCloud: "Try it on Cloud",
  github: "Star us on GitHub",
  toc: "On this page",
  sections: {
    versions: {
      title: "Cloud vs. Local",
      intro: "You are on Seekino Cloud: nothing to install, and you can explore the samples we have already parsed. To use Seekino on your own videos, deploy the Local version on your computer.",
      columns: ["", "Cloud", "Local"],
      rows: [
        ["Getting started", "Open it in a browser", "Start it on your computer with Docker"],
        ["What you can watch", "Published public samples such as anime and courses", "Any video you upload"],
        ["Upload and parsing", "Not available yet", "Full transcription, parallel chunk parsing and indexing, with progress tracking and retries"],
        ["Search, watch, ask", "Yes", "Yes"],
        ["Account", "Try it as a guest; sign in to keep your conversations", "Single local user, no sign-up"],
        ["Models and cost", "Provided by the platform", "Your choice of Ark, DeepSeek, Kimi and other APIs, or Codex; billed to your own account, with tokens recorded for every call"],
        ["MCP (footage for Claude Code / Codex)", "Not available yet", "Yes"],
        ["Where your data lives", "Platform servers", "Videos and databases stay on your computer"],
      ],
      note: "The Cloud server has limited capacity, so uploads, personal libraries and MCP are Local-only for now.",
    },
    usage: {
      title: "Ways to use Seekino",
      intro: "You can try these on the Cloud samples first.",
      items: [
        {
          title: "Find a moment",
          body: "Press ⌘K / Ctrl K and describe a scene from memory, such as “the one where they run in the rain”, or upload a screenshot. Seekino returns matching clips with exact time ranges, and playback starts at that moment.",
          where: "Cloud · Local",
        },
        {
          title: "Ask while you watch",
          body: "Open the assistant beside the player and ask “what was that part about?”. It knows where you are in the video, and its answers link to clips you can jump to and check.",
          where: "Cloud · Local",
        },
        {
          title: "Read the outline first",
          body: "After parsing, Seekino writes a video outline and a series overview. For a 116-minute course, see the structure first, then watch only the parts you need.",
          where: "Cloud · Local",
        },
        {
          title: "Source footage for editing",
          body: "Connect your own library to Claude Code or Codex. Describe what you remember; the agent finds clips over MCP, checks their sources and downloads the originals, and you review the result.",
          where: "Local",
        },
      ],
    },
    deploy: {
      title: "Deploy the Local version",
      intro: "The default setup runs in Docker and calls models over APIs. You don't need a GPU, local model weights, or Python or Node.js on your computer.",
      steps: [
        { title: "Install Docker", body: "On Windows or macOS, install Docker Desktop (on Windows, choose WSL 2 / Linux containers). On Linux, install Docker Engine with the Compose plugin." },
        { title: "Get the code", body: "Clone the repository and enter it.", code: `git clone ${GITHUB_URL}.git\ncd Seekino` },
        { title: "Add model keys", body: "Copy the environment template, then set ARK_API_KEY and DEEPSEEK_API_KEY in .env.docker.", code: "cp deploy/docker/env.example .env.docker" },
        { title: "Start it with one command", body: "The first build needs a network connection to download images and dependencies.", code: "docker compose --env-file .env.docker up --build -d --wait" },
        { title: "Upload your first video", body: "Open http://127.0.0.1:5173, create a series and upload a short video. Once parsing and indexing finish, you can search and ask." },
      ],
      mcpTitle: "Connect Claude Code / Codex",
      mcpBody: "MCP accepts local connections only, so the API needs to run directly on your machine: set up host development as described in the README, then run ./start.sh. Then add Seekino:",
      mcpCode: "claude mcp add --transport http seekino http://127.0.0.1:9621/mcp\n# or\ncodex mcp add seekino --url http://127.0.0.1:9621/mcp",
      more: "More setup:",
      links: [
        { label: "Local setup guide (Kimi / Codex, persistence, Windows; in Chinese)", href: SETUP_GUIDE },
        { label: "Project README", href: README_EN },
        { label: "API docs (/api/docs on your machine)", href: "http://127.0.0.1:5173/api/docs" },
      ],
    },
    roadmap: {
      title: "What's next",
      intro: "These are directions we're considering, with no dates yet. Tell us in the community group which one you want first.",
      status: "Planned",
      items: [
        { title: "Personal libraries on Cloud", body: "Sign in to upload and parse your own videos on Cloud, with nothing to deploy." },
        { title: "MCP on Cloud", body: "Connect your Cloud library to Claude Code / Codex with an access token." },
        { title: "Reusable editing references", body: "Keep the motion and camera references Seekino has parsed, so an agent can reuse past experience in every edit." },
        { title: "Parsing by video type", body: "Detect courses, films and series, or editing footage, and pick the parsing approach and cost for each type." },
      ],
    },
    community: {
      title: "Join the community",
      intro: "Seekino is early, and every piece of feedback shapes what we build next. Whether it works well for you, something breaks, or you just want to talk about video and AI, come say hi.",
      channels: [
        { label: "Community group", value: COMMUNITY_PLACEHOLDER },
        { label: "Bugs and ideas", value: "GitHub Issues", href: `${GITHUB_URL}/issues` },
      ],
      closing: "If Seekino is useful to you, a Star on GitHub is the best way to support us.",
    },
  },
  copy: "Copy",
  copied: "Copied",
};

export function docsContent(locale: Locale): DocsContent {
  return locale === "en" ? en : zh;
}
