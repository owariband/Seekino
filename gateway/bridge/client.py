from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import suppress
from threading import Lock, Thread, current_thread
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AllowedOutcome,
    CancelElicitationResponse,
    ClientCapabilities,
    CreateElicitationResponse,
    CreateFormRequestElicitationRequest,
    CreateFormSessionElicitationRequest,
    DeniedOutcome,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    ElicitationFormRequestMode,
    ElicitationFormSessionMode,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResumeSessionResponse,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
)

_logger = logging.getLogger(__name__)


class BridgeClient:
    """Keep an ACP subprocess alive across sessions and synchronous host calls.

    Synchronous callbacks run outside the I/O thread and should return quickly
    with an awaitable or concurrent Future for human interactions. Updates get
    a ``replay`` keyword; form callbacks get their resolved ``session_id``.
    """

    def __init__(
        self,
        command: str,
        *,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        on_update: Callable[..., Any] | None = None,
        on_permission: Callable[..., Any] | None = None,
        on_elicitation: Callable[..., Any] | None = None,
        startup_timeout: float = 30,
        cancel_timeout: float = 10,
        shutdown_timeout: float = 2,
    ) -> None:
        if not command.strip():
            raise ValueError("ACP command must not be empty")
        if min(startup_timeout, cancel_timeout, shutdown_timeout) <= 0:
            raise ValueError("ACP timeouts must be greater than zero")
        self._command, self._args = command, tuple(args)
        self._env, self._cwd = dict(env or {}), cwd
        self._on_update, self._on_permission = on_update, on_permission
        self._on_elicitation = on_elicitation
        self._startup_timeout, self._cancel_timeout = startup_timeout, cancel_timeout
        self._shutdown_timeout = shutdown_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="acp-bridge", daemon=True)
        self._lock = Lock()
        self._startup: Future | None = None
        self._closed = False
        self._connection: Any = None
        self._process: Any = None
        self._context: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._update_locks: dict[str, asyncio.Lock] = {}
        self._updates: dict[str, set[asyncio.Task]] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._interactions: dict[str, set[asyncio.Task]] = {}
        self._request_sessions: dict[int | str, str] = {}
        self._replaying: set[str] = set()
        self._initialize_response: InitializeResponse | None = None
        self._stderr_bytes = 0

    @property
    def initialize_response(self) -> InitializeResponse | None:
        return self._initialize_response

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def stderr_bytes(self) -> int:
        return self._stderr_bytes

    def __enter__(self) -> BridgeClient:
        self.open()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def open(self) -> InitializeResponse:
        if current_thread() is self._thread:
            raise RuntimeError("Use an asynchronous callback; do not block the ACP I/O thread")
        with self._lock:
            if self._closed:
                raise RuntimeError("ACP bridge is closed")
            if self._startup is None:
                self._thread.start()
                self._startup = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
            startup = self._startup
        try:
            return startup.result(timeout=self._startup_timeout + self._shutdown_timeout * 3)
        except BaseException:
            self.close()
            raise

    async def _open(self) -> InitializeResponse:
        self._context = spawn_agent_process(
            self, self._command, *self._args, env=self._env, cwd=self._cwd,
            transport_kwargs={"shutdown_timeout": self._shutdown_timeout, "limit": 4 * 1024 * 1024},
            use_unstable_protocol=True, observers=[self._observe],
        )
        self._connection, self._process = await self._context.__aenter__()
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        capabilities = ClientCapabilities()
        if self._on_elicitation is not None:
            capabilities.elicitation = ElicitationCapabilities(form=ElicitationFormCapabilities())
        self._initialize_response = await asyncio.wait_for(
            self._connection.initialize(
                protocol_version=PROTOCOL_VERSION, client_capabilities=capabilities,
                client_info=Implementation(name="seekino", version="0.1.0"),
            ), timeout=self._startup_timeout,
        )
        if self._initialize_response.protocol_version != PROTOCOL_VERSION:
            raise RuntimeError("ACP agent returned an unsupported protocol version")
        return self._initialize_response

    async def _drain_stderr(self) -> None:
        if self._process.stderr is not None:
            while chunk := await self._process.stderr.read(4096):
                # Do not retain or log stderr: adapters can include prompts or credentials.
                self._stderr_bytes += len(chunk)

    def _call(self, coroutine: Any) -> Any:
        with self._lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("ACP bridge is closed")
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    def new_session(
        self, *, cwd: str, mcp_servers: Sequence[Any] = (),
        additional_directories: Sequence[str] = (),
    ) -> NewSessionResponse:
        self.open()
        return self._call(self._connection.new_session(
            cwd=cwd, mcp_servers=list(mcp_servers),
            additional_directories=list(additional_directories) or None,
        ))

    def load_session(
        self, session_id: str, *, cwd: str, mcp_servers: Sequence[Any] = (),
        additional_directories: Sequence[str] = (),
    ) -> LoadSessionResponse | ResumeSessionResponse:
        self.open()
        return self._call(self._load_session(session_id, cwd, mcp_servers, additional_directories))

    async def _load_session(
        self, session_id: str, cwd: str, mcp_servers: Sequence[Any], additional_directories: Sequence[str],
    ) -> LoadSessionResponse | ResumeSessionResponse:
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            capabilities = self._initialize_response.agent_capabilities
            self._replaying.add(session_id)
            try:
                params = {
                    "session_id": session_id, "cwd": cwd, "mcp_servers": list(mcp_servers),
                    "additional_directories": list(additional_directories) or None,
                }
                if capabilities and capabilities.session_capabilities and capabilities.session_capabilities.resume:
                    response = await self._connection.resume_session(**params)
                elif capabilities and capabilities.load_session:
                    response = await self._connection.load_session(**params)
                else:
                    raise RuntimeError("ACP agent does not support loading or resuming sessions")
                updates = tuple(self._updates.get(session_id, ()))
                if updates:
                    await asyncio.gather(*(asyncio.shield(task) for task in updates))
                return response
            finally:
                self._replaying.discard(session_id)

    def set_session_mode(self, session_id: str, mode_id: str) -> SetSessionModeResponse:
        self.open()
        return self._call(self._connection.set_session_mode(session_id=session_id, mode_id=mode_id))

    def set_config_option(self, session_id: str, config_id: str, value: str | bool) -> SetSessionConfigOptionResponse:
        self.open()
        return self._call(self._connection.set_config_option(
            session_id=session_id, config_id=config_id, value=value,
        ))

    def prompt(self, session_id: str, prompt: str | Sequence[Any]) -> PromptResponse:
        self.open()
        content = [text_block(prompt)] if isinstance(prompt, str) else list(prompt)
        return self._call(self._prompt(session_id, content))

    async def _prompt(self, session_id: str, content: list[Any]) -> PromptResponse:
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            task = asyncio.current_task()
            self._active[session_id] = task
            try:
                return await self._connection.prompt(session_id=session_id, prompt=content)
            finally:
                self._active.pop(session_id, None)
                self._cancel_interactions(session_id)

    def cancel(self, session_id: str) -> None:
        self.open()
        try:
            self._call(self._cancel(session_id))
        except TimeoutError:
            # A timed-out cancellation leaves remote file actions ambiguous.
            self.close()
            raise

    async def _cancel(self, session_id: str) -> None:
        active = self._active.get(session_id)
        if active is None:
            return
        async with asyncio.timeout(self._cancel_timeout):
            await self._connection.cancel(session_id=session_id)
            self._cancel_interactions(session_id)
            await asyncio.gather(asyncio.shield(active), return_exceptions=True)

    def _cancel_interactions(self, session_id: str) -> None:
        for task in tuple(self._interactions.get(session_id, ())):
            task.cancel()

    async def _callback(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(callback):
            result = callback(*args, **kwargs)
        else:
            handoff_lock, returned = Lock(), []
            cancelled = False

            def invoke() -> Any:
                result = callback(*args, **kwargs)
                with handoff_lock:
                    if cancelled:
                        if isinstance(result, Future):
                            result.cancel()
                        elif inspect.iscoroutine(result):
                            result.close()
                    else:
                        returned.append(result)
                return result

            try:
                result = await asyncio.to_thread(invoke)
            except asyncio.CancelledError:
                with handoff_lock:
                    cancelled = True
                    if returned:
                        if isinstance(returned[0], Future):
                            returned[0].cancel()
                        elif inspect.iscoroutine(returned[0]):
                            returned[0].close()
                raise
        if isinstance(result, Future):
            result = await asyncio.wrap_future(result)
        while inspect.isawaitable(result):
            result = await result
        return result

    async def session_update(self, session_id: str, update: Any, **_kwargs: Any) -> None:
        replay = session_id in self._replaying
        task = asyncio.current_task()
        pending = self._updates.setdefault(session_id, set())
        pending.add(task)
        try:
            async with self._update_locks.setdefault(session_id, asyncio.Lock()):
                if self._on_update is not None:
                    await self._callback(self._on_update, session_id, update, replay=replay)
        finally:
            pending.discard(task)
            if not pending:
                self._updates.pop(session_id, None)

    async def _interaction(
        self, scope_session_id: str, callback: Callable[..., Any], request: Any, **kwargs: Any,
    ) -> Any:
        task = asyncio.current_task()
        waiting = self._interactions.setdefault(scope_session_id, set())
        waiting.add(task)
        try:
            return await self._callback(callback, request, **kwargs)
        finally:
            waiting.discard(task)
            if not waiting:
                self._interactions.pop(scope_session_id, None)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any,
    ) -> RequestPermissionResponse:
        option_id = None
        offered_ids = {option.option_id for option in options}
        if session_id not in self._active:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if self._on_permission is not None:
            try:
                option_id = await self._interaction(session_id, self._on_permission, RequestPermissionRequest(
                    session_id=session_id, tool_call=tool_call, options=options, field_meta=kwargs or None,
                ))
            except asyncio.CancelledError:
                pass
        if isinstance(option_id, str) and option_id in offered_ids:
            return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option_id))
        if option_id is not None:
            _logger.warning("ACP permission callback returned an unavailable option")
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> CreateElicitationResponse:
        if self._on_elicitation is None:
            return CancelElicitationResponse(action="cancel")
        if isinstance(mode, ElicitationFormSessionMode):
            request = CreateFormSessionElicitationRequest(
                message=message, mode="form", field_meta=kwargs or None,
                session_id=mode.session_id, tool_call_id=mode.tool_call_id, requested_schema=mode.requested_schema,
            )
            session_id = mode.session_id
            if session_id not in self._active:
                return CancelElicitationResponse(action="cancel")
        elif isinstance(mode, ElicitationFormRequestMode):
            request = CreateFormRequestElicitationRequest(
                message=message, mode="form", field_meta=kwargs or None,
                request_id=mode.request_id, requested_schema=mode.requested_schema,
            )
            session_id = self._request_sessions.get(mode.request_id)
            if session_id is None:
                return CancelElicitationResponse(action="cancel")
        else:
            return CancelElicitationResponse(action="cancel")
        try:
            return await self._interaction(session_id, self._on_elicitation, request, session_id=session_id)
        except asyncio.CancelledError:
            return CancelElicitationResponse(action="cancel")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        pass

    def _observe(self, event: StreamEvent) -> None:
        message = event.message
        request_id = message.get("id")
        if event.direction == StreamDirection.OUTGOING and message.get("method") == "session/prompt":
            self._request_sessions[request_id] = message["params"]["sessionId"]
        elif event.direction == StreamDirection.INCOMING and request_id is not None and "method" not in message:
            self._request_sessions.pop(request_id, None)

    def close(self) -> None:
        if current_thread() is self._thread:
            raise RuntimeError("Close the ACP bridge outside its I/O thread")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._startup is not None
        if not started:
            self._loop.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop).result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()

    async def _close(self) -> None:
        for session_id in tuple(self._interactions):
            self._cancel_interactions(session_id)

        async def release() -> None:
            try:
                if self._connection is not None:
                    await self._connection.close()
            finally:
                if self._context is not None and self._process is not None:
                    await self._context.__aexit__(None, None, None)

        async def watch_process() -> None:
            if self._process is None:
                return
            try:
                await asyncio.wait_for(self._process.wait(), timeout=self._shutdown_timeout)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=self._shutdown_timeout)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        self._process.kill()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=self._shutdown_timeout)
                    except TimeoutError:
                        # A known returncode means the child was reaped; inherited
                        # stdio descriptors can still hold old asyncio waiters.
                        if self._process.returncode is None:
                            raise RuntimeError("ACP subprocess did not exit after kill") from None

        cleanup = asyncio.create_task(release())
        watchdog = asyncio.create_task(watch_process())
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=self._shutdown_timeout * 3)
        except ProcessLookupError:
            if self._process is None or self._process.returncode is None:
                raise
        except TimeoutError:
            # SDK stdin drain can stall before its process-exit timeout begins.
            await watchdog
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        finally:
            await asyncio.gather(watchdog, return_exceptions=True)
            if self._process is not None and self._process.returncode is not None:
                # Python 3.12 Process has no public pipe-close API. ACP can leave
                # inherited stdio open after reaping; release only this process's pipes.
                self._process._transport.close()
                await asyncio.sleep(0)
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)
