# 1. 问题与现状

需求：Docker 发布资源完整性与 CPU 服务器 main 源码对齐。记录日期：2026-11-06。
工作分支：`fix/docker-web-assets`；[PR #58](https://github.com/owariband/Seekino/pull/58)。
发布分支：`chore/release-main-docker-fix`；[main 发布 PR #59](https://github.com/owariband/Seekino/pull/59)。
当前进度以 [管理工作台](../../dev/workbench.html) 的 `DEPLOY-01` 为准。

CPU 构建在网络代理恢复后通过 Python 依赖安装，但 Web 的 `npm run build` 报
`ENOENT /build/public/assets/seekino-logo.png`。`main` 的 `b82a1577` 与待发布提交
`560f3d51` 都包含该问题：Web 的 PNG/SVG Logo 是指向仓库根目录 `assets/` 的软链接，
`.dockerignore` 没有允许对应目标，且 Dockerfile 将 `web/` 平铺到 `/build`，丢失原有目录关系。
宿主机有完整仓库时能解析软链接，不能据此推定容器构建可用。

服务器当前是无 Git 元数据的源码副本。对照 `560f3d51` 检查的 486 个源码与构建文件中，
只有 Dockerfile 不同：服务器临时替换了 apt 源。API 镜像成功不能代替 main 的原样构建验收。
此前网络与构建原始记录保存在本机忽略目录 `outputs/ops/cpu-download-proxy/`。

本次修复发布资源的构建路径，提供无需修改源码的 apt 镜像参数，验证 API/Web 镜像，并将 CPU 源码切换为
可追溯的 main 提交。保留已有私有配置、媒体、数据库卷和代理；不改变模型、权限和业务逻辑。
密钥配置、真实模型接入、数据迁移和公网业务验收不由镜像构建成功推定完成。

# 2. 影响因素、假设与评价指标

| 因素与假设 | 证据 | 验证方法 |
| --- | --- | --- |
| 构建上下文缺少软链接目标 | `.dockerignore` 排除根 assets | 仅允许两份已跟踪 Logo，构建最终 Web 镜像并读取文件 |
| 构建目录改变相对路径 | `WORKDIR /build` 后仅复制 web | 在镜像内保留 `/build/web` 和 `/build/assets` 关系 |
| 服务器临时修改掩盖发布状态 | Dockerfile 比发布提交多一行 apt 替换 | 使用 Git 提交的干净副本构建；最终工作树与远端 main 相同 |

| 指标 | 基线 | 目标与方法 |
| --- | --- | --- |
| Web 镜像构建 | Vite ENOENT 失败 | 正式 Dockerfile 构建成功 |
| PNG/SVG 发布资源 | PNG 阻断构建，SVG 有相同缺口 | 镜像内两文件 SHA-256 与 Git 源文件一致，HTTP 返回 200 |
| 构建输入隔离 | assets 整体不入上下文 | 仅放行 Logo，不包含 assets/library、模型、密钥和本机素材 |
| API 既有行为 | 服务器临时 Dockerfile 已构建 | 无服务器源码补丁的 API 构建通过；初始化相关测试通过 |
| 发布来源 | 无 .git 的待发布副本 | CPU Git HEAD 等于 GitHub main，tracked diff 为空 |

# 3. 改造方案与建模

在 Web 构建阶段将工作目录改为 `/build/web`，仍按 package/lock → npm ci → Web 源码
的顺序利用依赖缓存；复制允许的根资产到 `/build/assets`，将最终产物路径改为
`/build/web/dist`。在 `.dockerignore` 只放行 `assets/seekino-logo.png` 和
`assets/mascot/seekino-logo.svg` 及其父目录，避免把持久媒体变成构建输入。

保留源码软链接，不复制维护第二份 Logo，不在服务器手工替换 Logo。
网络代理通过仓库外 Compose build 覆盖配置提供。API 增加可选 `DEBIAN_MIRROR` 构建参数，
默认保留官方源；受限环境指定镜像主机名，免于服务器修改源码。不提交临时代理地址。

依赖：[云部署 TRD](cloud-docker-deploy-trd.md)、[PR #57](https://github.com/owariband/Seekino/pull/57)。
#57 已进 dev，尚未进 main。先合入本修复到 dev，再用独立发布 PR 将 dev 树发布至 main，
保持 main 的 `.gitignore`。最终只将已合并的 main 提交部署为 CPU 正式源码；保留旧目录回退。

# 4. 对照实验与结果分析

计划在同一 CPU Docker 环境比较旧构建失败和修复后的构建。依赖缓存、代理和网络波动
只影响耗时，不作为本次性能收益；验收比较构建退出状态、资源哈希、HTTP 响应及源码一致性。
运行现有 `tests/server/test_docker_deployment.py`，验证初始化顺序、失败关闭和配置保留。

2026-11-06 构建上下文实测：只允许 assets 父目录仍会包含其余子目录；第一次探针捕获
`assets/library/DO_NOT_PACKAGE.txt`。随后显式排除 assets 子树，再放行两份目标；第二次
Docker scratch 导出只含 PNG/SVG，测试标记未进入。此失败与修正保留为输入隔离证据。

同日修复候选 b38d32ed 的 Web 镜像构建成功；临时 loopback HTTP 首页、PNG、SVG 均为 200，
两份图片 SHA-256 与源码一致。API 官方 apt 源虽能取得 InRelease，小请求可达，
但直连大索引超过 3 分钟未推进，代理下载也有中断；据此补充可选镜像构建参数，
避免依赖服务器临时源码补丁。

最终实现候选 `090782a0a8ed652424d276be888408eaf51f974b` 使用同一 CPU 的 Docker 构建，
通过仓库外 Compose build 参数指定 `DEBIAN_MIRROR=mirrors.tencentyun.com` 及下载代理，
源码无补丁，API/Web 构建整体退出码为 0。验收结果如下：

| 检查 | 实测结果 |
| --- | --- |
| API 镜像 | `sha256:4ff28940143c944f717215c902055595eb00bc7642bb4a3ef5cbb451e1684366` |
| Web 镜像 | `sha256:17a2005b2d44a157607cf84b4d81ebd3f0d5ee12760f5b376a051e4a61c215b7` |
| Web 临时 HTTP | 首页、`/assets/seekino-logo.png`、`/assets/seekino-logo.svg` 均为 200 |
| PNG SHA-256 | `3cebaabe9a01bd439ba65e0534d9fdd0646be207574a0d95975a8c1a09beb2c9`，与源码一致 |
| SVG SHA-256 | `4e5c67beeaa20033fcea456e779d60bac238cfba8cf6dbf46f55677fc194e220`，与源码一致 |
| API 无网络运行 | `fastapi` / `openai` 导入通过；运行环境无 HTTP_PROXY / HTTPS_PROXY / DEBIAN_MIRROR |
| Docker 初始化回归 | `tests/server/test_docker_deployment.py`，6 passed，最终一次 2.05 秒 |
| 构建输入隔离 | scratch 导出仅两份 Logo，`assets/library/DO_NOT_PACKAGE.txt` 未进入 |

构建日志、资源 HTTP/hash JSON 和源码清单保存在本机忽略目录
`outputs/verification/docker-web-assets-20261106/`；服务器原始日志保存在
`~/seekino-download-proxy/`。本轮没有测试业务 API、真实模型或公网域名。

# 5. 试点验证与方案取舍

先在服务器独立候选目录构建和验证，不覆盖现有源码。Web 镜像用仅绑定 loopback 的
临时端口检查首页、PNG 和 SVG；不启动数据库，不调用收费模型。确认构建和资源正确后
准备发布 PR；合并后从 main 提交建立带 Git 元数据的源码目录，保留原目录及私有配置。
若 main 构建失败，停止切换；已有卷和数据不删除，旧目录保留供恢复。

# 6. 结果验收与闭环

2026-11-06：上述候选的 API/Web 实机构建、资源 HTTP/hash、构建输入隔离及 6 项初始化
回归均已通过。发布通过 #58 → dev → #59 → main 进行，并保留 main 的 `.gitignore`。
候选验收时 main 合并及 CPU 正式源码切换尚待执行，不能将候选验证写成部署完成。
切换时必须记录远端 main SHA、CPU HEAD、tracked diff、最终镜像及旧源码备份路径。
服务启动、TLS、GPU 模型和真实业务效果单独报告，未验证项不标通过；状态只在管理工作台维护。

2026-11-06 23:00（UTC+8）补充：#58 已合入 dev，#59 经本次明确授权合入 main。
CPU 的 `/home/ubuntu/seekino` 已切换为带 Git 元数据的 `main` 分支，HEAD 与远端均为
`0282fcb0da58a4292007732c931823dc11720a22`，`git status --porcelain` 为空；main 原有
`.gitignore` 保持不变。从该 main 的干净预备目录构建 API/Web，退出码 0，镜像 ID 与第 4 节
候选完全一致；最终 main 的临时 HTTP、图片哈希与 API 无网络导入再次通过。

生产 Compose 用隔离的占位配置成功解析，并检查 production/cloud 身份、数据库不发布端口、
API 仅绑定 loopback；该检查没有使用真实凭据，也没有初始化数据库。两镜像保留
`main-0282fcb0` 标签，默认 `seekino-api:local` / `seekino-web:local` 已指向对应镜像。
旧源码（含 `.release` 旧提交标记）保留在 `/home/ubuntu/seekino-before-main-20261106-150017Z`，
旧 API 镜像保留 `seekino-api:before-main-20261106-150017Z` 标签，数据未删除。

最终原始证据在上述本机验收目录的 `main-verification.json`、`main-deployment.json`、
`build-main.log`；同名原件位于服务器 `~/seekino-download-proxy/`。本需求的源码来源、
构建与发布资源验收完成。服务器还没有私有 `.env.cloud`，本轮未启动业务容器，
未复制 API/SMTP 凭据；公网、数据库初始化和 GPU/真实模型链路未验收。
