<p align="center">
  <img src="assets/readme/seekino-idle.svg" width="96" alt="Seekino" />
</p>

<h1 align="center">Seekino</h1>

<p align="center">
  <strong>Ctrl+F for video.</strong><br />
  Search your footage with a line of dialogue, a description, or a screenshot. Open a match to play that moment in the original video.
</p>

<p align="center">
  <a href="https://seekino.hypn0s.cloud">Try Seekino Cloud</a> ·
  <a href="#self-hosting">Run your own library</a> ·
  <a href="https://seekino.hypn0s.cloud/docs">User guide</a> ·
  <a href="README.md">简体中文</a>
</p>

## Try a search

> Which episode explains self-attention, and at what minute?

*Neural Networks: Zero to Hero* runs for 14.6 hours across 8 episodes. Seekino can answer that across the series; inside the player, you can also jump straight to a line.

**Find a line inside the video.** Press `⌘F` / `Ctrl+F` (or click the magnifier in the player) and type a line you remember. Results carry their position in this episode; click one and playback lands on that second.

<p align="center">
  <img src="assets/readme/demo-find.gif" width="860" alt="Find in the watch page: press ⌘F, type most important, and land on episode 7 at 62:00" />
</p>

**Ask while you watch.** Open Kino beside the player and ask “What was that part about?” It knows your current position, and the answer includes a clip card you can replay.

<p align="center">
  <img src="assets/readme/demo-ask.gif" width="860" alt="Ask Kino beside the player and get a clip card for episode 7, 61:00–61:56" />
</p>

You can also ask the opening question on Cloud and let Seekino find it across episodes; the answer carries playable clip cards. The public courses and animation samples are available to guests.

## Features

- **Find a scene from what you remember.** Try “someone waiting at a door in the rain,” even if you don't know the episode. If you have a still image, send it to the assistant. Each clip includes the source video and time range so you can watch what happens before and after.
- **Choose which part of a course to watch.** Browse the series overview and episode outline, then pick a passage. Press `⌘F` in the player to find a line, or ask Kino beside it, without copying a timestamp or taking a screenshot.
- **Build a library of your own footage.** Group uploads by series and track their progress. You can retry a failed job or search an indexed video as often as you like. The usage page shows recorded tokens and API cost estimates where available.
- **Give your editing assistant the original clip.** Connect Claude Code or Codex through MCP to find footage, check the source and fetch the selected passage. With edit access to a series, you can also save motion effects code, source references and feedback there for the next edit.

More screenshots (2026-10-25): [Library](assets/readme/screenshots/library.jpg) · [Search results](assets/readme/screenshots/search-results.jpg) · [Ask while watching](assets/readme/screenshots/watch-chat.jpg)

## Self-hosting

Run Seekino locally to search your own videos. The default Docker setup includes Python and Node.js and uses API models, so you don't need local model weights.

1. Start Docker Desktop, or install Docker Engine with a recent Compose plugin. On Windows, select WSL 2 and Linux containers.
2. For a fresh install, clone the repository and copy the environment template. If you already have a `.env.docker`, edit it in place.

   ```bash
   git clone --depth 1 --single-branch --branch main https://github.com/owariband/Seekino.git
   cd Seekino
   cp deploy/docker/env.example .env.docker
   ```

3. Set `ARK_API_KEY` (Volcengine Ark) and `DEEPSEEK_API_KEY` in `.env.docker`. Check that your accounts can access the configured models. To use Jev to decide whether a question needs a search, also set `OPENROUTER_JEV_API_KEY`. Your library is stored locally; remote models receive the audio, video, frames or text they process and may charge your account.
4. From the repository root, start the services:

   ```bash
   docker compose --env-file .env.docker up --build -d --wait
   ```

Open [your local library](http://127.0.0.1:5173), create a series and upload a short video you know. When processing and indexing finish, search for a line you remember. Play the result and listen for it.

To stop, use `docker compose --env-file .env.docker stop`. Adding `-v` to `down` deletes the data volumes. See the [local setup guide](deploy/local-setup.md) for model selection, ports, backups, upgrades and Windows validation. Once the service is running, the [API reference](http://127.0.0.1:5173/api/docs) and [health check](http://127.0.0.1:5173/healthz) are available locally.

<a name="host-development-optional"></a>

<details>
<summary>Optional: run the API and web app on the host</summary>

Install Python 3.12, uv, Node.js 24+, npm, ffmpeg and ffprobe. On first setup, copy `.env.example` to `.env.local` and add your model credentials. Keep any existing file. From the repository root, run:

```bash
uv sync --locked
npm --prefix web ci
docker compose --env-file .env.local up -d --wait mysql elasticsearch
./start.sh
```

`./start.sh` uses a checked-out `main` worktree; use `./start.sh --branch dev` for a `dev` checkout. See the [deployment notes](deploy/README.md) for status and stop commands, a fully native install and recovery of existing data.

</details>

## Connect an editing assistant

To use public footage in Claude Code or Codex, follow the [Cloud MCP guide](https://seekino.hypn0s.cloud/docs/agents) to get a read-only guest token and add your client. For your own footage, start your local library and run the command for your client:

```sh
# Claude Code
claude mcp add --scope user --transport http seekino_local http://127.0.0.1:9621/mcp

# Codex
codex mcp add seekino_local --url http://127.0.0.1:9621/mcp
```

Then try: “Find the shot where Anon grits her teeth, check it against the source and save that clip to `clips/`.” You can connect both libraries. Cloud guests can read published public footage and fetch up to 60 seconds per clip.

## Architecture

When you add a video, Seekino indexes what happens and what is said along its timeline. At search time, it uses those events to find a passage and takes you back to the matching range in the source video.

![Seekino architecture: video processing and indexing, search, and playback from the source timeline](assets/readme/figures/fig-overview.png)

<!-- Add the technical blog link here once published. -->

## License and acknowledgements

This repository does not currently specify a license for Seekino as a whole. The following components are distributed under their own licenses:

- [video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) supplies shot recipes and production guidance. Its code and documentation use [Apache-2.0](server/skills/video-shotcraft/LICENSE).
- [motion-showreel](https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel) supplies scripts for animating graphics and compositing layers under the [MIT License](server/skills/motion-showreel/LICENSE).
- The Inter typeface uses the [SIL Open Font License 1.1](web/public/assets/fonts/Inter-LICENSE.txt).

Have a question or a suggestion? Open a [GitHub issue](https://github.com/owariband/Seekino/issues).
