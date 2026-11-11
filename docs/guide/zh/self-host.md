# 一条命令，在自己电脑上跑起完整的 Seekino

默认部署用 Docker，模型只调用 API：不需要显卡，不需要下载模型，也不需要在电脑上装 Python 或 Node.js。你只需要 Docker 和两把模型密钥。

1. **装好 Docker**

   Windows 和 macOS 装 Docker Desktop（Windows 选 WSL 2 / Linux containers）；Linux 装 Docker Engine 与 Compose 插件。

2. **拿到代码**

   克隆仓库并进入目录。

   ```sh
   git clone https://github.com/owariband/Seekino.git
   cd Seekino
   ```

3. **填两把密钥**

   复制环境模板，在 .env.docker 里填上 ARK_API_KEY 和 DEEPSEEK_API_KEY。

   ```sh
   cp deploy/docker/env.example .env.docker
   ```

4. **启动**

   第一次构建需要联网下载镜像和依赖。

   ```sh
   docker compose --env-file .env.docker up --build -d --wait
   ```

5. **传一个短视频试试**

   打开 http://127.0.0.1:5173，新建系列，上传一段几分钟的视频。解析完成后，回到“快速开始”的三步，用你自己的视频再试一次。

## 更多配置

- [本地配置指南：切换 Kimi / Codex、数据持久化、Windows](https://github.com/owariband/Seekino/blob/main/deploy/local-setup.md)
- [项目 README](https://github.com/owariband/Seekino)
- [API 文档（本机 /api/docs）](http://127.0.0.1:5173/api/docs)

## 接下来

- [接入 Claude Code / Codex](./agents.md)
