# 1. 问题与现状

工作分支 `fix/cloud-embedding-routing`。[PR #112](https://github.com/owariband/Seekino/pull/112)。

Cloud 在 GPU 关机时无法为 Chat 查询生成向量。MyGO 原问题出现 6 次失败工具块，396 秒后返回 3 段降级素材。课程查询出现 3 次失败工具块，耗时 123 秒。线上 `embed_query` 和索引阶段的 `embed_text` 使用同一个 llama.cpp 地址，GPU 转发当时拒绝连接。短查询 CPU 实测见 运维记录 `outputs/ops/cloud-runtime/query-embedding-cpu-20261111/verification.json`。

## 2. 影响因素、假设与评价指标

4C8G 云机限两核时短文本编码 P95 约 0.48 秒。假设本机可以承担查询及 GPU 离线时的解析索引。分别核对查询工具成功、证据回源、长文档编码、CPU 的资源上限与 GPU 优先顺序。

## 3. 改造方案与建模

在已有 llama.cpp source 允许声明有序的文档端点，缺省时沿用原地址。Cloud 查询指向本机，同一模型的文档端点依次为 GPU 转发、本机。故障切换只处理服务不可用；保留模型标识、维度、编码模板及取消合同。CPU 服务仅监听 API 的 loopback，运行参数与路由由部署选定。

## 4. 对照实验与结果分析

计划用 MockTransport 核对两种操作、首选健康、离线、响应错误与旧配置；在云机比较原问题的工具状态并记录实际耗时。

## 5. 试点验证与方案取舍

GPU 保持关机。以短查询和超过 4096 token 的文档验证 CPU，观察峰值内存。在 Cloud 游客入口重放两条原查询。GPU 在线优先顺序用受控端点验证。

## 6. 结果验收与闭环

部署前核对活跃请求与镜像版本。验收要求 GPU 离线时 Chat 成功检索、索引文档可回退、原索引可直接查询。将线上状态、耗时与未实测范围回填到本文。
