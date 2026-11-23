# LightRAG 怎么换存储

2026-11-23 核对 HKUDS/LightRAG 的 `main`，源码版本为 `ce3aa6bfe0982003c0684b408995945582681f05`。

LightRAG 启动时根据配置选存储类。以向量搜索为例，本地默认用 `NanoVectorDBStorage`。要让新库用 PostgreSQL 和 pgvector，配置 `LIGHTRAG_VECTOR_STORAGE=PGVectorStorage`，再提供 PostgreSQL 的连接信息。这个选项决定实体、关系和文档片段的向量存到哪里。 [默认配置](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/env.example#L1523-L1526) · [PostgreSQL 示例](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/docs/LightRAG-API-Server.md#L1008-L1017) · [连接配置要求](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/__init__.py#L128-L132)

1. API 读到 `LIGHTRAG_VECTOR_STORAGE`，把值作为 `vector_storage` 传给 `LightRAG`。 [读取环境变量](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/api/config.py#L640-L657) · [构造 LightRAG](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/api/lightrag_server.py#L2661-L2665)
2. `get_storage_class` 按名字找到类。默认类直接导入，`PGVectorStorage` 根据注册表从 `postgres_impl` 导入。接着用选中的类创建 `chunks_vdb` 等对象。 [按名字找类](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/factory.py#L17-L42) · [PostgreSQL 注册项](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/__init__.py#L174-L181) · [创建向量存储](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/lightrag.py#L2259-L2276)
3. 查文档片段时，业务代码调用 `chunks_vdb.query(..., top_k=...)`。本地类调用内存里的 NanoVectorDB，向量持久化到 JSON 文件。PostgreSQL 类执行向量距离查询，由数据库返回结果。两个类各自实现同一个 `query` 方法。 [业务调用](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/operate.py#L5869-L5875) · [本地文件路径](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/nano_vector_db_impl.py#L171-L179) · [本地查询](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/nano_vector_db_impl.py#L954-L991) · [PostgreSQL 查询](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/postgres_impl.py#L5474-L5519)

文档内容和缓存、向量、关系图、文档处理状态各有自己的存储选项，可以按用途选实现。这个版本还单列 `CONFIG_STORAGE`，用来保存服务设置和工作区的 embedding 基准。未指定时通常跟随 KV，KV 选 Redis 时配置改用 JSON。 [存储分类](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/lightrag/kg/__init__.py#L1-L69) · [配置存储说明](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/env.example#L1528-L1546)

官方文档要求在导入文档前选好后端。启动配置只决定程序打开哪个库。已有资料要在目标库使用，需要按适用的迁移流程搬运，或在新库从原文件重建。官方文档列出了几个专用迁移入口，分别用于旧 PostgreSQL 图后端迁到普通表、LLM 缓存和配置存储。 [已有数据的迁移边界](https://github.com/HKUDS/LightRAG/blob/ce3aa6bfe0982003c0684b408995945582681f05/docs/LightRAG-API-Server.md#L1020-L1023)

Seekino 可以借鉴启动时选实现的办法。服务端用 MySQL 保存视频和解析任务，用 Elasticsearch 查候选。桌面启动时选择 SQLite 和本地索引。导入和搜索调用对应的保存、查询方法，任务领取继续由各数据库实现自己的事务规则。已有素材库的迁移单独设计。
