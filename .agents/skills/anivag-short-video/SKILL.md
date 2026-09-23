---
name: anivag-short-video
description: Create finished self-contained offline HTML short films with HyperFrames, using AniVAG backed by the local Codex CLI for grounded source-video search, clip selection, subtitles, and material preparation. Use for montages, trailers, recaps, vertical social videos, or requests to turn AniVAG footage into a finished short.
---

# AniVAG Short Video

Create a finished short from AniVAG footage. The outer agent calls AniVAG, AniVAG's internal Agent runs through the local Codex CLI, and AniVAG returns grounded source clips. HyperFrames authors and plays the final film.

## Product contract

The default deliverable is **one self-contained offline HTML file**:

- It opens directly through `file://` in a normal browser.
- It runs the HyperFrames composition and its real timeline. It is not an MP4 wrapper.
- It embeds the HyperFrames Player, runtime, nested compositions, scripts, styles, fonts, images, source clips, audio, and provenance metadata required at playback.
- It must not depend on Studio, localhost, Node, a running process, a CDN, or network access.

Studio and `hyperframes preview` are internal authoring tools only. Never give the user a Studio URL as the product or stop at an editor/preview page.

Do not render an MP4 before packaging the HTML. Render an MP4 only when the user explicitly requests an additional video file; it is a separate optional export and never an input to the HTML.

## Required skills and tools

Load and follow both installed skills:

- `hyperframes`
- `hyperframes-cli`

Follow the HyperFrames router into its domain skills as needed. Use `hyperframes-creative` for the design spec, `hyperframes-animation` for motion, `general-video` for source-footage editing, and `hyperframes-audio` for mix treatment.

The local `codex` CLI must be installed and authenticated. Material prepared by another AniVAG Agent provider does not satisfy this skill.

HyperFrames is the required compositor. FFmpeg may inspect, trim, normalize, or transcode source material, but must not replace the HyperFrames composition.

## Workflow

1. Resolve the audience, duration, aspect ratio, and tone from the request. When omitted, use 15 seconds, 9:16, 1080×1920, and readable subtitles. Make reasonable editorial choices and deliver the film directly.
2. Initialize a HyperFrames project and pin one CLI version in its `package.json`. Use that version for lint, check, packaging, and any optional render.
3. Resolve the design spec through `hyperframes-creative`, then configure AniVAG's local Codex runtime.
4. Run `scripts/anivag_materials.py prepare` with `--expected-agent-model`; read the resulting `material-bundle.json` and its warnings.
5. Write a concise storyboard. Treat approved clips as the evidence set. Reorder or shorten them when the story benefits, record the reason, and never extend their source ranges.
6. Author the film as a HyperFrames composition. Preserve useful dialogue, keep text inside safe areas, and avoid padding with weak footage.
7. Use `npx hyperframes lint` while authoring. Before delivery, require `npx hyperframes check --snapshots` to exit successfully with no unresolved errors and inspect representative frames. A Studio preview may be used internally, but it is not a user approval gate or deliverable.
8. Package the checked composition with `scripts/package_offline_html.py`. Pass the material bundle through `--metadata` so provenance travels inside the HTML.
9. Open the output itself through `file://` with network disabled. Require the player `ready` and `assetsready` states, the expected duration and dimensions, working representative seeks, ready source media, no page or console errors, and zero HTTP(S) requests.
10. Return the offline HTML path, duration, aspect ratio, source clip count, material warnings, and the embedded material-bundle label. Do not report an internal Studio URL.

## Retrieve AniVAG material

### Required local Codex runtime

1. Verify authentication with `codex login status`.
2. Configure AniVAG and restart its API:

   ```yaml
   agent_model:
     provider: codex
     model: gpt-6-sol
     max_output_tokens: 8192
     request_timeout_seconds: 900
     max_concurrency: 5
   ```

3. Keep at least two Codex workers for one request. Supervisor, Query, Evaluator, and Chat share the executor, while Supervisor synchronously enters video search. `max_concurrency: 1` deadlocks the nested Query call. For `K` simultaneous Agent kicks, use at least `K + 1` workers; about `2K` lets inner roles progress concurrently.
4. Align provider, Agent execution/stage, and `ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS` timeouts. Raising one does not raise an earlier cap elsewhere.
5. Pass the exact configured model as `--expected-agent-model`. A mismatch is a failed material run.

List READY series:

```bash
python3 <skill-dir>/scripts/anivag_materials.py \
  --base-url http://127.0.0.1:9621 \
  --workspace-id local-default \
  list-series
```

Prepare grounded clips:

```bash
python3 <skill-dir>/scripts/anivag_materials.py \
  --base-url http://127.0.0.1:9621 \
  --workspace-id local-default \
  prepare \
  --series-id <series-id> \
  --query "Describe the story beats and footage needed" \
  --max-clips 6 \
  --expected-agent-model gpt-6-sol \
  --output <hyperframes-project>/anivag-materials
```

The bridge deliberately uses AniVAG's Agent API rather than raw search. It accepts only public video `renderBlocks` that AniVAG checked against retrieved evidence. A completed response with no video blocks means no usable material; refine the query once instead of bypassing the Agent.

## Package the offline film

```bash
python3 <skill-dir>/scripts/package_offline_html.py \
  <hyperframes-project> \
  --output <delivery-dir>/<short-name>.html \
  --title "<film title>" \
  --metadata <hyperframes-project>/anivag-materials/material-bundle.json
```

The packager resolves the HyperFrames distribution pinned by the project, refuses a version mismatch, fetches authored remote dependencies during the build, and embeds them. Remote dependencies are allowed during packaging only; the delivered HTML must pass the offline browser check.

## Evidence rules

- Keep every shot within its bundle `sourceStartMs` and `sourceEndMs`.
- Preserve `videoId`, `videoChunkId`, series, episode, selection reason, conversation, message, and `source.agentModel` in the embedded material bundle.
- Subtitle cues are already clipped and rebased to each local clip timeline.
- If `partial` is true, inspect and disclose every `source.warnings` entry. Use a clip only when the warnings do not undermine its requested dialogue, timing, or evidence range.
- Do not claim that generated titles, transitions, or graphic treatments came from the source footage.

## Delivery gate

- AniVAG reported the expected local Codex model.
- `check --snapshots` has no unresolved errors.
- The final artifact is a nonempty `.html` file, not a project editor or local URL.
- Forced-offline `file://` playback passes across the whole timeline.
- Every source shot remains traceable through embedded provenance.
- Dialogue, captions, music, and transitions do not obscure one another.
