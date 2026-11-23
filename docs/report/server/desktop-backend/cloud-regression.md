# 桌面后端解耦后的云端回归

2026-11-23，在 macOS 26.5.1 arm64 上，用 Python 3.12.16、MySQL Community Server 8.4.7、Elasticsearch 8.19.22 和项目的 FFmpeg CLI 跑了服务端回归。**65 个使用真实 MySQL 或 Elasticsearch 的用例通过。** 其中一个从 HTTP 入口上传两段视频，经过 V2 Consumer 写入索引，再按命中时间下载片段，逐帧和原片对照。[后端 TRD](../../../design/03-product-reassessment/macos-desktop-backend-trd.md)

| 验证 | 观察到的结果 |
| --- | --- |
| MySQL 并发和事务 | 38 个用例通过。多连接争抢任务、租约过期后重新领取、重试与提交并发、重复上传和迁移等行为均符合断言。用例通过 TCP 连接新库。 |
| MySQL 解析结果恢复 | 16 个用例通过。覆盖内容损坏恢复、旧租约提交、结果迁移和 Summary 发布或回滚。用例通过私有 Unix socket 登录另一套临时实例，每个用例单独创建和删除测试库。 |
| Elasticsearch 发布与检索 | 10 个用例通过。覆盖多向量 top-k、授权范围过滤、版本、批量发布部分失败后的重试和删除。这里的协调器用例使用 SQLite 保存业务事实，索引读写均访问真实 ES。 |
| HTTP 导入到片段回看 | 1 个用例通过。MySQL 存素材和任务，ES 存检索索引。普通素材命中后下载原片的 100–850 毫秒区间，参考素材按命中窗口播放。对比两份片段的 H.264/AAC、时长、帧数和逐帧像素。素材设为私有后，原播放票据返回 404。 |
| HTTP、装配与存储单元回归 | 另有 539 个用例通过，验证 API、云端权限、服务装配、数据库结构和 ES 适配器。各测试沿用原有临时库或替身。 |

HTTP 用例使用 FastAPI/Starlette 的 TestClient，请求经过应用路由。测试固定生成模型和 embedding 的回复，云端授权部分用替身处理 Redis 限额。这组结果覆盖服务端协议与数据流。Docker 镜像内的部署、真实模型输出质量和外网 HTTP 链路仍待相应环境验收。[完整链路测试](../../../../tests/server/test_editing_mcp_flow.py)

## 环境与复跑

本次从 Oracle 和 Elastic 的官方发行包启动临时进程。MySQL 与 ES 使用新数据目录和随机分配的回环端口。Unix socket 专用 MySQL 关闭了 TCP。命令执行完毕后，测试进程关闭了这三套实例。[MySQL 8.4.7 归档包](https://cdn.mysql.com/archives/mysql-8.4/mysql-8.4.7-macos15-arm64.tar.gz) · [Elasticsearch 8.19.22 发行包](https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-8.19.22-darwin-aarch64.tar.gz)

复跑前给 `SEEKINO_TEST_DATABASE_URL` 指定隔离 MySQL 测试库，给 `SEEKINO_MIGRATION_TEST_DATABASE_URL`、`SEEKINO_EDITING_TEST_DATABASE_URL` 各指定一个空测试库，迁移库名须以 `_migration_test` 结尾。用 `SEEKINO_TEST_ELASTICSEARCH_URL` 指向隔离 ES。第四条命令使用 `SEEKINO_ISOLATED_MYSQL_SOCKET`，对应另一套仅在私有目录开放 socket 的临时 MySQL。测试按各自约定创建和回收数据。

```sh
python -m pytest -q tests/server/test_video_parse_mysql_integration.py tests/server/test_mysql_integration.py
python -m pytest -q tests/server/test_elasticsearch_integration.py
python -m pytest -q tests/server/test_editing_mcp_flow.py::test_one_mcp_from_upload_to_both_source_clips
python -m pytest -q tests/server/test_video_parse_results_mysql.py
python -m pytest -q tests/server/test_api.py tests/server/test_cloud_access.py tests/server/test_elasticsearch_storage.py tests/server/test_elasticsearch_events.py tests/server/test_database.py tests/server/test_bootstrap.py
```
