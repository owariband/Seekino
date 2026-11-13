<div align="center">
  <a href="https://seekino.hypn0s.cloud">
    <img src="assets/readme/seekino-idle.svg" width="96" alt="Seekino" />
  </a>
</div>

<h1 align="center">Seekino</h1>

<p align="center">
  <strong>Ctrl+F for video</strong><br />
  Search what you see like text: one sentence, straight to that second — with a clip you can check against the original.
</p>

<p align="center">
  <img alt="English" src="https://img.shields.io/badge/English-DBEDFA">
  <a href="README.md"><img alt="简体中文" src="https://img.shields.io/badge/简体中文-DFE0E5"></a>
</p>

<p align="center">
  <a href="https://seekino.hypn0s.cloud"><img alt="Get Started" src="https://img.shields.io/badge/Try%20it%20live-seekino.hypn0s.cloud-2e6cc4"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-ffffff?labelColor=d4eaf7&color=2e6cc4"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="Node.js 24+" src="https://img.shields.io/badge/Node.js-24%2B-339933?logo=nodedotjs&logoColor=white">
</p>

<h4 align="center">
  <a href="https://seekino.hypn0s.cloud">Cloud</a> |
  <a href="https://seekino.hypn0s.cloud/docs">Document</a> |
  <a href="#-roadmap">Roadmap</a> |
  <a href="https://github.com/owariband/Seekino/issues">Issues</a>
</h4>

<!-- Demo video: drag the mp4 into any issue/PR comment, then paste the user-attachments URL below -->
<p align="center">
  <video src="https://github.com/user-attachments/assets/REPLACE_WITH_YOUR_DEMO" width="920" autoplay loop muted playsinline></video>
</p>

<p align="center"><sub>One sentence → retrieved clips → jump straight to that second</sub></p>

<details open>
<summary><b>📕 Table of Contents</b></summary>

- 💡 [What it solves](#-what-it-solves)
- 🎮 [How to use it](#-how-to-use-it)
- 🧩 [Features](#-features)
- 🔥 [What's new](#-whats-new)
- 🎉 [Star us](#-star-us)
- 🚀 [Quick start](#-quick-start)
- 🔌 [API](#-api)
- 📜 [Roadmap](#-roadmap)
- 📚 [Docs](#-docs)
- 🙌 [Acknowledgements](#-acknowledgements)

</details>

---

## 💡 What it solves

> **41 hours of lectures, and you need one segment.** Finding "where does he explain gradient descent" means scrubbing at 3× speed for half an hour. Seekino gets you there in one sentence.

| Your situation | What Seekino does |
| --- | --- |
| You remember the scene, not where it is | Describe it roughly, or upload an image — located in seconds |
| You want to ask while watching | The assistant knows where you are; no screenshots, no re-explaining |
| You just want to know if it's worth watching | An outline and a series overview first, then dive into what matters |
| Editing footage re-parsed over and over | Parse once, reuse forever — and hand it to Claude Code over MCP |

Every hit carries its **source video and time range**, so you can always go back and check. Not "looks similar" — "this is it".

<p align="center">
  <img src="assets/readme/screenshots/search-results.jpg" width="880" alt="Natural-language search: retrieved clips with reasons and original timecodes" />
</p>

<details>
<summary>More screenshots (2026-10-25, real footage)</summary>

![Seekino library: video series, analysis status and management controls](assets/readme/screenshots/library.jpg)

![Seekino viewing Q&A: original video player and conversation panel](assets/readme/screenshots/watch-chat.jpg)

</details>

## 🎮 How to use it

| I want to… | Go to | Cost |
| --- | --- | --- |
| See it working | **[Seekino Cloud](https://seekino.hypn0s.cloud)** | Nothing to install |
| Process footage on my own machine | [Docker in one command](#-quick-start) | Docker + 2 model API keys, ~15 min |
| Plug it into my own agent / editing flow | [API](#-api) | A running instance + an MCP client |

## 🧩 Features

### Find that moment

- **One sentence**: describe the scene, the line or the relationship you remember — no need to recall the episode or the timestamp.
- **Image to image**: upload a screenshot and search the video by what it looks like.
- **Routes, not just hits**: ask "what order should I watch this course in" and get an outline and a study path first.
- **Checkable**: every clip carries its episode and time range; open it and you're back in the original.

### Ask while watching

- The assistant knows where you are — ask "I don't follow this" without screenshots or recap.
- Answers stream in, the reasoning folds away, and the clips it cites sit under the sentence that used them.
- Follow-ups keep the context going.

### Parse and manage

- **Parse once, reuse forever**: finished footage goes into your own library, searchable from then on.
- **No babysitting**: parsing runs in the background, progress and failures are visible, failures retryable.
- **Structure first**: an outline and a series overview are generated as soon as parsing finishes.
- **Knows who is talking**: speaker grouping across the whole video, with background notes and voiceprints; without enough evidence it stays blank instead of guessing.
- **Cost you can see**: swap models per stage, and every call records its tokens and estimated cost.

### For agents

- Ships an **MCP** endpoint, so clients like Claude Code can pull clips directly.
- 11 tools for clip selection, style references and series-shared templates, plus an editing skill.
- Give it a vague impression, let it narrow things down, and review what comes back.

## 🔥 What's new

- 2026-11-12 In-episode **visual search** and smart correction, plus player shortcuts
- 2026-11-12 Mobile navigation reworked, more compact episode rows
- 2026-11-11 Editing MCP gains **shared series templates** and production skills
- 2026-11-11 Text search inside a video
- 2026-11-11 **Cloud MCP** access for editing agents

<sub>Full history in [closed issues](https://github.com/owariband/Seekino/issues?q=is%3Aissue+is%3Aclosed).</sub>

## 🎉 Star us

⭐️ Hit **Star** in the top-right corner to get release notifications.

<sub>The project is young — every star counts.</sub>

## 🚀 Quick start

### Option 1: Use the cloud

Open <https://seekino.hypn0s.cloud>. Public series are already parsed and searchable. For the editing MCP, see the [Cloud MCP guide](https://seekino.hypn0s.cloud/docs/agents).

### Option 2: Self-host with Docker

You need Docker Desktop (or Docker Engine with a current Compose plugin); on Windows use WSL2 / Linux containers. Python, Node.js, ffmpeg and the model SDKs are already in the image.

```bash
git clone --depth 1 --single-branch --branch main https://github.com/owariband/Seekino.git
cd Seekino

cp deploy/docker/env.example .env.docker
```

Fill in two keys in `.env.docker`; if the file already exists, edit it instead of overwriting:

```dotenv
ARK_API_KEY=your-volcengine-ark-key
DEEPSEEK_API_KEY=your-deepseek-key
```

> Ark is used for chat, audio/video understanding and embeddings, DeepSeek for the retrieval agent and reranking. Kimi, Codex and other models can be swapped in — see the [local setup guide](deploy/local-setup.md).
>
> **Privacy and cost:** with remote models, the relevant audio, frames or text are sent to the configured provider and may incur charges.

Then start it with one command:

```bash
docker compose --env-file .env.docker up --build -d --wait
```

- **Web:** <http://127.0.0.1:5173>
- **API docs:** <http://127.0.0.1:5173/api/docs>
- **Health:** <http://127.0.0.1:5173/healthz>

The first start initialises the database and search index. Suggested acceptance path: **create a series → upload a short video → wait for parsing → search a clip → jump back to the original → ask two follow-ups**. A healthy `/healthz` does not mean the model pipeline is verified.

```bash
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs -f api
docker compose --env-file .env.docker stop
```

Videos, logs and model config live in the `app_data` volume; `stop` / `down` keep them, `down -v` deletes them. Port conflicts, backups and recovery are covered in the [local setup guide](deploy/local-setup.md).

### Hand it to an AI

Paste this to an AI that can operate the repo; you still fill in the credentials:

```text
Set up Seekino locally in this repo. Read README.md and deploy/local-setup.md first.
Use the "Docker full stack + default DeepAgents / API models" path with .env.docker, in the documented order.
Do not change business code, and do not delete or rebuild the existing database, Elasticsearch indices or outputs/.
Stop and ask me when you need an API key, a model choice, or a port conflict resolved.
When done, check web, /healthz and /api/docs, and give me acceptance steps that include uploading a short video.
```

## 🔌 API

The UI and external tools share one service: HTTP endpoints for uploads, parse jobs, search and chat, and `/mcp` for a Streamable HTTP MCP endpoint that MCP clients can pull clips from.

OpenAPI docs are at [http://127.0.0.1:9621/api/docs](http://127.0.0.1:9621/api/docs); integration notes in [Server](server/README.md).

## 📜 Roadmap

- [ ] Route by content type (course / film / editing footage) to control cost
- [ ] GIF input (text and static images work today)
- [ ] Reuse loop for editing footage: store motion code, sources and feedback
- [ ] Publish more performance and cost numbers
- [ ] Versioned releases and Windows verification

## 📚 Docs

- [Project intro](docs/README.md) · [Local setup](deploy/local-setup.md) · [Deployment and backups](deploy/README.md)
- [HTTP and MCP API](server/README.md) · [Web](web/README.md)

## 🙌 Acknowledgements

[video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) provides shot recipes and production methods for the editing agent.

[motion-showreel](https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel) informs code animation and layered compositing.

Both upstream projects are well worth a star.

## 🤝 Feedback

- **You searched and found nothing**: open an [issue](https://github.com/owariband/Seekino/issues) with the source video and the moment you were after — concrete examples beat any other feedback.
- **Questions, collaboration, or wiring it into your own project**: issues and [discussions](https://github.com/owariband/Seekino/discussions) are both open.
- **Want to contribute**: roadmap items tagged `good first issue` are a good place to start.

<sub>Code is Apache-2.0; the Seekino name and logo are not licensed with it — see [TRADEMARK.md](TRADEMARK.md).</sub>
