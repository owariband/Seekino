# 原生 localhost Elasticsearch

无 Docker 的 macOS 可以在项目根目录执行：

```bash
bash deploy/elasticsearch/start.sh
```

首次下载与根目录 Compose 一致的 Elasticsearch 8.19.22 官方安装包，校验 SHA-512
后解压。Apple Silicon 使用 aarch64 包；包内自带 JDK，不需要系统 Java。
服务在后台运行，只监听 `127.0.0.1:9200`，单节点、512 MiB JVM heap、关闭认证。
这仅适用于本机开发，不可作为公网或生产配置。再次执行只复用已验证 ES 身份、
8.19 版本及 yellow/green 状态的服务；其它占用端口的进程不会被替换。
项目自身仍须验证索引与编码器合同。解压中断时，下次会重新校验并补齐程序安装，
不清理数据目录。程序先完整解压到暂存目录，再切换到正式程序目录；若已有未完成
安装，则保留为 `distribution/elasticsearch-8.19.22.previous-*`，不覆盖原生应用包。
启动托管复用项目既有的独立进程 session 方式，使用项目 Python（或系统 Python 3），
不会因调用它的终端命令结束而主动停止 ES。

本地 `.env.local` 连接配置：

```dotenv
ANIVAG_ELASTICSEARCH_URL=http://127.0.0.1:9200
ANIVAG_ELASTICSEARCH_INDEX=anivag_video_chunks_v2_development
ANIVAG_ELASTICSEARCH_INDEX_GENERATION=1
ANIVAG_TEST_ELASTICSEARCH_URL=http://127.0.0.1:9200
```

如设置 `ANIVAG_ELASTICSEARCH_PORT`，须同时调整以上 URL。
集成测试自行创建 UUID 命名的 `anivag_test_*` 索引并只清理自身索引，不使用开发索引。

```bash
curl --noproxy '*' http://127.0.0.1:9200/_cluster/health
uv run --no-sync --env-file .env.local pytest -q tests/server/test_elasticsearch_integration.py
```

安装包、程序、PID、数据与日志均在 `outputs/local-services/elasticsearch/`。
数据目录与程序目录分离；停止进程不删除数据。根目录 `./start.sh stop` 不管理此节点。
需要停止时，先核对该目录的 `elasticsearch.pid` 对应进程确为这个节点，再向该 PID
发送 TERM；不要批量终止其它 Java 或 Elasticsearch 进程。

该命令只启动 ES：不启动 MySQL、API、Consumer 或 Web，不修改 Parse execution mode，
不执行迁移、已有索引重建、历史数据切换或模型调用。运行真实 V2 上传前，还需检查
现有 MySQL Schema 与 V2 启动配置；已有数据不能直接套用仅支持空表的迁移脚本。

## 接入验证记录（2026-10-05 更新）

2026-10-04 实例通过独立健康复查与重复启动复用，脚本的非 ES / red / 超时 / green
四种响应回归均通过。随后真实接入发现 `ensure_index()` 报
`index field 'lexical_fields' differs`，6 个真实用例均在初始化失败：普通 mapping 回读
省略了与 `analyzer` 相同的 `search_analyzer`，字段默认值查询证明实际均为 `cjk`。

2026-10-05 已在 Server 校验中最小修复这一误判：仅对 text 字段缺省 search_analyzer
补实际 analyzer 的语义，再保留原有严格比较；不关闭校验、不修改 Engine 合同，
也不需要重建索引。新增 12 个直接场景，五文件定向集 `262 passed`；真实 ES 集
`6 passed`，包括实际查询、版本拒绝、部分索引失败恢复及删除恢复。

本次真实回归使用已安装 ES 8.19.22 在独立临时目录、`127.0.0.1:19200` 的测试节点，
不访问开发索引。项目正式启动校验用实际开发维度 1024 / 1024 连续通过两次，
自建空验证索引及 UUID 测试索引已清理。它不是业务服务恢复或整链启动：本轮开始时
`:9200` 未监听，未恢复该节点，未启动 MySQL / API / Consumer / Web 或调用真实模型。
真实 MySQL Schema、V2 mode 和产品上传仍待下一批检查；07B / 08A 尚未完整验收。

问题与证明边界见 [VP2-007](../../docs/design/potential_problems.md#vp2-007真实-es-mapping-默认分词器被-server-校验误拒绝)。

安装方法：[Elastic 官方 archive 文档](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/targz.html)。
