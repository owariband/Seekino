# One command runs the full Seekino on your computer

The default setup runs in Docker and calls models over APIs: no GPU, no model downloads, and no Python or Node.js on your machine. You need Docker and two model keys.

1. **Install Docker**

   On Windows or macOS, install Docker Desktop (on Windows, choose WSL 2 / Linux containers). On Linux, install Docker Engine with the Compose plugin.

2. **Get the code**

   Clone the repository and enter it.

   ```sh
   git clone https://github.com/owariband/Seekino.git
   cd Seekino
   ```

3. **Add two keys**

   Copy the environment template, then set ARK_API_KEY and DEEPSEEK_API_KEY in .env.docker.

   ```sh
   cp deploy/docker/env.example .env.docker
   ```

4. **Start it**

   The first build needs a network connection to download images and dependencies.

   ```sh
   docker compose --env-file .env.docker up --build -d --wait
   ```

5. **Try a short video**

   Open http://127.0.0.1:5173, create a series and upload a few minutes of video. Once it's parsed, go back to the three steps in Quick start and try them on your own video.

## More setup

- [Local setup guide: Kimi / Codex, persistence, Windows (in Chinese)](https://github.com/owariband/Seekino/blob/main/deploy/local-setup.md)
- [Project README](https://github.com/owariband/Seekino/blob/main/README.en.md)
- [API docs (/api/docs on your machine)](http://127.0.0.1:5173/api/docs)

## Next

- [Connect Claude Code / Codex](./agents.md)
