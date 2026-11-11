import type { Locale } from "../../shared/messages";

import { GITHUB_URL } from "./links";

export { GITHUB_URL };
const SETUP_GUIDE = `${GITHUB_URL}/blob/main/deploy/local-setup.md`;
const README_EN = `${GITHUB_URL}/blob/main/README.en.md`;

const pageOrigin = typeof window === "undefined" ? "" : window.location.origin;
const cloudOrigin = /^https:\/\/[a-z0-9.-]+(?::[0-9]+)?$/i.test(pageOrigin)
  ? pageOrigin : "https://your-seekino-cloud.example";
const cloudToken = [
  `SEEKINO_CLOUD='${cloudOrigin}'`,
  'SEEKINO_MCP_TOKEN=$(curl -fsS -X POST "$SEEKINO_CLOUD/api/auth/mcp-token" | python3 -c \'import json,sys; print(json.load(sys.stdin)["accessToken"])\') && export SEEKINO_MCP_TOKEN',
].join("\n");
const claudeCloud = `claude mcp add --transport http seekino_cloud "$SEEKINO_CLOUD/mcp" --header 'Authorization: Bearer \${SEEKINO_MCP_TOKEN}'`;
const codexCloud = 'codex mcp add seekino_cloud --url "$SEEKINO_CLOUD/mcp" --bearer-token-env-var SEEKINO_MCP_TOKEN';

// Community links arrive later; [xxx] marks every spot still waiting for one.
const COMMUNITY_PLACEHOLDER = "[xxx]";

export const DOC_PAGES = ["start", "versions", "self-host", "agents", "roadmap"] as const;
export type DocPageId = (typeof DOC_PAGES)[number];

/** Each page is a short stack of blocks, written lead-first: the title and lead carry the page's one idea. */
export type DocBlock =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string }
  | { kind: "question"; text: string }
  | { kind: "steps"; items: { title: string; body: string; code?: string }[] }
  | { kind: "examples"; label: string; items: { text: string; note: string }[] }
  | { kind: "shot"; label: string; asked: string; query: string; src: string; alt: string; note: string }
  | { kind: "compare"; columns: [string, string, string]; rows: [string, string, string][] }
  | { kind: "tools"; items: { name: string; body: string }[] }
  | { kind: "roadmap"; status: string; items: { title: string; body: string }[] }
  | { kind: "channels"; items: { label: string; value: string; href?: string }[] }
  | { kind: "star"; text: string }
  | { kind: "links"; label: string; items: { label: string; href: string }[] }
  | { kind: "next"; items: { label: string; page: DocPageId }[] };

export interface DocPage { nav: string; title: string; lead: string; blocks: DocBlock[] }

export interface DocsContent {
  kicker: string;
  toc: string;
  nextLabel: string;
  tryCloud: string;
  github: string;
  copy: string;
  copied: string;
  pages: Record<DocPageId, DocPage>;
}

const zh: DocsContent = {
  kicker: "Seekino 文档",
  toc: "文档",
  nextLabel: "接下来",
  tryCloud: "现在试一句",
  github: "GitHub 上给个 Star",
  copy: "复制",
  copied: "已复制",
  pages: {
    start: {
      nav: "快速开始",
      title: "41 小时的课，你要的只是其中一段",
      lead: "Seekino 把视频变成可以搜索的素材库。说出你记得的画面或想弄懂的概念，它把你直接送到原片里的那一秒。",
      blocks: [
        { kind: "question", text: "想想上一次，你在一段长视频里找某个画面，拖了多久进度条？" },
        { kind: "h", text: "三步试一次" },
        { kind: "steps", items: [
          { title: "说出你要找的", body: "按 ⌘K（Windows 上是 Ctrl+K），像问朋友一样描述。不用记得原话，也不用知道在第几集。" },
          { title: "点开时间，直接看", body: "每条结果都带着原片和精确到秒的时间范围。点一下，视频从那一秒开始播放，不用再前后拖。" },
          { title: "看不懂，就接着问", body: "播放时打开右侧的助手，问“刚才这段在讲什么”。它知道你看到了哪里，回答里的每个片段都能点开核对。" },
        ] },
        { kind: "shot", label: "Cloud 上的真实截图 · 2026-11-10", asked: "问", query: "self-attention 是在哪一集哪一分钟讲的", src: "/docs/recall-self-attention.png", alt: "Seekino 的回答：一张 EP 07 · 71:00–71:40 的片段卡片，下面列出第 7 集里讲 self-attention 的六个时间点", note: "8 集、14.6 小时的课，回答落在第 7 集的具体时间点，附带可以点开的原片片段。在 Cloud 上问同一句，就能自己核对。" },
        { kind: "examples", label: "也可以这样问", items: [
          { text: "反向传播的链式法则是在哪一集讲的", note: "课程" },
          { text: "Karpathy 说 tokenizer 是很多问题根源的那一段", note: "课程" },
          { text: "下雨天有人在门口等待的画面", note: "动画" },
        ] },
        { kind: "h", text: "为什么不直接倍速看？" },
        { kind: "p", text: "一节 116 分钟的课，开 3 倍速也要将近 40 分钟，而且你得一直盯着，生怕错过要找的那一段。Seekino 只需把视频解析一次，之后每次查找都像在文档里按 Ctrl+F，直接跳到那一段。" },
        { kind: "next", items: [{ label: "Cloud 和本地版有什么不同", page: "versions" }, { label: "在自己电脑上部署", page: "self-host" }] },
      ],
    },
    versions: {
      nav: "两个版本",
      title: "Cloud 用来试，本地版用来装你自己的视频",
      lead: "你现在打开的就是 Cloud：不用安装，直接在我们解析好的课程和动画里搜。想搜自己的视频，就在电脑上装本地版。",
      blocks: [
        { kind: "compare", columns: ["", "Cloud", "本地版"], rows: [
          ["开始方式", "打开浏览器", "Docker 一条命令启动"],
          ["能搜什么", "平台发布的公开样例：课程和动画", "你自己上传的任何视频"],
          ["上传与解析", "暂未开放", "支持；进度可查，失败可重试"],
          ["搜索、观看、追问", "支持", "支持"],
          ["账号", "游客即可使用；登录后保存对话", "本机使用，无需注册"],
          ["模型与费用", "平台提供", "自选 Ark、DeepSeek、Kimi 等，费用走你自己的账户，每次调用都记录 token"],
          ["让 Claude Code / Codex 取素材", "平台公开系列，可下载最多 60 秒的片段", "本机素材库"],
          ["数据在哪", "平台服务器", "视频和数据库都留在你的电脑上"],
        ] },
        { kind: "p", text: "上传和个人资料库目前在本地版提供。" },
        { kind: "next", items: [{ label: "部署本地版", page: "self-host" }] },
      ],
    },
    "self-host": {
      nav: "部署本地版",
      title: "一条命令，在自己电脑上跑起完整的 Seekino",
      lead: "默认部署用 Docker，模型只调用 API：不需要显卡，不需要下载模型，也不需要在电脑上装 Python 或 Node.js。你只需要 Docker 和两把模型密钥。",
      blocks: [
        { kind: "steps", items: [
          { title: "装好 Docker", body: "Windows 和 macOS 装 Docker Desktop（Windows 选 WSL 2 / Linux containers）；Linux 装 Docker Engine 与 Compose 插件。" },
          { title: "拿到代码", body: "克隆仓库并进入目录。", code: `git clone ${GITHUB_URL}.git\ncd Seekino` },
          { title: "填两把密钥", body: "复制环境模板，在 .env.docker 里填上 ARK_API_KEY 和 DEEPSEEK_API_KEY。", code: "cp deploy/docker/env.example .env.docker" },
          { title: "启动", body: "第一次构建需要联网下载镜像和依赖。", code: "docker compose --env-file .env.docker up --build -d --wait" },
          { title: "传一个短视频试试", body: "打开 http://127.0.0.1:5173，新建系列，上传一段几分钟的视频。解析完成后，回到“快速开始”的三步，用你自己的视频再试一次。" },
        ] },
        { kind: "links", label: "更多配置", items: [
          { label: "本地配置指南：切换 Kimi / Codex、数据持久化、Windows", href: SETUP_GUIDE },
          { label: "项目 README", href: GITHUB_URL },
          { label: "API 文档（本机 /api/docs）", href: "http://127.0.0.1:5173/api/docs" },
        ] },
        { kind: "next", items: [{ label: "接入 Claude Code / Codex", page: "agents" }] },
      ],
    },
    agents: {
      nav: "接入 Agent",
      title: "给你的 Agent 一个记得所有画面的素材库",
      lead: "在我们的剪辑测试里，Claude Code 通过 Seekino 在第 4 集里找出 Anon 咬牙切齿的那 1.3 秒，核对来源，再把原片片段取回自己的工作区。你只说要什么，它去找。",
      blocks: [
        { kind: "h", text: "接入" },
        { kind: "steps", items: [
          { title: "获取 Cloud 只读凭据", body: "在 Cloud 站点复制命令，到剪辑终端运行。游客可读取已发布的平台系列，凭据到期后重跑这一步。", code: cloudToken },
          { title: "Claude Code 接入 Cloud", body: "使用 Claude 时运行，然后从这个终端打开 Claude Code。", code: claudeCloud },
          { title: "Codex 接入 Cloud", body: "使用 Codex 时运行，然后从这个终端打开 Codex。", code: codexCloud },
          { title: "同时使用本机素材", body: "在宿主机启动本地 API，再为正在使用的客户端添加第二个服务。", code: "claude mcp add --transport http seekino_local http://127.0.0.1:9621/mcp\n# 或\ncodex mcp add seekino_local --url http://127.0.0.1:9621/mcp" },
          { title: "像交代同事一样说", body: "例如：“在 Seekino 里找出 Anon 咬牙切齿的镜头，核对来源后把片段存到 clips/。”" },
        ] },
        { kind: "h", text: "Agent 能用的四个工具" },
        { kind: "tools", items: [
          { name: "list_series", body: "看看素材库里有哪些系列。" },
          { name: "retrieve", body: "用一句描述检索片段，返回原片与时间范围。" },
          { name: "browse_events", body: "按时间顺序浏览一集里发生了什么。" },
          { name: "read_evidence", body: "核对原片上下文。给出起止毫秒后，可取得该区间的 MP4 下载地址。" },
        ] },
        { kind: "next", items: [{ label: "接下来要做什么", page: "roadmap" }] },
      ],
    },
    roadmap: {
      nav: "未来计划",
      title: "说出一个名字，列出 TA 出场的每一秒",
      lead: "这是我们接下来想做的：在整部作品里，Seekino 不只找到一个画面，而是列出某个角色出现过的每一段和对应时间，一段不漏。",
      blocks: [
        { kind: "roadmap", status: "计划中", items: [
          { title: "人物出场全量列举", body: "说出一个角色，列出 TA 在整部作品里出现的每一段。" },
          { title: "Cloud 个人资料库", body: "登录后直接在 Cloud 上传、解析自己的视频，不用自己部署。" },
          { title: "剪辑参考复用", body: "记住解析过的运镜和动效，让 Agent 下次剪辑直接用上。" },
        ] },
        { kind: "p", text: "这些都还没有排期。你最想先要哪一个？来交流群告诉我们，你的意见可能直接影响我们先做哪一个。" },
        { kind: "h", text: "加入交流" },
        { kind: "channels", items: [
          { label: "交流群", value: COMMUNITY_PLACEHOLDER },
          { label: "问题与建议", value: "GitHub Issues", href: `${GITHUB_URL}/issues` },
        ] },
        { kind: "star", text: "如果 Seekino 帮你省下了一次拖进度条，在 GitHub 上点个 Star 吧。" },
      ],
    },
  },
};

const en: DocsContent = {
  kicker: "Seekino docs",
  toc: "Docs",
  nextLabel: "Next",
  tryCloud: "Try a search now",
  github: "Star us on GitHub",
  copy: "Copy",
  copied: "Copied",
  pages: {
    start: {
      nav: "Quick start",
      title: "41 hours of lectures. You only need one part.",
      lead: "Seekino turns videos into a library you can search. Describe a scene you remember or a concept you want to understand, and it takes you straight to that second in the original video.",
      blocks: [
        { kind: "question", text: "Think about the last time you looked for one scene in a long video. How long did you drag the progress bar?" },
        { kind: "h", text: "Try it in three steps" },
        { kind: "steps", items: [
          { title: "Say what you're looking for", body: "Press ⌘K (Ctrl+K on Windows) and describe it the way you'd ask a friend. You don't need the exact words or the episode number." },
          { title: "Click the time and watch", body: "Every result comes with its source video and a time range down to the second. One click and playback starts right there, with no more scrubbing." },
          { title: "Lost? Just ask", body: "While watching, open the assistant beside the player and ask \"what was that part about?\". It knows where you are, and every clip in its answer links back to the source." },
        ] },
        { kind: "shot", label: "Real screenshot from Cloud · 2026-11-10", asked: "Asked", query: "self-attention 是在哪一集哪一分钟讲的 (which episode and minute covers self-attention?)", src: "/docs/recall-self-attention.png", alt: "Seekino's answer: a clip card for EP 07 · 71:00–71:40, followed by six time points in episode 7 where self-attention is explained", note: "Across 8 episodes and 14.6 hours, the answer lands on exact time points in episode 7, with source clips you can open. You can ask the same question on Cloud right now." },
        { kind: "examples", label: "Or ask things like", items: [
          { text: "which episode covers the chain rule in backprop", note: "Lectures" },
          { text: "the part where Karpathy says the tokenizer causes a lot of problems", note: "Lectures" },
          { text: "someone waiting at the door in the rain", note: "Anime" },
        ] },
        { kind: "h", text: "Why not just watch at 3×?" },
        { kind: "p", text: "A 116-minute lecture at 3× still takes nearly 40 minutes, and you have to keep watching so you don't miss the part you need. Seekino parses a video once; after that, every search works like Ctrl+F in a document: you land on the part you need." },
        { kind: "next", items: [{ label: "How Cloud differs from Local", page: "versions" }, { label: "Run it on your computer", page: "self-host" }] },
      ],
    },
    versions: {
      nav: "Cloud vs. Local",
      title: "Cloud is for trying it. Local is for your own videos.",
      lead: "You're on Cloud now: nothing to install, and you can search the lectures and anime we've already parsed. To search your own videos, install the Local version.",
      blocks: [
        { kind: "compare", columns: ["", "Cloud", "Local"], rows: [
          ["Getting started", "Open a browser", "One Docker command"],
          ["What you can search", "Published public samples: lectures and anime", "Any video you upload"],
          ["Upload and parsing", "Not available yet", "Yes, with progress tracking and retries"],
          ["Search, watch, ask", "Yes", "Yes"],
          ["Account", "Use it as a guest; sign in to keep your conversations", "Runs on your machine, no sign-up"],
          ["Models and cost", "Provided by the platform", "Your choice of Ark, DeepSeek, Kimi and more; billed to your own account, with tokens recorded for every call"],
          ["Footage for Claude Code / Codex", "Public series and clips up to 60 seconds", "Local video library"],
          ["Where your data lives", "Platform servers", "Videos and databases stay on your computer"],
        ] },
        { kind: "p", text: "Uploads and personal video libraries are available in the Local version." },
        { kind: "next", items: [{ label: "Deploy the Local version", page: "self-host" }] },
      ],
    },
    "self-host": {
      nav: "Run it locally",
      title: "One command runs the full Seekino on your computer",
      lead: "The default setup runs in Docker and calls models over APIs: no GPU, no model downloads, and no Python or Node.js on your machine. You need Docker and two model keys.",
      blocks: [
        { kind: "steps", items: [
          { title: "Install Docker", body: "On Windows or macOS, install Docker Desktop (on Windows, choose WSL 2 / Linux containers). On Linux, install Docker Engine with the Compose plugin." },
          { title: "Get the code", body: "Clone the repository and enter it.", code: `git clone ${GITHUB_URL}.git\ncd Seekino` },
          { title: "Add two keys", body: "Copy the environment template, then set ARK_API_KEY and DEEPSEEK_API_KEY in .env.docker.", code: "cp deploy/docker/env.example .env.docker" },
          { title: "Start it", body: "The first build needs a network connection to download images and dependencies.", code: "docker compose --env-file .env.docker up --build -d --wait" },
          { title: "Try a short video", body: "Open http://127.0.0.1:5173, create a series and upload a few minutes of video. Once it's parsed, go back to the three steps in Quick start and try them on your own video." },
        ] },
        { kind: "links", label: "More setup", items: [
          { label: "Local setup guide: Kimi / Codex, persistence, Windows (in Chinese)", href: SETUP_GUIDE },
          { label: "Project README", href: README_EN },
          { label: "API docs (/api/docs on your machine)", href: "http://127.0.0.1:5173/api/docs" },
        ] },
        { kind: "next", items: [{ label: "Connect Claude Code / Codex", page: "agents" }] },
      ],
    },
    agents: {
      nav: "Agents",
      title: "Give your agent a library that remembers every scene",
      lead: "In our editing tests, Claude Code used Seekino to find the 1.3 seconds where Anon grits her teeth in episode 4, checked the source, and pulled the clip into its own workspace. You say what you need; it does the finding.",
      blocks: [
        { kind: "h", text: "Connect" },
        { kind: "steps", items: [
          { title: "Get a Cloud read token", body: "Copy from your Cloud site and run in your editing terminal. Guests can read published platform series. Run it again when the token expires.", code: cloudToken },
          { title: "Connect Claude Code to Cloud", body: "Run this if you use Claude, then start Claude Code in this terminal.", code: claudeCloud },
          { title: "Connect Codex to Cloud", body: "Run this if you use Codex, then start Codex in this terminal.", code: codexCloud },
          { title: "Also use local footage", body: "Start the local API on your computer and add this second server to your client.", code: "claude mcp add --transport http seekino_local http://127.0.0.1:9621/mcp\n# or\ncodex mcp add seekino_local --url http://127.0.0.1:9621/mcp" },
          { title: "Ask the way you'd brief a colleague", body: "For example: \"Find the shots of Anon gritting her teeth in Seekino, check the sources, and save the clips to clips/.\"" },
        ] },
        { kind: "h", text: "Four tools your agent can use" },
        { kind: "tools", items: [
          { name: "list_series", body: "See which series are in the library." },
          { name: "retrieve", body: "Search clips with a description; returns the source video and time range." },
          { name: "browse_events", body: "Walk through what happens in an episode, in order." },
          { name: "read_evidence", body: "Read source context and supply start and end times in milliseconds to download an MP4 clip." },
        ] },
        { kind: "next", items: [{ label: "What's next", page: "roadmap" }] },
      ],
    },
    roadmap: {
      nav: "What's next",
      title: "Say a name. Get every second they appear.",
      lead: "This is what we want to do next: across a whole series, Seekino won't just find one scene. It will list every moment a character appears, with timestamps, without missing one.",
      blocks: [
        { kind: "roadmap", status: "Planned", items: [
          { title: "Every appearance of a character", body: "Name a character and get every part of the series they appear in." },
          { title: "Personal libraries on Cloud", body: "Sign in to upload and parse your own videos on Cloud, with nothing to deploy." },
          { title: "Reusable editing references", body: "Remember the camera moves and motion Seekino has parsed, so an agent can reuse them in the next edit." },
        ] },
        { kind: "p", text: "None of these have dates yet. Which one do you want first? Tell us in the community group; one message from you can decide what we build next." },
        { kind: "h", text: "Join the community" },
        { kind: "channels", items: [
          { label: "Community group", value: COMMUNITY_PLACEHOLDER },
          { label: "Bugs and ideas", value: "GitHub Issues", href: `${GITHUB_URL}/issues` },
        ] },
        { kind: "star", text: "If Seekino saved you one drag of the progress bar, give it a Star on GitHub." },
      ],
    },
  },
};

export function docsContent(locale: Locale): DocsContent {
  return locale === "en" ? en : zh;
}
