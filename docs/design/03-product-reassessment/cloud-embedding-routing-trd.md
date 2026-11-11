# 1. 问题与现状

工作分支 `fix/cloud-embedding-routing`。[PR #112](https://github.com/owariband/Seekino/pull/112)。

Cloud 在 GPU 关机时无法为 Chat 查询生成向量。MyGO 原问题出现 6 次失败工具块，396 秒后返回 3 段降级素材。课程查询出现 3 次失败工具块，耗时 123 秒。线上 `embed_query` 和索引阶段的 `embed_text` 使用同一个 llama.cpp 地址，GPU 转发当时拒绝连接。短查询 CPU 实测见 运维记录 `outputs/ops/cloud-runtime/query-embedding-cpu-20261111/verification.json`。

## 2. 影响因素、假设与评价指标

4C8G 云机限两核时短文本编码 P95 约 0.48 秒。假设本机可以承担查询及 GPU 离线时的解析索引。分别核对查询工具成功、证据回源、长文档编码、CPU 的资源上限与 GPU 优先顺序。

## 3. 改造方案与建模

在已有 llama.cpp source 允许声明有序的文档端点，缺省时沿用原地址。Cloud 查询指向本机，同一模型的文档端点依次为 GPU 转发、本机。故障切换只处理服务不可用；保留模型标识、维度、编码模板及取消合同。CPU 服务仅监听 API 的 loopback，运行参数与路由由部署选定。

## 4. 对照实验与结果分析

19 项 llama.cpp 专项和 71 项检索/索引测试通过。首选健康、拒绝连接、HTTP 503 和 429、协议错误、旧配置及索引身份均有覆盖。Gateway 全套首次运行 1735 项通过、8 项失败；7 项在未改基线上复现，1 项时序用例单跑通过。

## 5. 试点验证与方案取舍

GPU 关机。云机单槽 b12283 对 4129 token 合成文档返回 1024 维，67.78 秒，峰值 984 MiB。生产复用既有 Qwen 权重和 Compose 网络命名空间，部署限两核、1600 MiB、8192 token；长查询与长文档占用同一个槽位。排序成功分支在 MockTransport 中验证。

## 6. 结果验收与闭环

2026-11-11 在 Cloud main `fe614585` 镜像上附加本 PR 的三个 Gateway 文件。实际模型设置位于数据卷 `/data/config/cloud/models.yaml`。切换前活跃 Chat 和 Parse 均为 0。API 与 embedding 健康。API 内实测 `embed_query` 在本机返回 1024 维（0.282 秒）；`embed_text` 首选 GPU 转发拒绝连接后，本机返回 1024 维（0.085 秒）。真实 GPU 在线尚待下次开启时验证。

游客 MyGO 原题已进入 RETRIEVING。读取答案时公网 HTTPS 与 SSH 同时超时，未取得终态；课程题未再次提交。整机故障原因有待内核和云监控证据。公网检索验收尚未完成。[问题记录](../../dev/problems.md#cloud-embedding-01--gpu-离线时检索丢失查询向量) · [工作台](../../dev/workbench.html)
