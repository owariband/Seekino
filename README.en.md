<p align="center">
  <img src="assets/mascot/animated/seekino-idle.svg" width="88" alt="Seekino" />
</p>

<h1 align="center">Seekino</h1>

<p align="center"><strong>Parse a video once. Find, jump and ask inside it like text, forever after.</strong></p>
<p align="center">A personal video library: natural-language and image search, clip-level answers, and every result traced back to the original timestamp.</p>

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

- Can't get through a lecture's slides? A 116-min course video is daunting, and you can't tell which "knowledge" inside is what you actually want. Even searching at 3× speed still takes 30+ min in total!
- (TODO) Editing footage is hard to manage: you keep asking the agent to re-parse the same reference clip and animation source, you still have to find and supply most character art yourself, and the motion effects and animations you've analysed as references are never kept.

—Parse a video once, then locate and use it again and again. Jump around a video as freely as you skim text, and hand "what I'm watching right now" to the agent as easily as you'd paste a paragraph!

- Course positioning: one sentence, and you find the point you want in a dozen seconds, with no speed-watching.
- One piece of footage, parsed once and reused again and again, building your personal video editing library.

Seekino gives you flexible video positioning, and recalls exactly the clips you need!

<p align="center">
  <img src="docs/architecture/fig01.png" width="960" alt="From remembering a moment to a verifiable clip" />
</p>

### 1.1. Retrieval support

- Can't find that iconic anime scene any more? — Upload an image or describe your vague impression, and Seekino locates it in seconds.
- Want to study a course without taking screenshots? — Ask questions while you watch; the AI knows where you are and what's confusing you.
- Need your own "crash course route" through a video course? — Give your question; the AI "browses" the video's outline and builds a route around it.
- Need editing footage but too lazy to scrub through it yourself? — Seekino provides MCP, so your Claude Code fetches the clips you need; you just give a vague impression and review.
- (TODO) Need references for editing, but past reference results can't be reused? — Seekino stores your past motion-effect analyses in depth and lets MCP "update your own editing reference experience", so your editing compounds and gets reused.

### 1.2. Parsing support

- Have to sit and watch a long video parse? — Upload once. Seekino transcribes the whole video, then parses it in parallel chunks; you can watch progress in Parse Tasks, retry failures, and search as soon as it's done.
- Parsing too expensive for long courses? — Every stage can use a different model (Kimi, Doubao, DeepSeek…), and every call records its tokens and estimated cost, so you always know what you're spending.
- A sentence cut in half and the cross-chunk dialogue lost? — Sliding-window Refine re-reads adjacent chunks together, fills dropped lines, removes duplicates and joins sentences across the cut.
- Can't tell who's speaking? — Whole-video speaker grouping, plus series background notes and voiceprints, identifies the speaker; when the evidence isn't there, it stays blank instead of guessing.
- Just want to know what this video or series is about first? — After parsing, Seekino writes a video outline and a series overview, so you see the structure first and then dive into what matters.
- (TODO) Courses, films and editing footage all parsed the same way, wasting money? — Seekino recognises the parse type automatically and controls cost per type; you can customise it too.

<details>
<summary>Screenshots (2026-10-25, local build, real footage)</summary>

![Seekino library: video series, analysis status and management controls](assets/screenshots/library.jpg)

![Seekino search: a natural-language question, retrieved clips and selection reasons](assets/screenshots/search-results.jpg)

![Seekino viewing Q&A: original video player and conversation panel](assets/screenshots/watch-chat.jpg)

</details>

## 2. Technical architecture

To offer public Q&A on a cloud server that casual users can try, and let heavy users self-host their own personal video knowledge base, Seekino builds the following system across parsing, retrieval and Q&A:

<p align="center">
  <img src="docs/architecture/fig03.png" width="960" alt="Seekino macro architecture" />
</p>

Every architecture figure is editable in [docs/architecture/seekino-figures.drawio](docs/architecture/seekino-figures.drawio) (12 tabs). Numbers marked TODO have not been measured on the current build.

### 2.1. Parsing

At the parsing layer, Seekino uses WhisperX for ASR by default, and you can swap in an ASR API.

To work across many scenarios and many model APIs, at a cost every user can afford, Seekino parses in parallel as shown below (fixed 60 s chunks, 10 at a time by default). It also adds a hook that recognises the parse type automatically and routes to three parse modes, each with its own cost control (user-customisable): course, script (scripted film and TV) and clip (editing footage). (TODO: the parse-type hook is not implemented yet.)

Parsing a single chunk loses the context of its neighbours, and fixed chunking brings a series of other problems. We solve them with sliding-window Refine (three adjacent chunks per window) and a tail Summary (a video outline and series overview written after parsing).

<p align="center">
  <img src="docs/architecture/fig06.png" width="960" alt="Fixed 60 s chunks parsed in parallel" />
</p>

<p align="center">
  <img src="docs/architecture/fig05.png" width="960" alt="Parse V2 stages" />
</p>

Results: Understanding for one 1-min chunk at 3 FPS (max height 720), on the course *Let's build GPT* (116 min, 117 chunks), medians of successful runs:

| Model | FTT | TT | Tokens (input / chunk) | Est. cost (/ chunk) |
| --- | ---: | ---: | ---: | ---: |
| Kimi k3.1-pro | 43.4 s | 320.2 s | ≈244k | TODO |
| Kimi k3.1-pro (low) | TODO | 105.5 s | ≈244k | TODO |
| Doubao Seed 2.1 Pro | TODO | TODO | TODO | TODO |
| DeepSeek Flash (non-thinking) | TODO | 31.8 s | TODO | ≈¥0.22 (incomplete usage records) |

Sources: [Kimi trace](docs/report/server/kimi-parse-trace-20261101.md), [DeepSeek Flash audit](docs/report/gateway/deepseek-flash-chunk-audit-20261103.md), [devlog](docs/dev/devlog.md).

The advantage of parallel over pure serial parsing: whatever the model, the time a long video takes stays under control.

Challenge: governing concurrent Refine conflicts —

We moved from pessimistic locking, where tools forcibly controlled the agent's output, to optimistic, non-blocking control. As soon as the first overlapping window commits, we interrupt the other windows and send them a new message: "Chunk 13, shared by window [12, 14], has been updated; the new window JSON is: {…}". This frees the agent from the endless fail-and-retry tool pipeline, and cuts time and token use:

- On the same 3 chunks, the Canvas-tool approach took 8 requests, 593.7 s and 314,671 input tokens; direct JSON took 1 request, 247.0 s and 11,307 input tokens, with the same 45 events and 16 dialogue lines.
- Before optimistic control, one full-course run had 49 commits rejected for conflicts after generation had already finished, and failed attempts took 57% of all Refine time. An interrupted request now exits in about 1 ms (local SSE test).
- Whole-course time and token savings of optimistic control on real models: TODO.

Today's models are capable enough to justify the optimistic view; so far only DS Flash non-thinking has produced unreliable output.

Gate: so that an agent no longer constrained by the old Canvas tools can't break the JSON structure, we added a JSON gate that checks the output against JSON and the schema. When it fails, the gate returns a guide message — "field `members.0.content` is wrong: xxx, please fix it" — together with the agent's own last full output, so it fixes only what's wrong. Problems the host can fix deterministically, such as unsorted times, are fixed directly with no extra model call. This keeps the final structure correct and valid.

<p align="center">
  <img src="docs/architecture/fig09.png" width="960" alt="Refine before and after" />
</p>

### 2.2. Building retrieval

To support multimodal input (gif, jpg, text…), we designed the stored vectors around it. The retrieval unit changed from the 1-min chunk to the Event (one action, shot or line), so a short line isn't diluted inside a one-minute vector. Each Event is stored on three channels: lexical (BM25), semantic (text vectors) and visual (frame vectors). (TODO: gif input is not supported yet; text and still images are.)

We use ||L<sub>∞</sub>|| to keep a retrieval result reliable — taking the maximum so it isn't filtered out at the end, and so plain weighted RRF fusion, with its fixed weights, can't push a hit that should match (a channel's hit 1) out of the top 10 after weighting. (TODO: not adopted yet. Fusion is currently weighted RRF with query-adaptive weights; this must be added.)

Before results are returned, each one is checked against the timeline of its parse task and content version: a vector hit is only a candidate, and only a checked hit is a fact.

<p align="center">
  <img src="docs/architecture/fig10.png" width="960" alt="Why the retrieval unit changed from chunk to Event" />
</p>

There are two common kinds of retrieval, top-down and bottom-up:

- Top-down: judge from the video outline, combined with the user's vague intent such as "I want to build a GPT; which points should I focus on mastering?", plan at the macro level how to help the user, then selectively go deeper into chunks through the outline to find specific Events as evidence.
- Bottom-up: locate the Event directly, then go back to its chunk and use the context to answer more fully.

The agent picks for each question: questions about the whole or about a route go top-down, and searches for a line or a scene go bottom-up.

### 2.3. Retrieval Q&A

So that Chat can tell "this message needs a search" from "this is just a reply", every turn passes through a query router and rewriter before it reaches the Supervisor:

- Route: Jev-1.14 decides whether the message is respond (just reply), retrieve (search) or mixed (both), with a 3 s timeout.
- Rewrite: for retrieve / mixed, DeepSeek Flash writes only the part to search as `retrieval_query`, keeping quoted lines verbatim and never guessing names, episodes or plot, with a 4 s timeout.
- Fallback: an 8 s total budget; any timeout, error or invalid output falls back to the raw message, so it can only help, never get in the way.

<p align="center">
  <img src="docs/architecture/fig07.png" width="960" alt="Query router and rewrite" />
</p>

The Supervisor then calls tools as needed:
- it delegates to the video-search agent to recall Events;
- it reads the video outline and the series overview;
- it understands the frames you're watching at your current playback position;
- it searches the web for background.

Finally it places each clip as a playable card under the text that cites it, snapped to sentence and Event boundaries; click it to go back to the original and check.

Answers stream as they're generated, with the reasoning folded away; if the first token hasn't arrived in 15 s, the client backs off and reconnects, up to 3 times.

Results: turn latency, pass rate and token cost before and after the router: TODO.

<details>
<summary>Problems we hit, effects, and metrics still to measure</summary>

![Problems to fixes](docs/architecture/fig08.png)

![Effect comparison](docs/architecture/fig11.png)

![Metrics to measure](docs/architecture/fig12.png)

</details>

## API integration

The web interface and external tools use the same service APIs. Your tools can connect to these capabilities:

| Integration | Existing endpoints |
| --- | --- |
| Upload videos and start analysis | `POST /api/uploads/create` → `PUT /api/uploads/segments/upload` → `POST /api/uploads/complete` |
| Track analysis tasks | `GET /api/jobs/video-parse/get`, `GET /api/jobs/video-parse/events` |
| Retrieve footage | `POST /api/search`, returning source clips, time ranges and evidence. |
| Find footage through Agent chat | `/api/agent/conversations/*` and `/api/agent/messages/*`, with ongoing conversations and streamed answers. |
| Let an external agent fetch clips (MCP) | Streamable HTTP at `/mcp` (local auth mode), tools `list_series`, `retrieve`, `read_evidence`. |

After starting the service, inspect requests and responses in the [OpenAPI docs](http://127.0.0.1:9621/docs). See [Server](server/README.md) for integration details. The current scope field is `tenantId`. Video time fields use integer milliseconds relative to the original video, including fields that retain legacy `*Seconds` names; follow the current contract when integrating.

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
- API docs: <http://127.0.0.1:5173/docs>
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
- API docs: <http://127.0.0.1:9621/docs>
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
