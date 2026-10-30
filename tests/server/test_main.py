from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from server import main as main_module
from server.api import app as app_module
from server.config.auth import AuthConfig
from server.config.logging import LoggingConfig
from server.config.sse import SSEConfig


def test_configuration_is_validated_before_callers_are_initialized(monkeypatch):
    prepare = Mock(side_effect=ValueError("invalid configuration"))
    initialize = Mock()
    monkeypatch.setattr(main_module, "prepare_service_configuration", prepare)
    monkeypatch.setattr(main_module.caller, "init", initialize)

    with pytest.raises(ValueError, match="invalid configuration"):
        main_module.init_application(object())

    initialize.assert_not_called()


@pytest.mark.parametrize("input_mode", ["multimodal", "text"])
def test_application_initializes_callers_before_business_services(monkeypatch, input_mode):
    events = []
    settings, gateway_config, resources, services = object(), object(), Mock(), Mock()
    parse_defaults = SimpleNamespace(embedding=SimpleNamespace(dimensions=1024, input_mode=input_mode))
    prepare = Mock(side_effect=lambda value: events.append("configuration") or (gateway_config, parse_defaults))
    initialize = Mock(side_effect=lambda *args, **kwargs: events.append("callers") or resources)
    build = Mock(side_effect=lambda *args, **kwargs: events.append("business") or services)
    monkeypatch.setattr(main_module, "prepare_service_configuration", prepare)
    monkeypatch.setattr(main_module.caller, "init", initialize)
    monkeypatch.setattr(main_module, "build_application_services", build)

    runtime = main_module.init_application(settings)

    assert events == ["configuration", "callers", "business"]
    prepare.assert_called_once_with(settings)
    initialize.assert_called_once_with(settings, semantic_dimensions=1024, vision_dimensions=1024,
                                       vision_enabled=input_mode == "multimodal")
    build.assert_called_once_with(settings, resources, gateway_config=gateway_config, parse_defaults=parse_defaults)
    assert runtime.resources is resources
    assert runtime.services is services
    resources.close.assert_not_called()
    runtime.close()


def test_business_construction_failure_releases_callers(monkeypatch):
    resources = Mock()
    parse_defaults = SimpleNamespace(embedding=SimpleNamespace(dimensions=1024, input_mode="multimodal"))
    monkeypatch.setattr(main_module, "prepare_service_configuration", Mock(return_value=(object(), parse_defaults)))
    monkeypatch.setattr(main_module.caller, "init", Mock(return_value=resources))
    monkeypatch.setattr(main_module, "build_application_services", Mock(side_effect=RuntimeError("business failed")))

    with pytest.raises(RuntimeError, match="business failed"):
        main_module.init_application(object())

    resources.close.assert_called_once_with()


@pytest.mark.parametrize("close_failure", [False, True])
def test_runtime_stops_business_before_callers_and_closes_once(close_failure):
    events = []

    def close_services():
        events.append("business")
        if close_failure:
            raise RuntimeError("shutdown failed")

    services = Mock(close=Mock(side_effect=close_services))
    resources = Mock(close=Mock(side_effect=lambda: events.append("callers")))
    runtime = main_module.ApplicationRuntime(services=services, resources=resources)
    runtime.start()
    services.start.assert_called_once_with()
    if close_failure:
        with pytest.raises(RuntimeError, match="shutdown failed"):
            runtime.close()
    else:
        runtime.close()
    runtime.close()

    assert events == ["business", "callers"]
    services.close.assert_called_once_with()
    resources.close.assert_called_once_with()


def test_http_binding_failure_releases_completed_runtime(monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(main_module, "init_application", Mock(return_value=runtime))
    settings = SimpleNamespace(auth=AuthConfig(), sse=SSEConfig(), allowed_origins=("*",))

    with pytest.raises(ValueError, match="explicit allowed origins"):
        main_module.create_http_application(settings=settings)

    runtime.close.assert_called_once_with()
    runtime.start.assert_not_called()


@pytest.mark.parametrize("start_failure", [False, True])
def test_managed_http_lifespan_releases_runtime_even_when_start_fails(monkeypatch, start_failure):
    events = []

    def start():
        events.append("start")
        if start_failure:
            raise RuntimeError("startup failed")

    runtime = Mock(start=Mock(side_effect=start), close=Mock(side_effect=lambda: events.append("close")))
    monkeypatch.setattr(main_module, "init_application", Mock(return_value=runtime))
    settings = SimpleNamespace(auth=AuthConfig(), sse=SSEConfig(), allowed_origins=())
    app = main_module.create_http_application(settings=settings)
    assert app.state.runtime is runtime
    assert app.state.services is runtime.services
    assert events == []

    if start_failure:
        with pytest.raises(RuntimeError, match="startup failed"):
            with TestClient(app):
                pytest.fail("failed startup must not enter the application")
    else:
        with TestClient(app):
            assert events == ["start"]

    assert events == ["start", "close"]


def test_legacy_app_factory_delegates_to_process_composition(monkeypatch):
    settings, app = object(), object()
    create = Mock(return_value=app)
    monkeypatch.setattr(main_module, "create_http_application", create)

    assert app_module.create_app(settings=settings) is app
    create.assert_called_once_with(settings=settings)


def test_process_entrypoint_loads_settings_once_and_passes_them_to_uvicorn(monkeypatch):
    settings = SimpleNamespace(environment="development", logging=LoggingConfig())
    load = Mock(return_value=settings)
    logging = Mock()
    run = Mock()
    create = Mock(return_value=object())
    monkeypatch.setattr(main_module.ServerSettings, "from_environment", load)
    monkeypatch.setattr(main_module, "configure_logging", logging)
    monkeypatch.setattr(main_module.uvicorn, "run", run)
    monkeypatch.setattr(main_module, "create_http_application", create)
    monkeypatch.setattr("sys.argv", ["server.api", "--host", "0.0.0.0", "--port", "19621", "--log-level", "DEBUG"])

    main_module.main()
    factory = run.call_args.args[0]
    factory()

    load.assert_called_once_with()
    create.assert_called_once_with(settings=settings)
    assert run.call_args.kwargs == {
        "factory": True, "host": "0.0.0.0", "port": 19621,
        "log_level": "debug", "log_config": None, "access_log": False,
        "timeout_graceful_shutdown": 5,
    }
    assert logging.call_args.args == ("api",)
    assert logging.call_args.kwargs["environment"] == "development"
    assert logging.call_args.kwargs["config"].level == "debug"


def test_http_shutdown_cancels_open_stream_then_closes_application(monkeypatch):
    events = []
    services = SimpleNamespace(
        mcp_retrieval=None,
        start=lambda: events.append("start"),
        close=lambda: events.append("business_close"),
    )
    runtime = main_module.ApplicationRuntime(
        services=services,
        resources=SimpleNamespace(close=lambda: events.append("callers_close")),
    )
    monkeypatch.setattr(main_module, "init_application", lambda _settings: runtime)
    settings = SimpleNamespace(auth=AuthConfig(), sse=SSEConfig(), allowed_origins=())
    app = main_module.create_http_application(settings=settings)

    async def exercise():
        first_body = asyncio.Event()
        never_disconnect = asyncio.Event()

        @app.get("/shutdown-stream")
        async def stream():
            async def body():
                try:
                    yield "event: messages\ndata: {}\n\n"
                    await never_disconnect.wait()
                finally:
                    events.append("stream_closed")

            return StreamingResponse(body(), media_type="text/event-stream")

        async def receive():
            await never_disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        # Run the real ASGI stream and Uvicorn shutdown without opening a socket.
        # A short deadline exercises the same cancellation path as the entrypoint.
        config = main_module.uvicorn.Config(
            app, lifespan="on", log_config=None, timeout_graceful_shutdown=0.01,
        )
        config.load()
        server = main_module.uvicorn.Server(config)
        server.servers = []
        server.lifespan = config.lifespan_class(config)
        await server.lifespan.startup()
        request = asyncio.create_task(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/shutdown-stream", "raw_path": b"/shutdown-stream",
            "query_string": b"", "root_path": "", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 2),
        }, receive, send))
        server.server_state.tasks.add(request)
        request.add_done_callback(server.server_state.tasks.discard)
        try:
            await asyncio.wait_for(first_body.wait(), timeout=2)
            assert not request.done()
            assert events == ["start"]
            await asyncio.wait_for(server.shutdown(), timeout=2)
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())
    assert events == ["start", "stream_closed", "business_close", "callers_close"]
