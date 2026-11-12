# Cloud 游客剪辑额度验证（2026-11-12）

问题编号：`MCP-QUOTA-01`。关联[TRD](../../design/03-product-reassessment/cloud-editing-guest-quota-trd.md)、[工作台](../../dev/workbench.html)、[devlog](../../dev/devlog.md#devlog-cloud-editing-quota-20261112)。

检索和下载片段共用 `model_query` 的游客 IP 预算。`QUERY_LIMITED` 同时覆盖次数或并发不足；取到 3 段后失败不能理解成“最多 3 段”。本次复用既有环境配置，将部署样例的游客日次数设为 10,000、并发设为 4，保持登录用户配置、凭据签名、资料授权、证据版本与片段时长合同。

## 验证方法与结果

| 验证 | 条件与结果 | 结论边界 |
| --- | --- | --- |
| 配置装配 | 将样例中的游客配置送入 `AuthConfig.from_environment`，实际解析为 10,000 / 4，断言通过 | 只修改两项游客环境变量 |
| 既有配置回归 | `python -m pytest tests/server/test_auth.py -k 'configuration or redis_timeout' -q --tb=short`，3 passed、39 deselected | 没有运行完整后端回归；算法未改 |
| 已耗尽旧额度 | 从实际应用模块读取现有 `_ADMIT_QUERY`，在临时无网络 Redis 中种入计数 20。阈值 20 拒绝；改用阈值 10,000 接纳，计数变成 21 | 独立 Redis，无生产键写入；不是重置预算或更换身份 |
| 并发与拒绝不扣额 | 同一隔离 Redis 连续持有 4 个有效租约，第 5 个返回拒绝、重试 5 秒；计数保持 25 | 验证准入规则，不代表 4 路转码吞吐实测 |
| 公网 MCP 到 MP4 | 游客凭据、原生握手、`list_series`、`browse_events`、`read_evidence`，选择 MyGO 的 0–1,000 ms | 使用公开现有素材，不触发模型检索或重新解析 |
| 独立下载器 | 不携带 Cookie/Bearer，使用刚签发的片段 URL 连续下载 4 次；均 HTTP 200 | 每次 700,776 字节，1080p H.264/AAC，`ffprobe` 时长 1.001 秒 |

四次下载的端到端实测分别为 1.362、1.199、1.231、1.296 秒，包含取片、网络及 `ffprobe`。SHA-256 均为 `d8569cc3a5ba6bab8673b4c38cd7f8afd9768a068de8adf9420ccd3313da6dc4`。这四个样本用于证明调用链恢复，不用于推断延迟分布或长期带宽成本。[脱敏功能记录](evidence/cloud-editing-guest-quota-20261112/public-verification.json)

## 使用与未验证项

环境参数调整后 API 已重新加载并恢复健康；原签名密钥保持一致，已有会话不必因额度调整换 Cookie。签名取片地址最长有效 5 分钟，过期后对原证据重新调用 `read_evidence` 获取新地址。

完整剩余 9 镜头的续剪、长期成本、持续负载与多用户争用未验证。源片最长 60 秒、全局容量及其他身份权限继续按既有合同执行，不能将提高日次数解释成无限并发。

私有环境备份、部署源文件列表及详细运行快照只保留在服务器和本机忽略目录，公开记录不包含凭据或私有运行配置。
