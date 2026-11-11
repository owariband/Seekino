<p align="center">
  <img src="assets/mascot/animated/seekino-idle.svg" width="88" alt="Seekino" />
</p>

<h1 align="center">Seekino</h1>

<p align="center"><strong>Ctrl+F for video</strong></p>
<p align="center">AI video search: search what you see like text, and jump straight to that second.</p>

<p align="center">
  English · <a href="README.md">简体中文</a>
</p>

<p align="center">
  <a href="#1-highlights">Highlights</a> ·
  <a href="#2-technical-architecture">Architecture</a> ·
  <a href="#3-deployment">Deployment</a> ·
  <a href="#api-integration">API &amp; MCP</a> ·
  <a href="#references">References</a>
</p>

## 1. Highlights

> **Can't get through the lecture slides?** A 116-min course video is daunting, and you can't tell which "knowledge" inside is the part you want. Even searching at 3× speed still takes 30+ min!
>
> **Editing footage hard to manage?** You keep asking the agent to re-parse the same reference clip and animation source, you still find and supply most character art yourself, and the motion effects and animations you've analysed are never kept. 🚧

**Parse a video once, then locate and reuse it again and again.** Jump around a video as freely as you skim text, and hand "what I'm watching right now" to the agent as easily as you'd paste a paragraph.

- **Courses:** one sentence, and you find the point you want in a dozen seconds, with no speed-watching
- **Editing:** one piece of footage, parsed once and reused again and again, building your personal editing library

Seekino recalls the clips you need, with the source video and time range, so you can always check against the original.

<p align="center">
  <img src="docs/architecture/fig01.png" width="960" alt="From remembering a moment to a verifiable clip" />
</p>

### 1.1. Retrieval support

| Your problem | What Seekino does |
| --- | --- |
| Can't find that iconic anime scene any more? | Upload an image or describe your vague impression, and Seekino locates it in seconds |
| Want to study a course without taking screenshots? | Ask while you watch; the AI knows where you are and what's confusing you |
| Need your own "crash course route"? | Give your question; the AI "browses" the video's outline and tailors a route for you |
| Need editing footage but don't want to scrub for it? | Seekino provides MCP, so your Claude Code fetches the clips you need; you just give a vague impression and review |
| Need references for editing, but past results can't be reused? 🚧 | Seekino stores your past motion-effect analyses in depth and lets MCP "update your own editing reference experience", so your editing know-how compounds |

### 1.2. Parsing support

| Your problem | What Seekino does |
| --- | --- |
| Have to babysit a long video while it parses? | Upload once. Seekino transcribes the whole video and parses it in parallel chunks; progress is visible in Parse Tasks, failures can be retried, and you can search as soon as it's done |
| Long courses too expensive to parse? | Every stage can use a different model (Kimi, Doubao, DeepSeek…), and every call records its tokens and estimated cost, so you always know what you're spending |
| A sentence cut in half? | Sliding-window Refine re-reads adjacent chunks together, fills dropped lines, removes duplicates and joins sentences across the cut |
| Can't tell who's speaking? | Whole-video speaker grouping, series background notes and voiceprints identify the speaker; without enough evidence it stays blank instead of guessing |
| Just want to know what the video is about first? | After parsing, Seekino writes a video outline and a series overview, so you see the structure first and dive in where it matters |
| Courses, films and editing footage all parsed the same way? 🚧 | Seekino recognises the parse type and controls cost per type; you can customise it too |

<sub>🚧 = not implemented yet.</sub>

<details>
<summary>Screenshots (2026-10-25, local build, real footage)</summary>

![Seekino library: video series, analysis status and management controls](assets/screenshots/library.jpg)

![Seekino search: a natural-language question, retrieved clips and selection reasons](assets/screenshots/search-results.jpg)

![Seekino viewing Q&A: original video player and conversation panel](assets/screenshots/watch-chat.jpg)

</details>

## 2. Technical architecture

To offer public Q&A that casual users can try on a lightweight cloud server, and let heavy users self-host their own video knowledge base, Seekino builds the following system across three stages: parsing, retrieval and Q&A.

<p align="center">
  <img src="docs/architecture/fig-overview.png" width="960" alt="Seekino overview: parse, retrieve, chat and external services" />
</p>

<sub>Figures 2–8 are editable: open the matching `.excalidraw` file in [docs/architecture](docs/architecture/) on [excalidraw.com](https://excalidraw.com). Older figures live in [seekino-figures.drawio](docs/architecture/seekino-figures.drawio). 🚧 = not implemented yet; TBD = not yet measured on the current build.</sub>

### Data model

MySQL holds business facts, tasks and versions. Elasticsearch holds one derived document per chunk, addressed by parse task and chunk, and can be rebuilt at any time.

<p align="center">
  <img src="docs/architecture/fig-er.png" width="960" alt="Data model: MySQL tables and the derived Elasticsearch document" />
</p>

### 2.1. Parsing

#### Parallel parsing

The parsing layer uses WhisperX for ASR by default, and you can swap in an ASR API.

To work across many scenarios and model APIs at a cost every user can afford, Seekino cuts each video into fixed 60 s chunks and parses them in parallel (10 at a time by default). A hook recognises the parse type and routes to one of three parse modes, each with its own cost control (user-customisable) 🚧:

- **course**: lectures and courses
- **script**: scripted film and TV
- **clip**: editing footage

<p align="center">
  <img src="docs/architecture/fig06.png" width="960" alt="Fixed 60 s chunks parsed in parallel" />
</p>

#### Paying back what chunking costs

In parallel, a single chunk loses the context of its neighbours, and fixed chunking cuts through sentences and actions. Two passes repair this:

| Pass | What it does |
| --- | --- |
| Sliding-window Refine | Re-reads every three adjacent chunks together to fill dropped lines, remove duplicates and join dialogue across the cut |
| Tail Summary | After parsing, reads the whole timeline once more to write the video outline and the series overview |

<p align="center">
  <img src="docs/architecture/fig-parse.png" width="960" alt="Parse V2: stages, persisted state, Refine and the JSON gate" />
</p>

#### Results

Understanding of one 1-min chunk at 3 FPS (max height 720) on the course *Let's build GPT* (116 min, 117 chunks), medians of successful runs:

| Model | FTT | TT | Input tokens / chunk | Est. cost / chunk |
| --- | ---: | ---: | ---: | ---: |
| Kimi k3.1-pro | 43.4 s | 320.2 s | ≈244k | TBD |
| Kimi k3.1-pro (low) | TBD | 105.5 s | ≈244k | TBD |
| Doubao Seed 2.1 Pro | TBD | TBD | TBD | TBD |
| DeepSeek Flash (non-thinking) | TBD | 31.8 s | TBD | ≈¥0.22 |

<sub>FTT: time to first token; TT: total time per chunk. The DeepSeek cost comes from incomplete usage records. Sources: [Kimi trace](docs/report/server/kimi-parse-trace-20261101.md) · [DeepSeek Flash audit](docs/report/gateway/deepseek-flash-chunk-audit-20261103.md) · [devlog](docs/dev/devlog.md)</sub>

The benefit of parallel parsing: whatever the model, the time a long video takes stays under control. It grows with chunks ÷ concurrency, not with one serial pass over the whole video.

#### Challenge: concurrent Refine conflicts

Adjacent windows share chunks, so two of them can edit the same content at once. We moved from pessimistic locking, where tools forcibly controlled the agent's output, to optimistic, non-blocking control: as soon as the first overlapping window commits, the others are interrupted and receive a new message:

> Chunk 13, shared by window [12, 14], has been updated; the new window JSON is: {…}

The agent is freed from the endless fail-and-retry tool pipeline, and time and token use both drop. On the same 3 chunks:

| Approach | Model requests | Time | Input tokens | Result |
| --- | ---: | ---: | ---: | --- |
| Canvas tools, step-by-step reads and writes | 8 | 593.7 s | 314,671 | 45 events, 16 dialogue lines |
| One complete JSON | 1 | 247.0 s | 11,307 | 45 events, 16 dialogue lines |

Before optimistic control, one full-course run had 49 commits rejected for conflicts after generation had already finished, and failed attempts took 57% of all Refine time. An interrupted request now exits in about 1 ms (local SSE test). Whole-course time and token savings on real models are TBD.

Today's models are capable enough to justify the optimistic view; so far only DS Flash non-thinking has produced unreliable output.

#### Gate

Free of the Canvas tools, the agent still must not break the JSON structure. A JSON gate checks every output against JSON and the schema. When a check fails, the gate returns a guide message together with the agent's own last full output, so it fixes only what's wrong:

> field `members.0.content` is wrong: xxx, please fix it

Problems the host can fix deterministically, such as unsorted times, are fixed directly with no extra model call. The final structure is always correct and valid.

<p align="center">
  <img src="docs/architecture/fig09.png" width="960" alt="Refine before and after" />
</p>

### 2.2. Building retrieval

<p align="center">
  <img src="docs/architecture/fig-retrieve.png" width="960" alt="Retrieve: scope, channels, fusion, verification and rerank" />
</p>

#### Vectors designed for multimodal input

To support gif, jpg, text and other input, the stored vectors are designed around it. The retrieval unit changed from the 1-min chunk to the Event (one action, shot or line), so a short line isn't diluted inside a one-minute vector. Each Event is stored on three channels:

| Channel | What it stores | Good at |
| --- | --- | --- |
| Lexical | Event text and dialogue (BM25) | exact quotes, names, terms |
| Semantic | Text vectors of the description and dialogue | paraphrases, vague descriptions |
| Visual | Frame vectors | visual descriptions, image queries |

Text and still images work today; gif input 🚧.

#### Keeping strong hits with ‖L<sub>∞</sub>‖ 🚧

Plain weighted RRF uses fixed weights, which can push a channel's hit 1, a result that should match, out of the top 10 after weighting. Taking the maximum across channels keeps such a result from being filtered out at the end, so retrieval stays reliable. Fusion is currently weighted RRF with query-adaptive weights; this must be added.

#### A hit is not a fact

Before results are returned, each one is checked against the timeline of its parse task and content version: a vector hit is only a candidate, and only a checked hit is a fact.

<p align="center">
  <img src="docs/architecture/fig10.png" width="960" alt="Why the retrieval unit changed from chunk to Event" />
</p>

#### Top-down and bottom-up

- **Top-down** (the whole, a route): read the video outline with the user's vague intent, e.g. "I want to build a GPT; which points should I focus on mastering?", plan at the macro level how to help, then go deeper into selected chunks through the outline to find specific Events as evidence.
- **Bottom-up** (a line, a scene): locate the Event directly, then go back to its chunk and use the context to answer more fully.

The agent picks the approach for each question.

### 2.3. Retrieval Q&A

#### Decide whether to search at all

The same words can be a new search or just a reply to the last answer. Before each turn reaches the Supervisor, it passes through a query router and rewriter:

| Step | What it does |
| --- | --- |
| Route<br><sub>Jev-1.14 · 3 s</sub> | Decides whether the message is respond (just reply), retrieve (search) or mixed (both) |
| Rewrite<br><sub>DeepSeek Flash · 4 s</sub> | For retrieve / mixed, writes only the part to search as `retrieval_query`; quoted lines stay verbatim, and names, episodes or plot are never guessed |
| Fallback<br><sub>8 s total budget</sub> | Any timeout, error or invalid output falls back to the raw message, so it can only help, never get in the way |

<p align="center">
  <img src="docs/architecture/fig-chat.png" width="960" alt="Chat: query hook, Supervisor tools and delivery" />
</p>

#### Answer with clips

The Supervisor calls tools as needed:

- delegate to the video-search agent to recall Events
- read the video outline and the series overview
- understand what you're watching at your current playback position
- search the web for background

Each clip appears as a playable card under the text that cites it, snapped to sentence and Event boundaries; click it to go back to the original and check.

Answers stream as they're generated, with the reasoning folded away. If the first token hasn't arrived in 15 s, the client backs off and reconnects, up to 3 times. Turn latency, pass rate and token cost before and after the router are TBD.

<details>
<summary>Problems we hit, effects, and metrics still to measure</summary>

![Problems to fixes](docs/architecture/fig08.png)

![Effect comparison](docs/architecture/fig11.png)

![Metrics to measure](docs/architecture/fig12.png)

</details>

### 2.4. Layers and deployment

The repository is layered by folder. Calls only go downward, and each layer does only its own job:

<p align="center">
  <img src="docs/architecture/fig-layers.png" width="960" alt="Repository layers: web, server, agent, engine, gateway" />
</p>

Locally one Compose stack runs everything; the cloud sample library splits a CPU server from a GPU server:

<p align="center">
  <img src="docs/architecture/fig-deploy.png" width="960" alt="Deployment: self-hosted and cloud" />
</p>

## API integration

The web interface and external tools use the same service APIs. Your tools can connect to these capabilities:

| Integration | Existing endpoints |
| --- | --- |
| Upload videos and start analysis | `POST /api/uploads/create` → `PUT /api/uploads/segments/upload` → `POST /api/uploads/complete` |
| Track analysis tasks | `GET /api/jobs/video-parse/get`, `GET /api/jobs/video-parse/events` |
| Retrieve footage | `POST /api/search`, returning source clips, time ranges and evidence. |
| Find footage through Agent chat | `/api/agent/conversations/*` and `/api/agent/messages/*`, with ongoing conversations and streamed answers. |
| Let an external agent fetch clips (MCP) | Streamable HTTP at `/mcp` (local auth mode), tools `list_series`, `retrieve`, `browse_events`, and `read_evidence`. |

After starting the service, inspect requests and responses in the [OpenAPI docs](http://127.0.0.1:9621/api/docs). See [Server](server/README.md) for integration details. The current scope field is `tenantId`. Video time fields use integer milliseconds relative to the original video, including fields that retain legacy `*Seconds` names; follow the current contract when integrating.

## Creative experiments

A separate experiment has already connected retrieval, clip preparation and HyperFrames composition into an offline HTML short video. See the [short-video experiment workflow](.agents/skills/seekino-short-video/SKILL.md). It explores reusing retrieved footage; check its scripts and configuration against the current API before using them.


## 3. Deployment

Self-hosting is the way to run Seekino today; a cloud Docker deployment for the public demo is in progress.


The recommended first install runs **Web, API, video processing, MySQL and Elasticsearch in Docker**, with **DeepAgents** and API models by default. Local mode needs no application login.

Install Docker Desktop, or Docker Engine with a current Compose plugin. On Windows, select **WSL2 / Linux containers**; the commands also work in PowerShell. Python, Node.js, ffmpeg and the model SDKs are included in the images.

From the repository root:

```bash
cp deploy/docker/env.example .env.docker
```

Fill in `ARK_API_KEY` and `DEEPSEEK_API_KEY` in `.env.docker`, and confirm your accounts can access the models selected in `config.yaml`. Keep an existing environment file instead of overwriting it. Then start everything:

```bash
docker compose --env-file .env.docker up --build -d --wait
```

Initialization checks the configuration and creates the business tables, DeepAgents checkpoints and search indexes before starting the API and Web. Existing databases still require the documented upgrade procedure.

- Web: <http://127.0.0.1:5173>
- API docs: <http://127.0.0.1:5173/api/docs>
- Health: <http://127.0.0.1:5173/healthz>; the API is also published on port `9621`.

Kimi and Codex remain optional through the existing source/model/harness fields in **the same `config.yaml`**. No extra provider YAML is required. Kimi uses `KIMI_API_KEY`; Codex uses the bundled SDK runtime and a persistent container login. See the [Docker setup guide](deploy/local-setup.md) for these steps, custom ports and configuration updates.

The `app_data` volume stores media, logs, the effective model configuration and Codex credentials; MySQL and ES have separate volumes. The model YAML is imported only on first launch, preserving changes saved by the Settings page. `stop` and `down` retain volumes; `down -v` deletes them.

```bash
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs -f api
docker compose --env-file .env.docker stop
```

Full container startup and Windows validation are still pending; see the guide for the checks completed. After startup, upload a short video, wait for parsing, retrieve a clip and ask two follow-up questions. A healthy HTTP endpoint does not validate the model pipeline. Remote model calls send the relevant media or text to the configured provider and may incur charges.

### Host development (optional)

<details>
<summary>Run API and Web on the host, with Docker databases</summary>

#### 1. Prepare the environment and configuration

You need Python **3.12** (`>=3.12,<3.13`), `uv`, Node.js **24+**, npm, ffmpeg and ffprobe. Storage uses MySQL **8.4** and Elasticsearch **8.19.x**. Bring your own, or use the repository's Compose file (ES is pinned to 8.19.22).

Copy the environment file the first time. If `.env.local` already exists, edit it instead:

```bash
cp .env.example .env.local
uv sync --locked
npm --prefix web ci
```

Fill in model credentials and local connection details in `.env.local`. The default configuration uses these sources:

| Model role | Default source | What you need |
| --- | --- | --- |
| Chat Supervisor, transcription, frame understanding, OCR and summaries | Ark | Set `ARK_API_KEY`. No local weights are required by the default configuration. |
| Video search agent | DeepSeek, with an Ark fallback | Set both API keys. |
| Embeddings | Ark | Set `ARK_API_KEY`. |
| Reranking, text fallback when vision fails | DeepSeek | Set `DEEPSEEK_API_KEY`. |

Adjust the [model configuration](docs/model-config.md) to the models or endpoints you have. The default `supervisor_harness: deepagents` uses an API source. To select Codex, change the harness and Supervisor binding together. Kimi uses the existing OpenAI-compatible adapter and `kimi` profile.

| Where | What |
| --- | --- |
| `config.yaml` | `model_sources` connections; model choices for chat, transcription, vision, embedding and rerank under `models`; global chat parameters under `agent_runtime`. |
| `.env.local` | API keys, personal endpoint overrides, database/ES/HTTP/identity settings. Never committed to Git. |
| `web/.env.local` (optional) | `VITE_API_BASE_URL` and `VITE_TENANT_ID` for a standalone web build. Defaults to the same-origin proxy and the local tenant. |

When running by hand, exported shell variables with the same name take priority over `uv --env-file`. Use the same configuration in both terminals.

#### 2. Prepare MySQL and Elasticsearch

With Docker Compose:

```bash
docker compose --env-file .env.local up -d --wait mysql elasticsearch
```

On first start, Compose creates the default development database `seekino_development`, and database ports are only published on `127.0.0.1`. The server, web app and ffmpeg still run on the host. If you use an existing MySQL, create the database and account named in `SEEKINO_DATABASE_URL` first; `init-db` only creates tables, not the database.

Then initialise or verify the current schema and ES index:

```bash
uv run --no-sync --env-file .env.local -- python -m server.cli init-db
uv run --no-sync --env-file .env.local -- python -m server.cli init-es-index
```

Both commands can be re-run, but they do not migrate old databases or rebuild incompatible indexes automatically. Old identity fields, time units or index contracts raise an upgrade error; see "Upgrading an existing database" under Architecture and deployment below.

#### 3. Start the API and the web app

Terminal 1:

```bash
uv run --no-sync --env-file .env.local -- python -m server.main
```

Terminal 2:

```bash
uv run --no-sync --env-file .env.local -- npm --prefix web run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

- Web: <http://127.0.0.1:5173>
- API docs: <http://127.0.0.1:9621/api/docs>
- Consumer status: <http://127.0.0.1:9621/healthz>

`python -m server.api` forwards to the official `server.main` entry point. The API process already includes the V2 consumer, so no separate worker is needed. Stop each hand-started process with Ctrl-C. Stop the databases with `docker compose --env-file .env.local stop mysql elasticsearch`; their volumes are kept.

The Vite proxy reads `SEEKINO_HTTP_HOST` / `SEEKINO_HTTP_PORT` from the same environment. To use a custom web port, change `--port` above and `SEEKINO_ALLOWED_ORIGINS`. Changing `SEEKINO_WEB_PORT` alone does not change the port of a hand-started Vite.

#### Optional: local launcher

The repository includes a launcher for starting installed native MySQL and Elasticsearch when needed, reusing the API/web processes and managing them:

```bash
./start.sh
./start.sh status
./start.sh stop
```

The launcher defaults to the checked-out `main` worktree and its `.env.local`. Use `./start.sh --branch dev` (and `--branch dev status` / `--branch dev stop`) for an existing dev worktree. `start` uses current local files, including uncommitted changes, without pulling or switching branches. Managed API/Web processes are refreshed when their source directory, file contents, or configuration changes; unchanged processes are reused, and unknown port owners are rejected. MySQL/ES are not stopped by this refresh.

The launcher checks the API library and the web proxy against the current `tenantId` contract, using the local tenant configured in `SEEKINO_LOCAL_TENANT_ID`. It reuses listening MySQL/Elasticsearch instances or starts installed native binaries, then validates schemas, checkpoints and indices. `stop` only stops processes the launcher manages. It does not stop a hand-started API/web or the Compose databases.

To run entirely without Docker, follow the [native local setup](deploy/README.md#可选全原生本机开发) (in Chinese) to install MySQL and Elasticsearch and configure their executable paths. Use the same launcher commands above; `stop` also stops native databases started by the launcher.

Native database data defaults to `data/mysql/` and `data/elasticsearch/`; launcher logs and PID records go to `outputs/local-services/`. Persistent media goes to `assets/library/` by default, configurable with `SEEKINO_ARTIFACT_ROOT`. Existing media roots are not moved automatically; preserve their environment directories and back them up together with MySQL. An existing MySQL/ES is reused as configured, and old business schemas are never migrated automatically. For native binary paths, legacy data protection, Docker and port details, see the [development deployment notes](deploy/README.md) (in Chinese).

</details>

## Architecture and deployment

<details>
<summary>System architecture and module responsibilities</summary>

```mermaid
flowchart LR
    Web["Web: upload, watch, search"] --> API["Server API"]
    API --> SQL["MySQL: facts, jobs, leases, tokens"]
    API --> Media["Artifact store: videos and media"]
    SQL --> Consumer["In-process V2 consumer"]
    Consumer --> Parse["ASR / Understanding / Canvas Refine"]
    Parse --> SQL
    Parse --> ES["Elasticsearch: search projection"]
    API --> Supervisor["Chat Supervisor"]
    Supervisor --> Search["Video search: Query → Evaluator → Chat"]
    Search --> ES
    Supervisor --> Realtime["Understanding at the current position"]
    Realtime --> SQL
    Realtime --> Media
    Supervisor --> Answer["Text and timed clip evidence → jump to playback"]
```

| Module | Responsibility |
| --- | --- |
| `gateway/` | Model protocols, authentication, call control and usage events. Connects to Ark, OpenAI-compatible APIs, the Codex app-server, the Claude SDK and more. |
| `engine/` | Domain algorithms: video event conversion, time and content validation, index projection, retrieval fusion. |
| `agent/` | LangGraph and agent programs, the Context Canvas, video understanding/refinement, and the Supervisor's tool decisions. |
| `server/` | Identity and resource permissions, uploads, MySQL persistence, leases and recovery, media preparation, ES writes, HTTP and CLI. |
| `web/` | The React frontend, connected to the real API. When the API is unavailable it shows errors or empty states. |

A full parse runs basic understanding over fixed chunks, while sliding intervals are refined separately and drive indexing. The model reads evidence and edits local content through the **Context Canvas**; the program keeps the full state and assembles the committed result. Refine checks member versions and content fingerprints, so a local edit can't drop events it didn't mention. An optional prefix provides cross-chunk context and is off by default.

The Supervisor can answer directly, search the web, call video search, or understand the current and neighbouring chunks at the frozen playback position. Even before a video is fully indexed, basic evidence can be fetched on demand. The Supervisor decides which calls to make; response time depends on the models and the execution budget.

Retrieval tools return source evidence directly; additional context is read by chunk ID and cursor. The Jev classifier and local 8B query rewrite remain in the pre-processing path.

The production service only accepts **V2 + MySQL + Elasticsearch**. The consumer runs in the same server process as the API, job and recovery state live in MySQL, and ES is a search projection. Gateway, Engine and Agent need no separate processes. Local mode needs no Redis or external message queue. Parse V1, Qdrant, and the old offline import entrypoints have been removed.

Video durations, event/chunk boundaries, playback positions and frame times are all **integer milliseconds** from the start of the original video. Existing fields such as `durationSeconds`, `startSeconds`, `endSeconds`, `atSeconds` and `positionSeconds` keep their old names, but their values are not seconds. The browser player converts to seconds at its boundary. Absolute times are Unix epoch milliseconds. Chunk durations and timeouts in the parse config are still in seconds.

</details>

<details>
<summary>Models and runtime parameters</summary>

`model_sources` describes protocol adapters, authentication and providers. `models` defines each model and its source. The app currently uses the `supervisor`, `agent`, `transcription`, `vision`, `vision_fallback`, `embedding` and `rerank` roles. Parameters such as vector dimensions and video sampling rate live in `models.embedding`. By default, Refine reuses the fixed Understanding model.

The top-level `supervisor_harness` chooses between the Codex and Deep Agents execution styles, and `models.supervisor` binds the Supervisor model on its own. `models.agent` still picks the model for video-search subtasks. By default the Supervisor uses DeepAgents with Ark, and frame understanding also uses Ark. Whether other models receive raw images depends on each model's `supports_vision_input` and its adapter. Switching sources never switches the embedding model automatically.

The gateway supports OpenAI-compatible APIs, Ark, a local Codex login, the Claude SDK and more; each model role and harness has its own capability requirements. Restart the server after changing a source, model or harness. It will not silently switch execution style when the config doesn't match.

`agent_runtime` parameters saved from the Settings page are written to the server's `config.yaml`. The next new message uses the new values; an answer already in progress keeps the parameters it started with. `maximum_concurrent_kicks`, model connections and other startup settings need a server restart. Runtime parameters set explicitly in YAML override the matching `SEEKINO_AGENT_*` environment fallbacks.

The Model Usage page reads MySQL `model_usage`. It covers V2 parsing/indexing and assistant calls, and shows only recorded tokens and known subtotals. Failed records may be missing, and unknown usage is never counted as zero. It does not calculate cost or enforce hard token quotas. The production bootstrap **does not wire up the Langfuse writer/reader**, so setting `tracing.enabled` to true does not turn on server tracing; the standalone module and config fields are still kept.

For source configuration see [model configuration](docs/model-config.md) and [gateway agent sources](gateway/README.md#chat-agent-模型来源). For how usage is counted, see [model token usage](docs/parse-model-usage.md).

</details>

<details>
<summary>Local and cloud identity</summary>

The default `SEEKINO_AUTH_MODE=local` uses `local-user` / `local-default`, with no login on your own machine.

`cloud` mode also needs a signing secret, Redis, SMTP and same-origin HTTPS. Redis stores verification-code digests, rate limits and concurrency leases; MySQL stores users, tenants and resource ownership. You can start the optional development Redis from Compose:

```bash
docker compose --env-file .env.local --profile cloud-auth up -d --wait redis
```

Guests can only read content that is public and ready, and their conversations are isolated per guest. After logging in with an email code, a user gets a personal tenant and can switch between personal and public content. Library management and global settings are controlled by server permissions; public read access never grants platform write access.

Cloud mode limits request counts and concurrency. Guests share a per-IP request budget; local mode has no request quota. These limits are separate from token statistics. A production reverse proxy must forward the HTTPS scheme and client address correctly. See [.env.example](.env.example) for the settings and the [user/tenant handoff notes](docs/report/server/user-tenant-handoff-20261010.md) for identity details and migration.

Business HTTP scoping fields now use **`tenantId`**. Some older docs still show `workspaceId` examples; follow the current OpenAPI and DTOs.

</details>

<details>
<summary>Upgrading an existing database</summary>

Development, test and production use separate databases and ES indexes. Development database names end with `_dev` / `_development`, and test names end with `_test`. ES index names must include the matching `dev` / `development` / `test` component, and production indexes must not include these components. SQLite is only for isolated tests.

`init-db` verifies the current schema; it does not migrate historical databases. Check your existing structure against the [identity migration](docs/migrations/20261010_user_tenant_identity.sql), the [media milliseconds migration](docs/migrations/20261008_video_timeline_milliseconds.sql) and the [server notes](server/README.md). To copy an old ES index to tenant fields, use `python -m scripts.migrate_tenant_index --help`. Run it as planned once the relevant writes have stopped. It does not recompute embeddings or overwrite the old index.

</details>

## Development and verification

Common checks:

```bash
uv run --no-sync python -m pytest -m "not live_agent"
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
```

Browser tests run against a production build preview. Build first and install Playwright Chromium, then run:

```bash
npm --prefix web exec -- playwright install chromium
npm --prefix web run test:e2e
```

Real integration tests need separate `SEEKINO_TEST_DATABASE_URL`, `SEEKINO_TEST_ELASTICSEARCH_URL` and `SEEKINO_TEST_AUTH_REDIS_URL`. Tests against real models must be enabled explicitly and may cost money with your provider. Known issues and pending acceptance checks are recorded in the [issue list](docs/dev/problems.md).

Passing unit tests, builds, isolated SQL/ES tests and mocked-API browser tests is not the same as a full acceptance run with real models. Semantic quality, cold-start latency of on-demand understanding, real SMTP and full browser end-to-end runs each need separate acceptance. A successful `/healthz` only means the consumer is running.

## Repository and docs

```text
seekino/
├── config.yaml / .env.example   # Model defaults and environment template
├── gateway/                    # Model protocols and source adapters
├── engine/                     # parsev2, rag and shared media utilities
├── agent/                      # Chat, realtime understanding, parsing and the Context Canvas
├── server/                     # HTTP/CLI, persistence and reliable execution
├── web/                        # React + TypeScript + Vite frontend
├── scripts/                    # Local launcher and controlled migration tools
├── tests/ / evals/              # Automated tests, manual diagnostics and evaluations
├── deploy/ / compose.yaml      # Local infrastructure and deployment notes
├── assets/                     # Logo, screenshots and architecture diagrams
├── docs/                       # Contracts, designs, migrations and check logs
└── outputs/                    # Local media, logs and run artifacts, not in Git
```

The repository root is the runtime boundary. Python modules are managed together by `pyproject.toml` / `uv.lock` with `package=false`; the layers are not published as separate packages.

Most module docs below are written in Chinese.

- [Design document index](docs/design/README.md) · [Documentation conventions](docs/README.md)
- [Benchmark index](evals/benchmark/README.md): query capabilities, chat behavior, retrieval quality and intent routing.
- [Parse V2 Agent](agent/video_parse_v2/README.md) · [Context Canvas](docs/design/02-agent-harness/context-canvas-design.md)
- [Current-position understanding](agent/chat/realtime_understanding/runtime.py) · [Server](server/README.md)
- [Web](web/README.md) · [Gateway](gateway/README.md) · [Engine](engine/README.md)
- [Model configuration](docs/model-config.md) · [Model token usage](docs/parse-model-usage.md) · [User/tenant](docs/report/server/user-tenant-handoff-20261010.md)
- [Development deployment](deploy/README.md) · [Issue list](docs/dev/problems.md)

## References

These are key sources consulted during the project's research and design. The papers include directions still under investigation; listing them does not imply a complete implementation.

### Papers and research

| Paper | Area of interest |
| --- | --- |
| [OmniScript](https://arxiv.org/abs/2604.11102) | Structured scripts from long videos, scene and event hierarchies, and temporal grounding of characters and dialogue. |
| [ScreenWriter](https://arxiv.org/abs/2410.19809) | Automatic screenplay generation, scene organization, speaker attribution and movie summaries. |
| [TransNet V2](https://arxiv.org/abs/2008.04838) | Shot boundaries and transitions. |
| [ShotBench / ShotQA / ShotVL](https://arxiv.org/abs/2506.21356) | Understanding and evaluating shot scale, composition, lighting, camera movement and other cinematography dimensions. |
| [CameraBench — Towards Understanding Camera Motions in Any Video](https://arxiv.org/abs/2504.15376) | Camera motion taxonomy, understanding and evaluation. |
| [VideoRAG](https://arxiv.org/abs/2502.01549) | Multimodal retrieval-augmented generation over long videos and across videos. |
| [RAPTOR](https://arxiv.org/abs/2401.18059) | Hierarchical summaries and retrieval at multiple levels of detail. |
| [QVHighlights](https://arxiv.org/abs/2107.09609) | Query-based temporal localization and highlight selection evaluation. |
| [VISTA — LLM Agents Are Latent Context Managers](https://arxiv.org/abs/2606.30005) | Visible context state, on-demand archiving and recovery of complete content. |
| [ACM — Agentic Context Management for Long Horizon Tasks](https://arxiv.org/abs/2607.23809) | Context editing, offloading and on-demand retrieval in long tasks. |

### Engineering practice

- [Anthropic — Writing effective tools for agents — with agents](https://www.anthropic.com/research/writing-tools-for-agents): applied to the project's Agent tool design, including clear tool responsibilities, on-demand context reads, pagination and actionable error feedback. See the project's [Canvas tool design research](docs/research/2026-10-22-canvas-agent-tool-design.md) (in Chinese).
