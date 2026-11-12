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

## Footage, visual references and private templates

- `list_series` — See which series are in the library.
- `retrieve` — Search clips with a description; returns the source video and time range.
- `browse_events` — Walk through what happens in an episode, in order.
- `read_evidence` — Read source context and supply start and end times in milliseconds to download an MP4 clip.

- `find_edit_templates` finds visual techniques with playable original clips and your own templates. `get_style_card` presents the whole video and its exemplars.
- `get_edit_template` reads exact or default revision files, sources and feedback. `list_edit_template_versions` lists revision history and lineage.
- `save_edit_template_version` saves a revision, `append_edit_template_note` adds feedback or a client-reported check, and `set_default_edit_template_version` selects a default.

## Use the editing skill and your personal Cloud library

Sign in to Cloud, open your account menu, and select **Copy personal MCP token**. Set `SEEKINO_MCP_TOKEN` to the copied token in the terminal where you launch the client. This credential can read published footage and use your personal template library. Guest tokens can still fetch published footage. A local connection saves to your local library.

Read `skill://seekino-editing-materials/SKILL.md` with MCP `resources/read`, or discover the guide and helper with `skills/list` and `skills/get`. To install the guide and the local cutout script for both Claude Code and Codex on the editing computer, run the following command.

```sh
npx skills add https://github.com/owariband/Seekino/tree/main/server/skills/seekino-editing-materials -a claude-code -a codex -g -y
```

## Next

- [What's next](./roadmap.md)
