from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from redis.exceptions import ConnectionError as RedisConnectionError

from server.caller import elasticsearch as elasticsearch_caller, mysql as mysql_caller, redis as redis_caller
from server import caller
from server.config.auth import AuthConfig
from server.config.elasticsearch import ElasticsearchConfig
from server.database import DatabaseEnvironment


def test_mysql_client_sets_charset_pool_options_and_utc_session(monkeypatch):
    engine = Mock()
    create = Mock(return_value=engine)
    listen = Mock()
    monkeypatch.setattr(mysql_caller, "create_engine", create)
    monkeypatch.setattr(mysql_caller.event, "listen", listen)

    result = mysql_caller.create_database_engine(
        "mysql+pymysql://user:password@127.0.0.1/seekino_test",
        environment=DatabaseEnvironment.TEST, pool_recycle_seconds=1200,
    )

    assert result is engine
    url = create.call_args.args[0]
    assert url.database == "seekino_test" and url.query["charset"] == "utf8mb4"
    assert create.call_args.kwargs == {"pool_pre_ping": True, "pool_recycle": 1200}
    listen.assert_called_once_with(engine, "connect", mysql_caller._set_mysql_session_timezone_utc)
    cursor = Mock()
    listen.call_args.args[2](SimpleNamespace(cursor=lambda: cursor), object())
    cursor.execute.assert_called_once_with("SET time_zone = '+00:00'")
    cursor.close.assert_called_once()
    engine.dispose.assert_not_called()


def test_mysql_listener_registration_failure_disposes_allocated_engine(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(mysql_caller, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(mysql_caller.event, "listen", Mock(side_effect=RuntimeError("listener failed")))
    with pytest.raises(RuntimeError, match="listener failed"):
        mysql_caller.create_database_engine(
            "mysql+pymysql://user:password@127.0.0.1/seekino_test",
            environment=DatabaseEnvironment.TEST,
        )
    engine.dispose.assert_called_once()


def test_mysql_utc_session_failure_still_closes_cursor():
    cursor = Mock(execute=Mock(side_effect=RuntimeError("timezone failed")))
    with pytest.raises(RuntimeError, match="timezone failed"):
        mysql_caller._set_mysql_session_timezone_utc(SimpleNamespace(cursor=lambda: cursor), object())
    cursor.close.assert_called_once()


def test_mysql_admin_client_uses_validated_target_connection_without_database(monkeypatch):
    engine = Mock()
    create = Mock(return_value=engine)
    listen = Mock()
    monkeypatch.setattr(mysql_caller, "create_engine", create)
    monkeypatch.setattr(mysql_caller.event, "listen", listen)

    result = mysql_caller.create_admin_database_engine(
        "mysql+pymysql://user:password@127.0.0.1:3307/seekino_development",
        environment=DatabaseEnvironment.DEVELOPMENT,
    )

    assert result is engine
    url = create.call_args.args[0]
    assert url.database == "" and url.port == 3307 and url.username == "user"
    assert url.query["charset"] == "utf8mb4"
    assert create.call_args.kwargs == {
        "pool_pre_ping": True, "pool_recycle": 3600,
        "connect_args": {"connect_timeout": 5},
    }
    listen.assert_called_once_with(engine, "connect", mysql_caller._set_mysql_session_timezone_utc)
    mysql_caller.close_database_engine(engine)
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(("url", "environment", "message"), [
    ("mysql+pymysql://localhost/seekino", DatabaseEnvironment.PRODUCTION, "not allowed in production"),
    ("mysql+pymysql://localhost/seekino", DatabaseEnvironment.DEVELOPMENT, "development database name"),
    ("mysql+pymysql://localhost/", DatabaseEnvironment.TEST, "must select a database"),
    ("mysql+pymysql://localhost/seekino_test?charset=latin1", DatabaseEnvironment.TEST, "charset must be utf8mb4"),
    ("sqlite+pysqlite:///:memory:", DatabaseEnvironment.TEST, "requires a MySQL database URL"),
])
def test_mysql_admin_rejects_unsafe_connections_before_creating_client(monkeypatch, url, environment, message):
    create = Mock()
    monkeypatch.setattr(mysql_caller, "create_engine", create)
    with pytest.raises(ValueError, match=message):
        mysql_caller.create_admin_database_engine(url, environment=environment)
    create.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_elasticsearch_client_context_passes_timeout_and_closes_once(monkeypatch, fails):
    client = Mock()
    create = Mock(return_value=client)
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", create)
    config = ElasticsearchConfig("http://127.0.0.1:9200", "seekino_test", api_key="test-key")

    def command():
        with elasticsearch_caller.open_elasticsearch_client(config, request_timeout_seconds=3) as selected:
            assert selected is client
            client.close.assert_not_called()
            if fails:
                raise RuntimeError("command failed")

    if fails:
        with pytest.raises(RuntimeError, match="command failed"):
            command()
    else:
        command()
    create.assert_called_once_with(config.url, api_key="test-key", request_timeout=3)
    client.close.assert_called_once()


def test_elasticsearch_default_timeout_is_left_to_sdk(monkeypatch):
    client = Mock()
    create = Mock(return_value=client)
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", create)
    config = ElasticsearchConfig("http://127.0.0.1:9200", "seekino_test")
    assert elasticsearch_caller.create_elasticsearch_client(config) is client
    create.assert_called_once_with(config.url, api_key=None)


def test_event_store_borrows_client_without_initializing_index(monkeypatch):
    client = Mock()
    config = ElasticsearchConfig("http://127.0.0.1:9200", "seekino_test")
    store = elasticsearch_caller.build_event_elasticsearch_storage(
        client, config, environment=DatabaseEnvironment.TEST,
        semantic_dimensions=512, vision_dimensions=1024,
    )
    assert store._client is client
    assert store.index_name == "seekino_video_events_test_v3"
    client.assert_not_called()
    client.indices.exists.assert_not_called()
    client.close.assert_not_called()


def test_event_index_configuration_uses_seekino_variable_and_environment_guard(monkeypatch):
    monkeypatch.setenv("SEEKINO_ELASTICSEARCH_EVENT_INDEX", "seekino_custom_event_test")
    config = ElasticsearchConfig.from_environment(
        DatabaseEnvironment.TEST, url="http://127.0.0.1:9200", index_name="seekino_test",
    )
    assert config.event_index_name == "seekino_custom_event_test"
    with pytest.raises(ValueError, match="test component"):
        ElasticsearchConfig.from_environment(
            DatabaseEnvironment.TEST, url="http://127.0.0.1:9200", index_name="seekino_test",
            event_index_name="seekino_production_events",
        )


def test_elasticsearch_wrong_environment_fails_before_allocating_client(monkeypatch):
    create = Mock()
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", create)
    with pytest.raises(ValueError, match="test component"):
        elasticsearch_caller.build_elasticsearch_storage(
            ElasticsearchConfig("http://127.0.0.1:9200", "seekino"),
            environment=DatabaseEnvironment.TEST, semantic_dimensions=2, vision_dimensions=2,
        )
    create.assert_not_called()


def test_redis_local_mode_and_missing_cloud_url_do_not_allocate_client(monkeypatch):
    create = Mock()
    monkeypatch.setattr(redis_caller.Redis, "from_url", create)
    assert redis_caller.create_redis_client(AuthConfig()) is None
    with pytest.raises(ValueError, match="SEEKINO_AUTH_REDIS_URL"):
        redis_caller.create_redis_client(AuthConfig(mode="cloud", secret_key="x" * 32))
    create.assert_not_called()


def test_redis_caller_checks_readiness_and_owns_client_close(monkeypatch):
    client = Mock()
    create = Mock(return_value=client)
    monkeypatch.setattr(redis_caller.Redis, "from_url", create)
    config = AuthConfig(mode="cloud", secret_key="x" * 32,
                        redis_url="redis://127.0.0.1:16379/15", redis_timeout_seconds=12)
    assert redis_caller.create_redis_client(config) is client
    create.assert_called_once_with(config.redis_url, decode_responses=True,
                                   socket_timeout=12, socket_connect_timeout=12)
    client.ping.assert_called_once()
    client.close.assert_not_called()
    redis_caller.close_redis_client(client)
    client.close.assert_called_once()


@pytest.mark.parametrize("error", [RedisConnectionError("connection failed"), ValueError("unexpected setup error")])
def test_redis_failed_readiness_closes_allocated_client(monkeypatch, error):
    client = Mock(ping=Mock(side_effect=error))
    monkeypatch.setattr(redis_caller.Redis, "from_url", Mock(return_value=client))
    config = AuthConfig(mode="cloud", secret_key="x" * 32, redis_url="redis://127.0.0.1:16379/15")
    expected = RuntimeError if isinstance(error, RedisConnectionError) else ValueError
    with pytest.raises(expected, match="unavailable|unexpected setup error"):
        redis_caller.create_redis_client(config)
    client.close.assert_called_once()


def test_caller_init_keeps_local_redis_disabled_and_passes_storage_configuration(monkeypatch):
    order = []
    engine = SimpleNamespace(dispose=lambda: order.append("mysql"))
    client = SimpleNamespace(close=lambda: order.append("elasticsearch"))
    sessions, store = object(), object()
    create_database = Mock(return_value=engine)
    create_sessions = Mock(return_value=sessions)
    create_elasticsearch = Mock(return_value=client)
    create_store = Mock(return_value=store)
    create_redis = Mock(side_effect=AssertionError("local process must not create Redis"))
    monkeypatch.setattr(mysql_caller, "create_database_engine", create_database)
    monkeypatch.setattr(mysql_caller, "create_session_factory", create_sessions)
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", create_elasticsearch)
    monkeypatch.setattr(elasticsearch_caller, "ElasticsearchVideoChunkStore", create_store)
    monkeypatch.setattr(redis_caller.Redis, "from_url", create_redis)
    settings = SimpleNamespace(
        database_url="mysql+pymysql://localhost/seekino_test",
        environment=DatabaseEnvironment.TEST, database_pool_recycle_seconds=1200,
        auth=AuthConfig(),
        elasticsearch=ElasticsearchConfig("http://127.0.0.1:9200", "aggregate_test", api_key="test-key"),
    )

    resources = caller.init(settings, semantic_dimensions=512, vision_dimensions=1024)

    assert resources.engine is engine and resources.session_factory is sessions
    assert resources.elasticsearch_client is client and resources.elasticsearch_store is store
    assert resources.redis_client is None and order == []
    create_database.assert_called_once_with(
        settings.database_url, environment=settings.environment, pool_recycle_seconds=1200,
    )
    create_sessions.assert_called_once_with(engine)
    create_elasticsearch.assert_called_once_with(settings.elasticsearch.url, api_key="test-key")
    create_store.assert_called_once_with(
        client, index_name="aggregate_test", environment=DatabaseEnvironment.TEST,
        semantic_dimensions=512, vision_dimensions=1024, vision_enabled=True,
    )
    create_redis.assert_not_called()
    resources.close()
    resources.close()
    assert order == ["elasticsearch", "mysql"]


@pytest.mark.parametrize("failure", ["redis", "elasticsearch"])
def test_caller_init_releases_previous_resources_when_next_dependency_fails(monkeypatch, failure):
    order = []
    engine = SimpleNamespace(dispose=lambda: order.append("mysql"))
    redis_client = Mock(close=Mock(side_effect=lambda: order.append("redis")))
    client = SimpleNamespace(close=lambda: order.append("elasticsearch"))
    if failure == "redis":
        redis_client.ping.side_effect = RedisConnectionError("offline")
    monkeypatch.setattr(mysql_caller, "create_database_engine", Mock(return_value=engine))
    monkeypatch.setattr(mysql_caller, "create_session_factory", Mock(return_value=object()))
    monkeypatch.setattr(redis_caller.Redis, "from_url", Mock(return_value=redis_client))
    create_elasticsearch = Mock(return_value=client)
    monkeypatch.setattr(elasticsearch_caller, "Elasticsearch", create_elasticsearch)
    monkeypatch.setattr(
        elasticsearch_caller, "ElasticsearchVideoChunkStore",
        Mock(side_effect=RuntimeError("store setup failed")),
    )
    settings = SimpleNamespace(
        database_url="mysql+pymysql://localhost/seekino_test",
        environment=DatabaseEnvironment.TEST, database_pool_recycle_seconds=1200,
        auth=AuthConfig(mode="cloud", secret_key="x" * 32, redis_url="redis://localhost/15"),
        elasticsearch=ElasticsearchConfig("http://127.0.0.1:9200", "aggregate_test"),
    )

    with pytest.raises(RuntimeError, match="Redis is unavailable|store setup failed"):
        caller.init(settings, semantic_dimensions=512, vision_dimensions=512)

    assert order == (["redis", "mysql"] if failure == "redis" else ["elasticsearch", "redis", "mysql"])
    redis_client.close.assert_called_once()
    if failure == "redis":
        create_elasticsearch.assert_not_called()


def test_caller_rejects_missing_elasticsearch_before_creating_clients(monkeypatch):
    create = Mock()
    monkeypatch.setattr(mysql_caller, "create_database_engine", create)
    with pytest.raises(ValueError, match="SEEKINO_ELASTICSEARCH_URL"):
        caller.init(SimpleNamespace(elasticsearch=None), semantic_dimensions=512, vision_dimensions=512)
    create.assert_not_called()


def test_injected_caller_resources_close_remaining_dependencies_once_after_failure():
    order = []

    def fail_close():
        order.append("elasticsearch")
        raise RuntimeError("ES close failed")

    resources = caller.CallerResources(
        engine=SimpleNamespace(dispose=lambda: order.append("mysql")),
        elasticsearch_client=SimpleNamespace(close=fail_close),
        redis_client=SimpleNamespace(close=lambda: order.append("redis")),
    )
    with pytest.raises(RuntimeError, match="ES close failed"):
        resources.close()
    resources.close()
    assert order == ["elasticsearch", "redis", "mysql"]
