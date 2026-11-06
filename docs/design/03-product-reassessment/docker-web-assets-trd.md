# 1. 问题与现状

需求：Docker 发布资源完整性与 CPU 服务器 main 源码对齐。记录日期：2026-11-06。
工作分支：`fix/docker-web-assets`；PR：首个设计提交推送后回填。
当前进度以 [管理工作台](../../dev/workbench.html) 的 `DEPLOY-01` 为准。

CPU 构建在网络代理恢复后通过 Python 依赖安装，但 Web 的 `npm run build` 报
`ENOENT /build/public/assets/seekino-logo.png`。`main` 的 `b82a1577` 与待发布提交
`560f3d51` 都包含该问题：Web 的 PNG/SVG Logo 是指向仓库根目录 `assets/` 的软链接，
`.dockerignore` 没有允许对应目标，且 Dockerfile 将 `web/` 平铺到 `/build`，丢失原有目录关系。
宿主机有完整仓库时能解析软链接，不能据此推定容器构建可用。

服务器当前是无 Git 元数据的源码副本。对照 `560f3d51` 检查的 486 个源码与构建文件中，
只有 Dockerfile 不同：服务器临时替换了 apt 源。API 镜像成功不能代替 main 的原样构建验收。
此前网络与构建原始记录保存在本机忽略目录 `outputs/ops/cpu-download-proxy/`。

本次只修复发布资源进入 Docker 构建的路径，验证 API/Web 镜像，并将 CPU 源码切换为
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
网络代理通过仓库外 Compose build 覆盖配置提供，不提交临时代理和 apt 镜像源替换。

依赖：[云部署 TRD](cloud-docker-deploy-trd.md)、[PR #57](https://github.com/owariband/Seekino/pull/57)。
#57 已进 dev，尚未进 main。先合入本修复到 dev，再用独立发布 PR 将 dev 树发布至 main，
保持 main 的 `.gitignore`。最终只将已合并的 main 提交部署为 CPU 正式源码；保留旧目录回退。

# 4. 对照实验与结果分析

计划在同一 CPU Docker 环境比较旧构建失败和修复后的构建。依赖缓存、代理和网络波动
只影响耗时，不作为本次性能收益；验收比较构建退出状态、资源哈希、HTTP 响应及源码一致性。
运行现有 `tests/server/test_docker_deployment.py`，验证初始化顺序、失败关闭和配置保留。
构建前后日志、文件清单和提交号保存在忽略的验收目录；执行后回填实测结果。

# 5. 试点验证与方案取舍

先在服务器独立候选目录构建和验证，不覆盖现有源码。Web 镜像用仅绑定 loopback 的
临时端口检查首页、PNG 和 SVG；不启动数据库，不调用收费模型。确认构建和资源正确后
准备发布 PR；合并后从 main 提交建立带 Git 元数据的源码目录，保留原目录及私有配置。
若 main 构建失败，停止切换；已有卷和数据不删除，旧目录保留供恢复。

# 6. 结果验收与闭环

初稿时尚未实施。需要分别记录：修复提交、相关测试、候选镜像验证、发布 PR/main SHA、
CPU 源码切换与最终镜像验证。服务启动、TLS、GPU 模型和真实业务效果单独报告，
未验证项不标通过。最终证据与剩余事项回填本节，状态只在管理工作台维护。
