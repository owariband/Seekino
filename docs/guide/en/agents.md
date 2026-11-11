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

## Four tools your agent can use

- `list_series` — See which series are in the library.
- `retrieve` — Search clips with a description; returns the source video and time range.
- `browse_events` — Walk through what happens in an episode, in order.
- `read_evidence` — Read source context and supply start and end times in milliseconds to download an MP4 clip.

## Next

- [What's next](./roadmap.md)
