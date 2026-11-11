# 1. 问题与现状

工作分支 `fix/cloud-embedding-routing`。[PR #112](https://github.com/owariband/Seekino/pull/112)。

Cloud 在 GPU 关机时无法为 Chat 查询生成向量。MyGO 原问题出现 6 次失败工具块，396 秒后返回 3 段降级素材。课程查询出现 3 次失败工具块，耗时 123 秒。线上 `embed_query` 和索引阶段的 `embed_text` 使用同一个 llama.cpp 地址，GPU 转发当时拒绝连接。短查询 CPU 实测见 运维记录 `outputs/ops/cloud-runtime/query-embedding-cpu-20261111/verification.json`。

## 2. 影响因素、假设与评价指标

4C8G 云机限两核时短文本编码 P95 约 0.48 秒。假设本机可以承担查询及 GPU 离线时的解析索引。分别核对查询工具成功、证据回源、长文档编码、CPU 的资源上限与 GPU 优先顺序。

## 3. 改造方案与建模

在已有 llama.cpp source 允许声明有序的文档端点，缺省时沿用原地址。Cloud 查询指向本机，同一模型的文档端点依次为 GPU 转发、本机。故障切换只处理服务不可用，保留模型标识、维度、编码模板及取消合同。

CPU Qwen 独立加入 Compose 默认网络，API 通过服务名 `http://embedding:18845` 调用，CPU 宿主机通过 `http://127.0.0.1:18845` 调用。Docker 只在宿主机回环发布该端口。Gateway 仅为 `llama_cpp_embedding` 接受这个内部 HTTP 服务根。既有 GPU 文档转发仍在首位。

## 4. 对照实验与结果分析

19 项 llama.cpp 专项和 71 项检索/索引测试通过。首选健康、拒绝连接、HTTP 503 和 429、协议错误、旧配置及索引身份均有覆盖。Gateway 全套首次运行 1735 项通过、8 项失败；7 项在未改基线上复现，1 项时序用例单跑通过。

2026-11-12 增加固定服务根后，llama.cpp 专项 40 项通过。四个相关测试文件共 239 项通过，3 项默认模型或 Supervisor 配置断言在改动前也失败。合并仓库中的 Cloud Compose 配置后，已核对 API 与 Qwen 共享默认网络、Qwen 没有 API 启动依赖、宿主端口仅绑定在回环。

## 5. 试点验证与方案取舍

GPU 关机。云机单槽 b12283 对 4129 token 合成文档返回 1024 维，67.78 秒，峰值 984 MiB。生产复用既有 Qwen 权重和 Compose 网络命名空间，部署限两核、1600 MiB、8192 token；长查询与长文档占用同一个槽位。排序成功分支在 MockTransport 中验证。

11 月 11 日首轮部署让 Qwen 加入 API 的网络命名空间。API 被 OOM 杀掉并重启后，两边的 `127.0.0.1` 指向不同的空间。21:44 的查询在关键词检索返回 200 后，因生成查询向量失败而返回 `RETRIEVAL_UNAVAILABLE`。22:01 重新创建两个容器，22:10 从 API 调用恢复，返回 1024 维。独立服务名用于消除这项重启依赖。

## 6. 结果验收与闭环

2026-11-11 在 Cloud main `fe614585` 镜像上附加本 PR 的三个 Gateway 文件。实际模型设置位于数据卷 `/data/config/cloud/models.yaml`。切换前活跃 Chat 和 Parse 均为 0。API 与 embedding 健康。API 内实测 `embed_query` 在本机返回 1024 维（0.282 秒）；`embed_text` 首选 GPU 转发拒绝连接后，本机返回 1024 维（0.085 秒）。真实 GPU 在线尚待下次开启时验证。

公网 MyGO 原题进入 RETRIEVING，并已完成 3 次 Qwen 编码。19:57 可用内存仅余 50,000 KiB，swap 剩 12 KiB。21:31:06 内核因整机 OOM 杀掉 API 的 Python，匿名 RSS 为 4,938,604 KiB，API 没有内存上限；Qwen 容器未重启。21:38 公网首页返回 200，SSH 恢复。已把这条测试消息从处理中标记为服务中断。课程题等待受控重放。[检索内存修复 PR #116](https://github.com/owariband/Seekino/pull/116) · [工作台](../../dev/workbench.html)

2026-11-12 从 CPU 宿主机按 Compose 服务名 `exec -T embedding` 调用容器内 `POST /v1/embeddings`，返回 HTTP 200 和 1024 维。独立网络上线后，需从宿主机和 API 各发一次真实查询，并在替换 API 前后核对 Qwen 的容器 ID 和启动时间。
