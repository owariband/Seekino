# 本机持久数据库

原生启动器默认将 MySQL 数据放在 `data/mysql/`，Elasticsearch 数据放在
`data/elasticsearch/`；可分别通过 `SEEKINO_MYSQL_DATA_DIR` 和
`SEEKINO_ELASTICSEARCH_DATA_DIR` 指定其他路径。媒体另存 `assets/library/`。

这两个数据库目录不属于缓存，停止服务或清理日志时应保留。底层数据库文件不进入 Git；
MySQL 使用一致性逻辑备份，ES 使用 snapshot 或依据完整的 MySQL 成果重建。
不要把运行中目录的随意复制、Git 提交或原片副本当作完整数据库备份。

可重新下载的二进制和归档放在 `outputs/tools/`、`outputs/downloads/`，
PID 与日志位于 `outputs/local-services/`。删除工具后必须重新安装才能启动，
不会因此删除本目录中的业务数据。已有旧数据目录不会被启动器自动迁走或覆盖。

启动与停止步骤见 [部署说明](../deploy/README.md)。
