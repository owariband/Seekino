# Give your agent a library that remembers every scene

In our editing tests, Claude Code used Seekino to find the 1.3 seconds where Anon grits her teeth in episode 4, checked the source, and pulled the clip into its own workspace. You say what you need; it does the finding.

## Connect

1. **Get a Cloud read token**

   Copy from your Cloud site and run in your editing terminal. Guests can read published platform series. Run it again when the token expires.

   ```sh
   SEEKINO_CLOUD='https://your-seekino-cloud.example'
   SEEKINO_MCP_TOKEN=$(curl -fsS -X POST "$SEEKINO_CLOUD/api/auth/mcp-token" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])') && export SEEKINO_MCP_TOKEN
   ```

2. **Connect Claude Code to Cloud**

   Run this if you use Claude, then start Claude Code in this terminal.

   ```sh
   claude mcp add --transport http seekino_cloud "$SEEKINO_CLOUD/mcp" --header 'Authorization: Bearer ${SEEKINO_MCP_TOKEN}'
   ```

3. **Connect Codex to Cloud**

   Run this if you use Codex, then start Codex in this terminal.

   ```sh
   codex mcp add seekino_cloud --url "$SEEKINO_CLOUD/mcp" --bearer-token-env-var SEEKINO_MCP_TOKEN
   ```

4. **Also use local footage**

   Start the local API on your computer and add this second server to your client.

   ```sh
   claude mcp add --transport http seekino_local http://127.0.0.1:9621/mcp
   # or
   codex mcp add seekino_local --url http://127.0.0.1:9621/mcp
   ```

5. **Ask the way you'd brief a colleague**

   For example: "Find the shots of Anon gritting her teeth in Seekino, check the sources, and save the clips to clips/."

## Footage, visual references and shared series templates

- `list_series` — See which series are in the library.
- `retrieve` — Search clips with a description; returns the source video and time range.
- `browse_events` — Walk through what happens in an episode, in order.
- `read_evidence` — Read source context and supply start and end times in milliseconds to download an MP4 clip.

- `find_edit_templates` finds visual techniques with playable original clips and templates in accessible series. `get_style_card` presents the whole video and its exemplars.
- `get_edit_template` reads exact or default revision files, sources and feedback. `list_edit_template_versions` lists revision history and lineage.
- `save_edit_template_version` selects series_id and saves a revision, `append_edit_template_note` adds feedback or a client-reported check, and `set_default_edit_template_version` selects a default.

## Use the editing skill and shared series templates

Templates, versions and feedback belong to a series. Everyone who can view that series shares them, regardless of author or guest identity. Select series_id when creating a template. A new valid guest token reads the same public series templates.

Writes follow the existing series edit permission. Editors can copy an editing write token from the account menu. The compatible endpoint remains `POST /api/auth/mcp-token?scope=personal` with the current session and CSRF header. A local connection follows local series permissions.

Read `skill://seekino-editing-materials/SKILL.md` with MCP `resources/read`, or discover the guide and helper with `skills/list` and `skills/get`. To install the guide and the local cutout script for both Claude Code and Codex on the editing computer, run the following command.

```sh
npx skills add https://github.com/owariband/Seekino/tree/main/server/skills/seekino-editing-materials -a claude-code -a codex -g -y
```

## Shot library and motion references

The following MCP guides are Seekino's Chinese adaptations of pinned upstream versions, retaining their operational rules. The resources identify the source revision, original and adapted guide digests, and modifications. The install commands obtain the corresponding complete upstream packages.

MCP publishes `skill://motion-showreel/SKILL.md` for code-driven graphics, type, UI animation and supported subject-layer compositing. Its complete installation includes style references, starter assets, a timeline and local frame-rendering scripts. The upstream workflow previews in its workspace before export unless the user explicitly waives preview.

```sh
npx skills add https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel -a claude-code -a codex -g -y
```

MCP also publishes `skill://video-shotcraft/SKILL.md` with the original Apache-2.0 license. Install the complete package on the editing computer to consult its shot cards, demos, template and workbench.

```sh
npx skills add https://github.com/Vincentwei1021/video-shotcraft/tree/6700db2ba80572ebc777b99d72049679fed9840c -a claude-code -a codex -g -y
```

Seekino's editing guide offers camera motion references and suggestions. The agent writes velocity, position and deformation in the editing project's code according to the shot's intent, rhythm and source material. Bell-shaped speed and its S-shaped position integral can inform a slow departure, fast middle and settled arrival. Shape the duration, peak, symmetry and landing in the shot code, reusing or adapting upstream helpers where useful.

Author stretch, smear or blur when it suits the shot, deriving intensity from instantaneous speed and direction. Watch the result and adjust it. Production and rendering stay on the editing computer. If this library helps, consider starring [video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft).

motion-showreel comes from [ConardLi/garden-skills](https://github.com/ConardLi/garden-skills) under MIT. The MCP package retains its original copyright, license and pinned revision. It contributes animation and compositing references; follow the Seekino guide for source selection and cutting. If it helps, consider starring the upstream project.

## Next

- [What's next](./roadmap.md)
